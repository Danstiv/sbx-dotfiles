#!/usr/bin/env python3
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
SETTINGS = CLAUDE_DIR / "settings.json"
APP_STATE = Path.home() / ".claude.json"
MARKER = CLAUDE_DIR / ".sbx-config-initialized"
ASSETS = Path("/opt/sbx")
DESIRED_FILE = ASSETS / "claude-settings.json"
WORKDIR_FILE = Path.home() / ".sbx-workdir"
# Where git looks when core.excludesFile is unset.
DEFAULT_EXCLUDES = Path.home() / ".config" / "git" / "ignore"


def deep_merge(base, overlay):
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def install_asset(name, dst, mode=None):
    shutil.copyfile(ASSETS / name, dst)
    if mode is not None:
        dst.chmod(mode)


def get_sandbox_name():
    return os.environ.get("SANDBOX_NAME") or socket.gethostname()


def install_claude_md(dst):
    # ${SANDBOX_NAME} is substituted here rather than left for the agent to
    # resolve: the name is fixed for the life of the sandbox, and `sbx policy
    # allow network --sandbox <name>` is only useful if the name in it is right.
    text = (ASSETS / "CLAUDE.md").read_text(encoding="utf-8")
    dst.write_text(text.replace("${SANDBOX_NAME}", get_sandbox_name()), encoding="utf-8")


def get_workdir():
    # Install commands see WORKSPACE_DIR; the startup dispatcher does not, but
    # by then setup.files has written ~/.sbx-workdir.
    workdir = os.environ.get("WORKSPACE_DIR")
    if not workdir and WORKDIR_FILE.exists():
        workdir = WORKDIR_FILE.read_text(encoding="utf-8").strip()
    return Path(workdir) if workdir else None


def seed_app_state():
    # ~/.claude.json is answered-dialog state, wiped with the sandbox. Without
    # these a fresh VM greets the first session with onboarding, the
    # "trust this folder?" prompt and the "make auto mode the default?" nudge.
    # The kit does not extend sbx's claude kit, whose install step used to
    # seed the first two.
    state = {}
    if APP_STATE.exists():
        try:
            state = json.loads(APP_STATE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
    desired = {"hasCompletedOnboarding": True, "hasSeenAutoDefaultNudge": True}
    trusted = {"/": {"hasTrustDialogAccepted": True}}
    workdir = get_workdir()
    if workdir is not None:
        trusted[str(workdir)] = {"hasTrustDialogAccepted": True}
    merged = deep_merge(json.loads(json.dumps(state)), {**desired, "projects": trusted})
    if merged == state:
        return
    try:
        APP_STATE.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"[sbx-init] could not write {APP_STATE}: {exc}", file=sys.stderr)


def install_global_gitignore():
    # sbx writes the sandbox's ~/.gitconfig itself and points its
    # core.excludesFile at a ~/.gitignore_global of its own (containing `.sbx`).
    # Once that key is set git never consults ~/.config/git/ignore, so the
    # entries go into the file git actually reads — appended, not replaced.
    result = subprocess.run(
        ["git", "config", "--path", "--get", "core.excludesFile"],
        capture_output=True, text=True,
    )
    target = Path(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else DEFAULT_EXCLUDES
    wanted = [
        line for line in (ASSETS / "gitignore-global").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    existing = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    missing = [line for line in wanted if line not in existing]
    if not missing:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as f:
        if existing and existing[-1] != "":
            f.write("\n")
        f.write("\n".join(missing) + "\n")
    print(f"[sbx-init] added {len(missing)} global gitignore entries to {target}")


def remove_generated_guidance():
    # sbx writes its own CLAUDE.md into the workspace's parent directory so
    # Claude picks it up from there. Inside the VM that directory is ephemeral
    # and only the workspace itself is mounted, so nothing else can be there.
    # Only the startup pass catches it: sbx writes it after the install
    # commands have run.
    workdir = get_workdir()
    if workdir is None:
        return
    candidate = workdir.parent / "CLAUDE.md"
    try:
        if candidate.is_file():
            candidate.unlink()
            print(f"[sbx-init] removed generated guidance {candidate}")
    except OSError:
        pass


def main():
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)

    remove_generated_guidance()
    seed_app_state()
    install_global_gitignore()

    if MARKER.exists():
        print("[sbx-init] marker present, leaving config untouched")
        return 0

    install_asset("statusline-command.sh", CLAUDE_DIR / "statusline-command.sh", 0o755)

    # CLAUDE.md is personal (gitignored) — install it only if one was baked in.
    if (ASSETS / "CLAUDE.md").is_file():
        install_claude_md(CLAUDE_DIR / "CLAUDE.md")

    # claude-settings.json is personal (gitignored) — merge it only if one was
    # baked in.
    if DESIRED_FILE.is_file():
        desired = json.loads(DESIRED_FILE.read_text(encoding="utf-8"))

        existing = {}
        if SETTINGS.exists():
            try:
                existing = json.loads(SETTINGS.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = {}
            else:
                shutil.copyfile(SETTINGS, SETTINGS.with_name(SETTINGS.name + ".pre-sbx"))

        merged = deep_merge(existing, desired)
        SETTINGS.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    MARKER.write_text("", encoding="utf-8")

    print("[sbx-init] applied settings.json, statusline, CLAUDE.md, global gitignore")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # A non-zero exit from a kit install command aborts sandbox creation, so
        # a typo in claude-settings.json must cost the config, not the sandbox.
        print(f"[sbx-init] failed: {exc!r}", file=sys.stderr)
        sys.exit(0)
