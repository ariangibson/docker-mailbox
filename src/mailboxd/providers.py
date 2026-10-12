"""Provider presets for env-defined mailboxes (`MAILBOX_<NAME>=<provider>:...`).

Each preset is the `imap` / `smtp` section a mailbox on that provider starts
from; the account's username and password are added on top. To support
another provider, add an entry here — or define one at deploy time with
`MAILBOX_PROVIDER_<NAME>_IMAP_HOST` and friends, no code change needed.
"""

from __future__ import annotations

from typing import Any

Preset = dict[str, dict[str, Any]]

PROVIDERS: dict[str, Preset] = {
    "purelymail": {
        "imap": {"host": "imap.purelymail.com", "port": 993, "tls": "ssl"},
        "smtp": {"host": "smtp.purelymail.com", "port": 465, "tls": "ssl"},
    },
    "gmail": {
        # Gmail files every SMTP submission in Sent Mail itself, so appending
        # our own copy would duplicate it.
        "imap": {"host": "imap.gmail.com", "port": 993, "tls": "ssl", "save_sent": False},
        "smtp": {"host": "smtp.gmail.com", "port": 465, "tls": "ssl"},
    },
    "fastmail": {
        "imap": {"host": "imap.fastmail.com", "port": 993, "tls": "ssl"},
        "smtp": {"host": "smtp.fastmail.com", "port": 465, "tls": "ssl"},
    },
    "outlook": {
        # Office 365 / Exchange Online. Like Gmail it saves sent mail itself.
        "imap": {"host": "outlook.office365.com", "port": 993, "tls": "ssl", "save_sent": False},
        "smtp": {"host": "smtp.office365.com", "port": 587, "tls": "starttls"},
    },
}
