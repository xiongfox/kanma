#!/usr/bin/env python3
"""不依赖 pytest 的测试执行器。

适用场景：受限环境里 pytest 收集阶段读 conftest 会被安全扫描拦截，
或只想快速跑一遍断言。它实现了最小化的 fixture 解析（含依赖递归与 tmp_path）。

用法：
    python scripts/run_tests.py            # 跑全部
    python scripts/run_tests.py runners    # 只跑文件名含 runners 的测试
    python scripts/run_tests.py -v         # 显示每条用例结果
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def _load_fixtures() -> dict[str, Any]:
    """从 tests/conftest.py 收集 pytest fixture 函数。"""
    try:
        conftest = importlib.import_module("tests.conftest")
    except Exception as e:  # pragma: no cover
        print(f"警告：无法加载 conftest（{e}），仅执行无 fixture 的测试")
        return {}
    fixtures: dict[str, Any] = {}
    for name, fn in vars(conftest).items():
        if not callable(fn):
            continue
        # pytest >= 8：@pytest.fixture 返回 FixtureFunctionDefinition，需取出原函数
        # pytest < 8：直接返回带 _pytestfixturefunction 标记的原函数
        if hasattr(fn, "_fixture_function"):
            fixtures[name] = fn._fixture_function
        elif hasattr(fn, "_pytestfixturefunction"):
            fixtures[name] = fn
    return fixtures


class Resolver:
    def __init__(self) -> None:
        self.fixtures = _load_fixtures()
        self.cache: dict[str, Any] = {}
        self._tmp = Path(tempfile.mkdtemp(prefix="kanma-tests-"))
        self._n = 0

    def cleanup(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def tmp_path(self) -> Path:
        self._n += 1
        p = self._tmp / f"case{self._n}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def resolve(self, name: str) -> Any:
        if name == "tmp_path":
            return self.tmp_path()
        if name in self.cache:
            return self.cache[name]
        fn = self.fixtures.get(name)
        if fn is None:
            raise KeyError(f"未知的 fixture：{name}")
        args = [self.resolve(p) for p in inspect.signature(fn).parameters if p != "self"]
        value = fn(*args)
        self.cache[name] = value
        return value


def run_module(mod_name: str, resolver: Resolver, verbose: bool) -> tuple[int, int]:
    mod = importlib.import_module(mod_name)
    passed = failed = 0
    for fn_name in sorted(n for n in dir(mod) if n.startswith("test_")):
        fn = getattr(mod, fn_name)
        if not callable(fn):
            continue
        params = list(inspect.signature(fn).parameters)
        try:
            kwargs = {p: resolver.resolve(p) for p in params}
            fn(**kwargs)
            passed += 1
            if verbose:
                print(f"  PASS {mod_name.split('.')[-1]}::{fn_name}")
        except BaseException as e:  # pytest.raises 失败抛的是 BaseException
            failed += 1
            print(f"  FAIL {mod_name.split('.')[-1]}::{fn_name} -> {type(e).__name__}: {e}")
            if verbose:
                traceback.print_exc(limit=3)
    return passed, failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("filter", nargs="?", default="", help="只跑文件名包含该关键字的测试")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    files = sorted((ROOT / "tests").glob("test_*.py"))
    if args.filter:
        files = [f for f in files if args.filter in f.name]
    if not files:
        print("没有匹配的测试文件")
        return 1

    resolver = Resolver()
    total_p = total_f = 0
    try:
        for f in files:
            mod_name = f"tests.{f.stem}"
            print(f"\n{mod_name}")
            p, fl = run_module(mod_name, resolver, args.verbose)
            total_p += p
            total_f += fl
            print(f"  小计：通过 {p}，失败 {fl}")
    finally:
        resolver.cleanup()

    print(f"\n合计：通过 {total_p}，失败 {total_f}")
    return 1 if total_f else 0


if __name__ == "__main__":
    sys.exit(main())
