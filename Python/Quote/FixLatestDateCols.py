#!/usr/bin/env python3
"""
修复 Latest.mvsv 及归档文件中 Date/Time 列相关数据问题

检测并修复:
  1. 混合列宽：同一文件中 6 列与 8 列行混排（缺失 Date/Time）
  2. 异常列宽：列数不在 {6, 8} 范围内（如之前 bug 产生的 10 列）
  3. 元数据与数据列数不一致

修复策略:
  - 6 列 → 从 ts 按北京时间(Date/Time)补全为 8 列
  - 异常列宽 → 截取前 2 列(ts,Date,Time) + 后 5 列(c,v,t,r,cp) = 8 列
  - 更新 #字段 / #字段名称 / #字段类型 元数据

用法:
  python3 Python/Quote/FixLatestDateCols.py [--all]
  python3 Python/Quote/FixLatestDateCols.py           # 仅 Latest.mvsv
  python3 Python/Quote/FixLatestDateCols.py --all      # 包括归档文件

安全:
  - 只修复有问题的行，正常行保持不变
  - 原子写（tmp + rename），中断不产生半成品
  - 修复后 git status 可查看变更
"""

import sys, os, glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.mvsv import parse, serialize, MVSVData, _expand_to_11cols
from common.timeutil import ts_to_bjt_dt, BJT
from common.mvsv import _update_meta_with_datetime
from datetime import datetime


def repair_rows(data: MVSVData) -> int:
    """修复 data 中 Date/Time 列相关问题，返回修复的行数"""
    now_bjt = datetime.now(BJT)
    new_rows = []
    fixed = 0

    for r in data.rows:
        n = len(r)
        if n == 8:
            new_rows.append(r)  # 正常，保持不变
        elif n == 6:
            bjt_dt = ts_to_bjt_dt(int(r[0]))
            r = [r[0],
                 bjt_dt.strftime('%Y%m%d'),
                 bjt_dt.strftime('%H%M%S')] + r[1:]
            new_rows.append(r)
            fixed += 1
        else:
            # 异常列宽: 取前 1 列(ts) + Date/Time + 后 5 列(c,v,t,r,cp)
            bjt_dt = ts_to_bjt_dt(int(r[0]))
            r = [r[0],
                 bjt_dt.strftime('%Y%m%d'),
                 bjt_dt.strftime('%H%M%S')] + r[-5:]
            new_rows.append(r)
            fixed += 1

    data.rows = new_rows
    if fixed:
        _update_meta_with_datetime(data.metadata)
    return fixed


def scan_patterns(base_dir: str, include_archives: bool) -> list:
    """返回待扫描的文件路径列表"""
    patterns = [
        os.path.join(base_dir, 'Data', 'Finv', 'SecuQuote', '*', 'Latest.mvsv'),
    ]
    if include_archives:
        patterns.extend([
            os.path.join(base_dir, 'Archive', 'Finv', 'SecuQuote', 'Day', '*', '*.mvsv'),
            os.path.join(base_dir, 'Archive', 'Finv', 'SecuQuote', '*', '*', '*.mvsv'),
        ])
    files = []
    for p in patterns:
        files.extend(glob.glob(os.path.normpath(p)))
    return sorted(set(files))


def validate_after_fix(path: str, data: MVSVData, expected_cols: set = {8}):
    """修复后校验：所有行列数一致"""
    col_counts = set(len(r) for r in data.rows)
    if len(col_counts) != 1:
        raise ValueError(f'修复后仍存在混合列宽: {col_counts}')
    n = col_counts.pop()
    if n not in expected_cols:
        raise ValueError(f'修复后列宽异常: {n}')
    field_def = data.metadata.get('字段', '')
    if field_def:
        meta_n = len(field_def.split('|'))
        if meta_n != n:
            raise ValueError(f'元数据字段数({meta_n})与数据列数({n})不一致')


def main():
    include_archives = '--all' in sys.argv

    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    files = scan_patterns(base_dir, include_archives)

    if not files:
        print('未找到任何待扫描文件')
        sys.exit(0)

    total_fixed = 0
    total_files = 0

    for path in files:
        rel = os.path.relpath(path, base_dir)
        is_latest = 'Latest.mvsv' in path
        try:
            data = parse(path)
            fixed = repair_rows(data)
            if fixed:
                serialize(data, path)
                validate_after_fix(path, data)
                print(f'  ✅ {rel}: 修复 {fixed} 行')
                total_fixed += fixed
                total_files += 1
            # 将 Latest.mvsv 从 8 列扩展为 11 列（Open/Low/High）
            if is_latest and data.rows and len(data.rows[0]) == 8:
                _expand_to_11cols(data)
                serialize(data, path)
                validate_after_fix(path, data, {11})
                print(f'  ↪ {rel}: 扩展为 11 列 (Open/Low/High)')
                total_fixed += len(data.rows)
                total_files += 1
        except Exception as e:
            print(f'  ❌ {rel}: {e}')

    print(f'\n完成: 检查 {len(files)} 个文件，修复 {total_files} 个文件共 {total_fixed} 行')
    return total_fixed


if __name__ == '__main__':
    main()
