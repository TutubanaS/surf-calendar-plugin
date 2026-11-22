# surf-windows

Finds surfable windows on the Dutch coast for the next 5 days and publishes them as a calendar feed you can subscribe to. It updates twice a day through GitHub Actions.

- **Spots:** Scheveningen, Wijk aan Zee (edit `config.json`)
- **Data:** [Open-Meteo](https://open-meteo.com) Marine API (waves) and Forecast API (wind, sunrise/sunset). Free, no API key needed.
- **Output:** `docs/surf.ics` (calendar), `FORECAST.md` (readable table), `data/windows.json` (history of what was forecast)

👉 **[Current forecast](FORECAST.md)**

## Subscribe

Google Calendar → *Other calendars* → **+** → *From URL*:

```
https://raw.githubusercontent.com/TutubanaS/surf-windows/main/docs/surf.ics
```

Apple Calendar: *File → New Calendar Subscription*, same URL.

Google re-reads subscribed feeds on its own schedule (usually every 8–24 h), so the feed in the repo is always fresher than your calendar. Check `FORECAST.md` before a session.

## How it scores (0–10)

Tuned for the North Sea, where offshore model height on its own is misleading:

| Factor | Rule |
|---|---|
| Size | <0.7 m ≈ nothing · 1–1.5 m decent · 1.5–2.5 m best |
| Period | <6 s worthless · 7 s+ needed · 8–10 s+ good |
| Swell dir | W–N good · SW blocked at Scheveningen (harbour jetty) |
| Wind | offshore (E/SE) or <5 kn glassy is best · SW cross-shore · strong onshore kills it |
| Vetoes | <0.7 m, or <6 s and <1.2 m → capped at 1–2 (flat at the beach) |

A **window** is 2+ consecutive daylight hours scoring ≥ `min_score` (default 5). Events are labelled 🔥 ≥8, 👍 ≥6.5, 🤏 otherwise, and the description lists every hour plus a confidence note (forecasts beyond about 3 days are rough).

## Run locally

```bash
python surfwindows.py   # stdlib only
pytest -q
```

## Ideas

- Log actual conditions (webcam, own sessions) next to `data/windows.json` and tune the thresholds from that
- Tide (Scheveningen is often better around mid–high)
- Telegram push for 🔥 windows
