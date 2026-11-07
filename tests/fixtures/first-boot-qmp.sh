boot_vm() {
  local deadline=$((SECONDS + ${VPHONE_FIRST_BOOT_TIMEOUT:-600})) remaining
  if running_vm; then
    log "  this VM is already running (QEMU pid $QEMU_PID): waiting for vphoned"
  else
    local -a qmp_args=()
    if [ -n "${VPHONE_FIRST_BOOT_QMP:-}" ]; then
      qmp_args=(-qmp "unix:$VPHONE_FIRST_BOOT_QMP,server=on,wait=off")
    fi
    GFX=${GFX:-1} WINDOW=${WINDOW:-1} CID=$CID "$QEMU_RUN" "$VM" "${qmp_args[@]}" > "$VM/serial.log" 2>&1 &
    QEMU_PID=$!
    printf '%s\n' "$QEMU_PID" > "$QEMU_PIDFILE"
    log "  QEMU output → $VM/serial.log; waiting for vphoned"
  fi
  while true; do
    remaining=$((deadline - SECONDS))
    [ "$remaining" -gt 0 ] || die "vphoned did not become ready before the boot timeout; see $VM/serial.log"
    vphoned_up "$((remaining < 150 ? remaining : 150))" && break
    qemu_alive || { echo >&2; qemu_exit_report; die "QEMU exited; see $VM/serial.log"; }
    remaining=$((deadline - SECONDS))
    [ "$remaining" -gt 0 ] || die "vphoned did not become ready before the boot timeout; see $VM/serial.log"
    sleep "$((remaining < 10 ? remaining : 10))"; printf '.' >&2
  done
  echo >&2
}
