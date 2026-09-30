"""步骤 2 · 静态 review（**按规约章节并发**）。

勘码**不做正则机检**：没有规则表，全部缺陷由 LLM 按 `rules/data` 的规约做语义审查得出。

**并发模型**：步骤 2 在图里不是一个节点，而是**每个规约章节一个节点并发执行** + 一个汇总节点：

    diff_collect ─┬─→ static_review_ch_01_coding_standards   ─┐
                  ├─→ static_review_ch_02_exception_logging   ├─→ static_review_merge
                  ├─→ static_review_ch_03_security_standards  │   （去重 + 统一编号）
                  ├─→ static_review_ch_04_mysql_database      │        │
                  ├─→ static_review_ch_05_project_structure   │        ↓
                  └─→ static_review_ch_06_design_standards   ─┘   testcase_gen

**为什么按章节而不是按维度扇出**：维度（安全/性能/…）与章节是多对多关系，
按维度扇出就得把同一章规约重复注入给多个维度节点 —— 实测 6 个维度要发
13 个调用单元、注入 42K 字符，其中 69% 是重复内容（`01-coding-standards`
7583 字符被注入了 4 遍）。按章节扇出则每章**只注入一次**：6 个单元、12.9K 字符，
同样六路并发。

- 每个节点只注入**自己那一章**的规约，focus 取自 `spec.yaml` 的 `chapter_dimensions`
  （自由文本，如「数据库规范」「性能」），提示词里作为**审查重点**而非硬约束 ——
  否则「性能」这类没有专属章节的维度就没人审了；
- 章节节点把结果写进 `raw_findings`，汇总节点统一 **去重 + 编号**后写进 `findings`
  （`findings` 走累加 reducer，若各章节直接写它就无法再重写）；
- 无适用规约（如非 Java 工程）时退化为单个 `static_review_general` 节点：
  不注入规约，六个维度全审。

**日志编号**：多个节点并发跑，各自 `StepLog(2)` 会产出 N 条 `[步骤2.1]`（HR-09 禁止
重复编号）。故由图编排发一个 `SeqAllocator` 给这批节点**按章节位置确定性预约编号块**
（章节 i 固定占 `[i*块+1, (i+1)*块]`），使 `[步骤2.N]` 与「一/二/三…」自然顺序对齐，
不会因并发到达顺序错乱。

**串行兼容入口** `static_review()` 保留：顺序跑完各章再汇总，供单测与离线脚本使用。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from kanma.llm import SEVERITY_GUIDE, LLMContext, invoke_structured
from kanma.models.schemas import DiffBundle, ReviewDimension, Severity, StaticFinding
from kanma.nodes.step_log import SeqAllocator, StepLog
from kanma.rules import build_guide, chapter_dimensions, spec_applies, spec_chapters
from kanma.state import ReviewState
from kanma.utils import chunk_diff, truncate

logger = logging.getLogger(__name__)

#: 六个审查维度（汇总统计、无规约时的全量审查、严重级别映射的唯一来源）
REVIEW_DIMENSIONS: tuple[ReviewDimension, ...] = (
    ReviewDimension.SECURITY,
    ReviewDimension.PERFORMANCE,
    ReviewDimension.CORRECTNESS,
    ReviewDimension.NAMING,
    ReviewDimension.COMMENT,
    ReviewDimension.CONVENTION,
)

#: 章节审查节点名前缀（图编排、页面进度条、报告轨迹的唯一命名来源）
STATIC_REVIEW_NODE_PREFIX = "static_review_ch_"

#: 无适用规约时的兜底节点（不注入规约，六维全审）
STATIC_REVIEW_GENERAL_NODE = "static_review_general"
GENERAL_REVIEW_TITLE = "通用审查"

#: 各章审查完成后的汇总节点（去重 + 统一编号，产出 findings）
STATIC_REVIEW_MERGE_NODE = "static_review_merge"

#: 单章注入的字符预算（rules/data 单章【强制】条文 0.4~7.6K，9000 足够装下一整章）
CHAPTER_GUIDE_BUDGET = 9000

SYSTEM_PROMPT = """你是「勘码」，企业级代码评审 Agent。现在执行流程第 2 步：静态审查。

只评审给定的 diff 片段（新增/修改行），diff 之外的既有问题一律不输出。

{focus}

严重级别只能取以下四级，禁止自造级别：
{severity_guide}
{java_guide_section}
输出要求：
1. 每个问题必须给出**新文件侧的真实行号**（diff 中 `+|` 行前面的数字）与证据代码片段。
2. 没有行号与证据的"疑似问题"禁止输出 —— 宁可漏报，不可臆测。
3. 修改建议必须具体到"怎么改"，禁止"建议优化性能"这类空话。
4. 同一位置的同类问题只报一条。
5. 若证据不足以判定，标记 needs_human=true 并说明需要人工确认的点。
6. 没有任何问题时返回空数组，不要为了凑数输出 INFO。
"""


def _diff_languages(diff: DiffBundle) -> set[str]:
    """该工程变更涉及的语言集合（用于判断是否启用规约注入）。"""
    return {fd.language.value for fd in diff.reviewable_files}


def _dedup_semantic(findings: list[StaticFinding]) -> list[StaticFinding]:
    """语义审查按章节分多步执行，同一处问题可能被多章命中，按
    (文件, 行号, 维度, 标题) 去重，避免「重复刷量」（AGENTS.MD HR-02）。"""
    seen: set[tuple[str, int, ReviewDimension, str]] = set()
    out: list[StaticFinding] = []
    for f in findings:
        key = (f.file, f.line, f.dimension, f.title)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def chapter_node(key: str) -> str:
    """章节 key -> 图节点名（`01-coding-standards` → `static_review_ch_01_coding_standards`）。"""
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", key).strip("_").lower()
    return f"{STATIC_REVIEW_NODE_PREFIX}{slug}"


def review_units(
    diffs: list[DiffBundle] | None = None,
) -> list[tuple[str, str, str]]:
    """要并发执行的审查单元 `[(节点名, 章节 key, 章节标题)]`。

    **一个章节 = 一个并发节点 = 该章规约只注入一次**，这是省 token 的关键：
    按维度扇出会把同一章重复注入给多个维度（实测 3.26x 冗余）。

    - `diffs` 为 None：不做语言判定，返回 `rules/data` 的**全量章节**（图注册用，
      图结构与具体工程无关，节点必须先注册齐）；
    - 否则按工程语言判断（见 `spec.yaml` 的 `applies_to`），不适用规约时退化为
      单个兜底节点（不注入规约，六维全审）；
    - 章节数由 `rules/data/*.md` 决定，增删章节文件不需要改代码。
    """
    if diffs is None:
        chapters = spec_chapters()
    else:
        langs: set[str] = set()
        for d in diffs:
            if getattr(d, "is_empty", False):
                continue
            langs |= _diff_languages(d)
        chapters = spec_chapters() if spec_applies(langs) else []
    units = [(chapter_node(key), key, title) for key, title in chapters]
    if not units:  # 无适用规约 / rules/data 为空：保证图始终有扇出目标
        units = [(STATIC_REVIEW_GENERAL_NODE, "", GENERAL_REVIEW_TITLE)]
    return units


def _focus_section(focus: tuple[str, ...] | list[str]) -> str:
    """按本次注入的规约章节组装「审查重点」。

    标签来自 rules/data/spec.yaml 的 `chapter_dimensions`（数据外置、自由文本，
    如「java代码规范」「数据库规范」）。它是**重点而非硬约束**：该章节点仍可报出
    其它维度的问题，否则「性能」这类没有专属章节的维度就没人审了（重复项由汇总去重）。
    """
    enum_names = "、".join(d.value for d in ReviewDimension)
    dims = tuple(focus)
    if not dims:
        return (
            "本次不注入规约，请按下列六个维度全面审查。\n"
            f"每条问题的 dimension 字段只能取以下之一：{enum_names}（取最贴近的一项）。"
        )
    names = "、".join(dims)
    return (
        f"本次审查重点：{names}。\n"
        f"优先找出属于上述重点的问题；若在同一段代码里发现其它维度的确凿问题，"
        f"也可一并报出（其它章节各审其重点，重复项由汇总环节去重）。\n"
        f"每条问题的 dimension 字段只能取以下之一：{enum_names}（取最贴近的一项）。"
    )


def _guide_section(guide: str, title: str = "") -> str:
    """把规约条文包成 prompt 段落；无规约时返回空串（不占位）。"""
    if not guide.strip():
        return ""
    label = title or "内置规约"
    return f"""
## 附加规约（{label}）

以下是本次变更内容命中的规约条文，审查时**必须**逐条对照检查。
命中【强制】条款且证据确凿的，按上面的严重级别定义判定并在 reason 中注明规约出处。
未违反的规约不要输出，也不要把规约本身当成问题描述。

{guide}
"""


class _LLMFinding(BaseModel):
    dimension: ReviewDimension
    severity: Severity
    file: str = Field(description="文件路径，必须与 diff 中的文件路径一致")
    line: int = Field(description="新文件侧行号")
    title: str
    reason: str
    suggestion: str
    needs_human: bool = False


class _LLMSemanticFindings(BaseModel):
    findings: list[_LLMFinding]


def _llm_review(
    ctx: LLMContext,
    diff_text: str,
    requirement_text: str,
    repo_name: str = "",
    guide: str = "",
    guide_title: str = "",
    focus: tuple[str, ...] | list[str] = (),
    readme: str = "",
) -> list[StaticFinding]:
    if not ctx.available:
        return []
    # 项目概况来自该工程 README.md（AGENTS.MD 步骤 2 前置动作）：先读 README 建立项目
    # 上下文，再结合规约判断 diff。缺失时如实告知，避免模型臆测工程约定。
    readme_block = (
        "## 项目概况（取自该工程 README.md，仅用于建立项目上下文、理解代码意图与既有约定）\n\n"
        f"{readme}\n\n"
        "**开始审查前，先依据上面的项目概况建立对工程的理解，再结合下面的规约与 diff 判断问题。**"
        if readme.strip()
        else "## 项目概况\n\n"
        "（该工程未提供 README.md，无法获得项目概况，请仅基于 diff 与通用工程经验审查，"
        "不得臆测工程内部约定）"
    )
    messages = [
        SystemMessage(
            content=SYSTEM_PROMPT.format(
                severity_guide=SEVERITY_GUIDE,
                focus=_focus_section(focus),
                java_guide_section=_guide_section(guide, guide_title),
            )
        ),
        HumanMessage(
            content=(
                "## 待评审 diff\n\n"
                f"{diff_text}\n\n"
                f"{readme_block}\n\n"
                "## 需求开发文档（用于需求对齐，缺失则说明无需对齐）\n\n"
                f"{truncate(requirement_text, 6000) or '（缺失）'}"
            )
        ),
    ]
    # 通道由 invoke_structured 按端点能力自动选择：部分自部署推理服务
    # （vLLM / SGLang 直连）只认 json_schema，带 tools 时会 404。
    result: _LLMSemanticFindings = invoke_structured(ctx, _LLMSemanticFindings, messages)
    return [
        StaticFinding(
            id="",  # 稍后统一编号
            dimension=f.dimension,
            severity=f.severity,
            rule_id="LLM-SEMANTIC",
            repo_name=repo_name,
            file=f.file,
            line=f.line,
            title=f.title,
            evidence="",
            reason=f.reason,
            suggestion=f.suggestion,
            needs_human=f.needs_human,
        )
        for f in result.findings
    ]


def _renumber(findings: list[StaticFinding]) -> list[StaticFinding]:
    counters: dict[str, int] = {}
    for f in findings:
        prefix = {
            ReviewDimension.SECURITY: "SEC",
            ReviewDimension.PERFORMANCE: "PERF",
            ReviewDimension.CORRECTNESS: "CORR",
            ReviewDimension.NAMING: "NAM",
            ReviewDimension.COMMENT: "CMT",
            ReviewDimension.CONVENTION: "CONV",
        }[f.dimension]
        counters[prefix] = counters.get(prefix, 0) + 1
        f.id = f"{prefix}-{counters[prefix]:03d}"
    return findings


def _project_sync(barrier: Any | None) -> None:
    """工程级串行同步：同一工程的所有章节节点并发跑完，才放行下一个工程。

    放在步骤 2 章节节点的工程循环里（每个工程前后各一道屏障），保证**多工程评审时
    不同工程的模型请求不会交叉并发**——本地模型场景下避免「6 章 × N 工程」的请求叠加压垮模型。

    - `barrier` 为 None（单工程 / 单章节节点 / 串行兼容入口）→ 直接跳过，行为不变；
    - 屏障尺寸 = 本次实际扇出的章节节点数（由图编排在路由时确定）；单工程时所有节点一次到齐
      即放行，不产生额外串行；
    - 带超时与 `BrokenBarrierError` 兜底：某章节点异常/超时才中断屏障，降级为「不强制串行」
      而非整图挂死。
    """
    if barrier is None:
        return
    try:
        # 默认无超时（各章 _llm_review 自身有模型超时，不会永久阻塞）；兜底 30min 防极端卡死
        barrier.wait(timeout=1800)
    except threading.BrokenBarrierError:
        logger.warning("步骤2 工程级串行屏障中断（某章节点超时/异常），降级为不强制串行")


class OrderedGate:
    """按章节编号**严格先进先出**放行的并发闸门（步骤 2 用）。

    两条排序性质（并发受限时）：

    1. **首次启动严格 1→N**：编号 i 的章节只有当 i-1 已启动、且有空闲额度时才放行
       —— 限 2 路时「一、二执行中」，1/2 任一先释放额度，下一个进入执行的必定是三。
    2. **后续分片让位于未启动章节**：已启动章节的第 2+ 个分片必须等「本次实际扇出的
       章节全部启动过」才能复用额度。否则先跑的章会在分片间隙无序抢占自己刚释放的
       额度，把三/四/五/六饿成「成对推进」，违反性质 1 的直观语义。全部章节都启动后
       （`_started` 集合覆盖实际参与方），后续分片恢复自由竞争，无额外开销。

    `limit<=0` 或 `>=total` 时不设限（立即放行，只记录启动集合）。
    实际参与方数量 `_expected` 由路由函数在扇出前 `set_expected()` 写入（与工程屏障
    同一机制）：图按全量章节注册，但非规约语言只扇出 1 个 general 节点——若按注册数
    （7）判断「全部启动」，唯一节点永远等不齐。未设置时退化为注册总数。

    带 `ORDER_FAILSAFE` 兜底：万一某章节点在拿额度前异常终止，等待方超时后
    无视顺序强行放行，宁可短暂超出并发也不挂死整图。
    """

    ORDER_FAILSAFE = 600.0  # 秒；正常分片远快于此，触发即说明有节点已异常

    def __init__(self, limit: int, total: int) -> None:
        self.limit = max(0, int(limit))
        self.total = max(1, int(total))
        self._cond = threading.Condition()
        self._started: set[int] = set()
        self._running = 0
        self._expected = self.total

    def set_expected(self, n: int) -> None:
        """路由时写入本次实际扇出的章节节点数（必须在任何章节拿额度之前调用）。"""
        with self._cond:
            self._expected = max(1, int(n))
            self._cond.notify_all()

    def view(self, ordinal: int) -> "GateView":
        """发给编号为 `ordinal` 的章节节点的个人视角（首 shard 有序，后续只看额度）。"""
        return GateView(self, ordinal)

    def _can_start(self, ordinal: int, first_acquire: bool) -> bool:
        if self.limit <= 0 or self.limit >= self.total:
            return True
        if self._running >= self.limit:
            return False
        if first_acquire:
            # 唯一参与方（如非规约语言只扇出 1 个 general 节点）：没有排序约束可言，
            # 直接放行 —— 否则会被「前一章已启动」的注册序判定卡死（挂到兜底超时）。
            if self._expected <= 1:
                return True
            # 首次拿额度：严格按编号顺序（前一章已启动才开始）
            return ordinal == 0 or (ordinal - 1) in self._started
        # 已启动章节的后续分片：额度先尽着未启动的章节 —— 本次实际扇出的章节
        # 全部启动过才允许复用额度，保证「下一个进入执行的必定是编号最小的未启动章」。
        return len(self._started) >= self._expected

    def try_acquire(self, ordinal: int, first_acquire: bool) -> bool:
        with self._cond:
            if not self._can_start(ordinal, first_acquire):
                return False
            self._running += 1
            self._started.add(ordinal)
            self._cond.notify_all()
            return True

    def acquire(self, ordinal: int, first_acquire: bool) -> None:
        deadline = time.monotonic() + self.ORDER_FAILSAFE
        with self._cond:
            while not self._can_start(ordinal, first_acquire):
                remaining = deadline - time.monotonic()
                if remaining <= 0:  # 防挂死兜底：超时后无视顺序强行放行
                    break
                self._cond.wait(remaining)
            self._running += 1
            self._started.add(ordinal)
            self._cond.notify_all()

    def release(self) -> None:
        with self._cond:
            self._running = max(0, self._running - 1)
            self._cond.notify_all()

    def mark_resolved(self, ordinal: int) -> None:
        """标记某章「未启动即了结」：仅满足顺序条件，不占额度（防挂死）。"""
        with self._cond:
            self._started.add(ordinal)
            self._cond.notify_all()


class GateView:
    """单个章节节点视角的闸门句柄：首次拿额度参与编号排序，后续分片让位于未启动章节。

    `finish()` 在章节节点收尾时调用：若该章从未拿到过额度（提前返回/异常终止），
    标记为「已了结」，让编号更大的章节不必永远等它启动（防挂死）。
    """

    def __init__(self, gate: OrderedGate, ordinal: int) -> None:
        self._gate = gate
        self._ordinal = ordinal
        self._started = False

    def try_acquire(self) -> bool:
        ok = self._gate.try_acquire(self._ordinal, first_acquire=not self._started)
        if ok:
            self._started = True
        return ok

    def acquire(self) -> None:
        self._gate.acquire(self._ordinal, first_acquire=not self._started)
        self._started = True

    def release(self) -> None:
        self._gate.release()

    def finish(self) -> None:
        if not self._started:
            # 从未启动即收尾：标记「已了结」，放行编号更大的等待章节（不占额度）
            self._gate.mark_resolved(self._ordinal)


def review_chapter(
    state: ReviewState,
    ctx: LLMContext | None = None,
    chapter: str = "",
    title: str = "",
    seq: SeqAllocator | None = None,
    parallel_sem: Any | None = None,
    project_barrier: Any | None = None,
) -> dict:
    """步骤 2 的**单个章节**审查（每个章节一个节点，在图里并发执行）。

    只注入 `chapter` 这一章的规约（**只此一次**，不与其它节点重复），focus 取
    `chapter_dimensions` 为该章声明的审查重点；逐 diff 分片调用 LLM。
    结果写进 `raw_findings`，由 `static_review_merge` 统一去重编号。

    `chapter` 为空 = 兜底节点：不注入任何规约，六个维度全审。

    `seq`：各章节点并发共享的日志编号分配器（缺省时本节点自成 1..N 序列）。
           用于按「章节位置」确定性预约编号块，使 `[步骤2.N]` 与章节自然顺序对齐。
    """
    label = title or GENERAL_REVIEW_TITLE
    if ctx is None:
        from kanma.llm import get_llm

        ctx = get_llm()

    # ── 确定性编号：章节 i（0 起始）固定占 [i*块+1, (i+1)*块]，与「一/二/三…」对齐 ──
    # 块大小 = 本节点将要写出的日志行数（一个非空前置仓库一行；全空/离线记 1 行）。
    ordered_keys = [k for k, _ in spec_chapters()]
    my_index = ordered_keys.index(chapter) if (chapter and chapter in ordered_keys) else 0
    diffs = state.get("diffs", [])
    if all(d.is_empty for d in diffs):
        line_count = 1
    elif not ctx.available:
        # 离线时图会直接跳到汇总节点，这里只是防御（串行兼容入口也会走到）；记 1 行
        line_count = 1
    else:
        non_empty = [d for d in diffs if not d.is_empty]
        line_count = len(non_empty) if non_empty else 1
    start = my_index * line_count + 1
    if seq is not None:
        seq.reserve(start, line_count)
    log = StepLog(2, start_seq=start)

    if all(d.is_empty for d in diffs):
        return {
            "raw_findings": [],
            "steps_log": [log.add(f"{label}审查：所有工程 diff 均为空，跳过")],
        }
    if not ctx.available:
        # 离线时图会直接跳到汇总节点，这里只是防御（串行兼容入口也会走到）
        return {
            "raw_findings": [],
            "steps_log": [log.add(f"跳过{label}审查：{ctx.reason}")],
        }

    requirement_text = state["config"].requirement.raw_text
    findings: list[StaticFinding] = []
    notes: list[str] = []
    try:
        for d in diffs:
            # 工程边界同步点①：进入当前工程前，等齐所有并发章节节点
            _project_sync(project_barrier)
            if d.is_empty:
                # 空工程也走完两道屏障，维持各章锁步（否则屏障参与数错位会死锁）
                _project_sync(project_barrier)
                continue
            chunks = list(chunk_diff(d))
            # 该工程语言不适用规约（如非 Java）时仍要审，只是不注入规约
            injects = bool(chapter) and spec_applies(_diff_languages(d))
            guide, used = (
                build_guide(chapters=[chapter], max_chars=CHAPTER_GUIDE_BUDGET)
                if injects
                else ("", [])
            )
            focus = chapter_dimensions(chapter) if injects else ()
            total = len(chunks)
            log.add(
                f"{label}审查 · {d.repo_name}（{len(chunks)} 个 diff 分片"
                f"{'' if injects else ' · 无规约注入'}）"
            )
            done = 0
            for chunk in chunks:
                done += 1
                log.progress(done, total, f"{label}审查 {d.repo_name}")
                # 并发度节流：parallel_sem 是本章节的 GateView（由图编排按章节序号发放）。
                # 首个分片按章节编号有序拿额度（限 N 路时按 一→六 顺序启动）；
                # 后续分片让位于未启动章节（额度先尽着编号最小的未启动章），
                # 全部章节启动后恢复自由竞争；None 表示不限制。
                sem = parallel_sem
                if sem is not None and not sem.try_acquire():
                    # 没轮到/没额度：节点在图层面已 start（页面默认「进行中」），但实际
                    # 在排队——推送 waiting 让页面把节点换成排队色、提示排队文本，
                    # 拿到额度后再推 running 恢复，页面颜色与真实执行进度一致。
                    log.node_state("waiting", f"{label}排队等待并发额度…")
                    sem.acquire()
                    log.node_state("running")
                try:
                    findings.extend(
                        _llm_review(
                            ctx,
                            chunk,
                            requirement_text,
                            d.repo_name,
                            guide,
                            "、".join(used),
                            focus,
                            d.readme,
                        )
                    )
                finally:
                    if sem is not None:
                        sem.release()
            # 工程边界同步点②：当前工程审查完，等齐后再放行下一工程（实现「按工程串行」）
            _project_sync(project_barrier)
    except Exception as e:
        # 页面只看到降级结论，根因要落进服务端日志，否则事后无法复盘
        logger.warning("步骤2 %s 审查失败（%s）：%s", label, ctx.endpoint, e)
        notes.append(
            f"步骤2 降级（{label}）：语义型审查执行失败（{e}），该章本次无结论"
        )
        log.add(f"{label}审查失败：{e}")
    finally:
        # 无论正常结束、提前返回还是异常终止，都要了结本章节的闸门视角：
        # 否则编号更大的章节会永远等本章「已启动」，整图挂死。
        if parallel_sem is not None:
            parallel_sem.finish()

    return {
        "raw_findings": findings,
        "steps_log": log.lines,
        "degradation_notes": notes,
    }


def static_review_merge(
    state: ReviewState,
    ctx: LLMContext | None = None,
    seq: SeqAllocator | None = None,
) -> dict:
    """步骤 2 的汇总节点：各章并发审查全部结束后去重、统一编号，产出 `findings`。

    离线（LLM 不可用）时图会跳过所有章节节点直接到本节点，降级说明也在这里写，
    保证「步骤2 降级」只出现一次而不是每章一条。
    """
    log = StepLog(2, seq=seq)
    if ctx is None:
        from kanma.llm import get_llm

        ctx = get_llm()

    notes: list[str] = []
    if not ctx.available:
        notes.append(f"步骤2 降级：{ctx.degradation_note}")
        log.add(f"跳过语义审查：{ctx.reason}")
        return {
            "findings": [],
            "steps_log": log.lines,
            "degradation_notes": notes,
        }

    raw = list(state.get("raw_findings", []))
    merged = _renumber(_dedup_semantic(raw))
    counts = Counter(f.dimension.value for f in merged)
    detail = "、".join(f"{k}×{v}" for k, v in counts.items()) or "无"
    log.add(f"步骤2 并发审查完成：共 {len(merged)} 条（{detail}，已按 文件:行号 去重）")
    return {
        "findings": merged,
        "steps_log": log.lines,
        "degradation_notes": notes,
    }


def static_review(
    state: ReviewState,
    ctx: LLMContext | None = None,
    seq: SeqAllocator | None = None,
    parallel_sem: Any | None = None,
    project_barrier: Any | None = None,
) -> dict:
    """**串行**跑完各章再汇总——并发由图编排负责，此入口供单测/离线脚本使用。"""
    if ctx is None:
        from kanma.llm import get_llm

        ctx = get_llm()
    allocator = seq or SeqAllocator()

    out: dict = {"findings": [], "steps_log": [], "degradation_notes": [], "messages": []}
    raw: list[StaticFinding] = []
    if ctx.available:
        for _node, chapter, title in review_units(state.get("diffs", [])):
            delta = review_chapter(
                state, ctx, chapter, title, seq=allocator,
                parallel_sem=parallel_sem, project_barrier=project_barrier,
            )
            raw.extend(delta.get("raw_findings", []))
            out["steps_log"].extend(delta.get("steps_log", []))
            out["degradation_notes"].extend(delta.get("degradation_notes", []))

    merged = static_review_merge(
        {**state, "raw_findings": raw}, ctx, seq=allocator
    )
    out["findings"] = merged["findings"]
    out["steps_log"].extend(merged["steps_log"])
    out["degradation_notes"].extend(merged["degradation_notes"])
    return out


def make_static_review_node(
    ctx: LLMContext,
    chapter: str = "",
    title: str = "",
    parallel_sem: Any | None = None,
    project_barrier: Any | None = None,
) -> Any:
    """把 LLM 上下文与章节绑定到节点上，便于在图里直接注册。"""

    def _node(state: ReviewState) -> dict:
        return review_chapter(
            state, ctx, chapter, title,
            parallel_sem=parallel_sem, project_barrier=project_barrier,
        )

    return _node
