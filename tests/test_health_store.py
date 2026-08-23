"""health_store 纯逻辑单元测试（不依赖 AstrBot 运行时）。"""

from __future__ import annotations

import datetime

import pytest

from health_store import HealthStore, parse_date


@pytest.fixture()
def store(tmp_path):
    s = HealthStore(tmp_path / "health.db")
    s.hr_threshold = 120
    s.battery_threshold = 15
    yield s
    s.close()


def _sample(ts: int, kind: str = "ACTIVITY", steps: int = 0, hr: int = 0) -> dict:
    return {"ts": ts, "kind": kind, "steps": steps, "hr": hr, "intensity": 10.0}


def test_ingest_and_dedupe(store):
    device = {"address": "AA:BB:CC", "name": "Mi Band", "type": "XIAOMI", "battery": 80}
    received, alert = store.ingest(device, [_sample(1000, steps=5, hr=70), _sample(1060, steps=6, hr=75)])
    assert received == 2
    assert alert is None

    # 相同 (device, ts) 再传 → 覆盖，不新增
    received2, _ = store.ingest(device, [_sample(1000, steps=99, hr=71)])
    assert received2 == 1
    assert store.latest_hr(minutes=5) is None  # 时间戳太旧


def test_missing_address_rejected(store):
    with pytest.raises(ValueError):
        store.ingest({"address": " "}, [_sample(1000)])


def test_bad_samples_skipped(store):
    device = {"address": "AA:BB:CC", "name": "D", "type": "T", "battery": 50}
    received, _ = store.ingest(device, [_sample(1000), {"ts": "not-a-number"}, None, {"ts": -5}])
    assert received == 1


def test_steps_and_sleep_queries(store, monkeypatch):
    now = datetime.datetime.now()
    today = now.date()
    midnight = datetime.datetime(today.year, today.month, today.day)
    start = int(midnight.timestamp())
    device = {"address": "AA:BB:CC", "name": "D", "type": "T"}

    samples = [
        _sample(start + 0, steps=100, hr=0),
        _sample(start + 60, steps=200, hr=0),
        _sample(start - 3600, kind="DEEP_SLEEP"),
        _sample(start - 3540, kind="LIGHT_SLEEP"),
    ]
    store.ingest(device, samples)
    assert store.steps_on(today) == 300

    summary = store.sleep_summary(today)
    assert summary["total_minutes"] == 2
    assert summary["DEEP_SLEEP"] == 1
    assert summary["LIGHT_SLEEP"] == 1


def test_hr_alerts_dedupe(store):
    device = {"address": "AA:BB:CC", "name": "D", "type": "T"}
    now = int(datetime.datetime.now().timestamp())

    _, alert1 = store.ingest(device, [_sample(now - 60, hr=150)])
    assert alert1 is not None and alert1["type"] == "high_hr"

    # 30 分钟内同一类型不重复告警
    _, alert2 = store.ingest(device, [_sample(now - 30, hr=160)])
    assert alert2 is None

    # 低阈值内的心率不告警
    _, alert3 = store.ingest(device, [_sample(now, hr=100)])
    assert alert3 is None


def test_battery_alerts(store):
    device = {"address": "AA:BB:CC", "name": "D", "type": "T", "battery": 10}
    _, alert = store.ingest(device, [])
    assert alert is not None and alert["type"] == "low_battery"

    # 高电量不告警
    device2 = {"address": "DD:EE:FF", "name": "D2", "type": "T", "battery": 90}
    _, alert2 = store.ingest(device2, [])
    assert alert2 is None


def test_latest_hr(store, monkeypatch):
    device = {"address": "AA:BB:CC", "name": "Mi Band", "type": "T"}
    now = int(datetime.datetime.now().timestamp())
    store.ingest(device, [_sample(now - 120, hr=80), _sample(now - 60, hr=95)])
    latest = store.latest_hr(minutes=30)
    assert latest is not None
    assert latest["hr"] == 95
    assert latest["device"] == "Mi Band"


def test_parse_date():
    today = datetime.datetime.now().date()
    assert parse_date("today") == today
    assert parse_date("yesterday") == today - datetime.timedelta(days=1)
    assert parse_date("2024-01-15") == datetime.date(2024, 1, 15)
    assert parse_date("garbage") is None
    assert parse_date(None) is None
