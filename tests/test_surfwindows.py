import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import surfwindows as sw  # noqa: E402

UTC = timezone.utc
CFG = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())
SCHEV = CFG["spots"][0]


def hr(h, t, d, wind, wdir, daylight=True, time=None):
    return sw.Hour(time=time or datetime(2026, 11, 24, 10, tzinfo=UTC), wave_h=h, wave_t=t,
                   wave_dir=d, wind_kn=wind, wind_dir=wdir, gust_kn=wind * 1.3, daylight=daylight)


def test_short_period_small_swell_is_flat():
    # the Nov 2026 miss: ~0.8-1 m at 5 s looked fine on the model, was flat
    assert sw.score_hour(hr(0.9, 5, 300, 6, 100), SCHEV) <= 2


def test_real_day_scores_high():
    assert sw.score_hour(hr(1.3, 8, 310, 6, 110), SCHEV) >= 8


def test_sw_swell_blocked_and_sw_wind_cross():
    good = sw.score_hour(hr(1.3, 8, 300, 10, 120), SCHEV)
    sw_case = sw.score_hour(hr(1.3, 8, 220, 10, 220), SCHEV)
    assert sw_case < good - 3


def test_strong_onshore_kills_it():
    assert sw.score_hour(hr(1.8, 7, 310, 22, 300), SCHEV) < 4


def test_arc_wraps_north():
    assert sw.in_arc(350, [260, 20]) and sw.in_arc(10, [260, 20]) and not sw.in_arc(90, [260, 20])


def fake_fetch_factory(start):
    def fetch(spot, days):
        hours = []
        for i in range(days * 24):
            t = start + timedelta(hours=i)
            day = 7 <= t.hour < 16
            good = i // 24 == 1  # only tomorrow is good
            hours.append(sw.Hour(time=t, wave_h=1.4 if good else 0.6, wave_t=8 if good else 4,
                                 wave_dir=305, wind_kn=7, wind_dir=110, gust_kn=10, daylight=day))
        return hours
    return fetch


def test_end_to_end(tmp_path):
    now = datetime(2026, 11, 23, 4, tzinfo=UTC)
    cfg = dict(CFG, spots=[SCHEV])
    wins = sw.run(cfg, now, fetch=fake_fetch_factory(now.replace(hour=0)), out_dir=tmp_path)
    assert len(wins) == 1
    w = wins[0]
    assert w.start.startswith("2026-11-24T07") and w.end.startswith("2026-11-24T16")
    ics = (tmp_path / "docs" / "surf.ics").read_bytes().decode()
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.count("BEGIN:VEVENT") == 1
    assert all(len(l.encode()) <= 75 for l in ics.split("\r\n"))

    # next day's run: yesterday's window is past and kept, future ones replaced
    later = datetime(2026, 11, 25, 4, tzinfo=UTC)
    wins2 = sw.run(cfg, later, fetch=fake_fetch_factory(later.replace(hour=0)), out_dir=tmp_path)
    starts = sorted(w.start[:10] for w in wins2)
    assert starts == ["2026-11-24", "2026-11-26"]
    assert (tmp_path / "FORECAST.md").exists()

    page = (tmp_path / "docs" / "index.html").read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>") and "Scheveningen" in page
    assert 'class="windows"' in page and "win" in page  # tomorrow's window is listed and highlighted


def test_merge_api_shape():
    times = ["2026-11-24T07:00", "2026-11-24T08:00", "2026-11-24T09:00"]
    marine = {"hourly": {"time": times, "wave_height": [1.2, None, 1.3], "wave_direction": [300, 300, 310],
                         "wave_period": [6, 6, 6], "wave_peak_period": [8, 8, None]}}
    weather = {"hourly": {"time": times, "wind_speed_10m": [5, 5, 6], "wind_direction_10m": [100, 100, 110],
                          "wind_gusts_10m": [8, 8, 9]},
               "daily": {"sunrise": ["2026-11-24T07:20"], "sunset": ["2026-11-24T15:40"]}}
    hours = sw.merge(marine, weather)
    assert len(hours) == 2                      # null wave height skipped
    assert hours[0].daylight                    # 07:00 within sunrise-30min
    assert hours[1].wave_t == 6                 # falls back to mean period
