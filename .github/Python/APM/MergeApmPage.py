#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MergeApmPage —— Page 埋点 Data 侧批次按日聚合为 JSONL(Contents API 提交)
================================================================================

历史版本说明
------------
    本脚本原先位于 apm 分支顶层 Python/ 目录, 把 Data/Meta/WebMMCP/APM/Page/
    下全部批次 json 聚合为一个 Archive/Meta/WebMMCP/APM/Page/{时间戳}.json
    (时间戳命名, 重复运行产生重复内容的新文件)。

本次变更(2026-09)
-----------------
    1. 目录平移: 顶层 Python/ -> .github/Python/APM/(与 APMJsonlConvert.py
       等脚本统一收口);
    2. 职责升级: 由"全量聚合为时间戳 .json"改为"按日聚合为 JSONL" —— 只聚合
       **指定某一天**(缺省为运行时刻的北京时间日历日; --date YYYYMMDD 可覆盖,
       可重复传多日) Data 侧的批次, 输出
           Archive/Meta/WebMMCP/APM/Page/APM_Page_MetaCmsPage_DAY_ACANX_{yyyyMMdd}.jsonl
       并经 GitHub Contents API 提交到 apm 分支(文件修改/新增一律走
       Contents API, 不用 git push)。
    3. Archive 侧旧的 {时间戳}.json 合并产物维持原样不动, 但不再滚动进新的
       JSONL —— 那部分存量已由 apm.DailyJsonlArchive 的首轮全量转换覆盖。

    聚合/去重/分组口径与 .github/Python/APM/APMJsonlConvert.py(主转换)完全
    一致: 按记录 ts(13 位毫秒时间戳)换算北京时间日历日分组(**文件名日期
    不可靠**, 存量有按 UTC 命名的批次), 整行去重, 远端已有行原样保留。

用法
----
    python3 .github/Python/APM/MergeApmPage.py                # 聚合"今天"
    python3 .github/Python/APM/MergeApmPage.py --date 20260629
    python3 .github/Python/APM/MergeApmPage.py --dry-run

    本文件是 .github/Python/APM/APMJsonlConvert.DailyJsonl.py 的薄封装
    (限定 --type Page 且只扫 Data 侧), 分组/合并/提交逻辑全部在前者维护。

依赖: 仅 Python 3 标准库; 令牌经环境变量 GIT_COMMIT_TOKEN 注入。
"""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

# 同目录的 APMJsonlConvert.DailyJsonl.py 文件名带点, 不能作为普通模块名 import,
# 用 importlib 按文件路径加载(模块名自定, 不与任何真实模块冲突)
_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "apm_daily_jsonl", _HERE / "APMJsonlConvert.DailyJsonl.py")
_mod = importlib.util.module_from_spec(_SPEC)
sys.modules["apm_daily_jsonl"] = _mod
_SPEC.loader.exec_module(_mod)

TYPE = "Page"

# ── 路径(与原 MergeApmPage.py 的 DATA_DIR 一致) ─────────────────────────────
DATA_DIR = "Data/Meta/WebMMCP/APM/" + TYPE


def main() -> None:
    # 缺省日期 = 运行时刻的北京时间日历日(保持旧脚本"运行即处理当天"的直觉);
    # --date 可覆盖且可重复(多日), 其余参数原样透传
    argv = sys.argv[1:]
    today = datetime.now(_mod.BJT).strftime("%Y%m%d")
    if not any(a == "--date" or a.startswith("--date=") for a in argv):
        argv = ["--date", today] + argv
    argv = ["--type", TYPE, "--data-only"] + argv
    sys.argv = [_mod.__file__] + argv
    _mod.main()


if __name__ == "__main__":
    main()
