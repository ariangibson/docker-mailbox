"""Drafts, reply and Sent-copy behaviour against the in-process IMAP/SMTP mocks.

The IMAP mock records every command it receives and every APPEND it accepts,
so these tests assert on what actually went over the wire: which folder was
picked, which flags were set, and that an update appends before it deletes.
"""

from __future__ import annotations

import email
import json
from pathlib import Path
from typing import Any, Callable

import imap_mock
import pytest
from fastapi.testclient import TestClient

from mailboxd import imap_client, ops, smtp_client
from mailboxd.config import ImapConfig, MailboxConfig, SmtpConfig, load_config
from mailboxd.imap_client import ImapError, save_draft, save_sent
from mailboxd.message import FromNotAllowedError
from mailboxd.server import create_app

Factory = Callable[[dict[int, bytes]], "tuple[imap_mock.Sink, str, int]"]

LOGIN = "inbox@mydomain.example"
UIDPLUS = "IMAP4rev1 UIDPLUS"
RAW = b"From: a@x.com\r\nMessage-ID: <draft-1@x.com>\r\nSubject: d\r\n\r\nbody\r\n"

SPECIAL_USE_LIST = [
    '(\\HasNoChildren) "/" "INBOX"',
    '(\\HasNoChildren \\Drafts) "/" "Work in progress"',
    '(\\HasNoChildren \\Sent) "/" "INBOX/Sent Items"',
    '(\\HasNoChildren \\Trash) "/" "Trash"',
]


def _imap(host: str, port: int, **extra: Any) -> ImapConfig:
    return ImapConfig(host=host, port=port, tls="none", username=LOGIN, password="p", **extra)


def _mailbox(
    imap: tuple[str, int], smtp: tuple[str, int] | None = None, **extra: Any
) -> MailboxConfig:
    return MailboxConfig(
        name="shop",
        imap=_imap(*imap),
        smtp=(
            SmtpConfig(
                host=smtp[0],
                port=smtp[1],
                tls="none",
                username="",
                password="",
                from_address=LOGIN,
            )
            if smtp
            else None
        ),
        **extra,
    )


def _inbound(to: str = "Sales <sales@mydomain.example>", **headers: str) -> bytes:
    msg = email.message.EmailMessage()
    msg["Delivered-To"] = LOGIN
    msg["From"] = "Customer <customer@example.com>"
    msg["To"] = to
    msg["Subject"] = "Wholesale pricing?"
    msg["Date"] = "Wed, 18 May 2026 10:00:00 +0000"
    msg["Message-ID"] = "<question-1@example.com>"
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    msg.set_content("Do you sell by the keg?")
    return msg.as_bytes()


# --------------------------------------------------------------------------
# Folder discovery
# --------------------------------------------------------------------------


def test_drafts_and_sent_folders_come_from_special_use(imap_factory: Factory) -> None:
    sink, host, port = imap_factory({})
    sink.list_lines = SPECIAL_USE_LIST
    # Config names a different folder on purpose: SPECIAL-USE wins.
    cfg = _imap(host, port, drafts_folder="Configured Drafts", sent_folder="Configured Sent")
    assert save_draft(cfg, RAW)["folder"] == "Work in progress"
    assert save_sent(cfg, RAW) == "INBOX/Sent Items"
    assert [name for name, _, _ in sink.appended] == ["Work in progress", "INBOX/Sent Items"]


def test_special_use_is_requested_explicitly_when_plain_list_omits_it(
    imap_factory: Factory,
) -> None:
    sink, host, port = imap_factory({})
    sink.capabilities = "IMAP4rev1 SPECIAL-USE LIST-EXTENDED"
    sink.list_lines = ['(\\HasNoChildren) "/" "INBOX"', '(\\HasNoChildren) "/" "Brouillons"']
    sink.extended_list_lines = ['(\\HasNoChildren \\Drafts) "/" "Brouillons"']
    assert save_draft(_imap(host, port), RAW)["folder"] == "Brouillons"
    assert any("RETURN (SPECIAL-USE)" in c for c in sink.commands)


def test_folders_fall_back_to_config_then_to_defaults(imap_factory: Factory) -> None:
    sink, host, port = imap_factory({})
    configured = _imap(host, port, drafts_folder="My Drafts", sent_folder="My Sent")
    assert save_draft(configured, RAW)["folder"] == "My Drafts"
    assert save_sent(configured, RAW) == "My Sent"
    assert save_draft(_imap(host, port), RAW)["folder"] == "Drafts"
    assert save_sent(_imap(host, port), RAW) == "Sent"
    # No SPECIAL-USE capability advertised, so the extended LIST is never tried.
    assert not any("RETURN" in c for c in sink.commands)


def test_non_ascii_folder_names_are_sent_as_modified_utf7(imap_factory: Factory) -> None:
    sink, host, port = imap_factory({})
    cfg = _imap(host, port, drafts_folder="Entwürfe", sent_folder="Envoyés & co")
    save_draft(cfg, RAW)
    save_sent(cfg, RAW)
    appends = [c for c in sink.commands if " APPEND " in c]
    assert '"Entw&APw-rfe"' in appends[0]
    assert '"Envoy&AOk-s &- co"' in appends[1]


def test_append_to_a_missing_folder_names_the_config_key(imap_factory: Factory) -> None:
    sink, host, port = imap_factory({})
    sink.missing = {"Drafts"}
    with pytest.raises(ImapError, match="imap.drafts_folder"):
        save_draft(_imap(host, port), RAW)


# --------------------------------------------------------------------------
# save / list / get / update / delete
# --------------------------------------------------------------------------


def test_save_draft_appends_with_draft_flag_and_returns_appenduid(imap_factory: Factory) -> None:
    sink, host, port = imap_factory({})
    sink.capabilities = UIDPLUS
    out = ops.save_draft(
        _mailbox((host, port)),
        to=["customer@example.com"],
        subject="Pricing",
        body_text="Yes we do.",
        bcc=["boss@mydomain.example"],
        from_address="sales@mydomain.example",
    )
    folder, flags, raw = sink.appended[0]
    assert (folder, out["folder"]) == ("Drafts", "Drafts")
    assert "\\Draft" in flags
    assert out["uid"] == "100"
    stored = email.message_from_bytes(raw)
    assert stored["Message-ID"] == out["message_id"]
    assert stored["From"] == "sales@mydomain.example"
    assert stored["Bcc"] == "boss@mydomain.example"
    assert stored["Date"]
    # APPENDUID answered the question; no search needed.
    assert "UID SEARCH" not in sink.verbs()


def test_save_draft_finds_uid_by_message_id_without_uidplus(imap_factory: Factory) -> None:
    sink, host, port = imap_factory({})
    mb = _mailbox((host, port))
    first = ops.save_draft(mb, subject="one", body_text="1")
    second = ops.save_draft(mb, subject="two", body_text="2")
    assert (first["uid"], second["uid"]) == ("100", "101")
    assert any(f'HEADER Message-ID "{second["message_id"]}"' in c for c in sink.commands)


def test_list_and_get_draft(imap_factory: Factory) -> None:
    sink, host, port = imap_factory({1: imap_mock.sample_message("in the inbox")})
    mb = _mailbox((host, port))
    saved = ops.save_draft(
        mb,
        to=["customer@example.com"],
        subject="Pricing",
        body_text="Yes we do.",
        bcc=["boss@mydomain.example"],
        in_reply_to="<question-1@example.com>",
    )
    listed = ops.list_drafts(mb)
    assert listed["folder"] == "Drafts"
    assert [m["subject"] for m in listed["messages"]] == ["Pricing"]

    draft = ops.get_draft(mb, saved["uid"])
    assert draft["uid"] == saved["uid"]
    assert draft["folder"] == "Drafts"
    assert draft["bcc"] == "boss@mydomain.example"
    assert draft["in_reply_to"] == "<question-1@example.com>"
    assert "Yes we do." in draft["body_text"]


def test_update_draft_appends_new_version_then_expunges_the_old_uid(
    imap_factory: Factory,
) -> None:
    sink, host, port = imap_factory({})
    sink.capabilities = UIDPLUS
    mb = _mailbox((host, port))
    old = ops.save_draft(
        mb,
        to=["customer@example.com"],
        subject="Pricing",
        body_text="first attempt",
        in_reply_to="<question-1@example.com>",
        references="<question-1@example.com>",
    )
    sink.commands.clear()
    new = ops.update_draft(mb, old["uid"], body_text="second attempt")

    assert new["uid"] != old["uid"]
    assert list(sink.folders["Drafts"]) == [int(new["uid"])]
    # Append first, so a failure in between can't lose the draft.
    verbs = sink.verbs()
    assert verbs.index("APPEND") < verbs.index("UID STORE") < verbs.index("UID EXPUNGE")
    assert any(f"UID EXPUNGE {old['uid']}" in c for c in sink.commands)

    stored = email.message_from_bytes(sink.folders["Drafts"][int(new["uid"])])
    assert stored.get_payload(decode=True).strip() == b"second attempt"
    # Everything not passed carries over from the old version.
    assert stored["To"] == "customer@example.com"
    assert stored["Subject"] == "Pricing"
    assert stored["In-Reply-To"] == "<question-1@example.com>"
    assert stored["References"] == "<question-1@example.com>"


def test_update_draft_can_change_and_clear_fields(imap_factory: Factory) -> None:
    sink, host, port = imap_factory({})
    mb = _mailbox((host, port))
    old = ops.save_draft(
        mb, to=["a@example.com"], cc=["c@example.com"], subject="old", body_text="b"
    )
    new = ops.update_draft(mb, old["uid"], to=["z@example.com"], cc=[], subject="new")
    stored = email.message_from_bytes(sink.folders["Drafts"][int(new["uid"])])
    assert (stored["To"], stored["Cc"], stored["Subject"]) == ("z@example.com", None, "new")
    assert stored.get_payload(decode=True).strip() == b"b"
    # No UIDPLUS on this server: falls back to a plain EXPUNGE.
    assert "EXPUNGE" in sink.verbs()


def test_update_refuses_a_draft_with_attachments(imap_factory: Factory) -> None:
    # e.g. a draft someone attached a file to in their mail client. Rebuilding
    # it from fields would drop the file, and the old version gets expunged.
    msg = email.message.EmailMessage()
    msg["From"] = LOGIN
    msg["To"] = "customer@example.com"
    msg["Subject"] = "Price list"
    msg.set_content("attached")
    msg.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="prices.pdf")
    sink, host, port = imap_factory({})
    sink.folders["Drafts"] = {5: msg.as_bytes()}
    with pytest.raises(ValueError, match="attachments"):
        ops.update_draft(_mailbox((host, port)), "5", subject="New subject")
    assert sink.appended == []
    assert list(sink.folders["Drafts"]) == [5]


def test_update_unknown_draft_fails_without_appending(imap_factory: Factory) -> None:
    sink, host, port = imap_factory({})
    sink.folders["Drafts"] = {}
    with pytest.raises(ImapError):
        ops.update_draft(_mailbox((host, port)), "999", body_text="x")
    assert sink.appended == []


def test_delete_draft(imap_factory: Factory) -> None:
    sink, host, port = imap_factory({})
    mb = _mailbox((host, port))
    saved = ops.save_draft(mb, subject="scrap", body_text="x")
    assert ops.delete_draft(mb, saved["uid"]) == {"ok": True, "folder": "Drafts"}
    assert sink.folders["Drafts"] == {}
    with pytest.raises(ImapError):
        ops.delete_draft(mb, saved["uid"])


def test_draft_uid_must_be_a_single_uid(imap_factory: Factory) -> None:
    sink, host, port = imap_factory({})
    mb = _mailbox((host, port))
    ops.save_draft(mb, subject="keep me", body_text="x")
    # `1:*` is a valid IMAP sequence-set; it must not reach STORE \Deleted.
    with pytest.raises(ImapError):
        ops.delete_draft(mb, "1:*")
    assert "UID STORE" not in sink.verbs()
    assert len(sink.folders["Drafts"]) == 1


# --------------------------------------------------------------------------
# send / send_draft
# --------------------------------------------------------------------------


def test_send_saves_a_copy_to_sent(imap_factory: Factory, smtp_sink: Any) -> None:
    isink, ihost, iport = imap_factory({})
    ssink, shost, sport = smtp_sink
    out = ops.send(
        _mailbox((ihost, iport), (shost, sport)),
        to=["customer@example.com"],
        subject="hi",
        body_text="hello",
        bcc=["boss@mydomain.example"],
        in_reply_to="<question-1@example.com>",
        references="<question-1@example.com>",
    )
    assert out["saved_to_sent"] is True
    assert out["sent_folder"] == "Sent"
    wire = email.message_from_bytes(ssink.messages[0])
    assert wire["Bcc"] is None
    assert wire["In-Reply-To"] == "<question-1@example.com>"
    assert wire["References"] == "<question-1@example.com>"
    assert wire["Message-ID"] == out["message_id"]
    assert wire["Date"]

    folder, flags, raw = isink.appended[0]
    copy = email.message_from_bytes(raw)
    assert folder == "Sent"
    assert "\\Seen" in flags and "\\Draft" not in flags
    assert copy["Message-ID"] == out["message_id"]
    # The Sent copy is the record of who was Bcc'd.
    assert copy["Bcc"] == "boss@mydomain.example"


def test_send_succeeds_with_a_warning_when_the_sent_copy_fails(
    imap_factory: Factory, smtp_sink: Any
) -> None:
    isink, ihost, iport = imap_factory({})
    isink.missing = {"Sent"}
    ssink, shost, sport = smtp_sink
    out = ops.send(
        _mailbox((ihost, iport), (shost, sport)),
        to=["customer@example.com"],
        subject="hi",
        body_text="hello",
    )
    # The mail went out, so the call must not fail and invite a second send.
    assert len(ssink.messages) == 1
    assert out["saved_to_sent"] is False
    assert "imap.sent_folder" in out["warnings"][0]


@pytest.mark.parametrize("failure", ["refused", "bye", "bug"])
def test_nothing_after_the_send_can_fail_the_call(
    imap_factory: Factory, smtp_sink: Any, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    # Once SMTP has accepted the message, an error would make the caller retry
    # and send it twice. Whatever breaks afterwards has to come back as a warning.
    isink, ihost, iport = imap_factory({})
    ssink, shost, sport = smtp_sink
    mb = _mailbox((ihost, iport), (shost, sport))
    draft = ops.save_draft(mb, to=["customer@example.com"], subject="s", body_text="b")

    real_connect = imap_client._connect
    calls = {"n": 0}

    def _flaky(cfg: ImapConfig) -> Any:
        # Let send_draft read the draft, then break IMAP for everything after.
        calls["n"] += 1
        if calls["n"] > 1 and failure == "bug":
            raise RuntimeError("unexpected")
        return real_connect(cfg)

    monkeypatch.setattr(imap_client, "_connect", _flaky)
    original_send = smtp_client.send_raw

    def _send_then_break(*args: Any, **kwargs: Any) -> None:
        original_send(*args, **kwargs)
        if failure == "refused":
            mb.imap.port = 1  # type: ignore[union-attr]
        if failure == "bye":
            isink.greeting = "* BYE server shutting down"

    monkeypatch.setattr(smtp_client, "send_raw", _send_then_break)

    out = ops.send_draft(mb, draft["uid"])

    assert len(ssink.messages) == 1
    assert (out["saved_to_sent"], out["draft_deleted"]) == (False, False)
    assert len(out["warnings"]) == 2
    assert "could not be deleted" in out["warnings"][1]


def test_save_sent_false_skips_the_copy(imap_factory: Factory, smtp_sink: Any) -> None:
    isink, ihost, iport = imap_factory({})
    ssink, shost, sport = smtp_sink
    mb = _mailbox((ihost, iport), (shost, sport))
    assert mb.imap is not None
    mb.imap.save_sent = False
    out = ops.send(mb, to=["customer@example.com"], subject="hi", body_text="hello")
    assert out["saved_to_sent"] is False
    assert "warnings" not in out
    assert isink.commands == []


def test_send_draft_sends_as_saved_files_it_in_sent_and_deletes_the_draft(
    imap_factory: Factory, smtp_sink: Any
) -> None:
    isink, ihost, iport = imap_factory({})
    isink.capabilities = UIDPLUS
    ssink, shost, sport = smtp_sink
    mb = _mailbox((ihost, iport), (shost, sport))
    draft = ops.save_draft(
        mb,
        to=["Customer <customer@example.com>"],
        cc=["cc@example.com"],
        bcc=["boss@mydomain.example"],
        subject="Pricing",
        body_text="Yes we do.\nFrom $40 a keg.",
        from_address="Sales <sales@mydomain.example>",
    )
    saved_raw = isink.appended[0][2]

    out = ops.send_draft(mb, draft["uid"])

    assert out["message_id"] == draft["message_id"]
    assert (out["saved_to_sent"], out["sent_folder"], out["draft_deleted"]) == (True, "Sent", True)
    assert "warnings" not in out

    mail_from, rcpts = ssink.envelopes[0]
    assert mail_from == "sales@mydomain.example"
    assert set(rcpts) == {"customer@example.com", "cc@example.com", "boss@mydomain.example"}
    # On the wire: the saved bytes minus the Bcc header, nothing regenerated.
    assert (
        ssink.messages[0].rstrip()
        == saved_raw.replace(b"Bcc: boss@mydomain.example\r\n", b"").rstrip()
    )

    folder, _, sent_raw = isink.appended[1]
    assert folder == "Sent"
    assert sent_raw == saved_raw
    assert isink.folders["Drafts"] == {}


def test_send_draft_without_recipients_is_refused(imap_factory: Factory, smtp_sink: Any) -> None:
    isink, ihost, iport = imap_factory({})
    ssink, shost, sport = smtp_sink
    mb = _mailbox((ihost, iport), (shost, sport))
    draft = ops.save_draft(mb, subject="half-written", body_text="...")
    with pytest.raises(ValueError, match="no recipients"):
        ops.send_draft(mb, draft["uid"])
    assert ssink.messages == []
    assert len(isink.folders["Drafts"]) == 1


# --------------------------------------------------------------------------
# reply
# --------------------------------------------------------------------------


def test_reply_threads_and_answers_from_the_address_the_mail_was_sent_to(
    imap_factory: Factory, smtp_sink: Any
) -> None:
    original = _inbound(References="<root@example.com> <earlier@example.com>")
    isink, ihost, iport = imap_factory({7: original})
    ssink, shost, sport = smtp_sink
    out = ops.reply(_mailbox((ihost, iport), (shost, sport)), "7", body_text="Yes, by the keg.")

    wire = email.message_from_bytes(ssink.messages[0])
    assert wire["From"] == "sales@mydomain.example"
    assert wire["To"] == "Customer <customer@example.com>"
    assert wire["Subject"] == "Re: Wholesale pricing?"
    assert wire["In-Reply-To"] == "<question-1@example.com>"
    assert wire["References"].split() == [
        "<root@example.com>",
        "<earlier@example.com>",
        "<question-1@example.com>",
    ]
    assert wire["Message-ID"] and wire["Date"]
    assert ssink.envelopes[0] == ("sales@mydomain.example", ["customer@example.com"])
    assert out["draft"] is False
    assert out["saved_to_sent"] is True
    assert isink.appended[0][0] == "Sent"


def test_reply_prefers_reply_to_and_keeps_a_single_re(
    imap_factory: Factory, smtp_sink: Any
) -> None:
    original = _inbound(Reply_To="Support Desk <desk@example.com>").replace(
        b"Subject: Wholesale pricing?", b"Subject: RE: Re: Wholesale pricing?"
    )
    isink, ihost, iport = imap_factory({7: original})
    ssink, shost, sport = smtp_sink
    ops.reply(_mailbox((ihost, iport), (shost, sport)), "7", body_text="ok")
    wire = email.message_from_bytes(ssink.messages[0])
    assert wire["To"] == "Support Desk <desk@example.com>"
    assert wire["Subject"] == "Re: Wholesale pricing?"


def test_reply_from_address_overrides_received_for(imap_factory: Factory, smtp_sink: Any) -> None:
    isink, ihost, iport = imap_factory({7: _inbound()})
    ssink, shost, sport = smtp_sink
    ops.reply(
        _mailbox((ihost, iport), (shost, sport)),
        "7",
        body_text="ok",
        from_address="Billing <billing@mydomain.example>",
    )
    assert email.message_from_bytes(ssink.messages[0])["From"] == (
        "Billing <billing@mydomain.example>"
    )


def test_reply_falls_back_to_the_configured_from_address(
    imap_factory: Factory, smtp_sink: Any
) -> None:
    # Nothing in the original points at our domain (e.g. we were Bcc'd).
    original = _inbound(to="list@example.org").replace(b"Delivered-To: " + LOGIN.encode(), b"X: y")
    isink, ihost, iport = imap_factory({7: original})
    ssink, shost, sport = smtp_sink
    ops.reply(_mailbox((ihost, iport), (shost, sport)), "7", body_text="ok")
    assert email.message_from_bytes(ssink.messages[0])["From"] == LOGIN


def test_reply_as_draft_saves_instead_of_sending(imap_factory: Factory, smtp_sink: Any) -> None:
    isink, ihost, iport = imap_factory({7: _inbound()})
    ssink, shost, sport = smtp_sink
    out = ops.reply(
        _mailbox((ihost, iport), (shost, sport)), "7", body_text="Draft answer", draft=True
    )
    assert ssink.messages == []
    assert out["draft"] is True
    assert out["folder"] == "Drafts"
    folder, flags, raw = isink.appended[0]
    stored = email.message_from_bytes(raw)
    assert folder == "Drafts" and "\\Draft" in flags
    assert stored["From"] == "sales@mydomain.example"
    assert stored["In-Reply-To"] == "<question-1@example.com>"
    assert out["uid"] == str(next(iter(isink.folders["Drafts"])))


# --------------------------------------------------------------------------
# allowed_from
# --------------------------------------------------------------------------


def test_allowed_from_blocks_send_reply_and_send_draft(
    imap_factory: Factory, smtp_sink: Any
) -> None:
    # The original was addressed to a domain address that is NOT on the list.
    isink, ihost, iport = imap_factory({7: _inbound(to="ceo@mydomain.example")})
    ssink, shost, sport = smtp_sink
    mb = _mailbox(
        (ihost, iport),
        (shost, sport),
        allowed_from=["sales@mydomain.example", "*@shop.mydomain.example"],
    )

    with pytest.raises(FromNotAllowedError):
        ops.send(
            mb, to=["a@example.com"], subject="s", body_text="b", from_address="ceo@example.com"
        )
    # The config's own default From isn't exempt either.
    with pytest.raises(FromNotAllowedError):
        ops.send(mb, to=["a@example.com"], subject="s", body_text="b")
    # reply defaults From to received_for, which the list doesn't cover.
    with pytest.raises(FromNotAllowedError):
        ops.reply(mb, "7", body_text="b")
    with pytest.raises(FromNotAllowedError):
        ops.reply(mb, "7", body_text="b", draft=True)

    draft = ops.save_draft(
        mb, to=["a@example.com"], subject="s", body_text="b", from_address="ceo@example.com"
    )
    with pytest.raises(FromNotAllowedError):
        ops.send_draft(mb, draft["uid"])

    assert ssink.messages == []
    assert len(isink.folders["Drafts"]) == 1

    ops.send(
        mb,
        to=["a@example.com"],
        subject="s",
        body_text="b",
        from_address="Sales <sales@mydomain.example>",
    )
    ops.reply(mb, "7", body_text="b", from_address="orders@shop.mydomain.example")
    assert len(ssink.messages) == 2


# --------------------------------------------------------------------------
# HTTP + MCP surface
# --------------------------------------------------------------------------


def _write_config(tmp_path: Path, imap: tuple[str, int], smtp: tuple[str, int]) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"""
mailboxes:
  - name: shop
    allowed_from: ["*@mydomain.example"]
    imap:
      host: {imap[0]}
      port: {imap[1]}
      tls: none
      username: {LOGIN}
      password: "p"
    smtp:
      host: {smtp[0]}
      port: {smtp[1]}
      tls: none
      username: ""
      password: ""
      from_address: {LOGIN}
  - name: smtp_only
    smtp:
      host: {smtp[0]}
      port: {smtp[1]}
      tls: none
      username: ""
      password: ""
      from_address: {LOGIN}
""")
    return cfg


def _http(tmp_path: Path, imap: tuple[str, int], smtp: tuple[str, int]) -> TestClient:
    return TestClient(create_app(load_config(str(_write_config(tmp_path, imap, smtp)))))


def test_get_message_route_returns_headers_and_received_for(
    imap_factory: Factory, smtp_sink: Any, tmp_path: Path
) -> None:
    original = _inbound(
        X_Original_To="sales@mydomain.example",
        Envelope_To="sales@mydomain.example",
        Cc="other@example.com",
        In_Reply_To="<earlier@example.com>",
        References="<root@example.com> <earlier@example.com>",
    )
    isink, ihost, iport = imap_factory({7: original})
    _, shost, sport = smtp_sink
    body = _http(tmp_path, (ihost, iport), (shost, sport)).get("/mailboxes/shop/messages/7")
    assert body.status_code == 200, body.text
    msg = body.json()
    assert msg["delivered_to"] == LOGIN
    assert msg["x_original_to"] == "sales@mydomain.example"
    assert msg["envelope_to"] == "sales@mydomain.example"
    assert msg["to"] == "Sales <sales@mydomain.example>"
    assert msg["cc"] == "other@example.com"
    assert msg["message_id"] == "<question-1@example.com>"
    assert msg["in_reply_to"] == "<earlier@example.com>"
    assert msg["references"] == "<root@example.com> <earlier@example.com>"
    assert msg["received_for"] == "sales@mydomain.example"
    # Pre-existing fields are untouched.
    assert msg["subject"] == "Wholesale pricing?"
    assert "keg" in msg["body_text"]


def test_draft_and_reply_routes_end_to_end(
    imap_factory: Factory, smtp_sink: Any, tmp_path: Path
) -> None:
    isink, ihost, iport = imap_factory({7: _inbound()})
    isink.capabilities = UIDPLUS
    ssink, shost, sport = smtp_sink
    c = _http(tmp_path, (ihost, iport), (shost, sport))

    r = c.post("/mailboxes/shop/messages/7/reply", json={"body_text": "v1", "draft": True})
    assert r.status_code == 200, r.text
    uid = r.json()["uid"]

    r = c.get("/mailboxes/shop/drafts")
    assert [m["uid"] for m in r.json()["messages"]] == [uid]

    r = c.put(f"/mailboxes/shop/drafts/{uid}", json={"body_text": "v2"})
    assert r.status_code == 200, r.text
    new_uid = r.json()["uid"]
    assert new_uid != uid

    r = c.get(f"/mailboxes/shop/drafts/{new_uid}")
    assert r.json()["body_text"].strip() == "v2"
    assert r.json()["subject"] == "Re: Wholesale pricing?"

    r = c.post(f"/mailboxes/shop/drafts/{new_uid}/send")
    assert r.status_code == 200, r.text
    assert r.json()["draft_deleted"] is True
    wire = email.message_from_bytes(ssink.messages[0])
    assert wire["From"] == "sales@mydomain.example"
    assert wire["In-Reply-To"] == "<question-1@example.com>"
    assert wire.get_payload(decode=True).strip() == b"v2"

    r = c.post("/mailboxes/shop/drafts", json={"subject": "scratch"})
    assert r.status_code == 200, r.text
    r = c.delete(f"/mailboxes/shop/drafts/{r.json()['uid']}")
    assert r.json()["ok"] is True
    assert isink.folders["Drafts"] == {}


def test_routes_map_errors_to_statuses(
    imap_factory: Factory, smtp_sink: Any, tmp_path: Path
) -> None:
    isink, ihost, iport = imap_factory({7: _inbound()})
    ssink, shost, sport = smtp_sink
    c = _http(tmp_path, (ihost, iport), (shost, sport))

    send = {"to": ["a@example.com"], "subject": "s", "body_text": "b"}
    r = c.post("/mailboxes/shop/send", json={**send, "from_address": "ceo@example.com"})
    assert r.status_code == 403, r.text
    r = c.post(
        "/mailboxes/shop/messages/7/reply",
        json={"body_text": "b", "from_address": "ceo@example.com"},
    )
    assert r.status_code == 403, r.text
    # Header injection through a JSON field is a client error, not a 500.
    r = c.post("/mailboxes/shop/send", json={**send, "subject": "s\r\nBcc: x@example.com"})
    assert r.status_code == 422, r.text
    r = c.post("/mailboxes/shop/messages/7/reply", json={})
    assert r.status_code == 422, r.text
    # Drafts and replies need IMAP.
    assert c.get("/mailboxes/smtp_only/drafts").status_code == 409
    assert c.post("/mailboxes/smtp_only/drafts/1/send").status_code == 409
    assert (
        c.post("/mailboxes/smtp_only/messages/7/reply", json={"body_text": "b"}).status_code == 409
    )
    assert ssink.messages == []


_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _mcp_call(client: TestClient, request_id: int, method: str, params: dict[str, Any]) -> Any:
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


def test_mcp_exposes_and_runs_the_new_tools(
    imap_factory: Factory, smtp_sink: Any, tmp_path: Path
) -> None:
    isink, ihost, iport = imap_factory({7: _inbound()})
    ssink, shost, sport = smtp_sink
    with _http(tmp_path, (ihost, iport), (shost, sport)) as client:
        _mcp_call(
            client,
            1,
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "mailboxd-test", "version": "1"},
            },
        )
        tools = {t["name"]: t for t in _mcp_call(client, 2, "tools/list", {})["tools"]}
        assert {
            "save_draft",
            "list_drafts",
            "get_draft",
            "update_draft",
            "delete_draft",
            "send_draft",
            "reply",
        } <= set(tools)
        assert {"in_reply_to", "references"} <= set(tools["send"]["inputSchema"]["properties"])

        def call(request_id: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            result = _mcp_call(
                client, request_id, "tools/call", {"name": name, "arguments": arguments}
            )
            return json.loads(result["content"][0]["text"])

        got = call(3, "get_message", {"mailbox": "shop", "uid": "7"})
        assert got["received_for"] == "sales@mydomain.example"

        drafted = call(4, "reply", {"mailbox": LOGIN, "uid": "7", "body_text": "hi", "draft": True})
        assert drafted["draft"] is True
        sent = call(5, "send_draft", {"mailbox": "shop", "uid": drafted["uid"]})
        assert sent["draft_deleted"] is True
        assert email.message_from_bytes(ssink.messages[0])["From"] == "sales@mydomain.example"

        refused = call(
            6,
            "send",
            {
                "mailbox": "shop",
                "to": ["a@example.com"],
                "subject": "s",
                "body_text": "b",
                "from_address": "ceo@example.com",
            },
        )
        assert "allowed_from" in refused["error"]
        assert len(ssink.messages) == 1
