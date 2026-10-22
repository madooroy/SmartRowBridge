#!/usr/bin/env bash
# Install or update the SmartRow bridge on Raspberry Pi OS Lite (64-bit).
#   sudo bash install.sh
# Safe to run again after a "git pull": it replaces the code and restarts the service,
# and keeps your settings in /etc/default/smartrow-bridge.
set -euo pipefail

PREFIX=/opt/smartrow-bridge

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo bash install.sh" >&2
    exit 1
fi

# Work from the folder this script is in, wherever it was started from.
cd "$(dirname "$(readlink -f "$0")")"

apt-get update
apt-get install -y python3-venv python3-pip bluez
# The TP-Link UB500 (Realtek RTL8761BU) needs this firmware; usually already present.
apt-get install -y firmware-realtek || echo "firmware-realtek not installed (continuing)"
apt-get install -y rfkill || true

# A fresh Raspberry Pi OS image starts with Bluetooth soft-blocked.
rfkill unblock bluetooth || true

mkdir -p "$PREFIX"
rm -rf "$PREFIX/smartrow_bridge"
cp -r smartrow_bridge requirements.txt "$PREFIX"/
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --upgrade pip
"$PREFIX/venv/bin/pip" install -r "$PREFIX/requirements.txt"

if [[ ! -f /etc/default/smartrow-bridge ]]; then
    install -m 644 deploy/smartrow-bridge.env /etc/default/smartrow-bridge
fi
install -m 644 deploy/smartrow-bridge.service /etc/systemd/system/smartrow-bridge.service

systemctl daemon-reload
systemctl enable smartrow-bridge.service
systemctl restart smartrow-bridge.service

# Optional screen (tools/setup_display.sh): keep its libraries and files up to date.
if [[ -f /etc/systemd/system/smartrow-display.service ]]; then
    cp requirements-display.txt "$PREFIX"/
    "$PREFIX/venv/bin/pip" install -r "$PREFIX/requirements-display.txt"
    install -m 644 deploy/smartrow-display.service /etc/systemd/system/smartrow-display.service
    install -D -m 755 deploy/smartrow-display.shutdown /usr/lib/systemd/system-shutdown/smartrow-display.shutdown
    systemctl daemon-reload
    systemctl restart smartrow-display.service
fi

echo
echo "Installed. Wake the pulley (pull the handle), then check with:"
echo "  sudo bash tools/health_check.sh"
echo "Follow the log with: journalctl -u smartrow-bridge -f"
