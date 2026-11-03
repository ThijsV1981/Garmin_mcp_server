"""Push planned workouts to the intervals.icu calendar.

intervals.icu then sends them to Garmin Connect (Settings -> Connections ->
Garmin -> upload planned workouts), so they show up on the watch.

Events are written with the bulk endpoint and upsert=true, keyed on our own
external_id. Re-sending the same plan overwrites instead of duplicating.
Workouts we created earlier (external_id starting with PREFIX) that are no
longer in the plan can be removed within a date range ("prune"). Anything
you planned yourself in intervals.icu is never touched.

Environment:
  INTERVALS_API_KEY   intervals.icu -> Settings -> Developer Settings -> API key
"""

from __future__ import annotations

import base64
import datetime
import os

import httpx

BASE = "https://intervals.icu/api/v1/athlete/0"  # 0 = athlete that owns the API key
PREFIX = "lr-"
TYPES = {"Run", "Ride", "Swim", "TrailRun", "WeightTraining"}


class IntervalsError(RuntimeError):
    pass


def _client() -> httpx.Client:
    key = os.environ.get("INTERVALS_API_KEY", "").strip()
    if not key:
        raise IntervalsError(
            "INTERVALS_API_KEY is not set on the server. "
            "Set it with: fly secrets set INTERVALS_API_KEY=..."
        )
    return httpx.Client(auth=("API_KEY", key), timeout=30)


def _check(resp: httpx.Response) -> None:
    if resp.status_code == 401 or resp.status_code == 403:
        raise IntervalsError("intervals.icu rejected the API key (401/403). Check INTERVALS_API_KEY.")
    if resp.status_code >= 400:
        raise IntervalsError(f"intervals.icu error {resp.status_code}: {resp.text[:300]}")


def _day(value: str) -> str:
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError(f"dates must be YYYY-MM-DD, got {value!r}") from None


def _event(w: dict) -> dict:
    ext = str(w.get("external_id") or "")
    if not ext.startswith(PREFIX):
        raise ValueError(f"external_id must start with {PREFIX!r}, got {ext!r}")
    typ = w.get("type") or "Run"
    if typ not in TYPES:
        raise ValueError(f"type must be one of {sorted(TYPES)}, got {typ!r}")
    ev = {
        "category": "WORKOUT",
        "start_date_local": f"{_day(w['date'])}T00:00:00",
        "type": typ,
        "name": str(w.get("name") or "Workout")[:80],
        "external_id": ext,
    }
    fit = w.get("fit_base64")
    if fit:
        base64.b64decode(fit, validate=True)  # fail early on a broken payload
        ev["filename"] = f"{ext}.fit"
        ev["file_contents_base64"] = fit
    elif w.get("description"):
        ev["description"] = str(w["description"])
    else:
        raise ValueError(f"{ext}: give fit_base64 or description")
    return ev


def list_workouts(oldest: str, newest: str) -> list[dict]:
    with _client() as c:
        r = c.get(f"{BASE}/events", params={"oldest": _day(oldest), "newest": _day(newest), "category": "WORKOUT"})
        _check(r)
        return [
            {
                "id": e.get("id"),
                "date": str(e.get("start_date_local", ""))[:10],
                "type": e.get("type"),
                "name": e.get("name"),
                "external_id": e.get("external_id"),
                "moving_time_s": e.get("moving_time"),
                "distance_m": e.get("distance"),
            }
            for e in r.json()
        ]


def upsert_workouts(workouts: list[dict], prune_from: str | None, prune_to: str | None) -> dict:
    events = [_event(w) for w in workouts]
    ids = {e["external_id"] for e in events}
    if len(ids) != len(events):
        raise ValueError("external_id values must be unique")
    with _client() as c:
        written = []
        if events:
            r = c.post(f"{BASE}/events/bulk", params={"upsert": "true"}, json=events)
            _check(r)
            written = [
                {"date": str(e.get("start_date_local", ""))[:10], "name": e.get("name"), "external_id": e.get("external_id")}
                for e in r.json()
            ]
        removed = []
        if prune_from and prune_to:
            r = c.get(f"{BASE}/events", params={"oldest": _day(prune_from), "newest": _day(prune_to), "category": "WORKOUT"})
            _check(r)
            for e in r.json():
                ext = e.get("external_id") or ""
                if ext.startswith(PREFIX) and ext not in ids:
                    d = c.delete(f"{BASE}/events/{e['id']}")
                    _check(d)
                    removed.append({"date": str(e.get("start_date_local", ""))[:10], "name": e.get("name"), "external_id": ext})
    return {"written": len(written), "removed": len(removed), "workouts": written, "removed_workouts": removed}
