# SmartRow to Peloton BLE Virtual Bridge Project
## System Architecture & Hardware
- **Host:** Raspberry Pi 4 running Raspberry Pi OS Lite (64-bit).
- **Adapter 0 (hci0):** Built-in Raspberry Pi Bluetooth radio. Acts exclusively
as a BLE Central Client.
- **Adapter 1 (hci1):** TP-Link UB500 USB Bluetooth dongle. Acts exclusively as
a Dual-Peripheral Server.
- **Client 1:** the SmartRow App (tested on a Lenovo M9 tablet, an iPhone and an iPad).
- **Client 2:** the Peloton App (tested on the same three devices).
## Bridge Logic & Translation
1. **hci0 (Central Client)** connects directly to the physical SmartRow Pulley.
2. **Locked Communication Bypass:** The raw notification data packets coming
from the SmartRow pulley must be passed along completely unchanged to **hci1**.
3. **hci1 (Peripheral Server 1)** advertises the exact custom Service UUID and
characteristics of the physical SmartRow pulley, creating a "Virtual Clone" for
the SmartRow App to bind to.
4. **Data Splitting & Translation:** Concurrently, the script must parse the raw
packet stream, calculate instantaneous wattage and stroke rate, and feed that
data into **Peripheral Server 2**.
5. **hci1 (Peripheral Server 2)** hosts standard Bluetooth Fitness Machine
Service (FTMS) protocols (specifically Rower Data characteristic `0x2AD1`),
allowing the Peloton App to see a generic smart rowing machine (named `PiRower`).
## Operational Constraints
- Target environment is headless Linux. The script must execute automatically on
boot via a systemd service.
- Python: `bleak` (on BlueZ) for the central client role, `bumble` for
peripheral/GATT hosting on the dongle.