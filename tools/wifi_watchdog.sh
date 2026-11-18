#!/usr/bin/env bash
# Wi-Fi watchdog (smartrow-wifi-watchdog.timer, once a minute, as root).
#
# Why: the Pi has dropped off the network three times (3, 9 and 17 Nov 2026) while staying "connected"
# as far as it could tell: on 17 Nov the log shows no disconnect at all, just the router's regular
# radio-measurement requests stopping at 18:21, and nothing reached the Pi until it was restarted at
# 20:35. Wi-Fi power saving was already off. The mesh (Deco) seems to stop serving the Pi without telling
# it, so the Pi never reconnects by itself.
#
# What: ping the router. After FAILS_BEFORE_RECONNECT failed minutes in a row, reconnect Wi-Fi (and say so
# in the log); if that doesn't bring the router back, try again at the next failures. Nothing happens
# while the router answers, or when the Pi isn't on Wi-Fi (no default route through wlan0).
set -u
IFACE=${SRB_WIFI_IFACE:-wlan0}
STATE=${SRB_WIFI_STATE:-/run/smartrow-bridge/wifi-watchdog}
FAILS_BEFORE_RECONNECT=${SRB_WIFI_FAILS:-2}

gw=$(ip -4 route show default dev "$IFACE" 2>/dev/null | awk '{print $3; exit}')
if [[ -z "$gw" ]]; then
    exit 0  # not on Wi-Fi (or no address yet): NetworkManager is already working on it
fi
mkdir -p "$(dirname "$STATE")"
if ping -c 3 -W 2 -I "$IFACE" "$gw" >/dev/null 2>&1; then
    if [[ -s "$STATE" ]]; then
        echo "Wi-Fi watchdog: router $gw answers again"
    fi
    : > "$STATE"
    exit 0
fi
fails=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$STATE"
echo "Wi-Fi watchdog: router $gw not answering ($fails minute(s) in a row)"
if (( fails % FAILS_BEFORE_RECONNECT == 0 )); then
    echo "Wi-Fi watchdog: reconnecting $IFACE"
    nmcli device disconnect "$IFACE" >/dev/null 2>&1
    sleep "${SRB_WIFI_PAUSE_S:-3}"
    nmcli device connect "$IFACE" || echo "Wi-Fi watchdog: reconnect failed; will try again"
fi
