"""进程树：真实进程上跑，假宿主测不出平台分支。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable

from agentic_tty.core.runtime import process_tree
from agentic_tty.core.runtime.process_tree import (
    CREATE_SUSPENDED,
    ProcessTree,
    assign_job,
    create_job,
    resume_process,
)

_CHILD_CODE = (
    "import subprocess, sys, time;"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)']);"
    "time.sleep(10)"
)
_TWO_CHILDREN_CODE = (
    "import subprocess, sys, time;"
    "[subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)']) for _ in range(2)];"
    "time.sleep(10)"
)
_FORKING_CODE = (
    "import subprocess, sys, time;"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)']);"
    "print(g.pid, flush=True);"
    "time.sleep(10)"
)


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _spawn_tree(code: str, **popen_kwargs) -> tuple[subprocess.Popen, ProcessTree]:
    """按生产路径起进程：先建作业 → 挂起创建 → 入作业 → 恢复 → 建树。"""
    job = create_job()
    flags = CREATE_SUSPENDED if job is not None else 0
    proc = subprocess.Popen([sys.executable, "-c", code], creationflags=flags, **popen_kwargs)
    if job is not None:
        assign_job(job, proc.pid)
        resume_process(proc.pid)  # 入作业成败都要恢复，否则子进程永远挂着
    return proc, ProcessTree(proc.pid, job)


def _reap(proc: subprocess.Popen, tree: ProcessTree) -> None:
    tree.kill()
    tree.close()
    proc.wait(timeout=5)


def test_descendants_lists_tree_members_without_root():
    proc, tree = _spawn_tree(_CHILD_CODE)
    try:
        assert _wait_until(lambda: len(tree.descendants()) == 1)
        assert proc.pid not in tree.descendants()  # 根进程不算成员
    finally:
        _reap(proc, tree)


def test_descendants_is_empty_without_children():
    proc, tree = _spawn_tree("import time; time.sleep(10)")
    try:
        assert tree.descendants() == ()
    finally:
        _reap(proc, tree)


def test_descendants_grows_buffer_when_truncated(monkeypatch):
    # 初始容量压到 1：两个成员必然先被截断，逼出"扩容重查"那条路径
    monkeypatch.setattr(process_tree, "_JOB_PIDS_INITIAL_CAPACITY", 1)
    proc, tree = _spawn_tree(_TWO_CHILDREN_CODE)
    try:
        assert _wait_until(lambda: len(tree.descendants()) == 2)
    finally:
        _reap(proc, tree)


def test_grandchild_forked_immediately_stays_in_job():
    """回归：子进程"一起来就 fork"时，孙进程不能逃出作业。

    入作业若发生在创建之后，这里必然漏掉孙进程（它会逃出作业，既枚举不到也杀不到）。
    """
    proc, tree = _spawn_tree(_FORKING_CODE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None
        grandchild = int(proc.stdout.readline())
        assert _wait_until(lambda: grandchild in tree.descendants())
    finally:
        _reap(proc, tree)


def test_terminate_pid_tolerates_already_exited_process(monkeypatch):
    """进程已退出时 TerminateProcess 报 ERROR_ACCESS_DENIED，不该往上抛。"""

    def _denied(pid: int, sig: int) -> None:
        raise PermissionError(5, "拒绝访问")

    monkeypatch.setattr(os, "kill", _denied)
    process_tree._terminate_pid(4242)  # 不抛即通过
