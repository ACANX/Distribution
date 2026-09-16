# -*- coding: utf-8 -*-
"""
控制台日志行首时间戳
====================

作用:
    给脚本输出的**每一行最左侧**加东八区(UTC+8)时间戳, 格式:

        yyMMdd.HHmmss.SSS      例: 260916.111545.857

用法:
    在 main() 开头、任何打印之前调用一次即可(幂等):

        from ConsoleLog import enableLogTimestamps
        enableLogTimestamps()

    stdout 与 stderr 一并生效。

为什么要按行缓冲(而不是简单地给每次 write 拼前缀):
    print() 是分多次 write() 落到流上的 —— 先写正文, 再单独写一个 "\\n"。若在每次
    write 前都拼一次前缀, 时间戳就会插到行中间(如 "abc260916... def")。所以这里
    攒到 "\\n" 才吐出一整行, 前缀打在真正的行首; 每个换行各取一次时间, 同一行内被
    拆分的多次 write 共用该行开头的那一个时间戳。

时区:
    固定东八区(北京时间)。注意 GitHub Actions 页面自身标注的是 UTC, 与这里相差
    8 小时, 对照日志时别错位。

依赖: 仅 Python 3 标准库。
"""

import sys
from datetime import datetime, timedelta, timezone
from typing import Any

# 时间戳格式: 两位年 + 月日 . 时分秒 . 毫秒
LOG_TS_FORMAT = "%y%m%d.%H%M%S"

# 东八区(北京时间)
BEIJING_TZ = timezone(timedelta(hours=8))


def logTimeStamp() -> str:
    """当前东八区时间的 yyMMdd.HHmmss.SSS 字符串。"""
    now = datetime.now(BEIJING_TZ)
    return "%s.%03d" % (now.strftime(LOG_TS_FORMAT), now.microsecond // 1000)


class TimestampedStream:
    """给每条输出行最左侧加时间戳的 stdout/stderr 代理(设计见模块 docstring)。"""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._pending = ""

    def write(self, text: str) -> int:
        self._pending += text
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self._stream.write("%s %s\n" % (logTimeStamp(), line))
        return len(text)

    def flush(self) -> None:
        # 收尾: 结尾没有换行的残留也要落下, 否则会丢掉最后一行
        if self._pending:
            self._stream.write("%s %s\n" % (logTimeStamp(), self._pending))
            self._pending = ""
        self._stream.flush()

    def __getattr__(self, name: str) -> Any:
        # isatty / encoding / fileno 等其余属性一律透传给被包装的流
        return getattr(self._stream, name)


def enableLogTimestamps() -> None:
    """给 stdout / stderr 装上行首时间戳(幂等, 重复调用不会套娃)。"""
    if not isinstance(sys.stdout, TimestampedStream):
        sys.stdout = TimestampedStream(sys.stdout)
    if not isinstance(sys.stderr, TimestampedStream):
        sys.stderr = TimestampedStream(sys.stderr)
