"""Configuration from `MAILBOX_*` environment variables.

The whole server can be configured from the environment, no config file
needed. Every `MAILBOX_*` variable is one of four things:

  global setting     MAILBOX_AUTH_TOKENS, MAILBOX_LOG_LEVEL, MAILBOX_PORT,
                     MAILBOX_ALLOW_NO_AUTH
  provider preset    MAILBOX_PROVIDER_<NAME>_{IMAP,SMTP}_{HOST,PORT,TLS,TLS_VERIFY}
  account override   MAILBOX_<NAME>_<SUFFIX>, for the suffixes in `_ACCOUNT_FIELDS`
  account            anything else: MAILBOX_<NAME>=<provider>:<username>:<password>

`overlay` lays the result over the parsed config file, environment winning.

Account values contain passwords, so nothing here ever puts a variable's
*value* in an error message — only its name.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Mapping

from .providers import PROVIDERS, Preset

PREFIX = "MAILBOX_"
PROVIDER_PREFIX = "MAILBOX_PROVIDER_"
ACCOUNT_FORMAT = "<provider>:<username>:<password>"

_GLOBALS = frozenset(
    {"MAILBOX_AUTH_TOKENS", "MAILBOX_LOG_LEVEL", "MAILBOX_PORT", "MAILBOX_ALLOW_NO_AUTH"}
)
# Client-side names used by this project's own agent skill and stdio plugin.
# A shell that has them exported must not grow two bogus accounts.
_IGNORED = frozenset({"MAILBOX_URL", "MAILBOX_TOKEN"})

# Variable suffix -> where the value lands in a mailbox's config.
_PROVIDER_FIELDS: dict[str, tuple[str, ...]] = {
    "IMAP_HOST": ("imap", "host"),
    "IMAP_PORT": ("imap", "port"),
    "IMAP_TLS": ("imap", "tls"),
    "IMAP_TLS_VERIFY": ("imap", "tls_verify"),
    "SMTP_HOST": ("smtp", "host"),
    "SMTP_PORT": ("smtp", "port"),
    "SMTP_TLS": ("smtp", "tls"),
    "SMTP_TLS_VERIFY": ("smtp", "tls_verify"),
}
_ACCOUNT_FIELDS: dict[str, tuple[str, ...]] = {
    **_PROVIDER_FIELDS,
    "FROM_ADDRESS": ("smtp", "from_address"),
    "ALLOWED_FROM": ("allowed_from",),
    "DRAFTS_FOLDER": ("imap", "drafts_folder"),
    "SENT_FOLDER": ("imap", "sent_folder"),
    "DEFAULT_FOLDER": ("imap", "default_folder"),
    "SAVE_SENT": ("imap", "save_sent"),
}

_NAME_RE = re.compile(r"[a-z0-9_-]+")
_EMAIL_RE = re.compile(r"[^\s:@]+@[^\s:@]+")
_PORT_RE = re.compile(r"[0-9]{1,5}")
_TLS_MODES = ("ssl", "starttls", "none")
_LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("", "0", "false", "no", "off")


class EnvError(ValueError):
    """A `MAILBOX_*` variable is malformed. The message names the variable."""


def _split_suffix(rest: str, fields: Mapping[str, Any]) -> tuple[str, str] | None:
    """Split `<NAME>_<SUFFIX>` for a known suffix; None if there isn't one."""
    for suffix in fields:
        if rest.endswith(f"_{suffix}") and len(rest) > len(suffix) + 1:
            return rest[: -len(suffix) - 1], suffix
    return None


def _port(var: str, value: str) -> int:
    if not _PORT_RE.fullmatch(value) or not 1 <= int(value) <= 65535:
        raise EnvError(f"{var}: must be a port number between 1 and 65535")
    return int(value)


def _bool(var: str, value: str) -> bool:
    if value.strip().lower() not in _TRUE + _FALSE:
        raise EnvError(f"{var}: must be true or false")
    return value.strip().lower() in _TRUE


def _coerce(var: str, suffix: str, value: str) -> Any:
    value = value.strip()
    if suffix.endswith("_PORT"):
        return _port(var, value)
    if suffix.endswith("_TLS"):
        if value.lower() not in _TLS_MODES:
            raise EnvError(f"{var}: must be one of {' | '.join(_TLS_MODES)}")
        return value.lower()
    if suffix == "ALLOWED_FROM":
        patterns = [p.strip() for p in value.split(",") if p.strip()]
        if not patterns:
            # An empty list would mean "unrestricted", silently undoing the default.
            raise EnvError(f"{var}: must list at least one address pattern (`*` allows any)")
        return patterns
    if suffix == "SAVE_SENT" or suffix.endswith("_TLS_VERIFY"):
        return _bool(var, value)
    return value


def parse_account(var: str, value: str) -> tuple[str, str, str]:
    """Split an account value into (provider, username, password).

    The provider ends at the first colon. The username is an email address,
    so it ends at the first colon *after its `@`* — which leaves the password
    free to contain colons, `@` and anything else.
    """
    provider, sep, rest = value.partition(":")
    at = rest.find("@")
    end = rest.find(":", at) if at > 0 else -1
    if not sep or end < 0:
        raise EnvError(
            f"{var}: expected {ACCOUNT_FORMAT}, where <username> is the full email address"
        )
    username, password = rest[:end].strip(), rest[end + 1 :]
    if not _EMAIL_RE.fullmatch(username):
        raise EnvError(
            f"{var}: expected {ACCOUNT_FORMAT} — the username must be an email address "
            "and cannot contain ':' or spaces"
        )
    if not password:
        raise EnvError(f"{var}: the password is empty")
    return provider.strip().lower(), username, password


def _presets(environ: Mapping[str, str]) -> dict[str, Preset]:
    """Built-in provider presets with any `MAILBOX_PROVIDER_*` variables applied."""
    presets = copy.deepcopy(PROVIDERS)
    touched: dict[tuple[str, str], str] = {}
    for var in sorted(environ):
        if not var.startswith(PROVIDER_PREFIX):
            continue
        hit = _split_suffix(var[len(PROVIDER_PREFIX) :], _PROVIDER_FIELDS)
        if hit is None:
            raise EnvError(
                f"{var}: not a provider setting — expected MAILBOX_PROVIDER_<NAME>_ followed "
                f"by one of {', '.join(_PROVIDER_FIELDS)}"
            )
        if not environ[var].strip():
            continue
        name, suffix = hit[0].lower(), hit[1]
        proto, key = _PROVIDER_FIELDS[suffix]
        presets.setdefault(name, {}).setdefault(proto, {})[key] = _coerce(var, suffix, environ[var])
        touched.setdefault((name, proto), var)
    for (name, proto), var in touched.items():
        if not presets[name][proto].get("host"):
            raise EnvError(
                f"{var}: provider {name!r} has no {proto.upper()} host — also set "
                f"{PROVIDER_PREFIX}{name.upper()}_{proto.upper()}_HOST"
            )
    return presets


def _merge(*layers: Mapping[str, Any]) -> dict[str, Any]:
    """Later layers win; `imap` / `smtp` sections merge key by key."""
    out: dict[str, Any] = {}
    for layer in layers:
        for key, value in layer.items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = {**out[key], **value}
            else:
                out[key] = copy.deepcopy(value)
    return out


def _mailboxes(file_boxes: list[Any], environ: Mapping[str, str]) -> list[Any]:
    presets = _presets(environ)
    from_file = {b["name"]: b for b in file_boxes if isinstance(b, dict) and "name" in b}
    # Env names are lower-cased; let them still address a file mailbox `Work`.
    spelled = {str(name).lower(): name for name in from_file}
    accounts: dict[str, str] = {}  # account name -> defining variable
    overrides: list[tuple[str, str, str]] = []  # (variable, account name, suffix)
    for var in sorted(environ):
        if not var.startswith(PREFIX) or var.startswith(PROVIDER_PREFIX):
            continue
        if var in _GLOBALS or var in _IGNORED:
            continue
        rest = var[len(PREFIX) :]
        hit = _split_suffix(rest, _ACCOUNT_FIELDS)
        if hit is not None:
            if environ[var].strip():
                name = hit[0].lower()
                overrides.append((var, spelled.get(name, name), hit[1]))
            continue
        name = rest.lower()
        if not _NAME_RE.fullmatch(name):
            raise EnvError(f"{var}: an account name may only contain letters, digits, '_' and '-'")
        name = spelled.get(name, name)
        if name in accounts:
            raise EnvError(
                f"{var}: defines the same account as {accounts[name]} (names are lower-cased)"
            )
        accounts[name] = var

    # Three layers per mailbox, weakest first: defaults derived from the env
    # account, the config file, then values set explicitly in the environment.
    derived: dict[str, dict[str, Any]] = {}
    explicit: dict[str, dict[str, Any]] = {}
    for name, var in accounts.items():
        provider, username, password = parse_account(var, environ[var])
        preset = presets.get(provider)
        if preset is None:
            raise EnvError(
                f"{var}: unknown provider (the text before the first ':'). Known providers: "
                f"{', '.join(sorted(presets))}. Define another with "
                f"{PROVIDER_PREFIX}<NAME>_IMAP_HOST / _SMTP_HOST."
            )
        box: dict[str, Any] = {"name": name, "provider": provider}
        for proto, section in preset.items():
            box[proto] = {**section, "username": username, "password": password}
        explicit[name] = box
        derived[name] = {"allowed_from": [f"*@{username.rsplit('@', 1)[1].lower()}"]}
        if "smtp" in preset:
            derived[name]["smtp"] = {"from_address": username}

    for var, name, suffix in overrides:
        if name not in explicit and name not in from_file:
            raise EnvError(
                f"{var}: there is no account named {name!r} — define it with "
                f"{PREFIX}{name.upper()}={ACCOUNT_FORMAT}"
            )
        path = _ACCOUNT_FIELDS[suffix]
        target = explicit.setdefault(name, {})
        if len(path) == 2:
            existing = _merge(from_file.get(name, {}), target).get(path[0])
            if not isinstance(existing, dict):
                raise EnvError(f"{var}: account {name!r} has no {path[0].upper()} to configure")
            target = target.setdefault(path[0], {})
        target[path[-1]] = _coerce(var, suffix, environ[var])

    def _layered(name: str, box: Mapping[str, Any]) -> dict[str, Any]:
        return _merge(derived.get(name, {}), box, explicit.get(name, {}))

    merged: list[Any] = [
        _layered(b["name"], b) if isinstance(b, dict) and "name" in b else b for b in file_boxes
    ]
    merged += [_layered(name, {}) for name in sorted(explicit) if name not in from_file]
    return merged


def overlay(raw: Mapping[str, Any], environ: Mapping[str, str]) -> tuple[dict[str, Any], bool]:
    """Lay `MAILBOX_*` settings over the parsed config file.

    Returns the merged config mapping and whether running without bearer
    tokens was explicitly allowed (`MAILBOX_ALLOW_NO_AUTH`).
    """
    out = dict(raw)
    file_boxes = raw.get("mailboxes")
    out["mailboxes"] = _mailboxes(file_boxes if isinstance(file_boxes, list) else [], environ)

    level = environ.get("MAILBOX_LOG_LEVEL", "").strip()
    if level:
        if level.upper() not in _LOG_LEVELS:
            raise EnvError(f"MAILBOX_LOG_LEVEL: must be one of {' | '.join(_LOG_LEVELS)}")
        out["log_level"] = level.upper()

    tokens = [t.strip() for t in environ.get("MAILBOX_AUTH_TOKENS", "").split(",") if t.strip()]
    if tokens:
        auth = raw.get("auth")
        out["auth"] = {**(auth if isinstance(auth, dict) else {}), "tokens": tokens}

    if environ.get("MAILBOX_PORT", "").strip():
        _port("MAILBOX_PORT", environ["MAILBOX_PORT"].strip())

    return out, _bool("MAILBOX_ALLOW_NO_AUTH", environ.get("MAILBOX_ALLOW_NO_AUTH", ""))
