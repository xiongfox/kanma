"""勘码 KanMa —— 企业级代码 Review Agent。

行为定义以项目根 AGENTS.MD 为唯一事实源；本包是该人格的可执行实现：
Pydantic 定义契约，LangGraph 编排六步流程。
"""

from pathlib import Path

__version__ = "0.1.0"

#: 人格定义文件（唯一事实源）。改名只需改这一处常量。
PERSONA_FILE = "AGENTS.MD"

#: 历史文件名，仅用于兼容回退查找（已重命名为 AGENTS.MD）。
LEGACY_PERSONA_FILES = ("KANMA.md",)


def project_root() -> Path:
    """项目根目录（src/kanma/__init__.py 的上三级）。"""
    return Path(__file__).resolve().parents[2]


def persona_path() -> Path | None:
    """返回人格文件绝对路径；找不到返回 None（macOS 大小写不敏感，AGENTS.MD == AGENTS.md）。"""
    root = project_root()
    for name in (PERSONA_FILE, *LEGACY_PERSONA_FILES):
        p = root / name
        if p.exists():
            return p
    return None


from kanma.graph import build_graph, run_review

__all__ = [
    "build_graph",
    "run_review",
    "__version__",
    "PERSONA_FILE",
    "LEGACY_PERSONA_FILES",
    "project_root",
    "persona_path",
]
