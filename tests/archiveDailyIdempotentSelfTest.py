#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日归档幂等性自测 —— 钉住 issue #40 的诉求。

背景：日归档文件头曾原样照抄 Latest.mvsv 的元数据，于是带上了 FetchTime / 采集时间 /
备注(Remark)。Latest 保留窗口(36 天)大于归档线(9 天)，**每天都有一批已归档的日文件被
重新归档** —— 这三个字段随采集时刻变化，于是数据一行未变、文件头却在变，产生无意义的提交。

本自测把口径钉死：
    归档头       —— 只描述「文件里是什么」，不含 FetchTime/采集时间/备注（中英写法都不行）；
    幂等         —— 数据无变化时重复归档，已归档文件的内容与 mtime 都不许变；
    存量清理     —— 头里残留这些字段的旧文件，重新归档时被就地清干净；
    数据变化     —— 真有新数据时照常更新，Latest 自己的采集信息不受影响。

跑法（临时 git 仓库 + 临时目录，不碰真实数据）：
    python3 tests/archiveDailyIdempotentSelfTest.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".github", "Python", "Quote"))

import Task02ArchiveDaily as T2  # noqa: E402
from common.config import Config  # noqa: E402
from common.mvsv import (  # noqa: E402
    MVSVData, MVSVMetadata, parse, render, serialize, strip_volatile_meta,
    _11_FIELD, _11_NAME, _11_NAME_EN, _11_TYPE,
)
from common.timeutil import BJT, ts_to_bjt_dt  # noqa: E402

FAILS = []
CODE = "TEST"
VOLATILE = ("FetchTime", "采集时间", "备注", "Remark")
FETCH_A = "2026-09-24 15:16:45"
FETCH_B = "2026-09-25 09:00:00"


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name,
                       ("  ← " + str(detail)) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class NullLog:
    """顶掉 logger：归档流程只要求 info/warning，日志内容不是本自测的对象。"""

    def info(self, _msg):
        pass

    def warning(self, _msg):
        pass

    def for_code(self, _code):
        return self


# ---------------------------------------------------------------------------
# 隔离环境：临时 git 仓库 + Config（不碰仓库里的真实数据）
# ---------------------------------------------------------------------------


def git(root, *args):
    subprocess.run(["git"] + list(args), cwd=str(root), check=True,
                   capture_output=True, text=True)


def make_env():
    root = Path(tempfile.mkdtemp(prefix="archive_daily_selftest_")).resolve()
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
        log_dir=root / "logs",
    )
    (config.data_dir / CODE).mkdir(parents=True)
    return root, config


def make_rows(day_offsets):
    """按「距今 N 天」的 BJT 10:00 起造分钟行（11 列）。"""
    now_bjt = datetime.now(BJT)
    rows = []
    for off in day_offsets:
        t0 = (now_bjt - timedelta(days=off)).replace(
            hour=10, minute=0, second=0, microsecond=0)
        for i in range(3):
            ts = int((t0 + timedelta(minutes=i)).timestamp())
            dt = ts_to_bjt_dt(ts)
            rows.append([str(ts), dt.strftime("%Y%m%d"), dt.strftime("%H%M%S"),
                         "100.0", "101.0", "", "", "10", "1000.0", "1.0", "1.0"])
    rows.sort(key=lambda r: int(r[0]))
    return rows


def write_latest(config, rows, fetch_time=FETCH_A, remark="汇总: c=1|h=2|l=3|cnt=4"):
    """写 Latest.mvsv —— 带上真实采集端会写的那三个易变字段。"""
    meta = MVSVMetadata()
    meta["标题"] = "%s 分钟级行情数据" % CODE
    meta["数据供应商"] = "FT"
    meta["字段"] = _11_FIELD
    meta["字段名称"] = _11_NAME
    meta["字段类型"] = _11_TYPE
    meta["证券代码"] = CODE
    meta["市场"] = "usd"
    meta["Title"] = "%s Minute Quote Data" % CODE
    meta["DataProvider"] = "FT"
    meta["Field"] = _11_FIELD
    meta["FieldName"] = _11_NAME_EN
    meta["FieldType"] = _11_TYPE
    meta["SecuCode"] = CODE
    meta["USC"] = CODE
    meta["Market"] = "usd"
    meta.extra["FetchTime"] = fetch_time
    meta.extra["备注"] = remark
    meta.extra["采集时间"] = fetch_time
    serialize(MVSVData(metadata=meta, rows=rows), str(config.data_dir / CODE / "Latest.mvsv"))


def archive_dir(config):
    return config.archive_dir / "Day" / CODE


def snapshot(config):
    """{文件名: (内容, mtime_ns)} —— 用来证明「文件没被动过」。"""
    return {p.name: (p.read_text(encoding="utf-8"), p.stat().st_mtime_ns)
            for p in sorted(archive_dir(config).glob("*.mvsv"))}


def header(path):
    return [line for line in path.read_text(encoding="utf-8").split("\n")
            if line.startswith("#")]


def run_task(config):
    """按真实条件跑一遍 Task02 的归档逻辑（push 用环境变量短路）。"""
    os.environ["QUOTE_SKIP_PUSH"] = "1"
    T2.process_code(CODE, config, NullLog())


# ---------------------------------------------------------------------------
# 一、纯逻辑：易变字段的识别与「无变化不落盘」
# ---------------------------------------------------------------------------


def testStripVolatileMeta():
    """中英两种写法都要清掉；非易变字段一个都不能少。"""
    meta = MVSVMetadata()
    meta["标题"] = "X"
    meta["SecuCode"] = CODE
    meta.extra["FetchTime"] = FETCH_A
    meta.extra["采集时间"] = FETCH_A
    meta.extra["Remark"] = "r"
    meta.extra["备注"] = "b"
    meta.extra["自定义键"] = "keep"
    removed = strip_volatile_meta(meta)
    check("易变字段：四个中英写法都被移除", sorted(removed) == sorted(VOLATILE), repr(removed))
    check("易变字段：标准键与非易变 extra 原样保留",
          meta.values == {"标题": "X", "SecuCode": CODE} and meta.extra == {"自定义键": "keep"},
          repr((meta.values, meta.extra)))

    narrow = MVSVMetadata()
    narrow.extra["FetchTime"] = FETCH_A
    narrow.extra["备注"] = "keep"
    strip_volatile_meta(narrow, keys=("FetchTime", "采集时间"))
    check("易变字段：可只清指定键（月归档的备注留给 Task03）",
          narrow.extra == {"备注": "keep"}, repr(narrow.extra))


def testSerializeOnlyIfChanged():
    """only_if_changed：内容一致就不碰文件，返回 False；有变化才写、返回 True。"""
    tmp = Path(tempfile.mkdtemp(prefix="archive_daily_selftest_")).resolve()
    try:
        data = MVSVData(metadata=MVSVMetadata(), rows=[["1", "2", "3"]])
        p = tmp / "a.mvsv"
        check("串写：首次写入返回 True", serialize(data, str(p), only_if_changed=True) is True)
        before = p.stat().st_mtime_ns
        time.sleep(0.02)
        check("串写：内容一致时返回 False",
              serialize(data, str(p), only_if_changed=True) is False)
        check("串写：内容一致时不重写文件（mtime 不变）", p.stat().st_mtime_ns == before)
        data.rows.append(["4", "5", "6"])
        check("串写：内容变化时返回 True",
              serialize(data, str(p), only_if_changed=True) is True)
        check("串写：render 与落盘内容一致", p.read_text(encoding="utf-8") == render(data))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 二、端到端：归档头口径
# ---------------------------------------------------------------------------


def testHeaderStaysClean():
    """首次归档：头里只剩数据描述字段，Latest 自己的采集信息不进归档。"""
    root, config = make_env()
    try:
        write_latest(config, make_rows([15, 14, 13]))
        run_task(config)
        files = sorted(archive_dir(config).glob("*.mvsv"))
        check("归档头：3 天各生成一个日归档文件", len(files) == 3,
              [f.name for f in files])
        for f in files:
            lines = header(f)
            joined = "\n".join(lines)
            dirty = [key for key in VOLATILE if ("# %s :" % key) in joined]
            check("归档头：%s 不含 FetchTime/采集时间/备注(Remark)" % f.name, not dirty, dirty)
            check("归档头：%s 保留了数据描述字段" % f.name,
                  "# 证券代码 : %s" % CODE in joined and "# 市场 : usd" in joined,
                  joined)
            check("归档头：%s 计数与数据行数一致" % f.name,
                  "# 计数 : 3" in joined, joined)
            rows = parse(str(f)).rows
            check("归档头：%s 数据行完整（3 行 11 列）" % f.name,
                  len(rows) == 3 and all(len(r) == 11 for r in rows), len(rows))

        latest_head = "\n".join(header(config.data_dir / CODE / "Latest.mvsv"))
        check("Latest：自身的 FetchTime/采集时间/备注不受影响",
              all(("# %s :" % key) in latest_head for key in ("FetchTime", "采集时间", "备注")),
              latest_head)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 三、核心：数据无变化时重复归档不动已归档文件
# ---------------------------------------------------------------------------


def testRepeatArchiveKeepsFilesIntact():
    """只换了采集时刻、数据一行未变 —— 已归档文件的内容与 mtime 都不许变。"""
    root, config = make_env()
    try:
        rows = make_rows([15, 14, 13])
        write_latest(config, rows, fetch_time=FETCH_A, remark="汇总: c=1|h=2|l=3|cnt=4")
        run_task(config)
        before = snapshot(config)
        check("重复归档：首次归档产出 3 个文件", len(before) == 3, sorted(before))

        # 下一次采集：Latest 的时间戳与汇总都变了，数据行一模一样
        time.sleep(0.05)
        write_latest(config, rows, fetch_time=FETCH_B, remark="汇总: c=9|h=9|l=9|cnt=9")
        run_task(config)
        after = snapshot(config)

        check("重复归档：文件集合不变", set(before) == set(after),
              (sorted(before), sorted(after)))
        changed = [name for name in before if before[name][0] != after[name][0]]
        check("重复归档：内容逐字节一致", not changed, changed)
        touched = [name for name in before if before[name][1] != after[name][1]]
        check("重复归档：文件未被重写（mtime 不变）", not touched, touched)

        # 再跑一轮，仍应纹丝不动（可放心每日重跑）
        time.sleep(0.05)
        run_task(config)
        third = snapshot(config)
        check("重复归档：连跑第三轮仍不动文件",
              all(before[n][1] == third[n][1] for n in before), sorted(third))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def testRepeatArchiveMakesNoCommit():
    """无变化的一轮不该产生提交 —— 否则就是无意义的仓库噪音。"""
    root, config = make_env()
    try:
        rows = make_rows([15, 14, 13])
        write_latest(config, rows)
        run_task(config)
        git(root, "add", "-A")
        git(root, "commit", "-m", "seed archive")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root),
                              capture_output=True, text=True, check=True).stdout.strip()

        time.sleep(0.05)
        write_latest(config, rows, fetch_time=FETCH_B, remark="汇总: c=9")
        run_task(config)
        after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root),
                               capture_output=True, text=True, check=True).stdout.strip()
        check("无变化轮次：HEAD 未动（没有空提交）", head == after, (head, after))
        status = subprocess.run(["git", "status", "--porcelain", "Archive"], cwd=str(root),
                                capture_output=True, text=True, check=True).stdout.strip()
        check("无变化轮次：归档区工作区干净", not status, status)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 四、存量与变更：旧文件被就地清理；新数据照常并入
# ---------------------------------------------------------------------------


def testLegacyArchiveHeaderGetsCleaned():
    """头里残留易变字段的旧文件（修复前归档的），重新归档时被清干净且数据不丢。"""
    root, config = make_env()
    try:
        rows = make_rows([15, 14, 13])
        write_latest(config, rows)
        run_task(config)
        target = sorted(archive_dir(config).glob("*.mvsv"))[0]
        day_rows = parse(str(target)).rows
        others = {n: v for n, v in snapshot(config).items() if n != target.name}

        # 模拟存量文件：把易变字段插回文件头
        text = target.read_text(encoding="utf-8")
        meta_part, data_part = text.split("\n\n", 1)
        meta_part += ('\n# FetchTime : "%s"\n# Remark : "汇总: c=1"\n# 采集时间 : "%s"\n# 备注 : "汇总: c=1"'
                      % (FETCH_A, FETCH_A))
        target.write_text(meta_part + "\n\n" + data_part, encoding="utf-8")
        check("存量清理：旧文件确实带上了易变字段",
              all(("# %s :" % key) in "\n".join(header(target)) for key in VOLATILE),
              header(target))

        write_latest(config, rows, fetch_time=FETCH_B, remark="汇总: c=9")
        run_task(config)

        joined = "\n".join(header(target))
        left = [key for key in VOLATILE if ("# %s :" % key) in joined]
        check("存量清理：旧文件头里的易变字段被清掉", not left, left)
        check("存量清理：数据行一行不少、顺序不变",
              parse(str(target)).rows == day_rows, len(parse(str(target)).rows))
        now = snapshot(config)
        kept = [n for n in others if others[n][0] != now[n][0]]
        check("存量清理：其余文件保持原样", not kept, kept)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def testDataChangeStillUpdatesArchive():
    """真有新数据进来时必须照常更新 —— 幂等不能变成「不干活」。"""
    root, config = make_env()
    try:
        rows = make_rows([15, 14, 13])
        write_latest(config, rows)
        run_task(config)
        before = snapshot(config)
        picked = sorted(before)[0]
        before_rows = parse(str(archive_dir(config) / picked)).rows

        # 给同一天补一条新数据（同一天多了一个分钟的行情）
        extra_ts = int(parse(str(archive_dir(config) / picked)).rows[-1][0]) + 60
        dt = ts_to_bjt_dt(extra_ts)
        rows.append([str(extra_ts), dt.strftime("%Y%m%d"), dt.strftime("%H%M%S"),
                     "101.0", "102.0", "", "", "20", "2000.0", "1.0", "1.0"])
        rows.sort(key=lambda r: int(r[0]))
        time.sleep(0.05)
        write_latest(config, rows, fetch_time=FETCH_B)
        run_task(config)

        after = snapshot(config)
        after_rows = parse(str(archive_dir(config) / picked)).rows
        check("数据变更：对应天的归档行数 +1", len(after_rows) == len(before_rows) + 1,
              (len(before_rows), len(after_rows)))
        check("数据变更：该文件确已重写", before[picked][0] != after[picked][0])
        untouched = [n for n in before if n != picked and before[n][0] != after[n][0]]
        check("数据变更：未受影响的其余文件不动", not untouched, untouched)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    testStripVolatileMeta()
    testSerializeOnlyIfChanged()
    testHeaderStaysClean()
    testRepeatArchiveKeepsFilesIntact()
    testRepeatArchiveMakesNoCommit()
    testLegacyArchiveHeaderGetsCleaned()
    testDataChangeStillUpdatesArchive()
    print("-" * 60)
    if FAILS:
        print("FAILED %d: %s" % (len(FAILS), "、".join(FAILS)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
