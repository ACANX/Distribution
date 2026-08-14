#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
merge_latest.py
===============
将 Data\\Finv\\GoldQuote 下各证券目录中的行情 txt 文件按证券(Symbol)合并去重，
写入各证券目录下的 Latest.txt。

文件名模式 : {Region}_{market}_{Symbol}_{Provider}_{Period}_{yyyyMMddHHmmss}.txt
             例: CN_CNOTC_ACG-ICBC_ICBC_Min_20260812234534.txt
分组规则   : 文件名第三个下划线之前的文件名前缀(即 {Region}_{market}_{Symbol})
             相同者归为一组; 由于每个证券的 Symbol 唯一, 一个证券对应一份 Latest.txt。
数据格式   : 每行 ts|dt|c 三个字段, 以 '|' 分隔。
去重规则   : 按行中第一个字段 ts 的值去重。
排序规则   : ts 从小到大升序排列。
输出位置   : 相对于仓库根目录的 Data\\Finv\\GoldQuote\\{Symbol}\\Latest.txt
增量合并   : 若某证券的 Latest.txt 已存在, 会先读入其已有数据再合并(即使源文件被删除,
             旧数据仍保留在 Latest.txt 中); 同一 ts 冲突时以当前数据源文件的内容为准。
源文件清理 : 合并结果成功写入 Latest.txt 之后, 删除本次合并的源 txt 文件(位于 {year} 等
             子目录下), 并逐条打印被删除的文件路径; 可用 --keep-sources 跳过删除。
"""

import argparse
import re
import sys
from pathlib import Path

# 匹配形如 ..._{Provider}_{Period}_{yyyyMMddHHmmss}.txt 的文件,
# group(1)=前缀 {Region}_{market}_{Symbol}(可能含下划线), group(2)=Provider,
# group(3)=Period, group(4)=时间戳
_PATTERN = re.compile(r"^(.+?)_([^_]+)_([^_]+)_(\d{14})\.txt$")


def default_data_dir() -> Path:
    """仓库根目录下的 Data\\Finv\\GoldQuote。脚本位于 Python/QuoteGold/ 下。"""
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "Data" / "Finv" / "GoldQuote"


def read_rows(path: Path):
    """读取 txt 文件, 返回 [(ts, 原始行), ...]。跳过空行及非 ts|dt|c 结构的行。"""
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) != 3:
                continue
            rows.append((parts[0], line))
    return rows


def collect_files(data_root: Path):
    """
    递归扫描数据目录下所有符合条件的 txt 文件。

    返回 {输出路径: {前缀分组: [源文件路径, ...]}, ...},
    输出路径 = data_root / {Symbol} / Latest.txt。
    """
    groups = {}
    for f in sorted(data_root.rglob("*.txt")):
        if f.name == "Latest.txt":
            continue
        m = _PATTERN.match(f.name)
        if not m:
            continue
        prefix = m.group(1)  # {Region}_{market}_{Symbol}
        symbol = prefix.split("_", 2)[2]  # 第三个字段即证券 Symbol
        out_path = data_root / symbol / "Latest.txt"
        groups.setdefault(out_path, {}).setdefault(prefix, []).append(f)
    return groups


def main() -> None:
    parser = argparse.ArgumentParser(
        description="合并 GoldQuote 行情 txt 文件并按 ts 去重, 生成各证券的 Latest.txt"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=default_data_dir(),
        help="数据根目录, 默认取仓库下 Data/Finv/GoldQuote",
    )
    parser.add_argument(
        "--symbol",
        help="只处理指定 Symbol 的证券(可选), 默认处理全部",
    )
    parser.add_argument(
        "--keep-sources",
        action="store_true",
        help="合并成功后保留源 txt 文件, 不删除(默认: 删除本次合并的源文件)",
    )
    args = parser.parse_args()

    data_root = args.data_dir.resolve()
    if not data_root.is_dir():
        print(f"[错误] 数据目录不存在: {data_root}", file=sys.stderr)
        sys.exit(1)

    groups = collect_files(data_root)
    if args.symbol:
        groups = {p: g for p, g in groups.items() if p.parent.name == args.symbol}

    if not groups:
        print(f"在 {data_root} 下未找到符合 {_PATTERN.pattern} 模式的行情 txt 文件。")
        return

    for out_path, prefix_map in sorted(groups.items()):
        symbol = out_path.parent.name
        sources = [f for files in prefix_map.values() for f in files]

        rows = {}
        # 1) 已有 Latest.txt 作为增量合并基础(先读入, 保证源文件删除后旧数据仍保留)
        if out_path.exists():
            for ts, line in read_rows(out_path):
                rows[ts] = line

        # 2) 依次读取数据源文件, 文件名排序保证确定性; 同一 ts 冲突时以源文件为准(覆盖旧值)
        src_count = 0
        for src in sorted(sources, key=lambda p: p.name):
            for ts, line in read_rows(src):
                rows[ts] = line
                src_count += 1

        # 3) 按 ts 数值升序排列
        ordered = sorted(rows.items(), key=lambda kv: int(kv[0]))

        # 4) 写回 Latest.txt (CRLF 行结束, 与源文件一致)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="") as fh:
            for _ts, line in ordered:
                fh.write(line + "\r\n")

        # 5) 合并结果已成功保存到 Latest.txt 之后, 删除本次合并的源 txt 文件并打印路径
        deleted = 0
        if not args.keep_sources:
            for src in sorted(sources, key=lambda p: p.name):
                try:
                    src.unlink()
                    print(f"[DEL] {src}")
                    deleted += 1
                except OSError as e:
                    print(f"[WARN] 删除失败: {src} ({e})", file=sys.stderr)

        print(
            f"[OK] {symbol}: "
            f"前缀分组[{', '.join(sorted(prefix_map))}] "
            f"源文件 {len(sources)} 个/{src_count} 行, "
            f"Latest.txt 去重后共 {len(ordered)} 行, "
            f"已删除源文件 {deleted} 个 -> {out_path}"
        )


if __name__ == "__main__":
    main()
