import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import surfwindows as sw  # noqa: E402

UTC = timezone.utc
CFG = sw.load_config()
SPOTS = {s["id"]: s for s in CFG["spots"]}
SCHEV = SPOTS["scheveningen"]


def hr(h, t, d, wind, wdir, daylight=True, time=None):
    return sw.Hour(time=time or datetime(2026, 11, 24, 10, tzinfo=UTC), wave_h=h, wave_t=t,
                   wave_dir=d, wind_kn=wind, wind_dir=wdir, gust_kn=wind * 1.3, daylight=daylight)


def test_short_period_small_swell_is_flat():
    # the Nov 2026 miss: ~0.8-1 m at 5 s looked fine on the model, was flat
    assert sw.score_hour(hr(0.9, 5, 300, 6, 100), SCHEV) <= 2


def test_half_a_metre_is_mild_surf_not_flat():
    # at Scheveningen 0.5-0.6 m and up means mild surf; under that it is flat
    mild = sw.score_hour(hr(0.6, 8, 310, 4, 110), SCHEV)
    assert mild == CFG["min_score"] == 5                # just enough to go in the calendar, never more
    assert sw.score_hour(hr(0.45, 8, 310, 4, 110), SCHEV) <= 1


def test_real_day_scores_high():
    assert sw.score_hour(hr(1.3, 8, 310, 6, 110), SCHEV) >= 8


def test_sw_swell_blocked_and_sw_wind_cross():
    good = sw.score_hour(hr(1.3, 8, 300, 10, 120), SCHEV)
    sw_case = sw.score_hour(hr(1.3, 8, 220, 10, 220), SCHEV)
    assert sw_case < good - 3


def test_strong_onshore_kills_it():
    assert sw.score_hour(hr(1.8, 7, 310, 22, 300), SCHEV) < 4


def test_waves_heading_out_to_sea_are_flat():
    # a strong easterly builds wind sea a few km out, but it travels away from a west-facing beach
    assert sw.score_hour(hr(1.4, 4, 90, 18, 90), SCHEV) <= 1


def test_arc_wraps_north():
    assert sw.in_arc(350, [260, 20]) and sw.in_arc(10, [260, 20]) and not sw.in_arc(90, [260, 20])


def test_groundswell_profile():
    spot = SPOTS["ribeira-dilhas"]                       # Atlantic reef, faces 290, so 110 is offshore
    clean = sw.score_hour(hr(1.8, 13, 300, 8, 110), spot)
    assert clean >= 8
    assert sw.score_hour(hr(1.8, 6, 300, 8, 110), spot) < clean - 3     # same size as wind chop
    assert sw.score_hour(hr(5.0, 15, 300, 8, 110), spot) <= 3           # too big


def test_exposure_scales_the_swell():
    bay = SPOTS["imsouane-bay"]                          # sheltered bay: only part of the swell gets in
    assert bay["exposure"] < 1 and bay["swell_from"] != bay["facing_deg"]
    offshore = (bay["facing_deg"] + 180) % 360
    assert sw.score_hour(hr(0.7, 12, 300, 5, offshore), bay) <= 1       # flat inside the bay
    assert sw.score_hour(hr(3.0, 12, 300, 5, offshore), bay) >= 7


def test_swell_from_the_side_arrives_smaller():
    mareta = SPOTS["mareta"]                             # faces south; north-west swell has to wrap in
    offshore = (mareta["facing_deg"] + 180) % 360
    assert sw.swell_reach(mareta, sw.swell_from(mareta)) == 1
    assert 0 < sw.swell_reach(mareta, 310) < 0.8
    assert sw.swell_reach(mareta, 20) == 0
    direct = sw.score_hour(hr(2.5, 13, sw.swell_from(mareta), 5, offshore), mareta)
    assert sw.score_hour(hr(2.5, 13, 310, 5, offshore), mareta) < direct


def test_spots_file_is_valid():
    ids = [s["id"] for s in CFG["spots"]]
    assert len(ids) == len(set(ids)) and len(ids) > 100
    for s in CFG["spots"]:
        assert s["country"] in sw.COUNTRIES and s.get("profile", "windswell") in sw.PROFILES, s["id"]
        for k in ("marine", "beach"):
            assert -90 <= s[k]["lat"] <= 90 and -180 <= s[k]["lon"] <= 180, s["id"]
        assert 0 <= s["facing_deg"] < 360 and 0 < s.get("exposure", 1) <= 2, s["id"]
        assert s["name"] and s["tz"].count("/") == 1, s["id"]
    assert set(CFG["calendar_spots"]) <= set(ids)


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


def read_data(tmp_path):
    text = (tmp_path / "docs" / "data.js").read_text(encoding="utf-8")
    assert text.startswith("window.SURF=") and text.rstrip().endswith(";")
    return json.loads(text[len("window.SURF="):].rstrip().rstrip(";"))


def events(path):
    return path.read_bytes().decode().count("BEGIN:VEVENT")


def test_end_to_end(tmp_path):
    now = datetime(2026, 11, 23, 4, tzinfo=UTC)
    cfg = dict(CFG, spots=[SCHEV])
    wins = sw.run(cfg, now, fetch=fake_fetch_factory(now.replace(hour=0)), out_dir=tmp_path)
    assert len(wins) == 1
    w = wins[0]
    assert w.start.startswith("2026-11-24T07") and w.end.startswith("2026-11-24T16")
    ics = (tmp_path / "docs" / "surf.ics").read_bytes().decode()
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.count("BEGIN:VEVENT") == 1
    flat = ics.replace("\r\n ", "")                    # unfolded
    link = CFG["feed_base"] + "#spot=scheveningen"     # every event links back to its spot on the page
    assert "URL:" + link in flat and "Full forecast: " + link in flat
    assert all(len(l.encode()) <= 75 for l in ics.split("\r\n"))

    data = read_data(tmp_path)
    spot = data["spots"][0]
    assert spot["id"] == "scheveningen" and data["defaults"] == CFG["calendar_spots"]
    n = 24 * CFG["hourly_days"] + 8 * (CFG["forecast_days"] - CFG["hourly_days"])     # hourly, then 3-hourly
    assert len(spot["score"]) == len(spot["wh"]) == len(spot["kind"]) == len(spot["day"]) == len(spot["t"]) == n
    assert len(spot["windows"]) == 1 and spot["windows"][0]["best"] == w.best
    assert max(spot["score"]) == w.best          # thinning the far-out days never drops a day's best hour
    assert set(data["profiles"]) == set(sw.PROFILES)

    # next day's run: yesterday's window is past and kept, future ones replaced
    later = datetime(2026, 11, 25, 4, tzinfo=UTC)
    wins2 = sw.run(cfg, later, fetch=fake_fetch_factory(later.replace(hour=0)), out_dir=tmp_path)
    starts = sorted(w.start[:10] for w in wins2)
    assert starts == ["2026-11-24", "2026-11-26"]
    assert (tmp_path / "FORECAST.md").exists()
    assert events(tmp_path / "docs" / "cal" / "scheveningen.ics") == 2     # its own feed keeps the history too


def test_main_feed_has_only_the_calendar_spots(tmp_path):
    now = datetime(2026, 11, 23, 4, tzinfo=UTC)
    cfg = dict(CFG, spots=[SCHEV, SPOTS["wijk-aan-zee"], SPOTS["zandvoort"]], calendar_spots=["scheveningen"])
    wins = sw.run(cfg, now, fetch=fake_fetch_factory(now.replace(hour=0)), out_dir=tmp_path)
    assert {w.spot_id for w in wins} == {"scheveningen"}
    assert events(tmp_path / "docs" / "surf.ics") == 1
    cal = tmp_path / "docs" / "cal"
    assert sorted(p.stem for p in cal.glob("*.ics")) == ["scheveningen", "wijk-aan-zee", "zandvoort"]
    assert events(cal / "zandvoort.ics") == 1                # every spot still gets its own feed
    assert len(read_data(tmp_path)["spots"]) == 3
    assert "Zandvoort" not in (tmp_path / "FORECAST.md").read_text(encoding="utf-8")

    # a spot that leaves the config loses its feed; a failed fetch keeps the old one
    def flaky(spot, days):
        if spot["id"] == "wijk-aan-zee":
            raise OSError("api down")
        return fake_fetch_factory(now.replace(hour=0))(spot, days)
    sw.run(dict(cfg, spots=cfg["spots"][:2]), now, fetch=flaky, out_dir=tmp_path)
    assert sorted(p.stem for p in cal.glob("*.ics")) == ["scheveningen", "wijk-aan-zee"]
    assert events(cal / "wijk-aan-zee.ics") == 1


def test_quota_exhausted_publishes_what_arrived(tmp_path):
    now = datetime(2026, 11, 23, 4, tzinfo=UTC)
    cfg = dict(CFG, spots=[SCHEV, SPOTS["wijk-aan-zee"], SPOTS["zandvoort"]], fetch_workers=1)   # one at a time
    asked = []

    def fetch(spot, days):
        asked.append(spot["id"])
        if len(asked) == 2:
            raise sw.QuotaExhausted("Hourly API request limit exceeded")
        return fake_fetch_factory(now.replace(hour=0))(spot, days)
    sw.run(cfg, now, fetch=fetch, out_dir=tmp_path)
    assert asked == ["scheveningen", "wijk-aan-zee"]                 # the third spot is never asked for
    assert [s["id"] for s in read_data(tmp_path)["spots"]] == ["scheveningen"]

    # after a full run, a run that loses spots keeps their previous data on the page
    sw.run(cfg, now, fetch=fake_fetch_factory(now.replace(hour=0)), out_dir=tmp_path)
    asked.clear()
    sw.run(cfg, now, fetch=fetch, out_dir=tmp_path)
    assert [s["id"] for s in read_data(tmp_path)["spots"]] == ["scheveningen", "wijk-aan-zee", "zandvoort"]


def test_merge_api_shape():
    times = ["2026-11-24T07:00", "2026-11-24T08:00", "2026-11-24T09:00"]
    marine = {"hourly": {"time": times, "wave_height": [1.2, None, 1.3], "wave_direction": [300, 300, 310],
                         "wave_period": [6, 6, 6], "wave_peak_period": [8, 8, None]}}
    weather = {"hourly": {"time": times, "wind_speed_10m": [5, 5, 6], "wind_direction_10m": [100, 100, 110],
                          "wind_gusts_10m": [8, 8, 9], "weather_code": [0, 3, 61], "temperature_2m": [9.4, 9, 8]},
               "daily": {"sunrise": ["2026-11-24T07:20"], "sunset": ["2026-11-24T15:40"]}}
    tide = {"hourly": {"time": times, "sea_level_height_msl": [0.4, 0.1, -0.3], "sea_surface_temperature": [9.1, 9.1, 9.0]}}
    hours = sw.merge(marine, weather, tide)
    assert len(hours) == 2                      # null wave height skipped
    assert [h.tide for h in hours] == [0.4, -0.3] and [h.sst for h in hours] == [9.1, 9.0]
    assert hours[0].daylight                    # 07:00 within sunrise-30min
    assert hours[1].wave_t == 6                 # falls back to mean period
    assert (hours[1].weather, hours[1].temp_c) == (61, 8)


def test_merge_uses_median_of_models():
    # the real request asks for several wave models; keys come back suffixed with the model name
    times = ["2026-11-24T10:00", "2026-11-24T11:00"]
    marine = {"hourly": {
        "time": times,
        "wave_height_ewam": [0.6, None], "wave_direction_ewam": [300, None],
        "swell_wave_height_ewam": [0.5, None], "swell_wave_peak_period_ewam": [7.0, None],
        "wind_wave_height_ewam": [0.2, None], "wind_wave_peak_period_ewam": [2.0, None],
        "wave_height_gwam": [0.7, 0.8], "wave_direction_gwam": [310, 320],
        "swell_wave_height_gwam": [0.1, 0.1], "swell_wave_peak_period_gwam": [9.0, 9.0],
        "wind_wave_height_gwam": [0.6, 0.7], "wind_wave_peak_period_gwam": [4.0, 4.0],
        "wave_height_ecmwf_wam025": [1.3, 1.2], "wave_direction_ecmwf_wam025": [330, 330],
        "wave_peak_period_ecmwf_wam025": [6.0, 5.0], "wave_period_ecmwf_wam025": [4.5, 4.0],
        # a model that does not cover this sea answers zeros, not nulls; it must not drag the median down
        "wave_height_ncep_gfswave025": [0.0, 0.0], "wave_direction_ncep_gfswave025": [0, 0],
        "wave_period_ncep_gfswave025": [0.0, 0.0],
    }}
    weather = {"hourly": {"time": times, "wind_speed_10m": [5, 5], "wind_direction_10m": [100, 100],
                          "wind_gusts_10m": [8, 8]},
               "daily": {"sunrise": ["2026-11-24T07:20"], "sunset": ["2026-11-24T15:40"]}}
    a, b = sw.merge(marine, weather)
    assert (a.wave_h, a.wave_lo, a.wave_hi, a.n_models) == (0.7, 0.6, 1.3, 3)   # median, not the outlier
    assert a.wave_t == 6.0          # median of peak periods: swell 7 (ewam), wind sea 4 (gwam), 6 (ecmwf)
    assert a.wave_dir == 300        # direction from the finest model that has data
    assert (b.wave_h, b.n_models, b.wave_dir) == (1.0, 2, 320)   # ewam has run out
    assert b.wave_t == 4.5
    assert a.weather is None and a.tide is None      # weather and tide are optional


# --------------------------------------------------------------------------- ski and snowboard

RESORTS = {r["id"]: r for r in CFG["resorts"]}


def ski(depth=80, fresh=0, temp=-6, wind=8, gust=20, cloud=10, weather=0, vis=40000, snow=0.0, rain=0.0, feels=None):
    return sw.SkiHour(time=datetime(2026, 12, 10, 10, tzinfo=UTC), temp_c=temp, feels_c=temp - 4 if feels is None else feels,
                      snow_cm=snow, rain_mm=rain, fresh_cm=fresh, depth_cm=depth, wind_kmh=wind, gust_kmh=gust,
                      wind_dir=270, lifts=True, weather=weather, cloud=cloud, vis_m=vis)


def test_ski_scores():
    bluebird = ski(fresh=25)                                   # a night of snow, then sun and no wind
    assert sw.score_ski(bluebird) >= 9 and bluebird.kind == "powder"
    sunny = sw.score_ski(ski())
    assert CFG["min_score"] <= sunny < 8                       # a sunny day on a good base: worth going, not a powder day
    assert sw.score_ski(ski(cloud=95, weather=3)) < CFG["min_score"]          # grey, nothing new: not for the calendar
    fog = ski(cloud=100, weather=45, vis=300)
    assert sw.score_ski(fog) <= 3 and fog.kind == "fog"
    assert sw.score_ski(ski(fresh=15, snow=1.5, cloud=100, weather=73, vis=600)) >= 6   # snowing hard: powder, poor light


def test_ski_vetoes():
    rain = ski(fresh=12, temp=3, rain=1.4, cloud=100, weather=63)
    assert sw.score_ski(rain) <= 1.5 and rain.kind == "rain"
    storm = ski(fresh=25, wind=50, gust=95)
    assert sw.score_ski(storm) <= 1 and storm.kind == "storm"
    assert sw.score_ski(ski(fresh=25, wind=35, gust=75)) <= 3             # the upper lifts are on hold
    thin = ski(depth=4)
    assert sw.score_ski(thin) <= 3.5 and thin.kind == "thin"
    assert sw.score_ski(ski(feels=-30)) < sunny_score() - 1               # bitter cold


def sunny_score():
    return sw.score_ski(ski())


def test_merge_ski_splits_snow_and_rain_and_counts_new_snow():
    times = [f"2026-12-10T{h:02d}:00" for h in range(0, 12)]
    n = len(times)
    weather = {"hourly": {
        "time": times, "temperature_2m": [-5] * 6 + [1.5] * 3 + [4] * 3, "apparent_temperature": [-9] * n,
        "precipitation": [1.0] * 9 + [2.0, None, 0.0], "wind_speed_10m": [10] * n, "wind_gusts_10m": [20] * n,
        "wind_direction_10m": [270] * n, "snow_depth": [0.5] * n, "weather_code": [73] * n,
    }, "daily": {"sunrise": ["2026-12-10T06:40"], "sunset": ["2026-12-10T15:50"]}}
    hours = sw.merge_ski(weather, RESORTS["st-anton"])
    assert len(hours) == 11                                     # the hour without precipitation data is skipped
    assert hours[0].snow_cm == 1.0 and hours[0].rain_mm == 0    # -5 °C: all snow
    assert hours[6].snow_cm == 0.4 and hours[6].rain_mm == 0.5  # 1.5 °C: half and half
    assert hours[9].snow_cm == 0 and hours[9].rain_mm == 2.0    # 4 °C: rain
    assert hours[5].fresh_cm == 6.0 and hours[8].fresh_cm == 7.2   # new snow adds up over the day
    assert hours[0].depth_cm == 50 and hours[8].depth_cm == 50     # the model's 50 cm, more than 60 % of 7.2
    weather['hourly']['snow_depth'] = [0.0] * n                      # the model sees bare ground...
    assert sw.merge_ski(weather, RESORTS['st-anton'])[8].depth_cm == 4   # ...but 7.2 cm fell up here
    # St. Anton is UTC+1: the lifts run 08:00 to 15:00 UTC here
    assert [h.time.hour for h in hours if h.lifts] == [8, 9, 11]


def test_resorts_file_is_valid():
    ids = list(RESORTS)
    assert len(CFG["resorts"]) == len(ids) > 30
    assert not set(ids) & set(SPOTS)          # feeds share docs/cal/, so a resort may not take a surf spot's id
    for r in CFG["resorts"]:
        assert r["country"] in sw.SKI_COUNTRIES and r["tz"].count("/") == 1, r["id"]
        assert r["base"] < r["elev"] < r["top"] or r["base"] < r["top"] <= 900, r["id"]
        assert 35 < r["lat"] < 55 and -10 < r["lon"] < 45, r["id"]


def fake_ski_factory(start):
    def fetch(resort, days):
        out = []
        for i in range(days * 24):
            t = start + timedelta(hours=i)
            good = i // 24 == 1                    # tomorrow: fresh snow and sun
            out.append(sw.SkiHour(time=t, temp_c=-6, feels_c=-10, snow_cm=0, rain_mm=0, fresh_cm=20 if good else 0,
                                  depth_cm=90, wind_kmh=8, gust_kmh=15, wind_dir=270, lifts=8 <= t.hour < 15,
                                  weather=0 if good else 3, cloud=5 if good else 95, vis_m=40000))
        return out
    return fetch


def test_ski_end_to_end(tmp_path):
    now = datetime(2026, 12, 9, 4, tzinfo=UTC)
    cfg = dict(CFG, spots=[SCHEV], resorts=[RESORTS["uludag"], RESORTS["st-anton"]])
    by = sw.run_ski(cfg, now, fetch=fake_ski_factory(now.replace(hour=0)), out_dir=tmp_path)
    assert [len(by[r]) for r in ("uludag", "st-anton")] == [1, 1]
    w = by["st-anton"][0]
    assert w.start.startswith("2026-12-10T08") and "20 cm new snow" in w.summary and w.best >= 9
    cal = tmp_path / "docs" / "cal"
    assert events(cal / "st-anton.ics") == 1 and "Ski: St. Anton" in (cal / "st-anton.ics").read_text(encoding="utf-8")
    text = (tmp_path / "docs" / "ski.js").read_text(encoding="utf-8")
    data = json.loads(text[len("window.SKI="):].rstrip().rstrip(";"))
    assert [s["id"] for s in data["spots"]] == ["uludag", "st-anton"] and set(data["countries"]) == set(sw.SKI_COUNTRIES)
    s = data["spots"][1]
    assert len(s["score"]) == len(s["sf"]) == len(s["kind"]) == len(s["day"]) and s["windows"][0]["cond"].startswith("20 cm")

    # the surf run leaves the ski feeds alone; a resort that fails keeps its feed and its place on the page
    sw.run(cfg, now, fetch=fake_fetch_factory(now.replace(hour=0)), out_dir=tmp_path)
    assert (cal / "st-anton.ics").exists() and (cal / "uludag.ics").exists()

    def flaky(resort, days):
        if resort["id"] == "uludag":
            raise OSError("api down")
        return fake_ski_factory(now.replace(hour=0))(resort, days)
    sw.run_ski(cfg, now, fetch=flaky, out_dir=tmp_path)
    data = json.loads((tmp_path / "docs" / "ski.js").read_text(encoding="utf-8")[len("window.SKI="):].rstrip().rstrip(";"))
    assert [s["id"] for s in data["spots"]] == ["uludag", "st-anton"] and events(cal / "uludag.ics") == 1
