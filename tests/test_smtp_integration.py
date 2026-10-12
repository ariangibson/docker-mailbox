"""SMTP integration test against an in-process socketserver capture sink.

Validates the SMTP client formats messages correctly and that the HTTP
/send endpoint actually delivers them end-to-end. Uses a tiny hand-rolled
SMTP server (no aiosmtpd) so the fixture has no external moving parts.
"""

from __future__ import annotations

import email
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from mailboxd.config import load_config
from mailboxd.server import create_app


def _client(tmp_path: Path, host: str, port: int) -> TestClient:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"""
mailboxes:
  - name: sink
    smtp:
      host: {host}
      port: {port}
      tls: none
      username: ""
      password: ""
      from_address: "sender@example.com"
""")
    return TestClient(create_app(load_config(str(cfg))))


def test_send_delivers_message(smtp_sink: Any, tmp_path: Path) -> None:
    sink, host, port = smtp_sink
    c = _client(tmp_path, host, port)
    r = c.post(
        "/mailboxes/sink/send",
        json={
            "to": ["dest@example.com"],
            "subject": "hi from mailboxd",
            "body_text": "body line",
        },
    )
    assert r.status_code == 200, r.text
    assert len(sink.messages) == 1
    raw = sink.messages[0]
    msg = email.message_from_bytes(raw)
    assert msg["Subject"] == "hi from mailboxd"
    assert "dest@example.com" in msg["To"]
    assert "sender@example.com" in msg["From"]
    mail_from, rcpts = sink.envelopes[0]
    assert "sender@example.com" in mail_from
    assert "dest@example.com" in rcpts


def test_send_with_cc_and_bcc_routes_correctly(smtp_sink: Any, tmp_path: Path) -> None:
    sink, host, port = smtp_sink
    c = _client(tmp_path, host, port)
    r = c.post(
        "/mailboxes/sink/send",
        json={
            "to": ["a@example.com"],
            "cc": ["b@example.com"],
            "bcc": ["c@example.com"],
            "subject": "multi",
            "body_text": "hi",
        },
    )
    assert r.status_code == 200, r.text
    msg = email.message_from_bytes(sink.messages[0])
    # bcc must NOT appear in headers
    assert "c@example.com" not in (msg["To"] or "")
    assert "c@example.com" not in (msg["Cc"] or "")
    # but all 3 must be in the SMTP envelope rcpts
    _, rcpts = sink.envelopes[0]
    assert set(rcpts) == {"a@example.com", "b@example.com", "c@example.com"}
