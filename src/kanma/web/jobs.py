"""后台评审任务：在独立线程中跑完整六步流水线，可被页面轮询。"""

from __future__ import annotations

import json
import queue
import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from kanma.models.schemas import ProjectConfig
from kanma.nodes.step_log import _SUBSTEP_PREFIX
from kanma.report.renderer import wrap_tables, color_severity


class ReviewJob:
    """一次评审运行的轻量状态。

    除状态字段外还带一个事件队列：每个(graph)节点完成、报告片段产出都会 emit，
    页面通过 `/api/jobs/{id}/stream`（SSE）订阅，实现流式展示。
    """

    def __init__(self, slug: str) -> None:
        self.job_id = uuid.uuid4().hex[:12]
        self.slug = slug
        self.status: str = "queued"  # queued | running | done | error
        self.started_at: str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.finished_at: str | None = None
        self.markdown_path: str | None = None
        self.verdict: str | None = None
        self.summary: dict[str, Any] = {}
        self.steps_log: list[str] = []
        self.error: str | None = None
        self.llm: dict[str, Any] = {}
        self.skip_reasons: dict[str, str] = {}  # node -> skip reason (e.g., "diff_collect" -> "diff empty, skipped")
        # node -> 最新一次进度载荷（{"current":i,"total":n,"label":...}），
        # 仅用于观测性展示：断线重连时靠它把进度条还原到当前位置，不入报告。
        self.progress: dict[str, dict] = {}
        # node -> 真实执行状态（running/waiting/done/error）。
        # 页面重进（SSE snapshot）时靠它精确还原每个节点的颜色，而不是把
        # 所有已见节点一律猜成 done —— 排队中/进行中的章节必须如实显示。
        self.node_states: dict[str, str] = {}
        self.seen_nodes: set[str] = set()  # 已出现过的图节点名（含步骤2各章子节点），供断线重连还原 DAG
        self.node_labels: dict[str, str] = {}  # node -> 中文步骤名，供重连时恢复子节点标题
        self._subscribers: list[queue.Queue] = []
        self._sub_lock = threading.Lock()
        self._seq = 0

    # --- 事件总线（供 SSE 消费） ---
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._sub_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._sub_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def emit(self, kind: str, payload: dict | None = None) -> None:
        """广播一个事件。kind: step / live / report / done / error。"""
        self._seq += 1
        event = {"seq": self._seq, "kind": kind, "payload": payload or {}}
        with self._sub_lock:
            for q in list(self._subscribers):
                q.put(event)


_JOBS: dict[str, ReviewJob] = {}
_JOBS_LOCK = threading.Lock()

_ACTIVE = ("queued", "running")


class BusyError(RuntimeError):
    """全局已有评审在跑时启动新评审抛出的错误。

    携带 `job`，调用方可据此拼出「哪个项目正在评审」的提示文案。
    """

    def __init__(self, job: "ReviewJob") -> None:
        super().__init__(f"已有评审在进行中：{job.slug}（{job.job_id}）")
        self.job = job


def get_job(job_id: str) -> ReviewJob | None:
    return _JOBS.get(job_id)


def list_jobs(slug: str | None = None) -> list[ReviewJob]:
    with _JOBS_LOCK:
        jobs = list(_JOBS.values())
    if slug:
        jobs = [j for j in jobs if j.slug == slug]
    jobs.sort(key=lambda j: j.started_at, reverse=True)
    return jobs


def active_job(slug: str | None = None) -> ReviewJob | None:
    """当前正在进行（queued/running）的评审任务。

    不传 `slug` 时为**全局**视角——同一时刻只允许一个项目在评审，
    这是查看"后台是否还有评审在跑"的唯一入口。
    """
    with _JOBS_LOCK:
        jobs = [j for j in _JOBS.values() if j.status in _ACTIVE]
    if slug:
        jobs = [j for j in jobs if j.slug == slug]
    jobs.sort(key=lambda j: j.started_at)
    return jobs[0] if jobs else None


def active_jobs() -> list[ReviewJob]:
    """全部进行中的评审任务（正常只有一个，但保留列表形态便于诊断）。"""
    with _JOBS_LOCK:
        jobs = [j for j in _JOBS.values() if j.status in _ACTIVE]
    jobs.sort(key=lambda j: j.started_at)
    return jobs


def start_review(slug: str, cfg: ProjectConfig, store_dir: Path) -> ReviewJob:
    """启动后台评审，立即返回任务句柄。

    全局串行：同一时刻只允许一个项目评审。检查与注册在同一把 `_JOBS_LOCK` 内
    完成，避免两个并发请求同时通过检查窗口。

    线程是 daemon 且不依赖 SSE 订阅者 —— 页面关掉、跳走都不会中断，
    任务会继续跑到底并落盘，随时可以从 `/report/<slug>/<job_id>` 重连查看。

    Raises:
        BusyError: 已有评审在进行中。
    """
    job = ReviewJob(slug)
    with _JOBS_LOCK:
        busy = [j for j in _JOBS.values() if j.status in _ACTIVE]
        if busy:
            raise BusyError(sorted(busy, key=lambda j: j.started_at)[0])
        _JOBS[job.job_id] = job
    t = threading.Thread(target=_run, args=(job, cfg, store_dir), daemon=True)
    t.start()
    return job


def extract_skip_reason(lines: list[str]) -> str | None:
    """从某节点的流程日志里抽取「该大步骤被跳过」的原因文本。

    节点用 `StepLog` 输出形如 `[步骤5.1] 跳过：{原因}` / `[步骤2.1] 所有工程
    diff 均为空，跳过静态审查` 的日志。需先去掉 `[步骤N.M] ` 前缀再判断，并用
    「跳过短语」子串匹配，覆盖开头与中途出现的整步跳过信息；同时排除
    `diff_collect` 的「跳过 N 个非源码文件」这类非整步跳过的提示行。
    """
    _SKIP_MARKS = (
        "跳过：", "跳过语义审查", "跳过静态审查", "跳过用例生成",
        "跳过测试代码", "无用例，跳过", "无对应用例，跳过",
    )
    for line in lines:
        body = _SUBSTEP_PREFIX.sub("", line, count=1) if line.startswith("[步骤") else line
        if any(mark in body for mark in _SKIP_MARKS):
            return body
    return None


def _run(job: ReviewJob, cfg: ProjectConfig, store_dir: Path) -> None:
    job.status = "running"
    runs_dir = store_dir / job.slug / "runs"
    try:
        from kanma.graph import run_review, step_label
        from kanma.llm import get_llm
        from kanma.tools.sandbox import Sandbox

        def on_step(
            phase: str,
            node: str,
            elapsed_ms: float,
            lines: list[str],
            live: dict | None = None,
        ) -> None:
            """节点开始/结束推 step 事件（画时间线）；执行中经 live 实时流出。

            live 让长节点（LLM 语义审查、沙箱执行）不再"黑屏"：日志行与 i/N
            进度随做随推，页面即时追加，而不是等节点结束才一次性给出。
            """
            # 步骤 2 的章节节点名由 rules/data 决定，故标签动态解析而非查固定表
            label = step_label(node)
            job.seen_nodes.add(node)  # 记录已出现的节点，供重连时重建 DAG（含各章子节点）
            job.node_labels[node] = label  # 缓存中文步骤名，重连时前端据此还原子节点标题

            # ---- 节点级真实状态（供重进页面时精确还原颜色） ----
            if phase == "start":
                job.node_states[node] = "running"
            elif phase == "done":
                job.node_states[node] = "done"
            elif phase == "error":
                job.node_states[node] = "error"

            # ---- 实时通道 ----
            if phase == "live":
                payload = dict(live or {})
                payload["node"] = node
                payload["step_label"] = label
                if payload.get("type") == "log":
                    line = payload.get("line") or ""
                    if line:
                        job.steps_log.append(line)
                    # 跳过原因一出现就广播，不等节点结束（观测性）
                    reason = extract_skip_reason([payload.get("text") or line])
                    payload["skip_reason"] = reason or None
                    if reason:
                        job.skip_reasons[node] = reason
                elif payload.get("type") == "progress":
                    job.progress[node] = payload  # 供断线重连时还原进度条
                elif payload.get("type") == "node_state":
                    # waiting/running：并发受限时的排队与恢复，重进页面要能还原
                    job.node_states[node] = payload.get("state") or "running"
                job.emit("live", payload)
                return

            # ---- 节点开始/结束 ----
            if lines:
                job.steps_log.extend(lines)
            # 检测跳过原因：节点用 StepLog 输出 `[步骤N.M] 跳过：{原因}` 等，
            # 需在 on_step 回调里抽取并广播，页面据此在进度时间线展示「⚠ 跳过原因」。
            skip_reason = extract_skip_reason(lines)
            if skip_reason:
                job.skip_reasons[node] = skip_reason
            job.emit(
                "step",
                {
                    "phase": phase,
                    "node": node,
                    "label": label,
                    "elapsed_ms": elapsed_ms,
                    "lines": list(lines),
                    "total_lines": len(job.steps_log),
                    "skip_reason": skip_reason,
                },
            )

        ctx = get_llm()  # 按页面上选择的本地/云端模式解析
        job.llm = ctx.to_dict()
        sandbox = Sandbox(backend="auto", timeout=300)
        runs_dir.mkdir(parents=True, exist_ok=True)

        # 步骤 2 并发度阈值：优先用系统设置（Web 控制台系统设置卡，全局生效），
        # 缺失时回落到环境变量 / 不限制。CLI 显式 --max-static-parallel 仍会覆盖它。
        from kanma.system_settings import load_settings as load_sys_settings

        sys_cfg = load_sys_settings(store_dir)
        system_max_parallel = sys_cfg.static_review_max_parallel

        report, state = run_review(
            cfg, ctx=ctx, sandbox=sandbox, output_dir=str(runs_dir),
            on_step=on_step, system_max_parallel=system_max_parallel,
        )

        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        md_path = runs_dir / f"review-{ts}.md"
        md_path.write_text(report.markdown, encoding="utf-8")

        job.markdown_path = str(md_path)
        job.verdict = report.verdict.value
        job.summary = {
            "p0": report.p0_count,
            "p1": report.p1_count,
            "total": len(report.findings),
            "tests": len(report.test_results),
        }
        job.steps_log = state.get("steps_log", [])
        job.status = "done"

        # 报告正文分块推出，页面上像"打字"一样逐段出现
        chunks = iter_report_chunks(report.markdown)
        total = len(chunks)
        for idx, html in enumerate(chunks):
            job.emit("report", {"index": idx, "total": total, "html": html})
    except BaseException:  # noqa: BLE001 - 含 SystemExit 等 BaseException，须如实记录，不能静默吞掉（否则任务卡 running 且无 error）
        job.status = "error"
        job.error = traceback.format_exc()
    finally:
        job.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            _write_sidecar(runs_dir, job)
        except Exception:  # pragma: no cover - 落盘失败不影响收尾事件
            pass
        job.emit(
            "error" if job.status == "error" else "done",
            {
                "status": job.status,
                "verdict": job.verdict,
                "summary": job.summary,
                "error": job.error,
                "finished_at": job.finished_at,
                "markdown_path": job.markdown_path,
                "skip_reasons": job.skip_reasons,
            },
        )


def load_or_replay_job(job_id: str, store_dir: Path) -> ReviewJob | None:
    """按 job_id 取任务：内存优先；内存已回收（服务重启 / GC）时从磁盘重放。

    重放出来的任务处于终态且不注册到全局字典 —— SSE 连接后会立刻收到
    snapshot + done/error，页面上表现为"历史报告直接打开"。
    """
    job = get_job(job_id)
    if job is not None:
        return job
    disk = load_run(store_dir, job_id)
    if disk is None:
        return None
    replay = ReviewJob(disk.get("slug") or "")
    replay.job_id = job_id
    replay.status = disk.get("status") or "done"
    replay.started_at = disk.get("started_at") or ""
    replay.finished_at = disk.get("finished_at")
    replay.verdict = disk.get("verdict")
    replay.summary = disk.get("summary") or {}
    replay.steps_log = disk.get("steps_log") or []
    replay.error = disk.get("error")
    replay.llm = disk.get("llm") or {}
    md = disk.get("markdown_path")
    replay.markdown_path = md if md and Path(md).exists() else None
    return replay


def iter_report_chunks(markdown_text: str, min_chars: int = 900) -> list[str]:
    """把报告 Markdown 切成若干块，逐块转成 HTML 后流式推送。

    切块要保证语义完整：代码块（` ``` ` 围栏）整块输出，表格的连续行不被拆开，
    否则单块转换会丢样式。切完再按 min_chars 合并小块，避免刷新太碎。
    """
    import markdown as md_lib

    blocks: list[str] = []
    buf: list[str] = []
    in_fence = False
    for line in markdown_text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```"):
            if not in_fence:
                if buf:
                    blocks.append("\n".join(buf))
                    buf = []
                in_fence = True
                buf.append(line)
            else:
                buf.append(line)
                blocks.append("\n".join(buf))  # 代码块整块输出
                buf = []
                in_fence = False
            continue
        if in_fence:
            buf.append(line)
            continue
        if not line.strip() and buf:
            blocks.append("\n".join(buf))
            buf = []
            continue
        buf.append(line)
    if buf:
        blocks.append("\n".join(buf))

    # 合并过小的块（但单个代码块即便很长也单独成块，避免半截代码）
    merged: list[str] = []
    acc = ""
    for b in blocks:
        candidate = f"{acc}\n\n{b}" if acc else b
        if acc and len(candidate) <= min_chars:
            acc = candidate
        else:
            if acc:
                merged.append(acc)
            acc = b
    if acc:
        merged.append(acc)

    return [
        color_severity(
            wrap_tables(md_lib.markdown(chunk, extensions=["tables", "fenced_code"]))
        )
        for chunk in merged
    ]


def _write_sidecar(runs_dir: Path, job: ReviewJob) -> None:
    """把运行结果落盘为 JSON sidecar，报告 Markdown 旁边。

    这样即使服务重启（内存中的任务字典清空），历史评审仍可经磁盘读取与查看。
    """
    runs_dir.mkdir(parents=True, exist_ok=True)
    sidecar = runs_dir / f"{job.job_id}.json"
    sidecar.write_text(
        json.dumps(
            {
                "job_id": job.job_id,
                "slug": job.slug,
                "status": job.status,
                "verdict": job.verdict,
                "summary": job.summary,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "steps_log": job.steps_log,
                "markdown_path": job.markdown_path,
                "llm": job.llm,
                "error": job.error,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def list_runs(store_dir: Path, slug: str) -> list[dict]:
    """读取某项目的评审历史（来自磁盘 sidecar，重启后仍可用）。

    额外补两个展示字段：`duration_seconds`（耗时）与 `has_markdown`（报告原文是否还在）。
    """
    runs_dir = store_dir / slug / "runs"
    if not runs_dir.is_dir():
        return []
    runs: list[dict] = []
    for p in runs_dir.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # pragma: no cover - 损坏文件跳过
            continue
        data["duration_seconds"] = _duration_seconds(data)
        md = data.get("markdown_path")
        data["has_markdown"] = bool(md) and Path(md).exists()
        runs.append(data)
    runs.sort(key=lambda r: r.get("finished_at") or r.get("started_at") or "", reverse=True)
    return runs


def _duration_seconds(run: dict) -> int | None:
    """起止时间差（秒）；缺失或格式异常返回 None。"""
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        start = datetime.strptime(run.get("started_at") or "", fmt)
        end = datetime.strptime(run.get("finished_at") or "", fmt)
    except (TypeError, ValueError):
        return None
    return max(int((end - start).total_seconds()), 0)


def load_run(store_dir: Path, job_id: str) -> dict | None:
    """按 job_id 从磁盘回退读取运行记录（内存字典已清空时可用）。"""
    if not store_dir.is_dir():
        return None
    for sidecar in store_dir.glob("*/runs/*.json"):
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:  # pragma: no cover
            continue
        if data.get("job_id") == job_id:
            return data
    return None
