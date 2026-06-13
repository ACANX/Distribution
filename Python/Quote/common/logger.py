"""
中文结构化日志模块

格式: [ISO8601][LEVEL][task][code] 消息
输出: stdout + 文件 (Python/Quote/logs/{task}_{yyyymmdd}.log)
"""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

BJT = timezone(timedelta(hours=8))


class QuoteFormatter(logging.Formatter):
    """日志格式化器，为未设置上下文的记录提供默认值"""

    def format(self, record):
        if not hasattr(record, 'code'):
            record.code = ''
        if not hasattr(record, 'task'):
            record.task = ''
        return super().format(record)


class _CodeLogger:
    """绑定 code 上下文的日志代理"""

    def __init__(self, logger, task: str, code: str):
        self._logger = logger
        self._task = task
        self._code = code

    def _log(self, level, msg, *args, **kwargs):
        extra = kwargs.pop('extra', {})
        extra.setdefault('task', self._task)
        extra.setdefault('code', self._code)
        self._logger.log(level, msg, *args, extra=extra, **kwargs)

    def debug(self, msg, *args, **kwargs):
        self._log(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        self._log(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self._log(logging.WARNING, msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        self._log(logging.ERROR, msg, *args, **kwargs)

    def critical(self, msg, *args, **kwargs):
        self._log(logging.CRITICAL, msg, *args, **kwargs)


class TaskLogger:
    """任务级日志器，提供 for_code() 绑定证券代码"""

    def __init__(self, logger: logging.Logger, task: str):
        self._logger = logger
        self._task = task

    def for_code(self, code: str) -> _CodeLogger:
        return _CodeLogger(self._logger, self._task, code)

    def _log(self, level, msg, *args, **kwargs):
        extra = kwargs.pop('extra', {})
        extra.setdefault('task', self._task)
        extra.setdefault('code', '')
        self._logger.log(level, msg, *args, extra=extra, **kwargs)

    def debug(self, msg, *args, **kwargs):
        self._log(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        self._log(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self._log(logging.WARNING, msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        self._log(logging.ERROR, msg, *args, **kwargs)

    def critical(self, msg, *args, **kwargs):
        self._log(logging.CRITICAL, msg, *args, **kwargs)


def _cleanup_old_logs(log_dir: Path, retention_days: int):
    """删除早于 retention_days 的 .log 文件"""
    if not log_dir.is_dir():
        return
    now = datetime.now(BJT)
    cutoff = now - timedelta(days=retention_days)
    for f in sorted(log_dir.iterdir()):
        if f.suffix != '.log':
            continue
        parts = f.stem.split('_')
        if len(parts) < 2:
            continue
        try:
            date_str = parts[-1]
            file_date = datetime.strptime(date_str, '%Y%m%d').replace(tzinfo=BJT)
            if file_date < cutoff:
                f.unlink()
        except (ValueError, IndexError):
            continue


def setup_logger(task_name: str, log_dir: str,
                 retention_days: int = 14) -> TaskLogger:
    """初始化日志系统

    Args:
        task_name: 任务名称（如 task1/task2/task3）
        log_dir: 日志目录路径
        retention_days: 日志保留天数，启动时自动清理更早的日志

    Returns:
        TaskLogger 实例
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(f'quote.{task_name}')
    logger.setLevel(logging.INFO)

    # 避免重复添加 handler
    if logger.handlers:
        return TaskLogger(logger, task_name)

    fmt = QuoteFormatter(
        '[%(asctime)s][%(levelname)s][%(task)s][%(code)s] %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S'
    )

    # stdout
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    # 文件
    log_file = log_path / f'{task_name}_{datetime.now(BJT):%Y%m%d}.log'
    fh = logging.FileHandler(str(log_file), encoding='utf-8')
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # 滚动清理
    _cleanup_old_logs(log_path, retention_days)

    return TaskLogger(logger, task_name)
