"""项目配置加载。

支持从 YAML 读取评审项目定义（源码 git 信息 + 需求开发文档），
对应 AGENTS.MD 第 3 节的输入契约。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import yaml

from kanma.models.schemas import CodeLanguage, ProjectConfig, RequirementDoc, SourceRepo

# 简单可用的需求文档文本抽取：md/txt 直接读，pdf 走 pypdf（若安装）
_PDF_SUFFIX = {".pdf"}


def _extract_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:  # pragma: no cover - 可选依赖
        return ""
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def load_requirement(path: str | Path | None) -> RequirementDoc:
    """加载需求开发文档。缺失时返回空文档（走降级路径，不阻断流程）。"""
    if not path:
        return RequirementDoc()
    p = Path(path).expanduser()
    if not p.exists():
        return RequirementDoc(path=p, title="（文档缺失）")
    if p.suffix.lower() in _PDF_SUFFIX:
        text = _extract_pdf_text(p)
    else:
        text = p.read_text(encoding="utf-8", errors="ignore")
    title = p.stem
    for line in text.splitlines():
        if line.strip().startswith("#"):
            title = line.strip().lstrip("#").strip()
            break
    return RequirementDoc(path=p, title=title, raw_text=text)


def load_project_config(path: str | Path) -> ProjectConfig:
    """从 YAML 载入项目配置。兼容单工程（repo）与多工程（repos）两种写法。"""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))

    def _dt(v: object) -> datetime:
        if isinstance(v, datetime):
            return v
        return datetime.fromisoformat(str(v))

    def _repo(d: dict) -> SourceRepo:
        return SourceRepo(
            name=d.get("name", ""),
            git_url=d["git_url"],
            branch=d["branch"],
            base=d.get("base"),
            start_time=_dt(d["start_time"]),
            end_time=_dt(d["end_time"]),
        )

    repos_raw = raw.get("repos") or ([raw["repo"]] if raw.get("repo") else [])
    if not repos_raw:
        raise ValueError("配置缺少 repos（或 repo）字段")
    repos = [_repo(r) for r in repos_raw]

    req = load_requirement(raw.get("requirement_doc"))
    workdir = Path(raw.get("workdir") or ".kanma").expanduser()
    return ProjectConfig(
        name=raw.get("name") or Path(path).stem,
        repos=repos,
        requirement=req,
        workdir=workdir,
        source_extensions=raw.get("source_extensions") or ProjectConfig.model_fields[
            "source_extensions"
        ].default_factory(),  # type: ignore[union-attr]
        exclude_globs=raw.get("exclude_globs")
        or ProjectConfig.model_fields["exclude_globs"].default_factory(),  # type: ignore[union-attr]
        maven_path=raw.get("maven_path", "") or "",
        maven_conf=raw.get("maven_conf", "") or "",
    )


_EXT_LANG_MAP = {
    ".java": CodeLanguage.JAVA,
    ".kt": CodeLanguage.JAVA,
    ".py": CodeLanguage.PYTHON,
    ".go": CodeLanguage.GO,
    ".js": CodeLanguage.JAVASCRIPT,
    ".jsx": CodeLanguage.JAVASCRIPT,
    ".ts": CodeLanguage.TYPESCRIPT,
    ".tsx": CodeLanguage.TYPESCRIPT,
}


def detect_language(path: str) -> CodeLanguage:
    ext = Path(path).suffix.lower()
    return _EXT_LANG_MAP.get(ext, CodeLanguage.UNKNOWN)


def slugify(text: str) -> str:
    """把项目名转成报告文件名可用的 slug。"""
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text).strip("-")
    return s or "project"


def dump_project_config(cfg: ProjectConfig, path: str | Path) -> Path:
    """把项目配置落盘为 YAML。

    写入格式与 load_project_config 完全对齐（需求文档用 `requirement_doc`
    路径键），因此保存后的文件可直接被 `kanma review -c <path>` 消费。
    """
    req_path = cfg.requirement.path
    data = {
        "name": cfg.name,
        "repos": [
            {
                "name": r.name,
                "git_url": r.git_url,
                "branch": r.branch,
                "base": r.base,
                "start_time": r.start_time.isoformat(),
                "end_time": r.end_time.isoformat(),
            }
            for r in cfg.repos
        ],
        "requirement_doc": str(req_path) if req_path else None,
        "workdir": str(cfg.workdir),
        "source_extensions": cfg.source_extensions,
        "exclude_globs": cfg.exclude_globs,
        "maven_path": cfg.maven_path,
        "maven_conf": cfg.maven_conf,
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return p
