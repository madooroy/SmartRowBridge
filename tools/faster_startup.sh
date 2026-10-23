#!/usr/bin/env bash
# Optional: make the Pi start faster (about 7 s less from power-on to the bridge being
# visible on a Pi 4). It changes the Pi's boot settings, not the bridge.
#
#   sudo bash ~/smartrow-bridge/tools/faster_startup.sh           apply, then reboot
#   sudo bash ~/smartrow-bridge/tools/faster_startup.sh --undo    put everything back, then reboot
#
# What it changes:
#   1. /boot/firmware/config.txt  - no firmware pause or splash screen, no camera/display
#                                   probing, no initramfs (not needed to boot from an SD card).
#   2. /boot/firmware/cmdline.txt - adds cloud-init=disabled: the first-boot configuration
#                                   service no longer runs on every boot. Wi-Fi, user and SSH
#                                   stay as they are; the Pi just stops reading the set-up
#                                   files Raspberry Pi Imager left on the card.
#   3. Bootloader (Pi 4 only)     - NET_INSTALL_AT_POWER_ON=0: no wait for a keyboard at power-on.
#                                   This is stored on the Pi's board, not on the SD card.
#   4. /etc/initramfs-tools/conf.d/smartrow-bridge-speedup - MODULES=most. Started without an
#                                   initramfs, Linux names the system drive /dev/root, and
#                                   rebuilding the (now unused) initramfs then fails with "failed
#                                   to determine device for /". That rebuild is part of every
#                                   kernel or driver update, so those updates would fail too.
# The first time it runs it keeps the original config.txt next to the changed one as
# config.txt.before-speedup. Both files are on the SD card's boot partition, which any
# computer can read: if the Pi ever fails to start, put the card in a computer, copy
# config.txt.before-speedup back over config.txt, and delete the words cloud-init=disabled
# from the end of cmdline.txt.
set -euo pipefail

BOOT=${BOOT_DIR:-/boot/firmware}  # BOOT_DIR: try it on copies of the two files
CONFIG=$BOOT/config.txt
CMDLINE=$BOOT/cmdline.txt
INITRAMFS_CONF=${INITRAMFS_DIR:-/etc/initramfs-tools/conf.d}/smartrow-bridge-speedup   # INITRAMFS_DIR: for trying it out
SUFFIX=.before-speedup
UNDO=0
[[ ${1:-} == --undo ]] && UNDO=1

if [[ -z ${BOOT_DIR:-} && $EUID -ne 0 ]]; then
    echo "Run as root: sudo bash $0" >&2
    exit 1
fi
for f in "$CONFIG" "$CMDLINE"; do
    [[ -f $f ]] || { echo "$f not found - is this Raspberry Pi OS?" >&2; exit 1; }
done

# Set NET_INSTALL_AT_POWER_ON in the bootloader configuration (Raspberry Pi 4 only).
set_net_install() {
    local value=$1 current tmp
    [[ -z ${BOOT_DIR:-} ]] || return 0
    if ! grep -q "Raspberry Pi 4" /proc/device-tree/model 2>/dev/null || ! command -v rpi-eeprom-config >/dev/null; then
        echo "   bootloader: skipped (not a Raspberry Pi 4, or rpi-eeprom-config is missing)"
        return 0
    fi
    current=$(rpi-eeprom-config)
    if grep -q "^NET_INSTALL_AT_POWER_ON=$value\$" <<<"$current"; then
        echo "   bootloader: NET_INSTALL_AT_POWER_ON=$value already set"
        return 0
    fi
    tmp=$(mktemp)
    grep -v '^NET_INSTALL_AT_POWER_ON=' <<<"$current" >"$tmp" || true
    echo "NET_INSTALL_AT_POWER_ON=$value" >>"$tmp"
    rpi-eeprom-config --apply "$tmp" >/dev/null
    rm -f "$tmp"
    echo "   bootloader: NET_INSTALL_AT_POWER_ON=$value (written at the next reboot)"
}

if [[ $UNDO -eq 1 ]]; then
    echo "== Undoing the faster start-up settings =="
    if [[ -f $CONFIG$SUFFIX ]]; then
        cp "$CONFIG$SUFFIX" "$CONFIG"
        echo "   restored $CONFIG"
    else
        echo "   $CONFIG: no $SUFFIX copy, left as it is"
    fi
    if grep -qw 'cloud-init=disabled' "$CMDLINE"; then
        sed -i 's/[[:space:]]*cloud-init=disabled//g' "$CMDLINE"
        echo "   $CMDLINE: removed cloud-init=disabled"
    else
        echo "   $CMDLINE: cloud-init=disabled was not set"
    fi
    if [[ -f $INITRAMFS_CONF ]]; then
        rm -f "$INITRAMFS_CONF"
        echo "   initramfs: back to the default"
    fi
    set_net_install 1
    # The restored config.txt may predate the screen (tools/setup_display.sh): keep
    # its SPI line, or the screen goes blank after the reboot.
    SPI_LINE="dtoverlay=spi0-2cs,no_miso"
    if [[ -f ${DISPLAY_UNIT:-/etc/systemd/system/smartrow-display.service} ]] && ! grep -qx "$SPI_LINE" "$CONFIG"; then
        if grep -q '^dtparam=spi=on' "$CONFIG"; then
            sed -i "s/^dtparam=spi=on.*/$SPI_LINE/" "$CONFIG"
        else
            printf '\n# Screen on the Display HAT Mini (smartrow-bridge tools/setup_display.sh)\n[all]\n%s\n' \
                "$SPI_LINE" >>"$CONFIG"
        fi
        echo "   $CONFIG: kept $SPI_LINE for the screen"
    fi
    echo "Done. Reboot to apply: sudo reboot"
    exit 0
fi

echo "== Faster start-up =="
[[ -f $CONFIG$SUFFIX ]] || cp "$CONFIG" "$CONFIG$SUFFIX"

# config.txt: change a setting where it already is, otherwise add it under [all] at the end.
missing=()
for setting in camera_auto_detect=0 display_auto_detect=0 auto_initramfs=0 boot_delay=0 disable_splash=1; do
    key=${setting%%=*}
    if grep -q "^$key=" "$CONFIG"; then
        sed -i "s/^$key=.*/$setting/" "$CONFIG"
    else
        missing+=("$setting")
    fi
done
if [[ ${#missing[@]} -gt 0 ]]; then
    {
        echo
        echo "# Faster start-up (smartrow-bridge tools/faster_startup.sh)"
        echo "[all]"
        printf '%s\n' "${missing[@]}"
    } >>"$CONFIG"
fi
echo "   $CONFIG: camera_auto_detect=0 display_auto_detect=0 auto_initramfs=0 boot_delay=0 disable_splash=1"

# cmdline.txt is one line; the option goes at its end.
if grep -qw 'cloud-init=disabled' "$CMDLINE"; then
    echo "   $CMDLINE: cloud-init=disabled already set"
else
    sed -i '1 s/[[:space:]]*$/ cloud-init=disabled/' "$CMDLINE"
    echo "   $CMDLINE: added cloud-init=disabled"
fi

if [[ -d $(dirname "$INITRAMFS_CONF") ]]; then
    printf '%s\n' '# smartrow-bridge faster_startup.sh: the Pi starts without the initramfs (auto_initramfs=0),' \
        '# so build it without needing to know the system drive. Bigger, but not loaded.' 'MODULES=most' >"$INITRAMFS_CONF"
    echo "   initramfs: builds without looking up the system drive (keeps kernel updates working)"
fi

set_net_install 0

echo "Done. Reboot to apply: sudo reboot"
echo "Original kept as $CONFIG$SUFFIX"
