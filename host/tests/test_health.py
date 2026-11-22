from datetime import datetime
from zoneinfo import ZoneInfo

from spark_bridge.config import HealthConfig
from spark_bridge.health import HealthMonitor, fmt_uptime
from spark_bridge.store import Store

AMS = ZoneInfo("Europe/Amsterdam")


def ts(y, mo, d, h, mi=0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=AMS).timestamp())


def mk(now: int):
    st = Store()
    clock = {"t": now}
    return st, HealthMonitor(st, HealthConfig(tz="Europe/Amsterdam"), lambda: clock["t"]), clock


def test_picks_nearest_sample_within_90_minutes():
    st, hm, _ = mk(ts(2026, 11, 20, 15, 5))
    st.add_battery(ts(2026, 11, 20, 6, 40), 3900)
    st.add_battery(ts(2026, 11, 20, 7, 5), 3950)  # nearest to 07:00
    st.add_battery(ts(2026, 11, 20, 14, 30), 4100)
    assert hm.nearest(datetime(2026, 11, 20, 7, 0, tzinfo=AMS)) == 3950
    assert hm.nearest(datetime(2026, 11, 20, 15, 0, tzinfo=AMS)) == 4100
    assert hm.nearest(datetime(2026, 11, 20, 12, 0, tzinfo=AMS)) is None  # >90 min from both


def test_overnight_delta_and_missing_samples():
    st, hm, _ = mk(ts(2026, 11, 20, 15, 5))
    st.add_battery(ts(2026, 11, 19, 15, 2), 4150)
    st.add_battery(ts(2026, 11, 20, 7, 3), 3980)
    st.add_battery(ts(2026, 11, 20, 15, 1), 4090)
    r = hm.build_report()
    assert "07:00: 3980 mV" in r and "15:00: 4090 mV" in r and "-170 mV" in r


def test_report_with_no_data_is_na():
    _, hm, _ = mk(ts(2026, 11, 20, 15, 5))
    r = hm.build_report()
    assert r.count("n/a") >= 5


def test_next_report_time_dst_and_rollover():
    _, hm, _ = mk(0)
    before = ts(2026, 11, 20, 14, 0)
    assert hm.next_report(before) == ts(2026, 11, 20, 15, 5)
    after = ts(2026, 11, 20, 15, 5)
    assert hm.next_report(after) == ts(2026, 11, 21, 15, 5)
    # across the autumn DST change (2026-10-25) the wall-clock time stays 15:05
    assert hm.next_report(ts(2026, 10, 24, 16, 0)) == ts(2026, 10, 25, 15, 5)


def test_availability_from_link_events():
    now = ts(2026, 11, 20, 12, 0)
    st, hm, _ = mk(now)
    assert hm.availability(now) is None
    st.add_link_event(now - 86400 - 10, True)
    st.add_link_event(now - 43200, False)  # down for 6 hours
    st.add_link_event(now - 21600, True)
    assert abs(hm.availability(now) - 0.75) < 0.001


def test_availability_unknown_start_counts_from_first_event():
    now = ts(2026, 11, 20, 12, 0)
    st, hm, _ = mk(now)
    st.add_link_event(now - 3600, True)
    assert hm.availability(now) == 1.0


def test_record_and_live_values():
    st, hm, _ = mk(ts(2026, 11, 20, 7, 0))
    hm.record({"battery_mv": 3999, "uptime_secs": 90000, "noise_floor": -110})
    assert st.last_battery()["mv"] == 3999
    r = hm.build_report(live={"battery_mv": 4001, "uptime_secs": 90100, "noise_floor": -111,
                              "last_rssi": -90, "last_snr": 6.0})
    assert "-111 dBm" in r and "now: 4001 mV" in r and "1d 1h 1m" in r


def test_fmt_uptime():
    assert fmt_uptime(86400) == "1d 0h 0m" and fmt_uptime(None) == "n/a"
