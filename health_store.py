"""健康数据存储层：纯 SQLite 实现，不依赖任何 AstrBot API。

本模块可以独立单元测试（pytest），AstrBot 插件（main.py）只负责把它接上
Web API 与 LLM 工具。

多用户绑定模型：
- 设备（devices）携带一个绑定码 binding_code（手机端生成，随上传上报）；
- 会话（unified_msg_origin）通过绑定码把设备绑到自己名下（bindings 多对多）；
- 查询与告警都以"会话 → 其绑定的设备"为范围，互不可见。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT '',
    battery INTEGER,
    last_seen INTEGER
);

CREATE TABLE IF NOT EXISTS samples (
    device_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    steps INTEGER,
    hr INTEGER,
    intensity REAL,
    PRIMARY KEY (device_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_samples_device_ts ON samples(device_id, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    ts INTEGER NOT NULL,
    value INTEGER,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_device_type_ts ON alerts(device_id, type, ts);

-- 会话 ↔ 设备 多对多绑定
CREATE TABLE IF NOT EXISTS bindings (
    device_id INTEGER NOT NULL,
    umo TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (device_id, umo)
);
CREATE INDEX IF NOT EXISTS idx_bindings_umo ON bindings(umo);

-- 待绑定：会话已发出绑定码，但设备尚未上报过该码
CREATE TABLE IF NOT EXISTS pending_binds (
    code TEXT NOT NULL,
    umo TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (code, umo)
);

-- 扩展指标（血氧/压力/HRV/呼吸率/睡眠时段/每日汇总/PAI/运动记录）
-- payload 为手机端上传的行 JSON（键为小写列名，timestamp 为 epoch 秒）
CREATE TABLE IF NOT EXISTS extended (
    device_id INTEGER NOT NULL,
    table_name TEXT NOT NULL,
    ts INTEGER NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    PRIMARY KEY (device_id, table_name, ts, seq)
);
"""

#: 同一 (设备, 告警类型) 的去重窗口，秒
ALERT_DEDUPE_SECONDS = 30 * 60

#: 一次上传允许的最大样本条数（防御异常客户端）
MAX_SAMPLES_PER_UPLOAD = 100_000

#: 一次上传允许的最大扩展行数（防御异常客户端）
MAX_EXTENDED_PER_CATEGORY = 20_000

SLEEP_KINDS = {"LIGHT_SLEEP", "DEEP_SLEEP", "REM_SLEEP", "AWAKE_SLEEP"}


def normalize_binding_code(raw: Any) -> str | None:
    """把绑定码归一化：去空格/连字符，转大写，去掉显示用的 GB- 前缀；空值返回 None。"""
    if raw is None:
        return None
    code = str(raw).strip().upper().replace("-", "").replace(" ", "")
    if not code:
        return None
    # 显示格式 "GB-XXXXXX" → "XXXXXX"；仅当长度超过 6（即确实带前缀）时剥离
    if code.startswith("GB") and len(code) > 6:
        code = code[2:]
    return code or None


class HealthStore:
    """SQLite 存储：设备、分钟级样本、告警、会话绑定。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        """幂等迁移：老库补 binding_code 列。"""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(devices)")}
        if "binding_code" not in cols:
            self._conn.execute("ALTER TABLE devices ADD COLUMN binding_code TEXT")

    # ------------------------------------------------------------------ 写入

    def ingest(
        self,
        device: dict[str, Any],
        samples: list[dict[str, Any]],
        extended: dict[str, Any] | None = None,
    ) -> tuple[int, dict | None, list[str]]:
        """写入一批样本（按 (device, ts) upsert 去重）与扩展指标。

        返回 (接受条数, 告警或 None, 本次上传刚完成绑定的会话 umo 列表)。
        device: {"address", "name", "type", "battery"(可选), "binding_code"(可选)}
        samples: [{"ts", "kind", "steps", "hr", "intensity"}, ...]
        extended: {"spo2": [{"timestamp", "spo2", ...}, ...], ...}（可选）
        """
        address = str(device.get("address") or "").strip()
        if not address:
            raise ValueError("device.address 不能为空")
        if not isinstance(samples, list):
            raise ValueError("samples 必须是数组")
        samples = samples[:MAX_SAMPLES_PER_UPLOAD]

        battery = self._clean_int(device.get("battery"))
        binding_code = normalize_binding_code(device.get("binding_code"))
        now = int(datetime.now().timestamp())
        clean_samples: list[tuple] = []
        for s in samples:
            if not isinstance(s, dict):
                continue
            ts = self._clean_int(s.get("ts"))
            if ts is None or ts <= 0:
                continue
            clean_samples.append(
                (
                    ts,
                    str(s.get("kind") or "")[:64],
                    self._clean_int(s.get("steps")),
                    self._clean_int(s.get("hr")),
                    self._clean_float(s.get("intensity")),
                )
            )

        with self._lock:
            self._conn.execute(
                "INSERT INTO devices (address, name, type, battery, last_seen, binding_code)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(address) DO UPDATE SET"
                " name=excluded.name, type=excluded.type,"
                " battery=COALESCE(excluded.battery, devices.battery),"
                " last_seen=excluded.last_seen,"
                " binding_code=COALESCE(excluded.binding_code, devices.binding_code)",
                (address, str(device.get("name") or "")[:128], str(device.get("type") or "")[:64], battery, now, binding_code),
            )
            row = self._conn.execute("SELECT id FROM devices WHERE address=?", (address,)).fetchone()
            device_id = int(row["id"])

            self._conn.executemany(
                "INSERT INTO samples (device_id, ts, kind, steps, hr, intensity)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(device_id, ts) DO UPDATE SET"
                " kind=excluded.kind, steps=excluded.steps,"
                " hr=excluded.hr, intensity=excluded.intensity",
                [(device_id, *s) for s in clean_samples],
            )

            # 解析待绑定：把本设备绑到所有 pending 该 code 的会话
            newly_bound_umos: list[str] = []
            if binding_code:
                pending = self._conn.execute(
                    "SELECT umo FROM pending_binds WHERE code=?", (binding_code,)
                ).fetchall()
                if pending:
                    for p in pending:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO bindings (device_id, umo, created_at) VALUES (?, ?, ?)",
                            (device_id, p["umo"], now),
                        )
                    self._conn.execute("DELETE FROM pending_binds WHERE code=?", (binding_code,))
                    newly_bound_umos = [p["umo"] for p in pending]

            # 扩展指标（血氧/压力/HRV/...），按 (device, category, ts, seq) upsert
            if isinstance(extended, dict):
                ext_rows: list[tuple] = []
                for category, rows in extended.items():
                    if not isinstance(rows, list):
                        continue
                    for r in rows[:MAX_EXTENDED_PER_CATEGORY]:
                        if not isinstance(r, dict):
                            continue
                        ts = self._clean_int(r.get("timestamp"))
                        if ts is None or ts <= 0:
                            continue
                        seq = self._clean_int(r.get("seq")) or 0
                        payload = {
                            k: v
                            for k, v in r.items()
                            if k not in ("timestamp", "seq", "device_id", "user_id")
                        }
                        ext_rows.append(
                            (
                                device_id,
                                str(category)[:64],
                                ts,
                                seq,
                                json.dumps(payload, ensure_ascii=False),
                            )
                        )
                if ext_rows:
                    self._conn.executemany(
                        "INSERT INTO extended (device_id, table_name, ts, seq, payload)"
                        " VALUES (?, ?, ?, ?, ?)"
                        " ON CONFLICT(device_id, table_name, ts, seq) DO UPDATE SET payload=excluded.payload",
                        ext_rows,
                    )

            self._conn.commit()

        alert = self._check_alerts(device_id, address, battery, clean_samples)
        return len(clean_samples), alert, newly_bound_umos

    # ------------------------------------------------------------ 绑定机制

    def register_pending_bind(self, code: str, umo: str) -> bool:
        """登记待绑定；返回是否为新登记（True=等待设备上报，False=已登记过）。"""
        code = normalize_binding_code(code)
        if not code:
            raise ValueError("绑定码不能为空")
        now = int(datetime.now().timestamp())
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO pending_binds (code, umo, created_at) VALUES (?, ?, ?)",
                (code, umo, now),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def bind_by_code(self, code: str, umo: str) -> dict | None:
        """按绑定码直接绑定（设备已上报过该码时）；成功返回设备信息，否则 None。"""
        code = normalize_binding_code(code)
        if not code:
            return None
        now = int(datetime.now().timestamp())
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, address, type FROM devices WHERE binding_code=?", (code,)
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "INSERT OR IGNORE INTO bindings (device_id, umo, created_at) VALUES (?, ?, ?)",
                (row["id"], umo, now),
            )
            # 该会话对同一 code 的待绑记录不再需要
            self._conn.execute(
                "DELETE FROM pending_binds WHERE code=? AND umo=?", (code, umo)
            )
            self._conn.commit()
        return {"id": int(row["id"]), "name": row["name"], "address": row["address"], "type": row["type"]}

    def unbind(self, umo: str, device_identifier: str) -> dict | None:
        """解除 umo 与设备的绑定（identifier 匹配 name 或 address）；返回设备信息或 None。"""
        identifier = device_identifier.strip().lower()
        if not identifier:
            return None
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, address FROM devices WHERE LOWER(name)=? OR LOWER(address)=?",
                (identifier, identifier),
            ).fetchall()
            if not rows:
                return None
            # 若命中多个，解除当前会话与所有这些设备的绑定
            removed: list[dict] = []
            for row in rows:
                cur = self._conn.execute(
                    "DELETE FROM bindings WHERE device_id=? AND umo=?", (row["id"], umo)
                )
                if cur.rowcount > 0:
                    removed.append({"id": int(row["id"]), "name": row["name"], "address": row["address"]})
            self._conn.commit()
        return removed[0] if removed else None

    def bound_devices(self, umo: str) -> list[dict]:
        """当前会话已绑定的设备列表（含电量与最后上报时间）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT d.id, d.address, d.name, d.type, d.battery, d.last_seen"
                " FROM bindings b JOIN devices d ON d.id = b.device_id"
                " WHERE b.umo=? ORDER BY d.last_seen DESC",
                (umo,),
            ).fetchall()
        return [dict(r) for r in rows]

    def device_ids_for_umo(self, umo: str) -> list[int]:
        """当前会话可见的设备 id 列表。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT device_id FROM bindings WHERE umo=?", (umo,)
            ).fetchall()
        return [int(r["device_id"]) for r in rows]

    def umos_for_device(self, device_id: int) -> list[str]:
        """绑定到某设备的所有会话。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT umo FROM bindings WHERE device_id=?", (device_id,)
            ).fetchall()
        return [r["umo"] for r in rows]

    # ---------------------------------------------------------------- 告警

    def _check_alerts(
        self,
        device_id: int,
        address: str,
        battery: int | None,
        samples: list[tuple],
    ) -> dict | None:
        """按阈值检查本批数据，返回（若有）一条告警。"""
        now = int(datetime.now().timestamp())

        hr_values = [(ts, hr) for ts, _, _, hr, _ in samples if hr is not None and hr > 0]
        if hr_values:
            max_hr = max(hr for _, hr in hr_values)
            if max_hr >= self.hr_threshold:
                alert = self._record_alert(device_id, "high_hr", now, max_hr, address, battery)
                if alert:
                    return alert

        if battery is not None and 0 <= battery <= self.battery_threshold:
            alert = self._record_alert(device_id, "low_battery", now, battery, address, battery)
            if alert:
                return alert
        return None

    def _record_alert(
        self,
        device_id: int,
        alert_type: str,
        now: int,
        value: int,
        address: str,
        battery: int | None,
    ) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT ts FROM alerts WHERE device_id=? AND type=?"
                " ORDER BY ts DESC LIMIT 1",
                (device_id, alert_type),
            ).fetchone()
            if row and now - int(row["ts"]) < ALERT_DEDUPE_SECONDS:
                return None
            message = self._alert_text(alert_type, address, value, battery)
            self._conn.execute(
                "INSERT INTO alerts (device_id, type, ts, value, message) VALUES (?,?,?,?,?)",
                (device_id, alert_type, now, value, message),
            )
            self._conn.commit()
        return {"type": alert_type, "ts": now, "value": value, "text": message, "device_id": device_id}

    @staticmethod
    def _alert_text(alert_type: str, address: str, value: int, battery: int | None) -> str:
        if alert_type == "high_hr":
            return f"⚠️ 心率告警：设备 {address} 心率达到 {value} 次/分"
        if alert_type == "low_battery":
            return f"🔋 电量告警：设备 {address} 电量仅剩 {value}%"
        return f"告警：{address} {value}"

    # ---------------------------------------------------------------- 查询

    def _scope_sql(self, device_ids: list[int] | None, column: str = "device_id") -> tuple[str, list]:
        if device_ids is None:
            return "", []
        if not device_ids:
            # 显式空列表 = 无可见设备，必须返回空结果（防止泄漏其他设备数据）
            return " AND 0=1", []
        marks = ",".join("?" for _ in device_ids)
        return f" AND {column} IN ({marks})", list(device_ids)

    def latest_hr(self, minutes: int = 30, device_ids: list[int] | None = None) -> dict | None:
        """最近 N 分钟内最新一条心率（hr>0）。"""
        since = int(datetime.now().timestamp()) - minutes * 60
        scope, scope_args = self._scope_sql(device_ids)
        with self._lock:
            row = self._conn.execute(
                "SELECT s.ts, s.hr, d.name, d.address FROM samples s"
                " JOIN devices d ON d.id = s.device_id"
                f" WHERE s.hr > 0 AND s.ts >= ?{scope} ORDER BY s.ts DESC LIMIT 1",
                (since, *scope_args),
            ).fetchone()
        if row is None:
            return None
        return {"ts": int(row["ts"]), "hr": int(row["hr"]), "device": row["name"] or row["address"]}

    def hr_trend(self, minutes: int = 60, device_ids: list[int] | None = None) -> list[dict]:
        """最近 N 分钟心率序列（用于趋势描述）。"""
        since = int(datetime.now().timestamp()) - minutes * 60
        scope, scope_args = self._scope_sql(device_ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT ts, hr FROM samples WHERE hr > 0 AND ts >= ?{scope} ORDER BY ts",
                (since, *scope_args),
            ).fetchall()
        return [{"ts": int(r["ts"]), "hr": int(r["hr"])} for r in rows]

    def steps_on(self, date: datetime.date, device_ids: list[int] | None = None) -> int:
        """某自然日（服务器本地时区）的步数总和。"""
        start = int(datetime(date.year, date.month, date.day).timestamp())
        end = start + 24 * 3600
        scope, scope_args = self._scope_sql(device_ids)
        with self._lock:
            row = self._conn.execute(
                f"SELECT COALESCE(SUM(steps), 0) AS total FROM samples"
                f" WHERE ts >= ? AND ts < ? AND steps > 0{scope}",
                (start, end, *scope_args),
            ).fetchone()
        return int(row["total"])

    def sleep_summary(self, date: datetime.date, device_ids: list[int] | None = None) -> dict:
        """“date 那晚”的睡眠汇总：窗口为 date-1 20:00 → date 12:00。"""
        start = int(datetime(date.year, date.month, date.day, 20, 0).timestamp()) - 24 * 3600
        end = int(datetime(date.year, date.month, date.day, 12, 0).timestamp())
        scope, scope_args = self._scope_sql(device_ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT kind, COUNT(*) AS minutes FROM samples"
                f" WHERE ts >= ? AND ts < ? AND kind IN"
                f" ('LIGHT_SLEEP','DEEP_SLEEP','REM_SLEEP','AWAKE_SLEEP'){scope}"
                f" GROUP BY kind",
                (start, end, *scope_args),
            ).fetchall()
        result = {kind: 0 for kind in SLEEP_KINDS}
        for r in rows:
            result[str(r["kind"])] = int(r["minutes"])
        result["total_minutes"] = sum(result.values())
        return result

    def battery(self, device_ids: list[int] | None = None) -> list[dict]:
        """设备的最近电量与最后上报时间。"""
        scope, scope_args = self._scope_sql(device_ids, column="d.id")
        with self._lock:
            rows = self._conn.execute(
                f"SELECT name, address, battery, last_seen FROM devices"
                f" WHERE battery IS NOT NULL{scope} ORDER BY last_seen DESC",
                scope_args,
            ).fetchall()
        return [
            {"device": r["name"] or r["address"], "battery": int(r["battery"]), "last_seen": int(r["last_seen"])}
            for r in rows
        ]

    def devices(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT address, name, type, battery, last_seen, binding_code"
                " FROM devices ORDER BY last_seen DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_alerts(self, days: int = 3, device_ids: list[int] | None = None) -> list[dict]:
        since = int(datetime.now().timestamp()) - days * 24 * 3600
        scope, scope_args = self._scope_sql(device_ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT a.type, a.ts, a.value, a.message, d.name AS device FROM alerts a"
                f" JOIN devices d ON d.id = a.device_id"
                f" WHERE a.ts >= ?{scope} ORDER BY a.ts DESC LIMIT 50",
                (since, *scope_args),
            ).fetchall()
        return [dict(r) for r in rows]

    def last_data_time(self, device_ids: list[int] | None = None) -> int | None:
        scope, scope_args = self._scope_sql(device_ids)
        with self._lock:
            row = self._conn.execute(
                f"SELECT MAX(ts) AS t FROM samples WHERE 1=1{scope}", scope_args
            ).fetchone()
        return int(row["t"]) if row and row["t"] is not None else None

    # ------------------------------------------------------------ 扩展指标

    def extended_latest(self, category: str, device_ids: list[int] | None = None, limit: int = 5) -> list[dict]:
        """某类扩展指标的最新若干条（按时间倒序）。"""
        scope, scope_args = self._scope_sql(device_ids, column="e.device_id")
        with self._lock:
            rows = self._conn.execute(
                f"SELECT ts, payload FROM extended e"
                f" WHERE table_name=?{scope} ORDER BY ts DESC, seq DESC LIMIT ?",
                (category, *scope_args, max(1, limit)),
            ).fetchall()
        return [self._merge_payload(int(r["ts"]), r["payload"]) for r in rows]

    def extended_range(self, category: str, device_ids: list[int] | None, since: int) -> list[dict]:
        """某类扩展指标在 since 之后的全部条目（按时间升序）。"""
        scope, scope_args = self._scope_sql(device_ids, column="e.device_id")
        with self._lock:
            rows = self._conn.execute(
                f"SELECT ts, payload FROM extended e"
                f" WHERE table_name=? AND ts >= ?{scope} ORDER BY ts ASC, seq ASC LIMIT 5000",
                (category, since, *scope_args),
            ).fetchall()
        return [self._merge_payload(int(r["ts"]), r["payload"]) for r in rows]

    @staticmethod
    def _merge_payload(ts: int, payload_json: str) -> dict:
        try:
            payload = json.loads(payload_json)
            if isinstance(payload, dict):
                payload["timestamp"] = ts
                return payload
        except (TypeError, ValueError):
            pass
        return {"timestamp": ts}

    # ---------------------------------------------------------------- 工具

    @staticmethod
    def _clean_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _clean_float(value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


def parse_date(value: str | None) -> datetime.date | None:
    """解析 'YYYY-MM-DD' / 'today' / 'yesterday'，非法返回 None。"""
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in ("", "today", "今天"):
        return datetime.now().date()
    if v in ("yesterday", "昨天", "昨日"):
        return datetime.now().date() - timedelta(days=1)
    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except ValueError:
        return None
