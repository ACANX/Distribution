#!/usr/bin/env python3
"""Task 4: Archive SecuQuoteExecLog json files into a single daily .jsonl file

- 读取 Data/Finv/SecuQuoteExecLog 下生成的 json 文件（每个文件一个 JSON 对象）
- 将全部数据汇总、去重并按时间顺序排列
- 合并导出为一个 jsonl：Archive/Finv/SecuQuoteExecLog/{yyyyMMdd}.jsonl（每行一条记录）
- 文件名日期严格按北京时间处理（运行当天 BJT 日期）
- 目标文件已存在时，增量合并去重后写回（已有数据 + 新数据，整体有序）
"""
import sys, os, json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.config import load_config
from common.logger import setup_logger
from common import gitutil
from common.timeutil import BJT

TASK_NAME = 'Task04ArchiveSecuQuoteExecLogDaily'


def _canonical(record):
    """规范化 JSON 字符串（key 排序 + 紧凑），作为去重键"""
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _line(record):
    """归档行：保留原始 key 顺序的紧凑 JSON"""
    return json.dumps(record, ensure_ascii=False, separators=(',', ':'))


def _sort_ts(record):
    return int(record.get('ts', 0))


def load_existing(path):
    """读取已存在 jsonl 文件的全部记录"""
    if not path.exists():
        return []
    rows = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _archive(config, log):
    """归档逻辑（可测试）。返回本批次新增记录数。"""
    src = config.exec_log_data_dir
    dst = config.exec_log_archive_dir
    if not src.is_dir():
        log.error(f'数据目录不存在: {src}')
        sys.exit(1)
    dst.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in src.iterdir() if p.is_file())
    if not files:
        log.info('无数据文件')
        return 0
    log.info(f'原始文件: {len(files)}')

    # ── 1. 解析全部文件 ──
    records = []
    skipped = 0
    for fp in files:
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                record = json.load(f)
        except Exception:
            skipped += 1
            continue
        if not isinstance(record, dict) or 'ts' not in record:
            skipped += 1
            continue
        records.append(record)
    if skipped:
        log.warning(f'跳过无法解析的文件: {skipped}')

    # ── 2. 与已有归档合并去重 → 按时间排序 → 写回单个文件 ──
    ap = dst / f'{datetime.now(BJT):%Y%m%d}.jsonl'
    merged = {}
    for rec in load_existing(ap):
        merged[_canonical(rec)] = rec
    before = len(merged)
    for rec in records:
        merged[_canonical(rec)] = rec
    new_count = len(merged) - before
    rows = sorted(merged.values(), key=_sort_ts)
    with open(ap, 'w', encoding='utf-8') as f:
        for rec in rows:
            f.write(_line(rec) + '\n')
    log.info(f'{ap.name}: 新增 {new_count}, 总计 {len(rows)} 行')

    if new_count <= 0:
        log.info('无新增数据，跳过提交')
        return 0
    gitutil.add(str(ap), cwd=str(config.repo_root))
    sha = gitutil.commit(
        f'[Quote] Archive SecuQuoteExecLog daily ({ap.name}, +{new_count})',
        cwd=str(config.repo_root),
    )
    if sha:
        log.info(f'归档 commit: {sha}')
    gitutil.push_with_retry(retries=config.git_push_retries, cwd=str(config.repo_root))
    log.info(f'完成: {ap.name} 共 {len(rows)} 行, 新增 {new_count}')
    return new_count


def main():
    config = load_config()
    log = setup_logger(TASK_NAME, str(config.log_dir), config.log_retention_days)
    log.info('=' * 50)
    log.info(f'任务四: 归档 SecuQuoteExecLog 执行日志 ({config.exec_log_data_dir})')
    _archive(config, log)


if __name__ == '__main__':
    main()
