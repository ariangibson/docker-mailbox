"""A tiny in-process SMTP capture sink for tests.

Hand-rolled on `socketserver` (no aiosmtpd) so the fixture has no external
moving parts. Records every delivered message and its envelope.
"""

from __future__ import annotations

import socket
import socketserver
import threading


class Sink:
    def __init__(self) -> None:
        self.messages: list[bytes] = []
        self.envelopes: list[tuple[str, list[str]]] = []
        # AUTH command lines received; set `reject_auth` to refuse them.
        self.auth: list[str] = []
        self.reject_auth = False


def _make_handler(sink: Sink) -> type[socketserver.StreamRequestHandler]:
    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            mail_from = ""
            rcpts: list[str] = []
            data_buf: list[bytes] = []

            def send(line: str) -> None:
                self.wfile.write((line + "\r\n").encode())

            send("220 test ESMTP")
            while True:
                raw = self.rfile.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                up = line.upper()
                if up.startswith(("HELO", "EHLO")):
                    send("250-test")
                    send("250 AUTH PLAIN")
                elif up.startswith("AUTH"):
                    sink.auth.append(line)
                    if sink.reject_auth:
                        send(f"535 5.7.8 rejected: {line}")
                    else:
                        send("235 2.7.0 Authentication successful")
                elif up.startswith("MAIL FROM"):
                    mail_from = _between(line, "<", ">")
                    send("250 OK")
                elif up.startswith("RCPT TO"):
                    rcpts.append(_between(line, "<", ">"))
                    send("250 OK")
                elif up == "DATA":
                    send("354 End data with <CR><LF>.<CR><LF>")
                    while True:
                        chunk = self.rfile.readline()
                        if not chunk:
                            return
                        if chunk in (b".\r\n", b".\n"):
                            break
                        if chunk.startswith(b".."):
                            chunk = chunk[1:]
                        data_buf.append(chunk)
                    sink.messages.append(b"".join(data_buf))
                    sink.envelopes.append((mail_from, list(rcpts)))
                    data_buf.clear()
                    rcpts = []
                    mail_from = ""
                    send("250 OK queued")
                elif up == "QUIT":
                    send("221 bye")
                    return
                elif up == "RSET":
                    rcpts = []
                    mail_from = ""
                    data_buf.clear()
                    send("250 OK")
                elif up == "NOOP":
                    send("250 OK")
                else:
                    send("250 OK")

    return Handler


def _between(s: str, a: str, b: str) -> str:
    i = s.find(a)
    j = s.find(b, i + 1)
    if i < 0 or j < 0:
        return ""
    return s[i + 1 : j]


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        # The readiness probe in `start()` hangs up without reading the
        # greeting, which can reset the connection; that is not a failure.
        pass


def start() -> tuple[Sink, _Server, str, int]:
    """Start a capture sink on an ephemeral port and wait for it to listen."""
    sink = Sink()
    srv = _Server(("127.0.0.1", 0), _make_handler(sink))
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
