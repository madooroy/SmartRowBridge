# Draft: a "virtual SmartRow" profile for QZ's virtual rower

For Roberto Viola (cagnulein), from the SmartRow → SmartRow app + Peloton bridge project.

Two patches against `qdomyos-zwift` `master` at `7ebabb6c3bef` (fetched 2026-10-03). Both apply cleanly
with `git apply` from the repository root.

| Patch | What | Can be taken alone |
|---|---|---|
| `0001-virtualrower-SmartRow-pass-through-profile.patch` | New virtual rower profile: QZ exposes the pulley's own `0x1234` service so the SmartRow app can connect through QZ | yes |
| `0002-smartrowrower-V3.10-protocol-fixes.patch` | Parsing fixes for `smartrowrower.cpp`, verified against real V3.10 captures | yes |

**Status: untested draft.** It was written by reading the QZ sources; it has not been compiled or run.
The *behaviour* it implements is proven: the same pass-through runs in our Python bridge (bleak + Bumble on
a Raspberry Pi) with a V3.10 pulley, the SmartRow app on an Android tablet, and Peloton on an iPhone, both
apps recording the same session.

## 0001 - SmartRow pass-through profile

Unlike the FTMS and PM5 profiles, nothing is synthesised: the profile is a raw relay for a real SmartRow
pulley, which is what the SmartRow app needs (force curves and all).

```
pulley 0x1236 notify ──> smartrowrower::characteristicChanged ──emit smartRowRawData──> virtualrower 0x1236 notify ──> SmartRow app
pulley 0x1235 write  <── smartrowrower::virtualSmartRowWrite  <──emit smartRowWrite──── virtualrower 0x1235 write  <── SmartRow app
```

- New setting `virtual_device_rower_smartrow` (default off; ignored in PM5 mode and heart-only mode).
- `virtualrower` adds service `0x1234` next to FTMS, with the pulley's layout: `0x1235` write-without-response,
  then `0x1236` read + notify with a CCCD.
- Advertising: local name `SmartRow`, service `0x1234` added to the list, and the pulley's manufacturer data
  (company id `0x1235`, e.g. `"34"`) copied from `Rower->bluetoothDevice`. That manufacturer data is the pulley's
  ID: the SmartRow app shows it as `SmartRow-34` and remembers the pulley by it. To fit 31 bytes, the TX power
  level and `0xFF00` are dropped in this mode.
  The iOS SmartRow app lists a device only if this manufacturer data is advertised, and uses the service only
  if `0x1234`, `0x1235` and `0x1236` are declared as 16-bit UUIDs (as the patch does); the Android app accepts
  a device without the manufacturer data and with either UUID form.
- Hand-over of the pulley dialogue. While the app is attached (it subscribed to `0x1236`, or wrote to `0x1235`),
  QZ stops sending its own init, `$` polls, `#` and KEYLOCK answer: the app does all of that itself through the
  relay, and a second KEYLOCK answer or a competing init breaks the handshake. When the app leaves, QZ
  re-initialises and polls again. `smartrowrower` keeps parsing the notifications throughout, so FTMS, the UI
  and everything else in QZ keep working.
- App writes are forwarded in one write, exactly as received, not through the one-byte-per-200-ms queue QZ uses
  for its own commands.

### What 0001 does not cover

1. **The rest of the new setting.** Only `qzsettings.h/.cpp` are patched (appended at the end of `allSettings`,
   count 1011 → 1012). `settings.qml` and the other places a setting must be declared are not.
2. **Android advertising.** `virtualrower` advertises through `BleAdvertiser.startAdvertisingRower*` on Android.
   A SmartRow variant is needed there (name `SmartRow`, UUID `0x1234`, manufacturer data `0x1235` + pulley ID);
   the patch leaves the existing calls in place, so on Android the service is in the GATT table but not advertised.
3. **iOS native peripheral** (`ios_peloton_workaround` → `lockscreen`): not touched, so the profile is inert
   there. Note that CoreBluetooth only lets a peripheral advertise a local name and service UUIDs, not
   manufacturer data, so on iOS the SmartRow app would not see the pulley ID. Tested since with our bridge
   (`SRB_ADVERTISE_PULLEY_ID=0`): the iOS SmartRow app does not list a device without that manufacturer
   data, so a virtual pulley hosted by QZ on iOS cannot serve the iOS SmartRow app. The Android SmartRow
   app accepts it and lists it as plain `SmartRow`.
4. **One central at a time (the main design question).** As far as we know the Qt peripheral serves a
   single connection, so this patch gives "SmartRow app *or* an FTMS client", not both at once. Three ways
   it can be used:
   - **One app at a time** - what the patch gives.
   - **Both apps on the same phone/tablet** - they share one BLE link to QZ (the OS multiplexes GATT
     between apps), so a single connection is enough, and the single advertisement here already carries both
     `0x1826` and `0x1234`. Untested.
   - **Both apps on separate devices at the same time** - needs a multi-central peripheral. The radios and
     the OS stacks allow it (CoreBluetooth and Android's GATT server both accept several centrals; Android
     can also run several advertising sets), so this would go through QZ's native iOS/Android code rather
     than the Qt path. Our bridge does it on Linux with Bumble: two extended advertising sets on one
     controller (`SmartRow` with `0x1234` for the SmartRow app, a differently named FTMS rower for Peloton),
     two connections, one GATT database, plus the central link to the pulley on a second radio.
5. **The name.** In this mode FTMS clients also see the device as `SmartRow`.
6. The SmartRow app cannot connect to a virtual pulley hosted on the same phone/tablet; QZ and the SmartRow
   app need to be on different devices.

### Suggested test

1. Enable `virtual_device_rower` and `virtual_device_rower_smartrow`, connect QZ to the pulley (Linux/desktop
   build first, since Android/iOS need the items above).
2. `nRF Connect`: the device advertises as `SmartRow` with `0x1234` and manufacturer data `0x1235: <id>`.
3. SmartRow app on another device: pair `SmartRow-<id>`, start a session. In the QZ log: `SmartRow app attached
   to the virtual pulley: true`, the app's `24 0d 56 40 0d`, `23` and KEYLOCK answer as ` >> ... // from
   SmartRow app`, and no QZ `noOp` polls while attached.
4. Row: the app shows live data; QZ's own metrics keep updating.
5. Close the app: `attached: false`, QZ polls again and data keeps flowing.

## 0002 - protocol fixes (V3.10)

From a 2½-minute capture of 1476 records (easy, hard up to 264 W, steady, then stop).

| Record | Change | Evidence |
|---|---|---|
| all | Validate the checksum: bytes 14-15 are the hex of `sum(raw bytes 0..13) & 0xFF` (raw = before the V3 nibble unscrambling) | 1476/1476 records match |
| `a` | Elapsed time is `MMMSS` at `[6:11]`, not `HHMMSS`: the counter goes `00058` → `00101` → … → `00159` → `00200` | the current code reads 1:01 as 0h 10m 1s |
| `c` | `[6:9]` instantaneous W (unchanged); comment: `[9:14]` is average W ×10; `[6:9]` stays 0 until the first stroke completes | both match `W = 2.8 / (pace/500)^3` within ~2 % from 45 to 264 W |
| `e` | comment: average split is `m ss` at `[9:12]` | `318335` = 3:18 actual, 3:35 average |
| `f` | `'!'` at `[11]` means the flywheel has stopped → zero watts, speed (and cadence when it comes from the pulley) | after stopping, the pulley repeats the last `c`/`d`/`e` values twice a second indefinitely; without this QZ keeps reporting the last stroke (181 W for 30 s in the capture) |

Other things we observed, not in the patch:

- V3.10 handshake as implemented in QZ is confirmed (`KEYLOCK=CBFF31C4` → `ca63`,
  `KEYLOCK=D39F4FBF` → `9177`). A recently unlocked pulley streams again without a new KEYLOCK.
- The pulley accepts the 5-byte init in a single write as well as byte by byte.
- GATT layout: service `0x1234` at handle `0x0009`, `0x1235` (write without response) at `0x000A`, `0x1236`
  (read, notify) at `0x000C`, CCCD at `0x000E`. The pulley uses a random static address.
- Records arrive at about 10 per second; `x`/`y`/`z` are the force curve (not decoded by us, relayed untouched).

## Credit and licence

The SmartRow protocol knowledge in our bridge (service and characteristic IDs, init, record layout, the V3
KEYLOCK algorithm) comes from QZ's `smartrowrower.cpp`; our `keylock_response` is a Python port of
`calculateSmartRowV3ChallengeResponse`. These patches are offered under QZ's licence (GPL-3.0), and the bridge
will be published under GPL-3.0 with that credit.
