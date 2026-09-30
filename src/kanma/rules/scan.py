"""diff 行迭代与缺陷排序工具。

勘码**没有任何正则机检规则**：问题全部由 LLM 按 `rules/data/*.md` 规约逐章做语义审查得出。
本模块只保留两类与「规则」无关的通用工具：

- `iter_added_lines` / `iter_file_added_lines`：遍历 diff 的新增行，产出带**新文件侧行号**的
  `AddedLine`（HR-01 只评新增/修改行、HR-02 证据可溯源的基础）；
- `sort_findings` / `severity_rank` / `dimension_rank`：报告排序（级别 → 维度 → 文件 → 行号）。
"""

from __future__ import annotations

from dataclasses import dataclass

from kanma.models.schemas import (
    DiffBundle,
    FileDiff,
    ReviewDimension,
    Severity,
    StaticFinding,
)


@dataclass
class AddedLine:
    file: str
    line: int
    text: str
    language: str


def iter_added_lines(diff: DiffBundle):
    """产出 (文件路径, 新文件行号, 行内容, 语言)。行号与 hunk 解析保持一致。"""
    for fd in diff.reviewable_files:
        yield from iter_file_added_lines(fd)


def iter_file_added_lines(fd: FileDiff):
    for hunk in fd.hunks:
        cursor = hunk.new_start
        lines = hunk.content.splitlines()
        for raw in lines[1:]:  # 跳过 @@ 头
            if raw.startswith("+") and not raw.startswith("+++"):
                yield AddedLine(fd.path, cursor, raw[1:], fd.language.value)
                cursor += 1
            elif raw.startswith("-") and not raw.startswith("---"):
                continue
            elif raw.startswith("\\"):
                continue
            else:
                cursor += 1


def severity_rank(sev: Severity) -> int:
    from kanma.models.schemas import SEVERITY_ORDER

    return SEVERITY_ORDER[sev]


def dimension_rank(dim: ReviewDimension) -> int:
    order = [
        ReviewDimension.SECURITY,
        ReviewDimension.CORRECTNESS,
        ReviewDimension.PERFORMANCE,
        ReviewDimension.CONVENTION,
        ReviewDimension.COMMENT,
        ReviewDimension.NAMING,
    ]
    return order.index(dim)


def sort_findings(findings: list[StaticFinding]) -> list[StaticFinding]:
    """报告排序：级别降序 → 维度 → 文件 → 行号。"""
    return sorted(
        findings, key=lambda f: (severity_rank(f.severity), dimension_rank(f.dimension),
                                 f.file, f.line)
    )
