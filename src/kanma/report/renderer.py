"""报告渲染：把结构化 ReviewReport 渲染为固定 8 章节的 Markdown。

章节结构由 AGENTS.MD 步骤 6 规定，不得增删。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from jinja2 import Template

from kanma.models.schemas import (
    SEVERITY_STANDARD,
    ReviewDimension,
    ReviewReport,
    Severity,
    StaticFinding,
    TestStatus,
)
from kanma.nodes.step_log import STEP_NAMES, parse_substep

import re as _re

# 渲染后的 HTML 里，把每个 <table> 包进 .table-scroll 滚动容器。
# 注意：markdown 模板里**不能**用 raw <div> 直接包表格——python-markdown 不解析
# raw HTML 块内部的表格语法，会导致表格变成纯文本。正确做法是先渲染成 <table>，
# 再用后处理包一层滚动容器（代码块不受影响，它本来就能在 raw <div> 内正常渲染）。
_TABLE_RE = _re.compile(r"<table>.*?</table>", _re.DOTALL)


def wrap_tables(html: str) -> str:
    """给报告 HTML 里的每个表格套上横向滚动容器，避免宽表撑破页面。"""
    return _TABLE_RE.sub(lambda m: f'<div class="table-scroll">{m.group(0)}</div>', html)


# 严重级别词素配色：P0 红 / P1 橙 / P2 蓝 / P3 灰。
# 只匹配「独立的 P + 单个数字 0~3」：
#   - 前面不能是字母/数字/下划线（避免命中 someMethodP0、sev-P0 已包裹的情况）
#   - 后面不能是字母/数字（避免命中 P30、P0x 这类非级别写法）
_SEV_SPAN_RE = _re.compile(r"(?<!sev-)(?<![\w])P([0-3])(?![\w])(?!</span>)")


def color_severity(html: str) -> str:
    """把报告 HTML 里的 P0/P1/P2/P3 严重级别词素包成带配色徽标的 span。

    只在转好的 HTML 上做一次后处理，不污染下载用的 .md 纯文本。
    匹配规则：独立的 P 后接单个数字 0~3，且前面不是单词字符（防误伤标识符、
    以及防止对已经生成的 sev-* span 重复包裹）。
    """
    return _SEV_SPAN_RE.sub(
        lambda m: f'<span class="sev-{m.group(0)}">{m.group(0)}</span>', html
    )

TEMPLATE = Template(
    """# 勘码 · 代码 Review 报告

## 1. 评审概览

| 项 | 值 |
| --- | --- |
| 项目 | {{ r.project }} |
| 生成时间 | {{ r.generated_at.strftime('%Y-%m-%d %H:%M:%S') }} |
| 源码工程数 | {{ r.summary.get('repo_count', 1) }} |
{% for repo in r.summary.get('repos', []) -%}
| 工程 {{ loop.index }} | {{ repo.name }}（{{ repo.git_url }}）分支 {{ repo.branch }} · 时间窗 {{ repo.window }} |
{% endfor -%}
| Commit 数 | {{ r.summary.get('commit_count', 0) }} |
| 变更文件 | {{ r.summary.get('file_count', 0) }} 个（跳过 {{ r.summary.get('skipped_count', 0) }} 个） |
| 变更行数 | +{{ r.summary.get('added', 0) }} / -{{ r.summary.get('removed', 0) }} |
| 沙箱后端 | {{ r.summary.get('sandbox', '-') }} |
{%- set llm = r.summary.get('llm', {}) %}
| 模型 | {{ llm.get('label', '-') }}{% if llm.get('base_url') %} · {{ llm.get('base_url') }}{% endif %} |

## 2. 结论判定

# {{ r.verdict.value }}

**判定依据**：{{ r.verdict_reason }}

## 3. 缺陷汇总

### 3.1 严重级别标准（HR-08）

缺陷级别只有 P0~P3 四档，判定与处置口径如下（与提示词同源，见 `SEVERITY_STANDARD`）：

| 级别 | 含义 | 处置 |
| --- | --- | --- |
{% for sev, meaning, action in sev_rows -%}
| {{ sev }} | {{ meaning }} | {{ action }} |
{% endfor %}

### 3.2 统计

| 维度 \\ 级别 | P0 | P1 | P2 | P3 |
| --- | --- | --- | --- | --- |
{% for dim in dims -%}
| {{ dim }} | {{ stat[dim]['P0'] }} | {{ stat[dim]['P1'] }} | {{ stat[dim]['P2'] }} | {{ stat[dim]['P3'] }} |
{% endfor %}

### 3.3 缺陷清单

{% if r.findings -%}
| 编号 | 级别 | 维度 | 工程 | 位置 | 问题 | 来源 | 测试印证 |
| --- | --- | --- | --- | --- | --- | --- | --- |
{% for f in r.findings -%}
| {{ f.id }} | {{ f.severity.value }} | {{ f.dimension.value }} | {{ f.repo_name or '-' }} | `{{ f.file }}:{{ f.line }}` | {{ f.title }} | {{ f.source or '语义审查' }} | {{ confirm_label(f) }} |
{% endfor %}
{%- else -%}
本次变更未发现缺陷。
{%- endif %}

## 4. 静态审查详情

{% if r.findings -%}
{% for dim in dims -%}
{% set group = r.findings | selectattr('dimension.value', 'equalto', dim) | list -%}
{% if group -%}
### {{ dim }}（{{ group | length }} 条）

{% for f in group -%}
#### {{ f.id }} · {{ f.severity.value }} · {{ f.title }}

- **位置**：`{{ f.file }}:{{ f.line }}`
- **规则**：{{ f.rule_id or '-' }}
- **来源**：{{ f.source or 'LLM 语义审查' }}
{% if f.evidence -%}
- **证据**：
```diff
{{ f.evidence }}
```
{%- endif %}
- **理由**：{{ f.reason }}
- **修改建议**：{{ f.suggestion }}
{% if f.needs_human -%}
- **⚠ 存疑（NEEDS_HUMAN）**：证据不足，需人工确认
{%- endif %}

{% endfor -%}
{%- endif -%}
{%- endfor -%}
{%- else -%}
本次变更未发现静态缺陷。
{%- endif %}

## 5. 测试验证详情

### 5.1 执行概览

| 状态 | 数量 |
| --- | --- |
{% for s, n in status_counts.items() -%}
| {{ s }} | {{ n }} |
{% endfor %}

{% if pass_rate is not none -%}
**通过率（不含 NOT_RUN）**：{{ '%.1f' | format(pass_rate * 100) }}%
{%- else -%}
**通过率**：无有效执行结果
{%- endif %}

### 5.2 用例结果

{% if r.test_results -%}
| 用例 | 状态 | 耗时 | 说明 |
| --- | --- | --- | --- |
{% for tr in r.test_results -%}
| {{ tr.case_id }} | {{ tr.status.value }} | {{ tr.duration_ms }}ms | {{ tr.message | replace('|', '/') }} |
{% endfor %}
{%- else -%}
未执行测试。
{%- endif %}

{% if failures -%}
### 5.3 失败详情

{% for tr in failures -%}
#### {{ tr.case_id }} · {{ tr.status.value }}

{{ tr.message }}

{% if tr.log_excerpt -%}
```
{{ tr.log_excerpt }}
```
{%- endif %}
{% endfor -%}
{%- endif %}

### 5.4 生成的测试用例

> 步骤 3 产出，无论第 5 步是否执行均列出，供人工复核验收条件。

{% if r.test_cases -%}
| 编号 | 来源 | 标题 | 前置条件 | 输入 | 期望输出 | 关联位置 |
| --- | --- | --- | --- | --- | --- | --- |
{% for c in r.test_cases -%}
| {{ c.id }} | {{ c.origin.value }} | {{ c.title }} | {{ c.precondition | replace('|', '/') or '无' }} | {{ c.input_desc | replace('|', '/') or '无' }} | {{ c.expected | replace('|', '/') }} | {{ c.diff_ref or (c.requirement_ref or '无') }}{% if c.finding_id %}（缺陷 {{ c.finding_id }}）{% endif %} |
{% endfor %}
{%- else -%}
未生成测试用例。
{%- endif %}

### 5.5 生成的测试代码

> 步骤 4 产出。即使第 5 步未真实执行（NOT_RUN / 降级），代码内容也照常保留，便于人工复核。

{% if r.test_code -%}
{% for blk in r.test_code -%}
<div class="table-scroll">
#### {{ blk.repo_name or '工程' }} · {{ blk.path }} {% if blk.executed %}（已执行）{% else %}（未执行）{% endif %}

- 框架：{{ blk.framework or '未知' }}
- 覆盖用例：{{ blk.case_ids | join(', ') or '无' }}

{{ blk.fence }}{{ _lang_for(blk.path) }}
{{ blk.content }}
{{ blk.fence }}
</div>

{% endfor -%}
{%- else -%}
未生成测试代码。
{%- endif %}

## 6. 需求符合性核对表

{% if has_requirement -%}
需求开发文档：**{{ requirement_doc_title }}**

{% endif %}
{% if r.requirement_checks -%}
| 需求点 | 关联用例 | 结果 | 备注 |
| --- | --- | --- | --- |
{% for c in r.requirement_checks -%}
| {{ c.requirement_point | replace('|', '/') }} | {{ c.case_ids | join(', ') or '-' }} | {{ '✔ 通过' if c.passed else '✘ 未通过' }} | {{ c.note }} |
{% endfor %}
{%- else -%}
{{ '需求开发文档缺失，未生成需求符合性核对。' if not has_requirement else '未抽取到可核对的需求点。' }}
{%- endif %}

## 7. 流程降级说明

{% if r.degradation_notes -%}
{% for n in r.degradation_notes -%}
- {{ n }}
{% endfor -%}
{%- else -%}
无。六步流程完整执行。
{%- endif %}

{% if r.readme_notes -%}
### 项目上下文（README）

> 下列工程的 README.md 缺失，步骤 2 静态审查在未建立项目上下文下进行（AGENTS.MD 步骤 1/2）。
> 仅影响理解深度，**不计入流程降级**，不影响结论判定。

{% for n in r.readme_notes -%}
- {{ n }}
{% endfor -%}
{%- endif %}

### 流程轨迹

{% for g in step_groups -%}
{% if g.no -%}
- **步骤 {{ g.no }} · {{ g.name }}**
{% for line in g.lines -%}
  - {{ line }}
{% endfor %}
{%- else -%}
{% for line in g.lines -%}
- {{ line }}
{% endfor %}
{%- endif %}
{% endfor %}

## 8. 顺带发现（Out of Scope）

以下为 diff 范围之外的既有问题，仅供参考，**不计入本次结论**。

{% if r.out_of_scope -%}
{% for item in r.out_of_scope -%}
- {{ item }}
{% endfor -%}
{%- else -%}
无。
{%- endif %}
"""
)

DIMENSIONS = [d.value for d in ReviewDimension]


def group_steps(lines: list[str]) -> list[dict]:
    """把 `[步骤N.M]` 流水日志按大步骤连续分组，供「流程轨迹」树状展示。

    分组只合并**相邻的同一步**，因此即便日志出现乱序也不会重排。
    解析不出 `步骤N.M` 编号的行（如历史报告）原样平铺输出。

    组内**按二级序号升序**：步骤 2 的六个维度节点并发执行，编号在日志发出的瞬间
    领取，而写入状态（进而进入报告）是按节点结束的先后顺序，两者可能差一两位
    （如 2.6 先于 2.5 落盘）。报告是给人读的，组内排序后轨迹始终自洽。
    """
    groups: list[dict] = []
    for line in lines:
        parsed = parse_substep(line)
        no = str(parsed[0]) if parsed else ""
        if not groups or groups[-1]["no"] != no:
            groups.append(
                {
                    "no": no,
                    "name": STEP_NAMES.get(parsed[0], "") if parsed else "",
                    "lines": [],
                }
            )
        groups[-1]["lines"].append(line)

    # 组间按大步骤号升序（步骤 2 各章并发落盘可能乱序，报告需低→高）；
    # 无编号行（no=""）归到末尾并保持相对先后（stable sort 保证）。
    for g in groups:
        g["_sort_no"] = int(g["no"]) if g["no"].isdigit() else 9999
    groups.sort(key=lambda g: g["_sort_no"])
    # 组内按二级序号排序：并发子节点的落盘顺序可能与领号顺序差一两位
    for g in groups:
        g["lines"].sort(key=lambda ln: (parse_substep(ln) or (0, 0))[1])
        g.pop("_sort_no", None)
    return groups


def _confirm_label(f: StaticFinding) -> str:
    if f.confirmed_by_test is True:
        return "已复现"
    if f.confirmed_by_test is False:
        return "未复现（存疑）"
    return "-"


_LANG_BY_EXT = {
    ".py": "python",
    ".java": "java",
    ".go": "go",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "jsx",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".rs": "rust",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
}


def _lang_for(path: str) -> str:
    """从测试文件路径后缀推断代码围栏语言标识。"""
    ext = Path(path).suffix.lower()
    return _LANG_BY_EXT.get(ext, "")


def render_report(
    r: ReviewReport, has_requirement: bool = True, requirement_doc_title: str = ""
) -> str:
    stat: dict[str, Counter] = {d: Counter() for d in DIMENSIONS}
    for f in r.findings:
        stat[f.dimension.value][f.severity.value] += 1

    status_counts = Counter(tr.status.value for tr in r.test_results)
    for s in TestStatus:
        status_counts.setdefault(s.value, 0)

    counted = [tr for tr in r.test_results if tr.counted]
    pass_rate = (
        sum(1 for tr in counted if tr.status is TestStatus.PASSED) / len(counted)
        if counted
        else None
    )
    failures = [
        tr for tr in r.test_results if tr.status in (TestStatus.FAILED, TestStatus.ERROR)
    ]

    # 流程轨迹：把 `[步骤N.M]` 流水日志按大步骤分组（层级编号在 report_merge 产出）
    step_groups = group_steps(r.summary.get("steps_log") or [])

    return TEMPLATE.render(
        r=r,
        dims=DIMENSIONS,
        stat=stat,
        # 严重级别标准：单一来源 SEVERITY_STANDARD（HR-08），报告与提示词同一口径
        sev_rows=[
            (sev.value, meaning, action)
            for sev, (meaning, action) in SEVERITY_STANDARD.items()
        ],
        status_counts=status_counts,
        pass_rate=pass_rate,
        failures=failures,
        step_groups=step_groups,
        has_requirement=has_requirement,
        requirement_doc_title=requirement_doc_title,
        confirm_label=_confirm_label,
        _lang_for=_lang_for,
    )


def severity_of(value: str) -> Severity:
    return Severity(value)
