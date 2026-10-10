**Title:** virtualrower: SmartRow pass-through profile (draft)

## Reference

Reference: requested by @cagnulein in a chat on 2026-10-03 ("can you create a virtual rower profile in qz to do this? Qz does already for ftms rowers and pm5 rowers"). It ports the pass-through from a SmartRow → SmartRow app + Peloton (FTMS) bridge I built on a Raspberry Pi.

## Changes

New setting **Virtual Rower as SmartRow** (`virtual_device_rower_smartrow`, off by default, shown under the PM5 switch). With a SmartRow pulley as the real device, the virtual rower also exposes the pulley's own `0x1234` service next to FTMS, so the SmartRow app on another device can connect through QZ.

Unlike the FTMS and PM5 profiles nothing is synthesised - it is a raw relay, which is what the SmartRow app needs (force curves included):

```
pulley 0x1236 notify ─> smartrowrower::characteristicChanged ─ smartRowRawData ─> virtualrower 0x1236 notify ─> SmartRow app
pulley 0x1235 write  <─ smartrowrower::virtualSmartRowWrite  <─ smartRowWrite ─── virtualrower 0x1235 write  <─ SmartRow app
```

- `virtualrower`: service `0x1234` with the pulley's layout (`0x1235` write-without-response, then `0x1236` read + notify).
- Advertising: name `SmartRow`, service `0x1234`, and the pulley's manufacturer data (company id `0x1235`) taken from `Rower->bluetoothDevice`. That is the pulley ID: the SmartRow app shows it as `SmartRow-<id>` and remembers the pulley by it. TX power and `0xFF00` are dropped in this mode to fit 31 bytes.
- Hand-over of the pulley dialogue: while the app is attached (subscribed to `0x1236`, or writing to `0x1235`), `smartrowrower` stops sending its own init, `$` poll, `#` and KEYLOCK answer - the app does all of that through the relay, and a second KEYLOCK answer breaks the handshake. When the app leaves, QZ re-initialises and polls again. Parsing continues throughout, so FTMS and the UI keep working.
- App writes are forwarded in one write as received, not through the one-byte-per-tick queue.
- Setting declared in `qzsettings.h/.cpp`, `settings.qml` (property appended at the end + switch) and `settings-catalog.json`; `tools/check_settings_property_order.py` and `tools/check_settings_catalog.py` pass.

### Not covered - why this is a draft

1. **Android**: advertising goes through `BleAdvertiser.startAdvertisingRower*`; a SmartRow variant (name, `0x1234`, manufacturer data) is still needed, so on Android the service is in the GATT table but not advertised.
2. **iOS native peripheral** (`lockscreen` / `virtualrower.swift`): untouched, so the profile is inert there. CoreBluetooth cannot advertise manufacturer data, so the SmartRow app would not see the pulley ID. Tested with my bridge: the iOS SmartRow app does not list a device without that manufacturer data (the Android app does, as plain `SmartRow`), so QZ on iOS cannot host the virtual pulley for the iOS SmartRow app. The iOS app also needs `0x1234`/`0x1235`/`0x1236` declared as 16-bit UUIDs, as this patch does.
3. **One central at a time** (as far as I know for the Qt peripheral): this gives "SmartRow app *or* an FTMS client". Both apps on the *same* phone/tablet may work, since they share one BLE link and the single advertisement carries `0x1826` and `0x1234` - untested. Both on separate devices at once needs a multi-central peripheral (my bridge uses two extended advertising sets with Bumble on Linux).
4. In this mode FTMS clients also see the device named `SmartRow`.
5. QZ and the SmartRow app must run on different devices.

## Testing

- **Not compiled or run in QZ.** I don't have a QZ build environment; this was written against the source and needs your review and a build. The two settings checks were run locally and pass.
- The behaviour it ports is proven in my Python bridge (bleak + Bumble on a Raspberry Pi) with a V3.10 pulley: SmartRow app on an Android tablet and Peloton on an iPhone recording the same session, including the hand-over of the init/poll/KEYLOCK dialogue.
- Suggested test once it builds (desktop/Linux first): enable both rower settings, connect to the pulley, check with nRF Connect that it advertises `SmartRow` + `0x1234` + manufacturer data, then pair `SmartRow-<id>` in the SmartRow app on another device. The log should show `SmartRow app attached to the virtual pulley: true`, the app's writes as `>> … // from SmartRow app`, and no QZ `noOp` polls while attached.
- This code was drafted with AI assistance (Claude) and reviewed by me. Please review it with that in mind.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
