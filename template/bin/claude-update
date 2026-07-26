#!/usr/bin/env bash
# Update Claude Code from inside the sandbox.
#
# The sandbox routes all egress through a MITM proxy (gateway.docker.internal:3128).
# Claude's native updater can't fetch downloads.claude.ai through that proxy — it
# dies with "socket hang up" — so we strip the proxy env and let it use the
# sandbox's transparent egress instead (which reaches the same host fine).
#
# Runs at sandbox start (kit startup) and is also available as the `claude-update`
# alias. Wrapped in a timeout and always exits 0, so a slow download or an
# updater hang (anthropics/claude-code#55828, can fire when already up to date)
# never blocks startup.
set -u

timeout 120 env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy -u NODE_USE_ENV_PROXY \
  claude update
rc=$?

case "$rc" in
  0)   ;;
  124) echo "[claude-update] timed out after 120s, skipped" >&2 ;;
  *)   echo "[claude-update] failed (rc=$rc), continuing" >&2 ;;
esac
exit 0
