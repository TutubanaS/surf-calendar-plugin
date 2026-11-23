#!/usr/bin/env python3
"""Find surfable windows on the Dutch coast and publish them as an iCalendar feed.

Data: Open-Meteo Marine API (waves) + Open-Meteo Forecast API (wind, daylight).
Stdlib only, so the GitHub Action needs no dependencies.
"""
from __future__ import annotations

import hashlib
import html
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


HTML_HOURS = range(6, 21)  # local Amsterdam hours shown in the day strips

HTML_CSS = """
:root{--fog:#DDE4E3;--sea:#18343F;--mist:#566B71;--rule:#C2CECD;--night:rgba(24,52,63,.07);
--flat:#B4C2C1;--ok:#6FA79C;--good:#1F6F7A;--fire:#F26B1D;--win:rgba(242,107,29,.14)}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--fog);color:var(--sea);
font:400 1rem/1.5 Archivo,system-ui,-apple-system,"Segoe UI",sans-serif;font-stretch:100%}
main{max-width:52rem;margin:0 auto;padding:2.5rem 1.25rem 4rem}
a{color:inherit;text-underline-offset:.2em}
a:focus-visible,summary:focus-visible{outline:2px solid var(--fire);outline-offset:3px;border-radius:2px}
.updated{color:var(--mist);font-size:.875rem;margin:0 0 1.75rem}
.verdict{font-weight:800;font-stretch:125%;font-size:clamp(2rem,6.2vw,3.5rem);line-height:1.04;
letter-spacing:-.015em;margin:0 0 1rem;max-width:20ch;text-wrap:balance}
.sub{font-size:1.125rem;margin:0;max-width:60ch}
.windows{list-style:none;padding:0;margin:2rem 0 0;border-top:1px solid var(--rule)}
.windows li{display:grid;grid-template-columns:13rem 1fr auto;gap:.15rem 1rem;align-items:center;
padding:.75rem 0;border-bottom:1px solid var(--rule)}
.windows .when{font-weight:600;white-space:nowrap}
.windows .where{display:flex;flex-wrap:wrap;column-gap:1rem}
.windows .cond{color:var(--mist)}
.num{font-stretch:75%;font-weight:700;font-variant-numeric:tabular-nums}
.score{font-size:1.75rem;line-height:1}
.score small{font-size:.8rem;font-weight:500;color:var(--mist)}
.band-fire{color:var(--fire)} .band-good{color:var(--good)} .band-ok{color:var(--ok)} .band-flat{color:var(--mist)}
.spot{margin-top:3.5rem}
.spot h2{font-weight:700;font-stretch:112%;font-size:1.6rem;line-height:1.1;margin:0 0 .25rem}
.spot .note{color:var(--mist);margin:0 0 1.25rem;font-size:.9375rem}
.row{display:grid;grid-template-columns:5.25rem 1fr 2.75rem;gap:.75rem;align-items:end}
.axis{font-size:.75rem;color:var(--mist);margin-bottom:.25rem}
.axis .ticks{display:grid;grid-template-columns:repeat(15,1fr)}
.axis .ticks span{text-align:left}
details{border-top:1px solid var(--rule)}
details:last-of-type{border-bottom:1px solid var(--rule)}
summary{list-style:none;cursor:pointer;padding:.55rem 0}
summary::-webkit-details-marker{display:none}
.dname{font-weight:600;font-size:.9375rem;line-height:1.2;align-self:center;display:flex;gap:.4rem;align-items:center}
.dname svg{width:.6rem;height:.6rem;flex:none;transition:transform .2s ease;fill:var(--mist)}
details[open] .dname svg{transform:rotate(90deg)}
.best{text-align:right;font-size:1.5rem;line-height:1;align-self:center}
.strip{display:grid;grid-template-columns:repeat(15,1fr);gap:2px;height:3.25rem;position:relative}
.strip::after{content:"";position:absolute;left:0;right:0;bottom:calc(var(--min) * 100%);
border-top:1px dashed var(--mist);opacity:.55;pointer-events:none}
.h{display:flex;align-items:flex-end;height:100%;border-radius:2px}
.h.night{background:var(--night)}
.h.win{background:var(--win)}
.b{width:100%;height:calc(var(--s) * 100%);min-height:2px;border-radius:2px 2px 0 0;background:var(--flat)}
.h.ok .b{background:var(--ok)} .h.good .b{background:var(--good)} .h.fire .b{background:var(--fire)}
.h.past .b{opacity:.4}
.hours{width:100%;border-collapse:collapse;font-size:.875rem;margin:.25rem 0 1rem}
.hours th{font-weight:500;color:var(--mist);text-align:left;padding:.25rem .5rem .25rem 0}
.hours td{padding:.3rem .5rem .3rem 0;border-top:1px solid var(--rule);vertical-align:middle}
.hours td.num{font-size:1rem}
.hours tr.win td:first-child{box-shadow:inset 3px 0 var(--fire);padding-left:.5rem}
.arr{width:.85rem;height:.85rem;vertical-align:-.1em;fill:var(--sea);margin-right:.2rem}
.dim{color:var(--mist)}
.legend{display:flex;flex-wrap:wrap;gap:.4rem 1.25rem;font-size:.8125rem;color:var(--mist);margin:3rem 0 0;padding:0;list-style:none}
.legend i{display:inline-block;width:.7rem;height:.7rem;border-radius:2px;margin-right:.35rem;vertical-align:-.05em}
footer{margin-top:3rem;font-size:.875rem;color:var(--mist);max-width:60ch}
footer p{margin:.5rem 0}
@media (max-width:34rem){
 .row{grid-template-columns:3.6rem 1fr 2.1rem;gap:.5rem}
 .best{font-size:1.2rem}
 .strip{gap:1px;height:2.75rem}
 .windows li{grid-template-columns:1fr auto}
 .windows .where{grid-column:1}
 .windows .score{grid-column:2;grid-row:1/3}
 .hours .hide-s{display:none}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""


def _band(score: float, min_score: float) -> str:
    if score >= 8: return "fire"
    if score >= 6.5: return "good"
    if score >= min_score: return "ok"
    return "flat"


def _arrow(from_deg: float) -> str:
    """Arrow pointing where the wind/swell is heading (it comes *from* from_deg)."""
    return (f'<svg class="arr" viewBox="0 0 12 12" aria-hidden="true" '
            f'style="transform:rotate({(from_deg + 180) % 360:.0f}deg)"><path d="M6 1 10 10 6 7.6 2 10Z"/></svg>')


def _day_name(d, today, mid_sentence: bool = False) -> str:
    delta = (d - today).days
    name = "Today" if delta == 0 else "Tomorrow" if delta == 1 else f"{d:%A}"
    return name.lower() if mid_sentence and delta in (0, 1) else name


def _conditions(w: Window) -> str:
    return ", ".join(p.strip() for p in w.summary.split("·")[1:])


def to_html(spot_hours: dict[str, tuple[dict, list[Hour]]], windows: list[Window],
            cfg: dict, now: datetime) -> str:
    esc = html.escape
    min_s = cfg["min_score"]
    local_now = now.astimezone(AMS)
    today = local_now.date()
    upcoming = sorted((w for w in windows if datetime.fromisoformat(w.end) > now), key=lambda x: x.start)

    # verdict
    if upcoming:
        w = upcoming[0]
        s, e = (datetime.fromisoformat(x).astimezone(AMS) for x in (w.start, w.end))
        verdict = f"{_day_name(s.date(), today)} {s:%H:%M}–{e:%H:%M} at {w.spot_name}."
        cond = _conditions(w)
        more = len(upcoming) - 1
        sub = (f'Peaks at <span class="num band-{_band(w.best, min_s)}">{w.best:g}/10</span> with {esc(cond)}.'
               + (f" {more} more window{'s' if more > 1 else ''} below." if more else ""))
    else:
        cands = [(h, sp) for sp, hs in spot_hours.values() for h in hs if h.daylight and h.time >= now]
        verdict = f"Nothing worth the train in the next {cfg['forecast_days']} days."
        if cands:
            h, sp = max(cands, key=lambda x: x[0].score)
            t = h.time.astimezone(AMS)
            sub = (f"Closest call: {esc(sp['name'])}, {_day_name(t.date(), today, True)} {t:%H:%M}, "
                   f'<span class="num">{h.score:g}/10</span> with {h.wave_h:.1f} m at {h.wave_t:.0f} s '
                   f"and {h.wind_kn:.0f} kn {h.wind_kind} wind.")
        else:
            sub = "No forecast data came back this run."

    out = ["<!doctype html>", '<html lang="en"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           "<title>Surf windows NL</title>",
           '<meta name="theme-color" content="#DDE4E3">',
           '<link rel="preconnect" href="https://fonts.googleapis.com">',
           '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>',
           '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,100..900&display=swap">',
           f"<style>{HTML_CSS}</style></head><body><main>",
           f'<p class="updated">Updated {local_now:%a %d %b, %H:%M} Amsterdam time</p>',
           f'<h1 class="verdict">{esc(verdict)}</h1>', f'<p class="sub">{sub}</p>']

    if upcoming:
        out.append('<ul class="windows">')
        for w in upcoming:
            s, e = (datetime.fromisoformat(x).astimezone(AMS) for x in (w.start, w.end))
            cond = _conditions(w)
            out.append(f'<li><span class="when">{_day_name(s.date(), today)} {s:%H:%M}–{e:%H:%M}</span>'
                       f'<span class="where">{esc(w.spot_name)}<span class="cond">{esc(cond)}</span></span>'
                       f'<span class="score num band-{_band(w.best, min_s)}">{w.best:g}<small>/10</small></span></li>')
        out.append("</ul>")

    ticks = "".join(f"<span>{h:02d}</span>" if h % 3 == 0 else "<span></span>" for h in HTML_HOURS)
    chevron = '<svg viewBox="0 0 10 10" aria-hidden="true"><path d="M2 0 8 5 2 10Z"/></svg>'
    for spot, hours in spot_hours.values():
        wins = [(datetime.fromisoformat(w.start), datetime.fromisoformat(w.end))
                for w in windows if w.spot_id == spot["id"]]
        in_win = lambda t: any(a <= t < b for a, b in wins)  # noqa: E731
        days: dict = {}
        for h in hours:
            days.setdefault(h.time.astimezone(AMS).date(), {})[h.time.astimezone(AMS).hour] = h

        out += [f'<section class="spot" aria-labelledby="s-{esc(spot["id"])}">',
                f'<h2 id="s-{esc(spot["id"])}">{esc(spot["name"])}</h2>',
                f'<p class="note">{esc(spot.get("notes", ""))}</p>',
                f'<div class="row axis" aria-hidden="true"><span></span><span class="ticks">{ticks}</span>'
                f"<span>best</span></div>"]
        for d, by_hour in days.items():
            day_hours = [h for h in by_hour.values() if h.daylight]
            if not day_hours:
                continue  # e.g. the trailing UTC hours spilling into an extra local day
            best = max(day_hours, key=lambda x: x.score)
            cells = []
            for hh in HTML_HOURS:
                h = by_hour.get(hh)
                if h is None:
                    cells.append('<span class="h"></span>')
                    continue
                cls = ["h", _band(h.score, min_s)]
                if not h.daylight: cls.append("night")
                if in_win(h.time): cls.append("win")
                if h.time + timedelta(hours=1) <= now: cls.append("past")
                tip = (f"{hh:02d}:00 {h.score:g}/10, {h.wave_h:.1f} m {h.wave_t:.0f} s {compass(h.wave_dir)}, "
                       f"wind {h.wind_kn:.0f} kn {compass(h.wind_dir)}")
                cells.append(f'<span class="{" ".join(cls)}" style="--s:{h.score / 10:.2f}" title="{tip}">'
                             f'<span class="b"></span></span>')
            best_html = f'<span class="best num band-{_band(best.score, min_s)}">{best.score:g}</span>'
            label = d.strftime("%a %d").replace(" 0", " ")
            out.append(f'<details><summary class="row" aria-label="{d:%A %d %B}, best score '
                       f'{best.score:g}"><span class="dname">{chevron}{label}</span>'
                       f'<span class="strip" style="--min:{min_s / 10:.2f}">{"".join(cells)}</span>'
                       f"{best_html}</summary>")
            out.append('<table class="hours"><thead><tr><th>Time</th><th>Score</th><th>Waves</th>'
                       '<th>Wind</th><th class="hide-s">Gusts</th></tr></thead><tbody>')
            for h in sorted(day_hours, key=lambda x: x.time):
                t = h.time.astimezone(AMS)
                out.append(f'<tr class="{"win" if in_win(h.time) else ""}"><td>{t:%H:%M}</td>'
                           f'<td class="num band-{_band(h.score, min_s)}">{h.score:g}</td>'
                           f"<td>{_arrow(h.wave_dir)}{h.wave_h:.1f} m, {h.wave_t:.0f} s "
                           f'<span class="dim">{compass(h.wave_dir)}</span></td>'
                           f"<td>{_arrow(h.wind_dir)}{h.wind_kn:.0f} kn {compass(h.wind_dir)} "
                           f'<span class="dim">{h.wind_kind}</span></td>'
                           f'<td class="hide-s">{h.gust_kn:.0f} kn</td></tr>')
            out.append("</tbody></table></details>")
        out.append("</section>")

    out += ['<ul class="legend">',
            '<li><i style="background:var(--flat)"></i>Flat at the beach</li>',
            f'<li><i style="background:var(--ok)"></i>Rideable, {min_s:g}+</li>',
            '<li><i style="background:var(--good)"></i>Good, 6.5+</li>',
            '<li><i style="background:var(--fire)"></i>Go, 8+</li>',
            '<li><i style="background:var(--win)"></i>Calendar window</li>',
            '<li><i style="background:var(--night)"></i>Dark</li></ul>',
            "<footer>",
            "<p>Score is 0 to 10 per hour from wave height, period, swell direction and wind at the beach. "
            "The dashed line is the minimum for a calendar window. Beyond about 3 days the forecast is rough. "
            "Tap a day for hourly details.</p>",
            '<p><a href="surf.ics">Subscribe to the calendar</a> · Forecast data from '
            '<a href="https://open-meteo.com">Open-Meteo</a></p>',
            "</footer></main></body></html>"]
    return "\n".join(out) + "\n"


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
    old = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else []

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
    hist_path.write_text(json.dumps([asdict(w) for w in windows], indent=1, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    (out_dir / "docs" / "surf.ics").write_text(to_ics(windows, now), encoding="utf-8", newline="")
    (out_dir / "FORECAST.md").write_text(to_markdown(spot_hours, windows, now) + "\n", encoding="utf-8")
    (out_dir / "docs" / "index.html").write_text(to_html(spot_hours, windows, cfg, now), encoding="utf-8")
    print(f"{len(new)} new window(s), {len(windows)} in feed.")
    return windows


if __name__ == "__main__":
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    run(config, datetime.now(UTC).replace(minute=0, second=0, microsecond=0))
