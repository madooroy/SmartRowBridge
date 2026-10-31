#!/usr/bin/env bash
# Optional: upload each rowing session to Strava, with the coach's summary as its description.
#
#   sudo bash ~/smartrow-bridge/tools/setup_strava.sh          set it up
#   sudo bash ~/smartrow-bridge/tools/setup_strava.sh --test   check it
#   sudo bash ~/smartrow-bridge/tools/setup_strava.sh --undo   stop uploading, delete the authorization
#
# Needs your own Strava API application (Strava subscription required): create it at
# https://www.strava.com/settings/api with "Authorization Callback Domain" set to localhost.
# This asks for its Client ID and Client Secret, gives you a link to authorize uploads, and
# saves the result in /etc/smartrow-bridge/strava.json, readable by root only.
# Once it works, switch off the SmartRow app's own Strava link, or each session arrives twice.
set -euo pipefail

PREFIX=/opt/smartrow-bridge

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo bash $0" >&2
    exit 1
fi
if [[ ! -f $PREFIX/smartrow_bridge/strava.py ]]; then
    echo "The installed bridge can't upload to Strava yet. First run:" >&2
    echo "  cd ~/smartrow-bridge && git pull && sudo bash install.sh" >&2
    exit 1
fi

cd "$PREFIX"
case ${1:-} in
    --undo) exec "$PREFIX/venv/bin/python" -m smartrow_bridge.strava --forget ;;
    --test) exec "$PREFIX/venv/bin/python" -m smartrow_bridge.strava --test ;;
esac

"$PREFIX/venv/bin/python" -m smartrow_bridge.strava --setup
echo
echo "Sessions you row from now on are uploaded to Strava about seven minutes after the last stroke"
echo "(rowing again within five minutes continues the same session). To send a session already saved:"
echo "  cd $PREFIX && sudo venv/bin/python -m smartrow_bridge.uploads --status"
echo "  cd $PREFIX && sudo venv/bin/python -m smartrow_bridge.uploads --send ID --to strava"
