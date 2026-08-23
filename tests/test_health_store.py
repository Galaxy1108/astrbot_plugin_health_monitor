"""health_store 纯逻辑单元测试（不依赖 AstrBot 运行时）。"""

from __future__ import annotations

import datetime

import pytest

from health_store import HealthStore, normalize_binding_code, parse_date


@pytest.fixture()
def store(tmp_path):
    s = HealthStore(tmp_path / "health.db")
    s.hr_threshold = 120
    s.battery_threshold = 15
    yield s
    s.close()


def _sample(ts: int, kind: str = "ACTIVITY", steps: int = 0, hr: int = 0) -> dict:
    return {"ts": ts, "kind": kind, "steps": steps, "hr": hr, "intensity": 10.0}


def _device(address: str, **kw) -> dict:
    d = {"address": address, "name": f"dev-{address[-2:]}", "type": "XIAOMI"}
    d.update(kw)
    return d


# ---------------------------------------------------------------- 基础写入

def test_ingest_and_dedupe(store):
    device = _device("AA:BB:CC", battery=80)
    received, alert, bound = store.ingest(device, [_sample(1000, steps=5, hr=70), _sample(1060, steps=6, hr=75)])
    assert received == 2
    assert alert is None
    assert bound == []

    # 相同 (device, ts) 再传 → 覆盖，不新增
    received2, _, _ = store.ingest(device, [_sample(1000, steps=99, hr=71)])
    assert received2 == 1
    assert store.latest_hr(minutes=5) is None  # 时间戳太旧


def test_missing_address_rejected(store):
    with pytest.raises(ValueError):
        store.ingest({"address": " "}, [_sample(1000)])


def test_bad_samples_skipped(store):
    device = _device("AA:BB:CC", battery=50)
    received, _, _ = store.ingest(device, [_sample(1000), {"ts": "not-a-number"}, None, {"ts": -5}])
    assert received == 1


def test_steps_and_sleep_queries(store):
    now = datetime.datetime.now()
    today = now.date()
    midnight = datetime.datetime(today.year, today.month, today.day)
    start = int(midnight.timestamp())
    device = _device("AA:BB:CC")

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
    device = _device("AA:BB:CC")
    now = int(datetime.datetime.now().timestamp())

    _, alert1, _ = store.ingest(device, [_sample(now - 60, hr=150)])
    assert alert1 is not None and alert1["type"] == "high_hr"

    # 30 分钟内同一类型不重复告警
    _, alert2, _ = store.ingest(device, [_sample(now - 30, hr=160)])
    assert alert2 is None

    # 低阈值内的心率不告警
    _, alert3, _ = store.ingest(device, [_sample(now, hr=100)])
    assert alert3 is None


def test_battery_alerts(store):
    _, alert, _ = store.ingest(_device("AA:BB:CC", battery=10), [])
    assert alert is not None and alert["type"] == "low_battery"

    _, alert2, _ = store.ingest(_device("DD:EE:FF", battery=90), [])
    assert alert2 is None


# ---------------------------------------------------------------- 绑定机制

def test_normalize_binding_code():
    assert normalize_binding_code(" gb-a3k9qx ") == "A3K9QX"
    assert normalize_binding_code("GB-AB12CD") == "AB12CD"
    assert normalize_binding_code("ABC123") == "ABC123"
    assert normalize_binding_code("GB1234") == "GB1234"  # 原生码恰好以 GB 开头且长度 6，不剥离
    assert normalize_binding_code(None) is None
    assert normalize_binding_code("") is None


def test_pending_bind_then_auto_bind_on_upload(store):
    # 用户先发 /bind，设备还没上报过
    assert store.register_pending_bind("ABC123", "umo:group1") is True
    assert store.register_pending_bind("ABC123", "umo:group1") is False  # 幂等

    # 设备第一次上报（带同码）→ 自动绑定，返回新绑定会话
    device = _device("AA:BB:CC", binding_code="GB-ABC123")
    received, _, newly_bound = store.ingest(device, [_sample(1000)])
    assert received == 1
    assert newly_bound == ["umo:group1"]

    bound = store.bound_devices("umo:group1")
    assert len(bound) == 1
    assert bound[0]["address"] == "AA:BB:CC"

    # 再次上报不再触发绑定
    _, _, newly_bound2 = store.ingest(device, [_sample(1060)])
    assert newly_bound2 == []


def test_direct_bind_when_device_known(store):
    device = _device("AA:BB:CC", binding_code="XYZ789")
    store.ingest(device, [_sample(1000)])

    found = store.bind_by_code("xyz-789", "umo:private1")  # 归一化：去横线、大写
    assert found is not None and found["address"] == "AA:BB:CC"
    assert len(store.bound_devices("umo:private1")) == 1

    # 未知 code
    assert store.bind_by_code("NOPE123", "umo:private1") is None


def test_multi_session_same_device(store):
    device = _device("AA:BB:CC", binding_code="MULTI1")
    store.ingest(device, [_sample(1000)])

    store.bind_by_code("MULTI1", "umo:family-group")
    store.bind_by_code("MULTI1", "umo:dad-private")

    umos = store.umos_for_device(1)
    assert sorted(umos) == sorted(["umo:family-group", "umo:dad-private"])
    # 两个会话都能看到该设备
    assert len(store.bound_devices("umo:family-group")) == 1
    assert len(store.bound_devices("umo:dad-private")) == 1


def test_unbind_only_removes_session(store):
    device = _device("AA:BB:CC", binding_code="UNBND1")
    store.ingest(device, [_sample(1000)])
    store.bind_by_code("UNBND1", "umo:a")
    store.bind_by_code("UNBND1", "umo:b")

    removed = store.unbind("umo:a", "AA:BB:CC")
    assert removed is not None and removed["address"] == "AA:BB:CC"
    assert len(store.bound_devices("umo:a")) == 0
    assert len(store.bound_devices("umo:b")) == 1  # b 不受影响

    # 解绑不存在的绑定 → None
    assert store.unbind("umo:a", "AA:BB:CC") is None


def test_query_isolation_by_binding(store):
    now = int(datetime.datetime.now().timestamp())
    dev_a = _device("AA:BB:CC", binding_code="AAAAAA")
    dev_b = _device("DD:EE:FF", binding_code="BBBBBB")
    store.ingest(dev_a, [_sample(now - 60, steps=100, hr=80)])
    store.ingest(dev_b, [_sample(now - 30, steps=500, hr=95)])

    # 未绑定时：查不到任何数据
    assert store.device_ids_for_umo("umo:stranger") == []
    assert store.latest_hr(minutes=10, device_ids=[]) is None

    store.bind_by_code("AAAAAA", "umo:owner")
    ids = store.device_ids_for_umo("umo:owner")
    assert len(ids) == 1

    latest = store.latest_hr(minutes=10, device_ids=ids)
    assert latest is not None and latest["hr"] == 80  # 只看得到 A
    assert store.steps_on(datetime.datetime.now().date(), device_ids=ids) == 100

    # B 的心率对 owner 不可见
    assert store.latest_hr(minutes=10, device_ids=store.device_ids_for_umo("umo:other")) is None


def test_ingest_keeps_first_binding_code(store):
    device = _device("AA:BB:CC", binding_code="KEEP11")
    store.ingest(device, [_sample(1000)])
    # 老版本手机不带 code 再上报 → 不覆盖已记录的 code
    store.ingest(_device("AA:BB:CC"), [_sample(1060)])
    found = store.bind_by_code("KEEP11", "umo:x")
    assert found is not None


def test_parse_date():
    today = datetime.datetime.now().date()
    assert parse_date("today") == today
    assert parse_date("yesterday") == today - datetime.timedelta(days=1)
    assert parse_date("2024-01-15") == datetime.date(2024, 1, 15)
    assert parse_date("garbage") is None
    assert parse_date(None) is None
