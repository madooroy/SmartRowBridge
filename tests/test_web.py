import csv
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from smartrow_bridge import status
from smartrow_bridge.protocol import RowingMetrics
from smartrow_bridge.web.__main__ import PAGE, serve, status_payload
from tests.test_coach import FULL, STEADY

ROOT = Path(__file__).resolve().parent.parent


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "status.json")
        self.sessions = Path(self.tmp.name) / "sessions"
        self.server = serve(0, self.path, host="127.0.0.1", session_dir=str(self.sessions))
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def get(self, route, method="GET"):
        with urllib.request.urlopen(urllib.request.Request(self.base + route, method=method), timeout=5) as r:
            return r.status, r.headers, r.read()

    def test_page(self):
        code, headers, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn(b"FINISH THE STROKE", body)
        self.assertEqual(body, PAGE.read_bytes())

    def test_status_with_age(self):
        st = status.build(status.CONNECTED, "SmartRow-07", True, False, "SmartRow", "PiRower", RowingMetrics(),
                          rowing=False, now=1000.0)
        status.write(self.path, st)
        payload = status_payload(self.path, now=1003.5)
        self.assertEqual(payload["age_s"], 3.5)
        self.assertEqual(payload["pulley"], "SmartRow-07")
        code, _, body = self.get("/status.json")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["stage"], "connected")

    def test_saved_sessions(self):
        _, _, body = self.get("/sessions.json")
        self.assertEqual(json.loads(body), [])  # nothing saved yet (no directory either)
        import tools.coach_replay as replay_tool
        export = Path(self.tmp.name) / "session.csv"
        write_export(export)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(replay_tool.main(["--save", str(self.sessions), str(export)]), 0)
        _, _, body = self.get("/sessions.json")
        listing = json.loads(body)
        self.assertEqual(len(listing), 1)
        self.assertEqual((listing[0]["intervals"], listing[0]["distance_m"], listing[0]["complete"]),
                         (1, 1010, True))
        code, _, body = self.get(f"/session/{listing[0]['id']}.json")
        record = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(record["intervals"][0]["power_w"], 152)
        self.assertEqual(len(record["intervals"][0]["curve"]), 24)
        self.assertTrue(record["findings"])
        for bad in ("/session/2026-01-01T0000.json", "/session/..%2Fstatus.json", "/session/x.json"):
            with self.assertRaises(urllib.error.HTTPError) as err:
                self.get(bad)
            self.assertEqual(err.exception.code, 404)

    def test_status_when_bridge_is_not_writing(self):
        self.assertEqual(status_payload(self.path), {"stage": None, "age_s": None})
        _, _, body = self.get("/status.json")
        self.assertEqual(json.loads(body), {"stage": None, "age_s": None})

    def test_app_files(self):
        code, headers, body = self.get("/sw.js")
        self.assertEqual((code, headers["Content-Type"]), (200, "text/javascript; charset=utf-8"))
        self.assertIn(b"fetch(event.request)", body)
        _, _, body = self.get("/manifest.webmanifest")
        icons = json.loads(body)["icons"]
        self.assertEqual({i["sizes"] for i in icons}, {"192x192", "512x512"})
        for icon in icons:
            code, headers, png = self.get(icon["src"])
            self.assertEqual((code, headers["Content-Type"], png[:4]), (200, "image/png", b"\x89PNG"))

    def test_manifest_head_and_unknown(self):
        code, headers, _ = self.get("/manifest.webmanifest")
        self.assertEqual((code, headers["Content-Type"]), (200, "application/manifest+json"))
        code, headers, body = self.get("/", method="HEAD")
        self.assertEqual((code, body), (200, b""))
        self.assertGreater(int(headers["Content-Length"]), 1000)
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.get("/../../etc/passwd")
        self.assertEqual(err.exception.code, 404)

    def test_page_uses_the_screen_bands(self):
        """The page repeats the screen's finish bands and cue words; keep them in step."""
        try:
            from smartrow_bridge.display import screens
        except ImportError:  # Pillow is only installed with the optional screen
            self.skipTest("Pillow not installed")
        page = PAGE.read_text()
        self.assertIn(f"GOOD_PCT = {screens.GOOD_FINISH_PCT}, OK_PCT = {screens.OK_FINISH_PCT}", page)
        self.assertIn(f"METER_MIN = {screens.METER_MIN_PCT}, METER_MAX = {screens.METER_MAX_PCT}", page)
        for text, _ in screens.CUES.values():
            self.assertIn(f'"{text}"', page)


def write_export(path: Path) -> None:
    """A SmartRow-app-style CSV: 60 steady strokes, a 1-minute effort, 20 steady strokes."""
    fields = ["Stroke (#)", "Second (#)", "Timestamp (UTC)", "Distance (m)", "Work (J)", "Actual power (W)",
              "Stroke rate (SPM)", "Stroke length (cm)"] + [f"#{i} (N)" for i in range(25)]
    rows, t = [], 0.0
    plan = [(60, 110, STEADY, 115), (21, 152, FULL, 119), (20, 110, STEADY, 115)]
    start = datetime(2026, 10, 27, 11, 2, 0, tzinfo=timezone.utc)
    for n, watts, curve, length in plan:
        for _ in range(n):
            t += 60 / 21
            stamp = (start + timedelta(seconds=round(t))).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows.append([len(rows) + 1, round(t), stamp, len(rows) * 10 + 10, round(watts * 60 / 21, 1), watts,
                         21.0, length, 0, *curve])
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(fields)
        w.writerows(rows)


class ReplayToolTest(unittest.TestCase):
    def test_replay_finds_the_effort(self):
        import tools.coach_replay as replay_tool
        with tempfile.TemporaryDirectory() as tmp:
            export = Path(tmp) / "session.csv"
            write_export(export)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(replay_tool.main([str(export)]), 0)
        text = out.getvalue()
        self.assertIn("101 strokes, steady about 110 W", text)
        self.assertIn("Interval 1: 1:00, 21 strokes, 152 W", text)
        self.assertIn("live cue: good", text)

    def test_unreadable_file(self):
        import tools.coach_replay as replay_tool
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "notes.csv"
            bad.write_text("a,b\n1,2\n")
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                self.assertEqual(replay_tool.main([str(bad)]), 1)
        self.assertIn("can't read it as a SmartRow export", err.getvalue())


if __name__ == "__main__":
    unittest.main()
