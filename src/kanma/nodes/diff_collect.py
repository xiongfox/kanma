"""步骤 1 · 采集 diff。

对每个源码工程：克隆 → 按时间窗筛 commit → 聚合 diff → 解析为 DiffBundle。
多工程时分别采集，逐份存入 state.diffs。全部时间窗内无 commit 时终止流程
（后续节点据此直接跳到报告）。
"""

from __future__ import annotations

from kanma.models.schemas import DiffBundle
from kanma.nodes.step_log import StepLog
from kanma.state import ReviewState
from kanma.tools.git_tool import GitError, collect_diff_bundle


def diff_collect(state: ReviewState) -> dict:
    cfg = state["config"]
    diffs: list[DiffBundle] = []
    log = StepLog(1)
    notes: list[str] = []

    total_repos = len(cfg.repos)
    for idx, repo in enumerate(cfg.repos, start=1):
        # 采集前先报进度：克隆大仓耗时最久，用户需要知道"正在第几个"
        log.progress(idx - 1, total_repos, f"克隆并采集 {repo.display_name}")
        try:
            diff = collect_diff_bundle(cfg, repo)
        except GitError as e:
            diff = DiffBundle(
                project=cfg.name,
                repo_name=repo.display_name,
                branch=repo.branch,
                degraded_reason=f"采集 diff 失败：{e}",
            )
        diffs.append(diff)
        log.add(
            f"工程「{repo.display_name}」分支 {repo.branch}，时间窗 "
            f"{repo.start_time:%Y-%m-%d %H:%M} ~ {repo.end_time:%Y-%m-%d %H:%M}"
        )
        if diff.degraded_reason:
            log.add(f"终止：{repo.display_name} {diff.degraded_reason}")
            notes.append(f"步骤1 降级（{repo.display_name}）：{diff.degraded_reason}")
        else:
            if diff.readme:
                log.add(
                    f"工程「{repo.display_name}」已读取 README（{len(diff.readme)} 字摘要），"
                    f"供步骤2 建立项目上下文"
                )
            else:
                log.add(
                    f"工程「{repo.display_name}」未提供 README，步骤2 将在无项目上下文下审查"
                )
            log.add(
                f"工程「{repo.display_name}」命中 {len(diff.commits)} 个 commit，"
                f"{len(diff.reviewable_files)} 个可评审文件，"
                f"+{diff.total_added}/-{diff.total_removed} 行，"
                f"跳过 {len(diff.skipped_files)} 个非源码文件"
            )
        log.progress(idx, total_repos, f"已采集 {repo.display_name}")

    return {"diffs": diffs, "steps_log": log.lines, "degradation_notes": notes}
