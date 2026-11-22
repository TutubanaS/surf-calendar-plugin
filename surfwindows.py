#!/usr/bin/env python3
"""Find surfable windows on the Dutch coast and publish them as an iCalendar feed.

Data: Open-Meteo Marine API (waves) + Open-Meteo Forecast API (wind, daylight).
Stdlib only, so the GitHub Action needs no dependencies.
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UTC = timezone.utc
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"


# --------------------------------------------------------------------------- helpers

def compass(deg: float) -> str:
    return COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


def ang_diff(a: float, b: float) -> float:
    """Smallest absolute difference between two bearings (0..180)."""
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def in_arc(deg: float, arc: list[float]) -> bool:
    """True if bearing lies in arc [start, end] going clockwise (wraps over N)."""
    start, end = arc[0] % 360, arc[1] % 360
    deg %= 360
    return start <= deg <= end if start <= end else deg >= start or deg <= end


def get_json(url: str, params: dict) -> dict:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full, headers={"User-Agent": "surf-windows (github.com/TutubanaS)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


# --------------------------------------------------------------------------- data

@dataclass
class Hour:
    time: datetime
    wave_h: float
    wave_t: float
    wave_dir: float
    wind_kn: float
    wind_dir: float
    gust_kn: float
    daylight: bool
    score: float = 0.0
    wind_kind: str = ""


def fetch_spot(spot: dict, days: int) -> list[Hour]:
    marine = get_json(MARINE_URL, {
        "latitude": spot["marine"]["lat"], "longitude": spot["marine"]["lon"],
        "hourly": "wave_height,wave_direction,wave_period,wave_peak_period",
        "forecast_days": days, "timezone": "GMT",
    })
    weather = get_json(WEATHER_URL, {
        "latitude": spot["beach"]["lat"], "longitude": spot["beach"]["lon"],
        "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
        "daily": "sunrise,sunset",
        "wind_speed_unit": "kn", "forecast_days": days, "timezone": "GMT",
    })
    return merge(marine, weather)


def merge(marine: dict, weather: dict) -> list[Hour]:
    m, w = marine["hourly"], weather["hourly"]
    wind_by_time = {t: i for i, t in enumerate(w["time"])}
    suns = [(parse_utc(a) - timedelta(minutes=30), parse_utc(b))
            for a, b in zip(weather["daily"]["sunrise"], weather["daily"]["sunset"])]

    peak = m.get("wave_peak_period") or [None] * len(m["time"])
    hours = []
    for i, t in enumerate(m["time"]):
        j = wind_by_time.get(t)
        h, d = m["wave_height"][i], m["wave_direction"][i]
        period = peak[i] if peak[i] is not None else m["wave_period"][i]
        if j is None or None in (h, d, period, w["wind_speed_10m"][j], w["wind_direction_10m"][j]):
            continue
        ts = parse_utc(t)
        hours.append(Hour(
            time=ts, wave_h=h, wave_t=period, wave_dir=d,
            wind_kn=w["wind_speed_10m"][j], wind_dir=w["wind_direction_10m"][j],
            gust_kn=w["wind_gusts_10m"][j] or 0.0,
            daylight=any(a <= ts <= b for a, b in suns),
        ))
    return hours


# --------------------------------------------------------------------------- scoring

def score_hour(hr: Hour, spot: dict) -> float:
    """0..10. Tuned for the North Sea: offshore model height alone lies, period matters."""
    h, t = hr.wave_h, hr.wave_t

    # swell size
    if h < 0.7:    pts = 0.0
    elif h < 1.0:  pts = 1.0
    elif h < 1.5:  pts = 2.5
    elif h < 2.5:  pts = 3.5
    else:          pts = 3.0   # big + usually stormy/messy

    # period
    if t < 6:      pts += 0.0
    elif t < 7:    pts += 1.0
    elif t < 8:    pts += 2.0
    elif t < 10:   pts += 3.0
    else:          pts += 3.5

    # swell direction
    if in_arc(hr.wave_dir, spot["blocked_swell_dir"]):
        pts -= 2.0
    elif in_arc(hr.wave_dir, spot["good_swell_dir"]):
        pts += 1.0

    # wind relative to the beach
    offshore = (spot["facing_deg"] + 180) % 360
    rel = ang_diff(hr.wind_dir, offshore)
    s = hr.wind_kn
    if rel <= 45:
        hr.wind_kind = "offshore"
        pts += 2.5 if s <= 20 else 0.5
    elif rel <= 110:
        hr.wind_kind = "cross"
        pts += 1.5 if s < 8 else 0.0 if s < 14 else -2.0
    else:
        hr.wind_kind = "onshore"
        pts += 1.0 if s < 6 else -1.0 if s < 12 else -2.5 if s < 18 else -4.0
    if s < 5:
        hr.wind_kind = "glassy"

    # hard vetoes from experience: short-period small stuff = flat at the beach
    if h < 0.7:
        pts = min(pts, 1.0)
    if t < 6 and h < 1.2:
        pts = min(pts, 2.0)

    return round(max(0.0, min(10.0, pts)), 1)


def label(score: float) -> str:
    if score >= 8: return "🔥"
    if score >= 6.5: return "👍"
    return "🤏"


# --------------------------------------------------------------------------- windows

@dataclass
class Window:
    uid: str
    spot_id: str
    spot_name: str
    start: str
    end: str
    best: float
    summary: str
    description: str


def find_windows(hours: list[Hour], spot: dict, cfg: dict, now: datetime) -> list[Window]:
    for hr in hours:
        hr.score = score_hour(hr, spot)

    runs, cur = [], []
    for hr in hours:
        ok = hr.daylight and hr.score >= cfg["min_score"]
        if ok and cur and hr.time - cur[-1].time == timedelta(hours=1):
            cur.append(hr)
        elif ok:
            if cur: runs.append(cur)
            cur = [hr]
        else:
            if cur: runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)

    out = []
    for run in runs:
        if len(run) < cfg["min_window_hours"]:
            continue
        top = max(run, key=lambda x: x.score)
        start, end = run[0].time, run[-1].time + timedelta(hours=1)
        lead_days = (start - now).total_seconds() / 86400
        conf = "high" if lead_days < 1.5 else "medium" if lead_days < 3 else "low"
        summary = (f"{label(top.score)} {spot['name']} {top.score:g}/10 · "
                   f"{top.wave_h:.1f}m {top.wave_t:.0f}s {compass(top.wave_dir)} · "
                   f"{top.wind_kn:.0f}kn {compass(top.wind_dir)}")
        lines = [f"Best {top.score:g}/10 at {top.time:%H:%M} UTC. Forecast confidence: {conf}.", "",
                 "Hour (UTC)  score  waves            wind"]
        for x in run:
            lines.append(f"{x.time:%a %H:%M}  {x.score:>4g}   {x.wave_h:.1f}m {x.wave_t:4.1f}s {compass(x.wave_dir):>3}"
                         f"   {x.wind_kn:2.0f}kn (g{x.gust_kn:.0f}) {compass(x.wind_dir):>3} {x.wind_kind}")
        lines += ["", f"Check the webcam before you take the train: {spot['webcam']}",
                  f"Notes: {spot.get('notes', '')}",
                  f"Generated {now:%Y-%m-%d %H:%M} UTC from Open-Meteo."]
        uid_src = f"{spot['id']}-{start:%Y%m%dT%H}"
        out.append(Window(
            uid=hashlib.sha1(uid_src.encode()).hexdigest()[:16] + "@surf-windows",
            spot_id=spot["id"], spot_name=spot["name"],
            start=start.isoformat(), end=end.isoformat(), best=top.score,
            summary=summary, description="\n".join(lines),
        ))
    return out


# --------------------------------------------------------------------------- output

def ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics_fold(line: str) -> str:
    """Fold lines to 75 octets as RFC 5545 requires."""
    raw = line.encode()
    if len(raw) <= 75:
        return line
    parts, chunk = [], b""
    for ch in line:
        b = ch.encode()
        if len(chunk) + len(b) > (75 if not parts else 74):
            parts.append(chunk.decode())
            chunk = b""
        chunk += b
    parts.append(chunk.decode())
    return "\r\n ".join(parts)


def ics_dt(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def to_ics(windows: list[Window], now: datetime) -> str:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//TutubanaS//surf-windows//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Surf windows NL",
             "X-WR-TIMEZONE:Europe/Amsterdam", "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
             "X-PUBLISHED-TTL:PT6H"]
    for w in sorted(windows, key=lambda x: x.start):
        lines += ["BEGIN:VEVENT", f"UID:{w.uid}", f"DTSTAMP:{stamp}",
                  f"DTSTART:{ics_dt(w.start)}", f"DTEND:{ics_dt(w.end)}",
                  f"SUMMARY:{ics_escape(w.summary)}",
                  f"DESCRIPTION:{ics_escape(w.description)}",
                  f"LOCATION:{ics_escape(w.spot_name)}",
                  "TRANSP:TRANSPARENT", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(ics_fold(l) for l in lines) + "\r\n"


def to_markdown(spot_hours: dict[str, tuple[dict, list[Hour]]], windows: list[Window], now: datetime) -> str:
    md = [f"# Surf windows NL", "", f"_Updated {now:%Y-%m-%d %H:%M} UTC_", ""]
    upcoming = [w for w in windows if datetime.fromisoformat(w.end) > now]
    md += ["## Upcoming windows", ""]
    if upcoming:
        md += ["| When (Amsterdam) | Spot | Best | Conditions |", "|---|---|---|---|"]
        for w in sorted(upcoming, key=lambda x: x.start):
            s, e = (datetime.fromisoformat(x).astimezone(AMS) for x in (w.start, w.end))
            cond = w.summary.split("·", 1)[1].strip()
            md.append(f"| {s:%a %d %b %H:%M}–{e:%H:%M} | {w.spot_name} | {w.best:g}/10 | {cond} |")
    else:
        md.append("Nothing surfable in the forecast. Go swimming.")
    md += ["", "## Daily best score (daylight hours)", ""]
    for spot, hours in spot_hours.values():
        days: dict[str, Hour] = {}
        for h in hours:
            if not h.daylight:
                continue
            k = h.time.astimezone(AMS).strftime("%a %d %b")
            if k not in days or h.score > days[k].score:
                days[k] = h
        md += [f"**{spot['name']}**", "", "| Day | Best | At | Waves | Wind |", "|---|---|---|---|---|"]
        for k, h in days.items():
            md.append(f"| {k} | {h.score:g} | {h.time.astimezone(AMS):%H:%M} | "
                      f"{h.wave_h:.1f}m {h.wave_t:.0f}s {compass(h.wave_dir)} | "
                      f"{h.wind_kn:.0f}kn {compass(h.wind_dir)} ({h.wind_kind}) |")
        md.append("")
    return "\n".join(md)


try:
    from zoneinfo import ZoneInfo
    AMS = ZoneInfo("Europe/Amsterdam")
except Exception:  # pragma: no cover
    AMS = UTC


# --------------------------------------------------------------------------- main

def merge_history(old: list[dict], new: list[Window], fresh_spots: set[str],
                  now: datetime, keep_days: int) -> list[Window]:
    """Keep past windows (a log of what was forecast); replace future ones with the new run."""
    cutoff = now - timedelta(days=keep_days)
    kept = []
    for d in old:
        w = Window(**d)
        end = datetime.fromisoformat(w.end)
        if end < cutoff:
            continue
        if end > now and w.spot_id in fresh_spots:
            continue  # superseded by this run
        kept.append(w)
    new_uids = {w.uid for w in new}
    return [w for w in kept if w.uid not in new_uids] + new


def run(cfg: dict, now: datetime, fetch=fetch_spot, out_dir: Path = ROOT) -> list[Window]:
    hist_path = out_dir / "data" / "windows.json"
    old = json.loads(hist_path.read_text()) if hist_path.exists() else []

    new, spot_hours, fresh = [], {}, set()
    for spot in cfg["spots"]:
        if not spot.get("enabled", True):
            continue
        try:
            hours = fetch(spot, cfg["forecast_days"])
        except Exception as e:  # keep old windows for this spot if the API hiccups
            print(f"[warn] {spot['id']}: fetch failed: {e}", file=sys.stderr)
            continue
        fresh.add(spot["id"])
        new += find_windows(hours, spot, cfg, now)
        spot_hours[spot["id"]] = (spot, hours)

    if not fresh:
        raise SystemExit("All fetches failed; leaving outputs untouched.")

    windows = merge_history(old, new, fresh, now, cfg["history_days"])
    (out_dir / "data").mkdir(exist_ok=True)
    (out_dir / "docs").mkdir(exist_ok=True)
    hist_path.write_text(json.dumps([asdict(w) for w in windows], indent=1, ensure_ascii=False) + "\n")
    (out_dir / "docs" / "surf.ics").write_text(to_ics(windows, now), newline="")
    (out_dir / "FORECAST.md").write_text(to_markdown(spot_hours, windows, now) + "\n")
    print(f"{len(new)} new window(s), {len(windows)} in feed.")
    return windows


if __name__ == "__main__":
    config = json.loads((ROOT / "config.json").read_text())
    run(config, datetime.now(UTC).replace(minute=0, second=0, microsecond=0))
