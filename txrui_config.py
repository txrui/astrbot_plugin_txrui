"""txrui 平台适配器的配置元数据（WebUI 表单 schema）。

字段与 AstrBot `config_metadata` 约定一致：
- description：字段显示名
- type：string / int / bool
- hint：字段说明
- secret：True 表示敏感字段（WebUI 侧按密码框渲染）
- show_key：True 表示在配置里显式展示键名
"""

TXRUI_CONFIG_METADATA = {
    "host": {
        "description": "监听地址",
        "type": "string",
        "hint": "WebSocket 服务端监听地址。仅本机接入建议用 127.0.0.1。",
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
        "hint": "与 txrui_desktop 侧配置的 token 一致。留空则不校验，"
        "此时禁止监听 0.0.0.0（否则任何人都能注入微信消息）。",
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
    "media_dir": {
        "description": "媒体存储目录",
        "type": "string",
        "hint": "媒体本体落盘目录（相对 AstrBot 工作目录）。",
        "show_key": True,
    },
}

__all__ = ["TXRUI_CONFIG_METADATA"]
