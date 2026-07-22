#!/usr/bin/bash
set -euo pipefail

IMAGE="claude-code-custom"
TAR="${TMPDIR:-/tmp}/claude-code-custom.tar"
DIR="$(cd "$(dirname "$0")" && pwd)"
trap 'rm -f "$TAR"' EXIT

echo ">> docker build $IMAGE"
docker build -t "$IMAGE" "$DIR/template"

echo ">> docker image save -> $TAR"
docker image save "$IMAGE" -o "$TAR"

echo ">> sbx template load"
sbx template load "$TAR"

echo
echo "Done. Template loaded: $IMAGE"
echo "Run a sandbox with this template and kit:"
echo "  sbx run --template $IMAGE claude --kit \"$DIR/kit\""
