#!/usr/bin/env python3
"""Task 1: Aggregate raw source files into Latest.mvsv (full range)"""
import sys, os
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.config import load_config
from common.logger import setup_logger
from common.mvsv import MVSVData, MVSVMetadata, parse, serialize, merge_and_dedup, scan_source_files, _expand_to_11cols
from common import gitutil
from common.timeutil import ts_to_bjt_date, BJT, UTC

TASK_NAME = 'Task01AggregateLatest'


def cleanup_raw(source_files, latest_path, code, config, log):
    """Remove raw files fully covered by Latest.
    Logs detailed reason for any files that cannot be removed.
    """
    latest = parse(str(latest_path))
    if not latest.rows:
        return
    mn, mx = int(latest.rows[0][0]), int(latest.rows[-1][0])
    removed = 0
    kept = 0
    for sf in source_files:
        try:
            fd = parse(sf)
        except Exception:
            continue
        if not fd.rows:
            continue
        fmn, fmx = int(fd.rows[0][0]), int(fd.rows[-1][0])
        if fmn >= mn and fmx <= mx:
            gitutil.rm(sf, cwd=str(config.repo_root))
            removed += 1
            log.info(f'清理: {Path(sf).name}')
        else:
            kept += 1
            log.warning(
                f'{Path(sf).name} 未被清理，'
                f'文件 ts 范围 [{fmn}, {fmx}] 未被 Latest [{mn}, {mx}] 完整覆盖'
            )
    if removed:
        sha = gitutil.commit(f'[Quote] Cleanup raw for {code}', cwd=str(config.repo_root))
        if sha:
            log.info(f'清理 commit: {sha}')
    gitutil.push_with_retry(retries=config.git_push_retries, cwd=str(config.repo_root))


def process_code(code, config, log):
    data_dir = config.data_dir / code
    if not data_dir.is_dir():
        log.warning(f'目录不存在: {data_dir}')
        return
    source_files = scan_source_files(str(data_dir))
    if not source_files:
        log.info('无原始文件')
        return
    log.info(f'原始文件: {len(source_files)}')
    now_bjt = datetime.now(BJT)
    lp = data_dir / 'Latest.mvsv'
    base = parse(str(lp)) if lp.exists() else MVSVData()
    before = len(base.rows)
    for sf in source_files:
        fd = parse(sf)
        base = merge_and_dedup(base, fd, now_bjt=now_bjt)
    log.info(f'合并: {before} -> {len(base.rows)} 行')
    _expand_to_11cols(base)
    log.info(f'扩展: {len(base.rows)} 行 -> 11列 (Open/Low/High)')
    serialize(base, str(lp))
    log.info(f'Latest: {len(base.rows)} 行 (ts范围: {base.rows[0][0] if base.rows else "N/A"} ~ {base.rows[-1][0] if base.rows else "N/A"})')
    gitutil.add(str(lp), cwd=str(config.repo_root))
    sha = gitutil.commit(f'[Quote] Aggregate Latest for {code}', cwd=str(config.repo_root))
    if sha:
        log.info(f'Latest commit: {sha}')
    gitutil.push_with_retry(retries=config.git_push_retries, cwd=str(config.repo_root))
    if config.cleanup_raw_after_aggregate:
        cleanup_raw(source_files, lp, code, config, log)


def main():
    config = load_config()
    log = setup_logger(TASK_NAME, str(config.log_dir), config.log_retention_days)
    log.info('=' * 50)
    log.info(f'任务一: 聚合原始文件到 Latest（全量）')
    codes = config.codes or sorted(d.name for d in config.data_dir.iterdir() if d.is_dir())
    log.info(f'证券: {codes}')
    start = datetime.now()
    ok = fail = 0
    for code in codes:
        cl = log.for_code(code)
        cl.info('--- 开始 ---')
        try:
            process_code(code, config, cl)
            cl.info('--- 完成 ---')
            ok += 1
        except Exception as e:
            cl.error(f'失败: {e}')
            fail += 1
    sec = (datetime.now() - start).total_seconds()
    log.info(f'完成: 成功 {ok}, 失败 {fail}, 耗时 {sec:.1f}s')
    if fail:
        sys.exit(1)

if __name__ == '__main__':
    main()
