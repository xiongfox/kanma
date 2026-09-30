"""diff 采集与解析测试。"""

from datetime import datetime, timedelta

import pytest

from kanma.models.schemas import CodeLanguage, ProjectConfig, SourceRepo
from kanma.rules.scan import iter_file_added_lines
from kanma.tools.git_tool import collect_diff_bundle, parse_diff


def test_window_filters_commits(sample_repo, tmp_path, sample_config):
    """时间窗外的 commit 不应被纳入。"""
    import subprocess

    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "out of window"],
        cwd=str(sample_repo),
        capture_output=True,
        check=True,
    )

    # 时间窗收紧到"昨天之前"，应采不到任何 commit
    cfg = sample_config.model_copy(deep=True)
    cfg.repos[0].start_time = datetime.now() - timedelta(days=10)
    cfg.repos[0].end_time = datetime.now() - timedelta(days=5)
    diff = collect_diff_bundle(cfg)
    assert diff.is_empty
    assert diff.degraded_reason, "空 diff 必须写明原因"


def test_hunk_line_numbers_map_to_new_file(sample_config):
    diff = collect_diff_bundle(sample_config)
    fd = diff.reviewable_files[0]
    assert fd.language is CodeLanguage.PYTHON
    assert fd.added_lines > 0
    assert fd.hunks
    hunk = fd.hunks[0]
    assert hunk.new_start >= 1
    assert len(hunk.added_line_numbers) == fd.added_lines


def test_non_source_files_are_skipped(sample_repo, tmp_path, sample_config):
    import subprocess

    (sample_repo / "notes.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(sample_repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: notes"],
        cwd=str(sample_repo),
        check=True,
        capture_output=True,
    )
    diff = collect_diff_bundle(sample_config)
    assert "notes.txt" in diff.skipped_files
    assert all(not f.path.endswith(".txt") for f in diff.reviewable_files)


def _parse_one(diff_text: str):
    fd = parse_diff(diff_text)["a.py"]
    hunk = fd.hunks[0]
    return hunk, [(a.line, a.text) for a in iter_file_added_lines(fd)]


def test_hunk_new_start_parsed_from_header():
    """HR-02：new_start 必须来自 hunk 头，解析失败会让整份报告行号错位。"""
    hunk, added = _parse_one(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -5,3 +5,4 @@\n ctx1\n-old\n+new1\n+new2\n ctx2\n"
    )
    assert hunk.new_start == 5
    assert hunk.added_line_numbers == [6, 7]
    assert added == [(6, "new1"), (7, "new2")]


def test_new_file_starts_at_line_one():
    hunk, added = _parse_one(
        "diff --git a/a.py b/a.py\nnew file mode 100644\n--- /dev/null\n+++ b/a.py\n"
        "@@ -0,0 +1,3 @@\n+x=1\n+y=2\n+z=3\n"
    )
    assert hunk.new_start == 1
    assert hunk.added_line_numbers == [1, 2, 3]
    assert added == [(1, "x=1"), (2, "y=2"), (3, "z=3")]


def test_single_line_hunk_without_comma():
    """`@@ -20 +20 @@` 这种省略行数的头也必须解析正确。"""
    hunk, added = _parse_one(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -20 +20 @@\n-old\n+new\n"
    )
    assert hunk.new_start == 20
    assert hunk.added_line_numbers == [20]
    assert added == [(20, "new")]


def test_missing_window_returns_degraded(sample_config):
    cfg = sample_config.model_copy(deep=True)
    cfg.repos = [
        SourceRepo(
            git_url=cfg.repos[0].git_url,
            branch="main",
            start_time=datetime.now() + timedelta(days=3),
            end_time=datetime.now() + timedelta(days=5),
        )
    ]
    diff = collect_diff_bundle(cfg)
    assert diff.is_empty
    assert "无" in diff.degraded_reason


# ---------------------------------------------------------------------------
# 多源码工程
# ---------------------------------------------------------------------------


def test_multi_repo_collects_each_repo_independently(multi_repo_config):
    """每个源码工程各自采集一份 diff，且互不覆盖（按工程名隔离克隆目录）。"""
    from kanma.nodes.diff_collect import diff_collect

    state = diff_collect({"config": multi_repo_config})
    diffs = state["diffs"]
    assert len(diffs) == 2
    names = {d.repo_name for d in diffs}
    assert names == {"order-service", "pay-service"}

    by_name = {d.repo_name: d for d in diffs}
    assert not by_name["order-service"].is_empty
    assert not by_name["pay-service"].is_empty
    assert any(f.path == "pay_service.py" for f in by_name["pay-service"].files)
    assert all(f.path != "pay_service.py" for f in by_name["order-service"].files)


def test_multi_repo_clone_dirs_are_isolated(multi_repo_config):
    """两个工程的克隆目录必须分开，否则后采集的会覆盖前一个。"""
    dirs = {multi_repo_config.repo_dir_for(r) for r in multi_repo_config.repos}
    assert len(dirs) == 2


def test_multi_repo_requires_at_least_one_repo(sample_repo, tmp_path):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ProjectConfig(name="empty", repos=[], workdir=tmp_path / "w")
