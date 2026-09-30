"""以独立会话启动 `kanma web`，使其真正脱离启动它的终端/进程组。

为什么不能用裸 `nohup ... &`：
`nohup` 只忽略 SIGHUP，子进程仍留在**同一进程组**里。当父 shell 被回收
（例如被上层调用方按进程组清理）时，服务会跟着一起没——表现为服务看似
启动成功、过一会儿端口就拒绝连接，且评审任务被连带杀死。

这里用 `start_new_session=True`（等价于 setsid）让服务成为**新会话首进程**，
脱离原进程组与控制终端；再由 launchd(PID 1) 收养，才算是真正的常驻后台。

用法：
    python _daemon.py <解释器和-venv路径?> <LOGFILE> <PIDFILE> <kanma-web 参数...>

约定前三个位置参数是固定的，其后所有参数原样透传给 `python -m kanma.cli web`。
"""
from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    python, logfile, pidfile = sys.argv[1], sys.argv[2], sys.argv[3]
    web_args = sys.argv[4:]

    # 追加模式，避免覆盖 serve.sh 刚写入的内容
    log = open(logfile, "ab", buffering=0)
    devnull = open(os.devnull, "rb")

    proc = subprocess.Popen(
        [python, "-m", "kanma.cli", "web", *web_args],
        stdin=devnull,
        stdout=log,
        stderr=log,
        start_new_session=True,  # ← 关键：setsid，脱离原进程组
        close_fds=True,
    )
    with open(pidfile, "w") as fh:
        fh.write(str(proc.pid))
    # launcher 自己立刻退出；子进程已被 PID 1 收养，与原会话再无关系
    return 0


if __name__ == "__main__":
    raise SystemExit(main())