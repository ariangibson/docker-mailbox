"""MCP streamable-HTTP server exposing the same operations as the HTTP API.

Tool design: ONE flat set of tools. Each operation that targets a specific
mailbox takes a `mailbox` argument (mailbox name OR its address). This keeps
the tool catalog constant-sized regardless of how many mailboxes are
configured — agents discover available mailboxes via the `mailboxes` tool
and pass the chosen one as a parameter.

This module exposes a `StreamableHTTPSessionManager` and a lifespan helper
that the FastAPI app mounts under `/mcp`. There is no stdio transport.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, AsyncIterator, Callable

import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from . import __version__, ops
from .config import Config, MailboxConfig
from .imap_client import MAX_MOVE, ImapError
from .imap_client import delete_message as imap_delete
from .imap_client import list_folders as imap_list_folders
from .imap_client import list_messages as imap_list_messages
from .imap_client import mark_seen as imap_mark_seen
from .imap_client import mark_spam as imap_mark_spam
from .imap_client import move_messages as imap_move_messages
from .imap_client import search_messages as imap_search_messages
from .imap_client import unified_search as imap_unified_search
from .smtp_client import SmtpError

_MAILBOX_ARG: dict[str, Any] = {
    "type": "string",
    "description": (
        "Target mailbox — accepts the mailbox `name` from config or its "
        "email address (IMAP/SMTP username). Use the `mailboxes` tool to "
        "discover what's available."
    ),
}

_SEARCH_PROPS: dict[str, Any] = {
    "folder": {"type": "string"},
    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
    "from": {"type": "string", "description": "Filter by From address"},
    "to": {"type": "string"},
    "subject": {"type": "string"},
    "body": {"type": "string"},
    "text": {"type": "string", "description": "Full-text search"},
    "since": {"type": "string", "description": "IMAP date e.g. 1-Jan-2026"},
    "before": {"type": "string"},
    "unseen": {"type": "boolean"},
    "seen": {"type": "boolean"},
    "flagged": {"type": "boolean"},
    "answered": {"type": "boolean"},
    "larger_than": {"type": "integer", "minimum": 0},
    "smaller_than": {"type": "integer", "minimum": 0},
}


_THREAD_PROPS: dict[str, Any] = {
    "in_reply_to": {
        "type": "string",
        "description": "Message-ID of the message this one answers, e.g. `<id@host>`.",
    },
    "references": {
        "type": "string",
        "description": (
            "Space-separated Message-IDs of the thread so far, oldest first "
            "(the parent's `references` followed by its `message_id`)."
        ),
    },
}

_DRAFT_PROPS: dict[str, Any] = {
    "to": {"type": "array", "items": {"type": "string"}},
    "subject": {"type": "string"},
    "body_text": {"type": "string"},
    "body_html": {"type": "string"},
    "cc": {"type": "array", "items": {"type": "string"}},
    "bcc": {"type": "array", "items": {"type": "string"}},
    "from_address": {"type": "string"},
    "reply_to": {"type": "string"},
    **_THREAD_PROPS,
}

_DRAFT_UID: dict[str, Any] = {
    "type": "string",
    "description": "UID of the draft, as returned by `save_draft` / `list_drafts`.",
}


def _draft_kwargs(args: dict[str, Any]) -> dict[str, Any]:
    return {key: args.get(key) for key in _DRAFT_PROPS}


def _search_kwargs(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "folder": args.get("folder"),
        "limit": int(args.get("limit", 50)),
        "from_": args.get("from"),
        "to": args.get("to"),
        "subject": args.get("subject"),
        "body": args.get("body"),
        "text": args.get("text"),
        "since": args.get("since"),
        "before": args.get("before"),
        "unseen": args.get("unseen"),
        "seen": args.get("seen"),
        "flagged": args.get("flagged"),
        "answered": args.get("answered"),
        "larger_than": args.get("larger_than"),
        "smaller_than": args.get("smaller_than"),
    }


def _resolve_mailbox(cfg: Config, ident: str) -> MailboxConfig:
    """Look up a mailbox by name or by IMAP/SMTP username (address)."""
    if not ident:
        raise ValueError("`mailbox` is required")
    for m in cfg.mailboxes:
        if m.name == ident:
            return m
        if m.imap is not None and m.imap.username == ident:
            return m
        if m.smtp is not None and m.smtp.username == ident:
            return m
    raise ValueError(f"unknown mailbox: {ident!r}")


def _require_imap(mb: MailboxConfig) -> Any:
    if mb.imap is None:
        raise ValueError(f"mailbox {mb.name!r} has no IMAP configured")
    return mb.imap


def _require_smtp(mb: MailboxConfig) -> Any:
    if mb.smtp is None:
        raise ValueError(f"mailbox {mb.name!r} has no SMTP configured")
    return mb.smtp


def _build_tools(
    cfg: Config,
) -> tuple[list[types.Tool], dict[str, Callable[[dict[str, Any]], Any]]]:
    tools: list[types.Tool] = []
    handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}

    # ── Discovery ────────────────────────────────────────────────────────
    tools.append(
        types.Tool(
            name="mailboxes",
            description=(
                "List all configured mailboxes with their capabilities. "
                "Each entry has `name`, `description`, `address` (the IMAP/SMTP "
                "username if set), and booleans `imap`/`smtp` indicating which "
                "protocols are wired up. Pass `name` or `address` as the "
                "`mailbox` argument to other tools."
            ),
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        )
    )

    def _mailboxes(_args: dict[str, Any], _c: Config = cfg) -> dict[str, Any]:
        return {
            "mailboxes": [
                {
                    "name": m.name,
                    "description": m.description,
                    "address": (
                        m.imap.username
                        if m.imap is not None
                        else m.smtp.username if m.smtp is not None else None
                    ),
                    "imap": m.imap is not None,
                    "smtp": m.smtp is not None,
                }
                for m in _c.mailboxes
            ]
        }

    handlers["mailboxes"] = _mailboxes

    # ── Unified inbox ────────────────────────────────────────────────────
    if any(mb.imap is not None for mb in cfg.mailboxes):
        _register_unified(cfg, tools, handlers)

    # ── IMAP ops (only if at least one mailbox has IMAP) ─────────────────
    if any(mb.imap is not None for mb in cfg.mailboxes):
        _register_imap_tools(cfg, tools, handlers)

    # ── SMTP ops (only if at least one mailbox has SMTP) ─────────────────
    if any(mb.smtp is not None for mb in cfg.mailboxes):
        _register_smtp_tools(cfg, tools, handlers)

    # ── Reply + drafts (stored over IMAP; sending them needs SMTP too) ───
    if any(mb.imap is not None for mb in cfg.mailboxes):
        _register_reply_tool(cfg, tools, handlers)
        _register_draft_tools(cfg, tools, handlers)

    return tools, handlers


def _register_unified(
    cfg: Config,
    tools: list[types.Tool],
    handlers: dict[str, Callable[[dict[str, Any]], Any]],
) -> None:
    schema_props: dict[str, Any] = {
        "mailbox": {
            "type": "string",
            "description": (
                "CSV of mailbox names or email addresses to restrict the search "
                "to. Omit for all IMAP mailboxes."
            ),
        },
        **_SEARCH_PROPS,
    }

    tools.append(
        types.Tool(
            name="inbox",
            description=(
                "Unified inbox across all configured IMAP mailboxes. Returns a "
                "merged, newest-first feed. Use `mailbox` (CSV) to filter by "
                "mailbox name or email address. Use `from`, `subject`, etc. to "
                "filter content. Each result is tagged with its source `mailbox` "
                "and `mailbox_address`."
            ),
            inputSchema={
                "type": "object",
                "properties": schema_props,
                "additionalProperties": False,
            },
        )
    )

    def _inbox(args: dict[str, Any], _c: Config = cfg) -> dict[str, Any]:
        filter_csv = args.get("mailbox")
        if filter_csv:
            wanted = {s.strip() for s in str(filter_csv).split(",") if s.strip()}
            targets = [
                m
                for m in _c.mailboxes
                if m.imap is not None and (m.name in wanted or m.imap.username in wanted)
            ]
        else:
            targets = [m for m in _c.mailboxes if m.imap is not None]
        return imap_unified_search(targets, **_search_kwargs(args))

    handlers["inbox"] = _inbox


def _register_imap_tools(
    cfg: Config,
    tools: list[types.Tool],
    handlers: dict[str, Callable[[dict[str, Any]], Any]],
) -> None:
    tools.append(
        types.Tool(
            name="list_folders",
            description="List IMAP folders in the given mailbox.",
            inputSchema={
                "type": "object",
                "properties": {"mailbox": _MAILBOX_ARG},
                "required": ["mailbox"],
                "additionalProperties": False,
            },
        )
    )

    def _list_folders(args: dict[str, Any], _c: Config = cfg) -> dict[str, Any]:
        mb = _resolve_mailbox(_c, str(args["mailbox"]))
        return {"folders": imap_list_folders(_require_imap(mb))}

    handlers["list_folders"] = _list_folders

    tools.append(
        types.Tool(
            name="list_messages",
            description=(
                "List recent messages from a mailbox. Returns newest-first "
                "headers; `uid` is what other tools want."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "mailbox": _MAILBOX_ARG,
                    "folder": {"type": "string", "description": "IMAP folder (default INBOX)"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
                    "search": {
                        "type": "string",
                        "description": "IMAP SEARCH criteria, e.g. 'UNSEEN' or 'FROM foo@bar'",
                        "default": "ALL",
                    },
                },
                "required": ["mailbox"],
                "additionalProperties": False,
            },
        )
    )

    def _list_messages(args: dict[str, Any], _c: Config = cfg) -> dict[str, Any]:
        mb = _resolve_mailbox(_c, str(args["mailbox"]))
        return {
            "messages": imap_list_messages(
                _require_imap(mb),
                folder=args.get("folder"),
                limit=int(args.get("limit", 50)),
                search=str(args.get("search", "ALL")),
            )
        }

    handlers["list_messages"] = _list_messages

    tools.append(
        types.Tool(
            name="search",
            description=(
                "Structured search in a single mailbox. Filter by from/to/"
                "subject/body/text/dates/flags/size. Returns newest-first headers."
            ),
            inputSchema={
                "type": "object",
                "properties": {"mailbox": _MAILBOX_ARG, **_SEARCH_PROPS},
                "required": ["mailbox"],
                "additionalProperties": False,
            },
        )
    )

    def _search(args: dict[str, Any], _c: Config = cfg) -> dict[str, Any]:
        mb = _resolve_mailbox(_c, str(args["mailbox"]))
        return {"messages": imap_search_messages(_require_imap(mb), **_search_kwargs(args))}

    handlers["search"] = _search

    tools.append(
        types.Tool(
            name="get_message",
            description=(
                "Fetch a full message (incl. body) from a mailbox by UID. "
                "Pass `reader=true` to also include `body_reader` — a clean, "
                "readable text/markdown version of the HTML body with chrome "
                "stripped (no tables, styles, tracking pixels, just the words). "
                "Best for LLMs that don't want to read raw HTML. Also returns the "
                "threading headers (`message_id`, `in_reply_to`, `references`), "
                "the delivery headers (`delivered_to`, `x_original_to`, "
                "`envelope_to`) and `received_for`: which of the mailbox's own "
                "addresses the message was sent to (null if none matched). On a "
                "catch-all mailbox that is the address to answer from. It is "
                "read from the message's headers, so it is a hint, not proof."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "mailbox": _MAILBOX_ARG,
                    "uid": {"type": "string"},
                    "folder": {"type": "string"},
                    "reader": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include `body_reader` (HTML stripped to readable text).",
                    },
                },
                "required": ["mailbox", "uid"],
                "additionalProperties": False,
            },
        )
    )

    def _get_message(args: dict[str, Any], _c: Config = cfg) -> dict[str, Any]:
        mb = _resolve_mailbox(_c, str(args["mailbox"]))
        _require_imap(mb)
        return ops.get_message(
            mb,
            str(args["uid"]),
            folder=args.get("folder"),
            reader=bool(args.get("reader", False)),
        )

    handlers["get_message"] = _get_message

    tools.append(
        types.Tool(
            name="delete_message",
            description="Permanently delete a message (flag + expunge) in a mailbox.",
            inputSchema={
                "type": "object",
                "properties": {
                    "mailbox": _MAILBOX_ARG,
                    "uid": {"type": "string"},
                    "folder": {"type": "string"},
                },
                "required": ["mailbox", "uid"],
                "additionalProperties": False,
            },
        )
    )

    def _del(args: dict[str, Any], _c: Config = cfg) -> dict[str, Any]:
        mb = _resolve_mailbox(_c, str(args["mailbox"]))
        imap_delete(_require_imap(mb), str(args["uid"]), folder=args.get("folder"))
        return {"ok": True}

    handlers["delete_message"] = _del

    tools.append(
        types.Tool(
            name="mark_seen",
            description="Set or clear the \\Seen flag on a message in a mailbox.",
            inputSchema={
                "type": "object",
                "properties": {
                    "mailbox": _MAILBOX_ARG,
                    "uid": {"type": "string"},
                    "folder": {"type": "string"},
                    "seen": {"type": "boolean", "default": True},
                },
                "required": ["mailbox", "uid"],
                "additionalProperties": False,
            },
        )
    )

    def _seen(args: dict[str, Any], _c: Config = cfg) -> dict[str, Any]:
        mb = _resolve_mailbox(_c, str(args["mailbox"]))
        imap_mark_seen(
            _require_imap(mb),
            str(args["uid"]),
            folder=args.get("folder"),
            seen=bool(args.get("seen", True)),
        )
        return {"ok": True}

    handlers["mark_seen"] = _seen

    _move_props: dict[str, Any] = {
        "mailbox": _MAILBOX_ARG,
        "uids": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": MAX_MOVE,
            "description": "UIDs of the messages to move, all from `from_folder`.",
        },
        "from_folder": {
            "type": "string",
            "description": "Folder the messages are in (default INBOX).",
        },
    }

    tools.append(
        types.Tool(
            name="move_message",
            description=(
                "Move one or more messages to another folder. `to_folder` is a "
                "folder name (see `list_folders`) or one of the roles `junk`, "
                "`trash`, `archive`, which find the mailbox's own folder for "
                "that purpose. Returns the destination's real name and a result "
                "per UID: `moved`, `not_found`, or `error` with a reason."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_move_props,
                    "to_folder": {
                        "type": "string",
                        "description": "Destination folder name, or `junk` / `trash` / `archive`.",
                    },
                },
                "required": ["mailbox", "uids", "to_folder"],
                "additionalProperties": False,
            },
        )
    )

    def _move(args: dict[str, Any], _c: Config = cfg) -> dict[str, Any]:
        mb = _resolve_mailbox(_c, str(args["mailbox"]))
        return imap_move_messages(
            _require_imap(mb),
            [str(u) for u in args["uids"]],
            str(args["to_folder"]),
            from_folder=args.get("from_folder"),
        )

    handlers["move_message"] = _move

    tools.append(
        types.Tool(
            name="mark_spam",
            description=(
                "Move one or more messages to the mailbox's Junk folder — the "
                "same as `move_message` with `to_folder: junk`."
            ),
            inputSchema={
                "type": "object",
                "properties": _move_props,
                "required": ["mailbox", "uids"],
                "additionalProperties": False,
            },
        )
    )

    def _spam(args: dict[str, Any], _c: Config = cfg) -> dict[str, Any]:
        mb = _resolve_mailbox(_c, str(args["mailbox"]))
        return imap_mark_spam(
            _require_imap(mb),
            [str(u) for u in args["uids"]],
            from_folder=args.get("from_folder"),
        )

    handlers["mark_spam"] = _spam


def _register_smtp_tools(
    cfg: Config,
    tools: list[types.Tool],
    handlers: dict[str, Callable[[dict[str, Any]], Any]],
) -> None:
    tools.append(
        types.Tool(
            name="send",
            description=(
                "Send an email from a mailbox via SMTP. A copy is saved to the "
                "mailbox's Sent folder. To answer a message, prefer `reply`, "
                "which fills in recipient, subject and threading for you."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "mailbox": _MAILBOX_ARG,
                    "to": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "subject": {"type": "string"},
                    "body_text": {"type": "string"},
                    "body_html": {"type": "string"},
                    "cc": {"type": "array", "items": {"type": "string"}},
                    "bcc": {"type": "array", "items": {"type": "string"}},
                    "from_address": {"type": "string"},
                    "reply_to": {"type": "string"},
                    **_THREAD_PROPS,
                },
                "required": ["mailbox", "to", "subject"],
                "additionalProperties": False,
            },
        )
    )

    def _send(args: dict[str, Any], _c: Config = cfg) -> dict[str, Any]:
        mb = _resolve_mailbox(_c, str(args["mailbox"]))
        _require_smtp(mb)
        return ops.send(
            mb,
            to=list(args["to"]),
            subject=str(args["subject"]),
            body_text=args.get("body_text"),
            body_html=args.get("body_html"),
            cc=list(args.get("cc") or []),
            bcc=list(args.get("bcc") or []),
            from_address=args.get("from_address"),
            reply_to=args.get("reply_to"),
            in_reply_to=args.get("in_reply_to"),
            references=args.get("references"),
        )

    handlers["send"] = _send


def _register_reply_tool(
    cfg: Config,
    tools: list[types.Tool],
    handlers: dict[str, Callable[[dict[str, Any]], Any]],
) -> None:
    tools.append(
        types.Tool(
            name="reply",
            description=(
                "Reply to a message. Recipient (the original's Reply-To, else "
                "its From), subject (`Re: ...`) and threading headers are taken "
                "from the original — supply only the body. `from_address` "
                "defaults to the original's `received_for`, so a catch-all "
                "mailbox answers from the address the mail was sent to. Pass "
                "`draft=true` to save the reply to Drafts instead of sending it."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "mailbox": _MAILBOX_ARG,
                    "uid": {"type": "string", "description": "UID of the message to answer."},
                    "folder": {
                        "type": "string",
                        "description": "Folder that message is in (default INBOX).",
                    },
                    "body_text": {"type": "string"},
                    "body_html": {"type": "string"},
                    "from_address": {"type": "string"},
                    "draft": {"type": "boolean", "default": False},
                },
                "required": ["mailbox", "uid"],
                "additionalProperties": False,
            },
        )
    )

    def _reply(args: dict[str, Any], _c: Config = cfg) -> dict[str, Any]:
        mb = _resolve_mailbox(_c, str(args["mailbox"]))
        _require_imap(mb)
        draft = bool(args.get("draft", False))
        if not draft:
            _require_smtp(mb)
        return ops.reply(
            mb,
            str(args["uid"]),
            folder=args.get("folder"),
            body_text=args.get("body_text"),
            body_html=args.get("body_html"),
            from_address=args.get("from_address"),
            draft=draft,
        )

    handlers["reply"] = _reply


def _register_draft_tools(
    cfg: Config,
    tools: list[types.Tool],
    handlers: dict[str, Callable[[dict[str, Any]], Any]],
) -> None:
    tools.append(
        types.Tool(
            name="save_draft",
            description=(
                "Save a new message to the mailbox's Drafts folder without "
                "sending it. Every field but `mailbox` is optional. Returns the "
                "draft's `uid`."
            ),
            inputSchema={
                "type": "object",
                "properties": {"mailbox": _MAILBOX_ARG, **_DRAFT_PROPS},
                "required": ["mailbox"],
                "additionalProperties": False,
            },
        )
    )

    def _save_draft(args: dict[str, Any], _c: Config = cfg) -> dict[str, Any]:
        mb = _resolve_mailbox(_c, str(args["mailbox"]))
        _require_imap(mb)
        return ops.save_draft(mb, **_draft_kwargs(args))

    handlers["save_draft"] = _save_draft

    tools.append(
        types.Tool(
            name="list_drafts",
            description="List the mailbox's drafts, newest first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "mailbox": _MAILBOX_ARG,
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
                },
                "required": ["mailbox"],
                "additionalProperties": False,
            },
        )
    )

    def _list_drafts(args: dict[str, Any], _c: Config = cfg) -> dict[str, Any]:
        mb = _resolve_mailbox(_c, str(args["mailbox"]))
        _require_imap(mb)
        return ops.list_drafts(mb, limit=int(args.get("limit", 50)))

    handlers["list_drafts"] = _list_drafts

    tools.append(
        types.Tool(
            name="get_draft",
            description="Fetch a full draft (headers, incl. `bcc`, and body) by UID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "mailbox": _MAILBOX_ARG,
                    "uid": _DRAFT_UID,
                    "reader": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include `body_reader` (HTML stripped to readable text).",
                    },
                },
                "required": ["mailbox", "uid"],
                "additionalProperties": False,
            },
        )
    )

    def _get_draft(args: dict[str, Any], _c: Config = cfg) -> dict[str, Any]:
        mb = _resolve_mailbox(_c, str(args["mailbox"]))
        _require_imap(mb)
        return ops.get_draft(mb, str(args["uid"]), reader=bool(args.get("reader", False)))

    handlers["get_draft"] = _get_draft

    tools.append(
        types.Tool(
            name="update_draft",
            description=(
                "Change a draft. Only the fields you pass are changed; the rest "
                "(recipients, subject, threading) carry over. `body_text` and "
                "`body_html` are replaced as a pair. Pass an empty list or empty "
                "string to clear a field. The draft gets a NEW `uid` (returned) "
                "— the old one stops existing. A draft with attachments cannot "
                "be updated (it would lose them)."
            ),
            inputSchema={
                "type": "object",
                "properties": {"mailbox": _MAILBOX_ARG, "uid": _DRAFT_UID, **_DRAFT_PROPS},
                "required": ["mailbox", "uid"],
                "additionalProperties": False,
            },
        )
    )

    def _update_draft(args: dict[str, Any], _c: Config = cfg) -> dict[str, Any]:
        mb = _resolve_mailbox(_c, str(args["mailbox"]))
        _require_imap(mb)
        return ops.update_draft(mb, str(args["uid"]), **_draft_kwargs(args))

    handlers["update_draft"] = _update_draft

    tools.append(
        types.Tool(
            name="delete_draft",
            description="Permanently delete a draft (flag + expunge).",
            inputSchema={
                "type": "object",
                "properties": {"mailbox": _MAILBOX_ARG, "uid": _DRAFT_UID},
                "required": ["mailbox", "uid"],
                "additionalProperties": False,
            },
        )
    )

    def _delete_draft(args: dict[str, Any], _c: Config = cfg) -> dict[str, Any]:
        mb = _resolve_mailbox(_c, str(args["mailbox"]))
        _require_imap(mb)
        return ops.delete_draft(mb, str(args["uid"]))

    handlers["delete_draft"] = _delete_draft

    if not any(mb.imap is not None and mb.smtp is not None for mb in cfg.mailboxes):
        return

    tools.append(
        types.Tool(
            name="send_draft",
            description=(
                "Send a draft exactly as saved, file a copy in Sent, then "
                "delete the draft. A result with `warnings` still means the "
                "message was sent (only the Sent copy or the draft cleanup "
                "failed) — do not send it again."
            ),
            inputSchema={
                "type": "object",
                "properties": {"mailbox": _MAILBOX_ARG, "uid": _DRAFT_UID},
                "required": ["mailbox", "uid"],
                "additionalProperties": False,
            },
        )
    )

    def _send_draft(args: dict[str, Any], _c: Config = cfg) -> dict[str, Any]:
        mb = _resolve_mailbox(_c, str(args["mailbox"]))
        _require_imap(mb)
        _require_smtp(mb)
        return ops.send_draft(mb, str(args["uid"]))

    handlers["send_draft"] = _send_draft


def build_mcp_app(cfg: Config) -> Server:
    """Build the low-level MCP `Server` with all tools registered for `cfg`."""
    tools, handlers = _build_tools(cfg)

    server: Server = Server("mailboxd", version=__version__)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        fn = handlers.get(name)
        if fn is None:
            raise ValueError(f"unknown tool: {name}")
        try:
            result = fn(arguments or {})
        except (ImapError, SmtpError, ValueError) as e:
            return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]
        return [types.TextContent(type="text", text=json.dumps(result, default=str))]

    return server


def build_session_manager(cfg: Config) -> StreamableHTTPSessionManager:
    """Build a streamable-HTTP session manager for the MCP server.

    The returned manager must be driven by its `run()` async-context manager,
    typically from the host ASGI app's lifespan.
    """
    mcp_app = build_mcp_app(cfg)
    # stateless=True keeps things simple: no server-side session table, every
    # request carries its own correlation. Plays well behind any LB.
    return StreamableHTTPSessionManager(
        mcp_app,
        json_response=False,
        stateless=True,
    )


@contextlib.asynccontextmanager
async def mcp_lifespan(manager: StreamableHTTPSessionManager) -> AsyncIterator[None]:
    """Lifespan hook that starts/stops the MCP session manager."""
    async with manager.run():
        yield


def get_notification_options() -> NotificationOptions:
    """Exposed for callers/tests that need the default notification options."""
    return NotificationOptions()
