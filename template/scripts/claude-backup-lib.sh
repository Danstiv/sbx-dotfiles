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
