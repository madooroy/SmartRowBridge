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

    def test_stroke_rate_per_block(self):
        d = details("20 min Intervals Row", 20, [(60, 120, 0), (120, 150, 2), (150, 180, 2)])
        tm = d["target_metrics_data"]["target_metrics"]
        for t, (lo, hi) in zip(tm, [(18, 22), (24, 28), (26, 30)]):
            t["metrics"].append({"name": "stroke_rate", "lower": float(lo), "upper": float(hi)})
        tm[2]["offsets"]["end"] = 200  # the second Challenging piece is longer: its rate covers most of the block
        e = pp.index_entry("abc", d)
        self.assertEqual([x[3] for x in e["p"]], [[18, 22], [26, 30]])
        self.assertEqual(pp.index_entry("abc", details("20 min Row", 20, [(60, 120, 0)]))["p"][0][3], None)

    def test_groups(self):
        self.assertEqual(pp.class_group("20 min Tabata Row"), "HIIT & Tabata")
        self.assertEqual(pp.class_group("30 min Intervals & Arms Row"), "Intervals")
        self.assertEqual(pp.class_group("30 min 2000s Row"), "Music & themes")

    def test_drill_classes_group_and_stroke_part(self):
        self.assertEqual(pp.class_group("10 min Form & Drills Row: The Drive"), "Form & drills")
        self.assertEqual(pp.class_group("20 min Form Fixer Row"), "Form fixer")
        self.assertEqual(pp.class_group("20 min Advanced Beginner Row"), "Beginner")
        self.assertEqual(pp.stroke_part("10 min Form & Drills: The Recovery"), "Recovery")
        self.assertEqual(pp.stroke_part("10 min Form & Drills Row: Recovery"), "Recovery")
        self.assertEqual(pp.stroke_part("10 min Form & Drills Row"), "Whole stroke")
        e = pp.index_entry("abc", details("10 min Form & Drills: The Catch", 10, [(60, 300, 0)]))
        self.assertEqual((e["g"], e["fp"]), ("Form & drills", "Catch"))
        self.assertNotIn("fp", pp.index_entry("abc", details("30 min Pop Row", 30, [(0, 600, 1)])))
        self.assertEqual(pp.form_tags({"title": "10 min Form & Drills: The Recovery", "description": "Boilerplate."}),
                         ["recovery order"])

    def test_index_keeps_short_drill_classes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp, "c")
            (cache / "classes").mkdir(parents=True)
            for cid, title, m in (("a", "10 min Form & Drills: The Recovery", 10), ("b", "10 min Pop Row", 10),
                                  ("c", "30 min Pop Row", 30)):
                (cache / "classes" / f"{cid}.json").write_text(json.dumps(details(title, m, [(0, 300, 0)])))
            self.assertEqual(sorted(e["id"] for e in pp.build_index(cache, 20, None)), ["a", "c"])

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


class DrillsTest(unittest.TestCase):
    CATALOGUE = [
        {"id": "d1", "title": "10 min Rowing Form Drills", "duration": 600, "original_air_time": 1700000000,
         "instructor_id": "i1", "description": "Arms, body, then legs: a pick drill with pauses at the finish."},
        {"id": "d2", "title": "5 min Catch Technique", "duration": 300, "original_air_time": 1700100000,
         "instructor_id": "i2", "description": "Reach long at the catch, shins vertical."},
        {"id": "d3", "title": "20 min Pop Row", "duration": 1200, "original_air_time": 1700200000,
         "instructor_id": "i1", "description": "We'll finish with a drill on stroke rate."},
        {"id": "n1", "title": "10 min Rush Row", "duration": 600, "original_air_time": 1700300000,
         "description": "Hits from Rush."},
        {"id": "n2", "title": "30 min Performance Row", "duration": 1800, "description": "Push hard."},
    ]

    def test_match_and_tags(self):
        c = {r["id"]: r for r in self.CATALOGUE}
        self.assertEqual([pp.drill_match(c[k]) for k in ("d1", "d2", "d3", "n1", "n2")],
                         ["title", "title", "description", "", ""])
        self.assertEqual(pp.form_tags(c["d1"]), ["recovery order", "finish & layback"])
        self.assertEqual(pp.form_tags(c["d2"]), ["catch & reach"])
        self.assertEqual(pp.form_tags(c["n1"]), [])  # the band, not rushing the slide

    def test_drills_writes_csv_with_instructors_from_the_cache(self):
        import csv
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp, "c")
            (cache / "classes").mkdir(parents=True)
            (cache / "catalogue.json").write_text(json.dumps(self.CATALOGUE))
            d = details("10 min Rowing Form Drills", 10, [(0, 150, 0)])  # a quarter of the class has a target
            d["ride"].update(instructor_id="i1", instructor={"name": "Coach One"})
            (cache / "classes" / "d1.json").write_text(json.dumps(d))

            class A:
                pass
            args = A()
            args.cache, args.out, args.show = str(cache), str(Path(tmp, "out")), 30
            with redirect_stdout(io.StringIO()) as printed:
                pp.drills(args)
            rows = list(csv.DictReader(open(Path(tmp, "out", "drills.csv"), encoding="utf-8")))
            self.assertEqual([r["id"] for r in rows], ["d2", "d1", "d3"])  # title matches first, short first
            d1 = rows[1]
            self.assertEqual((d1["instructor"], d1["plan cached"], d1["matched"], d1["targets %"]),
                             ("Coach One", "True", "title", "25"))
            self.assertEqual(rows[0]["targets %"], "")  # not cached
            self.assertIn("none of the class 0, under half 1, half or more 0", printed.getvalue())
            self.assertIn("2 drill/technique by title, 1 more by description only", printed.getvalue())
            self.assertIn("rush 1", printed.getvalue())  # unmatched short-class title words, to spot missed names

    def test_fetch_drills_only(self):
        from unittest import mock
        asked = []

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def get(self, path, **params):
                asked.append(path)
                return details("x", 10, [(0, 60, 1)])

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(pp, "Client", FakeClient), \
                mock.patch.object(pp.getpass, "getpass", lambda *a: "secret"):
            Path(tmp, "catalogue.json").write_text(json.dumps(self.CATALOGUE))

            class A:
                pass
            args = A()
            args.cache, args.cookie, args.delay, args.max, args.refresh = tmp, False, 0, None, False
            args.minutes, args.drills = None, True
            with mock.patch("builtins.print"):
                pp.fetch(args)
            self.assertEqual(sorted(asked), ["/api/ride/d1/details", "/api/ride/d2/details", "/api/ride/d3/details"])


class SegmentsTest(unittest.TestCase):
    def test_outline_names_drills_and_flags_blocks_the_plan_misses(self):
        import csv
        import io
        from contextlib import redirect_stdout
        d = details("10 min Form & Drills: The Recovery", 10, [(60, 180, 0), (180, 300, 1)])
        d["ride"]["pedaling_start_offset"] = 60
        d["segments"] = {"segment_list": [
            {"name": "Warm Up", "is_drill": False, "start_time_offset": 0, "length": 120,
             "subsegments_v2": [{"offset": 0, "length": 120, "display_name": "Easy Effort",
                                 "movements": [{"name": "Easy Effort"}]}]},
            {"name": "Drills", "is_drill": True, "start_time_offset": 120, "length": 120,
             "subsegments_v2": [{"offset": 120, "length": 60, "display_name": "Arms Only",
                                 "movements": [{"name": "Arms Only"}, {"name": "Pause"}]},
                                {"offset": 180, "length": 60, "display_name": "Challenging Effort"}]}]}
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp, "c")
            (cache / "classes").mkdir(parents=True)
            (cache / "classes" / "a.json").write_text(json.dumps(d))
            (cache / "classes" / "b.json").write_text(json.dumps(details("30 min Pop Row", 30, [(0, 600, 1)])))

            class A:
                pass
            args = A()
            args.cache, args.out, args.groups, args.show = str(cache), str(Path(tmp, "o")), ["Form & drills"], 60
            with redirect_stdout(io.StringIO()) as printed:
                pp.segments(args)
            rows = list(csv.DictReader(open(Path(tmp, "o", "segments.csv"), encoding="utf-8")))
            self.assertEqual([(r["name"], r["start_s"], r["drill"], r["plan agrees"]) for r in rows],
                             [("Easy Effort", "60", "False", "yes"), ("Arms Only", "180", "True", ""),
                              ("Challenging Effort", "240", "True", "no")])
            self.assertEqual((rows[1]["movements"], rows[1]["part"]), ("Pause", "Recovery"))
            out = printed.getvalue()
            self.assertIn("1 have a part Peloton flags as a drill", out)
            self.assertIn("Drills > Arms Only *", out)
            self.assertIn("outline 'Challenging Effort' 60 s, plan Moderate", out)
