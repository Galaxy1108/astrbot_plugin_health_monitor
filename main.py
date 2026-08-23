"""健康数据监控插件：接收 Gadgetbridge Webhook 上传的健康数据并入库。

功能：
1. Web API `POST /{PLUGIN_NAME}/upload`：接收手机端上传的分钟级样本（token 校验）。
2. LLM 工具：health_latest / health_steps / health_sleep / health_alerts，支持
   “现在心率多少”“今天走了多少步”“昨晚睡眠怎么样”等自然语言查询。
3. 异常告警：心率过高 / 电量过低时，向配置的会话推送主动消息（可关闭）。

数据存储：AstrBot 数据目录下 health.db（SQLite），逻辑见 health_store.py。
"""

from __future__ import annotations

import asyncio
import hmac
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.config import AstrBotConfig
from astrbot.core.star.star_tools import StarTools

from .health_store import HealthStore
from .health_tools import (
    HealthAlertsTool,
    HealthLatestTool,
    HealthSleepTool,
    HealthStepsTool,
)

PLUGIN_NAME = "astrbot_plugin_health_monitor"


class HealthMonitorPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config: AstrBotConfig = config if config is not None else AstrBotConfig({})

        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        data_dir.mkdir(parents=True, exist_ok=True)
        self.store = HealthStore(data_dir / "health.db")

        # 可选二次校验 token（在 AstrBot API Key 之外再加一层）
        self.token = str(self.config.get("token") or "").strip()
        self.hr_threshold = self._as_int(self.config.get("hr_high_threshold"), 120)
        self.battery_threshold = self._as_int(self.config.get("battery_low_threshold"), 15)
        self.alert_target = str(self.config.get("alert_target_umo") or "").strip()

        self.store.hr_threshold = self.hr_threshold
        self.store.battery_threshold = self.battery_threshold

        context.register_web_api(
            f"/{PLUGIN_NAME}/upload",
            self._api_upload,
            ["POST"],
            "接收健康数据上传",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/ping",
            self._api_ping,
            ["GET"],
            "健康检查（鉴权测试）",
        )

        context.add_llm_tools(
            HealthLatestTool(store=self.store),
            HealthStepsTool(store=self.store),
            HealthSleepTool(store=self.store),
            HealthAlertsTool(store=self.store),
        )

        logger.info(f"{PLUGIN_NAME} 已加载：数据目录 {data_dir}")

    # ------------------------------------------------------------- Web API

    async def _api_ping(self) -> Any:
        return json_response({"status": "ok", "plugin": PLUGIN_NAME})

    async def _api_upload(self) -> Any:
        if self.token:
            header_token = request.headers.get("x-health-token") or ""
            if not hmac.compare_digest(header_token, self.token):
                return error_response("invalid token", status_code=403)

        body = await request.json(default=None)
        if not isinstance(body, dict):
            return error_response("请求体必须是 JSON 对象")

        device = body.get("device")
        samples = body.get("samples")
        if not isinstance(device, dict) or not isinstance(samples, list):
            return error_response("缺少 device 或 samples 字段")

        try:
            received, alert = await asyncio.to_thread(self.store.ingest, device, samples)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{PLUGIN_NAME} 入库失败: {exc}", exc_info=True)
            return error_response("存储失败", status_code=500)

        if alert:
            logger.info(f"{PLUGIN_NAME} 触发告警: {alert['text']}")
            asyncio.create_task(self._push_alert(alert["text"]))

        return json_response({"status": "ok", "received": received})

    async def _push_alert(self, text: str) -> None:
        if not self.alert_target:
            return
        try:
            await self.context.send_message(self.alert_target, MessageChain(chain=[Plain(text)]))
            logger.info(f"{PLUGIN_NAME} 告警已推送至 {self.alert_target}")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{PLUGIN_NAME} 告警推送失败: {exc}")

    # ---------------------------------------------------------------- 工具

    @staticmethod
    def _as_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
