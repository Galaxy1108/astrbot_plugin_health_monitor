"""健康数据监控插件：接收 Gadgetbridge Webhook 上传的健康数据并入库。

功能：
1. 独立上传端点：插件自带 HTTP 服务（默认 127.0.0.1:8765，经 Cloudflare Tunnel
   暴露公网），不需要 AstrBot API Key / 令牌 —— 安全模型是"绑定码即门禁"：
   设备未配对（绑定）前数据一律不落库，返回 pending_bind，手机端进入"等待配对"。
2. 设备绑定（多用户）：用户对机器人发送 /bind <绑定码> 把设备绑到当前会话；
   查询与告警都按"会话 → 其绑定的设备"隔离。
3. LLM 工具：health_latest / health_steps / health_sleep / health_alerts /
   health_extended，支持自然语言查询。
4. 异常告警：心率过高 / 电量过低时，推送到该设备全部绑定会话（可配置兜底目标）。

数据存储：AstrBot 数据目录下 health.db（SQLite），逻辑见 health_store.py。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.config import AstrBotConfig
from astrbot.core.star.star_tools import StarTools

from .health_store import HealthStore, normalize_binding_code
from .health_tools import (
    HealthAlertsTool,
    HealthExtendedTool,
    HealthHrHistoryTool,
    HealthLatestTool,
    HealthSleepTool,
    HealthStepsTool,
)
from .webhook_server import WebhookHttpServer

PLUGIN_NAME = "astrbot_plugin_health_monitor"


class HealthMonitorPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config: AstrBotConfig = config if config is not None else AstrBotConfig({})

        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        data_dir.mkdir(parents=True, exist_ok=True)
        self.store = HealthStore(data_dir / "health.db")

        self.hr_threshold = self._as_int(self.config.get("hr_high_threshold"), 120)
        self.battery_threshold = self._as_int(self.config.get("battery_low_threshold"), 15)
        # 兜底告警目标：设备没有任何绑定会话时使用
        self.alert_target = str(self.config.get("alert_target_umo") or "").strip()
        self.server_host = str(self.config.get("server_host") or "127.0.0.1").strip() or "127.0.0.1"
        self.server_port = self._as_int(self.config.get("server_port"), 8765)

        self.store.hr_threshold = self.hr_threshold
        self.store.battery_threshold = self.battery_threshold

        # AstrBot 路由（兼容保留；主通道是独立端口的 WebhookHttpServer）
        context.register_web_api(
            f"/{PLUGIN_NAME}/upload",
            self._api_upload,
            ["POST"],
            "接收健康数据上传（AstrBot 路由，兼容）",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/ping",
            self._api_ping,
            ["GET"],
            "健康检查",
        )

        context.add_llm_tools(
            HealthLatestTool(store=self.store),
            HealthHrHistoryTool(store=self.store),
            HealthStepsTool(store=self.store),
            HealthSleepTool(store=self.store),
            HealthAlertsTool(store=self.store),
            HealthExtendedTool(store=self.store),
        )

        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: WebhookHttpServer | None = None

        logger.info(f"{PLUGIN_NAME} 已加载：数据目录 {data_dir}")

    async def initialize(self) -> None:
        """启动独立上传服务（主事件循环就绪后）。"""
        self._loop = asyncio.get_running_loop()
        self._server = WebhookHttpServer(
            self.store,
            host=self.server_host,
            port=self.server_port,
            notify_coro=self._schedule_send,
            notify_alert_coro=self._schedule_alert,
        )
        self._server.start()

    async def terminate(self) -> None:
        if self._server is not None:
            self._server.stop()
            self._server = None

    # ------------------------------------------------------------- Web API

    async def _api_ping(self) -> Any:
        return json_response({"status": "ok", "plugin": PLUGIN_NAME})

    async def _api_upload(self) -> Any:
        """AstrBot 路由版上传（与独立端口同一套门禁逻辑）。"""
        body = await request.json(default=None)
        if not isinstance(body, dict):
            return error_response("请求体必须是 JSON 对象")

        device = body.get("device")
        samples = body.get("samples")
        extended = body.get("extended")
        if not isinstance(device, dict) or not isinstance(samples, list):
            return error_response("缺少 device 或 samples 字段")
        if extended is not None and not isinstance(extended, dict):
            return error_response("extended 必须是对象")

        try:
            received, alert, newly_bound_umos, pending_bind = await asyncio.to_thread(
                self.store.ingest, device, samples, extended
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{PLUGIN_NAME} 入库失败: {exc}", exc_info=True)
            return error_response("存储失败", status_code=500)

        if pending_bind:
            return json_response(
                {
                    "status": "pending_bind",
                    "message": f"设备未绑定，等待配对：请对机器人发送 /bind {device.get('binding_code')}",
                }
            )

        if newly_bound_umos:
            device_name = str(device.get("name") or device.get("address") or "设备")
            for umo in newly_bound_umos:
                asyncio.create_task(
                    self._send_to(
                        umo,
                        f"✅ 设备绑定成功：{device_name}（{device.get('address')}）。"
                        "现在可以直接问我健康数据了，例如“现在心率多少”。",
                    )
                )

        if alert:
            logger.info(f"{PLUGIN_NAME} 触发告警: {alert['text']}")
            asyncio.create_task(self._push_alert(alert))

        return json_response({"status": "ok", "received": received})

    # ------------------------------------------------------------ 绑定指令

    @filter.command("bind")
    async def bind_device(self, event: AstrMessageEvent):
        """绑定设备到当前会话。用法：/bind <绑定码>"""
        yield event.plain_result(await self._do_bind(event))

    @filter.command("绑定设备")
    async def bind_device_zh(self, event: AstrMessageEvent):
        """绑定设备（中文别名）。用法：绑定设备 <绑定码>"""
        yield event.plain_result(await self._do_bind(event))

    async def _do_bind(self, event: AstrMessageEvent) -> str:
        try:
            code = normalize_binding_code(self._command_arg(event.message_str))
            if not code:
                return "用法：/bind <绑定码>（绑定码在手机 Gadgetbridge 的「设置 → 自动化 → Webhook 上传」页面查看，形如 GB-XXXXXX）"
            umo = event.unified_msg_origin
            device = await asyncio.to_thread(self.store.bind_by_code, code, umo)
            if device:
                logger.info(f"device {device['address']} bound to session {umo} by /bind")
                return f"✅ 绑定成功：{device['name']}（{device['address']}）。现在可以问我“现在心率多少”“昨晚睡眠怎么样”了。"
            is_new = await asyncio.to_thread(self.store.register_pending_bind, code, umo)
            if is_new:
                return "📡 该设备还没有上报过这个绑定码。等手机端下一次上传后会自动完成绑定，到时我会通知你。"
            return "⏳ 你之前已经提交过这个绑定码，仍在等待设备上报。"
        except Exception as exc:  # noqa: BLE001
            logger.error(f"bind_device error: {exc}")
            return f"绑定失败：{exc}"

    @filter.command("unbind")
    async def unbind_device(self, event: AstrMessageEvent):
        """解除当前会话与设备的绑定。用法：/unbind <设备名或地址>"""
        yield event.plain_result(await self._do_unbind(event))

    @filter.command("解绑设备")
    async def unbind_device_zh(self, event: AstrMessageEvent):
        """解绑（中文别名）。用法：解绑设备 <设备名或地址>"""
        yield event.plain_result(await self._do_unbind(event))

    async def _do_unbind(self, event: AstrMessageEvent) -> str:
        try:
            identifier = self._command_arg(event.message_str)
            if not identifier:
                return "用法：/unbind <设备名或地址>（可用 /devices 查看）"
            umo = event.unified_msg_origin
            removed = await asyncio.to_thread(self.store.unbind, umo, identifier)
            if removed:
                logger.info(f"session {umo} unbound device {removed['address']}")
                return f"已解除绑定：{removed['name']}（{removed['address']}）。历史数据仍保留，重新绑定同码即可恢复可见。"
            return "未找到绑定关系。先用 /devices 查看你绑定的设备。"
        except Exception as exc:  # noqa: BLE001
            logger.error(f"unbind_device error: {exc}")
            return f"解绑失败：{exc}"

    @filter.command("devices")
    async def list_devices(self, event: AstrMessageEvent):
        """列出当前会话已绑定的设备。"""
        yield event.plain_result(await self._do_list_devices(event))

    @filter.command("我的设备")
    async def list_devices_zh(self, event: AstrMessageEvent):
        """我的设备（中文别名）。"""
        yield event.plain_result(await self._do_list_devices(event))

    async def _do_list_devices(self, event: AstrMessageEvent) -> str:
        try:
            umo = event.unified_msg_origin
            devices = await asyncio.to_thread(self.store.bound_devices, umo)
            if not devices:
                return "你还没有绑定任何设备。发送 /bind <绑定码> 完成绑定（绑定码在手机 Gadgetbridge 的「设置 → 自动化 → Webhook 上传」页面查看）。"
            lines = []
            for d in devices:
                battery = f"{d['battery']}%" if d["battery"] is not None else "未知"
                last = d["last_seen"] or 0
                last_str = datetime.fromtimestamp(last).strftime("%m-%d %H:%M") if last else "从未"
                lines.append(f"• {d['name'] or d['address']}（{d['address']}）电量 {battery}，最后上报 {last_str}")
            return "已绑定设备：\n" + "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"list_devices error: {exc}")
            return f"查询失败：{exc}"

    # ------------------------------------------------------- 通知/告警调度

    def _schedule_send(self, umo: str, text: str) -> None:
        """独立端口线程 → 主事件循环调度。"""
        try:
            if self._loop is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._send_to(umo, text)))
            else:
                logger.warning(f"{PLUGIN_NAME} 主循环未就绪，跳过推送: {text}")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{PLUGIN_NAME} 推送调度失败: {exc}")

    def _schedule_alert(self, alert: dict) -> None:
        try:
            if self._loop is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._push_alert(alert)))
            else:
                logger.warning(f"{PLUGIN_NAME} 主循环未就绪，告警仅入库: {alert['text']}")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{PLUGIN_NAME} 告警调度失败: {exc}")

    async def _push_alert(self, alert: dict) -> None:
        device_id = alert.get("device_id")
        targets: list[str] = []
        if device_id:
            targets = await asyncio.to_thread(self.store.umos_for_device, device_id)
        if not targets and self.alert_target:
            targets = [self.alert_target]
        if not targets:
            logger.info(f"{PLUGIN_NAME} 告警无推送目标（仅入库）: {alert['text']}")
            return
        for umo in targets:
            await self._send_to(umo, alert["text"])

    async def _send_to(self, umo: str, text: str) -> None:
        try:
            await self.context.send_message(umo, MessageChain(chain=[Plain(text)]))
            logger.info(f"{PLUGIN_NAME} 消息已推送至 {umo}")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{PLUGIN_NAME} 推送失败至 {umo}: {exc}")

    # ---------------------------------------------------------------- 工具

    @staticmethod
    def _command_arg(message_str: str) -> str:
        """去掉指令前缀，取剩余参数。"""
        text = (message_str or "").strip()
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    @staticmethod
    def _as_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
