#!/usr/bin/env python3
"""
修复 Latest.mvsv 中混合列宽问题

部分存量 Latest.mvsv 文件中存在 6 列和 8 列行混排的情况
（缺失 Date/Time 列）。此脚本扫描所有 Latest.mvsv，自动
从 ts 字段按北京时间计算 Date/Time 并补全。

用法:
  python3 Python/Quote/RepairMixedCols.py

安全:
  - 只修复列数不一致的行，8 列行保持不变
  - 原子写（tmp + rename），中断不产生半成品
  - 修复后 git status 可查看变更
"""

import sys, os, glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.mvsv import parse, serialize
from common.timeutil import ts_to_bjt_dt, BJT
from datetime import datetime


def repair_file(path: str) -> int:
    """修复单个 Latest.mvsv 文件，返回修复的行数"""
    data = parse(path)
    col_counts = set(len(r) for r in data.rows)

    if 6 not in col_counts:
        return 0  # 无需修复

    now_bjt = datetime.now(BJT)
    new_rows = []
    for r in data.rows:
        if len(r) == 6:
            bjt_dt = ts_to_bjt_dt(int(r[0]))
            r = [r[0],
                 bjt_dt.strftime('%Y%m%d'),
                 bjt_dt.strftime('%H%M%S')] + r[1:]
        new_rows.append(r)

    data.rows = new_rows
    serialize(data, path)
    return len(data.rows)


def main():
    pattern = os.path.join(
        os.path.dirname(__file__),
        '..', 'Data', 'Finv', 'SecuQuote', '*', 'Latest.mvsv'
    )
    files = glob.glob(os.path.normpath(pattern))

    if not files:
        print('未找到任何 Latest.mvsv 文件')
        sys.exit(0)

    total_fixed = 0
    for path in sorted(files):
        code = os.path.basename(os.path.dirname(path))
        try:
            fixed = repair_file(path)
            if fixed:
                print(f'{code}: 修复 {fixed} 行')
                total_fixed += fixed
            else:
                print(f'{code}: 无需修复')
        except Exception as e:
            print(f'{code}: 失败 - {e}')

    print(f'\n完成: 共修复 {total_fixed} 行')
    return total_fixed


if __name__ == '__main__':
    main()
