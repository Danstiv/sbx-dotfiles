# sbx-dotfiles

My personal config and image build for [Docker Sandbox](https://docs.docker.com/ai/sandboxes/)
(sbx). Everything here is currently about running Claude Code inside an sbx
microVM: it installs tools and applies `settings.json`, the status line and
`CLAUDE.md`.

Dotfiles, so the usual caveat applies: this is what works for me, not a product.
Take the parts you like.

## Layout

```
sbx-dotfiles/
    template/                     # image (docker build)
        Dockerfile                # FROM docker/sandbox-templates:claude-code-docker
        bin/                      # -> /opt/sbx/bin, which is on PATH
            claude                # wrapper that drops sbx's --dangerously-skip-permissions
            uv                    # wrapper that puts each repo's venv on native fs
            claude-backup         # snapshot ~/.claude to a tarball
            claude-restore        # restore such a snapshot
            docker-backup         # snapshot /var/lib/docker onto the workspace
            docker-restore        # restore such a snapshot
            play-sound            # ask the host's http_player to play a sound (hooks)
            git-context           # UserPromptSubmit hook: current git state into the prompt
        scripts/                  # -> /opt/sbx, plain data
            shim-lib.sh           # shared helper sourced by the wrappers
            docker-daemon.sh      # stop/start dockerd, sourced by the docker-* commands
            init-config.py        # lays out ~/.claude on startup (idempotent)
            claude-settings.json.example # sample settings.json
            claude-settings.json  # your settings.json, deep-merged over the base (gitignored)
            statusline-command.sh # status line
            bashrc-extra.sh       # interactive-only shell bits -> ~/.bashrc
            gitignore-global      # -> ~/.config/git/ignore (the backup archives)
            CLAUDE.md.example     # sample global CLAUDE.md
            CLAUDE.md             # your personal CLAUDE.md (gitignored)
    kit/spec.yaml                 # install + startup: invokes /opt/sbx/init-config.py
    build-template.sh             # docker build -> save -> sbx template load
```

## Build and run

```bash
./build-template.sh                                        # build the image and load it into sbx
sbx run --template claude-code-custom claude --kit ./kit   # first run: creates the sandbox
sbx run --name <sandbox>                                   # later: re-attach, no flags needed
```

`--template` and `--kit` apply only when the sandbox is created — on re-attach
they are ignored, and the kit stays part of the sandbox spec. So an **edited**
kit does not reach an existing sandbox by re-running: use
`sbx kit add <sandbox> ./kit`, which recreates the container with the kit
appended, keeping the workspace and kit-owned volumes.

## Notification sounds (optional, set up on the host)

There is no audio inside the VM, and the sandbox blocks raw UDP/TCP at the
network layer — the only way out is HTTP to the host. So the `Stop` and
`Notification` hooks in the settings just POST to a small player running on the
host.

Nothing for that is shipped here. If you want sounds, grab a release of
[http_player](https://github.com/25k1/http_player) (prebuilt for Windows and
Linux) and set it up yourself: put your `.wav` files in its `sounds/` folder,
start it, and arrange for it to run at logon however you prefer.

The port is opened by the kit (`caps.network.allow`), so no `sbx policy allow`
is needed — but a kit only reaches an existing sandbox via `sbx kit add`.

The hooks are just `play-sound stop` and `play-sound prompt` — that command
wraps the request and assumes http_player's defaults (port `57919`, sounds named
`stop.wav` and `prompt.wav`). See the script for the rest.

## Personal config

The global `CLAUDE.md` and `claude-settings.json` are per-person, so they are
gitignored. `CLAUDE.md.example` carries the parts that describe the sandbox
itself (network policy, the bind-mount's quirks, git state, `uv`); personal
rules go in your copy. Copy the samples and edit them to taste:

```bash
cp template/scripts/CLAUDE.md.example            template/scripts/CLAUDE.md
cp template/scripts/claude-settings.json.example template/scripts/claude-settings.json
```

`init-config.py` lays `CLAUDE.md` out to `~/.claude/CLAUDE.md` and deep-merges
`claude-settings.json` into `~/.claude/settings.json` on startup; if either file
is absent it simply skips that step (the build does not fail).

## Backing up / restoring ~/.claude

Everything in the VM except the working directory is ephemeral — on a sandbox
recreate `~/.claude` (session history, memory, config edits) is wiped. Two
commands are baked into the image:

```bash
claude-backup            # -> ./claude-home.tar.gz (in the working dir = on the mount)
claude-restore           # <- ./claude-home.tar.gz

claude-backup <path>     # custom path
claude-restore <path>
```

By default the archive is written to the current directory — which is bind-mounted
to the host, so it survives a recreate. `claude-restore` extracts over `~`
(merge: files from the archive overwrite, everything else is left alone); for a
clean restore run `rm -rf ~/.claude && claude-restore <file>`.

## Backing up Docker state

`/var/lib/docker` is its own ext4 volume rather than part of the image, so it
survives a stop/start on its own — but it belongs to the sandbox, and `sbx rm`
deletes it. That takes with it built images, the build cache and named volumes
(test databases, credentials typed in by hand).

```bash
docker-backup            # -> <workspace>/docker-data.tar.zst
docker-restore           # wipes /var/lib/docker, then restores
```

Both archives land in the workspace, i.e. inside the mounted repo, so
`claude-home.tar.gz` and `docker-data.tar.zst` are ignored globally via
`~/.config/git/ignore`.

Both commands stop the daemon first and start it again afterwards (a data root copied
from under a live dockerd has inconsistent metadata), so running containers do
not survive. The archive keeps hardlinks and, importantly, the
`trusted.overlay.*` xattrs that mark opaque directories — without them files
deleted in an upper layer reappear. Restoring into a newer Docker is fine;
downgrading is not.

## Git state in every prompt

`git-context` is a `UserPromptSubmit` hook (`bin/git-context`, wired up in
`claude-settings.json`) that keeps Claude's picture of the repo current: every
prompt gets a `<git-state>` block with `git status --short --branch` and the
last five commits.

Outside a repo it prints nothing, and a status over 40 lines is truncated.

## Permission mode

Two things push a sandbox into bypass, and both have to be answered.

**The launch flag.** sbx generates `/usr/local/lib/sandbox/start-agent`, which
runs `exec 'claude' '--dangerously-skip-permissions' "$@"`. That is a bare
`claude`, so it resolves through `PATH`, and `bin/claude` — sitting earlier in
`PATH` than the real binary — strips the flag: `/proc/<pid>/cmdline` of the
agent sbx started reads `/home/agent/.local/bin/claude` with no arguments. Set
`SBX_ALLOW_BYPASS=1` to keep sbx's original behaviour.

**The seeded settings.** sbx's own `claude` kit writes `~/.claude/settings.json`
during create (the command is in `sbx.exe`, *"Seed Claude settings.json from
SBX_CRED_ANTHROPIC_MODE"*), and it asks for bypass outright:
`"permissions": { "defaultMode": "bypassPermissions" }`, plus the two consent
keys so nothing prompts. Dropping the flag alone therefore changes nothing.

`setup.install` in `kit/spec.yaml` answers that one: install commands finish
during create, before the CLI launches the first session, and a mixin's run
after the base agent's — so `init-config.py` merges `claude-settings.json` over
the seed while nothing has read it yet. `setup.startup` cannot, being fired from
a detached dispatcher that lands ~1.5 s after the agent has already started.

Two things help when this needs checking. `SBX_SHIM_DEBUG=1` makes the wrapper
log argv and the state of `~/.claude/settings.json` to `/tmp/claude-shim.log` at
the last moment before Claude reads it; and `/var/log/sbx-kit-startup.log`
saying `marker present, leaving config untouched` means the install pass had
already done the work.

Note that the permission *mode* is resolved once at session start, while `deny`
rules and hooks are re-read from `settings.json` as it changes — so a session
can be in the wrong mode and still enforce `Read(**/.env)`.

## Python venvs

A venv cannot live in the workspace at all. The workspace is a virtiofs
passthrough of a host folder and it refuses to create symlinks (EPERM), while
every venv needs them — uv symlinks the interpreter
([uv#2103](https://github.com/astral-sh/uv/issues/2103)), and even
`python -m venv --copies` still creates `lib64 -> lib`.

So `bin/uv` is a wrapper: it walks up from the current directory to the nearest
`pyproject.toml` and points `UV_PROJECT_ENVIRONMENT` at
`~/.venvs/<repo>-<hash>`, on the VM's native filesystem. Nothing to set up —
`uv sync` in any repo just works, and each repo gets its own environment.
Set `UV_PROJECT_ENVIRONMENT` or `VIRTUAL_ENV` yourself and the wrapper stays out
of the way; `SBX_VENV_DIR` moves where the venvs are kept.

The visible trade-off: **there is no `./.venv` in the repo** — use `uv run`. As a
side effect a Windows-side `.venv` in the same folder is never touched.

Why a wrapper rather than something simpler: uv takes this path only from the
environment variable (there is no `pyproject.toml` or `uv.toml` equivalent), and
one absolute value would be shared by every repo in the sandbox. Computing it per
invocation is also what makes it work in Claude's non-interactive Bash tool,
where a shell function or direnv would not fire.

Mounting a native directory onto `<repo>/.venv` was tried and does not work:
renaming any file in an ancestor directory invalidates the mountpoint's dentry
and the kernel detaches the mount (`d_invalidate` → `detach_mounts`), so a plain
`git restore` silently kills it, after which uv writes a broken half-venv into
the repo. Passthrough filesystems invalidate dentries because the host owns the
truth about names; on ext4 this never happens.

Recreating the sandbox wipes `~/.venvs` along with the rest of the writable
layer; `uv sync` rebuilds them.

## Notes

- `init-config.py` is idempotent (marker `~/.claude/.sbx-config-initialized`):
  config edits made inside the sandbox survive a restart.
- On startup sbx generates a `CLAUDE.md` next to the workspace; `init-config.py`
  removes it by signature (a hand-written CLAUDE.md is left untouched).
- `init-config.py` also sets `hasSeenAutoDefaultNudge` in `~/.claude.json`, which
  suppresses the "Make auto mode your default permission mode?" dialog while
  leaving auto mode itself available. It runs ahead of the marker check, because
  that state is answered-dialog state, not config, and a fresh VM starts without
  it.
- `${SANDBOX_NAME}` in the personal `CLAUDE.md` is replaced with the sandbox's
  name (`$SANDBOX_NAME`, else the hostname) while the file is laid out. It is
  the only placeholder, and it exists so the agent can offer
  `sbx policy allow network --sandbox <name> <host>` ready to paste.
- `CLAUDE_CODE_RESUME_THRESHOLD_MINUTES` (settings `env`) is set absurdly high
  to silence the dialog `/resume` shows on an old, large session — the one
  offering to `/compact` before continuing. It fires when the session is older
  than 70 minutes *and* over ~100k tokens
  (`CLAUDE_CODE_RESUME_TOKEN_THRESHOLD`), so raising either threshold is enough.
- Env vars are set via `ENV` in the Dockerfile so Claude's non-interactive Bash
  tool sees them too.
- Kit `startup` commands run on **every** sandbox start, not only at creation
  (verified). The kit itself, however, is captured at creation: editing
  `kit/spec.yaml` does not reach an existing sandbox until `sbx kit add`.
