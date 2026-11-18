import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "wifi_watchdog.sh"


class WifiWatchdogTest(unittest.TestCase):
    """The watchdog with fake ip, ping and nmcli: reconnects after 2 failed minutes, quiet otherwise."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.bin = Path(self.dir.name, "bin")
        self.bin.mkdir()
        self.log = Path(self.dir.name, "calls")
        self.fake("ip", 'echo "default via 192.168.100.1 dev wlan0 proto dhcp"')
        self.fake("nmcli", f'echo "nmcli $*" >> {self.log}')
        self.ping_ok(True)

    def tearDown(self):
        self.dir.cleanup()

    def fake(self, name, body):
        path = self.bin / name
        path.write_text(f"#!/usr/bin/env bash\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def ping_ok(self, ok):
        self.fake("ping", "exit 0" if ok else "exit 1")

    def run_once(self):
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
               "SRB_WIFI_STATE": str(Path(self.dir.name, "state")), "SRB_WIFI_PAUSE_S": "0"}
        return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=True).stdout

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_router_answers_nothing_happens(self):
        self.assertEqual(self.run_once(), "")
        self.assertEqual(self.calls(), [])

    def test_reconnects_after_two_failed_minutes_and_reports_recovery(self):
        self.ping_ok(False)
        out = self.run_once()
        self.assertIn("not answering (1 minute(s)", out)
        self.assertEqual(self.calls(), [])  # one miss: wait
        out = self.run_once()
        self.assertIn("reconnecting wlan0", out)
        self.assertEqual(self.calls(), ["nmcli device disconnect wlan0", "nmcli device connect wlan0"])
        self.ping_ok(True)
        self.assertIn("answers again", self.run_once())
        self.assertEqual(self.run_once(), "")

    def test_not_on_wifi_does_nothing(self):
        self.fake("ip", "true")  # no default route through wlan0
        self.ping_ok(False)
        self.assertEqual(self.run_once(), "")
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
