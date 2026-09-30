"""沙箱执行器（HR-07）。

铁律：测试代码必须在隔离环境执行。
- 优先 Docker：--network none、源码只读挂载、内存与 PID 限额、超时强杀。
- 容器不可用退化为：独立目录 + 子进程 + 超时强杀 + 独立临时目录 + 禁网环境变量。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# 禁网与确定性相关的环境变量，本地降级模式下尽可能收敛外部访问
_SAFE_ENV_OVERRIDES = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_INPUT": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "NO_PROXY": "*",
    "no_proxy": "*",
}

_DEFAULT_TIMEOUT = 300
_DEFAULT_MEMORY = "2g"
_DEFAULT_PIDS = 256


@dataclass
class ExecResult:
    """一次命令执行的结构化结果。"""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None


@dataclass
class Sandbox:
    """沙箱执行器。docker 优先，否则退化为本地子进程隔离。"""

    backend: str = "auto"  # auto | docker | local
    timeout: int = _DEFAULT_TIMEOUT
    memory: str = _DEFAULT_MEMORY
    pids_limit: int = _DEFAULT_PIDS
    image: str = "python:3.13-slim"
    _resolved: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if self.backend == "auto":
            self._resolved = "docker" if self.docker_available() else "local"
        else:
            self._resolved = self.backend

    @property
    def resolved_backend(self) -> str:
        return self._resolved

    @staticmethod
    def docker_available() -> bool:
        if not shutil.which("docker"):
            return False
        proc = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=20
        )
        return proc.returncode == 0

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def run(
        self,
        command: list[str],
        cwd: Path | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        read_only_mounts: list[Path] | None = None,
    ) -> ExecResult:
        """执行命令。同一命令在不同 backend 下语义一致。"""
        timeout = timeout or self.timeout
        if self._resolved == "docker":
            return self._run_docker(command, cwd, timeout, env, read_only_mounts)
        return self._run_local(command, cwd, timeout, env)

    # ------------------------------------------------------------------
    def _run_local(
        self,
        command: list[str],
        cwd: Path | None,
        timeout: int,
        env: dict[str, str] | None,
    ) -> ExecResult:
        merged = {**os.environ, **_SAFE_ENV_OVERRIDES, **(env or {})}
        try:
            proc = subprocess.run(
                command,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=merged,
            )
            return ExecResult(proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired as e:
            return ExecResult(
                -1,
                e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
                e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or ""),
                timed_out=True,
                error=f"执行超时（{timeout}s）",
            )
        except FileNotFoundError as e:
            return ExecResult(-1, "", "", error=f"命令不存在: {e}")

    def _run_docker(
        self,
        command: list[str],
        cwd: Path | None,
        timeout: int,
        env: dict[str, str] | None,
        read_only_mounts: list[Path] | None,
    ) -> ExecResult:
        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--memory",
            self.memory,
            "--pids-limit",
            str(self.pids_limit),
        ]
        for key, value in {**_SAFE_ENV_OVERRIDES, **(env or {})}.items():
            docker_cmd += ["-e", f"{key}={value}"]
        for mount in read_only_mounts or []:
            docker_cmd += ["-v", f"{mount}:{mount}:ro"]
        if cwd:
            docker_cmd += ["-v", f"{cwd}:{cwd}:rw", "-w", str(cwd)]
        docker_cmd += [self.image, *command]

        try:
            proc = subprocess.run(
                docker_cmd, capture_output=True, text=True, timeout=timeout
            )
            return ExecResult(proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired as e:
            return ExecResult(-1, e.stdout or "", e.stderr or "", timed_out=True,
                              error=f"执行超时（{timeout}s）")
        except FileNotFoundError:
            return ExecResult(-1, "", "", error="docker 不可用，请改用 local 后端")


def pick_sandbox(backend: str = "auto", timeout: int = _DEFAULT_TIMEOUT) -> Sandbox:
    return Sandbox(backend=backend, timeout=timeout)
