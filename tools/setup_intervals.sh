#!/usr/bin/env bash
# Optional: put each rowing session's coach summary on your intervals.icu calendar, as a note.
#
#   sudo bash ~/smartrow-bridge/tools/setup_intervals.sh          set it up (asks for your API key)
#   sudo bash ~/smartrow-bridge/tools/setup_intervals.sh --undo   stop sending, delete the key
#
# Your API key is on intervals.icu under Settings, Developer settings ("API key").
# It is saved in /etc/smartrow-bridge/intervals.key, readable by root only, and is never
# shown on screen or written to a log. Needs the interval coach on (SRB_COACH=1).
set -euo pipefail

PREFIX=/opt/smartrow-bridge
DIR=/etc/smartrow-bridge
KEY_FILE=$DIR/intervals.key
ENV_FILE=/etc/default/smartrow-bridge

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo bash $0" >&2
    exit 1
fi

if [[ ${1:-} == --undo ]]; then
    rm -f "$KEY_FILE"
    echo "API key deleted: no more notes are sent to intervals.icu."
    exit 0
fi

if [[ ! -f $PREFIX/smartrow_bridge/intervals.py ]]; then
    echo "The installed bridge can't send to intervals.icu yet. First run:" >&2
    echo "  cd ~/smartrow-bridge && git pull && sudo bash install.sh" >&2
    exit 1
fi

echo "Paste your intervals.icu API key (Settings > Developer settings > API key)."
read -rsp "API key (hidden): " key
echo
key=$(tr -d '[:space:]' <<<"$key")
if [[ -z $key ]]; then
    echo "No key entered; nothing changed." >&2
    exit 1
fi

install -d -m 700 "$DIR"
( umask 077 && printf '%s\n' "$key" >"$KEY_FILE.new" )
mv "$KEY_FILE.new" "$KEY_FILE"
unset key

echo "Checking the key with intervals.icu..."
if (cd "$PREFIX" && "$PREFIX/venv/bin/python" -m smartrow_bridge.intervals --test --key-file "$KEY_FILE"); then
    echo
    echo "Done. After each session (two minutes after the last stroke) a note with the coach's summary"
    echo "appears on that day of your intervals.icu calendar. Sessions from the last week that were"
    echo "already saved are sent too, within a few minutes."
else
    echo
    echo "The key is saved, but the check failed (message above). Check the key and the Pi's internet"
    echo "connection, then run this again. Remove it with: sudo bash $0 --undo"
fi
if ! grep -qE '^SRB_COACH=(1|true|yes)' "$ENV_FILE" 2>/dev/null; then
    echo
    echo "Note: the interval coach is off, so there are no summaries to send. Add SRB_COACH=1 to $ENV_FILE"
    echo "and restart the bridge: sudo systemctl restart smartrow-bridge"
fi
