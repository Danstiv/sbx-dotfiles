# Stop/start dockerd around an offline operation on /var/lib/docker.
# Sourced by docker-backup and docker-restore; not a command of its own.
#
# `service docker stop` does nothing here: sbx starts dockerd directly under
# tini, so the init script finds no pidfile of its own and reports "already
# stopped" while the daemon keeps running. Hence signals — and setsid on the way
# back, so the daemon detaches from this shell and is reparented to init, the
# way sbx started it.

DOCKER_LOG=/var/log/dockerd.log

stop_daemon() {
  if ! pgrep -x dockerd >/dev/null; then
    return 0
  fi
  echo "docker: stopping daemon..."
  sudo pkill -x -TERM dockerd || true

  local i
  for i in $(seq 1 30); do
    if ! pgrep -x dockerd >/dev/null; then
      return 0
    fi
    sleep 1
  done

  echo "docker: daemon still running after 30s, aborting" >&2
  return 1
}

# Runs as an EXIT trap, so it must preserve the script's exit status and must
# not fail under `set -e` when dockerd is already up.
start_daemon() {
  local rc=$? i
  if ! pgrep -x dockerd >/dev/null; then
    echo "docker: starting daemon..."
    sudo sh -c "setsid --fork dockerd >>$DOCKER_LOG 2>&1 </dev/null"
    for i in $(seq 1 30); do
      if docker info >/dev/null 2>&1; then
        break
      fi
      sleep 1
    done
  fi
  return "$rc"
}
