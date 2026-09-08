#!/usr/bin/env python3
import json
import os
import shutil
import socket
import sys
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
SETTINGS = CLAUDE_DIR / "settings.json"
APP_STATE = Path.home() / ".claude.json"
MARKER = CLAUDE_DIR / ".sbx-config-initialized"
ASSETS = Path("/opt/sbx")
DESIRED_FILE = ASSETS / "claude-settings.json"
WORKDIR_FILE = Path.home() / ".sbx-workdir"
GUIDANCE_MARK = "This file provides context and guidance"
# git reads this path with no core.excludesFile setting.
GLOBAL_GITIGNORE = Path.home() / ".config" / "git" / "ignore"


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


def dismiss_auto_mode_nudge():
    # ~/.claude.json is answered-dialog state, wiped with the sandbox, so the
    # "Make auto mode your default permission mode?" dialog would come back on
    # every fresh VM. Pre-answering it leaves auto mode itself usable.
    state = {}
    if APP_STATE.exists():
        try:
            state = json.loads(APP_STATE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
    if state.get("hasSeenAutoDefaultNudge") is True:
        return
    state["hasSeenAutoDefaultNudge"] = True
    try:
        APP_STATE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def remove_generated_guidance():
    if not WORKDIR_FILE.exists():
        return
    workdir = Path(WORKDIR_FILE.read_text(encoding="utf-8").strip())
    candidate = workdir.parent / "CLAUDE.md"
    try:
        if candidate.is_file() and GUIDANCE_MARK in candidate.read_text(
            encoding="utf-8", errors="ignore"
        ):
            candidate.unlink()
            print(f"[sbx-init] removed generated guidance {candidate}")
    except OSError:
        pass


def main():
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)

    remove_generated_guidance()
    dismiss_auto_mode_nudge()

    if MARKER.exists():
        print("[sbx-init] marker present, leaving config untouched")
        return 0

    install_asset("statusline-command.sh", CLAUDE_DIR / "statusline-command.sh", 0o755)

    GLOBAL_GITIGNORE.parent.mkdir(parents=True, exist_ok=True)
    install_asset("gitignore-global", GLOBAL_GITIGNORE)

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
