# astrbot_plugin_health_monitor

> [!IMPORTANT]
> 本项目代码完全由 AI 生成，仅经过基本人工验证

> [!NOTE]
> 本插件需要配套的**改装版 Gadgetbridge 手机端**（带 Webhook 上传模块）才能工作，手机端操作见下文「配套软件」章节。

健康数据监控插件：接收改装版 [Gadgetbridge](https://gadgetbridge.org) 手机端通过 HTTP 上传的健康数据（步数 / 心率 / 睡眠 / 电量等），存入 SQLite；注册 LLM 工具支持自然语言查询，并在心率过高、电量过低时推送主动告警。

**安全模型（无令牌）**：不需要 API Key / 令牌。插件自带独立上传服务（默认 `127.0.0.1:8765`），**绑定码即门禁** —— 设备未被任何会话绑定（配对）前，上传的数据一律不落库，返回 `pending_bind`，手机端进入"等待配对"；只有对机器人发送 `/bind <绑定码>` 完成配对后才开始接收数据。

**多用户**：数据按"会话 ↔ 设备绑定"隔离 —— 一个设备可被多个会话绑定（如家庭群 + 私聊），一个会话可绑定多个设备。

## 功能

- **独立上传端点**：`POST http://127.0.0.1:8765/upload`（经 Cloudflare Tunnel 暴露公网，见下）
  - 无需任何令牌；未配对设备数据不落库（`{"status":"pending_bind"}`）
- **设备绑定（多用户）**：绑定码在手机 Gadgetbridge「设置 → 自动化 → Webhook 上传」页面显示（形如 `GB-XXXXXX`），随每次上传上报；聊天里发 `/bind GB-XXXXXX` 即可绑定
- **数据存储**：`<AstrBot 数据目录>/astrbot_plugin_health_monitor/health.db`（SQLite）
  - `devices` / `samples`（按 设备+时间戳 upsert 去重）/ `alerts`
  - `bindings`（会话↔设备多对多）/ `pending_binds`（待绑定）/ `extended`（扩展指标）
- **LLM 工具**（自然语言直接问，仅返回当前会话已绑定设备的数据）：
  - `health_latest` → “现在心率多少”“电量多少”
  - `health_steps` → “今天走了多少步”
  - `health_sleep` → “昨晚睡眠怎么样”（前一晚 20:00 → 当天 12:00，含深睡/浅睡/REM/清醒分钟数）
  - `health_alerts` → “最近有没有告警”
  - `health_extended` → 扩展指标：血氧 SpO2 / 压力 / HRV(RR 间期) / 睡眠呼吸率 / 睡眠时段 / 每日汇总 / PAI / 运动记录
- **主动告警**：心率 ≥ 阈值（默认 120）或电量 ≤ 阈值（默认 15%）时，推送到该设备**所有绑定会话**；设备无绑定会话时才使用配置的兜底目标；30 分钟去重
- **数据类别可开关**：手机端设置页可勾选要上传的数据类别，未勾选的不上传

## Cloudflare Tunnel 暴露（推荐，无需开放入站端口）

服务器不开公网入站端口，用 cloudflared 出站隧道转发：

```yaml
# cloudflared config.yml
tunnel: <你的 tunnel id>
credentials-file: /path/to/credentials.json
ingress:
  - hostname: astrbot.example.com      # AstrBot 管理面板
    service: http://localhost:6185
  - hostname: health.example.com       # 健康数据上传
    service: http://localhost:8765
  - service: http_status:404
```

手机端「服务器地址」填 `health.example.com`（协议和 `/upload` 路径自动补全）。
注意：**不要在 Cloudflare 开启 Under Attack Mode / Bot Fight Mode**，会拦截 App 的非浏览器请求。

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
| `/unbind <设备名/地址/绑定码>` | `解绑设备` | 解除当前会话与该设备的绑定（支持绑定码，历史数据保留，重新绑定同码即可恢复可见） |
| `/devices` | `我的设备` | 列出当前会话已绑定的设备（电量 / 最后上报时间） |

绑定码在手机 Gadgetbridge 的「设置 → 自动化 → Webhook 上传」页面查看；绑定码等同设备的访问凭证，请只分享给可信的人。

## 安装

1. 方式一（推荐）：AstrBot 管理面板 → 插件市场 → 输入仓库 `https://github.com/Galaxy1108/astrbot_plugin_health_monitor` 安装。
2. 方式二：将本目录放到 AstrBot 的 `addons` 目录后重启 AstrBot。

要求 AstrBot ≥ 4.16（需支持 `register_web_api` 插件 Web API）。

## 配置

| 配置项 | 说明 | 默认 |
|---|---|---|
| `server_host` | 独立上传服务监听地址 | `127.0.0.1` |
| `server_port` | 独立上传服务端口 | 8765 |
| `hr_high_threshold` | 心率过高告警阈值（次/分） | 120 |
| `battery_low_threshold` | 电量过低告警阈值（%） | 15 |
| `alert_target_umo` | 兜底告警目标；设备有绑定会话时优先推给绑定会话 | 空 |

> 无需任何令牌/API Key。若要让局域网或公网直连 8765 端口，把 `server_host` 改为 `0.0.0.0`（自行确保网络安全）。

## 验证

安装并配置后，用 curl 模拟一次上传（在服务器本机即可）：

```bash
# 1) 连通性测试
curl -i "http://127.0.0.1:8765/ping"

# 2) 上传一条测试数据（未绑定会返回 pending_bind，符合预期）
curl -X POST "http://127.0.0.1:8765/upload" \
  -H "Content-Type: application/json" \
  -d '{
    "device": {"address": "AA:BB:CC:DD:EE:FF", "name": "测试手表", "type": "XIAOMI", "battery": 88, "binding_code": "ABC123"},
    "samples": [
      {"ts": 1718000000, "kind": "ACTIVITY", "steps": 12, "hr": 76, "intensity": 5.0},
      {"ts": 1718000060, "kind": "DEEP_SLEEP", "steps": 0, "hr": 58, "intensity": 1.0}
    ]
  }'
```

流程验证：

1. 首次上传应返回 `{"status": "pending_bind", ...}`（设备未配对，数据不落库）；
2. 对机器人发送 `/bind ABC123`（或用手机设置页显示的完整码 `GB-ABC123`）；
3. 再次 curl → 返回 `{"status": "ok", "received": 2}`，数据入库；
4. 对机器人说“现在心率多少”“昨天睡眠怎么样”验证查询。

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
- 响应：
  - `{"status": "ok", "received": N}` —— 数据已入库
  - `{"status": "pending_bind", "message": "..."}` —— 设备未配对，数据未落库（手机端进入"等待配对"）
  - `{"status": "error", "message": "..."}` —— 请求错误
- 手机端仅在收到 `ok` 后才推进游标，失败/待配对不推进（配对完成后自动恢复上传）

## 开发

```bash
python3 -m venv .venv
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests/
```

核心逻辑在 `health_store.py`（纯 SQLite，可独立测试）；`main.py` 只做 AstrBot 接线。

## 配套软件：改装版 Gadgetbridge（手机端）

本插件需要配套的**改装版 Gadgetbridge**（带 Webhook 上传模块）把健康数据传上来。手机端与服务器端是两个独立的仓库：

- 手机端（本插件配套）：https://github.com/Galaxy1108/gadgetbridge-webhook （`webhook-module` 分支）
- 服务器端（本仓库）：即当前插件

### 1. 安装 APK

从 `gadgetbridge-webhook` 仓库的 **Releases** 下载最新 APK（文件名形如
`gadgetbridge-webhook-mainlineDebug-<commit>.apk`，带 commit 号用于区分版本），
覆盖安装即可（版本号保持 `0.93.0`，升级不会清除数据）。

> 若提示"已存在更高版本"，说明手机上是更新构建的包，无需降级。

### 2. 配置上传（设置 → 自动化 → Webhook 上传）

1. 打开 **Webhook 上传** 开关；
2. **服务器地址**：只填根地址，例如 `health.example.com`（不要加 `https://` 和路径，
   插件自动补全为 `https://health.example.com/upload`；自建 http 需另开"允许不安全连接"）；
3. **复制绑定码**：点"绑定码"一行会复制完整命令 `/bind GB-XXXXXX`，
   把它发给机器人完成配对（配对前数据不落库）；
4. **数据类型**：默认全选即可（步数 / 心率 / 睡眠 / 运动记录 / 血氧 / 压力等）；
5. **立即上传**：点击测试；积压超过 7 天时会询问上传范围（全部 / 仅最近 7 天）；
   **重置上传游标** 可强制从更早的数据重新上传（如换服务器后回补历史）。

### 3. 验证

- 服务器端：`/devices` 查看已绑定设备与最后上报时间；
- 对机器人说"现在心率多少""今天走了多少步""昨晚睡眠怎么样""最近运动记录"
  验证自然语言查询；心率过高 / 电量过低会自动推送告警。

> 详细说明见手机端仓库的 `WEBHOOK_SETUP.md`。

## 相关项目

- 手机端（配套改装版 Gadgetbridge）：https://github.com/Galaxy1108/gadgetbridge-webhook
- 上游 Gadgetbridge：https://gadgetbridge.org
