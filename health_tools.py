"""LLM 工具：把健康数据查询暴露给 AstrBot 智能体。

多用户隔离：每个工具从调用上下文取出当前会话（unified_msg_origin），
只查询该会话已绑定设备的数据；未绑定任何设备时返回引导文案。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass

from .health_store import HealthStore, parse_date
from .temp_files import (
    TEMP_MAX_AGE_SECONDS,
    cleanup_temp,
    resolve_temp_file,
    temp_tag,
    write_temp_series,
)

#: 未绑定时给 LLM 的引导文案（让模型原样回复用户）
_BIND_GUIDE = "你还没有绑定任何设备。请让用户先发送：/bind <绑定码>，绑定码在手机 Gadgetbridge 的「设置 → 自动化 → Webhook 上传」页面查看（形如 GB-XXXXXX）。"

#: 心率明细超过该条数时不再内联返回，而是写入临时文件让 AI 用 read_temp_file 读取
_HR_INLINE_LIMIT = 200


def _current_umo(context: ContextWrapper[AstrAgentContext]) -> str | None:
    """从工具调用上下文取当前会话 unified_msg_origin。"""
    try:
        event = context.context.event
        umo = getattr(event, "unified_msg_origin", None)
        return str(umo) if umo else None
    except Exception:  # noqa: BLE001
        return None


def _scope(store: HealthStore, umo: str | None) -> list[int]:
    """当前会话可见的设备 id；取不到会话时返回空（不可见任何数据）。"""
    if not umo:
        return []
    return store.device_ids_for_umo(umo)


def _fmt_ts(ts: int) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return str(ts)


@dataclass
class HealthLatestTool(FunctionTool[AstrAgentContext]):
    """查询最新健康状态：当前心率 / 电量 / 是否有新数据。"""

    name: str = "health_latest"
    description: str = (
        "查询健康数据的最新状态。metric 取值：heart_rate（最近心率）、"
        "battery（各设备电量）、overview（心率+电量+最后数据时间）。"
        "只能查询当前用户已绑定设备的数据。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "enum": ["heart_rate", "battery", "overview"],
                    "description": "要查询的指标",
                },
            },
            "required": ["metric"],
        }
    )
    store: Any = None

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        device_ids = _scope(self.store, _current_umo(context))
        if not device_ids:
            return _BIND_GUIDE
        metric = str(kwargs.get("metric") or "overview").strip()
        if metric == "heart_rate":
            latest = self.store.latest_hr(minutes=30, device_ids=device_ids)
            if latest is None:
                return "最近 30 分钟内没有心率数据。"
            return (
                f"{latest['device']} 最近心率：{latest['hr']} 次/分"
                f"（时间 {_fmt_ts(latest['ts'])}）。"
            )
        if metric == "battery":
            batteries = self.store.battery(device_ids=device_ids)
            if not batteries:
                return "还没有电量数据。"
            return "；".join(
                f"{b['device']} 电量 {b['battery']}%（最后上报 {_fmt_ts(b['last_seen'])}）"
                for b in batteries
            )
        # overview
        latest = self.store.latest_hr(minutes=60, device_ids=device_ids)
        batteries = self.store.battery(device_ids=device_ids)
        last_data = self.store.last_data_time(device_ids=device_ids)
        parts = []
        if latest:
            parts.append(f"最近心率 {latest['hr']} 次/分（{latest['device']}）")
        else:
            parts.append("最近 1 小时内无心率数据")
        if batteries:
            parts.append("；".join(f"{b['device']} 电量 {b['battery']}%" for b in batteries))
        if last_data:
            parts.append(f"最后数据时间 {_fmt_ts(last_data)}")
        return "。".join(parts) + "。"


@dataclass
class HealthStepsTool(FunctionTool[AstrAgentContext]):
    """查询步数。"""

    name: str = "health_steps"
    description: str = (
        "查询某一天的步数（仅当前用户已绑定设备）。date 为 YYYY-MM-DD，"
        "或 today / yesterday。不传时默认今天。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "日期：YYYY-MM-DD / today / yesterday",
                },
            },
        }
    )
    store: Any = None

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        device_ids = _scope(self.store, _current_umo(context))
        if not device_ids:
            return _BIND_GUIDE
        day = parse_date(kwargs.get("date"))
        if day is None:
            return "日期格式无法识别，请使用 YYYY-MM-DD 或 today / yesterday。"
        total = self.store.steps_on(day, device_ids=device_ids)
        return f"{day.isoformat()} 共走了 {total} 步。"


@dataclass
class HealthSleepTool(FunctionTool[AstrAgentContext]):
    """查询睡眠。"""

    name: str = "health_sleep"
    description: str = (
        "查询某天晚上的睡眠情况（前一晚 20:00 至当天 12:00），返回各睡眠阶段分钟数"
        "（仅当前用户已绑定设备）。date 为 YYYY-MM-DD，或 today / yesterday；"
        "不传时默认今天（即“昨晚”）。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "日期：YYYY-MM-DD / today / yesterday",
                },
            },
        }
    )
    store: Any = None

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        device_ids = _scope(self.store, _current_umo(context))
        if not device_ids:
            return _BIND_GUIDE
        day = parse_date(kwargs.get("date")) or datetime.now().date()
        summary = self.store.sleep_summary(day, device_ids=device_ids)
        total = summary["total_minutes"]
        if total == 0:
            return f"{day.isoformat()} 那晚没有睡眠数据。"
        parts = [
            f"总睡眠约 {total} 分钟（{total // 60} 小时 {total % 60} 分）",
            f"深睡 {summary['DEEP_SLEEP']} 分钟",
            f"浅睡 {summary['LIGHT_SLEEP']} 分钟",
            f"REM 睡眠 {summary['REM_SLEEP']} 分钟",
            f"清醒 {summary['AWAKE_SLEEP']} 分钟",
        ]
        return f"{day.isoformat()} 那晚：" + "，".join(parts) + "。"


@dataclass
class HealthHrHistoryTool(FunctionTool[AstrAgentContext]):
    """查询某天的心率统计或完整曲线。"""

    name: str = "health_hr_history"
    description: str = (
        "查询某一天的心率（仅当前用户已绑定设备）。date 为 YYYY-MM-DD，"
        "或 today / yesterday；不传时默认今天。detail=false（默认）返回统计"
        "（最低/最高/平均/条数）；detail=true 返回完整心率曲线（每条的时间与数值），"
        "数据量大时会写入临时文件并提示用 read_temp_file 读取。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "日期：YYYY-MM-DD / today / yesterday",
                },
                "detail": {
                    "type": "boolean",
                    "description": "是否返回完整心率曲线明细（默认 false）",
                },
            },
        }
    )
    store: Any = None
    data_dir: Any = None

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        device_ids = _scope(self.store, _current_umo(context))
        if not device_ids:
            return _BIND_GUIDE
        day = parse_date(kwargs.get("date")) or datetime.now().date()
        stats = self.store.hr_stats_on(day, device_ids=device_ids)
        if stats["count"] == 0:
            return f"{day.isoformat()} 没有心率数据。"
        if not kwargs.get("detail"):
            return (
                f"{day.isoformat()} 心率：最低 {stats['min']}，最高 {stats['max']}，"
                f"平均 {stats['avg']} 次/分（共 {stats['count']} 条记录）。"
            )
        series = self.store.hr_series_on(day, device_ids=device_ids)
        if len(series) <= _HR_INLINE_LIMIT:
            inline = json.dumps(
                [{"t": p["ts"], "hr": p["hr"]} for p in series],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            return f"{day.isoformat()} 完整心率曲线（{len(series)} 条）：{inline}"
        if self.data_dir is None:
            return f"{day.isoformat()} 共 {len(series)} 条心率记录，数据量较大，无法内联返回。"
        tag = temp_tag(_current_umo(context) or "")
        name = write_temp_series(Path(self.data_dir), [{"t": p["ts"], "hr": p["hr"]} for p in series], tag=tag)
        return (
            f"{day.isoformat()} 共 {len(series)} 条心率记录，已写入临时文件 {name}。"
            f"请调用 read_temp_file 工具读取该文件（path 参数传 {name}），读取后文件会自动删除。"
        )


@dataclass
class ReadTempFileTool(FunctionTool[AstrAgentContext]):
    """读取健康数据临时文件（读取后自动删除）。"""

    name: str = "read_temp_file"
    description: str = (
        "读取健康数据临时文件的内容（如完整心率曲线）。path 为文件名"
        "（形如 hr_20260824_235000_1a2b3c.json）。读取成功后文件自动删除；"
        "文件可能已被删除（已读过或过期清理），此时返回提示。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "临时文件名（不含路径）",
                },
            },
            "required": ["path"],
        }
    )
    data_dir: Any = None

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        if self.data_dir is None:
            return "临时文件目录未配置。"
        raw = str(kwargs.get("path") or "").strip()
        target = resolve_temp_file(Path(self.data_dir), raw)
        if target is None:
            return f"非法的临时文件名：{raw}。"
        cleanup_temp(target.parent, TEMP_MAX_AGE_SECONDS)
        if not target.is_file():
            return f"临时文件不存在（可能已读取删除或过期清理）：{target.name}"
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        finally:
            target.unlink(missing_ok=True)
        return content


@dataclass
class HealthAlertsTool(FunctionTool[AstrAgentContext]):
    """查询近期告警。"""

    name: str = "health_alerts"
    description: str = (
        "查询最近几天的健康告警（心率过高、电量过低，仅当前用户已绑定设备）。"
        "days 默认 3。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "查询最近多少天，默认 3"},
            },
        }
    )
    store: Any = None

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        device_ids = _scope(self.store, _current_umo(context))
        if not device_ids:
            return _BIND_GUIDE
        try:
            days = max(1, min(int(kwargs.get("days") or 3), 30))
        except (TypeError, ValueError):
            days = 3
        alerts = self.store.recent_alerts(days, device_ids=device_ids)
        if not alerts:
            return f"最近 {days} 天没有告警记录。"
        lines = [f"{a['device']}：{a['message']}（{_fmt_ts(a['ts'])}）" for a in alerts]
        return f"最近 {days} 天共有 {len(alerts)} 条告警：\n" + "\n".join(lines)


#: 各扩展指标的人类可读格式化（payload 键为手机端上传的小写列名）
_EXTENDED_FORMATTERS: dict[str, tuple[str, list[str]]] = {
    "spo2": ("血氧 SpO2", ["spo2"]),
    "stress": ("压力", ["stress"]),
    "hrv": ("HRV（RR 间期）", ["rr_millis", "hrv"]),
    "respiration": ("睡眠呼吸率", ["rate"]),
    "sleep_sessions": ("睡眠时段", ["total_duration", "deep_sleep_duration", "light_sleep_duration", "rem_sleep_duration", "awake_duration", "wakeup_time", "stage"]),
    "dailysummary": ("每日汇总", ["steps", "hr_resting", "hr_avg", "hr_max", "hr_min", "stress_avg", "stress_max", "type", "value"]),
    "pai": ("PAI", ["pai_today", "pai_total", "pai_low", "pai_moderate", "pai_high"]),
    "workouts": ("运动记录", ["name", "activity_kind", "type", "distance", "calories", "step_count", "duration", "summary_data", "start_time", "end_time"]),
    "workout_hr": ("运动过程心率", ["heart_rate", "step_rate", "speed"]),
}

#: 华为运动 type 编码 → 中文（见 HuaweiWorkoutGbParser.HuaweiActivityType）
_HUAWEI_TYPE_NAMES = {
    1: "跑步", 2: "走路", 3: "骑行", 4: "登山", 5: "室内跑步", 6: "泳池游泳",
    7: "室内骑行", 8: "公开水域游泳", 11: "越野跑", 13: "室内走路", 14: "徒步",
    21: "跳绳", 22: "自由潜水", 23: "闭气训练", 24: "闭气测试", 25: "水肺潜水",
    128: "乒乓球", 129: "羽毛球", 130: "网球", 131: "足球", 132: "篮球",
    133: "排球", 134: "椭圆机", 135: "划船机", 136: "踏步机", 137: "瑜伽",
    138: "普拉提", 139: "有氧操", 140: "力量训练", 141: "动感单车", 142: "空中漫步",
    143: "HIIT", 145: "CrossFit", 146: "功能性训练", 147: "体能训练",
    148: "跆拳道", 149: "拳击", 150: "自由搏击", 151: "空手道", 152: "击剑",
    153: "肚皮舞", 154: "爵士舞", 155: "拉丁舞", 156: "芭蕾", 157: "核心训练",
    158: "BodyCombat", 159: "剑道", 160: "单杠", 161: "双杠", 162: "街舞",
    163: "轮滑", 164: "武术", 165: "广场舞", 166: "太极", 167: "舞蹈",
    168: "呼啦圈", 169: "飞盘", 170: "飞镖", 171: "射箭", 172: "骑马",
    173: "激光枪战", 174: "放风筝", 175: "拔河", 176: "荡秋千", 177: "爬楼梯",
    178: "障碍赛", 179: "台球", 180: "瑜伽",
}

_ACTIVITY_KIND_NAMES = {
    1: "活动", 2: "浅睡", 4: "深睡", 8: "未佩戴", 16: "跑步", 32: "走路", 64: "游泳",
    128: "骑行", 256: "跑步机", 512: "锻炼", 1024: "公开水域游泳", 2048: "室内骑行",
    4096: "椭圆机", 8192: "跳绳", 16384: "瑜伽", 32768: "足球", 65536: "划船机",
    131072: "板球", 262144: "篮球", 524288: "乒乓球", 1048576: "羽毛球",
    2097152: "力量训练", 4194304: "徒步", 8388608: "攀岩", 33554432: "清醒",
}


def _workout_kind_name(row: dict) -> str:
    """运动类型：优先 GB activity_kind，其次华为 type。"""
    kind = row.get("activity_kind")
    if isinstance(kind, int) and kind in _ACTIVITY_KIND_NAMES:
        return _ACTIVITY_KIND_NAMES[kind]
    htype = row.get("type")
    if isinstance(htype, int) and htype in _HUAWEI_TYPE_NAMES:
        return _HUAWEI_TYPE_NAMES[htype]
    if isinstance(kind, int) or isinstance(htype, int):
        return f"未知({kind if isinstance(kind, int) else htype})"
    return "未知"


def _workout_hr_avg(row: dict) -> int | None:
    """运动记录的平均心率（summary_data 里的 averageHR）。"""
    sd = row.get("summary_data")
    if not isinstance(sd, str):
        return None
    try:
        obj = json.loads(sd)
        val = obj.get("averageHR", {}).get("value")
        return int(val) if isinstance(val, (int, float)) else None
    except (ValueError, TypeError):
        return None


def _fmt_extended_row(metric: str, row: dict) -> str:
    ts = row.get("timestamp")
    time_str = _fmt_ts(ts) if isinstance(ts, int) else ""
    if metric == "workouts":
        kind_name = _workout_kind_name(row)
        name = row.get("name") or kind_name
        parts = [f"{name}（{kind_name}）于 {time_str}"]
        dist = row.get("distance")
        if isinstance(dist, (int, float)) and dist > 0:
            parts.append(f"距离 {dist / 1000:.2f} 公里")
        calories = row.get("calories")
        if isinstance(calories, (int, float)) and calories > 0:
            parts.append(f"消耗 {calories} 千卡")
        steps = row.get("step_count")
        if isinstance(steps, (int, float)) and steps > 0:
            parts.append(f"{steps} 步")
        duration = row.get("duration")
        if isinstance(duration, (int, float)) and duration > 0:
            parts.append(f"时长 {int(duration) // 60} 分 {int(duration) % 60} 秒")
        avg_hr = _workout_hr_avg(row)
        if avg_hr is not None:
            parts.append(f"平均心率 {avg_hr}")
        return "，".join(parts)
    if metric == "workout_hr":
        hr = row.get("heart_rate")
        step_rate = row.get("step_rate")
        parts = [time_str]
        if isinstance(hr, (int, float)):
            parts.append(f"心率 {hr}")
        if isinstance(step_rate, (int, float)):
            parts.append(f"步频 {step_rate}")
        return "，".join(parts)
    if metric == "hrv":
        rr = row.get("rr_millis")
        if isinstance(rr, (int, float)):
            return f"RR 间期 {rr} ms（{time_str}）"
        return f"{row}（{time_str}）"
    if metric == "sleep_sessions":
        total = row.get("total_duration")
        if isinstance(total, (int, float)):
            minutes = int(total) // 60
            deep = row.get("deep_sleep_duration")
            deep_min = int(deep) // 60 if isinstance(deep, (int, float)) else None
            parts = [f"总睡眠 {minutes} 分钟"]
            if deep_min is not None:
                parts.append(f"深睡 {deep_min} 分钟")
            parts.append(f"开始 {time_str}")
            return "，".join(parts)
        return f"睡眠时段（{time_str}）"
    label, fields = _EXTENDED_FORMATTERS[metric]
    shown = [f"{k}={row[k]}" for k in fields if k in row]
    if not shown:
        return f"{label}（{time_str}）"
    return f"{label}：{'，'.join(shown)}（{time_str}）"


@dataclass
class HealthExtendedTool(FunctionTool[AstrAgentContext]):
    """查询扩展指标：血氧 / 压力 / HRV / 呼吸率 / 睡眠时段 / 每日汇总 / PAI / 运动记录 / 运动过程心率。"""

    name: str = "health_extended"
    description: str = (
        "查询扩展健康指标（仅当前用户已绑定设备）。metric 取值："
        "spo2（血氧）、stress（压力）、hrv（HRV/RR 间期）、respiration（睡眠呼吸率）、"
        "sleep_sessions（睡眠时段汇总）、dailysummary（每日汇总：步数/静息心率/压力等）、"
        "pai（PAI 活动指数）、workouts（运动记录：类型/距离/卡路里/步数/时长/平均心率）、"
        "workout_hr（运动过程逐点心率/步频，数据量大时写入临时文件并用 read_temp_file 读取）。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "enum": ["spo2", "stress", "hrv", "respiration", "sleep_sessions", "dailysummary", "pai", "workouts", "workout_hr"],
                    "description": "要查询的扩展指标",
                },
                "days": {"type": "integer", "description": "查询最近多少天的记录（默认 1）"},
            },
            "required": ["metric"],
        }
    )
    store: Any = None
    data_dir: Any = None

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        device_ids = _scope(self.store, _current_umo(context))
        if not device_ids:
            return _BIND_GUIDE
        metric = str(kwargs.get("metric") or "").strip()
        if metric not in _EXTENDED_FORMATTERS:
            return (
                "不支持的指标，可选：spo2 / stress / hrv / respiration / sleep_sessions / "
                "dailysummary / pai / workouts / workout_hr。"
            )
        try:
            days = max(1, min(int(kwargs.get("days") or 1), 30))
        except (TypeError, ValueError):
            days = 1
        since = int(datetime.now().timestamp()) - days * 24 * 3600
        rows = self.store.extended_range(metric, device_ids=device_ids, since=since)
        if not rows:
            return f"最近 {days} 天没有{_EXTENDED_FORMATTERS[metric][0]}数据。"
        if metric == "workouts":
            lines = [_fmt_extended_row(metric, r) for r in rows[-5:]]
            return f"最近 {days} 天共 {len(rows)} 条运动记录，最近几条：\n" + "\n".join(lines)
        if metric == "workout_hr":
            if len(rows) <= _HR_INLINE_LIMIT:
                inline = json.dumps(
                    [
                        {"t": r["timestamp"], "hr": r.get("heart_rate"), "sr": r.get("step_rate")}
                        for r in rows
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                return f"最近 {days} 天运动过程心率（{len(rows)} 点）：{inline}"
            if self.data_dir is None:
                return f"共 {len(rows)} 个运动过程数据点，数据量较大，无法内联返回。"
            tag = temp_tag(_current_umo(context) or "")
            name = write_temp_series(
                Path(self.data_dir),
                [{"t": r["timestamp"], "hr": r.get("heart_rate"), "sr": r.get("step_rate")} for r in rows],
                tag=tag,
            )
            return (
                f"共 {len(rows)} 个运动过程数据点，已写入临时文件 {name}。"
                f"请调用 read_temp_file 工具读取该文件（path 参数传 {name}），读取后文件会自动删除。"
            )
        # 展示最近若干条 + 汇总统计
        lines = [_fmt_extended_row(metric, r) for r in rows[-3:]]
        return f"最近 {days} 天共 {len(rows)} 条，最新：\n" + "\n".join(lines)
