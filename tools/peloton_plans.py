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
# The Row's five pace intensities, lowest first, used when a reply doesn't name them itself.
DEFAULT_NAMES = ["Recovery", "Easy", "Moderate", "Challenging", "Max"]
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
                                names[str(key)] = str(name)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(details)
    return names


def plan(details: dict, names: list[str] | None = None) -> list[tuple[int, int, str]]:
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
        value = lo if lo == hi or hi is None else f"{lo}-{hi}"
        label = found.get(str(value))
        if label is None and isinstance(value, (int, float)) and names and 0 <= int(value) < len(names):
            label = names[int(value)]
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
    print("Intensity names in the reply:", _intensity_names(details) or "none (DEFAULT_NAMES used)")
    for start, length, label in plan(details, DEFAULT_NAMES)[:20]:
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
        intervals = plan(details, DEFAULT_NAMES)
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
                        "class_min": minutes, "blocks": dict(blocks)})
    if not rows:
        raise SystemExit(f"No class plans found ({no_plan} cached classes without one): run inspect to see their shape.")
    order = [n for n in DEFAULT_NAMES if any(r["intensity"] == n for r in rows)] + \
        sorted({r["intensity"] for r in rows} - set(DEFAULT_NAMES))

    with open(out / "intervals.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    # one row per class: total time at each intensity, how many blocks, and their lengths
    with open(out / "classes.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["class_min", "title", "instructor", "aired"]
                   + [f"{i} {k}" for i in order for k in ("min", "blocks", "lengths")])
        for c in sorted(classes, key=lambda c: (-c["class_min"], c["aired"])):
            cells = []
            for i in order:
                b = c["blocks"].get(i, [])
                cells += [round(sum(b) / 60, 1), len(b), " ".join(_clock(x) for x in b)]
            w.writerow([c["class_min"], c["title"], c["instructor"], c["aired"]] + cells)
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
    (out / "report.html").write_text(_html(rows, classes, order, counts, by_len, no_plan), encoding="utf-8")

    # the console summary: per class length, longest call-out and typical time at each intensity
    print(f"{len(classes)} classes with a plan ({no_plan} without), {len(rows)} intervals.")
    for minutes in sorted({c["class_min"] for c in classes}, reverse=True):
        cs = [c for c in classes if c["class_min"] == minutes]
        print(f"\n{minutes}-minute classes ({len(cs)}):")
        for i in order:
            mine = [r for r in rows if r["class_min"] == minutes and r["intensity"] == i]
            if not mine:
                print(f"  {i:<12} never called")
                continue
            top = max(mine, key=lambda r: r["length_s"])
            totals = [sum(c["blocks"].get(i, [])) for c in cs]
            print(f"  {i:<12} longest {_clock(top['length_s'])} ({top['instructor']}, {top['aired']}); "
                  f"per class median {_clock(statistics.median(totals))}, most {_clock(max(totals))}")
    print(f"\nWrote {out / 'report.html'}, classes.csv, intervals.csv and summary.csv")


def instructor_table(rows, classes, order, minutes=None) -> list[list]:
    """Per instructor: classes, the longest call-out at each intensity, and the share of class time at each."""
    out = []
    for who in sorted({c["instructor"] for c in classes}):
        cs = [c for c in classes if c["instructor"] == who and (minutes is None or c["class_min"] == minutes)]
        if not cs:
            continue
        total = sum(sum(sum(b) for b in c["blocks"].values()) for c in cs) or 1
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


def _html(rows, classes, order, counts, by_len, no_plan) -> str:
    esc = html.escape
    colour = lambda i: COLOURS.get(i, "#b48cff")  # noqa: E731
    key = "".join(f'<span class="k"><i style="background:{colour(i)}"></i>{esc(i)}</span>' for i in order)
    sections = []
    for minutes in sorted({c["class_min"] for c in classes}, reverse=True):
        cs = sorted((c for c in classes if c["class_min"] == minutes), key=lambda c: c["aired"], reverse=True)
        mine = [r for r in rows if r["class_min"] == minutes]
        head = []
        for i in order:
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
            total = sum(r["length_s"] for r in plan_rows) or 1
            bar = "".join(f'<span title="{esc(r["intensity"])} {_clock(r["length_s"])}" style="flex:{r["length_s"] / total:.4f};'
                          f'background:{colour(r["intensity"])}"></span>' for r in plan_rows)
            sums = " · ".join(f"{esc(i)} {_clock(sum(c['blocks'][i]))} ({len(c['blocks'][i])})"
                              for i in order if c["blocks"].get(i))
            strips.append(f"<div class=c><div class=t>{esc(c['title'])} <small>{esc(str(c['instructor']))}, "
                          f"{c['aired']}</small></div><div class=strip>{bar}</div><small>{sums}</small></div>")
        inst = "".join(
            f"<tr><td>{esc(str(who))}</td><td>{n}</td>"
            + "".join(f"<td>{_clock(lo) if lo else '–'} <small>{share * 100:.0f}%</small></td>" for lo, share in cells)
            + "</tr>" for who, n, cells in instructor_table(rows, classes, order, minutes))
        sections.append(f"""<h2>{minutes}-minute classes <small>{len(cs)} classes</small></h2>
<table><tr><th>Intensity</th><th>Longest call-out</th><th>Per class: median</th><th>most</th><th>blocks (median)</th><th>classes using it</th></tr>{''.join(head)}</table>
<h3>By instructor <small>longest call-out, and share of class time</small></h3>
<table><tr><th>Instructor</th><th>Classes</th>{''.join(f'<th>{esc(i)}</th>' for i in order)}</tr>{inst}</table>
<h3>Each class <small>the plan from start to finish; time at each intensity (blocks)</small></h3>{''.join(strips)}
<h3>How long each call-out lasts</h3><div class="g">{_bars(by_len[minutes], order)}</div>""")
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
.table-wrap{{overflow-x:auto}}
</style></head><body>
<h1>Peloton Row class plans</h1>
<p><small>{len(classes)} classes with a class plan ({no_plan} without), {len(rows)} continuous intervals. Back-to-back
segments at the same intensity count as one interval. From Peloton's class plans, for personal analysis.</small></p>
<p>{key}</p>
{''.join(sections)}
<h2>All class lengths</h2><div class="g">{_bars(counts, order)}</div>
</body></html>"""


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
    i = sub.add_parser("inspect", help="show what a cached class plan looks like")
    i.add_argument("--cache", default="peloton-cache")
    i.add_argument("--id", help="a class id (default: the first cached)")
    a = sub.add_parser("analyse", help="the frequency report from the cache")
    a.add_argument("--cache", default="peloton-cache")
    a.add_argument("--out", default="peloton-report")
    a.add_argument("--minutes", type=int, nargs="+", help="only these class lengths")
    args = p.parse_args(argv)
    {"fetch": fetch, "inspect": inspect, "analyse": analyse}[args.cmd](args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("Stopped. Run fetch again to carry on where it left off.")
