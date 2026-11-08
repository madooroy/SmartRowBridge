import re
import unittest

from smartrow_bridge import addresses

DONGLE_A = "AA:BB:CC:11:22:33"
DONGLE_B = "AA:BB:CC:11:22:34"
MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


class AddressesTest(unittest.TestCase):
    def test_derived_addresses_are_static_random(self):
        for role in ("clone", "fitness"):
            address = addresses.derive(DONGLE_A, role)
            self.assertRegex(address, MAC_RE)
            self.assertEqual(int(address[:2], 16) & 0xC0, 0xC0)  # two top bits set

    def test_stable_per_pi_and_distinct(self):
        self.assertEqual(addresses.derive(DONGLE_A, "clone"), addresses.derive(DONGLE_A.lower(), "clone"))
        self.assertNotEqual(addresses.derive(DONGLE_A, "clone"), addresses.derive(DONGLE_A, "fitness"))
        self.assertNotEqual(addresses.derive(DONGLE_A, "clone"), addresses.derive(DONGLE_B, "clone"))

    def test_configured_value_wins(self):
        self.assertEqual(addresses.resolve("f2:53:52:4f:57:31", DONGLE_A, "clone"), "F2:53:52:4F:57:31")

    def test_fallback_without_adapter_mac(self):
        self.assertEqual(addresses.resolve(None, None, "clone"), addresses.FALLBACK_CLONE)
        self.assertEqual(addresses.resolve("", None, "fitness"), addresses.FALLBACK_FITNESS)
        self.assertNotEqual(addresses.FALLBACK_CLONE, addresses.FALLBACK_FITNESS)


if __name__ == "__main__":
    unittest.main()


class HeartRateSensorAddressTest(unittest.TestCase):
    def test_own_static_random_address(self):
        hr = addresses.derive(DONGLE_A, "heart_rate")
        self.assertEqual(int(hr[:2], 16) & 0xC0, 0xC0)
        self.assertNotIn(hr, (addresses.derive(DONGLE_A, "clone"), addresses.derive(DONGLE_A, "fitness")))
        self.assertEqual(addresses.resolve(None, None, "heart_rate"), addresses.FALLBACK_HEART_RATE)
        self.assertEqual(len({addresses.FALLBACK_CLONE, addresses.FALLBACK_FITNESS,
                              addresses.FALLBACK_HEART_RATE}), 3)
