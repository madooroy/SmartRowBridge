#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Form from a side-on video: body angles and timing for every stroke, from pose tracking.

Runs on a laptop or the cloud, not the Pi (too slow there). Google's MediaPipe Pose Landmarker
finds the joints in each frame; from the side the camera sees best, per stroke:

  drive, recovery       catch (hands furthest forward) to finish (hands closest to the body), and on
                        to the next catch, in seconds
  catch_lean            body angle at the catch, degrees forward of vertical ("1 o'clock" is ~30)
  finish_lean           body angle at the finish (negative = leaning back; "11 o'clock" is ~-30)
  shin                  shin angle at the catch, from vertical (0 = vertical)
  swing_vs_legs         when the back starts opening, against the legs being done (s; negative =
                        before the legs finish)
  arms_vs_legs          when the arms start bending, against the legs being done (s)
  lean_at_knees_up      body angle when the knees start to rise on the recovery (the usual order:
                        arms out, body over, then knees; negative = still leaning back)
  elbow_at_knees_up     elbow angle at that moment (180 = arms straight)

Checked on 4 Nov 2026 against the screen-flash row: drive times agree with the pulley's flag
to within 0.01 s per block; the recovery measure was checked on frames. Heel and toe points are
not reliable with straps and shoes, so heel lift is not measured.

With --strokes (a session's .strokes.json from the Pi), each clip is lined up with the session
by matching its finishes with the pulley's strokes, and every video stroke gets the pulley's power,
rate, length and drive flag. No screen flash is needed: the clip just has to show at least ~20
strokes of the session.

Setup (once): pip install -r requirements-video.txt. The pose model (about 30 MB) is downloaded
from storage.googleapis.com on first use into ~/.cache/smartrow-bridge/.

Usage:
    python3 tools/pose_video.py VIDEO [VIDEO ...] [--strokes SESSION.strokes.json]
                                [--step 2] [--from S --to S] [--out DIR] [--by 60]
Output: a summary per minute (or --by seconds) for each clip, and per-stroke CSV and JSON in --out.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import urllib.request
from contextlib import nullcontext
from pathlib import Path

import numpy as np

MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
             "pose_landmarker_heavy/float16/latest/pose_landmarker_heavy.task")
MODEL_PATH = Path.home() / ".cache" / "smartrow-bridge" / "pose_landmarker_heavy.task"
FRAME_W, FRAME_H = 960, 540  # frames are scaled to this before tracking

# MediaPipe landmark numbers, person's right side (left side = one less)
RIGHT = {"ear": 8, "sh": 12, "el": 14, "wr": 16, "hp": 24, "kn": 26, "an": 28, "he": 30, "to": 32}

MIN_PERIOD_S = 1.5  # strokes at most 40 spm
CATCH_PROMINENCE_PX = 150  # hands travel at least this far (at FRAME_W) between catch and finish
LEGS_DONE = 0.9  # legs "done" at this share of the knee's opening
SWING_DEG = 5.0  # the back has started opening once 5 degrees behind its catch lean
ARMS_BENT_DEG = 150.0  # arms have started bending below this elbow angle
KNEES_UP_DEG = 15.0  # knees have started to rise once this much below their straightest
ALIGN_TOL_S = 0.3  # a video finish and a pulley stroke this close count as the same stroke
FIELDS = ["clip", "t", "drive", "recovery", "catch_lean", "finish_lean", "shin", "swing_vs_legs",
          "arms_vs_legs", "lean_at_knees_up", "elbow_at_knees_up",
          "session_t", "power_w", "spm", "length_cm", "flag_drive_s"]


# ---------- tracking (needs mediapipe and opencv) ----------

def model_path(path: str | None) -> str:
    if path:
        return path
    if not MODEL_PATH.exists():
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        print(f"Downloading the pose model from {MODEL_URL} ...", file=sys.stderr)
        tmp = MODEL_PATH.with_suffix(".part")
        urllib.request.urlretrieve(MODEL_URL, tmp)
        os.replace(tmp, MODEL_PATH)
    return str(MODEL_PATH)


def track(video: str, model: str, step: int = 2, t_from: float = 0.0, t_to: float = 1e9, tick=None):
    """(times, landmarks[n, 33, 3] as x px, y px, visibility) for every step-th frame.
    tick: called once a frame (the power guard's battery check; it limits itself to every 30 s)."""
    os.environ.setdefault("GLOG_minloglevel", "2")  # MediaPipe's own start-up chatter
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    import cv2
    import mediapipe as mp
    from mediapipe.tasks import python as mpt
    from mediapipe.tasks.python import vision

    lm = vision.PoseLandmarker.create_from_options(vision.PoseLandmarkerOptions(
        base_options=mpt.BaseOptions(model_asset_path=model), running_mode=vision.RunningMode.VIDEO))
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise SystemExit(f"{video}: can't open it")
    cap.set(cv2.CAP_PROP_POS_MSEC, t_from * 1000)
    times, marks, n = [], [], 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
        if t > t_to:
            break
        n += 1
        if tick:
            tick()
        if n % step:
            continue
        rgb = cv2.cvtColor(cv2.resize(frame, (FRAME_W, FRAME_H)), cv2.COLOR_BGR2RGB)
        result = lm.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), int(t * 1000))
        if result.pose_landmarks:
            times.append(t)
            marks.append([(q.x * FRAME_W, q.y * FRAME_H, q.visibility) for q in result.pose_landmarks[0]])
    cap.release()
    lm.close()
    return np.array(times), np.array(marks)


# ---------- measures (numpy only) ----------

def smooth(x: np.ndarray, k: int = 5) -> np.ndarray:
    if len(x) < k:
        return x
    pad = np.pad(x, (k // 2, k // 2), mode="edge")
    return np.convolve(pad, np.ones(k) / k, mode="valid")


def joints(marks: np.ndarray) -> dict[str, np.ndarray]:
    """2 x n arrays (x, y) per joint, from the side the camera sees best."""
    near_right = np.median(marks[:, RIGHT["kn"], 2]) >= np.median(marks[:, RIGHT["kn"] - 1, 2])
    out = {}
    for name, i in RIGHT.items():
        j = i if near_right else i - 1
        out[name] = np.vstack([smooth(marks[:, j, 0]), smooth(marks[:, j, 1])])
    return out


def from_vertical(p: np.ndarray, q: np.ndarray) -> np.ndarray:
    """Angle of p->q from vertical, degrees, positive when q is toward the feet (the stern).
    The rower faces the stern, which the hands reach at the catch."""
    return np.degrees(np.arctan2(q[0] - p[0], p[1] - q[1]))


def joint_angle(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    v1, v2 = a - b, c - b
    cos = (v1 * v2).sum(0) / (np.hypot(*v1) * np.hypot(*v2) + 1e-9)
    return np.degrees(np.arccos(np.clip(cos, -1, 1)))


def signals(marks: np.ndarray) -> dict[str, np.ndarray]:
    J = joints(marks)
    # Which way the rower faces: the hands sit on the feet's side of the hips at the catch.
    facing = 1.0 if np.median(J["an"][0] - J["hp"][0]) > 0 else -1.0
    flip = lambda p: np.vstack([p[0] * facing, p[1]])  # noqa: E731 - make "toward the feet" +x
    J = {k: flip(v) for k, v in J.items()}
    return {"trunk": from_vertical(J["hp"], J["sh"]), "knee": joint_angle(J["hp"], J["kn"], J["an"]),
            "elbow": joint_angle(J["sh"], J["el"], J["wr"]), "shin": from_vertical(J["an"], J["kn"]),
            "hands": J["wr"][0]}


def peaks(x: np.ndarray, min_gap: int, prominence: float) -> list[int]:
    """Indices of maxima: each the highest point within min_gap samples either side, and at least
    prominence above the low points between it and its neighbouring maxima."""
    n = len(x)
    cand = [i for i in range(n) if x[i] == x[max(0, i - min_gap):i + min_gap + 1].max()]
    cand = [i for k, i in enumerate(cand) if k == 0 or i - cand[k - 1] >= min_gap]
    out = []
    for k, i in enumerate(cand):
        left = x[cand[k - 1]:i + 1].min() if k else x[:i + 1].min()
        right = x[i:cand[k + 1] + 1].min() if k + 1 < len(cand) else x[i:].min()
        if x[i] - max(left, right) >= prominence:
            out.append(i)
    return out


def strokes(t: np.ndarray, S: dict[str, np.ndarray]) -> list[dict]:
    """One dict per stroke (catch to next catch)."""
    if len(t) < 10:
        return []
    dt = float(np.median(np.diff(t)))
    catches = peaks(S["hands"], max(2, int(MIN_PERIOD_S / dt)), CATCH_PROMINENCE_PX)
    tr, kn, el = S["trunk"], S["knee"], S["elbow"]
    out = []
    for a, b in zip(catches[:-1], catches[1:]):
        fin = a + int(np.argmin(S["hands"][a:b]))
        if fin <= a or fin >= b:
            continue
        drive_kn = kn[a:fin + 1]
        kmin, kmax = kn[a], drive_kn.max()
        legs = a + int(np.argmax(drive_kn >= kmin + LEGS_DONE * (kmax - kmin)))
        swing = a + int(np.argmax(tr[a:fin + 1] < tr[a] - SWING_DEG))
        bent = el[a:fin + 1] < ARMS_BENT_DEG
        arms = a + int(np.argmax(bent)) if bent.any() else fin
        rising = kn[fin:b] < kmax - KNEES_UP_DEG
        up = fin + int(np.argmax(rising)) if rising.any() else b
        out.append({"t": round(float(t[a]), 3), "finish_t": float(t[fin]),
                    "drive": round(float(t[fin] - t[a]), 3), "recovery": round(float(t[b] - t[fin]), 3),
                    "catch_lean": round(float(tr[a]), 1), "finish_lean": round(float(tr[fin]), 1),
                    "shin": round(float(S["shin"][a]), 1),
                    "swing_vs_legs": round(float(t[swing] - t[legs]), 3),
                    "arms_vs_legs": round(float(t[arms] - t[legs]), 3),
                    "lean_at_knees_up": round(float(tr[up]), 1), "elbow_at_knees_up": round(float(el[up]), 1)})
    return out


# ---------- lining up with the pulley ----------

def align(video_finishes: list[float], session_t: list[float]) -> tuple[float, int] | None:
    """Offset (session time = video time + offset) that pairs the most video finishes with
    pulley strokes, and how many it pairs. None when fewer than 10 pair.

    The pulley's stroke time is its 'b' record, a fixed ~0.3 s after the finish; the offset
    absorbs that, which is all that matters for pairing strokes."""
    v = np.asarray(video_finishes)
    s = np.asarray(sorted(session_t))
    if len(v) < 10 or len(s) < 10:
        return None

    def score(off: float) -> tuple[int, float, float]:
        """(strokes paired, -mean distance of the pairs, median distance)"""
        j = np.clip(np.searchsorted(s, v + off), 1, len(s) - 1)
        d = np.where(np.abs(s[j] - (v + off)) < np.abs(s[j - 1] - (v + off)), s[j], s[j - 1]) - (v + off)
        ok = np.abs(d) < ALIGN_TOL_S
        if not ok.any():
            return 0, 0.0, 0.0
        return int(ok.sum()), -float(np.abs(d[ok]).mean()), float(np.median(d[ok]))

    # Candidate offsets: every pulley stroke against the first few video finishes. Most pairs wins;
    # among equals, the closest fit (strokes vary in length, which pins down the true match).
    candidates = {round(si - vi, 2) for vi in v[:5] for si in s}
    best = max(candidates, key=lambda off: score(off)[:2])
    n, _, fine = score(best)
    if n < 10:
        return None
    off = best + fine
    return round(off, 3), score(off)[0]


def attach(rows: list[dict], strokes_file: str, label: str) -> str:
    """Line the clip up with the session and copy the pulley's numbers onto each stroke."""
    data = json.loads(Path(strokes_file).read_text(encoding="utf-8"))
    cols = data.get("columns", [])
    pulley = [dict(zip(cols, r)) for r in data.get("rows", [])]
    if len(rows) < 10:
        return f"{label}: only {len(rows)} strokes; at least 10 are needed to line it up with the session"
    got = align([r["finish_t"] for r in rows], [p["t"] for p in pulley])
    if not got:
        return f"{label}: couldn't line it up with {Path(strokes_file).name} (is it the same row?)"
    off, n = got
    times = np.array([p["t"] for p in pulley])
    for r in rows:
        k = int(np.argmin(np.abs(times - (r["finish_t"] + off))))
        if abs(times[k] - (r["finish_t"] + off)) < ALIGN_TOL_S:
            p = pulley[k]
            r.update(session_t=p["t"], power_w=p.get("power_w"), spm=p.get("spm"),
                     length_cm=p.get("length_cm"), flag_drive_s=p.get("flag_drive_s"))
    return (f"{label}: lined up with {Path(strokes_file).name} (session time = video time {off:+.1f} s); "
            f"{n} of {len(rows)} strokes paired")


# ---------- report ----------

def summary(rows: list[dict], by: float) -> list[dict]:
    if not rows:
        return []
    t0 = rows[0]["t"]
    groups: dict[int, list[dict]] = {}
    for r in rows:
        groups.setdefault(int((r["t"] - t0) // by), []).append(r)
    out = []
    for k, g in sorted(groups.items()):
        if len(g) < 4:
            continue
        med = lambda f: statistics.median(r[f] for r in g)  # noqa: E731
        span = g[-1]["t"] - g[0]["t"]
        row = {"from_s": round(g[0]["t"]), "strokes": len(g), "spm": round(60 * (len(g) - 1) / span, 1) if span else None}
        for f in FIELDS[2:11]:
            row[f] = round(med(f), 2)
        powers = [r["power_w"] for r in g if r.get("power_w")]
        row["power_w"] = round(statistics.median(powers)) if powers else None
        out.append(row)
    return out


def print_summary(label: str, table: list[dict]) -> None:
    print(f"\n{label}")
    head = (f"{'from':>6} {'n':>3} {'spm':>5} {'W':>4} {'drive':>6} {'recov':>6} {'catch':>6} {'layback':>8} "
            f"{'back opens':>11} {'arms bend':>10} {'body@knees':>11} {'elbow@knees':>12}")
    print(head)
    for r in table:
        print(f"{r['from_s']:>5}s {r['strokes']:>3} {r['spm'] or 0:>5.1f} {r['power_w'] or '':>4} "
              f"{r['drive']:>6.2f} {r['recovery']:>6.2f} {r['catch_lean']:>5.0f}° {r['finish_lean']:>7.0f}° "
              f"{r['swing_vs_legs']:>+10.2f}s {r['arms_vs_legs']:>+9.2f}s {r['lean_at_knees_up']:>10.0f}° "
              f"{r['elbow_at_knees_up']:>11.0f}°")


def run(args, model: str, tick) -> list[dict]:
    """Track each clip, find its strokes, line it up with the session, print its summary."""
    everything = []
    for video in args.videos:
        label = Path(video).name
        t, marks = track(video, model, args.step, args.t_from, args.t_to, tick)
        if len(t) < 30:
            print(f"\n{label}: no rower found in enough frames (is the whole body in shot?)")
            continue
        rows = strokes(t, signals(marks))
        for r in rows:
            r["clip"] = label
        if args.strokes:
            print(attach(rows, args.strokes, label))
        table = summary(rows, args.by) or summary(rows, 1e9)  # too few per group: one line for the clip
        print_summary(f"{label}: {len(rows)} strokes", table)
        everything += rows
    return everything


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("videos", nargs="+")
    ap.add_argument("--strokes", help="the session's .strokes.json, to line each clip up with the pulley")
    ap.add_argument("--step", type=int, default=2, help="track every Nth frame (default 2: 30 of 60 fps)")
    ap.add_argument("--from", dest="t_from", type=float, default=0.0, help="start this many seconds into each clip")
    ap.add_argument("--to", dest="t_to", type=float, default=1e9, help="stop this many seconds into each clip")
    ap.add_argument("--by", type=float, default=60.0, help="summary groups, seconds (default 60)")
    ap.add_argument("--model", help="pose model file (default: download Google's heavy model once)")
    ap.add_argument("--out", help="folder for per-stroke CSV and JSON")
    ap.add_argument("--battery-min", type=int, default=50,
                    help="on battery, Best performance only at or above this %% (default 50); below: Balanced")
    ap.add_argument("--no-power-guard", action="store_true",
                    help="leave sleep and power mode alone (by default, on Windows: no sleep while it runs, "
                         "Best performance when plugged in or on enough battery, put back after)")
    args = ap.parse_args()
    if args.strokes and not Path(args.strokes).is_file():  # check now, not after an hour of tracking
        ap.error(f"no strokes file {args.strokes}")

    model = model_path(args.model)
    from power_guard import PowerGuard
    with PowerGuard(battery_min=args.battery_min) if not args.no_power_guard else nullcontext() as guard:
        everything = run(args, model, guard.check if guard else None)
    if args.out and everything:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        with open(out / "pose_strokes.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(everything)
        (out / "pose_strokes.json").write_text(json.dumps(everything, indent=1), encoding="utf-8")
        print(f"\nPer-stroke results: {out / 'pose_strokes.csv'}")


if __name__ == "__main__":
    main()
