"""Turn an activity's original .FIT file into compact time series and laps.

The upstream server can only save the FIT file on the server's own disk, which
is useless to a remote client. This parses it in memory and returns sampled
records (time, distance, HR, speed, cadence, power, altitude, temperature)
plus lap summaries, small enough to send over MCP.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime

import fitdecode

RECORD_FIELDS = {
    "heart_rate": "hr",
    "enhanced_speed": "speed_ms",
    "speed": "speed_ms",
    "distance": "distance_m",
    "cadence": "cadence",
    "power": "power_w",
    "enhanced_altitude": "altitude_m",
    "altitude": "altitude_m",
    "temperature": "temp_c",
    "position_lat": "lat",
    "position_long": "lon",
}

LAP_FIELDS = {
    "start_time": "start",
    "total_elapsed_time": "elapsed_s",
    "total_timer_time": "timer_s",
    "total_distance": "distance_m",
    "avg_heart_rate": "avg_hr",
    "max_heart_rate": "max_hr",
    "enhanced_avg_speed": "avg_speed_ms",
    "avg_speed": "avg_speed_ms",
    "avg_cadence": "avg_cadence",
    "avg_power": "avg_power_w",
    "total_ascent": "ascent_m",
    "total_descent": "descent_m",
    "lap_trigger": "trigger",
}

SEMICIRCLE = 180.0 / 2**31


def _fit_bytes(content: bytes) -> bytes:
    if content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for name in archive.namelist():
                if name.lower().endswith(".fit"):
                    return archive.read(name)
        raise ValueError("no .fit file in the activity archive")
    return content


def _clean(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float):
        return round(value, 3)
    return value


def parse_fit(content: bytes, interval_s: int = 10, include_gps: bool = False) -> dict:
    interval_s = max(1, int(interval_s))
    records: list[dict] = []
    laps: list[dict] = []
    start: datetime | None = None
    last_kept: float | None = None

    with fitdecode.FitReader(io.BytesIO(_fit_bytes(content))) as fit:
        for frame in fit:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            if frame.name == "record":
                ts = frame.get_value("timestamp", fallback=None)
                if ts is None:
                    continue
                if start is None:
                    start = ts
                t = (ts - start).total_seconds()
                if last_kept is not None and t - last_kept < interval_s:
                    continue
                last_kept = t
                row: dict = {"t": int(t)}
                for field, key in RECORD_FIELDS.items():
                    if key in row:
                        continue
                    if key in ("lat", "lon") and not include_gps:
                        continue
                    value = frame.get_value(field, fallback=None)
                    if value is None:
                        continue
                    if key in ("lat", "lon"):
                        value = value * SEMICIRCLE
                    row[key] = _clean(value)
                records.append(row)
            elif frame.name == "lap":
                lap: dict = {}
                for field, key in LAP_FIELDS.items():
                    if key in lap:
                        continue
                    value = frame.get_value(field, fallback=None)
                    if value is not None:
                        lap[key] = _clean(value)
                laps.append(lap)

    return {
        "start": start.isoformat() if start else None,
        "interval_s": interval_s,
        "fields": {"t": "seconds since start", "speed_ms": "m/s", "distance_m": "metres"},
        "laps": laps,
        "records": records,
    }
