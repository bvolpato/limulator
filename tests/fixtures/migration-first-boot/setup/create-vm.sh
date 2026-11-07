do_first_boot() {
  if [ "$DRY" = 1 ]; then
    dry env GFX="${GFX:-1}" WINDOW="${WINDOW:-1}" CID="$CID" "$QEMU_RUN" "$VM"
    dry "$T/vphoned.sh" POST /v1/rpc '{"method":"setup.status",...}'   # until data_migration_done
    dry "$T/vphoned.sh" POST /v1/rpc '{"method":"setup.skip","params":{"force":true},"id":1}'
    dry python3 "$T/auto-lock-never.py" "$VM" "$CID"
    [ "$SCALE" = 3 ] || dry python3 "$T/guest-dt-scale.py" "$CID" "$SCALE"
    [ "$TRIM" = 0 ] || dry python3 "$T/service-profile.py" "$CID" trimmed
    return 0
  fi
  log "  the first boot migrates iOS's data behind an Apple logo and progress bar (20+ min)"
  boot_vm
  local deadline=$((SECONDS + ${LIMULATOR_MIGRATION_TIMEOUT:-3600})) remaining
  while true; do
    remaining=$((deadline - SECONDS))
    [ "$remaining" -gt 0 ] || die "first-boot migration timed out; see $VM/serial.log"
    vrpc '{"method":"setup.status","params":{},"id":1}' "$((remaining < 120 ? remaining : 120))" 2>/dev/null |
      grep -q '"data_migration_done":true' && break
    qemu_alive || { qemu_exit_report; die "QEMU exited during migration; see $VM/serial.log"; }
    remaining=$((deadline - SECONDS))
    [ "$remaining" -gt 0 ] || die "first-boot migration timed out; see $VM/serial.log"
    sleep "$((remaining < 30 ? remaining : 30))"; printf '.' >&2
  done
  echo >&2
  vrpc '{"method":"setup.skip","params":{"force":true},"id":1}'; echo
  unlock_vm
  python3 "$T/auto-lock-never.py" "$VM" "$CID"
  local restart=0
  if [ "$SCALE" != 3 ]; then
    # iOS 27 also takes the scale from the guest's device tree: set it, then restart the guest
    # (vphoned shuts it down; QEMU exits by itself) so the new scale applies.
    python3 "$T/guest-dt-scale.py" "$CID" "$SCALE"
    restart=1
  fi
  if [ "$TRIM" = 1 ]; then
    # Takes effect at the next boot, the same restart.
    python3 "$T/service-profile.py" "$CID" trimmed ||
      warn "the trimmed profile was not fully applied (see above); the VM works without it"
    restart=1
  fi
  if [ "$restart" = 1 ]; then
    log "  restarting the guest so the changes apply"
    vrpc '{"method":"system.shutdown","params":{"force":true},"id":1}'; echo
    wait_vm_exit
    boot_vm
    unlock_vm
    if [ "$SCALE" = 1 ]; then python3 "$T/guest-1x-logo.py" "$CID"; fi
  fi
  if [ "$TRIM" = 1 ]; then
    log "  trimmed: this first boot is busy for about 10 minutes while widgets settle"
  fi
}
step booted do_first_boot
