"""健康数据存储层：纯 SQLite 实现，不依赖任何 AstrBot API。

本模块可以独立单元测试（pytest），AstrBot 插件（main.py）只负责把它接上
Web API 与 LLM 工具。
"""

from __future__ import annotations

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
"""

#: 同一 (设备, 告警类型) 的去重窗口，秒
ALERT_DEDUPE_SECONDS = 30 * 60

#: 一次上传允许的最大样本条数（防御异常客户端）
MAX_SAMPLES_PER_UPLOAD = 100_000

SLEEP_KINDS = {"LIGHT_SLEEP", "DEEP_SLEEP", "REM_SLEEP", "AWAKE_SLEEP"}


class HealthStore:
    """SQLite 存储：设备、分钟级样本、告警。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ 写入

    def ingest(self, device: dict[str, Any], samples: list[dict[str, Any]]) -> tuple[int, dict | None]:
        """写入一批样本（按 (device, ts) upsert 去重），返回 (接受条数, 告警或 None)。

        device: {"address", "name", "type", "battery"(可选)}
        samples: [{"ts", "kind", "steps", "hr", "intensity"}, ...]
        """
        address = str(device.get("address") or "").strip()
        if not address:
            raise ValueError("device.address 不能为空")
        if not isinstance(samples, list):
            raise ValueError("samples 必须是数组")
        samples = samples[:MAX_SAMPLES_PER_UPLOAD]

        battery = self._clean_int(device.get("battery"))
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
            cur = self._conn.execute(
                "INSERT INTO devices (address, name, type, battery, last_seen)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(address) DO UPDATE SET"
                " name=excluded.name, type=excluded.type,"
                " battery=COALESCE(excluded.battery, devices.battery),"
                " last_seen=excluded.last_seen",
                (address, str(device.get("name") or "")[:128], str(device.get("type") or "")[:64], battery, now),
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
            self._conn.commit()

        alert = self._check_alerts(device_id, address, battery, clean_samples)
        return len(clean_samples), alert

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
        return {"type": alert_type, "ts": now, "value": value, "text": message}

    @staticmethod
    def _alert_text(alert_type: str, address: str, value: int, battery: int | None) -> str:
        if alert_type == "high_hr":
            return f"⚠️ 心率告警：设备 {address} 心率达到 {value} 次/分（阈值 {value}）"
        if alert_type == "low_battery":
            return f"🔋 电量告警：设备 {address} 电量仅剩 {value}%"
        return f"告警：{address} {value}"

    # ---------------------------------------------------------------- 查询

    def latest_hr(self, minutes: int = 30) -> dict | None:
        """最近 N 分钟内最新一条心率（hr>0）。"""
        since = int(datetime.now().timestamp()) - minutes * 60
        with self._lock:
            row = self._conn.execute(
                "SELECT s.ts, s.hr, d.name, d.address FROM samples s"
                " JOIN devices d ON d.id = s.device_id"
                " WHERE s.hr > 0 AND s.ts >= ? ORDER BY s.ts DESC LIMIT 1",
                (since,),
            ).fetchone()
        if row is None:
            return None
        return {"ts": int(row["ts"]), "hr": int(row["hr"]), "device": row["name"] or row["address"]}

    def hr_trend(self, minutes: int = 60) -> list[dict]:
        """最近 N 分钟心率序列（用于趋势描述）。"""
        since = int(datetime.now().timestamp()) - minutes * 60
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, hr FROM samples WHERE hr > 0 AND ts >= ? ORDER BY ts",
                (since,),
            ).fetchall()
        return [{"ts": int(r["ts"]), "hr": int(r["hr"])} for r in rows]

    def steps_on(self, date: datetime.date) -> int:
        """某自然日（服务器本地时区）的步数总和。"""
        start = int(datetime(date.year, date.month, date.day).timestamp())
        end = start + 24 * 3600
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(steps), 0) AS total FROM samples"
                " WHERE ts >= ? AND ts < ? AND steps > 0",
                (start, end),
            ).fetchone()
        return int(row["total"])

    def sleep_summary(self, date: datetime.date) -> dict:
        """“date 那晚”的睡眠汇总：窗口为 date-1 20:00 → date 12:00。"""
        start = int(datetime(date.year, date.month, date.day, 20, 0).timestamp()) - 24 * 3600
        end = int(datetime(date.year, date.month, date.day, 12, 0).timestamp())
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*) AS minutes FROM samples"
                " WHERE ts >= ? AND ts < ? AND kind IN ('LIGHT_SLEEP','DEEP_SLEEP','REM_SLEEP','AWAKE_SLEEP')"
                " GROUP BY kind",
                (start, end),
            ).fetchall()
        result = {kind: 0 for kind in SLEEP_KINDS}
        for r in rows:
            result[str(r["kind"])] = int(r["minutes"])
        result["total_minutes"] = sum(result.values())
        return result

    def battery(self) -> list[dict]:
        """所有设备的最近电量与最后上报时间。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, address, battery, last_seen FROM devices WHERE battery IS NOT NULL"
                " ORDER BY last_seen DESC"
            ).fetchall()
        return [
            {"device": r["name"] or r["address"], "battery": int(r["battery"]), "last_seen": int(r["last_seen"])}
            for r in rows
        ]

    def devices(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT address, name, type, battery, last_seen FROM devices ORDER BY last_seen DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_alerts(self, days: int = 3) -> list[dict]:
        since = int(datetime.now().timestamp()) - days * 24 * 3600
        with self._lock:
            rows = self._conn.execute(
                "SELECT a.type, a.ts, a.value, a.message, d.name AS device FROM alerts a"
                " JOIN devices d ON d.id = a.device_id WHERE a.ts >= ? ORDER BY a.ts DESC LIMIT 50",
                (since,),
            ).fetchall()
        return [dict(r) for r in rows]

    def last_data_time(self) -> int | None:
        with self._lock:
            row = self._conn.execute("SELECT MAX(ts) AS t FROM samples").fetchone()
        return int(row["t"]) if row and row["t"] is not None else None

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
    from datetime import date as _date

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
