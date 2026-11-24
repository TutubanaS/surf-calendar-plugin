#!/usr/bin/env python3
"""Find surfable windows at surf spots and publish them as iCalendar feeds plus the data for the forecast page.

Data: Open-Meteo Marine API (waves) + Open-Meteo Forecast API (wind, weather, daylight).
Stdlib only, so the GitHub Action needs no dependencies.
"""
from __future__ import annotations

import functools
import hashlib
import json
import math
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UTC = timezone.utc
INF = float("inf")
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
COUNTRIES = {"NL": "Netherlands", "PT": "Portugal", "MA": "Morocco", "TR": "Türkiye"}

MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
# Wave models, finest grid first. One model alone can be far off near the coast, so each hour uses
# the median height across them. Only some publish a peak period, hence the component fields.
MARINE_MODELS = ["ewam", "gwam", "meteofrance_wave", "ncep_gfswave025", "ecmwf_wam025"]
ACTIVE_MODELS = MARINE_MODELS      # run() narrows this when config.json names its own "wave_models"
PAST_DAYS = 0                      # run() sets this from config.json: days before today to fetch as well
MARINE_VARS = ["wave_height", "wave_direction", "wave_period", "wave_peak_period",
               "swell_wave_height", "swell_wave_peak_period", "wind_wave_height", "wind_wave_peak_period"]

# Scoring thresholds per kind of coast. "size" and "period" are step tables: (upper limit, points).
# Heights are what reaches the break: model height at sea times the spot's "exposure" and swell_reach().
# The page reads flat/best/too_big/chop_t/swell_t from here to label its charts.
PROFILES = {
    # Short-period wind swell: North Sea, Black Sea, Mediterranean. Tuned on real sessions in the Netherlands.
    "windswell": {
        "size": [(0.5, 0.0), (0.7, 0.5), (1.0, 1.0), (1.5, 2.5), (2.5, 3.5), (INF, 3.0)],   # big is usually stormy and messy
        "period": [(6, 0.0), (7, 1.0), (8, 2.0), (10, 3.0), (INF, 3.5)],
        "flat": 0.5, "mild": 0.7, "best": [1.5, 2.5], "too_big": None,   # 0.5 to 0.7 m is mild surf at Scheveningen, not flat
        "chop_t": 6, "chop_h": 1.2, "swell_t": 8,
    },
    # Open-ocean groundswell: Portugal, Morocco. A first guess that has not been checked against real sessions.
    "groundswell": {
        "size": [(0.5, 0.0), (0.8, 1.0), (1.2, 2.0), (1.6, 3.0), (2.3, 3.5), (3.0, 1.5), (INF, 0.5)],
        "period": [(7, 0.0), (9, 0.5), (11, 1.5), (13, 2.5), (INF, 3.0)],
        "flat": 0.5, "best": [1.6, 2.3], "too_big": 3.0,
        "chop_t": 7, "chop_h": 2.0, "swell_t": 11,   # on an ocean coast, sub-7 s waves under 2 m are junk
    },
}


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


class QuotaExhausted(Exception):
    """Open-Meteo will not serve more requests for a while; retrying only burns the job's time."""


WAIT_BUDGET_S = 480     # total time one run may spend waiting on rate limits (GitHub's runners share their addresses)
MIN_GAP_S = 0.9         # between request starts: about 65 a minute. At twice that rate a run on GitHub's shared
                        # runners ran into the per-minute quota and lost 46 of 117 spots
_waited = 0.0
_next_slot = 0.0
_lock = threading.Lock()


def _take_slot() -> None:
    """Requests run in parallel because each one is slow from GitHub's runners; this keeps their rate legal."""
    global _next_slot
    with _lock:
        now = time.monotonic()
        at = max(now, _next_slot)
        _next_slot = at + MIN_GAP_S
    if at > now:
        time.sleep(at - now)


def get_json(url: str, params: dict, waits=(5, 20, 61)) -> dict:
    """GET with retries. Open-Meteo answers 429 when a quota is used up: per minute, per hour or per day."""
    global _waited
    full = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full, headers={"User-Agent": "surf-windows (github.com/TutubanaS)"})
    for wait in (*waits, None):
        _take_slot()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    reason = json.load(e).get("reason", "")
                except Exception:
                    reason = ""
                if "inutely" not in reason:          # hourly or daily quota: a minute's wait will not help
                    raise QuotaExhausted(reason or "rate limited") from e
                wait = 61 if wait is not None else None
            if wait is None or e.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError):
            if wait is None:
                raise
        with _lock:
            if _waited + wait > WAIT_BUDGET_S:
                raise QuotaExhausted(f"waited {_waited:.0f} s on rate limits already")
            _waited += wait
        time.sleep(wait)
    raise AssertionError("unreachable")


def parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


@functools.lru_cache(maxsize=None)
def zone(name: str):
    """Time zone by IANA name; UTC when the zone database is missing (bare Windows Python)."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # pragma: no cover
        return UTC


def spot_zone(spot: dict):
    return zone(spot.get("tz", "Europe/Amsterdam"))


def swell_from(spot: dict) -> float:
    """Bearing the swell has to come from. Differs from facing_deg at points and bays the swell wraps into."""
    return spot.get("swell_from", spot["facing_deg"])


def good_arc(spot: dict) -> list[float]:
    return spot.get("good_swell_dir") or [(swell_from(spot) - 60) % 360, (swell_from(spot) + 60) % 360]


def swell_reach(spot: dict, wave_dir: float) -> float:
    """Share of the swell that gets in, judged by its direction.

    1 inside the spot's swell window, fading to 0 at 60 degrees outside it: swell that has to bend
    around a headland arrives smaller, and swell running along the coast barely arrives at all.
    """
    arc = good_arc(spot)
    if in_arc(wave_dir, arc):
        return 1.0
    outside = min(ang_diff(wave_dir, arc[0]), ang_diff(wave_dir, arc[1]))
    return 0.0 if outside >= 60 else math.cos(math.radians(outside * 1.5))


def load_config(root: Path = ROOT) -> dict:
    """Settings from config.json, spots from spots.json."""
    cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
    if "spots" not in cfg:
        cfg["spots"] = json.loads((root / "spots.json").read_text(encoding="utf-8"))
    return cfg


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
    wave_lo: float | None = None   # lowest / highest wave height across the models
    wave_hi: float | None = None
    n_models: int = 1
    weather: int | None = None     # WMO weather code
    temp_c: float | None = None
    tide: float | None = None      # sea level in metres against mean sea level
    sst: float | None = None       # water temperature at the surface, in Celsius
    sst: float | None = None       # water temperature at the surface, in Celsius


def fetch_spot(spot: dict, days: int) -> list[Hour]:
    marine = get_json(MARINE_URL, {
        "latitude": spot["marine"]["lat"], "longitude": spot["marine"]["lon"],
        "hourly": ",".join(MARINE_VARS), "models": ",".join(ACTIVE_MODELS),
        "forecast_days": days, "past_days": PAST_DAYS, "timezone": "GMT",
    })
    weather = get_json(WEATHER_URL, {
        "latitude": spot["beach"]["lat"], "longitude": spot["beach"]["lon"],
        "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m,weather_code,temperature_2m",
        "daily": "sunrise,sunset",
        "wind_speed_unit": "kn", "forecast_days": days, "past_days": PAST_DAYS, "timezone": "GMT",
    })
    try:   # tide is a nice-to-have. It needs its own call: the field is empty when wave models are named
        tide = get_json(MARINE_URL, {
            "latitude": spot["marine"]["lat"], "longitude": spot["marine"]["lon"],
            "hourly": "sea_level_height_msl,sea_surface_temperature", "forecast_days": days,
            "past_days": PAST_DAYS, "timezone": "GMT",
        }, waits=(5,))
    except QuotaExhausted:
        raise
    except Exception:
        tide = None
    return merge(marine, weather, tide)


def _model_suffixes(m: dict) -> list[str]:
    """Key suffixes of the wave models in an Open-Meteo hourly block ('' when only one model was requested)."""
    if "wave_height" in m:
        return [""]
    order = {f"_{name}": i for i, name in enumerate(MARINE_MODELS)}
    found = [k[len("wave_height"):] for k in m if k.startswith("wave_height_")]
    return sorted(found, key=lambda sfx: order.get(sfx, len(order)))


def _peak_period(m: dict, sfx: str, i: int) -> float | None:
    """Peak period of the dominant waves for one model and hour, or None if the model does not publish one."""
    def get(key):
        col = m.get(key + sfx)
        return col[i] if col else None
    peak = get("wave_peak_period")
    if peak is not None:
        return peak
    swell_h, swell_t = get("swell_wave_height"), get("swell_wave_peak_period")
    wind_h, wind_t = get("wind_wave_height"), get("wind_wave_peak_period")
    if swell_t is not None and (wind_t is None or (swell_h or 0) >= (wind_h or 0)):
        return swell_t
    return wind_t


def merge(marine: dict, weather: dict, tide: dict | None = None) -> list[Hour]:
    m, w = marine["hourly"], weather["hourly"]
    levels = dict(zip(tide["hourly"]["time"], tide["hourly"].get("sea_level_height_msl") or [])) if tide else {}
    water = dict(zip(tide["hourly"]["time"], tide["hourly"].get("sea_surface_temperature") or [])) if tide else {}
    water = dict(zip(tide["hourly"]["time"], tide["hourly"].get("sea_surface_temperature") or [])) if tide else {}
    wind_by_time = {t: i for i, t in enumerate(w["time"])}
    suns = [(parse_utc(a) - timedelta(minutes=30), parse_utc(b))
            for a, b in zip(weather["daily"]["sunrise"], weather["daily"]["sunset"]) if a and b]
    suffixes = _model_suffixes(m)
    codes, temps = w.get("weather_code"), w.get("temperature_2m")

    hours = []
    for i, t in enumerate(m["time"]):
        j = wind_by_time.get(t)
        if j is None or None in (w["wind_speed_10m"][j], w["wind_direction_10m"][j]):
            continue
        # a model that does not cover this sea answers 0.0 instead of null (GFS-Wave in the Black Sea)
        models = [s for s in suffixes
                  if (m["wave_height" + s][i] or 0) > 0 and m["wave_direction" + s][i] is not None]
        if not models:
            continue
        heights = [m["wave_height" + s][i] for s in models]
        # thresholds in score_hour are for peak period; the mean period (20-30% shorter) is a last resort
        periods = ([p for p in (_peak_period(m, s, i) for s in models) if p]
                   or [p for p in (m["wave_period" + s][i] for s in models if m.get("wave_period" + s)) if p])
        if not periods:
            continue
        ts = parse_utc(t)
        hours.append(Hour(
            time=ts, wave_h=round(statistics.median(heights), 2), wave_t=statistics.median(periods),
            wave_dir=m["wave_direction" + models[0]][i],
            wind_kn=w["wind_speed_10m"][j], wind_dir=w["wind_direction_10m"][j],
            gust_kn=w["wind_gusts_10m"][j] or 0.0,
            daylight=any(a <= ts <= b for a, b in suns),
            wave_lo=min(heights), wave_hi=max(heights), n_models=len(models),
            weather=codes[j] if codes else None, temp_c=temps[j] if temps else None, tide=levels.get(t), sst=water.get(t),
        ))
    return hours


# --------------------------------------------------------------------------- scoring

def _step(table: list[tuple[float, float]], x: float) -> float:
    return next(points for limit, points in table if x < limit)


def score_hour(hr: Hour, spot: dict) -> float:
    """0..10 for one hour at one spot. Model height at sea alone lies; period, direction and wind matter."""
    prof = PROFILES[spot.get("profile", "windswell")]
    h = hr.wave_h * spot.get("exposure", 1.0) * swell_reach(spot, hr.wave_dir)   # what reaches the break
    t = hr.wave_t
    pts = _step(prof["size"], h) + _step(prof["period"], t)

    # swell direction
    blocked = spot.get("blocked_swell_dir")
    if blocked and in_arc(hr.wave_dir, blocked):
        pts -= 2.0
    elif in_arc(hr.wave_dir, good_arc(spot)):
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

    # hard vetoes
    if h < prof["flat"]:
        pts = min(pts, 1.0)                     # nothing reaches the beach
    if t < prof["chop_t"] and h < prof["chop_h"]:
        pts = min(pts, 2.0)                     # short-period small stuff is flat at the beach
    if ang_diff(hr.wave_dir, swell_from(spot)) > 100:
        pts = min(pts, 1.0)                     # the waves are travelling away from the beach
    if prof.get("mild") and h < prof["mild"]:
        pts = min(pts, 5.0)                     # mild surf: just enough for the calendar, never rated higher
    if prof["too_big"] and h >= prof["too_big"]:
        pts = min(pts, 3.0)                     # out of control

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
    conf: str = ""


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

    tz = spot_zone(spot)
    local = lambda t: t.astimezone(tz)  # noqa: E731
    out = []
    for run in runs:
        if len(run) < cfg["min_window_hours"]:
            continue
        top = max(run, key=lambda x: x.score)
        start, end = run[0].time, run[-1].time + timedelta(hours=1)
        lead_days = (start - now).total_seconds() / 86400
        conf = "high" if lead_days < 1.5 else "medium" if lead_days < 3 else "low"
        spread = ""
        if top.n_models > 1 and top.wave_lo is not None:
            spread = f" {top.n_models} wave models say {top.wave_lo:.1f} to {top.wave_hi:.1f} m."
            if top.wave_hi - top.wave_lo > max(0.4, 0.5 * top.wave_h):  # models disagree: trust it less
                conf = {"high": "medium", "medium": "low"}.get(conf, conf)
        summary = (f"{label(top.score)} {spot['name']} {top.score:g}/10 · "
                   f"{top.wave_h:.1f}m {top.wave_t:.0f}s {compass(top.wave_dir)} · "
                   f"{top.wind_kn:.0f}kn {compass(top.wind_dir)}")
        lines = [f"Best {top.score:g}/10 at {local(top.time):%H:%M}. Forecast confidence: {conf}.{spread}", "",
                 "Hour (local)  score  waves            wind"]
        for x in run:
            lines.append(f"{local(x.time):%a %H:%M}    {x.score:>4g}   {x.wave_h:.1f}m {x.wave_t:4.1f}s "
                         f"{compass(x.wave_dir):>3}   {x.wind_kn:2.0f}kn (g{x.gust_kn:.0f}) "
                         f"{compass(x.wind_dir):>3} {x.wind_kind}")
        lines.append("")
        if spot.get("webcam"):
            lines.append(f"Check the webcam before you go: {spot['webcam']}")
        if spot.get("notes"):
            lines.append(f"Notes: {spot['notes']}")
        lines.append(f"Generated {now:%Y-%m-%d %H:%M} UTC from Open-Meteo.")
        uid_src = f"{spot['id']}-{start:%Y%m%dT%H}"
        out.append(Window(
            uid=hashlib.sha1(uid_src.encode()).hexdigest()[:16] + "@surf-windows",
            spot_id=spot["id"], spot_name=spot["name"],
            start=start.isoformat(), end=end.isoformat(), best=top.score,
            summary=summary, description="\n".join(lines), conf=conf,
        ))
    return out


def conditions(w: Window) -> str:
    """The wave and wind part of a window summary, e.g. '1.3m 8s NW, 6kn ESE'."""
    return ", ".join(p.strip() for p in w.summary.split("·")[1:])


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


def to_ics(windows: list[Window], now: datetime, name: str = "Surf windows",
           tz: str = "Europe/Amsterdam", site: str = "") -> str:
    """site: the forecast page; each event links to its spot there (in the notes, which Google shows, and as URL)."""
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//TutubanaS//surf-windows//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{ics_escape(name)}",
             f"X-WR-TIMEZONE:{tz}", "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
             "X-PUBLISHED-TTL:PT6H"]
    for w in sorted(windows, key=lambda x: x.start):
        link = f"{site}#spot={w.spot_id}" if site else ""
        desc = w.description + (f"\n\nFull forecast: {link}" if link else "")
        lines += ["BEGIN:VEVENT", f"UID:{w.uid}", f"DTSTAMP:{stamp}",
                  f"DTSTART:{ics_dt(w.start)}", f"DTEND:{ics_dt(w.end)}",
                  f"SUMMARY:{ics_escape(w.summary)}",
                  f"DESCRIPTION:{ics_escape(desc)}",
                  *([f"URL:{link}"] if link else []),
                  f"LOCATION:{ics_escape(w.spot_name)}",
                  "TRANSP:TRANSPARENT", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(ics_fold(l) for l in lines) + "\r\n"


def to_markdown(spot_hours: dict[str, tuple[dict, list[Hour]]], windows: list[Window],
                now: datetime, total_spots: int) -> str:
    """Plain-text forecast for the calendar spots only; the page covers the rest."""
    zones = {sid: spot_zone(spot) for sid, (spot, _) in spot_hours.items()}
    md = ["# Surf windows", "", f"_Updated {now:%Y-%m-%d %H:%M} UTC. Calendar spots only; "
          f"the forecast page has all {total_spots}._", ""]
    upcoming = [w for w in windows if datetime.fromisoformat(w.end) > now]
    md += ["## Upcoming windows", ""]
    if upcoming:
        md += ["| When (local) | Spot | Best | Conditions |", "|---|---|---|---|"]
        for w in sorted(upcoming, key=lambda x: x.start):
            tz = zones.get(w.spot_id, zone("Europe/Amsterdam"))
            s, e = (datetime.fromisoformat(x).astimezone(tz) for x in (w.start, w.end))
            cond = w.summary.split("·", 1)[1].strip()
            md.append(f"| {s:%a %d %b %H:%M}–{e:%H:%M} | {w.spot_name} | {w.best:g}/10 | {cond} |")
    else:
        md.append("Nothing surfable in the forecast. Go swimming.")
    md += ["", "## Daily best score (daylight hours)", ""]
    for sid, (spot, hours) in spot_hours.items():
        days: dict[str, Hour] = {}
        for h in hours:
            if not h.daylight:
                continue
            k = h.time.astimezone(zones[sid]).strftime("%a %d %b")
            if k not in days or h.score > days[k].score:
                days[k] = h
        md += [f"**{spot['name']}**", "", "| Day | Best | At | Waves | Wind |", "|---|---|---|---|---|"]
        for k, h in days.items():
            md.append(f"| {k} | {h.score:g} | {h.time.astimezone(zones[sid]):%H:%M} | "
                      f"{h.wave_h:.1f}m {h.wave_t:.0f}s {compass(h.wave_dir)} | "
                      f"{h.wind_kn:.0f}kn {compass(h.wind_dir)} ({h.wind_kind}) |")
        md.append("")
    return "\n".join(md)


def to_data(cfg: dict, spot_hours: dict[str, tuple[dict, list[Hour]]],
            by_spot: dict[str, list[Window]], now: datetime, previous: dict[str, dict] | None = None) -> str:
    """docs/data.js: everything the page needs, as one script that sets window.SURF.

    A script rather than JSON so the page also works when opened straight from disk.
    """
    kinds = {"offshore": "o", "onshore": "n", "cross": "c", "glassy": "g"}
    epoch = lambda iso: int(datetime.fromisoformat(iso).timestamp())  # noqa: E731
    spots = []
    for sid, (spot, all_hours) in spot_hours.items():
        # every hour for the first days, then one hour per 3-hour block: far-out days are rough anyway.
        # The block keeps its best daylight hour, so a day's best score on the page matches its windows.
        t0 = all_hours[0].time
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        fine_from = today - timedelta(days=1)       # yesterday is hourly too; older past days are coarse
        fine_to = today + timedelta(days=cfg.get("hourly_days", cfg["forecast_days"]))
        blocks: dict = {}
        for h in all_hours:
            key = h.time if fine_from <= h.time < fine_to else (h.time.date(), h.time.hour // 3)
            best = blocks.get(key)
            if best is None or (h.daylight, h.score) > (best.daylight, best.score):
                blocks[key] = h
        hours = sorted(blocks.values(), key=lambda h: h.time)
        offsets = [int((h.time - t0).total_seconds() // 3600) for h in hours]
        d = {
            "id": sid, "name": spot["name"], "country": spot.get("country", ""), "region": spot.get("region", ""),
            "tz": spot.get("tz", "Europe/Amsterdam"), "facing": spot["facing_deg"], "from": swell_from(spot),
            "lat": spot["beach"]["lat"], "lon": spot["beach"]["lon"],
            "mlat": spot["marine"]["lat"], "mlon": spot["marine"]["lon"],
            "good": good_arc(spot), "blocked": spot.get("blocked_swell_dir"),
            "profile": spot.get("profile", "windswell"), "exposure": spot.get("exposure", 1),
            "webcam": spot.get("webcam"), "notes": spot.get("notes"),
            "t0": int(t0.timestamp()), "t": None if offsets == list(range(len(hours))) else offsets,
            "score": [h.score for h in hours],
            "wh": [round(h.wave_h, 2) for h in hours], "wt": [round(h.wave_t, 1) for h in hours],
            "wd": [round(h.wave_dir) for h in hours],
            "ws": [round(h.wind_kn, 1) for h in hours],      # one decimal: the page's own scoring has thresholds at whole knots
            "wg": [round(h.gust_kn) for h in hours],
            "wdir": [round(h.wind_dir) for h in hours],
            "kind": "".join(kinds.get(h.wind_kind, "c") for h in hours),
            "day": "".join("1" if h.daylight else "0" for h in hours),
            "windows": [{"s": epoch(w.start), "e": epoch(w.end), "best": w.best, "cond": conditions(w),
                         "conf": w.conf} for w in by_spot.get(sid, [])],
        }
        if any(h.n_models > 1 for h in hours):
            d["lo"] = [round(h.wave_lo if h.wave_lo is not None else h.wave_h, 2) for h in hours]
            d["hi"] = [round(h.wave_hi if h.wave_hi is not None else h.wave_h, 2) for h in hours]
        if any(h.weather is not None for h in hours):
            d["wc"] = [-1 if h.weather is None else int(h.weather) for h in hours]
            d["tc"] = [None if h.temp_c is None else round(h.temp_c) for h in hours]
        if any(h.tide is not None for h in hours):
            d["tide"] = [None if h.tide is None else round(h.tide, 2) for h in hours]
        if any(h.sst is not None for h in hours):
            d["sst"] = [None if h.sst is None else round(h.sst, 1) for h in hours]
        if any(h.sst is not None for h in hours):
            d["sst"] = [None if h.sst is None else round(h.sst, 1) for h in hours]
        spots.append({k: v for k, v in d.items() if v is not None})
    # a spot that could not be fetched this time stays on the page with what we had, in the configured order
    fresh = {d["id"]: d for d in spots}
    wanted = [s["id"] for s in cfg["spots"] if s.get("enabled", True)]
    spots = [fresh.get(sid) or (previous or {}).get(sid) for sid in wanted]
    spots = [d for d in spots if d]
    data = {
        "generated": int(now.timestamp()), "min_score": cfg["min_score"], "forecast_days": cfg["forecast_days"],
        "min_window_hours": cfg["min_window_hours"], "past_days": cfg.get("past_days", 0),
        "defaults": cfg.get("calendar_spots") or [], "feed_base": cfg.get("feed_base", ""),
        "countries": COUNTRIES,
        # the whole scoring table goes to the page: it scores a single wave model itself when the reader picks one
        "profiles": {name: {**{k: p.get(k) for k in ("flat", "mild", "best", "too_big", "chop_t", "chop_h", "swell_t")},
                            **{k: [[None if lim == INF else lim, pts] for lim, pts in p[k]] for k in ("size", "period")}}
                     for name, p in PROFILES.items()},
        "spots": spots,
    }
    return "window.SURF=" + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n"


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
    """Fetch every spot, write all outputs, return the windows of the main calendar feed."""
    hist_path = out_dir / "data" / "windows.json"
    old = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else []
    global ACTIVE_MODELS
    ACTIVE_MODELS = cfg.get("wave_models") or MARINE_MODELS     # e.g. ["ncep_gfswave025"] to follow one model only
    global PAST_DAYS
    PAST_DAYS = cfg.get("past_days", 0)
    spots = [s for s in cfg["spots"] if s.get("enabled", True)]
    # the main feed (surf.ics), its history and FORECAST.md cover these; every spot still gets its own feed
    cal_ids = set(cfg.get("calendar_spots") or [s["id"] for s in spots])
    stop = threading.Event()         # set when the API quota is gone: the spots not yet asked for are skipped

    def load(spot: dict):
        if stop.is_set():
            return spot, None, None
        try:
            hours = fetch(spot, cfg["forecast_days"])
            if not hours:
                raise ValueError("no wave data at this point")
            return spot, hours, None
        except QuotaExhausted as e:
            stop.set()
            return spot, None, e
        except Exception as e:       # this spot keeps its old feed if the API hiccups
            return spot, None, e

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=cfg.get("fetch_workers", 6)) as pool:
        results = list(pool.map(load, spots))
    spot_hours: dict[str, tuple[dict, list[Hour]]] = {}
    by_spot: dict[str, list[Window]] = {}
    for spot, hours, err in results:
        if hours is None:
            if err is not None:
                print(f"[warn] {spot['id']}: fetch failed: {err}", file=sys.stderr)
            continue
        by_spot[spot["id"]] = find_windows(hours, spot, cfg, now)
        spot_hours[spot["id"]] = (spot, hours)
    if stop.is_set():
        print(f"[warn] API quota exhausted: only {len(spot_hours)} of {len(spots)} spots fetched; "
              "the others keep their old feeds", file=sys.stderr)
    print(f"fetched {len(spot_hours)} of {len(spots)} spots in {time.monotonic() - started:.0f} s", flush=True)
    fresh = set(spot_hours)
    if not fresh:
        raise SystemExit("All fetches failed; leaving outputs untouched.")

    # the page shows every window; calendars only get the ones near enough to trust
    horizon = (now + timedelta(days=cfg.get("calendar_days", cfg["forecast_days"]))).isoformat()
    # (and not the hindcast of the past days: the calendar keeps what was forecast at the time)
    near = {sid: [w for w in ws if w.start < horizon and w.end > now.isoformat()] for sid, ws in by_spot.items()}
    new = [w for sid, ws in near.items() if sid in cal_ids for w in ws]
    windows = merge_history([d for d in old if d["spot_id"] in cal_ids], new, fresh, now, cfg["history_days"])

    docs, cal = out_dir / "docs", out_dir / "docs" / "cal"
    (out_dir / "data").mkdir(exist_ok=True)
    cal.mkdir(parents=True, exist_ok=True)
    hist_path.write_text(json.dumps([asdict(w) for w in windows], indent=1, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    site = cfg.get("feed_base", "")
    (docs / "surf.ics").write_text(to_ics(windows, now, site=site), encoding="utf-8", newline="")
    for sid, (spot, _) in spot_hours.items():
        feed = [w for w in windows if w.spot_id == sid] if sid in cal_ids else near[sid]
        (cal / f"{sid}.ics").write_text(
            to_ics(feed, now, f"Surf: {spot['name']}", spot.get("tz", "Europe/Amsterdam"), site),
            encoding="utf-8", newline="")
    known = {s["id"] for s in spots}
    for stale in cal.glob("*.ics"):
        if stale.stem not in known:
            stale.unlink()
    (out_dir / "FORECAST.md").write_text(
        to_markdown({sid: v for sid, v in spot_hours.items() if sid in cal_ids}, windows, now, len(spot_hours))
        + "\n", encoding="utf-8")
    data_path, previous = docs / "data.js", {}
    if data_path.exists():
        try:
            text = data_path.read_text(encoding="utf-8")
            previous = {d["id"]: d for d in json.loads(text[len("window.SURF="):].rstrip().rstrip(";"))["spots"]}
        except Exception:      # an unreadable old file only means there is nothing to carry over
            previous = {}
    data_path.write_text(to_data(cfg, spot_hours, by_spot, now, previous), encoding="utf-8")
    print(f"{sum(map(len, by_spot.values()))} window(s) at {len(fresh)} of {len(spots)} spots; "
          f"{len(windows)} in the main feed.")
    return windows


if __name__ == "__main__":
    run(load_config(), datetime.now(UTC).replace(minute=0, second=0, microsecond=0))
