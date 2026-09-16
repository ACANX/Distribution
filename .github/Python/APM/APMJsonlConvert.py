#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
APM JSON -> JSONL 归档转换程序
================================================================================

作用
----
把 APM 分支上的存量 JSON 数组文件(网页埋点数据, 每个文件是事件对象数组)
转换为 JSONL 文件(数组元素一行一个), 按 **事件记录自身的 ts 字段(毫秒时间戳)
换算成北京时间日历日** 分组落盘:

    源(两处, 均递归扫描):
        Data/Meta/WebMMCP/APM/{type}/**/*.json        采集批次文件
        Archive/Meta/WebMMCP/APM/{type}/**/*.json     merge 脚本的合并输出
    目标:
        Archive/Meta/WebMMCP/APM/{type}/APM_{type}_{Code}_DAY_ACANX_{yyyyMMdd}.jsonl

    其中 type = 源文件在 APM/ 下的父级目录名(当前为 API / Page),
    Code  = "MetaCms" + type(即 MetaCmsAPI / MetaCmsPage)。

    例: Archive/Meta/WebMMCP/APM/API/APM_API_MetaCmsAPI_DAY_ACANX_20260420.jsonl

为什么按记录 ts 而不按源文件名日期分组
--------------------------------
    源文件名(yyyyMMddHHmmss)是采集时刻, 但经核对存量数据, 有 27 个批次的
    文件名是 UTC 时刻命名的(文件名日期比记录 ts 的北京日期早一天), 且还存在
    另一种带下划线的命名格式 —— 文件名日期不可靠。记录内的 ts(13 位毫秒
    时间戳)是唯一可靠的时间源, 因此按 ts 换算北京时间日历日分组, 保证
    JSONL 文件名中的 yyyyMMdd 与文件内容严格一致。

    无 ts 或 ts 非法的记录跳过并告警, 不中断整体流程; 单个源文件 JSON 语法
    错误也跳过, 留待人工修复。

幂等与增量合并
--------------
    目标 JSONL 已存在时: 已有行原样保留, 新记录序列化后与已有行做整行比对,
    重复记录(与已有行完全相同)不重复写入, 只追加新行。因此本脚本可以放心
    定期重跑 —— 不产生重复行, 源数据不变时输出也不变。

    行格式: json.dumps(rec, ensure_ascii=False, separators=(",", ":")),
    紧凑格式, 保留中文; 字段顺序与源 JSON 保持一致。

设计为只读源文件: 不删除、不改写任何源 JSON(采集批次与 merge 输出都保留),
转换产物只增不改, 由 git 提交到 apm 分支。

配置来源(优先级从高到低)
------------------------
    1. 命令行参数(--type / --date / --data-dir / --archive-dir 等)
    2. 环境变量 APM_JSONL_DATA_DIR / APM_JSONL_ARCHIVE_DIR
    3. 内置默认值: Data/Meta/WebMMCP/APM 与 Archive/Meta/WebMMCP/APM

用法
----
    python3 .github/Python/APM/APMJsonlConvert.py                # 常规转换
    python3 .github/Python/APM/APMJsonlConvert.py --dry-run      # 只统计不写
    python3 .github/Python/APM/APMJsonlConvert.py --type API     # 只转换 API
    python3 .github/Python/APM/APMJsonlConvert.py --date 20260505 # 只转该日
    python3 .github/Python/APM/APMJsonlConvert.py --log          # 逐文件详情

依赖: 仅 Python 3 标准库。
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── 时区: 记录 ts 统一换算为北京时间日历日 ─────────────────────────────────────
BJT = timezone(timedelta(hours=8))

# ── 路径(相对仓库根, 可被环境变量覆盖) ─────────────────────────────────────────
DEFAULT_DATA_DIR = "Data/Meta/WebMMCP/APM"      # 采集批次
DEFAULT_ARCHIVE_DIR = "Archive/Meta/WebMMCP/APM"  # 转换产物落点(与 merge 输出同根)

# Code 命名规则: Code = MetaCms{type} -> MetaCmsAPI / MetaCmsPage
CODE_PREFIX = "MetaCms"

# 目标文件名模板
NAME_TMPL = "APM_{type}_{code}_DAY_ACANX_{date}.jsonl"

OUT_ENCODING = "utf-8"

# ts 字段容错阈值: 小于该值视为秒, 否则视为毫秒(当前存量全部为 13 位毫秒)
_MS_EPOCH = 10 ** 12


def repo_root() -> Path:
    """仓库根目录。本文件位于 <根>/.github/Python/APM/, 上溯三级即根。"""
    return Path(__file__).resolve().parents[3]


def line_of(rec) -> str:
    """记录 -> JSONL 行文本(紧凑, 保留中文, 字段顺序与源一致)。"""
    return json.dumps(rec, ensure_ascii=False, separators=(",", ":"))


def ts_to_date(ts) -> str:
    """记录 ts -> 北京时间日历日(yyyyMMdd); 非法返回 None。"""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    if ts < _MS_EPOCH:          # 秒 -> 毫秒
        ts *= 1000
    try:
        return datetime.fromtimestamp(ts / 1000, BJT).strftime("%Y%m%d")
    except (OverflowError, OSError, ValueError):
        return None


def collect_tasks(data_root: Path, archive_root: Path, only_types, only_date, log):
    """扫描两处源根目录, 返回 {(type, date): [记录, ...]}(保持源文件与记录顺序)。

    type = 源文件位于源根目录下的一级子目录名; 一级子目录下的 json 文件
    (含更深层的周子目录)全部纳入。only_types / only_date 为过滤条件(None=不限)。
    """
    grouped = defaultdict(list)
    for src_root in (data_root, archive_root):
        if not src_root.is_dir():
            log("源目录不存在, 跳过: %s" % src_root)
            continue
        for type_dir in sorted(p for p in src_root.iterdir() if p.is_dir()):
            if only_types and type_dir.name not in only_types:
                continue
            files = sorted(type_dir.rglob("*.json"))
            for f in files:
                try:
                    with open(f, "r", encoding=OUT_ENCODING) as fh:
                        data = json.load(fh)
                except (OSError, ValueError) as exc:
                    log("[WARN] JSON 解析失败, 跳过 %s: %s" % (f, exc))
                    continue
                if not isinstance(data, list):
                    data = [data]           # 兼容意外写入的单对象
                for rec in data:
                    if not isinstance(rec, dict):
                        log("[WARN] 非对象记录, 跳过: %s" % f)
                        continue
                    date = ts_to_date(rec.get("ts"))
                    if date is None:
                        log("[WARN] 记录 ts 缺失或非法, 跳过: %s" % f)
                        continue
                    if only_date and date != only_date:
                        continue
                    grouped[(type_dir.name, date)].append(rec)
    return grouped


def merge_into_jsonl(target: Path, recs):
    """把记录合并进目标 JSONL: 已有行原样保留, 整行相同的重复记录跳过。

    返回 (总行数, 新增行数)。已有行集合读入时不去除末尾空行, 与追加格式一致。
    """
    existing = []
    if target.exists():
        with open(target, "r", encoding=OUT_ENCODING) as fh:
            existing = [ln for ln in (l.rstrip("\n") for l in fh) if ln]
    seen = set(existing)
    new_lines = [line_of(r) for r in recs if line_of(r) not in seen]
    if new_lines:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding=OUT_ENCODING, newline="\n") as fh:
            for ln in new_lines:
                fh.write(ln + "\n")
    return len(existing) + len(new_lines), len(new_lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="APM 埋点 JSON 数组 -> 按日 JSONL 归档转换(仅标准库)")
    parser.add_argument("--type", dest="types", action="append", default=None,
                        help="只处理指定的一级子目录名(可重复), 如 API; 默认全部")
    parser.add_argument("--date", default=None, metavar="YYYYMMDD",
                        help="只处理记录时间(北京时间)为该日的记录; 缺省不限")
    parser.add_argument("--data-dir", default=None,
                        help="采集批次根目录(默认 Data/Meta/WebMMCP/APM)")
    parser.add_argument("--archive-dir", default=None,
                        help="转换产物根目录(默认 Archive/Meta/WebMMCP/APM)")
    parser.add_argument("--dry-run", action="store_true",
                        help="只统计将要写入的行数, 不写任何文件")
    parser.add_argument("--log", action="store_true",
                        help="输出每个源文件的处理详情")
    args = parser.parse_args()

    verbose = args.log

    def log(msg):
        if verbose:
            print(msg, flush=True)

    root = repo_root()
    data_root = Path(args.data_dir or os.environ.get(
        "APM_JSONL_DATA_DIR", DEFAULT_DATA_DIR))
    if not data_root.is_absolute():
        data_root = root / data_root
    archive_root = Path(args.archive_dir or os.environ.get(
        "APM_JSONL_ARCHIVE_DIR", DEFAULT_ARCHIVE_DIR))
    if not archive_root.is_absolute():
        archive_root = root / archive_root

    grouped = collect_tasks(data_root, archive_root,
                            args.types, args.date, log)

    types = sorted({t for (t, _d) in grouped})
    print("=" * 72)
    print("APM JSON -> JSONL 转换  %s" % ("[DRY-RUN]" if args.dry_run else ""))
    print("仓库根   : %s" % root)
    print("源目录   : %s" % data_root)
    print("         : %s" % archive_root)
    print("命名规则 : %s" % NAME_TMPL.format(type="<type>", code=CODE_PREFIX + "<type>", date="<yyyyMMdd>"))
    print("命中(type, 日期)组: %d  涉及 type: %s" % (len(grouped), ", ".join(types) or "(无)"))
    print("=" * 72)

    if not grouped:
        print("没有可转换的记录, 无事可做")
        return

    total_recs = total_new = 0
    for (type_name, date) in sorted(grouped):
        recs = grouped[(type_name, date)]
        target = archive_root / type_name / NAME_TMPL.format(
            type=type_name, code=CODE_PREFIX + type_name, date=date)
        if args.dry_run:
            print("  [DRY-RUN] %s <- %d 条记录" % (target.relative_to(root).as_posix(), len(recs)))
            total_recs += len(recs)
            continue
        lines, new = merge_into_jsonl(target, recs)
        total_recs += len(recs)
        total_new += new
        rel = target.relative_to(root).as_posix()
        if new:
            print("  %s: 新增 %d 行(现共 %d 行)" % (rel, new, lines))
        else:
            print("  %s: 无新增(已有 %d 行, 全部重复)" % (rel, lines))

    print("\n======== 转换结束 %s ========" % ("(dry-run)" if args.dry_run else ""))
    print("扫描记录: %d 条" % total_recs)
    if not args.dry_run:
        print("新增行  : %d 行" % total_new)
    print("全部完成")


if __name__ == "__main__":
    main()
