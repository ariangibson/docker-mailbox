"""Outbound message assembly and the header logic around it.

Pure functions, no network. `build_message` is the one place an RFC 5322
message gets put together (send, reply and drafts all go through it); the
rest is reply threading, catch-all recipient detection (`received_for`) and
the `allowed_from` sender policy.
"""

from __future__ import annotations

import re
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.policy import EmailPolicy
from email.utils import formataddr, formatdate, getaddresses, make_msgid
from typing import Any, Iterable

# Masquerade as Thunderbird — providers' spam heuristics treat unknown/missing
# User-Agent as suspicious; a well-known MUA string is a mild positive signal.
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Thunderbird/128.5.0"

# Where a catch-all delivery records the address the mail was really sent to,
# most trustworthy first: the first three are stamped by the receiving server,
# To/Cc are whatever the sender typed.
RECEIVED_FOR_HEADERS = ("Delivered-To", "X-Original-To", "Envelope-To", "To", "Cc")

_MSGID_HEADERS = frozenset({"in-reply-to", "references"})
_MSGID_RE = re.compile(r"<[^<>\s]+>")
_MSGID_TOKEN_RE = re.compile(r"<[^<>\s]*>|[^<>\s,]+")
_RE_PREFIX = re.compile(r"^(?:\s*re\s*:)+\s*", re.IGNORECASE)
_BCC_RE = re.compile(
    rb"^(?:resent-)?bcc[ \t]*:.*(?:\r?\n[ \t].*)*\r?\n", re.IGNORECASE | re.MULTILINE
)
_HEADER_END_RE = re.compile(rb"\r?\n\r?\n")
# The two shapes a single-address header value may take: `local@domain`, or
# `Display Name <local@domain>` with the name optionally a quoted string.
_ADDR = r"[^\s<>@,;:\"()\[\]\\]+@[^\s<>@,;:\"()\[\]\\]+"
_ONE_ADDRESS_RE = re.compile(
    rf"""\s*(?:(?:"(?:[^"\\]|\\.)*"|[^<>@,;:"\\()\[\]]*)\s*<({_ADDR})>|({_ADDR}))\s*"""
)
_MAX_LINE = 78


class MessageError(ValueError):
    """Raised when the given fields can't be turned into a sendable message."""


class FromNotAllowedError(MessageError):
    """Raised when a From address is not covered by the mailbox's `allowed_from`."""


def _fold_msgids(name: str, value: str, linesep: str) -> str:
    lines: list[str] = []
    cur = f"{name}:"
    for tok in str(value).split():
        if cur != f"{name}:" and len(cur) + 1 + len(tok) > _MAX_LINE:
            lines.append(cur)
            cur = f" {tok}"
            continue
        cur += f" {tok}"
    lines.append(cur)
    return linesep.join(lines) + linesep


class _MsgIdPolicy(EmailPolicy):
    """`email.policy.default`, except In-Reply-To / References are folded by hand.

    The stdlib treats those two as unstructured text, so a msg-id longer than
    the 78-column limit (Amazon SES, Exchange) comes out RFC 2047-encoded —
    which no mail client recognises as a msg-id, silently breaking the thread.
    Folding only at the whitespace between msg-ids keeps every id intact.
    """

    def fold(self, name: str, value: str) -> str:
        if name.lower() in _MSGID_HEADERS:
            return _fold_msgids(name, value, self.linesep)
        return super().fold(name, value)

    def fold_binary(self, name: str, value: str) -> bytes:
        if name.lower() in _MSGID_HEADERS:
            return _fold_msgids(name, value, self.linesep).encode("ascii")
        return super().fold_binary(name, value)


_POLICY = _MsgIdPolicy()


def decode_header_value(s: Any) -> str:
    if s is None:
        return ""
    if isinstance(s, bytes):
        try:
            s = s.decode("utf-8")
        except UnicodeDecodeError:
            s = s.decode("latin-1", errors="replace")
    try:
        return str(make_header(decode_header(str(s))))
    except Exception:
        return str(s)


def address_of(value: str | None) -> str:
    """Bare `local@domain` from a header value like `Name <a@b>`.

    Returns "" unless the value is exactly one address in one of the two
    plain shapes. This is matched against a fixed grammar rather than handed
    to the stdlib parser on purpose: that parser silently drops what it can't
    read (`evil@x[], ok@y` comes back as just `ok@y`), and anything looser
    than the header a mail server will act on is a way round `allowed_from`.
    """
    # No control characters, and no whitespace but the plain space (Unicode
    # has line separators that a lenient reader would treat as a line break).
    if not value or any(ord(c) < 32 or ord(c) == 127 or (c.isspace() and c != " ") for c in value):
        return ""
    m = _ONE_ADDRESS_RE.fullmatch(value)
    if m is None:
        return ""
    return m.group(1) or m.group(2)


def domain_of(address: str | None) -> str:
    if not address or "@" not in address:
        return ""
    return address.rsplit("@", 1)[1].strip().lower()


def header_addresses(msg: Message, name: str) -> list[str]:
    """Every address in header `name` as a clean `Name <addr>` string."""
    out: list[str] = []
    values = [str(v) for v in msg.get_all(name, [])]
    for raw_name, addr in getaddresses(values):
        if not addr or "@" not in addr:
            continue
        out.append(formataddr((" ".join(decode_header_value(raw_name).split()), addr)))
    return out


# ── allowed_from ────────────────────────────────────────────────────────────


def _pattern_matches(pattern: str, address: str) -> bool:
    # `*` is the only wildcard; everything else is literal (fnmatch's `?` and
    # `[...]` would turn ordinary address characters into patterns).
    regex = ".*".join(re.escape(part) for part in pattern.strip().lower().split("*"))
    return re.fullmatch(regex, address.lower()) is not None


def check_from_allowed(from_address: str | None, allowed: Iterable[str] | None) -> None:
    """Raise `FromNotAllowedError` unless `from_address` matches `allowed`.

    An empty / missing `allowed` list means the mailbox is unrestricted.
    """
    patterns = [p for p in (allowed or []) if p and p.strip()]
    if not patterns:
        return
    addr = address_of(from_address)
    if not addr:
        raise FromNotAllowedError(
            f"From {from_address!r} is not a single valid address, so it cannot be "
            "checked against allowed_from"
        )
    if not any(_pattern_matches(p, addr) for p in patterns):
        raise FromNotAllowedError(
            f"From address {addr!r} is not permitted by this mailbox's allowed_from"
        )


# ── threading ───────────────────────────────────────────────────────────────


def extract_msgids(value: str | None) -> list[str]:
    """The `<id@host>` tokens found in a received header value, in order."""
    if not value:
        return []
    return [m for m in _MSGID_RE.findall(str(value)) if m.isascii()]


def normalize_msgids(value: str | Iterable[str] | None) -> list[str]:
    """Caller-supplied msg-ids (a header-style string or a list) as `<id>` tokens."""
    if not value:
        return []
    text = value if isinstance(value, str) else " ".join(str(v) for v in value)
    # Bracketed ids are taken as-is; bare ones (`id@host`) get their brackets.
    if _MSGID_TOKEN_RE.sub("", text).strip(" \t\r\n,"):
        raise MessageError(f"not a valid Message-ID list: {text!r}")
    ids = [f"<{tok.strip('<>')}>" for tok in _MSGID_TOKEN_RE.findall(text)]
    for msgid in ids:
        if len(msgid) < 3 or not msgid.isascii() or not msgid.isprintable():
            raise MessageError(f"invalid Message-ID: {msgid!r}")
    return ids


def reply_subject(subject: str | None) -> str:
    """`subject` with exactly one leading `Re:` — `RE: Re: hi` becomes `Re: hi`."""
    base = _RE_PREFIX.sub("", " ".join((subject or "").split()))
    return f"Re: {base}".strip()


def reply_references(
    references: str | None,
    in_reply_to: str | None,
    message_id: str | None,
) -> list[str]:
    """References for a reply: the original's References plus its Message-ID.

    Per RFC 5322 §3.6.4, an original with no References but a single-id
    In-Reply-To contributes that id instead, so the chain survives a client
    that only ever set In-Reply-To.
    """
    chain = extract_msgids(references)
    if not chain:
        parent = extract_msgids(in_reply_to)
        if len(parent) == 1:
            chain = parent
    for msgid in extract_msgids(message_id)[:1]:
        if msgid not in chain:
            chain.append(msgid)
    return chain


# ── catch-all detection ─────────────────────────────────────────────────────


def received_for(msg: Message, domain: str | None, login_address: str | None = None) -> str | None:
    """Which of the mailbox's own addresses this message was sent to.

    The first address on `domain`, looking at Delivered-To, X-Original-To,
    Envelope-To, then To, then Cc. A catch-all's server usually stamps the
    delivery headers with the catch-all login itself, so the login address is
    passed over there and the address the sender typed is taken from To / Cc.

    Two guards keep that from naming somebody else's address:
    - only the topmost Delivered-To / X-Original-To / Envelope-To counts. The
      receiving server puts its own on top; any below it are earlier hops, or
      something the sender wrote in.
    - if To / Cc name the login address itself, the mail was sent to this
      mailbox directly and the login address is the answer. Without this a
      message `To: me@corp, Cc: boss@corp` would be answered as the boss.
    """
    domain = (domain or "").strip().lower()
    if not domain:
        return None
    login = (login_address or "").strip().lower()

    def _ours(values: list[Any]) -> list[str]:
        pairs = getaddresses([str(v) for v in values])
        return [addr for _, addr in pairs if addr and domain_of(addr) == domain]

    stamped_login: str | None = None
    for name in RECEIVED_FOR_HEADERS[:3]:
        for addr in _ours(msg.get_all(name, [])[:1]):
            if login and addr.lower() == login:
                stamped_login = stamped_login or addr
                continue
            return addr

    typed = [a for name in RECEIVED_FOR_HEADERS[3:] for a in _ours(msg.get_all(name, []))]
    for addr in typed:
        if login and addr.lower() == login:
            return addr
    if typed:
        return typed[0]
    return stamped_login


# ── building ────────────────────────────────────────────────────────────────


def build_message(
    from_address: str,
    to: Iterable[str] | None = None,
    subject: str = "",
    body_text: str | None = None,
    body_html: str | None = None,
    cc: Iterable[str] | None = None,
    bcc: Iterable[str] | None = None,
    reply_to: str | None = None,
    in_reply_to: str | Iterable[str] | None = None,
    references: str | Iterable[str] | None = None,
) -> EmailMessage:
    """Assemble an RFC 5322 message with a fresh Message-ID and Date.

    `bcc` is written as a real Bcc header so a draft / Sent copy remembers it;
    it is stripped again before the message goes on the wire. With neither
    body the message gets an empty text part (a draft can be body-less).
    """
    to_list = list(to or [])
    cc_list = list(cc or [])
    bcc_list = list(bcc or [])
    reply_ids = normalize_msgids(in_reply_to)
    ref_ids = normalize_msgids(references)

    msg = EmailMessage(policy=_POLICY)
    try:
        msg["From"] = from_address
        if to_list:
            msg["To"] = ", ".join(to_list)
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        # Derive the Message-ID domain from the sender so it matches the From: header —
        # mismatched Message-ID domains are a strong spam signal.
        sender_domain = domain_of(address_of(from_address))
        msg["Message-ID"] = make_msgid(domain=sender_domain) if sender_domain else make_msgid()
        msg["User-Agent"] = USER_AGENT
        if cc_list:
            msg["Cc"] = ", ".join(cc_list)
        if bcc_list:
            msg["Bcc"] = ", ".join(bcc_list)
        if reply_to:
            msg["Reply-To"] = reply_to
        if reply_ids:
            msg["In-Reply-To"] = " ".join(reply_ids)
        if ref_ids:
            msg["References"] = " ".join(ref_ids)
    except ValueError as e:
        # EmailMessage refuses CR/LF in a header value — that is header injection.
        raise MessageError(f"invalid header value: {e}") from e

    if body_text is not None:
        msg.set_content(body_text)
    if body_html is not None:
        if body_text is None:
            msg.set_content("This message requires an HTML-capable client.")
        msg.add_alternative(body_html, subtype="html")
    if body_text is None and body_html is None:
        msg.set_content("")
    return msg


def to_bytes(msg: Message) -> bytes:
    """Serialize with CRLF line endings, as both IMAP APPEND and SMTP expect."""
    return msg.as_bytes(policy=msg.policy.clone(linesep="\r\n"))


def strip_bcc(raw: bytes) -> bytes:
    """Drop Bcc / Resent-Bcc header fields from a raw message, touching nothing else."""
    end = _HEADER_END_RE.search(raw)
    if end is None:
        return _BCC_RE.sub(b"", raw)
    head, body = raw[: end.end()], raw[end.end() :]
    return _BCC_RE.sub(b"", head) + body
