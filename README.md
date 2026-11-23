# surf-calendar-plugin

Finds surfable windows at wave-surfing spots for the next two weeks and publishes them as calendar feeds you can subscribe to, plus a forecast page. It updates every 3 hours through GitHub Actions.

- **Spots:** 117 in the Netherlands, Portugal, Morocco and Türkiye (edit `spots.json`). Coordinates and beach orientation are approximate; fix a spot there if it looks off.
- **Data:** [Open-Meteo](https://open-meteo.com) Marine API (waves, tide, water temperature) and Forecast API (wind, weather, sunrise/sunset). Free, no API key needed. Wave height is the median of five wave models (DWD EWAM and GWAM, Météo-France, NOAA GFS-Wave, ECMWF), because a single model can be far off this close to the coast.
- **Output:** `docs/index.html` + `docs/data.js` (forecast page), `docs/cal/<spot>.ics` (one calendar per spot), `docs/surf.ics` (one calendar for your own spots), `FORECAST.md` (plain table for your own spots), `data/windows.json` (history of what was forecast for them)

👉 **[Current forecast](https://tutubanas.github.io/surf-calendar-plugin/)** (needs GitHub Pages on `main` / `docs`) · [plain table](FORECAST.md)

## Subscribe

Every spot has its own feed, so you only get the spots you want. On the forecast page press **Subscribe to the calendar**, tick your spots and add each feed to Google Calendar, Apple Calendar or Outlook. The feed address is:

```
https://raw.githubusercontent.com/TutubanaS/surf-calendar-plugin/main/docs/cal/<spot-id>.ics
```

There is also one combined feed with only the spots listed under `calendar_spots` in `config.json` (Scheveningen and Wijk aan Zee by default):

```
https://raw.githubusercontent.com/TutubanaS/surf-calendar-plugin/main/docs/surf.ics
```

Google Calendar → *Other calendars* → **+** → *From URL*. Apple Calendar: *File → New Calendar Subscription*.

Google re-reads subscribed feeds on its own schedule (usually every 8–24 h), so the feed in the repo is always fresher than your calendar. Check the forecast page before a session.

## How it scores (0–10)

Model height at sea on its own is misleading, so the score also uses period, swell direction and wind at the beach. There are two sets of thresholds (`PROFILES` in `surfwindows.py`):

| Factor | Wind swell (Netherlands, Türkiye) | Groundswell (Portugal, Morocco) |
|---|---|---|
| Size | <0.7 m ≈ nothing · 1–1.5 m decent · 1.5–2.5 m best | <0.5 m nothing · 1.6–2.3 m best · 3 m+ too big |
| Period | peak period: <6 s worthless · 7 s+ needed · 8–10 s+ good | <7 s chop · 11 s+ needed · 13 s+ good |
| Swell dir | within 60° of where the spot's swell comes from is good, and outside that the swell counts as smaller the further it is off; spots can list blocked directions (SW at Scheveningen, behind the harbour jetty) | same |
| Wind | offshore or <5 kn glassy is best · cross-shore is so-so · strong onshore kills it | same |
| Vetoes | <0.7 m, or <6 s and <1.2 m → capped at 1–2 (flat at the beach) · waves heading away from the beach → flat | <0.5 m, or <7 s and <2 m → capped · 3 m+ → capped at 3 |

The wind-swell numbers come from real sessions in the Netherlands. The groundswell numbers are a first guess and have not been checked against real sessions yet. A spot can set `exposure` (how much of the swell at sea reaches the break: under 1 for sheltered bays, over 1 for swell magnets) and `swell_from` (when the swell wraps in from a different side than the beach faces).

A **window** is 2+ consecutive daylight hours scoring ≥ `min_score` (default 5). The page shows windows for all 14 days; calendars only get those starting within `calendar_days` (7), because beyond a week only two or three coarse models are left. Events are labelled 🔥 ≥8, 👍 ≥6.5, 🤏 otherwise, and the description lists every hour plus a confidence note (forecasts beyond about 3 days are rough, and confidence drops when the wave models disagree).

## Run locally

```bash
python surfwindows.py   # stdlib only; fetches every spot, takes a few minutes
pytest -q
```

Open `docs/index.html` in a browser to see the page; it reads `docs/data.js` from the last run.

## Ideas

- Log actual conditions (webcam, own sessions) next to `data/windows.json` and tune the thresholds from that
- Tide (Scheveningen is often better around mid–high)
- Telegram push for 🔥 windows
