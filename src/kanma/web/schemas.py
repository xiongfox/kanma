"""Web 层 API 数据契约。

ProjectCreate 与 ProjectConfig 解耦：前端只提交"录入项"
（项目名、源码工程列表、需求文档本地路径、工作区）。

为兼容旧的单工程表单，ProjectCreate 同时接受「repos 列表」或「单组
git_url/branch/base/start_time/end_time」字段；服务端再组装为完整的
ProjectConfig（需求文档按本地路径加载）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from kanma.config import load_requirement
from kanma.models.schemas import ProjectConfig, RequirementDoc, SourceRepo


class RepoCreate(BaseModel):
    """单个源码工程的录入项。"""

    name: str = Field(default="", description="源码工程名（可选，缺省从 git 地址推导）")
    git_url: str = Field(description="仓库地址（SSH/HTTPS）")
    branch: str = Field(description="目标分支")
    base: str | None = Field(default=None, description="对比基线 commit/分支（可选）")
    start_time: datetime = Field(description="变更起始时间")
    end_time: datetime = Field(description="变更结束时间")


class ProjectCreate(BaseModel):
    """创建项目时提交的数据。支持单工程（旧字段）或多工程（repos 列表）。"""

    name: str = Field(description="项目名，用于报告命名")
    repos: list[RepoCreate] | None = Field(
        default=None, description="源码工程列表（多工程评审）"
    )
    # 兼容旧单工程表单
    git_url: str | None = Field(default=None, description="仓库地址（单工程表单）")
    branch: str | None = Field(default=None, description="目标分支（单工程表单）")
    base: str | None = Field(default=None, description="对比基线（单工程表单）")
    start_time: datetime | None = Field(default=None, description="变更起始时间（单工程表单）")
    end_time: datetime | None = Field(default=None, description="变更结束时间（单工程表单）")
    requirement_doc: str | None = Field(
        default=None, description="需求开发文档的本地路径（可选）"
    )
    workdir: str | None = Field(default=None, description="勘码工作区目录（可选）")
    maven_path: str = Field(
        default="", description="Maven 可执行文件绝对路径（本机未安装 Maven 时可指定自定义路径）"
    )
    maven_conf: str = Field(
        default="", description="Maven settings.xml 路径（可选，启用后 mvn 加 -s 参数）"
    )

    def to_config(self) -> ProjectConfig:
        """组装为完整 ProjectConfig。需求文档按本地路径加载（缺失走降级）。"""
        req: RequirementDoc = (
            load_requirement(self.requirement_doc) if self.requirement_doc else RequirementDoc()
        )

        repos: list[SourceRepo] = []
        if self.repos:
            repos = [
                SourceRepo(
                    name=r.name,
                    git_url=r.git_url,
                    branch=r.branch,
                    base=r.base,
                    start_time=r.start_time,
                    end_time=r.end_time,
                )
                for r in self.repos
            ]
        elif all(
            x is not None
            for x in (self.git_url, self.branch, self.start_time, self.end_time)
        ):
            repos = [
                SourceRepo(
                    git_url=self.git_url,  # type: ignore[arg-type]
                    branch=self.branch,  # type: ignore[arg-type]
                    base=self.base,
                    start_time=self.start_time,  # type: ignore[arg-type]
                    end_time=self.end_time,  # type: ignore[arg-type]
                )
            ]

        if not repos:
            raise ValueError(
                "至少提供一个源码工程：repos 列表，或 git_url/branch/start_time/end_time"
            )

        return ProjectConfig(
            name=self.name,
            repos=repos,
            requirement=req,
            workdir=Path(self.workdir).expanduser() if self.workdir else Path(".kanma"),
            maven_path=self.maven_path,
            maven_conf=self.maven_conf,
        )
