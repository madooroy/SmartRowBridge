"""The per-device GATT views (gatt_views.py), over a real Bumble link: two centrals each
see, read and subscribe to only the services meant for them."""

import asyncio
import unittest

from bumble.controller import Controller
from bumble.core import UUID
from bumble.device import Device, Peer
from bumble.gatt import Characteristic, Service
from bumble.hci import Address
from bumble.host import Host
from bumble.link import LocalLink

from smartrow_bridge import gatt_views

FTMS, ROWER = UUID.from_16_bits(0x1826), UUID.from_16_bits(0x2AD1)
HRS, HRM = UUID.from_16_bits(0x180D), UUID.from_16_bits(0x2A37)


def make_device(link, name, address):
    controller = Controller(name, link=link)
    controller.random_address = Address(address)
    host = Host(controller, controller)
    return Device(name, address=Address(address), host=host)


class ViewsTest(unittest.TestCase):
    def test_each_central_sees_only_its_services(self):
        async def run():
            # the simulated controllers complain at teardown; not part of the test
            asyncio.get_running_loop().set_exception_handler(lambda loop, context: None)
            link = LocalLink()
            server = make_device(link, "server", "F0:F1:F2:F3:F4:F5")
            rower = Characteristic(ROWER, Characteristic.Properties.NOTIFY | Characteristic.Properties.READ,
                                   Characteristic.READABLE, b"row")
            hrm = Characteristic(HRM, Characteristic.Properties.NOTIFY | Characteristic.Properties.READ,
                                 Characteristic.READABLE, b"hr")
            ftms, hrs = Service(FTMS, [rower]), Service(HRS, [hrm])
            server.add_services([ftms, hrs])
            hr_client_address = "E0:E1:E2:E3:E4:E5"

            def hidden(bearer):
                peer = str(gatt_views.connection_of(bearer).peer_address).upper()
                return gatt_views.handles(ftms) if peer.startswith(hr_client_address) else gatt_views.handles(hrs)

            gatt_views.install(server.gatt_server, hidden)
            await server.power_on()
            await server.start_advertising(auto_restart=True)

            seen = {}
            for name, address in (("rower app", "D0:D1:D2:D3:D4:D5"), ("hr app", hr_client_address)):
                client = make_device(link, name, address)
                await client.power_on()
                connection = await client.connect(server.random_address)
                peer = Peer(connection)
                await peer.discover_services()
                uuids = {s.uuid for s in peer.services}
                # reading a hidden handle by number fails too
                other = hrm if name == "rower app" else rower
                try:
                    await peer.read_value(other.handle)
                    hidden_read = "readable"
                except Exception:  # noqa: BLE001
                    hidden_read = "refused"
                seen[name] = (uuids, hidden_read)
                await connection.disconnect()
            return seen

        seen = asyncio.run(run())
        self.assertIn(FTMS, seen["rower app"][0])
        self.assertNotIn(HRS, seen["rower app"][0])
        self.assertIn(HRS, seen["hr app"][0])
        self.assertNotIn(FTMS, seen["hr app"][0])
        self.assertEqual(seen["rower app"][1], "refused")
        self.assertEqual(seen["hr app"][1], "refused")


if __name__ == "__main__":
    unittest.main()
