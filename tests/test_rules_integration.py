"""规约（评审知识库）的集成回归测试。

核心锁定三条设计约束：

1. **无机检**：勘码**不做正则机检**——没有规则表、没有 `hard_rules` 模块、没有 `scan_diff`。
   缺陷全部由 LLM 按规约逐章做语义审查得出（`test_no_machine_check_engine` 锁定此决定）。
2. **解耦**：勘码代码中不写死任何规约条款——分章原文在 `rules/data/*.md`，适用语言在
   `rules/data/spec.yaml`，由 `kanma.rules` 加载。改规约只改数据文件，不动代码。
3. **可用**：`rules/data/*.md` 能按文件名顺序加载、抽取【强制】条文、组装成注入 prompt 的片段。
"""
from __future__ import annotations

import importlib
import pathlib
import re
import sys

import pytest

from kanma.rules import (
    DATA_DIR,
    SPEC_YAML,
    build_guide,
    chapter_dimensions,
    extract_rules,
    spec_applies,
    spec_applies_to,
    spec_chapter_dimensions,
    spec_chapters,
)


def _security_chapter() -> str:
    """当前规约里讲安全的那一章（按文件名含 security 推导，重编号也不会失效）。"""
    for key, _title in spec_chapters():
        if "security" in key:
            return key
    raise AssertionError("rules/data 下找不到安全章节")


_SECURITY_CHAPTER = _security_chapter()


# ---------------------------------------------------------------------------
# 1. 机检已彻底移除
# ---------------------------------------------------------------------------
def test_no_machine_check_engine():
    """不留任何机检规则：规则引擎模块与规则数据文件都不应存在。"""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("kanma.rules.hard_rules")
    assert not (DATA_DIR / "rules.yaml").exists(), "rules/data/rules.yaml 应已删除"

    import kanma.rules as pkg

    for name in ("Rule", "all_rules", "rules_for", "scan_diff"):
        assert not hasattr(pkg, name), f"kanma.rules 仍导出机检接口 {name}"


def test_static_review_has_no_machine_check_stage():
    """静态审查节点不得再引用机检扫描器。"""
    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src/kanma/nodes/static_review.py"
    ).read_text(encoding="utf-8")
    assert "scan_diff" not in src
    assert "规则型机检" not in src


# ---------------------------------------------------------------------------
# 2. 适用语言门禁（数据来自 spec.yaml，而非代码）
# ---------------------------------------------------------------------------
def test_spec_applies_respects_applies_to():
    assert spec_applies_to(), "spec.yaml 应声明 applies_to"
    assert spec_applies({"java"}) is True
    assert spec_applies({"python", "go"}) is False


def test_chapter_dimensions_come_from_spec_yaml():
    """审查维度按章节声明（数据外置、自由文本），未声明的章节回退六个标准维度。"""
    from kanma.models.schemas import ReviewDimension

    declared = spec_chapter_dimensions()
    assert declared, "spec.yaml 应声明各章审查维度"
    for key, dims in declared.items():
        assert dims and all(isinstance(d, str) and d.strip() for d in dims)
    # 声明的键必须都能对上磁盘上的章节，否则该章会静默退回全量维度
    known = {k for k, _ in spec_chapters()}
    assert not (set(declared) - known), f"spec.yaml 存在孤儿章节键：{set(declared) - known}"

    # 未声明的章节不限制维度
    assert set(chapter_dimensions("99-not-declared")) == {d.value for d in ReviewDimension}


def test_chapter_dimensions_match_loaded_chapters():
    """每章实际拿到的维度必须等于 spec.yaml 的声明（曾因键对不上而整章退回六维）。"""
    for key, _title in spec_chapters():
        dims = chapter_dimensions(key)
        assert dims, f"{key} 没有任何审查维度"
        assert dims == spec_chapter_dimensions().get(key, tuple()), (
            f"{key} 的维度与 spec.yaml 声明不一致"
        )


def test_system_prompt_focus_is_dynamic():
    """系统提示词里的「审查重点」必须随章节动态组装，不能写死六维。"""
    from kanma.models.schemas import ReviewDimension
    from kanma.nodes import static_review  # 重导出的是函数，取真模块见下

    node = sys.modules["kanma.nodes.static_review"]
    assert static_review is not None  # 保证子模块已被导入
    assert "{focus}" in node.SYSTEM_PROMPT

    single = node._focus_section(("注入与越权",))
    focus_line = single.splitlines()[0]
    assert focus_line == "本次审查重点：注入与越权。", "自由文本标签必须原样进入提示词"
    # 维度字段仍受枚举约束，提示词要给出可选取值
    assert "dimension" in single
    # 重点是引导而非硬约束：否则「性能」这类没有专属章节的维度就没人审了
    assert "也可一并报出" in single

    fallback = node._focus_section(())
    for d in ReviewDimension:
        assert d.value in fallback, f"未声明重点时应回退全量，缺 {d.value}"


def test_spec_metadata_lives_in_dedicated_file():
    """适用语言必须独立于机检规则，避免删规则时把语言门禁一起删掉（曾导致静默不注入规约）。"""
    assert SPEC_YAML.is_file(), "rules/data/spec.yaml 缺失"
    assert not (DATA_DIR / "rules.yaml").exists()


# ---------------------------------------------------------------------------
# 3. 规约原文：按序加载与条文抽取
# ---------------------------------------------------------------------------
def test_spec_chapters_ordered_by_filename():
    """章节按文件名排序加载——断言以磁盘实际文件为准，增删章节不必改测试。"""
    on_disk = sorted(p.stem for p in DATA_DIR.glob("*.md"))
    chs = spec_chapters()
    assert on_disk, "rules/data 下应有分章规约原文"
    assert [k for k, _ in chs] == on_disk, "章节加载顺序必须与文件名排序一致"
    assert all(t for _, t in chs), "每章都应有中文标题"


def test_extract_mandatory_rules():
    rules = extract_rules(_SECURITY_CHAPTER, levels=("强制",))
    assert rules, "安全章节应能抽出强制条款"
    assert all(r.level == "强制" for r in rules)


def test_build_guide_single_chapter():
    guide, used = build_guide(chapters=[_SECURITY_CHAPTER], max_chars=9000)
    assert guide and used
    assert len(used) == 1
    assert any("SQL" in r.text for r in extract_rules(_SECURITY_CHAPTER))


def test_build_guide_loads_all_chapters_within_budget():
    # 全量【强制】条文合计约 13K 字符，需足够大的预算才能一次性覆盖
    guide, used = build_guide(max_chars=20000)
    assert guide
    assert len(used) == len(spec_chapters()), "足够预算内应覆盖全部章节"


def test_build_guide_respects_budget():
    _, used = build_guide(max_chars=2000)
    assert used
    assert len(used) < 7, "2000 预算不足以塞下全部章节，应提前截断"


def test_every_chapter_has_injectable_guide():
    """每章单独注入时都必须非空——曾出现搬移 .md 后 build_guide 静默返回空串。"""
    for key, _title in spec_chapters():
        guide, used = build_guide(chapters=[key], max_chars=9000)
        assert guide, f"章节 {key} 注入内容为空（数据文件丢失或格式不符？）"
        assert used == [_title] or len(used) == 1


# ---------------------------------------------------------------------------
# 4. 解耦的可验证性：勘码源码里不得出现硬编码规约
# ---------------------------------------------------------------------------
_SOURCE_DIRS = ["src/kanma/rules", "src/kanma/nodes"]


def test_no_hardcoded_rules_in_code():
    """扫描勘码源码，确保没有把规约条款写死在代码里（规约必须可插拔）。"""
    root = pathlib.Path(__file__).resolve().parents[1]
    forbidden = re.compile(r"SimpleDateFormat|Executors\s*\\*\.|阿里巴巴|ALI-JAVA-")
    for d in _SOURCE_DIRS:
        for py in (root / d).rglob("*.py"):
            text = py.read_text(encoding="utf-8", errors="ignore")
            hit = forbidden.search(text)
            assert hit is None, (
                f"{py.relative_to(root)} 里出现了硬编码规约（{hit.group(0)!r}）；"
                f"规约应声明在 rules/data 的数据文件中"
            )


def test_data_dir_exists():
    assert DATA_DIR.is_dir(), "rules/data 目录应存在"
    assert list(DATA_DIR.glob("*.md")), "rules/data 下应有分章规约原文"
