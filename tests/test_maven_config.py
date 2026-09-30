"""Maven 自定义配置（maven_path / maven_conf）的贯通测试。

解决「未检测到可用的 Maven/Gradle 工具链或构建文件，Java 测试无法执行」：
用户可在项目信息里指定本机 Maven 可执行文件路径与 settings.xml，链路需保证
API 录入 → ProjectConfig → YAML 落库 → 重载 全程不丢字段，且 runner 真正用到。
"""

import tempfile
from pathlib import Path

from kanma.config import dump_project_config, load_project_config
from kanma.models.schemas import ProjectConfig, SourceRepo
from kanma.web.schemas import ProjectCreate


def _cfg(maven_path: str = "", maven_conf: str = "") -> ProjectConfig:
    return ProjectConfig(
        name="maven-贯通",
        repos=[
            SourceRepo(
                name="r",
                git_url="git@x/y.git",
                branch="main",
                start_time="2026-01-01T00:00:00",
                end_time="2026-01-02T00:00:00",
            )
        ],
        maven_path=maven_path,
        maven_conf=maven_conf,
    )


def test_dump_load_roundtrip_preserves_maven_fields():
    cfg = _cfg("/opt/maven/bin/mvn", "/opt/maven/conf/settings.xml")
    d = Path(tempfile.mkdtemp())
    p = dump_project_config(cfg, d / "t.yaml")
    back = load_project_config(p)
    assert back.maven_path == "/opt/maven/bin/mvn"
    assert back.maven_conf == "/opt/maven/conf/settings.xml"


def test_default_maven_fields_survive_roundtrip():
    cfg = _cfg()  # 默认空串
    d = Path(tempfile.mkdtemp())
    back = load_project_config(dump_project_config(cfg, d / "t.yaml"))
    assert back.maven_path == ""
    assert back.maven_conf == ""


def test_projectcreate_to_config_carries_maven_fields():
    pc = ProjectCreate(
        name="web-maven",
        repos=[
            {
                "name": "r",
                "git_url": "git@x/y.git",
                "branch": "main",
                "start_time": "2026-01-01T00:00:00",
                "end_time": "2026-01-02T00:00:00",
            }
        ],
        maven_path="/usr/local/mvn/bin/mvn",
        maven_conf="/usr/local/mvn/conf/settings.xml",
    )
    cfg = pc.to_config()
    assert cfg.maven_path == "/usr/local/mvn/bin/mvn"
    assert cfg.maven_conf == "/usr/local/mvn/conf/settings.xml"
