#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Drive and recovery times from the pulley's drive flag, read from a sniff log.

The pulley sends a 17-byte record about every 0.1 s. In bytes 1-5 (the distance
digits) protocol.decode_packet() treats the high nibble as V3 scrambling and
throws it away. In captures/sniff2.log one bit of it (0x10 on byte 3) is set for
a run of 0.6-1.0 s before every stroke's force curve (x/y/z records), and clear
during the rest of the cycle: it looks like a drive flag. This tool measures
those runs so the idea can be checked against deliberate slow and quick drives.

Resolution is one record gap (about 0.09-0.12 s) at each end of the drive.

Usage:
    python3 tools/drive_flag.py SNIFF.log [--marks "60:slow drives,120:quick drives"]
"""

from __future__ import annotations

import argparse
import re
import statistics as st

LINE = re.compile(r"\s*([\d.]+)\s+0x[0-9A-Fa-f]{4}\s+1236\s+len=17\s+((?:[0-9a-f]{2} ){17})")
FLAG_BYTE, FLAG_BIT = 3, 0x10


def read_records(path: str) -> list[tuple[float, bytes]]:
    out = []
    with open(path, errors="replace") as fh:
        for line in fh:
            m = LINE.match(line)
            if m:
                out.append((float(m.group(1)), bytes.fromhex(m.group(2))))
    return out


def drive_runs(records):
    """(start, end, curve_follows) per flag run; end = first record without the flag."""
    runs, start = [], None
    for i, (t, rec) in enumerate(records):
        on = bool(rec[FLAG_BYTE] & FLAG_BIT)
        if on and start is None:
            start = t
        elif not on and start is not None:
            curve = any(r[0] == ord("x") for _, r in records[i : i + 3])
            runs.append((start, t, curve))
            start = None
    return runs


def other_bits(records):
    """Report any other high-nibble bit in bytes 1-5 that ever differs from 0x40."""
    seen = {}
    for _, rec in records:
        for i in range(1, 6):
            hi = rec[i] & 0xF0
            if hi != 0x40 and not (i == FLAG_BYTE and hi == 0x50):
                seen.setdefault((i, hi), 0)
                seen[(i, hi)] += 1
    return seen


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log")
    ap.add_argument("--marks", default="", help='section starts, e.g. "60:slow drives,120:quick drives" (seconds into the log)')
    args = ap.parse_args()

    records = read_records(args.log)
    runs = drive_runs(records)
    gaps = [b[0] - a[0] for a, b in zip(records, records[1:])]
    print(f"{len(records)} records, gap median {st.median(gaps):.3f} s; {len(runs)} flag runs, "
          f"{sum(r[2] for r in runs)} followed by a force curve")
    odd = other_bits(records)
    print("other high-nibble patterns:", odd if odd else "none")

    marks = []
    for part in filter(None, (p.strip() for p in args.marks.split(","))):
        sec, _, label = part.partition(":")
        marks.append((float(sec), label or sec))
    marks.sort()

    def section(t: float) -> str:
        label = "start"
        for sec, name in marks:
            if t >= sec:
                label = name
        return label

    rows = []
    for a, b in zip(runs, runs[1:]):
        cycle = b[0] - a[0]
        if cycle > 6:  # a pause, not a stroke
            continue
        drive = a[1] - a[0]
        rows.append((a[0], drive, cycle - drive, 60 / cycle, section(a[0])))

    print(f"\n{'time':>7} {'drive':>6} {'recov':>6} {'ratio':>6} {'spm':>5}  section")
    for t, d, r, spm, sec in rows:
        print(f"{t:7.1f} {d:6.2f} {r:6.2f} {r / d if d else 0:6.2f} {spm:5.1f}  {sec}")

    print("\nBy section (medians):")
    for name in dict.fromkeys(s for *_, s in rows):
        sel = [x for x in rows if x[4] == name]
        if len(sel) < 2:
            continue
        print(f"  {name:<20} n={len(sel):3d}  drive {st.median(x[1] for x in sel):.2f} s  "
              f"recovery {st.median(x[2] for x in sel):.2f} s  ratio 1:{st.median(x[2] / x[1] for x in sel if x[1]):.1f}  "
              f"{st.median(x[3] for x in sel):.1f} spm")


if __name__ == "__main__":
    main()
