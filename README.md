# ops-toolkit

A toolkit of small, self-contained operations tools. Every tool is a single
executable script built only on the Python 3.11+ standard library — nothing
to install, nothing running in the background. Copy it onto a machine, read
it in one sitting, use it.

| Tool | Description |
| --- | --- |
| [keydist](#keydist) | Push secrets from one central manifest into each consumer's `.env` file; run post commands when they change |

简体中文说明见 [README.zh-CN.md](README.zh-CN.md)。

## Principles

- **Zero dependencies** — standard library only (`tomllib` requires Python 3.11+).
- **Fail closed** — malformed or ambiguous configuration is a hard error,
  never silently downgraded into something unsafe.
- **Idempotent** — consumer files are rewritten only when their content
  actually changes, so downstream hooks fire exactly once per change.
- **Local-first** — manifests under `~/.local/etc/`, executables under
  `~/.local/bin/`, no network access.

---

## keydist

`keydist` reads a single manifest — `~/.local/etc/keydist.toml` — resolves
the secrets it references, and incrementally updates each consumer's `.env`
file in the format that consumer understands. When a file's content actually
changed, an optional `post` command runs (e.g. restart the service).

### Manifest

```toml
# ~/.local/etc/keydist.toml   (chmod 600 recommended)

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
post = "systemctl --user restart myapp"    # optional; runs only when the file changed

[targets."myapp".vars]                     # consumer key = secret name in [secrets]
API_KEY     = "api_key"
DB_PASSWORD = "db_pass"
DOMAIN      = "domain"
TLS_CERT    = "tls_cert"
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

### Install

```bash
install -Dm755 keydist ~/.local/bin/keydist
```

Then write `~/.local/etc/keydist.toml` (recommended `chmod 600`) and run `keydist`.
