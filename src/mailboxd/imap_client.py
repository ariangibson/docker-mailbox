"""IMAP operations: list folders, list/fetch/delete messages, drafts, Sent copies.

Connections are short-lived — opened per request and closed in a finally
block. Long-lived pooling would be nice but isn't worth the lifecycle
complexity for a control-plane API. UIDs (not sequence numbers) are used
everywhere so subsequent operations stay stable across mailbox mutations.
"""

from __future__ import annotations

import base64
import email
import imaplib
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any, Iterator

import html2text

from .config import ImapConfig, MailboxConfig, redact, tls_context
from .message import decode_header_value as _decode
from .message import received_for


def _html_to_reader(html: str) -> str:
    """Strip HTML chrome and return readable plain-text/markdown.

    Designed for email bodies: drops style/script, flattens tables, keeps
    links inline, ignores images. Output is wrapped at a sane width so
    long marketing-email lines don't blow out terminals.
    """
    h = html2text.HTML2Text()
    h.body_width = 0
    h.ignore_images = True
    h.ignore_emphasis = False
    h.single_line_break = False
    h.skip_internal_links = True
    h.unicode_snob = True
    return h.handle(html).strip()


class ImapError(Exception):
    """Raised when an IMAP operation fails."""


# IMAP command lines are CRLF-delimited and its atoms/quoted-strings cannot
# contain CR, LF or NUL. Python's imaplib passes caller-supplied arguments to
# the server verbatim (no sanitization), so a CR/LF smuggled into a
# folder/uid/search value would terminate the current command and inject an
# additional, attacker-chosen IMAP command onto the same authenticated
# connection (issue #1: command injection, incl. STORE+EXPUNGE through a
# read-only GET route). Reject those bytes at the boundary, before any value
# reaches imaplib.
_FORBIDDEN_CHARS = ("\r", "\n", "\x00")

# A UID (or sequence-set) is only ever digits and the range punctuation
# `,` `:` `*`. Anything else cannot be a valid UID and is refused outright,
# which also makes UID values injection-proof by construction.
_UID_RE = re.compile(r"^[0-9][0-9,:*]*$")
# Draft and reply operations act on exactly one message, so a sequence-set
# like `1:*` (which `_UID_RE` allows) must not get through to a STORE.
_SINGLE_UID_RE = re.compile(r"^[0-9]+$")

_TIMEOUT = 30

# RFC 6154 SPECIAL-USE attributes, and the folder name to assume when the
# server advertises none and the config doesn't name one either.
_SPECIAL_USE = {"drafts": ("\\drafts", "Drafts"), "sent": ("\\sent", "Sent")}
_LIST_RE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.*)$')
_APPENDUID_RE = re.compile(rb"\[APPENDUID\s+\d+\s+(\d+)\]", re.IGNORECASE)


def _reject_control_chars(value: str, field: str) -> None:
    if any(ch in value for ch in _FORBIDDEN_CHARS):
        raise ImapError(f"invalid {field}: control characters are not allowed")


def _validate_uid(uid: str) -> None:
    # fullmatch, not match: `$` would let a trailing newline through.
    if not _UID_RE.fullmatch(uid):
        raise ImapError(f"invalid uid: {uid!r}")


def _validate_single_uid(uid: str) -> None:
    if not _SINGLE_UID_RE.fullmatch(uid):
        raise ImapError(f"invalid uid: {uid!r} (expected a single numeric UID)")


@contextmanager
def _connect(cfg: ImapConfig) -> Iterator[imaplib.IMAP4]:
    # Whatever goes wrong in here leaves as an ImapError, so callers have one
    # thing to catch. Server text is redacted and the cause dropped: a server
    # may echo the LOGIN command it rejected, password included.
    try:
        if cfg.tls == "ssl":
            conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(
                cfg.host, cfg.port, ssl_context=tls_context(cfg.tls_verify), timeout=_TIMEOUT
            )
        else:
            conn = imaplib.IMAP4(cfg.host, cfg.port, timeout=_TIMEOUT)
            if cfg.tls == "starttls":
                conn.starttls(ssl_context=tls_context(cfg.tls_verify))
    except (imaplib.IMAP4.error, OSError) as e:
        raise ImapError(f"connection failed: {redact(str(e), cfg.password)}") from None
    try:
        try:
            conn.login(cfg.username, cfg.password)
        except (imaplib.IMAP4.error, OSError) as e:
            raise ImapError(f"login failed: {redact(str(e), cfg.password)}") from None
        except UnicodeError:
            raise ImapError(
                "login failed: the username or password has non-ASCII characters, "
                "which IMAP LOGIN cannot carry"
            ) from None
        try:
            yield conn
        except (imaplib.IMAP4.error, OSError) as e:
            # A `BAD` reply or a dropped connection raises out of imaplib
            # instead of coming back as a status.
            raise ImapError(redact(str(e), cfg.password)) from None
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def check_login(cfg: ImapConfig) -> None:
    """Connect and log in, nothing more. Raises `ImapError` / `OSError` on failure."""
    with _connect(cfg):
        pass


def _mutf7(name: str) -> str:
    """A mailbox name in IMAP's modified UTF-7 (RFC 3501 §5.1.3)."""
    out: list[str] = []
    run: list[str] = []

    def _flush() -> None:
        if run:
            b64 = base64.b64encode("".join(run).encode("utf-16-be")).decode("ascii")
            out.append("&" + b64.rstrip("=").replace("/", ",") + "-")
            run.clear()

    for ch in name:
        if " " <= ch <= "~":
            _flush()
            out.append("&-" if ch == "&" else ch)
        else:
            run.append(ch)
    _flush()
    return "".join(out)


def _quote(folder: str) -> str:
    _reject_control_chars(folder, "folder")
    # A name the server gave us is already ASCII (modified UTF-7) and goes back
    # untouched; one typed by a person ("Envoyés") has to be encoded.
    if not folder.isascii():
        folder = _mutf7(folder)
    # Send the name as a properly-escaped IMAP quoted string so an embedded
    # `"` or `\` cannot break out of the quoting either.
    escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _select(conn: imaplib.IMAP4, folder: str, readonly: bool = False) -> None:
    typ, _ = conn.select(_quote(folder), readonly=readonly)
    if typ != "OK":
        raise ImapError(f"could not select folder {folder!r}")


def _capabilities(conn: imaplib.IMAP4) -> set[str]:
    """Capabilities as advertised *after* login.

    imaplib only records the pre-auth set, which on most servers leaves out
    the ones that matter here (UIDPLUS, SPECIAL-USE, LIST-EXTENDED).
    """
    typ, data = conn.capability()
    if typ == "OK" and data and data[-1]:
        return set(data[-1].decode("ascii", errors="replace").upper().split())
    return {str(c).upper() for c in conn.capabilities}


def _parse_list(data: list[Any]) -> list[tuple[set[str], str]]:
    """LIST response lines as (lower-cased attributes, mailbox name) pairs."""
    out: list[tuple[set[str], str]] = []
    for raw in data or []:
        if not raw:
            continue
        literal: bytes | None = None
        if isinstance(raw, tuple):
            # Name sent as an IMAP literal: (b'(\\Drafts) "/" {6}', b'Drafts')
            raw, literal = raw[0], raw[1]
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        m = _LIST_RE.match(line)
        if m is None:
            continue
        name = m.group("name").strip()
        if literal is not None:
            name = literal.decode("utf-8", errors="replace")
        elif len(name) >= 2 and name[0] == name[-1] == '"':
            name = re.sub(r"\\(.)", r"\1", name[1:-1])
        if name:
            out.append(({f.lower() for f in m.group("flags").split()}, name))
    return out


def _special_folder(conn: imaplib.IMAP4, kind: str, configured: str | None) -> str:
    """Resolve the Drafts / Sent folder: SPECIAL-USE, then config, then the default."""
    attr, default = _SPECIAL_USE[kind]

    def _flagged(data: list[Any]) -> str | None:
        return next((name for flags, name in _parse_list(data) if attr in flags), None)

    typ, data = conn.list()
    if typ != "OK":
        raise ImapError("LIST failed")
    found = _flagged(data)
    if found is None and {"SPECIAL-USE", "LIST-EXTENDED"} <= _capabilities(conn):
        # RFC 6154 lets a server leave the attributes out of a plain LIST and
        # only return them when asked for explicitly.
        typ, data = conn.list('""', '"*" RETURN (SPECIAL-USE)')
        if typ == "OK":
            found = _flagged(data)
    return found or configured or default


def list_folders(cfg: ImapConfig) -> list[str]:
    with _connect(cfg) as conn:
        typ, data = conn.list()
        if typ != "OK":
            raise ImapError("LIST failed")
        out: list[str] = []
        for raw in data or []:
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            # IMAP LIST response: (\\HasNoChildren) "/" "INBOX"
            parts = line.rsplit(" ", 1)
            if len(parts) == 2:
                folder = parts[1].strip().strip('"')
                if folder:
                    out.append(folder)
        return out


def build_search_criteria(
    text: str | None = None,
    from_: str | None = None,
    to: str | None = None,
    subject: str | None = None,
    body: str | None = None,
    since: str | None = None,
    before: str | None = None,
    unseen: bool | None = None,
    seen: bool | None = None,
    flagged: bool | None = None,
    answered: bool | None = None,
    larger_than: int | None = None,
    smaller_than: int | None = None,
) -> list[str | bytes]:
    """Translate a structured search request into an IMAP SEARCH criteria list.

    Returns a list of tokens ready to splat into `conn.uid("SEARCH", *tokens)`.
    Strings with non-ASCII content are encoded as IMAP `CHARSET UTF-8 ...`
    literal byte tokens — most modern servers accept this.
    """
    crit: list[str | bytes] = []

    def _q(value: str) -> str | bytes:
        _reject_control_chars(value, "search term")
        # IMAP atoms must be quoted strings or literals. imaplib quotes
        # automatically when the value is a plain `str`, but trips on
        # non-ASCII. For non-ASCII we hand back a literal byte token.
        if any(ord(c) > 127 for c in value):
            return value.encode("utf-8")
        # Escape backslash + quote for inside a quoted string.
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    has_nonascii = False

    def _add_text(key: str, value: str) -> None:
        nonlocal has_nonascii
        if any(ord(c) > 127 for c in value):
            has_nonascii = True
        crit.append(key)
        crit.append(_q(value))

    if from_:
        _add_text("FROM", from_)
    if to:
        _add_text("TO", to)
    if subject:
        _add_text("SUBJECT", subject)
    if body:
        _add_text("BODY", body)
    if text:
        _add_text("TEXT", text)
    if since:
        _reject_control_chars(since, "since")
        crit.extend(["SINCE", since])
    if before:
        _reject_control_chars(before, "before")
        crit.extend(["BEFORE", before])
    if unseen:
        crit.append("UNSEEN")
    if seen:
        crit.append("SEEN")
    if flagged:
        crit.append("FLAGGED")
    if answered:
        crit.append("ANSWERED")
    if larger_than is not None:
        crit.extend(["LARGER", str(larger_than)])
    if smaller_than is not None:
        crit.extend(["SMALLER", str(smaller_than)])

    if not crit:
        crit.append("ALL")

    if has_nonascii:
        return ["CHARSET", "UTF-8", *crit]
    return crit


def _uid_search(conn: imaplib.IMAP4, criteria: list[str | bytes] | str) -> list[bytes]:
    if isinstance(criteria, str):
        typ, data = conn.uid("SEARCH", None, criteria)  # type: ignore[arg-type]
    else:
        typ, data = conn.uid("SEARCH", *criteria)  # type: ignore[arg-type]
    if typ != "OK":
        raise ImapError(f"SEARCH failed: {data!r}")
    return (data[0] or b"").split()


def list_messages(
    cfg: ImapConfig,
    folder: str | None = None,
    limit: int = 50,
    search: str = "ALL",
) -> list[dict[str, Any]]:
    folder = folder or cfg.default_folder
    _reject_control_chars(folder, "folder")
    _reject_control_chars(search, "search")
    with _connect(cfg) as conn:
        _select(conn, folder, readonly=True)
        uids = _uid_search(conn, search)
        return _fetch_headers(conn, uids, limit)


def search_messages(
    cfg: ImapConfig,
    folder: str | None = None,
    limit: int = 50,
    **criteria: Any,
) -> list[dict[str, Any]]:
    """Structured search inside one IMAP folder.

    Accepts the same keyword arguments as `build_search_criteria`.
    """
    folder = folder or cfg.default_folder
    _reject_control_chars(folder, "folder")
    spec = build_search_criteria(**criteria)
    with _connect(cfg) as conn:
        _select(conn, folder, readonly=True)
        uids = _uid_search(conn, spec)
        return _fetch_headers(conn, uids, limit)


def _fetch_headers(conn: imaplib.IMAP4, uids: list[bytes], limit: int) -> list[dict[str, Any]]:
    # Newest last in IMAP search → reverse, take limit
    uids = list(reversed(uids))[: max(0, limit)]
    if not uids:
        return []
    uid_set = b",".join(uids).decode("ascii")
    typ, fdata = conn.uid(
        "FETCH",
        uid_set,
        "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)] FLAGS RFC822.SIZE)",
    )
    if typ != "OK":
        raise ImapError("FETCH headers failed")
    return _parse_header_fetch(fdata)


def _parse_header_fetch(fdata: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in fdata:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        meta_raw, body_raw = item[0], item[1]
        meta = (
            meta_raw.decode("utf-8", errors="replace")
            if isinstance(meta_raw, bytes)
            else str(meta_raw)
        )
        uid = _extract(meta, "UID")
        size = _extract(meta, "RFC822.SIZE")
        flags = _extract_flags(meta)
        msg = email.message_from_bytes(
            body_raw if isinstance(body_raw, bytes) else body_raw.encode()
        )
        out.append(
            {
                "uid": uid,
                "size": int(size) if size and size.isdigit() else None,
                "flags": flags,
                "from": _decode(msg.get("From")),
                "to": _decode(msg.get("To")),
                "subject": _decode(msg.get("Subject")),
                "date": _decode(msg.get("Date")),
                "message_id": _decode(msg.get("Message-ID")),
            }
        )
    return out


def _extract(meta: str, key: str) -> str:
    # Crude but reliable: find `KEY VALUE` token in the metadata string.
    parts = meta.replace("(", " ").replace(")", " ").split()
    for i, p in enumerate(parts):
        if p == key and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def _extract_flags(meta: str) -> list[str]:
    i = meta.find("FLAGS")
    if i < 0:
        return []
    j = meta.find("(", i)
    k = meta.find(")", j)
    if j < 0 or k < 0:
        return []
    return meta[j + 1 : k].split()


def _fetch_raw(conn: imaplib.IMAP4, uid: str) -> bytes:
    typ, data = conn.uid("FETCH", uid, "(RFC822)")
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        raise ImapError(f"could not fetch UID {uid}")
    raw = data[0][1]
    return raw if isinstance(raw, bytes) else raw.encode()


def fetch_raw(cfg: ImapConfig, uid: str, folder: str | None = None) -> bytes:
    """The raw RFC 822 bytes of one message."""
    folder = folder or cfg.default_folder
    _validate_single_uid(uid)
    _reject_control_chars(folder, "folder")
    with _connect(cfg) as conn:
        _select(conn, folder, readonly=True)
        return _fetch_raw(conn, uid)


def fetch_message(
    cfg: ImapConfig,
    uid: str,
    folder: str | None = None,
    reader: bool = False,
    identity: tuple[str | None, str | None] = (None, None),
) -> dict[str, Any]:
    """Fetch and decode one message.

    `identity` is the mailbox's (login address, domain); it is what
    `received_for` is computed against.
    """
    folder = folder or cfg.default_folder
    _validate_uid(uid)
    _reject_control_chars(folder, "folder")
    with _connect(cfg) as conn:
        _select(conn, folder, readonly=True)
        return decode_message(_fetch_raw(conn, uid), uid=uid, reader=reader, identity=identity)


def decode_message(
    raw: bytes,
    uid: str | None = None,
    reader: bool = False,
    identity: tuple[str | None, str | None] = (None, None),
) -> dict[str, Any]:
    """Raw RFC 822 bytes as the dict `fetch_message` returns."""
    msg = email.message_from_bytes(raw)
    return _serialize_message(msg, uid=uid, reader=reader, identity=identity)


def _header(msg: Message, name: str) -> str:
    # Unfolded, and every instance kept: Delivered-To and friends are added
    # once per hop.
    values = (" ".join(_decode(raw).split()) for raw in msg.get_all(name, []))
    return ", ".join(v for v in values if v)


def _serialize_message(
    msg: Message,
    uid: str | None = None,
    reader: bool = False,
    identity: tuple[str | None, str | None] = (None, None),
) -> dict[str, Any]:
    body_text: str | None = None
    body_html: str | None = None
    attachments: list[dict[str, Any]] = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                payload = part.get_payload(decode=True) or b""
                attachments.append(
                    {
                        "filename": _decode(part.get_filename() or ""),
                        "content_type": ctype,
                        "size": len(payload),
                    }
                )
                continue
            if ctype == "text/plain" and body_text is None:
                body_text = _payload_str(part)
            elif ctype == "text/html" and body_html is None:
                body_html = _payload_str(part)
    else:
        ctype = msg.get_content_type()
        if ctype == "text/html":
            body_html = _payload_str(msg)
        else:
            body_text = _payload_str(msg)

    body_reader: str | None = None
    if reader:
        if body_html:
            body_reader = _html_to_reader(body_html)
        elif body_text:
            body_reader = body_text.strip() or None

    return {
        "uid": uid,
        "from": _decode(msg.get("From")),
        "to": _decode(msg.get("To")),
        "cc": _decode(msg.get("Cc")),
        "bcc": _decode(msg.get("Bcc")),
        "reply_to": _decode(msg.get("Reply-To")),
        "subject": _decode(msg.get("Subject")),
        "date": _decode(msg.get("Date")),
        "message_id": _decode(msg.get("Message-ID")),
        "in_reply_to": _header(msg, "In-Reply-To"),
        "references": " ".join(_header(msg, "References").replace(",", " ").split()),
        "delivered_to": _header(msg, "Delivered-To"),
        "x_original_to": _header(msg, "X-Original-To"),
        "envelope_to": _header(msg, "Envelope-To"),
        "received_for": received_for(msg, domain=identity[1], login_address=identity[0]),
        "body_text": body_text,
        "body_html": body_html,
        "body_reader": body_reader,
        "attachments": attachments,
    }


def _payload_str(part: Message) -> str:
    raw = part.get_payload(decode=True)
    if not isinstance(raw, bytes):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def delete_message(cfg: ImapConfig, uid: str, folder: str | None = None) -> None:
    folder = folder or cfg.default_folder
    # One message per call. A sequence-set here (`1:*`) would empty the folder,
    # permanently, on the say-so of a single request.
    _validate_single_uid(uid)
    _reject_control_chars(folder, "folder")
    with _connect(cfg) as conn:
        _select(conn, folder, readonly=False)
        typ, _ = conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
        if typ != "OK":
            raise ImapError(f"STORE \\Deleted failed for UID {uid}")
        conn.expunge()


# ── Drafts / Sent ───────────────────────────────────────────────────────────


def _append(
    conn: imaplib.IMAP4,
    folder: str,
    raw: bytes,
    flags: str,
    hint: str,
    message_id: str | None = None,
) -> str | None:
    """APPEND `raw` to `folder` and return its new UID if it can be learned.

    The UID comes from the UIDPLUS `APPENDUID` response code; a server without
    UIDPLUS is asked for it by Message-ID instead. `hint` names the config key
    to set when the folder turns out not to exist.
    """
    internal_date = imaplib.Time2Internaldate(time.time())
    # typeshed declares a `str` return; like every imaplib command it is (typ, [data]).
    resp: Any = conn.append(_quote(folder), flags, internal_date, raw)
    typ, data = resp
    if typ != "OK":
        detail = data[-1].decode("utf-8", errors="replace") if data and data[-1] else ""
        raise ImapError(
            f"could not append to folder {folder!r}: {detail or typ} "
            f"(if the folder has another name on this server, set `{hint}`)"
        )
    m = _APPENDUID_RE.search(data[-1] or b"") if data else None
    if m is not None:
        return m.group(1).decode("ascii")
    if not message_id:
        return None
    _reject_control_chars(message_id, "message id")
    quoted = message_id.replace("\\", "\\\\").replace('"', '\\"')
    _select(conn, folder, readonly=True)
    typ, found = conn.uid("SEARCH", "HEADER", "Message-ID", f'"{quoted}"')
    uids = (found[0] or b"").split() if typ == "OK" and found else []
    return max(uids, key=int).decode("ascii") if uids else None


def _delete_uid(conn: imaplib.IMAP4, uid: str) -> None:
    """Flag one UID \\Deleted and expunge it (the folder must be selected read-write)."""
    typ, _ = conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
    if typ != "OK":
        raise ImapError(f"STORE \\Deleted failed for UID {uid}")
    if "UIDPLUS" in _capabilities(conn):
        # UID EXPUNGE removes only this message; a bare EXPUNGE would also purge
        # anything another client had flagged \Deleted but not yet expunged.
        typ, _ = conn.uid("EXPUNGE", uid)
    else:
        typ, _ = conn.expunge()
    if typ != "OK":
        raise ImapError(f"EXPUNGE failed for UID {uid}")


def _require_uid(conn: imaplib.IMAP4, uid: str, folder: str) -> None:
    typ, data = conn.uid("FETCH", uid, "(FLAGS)")
    if typ != "OK" or not data or data[0] is None:
        raise ImapError(f"no message with UID {uid} in folder {folder!r}")


def save_draft(cfg: ImapConfig, raw: bytes, message_id: str | None = None) -> dict[str, Any]:
    """APPEND a message to the Drafts folder, flagged \\Draft.

    Returns `{"folder", "uid"}`; `uid` is None only when the server gives no
    way to learn it.
    """
    with _connect(cfg) as conn:
        folder = _special_folder(conn, "drafts", cfg.drafts_folder)
        uid = _append(conn, folder, raw, r"(\Draft \Seen)", "imap.drafts_folder", message_id)
        return {"folder": folder, "uid": uid}


def replace_draft(
    cfg: ImapConfig, uid: str, raw: bytes, message_id: str | None = None
) -> dict[str, Any]:
    """Swap a draft for a new version: append first, then delete + expunge the old UID.

    IMAP has no in-place edit. Appending before deleting means a failure
    half-way leaves both versions rather than neither.
    """
    _validate_single_uid(uid)
    with _connect(cfg) as conn:
        folder = _special_folder(conn, "drafts", cfg.drafts_folder)
        _select(conn, folder, readonly=False)
        _require_uid(conn, uid, folder)
        new_uid = _append(conn, folder, raw, r"(\Draft \Seen)", "imap.drafts_folder", message_id)
        _select(conn, folder, readonly=False)
        _delete_uid(conn, uid)
        return {"folder": folder, "uid": new_uid}


def list_drafts(cfg: ImapConfig, limit: int = 50) -> dict[str, Any]:
    with _connect(cfg) as conn:
        folder = _special_folder(conn, "drafts", cfg.drafts_folder)
        _select(conn, folder, readonly=True)
        uids = _uid_search(conn, "ALL")
        return {"folder": folder, "messages": _fetch_headers(conn, uids, limit)}


def fetch_draft_raw(cfg: ImapConfig, uid: str) -> tuple[str, bytes]:
    """(Drafts folder name, raw RFC 822 bytes) for one draft."""
    _validate_single_uid(uid)
    with _connect(cfg) as conn:
        folder = _special_folder(conn, "drafts", cfg.drafts_folder)
        _select(conn, folder, readonly=True)
        return folder, _fetch_raw(conn, uid)


def delete_draft(cfg: ImapConfig, uid: str) -> str:
    """Delete + expunge one draft. Returns the Drafts folder name."""
    _validate_single_uid(uid)
    with _connect(cfg) as conn:
        folder = _special_folder(conn, "drafts", cfg.drafts_folder)
        _select(conn, folder, readonly=False)
        _require_uid(conn, uid, folder)
        _delete_uid(conn, uid)
        return folder


def save_sent(cfg: ImapConfig, raw: bytes) -> str:
    """APPEND a copy of a sent message to the Sent folder. Returns the folder name."""
    with _connect(cfg) as conn:
        folder = _special_folder(conn, "sent", cfg.sent_folder)
        _append(conn, folder, raw, r"(\Seen)", "imap.sent_folder")
        return folder


def unified_search(
    mailboxes: list[MailboxConfig],
    folder: str | None = None,
    limit: int = 50,
    **criteria: Any,
) -> dict[str, Any]:
    """Fan-out search across multiple IMAP mailboxes.

    Each result message is tagged with `mailbox` (config name) and
    `mailbox_address` (IMAP login / email) so the caller can tell where
    it came from. Per-mailbox errors are collected in `errors` rather
    than aborting the whole call.
    """
    targets = [mb for mb in mailboxes if mb.imap is not None]
    if not targets:
        return {"messages": [], "errors": []}

    def _one(mb: MailboxConfig) -> tuple[str, list[dict[str, Any]], str | None]:
        assert mb.imap is not None
        try:
            msgs = search_messages(mb.imap, folder=folder, limit=limit, **criteria)
        except (ImapError, OSError) as e:
            return mb.name, [], str(e)
        for m in msgs:
            m["mailbox"] = mb.name
            m["mailbox_address"] = mb.imap.username
        return mb.name, msgs, None

    merged: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as ex:
        for name, msgs, err in ex.map(_one, targets):
            if err is not None:
                errors.append({"mailbox": name, "error": err})
                continue
            merged.extend(msgs)

    def _key(m: dict[str, Any]) -> float:
        try:
            return parsedate_to_datetime(m.get("date") or "").timestamp()
        except (TypeError, ValueError):
            return 0.0

    merged.sort(key=_key, reverse=True)
    return {"messages": merged[: max(0, limit)], "errors": errors}


def mark_seen(cfg: ImapConfig, uid: str, folder: str | None = None, seen: bool = True) -> None:
    folder = folder or cfg.default_folder
    _validate_uid(uid)
    _reject_control_chars(folder, "folder")
    flag_op = "+FLAGS" if seen else "-FLAGS"
    with _connect(cfg) as conn:
        _select(conn, folder, readonly=False)
        typ, _ = conn.uid("STORE", uid, flag_op, r"(\Seen)")
        if typ != "OK":
            raise ImapError(f"STORE \\Seen failed for UID {uid}")
