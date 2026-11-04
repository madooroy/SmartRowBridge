#!/usr/bin/env python3
"""How long Peloton's rowing classes hold each intensity: a frequency report from the class plans.

Peloton's class plans (the pace-intensity bar in the app: Easy, Moderate, Challenging, Max...) are
only in its members' API, so this runs with your own sign-in. It is Peloton's unofficial API: it can
change without notice, so the download keeps every class's raw reply and the analysis only reads
that cache. Use it gently (one request a second by default) and for your own analysis only.

    python3 tools/peloton_plans.py fetch --minutes 60 --max 5    # try it: five 60-minute classes
    python3 tools/peloton_plans.py inspect                         # what one class plan looks like
    python3 tools/peloton_plans.py fetch --minutes 60              # then one class length at a time,
    python3 tools/peloton_plans.py analyse --minutes 60            # longest first: 60, 45, 30 ...
    python3 tools/peloton_plans.py index                           # the Pi's Classes page (20 min and up)
    python3 tools/peloton_plans.py update                          # weekly: new classes, your history, the index
    python3 tools/peloton_plans.py drills                          # drill/technique classes in the catalogue (no token)
    python3 tools/peloton_plans.py fetch --drills                  # then download just their plans

Your token: sign in at members.onepeloton.com in a browser, open the developer tools (F12),
Network tab, reload, click any request to api.onepeloton.com and copy the value of its
"authorization" header after "Bearer ". It is asked for with hidden input, used only for these
requests, and never printed or saved. (An older-style peloton_session_id cookie also works:
--cookie.)

fetch is resumable (classes already in the cache are skipped) and goes longest classes first, one
class length at a time, so each length can be looked at before deciding to go on. analyse writes,
for each class length: the longest continuous call-out at each intensity (with its class), the
time at each intensity per class (median, most, how many blocks), the same per instructor (to
spot anyone programming differently), and each class's plan as a strip. Files: report.html,
classes.csv (one row per class: minutes and blocks at each intensity), intervals.csv (every
continuous interval) and summary.csv (interval counts by length).
"""

from __future__ import annotations

import argparse
import csv
import getpass
import html
import json
import os
import re
import sys
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

API = "https://api.onepeloton.com"
ROW_DISCIPLINES = ("caesar", "rowing")  # Peloton's internal name for the Row is "caesar"
# The Row's pace intensities as Peloton numbers them (Recovery is -1, Easy 0), used when a reply
# doesn't name them itself; DEFAULT_NAMES is their order in the report.
PELOTON_LEVELS = {-1: "Recovery", 0: "Easy", 1: "Moderate", 2: "Challenging", 3: "Max"}
DEFAULT_NAMES = list(PELOTON_LEVELS.values())


def _key(value) -> str:
    """One spelling per level: -1, -1.0 and "-1" are all "-1"."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f.is_integer() else str(f)
BUCKETS = [(20, "≤20 s"), (30, "30 s"), (45, "45 s"), (60, "1 min"), (90, "1.5 min"), (120, "2 min"),
           (180, "3 min"), (240, "4 min"), (300, "5 min"), (420, "6–7 min"), (540, "8–9 min"),
           (840, "10–14 min"), (1140, "15–19 min"), (10 ** 9, "20 min+")]


# ------------------------------------------------------------------ download

class Client:
    def __init__(self, token: str | None = None, cookie: str | None = None, delay: float = 1.0):
        self.headers = {"User-Agent": "peloton-plans/1 (personal analysis)", "Peloton-Platform": "web",
                        "Accept": "application/json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        if cookie:
            self.headers["Cookie"] = f"peloton_session_id={cookie}"
        self.delay, self.last = delay, 0.0

    def get(self, path: str, **params) -> dict:
        wait = self.delay - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        url = f"{API}{path}" + (f"?{urllib.parse.urlencode(params, doseq=True)}" if params else "")
        for attempt in range(4):
            self.last = time.monotonic()
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=self.headers), timeout=30) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise SystemExit(f"Peloton refused the request ({exc.code}): the token has expired or is "
                                     "wrong. Copy a fresh one and run fetch again (it resumes).") from None
                if exc.code == 429 or exc.code >= 500:
                    time.sleep(10 * (attempt + 1))
                    continue
                raise
            except urllib.error.URLError:
                time.sleep(5 * (attempt + 1))
        raise SystemExit(f"Gave up on {path} after several tries.")


def list_rows(client: Client, limit: int = 50) -> list[dict]:
    """Every rowing class in the library (newest first)."""
    for category in ROW_DISCIPLINES:
        out, page = [], 0
        while True:
            reply = client.get("/api/v2/ride/archived", browse_category=category, content_format="audio,video",
                               limit=limit, page=page, sort_by="original_air_time", desc="true")
            data = reply.get("data") or []
            out += [r for r in data if r.get("fitness_discipline") in ROW_DISCIPLINES]
            page += 1
            print(f"  catalogue page {page}/{reply.get('page_count', '?')}: {len(out)} rowing classes", flush=True)
            if not data or page >= (reply.get("page_count") or 0):
                break
        if out:
            return out
    return []


def class_minutes(ride: dict) -> int:
    return round((ride.get("duration") or 0) / 60)


def fetch(args) -> None:
    """Longest classes first, one class length at a time (60, then 45, then 30 ...). Each length can be
    analysed before going on: --minutes 60 fetches only the 60-minute classes."""
    cache = Path(args.cache)
    (cache / "classes").mkdir(parents=True, exist_ok=True)
    token = None if args.cookie else getpass.getpass("Peloton token (hidden): ").strip()
    cookie = getpass.getpass("peloton_session_id cookie (hidden): ").strip() if args.cookie else None
    client = Client(token, cookie, args.delay)
    listing = cache / "catalogue.json"
    if listing.exists() and not args.refresh:
        rides = json.loads(listing.read_text(encoding="utf-8"))
        print(f"Catalogue from the cache: {len(rides)} rowing classes (--refresh to list again)")
    else:
        rides = list_rows(client)
        if not rides:
            raise SystemExit("No rowing classes came back. Check the token, or run inspect on a class id.")
        listing.write_text(json.dumps(rides), encoding="utf-8")
    if getattr(args, "drills", False):
        rides = [r for r in rides if drill_match(r)]
        print(f"Drill and technique classes only (title or description): {len(rides)}")
    lengths = Counter(class_minutes(r) for r in rides)
    print("Classes by length: " + ", ".join(f"{m} min: {n}" for m, n in sorted(lengths.items(), reverse=True)))
    wanted = set(args.minutes) if args.minutes else set(lengths)
    for minutes in sorted(wanted, reverse=True):
        todo = [r for r in rides if class_minutes(r) == minutes
                and not (cache / "classes" / f"{r['id']}.json").exists()]
        if args.max:
            todo = todo[:args.max]
        print(f"{minutes}-minute classes: fetching {len(todo)} (about {len(todo) * args.delay / 60:.0f} min)")
        for k, ride in enumerate(todo, 1):
            details = client.get(f"/api/ride/{ride['id']}/details")
            (cache / "classes" / f"{ride['id']}.json").write_text(json.dumps(details), encoding="utf-8")
            if k % 25 == 0 or k == len(todo):
                print(f"  {k}/{len(todo)}", flush=True)
    return client


def fetch_history(client: Client, cache: Path) -> int:
    """Your own workouts (which class, when), for "taken before" on the Classes page: saved as
    history.json, [{"ride": class id, "start": epoch s}], newest first."""
    me = client.get("/api/me")
    user = me.get("id")
    if not user:
        raise SystemExit("Peloton didn't say who you are (no id in /api/me).")
    out, page = [], 0
    while True:
        reply = client.get(f"/api/user/{user}/workouts", joins="ride", limit=100, page=page, sort_by="-created")
        for w in reply.get("data") or []:
            ride = (w.get("ride") or {}).get("id") or w.get("ride_id")
            start = w.get("start_time") or w.get("created_at")
            if ride and start:
                out.append({"ride": ride, "start": int(start)})
        page += 1
        if page >= (reply.get("page_count") or 0) or not reply.get("data"):
            break
    Path(cache, "history.json").write_text(json.dumps(out), encoding="utf-8")
    print(f"Your workout history: {len(out)} workouts")
    return len(out)


def history(args) -> None:
    token = None if args.cookie else getpass.getpass("Peloton token (hidden): ").strip()
    cookie = getpass.getpass("peloton_session_id cookie (hidden): ").strip() if args.cookie else None
    fetch_history(Client(token, cookie, args.delay), Path(args.cache))


# ------------------------------------------------------------------ reading a class plan

def _target_lists(obj):
    """Every list of {offsets: {start, end}, metrics: [...]} in a reply, wherever it sits."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _target_lists(v)
    elif isinstance(obj, list):
        if obj and all(isinstance(x, dict) and isinstance(x.get("offsets"), dict) and "metrics" in x for x in obj):
            yield obj
        for v in obj:
            yield from _target_lists(v)


def _intensity_names(details: dict) -> dict:
    """{value: name} if the reply names its intensities (any list of {value/slug, display_name/name})."""
    names = {}

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if "intensit" in k.lower() and isinstance(v, list):
                    for x in v:
                        if isinstance(x, dict):
                            key = x.get("value", x.get("slug", x.get("id")))
                            name = x.get("display_name") or x.get("name")
                            if key is not None and name:
                                names[_key(key)] = str(name)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(details)
    return names


def plan(details: dict, levels: dict | None = None) -> list[tuple[int, int, str]]:
    """The class's intensity targets as continuous intervals: [(start s, length s, intensity)].
    Back-to-back segments at the same intensity are one interval (as the app's plan reads)."""
    lists = list(_target_lists(details))
    if not lists:
        return []
    found = _intensity_names(details)
    segs = []
    for t in max(lists, key=len):
        metric = None
        for m in t.get("metrics") or []:
            n = str(m.get("name", "")).lower()
            if "intensity" in n and (metric is None or "pace" in n):
                metric = m
        if metric is None:
            continue
        lo, hi = metric.get("lower"), metric.get("upper")
        value = _key(lo) if lo == hi or hi is None else f"{_key(lo)} to {_key(hi)}"
        label = found.get(value) or {_key(k): v for k, v in (levels or {}).items()}.get(value)
        start, end = int(t["offsets"].get("start", 0)), int(t["offsets"].get("end", 0))
        if end > start:
            segs.append([start, end, label or str(value)])
    segs.sort()
    merged = []
    for s in segs:
        if merged and merged[-1][2] == s[2] and s[0] - merged[-1][1] <= 1:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    return [(a, b - a, label) for a, b, label in merged]


def rate_targets(details: dict) -> list[tuple[int, int, int, int]]:
    """Peloton's stroke-rate target through the class: [(start s, end s, lower spm, upper spm)]."""
    lists = list(_target_lists(details))
    if not lists:
        return []
    out = []
    for t in max(lists, key=len):
        for m in t.get("metrics") or []:
            if "stroke_rate" in str(m.get("name", "")).lower() and m.get("lower") is not None:
                lo, hi = m.get("lower"), m.get("upper", m.get("lower"))
                start, end = int(t["offsets"].get("start", 0)), int(t["offsets"].get("end", 0))
                if end > start:
                    out.append((start, end, round(lo), round(hi if hi is not None else lo)))
    return sorted(out)


def block_rate(rates, start: int, length: int) -> list[int] | None:
    """The stroke-rate range covering most of a block, [lower, upper]; None when the class has none."""
    time = defaultdict(int)
    for a, b, lo, hi in rates:
        overlap = min(b, start + length) - max(a, start)
        if overlap > 0:
            time[(lo, hi)] += overlap
    return list(max(time, key=time.get)) if time else None


def inspect(args) -> None:
    files = sorted(Path(args.cache, "classes").glob("*.json"))
    if args.id:
        files = [Path(args.cache, "classes", f"{args.id}.json")]
    if not files:
        raise SystemExit("Nothing cached yet: run fetch first.")
    details = json.loads(files[0].read_text(encoding="utf-8"))
    ride = details.get("ride") or {}
    print(f"{ride.get('title', '?')} ({files[0].stem})")
    print("Top-level keys:", ", ".join(details))
    lists = list(_target_lists(details))
    print(f"Target lists found: {len(lists)}" + (f", longest has {max(map(len, lists))} entries" if lists else ""))
    if lists:
        print("First entry:", json.dumps(max(lists, key=len)[0])[:600])
    print("Intensity names in the reply:", _intensity_names(details) or "none (PELOTON_LEVELS used)")
    for start, length, label in plan(details, PELOTON_LEVELS)[:20]:
        print(f"  {start // 60:3d}:{start % 60:02d}  {length:4d} s  {label}")


# ------------------------------------------------------------------ the report

def bucket(seconds: int) -> str:
    return next(name for top, name in BUCKETS if seconds <= top)


def _clock(seconds: float) -> str:
    seconds = round(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def analyse(args) -> None:
    cache, out = Path(args.cache), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows, classes, no_plan = [], [], 0
    for f in sorted(Path(cache, "classes").glob("*.json")):
        details = json.loads(f.read_text(encoding="utf-8"))
        ride = details.get("ride") or {}
        minutes = class_minutes(ride)
        if args.minutes and minutes not in args.minutes:
            continue
        intervals = plan(details, PELOTON_LEVELS)
        if not intervals:
            no_plan += 1
            continue
        who = (ride.get("instructor") or {}).get("name") or str(ride.get("instructor_id", ""))
        aired = time.strftime("%Y-%m-%d", time.gmtime(ride.get("original_air_time") or 0))
        blocks = defaultdict(list)
        for start, length, label in intervals:
            blocks[label].append(length)
            rows.append({"class_id": f.stem, "title": ride.get("title", ""), "instructor": who, "aired": aired,
                         "class_min": minutes, "intensity": label, "start_s": start, "length_s": length,
                         "bucket": bucket(length)})
        classes.append({"class_id": f.stem, "title": ride.get("title", ""), "instructor": who, "aired": aired,
                        "class_min": minutes, "length_s": ride.get("duration") or minutes * 60, "blocks": dict(blocks)})
    if not rows:
        raise SystemExit(f"No class plans found ({no_plan} cached classes without one): run inspect to see their shape.")
    order = [n for n in DEFAULT_NAMES if any(r["intensity"] == n for r in rows)] + \
        sorted({r["intensity"] for r in rows} - set(DEFAULT_NAMES))
    # Recovery isn't one of Peloton's pace levels (on the Row its time isn't counted, and some rack the
    # handle): it's left out of the tables and shown only where class time is accounted for.
    levels = [i for i in order if i != "Recovery"]

    with open(out / "intervals.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    # one row per class: total time at each intensity, how many blocks, and their lengths
    with open(out / "classes.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["class_min", "title", "instructor", "aired"]
                   + [f"{i} {k}" for i in order for k in ("min", "blocks", "lengths")] + ["no target min"])
        for c in sorted(classes, key=lambda c: (-c["class_min"], c["aired"])):
            cells = []
            for i in order:
                b = c["blocks"].get(i, [])
                cells += [round(sum(b) / 60, 1), len(b), " ".join(_clock(x) for x in b)]
            w.writerow([c["class_min"], c["title"], c["instructor"], c["aired"]] + cells
                       + [round(no_target(c) / 60, 1)])
    counts = Counter((r["intensity"], r["bucket"]) for r in rows)
    by_len = defaultdict(Counter)
    for r in rows:
        by_len[r["class_min"]][(r["intensity"], r["bucket"])] += 1
    with open(out / "summary.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["class_min", "intensity"] + [b for _, b in BUCKETS])
        for minutes in ["all"] + sorted(by_len, reverse=True):
            c = counts if minutes == "all" else by_len[minutes]
            for i in order:
                w.writerow([minutes, i] + [c[(i, b)] for _, b in BUCKETS])
    (out / "report.html").write_text(_html(rows, classes, order, levels, counts, by_len, no_plan), encoding="utf-8")

    # the console summary: per class length, longest call-out and typical time at each intensity
    print(f"{len(classes)} classes with a plan ({no_plan} without), {len(rows)} intervals.")
    for minutes in sorted({c["class_min"] for c in classes}, reverse=True):
        cs = [c for c in classes if c["class_min"] == minutes]
        print(f"\n{minutes}-minute classes ({len(cs)}): {accounting(cs)}")
        for i in levels:
            mine = [r for r in rows if r["class_min"] == minutes and r["intensity"] == i]
            if not mine:
                print(f"  {i:<12} never called")
                continue
            top = max(mine, key=lambda r: r["length_s"])
            totals = [sum(c["blocks"].get(i, [])) for c in cs]
            print(f"  {i:<12} longest {_clock(top['length_s'])} ({top['instructor']}, {top['aired']}); "
                  f"per class median {_clock(statistics.median(totals))}, most {_clock(max(totals))}")
    print(f"\nWrote {out / 'report.html'}, classes.csv, intervals.csv and summary.csv")


def no_target(c: dict) -> int:
    """Seconds of the class with no pace target at all (drills, a warm-up or cool-down without one)."""
    return max(0, c["length_s"] - sum(sum(b) for b in c["blocks"].values()))


def accounting(cs: list[dict]) -> str:
    """Where the class time went: pace levels, Recovery, and no target, as shares of it."""
    total = sum(c["length_s"] for c in cs) or 1
    rec = sum(sum(c["blocks"].get("Recovery", [])) for c in cs)
    none = sum(no_target(c) for c in cs)
    return (f"pace levels {100 * (total - rec - none) / total:.0f}%, Recovery {100 * rec / total:.0f}%, "
            f"no target {100 * none / total:.0f}% of class time")


def instructor_table(rows, classes, order, minutes=None) -> list[list]:
    """Per instructor: classes, the longest call-out at each intensity, and the share of class time at each."""
    out = []
    for who in sorted({c["instructor"] for c in classes}):
        cs = [c for c in classes if c["instructor"] == who and (minutes is None or c["class_min"] == minutes)]
        if not cs:
            continue
        total = sum(c["length_s"] for c in cs) or 1  # share of class time
        cells = []
        for i in order:
            lens = [x for c in cs for x in c["blocks"].get(i, [])]
            cells.append((max(lens) if lens else None, sum(lens) / total))
        out.append([who, len(cs), cells])
    return out


COLOURS = {"Recovery": "#878787", "Easy": "#5aa5f0", "Moderate": "#46cd5f", "Challenging": "#f5af2d", "Max": "#eb4b41"}


def _bars(counts: Counter, order: list[str]) -> str:
    """One horizontal-bar chart per intensity: how many intervals of each length."""
    out = []
    for i in order:
        vals = [counts[(i, b)] for _, b in BUCKETS]
        top = max(vals) or 1
        rows = "".join(
            f'<div class="r"><span class="l">{html.escape(b)}</span><span class="b" style="width:{v / top * 100:.1f}%;'
            f'background:{COLOURS.get(i, "#b48cff")}"></span><span class="n">{v or ""}</span></div>'
            for (_, b), v in zip(BUCKETS, vals))
        out.append(f'<section><h3>{html.escape(i)} <small>{sum(vals)} intervals</small></h3>{rows}</section>')
    return "".join(out)


def _html(rows, classes, order, levels, counts, by_len, no_plan) -> str:
    esc = html.escape
    colour = lambda i: COLOURS.get(i, "#b48cff")  # noqa: E731
    key = "".join(f'<span class="k"><i style="background:{colour(i)}"></i>{esc(i)}</span>' for i in order)
    sections = []
    for minutes in sorted({c["class_min"] for c in classes}, reverse=True):
        cs = sorted((c for c in classes if c["class_min"] == minutes), key=lambda c: c["aired"], reverse=True)
        mine = [r for r in rows if r["class_min"] == minutes]
        head = []
        for i in levels:
            lens = [r for r in mine if r["intensity"] == i]
            totals = [sum(c["blocks"].get(i, [])) for c in cs]
            nblocks = [len(c["blocks"].get(i, [])) for c in cs]
            if not lens:
                head.append(f"<tr><td>{esc(i)}</td><td colspan=5>never called</td></tr>")
                continue
            top = max(lens, key=lambda r: r["length_s"])
            head.append(f"<tr><td><i class=sw style='background:{colour(i)}'></i>{esc(i)}</td>"
                        f"<td><b>{_clock(top['length_s'])}</b> <small>{esc(top['title'])}, {esc(str(top['instructor']))}, "
                        f"{top['aired']}</small></td><td>{_clock(statistics.median(totals))}</td>"
                        f"<td>{_clock(max(totals))}</td><td>{statistics.median(nblocks):g}</td>"
                        f"<td>{sum(1 for t in totals if t) }/{len(cs)}</td></tr>")
        strips = []
        for c in cs:
            plan_rows = sorted((r for r in mine if r["class_id"] == c["class_id"]), key=lambda r: r["start_s"])
            total, at, parts = c["length_s"] or 1, 0, []
            for r in plan_rows + [{"start_s": total, "length_s": 0, "intensity": None}]:
                if r["start_s"] > at:  # no target here: drawn as a gap
                    parts.append(f'<span title="no target {_clock(r["start_s"] - at)}" class=gap '
                                 f'style="flex:{(r["start_s"] - at) / total:.4f}"></span>')
                if r["intensity"]:
                    parts.append(f'<span title="{esc(r["intensity"])} {_clock(r["length_s"])}" '
                                 f'style="flex:{r["length_s"] / total:.4f};background:{colour(r["intensity"])}"></span>')
                at = max(at, r["start_s"] + r["length_s"])
            bar = "".join(parts)
            sums = " · ".join(f"{esc(i)} {_clock(sum(c['blocks'][i]))} ({len(c['blocks'][i])})"
                              for i in order if c["blocks"].get(i)) + f" · no target {_clock(no_target(c))}"
            strips.append(f"<div class=c><div class=t>{esc(c['title'])} <small>{esc(str(c['instructor']))}, "
                          f"{c['aired']}</small></div><div class=strip>{bar}</div><small>{sums}</small></div>")
        inst = "".join(
            f"<tr><td>{esc(str(who))}</td><td>{n}</td>"
            + "".join(f"<td>{_clock(lo) if lo else '–'} <small>{share * 100:.0f}%</small></td>" for lo, share in cells)
            + "</tr>" for who, n, cells in instructor_table(rows, classes, levels, minutes))
        sections.append(f"""<h2>{minutes}-minute classes <small>{len(cs)} classes</small></h2>
<p><small>Class time: {accounting(cs)}.</small></p>
<table><tr><th>Intensity</th><th>Longest call-out</th><th>Per class: median</th><th>most</th><th>blocks (median)</th><th>classes using it</th></tr>{''.join(head)}</table>
<h3>By instructor <small>longest call-out, and share of class time</small></h3>
<table><tr><th>Instructor</th><th>Classes</th>{''.join(f'<th>{esc(i)}</th>' for i in levels)}</tr>{inst}</table>
<h3>Each class <small>the plan from start to finish; time at each intensity (blocks)</small></h3>{''.join(strips)}
<h3>How long each call-out lasts</h3><div class="g">{_bars(by_len[minutes], levels)}</div>""")
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Peloton Row class plans</title><style>
:root{{--bg:#fff;--fg:#1a1a1a;--dim:#6b6b6b;--line:#e3e3e3}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--dim:#999;--line:#2a2a2a}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif;margin:24px auto;max-width:1100px;padding:0 16px}}
small{{color:var(--dim);font-weight:400}} .g{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:20px}}
.r{{display:flex;align-items:center;gap:6px;font-size:12px;margin:2px 0}} .l{{flex:none;width:72px;color:var(--dim);text-align:right}}
.b{{height:12px;border-radius:2px;min-width:1px}} .n{{flex:none;color:var(--dim)}}
table{{border-collapse:collapse;width:100%;font-size:14px}} td,th{{border-bottom:1px solid var(--line);padding:6px;text-align:left;vertical-align:top}}
.sw,.k i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}} .k{{margin-right:14px;font-size:13px}}
.c{{margin:10px 0}} .t{{font-size:14px}} .strip{{display:flex;height:14px;border-radius:3px;overflow:hidden;margin:3px 0}}
.table-wrap{{overflow-x:auto}} .gap{{background:repeating-linear-gradient(45deg,var(--line) 0 3px,transparent 3px 6px)}}
</style></head><body>
<h1>Peloton Row class plans</h1>
<p><small>{len(classes)} classes with a class plan ({no_plan} without), {len(rows)} continuous intervals. Back-to-back
segments at the same intensity count as one interval. Recovery isn't one of Peloton's pace levels, so it is
left out of the tables and only counted where class time is accounted for. From Peloton's class plans, for
personal analysis.</small></p>
<p>{key} <span class="k"><i class="gap"></i>no target</span></p>
{''.join(sections)}
<h2>All class lengths</h2><div class="g">{_bars(counts, levels)}</div>
</body></html>"""


# ------------------------------------------------------------------ drill and technique classes

# Words that make a class a drill/technique class when they are in its title. The same words found
# only in the description are reported apart: a normal class may say "we'll work on form" too.
DRILL_WORDS = re.compile(
    r"\b(drills?|technique|form|beginners?|learn(ing)?( to row)?|fundamentals|foundations?|basics|intro"
    r"|introduction|skills?|how to row|sequenc(e|ing)|stroke mechanics|pick drill)\b", re.I)
# Which form point a drill class trains, from its title and description (several can apply).
FORM_TAGS = [
    ("recovery order", r"arms,? (and )?body|body,? (and )?arms|arms away|body over|sequenc|pick drill|reverse pick"
                       r"|pause drill|pauses?\b|hands (away|past)|arms only"),
    ("finish & layback", r"\bfinish|lay ?back|lean(ing)? back|body angle|11 o.?clock"),
    ("catch & reach", r"\bcatch|\breach|compression|1 o.?clock|shins? vertical"),
    ("rhythm & ratio", r"\bratio|rhythm|timing|slide control|tempo|1:2|2:1|rush(ing)? the slide|don.t rush"),
    ("legs & drive", r"legs only|leg drive|\bdrive\b|push(ing)? (with|through) (the|your) legs|half slide"),
]
STOP_WORDS = {"min", "row", "rowing", "the", "and", "with", "a", "of", "to", "for", "in", "on", "&", "-", "x"}


def drill_match(ride: dict) -> str:
    """"title" when a drill word is in the class's title, "description" when only in its description, else ""."""
    if DRILL_WORDS.search(ride.get("title") or ""):
        return "title"
    if DRILL_WORDS.search(ride.get("description") or ""):
        return "description"
    return ""


def form_tags(ride: dict) -> list[str]:
    text = f"{ride.get('title') or ''} {ride.get('description') or ''}"
    return [name for name, pattern in FORM_TAGS if re.search(pattern, text, re.I)]


def _instructor_names(cache: Path, wanted: set) -> dict:
    """{instructor id: name}, from the class replies already cached (the catalogue has ids only)."""
    names = {}
    for f in Path(cache, "classes").glob("*.json"):
        if not wanted - set(names):
            break
        ride = (_read_json(f, {}) or {}).get("ride") or {}
        who = ride.get("instructor") or {}
        if ride.get("instructor_id") and who.get("name"):
            names[ride["instructor_id"]] = who["name"]
    return names


def drills(args) -> None:
    """Find the drill and technique classes in the cached catalogue (no token needed): how many at each
    length, which form point each trains, which already have their plan cached, and the title words of
    the short classes that weren't matched (to spot names the word list misses). Writes drills.csv."""
    cache = Path(args.cache)
    rides = _read_json(cache / "catalogue.json", None)
    if not rides:
        raise SystemExit(f"No catalogue in {cache}: run fetch (or update) once first.")
    found = [(r, drill_match(r)) for r in rides]
    found = [(r, how) for r, how in found if how]
    names = _instructor_names(cache, {r.get("instructor_id") for r, _ in found} - {None})
    rows = []
    for r, how in found:
        who = (r.get("instructor") or {}).get("name") or names.get(r.get("instructor_id"), "")
        aired = time.strftime("%Y-%m-%d", time.gmtime(int(r.get("original_air_time") or 0)))
        rows.append({"id": r.get("id", ""), "minutes": class_minutes(r), "aired": aired,
                     "title": r.get("title", ""), "instructor": who, "matched": how,
                     "form": "; ".join(form_tags(r)), "plan cached": (cache / "classes" / f"{r.get('id')}.json").exists(),
                     "description": " ".join((r.get("description") or "").split())})
    rows.sort(key=lambda x: (x["matched"] != "title", x["minutes"], x["aired"]), reverse=False)
    by_title = [x for x in rows if x["matched"] == "title"]
    by_desc = [x for x in rows if x["matched"] == "description"]
    print(f"{len(rides)} rowing classes in the catalogue: {len(by_title)} drill/technique by title, "
          f"{len(by_desc)} more by description only.")
    for label, group in (("By title", by_title), ("By description only", by_desc)):
        lengths = Counter(x["minutes"] for x in group)
        if lengths:
            print(f"  {label}: " + ", ".join(f"{m} min: {n}" for m, n in sorted(lengths.items())))
    tags = Counter(t for x in rows for t in x["form"].split("; ") if t)
    if tags:
        print("  Form points (title or description): " + ", ".join(f"{t} {n}" for t, n in tags.most_common()))
    print(f"  Plans already cached: {sum(x['plan cached'] for x in rows)} of {len(rows)}")
    print("\nDrill/technique classes by title:")
    for x in by_title:
        print(f"  {x['minutes']:>3} min  {x['aired']}  {x['title']}" + (f"  ({x['instructor']})" if x["instructor"] else "")
              + (f"  [{x['form']}]" if x["form"] else ""))
    if by_desc:
        print(f"\nBy description only (first {min(len(by_desc), args.show)} of {len(by_desc)}; all in drills.csv):")
        for x in by_desc[:args.show]:
            print(f"  {x['minutes']:>3} min  {x['aired']}  {x['title']}" + (f"  [{x['form']}]" if x["form"] else ""))
    matched = {x["id"] for x in rows}
    words = Counter(w for r in rides if class_minutes(r) <= 15 and r.get("id") not in matched
                    for w in re.findall(r"[a-z][a-z'&+:]*", (r.get("title") or "").lower()) if w not in STOP_WORDS)
    print("\nTitle words of the other classes of 15 min or less (names the word list may miss):")
    print("  " + ", ".join(f"{w} {n}" for w, n in words.most_common(40)))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "drills.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["id"])
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {out / 'drills.csv'}. To download their plans: fetch --drills (needs a token).")


# ------------------------------------------------------------------ the index for the Pi's Classes page

# Where the Pi's web page reads it (smartrow_bridge.web serves it as /peloton/classes.json). install.sh
# makes the folder, writable by the user who ran it, so this runs without sudo.
PUBLISHED = "/var/lib/smartrow-bridge/peloton/classes.json"
LEVEL_CODES = {name: code for code, name in PELOTON_LEVELS.items()}
# How hard a minute at each level is, for the ranking: about each band's middle as a share of the middle
# of Challenging (Recovery and stretches with no target count as easy-ish). Root-mean-square over the
# class, as cycling's intensity factor weights hard minutes more than easy ones.
LEVEL_WEIGHT = {-1: 0.45, 0: 0.55, 1: 0.78, 2: 1.0, 3: 1.3}
NO_TARGET_WEIGHT = 0.45
INDEX_MIN_MINUTES = 20
GROUPS = [("Endurance", ("endurance",)), ("Intervals", ("interval",)), ("HIIT & Tabata", ("hiit", "tabata")),
          ("Beginner & form", ("beginner", "form", "drill", "technique", "learn")),
          ("Warm-up & cool-down", ("warm up", "warm-up", "cool down", "cool-down"))]


def class_group(title: str) -> str:
    t = title.lower()
    return next((g for g, words in GROUPS if any(w in t for w in words)), "Music & themes")


def index_entry(class_id: str, details: dict) -> dict | None:
    """One class for the Classes page: its plan as [start s, length s, level code], time at each level,
    longest block, Max sprints, and the intensity and load computed from the plan."""
    ride = details.get("ride") or {}
    intervals = plan(details, PELOTON_LEVELS)
    if not intervals:
        return None
    length = int(ride.get("duration") or 0) or max(a + b for a, b, _ in intervals)
    rates = rate_targets(details)
    # [start, length, level code, [lower, upper] spm or None]
    steps = [[a, b, LEVEL_CODES.get(lbl), block_rate(rates, a, b)] for a, b, lbl in intervals if lbl in LEVEL_CODES]
    totals, longest = defaultdict(int), defaultdict(int)
    for _, b, code, _ in steps:
        totals[code] += b
        longest[code] = max(longest[code], b)
    none = max(0, length - sum(totals.values()))
    work = sum(LEVEL_WEIGHT[c] ** 2 * t for c, t in totals.items()) + NO_TARGET_WEIGHT ** 2 * none
    score = (work / length) ** 0.5 if length else 0.0
    title = ride.get("title", "")
    rating = ride.get("difficulty_rating_avg")
    return {"id": class_id, "t": title, "i": (ride.get("instructor") or {}).get("name") or "",
            "d": int(ride.get("original_air_time") or 0), "m": class_minutes(ride), "len": length,
            "k": re.sub(r"^\d+ min ", "", title), "g": class_group(title),
            "r": round(rating, 1) if rating else None, "rn": ride.get("difficulty_rating_count") or 0,
            "p": steps, "s": {str(c): t for c, t in totals.items()}, "none": none,
            "b": {str(c): t for c, t in longest.items()}, "x": sum(1 for _, _, c, _ in steps if c == 3),
            "if": round(score, 3), "load": round(score ** 2 * length / 3600 * 100)}


SESSIONS = "/var/lib/smartrow-bridge/sessions"  # the bridge's saved sessions (smartrow_bridge.sessions)
SESSION_MATCH_S = 20 * 60  # a Peloton workout and a Pi session starting this close are the same row


def _read_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def pi_sessions(directory) -> list[tuple[int, str]]:
    """(start epoch, session id) of the Pi's saved sessions."""
    out = []
    for f in Path(directory).glob("*.json"):
        if f.name.endswith(".strokes.json"):
            continue
        rec = _read_json(f, {})
        if rec.get("started_at") and rec.get("id"):
            out.append((int(rec["started_at"]), str(rec["id"])))
    return out


def build_index(cache: Path, min_minutes: int = INDEX_MIN_MINUTES, sessions_dir=SESSIONS) -> list[dict]:
    """Every class with a plan, plus what Peloton knows about you and it: bookmarked ("fav", from the
    catalogue), how often you've taken it ("n"), and when ("taken": [start, Pi session id or None],
    newest first, from history.json) with the Pi's session of that row when one starts close by."""
    catalogue = {r.get("id"): r for r in _read_json(Path(cache, "catalogue.json"), [])}
    taken = defaultdict(list)
    for w in _read_json(Path(cache, "history.json"), []):
        taken[w["ride"]].append(w["start"])
    rows = pi_sessions(sessions_dir) if sessions_dir and Path(sessions_dir).is_dir() else []
    out = []
    for f in sorted(Path(cache, "classes").glob("*.json")):
        details = json.loads(f.read_text(encoding="utf-8"))
        if class_minutes(details.get("ride") or {}) < min_minutes:
            continue
        entry = index_entry(f.stem, details)
        if not entry:
            continue
        listed = catalogue.get(f.stem) or {}
        when = sorted(taken.get(f.stem, []), reverse=True)
        entry["fav"] = bool(listed.get("is_favorite"))
        entry["n"] = max(len(when), int(listed.get("total_user_workouts") or 0))
        entry["taken"] = [[t, min(((abs(t - s), sid) for s, sid in rows if abs(t - s) <= SESSION_MATCH_S),
                                  default=(0, None))[1]] for t in when]
        out.append(entry)
    return out


def write_index(entries: list[dict], path: Path) -> None:
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps({"generated": int(time.time()), "classes": entries}, separators=(",", ":")),
                       encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except PermissionError:
        raise SystemExit(f"Can't write {path}: run 'sudo bash install.sh' once (it makes that folder yours), "
                         "or give --out a folder you can write.") from None


def index(args) -> None:
    entries = build_index(Path(args.cache), args.min_minutes, getattr(args, "sessions", SESSIONS))
    if not entries:
        raise SystemExit("No class plans in the cache yet: run fetch first.")
    write_index(entries, Path(args.out))
    lengths = Counter(e["m"] for e in entries)
    print(f"Indexed {len(entries)} classes (" + ", ".join(f"{m} min: {n}" for m, n in sorted(lengths.items(), reverse=True))
          + f") into {args.out}. The Pi's web page shows them under Classes.")
    taken = [e for e in entries if e["n"]]
    print(f"  bookmarked: {sum(e['fav'] for e in entries)}, taken before: {len(taken)}, "
          f"with your Pi session linked: {sum(1 for e in taken if any(sid for _, sid in e['taken']))}")


def update(args) -> None:
    """The weekly refresh: list the catalogue again, fetch the classes not seen yet, rebuild the index."""
    args.refresh, args.max = True, None
    args.minutes = args.minutes or [m for m in (60, 45, 30, 20) if m >= args.min_minutes]
    client = fetch(args)
    try:
        fetch_history(client, Path(args.cache))
    except (SystemExit, urllib.error.URLError, ValueError) as exc:  # the classes still get indexed
        print(f"Couldn't read your workout history ({exc}); taken-before dates are left as they were.")
    index(args)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download the rowing catalogue and every class plan (resumable)")
    f.add_argument("--cache", default="peloton-cache")
    f.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default 1)")
    f.add_argument("--max", type=int, help="fetch at most this many classes this run (to try it out)")
    f.add_argument("--cookie", action="store_true", help="sign in with a peloton_session_id cookie instead")
    f.add_argument("--minutes", type=int, nargs="+", help="only these class lengths, e.g. --minutes 60 45")
    f.add_argument("--refresh", action="store_true", help="list the catalogue again (new classes)")
    f.add_argument("--drills", action="store_true",
                   help="only the drill and technique classes (see the drills command), at every length")
    i = sub.add_parser("inspect", help="show what a cached class plan looks like")
    i.add_argument("--cache", default="peloton-cache")
    i.add_argument("--id", help="a class id (default: the first cached)")
    a = sub.add_parser("analyse", help="the frequency report from the cache")
    a.add_argument("--cache", default="peloton-cache")
    a.add_argument("--out", default="peloton-report")
    a.add_argument("--minutes", type=int, nargs="+", help="only these class lengths")
    d = sub.add_parser("drills", help="find the drill and technique classes in the cached catalogue (no token)")
    d.add_argument("--cache", default="peloton-cache")
    d.add_argument("--out", default="peloton-report", help="folder for drills.csv")
    d.add_argument("--show", type=int, default=30, help="how many description-only matches to print")
    x = sub.add_parser("index", help="write the class list the Pi's Classes page reads")
    x.add_argument("--cache", default="peloton-cache")
    x.add_argument("--out", default=PUBLISHED)
    x.add_argument("--min-minutes", type=int, default=INDEX_MIN_MINUTES)
    x.add_argument("--sessions", default=SESSIONS, help="the Pi's saved sessions, to link the classes you rowed")
    h = sub.add_parser("history", help="download your own workout history (which classes, when)")
    h.add_argument("--cache", default="peloton-cache")
    h.add_argument("--delay", type=float, default=1.0)
    h.add_argument("--cookie", action="store_true")
    u = sub.add_parser("update", help="weekly: fetch new classes (needs a token) and rebuild the index")
    u.add_argument("--cache", default="peloton-cache")
    u.add_argument("--out", default=PUBLISHED)
    u.add_argument("--min-minutes", type=int, default=INDEX_MIN_MINUTES)
    u.add_argument("--minutes", type=int, nargs="+", help="class lengths (default 60 45 30 20)")
    u.add_argument("--delay", type=float, default=1.0)
    u.add_argument("--cookie", action="store_true")
    u.add_argument("--sessions", default=SESSIONS)
    args = p.parse_args(argv)
    {"fetch": fetch, "inspect": inspect, "analyse": analyse, "index": index, "update": update,
     "history": history, "drills": drills}[args.cmd](args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("Stopped. Run fetch again to carry on where it left off.")
