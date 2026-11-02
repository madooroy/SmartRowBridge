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
        d = details("60 min Endurance", 60, [(0, 300, 1), (300, 600, 1), (600, 660, 2), (660, 1200, 3)])
        self.assertEqual(pp.plan(d, pp.DEFAULT_NAMES),
                         [(0, 600, "Easy"), (600, 60, "Moderate"), (660, 540, "Challenging")])

    def test_names_from_the_reply_win(self):
        d = details("x", 10, [(0, 60, 4)], names=True)
        self.assertEqual(pp.plan(d, pp.DEFAULT_NAMES), [(0, 60, "Mx")])

    def test_a_class_without_a_plan(self):
        self.assertEqual(pp.plan({"ride": {"title": "x"}}, pp.DEFAULT_NAMES), [])

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
                details("60 min Intervals", 60, [(0, 300, 1), (300, 840, 3), (840, 900, 0), (900, 960, 4)])))
            (cache / "classes" / "b.json").write_text(json.dumps(details("10 min HIIT", 10, [(0, 30, 4), (30, 90, 1)])))
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
