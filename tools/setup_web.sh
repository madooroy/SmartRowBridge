#!/usr/bin/env bash
# Optional: a web page with the bridge's rowing data and interval coach, for any phone
# or tablet on the same Wi-Fi (for example next to the SmartRow app in split screen).
#
#   sudo bash ~/smartrow-bridge/tools/setup_web.sh          set it up
#   sudo bash ~/smartrow-bridge/tools/setup_web.sh --undo   remove it again
#
# Run install.sh first (after a "git pull"), so the installed code includes the page.
# It installs and starts the smartrow-web service, which serves the page on port
# SRB_WEB_PORT (8080) of the Pi, read-only. The bridge itself is not changed: the page
# only reads its status file. Standard library only, nothing extra to install.
set -euo pipefail

PREFIX=/opt/smartrow-bridge
UNIT=/etc/systemd/system/smartrow-web.service
ENV_FILE=/etc/default/smartrow-bridge

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo bash $0" >&2
    exit 1
fi
cd "$(dirname "$(readlink -f "$0")")/.."

if [[ ${1:-} == --undo ]]; then
    echo "== Removing the web page =="
    systemctl disable --now smartrow-web.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    echo "   service removed"
    exit 0
fi

if [[ ! -f $PREFIX/smartrow_bridge/web/index.html || ! -x $PREFIX/venv/bin/python ]]; then
    echo "The installed bridge has no web page yet. First run:" >&2
    echo "  cd ~/smartrow-bridge && git pull && sudo bash install.sh" >&2
    exit 1
fi

echo "== Web page service =="
install -m 644 deploy/smartrow-web.service "$UNIT"
systemctl daemon-reload
systemctl enable smartrow-web.service >/dev/null 2>&1
systemctl restart smartrow-web.service
echo "   enabled and started"

port=$(sed -n 's/^SRB_WEB_PORT=//p' "$ENV_FILE" 2>/dev/null | tail -n 1)
port=${port:-8080}
ip=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -m1 '\.' || true)
echo
echo "Done. On a phone or tablet on the same Wi-Fi, open:"
echo "  http://$(hostname).local:$port"
[[ -n $ip ]] && echo "  http://$ip:$port   (if the name above doesn't work)"
if ! grep -qE '^SRB_COACH=(1|true|yes)' "$ENV_FILE" 2>/dev/null; then
    echo
    echo "The interval coach is off. To see it on the page, add SRB_COACH=1 to $ENV_FILE"
    echo "and restart the bridge: sudo systemctl restart smartrow-bridge"
fi
echo "Log: journalctl -u smartrow-web -f"
