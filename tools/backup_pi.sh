#!/usr/bin/env bash
# Back up what lives only on the Pi into one dated file, for copying to the laptop:
#
#   bash ~/smartrow-bridge/tools/backup_pi.sh            # -> ~/smartrow-backups/pi-backup-YYYY-MM-DD-HHMM.tar.gz
#   bash ~/smartrow-bridge/tools/backup_pi.sh --peloton  # also the Peloton class cache (~100+ MB; it can be
#                                                         # downloaded again, so it is left out by default)
#
# In it: the rows (/var/lib/smartrow-bridge: sessions, strokes, the sessions log, the Classes index),
# the bridge settings (/etc/default/smartrow-bridge), the Pi's own changes outside git (boot config,
# NetworkManager drop-ins, the service files), logs in your home folder, the Peloton reports, and a
# MANIFEST.txt saying what the code was (git commit, any local edits) and what is in the file.
#
# Never in it: the intervals.icu key and Strava tokens (/etc/smartrow-bridge, root-only), saved Wi-Fi
# passwords (/etc/NetworkManager/system-connections), SSH keys. Runs as you (no sudo): everything it
# copies is readable without root.
#
# Keeps the last 5 backups on the Pi. Copy to the laptop from the Claude Projects folder:
#   scp pi@192.168.100.106:"~/smartrow-backups/pi-backup-*.tar.gz" pi-backup\
set -euo pipefail

ROOT="${SRB_BACKUP_ROOT:-}"            # tests only: read everything under this folder instead of /
REPO="${SRB_BACKUP_REPO:-$HOME/smartrow-bridge}"
OUT_DIR="${SRB_BACKUP_DIR:-$HOME/smartrow-backups}"
KEEP=5
PELOTON=0
for arg in "$@"; do
    case "$arg" in
        --peloton) PELOTON=1 ;;
        -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

stamp=$(date +%Y-%m-%d-%H%M)
name="pi-backup-$stamp"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
dest="$work/$name"
mkdir -p "$dest"
missing=()

# copy SRC DEST_SUBDIR: copies a file or folder if present and readable, notes it otherwise.
copy() {
    local src="$ROOT$1" sub="$dest/$2"
    if [[ -e "$src" ]]; then
        mkdir -p "$sub"
        if ! cp -a "$src" "$sub/" 2>"$work/err"; then
            echo "  partly copied $1 ($(head -1 "$work/err"))" >&2
            missing+=("$1 (partly: some files not readable)")
        fi
    else
        missing+=("$1 (not on this Pi)")
    fi
}

echo "Backing up the Pi into $name ..."
copy /var/lib/smartrow-bridge data
copy /etc/default/smartrow-bridge etc/default
for f in /boot/firmware/config.txt /boot/firmware/cmdline.txt; do copy "$f" boot; done
for f in "$ROOT"/etc/NetworkManager/conf.d/*.conf; do [[ -e "$f" ]] && copy "${f#"$ROOT"}" etc/NetworkManager/conf.d; done
for f in "$ROOT"/etc/systemd/system/smartrow-*.service; do [[ -e "$f" ]] && copy "${f#"$ROOT"}" etc/systemd/system; done
shopt -s nullglob
logs=("$HOME"/*.log)
if (( ${#logs[@]} )); then mkdir -p "$dest/home"; cp -a "${logs[@]}" "$dest/home/"; fi
[[ -d "$REPO/peloton-report" ]] && { mkdir -p "$dest/repo"; cp -a "$REPO/peloton-report" "$dest/repo/"; }
if (( PELOTON )) && [[ -d "$REPO/peloton-cache" ]]; then mkdir -p "$dest/repo"; cp -a "$REPO/peloton-cache" "$dest/repo/"; fi

# Belt and braces: nothing secret may be in the file, whatever was copied above.
find "$dest" \( -path '*/smartrow-bridge/intervals.key' -o -name 'strava.json' -o -path '*/system-connections*' \
     -o -name 'id_*' -o -name '*.nmconnection' \) -prune -exec rm -rf {} +

{
    echo "PiRower backup $stamp from $(hostname)"
    echo
    if git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
        echo "Code: $(git -C "$REPO" log --oneline -1) on $(git -C "$REPO" branch --show-current)"
        changes=$(git -C "$REPO" status --porcelain --untracked-files=no)
        if [[ -n "$changes" ]]; then
            echo "Local edits not in git (also saved as repo/local-changes.diff):"
            echo "$changes"
            mkdir -p "$dest/repo"
            git -C "$REPO" diff > "$dest/repo/local-changes.diff"
        else
            echo "No local edits to the code."
        fi
    fi
    echo
    sessions=("$dest"/data/smartrow-bridge/sessions/*.json)
    rows=0
    for s in "${sessions[@]}"; do [[ "$s" == *.strokes.json ]] || rows=$((rows + 1)); done
    echo "Rows (session records): $rows"
    echo "Peloton class cache included: $([[ $PELOTON == 1 ]] && echo yes || echo 'no (--peloton to include)')"
    echo "Left out on purpose: /etc/smartrow-bridge (intervals.icu key, Strava tokens), saved Wi-Fi passwords, SSH keys."
    if (( ${#missing[@]} )); then
        echo "Not copied:"
        printf '  %s\n' "${missing[@]}"
    fi
    echo
    echo "Files:"
    (cd "$dest" && find . -type f | sort)
} > "$dest/MANIFEST.txt"

mkdir -p "$OUT_DIR"
tar -czf "$OUT_DIR/$name.tar.gz" -C "$work" "$name"
ls -1t "$OUT_DIR"/pi-backup-*.tar.gz | tail -n +$((KEEP + 1)) | xargs -r rm --
size=$(du -h "$OUT_DIR/$name.tar.gz" | cut -f1)
echo "Done: $OUT_DIR/$name.tar.gz ($size; rows: $rows). The last $KEEP backups are kept here."
(( ${#missing[@]} )) && printf 'Not copied: %s\n' "${missing[@]}"
echo "On the laptop, in the Claude Projects folder:"
echo "  scp pi@192.168.100.106:\"$OUT_DIR/$name.tar.gz\" pi-backup\\"
