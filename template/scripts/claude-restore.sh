#!/usr/bin/env bash
# Restore a ~/.claude snapshot made by claude-backup.sh.
#
# Extracts the archive over $HOME: files present in the archive overwrite their
# counterparts, anything not in the archive is left untouched (merge restore).
# For a pristine restore, wipe first:  rm -rf ~/.claude && claude-restore <file>
#
#   claude-restore            <- ./claude-home.tar.gz
#   claude-restore <path>     <- that path
set -euo pipefail

SRC="${1:-$PWD/claude-home.tar.gz}"

if [ ! -f "$SRC" ]; then
  echo "restore: archive $SRC not found" >&2
  exit 1
fi

tar -xzf "$SRC" -C "$HOME"
echo "restore: $SRC -> $HOME/.claude"
