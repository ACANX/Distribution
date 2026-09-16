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

源文件清理(归档后删除采集批次)
------------------------------
    Data 侧(Data/Meta/WebMMCP/APM)的采集批次文件在**其全部记录都已收录进目标
    JSONL 之后**由本脚本直接删除(本地 unlink, 随工作流的 git add -A 一并提交);
    Archive 侧的 merge 历史输出不在删除范围。判定与前置工序
    APMJsonlConvert.DailyJsonl.py 同口径(逐记录核对, 从严保护):
        - 文件里每条可归档记录 (日期, 行文本) 都必须在对应的
          Archive/.../{type}/APM_{type}_{Code}_DAY_ACANX_{yyyyMMdd}.jsonl 中命中;
          目标文件不存在或行未命中 → 整文件保留;
        - 含无法归档记录(JSON 解析失败 / 非对象 / ts 缺失或非法)的文件永不删除,
          删除会永久丢失未归档数据;
        - 空数组文件(无记录)按可删处理(无数据可丢); .gitkeep 不在 *.json 扫描范围。
    --keep-source 可整体关闭删除; --dry-run 只报告不删除。删除失败的源文件按
    [ERROR] 报告并以退出码 1 结束(记录已在 JSONL, 文件留待下次运行重删)。

    源 JSON 一经删除不可恢复, 故判定坚持"全量收录确认"而非"部分命中",
    宁可保留待下次收敛。

转换产物只增不改(目标 JSONL 的已有行原样保留), 由 git 提交到 apm 分支。

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
    python3 .github/Python/APM/APMJsonlConvert.py --keep-source  # 保留源文件
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
    """扫描两处源根目录, 返回 (grouped, tracked)。

    type = 源文件位于源根目录下的一级子目录名; 一级子目录下的 json 文件
    (含更深层的周子目录)全部纳入。only_types / only_date 为过滤条件(None=不限)。

    :return: (grouped, tracked)
        grouped: {(type, date): [记录, ...]}(保持源文件与记录顺序);
        tracked: {文件路径: {"type": str, "records": [(date, 行文本), ...],
                             "undecodable": bool}}
                 —— 仅 Data 侧(data_root 下)文件, 供"收录确认后删除源文件"判定;
                 undecodable=True 表示文件里有无法归档的记录(解析失败/非对象/
                 ts 缺失或非法), 这类文件绝不删除(删除会永久丢失未归档数据)。
    """
    grouped = defaultdict(list)
    tracked = {}
    for src_root in (data_root, archive_root):
        if not src_root.is_dir():
            log("源目录不存在, 跳过: %s" % src_root)
            continue
        is_data_side = (src_root == data_root)   # 仅 Data 侧参与删除判定
        for type_dir in sorted(p for p in src_root.iterdir() if p.is_dir()):
            if only_types and type_dir.name not in only_types:
                continue
            files = sorted(type_dir.rglob("*.json"))
            for f in files:
                info = {"type": type_dir.name, "records": [], "undecodable": False}
                try:
                    with open(f, "r", encoding=OUT_ENCODING) as fh:
                        data = json.load(fh)
                except (OSError, ValueError) as exc:
                    log("[WARN] JSON 解析失败, 跳过 %s: %s" % (f, exc))
                    info["undecodable"] = True
                    if is_data_side:
                        tracked[f] = info
                    continue
                if not isinstance(data, list):
                    data = [data]           # 兼容意外写入的单对象
                for rec in data:
                    if not isinstance(rec, dict):
                        log("[WARN] 非对象记录, 跳过: %s" % f)
                        info["undecodable"] = True
                        continue
                    date = ts_to_date(rec.get("ts"))
                    if date is None:
                        log("[WARN] 记录 ts 缺失或非法, 跳过: %s" % f)
                        info["undecodable"] = True
                        continue
                    info["records"].append((date, line_of(rec)))
                    if only_date and date != only_date:
                        continue
                    grouped[(type_dir.name, date)].append(rec)
                if is_data_side:
                    tracked[f] = info
    return grouped, tracked


def target_lines_reader(archive_root: Path, cache):
    """返回 target_lines(type, date) 查询函数: 目标 JSONL 当前全部行集合(带缓存)。

    首次访问某 (type, date) 时从磁盘读取(文件不存在 → 空集); 之后由调用方在
    转换循环里把本次新增行 update 进缓存, 与文件实际内容保持一致 —— 干跑时
    则为"模拟写入后的行集", 让 --dry-run 也能报告"将删除"。
    """
    def target_lines(type_name, date):
        key = (type_name, date)
        if key not in cache:
            target = archive_root / type_name / NAME_TMPL.format(
                type=type_name, code=CODE_PREFIX + type_name, date=date)
            lines = set()
            if target.exists():
                with open(target, "r", encoding=OUT_ENCODING) as fh:
                    lines = {ln for ln in (l.rstrip("\n") for l in fh) if ln}
            cache[key] = lines
        return cache[key]
    return target_lines


def plan_deletions(tracked, target_lines):
    """判定哪些 Data 侧源文件的全部记录都已收录进目标 JSONL。

    从严保护(任一不满足即整文件保留):
        - 含无法归档记录(undecodable)的文件直接保留(删除会永久丢失未归档数据);
        - 文件里每条可归档记录 (date, 行文本) 都必须在对应 (类型, 日期) 的
          目标 JSONL 全量行集合中命中 —— 逐条核对, 跨日文件也覆盖全部记录。
    空数组文件(records 为空)按可删处理: 无任何数据可丢。

    :param tracked: collect_tasks 的文件级追踪表
    :param target_lines: callable(type, date) -> 目标 JSONL 当前全部行文本集合
    :return: (deletable 路径列表, protected [(路径, 保留原因), ...])
    """
    deletable, protected = [], []
    for f, info in sorted(tracked.items()):
        if info["undecodable"]:
            protected.append((f, "含无法归档的记录"))
            continue
        missing = None
        for (date, line) in info["records"]:
            if line not in target_lines(info["type"], date):
                missing = "记录日期 %s 的行未在目标 JSONL 中命中" % date
                break
        if missing is None:
            deletable.append(f)
        else:
            protected.append((f, missing))
    return deletable, protected


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
    parser.add_argument("--keep-source", action="store_true",
                        help="保留 Data 侧源文件, 不做归档后删除"
                             "(默认删除全部记录已收录进目标 JSONL 的源文件)")
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

    grouped, tracked = collect_tasks(data_root, archive_root,
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
    line_cache = {}                   # (type, date) -> 目标 JSONL 当前全部行集合
    target_lines = target_lines_reader(archive_root, line_cache)
    for (type_name, date) in sorted(grouped):
        recs = grouped[(type_name, date)]
        target = archive_root / type_name / NAME_TMPL.format(
            type=type_name, code=CODE_PREFIX + type_name, date=date)
        lines = target_lines(type_name, date)      # 先读现有(缓存/文件), 再写
        if args.dry_run:
            print("  [DRY-RUN] %s <- %d 条记录" % (target.relative_to(root).as_posix(), len(recs)))
            total_recs += len(recs)
            lines.update(line_of(r) for r in recs)   # 模拟写入后的行集
            continue
        lines_count, new = merge_into_jsonl(target, recs)
        total_recs += len(recs)
        total_new += new
        lines.update(line_of(r) for r in recs)       # 与实际落盘内容一致
        rel = target.relative_to(root).as_posix()
        if new:
            print("  %s: 新增 %d 行(现共 %d 行)" % (rel, new, lines_count))
        else:
            print("  %s: 无新增(已有 %d 行, 全部重复)" % (rel, lines_count))

    # —— 源文件清理(全部记录已收录进目标 JSONL 的 Data 侧采集批次) ——
    deletable, protected = plan_deletions(tracked, target_lines)
    deleted = del_failed = 0
    for f, why in protected:
        log("  保留源文件 %s: %s" % (f.relative_to(root).as_posix(), why))
    if not args.keep_source:
        for f in deletable:
            rel = f.relative_to(root).as_posix()
            if args.dry_run:
                log("  [DRY-RUN] 将删除源文件: %s" % rel)
                continue
            try:
                f.unlink()
            except OSError as exc:
                print("[ERROR] 删除源文件失败: %s -> %s" % (rel, exc))
                del_failed += 1
                continue
            print("  已删除源文件: %s" % rel)
            deleted += 1

    print("\n======== 转换结束 %s ========" % ("(dry-run)" if args.dry_run else ""))
    print("扫描记录: %d 条" % total_recs)
    if not args.dry_run:
        print("新增行  : %d 行" % total_new)
    if args.keep_source:
        print("源文件清理: 已用 --keep-source 关闭, 保留 Data 侧源文件 %d 个"
              % len(tracked))
    elif args.dry_run:
        print("源文件清理: 将删除 %d / 保留 %d / 共 %d 个 Data 侧源文件"
              "(dry-run 不实际删除)" % (len(deletable), len(protected), len(tracked)))
    else:
        print("源文件清理: 删除 %d / 失败 %d / 保留 %d / 共 %d 个 Data 侧源文件"
              % (deleted, del_failed, len(protected), len(tracked)))
    if del_failed:
        print("存在删除失败的源文件, 请查看上方 [ERROR] 行")
        sys.exit(1)
    print("全部完成")


if __name__ == "__main__":
    main()
