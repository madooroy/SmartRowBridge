# SPDX-License-Identifier: GPL-3.0-only
"""A FIT activity file for one saved session: what intervals.icu and Strava receive.

Made from the session's summary (sessions.build_record) and its strokes
(sessions.read_strokes), with fit-tool (https://bitbucket.org/stagescycling/python_fit_tool,
BSD). Contents:

- one record per stroke: time, distance, speed, power, stroke rate (cadence), heart rate
  (when the bridge had a strap) and stroke length;
- laps: each interval the coach found, and the steady rowing between them, so the
  efforts show up as laps (intervals) in intervals.icu and Strava;
- the session: indoor rowing, with totals and averages.
"""

from __future__ import annotations

import time

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.device_info_message import DeviceInfoMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import (
    Activity, Event, EventType, FileType, Intensity, LapTrigger, Manufacturer, SessionTrigger, Sport, SubSport,
)

from .activity import lap_strokes, laps  # noqa: E402,F401 (laps: also used by tests)

PRODUCT = 0x5352  # "SR": SmartRowBridge, under the "development" manufacturer id
SOFTWARE_VERSION = 1.0
FIT_EPOCH = 631065600  # 1989-12-31T00:00:00Z


def _ms(epoch_s: float) -> int:
    return round(epoch_s * 1000)


def _stats(strokes: list[dict]) -> dict:
    powers = [s["power_w"] for s in strokes]
    hrs = [s["heart_rate"] for s in strokes if s.get("heart_rate")]
    rates = [s["spm"] for s in strokes]
    return {
        "avg_power": round(sum(powers) / len(powers)) if powers else None,
        "max_power": round(max(powers)) if powers else None,
        "avg_hr": round(sum(hrs) / len(hrs)) if hrs else None,
        "max_hr": max(hrs) if hrs else None,
        "avg_rate": round(sum(rates) / len(rates)) if rates else None,
        "max_rate": round(max(rates)) if rates else None,
    }


def build(record: dict, strokes: list[dict]) -> bytes:
    """The FIT file for a saved session with its strokes."""
    if not strokes:
        raise ValueError("no strokes saved for this session")
    from .sessions import distance_offset
    t0 = float(record["started_at"])  # wall time of the first stroke
    t_end = t0 + strokes[-1]["t"]
    offset = distance_offset([s["distance_m"] for s in strokes])  # the session starts at 0 m
    b = FitFileBuilder(auto_define=True, min_string_size=50)

    fid = FileIdMessage()
    fid.type = FileType.ACTIVITY
    fid.manufacturer = Manufacturer.DEVELOPMENT.value
    fid.product = PRODUCT
    fid.serial_number = int(t0) & 0xFFFFFFFF
    fid.time_created = _ms(t0)
    b.add(fid)

    dev = DeviceInfoMessage()
    dev.timestamp = _ms(t0)
    dev.manufacturer = Manufacturer.DEVELOPMENT.value
    dev.product = PRODUCT
    dev.software_version = SOFTWARE_VERSION
    dev.product_name = "SmartRowBridge"
    b.add(dev)

    start = EventMessage()
    start.timestamp = _ms(t0)
    start.event = Event.TIMER
    start.event_type = EventType.START
    b.add(start)

    prev_t, prev_d = None, None
    for s in strokes:
        r = RecordMessage()
        t = t0 + s["t"]
        r.timestamp = _ms(t)
        r.distance = float(max(0, s["distance_m"] - offset))
        if prev_t is not None and t > prev_t:
            r.speed = max(0.0, (s["distance_m"] - prev_d) / (t - prev_t))
        r.power = round(s["power_w"])
        r.cadence = round(s["spm"])
        if s.get("heart_rate"):
            r.heart_rate = int(s["heart_rate"])
        if s.get("length_cm"):
            r.cycle_length16 = s["length_cm"] / 100
        b.add(r)
        prev_t, prev_d = t, s["distance_m"]

    lap_list = laps(record, strokes)
    per_lap = lap_strokes(lap_list, strokes)
    prev_end_d = 0.0
    for n, (lap, inside) in enumerate(zip(lap_list, per_lap)):
        st = _stats(inside)
        m = LapMessage()
        m.message_index = n
        m.timestamp = _ms(t0 + lap["end"])
        m.start_time = _ms(t0 + lap["start"])
        m.event = Event.LAP
        m.event_type = EventType.STOP
        m.lap_trigger = LapTrigger.SESSION_END if n == len(lap_list) - 1 else LapTrigger.MANUAL
        m.sport = Sport.ROWING
        m.sub_sport = SubSport.INDOOR_ROWING
        m.intensity = Intensity.ACTIVE if lap["interval"] else Intensity.RECOVERY
        m.total_elapsed_time = lap["end"] - lap["start"]
        m.total_timer_time = lap["end"] - lap["start"]
        end_d = float(max(0, inside[-1]["distance_m"] - offset)) if inside else prev_end_d
        m.total_distance = end_d - prev_end_d  # laps add up to the session's distance
        prev_end_d = end_d
        m.total_cycles = len(inside)
        _set_averages(m, st)
        b.add(m)

    stop = EventMessage()
    stop.timestamp = _ms(t_end)
    stop.event = Event.TIMER
    stop.event_type = EventType.STOP_ALL
    b.add(stop)

    st = _stats(strokes)
    sess = SessionMessage()
    sess.message_index = 0
    sess.timestamp = _ms(t_end)
    sess.start_time = _ms(t0)
    sess.event = Event.SESSION
    sess.event_type = EventType.STOP
    sess.trigger = SessionTrigger.ACTIVITY_END
    sess.sport = Sport.ROWING
    sess.sub_sport = SubSport.INDOOR_ROWING
    sess.total_elapsed_time = t_end - t0
    sess.total_timer_time = t_end - t0
    sess.total_distance = float(max(0, strokes[-1]["distance_m"] - offset))
    sess.total_cycles = len(strokes)
    sess.first_lap_index = 0
    sess.num_laps = len(lap_list)
    _set_averages(sess, st)
    b.add(sess)

    act = ActivityMessage()
    act.timestamp = _ms(t_end)
    act.total_timer_time = t_end - t0
    act.num_sessions = 1
    # Local time of day, in FIT's seconds since 1989-12-31 (fit-tool doesn't convert this field).
    act.local_timestamp = round(t_end + time.localtime(t_end).tm_gmtoff - FIT_EPOCH)
    act.type = Activity.MANUAL
    act.event = Event.ACTIVITY
    act.event_type = EventType.STOP
    b.add(act)
    return b.build().to_bytes()


def _set_averages(m, st: dict) -> None:
    if st["avg_power"] is not None:
        m.avg_power = st["avg_power"]
        m.max_power = st["max_power"]
    if st["avg_hr"] is not None:
        m.avg_heart_rate = st["avg_hr"]
        m.max_heart_rate = st["max_hr"]
    if st["avg_rate"] is not None:
        m.avg_cadence = st["avg_rate"]
        m.max_cadence = st["max_rate"]
