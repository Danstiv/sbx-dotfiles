# Sourced by claude-backup and claude-restore.

# What init-config.py lays out from the image. These never travel in the
# archive: the image is their source of truth, and a restore that brought them
# back would silently undo a newer image's config. Keep in sync with
# init-config.py.
IMAGE_MANAGED=(
  .claude/.sbx-config-initialized
  .claude/settings.json
  .claude/settings.json.pre-sbx
  .claude/CLAUDE.md
  .claude/statusline-command.sh
)

# tar --exclude arguments for the list above.
managed_excludes() {
  local p
  for p in "${IMAGE_MANAGED[@]}"; do
    printf -- '--exclude=%s\n' "$p"
  done
}

# Where archives land by default: a directory in the working dir, which inside
# the sandbox IS the mounted workspace — the only thing that survives a recreate.
get_default_dir() {
  printf '%s' "$PWD/claude-backups"
}

# One timestamped archive per run, so a backup never lands on an earlier one.
# A second run within the same second would reuse the name, so wait for the
# clock instead of overwriting; a suffix would break the sort by name below.
build_archive_path() {
  local dir="${1%/}" path
  path="$dir/claude-home-$(date +%Y%m%d-%H%M%S).tar.gz"
  while [ -e "$path" ]; do
    sleep 1
    path="$dir/claude-home-$(date +%Y%m%d-%H%M%S).tar.gz"
  done
  printf '%s' "$path"
}

# Newest archive in a directory; the timestamp in the name sorts chronologically.
# Prints nothing if the directory holds none.
find_latest_archive() {
  local f latest=""
  for f in "${1%/}"/claude-home-*.tar.gz; do
    [ -f "$f" ] || continue
    if [ -z "$latest" ] || [[ "$f" > "$latest" ]]; then
      latest="$f"
    fi
  done
  printf '%s' "$latest"
}
