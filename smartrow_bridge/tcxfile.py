# SPDX-License-Identifier: GPL-3.0-only
"""A TCX activity file for one saved session: what Strava receives.

Shaped like the SmartRow app's own Strava uploads: no device ("Creator") in the file and
Sport "Other", the activity type being set on Strava afterwards (strava.py); the description
starts "Rowed using SmartRow". Unlike SmartRow's, it has a point per stroke, a lap for each
effort (Active) and one for the rowing between them (Resting) (activity.py). Standard library only.

Per point: time, distance from the session's start, heart rate (when the bridge had a
strap), power and stroke rate (as RunCadence, as SmartRow does).
"""

from __future__ import annotations

from datetime import datetime, timezone

from .activity import lap_strokes, laps
from .sessions import distance_offset

NS = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
TPX = "http://www.garmin.com/xmlschemas/ActivityExtension/v2"


def _time(epoch_s: float) -> str:
    return datetime.fromtimestamp(round(epoch_s), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build(record: dict, strokes: list[dict]) -> bytes:
    """The TCX file for a saved session with its strokes."""
    if not strokes:
        raise ValueError("no strokes saved for this session")
    t0 = float(record["started_at"])
    offset = distance_offset([s["distance_m"] for s in strokes])

    def dist(s: dict) -> int:
        return max(0, s["distance_m"] - offset)

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<TrainingCenterDatabase xmlns="{NS}" xmlns:ns3="{TPX}">',
           " <Activities>",
           '  <Activity Sport="Other">',
           f"   <Id>{_time(t0)}</Id>"]
    lap_list = laps(record, strokes)
    prev_end = 0
    for lap, inside in zip(lap_list, lap_strokes(lap_list, strokes)):
        end_d = dist(inside[-1]) if inside else prev_end
        hrs = [s["heart_rate"] for s in inside if s.get("heart_rate")]
        out += [f'   <Lap StartTime="{_time(t0 + lap["start"])}">',
                f'    <TotalTimeSeconds>{lap["end"] - lap["start"]:.1f}</TotalTimeSeconds>',
                f"    <DistanceMeters>{end_d - prev_end}</DistanceMeters>",
                "    <Calories>0</Calories>"]
        if hrs:
            out += [f"    <AverageHeartRateBpm><Value>{round(sum(hrs) / len(hrs))}</Value></AverageHeartRateBpm>",
                    f"    <MaximumHeartRateBpm><Value>{max(hrs)}</Value></MaximumHeartRateBpm>"]
        out += [f'    <Intensity>{"Active" if lap["interval"] else "Resting"}</Intensity>',
                "    <TriggerMethod>Manual</TriggerMethod>",
                "    <Track>"]
        for s in inside:
            point = [f"      <Time>{_time(t0 + s['t'])}</Time>",
                     f"      <DistanceMeters>{dist(s)}</DistanceMeters>"]
            if s.get("heart_rate"):
                point.append(f"      <HeartRateBpm><Value>{int(s['heart_rate'])}</Value></HeartRateBpm>")
            point.append(f'      <Extensions><ns3:TPX><ns3:Watts>{round(s["power_w"])}</ns3:Watts>'
                         f'<ns3:RunCadence>{round(s["spm"])}</ns3:RunCadence></ns3:TPX></Extensions>')
            out += ["     <Trackpoint>", *point, "     </Trackpoint>"]
        out += ["    </Track>", "   </Lap>"]
        prev_end = end_d
    out += ["  </Activity>", " </Activities>", "</TrainingCenterDatabase>", ""]
    return "\n".join(out).encode("utf-8")
