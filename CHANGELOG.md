# Changelog

## Unreleased

Drafts, threaded replies, catch-all address handling, a sender allow-list,
moving messages between folders, and configuration from environment variables.

**Behavior changes:**

- The server no longer starts without bearer tokens. A config with an empty
  or missing `auth.tokens` used to run unauthenticated; it now exits with an
  error unless `MAILBOX_ALLOW_NO_AUTH=true` is set.
- `${NAME}` inside a `config.yaml` string is now an environment-variable
  reference. A value that contains those characters literally (a password,
  say) must write them as `$${NAME}`.
- TLS certificates are now verified (see Security below). A mail server with
  a self-signed certificate needs `tls_verify: false`.
- `delete_message` takes one UID. It used to accept IMAP sequence-sets, so a
  single call with `1:*` permanently emptied a folder.

**Security:**

- **IMAP and SMTP connections did not verify the server's TLS certificate.**
  `imaplib` and `smtplib` default to an unverified context, and nothing here
  passed one, so anyone on the network path could impersonate the mail server
  and capture the password and the mail. Connections now use a verifying
  context; `tls_verify: false` (or `MAILBOX_<NAME>_IMAP_TLS_VERIFY` /
  `_SMTP_TLS_VERIFY`) opts out per mailbox.
- A bearer header containing non-ASCII bytes raised inside the auth check and
  returned `500`; it now returns `401` like any other bad token.

- **Drafts.** `save_draft`, `list_drafts`, `get_draft`, `update_draft`,
  `delete_draft` and `send_draft`, as MCP tools and under
  `/mailboxes/{name}/drafts`. Drafts are appended to the folder flagged
  SPECIAL-USE `\Drafts` (then `imap.drafts_folder`, then `Drafts`) with the
  `\Draft` flag; the new UID comes from `APPENDUID`, or a Message-ID search on
  servers without UIDPLUS. An update appends the new version before deleting
  and expunging the old UID. `send_draft` sends the saved bytes minus `Bcc`.
- **Threading.** `send` accepts `in_reply_to` and `references`. New `reply`
  tool / `POST /mailboxes/{name}/messages/{uid}/reply` derives recipient
  (`Reply-To`, else `From`), a single `Re:` subject, `In-Reply-To` and
  `References` from the original, and can save the reply as a draft.
  Message-IDs longer than 78 characters are no longer RFC 2047-encoded in
  those headers (a stdlib folding quirk that breaks threading).
- **Sent copies.** Every sent message is appended to the folder flagged
  SPECIAL-USE `\Sent` (then `imap.sent_folder`, then `Sent`). Opt out per
  mailbox with `imap.save_sent: false`. A failed copy is reported in
  `warnings` rather than failing a send that already went out.
- **Catch-all mailboxes.** `get_message` also returns `delivered_to`,
  `x_original_to`, `envelope_to`, `in_reply_to`, `references`, `bcc`,
  `reply_to`, and a computed `received_for`: the first address on the
  mailbox's domain in Delivered-To, X-Original-To, Envelope-To, To, Cc. The
  login address is passed over in the delivery headers when something more
  specific follows, but wins when To / Cc name it directly, and only the
  topmost delivery header is trusted — so a shared domain doesn't get a
  colleague's address. `reply` sends from it by default.
- **`allowed_from`.** Optional per-mailbox list of permitted From addresses
  (`*` wildcard). `send`, `reply` and `send_draft` refuse anything else with
  `403`.
- **Environment configuration.** `config.yaml` is optional. Each
  `MAILBOX_<NAME>=<provider>:<username>:<password>` variable defines a mailbox
  from a provider preset (`purelymail`, `gmail`, `fastmail`, `outlook`, or a
  custom `MAILBOX_PROVIDER_<NAME>_*` one), with `MAILBOX_<NAME>_<SETTING>`
  overrides and the globals `MAILBOX_AUTH_TOKENS`, `MAILBOX_ALLOW_NO_AUTH`,
  `MAILBOX_LOG_LEVEL` and `MAILBOX_PORT`. Environment values override the
  file. String values in `config.yaml` may reference `${VAR}`.
- **Startup.** The container now starts through `mailboxd http`, which
  validates the configuration first and exits with a one-line error naming
  the setting at fault; previously a bad config crash-looped the uvicorn
  workers. Configuration errors no longer echo configured values (pydantic's
  default message printed the mailbox mapping, password included).
- **CI.** New `ci` workflow: lint + tests on every push and pull request, then
  an image build that is booted and smoke-tested, published to
  `ghcr.io/<owner>/<repo>` (`latest`, `main`, `sha-<commit>`, and version tags)
  for amd64 and arm64 on pushes to `main`. The upstream project's workflows
  (Docker Hub, mirrors, registry publishing) are now skipped outside the
  upstream repository.
- **Moving messages.** `move_message` / `POST /mailboxes/{name}/messages/move`
  moves one or more UIDs to another folder, reporting `moved`, `not_found` or
  `error` per UID. `to_folder` may be the role `junk`, `trash` or `archive`,
  resolved via SPECIAL-USE (`\Junk`, `\Trash`, `\Archive`) and then common
  folder names. Uses IMAP `MOVE` when advertised; otherwise COPY, flag
  `\Deleted`, and `UID EXPUNGE` of just those UIDs. `mark_spam` /
  `POST /mailboxes/{name}/messages/spam` moves to Junk.
- **Reading no longer risks marking mail read.** Message bodies are fetched
  with `BODY.PEEK[]` instead of `RFC822`, which sets `\Seen` on any folder
  not opened read-only.
- **`GET /health/detail`** (authenticated) logs in to every account's IMAP and
  SMTP server and reports which succeeded.
- IMAP connections now time out after 30 seconds, and connection and protocol
  errors raised by `imaplib` surface as `502` instead of an unhandled `500`.
  Header values containing a line break are rejected with `422`. Non-ASCII
  folder names are encoded as modified UTF-7 instead of crashing.
- UID validation used `$`, which accepts a trailing newline; `"7\n"` reached
  the IMAP command line. It now requires a full match.

## v0.4.16

Documentation only. The README's agent integrations section names [peen](https://github.com/psyb0t/peen) as an example of an agent that reads `.agents/skills/`.

## v0.4.15

Fixes the documented `/mcp` endpoint for Streamable HTTP clients that do not
follow redirects.

- `POST /mcp` returned `307` to `/mcp/` because the raw ASGI mount matched only
  the slash form. This broke MCP clients that retain the documented `/mcp` URL
  and do not follow redirects on Streamable HTTP requests.
- The app now normalizes only the exact `/mcp` path before routing. Bearer auth
  remains outermost, `/mcp/` still works, and the other HTTP routes keep their
  existing slash behavior.
- MCP contract tests now disable redirect following and cover both endpoint
  forms, so another redirect cannot pass unnoticed.
- Removed stale wording from the CI workflow comment. No behavior change.

## v0.4.14

Refreshes the MCP stack and locks the Python dependency graph.

- Upgraded the MCP SDK to 1.28.1 and Black to 26.3.1. Both runtime and
  development images now install strictly from `uv.lock`; they no longer fall
  back to an unlocked resolution.
- Added a Streamable HTTP MCP contract test that initializes the mounted route,
  lists the exposed tools, and calls `mailboxes` through the real application.
- Package commands now update exact pins correctly and support the `dev` group,
  so a development-only tool does not collide with runtime dependencies.
- Aligned Python runtime, package, health-response example, and Codex plugin
  version declarations with the release tag.

## v0.4.13

Fixes an IMAP command injection vulnerability (issue #1). Closes #1.

- The `folder`, `uid` and `search` values that reach the IMAP client were
  string-interpolated into IMAP commands and handed to Python's `imaplib`,
  which does not sanitize its own arguments. A CR/LF in any of them (reachable
  from an ordinary `%0D%0A` in an HTTP query or path parameter) ended the
  current command and spliced a second, attacker-chosen IMAP command onto the
  same authenticated connection, including a `STORE +FLAGS (\Deleted)` +
  `EXPUNGE` pair that deletes a whole folder through the read-only message
  routes.
- Every such value is now validated before it reaches `imaplib`: `folder`,
  `search` and structured search terms (including `since`/`before`) reject CR,
  LF and NUL; `uid` must be digits and sequence-set punctuation only; and
  folder names have any embedded `"` and `\` escaped so they cannot break out
  of the IMAP quoted string. The checks live in `imap_client`, so both the
  HTTP API and the MCP server inherit them.
- Added an in-process mock IMAP server and an IMAP test suite: the happy path
  (list, fetch, structured search) plus the injection cases, including the
  verbatim issue #1 payload driven through the read-only GET route.

## v0.4.12

Fixes the CI lint failure introduced by v0.4.11 so the image builds and publishes.

- v0.4.11 added `make lint` to CI for the first time, which surfaced a pre-existing flake8
  E501: a 103-character line in `src/mailboxd/server.py`. The lint job failed and the image
  build and publish steps, which depend on it, were skipped. Wrapped the line so flake8
  passes. No behavior change.

## v0.4.11

CI now lints, tests, and security-scans the code before building the image, via
the generic reusable code-workflow.

- New `code` pipeline job runs `make lint` (flake8 + mypy), `make test` (pytest
  unit + docker-in-docker integration), and `make sec`, and the image build now
  waits on it. The pipeline previously built and published the image without
  running any of these.
- New `make sec`: semgrep, bandit and pip-audit run in parallel and their
  findings merge into `sec.sarif` for the GitHub Security tab. It never fails the
  build; findings are reported, not gated. The scanners live in an isolated venv
  in the dev image so semgrep's bundled mcp pin cannot collide with the project's
  `mcp==1.9.0`.
- Removed the `make check` alias; use `make lint`, `make test`, `make sec`.
- Kept `.telemetry/` out of git and out of the image.

## v0.4.10

CI plumbing only. No code in this repo changed — every commit in this release touches `.github/workflows/`.

- The pipeline was split: building and publishing stay in `pipeline.yml`, and everything that leaves the host now lives beside it in `mirror-and-archive.yml`.
- The repo is mirrored to Codeberg as well as GitLab.
- It is archived to the Wayback Machine, Software Heritage and archive.org.
- Issues opened on either mirror are copied back to GitHub every six hours, and closed here when the original closes.
- Pull requests are switched off on the mirrors — they are force-pushed from GitHub, so anything merged there would be destroyed by the next sync. Issues and forking stay enabled.

## v0.4.9

Codex install command was missing from the README. Documentation only, no behavior change.

- The Codex subsection of `## Agent integrations` told readers to run `codex plugin marketplace add psyb0t/agents` and stopped there — it never showed the actual install command. Added `codex plugin add mailbox@psyb0t` right after it.
- Clarified that the invocation form depends on how the skill was obtained: installed via the marketplace it's `$mailbox:docker-mailbox`; picked up automatically (no install) from a repo's own `.agents/skills/` it's plain `$docker-mailbox`.

## v0.4.8

Renames the plugin from `docker-mailbox` to `mailbox`. Metadata only, no behavior change.

- **The install name is now `mailbox@psyb0t`.** v0.4.7 named the plugin after the repository, which made it the only entry in the `psyb0t` marketplace carrying a `docker-` prefix; every other plugin installs under its bare tool name. `mailbox` also matches the MCP-bridge plugin in `.agents/plugins/mailbox` and the container's own name.
- The ClawHub skill is unaffected and stays `@psyb0t/docker-mailbox`, as does `openclaw plugins install clawhub:@psyb0t/mailbox`. Only the Claude Code / Codex plugin identifier changed.

## v0.4.7

Agent-client distribution wiring. Documentation and metadata only, no behavior change.

- Added `.agents/.claude-plugin/plugin.json` and `.agents/.codex-plugin/plugin.json` so the existing `docker-mailbox` skill installs natively as a plugin in Claude Code and Codex, rooted at `.agents/`.
- New `## Agent integrations` README section (with Table of Contents entry) documenting the `claude plugin` / `codex plugin` marketplace install commands, the OpenClaw skill install, and the OpenClaw MCP-bridge plugin install for `@psyb0t/mailbox`.

## v0.4.6

- Added a GitHub Actions CI status badge to the README.

## v0.4.5

- Added self-hosted version and license badges plus a Docker Hub pulls badge; wired a badges job into pipeline.yml.

## v0.4.4

Listed on the official MCP Registry — no behavior change.

- Added `server.json` — published to the official Model Context Protocol Registry (`registry.modelcontextprotocol.io`) as `io.github.psyb0t/mailbox`, pointing at the `psyb0t/mailbox` Docker image. Ownership is proven by an `io.modelcontextprotocol.server.name` LABEL on the image; publishing runs on tag pushes via GitHub OIDC (secretless). Also added a `glama.json` maintainer claim.

## v0.4.3

Third-party license notices. Documentation only, no behavior change.

- Added `THIRD_PARTY.md` + `LICENSES/` documenting the GPL-3.0 `html2text` dependency bundled into the published image. The project's own code stays WTFPL.
- gitignore the container-generated `CLAUDE.md` so it never ships.

## v0.4.2

Skill docs de-duplicated. Documentation only, no behavior change.

- Collapsed the three repeated message-deletion warnings in `.agents/skills/docker-mailbox/SKILL.md` into one clear mention in the delete-endpoint reference plus a brief Security & safety note.

## v0.4.1

- Hardened the skill docs with explicit destructive-operation guardrails and auth/exfil warnings. No behavior change — documentation only.
  - New "Security & safety" section in the `docker-mailbox` skill.
  - `DELETE /mailboxes/<name>/messages/<uid>` is now flagged as destructive & irreversible everywhere it's documented, with explicit confirm-before-delete guidance for agents.
  - The "Find and delete" workflow now treats the search step as a dry-run: list and confirm matches with the user before deleting, instead of piping a broad search straight into bulk delete.
  - Documented the unauthenticated-when-`auth.tokens`-is-empty risk and that `MAILBOX_URL` traffic leaves the host.

## v0.4.0

- ClawHub plugin: new `@psyb0t/mailbox` code plugin (stdio↔HTTP MCP bridge via `mcp-remote` to `/mcp`), MIT-licensed.
- Pipeline switched to reusable `clawhub-publish.yml` (publishes skill + plugin).
