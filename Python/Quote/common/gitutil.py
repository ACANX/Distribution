"""
Git 操作封装

使用 subprocess，支持 add/commit/rm/push，含 push_with_retry 增量发布。
"""

import os
import subprocess
from pathlib import Path
from typing import List, Optional


_git_dir: Optional[str] = None


def _run(args: List[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run git command and return result."""
    cmd = ["git"] + args
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd or _git_dir)
    if result.returncode != 0:
        raise RuntimeError(
            f"git error: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def add(path: str, cwd: Optional[str] = None) -> None:
    """git add <path>"""
    _run(["add", path], cwd)


def commit(message: str, cwd: Optional[str] = None) -> str:
    """git commit -m <msg>, return SHA or empty string."""
    result = _run(["commit", "-m", message], cwd)
    if "nothing to commit" in result.stdout:
        return ""
    for line in result.stdout.split("\n"):
        if line.startswith("["):
            parts = line.split("]")[0].split()
            if len(parts) >= 2:
                return parts[-1]
    return ""


def rm(path: str, cwd: Optional[str] = None) -> None:
    """git rm <path>"""
    _run(["rm", path], cwd)


def rm_many(paths, cwd: Optional[str] = None, batch_size: int = 400) -> None:
    """git rm 批量删除多个文件（分批执行，避免命令行过长）。

    Args:
        paths: 待删除文件路径列表（绝对路径，会转换为相对 cwd 以缩短命令行）
        batch_size: 每批文件数，防止单条命令参数超长（Windows 命令行约 32KB）
    """
    base = Path(cwd).resolve() if cwd else Path.cwd().resolve()
    rel = []
    for p in paths:
        ap = str(Path(p).resolve())
        try:
            rel.append(os.path.relpath(ap, base))
        except ValueError:
            # 不同盘符无法取相对路径，退回绝对路径
            rel.append(ap)
    for i in range(0, len(rel), batch_size):
        _run(["rm", "--"] + rel[i:i + batch_size], cwd)


def _get_current_branch(cwd=None) -> str:
    """Detect current git branch name."""
    result = _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return result.stdout.strip()


def push_with_retry(ref: Optional[str] = None, retries: int = 1,
                    cwd: Optional[str] = None) -> None:
    """Push with retry on failure (git pull --rebase + retry).
    If ref is None, uses the current branch.
    Skipped if QUOTE_SKIP_PUSH env var is set (local testing).
    """
    import os as _os
    if _os.environ.get("QUOTE_SKIP_PUSH"):
        import logging as _log
        _log.getLogger("quote.gitutil").info("QUOTE_SKIP_PUSH set, push skipped")
        return
    if ref is None:
        ref = _get_current_branch(cwd)
    for attempt in range(retries + 1):
        try:
            _run(["push", "origin", ref], cwd)
            return
        except RuntimeError:
            if attempt < retries:
                _run(["pull", "--rebase", "origin", ref], cwd)
            else:
                raise
