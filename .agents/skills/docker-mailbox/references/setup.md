# docker-mailbox setup

## Requirements

- Docker + Docker Compose
- One or more email accounts with IMAP and/or SMTP credentials
- For Gmail / Yahoo / Outlook / iCloud / most major providers: an **app password** (your normal account password will not work; you need to enable 2FA and generate an app-specific password in the provider's security settings)

## Quick Install

```bash
git clone https://github.com/psyb0t/docker-mailbox
cd docker-mailbox
cp config.example.yaml config.yaml
# Edit config.yaml — add your mailboxes and auth.tokens
```

Then either:

```bash
# Plain docker run
docker run --rm -p 8000:8000 \
  -v "$PWD/config.yaml:/etc/mailboxd/config.yaml:ro" \
  ghcr.io/ariangibson/docker-mailbox:latest

# Or docker compose (recommended)
docker compose up -d
```

Verify it came up:

```bash
curl -s http://localhost:8000/health
# {"ok": true, "version": "..."}

# and that every account can actually log in
curl -s -H "Authorization: Bearer $MAILBOX_TOKEN" http://localhost:8000/health/detail
```

### No config file: environment variables only

The server can be configured entirely from `MAILBOX_*` variables — one per account, `MAILBOX_<NAME>=<provider>:<username>:<password>`:

```bash
docker run --rm -p 8000:8000 \
  -e MAILBOX_AUTH_TOKENS="$(openssl rand -hex 32)" \
  -e MAILBOX_PERSONAL='gmail:me@gmail.com:app-password-here' \
  ghcr.io/ariangibson/docker-mailbox:latest
```

Built-in providers: `purelymail`, `gmail`, `fastmail`, `outlook`; others via `MAILBOX_PROVIDER_<NAME>_IMAP_HOST` / `_SMTP_HOST` (plus `_PORT` / `_TLS`). Per-account overrides (`MAILBOX_<NAME>_FROM_ADDRESS`, `_ALLOWED_FROM`, `_IMAP_HOST`, …), the full variable list and a compose example are in the [README](https://github.com/psyb0t/docker-mailbox#environment-only-configuration). A mounted `config.yaml` still loads; the environment overrides it.

## Configuration

### `config.yaml`

One YAML file. Lives at `MAILBOXD_CONFIG`, or `--config`, or `/etc/mailboxd/config.yaml` by default (which is where the prod container expects the read-only mount).

```yaml
log_level: INFO

# Bearer-token gate. Guards the HTTP API AND /mcp. Required: with no tokens
# the server won't start unless MAILBOX_ALLOW_NO_AUTH=true is set.
# Multi-token list = rotate without downtime: add a new one, swap clients
# over, retire the old one.
auth:
  tokens:
    - "long-random-token-1"        # generate with: openssl rand -hex 32
    # - "long-random-token-2"

mailboxes:
  - name: personal                  # URL-safe handle; matches [a-zA-Z0-9_-]+, must be unique
    description: "Gmail"

    imap:
      host: imap.gmail.com
      port: 993                     # default 993
      tls: ssl                      # ssl | starttls | none   (default ssl)
      username: me@gmail.com
      password: "app-password"      # not your real account password
      default_folder: INBOX         # default folder when callers don't specify

    smtp:
      host: smtp.gmail.com
      port: 465                     # default 587
      tls: ssl                      # default starttls
      username: me@gmail.com
      password: "app-password"
      from_address: "Me <me@gmail.com>"

  - name: work
    imap: { host: mail.work.com, port: 143, tls: starttls, username: me, password: "...", default_folder: INBOX }
    smtp: { host: mail.work.com, port: 587, tls: starttls, username: me, password: "...", from_address: me@work.com }
```

### Config rules

- **At least one mailbox** is required.
- Each mailbox must declare at least one of `imap` / `smtp`. Both is fine. Neither is a config error.
- **`name`** is the URL path segment AND what MCP tools take as `mailbox`. Matches `[a-zA-Z0-9_-]+`. Must be unique across the file.
- **Defaults**: IMAP `993/ssl`, SMTP `587/starttls`. Override per-mailbox if your provider is weird.
- **TLS certificates are verified** for IMAP and SMTP. For a self-signed server on a trusted network set `tls_verify: false` under `imap` / `smtp` (env: `MAILBOX_<NAME>_IMAP_TLS_VERIFY=false`).
- **Optional per-mailbox keys**: `allowed_from` (list of From addresses the mailbox may send as, `*` wildcard; other senders get `403`), and under `imap`: `drafts_folder` / `sent_folder` (fallbacks for servers without SPECIAL-USE folder flags) and `save_sent` (default `true`; set `false` on Gmail/Outlook, which file sent mail themselves).
- **The config file holds plaintext passwords and your bearer tokens.** Treat it like a credential vault: gitignore it (the repo already does), `chmod 600`, mount read-only into the container, don't paste it in chat. Or keep secrets out of it: any string value may be `"${ENV_VAR}"`, or skip the file and use `MAILBOX_*` variables.

### Provider quick-reference

| Provider     | IMAP host           | IMAP port | TLS       | SMTP host           | SMTP port | TLS       | Auth requirement                                          |
| ------------ | ------------------- | --------- | --------- | ------------------- | --------- | --------- | --------------------------------------------------------- |
| Gmail        | `imap.gmail.com`    | 993       | `ssl`     | `smtp.gmail.com`    | 465       | `ssl`     | 2FA + app password                                        |
| GMX          | `imap.gmx.com`      | 993       | `ssl`     | `mail.gmx.com`      | 587       | `starttls`| Regular account password works                            |
| Yahoo        | `imap.mail.yahoo.com` | 993     | `ssl`     | `smtp.mail.yahoo.com` | 587     | `starttls`| 2FA + app password (16 lowercase chars)                   |
| Outlook/Hotmail | `outlook.office365.com` | 993 | `ssl`    | `smtp.office365.com` | 587      | `starttls`| App password if 2FA, OAuth not supported                  |
| iCloud       | `imap.mail.me.com`  | 993       | `ssl`     | `smtp.mail.me.com`  | 587       | `starttls`| 2FA + app-specific password                               |
| FastMail     | `imap.fastmail.com` | 993       | `ssl`     | `smtp.fastmail.com` | 465       | `ssl`     | App password                                              |
| ProtonMail   | (via Bridge `127.0.0.1`) | 1143 | `starttls`| (via Bridge `127.0.0.1`) | 1025 | `starttls`| Bridge running locally; per-app credentials               |

Confirm with the provider's docs — these change occasionally.

## Ports

| Port | Service                                                                                  |
| ---- | ---------------------------------------------------------------------------------------- |
| 8000 | HTTP API (`/health`, `/mailboxes`, `/inbox`, `/mailboxes/<name>/...`) AND MCP at `/mcp`  |

That's it. One port, one process, two surfaces sharing the same bearer-auth gate. Override the host port with `-p HOST:8000` on `docker run` (or set `ports:` in compose).

## Auth

Every request except `GET /health` must carry one of the server's tokens (`MAILBOX_AUTH_TOKENS`, comma-separated, or `auth.tokens` in the config file):

```
Authorization: Bearer <token>
```

- Missing / malformed / wrong token → `401` with `WWW-Authenticate: Bearer`.
- Tokens are compared in constant time (no timing leaks).
- The same gate covers `/mcp` — there's no second auth system to learn.
- Multiple tokens in the list let you rotate without downtime: add the new one, switch clients over, remove the old one.

With no tokens configured the server refuses to start. Setting `MAILBOX_ALLOW_NO_AUTH=true` overrides that and leaves all endpoints open. Fine for "this is bound to `127.0.0.1` and there's a reverse proxy in front." Catastrophic otherwise.

## Management

```bash
# Lifecycle
docker compose up -d         # start
docker compose down          # stop
docker compose logs -f       # tail logs
docker compose pull          # grab a new image
docker compose restart       # restart after editing config.yaml

# Dev (in-repo workflow)
make help                    # list dev targets
make dev-image               # build the sandboxed dev container
make shell                   # drop into it
make run                     # boot the server locally (mounts CONFIG=path/to/config.yaml)
make test                    # full suite (unit + docker-in-docker integration)
make test-unit               # in-process only, fast feedback
make lint                    # flake8 + mypy
make sec                     # semgrep + bandit + pip-audit -> sec.sarif (reports, never fails)
make format                  # isort + black
```

## Logs

mailboxd logs to stdout in the format `%(asctime)s %(levelname)s %(name)s %(message)s`. Control level with `log_level: DEBUG|INFO|WARNING|ERROR` in `config.yaml`. Read with `docker compose logs -f` or whatever your orchestrator does with container logs.

## Public Access via Cloudflare Tunnel (optional)

Expose mailboxd to the internet without opening firewall ports. **Make sure `auth.tokens` is set first** — anything reachable from the internet without auth is a credential-exfiltration vector.

```bash
# Install cloudflared
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /tmp/cloudflared
sudo install /tmp/cloudflared /usr/local/bin/cloudflared

# Authenticate and create tunnel
cloudflared tunnel login
cloudflared tunnel create mailbox

# Route a subdomain
cloudflared tunnel route dns mailbox mailbox.yourdomain.com

# Stash creds
mkdir -p .data/cloudflared
cp ~/.cloudflared/<tunnel-id>.json .data/cloudflared/creds.json
```

Create `.data/cloudflared/config.yml`:

```yaml
tunnel: <tunnel-id>
credentials-file: /etc/cloudflared/creds.json

ingress:
  - hostname: mailbox.yourdomain.com
    service: http://mailbox:8000
  - service: http_status:404
```

Add a sidecar to `docker-compose.yml`:

```yaml
services:
  mailbox:
    image: ghcr.io/ariangibson/docker-mailbox:latest
    volumes:
      - ./config.yaml:/etc/mailboxd/config.yaml:ro
    # no `ports:` — only cloudflared reaches it

  cloudflared:
    image: cloudflare/cloudflared:latest
    command: tunnel --config /etc/cloudflared/config.yml run
    volumes:
      - ./.data/cloudflared:/etc/cloudflared:ro
    depends_on:
      - mailbox
```

Hit `https://mailbox.yourdomain.com` from anywhere. The bearer token is now your only line of defense — keep it long, keep it secret, rotate it.

Note: Cloudflare's free Universal SSL covers `*.yourdomain.com` but not deeper subdomains like `*.mail.yourdomain.com`. Stick with one-level subdomains under your root.

## Troubleshooting

| Symptom                                                  | Likely cause                                                                                              |
| -------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `401 missing or invalid Bearer token`                    | Forgot `-H "Authorization: Bearer ..."` or token doesn't match anything in `auth.tokens`.                  |
| `502 connection failed: ... CERTIFICATE_VERIFY_FAILED`   | The mail server's certificate isn't trusted or doesn't match the host name. Fix the host name; for a self-signed server see `tls_verify`. |
| `502 login failed`                                       | Wrong password OR you used your account password instead of an app password (Gmail/Yahoo/iCloud/Outlook).  |
| `502 [AUTHENTICATIONFAILED] LOGIN Invalid credentials`   | Yahoo: app passwords are **16 lowercase letters**. If yours has digits/symbols/caps, it's the wrong one.   |
| `409 mailbox 'X' has no imap configured`                 | You called an IMAP endpoint on an SMTP-only mailbox (or vice versa). Check `/mailboxes` for capabilities.  |
| `404 unknown mailbox`                                    | Mailbox `name` in the URL doesn't match anything in `config.yaml` / no `MAILBOX_<NAME>` variable defines it. |
| `403 ... not permitted by this mailbox's allowed_from`   | The From address isn't on the mailbox's `allowed_from` list. Env-defined accounts default to `*@<their domain>`. |
| Self-send lands in Spam (especially GMX)                 | Provider-side self-send heuristic — search `folder=Spam`. Not a mailboxd bug; the headers are well-formed. |
| Container won't start, `config not found: ...`           | No config file at `/etc/mailboxd/config.yaml` and no `MAILBOX_<NAME>` account variables. Check the volume path or the environment. |
| Container exits with `mailboxd: MAILBOX_...: ...`        | That variable is malformed — the message says how. Values (passwords) are never printed.                   |
| Container exits with `no bearer tokens configured`       | Set `MAILBOX_AUTH_TOKENS` (or `auth.tokens`), or `MAILBOX_ALLOW_NO_AUTH=true` to run open on purpose.       |
| `config invalid: ...` on startup                         | Validation failure — the message names the mailbox and the bad field.                                      |
| MCP client gets `Task group is not initialized`          | Server isn't fully started yet (lifespan hasn't completed). Retry after a moment.                          |
| `/inbox` `errors` array has entries                      | Those mailboxes failed to connect/auth. The rest still returned results — check the per-mailbox `error`.   |
