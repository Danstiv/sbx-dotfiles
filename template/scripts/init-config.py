#!/usr/bin/env python3
import json
import shutil
import sys
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
SETTINGS = CLAUDE_DIR / "settings.json"
MARKER = CLAUDE_DIR / ".sbx-config-initialized"
ASSETS = Path("/opt/sbx")
DESIRED_FILE = ASSETS / "claude-settings.json"
WORKDIR_FILE = Path.home() / ".sbx-workdir"
GUIDANCE_MARK = "This file provides context and guidance"


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

    if MARKER.exists():
        print("[sbx-init] marker present, leaving config untouched")
        return 0

    install_asset("statusline-command.sh", CLAUDE_DIR / "statusline-command.sh", 0o755)
    # CLAUDE.md is personal (gitignored) — install it only if one was baked in.
    if (ASSETS / "CLAUDE.md").is_file():
        install_asset("CLAUDE.md", CLAUDE_DIR / "CLAUDE.md")

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

    print("[sbx-init] applied settings.json, statusline, CLAUDE.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
