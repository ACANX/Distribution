#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Task07 月归档转存 —— 结构与月份过滤自测
================================================================================

纯离线自测, 不触网、不改仓库、不读环境令牌。覆盖三块:

1. parse_month_file: 月归档路径的结构/命名校验(必须拒掉日归档、FT 命名、
   目录与文件名 Code 不一致、文件名尾段非 6 位等)。
2. collect_files 的月份过滤: 默认排除当月, --include-current 放开, 显式 --month
   绕过当月保护。
3. delete_source_file 的删除语义: 转存端已有同内容时仍要删除源端; 删除请求的
   owner/repo 绝不能是转存端(TARGET_OWNER/TARGET_REPO)。

对着 Task05 的自测(archiveTransferDeleteSourceSelfTest.py)刻意保持同一套写法,
便于两处对照维护。

用法: python3 .github/Python/tests/archiveMonthlyTransferSelfTest.py
退出码: 0 = 全部通过; 1 = 有失败用例
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

# 本文件位于 <根>/.github/Python/tests/, 其本身也在 .github/Python 之下,
# 故仓库根 = parents[3]
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, os.path.join(ROOT, ".github", "Python", "Quote"))

_spec = importlib.util.spec_from_file_location(
    "Task07MonthlyMvsvTransfer",
    os.path.join(ROOT, ".github", "Python", "Quote", "Task07MonthlyMvsvTransfer.py"))
T7 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T7)

PASSED = 0
FAILED = []


def check(name, cond, detail=""):
    """断言并记录; 失败不中断, 便于一次跑出全部问题"""
    global PASSED
    if cond:
        PASSED += 1
        print("  PASS  %s" % name)
    else:
        FAILED.append(name)
        print("  FAIL  %s  %s" % (name, detail))


def test_parse_month_file():
    print("\n[1] parse_month_file 结构与命名校验")
    good = [
        ("Archive/Finv/SecuQuote/2026/000001/000001_Min_202606.mvsv", ("202606", "000001")),
        ("Archive/Finv/SecuQuote/2026/XAUUSD/XAUUSD_Min_202608.mvsv", ("202608", "XAUUSD")),
        ("Archive/Finv/SecuQuote/2026/518880/518880_Min_202608.mvsv", ("202608", "518880")),
    ]
    for path, expect in good:
        check("接受 %s" % path, T7.parse_month_file(path) == expect,
              "得到 %s, 期望 %s" % (T7.parse_month_file(path), expect))

    bad = [
        # 日归档(在 Day/ 下, 且文件名尾段是 8 位日期) —— 必须被拒
        "Archive/Finv/SecuQuote/Day/000001/000001_Min_20260804.mvsv",
        # 目录 Code 与文件名 Code 不一致
        "Archive/Finv/SecuQuote/2026/000001/000510_Min_202606.mvsv",
        # 月份只有 5 位
        "Archive/Finv/SecuQuote/2026/000001/000001_Min_20260.mvsv",
        # 扩展名不对
        "Archive/Finv/SecuQuote/2026/000001/000001_Min_202606.jsonl",
        # 非 SecuQuote 目录
        "Archive/Finv/SecuQuoteExecLog/LOG_x.jsonl",
        # 已转存端的 FT 命名(Data/ 下) —— 不该被当成源文件
        "Data/Finv/SecuQuote/FT/Day/CN_SH/000001/CN_SH_000001_Min_FT_20260609.mvsv",
        # 深度不足
        "Archive/Finv/SecuQuote/000001_Min_202606.mvsv",
    ]
    for path in bad:
        check("拒绝 %s" % path, T7.parse_month_file(path) == (None, None),
              "得到 %s" % (T7.parse_month_file(path),))


def test_collect_files_month_filter():
    print("\n[2] collect_files 月份过滤")
    cur = T7.current_month_bjt()
    cur_num = int(cur)
    prev = "%04d%02d" % ((cur_num - 1) // 100, (cur_num - 1) % 100 or 12)
    if prev[4:] == "00":
        prev = "%04d12" % (int(prev[:4]) - 1)
    # 用一个能覆盖"往月/当月"的最小构造: 直接算上一个月
    y, m = int(cur[:4]), int(cur[4:])
    pm = m - 1 or 12
    py = y if m > 1 else y - 1
    prev = "%04d%02d" % (py, pm)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for month, code in ((prev, "000001"), (cur, "000001"), (cur, "000510")):
            d = root / "2026" / code
            d.mkdir(parents=True, exist_ok=True)
            (d / ("%s_Min_%s.mvsv" % (code, month))).write_text("x", encoding="utf-8")
        # 夹一个日归档, 确保被排除
        dd = root / "Day" / "000001"
        dd.mkdir(parents=True, exist_ok=True)
        (dd / "000001_Min_20260804.mvsv").write_text("x", encoding="utf-8")

        default = T7.collect_files(root)
        months_default = sorted({t[2] for t in default})
        check("默认只收往月(当月 %s 被排除)" % cur,
              months_default == [prev],
              "收到月份 %s, 期望 [%s]" % (months_default, prev))
        check("默认不收入日归档 Day/", len(default) == 1,
              "收到 %d 个" % len(default))

        incl = T7.collect_files(root, include_current=True)
        check("--include-current 把当月纳入",
              sorted({t[2] for t in incl}) == sorted({prev, cur}) and len(incl) == 3,
              "收到 %d 个, 月份 %s" % (len(incl), sorted({t[2] for t in incl})))

        only_cur = T7.collect_files(root, only_month=cur)
        check("显式 --month 指定当月时放行",
              len(only_cur) == 2 and {t[2] for t in only_cur} == {cur},
              "收到 %d 个" % len(only_cur))

        none = T7.collect_files(root, only_month="209912")
        check("--month 无匹配时返回空", none == [], "收到 %d 个" % len(none))


def test_delete_target_identity():
    print("\n[3] delete_source_file 删除目标与语义")
    calls = []

    def fake_get_file_sha(api_base, owner, repo, path_key, branch, token, timeout):
        calls.append(("GET", owner, repo, path_key, branch))
        return "deadbeef"

    def fake_request(method, url, headers, body, timeout):
        calls.append((method, url))
        return 200, "{}", None

    def fake_parse_json(text):
        return {}

    orig = (T7._get_file_sha, T7._request, T7._parse_json)
    T7._get_file_sha = fake_get_file_sha
    T7._request = fake_request
    T7._parse_json = fake_parse_json
    try:
        res = T7.delete_source_file(
            "Archive/Finv/SecuQuote/2026/000001/000001_Min_202606.mvsv",
            "token", "quote", "ACANX", "Distribution")
        check("删除成功返回 success=True", res["success"] is True, repr(res))
        check("删除成功不算 already_gone", res["already_gone"] is False, repr(res))

        # 关键: 删除请求必须打到源端(ACANX/Distribution), 绝不能是转存端
        deletes = [c for c in calls if c[0] == "DELETE"]
        check("确实发出了 DELETE", len(deletes) == 1, "调用记录 %s" % (calls,))
        url = deletes[0][1] if deletes else ""
        check("DELETE 指向源端 ACANX/Distribution",
              "/ACANX/Distribution/" in url, url)
        check("DELETE 绝不指向转存端 %s/%s" % (T7.TARGET_OWNER, T7.TARGET_REPO),
              ("/%s/%s/" % (T7.TARGET_OWNER, T7.TARGET_REPO)) not in url, url)

        gets = [c for c in calls if c[0] == "GET"]
        check("查 sha 也走源端", gets and gets[0][1] == "ACANX" and gets[0][2] == "Distribution",
              repr(gets))
    finally:
        T7._get_file_sha, T7._request, T7._parse_json = orig


def test_delete_already_gone():
    print("\n[4] 源端已无该文件时视为已删除(幂等)")
    def fake_get_file_sha(*a, **k):
        return None

    orig = T7._get_file_sha
    T7._get_file_sha = fake_get_file_sha
    try:
        res = T7.delete_source_file(
            "Archive/Finv/SecuQuote/2026/000001/000001_Min_202606.mvsv",
            "token", "quote", "ACANX", "Distribution")
        check("success=True", res["success"] is True, repr(res))
        check("already_gone=True", res["already_gone"] is True, repr(res))
    finally:
        T7._get_file_sha = orig


def main():
    print("=" * 72)
    print("Task07 月归档转存自测")
    print("=" * 72)
    test_parse_month_file()
    test_collect_files_month_filter()
    test_delete_target_identity()
    test_delete_already_gone()

    print("\n" + "=" * 72)
    print("通过 %d 项, 失败 %d 项" % (PASSED, len(FAILED)))
    if FAILED:
        for name in FAILED:
            print("  - %s" % name)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
