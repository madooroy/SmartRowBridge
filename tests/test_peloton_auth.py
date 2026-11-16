import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from tools import peloton_auth as pa
from tools import peloton_plans as pp


class FakeReply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ParseTest(unittest.TestCase):
    def test_console_line(self):
        self.assertEqual(pa.parse_entry("  abc123 v1.MRrt-xyz\n"), ("abc123", "v1.MRrt-xyz"))

    def test_local_storage_value_and_key(self):
        value = json.dumps({"body": {"client_id": "cid", "refresh_token": "rt", "access_token": "at"},
                            "expiresAt": 1})
        self.assertEqual(pa.parse_entry(value), ("cid", "rt"))
        no_id = json.dumps({"body": {"refresh_token": "rt"}})
        key = "@@auth0spajs@@::fromkey::https://api.onepeloton.com/::openid offline_access"
        self.assertEqual(pa.parse_entry(f"{key}\t{no_id}"), ("fromkey", "rt"))

    def test_nothing_usable(self):
        for text in ("undefined", "one", json.dumps({"body": {"client_id": "c"}}), "{broken"):
            with self.assertRaises(ValueError):
                pa.parse_entry(text)


class TokenTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "cfg", "peloton-auth.json")
        self.calls = []

    def tearDown(self):
        self.dir.cleanup()

    def opener(self, reply=None, error=None):
        def fake(req, timeout=30):
            self.calls.append(json.loads(req.data))
            if error:
                raise urllib.error.HTTPError(req.full_url, error, "no", {}, io.BytesIO(b"{}"))
            return FakeReply(json.dumps(reply).encode())
        return fake

    def save(self, **extra):
        pa._save(self.path, {"client_id": "cid", "refresh_token": "rt1", **extra})

    def test_renews_saves_the_rotated_token_first_then_reuses(self):
        self.save()
        token = pa.access_token(self.path, now=1000.0,
                                opener=self.opener({"access_token": "at2", "refresh_token": "rt2", "expires_in": 172800}))
        self.assertEqual(token, "at2")
        self.assertEqual(self.calls, [{"grant_type": "refresh_token", "client_id": "cid", "refresh_token": "rt1"}])
        saved = json.loads(Path(self.path).read_text())
        self.assertEqual((saved["refresh_token"], saved["expires_at"]), ("rt2", 1000.0 + 172800))
        self.assertEqual(Path(self.path).stat().st_mode & 0o777, 0o600)  # readable by its owner only
        # Still valid (more than an hour left): no request at all.
        self.assertEqual(pa.access_token(self.path, now=1000.0 + 172800 - 7200, opener=self.opener(error=500)), "at2")
        self.assertEqual(len(self.calls), 1)
        # Within the last hour: renewed; without a new refresh token the old one is kept.
        pa.access_token(self.path, now=1000.0 + 172800 - 60, opener=self.opener({"access_token": "at3"}))
        self.assertEqual(json.loads(Path(self.path).read_text())["refresh_token"], "rt2")

    def test_refused_or_missing_sign_in(self):
        with self.assertRaises(pa.SignInNeeded):
            pa.access_token(self.path, opener=self.opener({}))  # nothing saved
        self.save()
        with self.assertRaises(pa.SignInNeeded):
            pa.access_token(self.path, opener=self.opener(error=403))
        self.assertEqual(json.loads(Path(self.path).read_text())["refresh_token"], "rt1")  # left as it was


class AutoTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.cache, self.sessions, self.out = root / "cache", root / "sessions", root / "pub" / "classes.json"
        for d in (self.cache, self.sessions, self.out.parent):
            d.mkdir(parents=True)
        self.auth = root / "auth.json"

    def tearDown(self):
        self.dir.cleanup()

    def touch(self, path, when):
        path.write_text("{}")
        os.utime(path, (when, when))

    def test_history_due_once_a_row_has_finished(self):
        now = time.time()
        self.assertFalse(pp.history_due(self.cache, str(self.sessions), now))  # no rows
        self.touch(self.sessions / "2026-11-16T1900.json", now - 120)
        self.touch(self.sessions / "2026-11-16T1900.strokes.json", now - 60)
        self.assertFalse(pp.history_due(self.cache, str(self.sessions), now))  # just finished: wait
        self.assertTrue(pp.history_due(self.cache, str(self.sessions), now + 600))
        self.touch(self.cache / "history.json", now + 600)
        self.assertFalse(pp.history_due(self.cache, str(self.sessions), now + 700))  # already downloaded

    def args(self, **kw):
        a = pp.main.__globals__["argparse"].Namespace(
            cache=str(self.cache), out=str(self.out), sessions=str(self.sessions), min_minutes=20, delay=0,
            after_row=True, weekly=False, auth=str(self.auth), cookie=False, ask=False)
        for k, v in kw.items():
            setattr(a, k, v)
        return a

    def test_refused_sign_in_is_shown_and_not_retried(self):
        self.auth.write_text(json.dumps({"client_id": "c", "refresh_token": "r"}))
        self.touch(self.sessions / "row.json", time.time() - 3600)
        with mock.patch.object(pp.peloton_auth, "access_token", side_effect=pa.SignInNeeded("run setup")) as at:
            with self.assertRaises(SystemExit):
                pp.auto(self.args())
            sync = json.loads(self.out.with_name("sync.json").read_text())
            self.assertIs(sync["ok"], False)
            self.assertIn("run setup", sync["message"])
            pp.auto(self.args())  # same saved sign-in: waits, no new attempt
            self.assertEqual(at.call_count, 1)

    def test_after_row_downloads_history_and_indexes(self):
        self.auth.write_text("{}")
        self.touch(self.sessions / "row.json", time.time() - 3600)
        with mock.patch.object(pp, "make_client", return_value="client"), \
                mock.patch.object(pp, "fetch_history") as fh, mock.patch.object(pp, "index") as ix, \
                mock.patch.object(pp, "fetch") as fe:
            pp.auto(self.args())
        fh.assert_called_once()
        ix.assert_called_once()
        fe.assert_not_called()
        sync = json.loads(self.out.with_name("sync.json").read_text())
        self.assertIs(sync["ok"], True)
        self.assertIn("history_at", sync)
        self.assertNotIn("classes_at", sync)

    def test_no_saved_sign_in_does_nothing(self):
        with mock.patch.object(pp, "make_client") as mc:
            pp.auto(self.args(weekly=True, after_row=False))
        mc.assert_not_called()
        self.assertFalse(self.out.with_name("sync.json").exists())


if __name__ == "__main__":
    unittest.main()
