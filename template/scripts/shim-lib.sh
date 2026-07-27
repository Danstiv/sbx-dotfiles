# Shared helpers for the wrappers in /opt/sbx/bin. Sourced, never executed.

# Print the real binary a wrapper stands in for: the first executable on PATH
# with the same name that is not the wrapper itself. Pass the wrapper's $0.
resolve_real() {
  local self name dir candidate
  self=$(readlink -f "$1")
  name=$(basename "$1")

  local IFS=:
  for dir in $PATH; do
    candidate="$dir/$name"
    [ -x "$candidate" ] || continue
    [ "$(readlink -f "$candidate")" = "$self" ] && continue
    printf '%s' "$candidate"
    return 0
  done

  echo "$name: real binary not found on PATH (wrapper at $self)" >&2
  return 127
}
