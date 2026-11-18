"""测试夹具：把插件包导入路径与测试依赖准备好。

插件目录下没有 `__init__.py`（AstrBot 插件以目录为包），所以这里把插件目录
的父目录（即 `data/plugins`）塞进 sys.path，用 `astrbot_plugin_txrui.xxx` 导入。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 测试环境禁用指标上传：`AstrMessageEvent.send()` / `Platform.send_by_session()`
# 会 `asyncio.create_task(Metric.upload(...))`，那会 POST 到外网 TickStats，
# 在离线沙箱里会一直挂着，导致 `asyncio.run()` 退出时卡住。
os.environ.setdefault("ASTRBOT_DISABLE_METRICS", "1")

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGIN_PARENT = PLUGIN_DIR.parent

for _p in (str(PLUGIN_DIR), str(PLUGIN_PARENT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: AstrBot 仓库根（用于 import astrbot.core.*）。
#: 插件装在 <AstrBot>/data/plugins/<plugin> 下时，仓库根就是上溯三级；
#: 但开发时插件可能位于独立目录，这里按候选路径逐个探测。
_CANDIDATE_REPO_ROOTS = [
    *list(PLUGIN_DIR.parents)[2:4],  # 目录层级不足时也能安全收集候选
    PLUGIN_DIR.parent.parent,
    Path("/www/wwwroot/AstrBot-master"),
]
for _root in _CANDIDATE_REPO_ROOTS:
    if (_root / "astrbot" / "api").is_dir():
        REPO_ROOT = _root
        break
else:  # pragma: no cover - 找不到时退到第一个候选，交由 import 报错
    REPO_ROOT = _CANDIDATE_REPO_ROOTS[0]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
