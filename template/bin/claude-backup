#!/usr/bin/env bash
# Snapshot ~/.claude into a tarball.
#
# The sandbox filesystem is ephemeral: only the workspace bind-mount survives a
# recreate. The default destination is the current directory, which inside the
# sandbox IS the mounted workspace — so the archive persists on the host. Pass an
# explicit path to put it elsewhere.
#
#   claude-backup             -> ./claude-home.tar.gz
#   claude-backup <path>      -> that path
set -euo pipefail

SRC="$HOME/.claude"
DEST="${1:-$PWD/claude-home.tar.gz}"

if [ ! -d "$SRC" ]; then
  echo "backup: $SRC does not exist, nothing to back up" >&2
  exit 1
fi

tar -czf "$DEST" -C "$HOME" .claude
echo "backup: $SRC -> $DEST ($(du -h "$DEST" | cut -f1))"
