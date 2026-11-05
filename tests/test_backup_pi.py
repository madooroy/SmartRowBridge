import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "backup_pi.sh"


class BackupPiTest(unittest.TestCase):
    def test_backs_up_rows_and_settings_but_never_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, home = Path(tmp, "root"), Path(tmp, "home")
            files = {
                "var/lib/smartrow-bridge/sessions/2026-11-04T1007.json": "{}",
                "var/lib/smartrow-bridge/sessions/2026-11-04T1007.strokes.json": "[]",
                "var/lib/smartrow-bridge/sessions.log": "row",
                "var/lib/smartrow-bridge/peloton/classes.json": "{}",
                "etc/default/smartrow-bridge": "SRB_COACH=1",
                "etc/NetworkManager/conf.d/wifi-powersave-off.conf": "[connection]",
                "etc/NetworkManager/system-connections/home.nmconnection": "psk=secret",
                "etc/smartrow-bridge/intervals.key": "secret",
                "etc/smartrow-bridge/strava.json": "secret",
                "etc/systemd/system/smartrow-bridge.service": "[Unit]",
                "boot/firmware/config.txt": "dtoverlay=spi0-2cs",
            }
            for rel, text in files.items():
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text)
            (home / "smartrow-bridge" / "peloton-cache").mkdir(parents=True)
            (home / "smartrow-bridge" / "peloton-cache" / "catalogue.json").write_text("[]")
            (home / "smartrow-bridge" / "peloton-report").mkdir()
            (home / "smartrow-bridge" / "peloton-report" / "drills.csv").write_text("id")
            (home / "testrow-2026-11-04.log").write_text("log")
            (home / ".ssh").mkdir()
            (home / ".ssh" / "id_ed25519").write_text("secret")
            env = dict(os.environ, HOME=str(home), SRB_BACKUP_ROOT=str(root),
                       SRB_BACKUP_REPO=str(home / "smartrow-bridge"), SRB_BACKUP_DIR=str(home / "backups"))
            out = subprocess.run(["bash", str(SCRIPT), "--no-peloton"], env=env, capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("rows: 1", out.stdout)
            [archive] = list((home / "backups").glob("pi-backup-*.tar.gz"))
            with tarfile.open(archive) as t:
                names = t.getnames()
                manifest = t.extractfile(next(n for n in names if n.endswith("MANIFEST.txt"))).read().decode()
            joined = "\n".join(names)
            for wanted in ("sessions/2026-11-04T1007.strokes.json", "sessions.log", "peloton/classes.json",
                           "etc/default/smartrow-bridge", "conf.d/wifi-powersave-off.conf", "boot/config.txt",
                           "smartrow-bridge.service", "home/testrow-2026-11-04.log", "peloton-report/drills.csv"):
                self.assertIn(wanted, joined)
            for never in ("intervals.key", "strava.json", "nmconnection", "system-connections", "id_ed25519",
                          "peloton-cache"):
                self.assertNotIn(never, joined)
            self.assertIn("Rows (session records): 1", manifest)
            self.assertIn("Left out on purpose", manifest)
            # by default the class cache is in it (it takes a while to download again)
            subprocess.run(["bash", str(SCRIPT)], env=env, check=True, capture_output=True)
            newest = max((home / "backups").glob("pi-backup-*.tar.gz"), key=lambda p: p.stat().st_mtime)
            with tarfile.open(newest) as t:
                self.assertTrue(any("peloton-cache/catalogue.json" in n for n in t.getnames()))
