import asyncio
import base64
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from smartrow_bridge import intervals, sessions
from tests.test_sessions import saturday


class FakeIntervals:
    """A stand-in for intervals.icu: records requests, answers like the real API."""

    def __init__(self, status=200):
        self.requests = []
        self.status = status
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self, body):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                fake.requests.append((self.command, self.path, dict(self.headers), json.loads(raw) if raw else None))
                data = json.dumps(body).encode()
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                self._answer({"id": "i1001", "name": "Test Rower"})

            def do_POST(self):  # noqa: N802
                self._answer({"id": 4242, "category": "NOTE"})

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class IntervalsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "sessions"
        self.key_file = Path(self.tmp.name) / "intervals.key"
        self.key_file.write_text("secret-key\n")
        self.fake = FakeIntervals()
        self._api, intervals.API = intervals.API, self.fake.url

    def tearDown(self):
        intervals.API = self._api
        self.fake.close()
        self.tmp.cleanup()

    def saved(self, started_at=None, complete=True):
        r = saturday()
        started_at = started_at or time.time() - 3600
        record = sessions.build_record(r.coach, started_at, started_at + 1800, complete, 6000, 1800)
        sessions.save(self.dir, record)
        return record

    def test_note(self):
        record = self.saved(started_at=1_800_000_000.0)
        note = intervals.note_for(record)
        self.assertEqual(note["category"], "NOTE")
        self.assertRegex(note["start_date_local"], r"^\d{4}-\d{2}-\d{2}T00:00:00$")
        self.assertIn("4 intervals, 2 went short", note["name"])
        self.assertIn("What the coach saw:", note["description"])
        self.assertIn("I3  ", note["description"])
        self.assertEqual(note["uid"], f"smartrowbridge-{record['id']}")

    def test_send_and_test(self):
        record = self.saved()
        self.assertEqual(intervals.athlete_name("secret-key"), "Test Rower")
        self.assertEqual(intervals.send(record, "secret-key"), 4242)
        method, path, headers, body = self.fake.requests[-1]
        self.assertEqual((method, path), ("POST", "/api/v1/athlete/0/events?upsertOnUid=true"))
        self.assertEqual(headers["Authorization"], "Basic " + base64.b64encode(b"API_KEY:secret-key").decode())
        self.assertEqual(body["uid"], f"smartrowbridge-{record['id']}")

    def run_ticks(self, uploader, n=1):
        async def go():
            for _ in range(n):
                uploader._next_check = 0.0
                uploader.tick()
                for _ in range(200):
                    await asyncio.sleep(0.01)
                    if not uploader._busy:
                        break
        asyncio.run(go())

    def test_uploader_sends_each_finished_session_once(self):
        a = self.saved(started_at=time.time() - 7200)
        b = self.saved(started_at=time.time() - 3600)
        self.saved(started_at=time.time() - 60, complete=False)  # still rowing: not yet
        self.saved(started_at=time.time() - 10 * 86400)  # too old
        up = intervals.IntervalsUploader(str(self.dir), str(self.key_file))
        with self.assertLogs("smartrow_bridge.intervals", "INFO"):
            self.run_ticks(up, 4)
        posts = [r for r in self.fake.requests if r[0] == "POST"]
        self.assertEqual([p[3]["uid"] for p in posts], [f"smartrowbridge-{a['id']}", f"smartrowbridge-{b['id']}"])
        marked = sessions.read_session(str(self.dir), a["id"])
        self.assertEqual(marked["intervals_icu"]["event_id"], 4242)
        # The session resumed and was saved again: its note is updated (same uid), once.
        sessions.save(self.dir, {**marked, "saved_at": marked["saved_at"] + 300, "intervals_icu": None})
        self.run_ticks(up, 2)
        posts = [r for r in self.fake.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 3)
        self.assertEqual(posts[-1][3]["uid"], f"smartrowbridge-{a['id']}")

    def test_no_key_no_requests(self):
        self.saved()
        self.key_file.unlink()
        up = intervals.IntervalsUploader(str(self.dir), str(self.key_file))
        self.run_ticks(up, 2)
        self.assertEqual(self.fake.requests, [])

    def test_failure_warns_once_and_retries_later(self):
        self.saved()
        self.fake.status = 401
        up = intervals.IntervalsUploader(str(self.dir), str(self.key_file))
        with self.assertLogs("smartrow_bridge.intervals", "WARNING") as logs:
            self.run_ticks(up, 3)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("refused the API key", logs.output[0])
        self.assertGreater(up._next_check, time.monotonic() + 60)  # backs off

    def test_cli(self):
        import io
        from contextlib import redirect_stdout
        record = self.saved()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(intervals.main(["--key-file", str(self.key_file), "--test"]), 0)
            self.assertEqual(intervals.main(["--session-dir", str(self.dir), "--preview", record["id"]]), 0)
            self.assertEqual(intervals.main(["--key-file", str(self.key_file), "--session-dir", str(self.dir),
                                             "--send", record["id"]]), 0)
        text = out.getvalue()
        self.assertIn("connected as Test Rower", text)
        self.assertIn("Row coach:", text)
        self.assertIn("event 4242", text)


if __name__ == "__main__":
    unittest.main()
