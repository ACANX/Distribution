#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task18 自测 —— 老链路 SecuQuote 存量切片转换为新链路格式。

被测脚本：.github/Python/Quote/Task18SecuQuoteToV5Convert.py

本自测把口径钉死：
    格式转换 —— 11 列（Open,Close,Low,High）与 6 列（无 Date/Time）两种源文件
                都能按字段名识别并转成新链路 12 列（Open,High,Low,Close,…,LastClose）；
    文件头 —— 全 16 个键、顺序与 META_ORDER 一致，字段名/类型/市场等取自新链路模板，
              标题/Symbol/SecuCode/USC 按目标证券改写，Count 与实际行数一致；
    切片 —— 每个输出文件不超过指定行数，文件名符合 <代码>_Min_<yyyyMMdd>_<HHmmss>.mvsv；
    前收价 —— 默认回填为上一行收盘价（首行留空），可关闭；
    幂等 —— 重复运行内容相同的切片跳过，不新增重复文件；
    安全 —— 默认不删除源文件；模板缺失时该证券失败且不写文件；
    兼容 —— 输出能被 Task11AggregateLatest 的解析/合并逻辑直接消费（12 列、按 ts 去重）。

跑法（仅标准库，全部在临时目录里做，不碰仓库真实数据）：
    python3 .github/Python/tests/secuQuoteToV5ConvertSelfTest.py
"""

import io
import os
import re
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / ".github" / "Python" / "Quote"))

import Task18SecuQuoteToV5Convert as C  # noqa: E402
import Task11AggregateLatest as AGG       # noqa: E402  （只借用解析/合并函数）

FAILS = []
CODE = "000001"
SLICE_PATTERN = re.compile(r"^%s_Min_\d{8}_\d{6}\.mvsv$" % CODE)

# 新链路模板（与仓库中 Data/Finv/SecurityQuoteV5/FTMM/Min/000001/Latest.mvsv 同构）
TARGET_TEMPLATE = """# 标题 : 000001 分钟级行情数据
# 字段名称 : "时间戳|日期|时间|开盘价|最高价|最低价|收盘价|成交量|成交额|涨跌值|涨跌幅|前收价"
# DataProvider : FTMM
# Field : "ts|d|t|o|h|l|c|v|t|cp|cr|lc"
# FieldName : "Timestamp|Date|Time|Open|High|Low|Close|Volume|Turnover|ChangePrice|ChangeRatio|LastClose"
# FieldType : "Int|Int|Int|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal"
# TypeKLine : Min
# Count : 1
# Symbol : SZ.000001
# SecuCode : 000001
# USC : 000001
# Name : 平安银行
# Region : CN
# Market : SZ
# TimeZone : Asia/Shanghai
# Currency : CNY

1789695000|20260918|093000||||11.59|373900|4333501|-0.02|-0.17|
"""

# 老链路 A：11 列聚合结果（Open,Close,Low,High）
SOURCE_11 = """# 标题 : 000001 分钟级行情数据
# 数据供应商 : FT
# 字段 : "Ts|Date|Time|Open|Close|Low|High|Volume|Turnover|ChangePrice|ChangeRatio"
# 字段名称 : "时间戳|日期|时间|开盘价|收盘价|最低价|最高价|成交量|成交额|涨跌值|涨跌幅"
# 字段类型 : "int|int|int|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|str"
# 计数 : 5
# 证券代码 : 000001
# 市场 : cny
# Title : 000001 Minute Quote Data
# DataProvider : FT
# FieldName : "Ts|Date|Time|Open|Close|Low|High|Volume|Turnover|ChangePrice|ChangeRatio"
# FieldType : "int|int|int|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|str"
# Count : 5
# SecuCode : 000001
# USC : 000001
# Market : cny
# FetchTime : "2026-10-21 14:31:45"
# 备注 : "汇总: c=17630|h=17390|l=16250|cnt=1161"
# 采集时间 : "2026-10-21 14:31:45"

1788139800|20260831|093000|16.72|16.66|||73300|1221178|-0.06|-0.36
1788139860|20260831|093100|16.66|16.76|||215600|3582062|0.04|0.24
1788139920|20260831|093200|16.76|16.76|||95000|1590749|0.04|0.24
1788139980|20260831|093300|16.76|16.80|16.70|16.85|10000|1678000|0.14|0.84
1788140040|20260831|093400|16.80|16.70|16.60|16.90|20000|3340000|0.04|0.24
"""

# 老链路 B：6 列待聚合原始文件（Ts|Close|Volume|Turnover|ChangePct|ChangePrice）
SOURCE_6 = """# 标题 : "000001 分钟级行情数据"
# 数据供应商 : FT
# 字段 : ts|c|v|t|r|cp
# 字段名称 : 时间戳(UTC)|收盘价|成交量|成交额|涨跌幅(%)|涨跌值
# 字段类型 : int|float|int|float|str|float
# 计数 : 3
# 采集时间 : "2026-10-21 15:56:45"
# 证券代码 : 000001
# 备注 : "汇总: c=17630|h=17390|l=16250|cnt=1205"
# Title : "000001 Minute Quote Data"
# DataProvider : FT
# Field : ts|c|v|t|r|cp
# FieldName : Ts|Close|Volume|Turnover|ChangePct|ChangePrice
# FieldType : int|float|int|float|str|float
# Count : 3
# FetchTime : "2026-10-21 15:56:45"
# SecuCode : 000001

1792027800|17.30|64900|1122770|-1.87|-0.33
1792027860|17.39|118100|2055529|-1.36|-0.24
1792027920|17.33|70200|1215090|-1.70|-0.30
"""


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name,
                       ("  ← " + str(detail)) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def build_tree(tmp, *, with_template=True, source_11=SOURCE_11,
               source_6=SOURCE_6, bad_line=None):
    """搭建临时仓库结构：老目录（11 列 + 6 列）+ 新目录模板"""
    root = Path(tmp)
    src = root / C.SOURCE_SUBDIR / CODE
    dst = root / C.TARGET_SUBDIR / CODE
    src.mkdir(parents=True, exist_ok=True)
    (src / "Latest.mvsv").write_text(source_11, encoding="utf-8")
    extra = source_6 if bad_line is None else source_6 + bad_line
    (src / ("%s_Min_20261021_155645.mvsv" % CODE)).write_text(
        extra, encoding="utf-8")
    if with_template:
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "Latest.mvsv").write_text(TARGET_TEMPLATE, encoding="utf-8")
    return root, src, dst


def run_main(root, *extra):
    """调用 main()，返回 (退出码, 控制台输出)"""
    argv = ["--root", str(root)] + list(extra)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = C.main(argv)
    return code, buffer.getvalue()


def read_parts(path):
    """读切片文件，返回 (meta: dict, meta_keys: list, rows: list[list])"""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    meta, keys, rows = {}, [], []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            body = stripped[1:].strip()
            key, value = body.split(":", 1)
            key = key.strip()
            meta[key] = C.unquote(value.strip())
            keys.append(key)
            continue
        rows.append(stripped.split("|"))
    return meta, keys, rows


def slice_files(dst, code=CODE):
    """目标目录下的切片文件（按文件名升序）"""
    return sorted(dst.glob("%s_Min_*.mvsv" % code))


def test_convert_and_slice(tmp):
    root, src, dst = build_tree(tmp)
    code, out = run_main(root, "--slice-rows", "2", "--no-delete-source")
    check("转换退出码 0", code == 0, out[-500:])

    files = slice_files(dst)
    check("生成切片文件（5+3 行 / 每片 2 行 = 5 个）", len(files) == 5,
          [p.name for p in files])
    check("切片文件名符合 <代码>_Min_<yyyyMMdd>_<HHmmss>.mvsv",
          all(SLICE_PATTERN.match(p.name) for p in files),
          [p.name for p in files])
    check("切片文件名时间基准取来源采集时刻（Latest 用 FetchTime）",
          [p.name for p in files[:3]] == ["000001_Min_20261021_143145.mvsv",
                                          "000001_Min_20261021_143146.mvsv",
                                          "000001_Min_20261021_143147.mvsv"],
          [p.name for p in files])
    check("切片文件名时间基准取来源采集时刻（待聚合文件用文件名标签）",
          [p.name for p in files[3:]] == ["000001_Min_20261021_155645.mvsv",
                                          "000001_Min_20261021_155646.mvsv"],
          [p.name for p in files])
    check("源文件一个都没删除",
          (src / "Latest.mvsv").is_file()
          and (src / ("%s_Min_20261021_155645.mvsv" % CODE)).is_file())

    for path in files:
        meta, keys, rows = read_parts(path)
        check("切片 %s 行数不超过 2 且 Count 一致" % path.name,
              len(rows) <= 2 and int(meta["Count"]) == len(rows),
              (len(rows), meta.get("Count")))
        check("切片 %s 每行都是 12 列" % path.name,
              all(len(row) == 12 for row in rows),
              {len(row) for row in rows})
        check("切片 %s 头部键顺序与 META_ORDER 一致" % path.name,
              tuple(keys) == C.META_ORDER, keys)

    meta, _keys, _rows = read_parts(files[0])
    check("头部沿用模板的字段名/类型/市场信息",
          meta["FieldName"].startswith("Timestamp|Date|Time|Open|High|Low|Close")
          and meta["FieldType"].startswith("Int|Int|Int|Decimal")
          and meta["DataProvider"] == "FTMM"
          and meta["TypeKLine"] == "Min"
          and meta["Region"] == "CN" and meta["Market"] == "SZ"
          and meta["TimeZone"] == "Asia/Shanghai"
          and meta["Currency"] == "CNY" and meta["Name"] == "平安银行",
          meta)
    check("头部按目标证券改写代码相关键",
          meta["标题"] == "000001 分钟级行情数据"
          and meta["Symbol"] == "SZ.000001"
          and meta["SecuCode"] == CODE and meta["USC"] == CODE, meta)
    check("头部不写易变键（FetchTime/备注/采集时间）",
          not any(key in meta for key in C.VOLATILE_META_KEYS), list(meta))

    # 11 列来源：Open,Close,Low,High → Open,High,Low,Close
    first_rows = [_rows[0] for _m, _k, _rows in
                  (read_parts(p) for p in files) if _rows]
    row = read_parts(files[0])[2][0]
    check("11 列来源列序转换正确（整行）",
          row == ["1788139800", "20260831", "093000", "16.72", "", "",
                  "16.66", "73300", "1221178", "-0.06", "-0.36", ""], row)
    rows_all = [r for p in files for r in read_parts(p)[2]]
    check("Open/High/Low/Close 四列各就各位",
          rows_all[3][3:7] == ["16.76", "16.85", "16.70", "16.80"], rows_all[3])
    check("前收价回填为上一行收盘价（首行空）",
          rows_all[0][11] == "" and rows_all[1][11] == "16.66"
          and rows_all[3][11] == "16.76", [r[11] for r in rows_all[:5]])

    # 6 列来源：Date/Time 由 ts 推导（UTC+8），Open/High/Low 为空
    six = rows_all[5:]
    check("6 列来源的日期时间由 ts 推导（UTC+8）",
          [r[1] for r in six] == ["20261015", "20261015", "20261015"]
          and [r[2] for r in six] == ["093000", "093100", "093200"],
          [(r[1], r[2]) for r in six])
    check("6 列来源映射 close/volume/turnover/cp/cr 正确",
          six[0][3:11] == ["", "", "", "17.30", "64900", "1122770",
                           "-0.33", "-1.87"], six[0])
    check("6 列来源首行前收价为空", six[0][11] == "", six[0])

    # 兼容性：Task11 的解析/合并逻辑能直接消费
    meta_agg, rows_agg = AGG.parseMvsv(str(files[0]))
    merged, bad = AGG.mergeRows([], [rows_agg])
    check("Task11 解析无坏行且能按 ts 合并", bad == 0 and len(merged) == len(rows_agg),
          bad)
    rendered = AGG.renderMvsv(meta_agg, merged)
    check("Task11 渲染后仍为 12 列且 Count 正确",
          int(meta_agg.get("Count", -1)) == len(merged)
          and all(len(r) == 12 for r in merged), len(merged))

    # 幂等：重复运行不新增文件
    code2, out2 = run_main(root, "--slice-rows", "2", "--no-delete-source")
    check("重复运行退出码 0", code2 == 0, out2[-300:])
    check("重复运行不新增切片（内容相同即跳过）",
          len(slice_files(dst)) == len(files)
          and "已存在跳过 5" in out2, out2[-300:])


def test_last_close_switch(tmp):
    root, _src, dst = build_tree(tmp)
    code, out = run_main(root, "--slice-rows", "10", "--no-fill-last-close",
                         "--no-delete-source")
    check("关闭前收价回填后整列为空",
          code == 0 and all(r[11] == "" for p in slice_files(dst)
                            for r in read_parts(p)[2]),
          out[-300:])


def test_template_missing(tmp):
    root, _src, dst = build_tree(tmp, with_template=False)
    # 新链路根目录存在（真实仓库就是这样），但该证券目录里没有模板文件
    (root / C.TARGET_SUBDIR).mkdir(parents=True, exist_ok=True)
    check("该证券的目标目录不存在（模板缺失）", not dst.exists())
    code, out = run_main(root)
    check("模板缺失时退出码 1 且指出模板问题",
          code == 1 and "缺少文件头模板" in out, (code, out[-300:]))
    check("模板缺失时不创建任何输出", not dst.exists())


def test_bad_rows(tmp):
    bad = "1799999999|20261020|093000|1|2\n"          # 列数不符
    root, _src, dst = build_tree(tmp, bad_line=bad)
    code, out = run_main(root, "--slice-rows", "100", "--no-delete-source")
    check("坏行不影响其它行：退出码 0", code == 0, out[-300:])
    check("坏行被跳过并计数", "坏行 1" in out, out[-400:])
    rows = [r for p in slice_files(dst) for r in read_parts(p)[2]]
    check("好行全部写出（5+3 行）", len(rows) == 8, len(rows))


def test_codes_and_limit(tmp):
    root, src, dst = build_tree(tmp)
    other = src.parent / "600570"
    other.mkdir(parents=True, exist_ok=True)
    (other / "Latest.mvsv").write_text(SOURCE_11.replace("000001", "600570"),
                                       encoding="utf-8")
    (root / C.TARGET_SUBDIR / "600570").mkdir(parents=True, exist_ok=True)
    shutil.copy(str(dst / "Latest.mvsv"),
                str(root / C.TARGET_SUBDIR / "600570" / "Latest.mvsv"))

    code, out = run_main(root, "--codes", "600570", "--slice-rows", "100",
                         "--no-delete-source")
    check("--codes 只处理指定证券",
          code == 0
          and len(slice_files(root / C.TARGET_SUBDIR / "600570", "600570")) == 1
          and slice_files(dst) == [], out[-300:])

    code, out = run_main(root, "--limit", "1", "--no-delete-source")
    check("--limit 只处理前 N 个证券",
          code == 0 and len(slice_files(dst)) == 2
          and len(slice_files(root / C.TARGET_SUBDIR / "600570", "600570")) == 1,
          (code, out[-300:]))


def test_delete_source(tmp):
    """删除开关默认开启：转换成功后删除源文件（含保留 Latest、不完整不删、可关闭）"""
    # 1) 默认即删除（不传任何开关）
    root = Path(tmp) / "case1"
    root, src, dst = build_tree(root)
    code, out = run_main(root, "--slice-rows", "5")
    check("默认开启删除：退出码 0、日志标注默认",
          code == 0 and "删除源文件 = 是（默认）" in out, out[-400:])
    check("默认开启删除：源文件全部删除", list(src.glob("*.mvsv")) == [],
          [p.name for p in src.glob("*.mvsv")])
    check("删除前切片已全部写入", len(slice_files(dst)) == 2,
          [p.name for p in slice_files(dst)])
    check("日志含删除标记与删除计数",
          "🗑️ 删除源文件" in out and "删除源文件 2 个" in out, out[-400:])
    check("Latest.mvsv 也在删除范围内，且汇总区分两类",
          "（Latest.mvsv）" in out
          and "Latest.mvsv 1 个 / 增量文件 1 个" in out, out[-400:])
    check("删除日志打印完整路径", str(src) in out, out[-400:])

    # 删除后重跑：无源文件时安全退出 0
    code, out = run_main(root)
    check("删除后重跑安全退出 0",
          code == 0 and "没有 .mvsv 文件" in out, (code, out[-200:]))

    # 2) --keep-latest：只删增量文件
    root2 = Path(tmp) / "case2"
    root2, src2, dst2 = build_tree(root2)
    code, out = run_main(root2, "--slice-rows", "5", "--keep-latest")
    check("--keep-latest 只删增量文件，Latest 保留",
          code == 0 and [p.name for p in src2.glob("*.mvsv")] == ["Latest.mvsv"],
          [p.name for p in src2.glob("*.mvsv")])

    # 3) 有坏行的源文件保留（数据不完整），其余删除；退出码 1
    root3 = Path(tmp) / "case3"
    root3, src3, _dst3 = build_tree(
        root3, bad_line="1799999999|20261020|093000|1|2\n")
    code, out = run_main(root3, "--slice-rows", "5")
    check("有坏行的源文件不被删除，其余删除",
          code == 1
          and [p.name for p in src3.glob("*.mvsv")]
          == ["%s_Min_20261021_155645.mvsv" % CODE],
          (code, [p.name for p in src3.glob("*.mvsv")]))
    check("有保留文件时退出码 1 且列出清单",
          "保留的源文件清单" in out and "坏行" in out, out[-500:])

    # 4) 环境变量可显式开启 / 关闭
    root5 = Path(tmp) / "case5"
    root5, src5, _dst5 = build_tree(root5)
    os.environ["CONVERT_DELETE_SOURCE"] = "false"
    try:
        code, out = run_main(root5, "--slice-rows", "5")
    finally:
        os.environ.pop("CONVERT_DELETE_SOURCE", None)
    check("CONVERT_DELETE_SOURCE=false 关闭删除",
          code == 0 and len(list(src5.glob("*.mvsv"))) == 2
          and "已关闭删除开关" in out, out[-400:])

    root6 = Path(tmp) / "case6"
    root6, src6, _dst6 = build_tree(root6)
    os.environ["CONVERT_DELETE_SOURCE"] = "true"
    try:
        code, out = run_main(root6, "--slice-rows", "5")
    finally:
        os.environ.pop("CONVERT_DELETE_SOURCE", None)
    check("CONVERT_DELETE_SOURCE=true 关闭命令可被显式开启覆盖",
          code == 0 and list(src6.glob("*.mvsv")) == [], out[-400:])

    # 6b) Latest.mvsv 只有头、无数据行时也要删（空文件无数据可丢）
    root8 = Path(tmp) / "case8"
    empty_latest = SOURCE_11.split("1788139800", 1)[0]
    root8, src8, _dst8 = build_tree(root8, source_11=empty_latest)
    code, out = run_main(root8, "--slice-rows", "5")
    check("无数据行的 Latest.mvsv 也会被删除",
          code == 0 and list(src8.glob("*.mvsv")) == [],
          (code, [p.name for p in src8.glob("*.mvsv")]))
    check("空文件删除日志带 Latest 标注",
          "没有可转换的数据行" in out and "（Latest.mvsv）" in out, out[-400:])

    # 7) --no-delete-source 优先于环境变量
    root7 = Path(tmp) / "case7"
    root7, src7, _dst7 = build_tree(root7)
    os.environ["CONVERT_DELETE_SOURCE"] = "true"
    try:
        code, out = run_main(root7, "--slice-rows", "5", "--no-delete-source")
    finally:
        os.environ.pop("CONVERT_DELETE_SOURCE", None)
    check("--no-delete-source 优先于环境变量",
          code == 0 and len(list(src7.glob("*.mvsv"))) == 2, out[-400:])


def main():
    C.log = print
    cases = (
        ("基础转换/切片/幂等/兼容", test_convert_and_slice),
        ("前收价回填开关", test_last_close_switch),
        ("模板缺失", test_template_missing),
        ("坏行容错", test_bad_rows),
        ("--codes / --limit", test_codes_and_limit),
        ("删除源文件开关", test_delete_source),
    )
    for name, func in cases:
        tmp = tempfile.mkdtemp(prefix="task18_")
        print("\n==== %s ====" % name)
        try:
            func(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print("\n%s" % ("全部通过" if not FAILS else "失败 %d 项：%s" % (len(FAILS), FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
