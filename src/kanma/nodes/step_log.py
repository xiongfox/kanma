"""六步流程的二级编号日志（步骤 N.M）。

nodes/* 里每一行流水日志都从这里取号，禁止在各节点手写 `[步骤N]` 前缀 ——
手写前缀的结果是同一步内的多行日志都长一个样（多条 `[步骤2]`），
在报告「流程轨迹」与 Web 控制台日志窗里看不出先后与条数。

规则：
- N 是六步流程的大步骤号（1~6），由节点自己声明；
- M 是节点内流水日志的序号，同一节点按输出顺序从 1 递增；
- 每个节点独立计数，跨节点不串号；一次评审重新开一个对象即重新计数。

用法：
    log = StepLog(2)                      # 步骤 2
    log.add("语义审查 · 编程规约")         # [步骤2.1] 语义审查 · 编程规约
    log.add("语义审查共 3 条")             # [步骤2.2] 语义审查共 3 条
    return {"steps_log": log.lines}       # 取出全部行

    # return 字典里内联使用（add 返回编号后的完整文本）
    return {"steps_log": [log.add("所有工程 diff 均为空，跳过静态审查")]}

实时流出（观测性）：
    节点往往要跑几十秒（LLM 语义审查、沙箱执行）。若日志只在节点结束时
    一次性交付，页面会在整个节点期间「黑屏」。因此 add() 在追加的同时，
    若当前线程已登记发射器（由 graph._wrap_node 在节点执行前登记），
    就把这一行**立刻**推给订阅者；progress() 同理，用于推送 i/N 量化进度。

    发射器走 ContextVar：节点函数内 new StepLog(...) 无需感知调用方管线，
    也就不必给每个节点函数加一个 emitter 形参（避免污染节点签名）。

并发子任务（步骤 2 的六章审查）：
    同一大步骤的多个节点并发执行时，各自 new 一个 StepLog 会各自从 1 开始编号，
    报告里就会出现多条 `[步骤2.1]`（违反 HR-09）。此时由调用方（图编排）建一个
    `SeqAllocator` 发给这批并发节点，它们共享同一条编号序列。

    ⚠️ 编号**按章节位置确定性预约**，而不是按线程到达顺序：章节 i（0 起始）固定
    占用 `[i*块大小+1, (i+1)*块大小]`，因此无论哪个线程先跑完，「一、编程规约」
    永远拿到 `[步骤2.1]`、「二、异常日志」永远 `[步骤2.2]`，与页面进度 DAG 的
    章节顺序一致，读报告时不会再出现「步骤号与章节名对不上」的混乱。

        seq = SeqAllocator()                  # 一次评审一个
        # 各章节点：比自己位置预约编号块，再交给 StepLog 从块首自增
        seq.reserve(my_index * block + 1, block)
        StepLog(2, start_seq=my_index * block + 1).add("编程规约审查…")   # [步骤2.1]
        # 汇总节点：接着已预约的水位线发号，绝不与各章块冲突
        StepLog(2, seq=seq).add("并发审查完成…")                           # [步骤2.7]
"""

from __future__ import annotations

import re
import threading
from contextvars import ContextVar, Token
from typing import Callable, Iterable

#: 大步骤号 -> 中文步骤名（单一定义点：页面进度条与报告「流程轨迹」共用，禁止各处另写一份）
STEP_NAMES: dict[int, str] = {
    1: "采集 diff",
    2: "静态审查",
    3: "生成回归用例",
    4: "生成测试代码",
    5: "沙箱执行测试",
    6: "汇总与结论",
}

#: 节点名 -> 大步骤号（六步顺序的唯一定义，页面进度条据此排序）
#: ⚠️ 步骤 2 的章节节点名由 `rules/data/*.md` 决定（`static_review_ch_*`），
#: 不在此硬编码 —— 查表请用 `step_of()`，它按前缀识别动态节点。
NODE_STEPS: dict[str, int] = {
    "diff_collect": 1,
    "static_review": 2,
    "static_review_merge": 2,
    "testcase_gen": 3,
    "testcode_gen": 4,
    "sandbox_run": 5,
    "report_merge": 6,
}


def step_of(node: str) -> int:
    """节点名 -> 大步骤号；认不出的返回 0。

    步骤 2 的并发节点名随规约数据变化（`static_review_ch_*` / `static_review_general`），
    故按前缀兜底归入大步骤 2，而不是维护一张会漂移的节点名表。
    """
    if node in NODE_STEPS:
        return NODE_STEPS[node]
    if node.startswith("static_review_"):
        return 2
    return 0

#: 日志行前缀：`[步骤N.M] `
_SUBSTEP_PREFIX = re.compile(r"^\[步骤(\d+)\.(\d+)\]\s*")

# ---------------------------------------------------------------------------
# 实时发射器（线程内生效；由 graph._wrap_node 在节点执行前后登记/复位）
# ---------------------------------------------------------------------------
#: 载荷形如 {"type": "log"|"progress", ...}，见 StepLog.add / StepLog.progress
StepEmitter = Callable[[dict], None]
_EMITTER: ContextVar[StepEmitter | None] = ContextVar("kanma_step_emitter", default=None)


def set_emitter(fn: StepEmitter | None) -> Token:
    """登记当前线程的实时发射器，返回用于复位的 token。"""
    return _EMITTER.set(fn)


def reset_emitter(token: Token) -> None:
    """复位发射器（放在 finally 里，保证异常路径也清理）。"""
    _EMITTER.reset(token)


class SeqAllocator:
    """步骤 2 并发子任务的二级序号分配器（按章节位置确定性预约）。

    早期实现按「线程到达顺序」发号，导致报告里的 `[步骤2.N]` 与各规约章节的
    自然顺序（一/二/三…）对不上，读起来混乱。改为**按章节位置预约**：章节 i
    （0 起始）固定占用一段连续编号 `[i*块大小+1, (i+1)*块大小]`，无论哪个线程
    先跑完，「一、编程规约」永远是 `[步骤2.1]`、「二、异常日志」永远是 `[步骤2.2]`，
    依此类推。汇总节点用 `next_after()` 接着已预约的水位线发号，绝不与各章块冲突
    （HR-09：同一份报告不允许出现两条编号相同的日志）。

    `块大小` = 该章将要写出的日志行数（一个非空前置仓库一行），所有章节遍历同一
    份 diff，故块大小相同、各章块首尾相接、无重叠。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._high = 0  # 已占用编号的水位线（含）
        self._used: set[int] = set()

    def reserve(self, start: int, count: int) -> None:
        """章节节点按「章节序号 * 块大小 + 1」确定性预约一段编号。

        `count` 为该章将要写出的日志行数。预约只做记账：保证不与其他章节重叠、
        并把水位线抬高，供汇总节点的 `next_after()` 接着发号。
        """
        if count <= 0:
            return
        with self._lock:
            for s in range(start, start + count):
                if s in self._used:
                    raise ValueError(f"步骤2 子步骤编号冲突：{s} 已被占用")
                self._used.add(s)
            self._high = max(self._high, start + count - 1)

    def next_after(self) -> int:
        """汇总/兜底节点用：取当前已占用编号之后的下一个空闲号，确保不与各章块冲突。"""
        with self._lock:
            self._high += 1
            while self._high in self._used:
                self._high += 1
            self._used.add(self._high)
            return self._high

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"SeqAllocator(high={self._high})"


def parse_substep(line: str) -> tuple[int, int] | None:
    """解析 `[步骤N.M]` 前缀，返回 (大步骤号, 二级序号)；无编号时返回 None。"""
    m = _SUBSTEP_PREFIX.match(line)
    return (int(m.group(1)), int(m.group(2))) if m else None


class StepLog:
    """某个步骤的流水日志收集器，自动编号 `[步骤 N.M]`。"""

    def __init__(self, step: int, seq: SeqAllocator | None = None, start_seq: int | None = None) -> None:
        """二级序号来源（按优先级）：

        - `start_seq` 给定：从该号开始本节点内自增（并发子任务按章节位置确定性编号）；
        - 否则 `seq` 分配器非空：每次从 `seq.next_after()` 领取（汇总节点）；
        - 否则：本节点内 1,2,3… 自增（独立使用）。
        """
        if step < 1:
            raise ValueError(f"步骤号必须 ≥ 1，收到 {step}")
        if start_seq is not None and start_seq < 1:
            raise ValueError(f"起始序号必须 ≥ 1，收到 {start_seq}")
        self.step = step
        self.lines: list[str] = []
        # 计数起点：start_seq 给定则从其前一个值起，首个 add 即取到 start_seq
        self._start_seq = start_seq
        self._seq = (start_seq - 1) if start_seq is not None else 0
        self._allocator = seq

    def _take_seq(self) -> int:
        if self._allocator is not None:
            return self._allocator.next_after()
        self._seq += 1
        return self._seq

    def add(self, text: str) -> str:
        """追加一行并返回编号后的完整文本。

        若当前线程登记了发射器，这一行会**立即**推给订阅者（页面实时追加），
        而不是攒到节点结束。返回值与去重无关，仍照常返回编号后的文本。
        """
        seq_no = self._take_seq()
        body = str(text).strip()
        line = f"[步骤{self.step}.{seq_no}] {body}"
        self.lines.append(line)
        emit = _EMITTER.get()
        if emit is not None:
            try:
                emit(
                    {
                        "type": "log",
                        "step": self.step,
                        "seq": seq_no,
                        "text": body,
                        "line": line,
                    }
                )
            except Exception:  # 观测性通道不能反过来拖垮评审主流程
                pass
        return line

    def progress(self, current: int, total: int, label: str = "") -> None:
        """推送一次量化进度（i/N），供页面画进度条。

        与 add() 的区别：progress **不写入** self.lines，因此不会进入报告的
        「流程轨迹」——它是瞬时状态，不是流水线事实记录；每完成一项推一次即可。
        """
        if total <= 0:
            return
        emit = _EMITTER.get()
        if emit is None:
            return
        try:
            emit(
                {
                    "type": "progress",
                    "step": self.step,
                    "current": int(current),
                    "total": int(total),
                    "label": str(label or "").strip(),
                }
            )
        except Exception:
            pass

    def node_state(self, state: str, text: str = "") -> None:
        """推送节点执行状态变化（瞬时状态，不写入流水日志）。

        步骤 2 并发度受限时，章节节点在抢不到信号量额度期间实际处于「排队等待」，
        而图层面节点已 start（页面默认显示「进行中」）——两者不符会让页面颜色与
        真实进度脱节。这里把 waiting/running 变化推给页面，节点盒据此换色、
        提示文本区分「正在审查 / 排队中」。
        """
        emit = _EMITTER.get()
        if emit is None:
            return
        try:
            emit({"type": "node_state", "state": state, "text": str(text or "").strip()})
        except Exception:  # 观测性通道不能反过来拖垮评审主流程
            pass

    def extend(self, texts: Iterable[str]) -> list[str]:
        """批量追加，返回编号后的各行（按输入顺序）。"""
        return [self.add(t) for t in texts]

    def __iter__(self):
        return iter(self.lines)

    def __len__(self) -> int:
        return len(self.lines)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"StepLog(step={self.step}, lines={len(self.lines)})"
