# Fish Forecast

Finds surfable windows at wave-surfing spots, and good days at ski resorts, for the next two weeks and publishes them as calendar feeds you can subscribe to, plus a forecast page. It updates every 3 hours through GitHub Actions.

- **Spots:** 117 in the Netherlands, Portugal, Morocco and Türkiye (edit `spots.json`). Coordinates and beach orientation are approximate; fix a spot there if it looks off.
- **Data:** [Open-Meteo](https://open-meteo.com) Marine API (waves, tide, water temperature) and Forecast API (wind, weather, sunrise/sunset). Free, no API key needed. Wave height is the median of five wave models (DWD EWAM and GWAM, Météo-France, NOAA GFS-Wave, ECMWF), because a single model can be far off this close to the coast.
- **Ski resorts:** 46 in Türkiye, Austria, Switzerland, France, Italy and Germany (edit `resorts.json`). The page has a **Surf / Ski & snowboard** switch at the top.
- **Output:** `docs/index.html` + `docs/data.js` (forecast page), `docs/ski.js` (the ski side of the page), `docs/cal/<id>.ics` (one calendar per spot and per resort), `docs/surf.ics` (one calendar for your own spots), `FORECAST.md` (plain table for your own spots), `data/windows.json` (history of what was forecast for them)

👉 **[Current forecast](https://tutubanas.github.io/surf-calendar-plugin/)** (needs GitHub Pages on `main` / `docs`) · [plain table](FORECAST.md)

## Subscribe

Every spot and every ski resort has its own feed, so you only get the ones you want. On the forecast page open a spot and press **Subscribe to calendar** next to its name: its feed appears right there, with buttons for Google Calendar, Apple or Outlook, and Copy link. The feed address is:

```
https://tutubanas.github.io/surf-calendar-plugin/cal/<spot-id>.ics
```

There is also one combined feed with only the spots listed under `calendar_spots` in `config.json` (Scheveningen and Wijk aan Zee by default):

```
https://tutubanas.github.io/surf-calendar-plugin/surf.ics
```

Google Calendar → *Other calendars* → **+** → *From URL*. Apple Calendar: *File → New Calendar Subscription*.

Google re-reads subscribed feeds on its own schedule (usually every 8–24 h), so the feed in the repo is always fresher than your calendar. Check the forecast page before a session.

## How it scores (0–10)

Model height at sea on its own is misleading, so the score also uses period, swell direction and wind at the beach. There are two sets of thresholds (`PROFILES` in `surfwindows.py`):

| Factor | Wind swell (Netherlands, Türkiye) | Groundswell (Portugal, Morocco) |
|---|---|---|
| Size | <0.5 m flat · 0.5–0.7 m mild (capped at 5, the lowest score that makes a calendar window) · 1–1.5 m decent · 1.5–2.5 m best | <0.5 m nothing · 1.6–2.3 m best · 3 m+ too big |
| Period | peak period: <6 s worthless · 7 s+ needed · 8–10 s+ good | <7 s chop · 11 s+ needed · 13 s+ good |
| Swell dir | within 60° of where the spot's swell comes from is good, and outside that the swell counts as smaller the further it is off; spots can list blocked directions (SW at Scheveningen, behind the harbour jetty) | same |
| Wind | offshore or <5 kn glassy is best · cross-shore is so-so · strong onshore kills it | same |
| Vetoes | <0.5 m, or <6 s and <1.2 m → capped at 1–2 (flat at the beach) · waves heading away from the beach → flat | <0.5 m, or <7 s and <2 m → capped · 3 m+ → capped at 3 |

The wind-swell numbers come from real sessions in the Netherlands. The groundswell numbers are a first guess and have not been checked against real sessions yet. A spot can set `exposure` (how much of the swell at sea reaches the break: under 1 for sheltered bays, over 1 for swell magnets) and `swell_from` (when the swell wraps in from a different side than the beach faces).

A **window** is 2+ consecutive daylight hours scoring ≥ `min_score` (default 5). The page shows windows for all 14 days; calendars only get those starting within `calendar_days` (7), because beyond a week only two or three coarse models are left. Events are labelled 🔥 ≥8, 👍 ≥6.5, 🤏 otherwise, and the description lists every hour plus a confidence note (forecasts beyond about 3 days are rough, and confidence drops when the wave models disagree).

## Ski and snowboard

Each resort in `resorts.json` has a point in the ski area, its bottom and top lift (`base`, `top`) and `elev`, the height the weather is taken at: two thirds of the way up. One Open-Meteo call per resort; Open-Meteo scales the temperature to that height.

- **Snow or rain** is worked out from that temperature, not taken from the model, whose smoothed mountains sit hundreds of metres too low: all snow at 0.5 °C and below, all rain from 2.5 °C, 1 cm of snow per mm of water when it is −5 °C or colder, 0.8 cm otherwise.
- **Snow on the ground** is the larger of the model's snow depth and 60 % of the snow that fell at the resort's height over the last week. It is an estimate, not the resort's snow report: runs on machine-made snow can be open while it shows next to nothing.
- **Score** per lift hour (09:00 to 16:00): new snow over 24 h (up to +4, 10 cm+ is a powder day), snow on the ground (up to +2), sky (sun +2, fog −1.5), wind (light +1.5, 45 km/h+ −2.5) and temperature (−12 to −2 °C +1, bitter cold −1.5). Vetoes: under 10 cm on the ground → at most 3.5, rain → at most 3, gusts of 70 km/h+ (upper lifts stop) → at most 3, 90 km/h+ → at most 1. A sunny day on a good base lands around 6, a powder day with sun 9 to 10. These are first guesses, not tuned on real days yet.
- A **good stretch** is 2+ lift hours in a row at `min_score` or more; it goes into that resort's feed, `cal/<resort-id>.ics`, if it starts within `calendar_days`. There is no combined ski feed.

## Looking back

The page also shows the last 3 days (`past_days`), scored with the current rules. Use it to check the score against a day you were in the water, and tune the thresholds from that.

## Wave source

By default every number is the blend (median) of five wave models. On the page, **Wave source** lets a reader switch the open spot and their own spots to a single model, fetched live from Open-Meteo: GFS-Wave (the model behind Windguru's standard wave forecast), EWAM, GWAM, ECMWF WAM or Météo-France MFWAM. The calendars always use what the bot computed. To make the bot itself follow one model, add `"wave_models": ["ncep_gfswave025"]` to `config.json`.

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
