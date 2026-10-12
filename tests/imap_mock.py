"""A tiny in-process IMAP4 server for tests.

Hand-rolled on `socketserver` (same approach as the SMTP capture sink in
`test_smtp_integration.py`) rather than a container, so the suite has no
external moving parts and starts in milliseconds. It speaks just enough of
IMAP4rev1 for `mailboxd.imap_client` to drive it: CAPABILITY, LOGIN, LIST,
SELECT/EXAMINE, APPEND, UID SEARCH/FETCH/STORE/EXPUNGE and EXPUNGE, including
the `{N}` literal framing imaplib needs to parse FETCH bodies and send APPEND
ones.

Every command line the client sends is recorded verbatim on `Sink.commands`,
which is what lets a test assert that a crafted folder/uid/search value did
NOT smuggle an extra IMAP command onto the connection.
"""

from __future__ import annotations

import email
import re
import socket
import socketserver
import threading
from typing import Any, Iterator


class Sink:
    """Shared state between the test and the mock server."""

    def __init__(self, messages: dict[int, bytes]) -> None:
        # uid -> raw RFC822 bytes the server will hand back on FETCH.
        self.messages = messages
        # Every command line received, tag included, CRLF stripped.
        self.commands: list[str] = []
        # Folders with a store of their own. Any other name SELECTs `messages`,
        # unless it is listed in `missing`, in which case it doesn't exist.
        self.folders: dict[str, dict[int, bytes]] = {}
        self.missing: set[str] = set()
        # What LIST answers, one `(attrs) "delim" name` string per folder.
        # `extended_list_lines` is the answer to `LIST ... RETURN (SPECIAL-USE)`.
        self.list_lines: list[str] = ['(\\HasNoChildren) "/" "INBOX"']
        self.extended_list_lines: list[str] | None = None
        self.capabilities = "IMAP4rev1"
        self.reject_login = False
        # First line the server sends; `* BYE ...` refuses the connection.
        self.greeting = "* OK [CAPABILITY IMAP4rev1] mock ready"
        # Every APPEND accepted: (folder, flags, raw message).
        self.appended: list[tuple[str, str, bytes]] = []
        self._next_uid = 100
        self._deleted: set[tuple[int, int]] = set()

    def verbs(self) -> list[str]:
        """Command verbs in the order received (`UID STORE`, `APPEND`, ...)."""
        out = []
        for line in self.commands_after_login():
            parts = line.split(" ")
            verb = parts[1].upper() if len(parts) > 1 else ""
            if verb == "UID" and len(parts) > 2:
                verb = f"UID {parts[2].upper()}"
            out.append(verb)
        return out

    def commands_after_login(self) -> list[str]:
        """Command lines with the connection-setup noise removed."""
        skip = ("CAPABILITY", "LOGIN", "LOGOUT")
        out = []
        for line in self.commands:
            parts = line.split(" ", 2)
            verb = parts[1].upper() if len(parts) > 1 else ""
            if verb in skip:
                continue
            out.append(line)
        return out


_QUOTED = r'"((?:[^"\\]|\\.)*)"'
_APPEND_RE = re.compile(rf"^{_QUOTED}\s+(?:\(([^)]*)\)\s+)?(?:\"[^\"]*\"\s+)?\{{(\d+)\}}$")


def _unquote(arg: str) -> str:
    m = re.match(_QUOTED, arg)
    return re.sub(r"\\(.)", r"\1", m.group(1)) if m else arg


def _make_handler(sink: Sink) -> type[socketserver.StreamRequestHandler]:
    class Handler(socketserver.StreamRequestHandler):
        store: dict[int, bytes] = sink.messages

        def _send(self, line: str) -> None:
            self.wfile.write((line + "\r\n").encode())

        def _ok(self, tag: str, text: str) -> None:
            self._send(f"{tag} OK {text}")

        def handle(self) -> None:
            self._send(sink.greeting)
            while True:
                raw = self.rfile.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                sink.commands.append(line)
                parts = line.split(" ", 2)
                if len(parts) < 2:
                    continue
                tag, cmd = parts[0], parts[1].upper()
                rest = parts[2] if len(parts) > 2 else ""
                if self._dispatch(tag, cmd, rest):
                    return

        def _dispatch(self, tag: str, cmd: str, rest: str) -> bool:
            """Handle one command. Returns True when the connection should close."""
            if cmd == "CAPABILITY":
                self._send(f"* CAPABILITY {sink.capabilities}")
                self._ok(tag, "CAPABILITY completed")
            elif cmd == "LOGIN":
                if sink.reject_login:
                    # Echo the command back, as some servers do — credentials and all.
                    self._send(f"{tag} NO [AUTHENTICATIONFAILED] rejected: LOGIN {rest}")
                else:
                    self._ok(tag, "LOGIN completed")
            elif cmd == "LIST":
                lines = sink.list_lines
                if "RETURN (SPECIAL-USE)" in rest and sink.extended_list_lines is not None:
                    lines = sink.extended_list_lines
                for line in lines:
                    self._send(f"* LIST {line}")
                self._ok(tag, "LIST completed")
            elif cmd in ("EXAMINE", "SELECT"):
                name = _unquote(rest)
                if name in sink.missing:
                    self._send(f"{tag} NO [NONEXISTENT] no such mailbox")
                    return False
                self.store = sink.folders.get(name, sink.messages)
                mode = "READ-ONLY" if cmd == "EXAMINE" else "READ-WRITE"
                self._send(f"* {len(self.store)} EXISTS")
                self._ok(tag, f"[{mode}] {cmd} completed")
            elif cmd == "APPEND":
                self._handle_append(tag, rest)
            elif cmd == "UID":
                self._handle_uid(tag, rest)
            elif cmd == "EXPUNGE":
                self._expunge(None)
                self._send("* 1 EXPUNGE")
                self._ok(tag, "EXPUNGE completed")
            elif cmd == "LOGOUT":
                self._send("* BYE mock logging out")
                self._ok(tag, "LOGOUT completed")
                return True
            else:
                self._ok(tag, f"{cmd} completed")
            return False

        def _handle_append(self, tag: str, rest: str) -> None:
            m = _APPEND_RE.match(rest)
            if m is None:
                self._send(f"{tag} BAD cannot parse APPEND")
                return
            name = re.sub(r"\\(.)", r"\1", m.group(1))
            if name in sink.missing:
                self._send(f"{tag} NO [TRYCREATE] no such mailbox")
                return
            self._send("+ Ready for literal data")
            raw = self.rfile.read(int(m.group(3)))
            self.rfile.readline()
            uid = sink._next_uid
            sink._next_uid += 1
            sink.folders.setdefault(name, {})[uid] = raw
            sink.appended.append((name, m.group(2) or "", raw))
            code = f"[APPENDUID 1 {uid}] " if "UIDPLUS" in sink.capabilities else ""
            self._ok(tag, f"{code}APPEND completed")

        def _expunge(self, only: int | None) -> None:
            for key in [k for k in sink._deleted if k[0] == id(self.store)]:
                if only is None or key[1] == only:
                    self.store.pop(key[1], None)
                    sink._deleted.discard(key)

        def _handle_uid(self, tag: str, rest: str) -> None:
            sub, _, args = rest.partition(" ")
            sub = sub.upper()
            if sub == "SEARCH":
                found = sorted(self.store)
                by_id = re.search(r'HEADER Message-ID "([^"]*)"', args, re.IGNORECASE)
                if by_id:
                    found = [
                        u
                        for u in found
                        if email.message_from_bytes(self.store[u])["Message-ID"] == by_id.group(1)
                    ]
                uids = " ".join(str(u) for u in found)
                self._send(f"* SEARCH {uids}".rstrip())
                self._ok(tag, "UID SEARCH completed")
            elif sub == "FETCH":
                self._handle_fetch(tag, args)
            elif sub == "STORE":
                uid = args.split(" ", 1)[0]
                if "+FLAGS" in args and "\\Deleted" in args and uid.isdigit():
                    sink._deleted.add((id(self.store), int(uid)))
                self._send(f"* 1 FETCH (UID {uid} FLAGS (\\Deleted))")
                self._ok(tag, "UID STORE completed")
            elif sub == "EXPUNGE":
                self._expunge(int(args) if args.isdigit() else None)
                self._ok(tag, "UID EXPUNGE completed")
            else:
                self._ok(tag, f"UID {sub} completed")

        def _handle_fetch(self, tag: str, args: str) -> None:
            uid_set, _, items = args.partition(" ")
            want_full = "RFC822" in items and "HEADER" not in items
            for uid in _parse_uid_set(uid_set):
                raw = self.store.get(uid)
                if raw is None:
                    continue
                if want_full:
                    self._fetch_full(uid, raw)
                else:
                    self._fetch_headers(uid, raw)
            self._ok(tag, "UID FETCH completed")

        def _fetch_full(self, uid: int, raw: bytes) -> None:
            head = b"* %d FETCH (UID %d RFC822 {%d}\r\n" % (uid, uid, len(raw))
            self.wfile.write(head + raw + b")\r\n")

        def _fetch_headers(self, uid: int, raw: bytes) -> None:
            msg = email.message_from_bytes(raw)
            fields = ["From", "To", "Subject", "Date", "Message-ID"]
            lines = [f"{f}: {msg[f]}" for f in fields if msg[f] is not None]
            hdr = ("\r\n".join(lines) + "\r\n\r\n").encode()
            head = (
                b"* %d FETCH (UID %d RFC822.SIZE %d FLAGS (\\Seen) "
                b"BODY[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)] {%d}\r\n"
                % (uid, uid, len(raw), len(hdr))
            )
            self.wfile.write(head + hdr + b")\r\n")

    return Handler


def _parse_uid_set(uid_set: str) -> list[int]:
    out: list[int] = []
    for tok in uid_set.split(","):
        tok = tok.strip()
        if tok.isdigit():
            out.append(int(tok))
    return out


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Injection tests deliberately feed malformed sequences; a handler
        # thread blowing up on them is expected, so don't spew tracebacks.
        pass


def start(messages: dict[int, bytes], tls: Any = None) -> tuple[Sink, _Server, str, int]:
    """Start a mock IMAP server on an ephemeral port and wait for it to listen.

    Pass an `ssl.SSLContext` as `tls` to serve implicit TLS (IMAPS).
    """
    sink = Sink(messages)
    srv = _Server(("127.0.0.1", 0), _make_handler(sink))
    if tls is not None:
        srv.socket = tls.wrap_socket(srv.socket, server_side=True)
    host, port = srv.server_address[:2]
    # A short poll interval keeps `shutdown()` in test teardown from taking 0.5s.
    threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True).start()
    for _ in range(50):
        try:
            with socket.create_connection((host, port), timeout=0.5):
                break
        except OSError:
            continue
    return sink, srv, str(host), int(port)


def serve(messages: dict[int, bytes]) -> Iterator[tuple[Sink, str, int]]:
    """Context-manager style helper: yields (sink, host, port), cleans up after."""
    sink, srv, host, port = start(messages)
    try:
        yield sink, host, port
    finally:
        srv.shutdown()
        srv.server_close()


def sample_message(subject: str, sender: str = "alice@example.com") -> bytes:
    """Build a minimal multipart-free RFC822 message for FETCH responses."""
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["To"] = "bob@example.com"
    msg["Subject"] = subject
    msg["Date"] = "Wed, 18 May 2026 10:00:00 +0000"
    msg["Message-ID"] = f"<{subject.replace(' ', '-')}@example.com>"
    msg.set_content("hello from the mock imap server")
    return msg.as_bytes()
