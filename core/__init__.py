"""``astrbot_plugin_llm_model_guard`` 的核心实现。

模块划分（规划文档 §4.1，依赖方向严格单向）：

* :mod:`core.models`：纯数据模型与配置快照；
* :mod:`core.classifier`：错误分类；
* :mod:`core.routing`：umo → 配置文件 → 候选链；
* :mod:`core.registry`：provider 实例包装的挂载/卸载；
* :mod:`core.wrapper`：守护运行时（重试、降级、标记、统计）；
* :mod:`core.guardian`：标记状态机 + 测活 supervisor；
* :mod:`core.store`：``state.json`` 原子读写与迁移；
* :mod:`core.metrics`：调用统计聚合；
* :mod:`core.commands`：管理员命令的文本渲染与解析；
* :mod:`core.web_api`：Web API 视图；
* :mod:`core.schema_sync`：把框架配置文件动态注入 schema 的 options/labels。

除 ``main.py`` 外，任何模块都不得直接访问 ``context``。
"""

from __future__ import annotations

__all__ = [
    "classifier",
    "commands",
    "guardian",
    "metrics",
    "models",
    "registry",
    "routing",
    "schema_sync",
    "store",
    "web_api",
    "wrapper",
]
