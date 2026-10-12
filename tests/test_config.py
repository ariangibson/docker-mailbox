from pathlib import Path

import pytest

from mailboxd.config import ConfigError, load_config


def _write(tmp_path: Path, body: str) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return str(p)


def test_load_minimal_imap_only(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """
mailboxes:
  - name: a
    imap:
      host: imap.example.com
      username: u
      password: p
""",
        )
    )
    assert len(cfg.mailboxes) == 1
    m = cfg.get("a")
    assert m is not None
    assert m.imap is not None
    assert m.smtp is None
    assert m.imap.port == 993
    assert m.imap.tls == "ssl"
    assert m.imap.default_folder == "INBOX"


def test_load_both_protocols(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """
mailboxes:
  - name: full
    imap: {host: i, username: u, password: p}
    smtp: {host: s, username: u, password: p, from_address: u@example.com}
""",
        )
    )
    m = cfg.get("full")
    assert m is not None
    assert m.imap and m.smtp


def test_rejects_mailbox_with_no_protocols(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(
            _write(
                tmp_path,
                """
mailboxes:
  - name: nope
""",
            )
        )


def test_rejects_duplicate_names(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(
            _write(
                tmp_path,
                """
mailboxes:
  - name: dup
    imap: {host: x, username: u, password: p}
  - name: dup
    imap: {host: y, username: u, password: p}
""",
            )
        )


def test_rejects_invalid_name(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(
            _write(
                tmp_path,
                """
mailboxes:
  - name: "has spaces"
    imap: {host: x, username: u, password: p}
""",
            )
        )


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "nope.yaml"))


def test_invalid_yaml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "this is: not [valid yaml :::"))


# --------------------------------------------------------------------------
# ${VAR} expansion
# --------------------------------------------------------------------------

_ENV_YAML = """
auth:
  tokens: ["${API_TOKEN}"]
mailboxes:
  - name: a
    imap:
      host: "${IMAP_HOST:-imap.example.com}"
      port: "${IMAP_PORT:-993}"
      username: u
      password: "${IMAP_PASSWORD}"
"""


def test_env_vars_expand_in_string_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A value full of YAML syntax must arrive verbatim, not reshape the document.
    password = 'p@ss: "w0rd" #1 {x} [y], ${NOT_A_VAR}'
    monkeypatch.setenv("API_TOKEN", "token-from-env")
    monkeypatch.setenv("IMAP_PASSWORD", password)
    monkeypatch.setenv("IMAP_PORT", "143")
    cfg = load_config(_write(tmp_path, _ENV_YAML))
    m = cfg.get("a")
    assert m is not None and m.imap is not None
    assert cfg.auth.tokens == ["token-from-env"]
    assert m.imap.password == password
    assert m.imap.host == "imap.example.com"
    assert m.imap.port == 143


def test_unset_env_var_is_an_error_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_TOKEN", "t")
    monkeypatch.delenv("IMAP_PASSWORD", raising=False)
    with pytest.raises(ConfigError, match=r"mailboxes\[0\]\.imap\.password: \$\{IMAP_PASSWORD\}"):
        load_config(_write(tmp_path, _ENV_YAML))


def test_double_dollar_escapes_expansion(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """
mailboxes:
  - name: a
    imap: {host: h, username: u, password: "pa$${ss}wo$rd"}
""",
        )
    )
    m = cfg.get("a")
    assert m is not None and m.imap is not None
    assert m.imap.password == "pa${ss}wo$rd"


def test_new_mailbox_keys(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """
mailboxes:
  - name: a
    allowed_from: ["*@example.com"]
    imap: {host: h, username: u, password: p, drafts_folder: Entwürfe, sent_folder: Gesendet,
      save_sent: false}
  - name: b
    imap: {host: h, username: u, password: p}
""",
        )
    )
    a, b = cfg.mailboxes
    assert a.imap is not None and b.imap is not None
    assert a.allowed_from == ["*@example.com"]
    assert (a.imap.drafts_folder, a.imap.sent_folder, a.imap.save_sent) == (
        "Entwürfe",
        "Gesendet",
        False,
    )
    # Defaults keep an existing config behaving as before.
    assert b.allowed_from == []
    assert (b.imap.drafts_folder, b.imap.sent_folder, b.imap.save_sent) == (None, None, True)
