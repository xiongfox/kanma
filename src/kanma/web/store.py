"""项目配置的持久化：保存为 YAML，供 `kanma review -c` 直接消费。"""

from __future__ import annotations

import json
import shutil
import yaml
from pathlib import Path

from kanma.config import dump_project_config, load_project_config, slugify
from kanma.models.schemas import ProjectConfig


def project_path(slug: str, store_dir: Path) -> Path:
    return store_dir / f"{slug}.yaml"


def save_project(cfg: ProjectConfig, store_dir: Path) -> Path:
    """保存（或覆盖）一个项目配置，返回落盘路径。"""
    slug = slugify(cfg.name)
    return dump_project_config(cfg, project_path(slug, store_dir))


def list_projects(store_dir: Path) -> list[dict]:
    """列出已保存项目（含多工程信息，不含需求正文等重字段）。"""
    if not store_dir.exists():
        return []
    items: list[dict] = []
    for p in sorted(store_dir.glob("*.yaml")):
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:  # pragma: no cover - 损坏文件跳过
            continue
        repos_raw = raw.get("repos") or ([raw["repo"]] if raw.get("repo") else [])
        repos = [
            {
                "name": r.get("name", ""),
                "git_url": r.get("git_url", ""),
                "branch": r.get("branch", ""),
                "base": r.get("base"),
                "start_time": str(r.get("start_time", "")),
                "end_time": str(r.get("end_time", "")),
            }
            for r in repos_raw
        ]
        first = repos[0] if repos else {}
        items.append(
            {
                "slug": p.stem,
                "name": raw.get("name", p.stem),
                "repos": repos,
                "repo_count": len(repos),
                "git_url": first.get("git_url", ""),
                "branch": first.get("branch", ""),
                "base": first.get("base"),
                "start_time": first.get("start_time", ""),
                "end_time": first.get("end_time", ""),
                "requirement_doc": raw.get("requirement_doc"),
                "updated": p.stat().st_mtime,
            }
        )
    items.sort(key=lambda x: x["updated"], reverse=True)
    return items


def load_project(slug: str, store_dir: Path) -> ProjectConfig | None:
    """按 slug 读取项目配置；不存在返回 None。"""
    p = project_path(slug, store_dir)
    if not p.exists():
        return None
    return load_project_config(p)


def migrate_project(old_slug: str, new_slug: str, store_dir: Path) -> None:
    """项目改名后迁移产物：旧 runs 目录搬到新 slug 下，并回写 sidecar 里的 slug。

    slug 由项目名推导，改名即换 slug。若不做迁移，历史评审会留在旧目录下取不到，
    页面上表现为"项目改名后历史全部消失"。
    """
    if old_slug == new_slug:
        return

    old_dir = store_dir / old_slug
    new_dir = store_dir / new_slug
    if old_dir.is_dir():
        if not new_dir.exists():
            old_dir.rename(new_dir)
        else:  # 新 slug 目录已存在（极少见）：逐文件合并，重名以旧产物为准
            for item in old_dir.rglob("*"):
                rel = item.relative_to(old_dir)
                target = new_dir / rel
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                elif not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    item.replace(target)
            shutil.rmtree(old_dir, ignore_errors=True)

    # 旧配置文件的活已被新 slug 的文件接走，删掉它
    old_yaml = project_path(old_slug, store_dir)
    if old_yaml.exists():
        old_yaml.unlink()

    # sidecar 里记的 slug 与报告路径仍指向旧目录，逐一纠正，
    # 否则历史条目会显示"报告文件不存在"。
    old_prefix = str(old_dir)
    new_prefix = str(new_dir)
    for sidecar in (new_dir / "runs").glob("*.json"):
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:  # pragma: no cover - 损坏文件跳过
            continue
        changed = False
        if data.get("slug") != new_slug:
            data["slug"] = new_slug
            changed = True
        md = data.get("markdown_path") or ""
        if md.startswith(old_prefix):
            data["markdown_path"] = new_prefix + md[len(old_prefix):]
            changed = True
        if changed:
            sidecar.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )


def delete_project(slug: str, store_dir: Path) -> bool:
    """删除项目配置及其评审产物目录。"""
    p = project_path(slug, store_dir)
    if not p.exists():
        return False
    p.unlink()
    runs = store_dir / slug
    if runs.is_dir():
        import shutil

        shutil.rmtree(runs, ignore_errors=True)
    return True


def delete_run(store_dir: Path, job_id: str) -> bool:
    """删除单份评审报告：sidecar JSON + 关联的 markdown 报告文件。

    返回 True 表示找到并删除；找不到返回 False。删除运行中的报告由调用方拦截。
    """
    if not store_dir.is_dir():
        return False
    for sidecar in store_dir.glob("*/runs/*.json"):
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:  # pragma: no cover - 损坏文件跳过
            continue
        if data.get("job_id") != job_id:
            continue
        md = data.get("markdown_path")
        if md and Path(md).exists():
            try:
                Path(md).unlink()
            except OSError:
                pass
        try:
            sidecar.unlink()
        except OSError:
            return False
        return True
    return False


def clear_runs(store_dir: Path, slug: str, skip_job_ids: set[str] | None = None) -> int:
    """清空某项目的全部评审报告，返回实际删除的份数。

    `skip_job_ids` 用于保护正在进行的评审（不删除 running/queued 的 sidecar）。
    空目录一并移除。
    """
    skip_job_ids = skip_job_ids or set()
    runs_dir = store_dir / slug / "runs"
    if not runs_dir.is_dir():
        return 0
    count = 0
    for sidecar in list(runs_dir.glob("*.json")):
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if data.get("job_id") in skip_job_ids:
            continue
        md = data.get("markdown_path")
        if md and Path(md).exists():
            try:
                Path(md).unlink()
            except OSError:
                pass
        try:
            sidecar.unlink()
            count += 1
        except OSError:
            pass
    # 报告清光后，runs 目录本身没价值了，清掉避免遗留空目录
    try:
        runs_dir.rmdir()
    except OSError:
        pass
    return count
