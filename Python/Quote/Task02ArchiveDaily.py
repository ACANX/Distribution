#!/usr/bin/env python3
"""Task 2: Archive daily from Latest.mvsv"""
import sys, os
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.config import load_config
from common.logger import setup_logger
from common.mvsv import MVSVData, MVSVMetadata, parse, serialize, merge_and_dedup
from common import gitutil
from common.timeutil import ts_to_bjt_date, BJT, UTC

TASK_NAME = 'Task02ArchiveDaily.'


def process_code(code, config, log):
    data_dir = config.data_dir / code
    latest_path = data_dir / 'Latest.mvsv'
    if not latest_path.exists():
        log.info('Latest.mvsv 不存在')
        return
    latest = parse(str(latest_path))
    if not latest.rows:
        log.info('Latest.mvsv 无数据')
        return

    # ── 1. 归档超过 DailyArchiveAfterDays 的数据 ──
    cutoff = int((datetime.now(UTC) - timedelta(days=config.daily_archive_after_days)).timestamp())
    archive_candidates = [r for r in latest.rows if int(r[0]) < cutoff]
    if archive_candidates:
        log.info(f'待归档: {len(archive_candidates)} 行')
        by_date = {}
        for r in archive_candidates:
            d = ts_to_bjt_date(int(r[0]))
            by_date.setdefault(d, []).append(r)
        archive_base = config.archive_dir / 'Day' / code
        archive_base.mkdir(parents=True, exist_ok=True)
        archived_dates = []
        for d, rows in sorted(by_date.items()):
            ds = d.strftime('%Y%m%d')
            ap = archive_base / f'{code}_Min_{ds}.mvsv'
            md = MVSVMetadata()
            for k, v in latest.metadata.values.items():
                md.values[k] = v
            md.extra = dict(latest.metadata.extra)
            day_data = MVSVData(metadata=md, rows=rows)
            if ap.exists():
                day_data = merge_and_dedup(parse(str(ap)), day_data, now_bjt=datetime.now(BJT))
                log.info(f'归档合并: {ap.name} ({len(day_data.rows)} 行)')
            serialize(day_data, str(ap))
            log.info(f'写入: {ap.name} ({len(rows)} 行)')
            gitutil.add(str(ap), cwd=str(config.repo_root))
            archived_dates.append(ds)
        commit_msg = f'[Quote] Archive daily for {code} ({len(archived_dates)} days)'
        sha = gitutil.commit(commit_msg, cwd=str(config.repo_root))
        if sha:
            log.info(f'归档 commit: {sha}')
        archive_ts = {int(r[0]) for r in archive_candidates}
        latest.rows = [r for r in latest.rows if int(r[0]) not in archive_ts]
    else:
        log.info('无需要归档的数据')

    # ── 2. 裁剪超过 LatestWindowDays 的数据 ──
    before_trim = len(latest.rows)
    cutoff_latest = int((datetime.now(UTC) - timedelta(days=config.latest_window_days)).timestamp())
    latest.rows = [r for r in latest.rows if int(r[0]) >= cutoff_latest]
    trimmed = before_trim - len(latest.rows)
    if trimmed:
        log.info(f'Latest 裁剪: {trimmed} 行（超过 {config.latest_window_days} 天）')
    else:
        log.info(f'Latest: {len(latest.rows)} 行，无需裁剪')

    # ── 3. 有变更时写回 + 提交 ──
    if archive_candidates or trimmed:
        serialize(latest, str(latest_path))
        log.info(f'Latest 写回: {len(latest.rows)} 行')
        gitutil.add(str(latest_path), cwd=str(config.repo_root))
        sha2 = gitutil.commit(
            f'[quote] trim Latest for {code}'
            f' (archive {len(archive_candidates)} rows, trim {trimmed} rows)',
            cwd=str(config.repo_root),
        )
        if sha2:
            log.info(f'裁剪 commit: {sha2}')
        gitutil.push_with_retry(retries=config.git_push_retries, cwd=str(config.repo_root))
    else:
        log.info('无变更，跳过提交')


def main():
    config = load_config()
    log = setup_logger(TASK_NAME, str(config.log_dir), config.log_retention_days)
    log.info('=' * 50)
    log.info(f'任务二: 按日归档 ({config.data_dir})')
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
