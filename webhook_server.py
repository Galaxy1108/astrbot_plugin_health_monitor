"""独立上传 HTTP 服务（不经过 AstrBot 的鉴权路由，因此不需要 API Key/令牌）。

安全模型：绑定码即门禁 —— 设备尚未被任何会话绑定（配对）时，上传的数据一律
不落库，返回 {"status": "pending_bind"}，手机端进入"等待配对"状态；只有完成
配对（/bind）后数据才被接收。

使用标准库 http.server，无第三方依赖；通过 Cloudflare Tunnel 把公网流量转发
到本服务端口即可（无需开放服务器入站端口）。
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger("astrbot_plugin_health_monitor.webhook_server")


class _Handler(BaseHTTPRequestHandler):
    """处理 /upload 与 /ping。"""

    store: Any = None  # 由 server 注入

    # ------------------------------------------------------------------ 路由

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/ping":
            self._json({"status": "ok", "plugin": "astrbot_plugin_health_monitor"})
        else:
            self._json({"status": "error", "message": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/upload":
            self._json({"status": "error", "message": "not found"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 50 * 1024 * 1024:
                self._json({"status": "error", "message": "invalid body size"}, status=400)
                return
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json({"status": "error", "message": "invalid json"}, status=400)
            return

        device = body.get("device") if isinstance(body, dict) else None
        samples = body.get("samples") if isinstance(body, dict) else None
        extended = body.get("extended") if isinstance(body, dict) else None
        if not isinstance(device, dict) or not isinstance(samples, list):
            self._json({"status": "error", "message": "缺少 device 或 samples 字段"}, status=400)
            return

        try:
            received, alert, newly_bound_umos, pending_bind = self.store.ingest(device, samples, extended)
        except ValueError as exc:
            self._json({"status": "error", "message": str(exc)}, status=400)
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("ingest failed: %s", exc, exc_info=True)
            self._json({"status": "error", "message": "存储失败"}, status=500)
            return

        if pending_bind:
            code = str(device.get("binding_code") or "")
            self._json(
                {
                    "status": "pending_bind",
                    "message": "设备未绑定，等待配对：请对机器人发送 /bind GB-" + code,
                }
            )
            return

        # 刚完成绑定的会话 → 通知
        if newly_bound_umos:
            device_name = str(device.get("name") or device.get("address") or "设备")
            for umo in newly_bound_umos:
                text = (
                    f"✅ 设备绑定成功：{device_name}（{device.get('address')}）。"
                    "现在可以直接问我健康数据了，例如“现在心率多少”。"
                )
                threading.Thread(target=self._notify, args=(umo, text), daemon=True).start()

        if alert:
            threading.Thread(target=self._notify_alert, args=(alert,), daemon=True).start()

        self._json({"status": "ok", "received": received})

    # ---------------------------------------------------------------- 工具

    def _notify(self, umo: str, text: str) -> None:
        try:
            self.server.notify_coro(umo, text)
        except Exception as exc:  # noqa: BLE001
            logger.error("notify failed: %s", exc)

    def _notify_alert(self, alert: dict) -> None:
        try:
            self.server.notify_alert_coro(alert)
        except Exception as exc:  # noqa: BLE001
            logger.error("alert notify failed: %s", exc)

    def _json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug(fmt, *args)


class WebhookHttpServer:
    """线程化 HTTP 服务：start() 启动，stop() 关闭。"""

    def __init__(
        self,
        store: Any,
        host: str = "127.0.0.1",
        port: int = 8765,
        notify_coro=None,
        notify_alert_coro=None,
    ) -> None:
        self._store = store
        self._host = host
        self._port = port
        self._notify_coro = notify_coro or (lambda umo, text: None)
        self._notify_alert_coro = notify_alert_coro or (lambda alert: None)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return

        class Handler(_Handler):
            pass

        Handler.store = self._store

        server = ThreadingHTTPServer((self._host, self._port), Handler)
        server.notify_coro = self._notify_coro
        server.notify_alert_coro = self._notify_alert_coro
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        logger.info(f"webhook upload server listening on http://{self._host}:{self._port}")

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        logger.info("webhook upload server stopped")

    @property
    def port(self) -> int:
        return self._port
