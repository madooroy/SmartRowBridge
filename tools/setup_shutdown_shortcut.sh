#!/usr/bin/env bash
# Let an iOS Shortcut ("Run Script over SSH") shut the Pi down cleanly - and nothing else.
#
# Run as your normal user (not with sudo):
#   bash ~/smartrow-bridge/tools/setup_shutdown_shortcut.sh
# and paste the public key the Shortcuts app generated when asked.
#
# It installs two narrowly scoped permissions:
#   1. /etc/sudoers.d/010-<user>-shutdown - your user may run exactly "shutdown -h now" without a password.
#   2. ~/.ssh/authorized_keys entry  - the phone's key is forced to run only that command
#      (no shell, no port forwarding), whatever the Shortcut asks for.
set -euo pipefail

if [[ $EUID -eq 0 ]]; then
    echo "Run as pi, without sudo (it asks for sudo itself where needed)." >&2
    exit 1
fi

SHUTDOWN=$(command -v shutdown || true)
[[ -n $SHUTDOWN ]] || SHUTDOWN=/usr/sbin/shutdown
SUDOERS=/etc/sudoers.d/010-${USER}-shutdown
RULE="$USER ALL=(root) NOPASSWD: $SHUTDOWN -h now"
FORCED="command=\"sudo -n $SHUTDOWN -h now\",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty"

echo "== 1. Passwordless sudo for '$SHUTDOWN -h now' only =="
tmp=$(mktemp)
echo "$RULE" >"$tmp"
if sudo visudo -cqf "$tmp"; then
    sudo install -m 0440 -o root -g root "$tmp" "$SUDOERS"
    echo "   installed $SUDOERS"
else
    echo "   sudoers rule failed validation; nothing installed" >&2
    rm -f "$tmp"
    exit 1
fi
rm -f "$tmp"

echo
echo "== 2. Authorise the iPhone's key for shutdown only =="
read -rp "Paste the public key from the Shortcuts app, then press Enter: " KEY
KEY=${KEY//$'\r'/}
KEY="${KEY#"${KEY%%[![:space:]]*}"}"  # trim leading whitespace
KEY="${KEY%"${KEY##*[![:space:]]}"}"  # trim trailing whitespace (comment may contain quotes)
if [[ ! $KEY =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp[0-9]+)\ [A-Za-z0-9+/=]+ ]]; then
    echo "   That doesn't look like an SSH public key (should start with ssh-ed25519 or ssh-rsa)." >&2
    exit 1
fi
install -d -m 700 "$HOME/.ssh"
touch "$HOME/.ssh/authorized_keys"
chmod 600 "$HOME/.ssh/authorized_keys"
key_body=$(awk '{print $2}' <<<"$KEY")
if grep -qF "$key_body" "$HOME/.ssh/authorized_keys"; then
    echo "   key already present; leaving authorized_keys unchanged"
else
    echo "$FORCED $KEY" >>"$HOME/.ssh/authorized_keys"
    echo "   added (restricted to shutdown)"
fi

echo
echo "== 3. Check (does not shut down) =="
# Ask root to list pi's rules ("sudo -n -l" as pi would itself need a password).
if sudo -l -U "$USER" | grep -qF "NOPASSWD: $SHUTDOWN -h now"; then
    echo "   OK: '$SHUTDOWN -h now' is allowed without a password"
else
    echo "   FAIL: sudo still wants a password for shutdown" >&2
    exit 1
fi
echo
echo "Done. In the Shortcut, set Script to:  sudo -n $SHUTDOWN -h now"
echo "(The Pi forces that command for this key anyway.)"
