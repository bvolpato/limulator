#!/usr/bin/env bash
set -Eeuo pipefail
group=$(awk -F: '$1 == "0" {print $3}' /proc/self/cgroup)
printf 'cgroup=%s\n' "$group"
for field in memory.high memory.max memory.swap.max cpu.max; do
  printf '%s=' "$field"
  cat "/sys/fs/cgroup$group/$field"
done
exec nice -n 10 timeout --signal=TERM --kill-after=2s 20s "$@"
