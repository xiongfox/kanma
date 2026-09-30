"""测试公共 fixture：构造带真实 commit 的临时 git 仓库。"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from kanma.models.schemas import ProjectConfig, RequirementDoc, SourceRepo

# 故意埋入各类缺陷的样例代码，用于验证 diff 采集与语义审查
SAMPLE_BAD_CODE = '''
import os
import pickle
import subprocess
import logging

log = logging.getLogger(__name__)

API_TOKEN = "PLACEHOLDER_CREDENTIAL_VALUE"
DB_PASSWORD = "PLACEHOLDER_DB_CREDENTIAL"


def query_order(conn, order_id):
    sql = "SELECT * FROM orders WHERE id = " + order_id
    return conn.execute(sql)


def calc_discount(amount):
    if amount >= 200:
        return 30
    # TODO 处理边界
    return 10


def run_report(date):
    os.system("sh report.sh " + date)
    subprocess.run("rm -rf /tmp/" + date, shell=True)


def load_cache(data):
    return pickle.loads(data)


def audit(user, request):
    log.info("user login, password=%s, phone=%s", user.password, user.phone)
    return True


def sum_prices(prices):
    total = ""
    for p in prices:
        total += str(p)
    return total


def fetch_all(conn):
    for row in conn.query("SELECT * FROM orders"):
        print(row)
'''

REQUIREMENT_SAMPLE = """# 订单折扣需求

- 单笔订单金额满 100 元减 10 元，满 200 元减 30 元。
- 折扣不可叠加，取满足条件的最大优惠。
- 订单金额为 0 或负数时返回参数错误。
"""

# 故意埋入各类缺陷的 Java 样例代码，用于验证 Java 规约注入与语义审查
JAVA_BAD_CODE = """\
package demo;

public class OrderService {
    private static SimpleDateFormat SDF = new SimpleDateFormat("yyyy-MM-dd");
    private ExecutorService pool = Executors.newFixedThreadPool(10);

    @Select("SELECT * FROM t_order WHERE user_id = ${userId}")
    public List<OrderDO> list(Long userId) {
        try {
            return mapper.select(userId);
        } catch (Exception e) {
            return null;
        }
    }

    public void fire() { new Thread(() -> {}).start(); }
}
"""


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return proc.stdout


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """含一次真实 commit 的临时仓库（main 分支）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "kanma@example.com"], repo)
    _git(["config", "user.name", "kanma"], repo)
    (repo / "order_service.py").write_text(SAMPLE_BAD_CODE, encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-m", "feat: order discount"], repo)
    return repo


@pytest.fixture
def sample_config(sample_repo: Path, tmp_path: Path) -> ProjectConfig:
    """单源码工程的评审配置。"""
    now = datetime.now()
    return ProjectConfig(
        name="smoke",
        repos=[
            SourceRepo(
                name="order-service",
                git_url=str(sample_repo),
                branch="main",
                start_time=now - timedelta(days=1),
                end_time=now + timedelta(days=1),
            )
        ],
        requirement=RequirementDoc(title="订单折扣需求", raw_text=REQUIREMENT_SAMPLE),
        workdir=tmp_path / "workdir",
    )


@pytest.fixture
def java_repo(tmp_path: Path) -> Path:
    """含一次真实 commit 的临时 Java 仓库，埋入会触发阿里 skill 的缺陷。"""
    repo = tmp_path / "java_repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "kanma@example.com"], repo)
    _git(["config", "user.name", "kanma"], repo)
    (repo / "OrderService.java").write_text(JAVA_BAD_CODE, encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-m", "feat: order service"], repo)
    return repo


@pytest.fixture
def java_config(java_repo: Path, tmp_path: Path) -> ProjectConfig:
    """单源码工程（Java）的评审配置，用于验证 Java 规约注入。"""
    now = datetime.now()
    return ProjectConfig(
        name="java-smoke",
        repos=[
            SourceRepo(
                name="order-service",
                git_url=str(java_repo),
                branch="main",
                start_time=now - timedelta(days=1),
                end_time=now + timedelta(days=1),
            )
        ],
        requirement=RequirementDoc(title="订单查询需求", raw_text=REQUIREMENT_SAMPLE),
        workdir=tmp_path / "workdir_java",
    )


@pytest.fixture
def sample_repo_b(tmp_path: Path) -> Path:
    """第二个源码工程（含一处硬编码密钥），用于验证多工程采集。"""
    repo = tmp_path / "repo_b"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "kanma@example.com"], repo)
    _git(["config", "user.name", "kanma"], repo)
    (repo / "pay_service.py").write_text(
        'API_KEY = "PLACEHOLDER_CREDENTIAL"\n\n\ndef pay(uid, amount):\n    return amount * 0.9\n',
        encoding="utf-8",
    )
    _git(["add", "."], repo)
    _git(["commit", "-m", "feat: pay"], repo)
    return repo


@pytest.fixture
def multi_repo_config(
    sample_repo: Path, sample_repo_b: Path, tmp_path: Path
) -> ProjectConfig:
    """多源码工程的评审配置：两个工程各自独立的 git 地址 / 分支 / 时间窗。"""
    now = datetime.now()
    return ProjectConfig(
        name="multi",
        repos=[
            SourceRepo(
                name="order-service",
                git_url=str(sample_repo),
                branch="main",
                start_time=now - timedelta(days=1),
                end_time=now + timedelta(days=1),
            ),
            SourceRepo(
                name="pay-service",
                git_url=str(sample_repo_b),
                branch="main",
                start_time=now - timedelta(days=1),
                end_time=now + timedelta(days=1),
            ),
        ],
        requirement=RequirementDoc(title="订单折扣需求", raw_text=REQUIREMENT_SAMPLE),
        workdir=tmp_path / "workdir_multi",
    )


@pytest.fixture
def offline_ctx():
    """离线 LLM 上下文：available=False，用于验证降级路径不伪造结果。"""
    from kanma.llm import LLMContext

    return LLMContext(False, "", reason="冒烟测试：未配置 KANMA_MODEL")
