"""LLM 工具：把健康数据查询暴露给 AstrBot 智能体。

多用户隔离：每个工具从调用上下文取出当前会话（unified_msg_origin），
只查询该会话已绑定设备的数据；未绑定任何设备时返回引导文案。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass

from .health_store import HealthStore, parse_date

#: 未绑定时给 LLM 的引导文案（让模型原样回复用户）
_BIND_GUIDE = "你还没有绑定任何设备。请让用户先发送：/bind <绑定码>，绑定码在手机 Gadgetbridge 的「设置 → 自动化 → Webhook 上传」页面查看（形如 GB-XXXXXX）。"


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
