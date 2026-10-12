"""YAML config loader.

Single file, validated via pydantic. Each mailbox can have any subset of
{imap, smtp} — at least one is required. `name` must be unique and
URL-safe (it becomes a path segment in the HTTP API).

String values may reference environment variables as `${VAR}` (or
`${VAR:-default}`), so passwords and tokens can stay out of the file.

The file is optional: `MAILBOX_*` environment variables can define the whole
configuration, and override the file where both are present (see
`env_config`).
"""

from __future__ import annotations

import base64
import os
import re
import ssl
from pathlib import Path
from typing import Any, Literal, Mapping

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from . import env_config

DEFAULT_CONFIG_PATH = "/etc/mailboxd/config.yaml"
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
# `${VAR}` / `${VAR:-default}`; a doubled dollar (`$${VAR}`) escapes it.
_ENV_RE = re.compile(r"\$(\$)?\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


TlsMode = Literal["ssl", "starttls", "none"]


class ImapConfig(BaseModel):
    host: str
    port: int = 993
    tls: TlsMode = "ssl"
    # Check the server's certificate and hostname. Only turn off for a server
    # with a self-signed certificate that you reach over a network you trust.
    tls_verify: bool = True
    username: str
    password: str
    default_folder: str = "INBOX"
    # Used only when the server doesn't flag a folder with SPECIAL-USE
    # \Drafts / \Sent; unset falls back to "Drafts" / "Sent".
    drafts_folder: str | None = None
    sent_folder: str | None = None
    # APPEND a copy of every sent message to the Sent folder. Turn off for
    # providers that already file SMTP submissions there (Gmail, Outlook).
    save_sent: bool = True


class SmtpConfig(BaseModel):
    host: str
    port: int = 587
    tls: TlsMode = "starttls"
    tls_verify: bool = True
    username: str
    password: str
    from_address: str


class MailboxConfig(BaseModel):
    name: str
    description: str = ""
    # Preset an env-defined mailbox was built from; informational only.
    provider: str | None = None
    imap: ImapConfig | None = None
    smtp: SmtpConfig | None = None
    # From addresses this mailbox may send as; `*` is a wildcard
    # (`*@example.com`). Empty list = unrestricted.
    allowed_from: list[str] = Field(default_factory=list)

    @field_validator("allowed_from")
    @classmethod
    def _no_empty_patterns(cls, v: list[str]) -> list[str]:
        for p in v:
            if not p or not p.strip():
                raise ValueError("allowed_from must not contain empty strings")
        return v

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(f"mailbox name {v!r} invalid: must match [a-zA-Z0-9_-]+")
        return v

    @model_validator(mode="after")
    def _has_at_least_one_protocol(self) -> "MailboxConfig":
        if self.imap is None and self.smtp is None:
            raise ValueError(
                f"mailbox {self.name!r} declares no protocols — " "set at least one of imap / smtp"
            )
        return self


class AuthConfig(BaseModel):
    """Bearer-token auth. Empty `tokens` list disables auth entirely.

    `load_config` refuses to produce that state unless
    `MAILBOX_ALLOW_NO_AUTH=true` says it is intended.
    """

    tokens: list[str] = Field(default_factory=list)

    @field_validator("tokens")
    @classmethod
    def _no_empty_tokens(cls, v: list[str]) -> list[str]:
        for t in v:
            if not t or not t.strip():
                raise ValueError("auth.tokens must not contain empty strings")
        return v


class Config(BaseModel):
    log_level: str = "INFO"
    auth: AuthConfig = Field(default_factory=AuthConfig)
    mailboxes: list[MailboxConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> "Config":
        seen: set[str] = set()
        for m in self.mailboxes:
            if m.name in seen:
                raise ValueError(f"duplicate mailbox name: {m.name!r}")
            seen.add(m.name)
        return self

    def get(self, name: str) -> MailboxConfig | None:
        for m in self.mailboxes:
            if m.name == name:
                return m
        return None


def tls_context(verify: bool = True) -> ssl.SSLContext:
    """The TLS context for an IMAP / SMTP connection.

    This has to be passed explicitly: left to themselves, imaplib and smtplib
    use a context that checks neither the certificate nor the hostname, so
    anyone on the network path could impersonate the mail server and collect
    the password.
    """
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class ConfigError(Exception):
    """Raised when config can't be loaded or is invalid.

    Its message is safe to log: it never contains a configured value, only
    the names of the settings at fault.
    """


# Shorter than this and blanking it out would mangle ordinary words.
_MIN_SECRET_LEN = 4


def redact(text: str, *secrets: str | None) -> str:
    """Blank out any of `secrets` appearing in `text` (e.g. a server echoing a login).

    Also catches the forms a secret takes on the wire: backslash-escaped
    inside an IMAP quoted string, and base64 as SMTP AUTH sends it.
    """
    forms: set[str] = set()
    for secret in secrets:
        if not secret or len(secret) < _MIN_SECRET_LEN:
            continue
        forms.add(secret)
        forms.add(secret.replace("\\", "\\\\").replace('"', '\\"'))
        forms.add(base64.b64encode(secret.encode("utf-8")).decode("ascii"))
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, "***")
    return text


def _expand_env(value: Any, environ: Mapping[str, str], path: str = "") -> Any:
    """Substitute `${VAR}` references in every string value of parsed YAML.

    Runs on the parsed tree, not the raw text, so a secret full of YAML
    metacharacters can't change the document's structure.
    """
    if isinstance(value, dict):
        return {
            k: _expand_env(v, environ, f"{path}.{k}" if path else str(k)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_expand_env(v, environ, f"{path}[{i}]") for i, v in enumerate(value)]
    if not isinstance(value, str):
        return value

    def _sub(m: re.Match[str]) -> str:
        if m.group(1):
            return m.group(0)[1:]
        name, default = m.group(2), m.group(3)
        env = environ.get(name)
        if default is not None:
            return env or default
        if env is None:
            # Name the variable only when the value is nothing but the
            # reference. Inside a longer string it may just as well be part of
            # a literal password (`pa${ss}word`), which must not be echoed.
            which = f"${{{name}}}" if m.group(0) == value else "a ${...} reference"
            raise ConfigError(
                f"{path}: {which} names an environment variable that is not set "
                "(write $${ for a literal ${)"
            )
        return env

    return _ENV_RE.sub(_sub, value)


def _load_file(p: Path, environ: Mapping[str, str]) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        # Position only. PyYAML's message quotes the offending line and names
        # the offending token, and either may well be a password.
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ConfigError(f"config {p} is not valid YAML{where}") from None
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"config {p} must be a YAML mapping")
    try:
        return dict(_expand_env(raw, environ))
    except ConfigError as e:
        raise ConfigError(f"config {p} invalid: {e}") from None


def _describe(e: ValidationError, raw: Mapping[str, Any]) -> str:
    """Validation errors as `where: what`, without pydantic's echo of the input.

    The default rendering prints the offending value — for a mailbox that is
    the dict holding its password.
    """
    boxes = raw.get("mailboxes")
    parts = []
    for err in e.errors(include_url=False, include_context=False, include_input=False):
        loc = [str(x) for x in err["loc"]]
        if loc[:1] == ["mailboxes"] and len(loc) > 1 and isinstance(boxes, list):
            box = boxes[int(loc[1])] if loc[1].isdigit() and int(loc[1]) < len(boxes) else None
            if isinstance(box, dict) and isinstance(box.get("name"), str):
                loc[:2] = [f"mailbox {box['name']!r}"]
        parts.append(f"{' -> '.join(loc)}: {err['msg']}" if loc else err["msg"])
    return "; ".join(parts)


def load_config(path: str | None = None, environ: Mapping[str, str] | None = None) -> Config:
    """Load the config file (if there is one) with `MAILBOX_*` variables laid over it."""
    env = os.environ if environ is None else environ
    cfg_path = path or env.get("MAILBOXD_CONFIG") or DEFAULT_CONFIG_PATH
    p = Path(cfg_path)
    has_file = p.is_file()
    try:
        raw, allow_no_auth = env_config.overlay(_load_file(p, env) if has_file else {}, env)
    except env_config.EnvError as e:
        raise ConfigError(str(e)) from None
    if not has_file and not raw["mailboxes"]:
        raise ConfigError(
            f"config not found: {cfg_path} — mount a config file there, or define accounts "
            f"with {env_config.PREFIX}<NAME>={env_config.ACCOUNT_FORMAT}"
        )
    try:
        cfg = Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"config invalid: {_describe(e, raw)}") from None
    if not cfg.auth.tokens and not allow_no_auth:
        raise ConfigError(
            "no bearer tokens configured: set MAILBOX_AUTH_TOKENS (or auth.tokens in the "
            "config file), or set MAILBOX_ALLOW_NO_AUTH=true to run without authentication"
        )
    return cfg
