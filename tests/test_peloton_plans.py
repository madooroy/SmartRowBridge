import json
import tempfile
import unittest
from pathlib import Path

from tools import peloton_plans as pp


def details(title, minutes, segs, names=False):
    """A class reply shaped like Peloton's: target_metrics_data.target_metrics[...] (segs: (start, end, level))."""
    d = {"ride": {"title": title, "duration": minutes * 60, "original_air_time": 1700000000,
                  "instructor": {"name": "Coach"}},
         "target_metrics_data": {"target_metrics": [
             {"offsets": {"start": a, "end": b}, "segment_type": "row",
              "metrics": [{"name": "pace_intensity", "lower": v, "upper": v}]} for a, b, v in segs]}}
    if names:
        d["target_metrics_data"]["pace_intensities"] = [{"value": k, "display_name": n}
                                                       for k, n in enumerate(["Rec", "Ez", "Mod", "Chal", "Mx"])]
    return d


class PelotonPlansTest(unittest.TestCase):
    def test_back_to_back_segments_at_one_intensity_are_one_interval(self):
        d = details("60 min Endurance", 60, [(0, 300, 0), (300, 600, 0), (600, 660, 1), (660, 1200, 2)])
        self.assertEqual(pp.plan(d, pp.PELOTON_LEVELS),
                         [(0, 600, "Easy"), (600, 60, "Moderate"), (660, 540, "Challenging")])

    def test_names_from_the_reply_win(self):
        d = details("x", 10, [(0, 60, 4)], names=True)
        self.assertEqual(pp.plan(d, pp.PELOTON_LEVELS), [(0, 60, "Mx")])

    def test_peloton_numbering_and_float_levels(self):
        # Peloton numbers Recovery -1 and Easy 0; some replies store -1 as -1.0.
        d = details("x", 10, [(0, 60, -1.0), (60, 120, 0), (120, 180, 3), (180, 240, -1)])
        self.assertEqual([x[2] for x in pp.plan(d, pp.PELOTON_LEVELS)], ["Recovery", "Easy", "Max", "Recovery"])

    def test_a_class_without_a_plan(self):
        self.assertEqual(pp.plan({"ride": {"title": "x"}}, pp.PELOTON_LEVELS), [])

    def test_buckets(self):
        self.assertEqual(pp.bucket(20), "≤20 s")
        self.assertEqual(pp.bucket(60), "1 min")
        self.assertEqual(pp.bucket(540), "8–9 min")
        self.assertEqual(pp.bucket(3000), "20 min+")

    def test_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp, "c")
            (cache / "classes").mkdir(parents=True)
            (cache / "classes" / "a.json").write_text(json.dumps(
                details("60 min Intervals", 60, [(0, 300, 0), (300, 840, 2), (840, 900, -1.0), (900, 960, 3)])))
            (cache / "classes" / "b.json").write_text(json.dumps(details("10 min HIIT", 10, [(0, 30, 3), (30, 90, 0)])))
            (cache / "classes" / "c.json").write_text(json.dumps({"ride": {"title": "no plan"}}))

            class A:
                pass
            args = A()
            args.cache, args.out, args.minutes = str(cache), str(Path(tmp, "out")), None
            pp.analyse(args)
            report = Path(tmp, "out", "report.html").read_text(encoding="utf-8")
            self.assertIn("2 classes with a class plan (1 without)", report)
            self.assertLess(report.index("60-minute classes"), report.index("10-minute classes"))  # longest first
            self.assertIn("<b>9:00</b> <small>60 min Intervals, Coach", report)
            self.assertIn("Recovery 1:00 (1) · Easy 5:00 (1) · Challenging 9:00 (1) · Max 1:00 (1)", report)
            # Recovery isn't a pace level: not in the tables, only in the time accounting
            self.assertNotIn("></i>Recovery</td>", report)
            self.assertIn("pace levels 25%, Recovery 2%, no target 73% of class time", report)
            rows = Path(tmp, "out", "intervals.csv").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 1 + 6)
            per_class = Path(tmp, "out", "classes.csv").read_text(encoding="utf-8").splitlines()
            self.assertTrue(per_class[1].startswith("60,60 min Intervals,Coach"))
            self.assertIn("9.0,1,9:00", per_class[1])  # Challenging: 9 min in one block

            args.minutes = [10]  # one class length only
            pp.analyse(args)
            report = Path(tmp, "out", "report.html").read_text(encoding="utf-8")
            self.assertNotIn("60-minute classes", report)

    def test_fetch_goes_longest_first_one_length_at_a_time(self):
        from unittest import mock
        catalogue = [{"id": f"c{m}{k}", "duration": m * 60, "fitness_discipline": "caesar"}
                     for m in (10, 30, 60) for k in range(2)]
        asked = []

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def get(self, path, **params):
                asked.append(path)
                return details("x", 10, [(0, 60, 1)])

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(pp, "Client", FakeClient), \
                mock.patch.object(pp, "list_rows", lambda c: catalogue), \
                mock.patch.object(pp.getpass, "getpass", lambda *a: "secret"):
            class A:
                pass
            args = A()
            args.cache, args.cookie, args.delay, args.max, args.refresh = tmp, False, 0, None, False
            args.minutes = [60, 30]
            pp.fetch(args)
            self.assertEqual(asked, ["/api/ride/c600/details", "/api/ride/c601/details",
                                     "/api/ride/c300/details", "/api/ride/c301/details"])
            asked.clear()
            args.minutes = None  # the rest; the cached ones are skipped
            pp.fetch(args)
            self.assertEqual(asked, ["/api/ride/c100/details", "/api/ride/c101/details"])


class IndexTest(unittest.TestCase):
    def test_entry_scores_and_totals(self):
        d = details("30 min Endurance Row", 30, [(60, 1260, 0), (1260, 1560, 2), (1560, 1590, 3), (1590, 1740, -1)])
        e = pp.index_entry("abc", d)
        self.assertEqual((e["m"], e["g"], e["k"], e["i"]), (30, "Endurance", "Endurance Row", "Coach"))
        self.assertEqual(e["s"], {"0": 1200, "2": 300, "3": 30, "-1": 150})
        self.assertEqual(e["none"], 1800 - 1680)
        self.assertEqual((e["x"], e["b"]["2"]), (1, 300))
        expected = ((0.55 ** 2 * 1200 + 1.0 ** 2 * 300 + 1.3 ** 2 * 30 + 0.45 ** 2 * 150 + 0.45 ** 2 * 120) / 1800) ** 0.5
        self.assertAlmostEqual(e["if"], round(expected, 3))
        self.assertEqual(e["load"], round(expected ** 2 * 0.5 * 100))

    def test_groups(self):
        self.assertEqual(pp.class_group("20 min Tabata Row"), "HIIT & Tabata")
        self.assertEqual(pp.class_group("30 min Intervals & Arms Row"), "Intervals")
        self.assertEqual(pp.class_group("30 min 2000s Row"), "Music & themes")

    def test_index_skips_short_classes_and_writes_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp, "c")
            (cache / "classes").mkdir(parents=True)
            (cache / "classes" / "a.json").write_text(json.dumps(details("30 min Pop Row", 30, [(0, 600, 1)])))
            (cache / "classes" / "b.json").write_text(json.dumps(details("15 min HIIT Row", 15, [(0, 600, 2)])))

            class A:
                pass
            args = A()
            args.cache, args.out, args.min_minutes = str(cache), str(Path(tmp, "pub", "classes.json")), 20
            pp.index(args)
            data = json.loads(Path(tmp, "pub", "classes.json").read_text())
            self.assertEqual([c["id"] for c in data["classes"]], ["a"])


class TakenAndBookmarksTest(unittest.TestCase):
    def test_index_marks_bookmarks_taken_and_links_pi_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache, sess = Path(tmp, "c"), Path(tmp, "sessions")
            (cache / "classes").mkdir(parents=True)
            sess.mkdir()
            for cid in ("a", "b"):
                (cache / "classes" / f"{cid}.json").write_text(json.dumps(details(f"30 min {cid}", 30, [(0, 600, 1)])))
            (cache / "catalogue.json").write_text(json.dumps([
                {"id": "a", "is_favorite": True, "total_user_workouts": 2},
                {"id": "b", "is_favorite": False, "total_user_workouts": 0}]))
            (cache / "history.json").write_text(json.dumps([
                {"ride": "a", "start": 1_000_000}, {"ride": "a", "start": 2_000_000}]))
            (sess / "2026-11-02T0603.json").write_text(json.dumps({"id": "2026-11-02T0603", "started_at": 2_000_300}))
            (sess / "2026-11-02T0603.strokes.json").write_text("[]")
            entries = {e["id"]: e for e in pp.build_index(cache, 20, sess)}
            a, b = entries["a"], entries["b"]
            self.assertTrue(a["fav"])
            self.assertEqual(a["n"], 2)
            self.assertEqual(a["taken"], [[2_000_000, "2026-11-02T0603"], [1_000_000, None]])  # newest first
            self.assertEqual((b["fav"], b["n"], b["taken"]), (False, 0, []))

    def test_fetch_history(self):
        class FakeClient:
            def get(self, path, **params):
                if path == "/api/me":
                    return {"id": "u1"}
                self.assertion = path
                pages = [{"data": [{"ride": {"id": "a"}, "start_time": 5}, {"ride_id": "b", "start_time": 4}],
                          "page_count": 2},
                         {"data": [{"ride": {"id": "c"}, "created_at": 3}, {"ride": {}}], "page_count": 2}]
                return pages[params["page"]]
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pp.fetch_history(FakeClient(), Path(tmp)), 3)
            self.assertEqual(json.loads(Path(tmp, "history.json").read_text()),
                             [{"ride": "a", "start": 5}, {"ride": "b", "start": 4}, {"ride": "c", "start": 3}])
