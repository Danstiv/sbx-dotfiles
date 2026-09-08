# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Personal config for running Claude Code inside a [Docker Sandbox](https://docs.docker.com/ai/sandboxes/) (sbx) microVM. Two independent halves that never import each other:

| | runs | built/started by |
| -- | -- | -- |
| `template/` + `kit/` | **inside** the sandbox VM | `./build-template.sh`, then `sbx run` |
| `mcp_gateway/` | on the **host**, in front of `sandboxd` | `uv run -m daemon` |

`README.md` (image, wrappers, backups) and `mcp_gateway/README.md` (the gate) are the design record — they carry the reasoning for nearly every decision below, as do the module docstrings. Both are worth reading before changing behaviour, and worth updating when you do.

## Commands

Everything Python goes through `uv run` (see the global CLAUDE.md); there is no `./.venv` in the repo — the wrapper puts it under `~/.venvs`. A `mcp_gateway/.venv` with `Scripts/*.exe` may exist: that is the *host's* Windows venv on the bind-mount, not yours. Ignore it.

```bash
# mcp_gateway (cwd: mcp_gateway/)
uv run pytest                                  # 35 tests, ~13s
uv run pytest tests/test_gate.py -q            # one file
uv run pytest tests/test_e2e.py::test_a_json_rpc_batch_is_refused -q   # one test
uv run -m daemon [-v] [-d|--stop|--status]     # the gate
uv run -m cli                                  # the approval console

# image
./build-template.sh    # docker build -> image save -> sbx template load
```

`build-template.sh` and every `sbx …` command need the `sbx` CLI, which exists only on the host — you cannot run them from inside the sandbox. Ask the user.

The e2e tests are not mocked: they issue a throwaway CA, run a real HTTPS server, start the real daemon and drive it through the real proxy. They need no network beyond loopback, so they do run in the sandbox.

## mcp_gateway architecture

An approval gate for MCP `tools/call`, sitting on the host outside the VM: `agent (VM) → sbx gateway → sandboxd → this daemon → MCP server`. Layering, strictly one-way:

- `protocol/` — the wire vocabulary (`StrEnum`s + constructor functions), imported by both ends so the daemon and any client cannot drift. Nine message types, nothing else.
- `daemon/addon.py` — the *only* module that touches mitmproxy or HTTP. Parses bodies, strips the sandbox suffix, attaches tokens, writes refusals.
- `daemon/gate.py` — the holding pen: pending calls as futures, keyed by id. Knows nothing about mitmproxy or websockets, which is what lets a client drop mid-prompt without losing the call.
- `daemon/control.py` — websocket server, auth, broadcast. Owns no state about pending calls.
- `daemon/config.py` — all file I/O (`Config`, `PolicyFile`, `DecisionStore`, secret/token readers).
- `daemon/detach.py` — background run. Not a POSIX double-fork (Windows); liveness is always the control port answering, never the pidfile.

mitmproxy is used as a library, not as a `mitmdump` subprocess.

### Invariants worth not breaking

- **A refused call is HTTP 200 with `isError: true`.** sbx reads any 4xx from a backend as "not authorized" and parks the whole server until it is kicked by hand. Only a body that could not be parsed far enough to name a tool gets a JSON-RPC `error` object (still inside a 200).
- **Sandbox identity is the URL path suffix** (`/mcp-project1` → sandbox `project1`, path rewritten to `/mcp`). Nothing else in the request says which sandbox called. A suffix absent from `policy.json` means the call is unattributable: the `tools/call` is refused, everything else is forwarded un-rewritten and 404s upstream — which doubles as the canary that the gate is still in the path.
- **Decisions are keyed by `(sandbox, host, tool)`.** The host is in the key because tool names collide across MCP servers (`get_issue` exists in both Linear and GitHub).
- **Only `tools/call` is gated.** `initialize`, `tools/list`, notifications and OAuth pass through, reported as `passthrough` events.
- **Requests are buffered and parsed; responses never are.** MCP answers over `text/event-stream` and may hold a stream open forever — `responseheaders` sets `flow.response.stream = True`. Bodies over `MAX_BODY` (1 MB) are refused rather than waved through, and a JSON array (a JSON-RPC batch) is refused rather than forwarded.
- **`policy.json` is hot-reloaded; `config.json` and `tokens.json` are read once.** The split is by whether a setting can change under a running process — ports are bound at startup, a credential re-read is one more moment it is on disk. A policy edit that does not parse leaves the last good policy in force. `watch_policy` polls as well as traffic, because `allow_hosts` is consulted when a connection opens, so a newly added host would otherwise never produce a call to trigger the re-read.
- **Never fail open.** An unreadable `decisions.json` means "ask about everything"; upstream TLS verification is never disabled.

`data/` is gitignored whole; `examples/` holds the copies to seed it from. Editing `examples/policy.json` or `examples/config.json` does not affect a running gate — the user's `data/` is what is read.

## template/ and kit/ invariants

- **`bin/*` are PATH wrappers that shadow real binaries.** `/opt/sbx/bin` is first in `PATH` (set via `ENV` in the Dockerfile so Claude's non-interactive Bash tool sees it too). `shim-lib.sh:resolve_real` finds the real binary by walking `PATH` and skipping anything that resolves to the wrapper itself — so a wrapper must never be renamed apart from the binary it stands in for.
  - `bin/claude` strips `--dangerously-skip-permissions`, which sbx's generated `/usr/local/lib/sandbox/start-agent` passes as a bare `claude` (so the wrapper does intercept it, verified via `/proc/<pid>/cmdline`). Removing the flag is only half of it — see the seed below.
  - `bin/uv` derives `UV_PROJECT_ENVIRONMENT=~/.venvs/<repo>-<hash>` per invocation, because a venv cannot live on the virtiofs workspace (symlinks are EPERM) and uv reads that path only from the environment.
- **Dockerfile layer order is deliberate**: `COPY bin/ scripts/` comes last so editing a script does not invalidate the apt and uv install layers above it. `--chmod` on the COPY is what sets modes — a Windows bind-mount reports 0777, and there is no separate chmod step to keep in sync.
- **sbx's own `claude` kit seeds `~/.claude/settings.json` with `"defaultMode": "bypassPermissions"`.** The command is in `sbx.exe` (`description: Seed Claude settings.json from SBX_CRED_ANTHROPIC_MODE`) and also writes `bypassPermissionsModeAccepted` and `skipDangerousModePermissionPrompt`, so nothing prompts. It is an *install* command: synchronous, finished during create, before the first session. Baking a `settings.json` into the image does not help — it is overwritten in place, and only that file is targeted.
  - **Permission mode is resolved once at session start; deny rules and hooks are re-read as `settings.json` changes.** So the seed has to be beaten before the first read, not corrected after it — a session in the wrong mode still enforces `Read(**/.env)`.
  - `bin/claude` logs argv plus the size, owner, mtime and full contents of `~/.claude/settings.json` under `SBX_SHIM_DEBUG=1` — the only way to see that file before `init-config.py` merges over it.
- **`init-config.py` is idempotent** via `~/.claude/.sbx-config-initialized`, so config edited inside the sandbox survives a restart. Two steps run *before* that marker check — removing sbx's generated `CLAUDE.md` and pre-answering the auto-mode default nudge in `~/.claude.json` — because neither is config the user might have edited. It deep-merges `claude-settings.json` over the existing `settings.json` (backing the old one up as `.pre-sbx`), lays out `gitignore-global` to `~/.config/git/ignore`, and deletes sbx's auto-generated `CLAUDE.md` by signature only.
  - **`${SANDBOX_NAME}` in `CLAUDE.md` is the one placeholder** — substituted at lay-out time from `$SANDBOX_NAME` (else the hostname), so the agent can offer `sbx policy allow network --sandbox <name> …` without looking the name up. Everything else is copied byte for byte.
- **`bin/git-context` is a `UserPromptSubmit` hook, not a command anyone runs.** Its stdout is appended to every prompt (`<git-state>`: `git status --short --branch` + last five commits), which is what keeps the agent's picture of the repo current while the user commits in a parallel tab. It must exit 0 no matter what — a failing `UserPromptSubmit` hook blocks the prompt — so "not a repo", "no jq" and an over-long status all degrade to printing less.
- **`kit/spec.yaml` is schemaVersion 2 and runs `init-config.py` twice; the `setup.install` entry is the one that matters.** Mixin install commands run after the base agent's, synchronously during create — so ours merges over the bypass seed before the first session reads it. `setup.startup` cannot: sbx fires those from a detached dispatcher, measured 1.5 s after the agent had already started. The startup entry stays for the steps ahead of the idempotence marker. Note the two shapes differ: `install.command` is a shell string defaulting to root, `startup.command` is argv defaulting to uid 1000 — hence the explicit `user: "1000"` on install.
- **`sbx kit validate ./kit` before rebuilding.** A spec the schema rejects errors out of `sbx run`, which is easy to work around by creating the sandbox without the kit — and a sandbox running without it looks normal until you notice there is no `002-startup-claude-config` in `/var/log/sbx-kit-startup.log` and no `~/.claude/.sbx-config-initialized`. Also note `sbx kit add` only applies `environment.variables`, `setup.install` and `permissions.network.allow` — this kit's `setup.startup` and `setup.files` need a recreate.
- **Kit `startup` commands run on every sandbox start, but the kit itself is captured at creation.** Editing `kit/spec.yaml` does not reach an existing sandbox by re-running `sbx run` — that needs `sbx kit add <sandbox> ./kit`. Same for `--template`.
- **Personal, gitignored, must stay absent from commits**: `template/scripts/CLAUDE.md`, `template/scripts/claude-settings.json`, `mcp_gateway/data/`. Each has an `.example` (or `examples/`) counterpart that *is* tracked; when you change what a config key means, update the example.
