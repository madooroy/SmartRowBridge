import asyncio
import base64
import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from smartrow_bridge import coach as coach_mod, intervals, sessions, strava, uploads
from tests.test_sessions import saturday

try:
    import fitdecode
    from smartrow_bridge import fitfile
except ImportError:  # fit-tool / fitdecode not installed
    fitfile = None


class FakeServices:
    """Stands in for intervals.icu and Strava: records requests, answers like the real APIs."""

    def __init__(self):
        self.requests = []
        self.fail = None  # (path prefix, HTTP status)
        self.strava_duplicate = False
        self.activity_404s = 0  # Strava: answer 404 to this many activity updates first
        self.polls = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _body(self):
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def _answer(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self):
                raw = self._body()
                fake.requests.append((self.command, self.path, dict(self.headers), raw))
                if fake.fail and self.path.startswith(fake.fail[0]):
                    return self._answer(fake.fail[1], {"error": "nope"})
                p = self.path
                if p == "/api/v1/athlete/0":
                    return self._answer(200, {"id": "i1001", "name": "Test Rower", "sportSettings": [
                        {"types": ["Ride", "VirtualRide"], "ftp": 305},
                        {"types": ["Rowing"], "ftp": 200, "indoor_ftp": 185}]})
                if p.startswith("/api/v1/athlete/0/events"):
                    return self._answer(200, {"id": 4242, "category": "NOTE"})
                if p == "/api/v1/athlete/0/activities":
                    return self._answer(201, {"id": "i777"})
                if p.startswith("/api/v1/activity/i777/streams"):
                    return self._answer(200, [{"type": "time", "data": list(range(5, 3000))}])
                if p.startswith("/api/v1/activity/"):
                    return self._answer(200, {"id": "i777"})
                if p == "/oauth/token":
                    return self._answer(200, {"access_token": "fresh-token", "refresh_token": "new-refresh",
                                              "expires_at": int(time.time()) + 21600})
                if p.startswith("/api/v3/activities/") and fake.activity_404s > 0:
                    fake.activity_404s -= 1
                    return self._answer(404, {"message": "Record Not Found", "errors": []})
                if p.startswith("/api/v3/activities/"):
                    return self._answer(200, {"id": 31337})
                if p == "/api/v3/athlete":
                    return self._answer(200, {"firstname": "Test", "lastname": "Rower"})
                if p == "/api/v3/uploads":
                    return self._answer(201, {"id": 55, "status": "Your activity is still being processed.",
                                              "error": None, "activity_id": None})
                if p == "/api/v3/uploads/55":
                    fake.polls += 1
                    if fake.strava_duplicate:
                        return self._answer(200, {"id": 55, "error": "x.fit duplicate of activity 999",
                                                  "activity_id": None})
                    return self._answer(200, {"id": 55, "error": None, "activity_id": 31337})
                return self._answer(404, {})

            do_GET = do_POST = do_PUT = _handle

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def posts(self, path):
        return [r for r in self.requests if r[0] == "POST" and r[1].startswith(path)]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class UploadsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.dir = root / "sessions"
        self.key_file = root / "intervals.key"
        self.strava_file = root / "strava.json"
        self.fake = FakeServices()
        self.saved_urls = (intervals.API, strava.API, strava.TOKEN_URL)
        intervals.API = f"{self.fake.base}/api/v1"
        strava.API = f"{self.fake.base}/api/v3"
        strava.TOKEN_URL = f"{self.fake.base}/oauth/token"
        self.settings = uploads.Settings(str(self.dir), str(self.key_file), "activity", str(self.strava_file), "Rowing")

    def tearDown(self):
        intervals.API, strava.API, strava.TOKEN_URL = self.saved_urls
        self.fake.close()
        self.tmp.cleanup()

    def set_up_intervals(self, mode="activity", when=None):
        self.key_file.write_text("secret-key\n")
        if when is not None:
            import os
            os.utime(self.key_file, (when, when))
        self.settings.intervals_upload = mode

    def set_up_strava(self, since=0.0, expires_at=0):
        self.strava_file.write_text(json.dumps({"client_id": "123", "client_secret": "shh", "access_token": "old",
                                                "refresh_token": "refresh-1", "expires_at": expires_at,
                                                "since": since}))

    def saved(self, minutes_ago=30, complete=True, source="bridge", rowing_s=1800, saved_ago=None, strokes=True):
        r = saturday()
        started = time.time() - minutes_ago * 60 - rowing_s
        saved_at = started + rowing_s if saved_ago is None else time.time() - saved_ago
        record = sessions.build_record(r.coach, started, saved_at, complete, 6000, rowing_s, source,
                                       ended_at=started + rowing_s)
        record["rowing_s"] = rowing_s
        sessions.save(self.dir, record, sessions.stroke_rows(r.coach) if strokes else None)
        return record

    def run_ticks(self, up, n=1):
        async def go():
            for _ in range(n):
                up._next_check = 0.0
                up.tick()
                for _ in range(300):
                    await asyncio.sleep(0.01)
                    if not up._busy:
                        break
        asyncio.run(go())

    # ------------------------------------------------------------ intervals.icu

    def test_intervals_activity_upload(self):
        self.set_up_intervals(when=time.time() - 86400)
        record = self.saved()
        result = intervals.upload_activity(record, b"FITDATA", "secret-key")
        self.assertEqual(result, {"activity_id": "i777"})
        method, path, headers, raw = self.fake.posts("/api/v1/athlete/0/activities")[0]
        self.assertEqual(headers["Authorization"], "Basic " + base64.b64encode(b"API_KEY:secret-key").decode())
        self.assertIn("multipart/form-data; boundary=", headers["Content-Type"])
        self.assertIn(b'name="file"; filename="smartrow-', raw)
        self.assertIn(b"FITDATA", raw)
        put = [r for r in self.fake.requests if r[0] == "PUT"][0]
        self.assertEqual(put[1], "/api/v1/activity/i777")
        body = json.loads(put[3])
        self.assertTrue(body["name"].endswith(" Row"))
        self.assertEqual(len(body["description"].splitlines()), 2)  # short: the full report is on the web page
        self.assertTrue(body["description"].startswith("Rowed using SmartRow\n"))
        self.assertNotIn("FTP", body["description"])
        self.assertTrue(body["description"].splitlines()[1].startswith("Coach: "))

    def test_efforts_marked_as_the_only_intervals(self):
        self.set_up_intervals(when=time.time() - 86400)
        record = self.saved()
        strokes = sessions.read_strokes(str(self.dir), record["id"])
        intervals.upload_activity(record, b"FITDATA", "secret-key", strokes)
        put = [r for r in self.fake.requests if r[0] == "PUT" and "/intervals" in r[1]]
        self.assertEqual(put[0][1], "/api/v1/activity/i777/intervals?all=true")
        marks = json.loads(put[0][3])
        self.assertEqual(len(marks), len(record["intervals"]))
        offset = 5 - strokes[0]["t"]  # the stream starts at the first stroke
        for mark, iv in zip(marks, record["intervals"]):
            self.assertEqual(mark["type"], "WORK")
            self.assertEqual(mark["label"], iv["intensity"])
            self.assertAlmostEqual(5 + mark["start_index"], iv["started_at_s"] + offset, delta=1)
            self.assertAlmostEqual(mark["end_index"] - mark["start_index"], iv["duration_s"], delta=1.5)

    def test_coach_line(self):
        e = lambda n, w, r="good", cm=1: {"index": n, "intensity": "Moderate", "power_w": w, "rating": r,
                                          "length_delta_cm": cm}
        self.assertEqual(intervals.coach_line({"intervals": [e(1, 147, "ok"), e(2, 145, "ok"), e(3, 156, "ok")]}),
                         "Coach: 3 Moderate efforts (145\u2013156 W); the finish eased in all three.")
        self.assertEqual(intervals.coach_line({"intervals": [e(1, 140), e(2, 132, "ok", -4)]}),
                         "Coach: 2 Moderate efforts (132\u2013140 W); the finish eased in I2; strokes were shorter in I2.")
        self.assertEqual(intervals.coach_line({"intervals": [e(1, 140)]}),
                         "Coach: 1 Moderate effort (140 W); it kept its shape.")
        self.assertEqual(intervals.coach_line({"intervals": []}), "Coach: Easy rowing, no efforts.")

    def test_effort_line(self):
        i = {"index": 3, "intensity": "Moderate", "duration_s": 56, "power_w": 132, "rating": "ok",
             "finish_pct": -7, "length_delta_cm": -4.2, "hr_peak": 126}
        self.assertEqual(intervals.effort_line(i),
                         "I3 Moderate \u00b7 0:56 at 132 W \u00b7 finish \u22127%, strokes 4 cm shorter \u00b7 HR 126")
        self.assertEqual(intervals.effort_line({**i, "rating": "good", "length_delta_cm": 1, "hr_peak": None}),
                         "I3 Moderate \u00b7 0:56 at 132 W \u00b7 good")

    def test_easy_and_recovery_are_not_marked(self):
        record = {"intervals": [{"started_at_s": 100, "duration_s": 60, "intensity": "Moderate"},
                                {"started_at_s": 300, "duration_s": 60, "intensity": "Easy"}]}
        marks = intervals.work_intervals(record, [{"t": 3.0}], [float(t) for t in range(0, 1000)])
        self.assertEqual([(m["start_index"], m["end_index"]) for m in marks], [(97, 157)])

    def test_rowing_ftp_from_intervals(self):
        self.assertEqual(intervals.rowing_ftp("secret-key"), 185.0)  # the indoor FTP when set

    def test_note_mode(self):
        self.set_up_intervals("note", when=time.time() - 86400)
        record = self.saved(minutes_ago=1)  # complete: a note doesn't wait for the session to be over
        up = uploads.Uploader(self.settings)
        with self.assertLogs("smartrow_bridge.uploads", "INFO"):
            self.run_ticks(up, 2)
        notes = self.fake.posts("/api/v1/athlete/0/events?upsertOnUid=true")
        self.assertEqual(len(notes), 1)
        self.assertEqual(json.loads(notes[0][3])["uid"], f"smartrowbridge-{record['id']}")
        self.assertEqual(self.fake.posts("/api/v1/athlete/0/activities"), [])

    # ------------------------------------------------------------ Strava

    def test_strava_refresh_upload_and_poll(self):
        self.set_up_strava(expires_at=0)
        account = strava.StravaAccount(str(self.strava_file))
        result = account.upload(b"FITDATA", "smartrowbridge-x", "Morning Row", "summary", poll_s=0.01,
                                data_type="fit")
        self.assertEqual(result, {"activity_id": 31337})
        token = self.fake.posts("/oauth/token")[0]
        self.assertIn(b"grant_type=refresh_token", token[3])
        self.assertIn(b"refresh_token=refresh-1", token[3])
        saved = json.loads(self.strava_file.read_text())
        self.assertEqual((saved["access_token"], saved["refresh_token"]), ("fresh-token", "new-refresh"))
        self.assertEqual(self.strava_file.stat().st_mode & 0o777, 0o600)
        upload = self.fake.posts("/api/v3/uploads")[0]
        self.assertEqual(upload[2]["Authorization"], "Bearer fresh-token")
        self.assertNotIn(b'name="sport_type"', upload[3])  # not an upload field: Rowing comes from the file
        self.assertEqual([r for r in self.fake.requests if r[0] == "PUT"], [])
        for field in (b'name="data_type"\r\n\r\nfit', b'name="trainer"\r\n\r\n1',
                      b'name="external_id"\r\n\r\nsmartrowbridge-x', b'name="description"\r\n\r\nsummary'):
            self.assertIn(field, upload[3])
        # A still-valid token is not refreshed again.
        account.upload(b"FITDATA", "smartrowbridge-y", "Morning Row", "summary", poll_s=0.01, data_type="fit")
        self.assertEqual(len(self.fake.posts("/oauth/token")), 1)

    def test_strava_other_sport_type_set_afterwards(self):
        self.set_up_strava(expires_at=int(time.time()) + 9999)
        strava.StravaAccount(str(self.strava_file)).upload(b"F", "e", "n", "d", sport_type="VirtualRow", poll_s=0.01)
        put = [r for r in self.fake.requests if r[0] == "PUT"]
        self.assertEqual(put[0][1], "/api/v3/activities/31337")
        self.assertEqual(json.loads(put[0][3]), {"sport_type": "VirtualRow"})

    def test_strava_tcx_gets_its_type_set(self):
        self.set_up_strava(expires_at=int(time.time()) + 9999)
        strava.StravaAccount(str(self.strava_file)).upload(b"<x/>", "e", "n", "d", poll_s=0.01)
        upload = self.fake.posts("/api/v3/uploads")[0]
        self.assertIn(b'name="data_type"\r\n\r\ntcx', upload[3])
        self.assertIn(b'filename="e.tcx"', upload[3])
        put = [r for r in self.fake.requests if r[0] == "PUT"]
        self.assertEqual(json.loads(put[0][3]), {"sport_type": "Rowing"})

    def test_strava_type_set_once_the_activity_is_there(self):
        self.set_up_strava(expires_at=int(time.time()) + 9999)
        self.fake.activity_404s = 2  # just uploaded: not found yet
        strava.StravaAccount(str(self.strava_file)).upload(b"<x/>", "e", "n", "d", poll_s=0.01)
        put = [r for r in self.fake.requests if r[0] == "PUT"]
        self.assertEqual(len(put), 3)

    def test_strava_duplicate_counts_as_done(self):
        self.set_up_strava(expires_at=int(time.time()) + 9999)
        self.fake.strava_duplicate = True
        result = strava.StravaAccount(str(self.strava_file)).upload(b"F", "e", "n", "d", poll_s=0.01)
        self.assertIn("duplicate", result["duplicate"])

    def test_code_from_pasted_address(self):
        self.assertEqual(strava.code_from("http://localhost/exchange_token?state=&code=abc123&scope=read,activity:write"),
                         ("abc123", "read,activity:write"))
        self.assertEqual(strava.code_from("  abc123 "), ("abc123", ""))
        self.assertEqual(strava.code_from(""), (None, ""))
        self.assertIn("scope=read%2Cactivity%3Awrite", strava.authorize_url("123"))
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%2Fexchange_token", strava.authorize_url("123"))

    # ------------------------------------------------------------ when sessions are sent

    @unittest.skipIf(fitfile is None, "fit-tool not installed")
    def test_sent_once_to_each_when_over(self):
        self.set_up_intervals(when=time.time() - 86400)
        self.set_up_strava(since=time.time() - 86400, expires_at=int(time.time()) + 9999)
        recent = self.saved(minutes_ago=2)  # finished 2 minutes ago: could still resume
        over = self.saved(minutes_ago=30)
        up = uploads.Uploader(self.settings)
        with self.assertLogs("smartrow_bridge.uploads", "INFO"):
            self.run_ticks(up, 5)
        self.assertEqual(len(self.fake.posts("/api/v1/athlete/0/activities")), 1)
        self.assertEqual(len(self.fake.posts("/api/v3/uploads")), 1)
        marks = sessions.read_session(str(self.dir), over["id"])["uploads"]
        self.assertEqual(marks["intervals_activity"]["activity_id"], "i777")
        self.assertEqual(marks["strava"]["activity_id"], 31337)
        self.assertNotIn("uploads", sessions.read_session(str(self.dir), recent["id"]))
        self.run_ticks(up, 3)  # nothing new: nothing sent again
        self.assertEqual(len(self.fake.posts("/api/v3/uploads")), 1)

    def test_not_sent_by_itself(self):
        self.set_up_intervals(when=time.time() - 86400)
        self.set_up_strava(since=time.time() - 3600, expires_at=int(time.time()) + 9999)
        self.saved(minutes_ago=30, source="replay")  # from a CSV
        self.saved(minutes_ago=200)  # rowed before Strava was set up (intervals.icu: yes)
        self.saved(minutes_ago=30, complete=False, saved_ago=60)  # not complete, saved a minute ago: rowing
        self.saved(minutes_ago=60 * 24 * 10)  # ten days ago
        up = uploads.Uploader(self.settings)
        pending = up.pending()
        self.assertEqual([d for d, _ in pending], ["intervals_activity"])

    def test_switched_off_mid_session_still_sent(self):
        self.set_up_intervals(when=time.time() - 86400)
        record = self.saved(minutes_ago=30, complete=False, saved_ago=uploads.ABANDONED_AFTER_S + 60)
        self.assertEqual([(d, r["id"]) for d, r in uploads.Uploader(self.settings).pending()],
                         [("intervals_activity", record["id"])])

    @unittest.skipIf(fitfile is None, "fit-tool not installed")
    def test_one_failing_session_does_not_hold_up_the_rest(self):
        self.set_up_intervals(when=time.time() - 86400)
        bad = self.saved(minutes_ago=300)
        good = self.saved(minutes_ago=30)
        (self.dir / f"{bad['id']}.strokes.json").write_text('{"rows": [[0, 0, "x", 0, 0, 0]]}')  # a broken file
        up = uploads.Uploader(self.settings)
        with self.assertLogs("smartrow_bridge.uploads", "INFO"):
            self.run_ticks(up, 4)
        self.assertEqual(len(self.fake.posts("/api/v1/athlete/0/activities")), 1)
        self.assertIn("intervals_activity", sessions.read_session(str(self.dir), good["id"])["uploads"])
        self.assertEqual(up.pending(), [])  # the broken one waits before it is tried again

    def test_turned_down_is_noted_and_not_retried(self):
        self.set_up_intervals(when=time.time() - 86400)
        record = self.saved(minutes_ago=30)
        self.fake.fail = ("/api/v1/athlete/0/activities", 422)
        up = uploads.Uploader(self.settings)
        if fitfile is None:
            self.skipTest("fit-tool not installed")
        with self.assertLogs("smartrow_bridge.uploads", "WARNING"):
            self.run_ticks(up, 1)
        mark = sessions.read_session(str(self.dir), record["id"])["uploads"]["intervals_activity"]
        self.assertIn("HTTP 422", mark["failed"])
        up._retry_at.clear()
        self.assertEqual(up.pending(), [])

    def test_sessions_from_before_uploads_are_skipped(self):
        self.set_up_intervals(when=time.time() - 86400)
        self.saved(minutes_ago=30, strokes=False)  # saved by an older version: no strokes file
        self.assertEqual(uploads.Uploader(self.settings).pending(), [])

    def test_mark_kept_when_session_rewritten(self):
        record = self.saved(minutes_ago=30)
        sessions.save(self.dir, {**record, "saved_at": record["saved_at"] + 5})
        self.assertTrue(uploads.record_sent(str(self.dir), record, "strava", {"activity_id": 1}))
        self.assertEqual(sessions.read_session(str(self.dir), record["id"])["uploads"]["strava"]["activity_id"], 1)

    def test_credentials_not_sent_to_another_site(self):
        from smartrow_bridge import webapi
        import urllib.request
        handler = webapi._Redirect()
        req = urllib.request.Request("https://www.strava.com/api/v3/uploads")
        req.add_header("Authorization", "Bearer secret")
        other = handler.redirect_request(req, None, 302, "Found", {}, "https://elsewhere.example/x")
        same = handler.redirect_request(req, None, 302, "Found", {}, "https://www.strava.com/api/v3/y")
        self.assertNotIn("Authorization", other.headers)
        self.assertEqual(same.headers.get("Authorization"), "Bearer secret")

    @unittest.skipIf(fitfile is None, "fit-tool not installed")
    def test_finished_by_hand_goes_at_once(self):
        self.set_up_intervals(when=time.time() - 86400)
        record = self.saved(minutes_ago=1)  # just stopped: would wait FINAL_AFTER_S
        up = uploads.Uploader(self.settings)
        self.assertEqual(up.pending(), [])
        snap = up.snapshot()
        self.assertEqual((snap["waiting"], snap["latest"]["dests"]), (1, {"intervals.icu activity": "later"}))
        self.assertEqual(up.latest_waiting(), record["id"])
        sessions.mark_finished(str(self.dir), record["id"])  # Y on the screen
        up.check_now("t1")
        with self.assertLogs("smartrow_bridge.uploads", "INFO"):
            self.run_ticks(up, 2)
        self.assertEqual(len(self.fake.posts("/api/v1/athlete/0/activities")), 1)
        snap = up.snapshot()
        self.assertEqual(snap["latest"]["dests"], {"intervals.icu activity": "sent"})
        self.assertEqual((snap["waiting"], snap["ack"], snap["sending"]), (0, "t1", None))

    def test_snapshot_shows_a_service_out_of_reach(self):
        self.set_up_intervals("note", when=time.time() - 86400)
        self.saved()
        self.fake.fail = ("/api/v1", 503)
        up = uploads.Uploader(self.settings)
        with self.assertLogs("smartrow_bridge.uploads", "WARNING"):
            self.run_ticks(up, 2)
        snap = up.snapshot()
        self.assertEqual((snap["retrying"], snap["waiting"]), (1, 0))
        self.assertEqual(snap["latest"]["dests"], {"intervals.icu note": "retrying"})
        self.assertIn("503", snap["error"])

    def test_nothing_set_up_nothing_sent(self):
        self.saved()
        up = uploads.Uploader(self.settings)
        self.run_ticks(up, 2)
        self.assertEqual(self.fake.requests, [])

    def test_failure_warns_once_and_backs_off(self):
        self.set_up_intervals("note", when=time.time() - 86400)
        self.saved()
        self.fake.fail = ("/api/v1", 401)
        up = uploads.Uploader(self.settings)
        with self.assertLogs("smartrow_bridge.uploads", "WARNING") as logs:
            self.run_ticks(up, 3)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("refused the API key", logs.output[0])
        self.assertEqual(up.pending(), [])  # waiting RETRY_AFTER_S before trying intervals.icu again

    # ------------------------------------------------------------ command line

    @unittest.skipIf(fitfile is None, "fit-tool not installed")
    def test_cli_send_status_and_fit(self):
        import os
        self.set_up_intervals(when=time.time() - 86400)
        self.set_up_strava(since=time.time(), expires_at=int(time.time()) + 9999)
        record = self.saved(source="replay")
        env = {"SRB_SESSION_DIR": str(self.dir), "SRB_INTERVALS_KEY_FILE": str(self.key_file),
               "SRB_STRAVA_FILE": str(self.strava_file)}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                self.assertEqual(uploads.main(["--status"]), 0)
                strava.POLL_S, poll = 0.01, strava.POLL_S
                try:
                    self.assertEqual(uploads.main(["--send", record["id"]]), 0)
                    self.assertEqual(uploads.main(["--send", record["id"]]), 0)  # again: already sent
                finally:
                    strava.POLL_S = poll
                fit_path = Path(self.tmp.name) / "s.fit"
                self.assertEqual(uploads.main(["--fit", record["id"], str(fit_path)]), 0)
            text = out.getvalue()
            self.assertIn("not sent by itself (--send to send)", text)
            self.assertIn("intervals.icu activity: sent", text)
            self.assertIn("Strava: sent", text)
            self.assertIn("already sent, not again", text)
            self.assertEqual(fit_path.read_bytes()[8:12], b".FIT")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


@unittest.skipIf(fitfile is None, "fit-tool / fitdecode not installed")
class FitFileTest(unittest.TestCase):
    def test_activity_file(self):
        r = saturday()
        record = sessions.build_record(r.coach, 1_800_000_000.0, 1_800_001_000.0, True, 6000, 1000)
        strokes = [dict(zip(sessions.STROKE_COLUMNS, row)) for row in sessions.stroke_rows(r.coach)]
        data = fitfile.build(record, strokes)
        found = {}
        with fitdecode.FitReader(io.BytesIO(data), check_crc=fitdecode.CrcCheck.RAISE) as fr:
            for fm in fr:
                if isinstance(fm, fitdecode.FitDataMessage):
                    found.setdefault(fm.name, []).append({f.name: f.value for f in fm.fields})
        self.assertEqual(len(found["record"]), len(strokes))
        session = found["session"][0]
        self.assertEqual((session["sport"], session["sub_sport"]), ("rowing", "indoor_rowing"))
        self.assertEqual(session["total_strokes"], len(strokes))
        self.assertEqual(session["start_time"].timestamp(), 1_800_000_000)
        laps = found["lap"]
        efforts = [lap for lap in laps if lap["intensity"] == "interval"]
        self.assertEqual(len(efforts), len(record["intervals"]))  # one lap per effort ...
        self.assertGreaterEqual(len(laps), 2 * len(record["intervals"]) + 1)  # ... with Easy / Recovery around them
        self.assertEqual(len(laps), 2 * len(record["intervals"]) + 1)  # efforts, and the rowing between
        self.assertEqual((laps[0]["intensity"], laps[-1]["intensity"]), ("warmup", "cooldown"))
        self.assertEqual({lap["intensity"] for lap in laps[1:-1:2]}, {"interval"})
        self.assertEqual({lap["intensity"] for lap in laps[2:-1:2]}, {"recovery"})
        self.assertAlmostEqual(sum(lap["total_elapsed_time"] for lap in laps), session["total_elapsed_time"], delta=0.01)
        self.assertEqual(sum(lap["total_strokes"] for lap in laps), len(strokes))  # each stroke in one lap
        self.assertAlmostEqual(sum(lap["total_distance"] for lap in laps), session["total_distance"], delta=0.01)
        for a, b in zip(laps, laps[1:]):
            self.assertEqual(a["timestamp"], b["start_time"])  # back to back
        self.assertEqual(laps[-1]["lap_trigger"], "session_end")
        self.assertEqual(laps[0]["lap_trigger"], "manual")
        self.assertEqual(found["record"][100]["power"], round(strokes[100]["power_w"]))

    def test_recovery_laps_between_efforts(self):
        from smartrow_bridge.activity import laps
        r = saturday()
        record = sessions.build_record(r.coach, 1_800_000_000.0, 1_800_001_000.0, True, 6000, 1000)
        strokes = [dict(zip(sessions.STROKE_COLUMNS, row)) for row in sessions.stroke_rows(r.coach)]
        easy = record["steady_power_w"]
        # Paddle very lightly for 40 strokes after the first effort.
        first = record["intervals"][0]
        end = first["started_at_s"] + first["duration_s"]
        light = [s for s in strokes if end < s["t"] <= end + 120]
        for s in light:
            s["power_w"] = easy * 0.5
        kinds = [lap["kind"] for lap in laps(record, strokes, split_easy=True)]
        self.assertIn("Recovery", kinds)
        self.assertEqual(kinds[0], "Easy")

    def test_force_curves_as_developer_fields(self):
        r = saturday()
        record = sessions.build_record(r.coach, 1_800_000_000.0, 1_800_001_000.0, True, 6000, 1000)
        strokes = [dict(zip(sessions.STROKE_COLUMNS, row)) for row in sessions.stroke_rows(r.coach)]
        data = fitfile.build(record, strokes)
        described, records = {}, []
        with fitdecode.FitReader(io.BytesIO(data), check_crc=fitdecode.CrcCheck.RAISE) as fr:
            for fm in fr:
                if isinstance(fm, fitdecode.FitDataMessage) and fm.name == "field_description":
                    f = {x.name: x.value for x in fm.fields}
                    described[f["field_name"]] = (f["field_definition_number"], f.get("scale"), f.get("units"))
                elif isinstance(fm, fitdecode.FitDataMessage) and fm.name == "record":
                    records.append({x.name: x.value for x in fm.fields})
        self.assertEqual(described["HandleForceCurve"], (60, 10, "N"))
        self.assertEqual(described["InstrokeAbscissaType"][0], 90)
        s, rec = strokes[100], records[100]
        count = coach_mod.drive_end(s["force_n"]) + 1
        self.assertEqual(rec["InstrokePointCount"], count)
        self.assertEqual(rec["InstrokeAbscissaType"], fitfile.ABSCISSA_DISTANCE)
        # fitdecode gives the stored values (value x scale)
        self.assertEqual(list(rec["HandleForceCurve"][:count]), [v * 10 for v in s["force_n"][:count]])
        self.assertEqual(rec["DriveLength"], round(s["length_cm"]))
        self.assertEqual(rec["PeakDriveForce"], max(s["force_n"]) * 10)
        self.assertAlmostEqual(rec["InstrokeSampleInterval"] / 100, s["length_cm"] / count, delta=0.01)

    def test_old_strokes_without_curves(self):
        r = saturday()
        record = sessions.build_record(r.coach, 1_800_000_000.0, 1_800_001_000.0, True, 6000, 1000)
        strokes = [dict(zip(sessions.STROKE_COLUMNS[:-1], row[:-1])) for row in sessions.stroke_rows(r.coach)]
        data = fitfile.build(record, strokes)  # saved before force curves were: no developer fields, still valid
        with fitdecode.FitReader(io.BytesIO(data), check_crc=fitdecode.CrcCheck.RAISE) as fr:
            names = {fm.name for fm in fr if isinstance(fm, fitdecode.FitDataMessage)}
        self.assertNotIn("field_description", names)

    def test_distance_starts_at_zero(self):
        # A session that started part-way through the pulley's count (a long pause, same connection).
        r = saturday()
        record = sessions.build_record(r.coach, 1_800_000_000.0, 1_800_001_000.0, True, 0, 1000)
        strokes = [dict(zip(sessions.STROKE_COLUMNS, row)) for row in sessions.stroke_rows(r.coach)]
        base = [s["distance_m"] for s in strokes]
        for s in strokes:
            s["distance_m"] += 5000
        data = fitfile.build(record, strokes)
        found = {}
        with fitdecode.FitReader(io.BytesIO(data)) as fr:
            for fm in fr:
                if isinstance(fm, fitdecode.FitDataMessage):
                    found.setdefault(fm.name, []).append({f.name: f.value for f in fm.fields})
        self.assertLess(found["record"][0]["distance"], 50)
        self.assertAlmostEqual(found["record"][-1]["distance"], found["session"][0]["total_distance"], delta=0.01)
        self.assertAlmostEqual(found["session"][0]["total_distance"], base[-1], delta=base[1] - base[0] + 1)

    def test_tcx_for_strava(self):
        import xml.etree.ElementTree as ET
        from smartrow_bridge import tcxfile
        r = saturday()
        record = sessions.build_record(r.coach, 1_800_000_000.0, 1_800_001_000.0, True, 0, 1000)
        strokes = [dict(zip(sessions.STROKE_COLUMNS, row)) for row in sessions.stroke_rows(r.coach)]
        root = ET.fromstring(tcxfile.build(record, strokes))
        ns = {"t": tcxfile.NS, "x": tcxfile.TPX}
        self.assertIsNone(root.find(".//t:Creator", ns))  # no device: Strava names the uploading app
        self.assertEqual(root.find(".//t:Activity", ns).get("Sport"), "Other")
        points = root.findall(".//t:Trackpoint", ns)
        self.assertEqual(len(points), len(strokes))
        self.assertEqual(points[100].find(".//x:Watts", ns).text, str(round(strokes[100]["power_w"])))
        laps = root.findall(".//t:Lap", ns)
        self.assertEqual(len(laps), 2 * len(record["intervals"]) + 1)
        total = sum(int(lap.find("t:DistanceMeters", ns).text) for lap in laps)
        self.assertEqual(total, int(points[-1].find("t:DistanceMeters", ns).text))
        self.assertEqual(laps[0].get("StartTime"), "2027-01-15T08:00:00Z")

    def test_no_strokes(self):
        with self.assertRaises(ValueError):
            fitfile.build({"started_at": 0}, [])


if __name__ == "__main__":
    unittest.main()
