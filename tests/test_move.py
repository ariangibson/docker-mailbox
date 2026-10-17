"""Moving messages between folders, against the in-process IMAP mock.

The mock records every command, so these tests assert on what went over the
wire: MOVE when the server has it, otherwise COPY + STORE \\Deleted + a UID
EXPUNGE naming only the copied messages.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import imap_mock
import pytest
from fastapi.testclient import TestClient

from mailboxd import imap_client, ops
from mailboxd.config import ImapConfig, MailboxConfig, load_config
from mailboxd.imap_client import ImapError, mark_spam, move_messages
from mailboxd.server import create_app

Factory = Callable[[dict[int, bytes]], "tuple[imap_mock.Sink, str, int]"]

MOVE = "IMAP4rev1 UIDPLUS MOVE"
UIDPLUS = "IMAP4rev1 UIDPLUS"


def _cfg(host: str, port: int) -> ImapConfig:
    return ImapConfig(host=host, port=port, tls="none", username="u@example.com", password="p")


def _inbox(*uids: int) -> dict[int, bytes]:
    return {uid: imap_mock.sample_message(f"Message {uid}") for uid in uids}


def _server(imap_factory: Factory, caps: str, *uids: int) -> tuple[imap_mock.Sink, ImapConfig]:
    sink, host, port = imap_factory(_inbox(*uids))
    sink.capabilities = caps
    return sink, _cfg(host, port)


# --------------------------------------------------------------------------
# MOVE vs COPY fallback
# --------------------------------------------------------------------------


def test_move_uses_move_and_reports_each_uid(imap_factory: Factory) -> None:
    sink, cfg = _server(imap_factory, MOVE, 1, 2, 3)
    out = move_messages(cfg, ["1", "3", "9"], "Keep")
    assert out["to_folder"] == "Keep"
    assert out["from_folder"] == "INBOX"
    assert out["method"] == "MOVE"
    assert out["moved"] == 2
    assert out["results"] == [
        {"uid": "1", "status": "moved"},
        {"uid": "3", "status": "moved"},
        {"uid": "9", "status": "not_found"},
    ]
    assert "warnings" not in out
    assert set(sink.messages) == {2}
    assert len(sink.folders["Keep"]) == 2
    verbs = sink.verbs()
    assert verbs.count("UID MOVE") == 2
    assert not {"UID COPY", "UID STORE", "UID EXPUNGE", "EXPUNGE"} & set(verbs)
    # The source is opened read-write, the destination quoted.
    assert any(c.split(" ", 1)[1].startswith('SELECT "INBOX"') for c in sink.commands)
    assert any('UID MOVE 1 "Keep"' in c for c in sink.commands)


def test_copy_fallback_expunges_only_the_moved_uids(imap_factory: Factory) -> None:
    sink, cfg = _server(imap_factory, UIDPLUS, 1, 2, 3)
    # Another client flagged message 2 \Deleted without expunging it yet.
    sink._deleted.add((id(sink.messages), 2))
    out = move_messages(cfg, ["1", "3"], "Keep")
    assert out["method"] == "COPY"
    assert [r["status"] for r in out["results"]] == ["moved", "moved"]
    assert "warnings" not in out
    verbs = sink.verbs()
    assert verbs.count("UID COPY") == 2 and verbs.count("UID STORE") == 2
    assert "EXPUNGE" not in verbs
    assert any(c.endswith("UID EXPUNGE 1,3") for c in sink.commands)
    # Message 2 is still there for its own client to deal with.
    assert set(sink.messages) == {2}
    assert len(sink.folders["Keep"]) == 2


def test_without_uidplus_the_originals_are_flagged_but_not_expunged(
    imap_factory: Factory,
) -> None:
    sink, cfg = _server(imap_factory, "IMAP4rev1", 1, 2)
    out = move_messages(cfg, ["1"], "Keep")
    assert out["results"] == [{"uid": "1", "status": "moved"}]
    assert "flagged \\Deleted" in out["warnings"][0]
    assert not {"EXPUNGE", "UID EXPUNGE"} & set(sink.verbs())
    assert (id(sink.messages), 1) in sink._deleted
    assert len(sink.folders["Keep"]) == 1


def test_a_refused_uid_is_an_error_and_the_rest_still_move(imap_factory: Factory) -> None:
    sink, cfg = _server(imap_factory, MOVE, 1, 2, 3)
    sink.refuse = {2}
    out = move_messages(cfg, ["1", "2", "3"], "Keep")
    assert out["moved"] == 2
    assert out["results"][1] == {"uid": "2", "status": "error", "error": "refused by the mock"}
    assert set(sink.messages) == {2}


def test_a_refused_copy_is_not_flagged_or_expunged(imap_factory: Factory) -> None:
    sink, cfg = _server(imap_factory, UIDPLUS, 1, 2)
    sink.refuse = {2}
    out = move_messages(cfg, ["1", "2"], "Keep")
    assert [r["status"] for r in out["results"]] == ["moved", "error"]
    assert any(c.endswith("UID EXPUNGE 1") for c in sink.commands)
    assert not any(c.endswith("UID STORE 2 +FLAGS (\\Deleted)") for c in sink.commands)
    assert set(sink.messages) == {2}


def test_a_missing_destination_fails_every_uid(imap_factory: Factory) -> None:
    sink, cfg = _server(imap_factory, MOVE, 1)
    sink.missing = {"Nowhere"}
    out = move_messages(cfg, ["1"], "Nowhere")
    assert out["moved"] == 0
    assert out["results"][0]["status"] == "error"
    assert "TRYCREATE" in out["results"][0]["error"]
    assert set(sink.messages) == {1}


def test_from_folder_is_honoured(imap_factory: Factory) -> None:
    sink, cfg = _server(imap_factory, MOVE)
    sink.folders["Later"] = _inbox(5)
    out = move_messages(cfg, ["5"], "INBOX", from_folder="Later")
    assert out["from_folder"] == "Later"
    assert out["moved"] == 1
    assert sink.folders["Later"] == {}


# --------------------------------------------------------------------------
# Input checks
# --------------------------------------------------------------------------


@pytest.mark.parametrize("uids", [[], ["1:*"], ["1,2"], ["5\n"], ["*"], ["abc"]])
def test_bad_uids_are_refused_before_connecting(imap_factory: Factory, uids: list[str]) -> None:
    sink, cfg = _server(imap_factory, MOVE, 1, 2)
    with pytest.raises(ImapError):
        move_messages(cfg, uids, "Keep")
    assert sink.commands == []


def test_duplicate_uids_are_moved_once(imap_factory: Factory) -> None:
    sink, cfg = _server(imap_factory, MOVE, 7)
    out = move_messages(cfg, ["7", "07", "7"], "Keep")
    assert out["results"] == [{"uid": "7", "status": "moved"}]
    assert sink.verbs().count("UID MOVE") == 1


def test_too_many_uids_are_refused(imap_factory: Factory) -> None:
    sink, cfg = _server(imap_factory, MOVE, 1)
    with pytest.raises(ImapError, match="at most"):
        move_messages(cfg, [str(n) for n in range(1, imap_client.MAX_MOVE + 2)], "Keep")
    assert sink.commands == []


@pytest.mark.parametrize("bad", ["Arch\r\nive", "x\x00", " "])
def test_bad_destination_is_refused(imap_factory: Factory, bad: str) -> None:
    _, cfg = _server(imap_factory, MOVE, 1)
    with pytest.raises(ImapError):
        move_messages(cfg, ["1"], bad)


def test_moving_into_the_same_folder_is_refused(imap_factory: Factory) -> None:
    sink, cfg = _server(imap_factory, MOVE, 1)
    with pytest.raises(ImapError, match="already in"):
        move_messages(cfg, ["1"], "inbox")
    assert "UID MOVE" not in sink.verbs()


# --------------------------------------------------------------------------
# junk / trash / archive
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "lines", "expected"),
    [
        # SPECIAL-USE wins over a folder that merely has the name.
        ("junk", ['() "/" "Junk"', '(\\Junk) "/" "Bulk Mail"'], "Bulk Mail"),
        ("JUNK", ['(\\Junk) "/" "Bulk Mail"'], "Bulk Mail"),
        ("trash", ['(\\HasNoChildren \\Trash) "/" "Deleted Items"'], "Deleted Items"),
        ("archive", ['(\\Archive) "/" "All Archived"'], "All Archived"),
        # No attribute: Junk first, then Spam, matched case-insensitively.
        ("junk", ['() "/" "Spam"', '() "/" "junk"'], "junk"),
        ("junk", ['() "/" "Spam"'], "Spam"),
        ("trash", ['() "/" "Trash"'], "Trash"),
        ("archive", ['() "/" "Archive"'], "Archive"),
        # Dovecot-style namespace: everything lives under INBOX.
        ("junk", ['() "." "INBOX.Junk"'], "INBOX.Junk"),
        # A \Noselect placeholder can't hold messages.
        ("junk", ['(\\Noselect) "/" "Junk"', '() "/" "Spam"'], "Spam"),
    ],
)
def test_roles_resolve_to_the_mailboxs_own_folder(
    imap_factory: Factory, role: str, lines: list[str], expected: str
) -> None:
    sink, cfg = _server(imap_factory, MOVE, 1)
    sink.list_lines = ['(\\HasNoChildren) "/" "INBOX"', *lines]
    out = move_messages(cfg, ["1"], role)
    assert out["to_folder"] == expected
    assert out["moved"] == 1
    assert list(sink.folders) == [expected]


def test_special_use_is_requested_when_plain_list_omits_it(imap_factory: Factory) -> None:
    sink, cfg = _server(imap_factory, f"{MOVE} SPECIAL-USE LIST-EXTENDED", 1)
    sink.list_lines = ['() "/" "INBOX"', '() "/" "Junk"', '() "/" "Quarantine"']
    sink.extended_list_lines = ['() "/" "INBOX"', '(\\Junk) "/" "Quarantine"']
    assert move_messages(cfg, ["1"], "junk")["to_folder"] == "Quarantine"


def test_a_role_with_no_folder_is_an_error(imap_factory: Factory) -> None:
    sink, cfg = _server(imap_factory, MOVE, 1)
    # Nested somewhere other than under INBOX: somebody's own folder.
    sink.list_lines = ['() "/" "INBOX"', '() "/" "Projects/Archive"']
    with pytest.raises(ImapError, match="no archive folder"):
        move_messages(cfg, ["1"], "archive")
    assert "UID MOVE" not in sink.verbs()


def test_mark_spam_moves_to_junk(imap_factory: Factory) -> None:
    sink, cfg = _server(imap_factory, MOVE, 1, 2)
    sink.list_lines = ['() "/" "INBOX"', '(\\Junk) "/" "Spam"']
    out = mark_spam(cfg, ["2"])
    assert out["to_folder"] == "Spam"
    assert out["results"] == [{"uid": "2", "status": "moved"}]
    assert set(sink.messages) == {1}


# --------------------------------------------------------------------------
# Reading never sets \Seen
# --------------------------------------------------------------------------


def test_reading_messages_only_ever_peeks(imap_factory: Factory) -> None:
    sink, cfg = _server(imap_factory, UIDPLUS, 1)
    mb = MailboxConfig(name="shop", imap=cfg)
    ops.get_message(mb, "1")
    ops.get_message(mb, "1", reader=True)
    imap_client.fetch_raw(cfg, "1")
    fetches = [c for c in sink.commands if " UID FETCH " in c.upper()]
    assert fetches
    for command in fetches:
        items = command.upper().split(" ", 4)[4]
        assert "RFC822)" not in items and "BODY[" not in items, command


# --------------------------------------------------------------------------
# HTTP + MCP surface
# --------------------------------------------------------------------------


def _http(tmp_path: Path, host: str, port: int) -> TestClient:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"""
mailboxes:
  - name: shop
    imap:
      host: {host}
      port: {port}
      tls: none
      username: u@example.com
      password: "p"
  - name: smtp_only
    smtp:
      host: {host}
      port: 1
      tls: none
      username: ""
      password: ""
      from_address: u@example.com
""")
    return TestClient(create_app(load_config(str(cfg))))


def test_move_and_spam_routes(imap_factory: Factory, tmp_path: Path) -> None:
    sink, host, port = imap_factory(_inbox(1, 2, 3))
    sink.capabilities = MOVE
    sink.list_lines = ['() "/" "INBOX"', '(\\Junk) "/" "Spam"', '(\\Trash) "/" "Bin"']
    c = _http(tmp_path, host, port)

    r = c.post("/mailboxes/shop/messages/move", json={"uids": ["1", "8"], "to_folder": "trash"})
    assert r.status_code == 200, r.text
    assert r.json()["to_folder"] == "Bin"
    assert [x["status"] for x in r.json()["results"]] == ["moved", "not_found"]

    r = c.post("/mailboxes/shop/messages/spam", json={"uids": ["2"]})
    assert r.status_code == 200, r.text
    assert r.json()["to_folder"] == "Spam"
    assert set(sink.messages) == {3}

    # The fixed `move` segment doesn't shadow per-message routes.
    assert c.get("/mailboxes/shop/messages/3").status_code == 200


def test_move_routes_map_errors_to_statuses(imap_factory: Factory, tmp_path: Path) -> None:
    sink, host, port = imap_factory(_inbox(1))
    sink.capabilities = MOVE
    c = _http(tmp_path, host, port)
    move = "/mailboxes/shop/messages/move"
    assert c.post(move, json={"uids": [], "to_folder": "x"}).status_code == 422
    assert c.post(move, json={"uids": ["1"]}).status_code == 422
    assert c.post(move, json={"uids": ["1"], "to_folder": ""}).status_code == 422
    too_many = [str(n) for n in range(imap_client.MAX_MOVE + 1)]
    assert c.post(move, json={"uids": too_many, "to_folder": "x"}).status_code == 422
    assert c.post(move, json={"uids": ["1:*"], "to_folder": "x"}).status_code == 502
    assert c.post(move, json={"uids": ["1"], "to_folder": "archive"}).status_code == 502
    assert c.post("/mailboxes/shop/messages/spam", json={}).status_code == 422
    assert c.post("/mailboxes/nope/messages/spam", json={"uids": ["1"]}).status_code == 404
    assert c.post("/mailboxes/smtp_only/messages/spam", json={"uids": ["1"]}).status_code == 409
    assert set(sink.messages) == {1}


_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _mcp(client: TestClient, request_id: int, method: str, params: dict[str, Any]) -> Any:
    response = client.post(
        "/mcp",
        headers=_MCP_HEADERS,
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    )
    assert response.status_code == 200, response.text
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line.removeprefix("data: "))["result"]
    return response.json()["result"]


def test_mcp_move_and_mark_spam(imap_factory: Factory, tmp_path: Path) -> None:
    sink, host, port = imap_factory(_inbox(1, 2, 3))
    sink.capabilities = MOVE
    sink.list_lines = ['() "/" "INBOX"', '() "/" "Spam"']
    with _http(tmp_path, host, port) as client:
        _mcp(
            client,
            1,
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "mailboxd-test", "version": "1"},
            },
        )
        tools = {t["name"]: t for t in _mcp(client, 2, "tools/list", {})["tools"]}
        assert tools["move_message"]["inputSchema"]["required"] == ["mailbox", "uids", "to_folder"]
        assert tools["mark_spam"]["inputSchema"]["required"] == ["mailbox", "uids"]

        def call(request_id: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            result = _mcp(client, request_id, "tools/call", {"name": name, "arguments": arguments})
            return json.loads(result["content"][0]["text"])

        moved = call(3, "move_message", {"mailbox": "shop", "uids": ["1"], "to_folder": "Keep"})
        assert moved["to_folder"] == "Keep" and moved["moved"] == 1

        spammed = call(4, "mark_spam", {"mailbox": "u@example.com", "uids": ["2", "9"]})
        assert spammed["to_folder"] == "Spam"
        assert [r["status"] for r in spammed["results"]] == ["moved", "not_found"]

        refused = call(5, "move_message", {"mailbox": "shop", "uids": ["1:*"], "to_folder": "X"})
        assert "invalid uid" in refused["error"]
        assert set(sink.messages) == {3}
