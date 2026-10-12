"""`GET /health/detail`: per-account login checks against the mocks."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import imap_mock
from fastapi.testclient import TestClient

from mailboxd.cli import main
from mailboxd.config import load_config
from mailboxd.server import create_app

Factory = Callable[[dict[int, bytes]], "tuple[imap_mock.Sink, str, int]"]

TOKEN = "s3cret-bearer-token"
PASSWORD = "hunter2:with,odd@chars"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _client(tmp_path: Path, imap: tuple[str, int], smtp: tuple[str, int]) -> TestClient:
    env = {
        "MAILBOX_AUTH_TOKENS": TOKEN,
        "MAILBOX_PROVIDER_MOCK_IMAP_HOST": imap[0],
        "MAILBOX_PROVIDER_MOCK_IMAP_PORT": str(imap[1]),
        "MAILBOX_PROVIDER_MOCK_IMAP_TLS": "none",
        "MAILBOX_PROVIDER_MOCK_SMTP_HOST": smtp[0],
        "MAILBOX_PROVIDER_MOCK_SMTP_PORT": str(smtp[1]),
        "MAILBOX_PROVIDER_MOCK_SMTP_TLS": "none",
        "MAILBOX_SHOP": f"mock:inbox@mydomain.example:{PASSWORD}",
    }
    return TestClient(create_app(load_config(str(tmp_path / "none.yaml"), environ=env)))


def test_health_detail_requires_auth_but_health_does_not(
    imap_factory: Factory, smtp_sink: Any, tmp_path: Path
) -> None:
    _, ihost, iport = imap_factory({})
    _, shost, sport = smtp_sink
    c = _client(tmp_path, (ihost, iport), (shost, sport))
    assert c.get("/health").status_code == 200
    assert c.get("/health/detail").status_code == 401
    assert c.get("/health/detail", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_health_detail_reports_successful_logins(
    imap_factory: Factory, smtp_sink: Any, tmp_path: Path
) -> None:
    isink, ihost, iport = imap_factory({})
    ssink, shost, sport = smtp_sink
    r = _client(tmp_path, (ihost, iport), (shost, sport)).get("/health/detail", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["accounts"] == [
        {"name": "shop", "provider": "mock", "imap_login": True, "smtp_login": True}
    ]
    # It really logged in to both, and nothing in the response carries the secret.
    assert any(" LOGIN " in c for c in isink.commands)
    assert len(ssink.auth) == 1
    assert PASSWORD not in r.text and TOKEN not in r.text


def test_health_detail_reports_failed_logins_without_leaking_the_password(
    imap_factory: Factory, smtp_sink: Any, tmp_path: Path
) -> None:
    isink, ihost, iport = imap_factory({})
    ssink, shost, sport = smtp_sink
    # Both mocks echo the rejected command back, credentials included.
    isink.reject_login = True
    ssink.reject_auth = True
    r = _client(tmp_path, (ihost, iport), (shost, sport)).get("/health/detail", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    account = body["accounts"][0]
    assert body["ok"] is False
    assert (account["imap_login"], account["smtp_login"]) == (False, False)
    assert "login failed" in account["imap_error"]
    assert "login failed" in account["smtp_error"]
    assert "***" in account["imap_error"]
    assert "***" in account["smtp_error"]
    assert PASSWORD not in r.text
    # The SMTP mock echoes `AUTH PLAIN <base64 of user and password>`.
    assert ssink.auth[0].split()[-1] not in r.text


def test_health_detail_survives_a_server_that_refuses_the_connection(
    imap_factory: Factory, smtp_sink: Any, tmp_path: Path
) -> None:
    isink, ihost, iport = imap_factory({})
    isink.greeting = "* BYE too many connections"
    _, shost, sport = smtp_sink
    c = _client(tmp_path, (ihost, iport), (shost, sport))
    account = c.get("/health/detail", headers=AUTH).json()["accounts"][0]
    assert account["imap_login"] is False and "connection failed" in account["imap_error"]
    assert account["smtp_login"] is True
    # ...and on an ordinary route it is an upstream error, not a crash.
    assert c.get("/mailboxes/shop/folders", headers=AUTH).status_code == 502


def test_health_detail_marks_missing_protocols_and_unreachable_servers(
    imap_factory: Factory, tmp_path: Path
) -> None:
    _, ihost, iport = imap_factory({})
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"""
mailboxes:
  - name: imap_only
    imap: {{host: {ihost}, port: {iport}, tls: none, username: u, password: p}}
  - name: dead
    smtp: {{host: 127.0.0.1, port: 1, tls: none, username: "", password: "",
      from_address: a@example.com}}
""")
    body = TestClient(create_app(load_config(str(cfg)))).get("/health/detail").json()
    by_name = {a["name"]: a for a in body["accounts"]}
    assert body["ok"] is False
    assert by_name["imap_only"] == {
        "name": "imap_only",
        "provider": None,
        "imap_login": True,
        "smtp_login": None,
    }
    assert by_name["dead"]["imap_login"] is None
    assert by_name["dead"]["smtp_login"] is False


def test_login_failure_on_a_normal_route_is_redacted_too(
    imap_factory: Factory, smtp_sink: Any, tmp_path: Path
) -> None:
    isink, ihost, iport = imap_factory({})
    isink.reject_login = True
    _, shost, sport = smtp_sink
    r = _client(tmp_path, (ihost, iport), (shost, sport)).get(
        "/mailboxes/shop/folders", headers=AUTH
    )
    assert r.status_code == 502
    assert "login failed" in r.json()["detail"]
    assert PASSWORD not in r.text


def test_cli_port_defaults_to_mailbox_port(monkeypatch: Any) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: seen.update(kw))
    monkeypatch.setenv("MAILBOX_A", "gmail:a@example.com:pw")
    main(["http"])
    assert seen["port"] == 8000
    monkeypatch.setenv("MAILBOX_PORT", "9090")
    main(["http"])
    assert seen["port"] == 9090
    main(["http", "--port", "7000"])
    assert seen["port"] == 7000


def test_cli_exits_on_bad_config_instead_of_starting_workers(monkeypatch: Any, capsys: Any) -> None:
    started: list[Any] = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: started.append(kw))
    monkeypatch.setenv("MAILBOX_WORK", f"protonmail:me@proton.me:{PASSWORD}")
    assert main(["http"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("mailboxd: MAILBOX_WORK: unknown provider")
    assert PASSWORD not in err
    assert started == []
