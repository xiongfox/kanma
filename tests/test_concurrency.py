"""评审并发互斥的回归测试。

锁定两条规则：
1. 同一时刻只有一个评审（跨项目全局串行），第二个立即 BusyError；
2. 任务完成后互斥自动解除；
3. active_job() 是"后台是否还在跑"的唯一真相。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from kanma.models.schemas import ProjectConfig, SourceRepo
from kanma.web import jobs as jobs_mod


def _cfg(name: str) -> ProjectConfig:
    return ProjectConfig(
        name=name,
        repos=[
            SourceRepo(
                name="r",
                git_url="git@x/y.git",
                branch="main",
                start_time=datetime(2026, 1, 1),
                end_time=datetime(2026, 1, 2),
            )
        ],
    )


@pytest.fixture(autouse=True)
def _clean_jobs():
    """每个用例前后清空全局任务表，避免用例间互相污染。"""
    saved = dict(jobs_mod._JOBS)
    jobs_mod._JOBS.clear()
    yield
    jobs_mod._JOBS.clear()
    jobs_mod._JOBS.update(saved)


@pytest.fixture
def no_run(monkeypatch):
    """让 start_review 不起真实评审线程（用例自己控制任务状态）。"""
    monkeypatch.setattr(jobs_mod, "_run", lambda *a, **k: None)


def test_second_project_rejected_while_first_runs(no_run, tmp_path: Path):
    j1 = jobs_mod.start_review("proj-a", _cfg("A"), tmp_path)
    assert j1.status == "queued"
    assert jobs_mod.active_job() is j1

    # 不同项目也要被拦 —— 这是"全局串行"的核心
    with pytest.raises(jobs_mod.BusyError) as exc:
        jobs_mod.start_review("proj-b", _cfg("B"), tmp_path)
    assert exc.value.job.slug == "proj-a"
    assert exc.value.job.job_id == j1.job_id
    # 失败后不应有残留的新任务
    assert len(jobs_mod._JOBS) == 1


def test_same_project_rejected_while_running(no_run, tmp_path: Path):
    jobs_mod.start_review("proj-a", _cfg("A"), tmp_path)
    with pytest.raises(jobs_mod.BusyError):
        jobs_mod.start_review("proj-a", _cfg("A"), tmp_path)


def test_lock_released_after_finish(no_run, tmp_path: Path):
    j1 = jobs_mod.start_review("proj-a", _cfg("A"), tmp_path)
    j1.status = "done"  # 模拟 _run 收尾
    assert jobs_mod.active_job() is None

    j2 = jobs_mod.start_review("proj-b", _cfg("B"), tmp_path)
    assert j2.slug == "proj-b"
    assert jobs_mod.active_job() is j2


def test_error_job_also_releases_lock(no_run, tmp_path: Path):
    j1 = jobs_mod.start_review("proj-a", _cfg("A"), tmp_path)
    j1.status = "error"
    assert jobs_mod.active_job() is None
    jobs_mod.start_review("proj-b", _cfg("B"), tmp_path)  # 不应抛


def test_active_job_scoped_by_slug(no_run, tmp_path: Path):
    jobs_mod.start_review("proj-a", _cfg("A"), tmp_path)
    assert jobs_mod.active_job("proj-a") is not None
    assert jobs_mod.active_job("proj-z") is None


def test_concurrent_starts_only_one_wins(no_run, tmp_path: Path):
    """并发启动：检查+注册必须在同一锁内，不允许两个都成功。"""
    import threading

    winners: list[str] = []

    def attempt(slug: str) -> None:
        try:
            jobs_mod.start_review(slug, _cfg(slug), tmp_path)
            winners.append(slug)
        except jobs_mod.BusyError:
            pass

    ts = [threading.Thread(target=attempt, args=(f"p{i}",)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert len(winners) == 1, f"应只有一个胜出，实际：{winners}"
    assert len(jobs_mod._JOBS) == 1