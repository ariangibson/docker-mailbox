import os
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import imap_mock  # noqa: E402
import smtp_mock  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_mailbox_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own MAILBOX_* variables out of the tests.

    Most tests load a token-less config file, so running without auth is
    allowed by default; tests of the auth requirement delete the variable.
    """
    for var in list(os.environ):
        if var.startswith("MAILBOX_") or var == "MAILBOXD_CONFIG":
            monkeypatch.delenv(var)
    monkeypatch.setenv("MAILBOX_ALLOW_NO_AUTH", "true")


@pytest.fixture()
def imap_factory() -> Any:
    servers = []

    def _make(messages: dict[int, bytes]) -> tuple[imap_mock.Sink, str, int]:
        sink, srv, host, port = imap_mock.start(messages)
        servers.append(srv)
        return sink, host, port

    yield _make
    for srv in servers:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def smtp_sink() -> Any:
    sink, srv, host, port = smtp_mock.start()
    try:
        yield sink, host, port
    finally:
        srv.shutdown()
        srv.server_close()
