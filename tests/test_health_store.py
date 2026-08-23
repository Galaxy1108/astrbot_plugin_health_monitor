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


def _bind_device(store: HealthStore, address: str, code: str = "TEST11", umo: str = "umo:test") -> None:
    """注册设备并绑定到测试会话（配对门禁要求先绑定才能收数据）。"""
    store.ingest(_device(address, binding_code=code), [])
    found = store.bind_by_code(code, umo)
    assert found is not None, f"bind {address} failed"


# ---------------------------------------------------------------- 配对门禁

def test_pending_bind_gate_blocks_data(store):
    """未绑定设备上传 → 不落库、返回 pending_bind。"""
    device = _device("AA:BB:CC", binding_code="GATE11")
    received, alert, bound, pending = store.ingest(device, [_sample(1000, steps=5, hr=70)])
    assert received == 0
    assert alert is None
    assert bound == []
    assert pending is True
    # 数据未落库
    assert store.steps_on(datetime.datetime.now().date()) == 0
    assert store.latest_hr(minutes=60) is None
    # 但设备行已登记，绑定码可立即生效
    assert store.bind_by_code("GATE11", "umo:owner") is not None


def test_bound_device_receives_data(store):
    _bind_device(store, "AA:BB:CC", code="BND11")
    received, alert, bound, pending = store.ingest(_device("AA:BB:CC"), [_sample(1000, steps=5, hr=70)])
    assert received == 1
    assert alert is None
    assert bound == []
    assert pending is False
    assert store.latest_hr(minutes=5) is None  # 时间戳太旧
    assert len(store.bound_devices("umo:test")) == 1


# ---------------------------------------------------------------- 基础写入

def test_ingest_and_dedupe(store):
    _bind_device(store, "AA:BB:CC")
    device = _device("AA:BB:CC", battery=80)
    received, alert, bound, pending = store.ingest(device, [_sample(1000, steps=5, hr=70), _sample(1060, steps=6, hr=75)])
    assert received == 2
    assert alert is None
    assert bound == []
    assert pending is False

    # 相同 (device, ts) 再传 → 覆盖，不新增
    received2, _, _, _ = store.ingest(device, [_sample(1000, steps=99, hr=71)])
    assert received2 == 1
    assert store.latest_hr(minutes=5) is None  # 时间戳太旧


def test_missing_address_rejected(store):
    with pytest.raises(ValueError):
        store.ingest({"address": " "}, [_sample(1000)])


def test_bad_samples_skipped(store):
    _bind_device(store, "AA:BB:CC")
    received, _, _, _ = store.ingest(_device("AA:BB:CC", battery=50), [_sample(1000), {"ts": "not-a-number"}, None, {"ts": -5}])
    assert received == 1


def test_steps_and_sleep_queries(store):
    _bind_device(store, "AA:BB:CC")
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
    _bind_device(store, "AA:BB:CC")
    device = _device("AA:BB:CC")
    now = int(datetime.datetime.now().timestamp())

    _, alert1, _, _ = store.ingest(device, [_sample(now - 60, hr=150)])
    assert alert1 is not None and alert1["type"] == "high_hr"

    # 30 分钟内同一类型不重复告警
    _, alert2, _, _ = store.ingest(device, [_sample(now - 30, hr=160)])
    assert alert2 is None

    # 低阈值内的心率不告警
    _, alert3, _, _ = store.ingest(device, [_sample(now, hr=100)])
    assert alert3 is None


def test_battery_alerts(store):
    _bind_device(store, "AA:BB:CC")
    _, alert, _, _ = store.ingest(_device("AA:BB:CC", battery=10), [])
    assert alert is not None and alert["type"] == "low_battery"

    _bind_device(store, "DD:EE:FF", code="BATT2")
    _, alert2, _, _ = store.ingest(_device("DD:EE:FF", battery=90), [])
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

    # 设备第一次上报（带同码）→ 自动绑定 + 本次数据入库
    device = _device("AA:BB:CC", binding_code="GB-ABC123")
    received, _, newly_bound, pending = store.ingest(device, [_sample(1000)])
    assert received == 1
    assert newly_bound == ["umo:group1"]
    assert pending is False

    bound = store.bound_devices("umo:group1")
    assert len(bound) == 1
    assert bound[0]["address"] == "AA:BB:CC"

    # 再次上报不再触发绑定
    _, _, newly_bound2, _ = store.ingest(device, [_sample(1060)])
    assert newly_bound2 == []


def test_direct_bind_when_device_known(store):
    device = _device("AA:BB:CC", binding_code="XYZ789")
    store.ingest(device, [_sample(1000)])  # 未绑定 → 门禁拦截，但设备行已登记

    found = store.bind_by_code("xyz-789", "umo:private1")  # 归一化：去横线、大写
    assert found is not None and found["address"] == "AA:BB:CC"
    assert len(store.bound_devices("umo:private1")) == 1

    # 未知 code
    assert store.bind_by_code("NOPE123", "umo:private1") is None


def test_multi_session_same_device(store):
    _bind_device(store, "AA:BB:CC", code="MULTI1")
    store.bind_by_code("MULTI1", "umo:family-group")
    store.bind_by_code("MULTI1", "umo:dad-private")

    umos = store.umos_for_device(1)
    assert sorted(umos) == sorted(["umo:test", "umo:family-group", "umo:dad-private"])
    # 所有会话都能看到该设备
    assert len(store.bound_devices("umo:family-group")) == 1
    assert len(store.bound_devices("umo:dad-private")) == 1
    assert len(store.bound_devices("umo:test")) == 1


def test_unbind_only_removes_session(store):
    _bind_device(store, "AA:BB:CC", code="UNBND1")
    store.bind_by_code("UNBND1", "umo:b")

    removed = store.unbind("umo:test", "AA:BB:CC")
    assert removed is not None and removed["address"] == "AA:BB:CC"
    assert len(store.bound_devices("umo:test")) == 0
    assert len(store.bound_devices("umo:b")) == 1  # b 不受影响

    # 解绑不存在的绑定 → None
    assert store.unbind("umo:test", "AA:BB:CC") is None


def test_unbind_by_binding_code(store):
    """解绑支持绑定码（形如 GB-XXXXXX）。"""
    _bind_device(store, "AA:BB:CC", code="CODE12")
    store.bind_by_code("CODE12", "umo:other")

    # 用带前缀的完整绑定码解绑 umo:test
    removed = store.unbind("umo:test", "GB-CODE12")
    assert removed is not None and removed["address"] == "AA:BB:CC"
    assert len(store.bound_devices("umo:test")) == 0
    # 其他会话不受影响
    assert len(store.bound_devices("umo:other")) == 1


def test_query_isolation_by_binding(store):
    now = int(datetime.datetime.now().timestamp())
    _bind_device(store, "AA:BB:CC", code="AAAAAA")
    _bind_device(store, "DD:EE:FF", code="BBBBBB", umo="umo:other")
    store.ingest(_device("AA:BB:CC", battery=80), [_sample(now - 60, steps=100, hr=80)])
    store.ingest(_device("DD:EE:FF", battery=90), [_sample(now - 30, steps=500, hr=95)])

    # 未绑定时：查不到任何数据
    assert store.device_ids_for_umo("umo:stranger") == []
    assert store.latest_hr(minutes=10, device_ids=[]) is None

    ids = store.device_ids_for_umo("umo:test")
    assert len(ids) == 1

    latest = store.latest_hr(minutes=10, device_ids=ids)
    assert latest is not None and latest["hr"] == 80  # owner 只看得到 A
    assert store.steps_on(datetime.datetime.now().date(), device_ids=ids) == 100

    # other 只能看到 B
    other_latest = store.latest_hr(minutes=10, device_ids=store.device_ids_for_umo("umo:other"))
    assert other_latest is not None and other_latest["hr"] == 95
    # A 的心率对 other 不可见（B 已绑 other，A 只绑 test）
    assert len(store.device_ids_for_umo("umo:test")) == 1

    # 电量查询带作用域（回归：battery 的 SQL 曾因表别名错误而失败）
    own_batteries = store.battery(device_ids=store.device_ids_for_umo("umo:test"))
    assert len(own_batteries) == 1
    assert own_batteries[0]["device"] == "dev-CC"
    assert store.battery(device_ids=store.device_ids_for_umo("umo:stranger")) == []


def test_ingest_keeps_first_binding_code(store):
    device = _device("AA:BB:CC", binding_code="KEEP11")
    store.ingest(device, [_sample(1000)])
    # 老版本手机不带 code 再上报 → 不覆盖已记录的 code
    store.ingest(_device("AA:BB:CC"), [_sample(1060)])
    found = store.bind_by_code("KEEP11", "umo:x")
    assert found is not None


# ---------------------------------------------------------------- 扩展指标

def test_extended_ingest_and_query(store):
    _bind_device(store, "AA:BB:CC", code="EXTND1")
    now = int(datetime.datetime.now().timestamp())
    store.ingest(
        _device("AA:BB:CC"),
        [_sample(1060)],
        extended={
            "spo2": [
                {"timestamp": now - 120, "spo2": 97, "device_id": 1},
                {"timestamp": now - 60, "spo2": 98},
            ],
            "hrv": [
                {"timestamp": now - 60, "seq": 1, "rr_millis": 800},
                {"timestamp": now - 60, "seq": 2, "rr_millis": 810},
            ],
            "workouts": [{"timestamp": now - 3600, "name": "晨跑", "activity_kind": 16}],
        },
    )
    # 同 (device, category, ts, seq) 覆盖更新
    store.ingest(_device("AA:BB:CC"), [], extended={"spo2": [{"timestamp": now - 120, "spo2": 99}]})

    latest = store.extended_latest("spo2", device_ids=[1], limit=2)
    assert len(latest) == 2
    assert latest[0]["spo2"] == 98
    assert latest[1]["spo2"] == 99  # 覆盖生效
    assert "device_id" not in latest[0]  # 服务端剥离内部列

    hrv = store.extended_latest("hrv", device_ids=[1], limit=5)
    assert len(hrv) == 2  # seq 区分同 ts 的多行
    assert hrv[0]["rr_millis"] == 810

    workouts = store.extended_range("workouts", device_ids=[1], since=0)
    assert len(workouts) == 1
    assert workouts[0]["name"] == "晨跑"

    # 隔离：空列表/其他设备查不到
    assert store.extended_latest("spo2", device_ids=[], limit=2) == []
    assert store.extended_latest("spo2", device_ids=[999], limit=2) == []
    assert store.extended_range("spo2", device_ids=[], since=0) == []


def test_extended_invalid_rows_skipped(store):
    _bind_device(store, "AA:BB:CC")
    now = int(datetime.datetime.now().timestamp())
    store.ingest(
        _device("AA:BB:CC"),
        [],
        extended={
            "spo2": [
                {"timestamp": now, "spo2": 96},
                {"timestamp": "bad", "spo2": 50},
                None,
                {"timestamp": -1, "spo2": 50},
            ]
        },
    )
    assert len(store.extended_latest("spo2", device_ids=[1], limit=10)) == 1


def test_extended_blocked_when_unbound(store):
    """未绑定设备的扩展数据同样不落库。"""
    now = int(datetime.datetime.now().timestamp())
    store.ingest(
        _device("AA:BB:CC", binding_code="EXTG1"),
        [_sample(1000)],
        extended={"spo2": [{"timestamp": now, "spo2": 96}]},
    )
    assert store.extended_latest("spo2", device_ids=[1], limit=10) == []


def test_parse_date():
    today = datetime.datetime.now().date()
    assert parse_date("today") == today
    assert parse_date("yesterday") == today - datetime.timedelta(days=1)
    assert parse_date("2024-01-15") == datetime.date(2024, 1, 15)
    assert parse_date("garbage") is None
    assert parse_date(None) is None


def test_hr_stats_on(store):
    """按天查询心率统计。"""
    _bind_device(store, "AA:BB:CC")
    now = datetime.datetime.now()
    today = now.date()
    midnight = datetime.datetime(today.year, today.month, today.day)
    start = int(midnight.timestamp())
    store.ingest(
        _device("AA:BB:CC"),
        [
            _sample(start + 0, hr=70),
            _sample(start + 60, hr=80),
            _sample(start + 120, hr=90),
            _sample(start + 180, hr=0),  # 无心率样本不计入
        ],
    )
    stats = store.hr_stats_on(today)
    assert stats["count"] == 3
    assert stats["min"] == 70
    assert stats["max"] == 90
    assert stats["avg"] == 80.0

    empty = store.hr_stats_on(datetime.date(2020, 1, 1))
    assert empty["count"] == 0 and empty["min"] is None
