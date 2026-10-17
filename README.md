# mailbox

[![CI](https://github.com/psyb0t/docker-mailbox/actions/workflows/pipeline.yml/badge.svg?branch=main)](https://github.com/psyb0t/docker-mailbox/actions/workflows/pipeline.yml)
[![version](https://raw.githubusercontent.com/psyb0t/docker-mailbox/badges/version.svg)](https://github.com/psyb0t/docker-mailbox/releases)
[![license](https://raw.githubusercontent.com/psyb0t/docker-mailbox/badges/license.svg)](LICENSE)
[![Docker Pulls](https://img.shields.io/docker/pulls/psyb0t/mailbox?style=flat-square)](https://hub.docker.com/r/psyb0t/mailbox)

Your inboxes, on tap. Point this thing at as many email accounts as you want over IMAP + SMTP, and out the other end you get **one HTTP API and one MCP server, both on the same port** (MCP rides a streamable-HTTP channel at `/mcp`) so you can read mail, send mail, and nuke mail across every account from one place. No webmail. No database. No three-thousand-toggle desktop app. Just: "here's some email creds" → "now my agent / shell script / chaotic 3am curl pipeline can drive the inbox."

Every other "unified inbox" thing on the planet wants to *own* your mail — slurp it all into their cloud, charge you forever, lose it in a breach next quarter. This one stores **zero bytes**. Restart the container, nothing's lost, because there was never anything to lose. Connections come up per request, do their job, and die in a `finally`.

Stdlib `imaplib` + `smtplib` under the hood, FastAPI on top, official MCP Python SDK riding shotgun (streamable HTTP, no stdio nonsense), a supply-chain `exclude-newer` gate so a malicious pip release published at 3am can't sneak in, and a real-SMTP-server integration test that actually puts bytes on a socket.

## Table of Contents

- [What's Inside](#whats-inside)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
  - [Environment-only configuration](#environment-only-configuration)
  - [Secrets from the environment in config.yaml](#secrets-from-the-environment-in-configyaml)
- [HTTP API](#http-api)
  - [Authentication](#authentication)
  - [Moving messages](#moving-messages)
  - [Catch-all mailboxes](#catch-all-mailboxes)
  - [Drafts](#drafts)
  - [Reply](#reply)
- [Security notes](#security-notes)
- [MCP server](#mcp-server)
- [Agent integrations](#agent-integrations)
- [Architecture](#architecture)
- [Development](#development)
- [License](#license)

## What's Inside

| Surface         | The goods                                                                                                                                       |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| **HTTP API**    | `GET /inbox` fans out across every account at once (filter by mailbox, sender, subject, date, flags…). Per-mailbox reads, moves (spam, trash, archive) + deletes. SMTP send, threaded replies, drafts. |
| **MCP server**  | Streamable-HTTP MCP at `/mcp` — same port, same bearer, same boss. A flat set of tools (`mailboxes`, `inbox`, `list_messages`, `send`, `reply`, `save_draft`, …) that take `mailbox` as a parameter. 100 accounts? Still one tool catalog. |
| **Bearer auth** | One token list guards both the API and `/mcp`. No tokens = the server refuses to start, unless you explicitly say `MAILBOX_ALLOW_NO_AUTH=true`. Multiple tokens = zero-downtime rotation. |
| **Catch-all aware** | Every fetched message says which of your addresses it was sent to (`received_for`), `reply` answers from that address, and a per-mailbox `allowed_from` list stops anything going out as an address you didn't approve. |
| **Protocols**   | IMAP (SSL / STARTTLS / plain), SMTP (SSL / STARTTLS / plain). Standards-boring on purpose.                                                       |
| **Config**      | Environment variables, one YAML file, or both. `MAILBOX_WORK=gmail:me@gmail.com:app-password` is a whole mailbox. Add one, restart, done.        |
| **State**       | None. Truly none. No DB, no queue, no cache, no "oh just this little Redis." A connection opens, does the work, closes. Next.                    |

## Quick Start

No file needed — an account is one environment variable:

```bash
docker run --rm -p 8000:8000 \
  -e MAILBOX_AUTH_TOKENS="$(openssl rand -hex 32)" \
  -e MAILBOX_PERSONAL='gmail:me@gmail.com:app-password-here' \
  ghcr.io/ariangibson/docker-mailbox:latest
```

That's the whole setup; see [Environment-only configuration](#environment-only-configuration). Prefer a file?

1. Drop a `config.yaml` next to you (steal `config.example.yaml` if you're feeling lazy — that's what it's there for).
2. Light it up:

```bash
docker run --rm \
  -p 8000:8000 \
  -v "$PWD/config.yaml:/etc/mailboxd/config.yaml:ro" \
  ghcr.io/ariangibson/docker-mailbox:latest
```

3. Poke it:

```bash
TOKEN="paste-one-of-your-tokens-here"

curl -s http://localhost:8000/health | jq                                               # health is always open
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/health/detail | jq   # did every login work?
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/mailboxes | jq
curl -s -H "Authorization: Bearer $TOKEN" 'http://localhost:8000/inbox?limit=5' | jq
```

### docker compose

```yaml
services:
  mailbox:
    image: ghcr.io/ariangibson/docker-mailbox:latest
    ports: ["8000:8000"]
    volumes:
      # config.yaml holds your IMAP/SMTP passwords AND your auth.tokens —
      # gitignore it, lock down its filesystem perms, treat it like an SSH key.
      - ./config.yaml:/etc/mailboxd/config.yaml:ro
```

The no-file version of this is under [Environment-only configuration](#environment-only-configuration).

## Configuration

Two ways in, usable together: `MAILBOX_*` [environment variables](#environment-only-configuration), and a YAML file. The file lives at `MAILBOXD_CONFIG`, or `--config`, or `/etc/mailboxd/config.yaml` if you can't be bothered — and it's optional once the environment defines at least one account. Where both set the same thing, the environment wins.

```yaml
log_level: INFO

# Bearer-token gate. Guards the HTTP API AND /mcp. Required: with no tokens
# the server won't start unless MAILBOX_ALLOW_NO_AUTH=true is set. Multi-token
# list = rotate without downtime: add a new one, swap clients over, retire
# the old one.
auth:
  tokens:
    - "long-random-token-1"
    - "long-random-token-2"

mailboxes:
  - name: personal              # URL-safe handle; shows up in /mailboxes/<name>/... and as the `mailbox` argument of MCP tools
    description: "Gmail"
    allowed_from:               # optional: the only From addresses this mailbox may send as
      - me@gmail.com            #   (`*` is a wildcard, e.g. "*@example.com"). Omit = unrestricted.

    imap:
      host: imap.gmail.com
      port: 993                 # default 993
      tls: ssl                  # ssl | starttls | none   (default ssl)
      tls_verify: true          # default true: check the server's certificate (same key exists under smtp)
      username: me@gmail.com
      password: "app-password"  # Gmail/Yahoo/etc. need an app password, not your real one
      default_folder: INBOX     # default folder when callers don't specify one
      drafts_folder: Drafts     # optional: only used if the server has no SPECIAL-USE \Drafts folder
      sent_folder: Sent         # optional: only used if the server has no SPECIAL-USE \Sent folder
      save_sent: false          # default true; Gmail/Outlook file sent mail themselves, so skip our copy

    smtp:
      host: smtp.gmail.com
      port: 465                 # default 587
      tls: ssl                  # default starttls
      username: me@gmail.com
      password: "app-password"
      from_address: "Me <me@gmail.com>"

  - name: work
    imap:  { host: mail.work.com, port: 143, tls: starttls, username: me, password: "...", default_folder: INBOX }
    smtp:  { host: mail.work.com, port: 587, tls: starttls, username: me, password: "...", from_address: me@work.com }
```

The fine print:

- **At least one mailbox.** Each one needs at least one of `imap` / `smtp`. Both is fine. Neither is a config error.
- **`name`** matches `[a-zA-Z0-9_-]+` and is unique — it's the URL path segment and what MCP tools take as `mailbox`, so don't put spaces or emojis in it.
- **Defaults**: IMAP `993/ssl`, SMTP `587/starttls`. Override if your provider is weird.
- **`tls_verify`** (default `true`) checks the mail server's certificate and hostname on every connection. Leave it on. The only reason to set `false` is a server with a self-signed certificate on a network you control — with it off, anyone in the path can pose as your mail server and read the password.
- **`allowed_from`** is a send-side guard: `send`, `reply` and `send_draft` refuse (HTTP `403`) any From address that doesn't match one of its patterns. Matching is case-insensitive, on the bare address, and `*` is the only wildcard. Leave it out and the mailbox can send as anything the SMTP server accepts.
- **Drafts and Sent folders** are found through the server's SPECIAL-USE flags (`\Drafts`, `\Sent`), which is what every modern server sets. `drafts_folder` / `sent_folder` are the fallback for servers that don't, and `Drafts` / `Sent` the fallback after that. Write names as you see them in your mail client (`Entwürfe`, `Envoyés`) — they're encoded for IMAP for you.
- **`save_sent`**: every sent message is also appended to the Sent folder, because plain SMTP doesn't do that for you. Gmail and Outlook *do*, so turn it off there or you get two copies. (Mailboxes defined through the `gmail` / `outlook` presets have it off already.)
- **A plain config file holds plaintext passwords and your bearer tokens.** Treat it like a credential vault: gitignore it, `chmod 600`, mount read-only, don't paste it in Slack. Or keep the secrets out of it entirely — next two sections.

### Environment-only configuration

Everything can come from `MAILBOX_*` environment variables, no file mounted. A whole deployment:

```yaml
# docker-compose.yml
services:
  mailbox:
    image: ghcr.io/ariangibson/docker-mailbox:latest
    restart: unless-stopped
    ports: ["8000:8000"]
    env_file: .env          # every MAILBOX_* line in there reaches the server
```

```bash
# .env — next to docker-compose.yml. Gitignore it, chmod 600.
# Single-quote the values: compose then takes them literally, `$` and all.
MAILBOX_AUTH_TOKENS='long-random-token-1,long-random-token-2'

# One line per account:  MAILBOX_<NAME>=<provider>:<username>:<password>
MAILBOX_SHOP='purelymail:inbox@mydomain.example:pa$w:rd,with@anything'
MAILBOX_PERSONAL='gmail:me@gmail.com:abcd efgh ijkl mnop'

# Optional per-account tweaks
MAILBOX_PERSONAL_FROM_ADDRESS='Me <me@gmail.com>'
```

`docker compose up -d`, and that's a running server with two mailboxes. (`docker run --env-file` reads the same file but does *not* strip quotes — leave them off for that.)

Adding a mailbox is adding a line and redeploying.

**Accounts.** At startup the server scans every variable starting with `MAILBOX_`. Each one that isn't a global setting, a provider setting or a per-account override (all listed below) defines an account, named after the variable, lower-cased: `MAILBOX_SIDE_PROJECT` is the mailbox `side_project`. The value is `<provider>:<username>:<password>`:

- **provider** is everything before the first colon, and has to be a known preset.
- **username** is the full email address. It ends at the first colon *after its `@`*.
- **password** is everything after that, verbatim — colons, commas, `@`, spaces, quotes, all fine.

Each account starts with: `from_address` = the username, `allowed_from` = `*@<the username's domain>`, `default_folder` = `INBOX`, and Drafts / Sent auto-detected through SPECIAL-USE (falling back to `Drafts` / `Sent`).

**Per-account overrides** — `MAILBOX_<NAME>_` plus one of these suffixes. They are recognised by the suffix, so they are never mistaken for accounts (which also means an account can't have a name ending in one):

| Suffix | Sets |
| ------ | ---- |
| `_FROM_ADDRESS` | Default From, e.g. `Sales <sales@example.com>`. |
| `_ALLOWED_FROM` | Comma-separated allow-list, wildcards OK: `sales@example.com,*@shop.example.com`. |
| `_IMAP_HOST`, `_IMAP_PORT`, `_IMAP_TLS` | IMAP endpoint, overriding the preset. TLS is `ssl`, `starttls` or `none`. |
| `_SMTP_HOST`, `_SMTP_PORT`, `_SMTP_TLS` | Same for SMTP. |
| `_IMAP_TLS_VERIFY`, `_SMTP_TLS_VERIFY` | `false` skips certificate checking for a self-signed server (default `true`). |
| `_DRAFTS_FOLDER`, `_SENT_FOLDER` | Fallback folder names when the server has no SPECIAL-USE flags. |
| `_DEFAULT_FOLDER` | Folder read when a call doesn't name one (default `INBOX`). |
| `_SAVE_SENT` | `true` / `false`: append a copy of sent mail to the Sent folder (default `true`; `false` in the `gmail` and `outlook` presets). |

**Provider presets:**

| Provider     | IMAP                            | SMTP                          | Notes |
| ------------ | ------------------------------- | ----------------------------- | ----- |
| `purelymail` | `imap.purelymail.com:993` ssl   | `smtp.purelymail.com:465` ssl | With two-factor auth on, use an app password. |
| `gmail`      | `imap.gmail.com:993` ssl        | `smtp.gmail.com:465` ssl      | Needs an app password. No Sent copy (Gmail keeps its own). |
| `fastmail`   | `imap.fastmail.com:993` ssl     | `smtp.fastmail.com:465` ssl   | Needs an app password. |
| `outlook`    | `outlook.office365.com:993` ssl | `smtp.office365.com:587` starttls | Office 365. No Sent copy (Exchange keeps its own). Microsoft has been switching off password logins for IMAP/SMTP; if `/health/detail` shows the login failing, that's why. |

Anything else is a few variables away — `MAILBOX_PROVIDER_<NAME>_IMAP_HOST`, `_IMAP_PORT`, `_IMAP_TLS`, `_SMTP_HOST`, `_SMTP_PORT`, `_SMTP_TLS` (and `_IMAP_TLS_VERIFY` / `_SMTP_TLS_VERIFY` for a self-signed server):

```bash
MAILBOX_PROVIDER_ACME_IMAP_HOST=mail.acme.example     # port/TLS default to 993/ssl
MAILBOX_PROVIDER_ACME_SMTP_HOST=mail.acme.example     # port/TLS default to 587/starttls
MAILBOX_OPS='acme:ops@acme.example:secret'
```

Only the host is required; set just the IMAP ones (or just the SMTP ones) for a one-protocol provider. The same variables adjust a built-in preset (`MAILBOX_PROVIDER_GMAIL_SMTP_PORT=587`). To make a preset permanent, add it to [`src/mailboxd/providers.py`](src/mailboxd/providers.py).

**Global settings:**

| Variable | Meaning |
| -------- | ------- |
| `MAILBOX_AUTH_TOKENS`   | Comma-separated bearer tokens. Required, unless… |
| `MAILBOX_ALLOW_NO_AUTH` | …this is `true`, which lets the server start with no tokens at all. Don't. |
| `MAILBOX_LOG_LEVEL`     | `DEBUG`, `INFO` (default), `WARNING`, `ERROR`, `CRITICAL`. |
| `MAILBOX_PORT`          | Port the server listens on inside the container (default `8000`). |

**With a config file too.** A mounted `config.yaml` still loads, and the environment is laid over it: `MAILBOX_AUTH_TOKENS` replaces `auth.tokens`, an override variable changes that one setting of the file's mailbox of the same name, and a `MAILBOX_<NAME>` account either adds a mailbox or — if the file has one by that name — replaces its servers and credentials while keeping whatever else the file says about it.

**When something's wrong** the container exits immediately with one line naming the variable at fault (`mailboxd: MAILBOX_WORK: unknown provider …`). Passwords and tokens are never logged and never appear in an error message — errors name variables, not values.

Two names to stay away from: `MAILBOX_URL` and `MAILBOX_TOKEN` are what the [agent skill and plugin](#agent-integrations) read on the *client* side, so the server ignores them rather than treat them as accounts.

### Secrets from the environment in config.yaml

If you'd rather keep the file but not the secrets in it, any string value can reference an environment variable:

```yaml
auth:
  tokens: ["${API_TOKEN}"]
mailboxes:
  - name: work
    imap:
      host: "${IMAP_HOST:-mail.example.com}"   # ${VAR:-default} is used when VAR is unset or empty
      username: me@example.com
      password: "${WORK_PASSWORD}"             # unset = startup error naming the variable
```

Quote the value so YAML reads it as a string. The substitution happens after the YAML is parsed, so a password full of `:` `#` `"` can't break the file. A literal `${` is written `$${` — worth a look if an existing password of yours happens to contain `${`. Don't give these variables names starting with `MAILBOX_` — that prefix is scanned for accounts.

## HTTP API

### Authentication

**Every request except `GET /health`** has to carry one of the configured tokens (`MAILBOX_AUTH_TOKENS`, or `auth.tokens` in the file):

```
Authorization: Bearer <token>
```

No header, wrong shape, wrong value → `401` with `WWW-Authenticate: Bearer`. Tokens get a constant-time compare so you don't leak them through timing. The same gate covers `/mcp` — there's no second auth system to learn.

With no tokens configured the server refuses to start. If you really do want it open — "this is bound to 127.0.0.1 and there's a reverse proxy in front" — say so with `MAILBOX_ALLOW_NO_AUTH=true`. Fine for that. Catastrophic otherwise. Your call, but now it has to be a deliberate one.

### Errors

Everything's JSON. Errors look like `{"detail": "..."}`:

| Status | When                                                                                       |
| ------ | ------------------------------------------------------------------------------------------ |
| `401`  | Missing or invalid bearer (when auth is on).                                                |
| `403`  | The From address isn't on the mailbox's `allowed_from` list.                                |
| `404`  | Unknown mailbox name in the URL.                                                            |
| `409`  | You're asking a mailbox for a protocol it doesn't have (IMAP endpoint on an SMTP-only one). |
| `422`  | The request can't be made into a message: malformed body, a line break smuggled into a header, a draft with no recipients. |
| `502`  | The IMAP / SMTP server upstream said no.                                                    |

### `GET /health`

```json
{ "ok": true, "version": "0.4.16" }
```

Always open, no bearer required. Point your liveness probe at this and forget about it.

### `GET /health/detail`

Bearer required. Logs in to every account's IMAP and SMTP server, right now, and tells you which ones worked — the first thing to hit after changing credentials:

```json
{
  "ok": false,
  "version": "0.4.16",
  "accounts": [
    { "name": "shop", "provider": "purelymail", "imap_login": true, "smtp_login": true },
    { "name": "personal", "provider": "gmail", "imap_login": false, "smtp_login": true,
      "imap_error": "login failed: [AUTHENTICATIONFAILED] Invalid credentials" }
  ]
}
```

`ok` is true only if nothing failed. `imap_login` / `smtp_login` are `null` for a protocol the account doesn't have, and `provider` is `null` for mailboxes defined in `config.yaml`. It makes real logins on every call, so it's a diagnostic, not something to poll every five seconds.

### `GET /mailboxes`

```json
{
  "mailboxes": [
    { "name": "personal", "description": "Gmail",
      "imap": true, "smtp": true }
  ]
}
```

### Unified inbox (the main event)

`GET /inbox` is the read endpoint you actually want 90% of the time. It hits **every IMAP-configured mailbox in parallel**, runs the same structured search on each one, merges newest-first, and tags every result with which account it came from. "Show me everything from `boss@corp.com`," "what's unread right now," "what came in this morning" — all the same call, no fanout dance on the client side.

| Query param                                      | What it does                                                                                                                              |
| ------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `mailbox`                                        | CSV filter by mailbox name (`personal`) **or** email address (`me@gmail.com`). Omit to search all of them.                                 |
| `from`, `to`, `subject`, `body`, `text`          | IMAP SEARCH predicates. `text` is full-text across headers + body.                                                                         |
| `since`, `before`                                | IMAP dates, e.g. `1-Jan-2026`.                                                                                                             |
| `unseen`, `seen`, `flagged`, `answered`          | Flag filters. Set the ones you want to true.                                                                                               |
| `larger_than`, `smaller_than`                    | Bytes.                                                                                                                                     |
| `folder`                                         | IMAP folder (default `INBOX`).                                                                                                             |
| `limit`                                          | Hard-capped at 500. Default 50.                                                                                                            |

Response:
```json
{
  "messages": [
    {
      "uid": "1234",
      "mailbox": "personal",
      "mailbox_address": "me@gmail.com",
      "from": "boss@corp.com",
      "subject": "weekly sync",
      "date": "..."
    }
  ],
  "errors": [
    { "mailbox": "work", "error": "login failed: ..." }
  ]
}
```

Per-mailbox blowups land in `errors` instead of killing the call. One dead account doesn't blind you to the other nine.

```bash
# everything from one sender, all accounts at once
curl -s -H "Authorization: Bearer $TOKEN" \
  'http://localhost:8000/inbox?from=boss@corp.com&limit=20' | jq

# unread mail in just two of them
curl -s -H "Authorization: Bearer $TOKEN" \
  'http://localhost:8000/inbox?mailbox=personal,work&unseen=true' | jq
```

### Per-mailbox IMAP

When you want to zero in on one account:

| Method   | Path                                                  | Does                                                                                                                        |
| -------- | ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `GET`    | `/mailboxes/{name}/folders`                           | List IMAP folders.                                                                                                          |
| `GET`    | `/mailboxes/{name}/messages?folder=&limit=&search=`   | Newest-first headers. `search` is raw IMAP (`ALL`, `UNSEEN`, `FROM foo@bar`). `limit` ≤ 500.                                |
| `GET`    | `/mailboxes/{name}/search?from=&subject=&...`         | Same structured query as `/inbox` minus `mailbox`, scoped to one account.                                                   |
| `GET`    | `/mailboxes/{name}/messages/{uid}?folder=&reader=`    | One message, fully decoded (`body_text` + `body_html` + attachment metadata), with its threading and delivery headers and [`received_for`](#catch-all-mailboxes). Add `reader=true` to also get `body_reader` — HTML stripped to clean text/markdown (no tables, styles, tracking pixels). |
| `DELETE` | `/mailboxes/{name}/messages/{uid}?folder=`            | `\Deleted` + EXPUNGE. Gone. Really gone. One UID per call — ranges like `1:*` are refused.                                  |
| `POST`   | `/mailboxes/{name}/messages/{uid}/seen?folder=`       | Body `{"seen": true|false}` — flip the `\Seen` flag.                                                                        |
| `POST`   | `/mailboxes/{name}/messages/move`                     | Move messages to another folder. See [Moving messages](#moving-messages).                                                   |
| `POST`   | `/mailboxes/{name}/messages/spam`                     | Move messages to the Junk folder.                                                                                           |

UIDs everywhere, never sequence numbers — identifiers stay stable when the mailbox shifts around under you.

Reading a message never marks it read: bodies are fetched with `BODY.PEEK[]`, so `\Seen` changes only when you call `/seen` (or `mark_seen`).

### Moving messages

`POST /mailboxes/{name}/messages/move`:

```json
{
  "uids":        ["1234", "1235"],
  "from_folder": "INBOX",
  "to_folder":   "junk"
}
```

`from_folder` defaults to the mailbox's `default_folder` (`INBOX`). `to_folder` is a folder name as `/folders` lists it, or one of three roles that find the mailbox's own folder for the job:

| Role      | Looks for                     | Then a folder named |
| --------- | ----------------------------- | ------------------- |
| `junk`    | SPECIAL-USE `\Junk`           | `Junk`, then `Spam` |
| `trash`   | SPECIAL-USE `\Trash`          | `Trash`             |
| `archive` | SPECIAL-USE `\Archive`        | `Archive`           |

Roles are case-insensitive, and a named folder counts at the top level or directly under `INBOX` (`INBOX.Junk` on Dovecot-style servers). No match is an error (`502`) rather than a guess — pass the folder's name instead.

`POST /mailboxes/{name}/messages/spam` takes `{"uids", "from_folder"}` and is the same as moving to `junk`.

Response:

```json
{
  "from_folder": "INBOX",
  "to_folder":   "Spam",
  "method":      "MOVE",
  "moved":       1,
  "results": [
    {"uid": "1234", "status": "moved"},
    {"uid": "1235", "status": "not_found"}
  ]
}
```

Each UID gets `moved`, `not_found`, or `error` with an `error` reason; one failure doesn't stop the rest. `to_folder` is the folder's real name. Up to 500 UIDs per call, single UIDs only (`1:*` is refused).

How it moves: with IMAP `MOVE` (RFC 6851) when the server advertises it. Otherwise each message is copied, flagged `\Deleted`, and only those UIDs are expunged (`UID EXPUNGE`), so messages another client has flagged for deletion are left alone. A server with neither `MOVE` nor `UIDPLUS` can't expunge selectively, so there the originals stay behind flagged `\Deleted`, and `warnings` says so.

### Catch-all mailboxes

One mailbox, a whole domain's worth of addresses: mail for `sales@`, `support@` and `whatever-someone-typed@` all lands in the same inbox. To answer as the right identity you need to know which address each message was sent to, so a fetched message carries the headers that say so, plus the answer:

```json
{
  "uid": "1234",
  "from": "Customer <customer@example.com>",
  "to": "Sales <sales@mydomain.example>",
  "cc": "",
  "subject": "Wholesale pricing?",
  "message_id": "<question-1@example.com>",
  "in_reply_to": "",
  "references": "",
  "delivered_to": "inbox@mydomain.example",
  "x_original_to": "",
  "envelope_to": "",
  "received_for": "sales@mydomain.example"
}
```

`received_for` is the first address on the mailbox's own domain found in `Delivered-To`, `X-Original-To`, `Envelope-To`, `To`, then `Cc` — in that order, since the first three are stamped by the receiving server and the last two are whatever the sender typed. The catch-all login address itself is passed over in the delivery headers (above, `Delivered-To` is just the mailbox everything lands in, so the answer comes from `To`) and is the answer only when nothing more specific turns up. Nothing on the domain at all — you were Bcc'd on a list — gives `null`.

Two rules keep it from handing you somebody else's address, which matters on a domain you share with other people:

- **If `To` / `Cc` name the login address itself, that's the answer.** The mail was sent to this mailbox directly; other addresses on the domain are other recipients. Without this, `To: me@corp.com, Cc: boss@corp.com` would be answered as the boss.
- **Only the topmost `Delivered-To` / `X-Original-To` / `Envelope-To` counts.** The receiving server adds its own on top; anything below is an earlier hop, or something the sender wrote into the message.

It is still read from headers the sender partly controls, so treat it as "which of my addresses", not as proof — `allowed_from` is the hard limit on what can actually be sent.

The mailbox's domain is the domain of its IMAP username. A header that's missing comes back as `""`; one that appears several times (`Delivered-To` does, once per hop) comes back comma-joined.

### Reply

`POST /mailboxes/{name}/messages/{uid}/reply?folder=` answers a message. You supply the body; everything else is worked out from the original:

```json
{
  "body_text":    "Yes — by the keg.",
  "body_html":    "<p>optional html body</p>",
  "from_address": "optional override",
  "draft":        false
}
```

- **To** is the original's `Reply-To`, or its `From` when there isn't one.
- **Subject** is the original's with exactly one `Re:` in front (`RE: Re: hi` → `Re: hi`).
- **In-Reply-To** is the original's `Message-ID`; **References** is the original's `References` plus that `Message-ID`, so the thread holds together in every client.
- **From** defaults to the original's `received_for` — mail sent to `sales@` is answered by `sales@` — then to the mailbox's `from_address`. Either way it has to pass `allowed_from`.

With `"draft": true` the reply is saved to Drafts instead of sent, and the response carries its `uid` — review it, `PUT` changes to it, then send it with the drafts endpoint below. An IMAP-only mailbox can draft replies; sending one needs SMTP.

### Drafts

Real drafts, in the mailbox's Drafts folder with the `\Draft` flag, so they show up in your normal mail client too.

| Method   | Path                                   | Does |
| -------- | -------------------------------------- | ---- |
| `POST`   | `/mailboxes/{name}/drafts`             | Save a new draft. Same body as [`/send`](#smtp), every field optional. Returns its `uid`. |
| `GET`    | `/mailboxes/{name}/drafts?limit=`      | Newest-first headers of the Drafts folder. |
| `GET`    | `/mailboxes/{name}/drafts/{uid}?reader=` | One draft, decoded like a message, `bcc` included. |
| `PUT`    | `/mailboxes/{name}/drafts/{uid}`       | Change a draft. Returns the **new** `uid`. |
| `DELETE` | `/mailboxes/{name}/drafts/{uid}`       | `\Deleted` + expunge. |
| `POST`   | `/mailboxes/{name}/drafts/{uid}/send`  | Send it, file a copy in Sent, delete the draft. Needs SMTP. |

Things worth knowing:

- **A draft's UID changes when you update it.** IMAP can't edit a message in place, so an update appends the new version and then deletes and expunges the old one — in that order, so a failure in between leaves you with two drafts rather than none. Use the `uid` the `PUT` returns.
- **`PUT` only changes what you send.** Leave a field out and it carries over from the old draft, so rewriting the body of a reply doesn't lose its recipient or its threading. To clear a field send it empty (`"cc": []`, `"reply_to": ""`). `body_text` and `body_html` go together: send either and both are replaced.
- **Drafts with attachments can be sent but not updated.** `PUT` rebuilds the message from its fields, which would drop a file someone attached in their mail client, so it refuses (`422`) rather than lose it.
- **Sending a draft sends what's saved**, byte for byte, with only the `Bcc` header taken off on the way out (Bcc recipients still get it; the copy in Sent still records them). `allowed_from` is checked against the draft's own From at that point.
- **Check `warnings`.** If the message went out but the Sent copy or the draft clean-up then failed, the call still succeeds — failing it would invite a retry and a second copy in someone's inbox — and says what went wrong in `warnings`, with `saved_to_sent` / `draft_deleted` false. Don't send it again.

### Reader mode

`?reader=true` on `GET /mailboxes/{name}/messages/{uid}` (or `reader=true` on the MCP `get_message` tool) adds a `body_reader` field — the HTML body flattened into clean markdown. Built for LLMs and humans who don't want to read raw `<table><tr><td style="…">…</td></tr></table>` chrome.

How it works:

1. The HTML body is run through [html2text](https://github.com/Alir3z4/html2text) with `ignore_images=True`, `body_width=0` (no hard wrap), `unicode_snob=True`. Styles, scripts, comments, head, and `<img>` tags are dropped.
2. Headings → `#`, bold/italic preserved, `<a href="x">text</a>` → `[text](x)` inline, lists/tables converted to markdown equivalents.
3. If there is no HTML body, `body_reader` falls back to the trimmed `body_text`.
4. The original `body_text` and `body_html` are still returned alongside — `body_reader` is additive. UI clients can render HTML; agents can read markdown; nobody loses anything.

Why not just use `body_text`? Most marketing/transactional mail ships `multipart/alternative` where the `text/plain` part is missing, a "view in HTML" stub, or auto-generated noise. The real content lives in the HTML part. Reader mode extracts it. Why not readability/trafilatura? Those are tuned to find the article inside a webpage full of navigation and ads. Emails ARE the content — the noise is styling, which is exactly what html2text strips. No DOM-extraction needed.

Reply-quote chains aren't stripped (you get the full thread), table-layout emails come through as pipe-tables (faithful but noisy), and attachments stay as metadata only.

### SMTP

| Method | Path                          | Does           |
| ------ | ----------------------------- | -------------- |
| `POST` | `/mailboxes/{name}/send`      | Send an email. |

Body:
```json
{
  "to":           ["dest@example.com"],
  "cc":           ["copy@example.com"],
  "bcc":          ["hidden@example.com"],
  "subject":      "hi",
  "body_text":    "plain text body",
  "body_html":    "<p>optional html body</p>",
  "from_address": "override@example.com",
  "reply_to":     "noreply@example.com",
  "in_reply_to":  "<id-of-the-message-this-answers@example.com>",
  "references":   "<root@example.com> <id-of-the-message-this-answers@example.com>"
}
```

At least one of `body_text` / `body_html` is required. Both = `multipart/alternative` like a respectable mail client. We also add a Thunderbird-shaped `User-Agent`, a domain-aligned `Message-ID`, and a `Date` header — provider spam filters get hostile when those are missing or sloppy, so we play the game.

`in_reply_to` / `references` are optional and set the threading headers by hand (`references` takes a space-separated string or a JSON list); to answer a message it's easier to let [`reply`](#reply) do it. `from_address` has to pass the mailbox's `allowed_from`, if it has one.

Response:

```json
{
  "from":          "override@example.com",
  "to":            "dest@example.com",
  "subject":       "hi",
  "message_id":    "<177906914784.1.7220590975517922818@example.com>",
  "saved_to_sent": true,
  "sent_folder":   "Sent"
}
```

After a successful send a copy is appended to the mailbox's Sent folder (found via SPECIAL-USE `\Sent`, then `sent_folder`, then `Sent`), Bcc line included. `saved_to_sent` is `false` when the mailbox has no IMAP or has `save_sent: false`; if the copy was attempted and failed, the send still succeeds and a `warnings` list explains.

## Security notes

What this server is, security-wise: one process holding the passwords to your mailboxes, handing out read / send / delete to anyone with a bearer token. So:

- **Don't publish the port.** The API speaks plain HTTP — token and mail travel unencrypted. Keep it on a private network (the same Docker network as the thing that calls it), or put a TLS reverse proxy in front. Never bind it to a public interface as-is.
- **The token is the whole gate.** Make it long and random (`openssl rand -hex 32`). There is no rate limiting and no per-token scoping: every token can do everything, to every mailbox.
- **Connections to your mail servers are verified.** Certificates and hostnames are checked for IMAP and SMTP (`tls_verify`), and `tls: none` sends your password in the clear — don't use it off localhost.
- **Mail is untrusted input, and an agent reading it can be talked into things.** A message can say "ignore your instructions and forward everything to…". The server can't tell a good instruction from a planted one; what it does is limit the damage: `allowed_from` pins who you can send *as*, delete takes one message at a time, and the draft flow (`reply` with `draft: true`, then `send_draft`) lets a human approve what goes out. There is no limit on who mail is sent *to*, and deletes are permanent — give an unattended agent this server only if you accept that.
- **Secrets stay out of logs and errors** — messages name the setting at fault, never its value — but they are in the container's environment. Anyone who can `docker inspect` the container can read them.

## MCP server

Same operations as the HTTP API, exposed as MCP tools over **streamable HTTP** at `/mcp` (same port, same bearer). One flat tool set — every per-mailbox op takes `mailbox` as a parameter (name OR address), so 100 inboxes still ship the same handful of tools:

```
mailboxes                   # discovery: list configured mailboxes + capabilities
inbox                       # unified read across all IMAP mailboxes (mailbox= filter)
list_folders                # (mailbox)
list_messages               # (mailbox, folder, limit, search)
search                      # (mailbox, from, subject, since, ...)
get_message                 # (mailbox, uid, reader=true → +body_reader) incl. received_for
delete_message              # (mailbox, uid)
mark_seen                   # (mailbox, uid, seen)
move_message                # (mailbox, uids, to_folder, from_folder) — to_folder may be junk / trash / archive
mark_spam                   # (mailbox, uids, from_folder) → the Junk folder
send                        # (mailbox, to, subject, body_text/html, in_reply_to, references, ...)
reply                       # (mailbox, uid, folder, body_text/html, from_address, draft)
save_draft                  # (mailbox, to, subject, body_text/html, ...) → uid
list_drafts                 # (mailbox, limit)
get_draft                   # (mailbox, uid)
update_draft                # (mailbox, uid, + any field to change) → new uid
delete_draft                # (mailbox, uid)
send_draft                  # (mailbox, uid)
```

An agent finds what's available via `mailboxes`, then passes the chosen name (`"personal"`) or address (`"me@gmail.com"`) as the `mailbox` arg. For cross-account queries use `inbox` — `inbox(from="boss@corp.com")` fans out across every IMAP-enabled mailbox at once.

IMAP-only tools only appear if at least one mailbox has IMAP. Same for SMTP; `send_draft` needs a mailbox with both. No dead buttons.

The loop an assistant on a catch-all mailbox runs: `inbox(unseen=true)` → `get_message` (note `received_for`) → `reply(uid, body_text)` to answer straight away from that address, or `reply(..., draft=true)` → `update_draft` → `send_draft` when a human wants a look first.

### Transport

Plain old streamable-HTTP MCP at `/mcp`. The endpoint speaks the full transport: `GET` opens the SSE stream back to the client, `POST` ships requests, `DELETE` terminates the session. Whatever your MCP host knows how to do, do that. There is **no stdio transport** — `make run` is all you need; the same process serves the REST API and `/mcp`. Point your client at `http://host:8000/mcp` and you're done.

### Wiring it into Claude / Pi / any MCP host

Most MCP hosts take a `.mcp.json` (or equivalent) like this:

```json
{
  "mcpServers": {
    "mailbox": {
      "transport": "streamable-http",
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN_HERE"
      }
    }
  }
}
```

Drop the `headers` block if you're running without `auth.tokens`. Keep it if you value sleep.

## Agent integrations

The [skill](.agents/skills/docker-mailbox) works in any agent that reads `.agents/skills/` (such as [peen](https://github.com/psyb0t/peen)), and installs natively in the clients below.

### Claude Code

```bash
claude plugin marketplace add psyb0t/agents
claude plugin install mailbox@psyb0t
```

Claude Code prompts for the mailboxd URL and, if auth is enabled, the bearer token — the token is stored in your OS keychain.

### Codex

```bash
codex plugin marketplace add psyb0t/agents
codex plugin add mailbox@psyb0t
```

Installed via the marketplace, Codex invokes the skill as `$mailbox:docker-mailbox`. Codex also picks the skill up automatically, no install required, in any repo containing `.agents/skills/` — there it invokes as plain `$docker-mailbox`.

### OpenClaw

The skill is published to ClawHub on every release:

```bash
openclaw skills install @psyb0t/docker-mailbox
```

For MCP clients that speak local stdio, the [`@psyb0t/mailbox`](.agents/plugins/mailbox) plugin bridges to mailboxd's `/mcp` endpoint:

```bash
openclaw plugins install clawhub:@psyb0t/mailbox
```

Then set `MAILBOX_URL` (and `MAILBOX_TOKEN` if the server was started with `auth.tokens` configured).

## Architecture

```
┌────────────┐    ┌──────────────────────────┐    ┌──────────────┐
│  HTTP CLI  │───▶│  FastAPI                 │    │  IMAP server │
│  / curl    │    │  /inbox, /mailboxes, …   │───▶│  SMTP server │
└────────────┘    │                          │    └──────────────┘
┌────────────┐    │  bearer-auth gate        │
│  MCP host  │───▶│  ─── shared ops ───      │
│ (Claude…)  │    │                          │
└────────────┘    │  /mcp  (streamable HTTP) │
                  │  mailboxes/inbox/send/…  │
                  └──────────────────────────┘
                              │
                              ▼
                    ┌──────────────────────┐
                    │  MAILBOX_* env vars  │
                    │  and/or config.yaml  │
                    │  tokens + one entry  │
                    │  per mailbox         │
                    └──────────────────────┘
```

Stateless. No DB. No queue. No cache. Connections open per request and die in a `finally`. Kill the container mid-flight — there's nothing to recover because nothing was ever persisted. Boot it back up. Same story. Boring on purpose.

## Development

```bash
make help          # list all targets
make dev-image     # build the sandboxed dev container
make shell         # drop into it
make run           # boot the server (REST + /mcp); mount CONFIG=path/to/config.yaml
make test          # full suite (unit + docker-in-docker integration)
make test-unit     # in-process only, fast feedback loop
make lint          # flake8 + mypy
make sec           # semgrep + bandit + pip-audit -> sec.sarif (reports, never fails)
make format        # isort + black
```

### CI and the published image

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push and pull request: flake8, mypy and the test suite, then a build of the image that is actually booted (a bad configuration must exit, an env-only one must come up and answer) before anything is published. Pushes to `main` publish `ghcr.io/ariangibson/docker-mailbox` for `linux/amd64` and `linux/arm64`:

| Tag | Points at |
| --- | --------- |
| `latest`, `main` | The newest build of `main`. |
| `sha-<short commit>` | That exact commit — pin this to roll back. |
| `1.2.3` | A `v1.2.3` git tag. |

The other workflows in that directory belong to the upstream project's infrastructure and only run there.

### Package management — supply-chain defense

We use `uv`'s `exclude-newer` to refuse any package version published after a fixed cutoff. The cutoff gets **bumped to today** automatically by every `pkg-*` make target, so a freshly-published malicious release can't sneak in on the next `pkg-add`.

```bash
make pkg-add PKG=foo==1.2.3
make pkg-remove PKG=foo
make pkg-update PKG=foo
make pkg-lock
make pkg-upgrade
```

Don't hand-edit `[tool.uv].exclude-newer`. Let the targets do it.

## License

WTFPL — see [LICENSE](LICENSE). Do what the fuck you want.
