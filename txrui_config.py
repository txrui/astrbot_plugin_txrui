"""txrui 平台适配器的配置元数据（WebUI「平台」页表单 schema）。

字段与 AstrBot `config_metadata` 约定一致：
- description：字段显示名
- type：string / int / float / bool / text / list
- hint：字段说明
- secret：True 表示敏感字段（WebUI 侧按密码框渲染，可点击眼睛切换明文）
- show_key：True 表示在配置里显式展示键名
- slider：数值字段的滑杆范围 {min, max, step}
- collapsed：True 表示该字段折叠到「更多配置」下（进阶选项）

**配置入口**：所有可调项都在 WebUI「平台」页 → 编辑 txrui 机器人实例的
弹窗里填写，保存在 `data/cmd_config.json` 的 `platform` 数组中。
保存后 AstrBot 会调用 `platform_manager.reload()` 重建平台实例，配置立即生效。

**分组约定**：AstrBot 的平台配置表单是**扁平**的，没有嵌套分组控件。
这里用 `collapsed` 标记来划分「常用 / 进阶」两档，WebUI 会自动把进阶项
收进可展开的「更多配置」区域，效果等价于分组。
"""

from __future__ import annotations

#: 字段默认值（同时用于生成 `default_config_tmpl` 与适配器兜底）
TXRUI_CONFIG_DEFAULTS: dict[str, object] = {
    "id": "txrui_default",
    "logo_token": "",
    "display_name": "",
    "host": "0.0.0.0",
    "port": 8788,
    "token": "c3gNfV5sCCR6z4wh1",
    "connector_id": "txrui_001",
    "account_id": "wx_001",
    "media_dir": "data/txrui_media",
    "max_media_mb": 20,
    "max_frame_mb": 64,
    "send_ack_timeout": 30.0,
    "heartbeat_interval": 0,
    "handshake_timeout": 15,
    "enable_media": True,
    "media_fallback_to_text": True,
    "record_as_file": True,
    "strip_at_in_text": False,
    "max_text_length": 0,
    "blocked_sender_ids": [],
    "blocked_chat_ids": [],
    "debug_log": False,
    "log_payload": False,
}

#: 所有可配置字段的键名（顺序与 METADATA 一致）
TXRUI_CONFIG_KEYS: tuple[str, ...] = tuple(TXRUI_CONFIG_DEFAULTS.keys())

#: 连接设置（常用，必须与 txrui_desktop 侧一致）
TXRUI_CONFIG_METADATA: dict[str, dict] = {
    # 宿主注入的图标令牌，不是用户配置；放在默认键集合中保证模板一致。
    "id": {
        "description": "机器人 ID",
        "type": "string",
        "hint": "机器人的唯一标识，用于关联连接和会话。",
    },
    "logo_token": {
        "description": "平台图标令牌",
        "type": "string",
        "invisible": True,
    },
    "display_name": {
        "description": "机器人名称",
        "type": "string",
        "hint": "自定义此机器人的显示名称，留空时显示机器人 ID。修改名称不会影响连接和已有会话。",
    },
    "host": {
        "description": "监听地址",
        "type": "string",
        "hint": "WebSocket 服务端监听地址。仅本机接入建议用 127.0.0.1；"
        "需要局域网内 txrui_desktop 连接才用 0.0.0.0。",
        "show_key": True,
    },
    "port": {
        "description": "监听端口",
        "type": "int",
        "hint": "WebSocket 服务端监听端口，默认 8788。",
        "show_key": True,
    },
    "token": {
        "description": "连接令牌",
        "type": "string",
        "hint": "与 txrui_desktop 侧 ASTRBOT_TOKEN 一致。留空则不校验令牌，"
        "此时监听 0.0.0.0 会有安全风险（任何人都能注入微信消息）。",
        "secret": True,
        "show_key": True,
    },
    "connector_id": {
        "description": "连接器 ID",
        "type": "string",
        "hint": "与 txrui_desktop 侧 CONNECTOR_ID 一致，默认 txrui_001。",
        "show_key": True,
    },
    "account_id": {
        "description": "微信账号 ID",
        "type": "string",
        "hint": "与 txrui_desktop 侧 WECHAT_ACCOUNT_ID 一致，默认 wx_001。",
        "show_key": True,
    },
    # ---- 媒体 ----
    "media_dir": {
        "description": "媒体存储目录",
        "type": "string",
        "hint": "媒体本体落盘目录（相对 AstrBot 工作目录）。"
        "留空则使用默认目录 data/txrui_media。",
        "show_key": True,
    },
    "max_media_mb": {
        "description": "单文件大小上限（MB）",
        "type": "int",
        "hint": "单个媒体文件的最大字节数（MB）。超过此值的上传会被拒绝，"
        "与 txrui_desktop 侧 MAX_MEDIA_BYTES 对齐。",
        "slider": {"min": 1, "max": 100, "step": 1},
        "show_key": True,
    },
    "max_frame_mb": {
        "description": "单帧大小上限（MB）",
        "type": "int",
        "hint": "单个 WebSocket 帧的最大字节数（MB）。需 ≥ 单文件上限，"
        "否则大文件会在帧层就被掐断。",
        "slider": {"min": 1, "max": 256, "step": 1},
        "show_key": True,
    },
    # ---- 行为 ----
    "send_ack_timeout": {
        "description": "发送确认超时（秒）",
        "type": "float",
        "hint": "下发 outbound 后等待 txrui_desktop 回 send_ack 的超时时间（秒）。"
        "超时按「结果未知」处理，不谎报成功。",
        "slider": {"min": 5, "max": 120, "step": 1},
        "show_key": True,
    },
    "heartbeat_interval": {
        "description": "心跳应答间隔（秒）",
        "type": "int",
        "hint": "客户端发来 heartbeat 时本端的应答间隔（秒）。0 表示每次都立即应答，"
        "不额外节流。",
        "slider": {"min": 0, "max": 60, "step": 1},
        "show_key": True,
    },
    "handshake_timeout": {
        "description": "握手超时（秒）",
        "type": "int",
        "hint": "等待客户端发送首帧 hello 的超时时间（秒）。超时即断开该连接。",
        "slider": {"min": 5, "max": 120, "step": 5},
        "show_key": True,
    },
    # ---- 消息处理（进阶）----
    "enable_media": {
        "description": "启用媒体收发",
        "type": "bool",
        "hint": "关闭后忽略所有媒体上传（media_upload）与媒体出站，"
        "仅处理纯文本消息。",
        "collapsed": True,
        "show_key": True,
    },
    "media_fallback_to_text": {
        "description": "媒体不可用时降级为文本",
        "type": "bool",
        "hint": "开启后，当图片/文件本体取不到时，回复内容降级为占位文本"
        "（如「[图片]」），而不是静默丢弃该段。",
        "collapsed": True,
        "show_key": True,
    },
    "record_as_file": {
        "description": "语音按文件发送",
        "type": "bool",
        "hint": "微信视觉端没有真语音通道，语音一律降级为文件段。"
        "开启后会显式标记 file_fallback；关闭则语音段直接跳过。",
        "collapsed": True,
        "show_key": True,
    },
    "strip_at_in_text": {
        "description": "移除文本中的 @ 提及",
        "type": "bool",
        "hint": "上游消息里的 @某人 文本会原样进入流水线。开启后剥离 @ 前缀，"
        "只保留 @ 之后的正文。",
        "collapsed": True,
        "show_key": True,
    },
    "max_text_length": {
        "description": "单条文本最大长度",
        "type": "int",
        "hint": "入站文本超过该长度会被截断（0 表示不限制）。"
        "用于挡住异常长的 OCR 文本拖垮流水线。",
        "slider": {"min": 0, "max": 20000, "step": 500},
        "collapsed": True,
        "show_key": True,
    },
    "blocked_sender_ids": {
        "description": "屏蔽的发送者 ID",
        "type": "list",
        "hint": "逗号分隔的 platform_user_id。命中这些 ID 的消息直接丢弃，"
        "不入队、不进入流水线。",
        "collapsed": True,
        "show_key": True,
    },
    "blocked_chat_ids": {
        "description": "屏蔽的会话 ID",
        "type": "list",
        "hint": "逗号分隔的 chat_id（如公众号名、群名）。命中直接丢弃。",
        "collapsed": True,
        "show_key": True,
    },
    # ---- 调试（进阶）----
    "debug_log": {
        "description": "输出调试日志",
        "type": "bool",
        "hint": "打印每条入站/出站消息的完整内容，排障用。生产环境建议关闭。",
        "collapsed": True,
        "show_key": True,
    },
    "log_payload": {
        "description": "记录原始载荷",
        "type": "bool",
        "hint": "在日志里保留事件帧的原始 JSON。含消息内容，"
        "仅在排障时开启。",
        "collapsed": True,
        "show_key": True,
    },
}


def build_default_config_tmpl() -> dict:
    """生成平台实例的默认配置模板。

    模板里的字段与 `TXRUI_CONFIG_METADATA` 一一对应，WebUI「平台」页新增
    txrui 实例时会用它预填表单。`id` / `enable` / `type` 由 AstrBot 的
    `register_platform_adapter` 自动补齐，这里不重复设置。
    """
    return {key: TXRUI_CONFIG_DEFAULTS[key] for key in TXRUI_CONFIG_KEYS}


__all__ = [
    "TXRUI_CONFIG_DEFAULTS",
    "TXRUI_CONFIG_KEYS",
    "TXRUI_CONFIG_METADATA",
    "build_default_config_tmpl",
]
