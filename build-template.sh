#!/usr/bin/bash
set -euo pipefail

IMAGE="claude-code-custom"
TAR="${TMPDIR:-/tmp}/claude-code-custom.tar"
DIR="$(cd "$(dirname "$0")" && pwd)"
trap 'rm -f "$TAR"' EXIT

echo ">> docker build $IMAGE"
# The stamp defeats docker's cache for the `claude update` layer, so every build
# ships the current release (see the Dockerfile).
docker build --build-arg "CLAUDE_UPDATE_STAMP=$(date +%s)" -t "$IMAGE" "$DIR/template"

echo ">> docker image save -> $TAR"
docker image save "$IMAGE" -o "$TAR"

echo ">> sbx template load"
sbx template load "$TAR"

echo
echo "Done. Template loaded: $IMAGE (referenced by kit/spec.yaml as sandbox.image)"
echo "Run a sandbox from the kit:"
echo "  sbx run \"$DIR/kit\""
