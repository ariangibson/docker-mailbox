"""Regression tests for the security audit findings."""

from __future__ import annotations

import datetime
import smtplib
import ssl
from pathlib import Path
from typing import Any, Callable

import imap_mock
import pytest
from fastapi.testclient import TestClient

from mailboxd import smtp_client
from mailboxd.config import Config, ImapConfig, SmtpConfig, tls_context
from mailboxd.imap_client import ImapError, delete_message, list_messages
from mailboxd.server import create_app

Factory = Callable[[dict[int, bytes]], "tuple[imap_mock.Sink, str, int]"]

# --------------------------------------------------------------------------
# TLS certificate verification
# --------------------------------------------------------------------------


def test_tls_context_verifies_by_default() -> None:
    ctx = tls_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    off = tls_context(verify=False)
    assert off.verify_mode == ssl.CERT_NONE


def _self_signed(tmp_path: Path) -> ssl.SSLContext:
    """A server-side TLS context with a throwaway self-signed certificate."""
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "mail.invalid")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .sign(key, hashes.SHA256())
    )
    pem = tmp_path / "server.pem"
    pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        + cert.public_bytes(serialization.Encoding.PEM)
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(pem))
    return ctx


def test_imap_refuses_an_untrusted_certificate(tmp_path: Path) -> None:
    # The audit finding: with imaplib's default context this connection
    # succeeded — to a server presenting a certificate nobody vouches for —
    # and the client then sent it the password.
    sink, srv, host, port = imap_mock.start(
        {1: imap_mock.sample_message("Hello")}, tls=_self_signed(tmp_path)
    )
    try:
        cfg = ImapConfig(host=host, port=port, tls="ssl", username="u", password="hunter2-secret")
        with pytest.raises(ImapError, match="connection failed.*CERTIFICATE_VERIFY_FAILED"):
            list_messages(cfg)
        assert not any("LOGIN" in c for c in sink.commands)

        # The explicit opt-out still works, for a self-signed server you trust.
        cfg.tls_verify = False
        assert [m["subject"] for m in list_messages(cfg)] == ["Hello"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_smtp_passes_a_verifying_context(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[ssl.SSLContext] = []

    def _fake_ssl(*args: Any, context: ssl.SSLContext | None = None, **kwargs: Any) -> Any:
        assert context is not None
        seen.append(context)
        raise smtplib.SMTPException("stop here")

    def _fake_starttls(self: Any, *, context: ssl.SSLContext | None = None, **kwargs: Any) -> Any:
        assert context is not None
        seen.append(context)
        raise smtplib.SMTPException("stop here")

    monkeypatch.setattr(smtplib, "SMTP_SSL", _fake_ssl)
    monkeypatch.setattr(smtplib.SMTP, "__init__", lambda self, *a, **kw: None)
    monkeypatch.setattr(smtplib.SMTP, "ehlo", lambda self, *a, **kw: (250, b"ok"))
    monkeypatch.setattr(smtplib.SMTP, "starttls", _fake_starttls)
    monkeypatch.setattr(smtplib.SMTP, "__exit__", lambda self, *a: None)

    for tls in ("ssl", "starttls"):
        cfg = SmtpConfig(
            host="smtp.invalid", tls=tls, username="u", password="p", from_address="a@b.example"
        )
        with pytest.raises(smtp_client.SmtpError):
            smtp_client.check_login(cfg)
    assert len(seen) == 2
    assert all(c.verify_mode == ssl.CERT_REQUIRED and c.check_hostname for c in seen)


# --------------------------------------------------------------------------
# Bearer auth
# --------------------------------------------------------------------------


def _app(tokens: list[str]) -> TestClient:
    cfg = Config.model_validate(
        {
            "auth": {"tokens": tokens},
            "mailboxes": [{"name": "a", "imap": {"host": "h", "username": "u", "password": "p"}}],
        }
    )
    return TestClient(create_app(cfg), raise_server_exceptions=False)


@pytest.mark.parametrize(
    "header",
    [
        b"Bearer t\xf6ken",  # non-ASCII bytes used to crash the comparison -> 500
        b"Bearer \xff\xfe",
        b"Bearer ",
        b"bearer a-long-token-123456",
        b"Basic a-long-token-123456",
        b"Bearer a-long-token-12345",
        b"Bearer a-long-token-1234567",
    ],
)
def test_bad_bearer_headers_are_401_never_500(header: bytes) -> None:
    r = _app(["a-long-token-123456"]).get("/mailboxes", headers={"Authorization": header})
    assert r.status_code == 401


def test_good_bearer_including_non_ascii_token() -> None:
    client = _app(["a-long-token-123456", "töken-ünïcode"])
    for token in ("a-long-token-123456", "töken-ünïcode"):
        header = b"Bearer " + token.encode("utf-8")
        assert client.get("/mailboxes", headers={"Authorization": header}).status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/health/", "/health/detail"])
def test_only_exact_health_is_open(path: str) -> None:
    client = _app(["a-long-token-123456"])
    assert client.get("/health").status_code == 200
    assert client.get(path).status_code == 401


# --------------------------------------------------------------------------
# Destructive calls
# --------------------------------------------------------------------------


@pytest.mark.parametrize("uid", ["1:*", "1,2", "1:2", "*"])
def test_delete_takes_exactly_one_uid(imap_factory: Factory, uid: str) -> None:
    # A sequence-set would let one request permanently empty a folder.
    msgs = {1: imap_mock.sample_message("a"), 2: imap_mock.sample_message("b")}
    sink, host, port = imap_factory(msgs)
    cfg = ImapConfig(host=host, port=port, tls="none", username="u", password="p")
    with pytest.raises(ImapError):
        delete_message(cfg, uid)
    assert sink.commands == []
    assert len(sink.messages) == 2
