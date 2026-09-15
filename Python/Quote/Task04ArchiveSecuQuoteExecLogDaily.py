#!/usr/bin/env python3
"""Task 4: Archive SecuQuoteExecLog json files into a single daily .jsonl file

- 读取 Data/Finv/SecuQuoteExecLog 下生成的 json 文件（每个文件一个 JSON 对象）
- 将全部数据汇总、去重（唯一键: ts + selected_code）并按时间顺序排列
- 合并导出为一个 jsonl：Archive/Finv/SecuQuoteExecLog/{yyyyMMdd}.jsonl（每行一条记录）
- 文件名日期严格按北京时间处理（运行当天 BJT 日期）
- 目标文件已存在时，增量合并去重后写回（已有数据 + 新数据，整体有序）
- 归档确认成功后，删除已入档的源文件（git rm 留痕，日志打印每个文件路径）
"""
import sys, os, json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.config import load_config
from common.logger import setup_logger
from common import gitutil
from common.timeutil import BJT

TASK_NAME = 'Task04ArchiveSecuQuoteExecLogDaily'


def _dedup_key(record):
    """去重键: ts + selected_code（同一时间戳同一标的只保留一条）"""
    return (int(record.get('ts', 0)), str(record.get('selected_code', '')))


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
    """归档逻辑（可测试）。返回本批次新增记录数。

    流程:
      1. 解析源目录全部文件
      2. 与已有归档合并去重、排序，写回 Archive/{BJT日期}.jsonl
      3. 确认归档写入成功（读回校验行数）
      4. 删除已成功归档的源文件（git rm 留痕），日志打印每个文件路径
    """
    src = config.exec_log_data_dir
    dst = config.exec_log_archive_dir
    if not src.is_dir():
        log.info(f'数据目录不存在（可能已全部归档清理）: {src}')
        return 0
    dst.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in src.iterdir() if p.is_file())
    if not files:
        log.info('无数据文件')
        return 0
    log.info(f'原始文件: {len(files)}')

    # ── 1. 解析全部文件，记录成功解析的文件（用于删除） ──
    records = []
    parsed_files = []   # 成功解析、将入档的源文件
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
        parsed_files.append(fp)
    if skipped:
        log.warning(f'跳过无法解析的文件: {skipped}（不删除）')

    # ── 2. 与已有归档合并去重（键: ts+selected_code）→ 按时间排序 → 写回单个文件 ──
    ap = dst / f'{datetime.now(BJT):%Y%m%d}.jsonl'
    merged = {}
    for rec in load_existing(ap):
        merged[_dedup_key(rec)] = rec
    before = len(merged)
    for rec in records:
        merged[_dedup_key(rec)] = rec
    new_count = len(merged) - before
    rows = sorted(merged.values(), key=_sort_ts)
    with open(ap, 'w', encoding='utf-8') as f:
        for rec in rows:
            f.write(_line(rec) + '\n')
    log.info(f'{ap.name}: 新增 {new_count}, 总计 {len(rows)} 行')

    # ── 3. 确认归档已成功写出（读回校验行数） ──
    with open(ap, 'r', encoding='utf-8') as f:
        written = sum(1 for _ in f if _.strip())
    if written != len(rows):
        log.error(f'归档校验失败: 写入 {len(rows)} 行, 读回 {written} 行，中止删除源文件')
        return 0
    log.info(f'归档校验通过: {ap.name} {written} 行')

    # ── 4. 归档 commit（如有新增） ──
    committed = False
    if new_count > 0:
        gitutil.add(str(ap), cwd=str(config.repo_root))
        sha = gitutil.commit(
            f'[Quote] Archive SecuQuoteExecLog daily ({ap.name}, +{new_count})',
            cwd=str(config.repo_root),
        )
        if sha:
            log.info(f'归档 commit: {sha}')
            committed = True
    else:
        log.info('无新增数据，跳过归档提交')

    # ── 5. 归档成功后删除已入档的源文件（git rm 留痕，日志打印路径） ──
    if config.cleanup_exec_log_raw_after_archive and parsed_files:
        log.info(f'删除源文件: {len(parsed_files)} 个')
        for fp in parsed_files:
            log.info(f'  删除 {fp}')
        gitutil.rm_many(parsed_files, cwd=str(config.repo_root))
        sha = gitutil.commit(
            f'[Quote] Cleanup SecuQuoteExecLog raw ({len(parsed_files)} files)',
            cwd=str(config.repo_root),
        )
        if sha:
            log.info(f'清理 commit: {sha}')
            committed = True
    elif not config.cleanup_exec_log_raw_after_archive:
        log.info('已禁用源文件清理 (CleanupExecLogRawAfterArchive=false)，保留源文件')
    else:
        log.info('无源文件可删除')

    if committed:
        gitutil.push_with_retry(retries=config.git_push_retries, cwd=str(config.repo_root))
    else:
        log.info('无 commit，跳过 push')
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
