#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""月归档幂等性自测 —— 月归档重复归档不得改动已归档文件。

背景：`Task03ArchiveMonthly.py` 每月把月内日归档并进月归档。它有两处会让重复归档
改写已归档文件（或让功能静默失效）的地方：

    1. 「缺失交易日」备注原本用 `metadata.get('备注')` 读、`metadata['备注'] = …` 写。
       但 备注/Remark 不在 `MVSVMetadata.STANDARD_KEYS` 里，`parse()` 把它们读进
       **extra** 区，`to_lines()` 也只从 extra 输出 —— 于是旧实现既读不到旧值，写回
       的也永远不会落盘，缺失交易日备注实际处于失效状态。
    2. 写入没有「内容没变就不落盘」的约定，`git add` + commit 无条件执行。

本自测把口径钉死：
    备注读写   —— 落在 extra、能被序列化输出，且两个写法保持一致；
    幂等       —— 重复归档时月归档内容与 mtime 都不许变；
    备注刷新   —— 日归档补齐后缺失交易日从备注里消失；
    不误报     —— 月归档里已有的交易日，不该因日归档被清理而被当成缺失。

跑法（临时 git 仓库 + 临时目录，不碰真实数据）：
    python3 tests/archiveMonthlyIdempotentSelfTest.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".github", "Python", "Quote"))

import Task03ArchiveMonthly as T3  # noqa: E402
from common.config import Config  # noqa: E402
from common.mvsv import (  # noqa: E402
    MVSVData, MVSVMetadata, parse, serialize, render,
    _11_FIELD, _11_NAME, _11_NAME_EN, _11_TYPE,
)
from common.timeutil import BJT, UTC, last_complete_month  # noqa: E402

FAILS = []
CODE = "BTC"          # crypto：该月每天都是交易日，缺失日期的断言不受节假日影响
MARKET = "crypto"

MISSING_PREFIX = T3.MISSING_DAYS_REMARK_PREFIX


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name,
                       ("  ← " + str(detail)) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class NullLog:
    def info(self, _msg):
        pass

    def warning(self, _msg):
        pass


# ---------------------------------------------------------------------------
# 隔离环境
# ---------------------------------------------------------------------------


def git(root, *args):
    subprocess.run(["git"] + list(args), cwd=str(root), check=True,
                   capture_output=True, text=True)


def make_env():
    root = Path(tempfile.mkdtemp(prefix="archive_monthly_selftest_")).resolve()
    git(root, "init")
    git(root, "config", "user.email", "selftest@local")
    git(root, "config", "user.name", "selftest")
    config = Config(
        repo_root=root,
        data_dir=root / "Data" / "Finv" / "SecuQuote",
        archive_dir=root / "Archive" / "Finv" / "SecuQuote",
        exec_log_data_dir=root / "Data" / "Finv" / "SecuQuoteExecLog",
        exec_log_archive_dir=root / "Archive" / "Finv" / "SecuQuoteExecLog",
        codes=[CODE],
        latest_window_days=36,
        daily_archive_after_days=9,
        monthly_delete_lag_months=2,
        log_dir=root / "logs",
    )
    (config.data_dir / CODE).mkdir(parents=True)
    return root, config


def day_list(month_str):
    """该月每一天的 datetime（BJT 10:00），用于造日归档。"""
    year, month = int(month_str[:4]), int(month_str[4:6])
    out, d = [], 1
    while True:
        try:
            out.append(datetime(year, month, d, 10, 0, 0, tzinfo=BJT))
        except ValueError:
            break
        d += 1
    return out


def write_day(config, month_str, day_index, rows=3):
    """往 Day/ 下写一天的日归档，返回 yyyyMMdd。"""
    days = day_list(month_str)
    dt = days[day_index]
    ds = dt.strftime("%Y%m%d")
    meta = MVSVMetadata()
    meta["标题"] = "%s 分钟级行情数据" % CODE
    meta["数据供应商"] = "FT"
    meta["字段"] = _11_FIELD
    meta["字段名称"] = _11_NAME
    meta["字段类型"] = _11_TYPE
    meta["证券代码"] = CODE
    meta["市场"] = MARKET
    meta["Title"] = "%s Minute Quote Data" % CODE
    meta["DataProvider"] = "FT"
    meta["Field"] = _11_FIELD
    meta["FieldName"] = _11_NAME_EN
    meta["FieldType"] = _11_TYPE
    meta["SecuCode"] = CODE
    meta["USC"] = CODE
    meta["Market"] = MARKET
    row_list = []
    for i in range(rows):
        ts = int(dt.timestamp()) + i * 60
        row_list.append([str(ts), ds, dt.strftime("%H%M%S") if i == 0 else
                         datetime.fromtimestamp(ts, tz=BJT).strftime("%H%M%S"),
                         "100.0", "101.0", "", "", "10", "1000.0", "1.0", "1.0"])
    d = config.archive_dir / "Day" / CODE
    d.mkdir(parents=True, exist_ok=True)
    serialize(MVSVData(metadata=meta, rows=row_list), str(d / f"{CODE}_Min_{ds}.mvsv"))
    return ds


def month_path(config, month_str):
    return config.archive_dir / month_str[:4] / CODE / f"{CODE}_Min_{month_str}.mvsv"


def snapshot(path):
    return (path.read_text(encoding="utf-8"), path.stat().st_mtime_ns) if path.exists() else None


def header(path):
    return [ln for ln in path.read_text(encoding="utf-8").split("\n") if ln.startswith("#")]


def run_task(config, month_str):
    os.environ["QUOTE_SKIP_PUSH"] = "1"
    year, month = int(month_str[:4]), int(month_str[4:6])
    s = datetime(year, month, 1, tzinfo=BJT)
    e = datetime(year + 1, 1, 1, tzinfo=BJT) if month == 12 else \
        datetime(year, month + 1, 1, tzinfo=BJT)
    T3.process_code(CODE, config,
                    int(s.astimezone(UTC).timestamp()),
                    int(e.astimezone(UTC).timestamp()),
                    month_str, NullLog())


# ---------------------------------------------------------------------------
# 一、备注的读写路径
# ---------------------------------------------------------------------------


def testRemarkLivesInExtra():
    """写入必须落在 extra（to_lines 只从这里输出 备注/Remark），而不是 values。"""
    meta = MVSVMetadata()
    meta["标题"] = "X"
    T3.set_missing_days_remark(meta, ["20260803", "20260804"])
    check("备注：写进 extra 而不是 values",
          meta.extra.get("备注") and not meta.values.get("备注"), repr(meta.values))
    check("备注：英文写法同步写入", meta.extra.get("Remark") == meta.extra.get("备注"),
          repr(meta.extra))
    out = "\n".join(meta.to_lines())
    check("备注：能被序列化输出", MISSING_PREFIX + "20260803, 20260804" in out, out)

    # 读回：解析后应能读到，并刷新（去掉旧缺失、写入新缺失）
    reparsed = parse_roundtrip(meta)
    T3.set_missing_days_remark(reparsed, ["20260805"])
    check("备注：重算时旧缺失日被替换、非缺失内容保留",
          reparsed.extra["备注"] == MISSING_PREFIX + "20260805", repr(reparsed.extra["备注"]))

    # 无缺失时应把两个写法都清掉
    T3.set_missing_days_remark(reparsed, [])
    check("备注：无缺失时两个写法都清掉",
          "备注" not in reparsed.extra and "Remark" not in reparsed.extra, repr(reparsed.extra))


def parse_roundtrip(meta):
    """序列化再解析，模拟「下一次归档读回已有文件」。"""
    text = "\n".join(meta.to_lines()) + "\n\n"
    meta_lines = [ln for ln in text.split("\n") if ln.startswith("#")]
    return MVSVMetadata.from_lines(meta_lines)


def testLegacyRemarkInValuesStillRead():
    """历史文件可能把备注混在 values：读取要能兼容，但写回仍落在 extra。"""
    meta = MVSVMetadata()
    meta["备注"] = "汇总: c=1; " + MISSING_PREFIX + "20260801"
    T3.set_missing_days_remark(meta, [])
    check("备注：values 里的历史残留能被读到并清理",
          meta.extra.get("备注") == "汇总: c=1" and "备注" not in meta.values,
          repr((meta.values, meta.extra)))


# ---------------------------------------------------------------------------
# 二、重复归档与备注刷新
# ---------------------------------------------------------------------------


def testMonthlyIdempotent():
    root, config = make_env()
    try:
        _, _, month_str = last_complete_month()
        days = day_list(month_str)
        for i in range(len(days) - 2):          # 只缺最后 2 天
            write_day(config, month_str, i)

        run_task(config, month_str)
        mp = month_path(config, month_str)
        before = snapshot(mp)
        check("月归档：首次生成", before is not None, str(mp))
        head = "\n".join(header(mp))
        check("月归档：备注写明了缺失交易日",
              MISSING_PREFIX in head and days[-1].strftime("%Y%m%d") in head, head)
        check("月归档：不含 FetchTime/采集时间",
              "# FetchTime :" not in head and "# 采集时间 :" not in head, head)

        time.sleep(0.05)
        run_task(config, month_str)
        after = snapshot(mp)
        check("月归档：重复归档内容逐字节一致", before[0] == after[0])
        check("月归档：重复归档未被重写（mtime 不变）", before[1] == after[1])

        time.sleep(0.05)
        run_task(config, month_str)
        third = snapshot(mp)
        check("月归档：连跑第三轮仍不动文件", before[1] == third[1])
    finally:
        shutil.rmtree(root, ignore_errors=True)


def testMissingDaysRefreshWhenDailyArrives():
    """补齐日归档后，缺失交易日应从备注里消失（且仍幂等）。"""
    root, config = make_env()
    try:
        _, _, month_str = last_complete_month()
        days = day_list(month_str)
        for i in range(len(days) - 2):
            write_day(config, month_str, i)
        run_task(config, month_str)
        mp = month_path(config, month_str)
        missing_day = days[-2].strftime("%Y%m%d")
        check("备注刷新：补齐前该日被标记为缺失",
              missing_day in "\n".join(header(mp)), header(mp))

        write_day(config, month_str, len(days) - 2)    # 补上倒数第二天
        time.sleep(0.05)
        run_task(config, month_str)
        head = "\n".join(header(mp))
        check("备注刷新：补齐后该日不再算缺失", missing_day not in head, head)
        check("备注刷新：仍未补齐的那天保留在备注里",
              days[-1].strftime("%Y%m%d") in head, head)

        frozen = snapshot(mp)
        time.sleep(0.05)
        run_task(config, month_str)
        check("备注刷新：刷新后再次归档保持稳定", frozen[0] == snapshot(mp)[0])
    finally:
        shutil.rmtree(root, ignore_errors=True)


def testAlreadyArchivedDaysNotReportedMissing():
    """日归档被清理后，月归档里已有的交易日不该被当成缺失。"""
    root, config = make_env()
    try:
        _, _, month_str = last_complete_month()
        days = day_list(month_str)
        for i in range(len(days)):
            write_day(config, month_str, i)
        run_task(config, month_str)
        mp = month_path(config, month_str)
        check("不误报：日归档齐全时备注里没有缺失交易日",
              MISSING_PREFIX not in "\n".join(header(mp)), header(mp))

        # 模拟 Task03 到期清理日归档：把 Day/ 下的文件全删掉，只留月归档
        day_dir = config.archive_dir / "Day" / CODE
        for f in day_dir.glob("*.mvsv"):
            f.unlink()
        existing = parse(str(mp))
        missing = T3.check_completeness(
            CODE, month_str, [], config, existing.metadata, NullLog(),
            already_archived=T3.archived_dates_of(existing))
        check("不误报：日归档被清空后，月归档已有的天不算缺失", not missing, missing)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    testRemarkLivesInExtra()
    testLegacyRemarkInValuesStillRead()
    testMonthlyIdempotent()
    testMissingDaysRefreshWhenDailyArrives()
    testAlreadyArchivedDaysNotReportedMissing()
    print("-" * 60)
    if FAILS:
        print("FAILED %d: %s" % (len(FAILS), "、".join(FAILS)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
