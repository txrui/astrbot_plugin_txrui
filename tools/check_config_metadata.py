"""校验 txrui 平台配置元数据的自洽性。

txrui 的配置全部走 WebUI「平台」页（`config_metadata` + `default_config_tmpl`），
没有独立的插件配置文件，因此这里不再生成 schema，而是校验：

1. `TXRUI_CONFIG_METADATA` 与 `TXRUI_CONFIG_DEFAULTS` 的键完全一致；
2. 每个字段的 `type` 是 WebUI 支持的类型；
3. `build_default_config_tmpl()` 产出的模板与默认值表一致；
4. 数值字段的 `slider` 范围合法，且 `default` 落在范围内。

用法：python tools/gen_conf_schema.py
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
METADATA_PATH = PLUGIN_ROOT / "txrui_config.py"

#: WebUI ConfigItemRenderer 支持的字段类型
SUPPORTED_TYPES = {
    "string",
    "int",
    "float",
    "bool",
    "text",
    "list",
    "dict",
    "file",
    "template_list",
}


def load_config_module():
    """从 txrui_config.py 读取配置元数据与默认值。"""
    spec = importlib.util.spec_from_file_location("txrui_config", METADATA_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {METADATA_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    module = load_config_module()
    metadata: dict[str, dict] = module.TXRUI_CONFIG_METADATA
    defaults: dict[str, object] = module.TXRUI_CONFIG_DEFAULTS

    errors: list[str] = []

    if set(metadata.keys()) != set(defaults.keys()):
        missing = set(defaults.keys()) - set(metadata.keys())
        extra = set(metadata.keys()) - set(defaults.keys())
        if missing:
            errors.append(f"metadata 缺少字段: {sorted(missing)}")
        if extra:
            errors.append(f"metadata 多出字段: {sorted(extra)}")

    for key, meta in metadata.items():
        if "description" not in meta:
            errors.append(f"{key} 缺 description")
        if meta.get("type") not in SUPPORTED_TYPES:
            errors.append(f"{key} 的 type 非法: {meta.get('type')!r}")
        if key not in defaults:
            continue

        default = defaults[key]
        slider = meta.get("slider")
        if slider:
            lo, hi = slider.get("min"), slider.get("max")
            if lo is None or hi is None or lo > hi:
                errors.append(f"{key} 的 slider 范围非法: {slider!r}")
            elif isinstance(default, (int, float)) and not (lo <= default <= hi):
                errors.append(
                    f"{key} 的默认值 {default} 超出 slider 范围 [{lo}, {hi}]"
                )

    template = module.build_default_config_tmpl()
    if template != {key: defaults[key] for key in defaults}:
        errors.append("build_default_config_tmpl() 与默认值表不一致")

    if errors:
        for err in errors:
            print(f"[错误] {err}")
        raise SystemExit(1)

    print(f"txrui 平台配置元数据校验通过（{len(metadata)} 项）")


if __name__ == "__main__":
    main()
