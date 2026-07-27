# sbx-dotfiles

My personal config and image build for [Docker Sandbox](https://docs.docker.com/ai/sandboxes/)
(sbx). Everything here is currently about running Claude Code inside an sbx
microVM: it installs tools, applies `settings.json`, the status line and
`CLAUDE.md`, and wires up notification sounds through the host.

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
            claude-update         # update Claude, bypassing the proxy; also runs at start
        scripts/                  # -> /opt/sbx, plain data
            shim-lib.sh           # shared helper sourced by the wrappers
            init-config.py        # lays out ~/.claude on startup (idempotent)
            claude-settings.json.example # sample settings.json
            claude-settings.json  # your settings.json, deep-merged over the base (gitignored)
            statusline-command.sh # status line
            bashrc-extra.sh       # interactive-only shell bits -> ~/.bashrc
            CLAUDE.md.example     # sample global CLAUDE.md
            CLAUDE.md             # your personal CLAUDE.md (gitignored)
    kit/spec.yaml                 # startup: invokes /opt/sbx/init-config.py
    host/                         # RUNS ON WINDOWS
        notify-daemon.ps1         # HTTP -> plays .wav from ./sounds
        sounds/                   # stop.wav / prompt.wav (gitignored)
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

## Notification sounds (one-time, on Windows)

There is no audio inside the VM — the signal goes to the host over HTTP (the
sandbox blocks raw UDP/TCP at the network layer). A daemon runs on Windows and
plays the `.wav`; the `Stop` / `Notification` hooks poke it via
`host.docker.internal`.

```powershell
sbx policy allow network localhost:53999          # allow the port (once)
powershell -NoProfile -ExecutionPolicy Bypass -File host\notify-daemon.ps1
```

Sounds live in `host/sounds/`: `stop.wav` (the Stop event) and `prompt.wav`
(Notification). The daemon finds the folder next to itself. It is gitignored —
drop your own two files with those names there. The port is set at the top of
`notify-daemon.ps1`; for auto-start at logon use a scheduled task
(`Register-ScheduledTask ... -AtLogOn`, example in the script).

## Personal config

The global `CLAUDE.md` and `claude-settings.json` are per-person, so they are
gitignored. Copy the samples and edit them to taste:

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

## Permission mode

sbx launches the agent as `claude --dangerously-skip-permissions` — the flag is
baked into `sbx.exe`, it is not a sandbox setting, and it overrides
`permissions.defaultMode` in `settings.json`, so whatever you configure there is
ignored.

`bin/claude` is a wrapper that sits earlier in `PATH` than the real binary and
removes the flag, which hands the decision back to `settings.json`. Verified:
with `SBX_SHIM_DEBUG=1` the wrapper logs `argv: --dangerously-skip-permissions
--version`, i.e. sbx resolves `claude` through `PATH` and the interception
holds. Set `SBX_ALLOW_BYPASS=1` to keep sbx's original behaviour.

There is no supported setting for this: Claude Code has no managed-settings key
that disables bypass mode. The image's `CMD` is no help either — sbx replaces
the container command with a keep-alive (`tini -- sh -c 'sleep infinity'`) and
starts the agent separately, so `CMD` never runs. Verified by building with
`CMD ["claude", "--marker-from-cmd"]`: the marker never reached the wrapper.

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

The visible trade-off: **there is no `./.venv` in the repo**. Use `uv run`, or
activate explicitly with `source "$UV_PROJECT_ENVIRONMENT/bin/activate"`. As a
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
- Env vars are set via `ENV` in the Dockerfile so Claude's non-interactive Bash
  tool sees them too.
- Kit `startup` commands run on **every** sandbox start, not only at creation
  (verified) — which is what makes the update check work. The kit itself,
  however, is captured at creation: editing `kit/spec.yaml` does not reach an
  existing sandbox until `sbx kit add`.
- Claude's background auto-updater is off (`DISABLE_AUTOUPDATER=1`) — it can't
  reach `downloads.claude.ai` through the sandbox's MITM proxy (socket hang up).
  Instead `claude-update` runs at each start (kit startup) with the proxy env
  stripped, so the updater uses transparent egress; run `claude-update` by hand
  anytime. Explicit `claude update` still works with the auto-updater disabled.
