# astrbot_plugin_health_monitor

健康数据监控插件：接收改装版 [Gadgetbridge](https://gadgetbridge.org) 手机端通过 HTTP 上传的健康数据（步数 / 心率 / 睡眠 / 电量），存入 SQLite；注册 LLM 工具支持自然语言查询，并在心率过高、电量过低时推送主动告警。

**多用户设计**：数据按"会话 ↔ 设备绑定"隔离 —— 用户先用绑定码把设备绑到自己的会话，之后所有查询与告警都只针对该会话绑定的设备；一个设备可被多个会话绑定（如家庭群 + 私聊），一个会话可绑定多个设备。

## 功能

- **Web API 接收端点**：`POST /api/v1/plugins/extensions/astrbot_plugin_health_monitor/upload`
  - AstrBot 层鉴权：需要带 `plugin` 作用域的 [API Key](https://github.com/AstrBotDevs/AstrBot)（`Authorization: Bearer <key>`）
  - 插件层二次校验：配置 `token` 后，请求必须携带 `X-Health-Token` 头
- **设备绑定（多用户）**：绑定码在手机 Gadgetbridge「设置 → 自动化 → Webhook 上传」页面显示（形如 `GB-XXXXXX`），随每次上传上报；聊天里发 `/bind GB-XXXXXX` 即可绑定
- **数据存储**：`<AstrBot 数据目录>/astrbot_plugin_health_monitor/health.db`（SQLite）
  - `devices` / `samples`（按 设备+时间戳 upsert 去重）/ `alerts`
  - `bindings`（会话↔设备多对多）/ `pending_binds`（待绑定）
- **LLM 工具**（自然语言直接问，仅返回当前会话已绑定设备的数据）：
  - `health_latest` → “现在心率多少”“电量多少”
  - `health_steps` → “今天走了多少步”
  - `health_sleep` → “昨晚睡眠怎么样”（前一晚 20:00 → 当天 12:00，含深睡/浅睡/REM/清醒分钟数）
  - `health_alerts` → “最近有没有告警”
  - `health_extended` → 扩展指标：血氧 SpO2 / 压力 / HRV(RR 间期) / 睡眠呼吸率 / 睡眠时段 / 每日汇总 / PAI / 运动记录
- **主动告警**：心率 ≥ 阈值（默认 120）或电量 ≤ 阈值（默认 15%）时，推送到该设备**所有绑定会话**；设备无绑定会话时才使用配置的兜底目标；30 分钟去重
- **数据类别可开关**：手机端设置页可勾选要上传的数据类别（分钟样本 / 距离与卡路里 / 电量 / 血氧 / 压力 / HRV / 呼吸率 / 睡眠时段 / 每日汇总 / PAI / 运动记录），未勾选的不上传

## 支持的数据范围

| 类别 | 手机端来源 | 说明 |
|---|---|---|
| 分钟样本 | GB 统一 SampleProvider | 每分钟一条：步数 / 心率 / 活动类型（含深睡/浅睡/REM/清醒分期与跑步/游泳/骑行等 20+ 运动类型）/ 强度 |
| 距离与卡路里 | 同上（附加字段） | distance_cm / calories |
| 设备电量 | GBDevice | 每次上传附带 |
| 血氧 SpO2 | Huami/Cmf/Colmi/HybridHR/Moyoung/Garmin 表 | 分钟级 |
| 压力 | Huami/Cmf/Colmi/Moyoung/Garmin/Wena3 表 | 分钟级 |
| HRV | Xiaomi RR 间期 / Colmi / Garmin | 原始 RR 间期（每跳一条，限最新 2000 条/次上传） |
| 睡眠呼吸率 | Huami / Garmin | 夜间每分钟 |
| 睡眠时段 | Huami/Xiaomi/Cmf/Colmi/Lefun 睡眠时段表 | 入睡/醒来、各分期时长 |
| 每日汇总 | Xiaomi 每日汇总 / 手动测量 | 步数、静息/最高/平均心率、压力均值、手动测量值 |
| PAI | Huami | paiToday / paiTotal 等 |
| 运动记录 | BaseActivitySummary（统一表） | 运动名称/类型/起止时间/轨迹摘要 |

## 聊天指令

| 指令 | 别名 | 说明 |
|---|---|---|
| `/bind <绑定码>` | `绑定设备` | 把当前会话绑定到该码对应的设备。设备已上报过 → 立即成功；未上报过 → 等待设备下次上报后自动绑定并通知你 |
| `/unbind <设备名或地址>` | `解绑设备` | 解除当前会话与该设备的绑定（历史数据保留，重新绑定同码即可恢复可见） |
| `/devices` | `我的设备` | 列出当前会话已绑定的设备（电量 / 最后上报时间） |

绑定码在手机 Gadgetbridge 的「设置 → 自动化 → Webhook 上传」页面查看；绑定码等同设备的访问凭证，请只分享给可信的人。

## 安装

1. 方式一（推荐）：AstrBot 管理面板 → 插件市场 → 输入仓库 `https://github.com/Galaxy1108/astrbot_plugin_health_monitor` 安装。
2. 方式二：将本目录放到 AstrBot 的 `addons` 目录后重启 AstrBot。

要求 AstrBot ≥ 4.16（需支持 `register_web_api` 插件 Web API）。

## 配置

| 配置项 | 说明 | 默认 |
|---|---|---|
| `token` | 上传令牌（`X-Health-Token`）；留空关闭二次校验 | 空 |
| `hr_high_threshold` | 心率过高告警阈值（次/分） | 120 |
| `battery_low_threshold` | 电量过低告警阈值（%） | 15 |
| `alert_target_umo` | 兜底告警目标；设备有绑定会话时优先推给绑定会话 | 空 |

## 验证

安装并配置后，用 curl 模拟一次上传：

```bash
# 1) 鉴权连通性测试（GET）
curl -i "https://<你的服务器>/api/v1/plugins/extensions/astrbot_plugin_health_monitor/ping" \
  -H "Authorization: Bearer <AstrBot API Key>"

# 2) 上传一条测试数据（binding_code 需与手机端设置页显示的绑定码一致）
curl -X POST "https://<你的服务器>/api/v1/plugins/extensions/astrbot_plugin_health_monitor/upload" \
  -H "Authorization: Bearer <AstrBot API Key>" \
  -H "X-Health-Token: <插件 token，若已配置>" \
  -H "Content-Type: application/json" \
  -d '{
    "device": {"address": "AA:BB:CC:DD:EE:FF", "name": "测试手表", "type": "XIAOMI", "battery": 88, "binding_code": "ABC123"},
    "samples": [
      {"ts": 1718000000, "kind": "ACTIVITY", "steps": 12, "hr": 76, "intensity": 5.0},
      {"ts": 1718000060, "kind": "DEEP_SLEEP", "steps": 0, "hr": 58, "intensity": 1.0}
    ]
  }'
```

期望返回 `{"status": "ok", "received": 2}`。然后：

1. 对机器人发送 `/bind ABC123`（或用手机设置页显示的完整码 `GB-ABC123`）；
2. 等下一次上传（或重新 curl 一次）→ 收到"设备绑定成功"通知；
3. 对机器人说“现在心率多少”“昨天睡眠怎么样”验证查询。

## 上传数据契约（与手机端约定）

```json
{
  "device": {"address": "MAC", "name": "设备名", "type": "XIAOMI", "battery": 88, "binding_code": "ABC123"},
  "since": 1717999999,
  "samples": [
    {"ts": 1718000000, "kind": "ACTIVITY", "steps": 12, "hr": 76, "intensity": 5.0}
  ],
  "extended": {
    "spo2": [{"timestamp": 1718000000, "spo2": 97}],
    "workouts": [{"timestamp": 1718000000, "name": "晨跑", "activity_kind": 16}]
  }
}
```

- `kind` 为 Gadgetbridge `ActivityKind` 归一化枚举名：`ACTIVITY` / `LIGHT_SLEEP` / `DEEP_SLEEP` / `REM_SLEEP` / `AWAKE_SLEEP` / `NOT_MEASURED` 等
- `binding_code`：手机端设置页显示的绑定码（去掉 `GB-` 前缀与横线后比对，不区分大小写）
- `extended`（可选）：按类别分组的上传开关选中的数据；键为小写列名，`timestamp` 为 epoch 秒；服务端按 (设备, 类别, 时间戳, seq) upsert
- 响应：`{"status": "ok", "received": N}`；错误：`{"status": "error", "message": "..."}`
- 手机端仅在收到 `ok` 后才推进游标，失败自动重传

## 开发

```bash
python3 -m venv .venv
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests/
```

核心逻辑在 `health_store.py`（纯 SQLite，可独立测试）；`main.py` 只做 AstrBot 接线。

## 相关项目

- 手机端：https://github.com/Galaxy1108/gadgetbridge-webhook
