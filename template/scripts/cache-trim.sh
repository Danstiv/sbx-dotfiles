#!/usr/bin/env bash
# Background loop that keeps the VM's page cache small, so the memory goes back
# to the Windows host without anyone running free-mem.
#
# Every INTERVAL seconds, if the file cache exceeds KEEP_MIB, it asks the root
# cgroup to reclaim up to STEP_MIB. memory.reclaim follows the LRU, so the
# coldest pages go first and a hot working set survives; there is no swap, and
# swappiness=0 keeps anonymous memory out of it. Compaction afterwards turns the
# freed pages into the 2 MiB blocks the balloon's free page reporting returns.
#
# Runs as root, started by kit/spec.yaml; the flock makes a second start a no-op.
#
#   CACHE_TRIM_KEEP_MIB   cache left alone, default 1024
#   CACHE_TRIM_STEP_MIB   most reclaimed per round, default 512
#   CACHE_TRIM_INTERVAL   seconds between rounds, default 30
set -u

keep=${CACHE_TRIM_KEEP_MIB:-1024}
step=${CACHE_TRIM_STEP_MIB:-512}
interval=${CACHE_TRIM_INTERVAL:-30}

exec 9>/run/sbx-cache-trim.lock
flock -n 9 || exit 0

file_cache_mib() {
  awk '/^(Active|Inactive)\(file\):/ { kb += $2 } END { print int(kb / 1024) }' /proc/meminfo
}

while sleep "$interval"; do
  excess=$(( $(file_cache_mib) - keep ))
  [ "$excess" -gt 0 ] || continue
  [ "$excess" -le "$step" ] || excess=$step
  # Fails with EAGAIN when less than asked could be reclaimed; that is fine.
  echo "${excess}M swappiness=0" > /sys/fs/cgroup/memory.reclaim 2>/dev/null
  echo 1 > /proc/sys/vm/compact_memory
done
