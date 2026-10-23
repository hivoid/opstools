# opstools

A collection of single-file operations tools. Each tool is one standalone
executable script — Python, shell, or whatever language suits the job —
sharing no code with its siblings: no installer, no daemon, no framework.
Copy a tool onto a machine, read it in one sitting, use it. More tools join
over time.

简体中文说明见 [README.zh-CN.md](README.zh-CN.md)。

## Tools

| Tool | Language | Description |
| --- | --- | --- |
| [keydist](#keydist) | Python 3.11+ | Push secrets from one central manifest into each consumer's `.env` file; run post commands when they change |

## Principles

- **Single file, zero dependencies** — one executable per tool, depending
  only on its language's standard library; the language is chosen per tool
  and noted in the table above.
- **Fail closed** — malformed or ambiguous configuration is a hard error,
  never silently downgraded into something unsafe.
- **Idempotent** — repeated runs converge to the same state; side effects
  (file rewrites, hooks, commands) fire only on real change.
- **Local-first** — config under `~/.config/` (machine-wide under `/etc/`),
  executables under `~/.local/bin/`, no network access.

## Install

Tools are independent — install only the ones you need, straight from the
repository into `~/.local/bin`:

```bash
install -m755 keydist ~/.local/bin/keydist
```

---

## keydist

`keydist` locates a manifest (see below), resolves the secrets it
references, and incrementally updates each consumer's `.env` file in the
format that consumer understands. When a file's content actually changed,
an optional `post` command runs (e.g. restart the service).

### Use cases

- **One source of truth for API keys** — several local tools (an nvim AI
  plugin, aider, litellm, …) each read their own `.env` yet need the same
  upstream keys. Store each key once in the manifest and let keydist push
  it everywhere, instead of hand-editing a dozen files.
- **Same secret, different formats** — the same value must be `KEY='v'` in
  a shell-sourced file and `KEY="v"` in systemd's `EnvironmentFile=`;
  keydist renders each target's style from one manifest.
- **Rotate once, update everywhere** — change the key in whatever it
  references (env var / dotenv / file), re-run keydist: only affected
  files are rewritten and their `post` commands (e.g. service restarts)
  run automatically.
- **Change with confidence** — `--dry-run` diffs every pending change
  before anything is touched.

### Manifest

The manifest is taken from `--config PATH` / `-c PATH` when given (it must
exist); otherwise the first existing file of this lookup order is used:

1. `$XDG_CONFIG_HOME/keydist/keydist.toml` (default `~/.config/keydist/keydist.toml`)
2. `~/.local/etc/keydist.toml` (legacy default)
3. `/etc/keydist/keydist.toml`

```toml
# ~/.config/keydist/keydist.toml   (chmod 600 recommended)

[secrets]
# Bare string / number / boolean -> literal, written verbatim (booleans as true/false):
domain  = "example.com"
retries = 3

# Tables are references, resolved at run time:
api_key = { env = "API_KEY" }                                 # process environment variable
db_pass = { envfile = "~/.config/db.env", key = "password" }  # key in a dotenv file
ssh_key = { file = "~/.ssh/id_ed25519" }                      # whole file, trailing newline stripped

# Inline tables cannot span lines (TOML 1.0); use a sub-table for multi-line chains:
[secrets.tls_cert]
chain = [                        # fallback chain: first success wins
  { env = "TLS_CERT" },
  { file = "~/.local/etc/tls/cert.pem" },
]

[targets."myapp"]
path = "~/srv/myapp/.env"                  # required; ~ expanded, parents created
style = "dotenv"                           # optional; shell / dotenv / systemd / docker (overrides inference)
post = "systemctl --user restart myapp"    # optional; runs only when the file changed

[targets."myapp".vars]                     # consumer key = secret name in [secrets]
API_KEY     = "api_key"
DB_PASSWORD = "db_pass"
DOMAIN      = "domain"
TLS_CERT    = "tls_cert"

# style omitted -> inferred from the target's name/path/post ("systemd" here,
# from the systemctl post); falls back to the default "shell" if nothing matches.
[targets."myapi"]
path = "/etc/myapi/myapi.env"
post = "systemctl restart myapi"

[targets."myapi".vars]
API_KEY = "api_key"
DOMAIN  = "domain"
```

### Secret forms

| Form | Meaning |
| --- | --- |
| bare string / number / boolean | literal value (booleans render as `true` / `false`) |
| `{ env = "NAME" }` | process environment variable `NAME` |
| `{ envfile = "PATH", key = "KEY" }` | value of `KEY` in a dotenv file; omit `key` when the file holds exactly one pair |
| `{ file = "PATH" }` | whole file content, trailing newline stripped (handy for private keys) |
| `{ chain = [e1, e2, …] }` | try elements in order, first success wins; elements may be literals or references (a nested `chain` is allowed) |

A reference table must set exactly one of `env` / `envfile` / `file` /
`chain`; unknown or conflicting keys are hard errors. Because a bare string
is a literal by type, there is no prefix to mistype — a misspelled reference
such as `{ en = "X" }` fails loudly instead of leaking a literal into the
consumer's file.

### Output styles

Each target is rendered in one of four styles: set `style = "..."` explicitly,
or let keydist infer it from the target's name / path / post command:

| Style | Quoting | Typical consumer |
| --- | --- | --- |
| `shell` (default) | POSIX single quotes | anything that `source`s the file — the maximum-compatibility superset |
| `dotenv` | double quotes, `\n` expanded to newlines | node/python/Go/Ruby dotenv parsers |
| `systemd` | double quotes, no `export` | systemd `EnvironmentFile=` |
| `docker` | raw value, no escaping at all | `docker run --env-file` |

Style-aware checks run before writing: multi-line values are rejected for
`systemd` and `docker`; `docker` warns where compose and `docker run`
disagree about quote semantics.

### Behavior

- Files are rewritten only when content changes; unchanged files keep their
  mtime and skip `post`.
- `--dry-run` / `-n` prints a unified diff of every pending change plus the
  `post` commands that would run — no files are written, chmodded, or
  executed.
- Target files are always ensured mode `0600`; new files are created `0600`
  from the start.
- `post` is split with `shlex` and capped at 30 s; non-zero exit or timeout
  marks the target as failed.
- Stale duplicate keys left behind by earlier runs are cleaned up (with a
  warning).
- The manifest's permissions are checked at startup; group/world-readable
  modes warn (it may contain secret references).
- Exit code `0` when every target succeeded, `1` otherwise, with a failure
  summary on stderr.

### Usage

```bash
keydist                 # apply the manifest
keydist --dry-run       # show unified diffs of pending changes; run nothing
keydist -c ~/alt.toml   # use an explicit manifest
```

### Getting started

Write `~/.config/keydist/keydist.toml` (recommended `chmod 600`), then run
`keydist` (see [Install](#install)).
