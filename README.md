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
        scripts/                  # baked into /opt/sbx
            init-config.py        # lays out ~/.claude on startup (idempotent)
            claude-settings.json.example # sample settings.json
            claude-settings.json  # your settings.json, deep-merged over the base (gitignored)
            statusline-command.sh # status line
            bashrc-extra.sh       # aliases -> ~/.bashrc
            claude-backup.sh      # snapshot ~/.claude to tar (alias claude-backup)
            claude-restore.sh     # restore a snapshot (alias claude-restore)
            claude-update.sh      # update Claude at start, bypassing the proxy (alias claude-update)
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
aliases are baked into the image:

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

## Notes

- `init-config.py` is idempotent (marker `~/.claude/.sbx-config-initialized`):
  config edits made inside the sandbox survive a restart.
- On startup sbx generates a `CLAUDE.md` next to the workspace; `init-config.py`
  removes it by signature (a hand-written CLAUDE.md is left untouched).
- Env vars are set via `ENV` in the Dockerfile so Claude's non-interactive Bash
  tool sees them too. `UV_PROJECT_ENVIRONMENT=/home/agent/.venv` puts the venv on
  the VM's native fs — the workspace mount is a virtiofs passthrough that can't
  create the interpreter symlink uv needs (EPERM).
- Claude's background auto-updater is off (`DISABLE_AUTOUPDATER=1`) — it can't
  reach `downloads.claude.ai` through the sandbox's MITM proxy (socket hang up).
  Instead `claude-update.sh` runs at each start (kit startup) with the proxy env
  stripped, so the updater uses transparent egress; run `claude-update` by hand
  anytime. Explicit `claude update` still works with the auto-updater disabled.
