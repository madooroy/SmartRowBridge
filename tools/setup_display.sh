#!/usr/bin/env bash
# Optional: show the bridge's status on a Pimoroni Display HAT Mini (320 x 240 screen,
# four buttons, one RGB LED) fitted on the Pi's 40-pin header.
#
#   sudo bash ~/smartrow-bridge/tools/setup_display.sh          set it up
#   sudo bash ~/smartrow-bridge/tools/setup_display.sh --undo   remove it again
#
# Run install.sh first (after a "git pull"), so the installed code includes the screen.
# What it does:
#   1. switches on the Pi's SPI interface, which the screen uses, without its MISO line
#      (needs one reboot). The screen's data/command pin is GPIO 9, which is also SPI's
#      MISO (data-in) pin; the screen never sends data back. With the usual
#      dtparam=spi=on the kernel keeps GPIO 9 and the screen stays blank, so this uses
#      dtoverlay=spi0-2cs,no_miso instead (found on the same HAT in the PiRadioAux project);
#   2. installs a font and the screen's Python libraries into /opt/smartrow-bridge/venv
#      (one of them, spidev, is compiled on the Pi, hence the compiler);
#   3. installs and starts the smartrow-display service;
#   4. installs a small shutdown hook that draws "Safe to unplug" as the Pi stops.
# The bridge itself is not changed: the screen only reads its status file.
set -euo pipefail

PREFIX=/opt/smartrow-bridge
UNIT=/etc/systemd/system/smartrow-display.service
HOOK=/usr/lib/systemd/system-shutdown/smartrow-display.shutdown
CONFIG=/boot/firmware/config.txt
SPI_LINE="dtoverlay=spi0-2cs,no_miso"

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo bash $0" >&2
    exit 1
fi
cd "$(dirname "$(readlink -f "$0")")/.."

if [[ ${1:-} == --undo ]]; then
    echo "== Removing the screen =="
    systemctl disable --now smartrow-display.service 2>/dev/null || true
    rm -f "$UNIT" "$HOOK"
    systemctl daemon-reload
    echo "   service and shutdown hook removed"
    echo "   left in place: SPI ($SPI_LINE in $CONFIG) and the screen's Python libraries (both harmless)"
    exit 0
fi

if [[ ! -d $PREFIX/smartrow_bridge/display || ! -x $PREFIX/venv/bin/python ]]; then
    echo "The installed bridge has no screen code yet. First run:" >&2
    echo "  cd ~/smartrow-bridge && git pull && sudo bash install.sh" >&2
    exit 1
fi

echo "== 1. SPI interface (without MISO, so GPIO 9 is free for the screen) =="
reboot_needed=0
if grep -qx "$SPI_LINE" "$CONFIG"; then
    echo "   already set: $SPI_LINE"
elif grep -q '^dtparam=spi=on' "$CONFIG"; then
    sed -i "s/^dtparam=spi=on.*/$SPI_LINE/" "$CONFIG"
    echo "   changed dtparam=spi=on to $SPI_LINE (takes effect after a reboot)"
    reboot_needed=1
else
    printf '\n# Screen on the Display HAT Mini (smartrow-bridge tools/setup_display.sh)\n[all]\n%s\n' \
        "$SPI_LINE" >>"$CONFIG"
    echo "   switched on: $SPI_LINE (takes effect after a reboot)"
    reboot_needed=1
fi
[[ -e /dev/spidev0.1 ]] || reboot_needed=1

echo "== 2. Font and Python libraries =="
apt-get install -y fonts-dejavu-core build-essential python3-dev >/dev/null
"$PREFIX/venv/bin/pip" install -q -r requirements-display.txt
cp requirements-display.txt "$PREFIX/"
echo "   installed"

echo "== 3. Screen service =="
install -m 644 deploy/smartrow-display.service "$UNIT"
systemctl daemon-reload
systemctl enable smartrow-display.service >/dev/null 2>&1
if [[ $reboot_needed -eq 0 ]]; then
    systemctl restart smartrow-display.service
    echo "   enabled and started"
else
    echo "   enabled (starts after the reboot)"
fi

echo "== 4. Shutdown screen =="
install -m 755 deploy/smartrow-display.shutdown "$HOOK"
echo "   installed $HOOK"

echo
if [[ $reboot_needed -eq 1 ]]; then
    echo "Done. Reboot to switch SPI on: sudo reboot"
else
    echo "Done. The screen should show the bridge status now."
fi
echo "Log: journalctl -u smartrow-display -f"
