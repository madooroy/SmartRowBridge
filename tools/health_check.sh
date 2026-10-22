#!/usr/bin/env bash
# SmartRow bridge health check - run on the Pi:
#   sudo bash ~/smartrow-bridge/tools/health_check.sh
# Read-only: it inspects the service, installed code, config, Bluetooth, logs and
# SD card / power health, and changes nothing.
set -u

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo bash $0" >&2
    exit 1
fi

PREFIX=/opt/smartrow-bridge
SERVICE=smartrow-bridge
ENV_FILE=/etc/default/smartrow-bridge
USER_HOME=$(getent passwd "${SUDO_USER:-pi}" | cut -d: -f6)
SRC="$USER_HOME/smartrow-bridge"

PASS=0; WARN=0; FAIL=0
ok()      { echo "  OK    $*"; PASS=$((PASS + 1)); }
warn()    { echo "  WARN  $*"; WARN=$((WARN + 1)); }
bad()     { echo "  FAIL  $*"; FAIL=$((FAIL + 1)); }
info()    { echo "        $*"; }
section() { echo; echo "== $* =="; }

section "Service"
if [[ $(systemctl is-enabled "$SERVICE" 2>/dev/null) == enabled ]]; then
    ok "starts on boot (enabled)"
else
    bad "not enabled - run: sudo systemctl enable $SERVICE"
fi
if [[ $(systemctl is-active "$SERVICE" 2>/dev/null) == active ]]; then
    ok "running since $(systemctl show -p ActiveEnterTimestamp --value "$SERVICE")"
else
    bad "not running - see: journalctl -u $SERVICE -b"
fi
restarts=$(systemctl show -p NRestarts --value "$SERVICE")
if [[ ${restarts:-0} -eq 0 ]]; then
    ok "no crash-restarts since boot"
else
    warn "$restarts automatic restart(s) since boot - the bridge crashed and recovered"
fi

section "Installed code ($PREFIX)"
if [[ -x $PREFIX/venv/bin/python ]]; then
    ok "Python environment present"
    versions=$("$PREFIX/venv/bin/python" -c \
        "from importlib.metadata import version as v; print('bleak', v('bleak'), '/ bumble', v('bumble'))" 2>&1) \
        && ok "libraries import: $versions" || bad "library check failed: $versions"
else
    bad "missing $PREFIX/venv - rerun install.sh"
fi
if [[ -d $SRC/smartrow_bridge ]]; then
    if diff -rq -x __pycache__ "$SRC/smartrow_bridge" "$PREFIX/smartrow_bridge" >/dev/null; then
        ok "installed code matches $SRC/smartrow_bridge"
    else
        warn "installed code differs from $SRC (copied but not installed?):"
        diff -rq -x __pycache__ "$SRC/smartrow_bridge" "$PREFIX/smartrow_bridge" | sed 's/^/          /'
    fi
fi
info "fingerprints (compare with the laptop copy):"
(cd "$PREFIX" && sha256sum smartrow_bridge/*.py | cut -c1-12,65- | sed 's/^/          /')

section "Configuration ($ENV_FILE)"
if [[ -f $ENV_FILE ]]; then
    grep -E '^SRB_[A-Z_]+=.+' "$ENV_FILE" | sed 's/^/          /'
    grep -q '^SRB_LOG_LEVEL=DEBUG' "$ENV_FILE" && warn "log level is DEBUG - set INFO for everyday use"
    if grep -qE '^SRB_CENTRAL_MAC=.+' "$ENV_FILE" && grep -qE '^SRB_PERIPHERAL_MAC=.+' "$ENV_FILE"; then
        ok "adapters pinned by MAC"
    else
        ok "adapters chosen by type (built-in radio for the pulley, USB dongle for the apps)"
    fi
else
    warn "$ENV_FILE missing - defaults in use"
fi

section "Bluetooth hardware"
if rfkill list bluetooth | grep -q 'Soft blocked: yes'; then
    bad "an adapter is rfkill-blocked - run: sudo rfkill unblock bluetooth"
else
    ok "no rfkill block"
fi
count=$(ls /sys/class/bluetooth | grep -cE '^hci[0-9]+$')
[[ $count -eq 2 ]] && ok "2 Bluetooth adapters present" || bad "expected 2 adapters, found $count"
if lsusb | grep -qiE '2357:0604|0bda:8771'; then
    ok "TP-Link UB500 on USB"
else
    bad "UB500 not found on USB"
fi

section "Bridge log (current run of the service)"
# Only this run: earlier runs in the same boot may predate the installed code.
invocation=$(systemctl show -p InvocationID --value "$SERVICE")
if [[ -n $invocation ]]; then
    log=$(journalctl --no-pager -o cat _SYSTEMD_INVOCATION_ID="$invocation")
else
    log=$(journalctl -u "$SERVICE" -b --no-pager -o cat)
fi
check_log() {  # pattern, description, fail|warn
    if grep -q "$1" <<<"$log"; then ok "$2"; else "$3" "$2 - not seen"; fi
}
check_log "Central -> hci" "adapters bound to the right radios" bad
# The virtual devices are built from the real pulley's GATT table, so they only
# start advertising once the pulley has connected.
if grep -q "Connected to pulley" <<<"$log"; then
    ok "pulley connected"
    check_log "for the SmartRow app" "SmartRow clone advertising" bad
    check_log "for the fitness apps" "fitness rower advertising" bad
else
    warn "pulley not connected yet - pull the handle to wake it, then rerun"
    info "(SmartRow and PiRower only start advertising after the pulley connects)"
fi

# Evidence comes from the current run if it has rowed; otherwise from the last run
# that did, in the on-disk session record (the journal itself is lost at shutdown).
SESSION_LOG=$(sed -n 's/^SRB_SESSION_LOG=//p' "$ENV_FILE" 2>/dev/null | tail -n 1)
SESSION_LOG=${SESSION_LOG:-/var/lib/smartrow-bridge/sessions.log}
evidence=$log
source_label="current run"
if ! grep -q 'Data flow' <<<"$log"; then
    record_files=()
    for f in "$SESSION_LOG.1" "$SESSION_LOG"; do [[ -f $f ]] && record_files+=("$f"); done
    if (( ${#record_files[@]} )); then
        # Last block (from one "---- bridge started" marker to the next) that saw data flow.
        last_session=$(awk '
            /^---- bridge started/ { if (has) last = blk; blk = ""; has = 0 }
            { blk = blk $0 "\n" }
            /Data flow/ { has = 1 }
            END { if (has) last = blk; printf "%s", last }' "${record_files[@]}")
        if [[ -n $last_session ]]; then
            evidence=$last_session
            started=$(head -n 1 <<<"$last_session" | awk '{print $4}')
            ended=$(grep 'Data flow' <<<"$last_session" | tail -n 1 | awk '{print $2}' | cut -c1-5)
            source_label="last recorded session: run started $started, last data ${ended:-?}"
        fi
    fi
fi

section "Rowing session evidence ($source_label)"
# Proof of the full chain: pulley -> bridge -> SmartRow app AND pulley -> FTMS -> Peloton.
if grep -q "SmartRow app subscribed" <<<"$evidence"; then
    ok "SmartRow app connected to the clone"
else
    warn "SmartRow app did not connect in this session"
fi
if grep -q "Rower Data subscribed" <<<"$evidence"; then
    ok "fitness app (Peloton) subscribed to Rower Data"
else
    warn "no fitness app subscribed to Rower Data in this session"
fi
flow=$(grep 'Data flow' <<<"$evidence")
if [[ -z $flow ]]; then
    warn "no rowing session recorded yet - row for at least a minute, then rerun"
else
    grep -qE '[1-9][0-9]* pulley packets in' <<<"$flow" \
        && ok "pulley data received" || bad "no pulley packets received"
    grep -qE '[1-9][0-9]* forwarded to SmartRow app' <<<"$flow" \
        && ok "raw pulley data forwarded to SmartRow app" || warn "nothing forwarded to SmartRow app yet"
    grep -qE '[1-9][0-9]* FTMS updates' <<<"$flow" \
        && ok "FTMS rower data sent to fitness app" || warn "no FTMS updates sent to a fitness app yet"
    info "latest: $(tail -n 1 <<<"$flow" | sed 's/.*Data flow, //')"
fi

section "Warnings and errors (current run)"
problems=$(grep -E 'WARNING|ERROR|Traceback' <<<"$log" | grep -v 'Ignoring spurious disconnect')
if [[ -z $problems ]]; then
    ok "no warnings or errors"
else
    warn "$(wc -l <<<"$problems") warning/error line(s); most recent:"
    tail -n 5 <<<"$problems" | sed 's/^/          /'
fi

if [[ -f /etc/systemd/system/smartrow-display.service ]]; then
    section "Screen (Display HAT Mini)"
    if [[ $(systemctl is-active smartrow-display 2>/dev/null) == active ]]; then
        ok "screen service running"
    else
        bad "screen service not running - see: journalctl -u smartrow-display -b"
    fi
    if ! grep -qx 'dtoverlay=spi0-2cs,no_miso' /boot/firmware/config.txt 2>/dev/null; then
        bad "SPI is not set up without MISO, so GPIO 9 is not free for the screen - rerun tools/setup_display.sh, then reboot"
    elif [[ -e /dev/spidev0.1 ]]; then
        ok "SPI on, without MISO (GPIO 9 free for the screen)"
    else
        bad "SPI is set up but not on yet - reboot once (sudo reboot)"
    fi
    STATUS_FILE=$(sed -n 's/^SRB_STATUS_FILE=//p' "$ENV_FILE" 2>/dev/null | tail -n 1)
    STATUS_FILE=${STATUS_FILE:-/run/smartrow-bridge/status.json}
    if [[ $STATUS_FILE == off ]]; then
        bad "SRB_STATUS_FILE=off - the screen has nothing to show"
    elif [[ -f $STATUS_FILE ]] && (( $(date +%s) - $(stat -c %Y "$STATUS_FILE") <= 5 )); then
        ok "bridge status file up to date ($STATUS_FILE)"
    else
        warn "bridge status file missing or old ($STATUS_FILE)"
    fi
fi

section "SD card and system"
root_dev=$(findmnt -no SOURCE /)
if findmnt -no OPTIONS / | grep -qE '(^|,)rw(,|$)'; then
    ok "root filesystem mounted read-write ($root_dev)"
else
    warn "root filesystem is read-only (overlay or errors?)"
fi
state=$(tune2fs -l "$root_dev" 2>/dev/null | sed -n 's/^Filesystem state: *//p')
if [[ $state == clean ]]; then
    ok "ext4 state: clean"
else
    bad "ext4 state: ${state:-unknown} - the card may need fsck"
fi
errors=$(dmesg | grep -iE 'EXT4-fs (error|warning)|mmc[0-9].*(error|timeout)|I/O error' | tail -n 3)
[[ -z $errors ]] && ok "no SD card / filesystem errors in kernel log" || { bad "kernel reports storage errors:"; info "$errors"; }
used=$(df --output=pcent / | tail -n 1 | tr -dc '0-9')
[[ $used -lt 85 ]] && ok "disk ${used}% used" || warn "disk ${used}% used"

if command -v vcgencmd >/dev/null; then
    throttled=$(vcgencmd get_throttled | cut -d= -f2)
    if [[ $throttled == 0x0 ]]; then
        ok "power supply: no under-voltage or throttling since boot"
    else
        t=$((throttled))
        (( t & 0x1 ))     && bad "UNDER-VOLTAGE NOW - use the official 5.1 V / 3 A supply"
        (( t & 0x10000 )) && warn "under-voltage occurred since boot (flaky supply or cable)"
        (( t & 0x40000 )) && warn "CPU was throttled since boot"
        (( t & 0x80000 )) && warn "soft temperature limit reached since boot"
    fi
    info "$(vcgencmd measure_temp)"
fi
# The directory can exist while journald is configured Storage=volatile (Raspberry Pi
# OS default, to spare the SD card), so look for actual journal files.
if find /var/log/journal -name '*.journal' -print -quit 2>/dev/null | grep -q .; then
    ok "logs persist across reboots"
else
    info "logs are kept in RAM only (Pi OS default): save them before powering off"
fi
if [[ $(timedatectl show -p NTPSynchronized --value) == yes ]]; then
    ok "clock synchronised"
else
    warn "clock not synchronised (no network yet?) - log timestamps may be off"
fi

echo
echo "Result: $PASS OK, $WARN warning(s), $FAIL failure(s)"
[[ $FAIL -eq 0 ]]
