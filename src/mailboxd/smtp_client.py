"""SMTP send."""

from __future__ import annotations

import base64
import smtplib
from contextlib import contextmanager
from email.message import Message
from typing import Iterator

from .config import SmtpConfig, redact, tls_context


class SmtpError(Exception):
    """Raised when an SMTP send fails."""


def _login(s: smtplib.SMTP, cfg: SmtpConfig) -> None:
    try:
        s.login(cfg.username, cfg.password)
    except UnicodeError:
        raise smtplib.SMTPException(
            "the username or password has non-ASCII characters, which SMTP AUTH cannot carry"
        ) from None


@contextmanager
def _session(cfg: SmtpConfig, doing: str = "send") -> Iterator[smtplib.SMTP]:
    try:
        if cfg.tls == "ssl":
            ctx = tls_context(cfg.tls_verify)
            with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=30, context=ctx) as s:
                _login(s, cfg)
                yield s
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=30) as s:
                s.ehlo()
                if cfg.tls == "starttls":
                    s.starttls(context=tls_context(cfg.tls_verify))
                    s.ehlo()
                if cfg.username:
                    _login(s, cfg)
                yield s
    except (smtplib.SMTPException, OSError) as e:
        # Redacted, cause dropped: a server may echo back the AUTH it rejected,
        # which carries the credentials as base64.
        auth_plain = base64.b64encode(f"\0{cfg.username}\0{cfg.password}".encode()).decode()
        detail = redact(str(e), cfg.password, auth_plain)
        raise SmtpError(f"{doing} failed: {detail}") from None


def check_login(cfg: SmtpConfig) -> None:
    """Connect and authenticate, nothing more. Raises `SmtpError` on failure."""
    with _session(cfg, doing="login"):
        pass


def send_message(cfg: SmtpConfig, msg: Message, to_addrs: list[str]) -> None:
    """Send a message we built. smtplib drops the Bcc header on the way out."""
    with _session(cfg) as s:
        s.send_message(msg, to_addrs=to_addrs)


def send_raw(cfg: SmtpConfig, raw: bytes, from_addr: str, to_addrs: list[str]) -> None:
    """Send already-serialized bytes untouched (the caller strips Bcc first)."""
    opts: list[str] = []
    if not "".join([from_addr, *to_addrs]).isascii():
        opts = ["SMTPUTF8", "BODY=8BITMIME"]
    with _session(cfg) as s:
        s.sendmail(from_addr, to_addrs, raw, mail_options=opts)
