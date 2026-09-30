"""规约（评审知识库）加载器。

**设计原则：勘码代码与规约内容解耦。** 规约原文放在 `rules/data/*.md`（按文件名顺序即
章节阅读顺序），适用语言与各章负责的审查维度放在 `rules/data/spec.yaml`，本模块只做
加载/抽取/组装，不写死任何条款。改规约只改数据文件，不动此处一行代码。

勘码**不做正则机检**：没有任何规则表，全部问题由 LLM 按本目录规约逐章做语义审查。

为什么按序逐章注入：rules/data 全量约 13K 字符，逐 chunk 审查时每个 chunk 只注入 1 章
规约（约 0.4~7.6K 字符），既覆盖全部章节，又避免单次调用把全部章节塞进 prompt 造成
token 超限。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
import yaml

from kanma.models.schemas import ReviewDimension

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent / "data"
SPEC_YAML = DATA_DIR / "spec.yaml"

# 条文行： **N.【强制】** 内容
_RULE_LINE = re.compile(r"^\s*\*{0,2}\d+\.\s*\*{0,2}【(强制|推荐|参考)】\s*\*{0,2}\s*(.+?)\s*$")
# 章节小标题： ## (一) 命名风格
_SECTION = re.compile(r"^#{2,3}\s*(\([一二三四五六七八九十]+\)\s*.+?)\s*$")


@dataclass(frozen=True)
class SpecRule:
    """一条从规约原文抽取出来的条文（供语义审查注入 prompt）。"""

    chapter: str
    section: str
    level: str
    text: str

    def render(self, with_section: bool = True) -> str:
        prefix = f"[{self.section}] " if with_section and self.section else ""
        return f"- {prefix}【{self.level}】{self.text}"


def spec_applies_to() -> tuple[str, ...]:
    """规约适用的语言集合（来自 spec.yaml 的 `applies_to`；空表示通用，不限语言）。"""
    try:
        return tuple(str(x) for x in (_spec_meta().get("applies_to") or []))
    except Exception:
        logger.warning("spec.yaml 读取 applies_to 失败，规约按通用处理")
    return ()


def spec_applies(languages: set[str] | frozenset[str]) -> bool:
    """该批语言是否适用规约（决定要不要注入规约做语义审查）。

    由 spec.yaml 的 `applies_to` 声明，勘码不写死「Java 才用某规约」。
    """
    langs = spec_applies_to()
    if not langs:
        return True
    return bool(set(langs) & set(languages))


def _spec_meta() -> dict:
    """读 spec.yaml（规约元数据：适用语言 + 各章审查维度）。"""
    if not SPEC_YAML.is_file():
        return {}
    try:
        return yaml.safe_load(SPEC_YAML.read_text(encoding="utf-8")) or {}
    except Exception as e:  # pragma: no cover - 元数据损坏不应拖垮评审
        logger.warning("spec.yaml 解析失败：%s", e)
        return {}


def spec_chapter_dimensions() -> dict[str, tuple[str, ...]]:
    """章节 → 该章的审查维度标签（来自 spec.yaml 的 `chapter_dimensions`）。

    标签是**自由文本**（如「java代码规范」「系统安规范」），不要求命中 ReviewDimension 枚举：
    每份规约的侧重点不同，用规约自己的说法描述聚焦点，比套六个固定维度更准。
    """
    raw = _spec_meta().get("chapter_dimensions") or {}
    out: dict[str, tuple[str, ...]] = {}
    for key, dims in raw.items():
        picked = tuple(str(d).strip() for d in (dims or []) if str(d).strip())
        if picked:
            out[str(key)] = picked
    _warn_orphan_keys(out)
    return out


def _warn_orphan_keys(mapping: dict[str, tuple[str, ...]]) -> None:
    """配置漂移告警：spec.yaml 里写了但磁盘上已不存在的章节键。

    增删/重命名 `rules/data/*.md` 后若忘了同步 spec.yaml，这些维度声明会静默失效
    （该章退回全量维度），页面只会看到「维度不对」却查不到原因——故显式落日志。
    """
    if not mapping:
        return
    known = {k for k, _ in spec_chapters()}
    orphans = sorted(set(mapping) - known)
    if orphans:
        logger.warning(
            "spec.yaml 的 chapter_dimensions 里有 %d 个章节键在 rules/data 下不存在，"
            "这些声明不会生效：%s", len(orphans), "、".join(orphans)
        )


def chapter_dimensions(key: str) -> tuple[str, ...]:
    """某章的审查维度标签。

    未在 spec.yaml 声明的章节**不限制维度**（返回六个标准维度），保证不因漏配而漏审。
    """
    return spec_chapter_dimensions().get(key) or tuple(d.value for d in ReviewDimension)


def _chapter_title(key: str, text: str) -> str:
    """章节中文名优先取文件首个 `# ` 一级标题，否则退回文件名。"""
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return key


def spec_chapters() -> list[tuple[str, str]]:
    """返回 (章节 key, 章节标题) 列表，按文件名排序（即规约顺序）。"""
    out: list[tuple[str, str]] = []
    if not DATA_DIR.is_dir():
        return out
    for p in sorted(DATA_DIR.glob("*.md")):
        key = p.stem
        text = p.read_text(encoding="utf-8", errors="ignore")
        out.append((key, _chapter_title(key, text)))
    return out


def extract_rules(key: str, levels: tuple[str, ...] = ("强制",)) -> list[SpecRule]:
    """从某章节原文抽取指定级别的规约条文。

    只保留条文标题行，丢弃其后的「说明/正例/反例」——它们占体积但对 LLM 判定
    的边际价值低（模型本身知道这些 API 的正确用法）。
    """
    p = DATA_DIR / f"{key}.md"
    if not p.is_file():
        return []
    text = p.read_text(encoding="utf-8", errors="ignore")
    rules: list[SpecRule] = []
    section = ""
    for line in text.splitlines():
        m_sec = _SECTION.match(line)
        if m_sec:
            section = m_sec.group(1).strip()
            continue
        m = _RULE_LINE.match(line)
        if m and m.group(1) in levels:
            body = m.group(2).strip().rstrip("*").strip().replace("**", "").strip()
            if body:
                rules.append(SpecRule(key, section, m.group(1), body))
    return rules


def build_guide(
    chapters: list[str] | None = None,
    max_chars: int = 9000,
    levels: tuple[str, ...] = ("强制",),
) -> tuple[str, list[str]]:
    """组装注入 prompt 的规约片段（按章节顺序）。

    返回 `(规约文本, 实际使用的章节中文名列表)`。
    `chapters` 为 None 时按序加载全部章节；每个章节独立成一次审查子步骤，避免单次调用
    把全部章节塞进 prompt 造成 token 超限。
    """
    all_chapters = spec_chapters()
    valid = {k: t for k, t in all_chapters}
    picked = chapters if chapters is not None else [k for k, _ in all_chapters]
    blocks: list[str] = []
    used: list[str] = []
    total = 0
    for ch in picked:
        if ch not in valid:
            continue
        rules = extract_rules(ch, levels=levels)
        if not rules:
            continue
        piece = f"【{valid[ch]}】\n" + "\n".join(r.render() for r in rules) + "\n"
        if total + len(piece) > max_chars and blocks:
            break
        blocks.append(piece)
        used.append(valid[ch])
        total += len(piece)
    return "\n".join(blocks).strip(), used
