"""Configuration from MAILBOX_* environment variables.

`load_config` is given an explicit `environ` mapping, so these tests say
exactly which variables exist without touching the process environment.
"""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mailboxd.config import Config, ConfigError, load_config, redact
from mailboxd.env_config import EnvError, parse_account
from mailboxd.server import create_app

TOKEN = "s3cret-bearer-token"
PASSWORD = "p:a,s@s w0rd!#$%^&*(){}'\"\\"
BASE = {"MAILBOX_AUTH_TOKENS": TOKEN}


def _load(tmp_path: Path, env: dict[str, str], yaml_text: str | None = None) -> Config:
    """Load with `env` as the entire environment and an optional config file."""
    path = tmp_path / "config.yaml"
    if yaml_text is not None:
        path.write_text(yaml_text)
    return load_config(str(path), environ=env)


def _error(tmp_path: Path, env: dict[str, str], yaml_text: str | None = None) -> str:
    with pytest.raises(ConfigError) as info:
        _load(tmp_path, env, yaml_text)
    # What a crash at startup would print: the message and any chained cause.
    return "".join(traceback.format_exception(info.value))


# --------------------------------------------------------------------------
# Account value parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "password",
    [
        "simple",
        "with:colon",
        ":leading:and:trailing:",
        "comma,separated,looking",
        "has@at@signs",
        "user@example.com:looks-like-another-account",
        "spaces in the middle",
        " leading and trailing spaces ",
        "quotes'\"and\\backslashes",
        "${NOT_EXPANDED} $HOME #hash %percent !bang",
        "ünïcødé-pässwörd-🔑",
        PASSWORD,
    ],
)
def test_password_is_everything_after_the_username(password: str) -> None:
    value = f"purelymail:inbox@mydomain.example:{password}"
    assert parse_account("MAILBOX_X", value) == (
        "purelymail",
        "inbox@mydomain.example",
        password,
    )


def test_provider_and_username_are_normalised() -> None:
    assert parse_account("MAILBOX_X", " Gmail :me.name+tag@Gmail.com:pw") == (
        "gmail",
        "me.name+tag@Gmail.com",
        "pw",
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "just-one-field",
        "gmail:me@gmail.com",  # no password
        "gmail:me@gmail.com:",  # empty password
        "gmail:me:hunter2",  # username is not an email address
        "gmail:me:hunt@er2:zzz",  # ditto: the only @ is inside the password
        "gmail::hunter2@x:zzz",
        "hunter2",
    ],
)
def test_malformed_account_names_the_variable_and_never_the_value(value: str) -> None:
    with pytest.raises(EnvError) as info:
        parse_account("MAILBOX_WORK", value)
    message = str(info.value)
    assert message.startswith("MAILBOX_WORK: ")
    for fragment in ("hunter2", "hunt@er2", "zzz", "gmail"):
        assert fragment not in message


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def test_env_only_configuration_needs_no_file(tmp_path: Path) -> None:
    cfg = _load(
        tmp_path,
        {
            **BASE,
            "MAILBOX_SHOP": f"purelymail:inbox@mydomain.example:{PASSWORD}",
            "MAILBOX_PERSONAL": "gmail:me@gmail.com:app-password",
            "UNRELATED": "x",
            "MAILBOXD_CONFIG": str(tmp_path / "config.yaml"),
        },
    )
    assert [m.name for m in cfg.mailboxes] == ["personal", "shop"]
    assert cfg.auth.tokens == [TOKEN]

    mb = cfg.get("shop")
    assert mb is not None and mb.imap is not None and mb.smtp is not None
    assert mb.provider == "purelymail"
    assert (mb.imap.host, mb.imap.port, mb.imap.tls) == ("imap.purelymail.com", 993, "ssl")
    assert (mb.smtp.host, mb.smtp.port, mb.smtp.tls) == ("smtp.purelymail.com", 465, "ssl")
    assert mb.imap.username == mb.smtp.username == "inbox@mydomain.example"
    assert mb.imap.password == mb.smtp.password == PASSWORD
    # Per-account defaults.
    assert mb.smtp.from_address == "inbox@mydomain.example"
    assert mb.allowed_from == ["*@mydomain.example"]
    assert mb.imap.default_folder == "INBOX"
    assert mb.imap.drafts_folder is None and mb.imap.sent_folder is None


@pytest.mark.parametrize(
    ("provider", "imap", "smtp"),
    [
        ("purelymail", ("imap.purelymail.com", 993, "ssl"), ("smtp.purelymail.com", 465, "ssl")),
        ("gmail", ("imap.gmail.com", 993, "ssl"), ("smtp.gmail.com", 465, "ssl")),
        ("fastmail", ("imap.fastmail.com", 993, "ssl"), ("smtp.fastmail.com", 465, "ssl")),
        (
            "outlook",
            ("outlook.office365.com", 993, "ssl"),
            ("smtp.office365.com", 587, "starttls"),
        ),
    ],
)
def test_builtin_presets(
    tmp_path: Path, provider: str, imap: tuple[Any, ...], smtp: tuple[Any, ...]
) -> None:
    mb = _load(tmp_path, {**BASE, "MAILBOX_A": f"{provider}:a@example.com:pw"}).get("a")
    assert mb is not None and mb.imap is not None and mb.smtp is not None
    assert (mb.imap.host, mb.imap.port, mb.imap.tls) == imap
    assert (mb.smtp.host, mb.smtp.port, mb.smtp.tls) == smtp


def test_reserved_and_override_variables_are_not_accounts(tmp_path: Path) -> None:
    cfg = _load(
        tmp_path,
        {
            "MAILBOX_AUTH_TOKENS": f"{TOKEN}, second-token ,",
            "MAILBOX_LOG_LEVEL": "debug",
            "MAILBOX_PORT": "9000",
            "MAILBOX_ALLOW_NO_AUTH": "false",
            # Client-side names from this project's own skill / plugin.
            "MAILBOX_URL": "http://localhost:8000",
            "MAILBOX_TOKEN": TOKEN,
            "MAILBOX_PROVIDER_ACME_IMAP_HOST": "imap.acme.test",
            "MAILBOX_SALES": "purelymail:sales@example.com:pw",
            "MAILBOX_SALES_FROM_ADDRESS": "Sales Team <sales@example.com>",
            "MAILBOX_SALES_ALLOWED_FROM": "sales@example.com, *@shop.example.com ,",
            "MAILBOX_SALES_IMAP_HOST": "imap.internal.test",
            "MAILBOX_SALES_IMAP_PORT": "143",
            "MAILBOX_SALES_IMAP_TLS": "STARTTLS",
            "MAILBOX_SALES_SMTP_HOST": "smtp.internal.test",
            "MAILBOX_SALES_SMTP_PORT": "25",
            "MAILBOX_SALES_SMTP_TLS": "none",
            "MAILBOX_SALES_DRAFTS_FOLDER": "INBOX/Drafts",
            "MAILBOX_SALES_SENT_FOLDER": "Sent Items",
            "MAILBOX_SALES_DEFAULT_FOLDER": "Support",
        },
    )
    assert [m.name for m in cfg.mailboxes] == ["sales"]
    assert cfg.auth.tokens == [TOKEN, "second-token"]
    assert cfg.log_level == "DEBUG"

    mb = cfg.mailboxes[0]
    assert mb.imap is not None and mb.smtp is not None
    assert (mb.imap.host, mb.imap.port, mb.imap.tls) == ("imap.internal.test", 143, "starttls")
    assert (mb.smtp.host, mb.smtp.port, mb.smtp.tls) == ("smtp.internal.test", 25, "none")
    assert mb.smtp.from_address == "Sales Team <sales@example.com>"
    assert mb.allowed_from == ["sales@example.com", "*@shop.example.com"]
    assert mb.imap.drafts_folder == "INBOX/Drafts"
    assert mb.imap.sent_folder == "Sent Items"
    assert mb.imap.default_folder == "Support"


def test_sent_copy_is_off_for_providers_that_keep_their_own(tmp_path: Path) -> None:
    env = {
        **BASE,
        "MAILBOX_G": "gmail:a@gmail.com:pw",
        "MAILBOX_O": "outlook:a@example.com:pw",
        "MAILBOX_P": "purelymail:a@example.com:pw",
        "MAILBOX_F": "fastmail:a@example.com:pw",
        "MAILBOX_F_SAVE_SENT": "false",
        "MAILBOX_O_SAVE_SENT": "true",
    }
    saved = {m.name: m.imap.save_sent for m in _load(tmp_path, env).mailboxes if m.imap}
    assert saved == {"g": False, "o": True, "p": True, "f": False}
    assert "MAILBOX_F_SAVE_SENT" in _error(tmp_path, {**env, "MAILBOX_F_SAVE_SENT": "sometimes"})


def test_tls_verification_is_on_unless_switched_off(tmp_path: Path) -> None:
    env = {
        **BASE,
        "MAILBOX_PROVIDER_LAN_IMAP_HOST": "mail.lan.test",
        "MAILBOX_PROVIDER_LAN_IMAP_TLS_VERIFY": "false",
        "MAILBOX_PROVIDER_LAN_SMTP_HOST": "mail.lan.test",
        "MAILBOX_HOME": "lan:me@lan.test:pw",
        "MAILBOX_HOME_SMTP_TLS_VERIFY": "no",
        "MAILBOX_P": "purelymail:a@example.com:pw",
    }
    cfg = _load(tmp_path, env)
    home, p = cfg.get("home"), cfg.get("p")
    assert home and home.imap and home.smtp and p and p.imap and p.smtp
    assert (home.imap.tls_verify, home.smtp.tls_verify) == (False, False)
    assert (p.imap.tls_verify, p.smtp.tls_verify) == (True, True)
    # `_IMAP_TLS_VERIFY` must not be read as `_IMAP_TLS`, nor as an account.
    assert home.imap.tls == "ssl"
    assert "TLS_VERIFY" in _error(tmp_path, {**env, "MAILBOX_HOME_IMAP_TLS_VERIFY": "maybe"})


def test_account_names_are_lowercased_and_may_contain_underscores(tmp_path: Path) -> None:
    cfg = _load(tmp_path, {**BASE, "MAILBOX_Side_Project_2": "fastmail:a@example.com:pw"})
    assert [m.name for m in cfg.mailboxes] == ["side_project_2"]


def test_override_for_an_undefined_account_fails(tmp_path: Path) -> None:
    message = _error(tmp_path, {**BASE, "MAILBOX_GHOST_IMAP_HOST": "imap.example.com"})
    assert "MAILBOX_GHOST_IMAP_HOST" in message
    assert "MAILBOX_GHOST=<provider>:<username>:<password>" in message


def test_unknown_provider_fails_naming_the_variable(tmp_path: Path) -> None:
    message = _error(tmp_path, {**BASE, "MAILBOX_WORK": f"protonmail:me@proton.me:{PASSWORD}"})
    assert "MAILBOX_WORK: unknown provider" in message
    # The known presets are listed so the fix is obvious...
    assert "fastmail, gmail, outlook, purelymail" in message
    # ...but the value is never quoted: a malformed one could put the password first.
    assert PASSWORD not in message and "protonmail" not in message


@pytest.mark.parametrize(
    ("env", "names"),
    [
        ({"MAILBOX_A": "gmail:a@example.com"}, "MAILBOX_A"),
        ({"MAILBOX_A": ""}, "MAILBOX_A"),
        ({"MAILBOX_A.B": "gmail:a@example.com:pw"}, "MAILBOX_A.B"),
        ({"MAILBOX_A": "gmail:a@example.com:pw", "MAILBOX_A_IMAP_PORT": "imap"}, "IMAP_PORT"),
        ({"MAILBOX_A": "gmail:a@example.com:pw", "MAILBOX_A_SMTP_PORT": "70000"}, "SMTP_PORT"),
        (
            {"MAILBOX_A": "gmail:a@example.com:pw", "MAILBOX_A_SMTP_TLS": "tls"},
            "MAILBOX_A_SMTP_TLS",
        ),
        ({"MAILBOX_A": "gmail:a@example.com:pw", "MAILBOX_a": "gmail:b@example.com:pw"}, "same"),
        ({"MAILBOX_A": "gmail:a@example.com:pw", "MAILBOX_A_ALLOWED_FROM": " , "}, "ALLOWED_FROM"),
        ({"MAILBOX_PROVIDER_ACME": "imap.acme.test"}, "MAILBOX_PROVIDER_ACME"),
        ({"MAILBOX_PROVIDER_ACME_IMAP_PORT": "993"}, "MAILBOX_PROVIDER_ACME_IMAP_HOST"),
        ({"MAILBOX_PROVIDER_ACME_SMTP_TLS": "yes"}, "MAILBOX_PROVIDER_ACME_SMTP_TLS"),
        ({"MAILBOX_A": "gmail:a@example.com:pw", "MAILBOX_PORT": "http"}, "MAILBOX_PORT"),
        ({"MAILBOX_A": "gmail:a@example.com:pw", "MAILBOX_LOG_LEVEL": "loud"}, "MAILBOX_LOG_LEVEL"),
        (
            {"MAILBOX_A": "gmail:a@example.com:pw", "MAILBOX_ALLOW_NO_AUTH": "maybe"},
            "MAILBOX_ALLOW_NO_AUTH",
        ),
    ],
)
def test_bad_variables_fail_fast_naming_the_variable(
    tmp_path: Path, env: dict[str, str], names: str
) -> None:
    assert names in _error(tmp_path, {**BASE, **env})


def test_nothing_configured_at_all_is_an_error(tmp_path: Path) -> None:
    message = _error(tmp_path, BASE)
    assert "config not found" in message and "MAILBOX_<NAME>=" in message


# --------------------------------------------------------------------------
# Provider presets
# --------------------------------------------------------------------------


def test_custom_provider_preset(tmp_path: Path) -> None:
    cfg = _load(
        tmp_path,
        {
            **BASE,
            "MAILBOX_PROVIDER_ACME_MAIL_IMAP_HOST": "imap.acme.test",
            "MAILBOX_PROVIDER_ACME_MAIL_IMAP_PORT": "143",
            "MAILBOX_PROVIDER_ACME_MAIL_IMAP_TLS": "starttls",
            "MAILBOX_PROVIDER_ACME_MAIL_SMTP_HOST": "smtp.acme.test",
            "MAILBOX_OPS": "acme_mail:ops@acme.test:pw",
        },
    )
    mb = cfg.get("ops")
    assert mb is not None and mb.imap is not None and mb.smtp is not None
    assert mb.provider == "acme_mail"
    assert (mb.imap.host, mb.imap.port, mb.imap.tls) == ("imap.acme.test", 143, "starttls")
    # Unset port / TLS take the normal defaults.
    assert (mb.smtp.host, mb.smtp.port, mb.smtp.tls) == ("smtp.acme.test", 587, "starttls")


def test_custom_provider_may_be_imap_only(tmp_path: Path) -> None:
    cfg = _load(
        tmp_path,
        {
            **BASE,
            "MAILBOX_PROVIDER_ARCHIVE_IMAP_HOST": "imap.archive.test",
            "MAILBOX_OLD": "archive:old@archive.test:pw",
        },
    )
    mb = cfg.get("old")
    assert mb is not None and mb.imap is not None and mb.smtp is None
    # Nothing to send from, but the allow-list default still applies to drafts.
    assert mb.allowed_from == ["*@archive.test"]
    assert "MAILBOX_OLD_FROM_ADDRESS" in _error(
        tmp_path,
        {
            **BASE,
            "MAILBOX_PROVIDER_ARCHIVE_IMAP_HOST": "imap.archive.test",
            "MAILBOX_OLD": "archive:old@archive.test:pw",
            "MAILBOX_OLD_FROM_ADDRESS": "old@archive.test",
        },
    )


def test_builtin_provider_can_be_adjusted(tmp_path: Path) -> None:
    cfg = _load(
        tmp_path,
        {
            **BASE,
            "MAILBOX_PROVIDER_GMAIL_SMTP_PORT": "587",
            "MAILBOX_PROVIDER_GMAIL_SMTP_TLS": "starttls",
            "MAILBOX_ME": "gmail:me@gmail.com:pw",
        },
    )
    mb = cfg.get("me")
    assert mb is not None and mb.smtp is not None
    assert (mb.smtp.host, mb.smtp.port, mb.smtp.tls) == ("smtp.gmail.com", 587, "starttls")


# --------------------------------------------------------------------------
# Environment over file
# --------------------------------------------------------------------------

_FILE = """
log_level: WARNING
auth:
  tokens: [file-token]
mailboxes:
  - name: Work
    description: "from the file"
    allowed_from: ["me@work.example"]
    imap: {host: imap.work.example, username: me@work.example, password: file-password}
    smtp: {host: smtp.work.example, username: me@work.example, password: file-password,
      from_address: "Me <me@work.example>"}
  - name: untouched
    imap: {host: imap.other.example, username: u, password: file-password}
"""


def test_file_still_loads_on_its_own(tmp_path: Path) -> None:
    cfg = _load(tmp_path, {}, _FILE)
    assert [m.name for m in cfg.mailboxes] == ["Work", "untouched"]
    assert cfg.auth.tokens == ["file-token"]
    assert cfg.log_level == "WARNING"


def test_env_values_override_file_values(tmp_path: Path) -> None:
    cfg = _load(
        tmp_path,
        {
            "MAILBOX_AUTH_TOKENS": TOKEN,
            "MAILBOX_LOG_LEVEL": "ERROR",
            "MAILBOX_WORK_IMAP_HOST": "imap.new.example",
            "MAILBOX_WORK_SENT_FOLDER": "Sent Items",
            "MAILBOX_EXTRA": "fastmail:x@fastmail.com:pw",
        },
        _FILE,
    )
    assert [m.name for m in cfg.mailboxes] == ["Work", "untouched", "extra"]
    assert cfg.auth.tokens == [TOKEN]
    assert cfg.log_level == "ERROR"
    work = cfg.get("Work")
    assert work is not None and work.imap is not None
    assert work.imap.host == "imap.new.example"
    assert work.imap.sent_folder == "Sent Items"
    # Everything the environment didn't mention is still the file's.
    assert work.imap.password == "file-password"
    assert work.description == "from the file"


def test_env_account_replaces_the_file_accounts_connection(tmp_path: Path) -> None:
    cfg = _load(tmp_path, {"MAILBOX_WORK": "gmail:new@gmail.com:env-password"}, _FILE)
    work = cfg.get("Work")
    assert work is not None and work.imap is not None and work.smtp is not None
    assert [m.name for m in cfg.mailboxes] == ["Work", "untouched"]
    assert (work.imap.host, work.imap.username, work.imap.password) == (
        "imap.gmail.com",
        "new@gmail.com",
        "env-password",
    )
    assert work.smtp.host == "smtp.gmail.com"
    # from_address / allowed_from defaults are only defaults: the file set both.
    assert work.smtp.from_address == "Me <me@work.example>"
    assert work.allowed_from == ["me@work.example"]


# --------------------------------------------------------------------------
# Auth is required
# --------------------------------------------------------------------------


def test_tokens_are_required_unless_explicitly_waived(tmp_path: Path) -> None:
    account = {"MAILBOX_A": "gmail:a@example.com:pw"}
    message = _error(tmp_path, account)
    assert "MAILBOX_AUTH_TOKENS" in message and "MAILBOX_ALLOW_NO_AUTH" in message
    assert "MAILBOX_AUTH_TOKENS" in _error(tmp_path, {**account, "MAILBOX_AUTH_TOKENS": " , "})
    assert "MAILBOX_AUTH_TOKENS" in _error(tmp_path, {**account, "MAILBOX_ALLOW_NO_AUTH": "0"})

    assert _load(tmp_path, {**account, "MAILBOX_ALLOW_NO_AUTH": "true"}).auth.tokens == []
    # Tokens in the file satisfy the requirement too.
    assert _load(tmp_path, account, "auth: {tokens: [file-token]}").auth.tokens == ["file-token"]


def test_a_token_less_file_no_longer_starts_open_by_accident(tmp_path: Path) -> None:
    yaml_text = "mailboxes:\n  - name: a\n    imap: {host: h, username: u, password: p}\n"
    assert "MAILBOX_ALLOW_NO_AUTH" in _error(tmp_path, {}, yaml_text)
    assert _load(tmp_path, {"MAILBOX_ALLOW_NO_AUTH": "yes"}, yaml_text).auth.tokens == []


def test_allow_no_auth_does_not_switch_off_configured_tokens(tmp_path: Path) -> None:
    cfg = _load(
        tmp_path,
        {**BASE, "MAILBOX_ALLOW_NO_AUTH": "true", "MAILBOX_A": "gmail:a@example.com:pw"},
    )
    client = TestClient(create_app(cfg))
    assert client.get("/mailboxes").status_code == 401
    assert client.get("/mailboxes", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


# --------------------------------------------------------------------------
# Secrets stay out of error messages
# --------------------------------------------------------------------------


def test_validation_errors_do_not_echo_passwords(tmp_path: Path) -> None:
    # `tls: bogus` fails validation; pydantic's own message would print the
    # whole imap mapping, password included.
    yaml_text = f"""
auth: {{tokens: [{TOKEN}]}}
mailboxes:
  - name: leaky
    imap: {{host: h, username: u, password: "{TOKEN}-imap-password", tls: bogus}}
"""
    message = _error(tmp_path, {}, yaml_text)
    assert "mailbox 'leaky'" in message and "tls" in message
    assert TOKEN not in message


@pytest.mark.parametrize(
    "line",
    [
        "imap: {password: hunter2-secret: oops",  # PyYAML quotes the line
        "imap: {host: h, username: u, password: *hunter2-secret}",  # ...names the alias
        "imap: {host: h, username: u, password: !hunter2-secret x}",  # ...names the tag
    ],
)
def test_yaml_errors_do_not_echo_the_offending_line(tmp_path: Path, line: str) -> None:
    message = _error(tmp_path, {}, f"mailboxes:\n  - name: a\n    {line}\n")
    assert "not valid YAML" in message and "line 3" in message
    assert "hunter2" not in message


def test_unset_reference_inside_a_longer_value_is_not_echoed(tmp_path: Path) -> None:
    # An old config whose password merely contains `${...}` must not have that
    # piece of the password printed as a "variable name".
    yaml_text = """
mailboxes:
  - name: a
    imap: {host: h, username: u, password: "pa${hunter2}word"}
"""
    message = _error(tmp_path, {}, yaml_text)
    assert "mailboxes[0].imap.password" in message
    assert "hunter2" not in message
    # A value that is nothing but the reference does get named.
    assert "${WORK_PASSWORD}" in _error(
        tmp_path, {}, yaml_text.replace("pa${hunter2}word", "${WORK_PASSWORD}")
    )


def test_env_errors_do_not_echo_passwords(tmp_path: Path) -> None:
    for env in (
        {"MAILBOX_A": f"nope:a@example.com:{PASSWORD}"},
        {"MAILBOX_A": f"{PASSWORD}"},
        {"MAILBOX_A": f"gmail:a@example.com:{PASSWORD}", "MAILBOX_A_IMAP_PORT": "x"},
        {"MAILBOX_A": f"gmail:a@example.com:{PASSWORD}", "MAILBOX_AUTH_TOKENS": ""},
    ):
        message = _error(tmp_path, {**BASE, **env})
        assert PASSWORD not in message and TOKEN not in message


def test_redact() -> None:
    assert redact("LOGIN u hunter2-secret failed", "hunter2-secret") == "LOGIN u *** failed"
    # As the wire carries it: escaped in an IMAP quoted string, base64 in SMTP AUTH.
    assert redact('LOGIN u "pa\\"ss\\\\wd"', 'pa"ss\\wd') == 'LOGIN u "***"'
    assert redact("AUTH LOGIN aHVudGVyMi1zZWNyZXQ=", "hunter2-secret") == "AUTH LOGIN ***"
    assert redact("abc abcdef", "abc", "abcdef", None, "") == "abc ***"
    # Too short to blank out without wrecking the message around it.
    assert redact("login failed", "in") == "login failed"
