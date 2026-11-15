# astrbot_plugin_txrui — 微信桌面端(txrui)接入

把 txrui_desktop 微信桌面端采集端接进 AstrBot：
AstrBot 侧作为 **WebSocket 服务端**，txrui_desktop 作为客户端接入，
复用其既有的 connector 协议（纯 JSON + base64，无核心模块依赖）。

```
微信PC窗口 ──截屏OCR──> txrui_desktop ──WS──> 本插件 ──> AstrBot Pipeline + LLM
     ^                                          │
     └────────── 剪贴板粘贴 <── outbound ──────┘
```

## 特性

- **零协议改动**：直接复用 txrui_desktop 的 connector 协议（`hello` / `event` /
  `media_upload` / `media_fetch` / `outbound` / `send_ack`），切换后端不需要改协议层。
- **媒体闭环**：出站图片/文件/视频由服务端落盘（sha256 内容寻址去重），
  客户端通过 `media_fetch` 取回本体再粘贴进微信。
- **如实降级**：客户端不支持真语音时降级为文件段并标 `extra.file_fallback`；
  拿不到媒体本体时降级为占位文本，**绝不引用不存在的 media_id**。
- **不引入 DB 迁移**：媒体走文件系统 + 进程内内存索引。

## 部署步骤

### 1. 在 AstrBot WebUI 添加平台

「平台」页 → 添加平台 → 类型选 **微信桌面端(txrui)**，填：

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `id` | 平台实例 ID，**不能含 `:` `!` 和空白** | `txrui_default` |
| `host` | 监听地址 | `0.0.0.0` |
| `port` | 监听端口 | `8788` |
| `token` | 连接令牌（**必填**，见下方安全提示） | 空 |
| `connector_id` | 连接器 ID，须与 txrui 侧 `CONNECTOR_ID` 一致 | `txrui_001` |
| `account_id` | 微信账号 ID，须与 txrui 侧 `WECHAT_ACCOUNT_ID` 一致 | `wx_001` |
| `media_dir` | 媒体落盘目录 | `data/txrui_media` |

开启该平台的 `enable` 开关。

### 2. 在 txrui_desktop 设置页填同样的令牌

打开 txrui_desktop 网页控制台 → **设置** 页：

- 勾选 **AstrBot 直连**
- WS 地址填 AstrBot 插件的监听地址（默认 `ws://127.0.0.1:8788`）
- Token 填与 AstrBot 插件 `token` **完全一致**的值
- `CONNECTOR_ID` / 微信账号与 AstrBot 插件的 `connector_id` / `account_id` 保持一致

点「保存设置」后点「测试验证」，应显示 `hello_ack NNNms`。

### 3. 启动 txrui 采集

点「启动」按钮。连接成功后 AstrBot 的「平台」页应显示 txrui 为运行中，
微信消息会进入 AstrBot 流水线。

### 命令行方式（headless）

```bat
cd txrui_desktop
copy config.toml.example config.toml
REM 编辑 config.toml 的 [astrbot] 段：
REM   enabled = "1"
REM   ws_url  = "ws://127.0.0.1:8788"
REM   token   = "<与 AstrBot 插件一致的令牌>"
python run.py --headless
```

## 安全提示

> **公网暴露必须配置 token。**
> 若把 `host` 设为 `0.0.0.0` 且 `token` 留空，任何能连上该端口的人
> 都可以注入伪造的微信消息。仅本机接入建议把 `host` 设为 `127.0.0.1`。

握手时 **connector_id 与 token 都要匹配**才放行，不匹配则直接关闭连接。

## 协议说明

### 上行（txrui -> AstrBot）

| 帧 | AstrBot 侧行为 |
|---|---|
| `hello` | 校验 `connector_id` + `token`，通过后回 `hello_ack` |
| `heartbeat` | 回 `heartbeat_ack` |
| `event` | 转成 `AstrBotMessage` 后交给流水线 |
| `media_upload` | 落盘去重后回 `media_ack`（含 `media_id`） |
| `media_fetch` | 回 `media_fetch_ack`（含 base64 本体） |
| `send_ack` | 按 `request_id` 唤醒对应的出站 Future |

### 下行（AstrBot -> txrui）

| 帧 | 说明 |
|---|---|
| `outbound` | `request_id` + `payload.content[]`，等待 `send_ack` |
| `send_ack` | 由客户端回，确认出站结果 |

### 段类型映射

| AstrBot 组件 | 出站段 |
|---|---|
| `Plain` | `{"type": "text", "text": "..."}` |
| `Image` | `{"type": "image", "media_id": ..., "mime": ..., "url": ...}` |
| `Video` | `{"type": "video", ...}` |
| `File` | `{"type": "file", ...}` |
| `Record` | `{"type": "file", ...}` + `extra.file_fallback=true`（客户端不支持真语音） |

### 媒体流向

出站媒体**不是**服务端向客户端上传（客户端不处理 `media_upload` 帧）：

```
AstrBot 本地文件
  -> 服务端 media_store 落盘（sha256 内容寻址，得到 media_id）
  -> outbound 段里带 media_id
  -> 客户端收到 outbound 后走 media_fetch 取回本体
  -> 客户端粘贴进微信
```

## 配置参考

`cmd_config.json` 的 `platform` 数组：

```json
{
  "id": "txrui_default",
  "type": "txrui",
  "enable": false,
  "host": "0.0.0.0",
  "port": 8788,
  "token": "",
  "connector_id": "txrui_001",
  "account_id": "wx_001",
  "media_dir": "data/txrui_media"
}
```

## 已知限制

- **媒体索引不跨进程**：媒体本体落盘在 `media_dir`，但进程内索引（`media_id` ->
  元信息）在重启后丢失。索引丢失时会按内容寻址路径自动回捞已落盘的 blob，
  但 `mime`/`filename` 会退化为默认值。
- **平台 ID 字符限制**：AstrBot 的 `PlatformManager` 会把 `id` 里的 `:` / `!`
  自动替换成 `_`，前端还额外禁止空白字符。建议只用字母/数字/下划线/短横线。
- **插件热重载**：`register_platform_adapter` 对重名注册会抛
  `ValueError: 平台适配器 txrui 已经注册过了`。AstrBot 的
  `unregister_platform_adapters_by_module` 会按模块路径自动注销，正常热重载没问题。
- **不匹配 `@filter.platform_adapter_type`**：该过滤器的 `ADAPTER_NAME_2_TYPE`
  是硬编码字典，自定义平台默认不匹配，本插件不依赖它。

## 测试

```bat
cd AstrBot
set ASTRBOT_DISABLE_METRICS=1
python -m pytest data/plugins/astrbot_plugin_txrui/tests/ -q
```

> `ASTRBOT_DISABLE_METRICS=1` 用来关掉指标上报——`AstrMessageEvent.send()`
> 会向外部服务 POST，离线环境下会导致 `asyncio.run()` 退出时卡住。
