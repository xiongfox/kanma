"""Git 操作封装：克隆、按时间窗筛 commit、生成并解析聚合 diff。

时间窗是划分 diff 的首要依据（AGENTS.MD 3.1）：取 [start_time, end_time] 内
落在目标分支上的 commit 集合，合并为聚合 diff；与 commit 范围冲突时以时间窗为准。
"""

from __future__ import annotations

import fnmatch
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from kanma.models.schemas import CodeLanguage, DiffBundle, DiffHunk, FileDiff, SourceRepo
from kanma.utils import truncate

#: README 摘要上限（与需求文档截断同量级，避免大 README 撑爆上下文）
README_LIMIT = 6000


class GitError(RuntimeError):
    pass


def _run(args: list[str], cwd: Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} 失败: {proc.stderr.strip()}")
    return proc.stdout


def git_available() -> bool:
    return shutil.which("git") is not None


def prepare_repo(repo_url: str, branch: str, dest: Path) -> Path:
    """浅克隆目标分支到 dest。已存在则复用并 fetch（便于本地反复评审）。"""
    dest = dest.expanduser()
    if dest.exists() and (dest / ".git").exists():
        _run(["fetch", "--all", "--prune"], cwd=dest, check=False)
    else:
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _run(["clone", "--filter=blob:none", "--no-checkout", repo_url, str(dest)])
    # 确保本地有该分支并检出
    _run(["checkout", "-B", branch, f"origin/{branch}"], cwd=dest, check=False)
    _run(["checkout", branch], cwd=dest, check=False)
    return dest


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S%z")


def commits_in_window(repo: Path, branch: str, start: datetime, end: datetime) -> list[str]:
    """取时间窗内、在指定分支上的 commit sha（按时间升序）。"""
    out = _run(
        [
            "log",
            branch,
            "--no-merges",
            f"--since={_iso(start)}",
            f"--until={_iso(end)}",
            "--format=%H",
            "--reverse",
        ],
        cwd=repo,
        check=False,
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def aggregate_diff(repo: Path, commits: list[str], base: str | None = None) -> str:
    """生成 commit 集合的聚合 diff。

    有 base 时对比 base..最后一个 commit；否则对每个 commit 生成 diff 再拼接
    （首个 commit 用 `git show` 以包含其初始内容）。
    """
    if not commits:
        return ""
    if base:
        return _run(["diff", f"{base}..{commits[-1]}"], cwd=repo, check=False)

    parts: list[str] = []
    for i, sha in enumerate(commits):
        if i == 0:
            parts.append(_run(["show", "--format=", "--patch", sha], cwd=repo, check=False))
        else:
            parts.append(_run(["diff", f"{commits[i - 1]}..{sha}"], cwd=repo, check=False))
    return "\n".join(parts)


# @@ -5,3 +5,4 @@ —— 必须整体正则解析：手工 split 会把 "4 @@" 丢给 int() 而抛异常
_HUNK_HEADER = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _parse_hunk(header: str, body: list[str]) -> DiffHunk:
    """解析单个 hunk，记录**新文件侧**的增/删行号（报告定位用）。

    new_start 是行号推算的基准，解析失败会导致整份报告的行号全部错位（HR-02）。
    """
    new_start = 0
    new_lines = 0
    m = _HUNK_HEADER.search(header)
    if m:
        new_start = int(m.group(1))
        new_lines = int(m.group(2)) if m.group(2) else 1

    added: list[int] = []
    removed: list[int] = []
    cursor = new_start
    # body 首行是 "@@ ..." 头，不参与行号推算，否则所有行号会整体偏移 1（HR-02）
    lines = body[1:] if body and body[0].startswith("@@") else body
    for line in lines:
        if line.startswith("+") and not line.startswith("+++"):
            added.append(cursor)
            cursor += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(cursor)
        elif line.startswith("\\"):
            continue
        else:
            cursor += 1

    return DiffHunk(
        header=header,
        new_start=new_start,
        new_lines=new_lines,
        added_line_numbers=added,
        removed_line_numbers=removed,
        content="\n".join(body),
    )


def parse_diff(diff_text: str) -> dict[str, FileDiff]:
    """把 unified diff 文本解析为以路径为 key 的 FileDiff 集合。"""
    files: dict[str, FileDiff] = {}
    current: FileDiff | None = None
    hunk_header: str | None = None
    hunk_body: list[str] = []

    def flush_hunk() -> None:
        if current is not None and hunk_header:
            current.hunks.append(_parse_hunk(hunk_header, hunk_body))

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("diff --git"):
            flush_hunk()
            hunk_header, hunk_body = None, []
            path = raw_line.split(" b/")[-1].strip()
            current = FileDiff(path=path)
            files[path] = current
            continue
        if current is None:
            continue
        if raw_line.startswith("new file mode"):
            current.change_type = "added"
        elif raw_line.startswith("deleted file mode"):
            current.change_type = "deleted"
        elif raw_line.startswith("rename from"):
            current.change_type = "renamed"
        elif raw_line.startswith("Binary files"):
            current.binary = True
        elif raw_line.startswith("@@"):
            flush_hunk()
            hunk_header = raw_line
            hunk_body = [raw_line]
            continue
        elif hunk_header:
            hunk_body.append(raw_line)
            if raw_line.startswith("+"):
                current.added_lines += 1
            elif raw_line.startswith("-"):
                current.removed_lines += 1

    flush_hunk()
    return files


def _is_excluded(path: str, exclude_globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pat) for pat in exclude_globs)


def collect_diff_bundle(cfg, repo: SourceRepo | None = None) -> DiffBundle:
    """步骤 1 的主入口：克隆 → 筛 commit → 聚合 diff → 解析为 DiffBundle。

    按单个源码工程采集；多工程时由 diff_collect 节点逐个调用。
    未显式传入 repo 时取首个源码工程（单工程场景的便捷用法）。
    """
    if not git_available():
        raise GitError("未检测到 git，无法执行步骤 1")
    if repo is None:
        if not cfg.repos:
            raise GitError("未配置任何源码工程，无法执行步骤 1")
        repo = cfg.repos[0]

    repo_dir = prepare_repo(repo.git_url, repo.branch, cfg.repo_dir_for(repo))
    # 顺带抽取该工程 README（步骤 2 建立项目上下文用，AGENTS.MD 步骤 1/2）
    readme = extract_readme(repo_dir)
    commits = commits_in_window(repo_dir, repo.branch, repo.start_time, repo.end_time)
    if not commits:
        return DiffBundle(
            project=cfg.name,
            repo_name=repo.display_name,
            branch=repo.branch,
            base=repo.base,
            start_time=repo.start_time,
            end_time=repo.end_time,
            readme=readme,
            readme_absent=not readme,
            degraded_reason="时间窗内无 commit，无可评审变更",
        )

    diff_text = aggregate_diff(repo_dir, commits, repo.base)
    parsed = parse_diff(diff_text)

    files: list[FileDiff] = []
    skipped: list[str] = []
    for path, fd in parsed.items():
        if _is_excluded(path, cfg.exclude_globs):
            skipped.append(path)
            continue
        suffix = Path(path).suffix.lower()
        if suffix and suffix not in cfg.source_extensions:
            skipped.append(path)
            continue
        fd.language = _detect(suffix)
        if fd.binary:
            fd.skipped_reason = "二进制文件，不做内容评审"
            skipped.append(path)
        files.append(fd)

    return DiffBundle(
        project=cfg.name,
        repo_name=repo.display_name,
        branch=repo.branch,
        base=repo.base,
        start_time=repo.start_time,
        end_time=repo.end_time,
        commits=commits,
        files=files,
        skipped_files=skipped,
        readme=readme,
        readme_absent=not readme,
    )


def _detect(suffix: str) -> CodeLanguage:
    from kanma.config import _EXT_LANG_MAP

    return _EXT_LANG_MAP.get(suffix, CodeLanguage.UNKNOWN)


def extract_readme(repo_dir: Path) -> str:
    """抽取工程 README 摘要，供步骤 2 建立项目上下文（AGENTS.MD 步骤 1/2）。

    查找优先级：
    1. 根目录 `README.md`（大小写不敏感，含无扩展名的 `README`）；
    2. 根目录下任意 `README*.md`（如 `README-zh.md`、`README_CN.md`）；
    3. 一级子目录里的 `README.md` / `readme.md`。

    命中即读取并截断返回；全都缺失返回空串（调用方据此置 `readme_absent=True`）。
    """
    repo_dir = repo_dir.expanduser()

    # 1) 根目录 README.md（大小写不敏感）
    for name in ("README.md", "readme.md", "README", "readme"):
        p = repo_dir / name
        if p.is_file():
            return _read_readme(p)
    # 2) 任意 README*.md（根目录，按名稳定排序取第一个）
    for p in sorted(repo_dir.glob("README*.md")):
        return _read_readme(p)
    # 3) 一级子目录的 README
    for sub in sorted(p for p in repo_dir.iterdir() if p.is_dir()):
        for name in ("README.md", "readme.md"):
            p = sub / name
            if p.is_file():
                return _read_readme(p)
    return ""


def _read_readme(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    return truncate(text, README_LIMIT)
