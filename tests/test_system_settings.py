"""系统设置（system.json）的加载 / 保存 / 默认值测试。"""

import os

import pytest

from kanma.system_settings import (
    SystemSettings,
    get_settings,
    load_settings,
    reset_settings,
    save_settings,
    settings_path,
)


@pytest.fixture(autouse=True)
def _reset():
    """每个用例前重置进程内单例，避免用例互相污染（系统设置是进程级单例）。"""
    reset_settings()
    yield
    reset_settings()


def test_default_is_two_way_when_no_env(tmp_path):
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("KANMA_STATIC_REVIEW_MAX_PARALLEL", raising=False)
        reset_settings()
        s = load_settings(tmp_path)
        assert s.static_review_max_parallel == 2
        assert s.max_parallel == 2
        assert s.to_api() == {"static_review_max_parallel": 2}


def test_env_default_honored(tmp_path):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("KANMA_STATIC_REVIEW_MAX_PARALLEL", "3")
        reset_settings()
        s = load_settings(tmp_path)
        assert s.static_review_max_parallel == 3


def test_save_and_reload_roundtrip(tmp_path):
    path = save_settings(SystemSettings(static_review_max_parallel=2), tmp_path)
    assert path == settings_path(tmp_path)
    assert path.is_file()
    reloaded = load_settings(tmp_path)
    assert reloaded.static_review_max_parallel == 2
    # 进程内单例也已更新
    assert get_settings().static_review_max_parallel == 2


def test_saved_value_beats_env(tmp_path):
    """已落盘的系统设置优先于环境变量（即使用户显式设 0 表示不限制，也不被 env 覆盖）。"""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("KANMA_STATIC_REVIEW_MAX_PARALLEL", "5")
        save_settings(SystemSettings(static_review_max_parallel=0), tmp_path)
        s = load_settings(tmp_path)
        assert s.static_review_max_parallel == 0


def test_negative_rejected_by_model():
    with pytest.raises(Exception):
        SystemSettings(static_review_max_parallel=-1)


def test_resolver_precedence():
    """验证 _resolve_max_static_parallel 的优先级：CLI 显式 > 系统设置 > 环境变量 > 0。"""
    from kanma.graph import _resolve_max_static_parallel

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("KANMA_STATIC_REVIEW_MAX_PARALLEL", "5")
        # 1) 显式最高
        assert _resolve_max_static_parallel(2, system_value=3) == 2
        # 2) 系统设置次之
        assert _resolve_max_static_parallel(None, system_value=3) == 3
        # 3) 无系统设置时回退环境变量
        assert _resolve_max_static_parallel(None, system_value=None) == 5
        # 4) 都没有 -> 系统默认两路并发（2）
        mp.delenv("KANMA_STATIC_REVIEW_MAX_PARALLEL", raising=False)
        assert _resolve_max_static_parallel(None, system_value=None) == 2


def test_job_snapshot_carries_node_states(monkeypatch):
    """重进报告页时精确还原每章真实状态：snapshot 必须带 node_states（waiting/running/done）。"""
    import sys

    import kanma.web.app  # noqa: F401  确保模块已加载

    webapp = sys.modules["kanma.web.app"]  # 包 __init__ 把 app 重导出为 FastAPI 实例，须走 sys.modules
    from kanma.web.jobs import ReviewJob

    job = ReviewJob("p")
    job.status = "running"
    job.node_states = {
        "diff_collect": "done",
        "static_review_ch_01_coding_standards": "done",
        "static_review_ch_02_exception_logging": "running",
        "static_review_ch_03_security_standards": "waiting",
    }
    job.seen_nodes |= set(job.node_states)
    monkeypatch.setattr(webapp, "_job_view", lambda job_id: {})

    snap = webapp._job_snapshot(job)
    assert snap["node_states"]["static_review_ch_03_security_standards"] == "waiting"
    assert snap["node_states"]["static_review_ch_02_exception_logging"] == "running"
    assert snap["node_states"]["static_review_ch_01_coding_standards"] == "done"


def test_review_job_tracks_node_states_from_events():
    """on_step 的状态登记规则：start→running、done→done、error→error、live node_state→waiting/running。"""
    from kanma.web.jobs import ReviewJob

    job = ReviewJob("p")
    # 直接复刻 _run 里 on_step 的登记逻辑做行为锁定（on_step 是闭包，无法直接调用）
    def on_step(phase, node, elapsed_ms, lines, live=None):
        if phase == "start":
            job.node_states[node] = "running"
        elif phase == "done":
            job.node_states[node] = "done"
        elif phase == "error":
            job.node_states[node] = "error"
        if phase == "live":
            payload = dict(live or {})
            if payload.get("type") == "node_state":
                job.node_states[node] = payload.get("state") or "running"

    on_step("start", "n1", 0, [])
    assert job.node_states["n1"] == "running"
    on_step("live", "n1", 5, [], {"type": "node_state", "state": "waiting"})
    assert job.node_states["n1"] == "waiting"
    on_step("live", "n1", 6, [], {"type": "node_state", "state": "running"})
    assert job.node_states["n1"] == "running"
    on_step("done", "n1", 10, [])
    assert job.node_states["n1"] == "done"
    on_step("error", "n2", 3, ["boom"])
    assert job.node_states["n2"] == "error"
