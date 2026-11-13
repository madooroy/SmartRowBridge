import asyncio
import struct
import unittest
from types import SimpleNamespace
from unittest import mock

from smartrow_bridge import advertising, config
from smartrow_bridge.peripheral import BridgePeripheral


def ad_structures(data: bytes) -> dict[int, bytes]:
    out, i = {}, 0
    while i < len(data):
        n = data[i]
        out[data[i + 1]] = data[i + 2:i + 1 + n]
        i += n + 1
    return out


class AdvertisingTest(unittest.TestCase):
    def test_listed_as_a_heart_rate_monitor(self):
        adv, scan_rsp = advertising.heart_rate("PiRower HR")
        self.assertLessEqual(len(adv), advertising.MAX_AD_LEN)
        self.assertLessEqual(len(scan_rsp), advertising.MAX_AD_LEN)
        ads = ad_structures(adv)
        self.assertEqual(ads[0x03], struct.pack("<H", 0x180D))  # Heart Rate service
        self.assertEqual(ads[0x19], struct.pack("<H", 0x0341))  # appearance: heart rate belt
        self.assertEqual(ads[0x09], b"PiRower HR")  # whole name in the advertisement
        self.assertNotIn(b"SmartRow", adv + scan_rsp)
        self.assertNotIn(struct.pack("<H", 0x1826), adv)  # not a rower


def peripheral(hr_sensor=("F2:53:52:4F:57:33", "PiRower HR")):
    return BridgePeripheral(transport="usb:0", clone_address="F2:53:52:4F:57:31", clone_name="SmartRow",
                            fitness_address="F2:53:52:4F:57:32", fitness_name="PiRower",
                            pulley=SimpleNamespace(services=[], manufacturer_data={}),
                            on_clone_write=lambda h, d: None, hr_sensor=hr_sensor)


class GattTest(unittest.TestCase):
    def test_heart_rate_service_only_when_asked(self):
        uuids = lambda p: [str(s.uuid).upper() for s in p._build_services()]
        self.assertTrue(any("180D" in u for u in uuids(peripheral())))
        self.assertFalse(any("180D" in u for u in uuids(peripheral(None))))

    def test_measurement_is_standard(self):
        p = peripheral()
        p._build_services()
        p.device = SimpleNamespace(notify_subscribers=mock.AsyncMock())
        asyncio.run(p.publish_heart_rate(112))
        char, value = p.device.notify_subscribers.call_args.args
        self.assertIs(char, p._hr_measurement)
        self.assertEqual(value, bytes([0x06, 112]))  # 8-bit bpm, sensor contact detected
        self.assertEqual(p.hr_updates_sent, 0)  # nobody subscribed yet
        p._hr_connections.add(0x19)
        asyncio.run(p.publish_heart_rate(113))
        self.assertEqual(p.hr_updates_sent, 1)
        p.device.notify_subscribers.reset_mock()
        asyncio.run(p.publish_heart_rate(0))  # no reading: nothing sent
        p.device.notify_subscribers.assert_not_called()


class SettingsTest(unittest.TestCase):
    def test_off_by_default_and_named(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            c = config.parse([])
        self.assertFalse(c.heart_rate_sensor)
        self.assertEqual(c.heart_rate_sensor_name, "PiRower HR")
        with mock.patch.dict("os.environ", {"SRB_HEART_RATE_SENSOR": "1"}, clear=True):
            self.assertTrue(config.parse([]).heart_rate_sensor)


if __name__ == "__main__":
    unittest.main()


class StrapSearchTest(unittest.TestCase):
    def test_never_takes_its_own_sensor_for_the_strap(self):
        from smartrow_bridge import heartrate
        own = SimpleNamespace(address="F2:53:52:4F:57:33", name="PiRower HR")
        strap = SimpleNamespace(address="CE:07:DE:2C:F1:D5", name="COROS HEART RATE A5DB5E")
        adv = lambda name: SimpleNamespace(local_name=name, service_uuids=[heartrate.HR_SERVICE_UUID])

        class Scanner:
            def __init__(self, cb, adapter):
                self.cb = cb

            async def __aenter__(self):
                self.cb(own, adv("PiRower HR"))  # the bridge's own sensor is seen first
                self.cb(strap, adv("COROS HEART RATE A5DB5E"))
                return self

            async def __aexit__(self, *a):
                return False

        async def find():
            link = heartrate.HeartRateLink("hci0", "auto", asyncio.Lock(), on_reading=lambda b: None,
                                           exclude_addresses=("f2:53:52:4f:57:33",))
            with mock.patch.object(heartrate, "BleakScanner", Scanner):
                return await link._find()

        self.assertIs(asyncio.run(find()), strap)


class ViewsByDeviceTest(unittest.TestCase):
    def test_hr_device_shows_only_heart_rate_and_the_others_never_do(self):
        from smartrow_bridge import gatt_views
        p = peripheral()
        services = p._build_services()
        handle = 1
        for s in services:  # number them as Bumble would
            s.handle = handle
            handle += 1 + 2 * len(s.characteristics)
            s.end_group_handle = handle - 1
        p.device = SimpleNamespace(gatt_server=object())
        with mock.patch.object(gatt_views, "install") as install:
            p._install_views(services)
        hidden = install.call_args.args[1]
        hr = next(s for s in services if "180D" in str(s.uuid).upper())
        ftms = next(s for s in services if "1826" in str(s.uuid).upper())
        conn = lambda own: SimpleNamespace(self_address=own, peer_address="5D:33:26:B2:E8:A6")
        to_hr, to_rower = hidden(conn("F2:53:52:4F:57:33/P")), hidden(conn("F2:53:52:4F:57:32/P"))
        self.assertTrue(gatt_views.handles(ftms) <= to_hr)
        self.assertFalse(gatt_views.handles(hr) & to_hr)
        self.assertEqual(to_rower, gatt_views.handles(hr))
        self.assertEqual(hidden(conn("F2:53:52:4F:57:31/P")), gatt_views.handles(hr))  # SmartRow app
