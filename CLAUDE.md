# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-script Python tool (`surfwindows.py`, stdlib only) that pulls Open-Meteo forecasts for Dutch surf spots, scores each hour 0–10, groups good daylight hours into "windows", and publishes them as an iCalendar feed. A GitHub Action runs it twice a day and commits the outputs. `README.md` documents the scoring rules for users. Keep it in sync when thresholds change.

## Commands

```bash
python surfwindows.py                          # live run: hits Open-Meteo, overwrites the tracked outputs
pytest -q                                      # all tests (no network; the fetch is faked)
pytest -q tests/test_surfwindows.py::test_end_to_end   # single test
```

No dependencies beyond pytest. Don't add third-party imports to `surfwindows.py`. CI installs only pytest.

## Architecture / data flow

`run()` is the pipeline: `fetch_spot` → `merge` → `find_windows` (calls `score_hour`) → `merge_history` → write the 4 outputs.

- **`merge`** joins marine hourly data (waves, from the offshore `marine` coordinate) with weather hourly data (wind, from the `beach` coordinate) by timestamp. It prefers `wave_peak_period` over `wave_period`, skips hours with nulls, and marks daylight as sunrise−30 min through sunset. All times are UTC (`timezone: GMT` in the API calls). Amsterdam time (`AMS`) is used only for display in `FORECAST.md` and `docs/index.html`.
- **`score_hour`** applies the per-spot config: `facing_deg` (offshore = facing+180), `good_swell_dir` / `blocked_swell_dir` arcs (clockwise, can wrap over north, see `in_arc`), and the hard vetoes. It also sets `Hour.wind_kind` as a side effect.
- **`find_windows`** keeps runs of consecutive daylight hours with score ≥ `min_score` and length ≥ `min_window_hours`. Event UIDs are a hash of `spot_id` + start hour, so a window that moves or changes length gets a new UID.
- **`merge_history`** keeps `data/windows.json` as a log. Past windows are kept for `history_days`. Future windows are replaced only for spots whose fetch succeeded this run, so if a spot's API call fails, its old future windows stay. If every fetch fails, the script exits without writing anything.
- **Outputs** (all committed by the bot, so don't hand-edit them): `docs/surf.ics` (CRLF, RFC 5545 escaping and 75-octet folding via `ics_escape`/`ics_fold`), `docs/index.html` (static page served by GitHub Pages from `/docs`; `to_html` builds it with inline CSS, no JS), `FORECAST.md`, `data/windows.json`. The ICS URL on raw.githubusercontent.com is what users subscribe to, so don't move or rename `docs/surf.ics`.

`run()` takes `fetch=` and `out_dir=` parameters so tests can inject fake hours and write to `tmp_path`.

## Gotchas

- Tests load the real `config.json` and use `spots[0]` (Scheveningen) as the fixture. Changing that spot's arcs or `facing_deg`, or reordering spots, can break the scoring tests.
- Scoring thresholds come from real sessions (e.g. the test about short-period swell being flat at the beach). When changing them, add or adjust a test that captures the real-world case.
- The workflow (`.github/workflows/update.yml`) runs pytest before fetching, on cron at 04:17 and 16:17 UTC, on manual dispatch, and on pushes that touch `surfwindows.py`, `config.json`, or the workflow file. Bot commits are titled `forecast update <timestamp>`, so pull before pushing.
