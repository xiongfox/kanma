"""运行器注册表与测试工作台准备。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from kanma.models.schemas import CodeLanguage, ProjectConfig, SourceRepo, _safe_name
from kanma.runners.base import LanguageRunner
from kanma.runners.go import GoRunner
from kanma.runners.java import JavaRunner
from kanma.runners.js import JsRunner
from kanma.runners.python import PythonRunner

__all__ = [
    "LanguageRunner",
    "get_runner",
    "detect_runner",
    "prepare_workspace",
    "PythonRunner",
    "JavaRunner",
    "GoRunner",
    "JsRunner",
]


def get_runner(lang: CodeLanguage) -> LanguageRunner:
    if lang is CodeLanguage.JAVA:
        return JavaRunner()
    if lang is CodeLanguage.GO:
        return GoRunner()
    if lang in (CodeLanguage.JAVASCRIPT, CodeLanguage.TYPESCRIPT):
        return JsRunner(lang)
    return PythonRunner()


# 语言未知时按构建文件探测的顺序
_PROBE_ORDER = [
    (CodeLanguage.GO, "go.mod"),
    (CodeLanguage.JAVA, "pom.xml"),
    (CodeLanguage.JAVA, "build.gradle"),
    (CodeLanguage.JAVASCRIPT, "package.json"),
]


def detect_runner(workspace: Path, hint: CodeLanguage | None = None) -> LanguageRunner:
    """优先按 hint，其次按工作台里的构建文件探测。"""
    ws = Path(workspace)
    if hint and hint is not CodeLanguage.UNKNOWN:
        runner = get_runner(hint)
        # hint 与工作台不符时（如 diff 是 .py 但仓库是 Maven 项目），以构建文件为准
        if runner.detect(ws) or not any((ws / f).exists() for _, f in _PROBE_ORDER):
            return runner
    for lang, marker in _PROBE_ORDER:
        if (ws / marker).exists():
            return get_runner(lang)
    if any(ws.glob("*.py")) or (ws / "pyproject.toml").exists():
        return PythonRunner()
    return PythonRunner()


def _copy_tree_resilient(
    src: Path, dst: Path, ignore_fn, skipped: list[str]
) -> None:
    """逐文件复制，单个文件失败（敏感内容拒访、权限不足等）只跳过不中断。

    背景：沙箱环境的内容拦截 shim 会代理 shutil.copytree，仓库里若含密钥类
    文件（如 TrustMerchant.properties），整次 copytree 会聚合抛 shutil.Error，
    导致整个评审中断。这类文件本就不该进测试工作台——跳过并记录即可。
    """
    for root, dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        dirs[:] = [d for d in dirs if d not in ignore_fn(root, dirs)]
        target_dir = dst / rel_root
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            if name in ignore_fn(root, [name]):
                continue
            try:
                shutil.copy2(Path(root) / name, target_dir / name)
            except Exception:
                # 不读内容、不打日志（防敏感名泄漏进日志也尽量精简），只记相对路径
                skipped.append(str(rel_root / name) if str(rel_root) != "." else name)


def prepare_workspace(
    cfg: ProjectConfig, repo: SourceRepo, skipped: list[str] | None = None
) -> Path:
    """把某个源码工程的克隆副本复制为「测试工作台」，测试代码只写在这里（HR-03）。

    返回工作台路径。仓库不可用时返回空 Path，调用方需据此降级。
    `skipped` 传入一个可变列表时，复制中被跳过的文件（相对路径）会追加进去，
    调用方应据此写降级说明——跳过个别不可读文件不算失败，但必须可观测。
    """
    # 工程名可能含 / : 等路径不安全字符，用与克隆目录一致的规则清洗
    workspace = cfg.tmp_dir / "workspace" / _safe_name(repo.display_name)
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    repo_dir = cfg.repo_dir_for(repo)
    if not repo_dir.exists():
        return Path()
    workspace.parent.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns(".git", "node_modules", "target")
    try:
        shutil.copytree(repo_dir, workspace, ignore=ignore)
    except (shutil.Error, OSError):
        # 快速路径失败（典型：沙箱对敏感文件拒访，copytree 聚合报错）。
        # 清掉半成品后逐文件重拷：正常文件照常进工作台，个别文件跳过并记录。
        shutil.rmtree(workspace, ignore_errors=True)
        out = skipped if skipped is not None else []
        _copy_tree_resilient(repo_dir, workspace, ignore, out)
    return workspace
