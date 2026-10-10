# smartrow-bridge

Row on a **SmartRow** with the **Peloton app** - and keep using the SmartRow app at the same time.

The SmartRow pulley only talks to the SmartRow app. The app can broadcast your rowing to a list of other
apps, but Peloton is not one of them: the Peloton app can connect to that broadcast, but it reads the data
wrongly (the distance rowed, for example, climbs far too fast). This bridge fills that gap: a Raspberry Pi
holds the only Bluetooth connection the pulley allows and re-publishes it as two devices:

- **`PiRower`** - a standard Bluetooth fitness machine (FTMS rower), which the Peloton app can connect to;
- **`SmartRow`** - a byte-for-byte copy of your pulley, for the SmartRow app (force curves and all).

Both apps record the same strokes. Switch the Pi on, pull the handle, open your apps.

**Do you need this?** Only if the app you want is Peloton, or another app that accepts a standard Bluetooth
rower but that the SmartRow app cannot broadcast to. At the time of writing the SmartRow app broadcasts to
EXR, Kinomap, Zwift, Bitgym, Regatta, ImersU, Holofit and ErgZone
([SmartRow's FAQ](https://smartrow.fit/faq-smartrow-app/) has the current list). If your app is on that
list, the SmartRow app already does the job without any extra hardware.

```
SmartRow pulley ──BLE──> Pi built-in radio (central, bleak/BlueZ)
                              │ raw notifications, byte for byte
                              ├──────────> "SmartRow" ──> tablet / phone running the SmartRow app
                              │ <── the app's commands forwarded to the pulley ──
                              └─ parse → watts / stroke rate / pace / distance / time
                                           "PiRower" ──> phone / tablet running Peloton (FTMS 0x2AD1)
USB Bluetooth dongle (peripheral, Bumble): two advertising sets, two connections, one GATT server
```

> **Unofficial.** This project is not affiliated with or endorsed by SmartRow, WaterRower, Peloton or
> anyone else. It was worked out from a pulley's Bluetooth traffic and may stop working after a firmware
> or app update. Use it at your own risk.

## Status

Working on the author's setup: SmartRow pulley firmware **V3.10**, Raspberry Pi 4 and TP-Link UB500, with

- the SmartRow app on an Android tablet (Lenovo), an iPhone and an iPad;
- the Peloton app on an Android tablet (Lenovo), an iPhone and an iPad.

Cold boot to both apps recording needs no interaction. Other firmware versions, dongles and apps are
untested - reports welcome.

Peloton shows stroke rate, pace and heart rate live, and output (watts, kJ), distance and stroke count in the
workout summary.

## What you need

| Part | Notes |
|---|---|
| Raspberry Pi 4 | Other models with built-in Bluetooth probably work; untested |
| microSD card | Raspberry Pi OS **Lite (64-bit)**, flashed with Raspberry Pi Imager; set up Wi-Fi and SSH there |
| USB Bluetooth dongle | TP-Link **UB500** (Realtek RTL8761BU). It must support LE extended advertising; other Bluetooth 5 dongles may work |
| SmartRow pulley | Firmware V3.10 tested |
| Two devices for the apps | The SmartRow app on one, the fitness app on another (or the same one) |

Why two radios: the built-in radio is the *client* of the pulley, and the dongle is driven directly by
[Bumble](https://github.com/google/bumble) to host the two virtual devices.

## Install

### 1. Prepare the Pi

Skip this if your Pi already runs Raspberry Pi OS Lite (64-bit) and you can reach it over SSH.

1. Flash **Raspberry Pi OS Lite (64-bit)** to the microSD card with
   [Raspberry Pi Imager](https://www.raspberrypi.com/software/). In its settings, set a user name and
   password, enter your Wi-Fi network, and enable SSH. Leave **Raspberry Pi Connect** off: it is Raspberry
   Pi's service for reaching a Pi from outside your home through their website, and the bridge does not use it.
2. Put the card in the Pi, plug in the USB Bluetooth dongle and power the Pi on. The first boot takes a
   minute or two.
3. Connect from your computer: `ssh <user>@<hostname>.local`, with the user and hostname you set in the Imager.

### 2. Install the bridge

On the Pi, over SSH, with the USB Bluetooth dongle plugged in:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/madooroy/smartrow-bridge.git ~/smartrow-bridge
cd ~/smartrow-bridge
sudo bash install.sh
```

The installer puts the code in `/opt/smartrow-bridge`, creates the `smartrow-bridge` service (starts on every
boot) and the settings file `/etc/default/smartrow-bridge`. No settings are required.

No reboot is needed: the bridge starts as soon as the installer finishes, and from then on at every boot.

### 3. Check it

Pull the handle to wake the pulley, then run:

```bash
sudo bash ~/smartrow-bridge/tools/health_check.sh
```

### 4. Pair the apps (once)

The SmartRow app has to be paired again, this time with the Pi's copy of your pulley instead of the pulley
itself. The copy has the same name and number as the real one, so it is easy to think nothing needs doing.

1. **SmartRow app**: forget (remove) the pulley you have paired now, then pair the one that appears as
   **`SmartRow-<number>`**. It shows the same number as before, because the bridge copies the pulley's ID,
   but it is the Pi you are pairing with. From then on it reconnects by itself.
2. **Peloton** (or another FTMS app): add the rower called **`PiRower`**.

The real pulley no longer shows up in the apps while the bridge is running: the bridge is holding its
connection, so the only `SmartRow-<number>` you can see is the Pi's copy.

## Everyday use

1. Power on the Pi and pull the handle straight away. The virtual devices appear once the pulley has
   connected, about 20 s after power-on.
2. Open the SmartRow app and/or your fitness app. Row.
3. Shut the Pi down before cutting power (`sudo shutdown -h now`, wait for the green LED to go dark).
   To do that with one tap on an iPhone, see [switching the Pi off from an iPhone](#optional-switch-the-pi-off-from-an-iphone).

Updating: `cd ~/smartrow-bridge && git pull && sudo bash install.sh`.

Pulley firmware updates still come from the SmartRow app talking to the real pulley: stop the bridge first
(`sudo systemctl stop smartrow-bridge`), update, start it again.

## Checking and troubleshooting

```bash
sudo bash ~/smartrow-bridge/tools/health_check.sh
```

It is read-only and checks the service, the installed code, the settings, both radios, the last rowing
session (pulley data received → forwarded to the SmartRow app → FTMS sent to the fitness app), warnings, and
the SD card and power supply. It works after a reboot too: the bridge keeps a small record of each session in
`/var/lib/smartrow-bridge/sessions.log`.

| Tool | Use |
|---|---|
| `tools/health_check.sh` | Everything above, in one screen |
| `tools/check_adapters.py` | Lists both radios (MAC, type, USB ID) and scans with each |
| `tools/pulley_rssi.py` | Live signal strength of the pulley link. Aim for −75 dBm or better; move the Pi closer if not |
| `tools/sniff_pulley.py` | Connects to the pulley directly and prints its services and raw packets (stop the bridge first) |
| `tools/setup_shutdown_shortcut.sh` | Lets an iOS Shortcut shut the Pi down, and nothing else, see [switching the Pi off from an iPhone](#optional-switch-the-pi-off-from-an-iphone) |
| `tools/faster_startup.sh` | Optional boot-time tuning of the Pi, see [faster start-up](#optional-faster-start-up) |

Live log: `journalctl -u smartrow-bridge -f`. Raspberry Pi OS keeps the log in memory, so save it before
shutting down if you want to share it: `journalctl -u smartrow-bridge -b --no-pager > ~/bridge.log`.

Common problems:

- **Both radios "not powered" / nothing happens** - Bluetooth is soft-blocked on a fresh image:
  `sudo rfkill unblock bluetooth` (the installer does this).
- **The dongle is not found** - `lsusb` should list it; `dmesg | grep -i rtl` should show its firmware loading.
- **Apps cannot see `SmartRow` / `PiRower`** - they only appear after the pulley has connected. Pull the handle.
- **The SmartRow app does not reconnect after you replaced the pulley** - it is a different pulley ID; pair
  the new `SmartRow-<number>` once.

## Settings

`/etc/default/smartrow-bridge` - all optional. Restart after a change: `sudo systemctl restart smartrow-bridge`.

| Setting | Default | Meaning |
|---|---|---|
| `SRB_PULLEY_ADDRESS` | empty | Empty = the first device advertising the SmartRow service; a MAC pins one pulley (use where several SmartRows are in range) |
| `SRB_FITNESS_NAME` | `PiRower` | The name fitness apps see |
| `SRB_CLONE_NAME` | the pulley's name | The name the SmartRow app sees. Leave it empty: a test with `SmartRowPi` did not work with the SmartRow app |
| `SRB_CLONE_ADDRESS`, `SRB_FITNESS_ADDRESS` | derived from the dongle's MAC | Addresses of the two virtual devices: the same on every boot, unique per Pi |
| `SRB_ADVERTISE_PULLEY_ID` | `1` | Test only: `0` advertises the clone without the pulley ID (manufacturer data), as an iPhone would; change `SRB_CLONE_ADDRESS` too so the app sees a new device |
| `SRB_CENTRAL_MAC`, `SRB_PERIPHERAL_MAC` | empty | Empty = built-in radio for the pulley, USB dongle for the apps; set the radios' MACs to pin them |
| `SRB_DRIVE_WITHOUT_TABLET` | `1` | The bridge runs the pulley's start-up handshake itself when the SmartRow app is not connected, so the fitness app works alone |
| `SRB_SESSION_LOG` | `/var/lib/smartrow-bridge/sessions.log` | Session record that survives shutdown; `off` disables |
| `SRB_SNIFF_LOG` | empty | Append every raw pulley packet to this file |
| `SRB_GATT_LOG` | empty | `1` logs every GATT request the apps make to the virtual devices (verbose) |
| `SRB_LOG_LEVEL` | `INFO` | `DEBUG` adds a line per second |

## How it works

| Module | Role |
|---|---|
| `smartrow_bridge/central.py` | Finds and holds the pulley, copies its GATT table, reconnects, keeps writes in order |
| `smartrow_bridge/peripheral.py` | Bumble GATT server: pulley clone + FTMS rower, two advertising sets |
| `smartrow_bridge/protocol.py` | SmartRow record decoding, checksum, V3 KEYLOCK handshake |
| `smartrow_bridge/ftms.py` | FTMS Rower Data (0x2AD1), Feature, Control Point |
| `smartrow_bridge/bridge.py` | Wiring; runs the pulley handshake when the SmartRow app is absent; 1 Hz FTMS; data-flow log |
| `smartrow_bridge/adapters.py` | Picks the two radios, clears rfkill, hands the dongle to Bumble |
| `smartrow_bridge/addresses.py`, `advertising.py`, `session_log.py`, `config.py` | Virtual device addresses, advertising payloads, session record, settings |

- **Pass-through.** Every pulley notification is forwarded to the clone first, unchanged and in order; parsing
  works on a copy. The SmartRow app's writes go back to the pulley unchanged.
- **Who talks to the pulley.** The pulley streams only after an init command, a `$` poll every ~2 s and, on V3
  firmware, a KEYLOCK challenge-response. The SmartRow app does that through the clone. While the app is not
  connected, the bridge does it, so a fitness app works on its own.
- **Stopping.** After you stop, the pulley keeps repeating the last stroke's values; the bridge zeroes the live
  values when the pulley's "stopped" marker arrives or no new stroke comes for 6 s.

Tests (no Bluetooth needed): `python -m unittest discover -s tests -t .` - they replay a real 2½-minute capture
(`captures/sniff2.log`).

### SmartRow protocol notes (firmware V3.10)

Service `0x1234`; `0x1235` write without response; `0x1236` read + notify. The pulley advertises its ID as
manufacturer data under company id `0x1235` (the SmartRow app shows it as `SmartRow-<id>`).

What the SmartRow app needs from a clone (tested with the Android and iOS apps): the iOS app lists a device
only if that manufacturer data is advertised, and after connecting it uses the service only if `0x1234`,
`0x1235` and `0x1236` are declared as 16-bit UUIDs. The Android app accepts a clone without the manufacturer
data (it lists it as plain `SmartRow`) and with either UUID form.

Handshake: write `$\rV@\r` → version reply (`SmartRow 'V3.10'`) → write `#` → `KEYLOCK=…` → answer → records
stream at about 10 per second.

Records are 17 ASCII bytes: type, distance in metres (bytes 1-5, high nibble scrambled on V3), fields, hex
checksum of bytes 0-13 (bytes 14-15), CR.

| Record | Fields |
|---|---|
| `a` | elapsed time `MMMSS` [6:11] |
| `c` | instantaneous W [6:9], average W×10 [9:14] |
| `d` | strokes/min ×10 [6:9], stroke count [9:13] |
| `e` | instantaneous split `mss` [6:9], average split [9:12] |
| `f` | `!` at [11] = rowing stopped |
| `b`, `x`/`y`/`z` | work and stroke length, force curve (passed through, not translated) |

## Optional: switch the Pi off from an iPhone

The Pi should be shut down before its power is cut. This sets up a button on your iPhone or iPad that does
it with one tap, with no password and no SSH app. The phone gets a key that can do exactly one thing on the
Pi: shut it down.

1. **Create the shortcut.** In the **Shortcuts** app, tap **+**, add the action **Run Script Over SSH**, and
   fill it in:

   | Field | Value |
   |---|---|
   | Host | your Pi's name, e.g. `raspberrypi.local` |
   | Port | `22` |
   | User | your user on the Pi, e.g. `pi` |
   | Authentication | **SSH Key** |
   | Script | `sudo -n /usr/sbin/shutdown -h now` |

   Name the shortcut, e.g. **Pi Off**.

2. **Copy the phone's public key.** In the same action, tap the **SSH Key** row and choose **Copy Public
   Key**. Get that line of text to the computer you use for SSH - for example paste it into a note or an
   e-mail to yourself. It starts with `ssh-ed25519` or `ssh-rsa`. It is not a secret.

3. **Authorise it on the Pi.** Over SSH, as your normal user (not with `sudo`):

   ```bash
   bash ~/smartrow-bridge/tools/setup_shutdown_shortcut.sh
   ```

   Paste the public key when it asks. It should end with `OK: ... is allowed without a password`.

4. **Try it.** Tap the shortcut. The Pi's green LED blinks for a few seconds and then stays dark: now it is
   safe to cut the power. To have it on your home screen, use **Add to Home Screen** in the shortcut's
   menu.

What the script sets up on the Pi: a rule that lets your user run `shutdown -h now` without a password, and
an entry in `~/.ssh/authorized_keys` that forces the phone's key to run only that command - it cannot open a
shell or do anything else. To remove it again, delete that line from `~/.ssh/authorized_keys` and the file
`/etc/sudoers.d/010-<user>-shutdown`.

The setup belongs to the SD card: after re-flashing the card or moving to a new one, run step 3 again with
the same key. A second phone needs its own key: repeat all four steps on it.

## Optional: faster start-up

The bridge works without this. It shortens the time from power-on to the bridge being visible by about 7 s
on a Pi 4 (roughly 26 s to 19 s, measured with a Bluetooth scanner) by changing the Pi's own boot settings.

1. Apply the settings and reboot:

   ```bash
   sudo bash ~/smartrow-bridge/tools/faster_startup.sh
   sudo reboot
   ```

2. After the reboot, pull the handle, then check that the bridge is running as before:

   ```bash
   sudo bash ~/smartrow-bridge/tools/health_check.sh
   ```

To see the gain yourself, run this before step 1 and again after step 2. The number in square brackets is the
time in seconds from the start of Linux to the bridge being ready and looking for the pulley (about 7 s on a
Pi 4 with these settings). It only means something after a normal power-on or reboot, not in the session in
which you ran the installer.

```bash
journalctl -u smartrow-bridge -b -o short-monotonic | grep -m1 "Scanning"
```

What the script changes:

| Where | Change | Effect |
|---|---|---|
| `/boot/firmware/config.txt` | `boot_delay=0`, `disable_splash=1`, `camera_auto_detect=0`, `display_auto_detect=0` | No firmware pause, splash screen or probing for cameras and displays |
| `/boot/firmware/config.txt` | `auto_initramfs=0` | Skips loading the initramfs (not needed to boot from the SD card) |
| `/boot/firmware/cmdline.txt` | adds `cloud-init=disabled` | The first-boot configuration service no longer runs on every boot (about 2 s) |
| Bootloader (Pi 4 only) | `NET_INSTALL_AT_POWER_ON=0` | No wait for a keyboard at power-on. Stored on the Pi's board, not on the SD card |

Things to know:

- **Do it after the install works.** The Wi-Fi, user and SSH settings from Raspberry Pi Imager are applied
  by cloud-init on the first boot. Once that has happened they stay; with cloud-init off the Pi only stops
  reading the Imager's set-up files on the card, so editing those files later has no effect until you undo.
- **SSH may answer a little later than the bridge.** The bridge is ready about 10 s after Linux starts, the
  Wi-Fi about 10 s after that, and your computer may need longer still to find the Pi by name. That is the
  same with or without these settings.
- **A new camera or DSI display** would no longer be detected automatically.
- **Undo:** `sudo bash ~/smartrow-bridge/tools/faster_startup.sh --undo`, then reboot.
- **If the Pi does not start afterwards:** both files are on the SD card's boot partition, which any
  computer can read. Put the card in a computer, copy `config.txt.before-speedup` back over `config.txt`,
  and delete the words `cloud-init=disabled` from the end of `cmdline.txt`.

## Acknowledgements

The SmartRow protocol knowledge here - service and characteristic IDs, the init command, the record layout and
the V3 KEYLOCK handshake - comes from **[qdomyos-zwift](https://github.com/cagnulein/qdomyos-zwift)** by Roberto
Viola, in particular `src/devices/smartrowrower/smartrowrower.cpp`; `keylock_response()` is a Python port of its
`calculateSmartRowV3ChallengeResponse()`. If you want a rower (or bike, treadmill…) bridge that runs on your
phone, with far wider device support, use QZ.

The findings from this project (record checksum, elapsed-time format, stop marker) and a draft "virtual
SmartRow" profile were offered back to QZ; the patches are in `contrib/qz-virtual-smartrow/`.

Built on [bleak](https://github.com/hbldh/bleak) and [Bumble](https://github.com/google/bumble).

## Licence

GPL-3.0 - see [LICENSE](LICENSE). You may use, change and redistribute it; distributed versions must stay
under the same licence.
