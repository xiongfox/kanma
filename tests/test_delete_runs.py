"""delete_run / clear_runs 行为回归测试。

锁定以下不变量：
1. 单删会同时删 sidecar JSON 与 markdown 原文；
2. 清空会遍历整个 runs/，跳过受保护的 running job_id，并清理空目录；
3. 删不存在 / 二次清空 都是幂等的（不抛异常，返回值正确）。
"""
from __future__ import annotations

import json
from pathlib import Path

from kanma.web import store


def _seed(store_dir: Path, slug: str, job_id: str, md_name: str) -> dict:
    """造一份报告：sidecar JSON 指向 markdown 文件。返回 sidecar 内容。"""
    project = store_dir / slug
    runs_dir = project / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    md = project / md_name
    md.write_text(f"# {md_name}", encoding="utf-8")
    sidecar = {
        "job_id": job_id,
        "slug": slug,
        "status": "done",
        "markdown_path": str(md.resolve()),
    }
    (runs_dir / f"{job_id}.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return sidecar


def test_delete_run_removes_sidecar_and_markdown(tmp_path: Path):
    _seed(tmp_path, "p1", "jobA", "A.md")
    _seed(tmp_path, "p1", "jobB", "B.md")

    ok = store.delete_run(tmp_path, "jobA")
    assert ok is True

    runs = tmp_path / "p1" / "runs"
    assert (runs / "jobA.json").exists() is False
    assert (runs / "jobB.json").exists() is True
    assert (tmp_path / "p1" / "A.md").exists() is False
    assert (tmp_path / "p1" / "B.md").exists() is True


def test_delete_run_returns_false_when_missing(tmp_path: Path):
    assert store.delete_run(tmp_path, "nope") is False


def test_clear_runs_removes_all_and_cleans_empty_dir(tmp_path: Path):
    _seed(tmp_path, "p2", "jobA", "A.md")
    _seed(tmp_path, "p2", "jobB", "B.md")

    n = store.clear_runs(tmp_path, "p2")
    assert n == 2

    runs = tmp_path / "p2" / "runs"
    assert runs.exists() is False  # 空目录被自动清理
    assert (tmp_path / "p2" / "A.md").exists() is False
    assert (tmp_path / "p2" / "B.md").exists() is False


def test_clear_runs_skips_protected_job_ids(tmp_path: Path):
    _seed(tmp_path, "p3", "running", "running.md")
    _seed(tmp_path, "p3", "done-1", "done-1.md")
    _seed(tmp_path, "p3", "done-2", "done-2.md")

    n = store.clear_runs(tmp_path, "p3", skip_job_ids={"running"})
    assert n == 2

    runs = tmp_path / "p3" / "runs"
    assert (runs / "running.json").exists() is True, "running 应被保留"
    assert (tmp_path / "p3" / "running.md").exists() is True
    assert (runs / "done-1.json").exists() is False
    assert (runs / "done-2.json").exists() is False


def test_clear_runs_idempotent_when_empty(tmp_path: Path):
    # 第一次：runs 目录不存在
    assert store.clear_runs(tmp_path, "p4") == 0
    # 第二次：清完后再清
    _seed(tmp_path, "p4", "x", "x.md")
    assert store.clear_runs(tmp_path, "p4") == 1
    assert store.clear_runs(tmp_path, "p4") == 0