"""勘码的 Pydantic 契约层。

所有跨节点流转的数据结构都定义在此，节点之间只通过契约通信，
避免用裸 dict 传递状态（LangGraph 状态里出现的 key 都能在这里找到定义）。
"""

from kanma.models.schemas import (
    CodeLanguage,
    DiffBundle,
    DiffHunk,
    FileDiff,
    ProjectConfig,
    RequirementDoc,
    ReviewReport,
    ReviewVerdict,
    Severity,
    StaticFinding,
    TestArtifact,
    TestCase,
    TestResult,
    TestStatus,
)

__all__ = [
    "CodeLanguage",
    "DiffBundle",
    "DiffHunk",
    "FileDiff",
    "ProjectConfig",
    "RequirementDoc",
    "ReviewReport",
    "ReviewVerdict",
    "Severity",
    "StaticFinding",
    "TestArtifact",
    "TestCase",
    "TestResult",
    "TestStatus",
]
