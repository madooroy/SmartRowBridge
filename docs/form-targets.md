# Form targets

The single reference for the form aims the coach, the reviews and Claude give Marc.
**Rule:** a form number (angle, ratio, timing) is only given as an aim if it is in this table,
with its source. A number that isn't here is looked up in a cited source and added first, or
said plainly to be unverified. Averages of good rowers are guides, not personal prescriptions;
a rowing coach's review of Marc's own video overrides them.

| Measure | Aim | Source (checked) | Marc now | How we measure it |
|---|---|---|---|---|
| Trunk lean at the catch | about **22°** forward (world-best average 22.5°; Kleshnev suggests about ±25° as optimal) | Kleshnev, *Rowing Styles: Analysis and Optimisation*, row2k ([link](https://www.row2k.com/features/5531/rowing-styles-analysis-and-optimisation-by-dr-valery-kleshnev/)); 7 Nov 2026 | 18–19° (7 Nov); 22° early on 1 Nov | `tools/pose_video.py` `catch_lean`: hip–shoulder line from vertical, side-on video |
| Layback at the finish | about **25°** back (world-best average 25°); "11 o'clock", leaning back slightly | Kleshnev (as above); RowPerfect, *How much layback is needed at the finish on ergs* ([link](https://rowperfect.co.uk/how-much-layback-is-needed-finish-position-on-ergs)); 7 Nov 2026 | 37–40° (7 Nov) | `finish_lean` (negative = behind vertical) |
| Drive : recovery | about **1:2** at low rates (nearer 1:1 only at high rates) | Ready All Row, *Ratio* ([link](https://readyallrow.org/tag/ratio/)); 7 Nov 2026 | 1:1.2–1.3 at ~20 spm (7 Nov) | coach rhythm (pulley flag, calibrated); pose `drive`/`recovery` |
| Recovery order | arms away, body over, then knees ("hands past the knees before the knees bend") | Standard sequence in the technique guides Marc shared (31 Oct); RowPerfect (as above) | knees lift with the body still ~5° back (7 Nov) | pose `lean_at_knees_up`; lidar seat parked after the finish |

Not set yet (needs a source before it becomes an aim): seat speed in the recovery, leg-drive time,
handle-minus-seat travel as a layback gauge (calibration only, 7 Nov: r = −0.37 within one row).

Changes to this file: say what changed, why and the source, in HANDOFF.md.
