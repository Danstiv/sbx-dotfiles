#!/usr/bin/env bash
# Give a repo a real ./.venv without putting the venv on the workspace mount.
#
# uv symlinks the interpreter into the venv it creates, and the workspace is a
# virtiofs passthrough of a host folder that refuses symlinks (EPERM "Operation
# not permitted"; uv bug astral-sh/uv#2103). So the venv is stored on the VM's
# native fs and bind-mounted onto <repo>/.venv, where every tool expects it.
#
# A bind mount is mount-namespace state: it dies when the sandbox stops, while
# its backing dir survives (that one lives on the writable layer). So each bind
# is recorded in a registry and replayed at start by --restore, which the kit
# runs as a startup command.
#
#   venv-bind                  bind ./.venv here, and remember it
#   venv-bind <dir>            bind <dir> instead (a repo root or a .venv path)
#   venv-bind --list           show registered binds and whether they are up
#   venv-bind --unbind <dir>   unmount and forget (the stored venv is kept)
#   venv-bind --restore        replay the registry (run at sandbox start)
#
# The backing dirs live in ~/.venvs and are wiped when the sandbox is recreated
# — venvs are rebuildable, `uv sync` brings them back.
set -u

REGISTRY="$HOME/.venv-binds"
STORE="$HOME/.venvs"

# A repo root is accepted as a convenience: bind the .venv inside it.
resolve_target() {
  local t
  t=$(realpath -m "${1:-$PWD}")
  [ "$(basename "$t")" = ".venv" ] || t="$t/.venv"
  printf '%s' "$t"
}

# Readable but collision-free: <repo-name>-<hash of the full path>.
backing_of() {
  local t=$1 name hash
  name=$(basename "$(dirname "$t")")
  hash=$(printf '%s' "$t" | sha1sum | cut -c1-12)
  printf '%s/%s-%s' "$STORE" "$name" "$hash"
}

remember() {
  local t=$1
  mkdir -p "$(dirname "$REGISTRY")"
  touch "$REGISTRY"
  grep -qxF "$t" "$REGISTRY" || printf '%s\n' "$t" >> "$REGISTRY"
}

forget() {
  local t=$1
  [ -f "$REGISTRY" ] || return 0
  grep -vxF "$t" "$REGISTRY" > "$REGISTRY.tmp" && mv "$REGISTRY.tmp" "$REGISTRY"
}

# Mount backing over target. Idempotent: an existing mount is left alone.
bind_one() {
  local t=$1 src
  src=$(backing_of "$t")
  mountpoint -q "$t" && { echo "already bound: $t"; return 0; }
  mkdir -p "$src" || { echo "cannot create backing $src" >&2; return 1; }
  # The mountpoint itself must exist on the workspace. Creating a plain dir
  # there is fine — only symlinks are refused.
  mkdir -p "$t" || { echo "cannot create mountpoint $t (workspace gone?)" >&2; return 1; }
  sudo mount --bind "$src" "$t" || { echo "mount failed: $t" >&2; return 1; }
  echo "bound: $t -> $src"
}

case "${1:-}" in
--list)
  [ -s "$REGISTRY" ] || { echo "no binds registered"; exit 0; }
  while IFS= read -r t; do
    [ -n "$t" ] || continue
    if mountpoint -q "$t"; then state="up"; else state="DOWN"; fi
    printf '%-5s %s -> %s\n' "$state" "$t" "$(backing_of "$t")"
  done < "$REGISTRY"
  ;;

--restore)
  # Runs at sandbox start. Never fail the boot over a venv.
  [ -s "$REGISTRY" ] || exit 0
  while IFS= read -r t; do
    [ -n "$t" ] || continue
    bind_one "$t" || echo "[venv-bind] skipped $t" >&2
  done < "$REGISTRY"
  exit 0
  ;;

--unbind)
  [ $# -ge 2 ] || { echo "usage: venv-bind --unbind <dir>" >&2; exit 2; }
  target=$(resolve_target "$2")
  mountpoint -q "$target" && sudo umount "$target"
  forget "$target"
  echo "unbound: $target (backing kept at $(backing_of "$target"))"
  ;;

-h|--help)
  sed -n '2,26p' "$0" | sed 's/^# \?//'
  ;;

*)
  target=$(resolve_target "${1:-}")
  bind_one "$target" || exit 1
  remember "$target"
  ;;
esac
