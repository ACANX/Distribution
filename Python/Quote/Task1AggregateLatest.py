#!/usr/bin/env python3
"""Task 1: Aggregate to Latest.mvsv"""
import sys, os
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.config import load_config
from common.logger import setup_logger
from common.mvsv import MVSVData, MVSVMetadata, parse, serialize, merge_and_dedup, scan_source_files
from common import gitutil
from common.timeutil import ts_to_bjt_date, BJT, UTC

TASK_NAME = 'task1'


def archive_and_trim(base, code, config, log):
    """Archive rows older than daily_archive_after_days, then trim oldest."""
    now_utc = datetime.now(UTC)
    cutoff = int((now_utc - timedelta(days=config.daily_archive_after_days)).timestamp())
    archive_rows = [r for r in base.rows if int(r[0]) < cutoff]
    keep_rows = [r for r in base.rows if int(r[0]) >= cutoff]
    if not archive_rows:
        return base
    log.info(f'归档 {len(archive_rows)} 行，保留 {len(keep_rows)} 行')
    by_date = {}
    for r in archive_rows:
        d = ts_to_bjt_date(int(r[0]))
        by_date.setdefault(d, []).append(r)
    archive_base = config.archive_dir / 'Day' / code
    archive_base.mkdir(parents=True, exist_ok=True)
    for d, rows in sorted(by_date.items()):
        ds = d.strftime('%Y%m%d')
        ap = archive_base / f'{code}_Min_{ds}.mvsv'
        md = MVSVMetadata()
        for k, v in base.metadata.values.items():
            md.values[k] = v
        md.extra = dict(base.metadata.extra)
        day_data = MVSVData(metadata=md, rows=rows)
        if ap.exists():
            day_data = merge_and_dedup(parse(str(ap)), day_data, now_bjt=datetime.now(BJT))
        serialize(day_data, str(ap))
        log.info(f'日归档: {ap.name} ({len(rows)} 行)')
        gitutil.add(str(ap), cwd=str(config.repo_root))
    sha = gitutil.commit(f'[quote] archive daily for {code} ({len(by_date)} days)', cwd=str(config.repo_root))
    if sha:
        log.info(f'归档 commit: {sha}')
    return MVSVData(metadata=base.metadata, rows=keep_rows)


def cleanup_raw(source_files, latest_path, config, log):
    """Remove raw files fully covered by Latest."""
    latest = parse(str(latest_path))
    if not latest.rows:
        return
    mn, mx = int(latest.rows[0][0]), int(latest.rows[-1][0])
    removed = 0
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
            log.warning(f'未覆盖保留: {Path(sf).name}')
    if removed:
        sha = gitutil.commit(f'[quote] cleanup raw for {code}', cwd=str(config.repo_root))
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
    log.info(f'合并: {before} -> {len(base.rows)} 行 (+{len(base.rows)-before})')
    base = archive_and_trim(base, code, config, log)
    cutoff_latest = int((datetime.now(UTC) - timedelta(days=config.latest_window_days)).timestamp())
    base.rows = [r for r in base.rows if int(r[0]) >= cutoff_latest]
    serialize(base, str(lp))
    log.info(f'Latest: {len(base.rows)} 行')
    gitutil.add(str(lp), cwd=str(config.repo_root))
    sha = gitutil.commit(f'[quote] aggregate Latest for {code}', cwd=str(config.repo_root))
    if sha:
        log.info(f'Latest commit: {sha}')
    gitutil.push_with_retry(retries=config.git_push_retries, cwd=str(config.repo_root))
    if config.cleanup_raw_after_aggregate:
        cleanup_raw(source_files, lp, config, log)


def main():
    config = load_config()
    log = setup_logger(TASK_NAME, str(config.log_dir), config.log_retention_days)
    log.info('=' * 50)
    log.info(f'任务一: 聚合到 Latest ({config.data_dir})')
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
