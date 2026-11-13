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

# System packages: only when one is missing (an update usually has nothing to install).
have() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }
if ! have python3-venv || ! have python3-pip || ! have bluez || ! have rfkill; then
    apt-get update
    apt-get install -y python3-venv python3-pip bluez
    # The TP-Link UB500 (Realtek RTL8761BU) needs this firmware; usually already present.
    apt-get install -y firmware-realtek || echo "firmware-realtek not installed (continuing)"
    apt-get install -y rfkill || true
fi

# A fresh Raspberry Pi OS image starts with Bluetooth soft-blocked.
rfkill unblock bluetooth || true

mkdir -p "$PREFIX"
rm -rf "$PREFIX/smartrow_bridge"
cp -r smartrow_bridge requirements.txt "$PREFIX"/
# Python libraries: only when a requirements file changed since the last install.
pip_install() {  # pip_install FILE
    local stamp="$PREFIX/venv/.installed-$(basename "$1")"
    if [[ -f "$stamp" ]] && cmp -s "$1" "$stamp"; then
        echo "$(basename "$1"): unchanged, skipping pip"
        return
    fi
    "$PREFIX/venv/bin/pip" install -r "$1"
    cp "$1" "$stamp"
}
if [[ ! -x "$PREFIX/venv/bin/pip" ]]; then
    python3 -m venv "$PREFIX/venv"
    "$PREFIX/venv/bin/pip" install --upgrade pip
fi
pip_install "$PREFIX/requirements.txt"

# The Peloton class index for the web page's Classes screen (tools/peloton_plans.py index): a folder the
# user who ran this owns, so the scanner writes it without sudo and the web page can read it.
install -d -m 755 -o "${SUDO_USER:-root}" /var/lib/smartrow-bridge/peloton
# Planned workouts (smartrow_bridge/workout.py): copy a FIT workout file in (scp, no sudo); the bridge
# moves it to done/ once it starts.
install -d -m 755 -o "${SUDO_USER:-root}" /var/lib/smartrow-bridge/workouts

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
    pip_install "$PREFIX/requirements-display.txt"
    install -m 644 deploy/smartrow-display.service /etc/systemd/system/smartrow-display.service
    install -D -m 755 deploy/smartrow-display.shutdown /usr/lib/systemd/system-shutdown/smartrow-display.shutdown
    systemctl daemon-reload
    systemctl restart smartrow-display.service
fi

# Optional web page (tools/setup_web.sh): keep its service file up to date.
if [[ -f /etc/systemd/system/smartrow-web.service ]]; then
    install -m 644 deploy/smartrow-web.service /etc/systemd/system/smartrow-web.service
    systemctl daemon-reload
    systemctl restart smartrow-web.service
fi

echo
echo "Installed. Wake the pulley (pull the handle), then check with:"
echo "  sudo bash tools/health_check.sh"
echo "Follow the log with: journalctl -u smartrow-bridge -f"
