"""Mailbox-level operations shared by the HTTP API and the MCP server.

Everything here takes a whole `MailboxConfig`, because sending, replying and
drafts each need more than one of {imap, smtp, allowed_from}. The callers
have already checked the mailbox has the protocols an operation needs.
"""

from __future__ import annotations

import email
import logging
from email.message import Message
from email.utils import formatdate, getaddresses, make_msgid
from typing import Any, Iterable

from . import imap_client, smtp_client
from .config import MailboxConfig
from .message import (
    MessageError,
    address_of,
    build_message,
    check_from_allowed,
    decode_header_value,
    domain_of,
    extract_msgids,
    header_addresses,
    received_for,
    reply_references,
    reply_subject,
    strip_bcc,
    to_bytes,
)
from .smtp_client import SmtpError

log = logging.getLogger("mailboxd")


def mailbox_identity(mb: MailboxConfig) -> tuple[str | None, str | None]:
    """The mailbox's own (login address, domain), both lower-cased.

    Taken from the IMAP login, falling back to the SMTP login and then the
    configured From, for accounts whose username isn't an email address.
    """
    candidates = [
        mb.imap.username if mb.imap is not None else "",
        mb.smtp.username if mb.smtp is not None else "",
        mb.smtp.from_address if mb.smtp is not None else "",
    ]
    for candidate in candidates:
        addr = address_of(candidate)
        if addr:
            return addr.lower(), domain_of(addr)
    return None, None


def _default_from(mb: MailboxConfig) -> str:
    if mb.smtp is not None:
        return mb.smtp.from_address
    login, _ = mailbox_identity(mb)
    if login is None:
        raise MessageError("from_address is required: this mailbox has no default sender")
    return login


def _text(msg: Message, name: str) -> str:
    """A header as one decoded, unfolded line ("" when absent)."""
    return " ".join(decode_header_value(msg.get(name)).split())


def _summary(msg: Message) -> dict[str, Any]:
    return {
        "from": _text(msg, "From"),
        "to": _text(msg, "To"),
        "subject": _text(msg, "Subject"),
        "message_id": _text(msg, "Message-ID"),
    }


def _file_sent(mb: MailboxConfig, raw: bytes) -> dict[str, Any]:
    """Copy a message that has already gone out into the Sent folder.

    Never raises: the mail is sent, and failing the call now would invite a
    retry that sends it twice. A failed copy is reported as a warning.
    """
    if mb.imap is None or not mb.imap.save_sent:
        return {"saved_to_sent": False}
    try:
        folder = imap_client.save_sent(mb.imap, raw)
    except Exception as e:  # anything at all — see the docstring
        log.warning("mailbox %s: sent, but the Sent copy failed: %s", mb.name, e)
        return {
            "saved_to_sent": False,
            "warnings": [f"message was sent, but saving a copy to Sent failed: {e}"],
        }
    return {"saved_to_sent": True, "sent_folder": folder}


def get_message(
    mb: MailboxConfig, uid: str, folder: str | None = None, reader: bool = False
) -> dict[str, Any]:
    assert mb.imap is not None
    return imap_client.fetch_message(
        mb.imap, uid, folder=folder, reader=reader, identity=mailbox_identity(mb)
    )


# ── Sending ─────────────────────────────────────────────────────────────────


def send(
    mb: MailboxConfig,
    to: Iterable[str],
    subject: str,
    body_text: str | None = None,
    body_html: str | None = None,
    cc: Iterable[str] | None = None,
    bcc: Iterable[str] | None = None,
    from_address: str | None = None,
    reply_to: str | None = None,
    in_reply_to: str | None = None,
    references: str | Iterable[str] | None = None,
) -> dict[str, Any]:
    assert mb.smtp is not None
    to_list = list(to)
    if not to_list:
        raise SmtpError("`to` must contain at least one address")
    if body_text is None and body_html is None:
        raise SmtpError("must supply body_text and/or body_html")

    sender = from_address or mb.smtp.from_address
    check_from_allowed(sender, mb.allowed_from)
    msg = build_message(
        from_address=sender,
        to=to_list,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        cc=cc,
        bcc=bcc,
        reply_to=reply_to,
        in_reply_to=in_reply_to,
        references=references,
    )
    return _deliver(mb, msg, to_list + list(cc or []) + list(bcc or []))


def _deliver(mb: MailboxConfig, msg: Message, rcpts: list[str]) -> dict[str, Any]:
    assert mb.smtp is not None
    smtp_client.send_message(mb.smtp, msg, rcpts)
    return {**_summary(msg), **_file_sent(mb, to_bytes(msg))}


def reply(
    mb: MailboxConfig,
    uid: str,
    folder: str | None = None,
    body_text: str | None = None,
    body_html: str | None = None,
    from_address: str | None = None,
    draft: bool = False,
) -> dict[str, Any]:
    """Reply to the sender of message `uid`, threaded under it.

    With no `from_address` the reply goes out from the address the original
    was sent to (`received_for`), which is what makes a catch-all mailbox
    answer as the right identity.
    """
    assert mb.imap is not None
    if body_text is None and body_html is None:
        raise MessageError("must supply body_text and/or body_html")

    orig = email.message_from_bytes(imap_client.fetch_raw(mb.imap, uid, folder=folder))
    to = header_addresses(orig, "Reply-To") or header_addresses(orig, "From")
    if not to:
        raise MessageError(f"message {uid} has no Reply-To or From address to reply to")

    login, domain = mailbox_identity(mb)
    sender = from_address or received_for(orig, domain, login) or _default_from(mb)
    check_from_allowed(sender, mb.allowed_from)

    message_id = _text(orig, "Message-ID")
    msg = build_message(
        from_address=sender,
        to=to,
        subject=reply_subject(_text(orig, "Subject")),
        body_text=body_text,
        body_html=body_html,
        in_reply_to=extract_msgids(message_id)[:1],
        references=reply_references(
            _text(orig, "References"), _text(orig, "In-Reply-To"), message_id
        ),
    )
    if draft:
        return {**_store_draft(mb, msg), "draft": True}
    return {**_deliver(mb, msg, to), "draft": False}


# ── Drafts ──────────────────────────────────────────────────────────────────


def _store_draft(mb: MailboxConfig, msg: Message, replaces: str | None = None) -> dict[str, Any]:
    assert mb.imap is not None
    message_id = _summary(msg)["message_id"]
    if replaces is None:
        stored = imap_client.save_draft(mb.imap, to_bytes(msg), message_id=message_id)
    else:
        stored = imap_client.replace_draft(mb.imap, replaces, to_bytes(msg), message_id=message_id)
    return {"uid": stored["uid"], "folder": stored["folder"], **_summary(msg)}


def save_draft(
    mb: MailboxConfig,
    to: Iterable[str] | None = None,
    subject: str | None = None,
    body_text: str | None = None,
    body_html: str | None = None,
    cc: Iterable[str] | None = None,
    bcc: Iterable[str] | None = None,
    from_address: str | None = None,
    reply_to: str | None = None,
    in_reply_to: str | None = None,
    references: str | Iterable[str] | None = None,
) -> dict[str, Any]:
    msg = build_message(
        from_address=from_address or _default_from(mb),
        to=to,
        subject=subject or "",
        body_text=body_text,
        body_html=body_html,
        cc=cc,
        bcc=bcc,
        reply_to=reply_to,
        in_reply_to=in_reply_to,
        references=references,
    )
    return _store_draft(mb, msg)


def list_drafts(mb: MailboxConfig, limit: int = 50) -> dict[str, Any]:
    assert mb.imap is not None
    return imap_client.list_drafts(mb.imap, limit=limit)


def get_draft(mb: MailboxConfig, uid: str, reader: bool = False) -> dict[str, Any]:
    assert mb.imap is not None
    folder, raw = imap_client.fetch_draft_raw(mb.imap, uid)
    return {**imap_client.decode_message(raw, uid=uid, reader=reader), "folder": folder}


def update_draft(
    mb: MailboxConfig,
    uid: str,
    to: Iterable[str] | None = None,
    subject: str | None = None,
    body_text: str | None = None,
    body_html: str | None = None,
    cc: Iterable[str] | None = None,
    bcc: Iterable[str] | None = None,
    from_address: str | None = None,
    reply_to: str | None = None,
    in_reply_to: str | None = None,
    references: str | Iterable[str] | None = None,
) -> dict[str, Any]:
    """Replace draft `uid` with a new version; returns the new UID.

    A field left as None keeps the old draft's value, so changing the body of
    a reply draft doesn't lose its recipients or threading. Pass an empty
    list / empty string to clear one. The two bodies travel together: giving
    either replaces both.
    """
    assert mb.imap is not None
    _, raw = imap_client.fetch_draft_raw(mb.imap, uid)
    old = email.message_from_bytes(raw)
    decoded = imap_client.decode_message(raw)
    if decoded["attachments"]:
        # The new version is rebuilt from fields, and the old one is expunged.
        raise MessageError(
            f"draft {uid} has attachments, which update_draft cannot carry over — "
            "edit it in a mail client, or delete it and save a new draft"
        )
    if body_text is None and body_html is None:
        body_text, body_html = decoded["body_text"], decoded["body_html"]

    def _addrs(new: Iterable[str] | None, header: str) -> list[str]:
        return header_addresses(old, header) if new is None else list(new)

    msg = build_message(
        from_address=from_address or (_addrs(None, "From") or [_default_from(mb)])[0],
        to=_addrs(to, "To"),
        subject=_text(old, "Subject") if subject is None else subject,
        body_text=body_text,
        body_html=body_html,
        cc=_addrs(cc, "Cc"),
        bcc=_addrs(bcc, "Bcc"),
        reply_to=", ".join(_addrs(None, "Reply-To")) if reply_to is None else reply_to,
        in_reply_to=(
            extract_msgids(_text(old, "In-Reply-To")) if in_reply_to is None else in_reply_to
        ),
        references=extract_msgids(_text(old, "References")) if references is None else references,
    )
    return _store_draft(mb, msg, replaces=uid)


def delete_draft(mb: MailboxConfig, uid: str) -> dict[str, Any]:
    assert mb.imap is not None
    return {"ok": True, "folder": imap_client.delete_draft(mb.imap, uid)}


def send_draft(mb: MailboxConfig, uid: str) -> dict[str, Any]:
    """Send draft `uid` as saved, file a copy in Sent, then delete the draft."""
    assert mb.imap is not None and mb.smtp is not None
    _, raw = imap_client.fetch_draft_raw(mb.imap, uid)
    msg = email.message_from_bytes(raw)

    sender = address_of(", ".join(" ".join(str(v).split()) for v in msg.get_all("From", [])))
    if not sender:
        raise MessageError(f"draft {uid} has no single valid From address")
    check_from_allowed(sender, mb.allowed_from)
    fields = [str(v) for name in ("To", "Cc", "Bcc") for v in msg.get_all(name, [])]
    rcpts = [addr for _, addr in getaddresses(fields) if addr]
    if not rcpts:
        raise MessageError(f"draft {uid} has no recipients")

    # Our own drafts always carry both; one written by another client may not.
    if not msg.get("Message-ID") or not msg.get("Date"):
        if not msg.get("Message-ID"):
            msg["Message-ID"] = make_msgid(domain=domain_of(sender) or None)
        if not msg.get("Date"):
            msg["Date"] = formatdate(localtime=True)
        raw = to_bytes(msg)

    smtp_client.send_raw(mb.smtp, strip_bcc(raw), sender, rcpts)

    result = {**_summary(msg), **_file_sent(mb, raw)}
    try:
        imap_client.delete_draft(mb.imap, uid)
        result["draft_deleted"] = True
    except Exception as e:  # the mail is out: report, never fail (see _file_sent)
        log.warning("mailbox %s: sent draft %s but could not delete it: %s", mb.name, uid, e)
        result["draft_deleted"] = False
        result.setdefault("warnings", []).append(
            f"message was sent, but draft {uid} could not be deleted: {e} — "
            "delete it rather than sending it again"
        )
    return result
