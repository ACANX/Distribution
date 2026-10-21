#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
清理归档文件头中的易变元数据字段

背景
----
归档文件头曾原样照抄 Latest.mvsv 的元数据，于是带上了 FetchTime / 采集时间 /
备注(Remark) 三个字段：

    # FetchTime : "2026-09-24 03:16:45"
    # 备注 : "汇总: c=4302500|h=4438700|l=4294800|cnt=6786"
    # 采集时间 : "2026-09-24 03:16:45"

它们描述的是「什么时候抓的」而非「文件里是什么」：时间戳随采集时刻变化，备注里的
汇总统计的还是 Latest 全体而非本文件数据。Latest 保留窗口(36 天)大于归档线(9 天)，
所以每天都有一批已归档的日文件被重新归档 —— 数据一行未变，文件头却在变，于是产生
无意义的提交。

Task02ArchiveDaily.py 已改为归档时剔除这些字段，并只在内容有变化时落盘；本脚本
清理存量文件，让它们此后保持稳定。

清理范围
--------
日归档  Archive/Finv/SecuQuote/Day/<code>/<code>_Min_<yyyyMMdd>.mvsv
    → 移除 FetchTime / 采集时间 / Remark / 备注

月归档  Archive/Finv/SecuQuote/<yyyy>/<code>/<code>_Min_<yyyyMM>.mvsv  (--monthly)
    → 只移除 FetchTime / 采集时间；备注/Remark 由 Task03 用来记「缺失交易日」，保留

用法
----
  python3 .github/Python/Quote/FixArchiveMeta.py --dry-run    # 只列出将清理的字段
  python3 .github/Python/Quote/FixArchiveMeta.py              # 清理日归档
  python3 .github/Python/Quote/FixArchiveMeta.py --monthly    # 一并清理月归档时间戳

安全
----
  - 只动文件头的元数据，数据行原样保留
  - 字段本来就没有的文件直接跳过，不写、不碰（可放心重复执行）
  - 原子写（tmp + rename），中断不产生半成品
  - 不自动提交，修复后 git status 可查看变更
"""

import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.mvsv import parse, serialize, strip_volatile_meta

# 月归档的 备注 / Remark 归 Task03ArchiveMonthly 管（记缺失交易日），不当易变字段清
MONTHLY_STRIP_KEYS = ('FetchTime', '采集时间')


def repo_root() -> str:
    """仓库根目录。本文件位于 <根>/.github/Python/Quote/，上溯三级即根。"""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    # 自校验：万一将来本文件被移到其它层级，这里立刻报错，
    # 而不是静默地去错误的目录里扫描、进而误改无关文件
    if os.path.abspath(__file__) != os.path.join(
            root, '.github', 'Python', 'Quote', os.path.basename(__file__)):
        raise RuntimeError(
            f'无法从脚本位置推断仓库根目录: {root}（本文件所在层级可能已变更）')
    return root


def scan_targets(root: str, monthly: bool):
    """返回 [(文件绝对路径, 要清理的键|None)]，None 表示 VOLATILE_META_KEYS 全集。"""
    quote_root = os.path.join(root, 'Archive', 'Finv', 'SecuQuote')
    targets = [(p, None) for p in
               glob.glob(os.path.join(quote_root, 'Day', '*', '*.mvsv'))]
    if monthly:
        # 月归档在 <四位年份>/<code>/ 下，用字符类把 Day 目录排除在外
        targets += [(p, MONTHLY_STRIP_KEYS) for p in
                    glob.glob(os.path.join(quote_root, '[0-9][0-9][0-9][0-9]', '*', '*.mvsv'))]
    return sorted(set(targets), key=lambda item: item[0])


def main() -> int:
    dry_run = '--dry-run' in sys.argv
    monthly = '--monthly' in sys.argv
    root = repo_root()
    targets = scan_targets(root, monthly)
    if not targets:
        print('未找到任何归档文件')
        return 0

    print('=' * 60)
    print('清理归档文件头的易变元数据' + ('  [DRY-RUN]' if dry_run else ''))
    print(f'范围: 日归档{combined_hint(monthly)}')
    print(f'待检查: {len(targets)} 个文件')
    print('=' * 60)

    cleaned = skipped = failed = 0
    for path, keys in targets:
        rel = os.path.relpath(path, root)
        try:
            data = parse(path)
            removed = strip_volatile_meta(data.metadata, keys)
            if not removed:
                skipped += 1
                continue
            if dry_run:
                print(f'  [DRY-RUN] {rel}: 移除 {", ".join(removed)}')
                cleaned += 1
                continue
            serialize(data, path, only_if_changed=True)
            print(f'  ✅ {rel}: 移除 {", ".join(removed)}')
            cleaned += 1
        except Exception as e:
            print(f'  ❌ {rel}: {e}')
            failed += 1

    print()
    if dry_run:
        print(f'预演: 需清理 {cleaned} 个文件，无需清理 {skipped} 个，失败 {failed} 个')
    else:
        print(f'完成: 清理 {cleaned} 个文件，无需清理 {skipped} 个，失败 {failed} 个')
        if cleaned:
            print('变更已写入工作区（未提交），可用 git status / git diff 查看')
    return 1 if failed else 0


def combined_hint(monthly: bool) -> str:
    return ' + 月归档时间戳' if monthly else ''


if __name__ == '__main__':
    sys.exit(main())
