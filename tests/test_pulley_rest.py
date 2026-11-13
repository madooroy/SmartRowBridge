import asyncio
import unittest
from unittest import mock

from smartrow_bridge import central


class FakeClient:
    def __init__(self):
        self.is_connected = True
        self.services = []
        self.disconnects = 0

    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False


class RestTest(unittest.TestCase):
    def run_link(self, idle_s, rest_after_s, seconds=0.3):
        link = central.PulleyLink("hci0", "AA:BB:CC:DD:EE:FF", "SmartRow", (), lambda *a: None)
        client = FakeClient()
        link._client = client
        link.rest_after_s = rest_after_s
        link.idle_s = lambda: idle_s
        scans = []

        async def scan():
            scans.append(1)
            await asyncio.Event().wait()  # the pulley is asleep: never found

        link._scan = scan

        async def go():
            task = asyncio.create_task(link.run())
            await asyncio.sleep(seconds)
            client.before_stop = client.disconnects  # (stopping the bridge lets go of it too)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        with mock.patch.object(central, "REST_CHECK_S", 0.02), mock.patch.object(central, "REST_HOLD_S", 0.1), \
                mock.patch.object(central, "RETRY_DELAY_S", 0.01):
            asyncio.run(go())
        return link, client, scans

    def test_an_idle_pulley_is_let_go_then_looked_for_after_the_hold(self):
        link, client, scans = self.run_link(idle_s=700, rest_after_s=600)
        self.assertEqual(client.before_stop, 1)
        self.assertTrue(link.resting)
        self.assertEqual(len(scans), 1)  # looked for again after REST_HOLD_S: a pull wakes it

    def test_kept_while_rowing_or_when_off(self):
        for idle, after in ((30, 600), (99999, 0)):
            link, client, scans = self.run_link(idle_s=idle, rest_after_s=after)
            self.assertEqual(client.before_stop, 0)
            self.assertFalse(link.resting)
            self.assertEqual(scans, [])


if __name__ == "__main__":
    unittest.main()
