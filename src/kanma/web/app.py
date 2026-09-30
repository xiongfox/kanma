"""勘码 Web 控制台（FastAPI）。

提供评审项目的录入页面与一组 JSON 接口；保存的配置为 YAML，
可直接被 `kanma review -c` 消费。立即评审在后台线程跑六步流水线。
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from kanma import __version__
from kanma.config import slugify
from kanma.report.renderer import wrap_tables, color_severity
from kanma.llm import ping_llm
from kanma.llm_settings import (
    get_settings,
    load_settings,
    probe_local_services,
    save_settings,
)
from kanma.system_settings import (
    SystemSettings,
    get_settings as get_sys_settings,
    load_settings as load_sys_settings,
    save_settings as save_sys_settings,
)
from kanma.web.jobs import (
    BusyError,
    active_job,
    get_job,
    iter_report_chunks,
    list_jobs,
    list_runs,
    load_or_replay_job,
    load_run,
    start_review,
)
from kanma.web.schemas import ProjectCreate

logger = logging.getLogger(__name__)
from kanma.web.store import (
    delete_project,
    list_projects,
    load_project,
    migrate_project,
    project_path,
    save_project,
)

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时把项目目录里保存的模型设置 / 系统设置载入进程（页面切换后无需重启）。"""
    store = Path(app.state.store_dir)
    store.mkdir(parents=True, exist_ok=True)
    load_settings(store)
    load_sys_settings(store)
    yield


app = FastAPI(title="勘码 KanMa", version=__version__, lifespan=lifespan)
app.state.store_dir = Path("projects")


def _store() -> Path:
    d = Path(app.state.store_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    projects = list_projects(_store())
    return _templates.TemplateResponse(
        request,
        "index.html",
        {
            "projects": projects,
            "version": __version__,
            "llm": get_settings().to_api(),
            "sys": get_sys_settings().to_api(),
        },
    )


@app.get("/api/health")
def health():
    return {"ok": True, "version": __version__}


# --- 大模型设置：本地模式 / 云端模式 -------------------------------------


@app.get("/api/llm")
def api_get_llm():
    """当前模型设置（密钥脱敏）。"""
    return get_settings().to_api()


@app.put("/api/llm")
def api_put_llm(payload: dict[str, Any]):
    """保存并切换到指定模式。api_key 传空表示保留原值，传 `__CLEAR__` 表示清空。"""
    try:
        merged = get_settings().with_incoming(payload)
    except Exception as exc:  # 非法 mode / 数值越界等
        raise HTTPException(status_code=422, detail=f"模型设置不合法：{exc}") from exc
    path = save_settings(merged, _store())
    return {"message": "模型设置已保存", "path": str(path), **merged.to_api()}


@app.post("/api/llm/test")
def api_test_llm(payload: dict[str, Any] | None = None):
    """连通性自检。可带临时参数覆盖（仅测试，不落盘）。"""
    settings = get_settings()
    override_model: str | None = None
    if payload:
        try:
            settings = settings.with_incoming(payload)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"模型设置不合法：{exc}") from exc
        override_model = payload.get("model") or None
    return ping_llm(settings=settings, model=override_model)


@app.post("/api/llm/probe")
def api_probe_local():
    """探测本机常见推理服务（Ollama / LM Studio / vLLM / Xinference / LocalAI）。"""
    return {"results": probe_local_services()}


# --- 系统设置：全局生效（步骤 2 并发度阈值等） -------------------------------


@app.get("/api/system")
def api_get_system():
    """当前系统设置（全局生效，对所有评审项目共享）。"""
    return get_sys_settings().to_api()


@app.put("/api/system")
def api_put_system(payload: dict[str, Any]):
    """保存系统设置（当前仅步骤 2 并发度阈值）。"""
    cur = get_sys_settings()
    data = dict(cur.model_dump())
    if "static_review_max_parallel" in payload and payload["static_review_max_parallel"] is not None:
        try:
            raw = int(payload["static_review_max_parallel"])
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422, detail="并发度上限必须为非负整数"
            ) from None
        if raw < 0:
            raise HTTPException(
                status_code=422, detail="并发度上限不能为负数（0 表示不限制）"
            )
        data["static_review_max_parallel"] = raw
    saved = SystemSettings.model_validate(data)
    save_sys_settings(saved, _store())
    return saved.to_api()


@app.get("/api/projects")
def api_list_projects():
    return {"projects": list_projects(_store())}


@app.post("/api/projects")
def api_create_project(payload: ProjectCreate):
    # 需求文档本地路径必须存在（服务端可见），否则明确报错
    if payload.requirement_doc:
        p = Path(payload.requirement_doc).expanduser()
        if not p.exists():
            raise HTTPException(
                status_code=422,
                detail=f"需求文档路径不存在：{payload.requirement_doc}",
            )
    try:
        cfg = payload.to_config()  # 此处会触发 end_time>=start_time 等校验
    except Exception as exc:  # 校验失败（如时间窗非法）转为 422
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    path = save_project(cfg, _store())
    return {
        "slug": path.stem,
        "name": cfg.name,
        "path": str(path),
        "message": "项目已保存",
    }


@app.get("/api/projects/{slug}")
def api_get_project(slug: str):
    cfg = load_project(slug, _store())
    if cfg is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return cfg.model_dump(mode="json")


@app.put("/api/projects/{slug}")
def api_update_project(slug: str, payload: ProjectCreate):
    """编辑已保存项目。改名会换 slug，评审历史随之迁移到新 slug 下。"""
    store = _store()
    if load_project(slug, store) is None:
        raise HTTPException(status_code=404, detail="项目不存在")

    if payload.requirement_doc:
        p = Path(payload.requirement_doc).expanduser()
        if not p.exists():
            raise HTTPException(
                status_code=422,
                detail=f"需求文档路径不存在：{payload.requirement_doc}",
            )
    try:
        cfg = payload.to_config()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    new_slug = slugify(cfg.name)
    if new_slug != slug and project_path(new_slug, store).exists():
        raise HTTPException(
            status_code=409, detail=f"已存在同名项目（{new_slug}），请换个项目名"
        )

    path = save_project(cfg, store)
    renamed = new_slug != slug
    if renamed:
        migrate_project(slug, new_slug, store)
    return {
        "slug": new_slug,
        "name": cfg.name,
        "path": str(path),
        "renamed": renamed,
        "message": "项目已更新",
    }


@app.delete("/api/projects/{slug}")
def api_delete_project(slug: str):
    if not delete_project(slug, _store()):
        raise HTTPException(status_code=404, detail="项目不存在")
    return {"message": "已删除", "slug": slug}


@app.delete("/api/runs/{job_id}")
def api_delete_run(job_id: str):
    """删除单份评审报告（JSON sidecar + markdown 原文）。"""
    from kanma.web.store import delete_run

    # 进行中的评审不允许删，避免与正在写的 sidecar 互相踩
    running = [j for j in list_jobs() if j.job_id == job_id and j.status in ("queued", "running")]
    if running:
        raise HTTPException(status_code=409, detail="该评审正在进行中，无法删除")
    if not delete_run(_store(), job_id):
        raise HTTPException(status_code=404, detail="报告不存在")
    return {"message": "已删除", "job_id": job_id}


@app.delete("/api/projects/{slug}/runs")
def api_clear_runs(slug: str):
    """清空某项目的全部评审报告（进行中的除外）。"""
    from kanma.web.store import clear_runs

    cfg = load_project(slug, _store())
    if cfg is None:
        # 项目配置已删但产物目录还在时，仍允许清产物
        if not (_store() / slug / "runs").is_dir():
            raise HTTPException(status_code=404, detail="项目不存在")
    running = {j.job_id for j in list_jobs(slug) if j.status in ("queued", "running")}
    deleted = clear_runs(_store(), slug, skip_job_ids=running)
    return {"message": "已清空", "slug": slug, "deleted": deleted}


@app.post("/api/projects/{slug}/run")
def api_run_review(slug: str):
    """发起评审（立即返回，任务在后台线程执行）。

    全局串行：同一时刻只允许一个项目在评审——不同项目的工作区、克隆目录与
    测试沙箱会互相踩。已有评审时返回 409 并带上是谁在跑，方便前端提示。

    任务一旦启动即与请求解耦：关掉页面、跳转都不会中断，稍后打开报告页
    可通过 SSE 重连继续看实时进度。
    """
    cfg = load_project(slug, _store())
    if cfg is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    try:
        job = start_review(slug, cfg, _store())
    except BusyError as e:
        other = e.job
        same = other.slug == slug
        raise HTTPException(
            status_code=409,
            detail=(
                f"该项目已有评审在进行中（{other.job_id}），请等待其完成后再发起"
                if same
                else f"「{other.slug}」的评审正在进行中（{other.job_id}），"
                f"同一时刻只能评审一个项目，请等待其完成后再发起"
            ),
        ) from e
    return {
        "job_id": job.job_id,
        "status": job.status,
        "background": True,  # 明确告知前端：这是后台任务，页面可自由离开
    }


@app.get("/api/jobs/active")
def api_active_job():
    """当前正在后台评审的任务（没有则 `active=None`）。

    前端据此渲染「后台运行中」横幅、禁用其它项目的评审按钮，并在任务结束后
    自动刷新列表。
    """
    job = active_job()
    if job is None:
        return {"active": None}
    elapsed = None
    try:
        started = datetime.strptime(job.started_at, "%Y-%m-%d %H:%M:%S")
        elapsed = int((datetime.now() - started).total_seconds())
    except ValueError:  # pragma: no cover - 格式异常时不影响返回结构
        pass
    return {
        "active": {
            "job_id": job.job_id,
            "slug": job.slug,
            "status": job.status,
            "started_at": job.started_at,
            "elapsed_seconds": elapsed,
            "total_steps": len(job.steps_log),
        }
    }


def _job_view(job_id: str) -> dict | None:
    """单次评审的合并视图：磁盘 sidecar 为准，内存任务兜底。

    以磁盘为准是因为项目改名会迁移产物目录，sidecar 里的 slug 与报告路径会被同步纠正，
    而内存中仍可能是迁移前的旧路径（会导致报告页空白、下载 404）。
    """
    job = get_job(job_id)
    disk = load_run(_store(), job_id) or {}
    if job is None and not disk:
        return None
    md_path = next(
        (
            p
            for p in (disk.get("markdown_path"), job.markdown_path if job else None)
            if p and Path(p).exists()
        ),
        None,
    )
    return {
        "job_id": job_id,
        "slug": disk.get("slug") or (job.slug if job else None),
        "status": (job.status if job else None) or disk.get("status"),
        "started_at": disk.get("started_at") or (job.started_at if job else None),
        "finished_at": (job.finished_at if job else None) or disk.get("finished_at"),
        "verdict": (job.verdict if job else None) or disk.get("verdict"),
        "summary": (job.summary if job else None) or disk.get("summary") or {},
        "steps_log": (job.steps_log if job else None) or disk.get("steps_log") or [],
        "markdown_path": md_path,
        "llm": (job.llm if job else None) or disk.get("llm") or {},
        "error": (job.error if job else None) or disk.get("error"),
    }


@app.get("/api/jobs/{job_id}")
def api_job_status(job_id: str):
    view = _job_view(job_id)
    if view is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return view


@app.get("/api/projects/{slug}/runs")
def api_project_runs(slug: str):
    """某项目的评审历史（来自磁盘，重启后可用）。"""
    return {"runs": list_runs(_store(), slug)}


@app.get("/api/runs")
def api_list_runs(limit: int = 50):
    """全部项目的评审报告（列表页用，按完成时间倒序）。

    项目名称从项目配置取；项目已删但产物目录还在的，标 `orphan=True` 继续展示，
    避免"报告还在页面上却找不到"的困惑。
    """
    store = _store()
    names = {p["slug"]: p["name"] for p in list_projects(store)}
    runs: list[dict] = []
    if store.is_dir():
        for d in sorted(store.iterdir()):
            if not d.is_dir():
                continue
            for r in list_runs(store, d.name):
                r.setdefault("slug", d.name)
                r["project_name"] = names.get(d.name) or d.name
                r["orphan"] = d.name not in names
                runs.append(r)
    runs.sort(
        key=lambda r: r.get("finished_at") or r.get("started_at") or "", reverse=True
    )
    return {"runs": runs[: max(limit, 0) or 50]}


def _json_safe(value: Any, _depth: int = 0) -> Any:
    """把载荷里的不可序列化对象降级成字符串，避免一帧坏数据打断整条 SSE 流。

    SSE 是**单条长连接**：`json.dumps` 抛 TypeError 会沿生成器一路冒泡，
    把整个 stream 打断（页面从此再收不到任何进度）。与其让一处笔误搞挂整次
    评审的实时通道，不如把该字段降级为 `str(...)` 并落一条 warning 便于复盘。
    """
    if _depth > 6:
        return str(value)
    try:
        json.dumps(value, ensure_ascii=False)
    except TypeError:
        pass
    else:
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v, _depth + 1) for v in value]
    logger.warning("SSE 载荷含不可 JSON 序列化的字段，已降级为字符串：%r", type(value))
    return str(value)


def _sse_frame(event: str, data: dict) -> str:
    return (
        f"event: {event}\n"
        f"data: {json.dumps(_json_safe(data), ensure_ascii=False)}\n\n"
    )


def _job_snapshot(job) -> dict:
    """一次 SSE 连接开始时的全量快照，保证晚到的/重连的客户端不丢进度。"""
    view = _job_view(job.job_id) or {}
    doneRec = job.status in ("done", "error")
    html = ""
    md = view.get("markdown_path")
    if doneRec and md:
        import markdown

        try:
            html = color_severity(
                wrap_tables(
                    markdown.markdown(
                        Path(md).read_text(encoding="utf-8"),
                        extensions=["tables", "fenced_code", "toc"],
                    )
                )
            )
        except Exception:  # pragma: no cover - 读不到就退化为空，页面会提示
            html = ""
    return {
        "job_id": job.job_id,
        "slug": view.get("slug") or job.slug,
        "status": job.status,
        "steps": view.get("steps_log") or job.steps_log,
        "verdict": view.get("verdict") or job.verdict,
        "summary": view.get("summary") or job.summary or {},
        "llm": view.get("llm") or job.llm or {},
        "error": view.get("error") or job.error,
        "finished_at": view.get("finished_at") or job.finished_at,
        "report_html": html,
        "skip_reasons": job.skip_reasons if hasattr(job, "skip_reasons") else {},
        # 断线重连时还原进度条位置（仅观测性，不入报告）
        "progress": job.progress if hasattr(job, "progress") else {},
        # 各节点的真实执行状态（running/waiting/done/error）：重进页面时按它
        # 精确上色，而不是把所有已见节点一律猜成 done
        "node_states": dict(getattr(job, "node_states", {}) or {}),
        # 断线重连时重建 DAG：含步骤 2 各章子节点（仅靠 [步骤N.M] 日志无法还原子节点）
        "nodes": sorted(getattr(job, "seen_nodes", [])),
        # 各节点的中文步骤名（含章节标题），重连时前端据此把子节点显示成「编码规范」而非节点 slug
        "labels": getattr(job, "node_labels", {}) or {},
    }


@app.get("/api/jobs/{job_id}/stream")
def api_job_stream(job_id: str):
    """评审过程流式接口（Server-Sent Events）。

    事件序列：`snapshot`（全量快照）→ 若干 `step` / `report` → `done` 或 `error`。
    任务是历史重放（服务重启过）时，只发 snapshot + 终态事件即关闭。
    """
    job = load_or_replay_job(job_id, _store())
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    def gen():
        yield _sse_frame("snapshot", _job_snapshot(job))
        if job.status in ("done", "error"):
            yield _sse_frame(
                "error" if job.status == "error" else "done",
                {
                    "status": job.status,
                    "verdict": job.verdict,
                    "summary": job.summary,
                    "error": job.error,
                    "finished_at": job.finished_at,
                },
            )
            return

        q = job.subscribe()
        try:
            while True:
                try:
                    ev = q.get(timeout=15)
                except Exception:  # queue.Empty - 空闲则发注释帧保活
                    yield ": keepalive\n\n"
                    continue
                yield _sse_frame(ev["kind"], ev["payload"])
                if ev["kind"] in ("done", "error"):
                    break
        finally:
            job.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 关掉 nginx 缓冲，否则前端收不到增量
        },
    )


@app.get("/api/runs/{job_id}/markdown")
def api_run_markdown(job_id: str):
    """下载某次评审的报告原文（Markdown）。"""
    view = _job_view(job_id)
    md_path = (view or {}).get("markdown_path")
    if not md_path or not Path(md_path).exists():
        raise HTTPException(status_code=404, detail="报告文件不存在（评审未完成或失败）")
    return PlainTextResponse(
        Path(md_path).read_text(encoding="utf-8"),
        headers={"Content-Disposition": f'attachment; filename="review-{job_id}.md"'},
        media_type="text/markdown; charset=utf-8",
    )


@app.get("/report/{slug}/{job_id}", response_class=HTMLResponse)
def report_page(request: Request, slug: str, job_id: str):
    import markdown

    view = _job_view(job_id)
    if view is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    status = view["status"]
    verdict = view["verdict"]
    summary = view["summary"]
    error = view["error"]
    llm_info = view["llm"]
    md_path = view["markdown_path"]

    html = ""
    if md_path and Path(md_path).exists():
        md_text = Path(md_path).read_text(encoding="utf-8")
        html = color_severity(
            wrap_tables(
                markdown.markdown(
                    md_text, extensions=["tables", "fenced_code", "toc"]
                )
            )
        )
    return _templates.TemplateResponse(
        request,
        "report.html",
        {
            "slug": slug,
            "job_id": job_id,
            "status": status,
            "verdict": verdict,
            "summary": summary,
            "error": error,
            "llm": llm_info,
            "html": html,
        },
    )
