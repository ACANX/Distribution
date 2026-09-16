#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ArchiveDailyMvsv.py —— QuoteGold 行情按北京时间自然日归档为 .mvsv
================================================================================

功能
----
读取 quote-gold 分支 Data/Finv/GoldQuote/{Code}/Latest.txt 中的分钟级行情, 按北京时间
自然日切分, 逐日渲染成 .mvsv, 再经 GitHub Contents API 提交回**同分支**的归档路径:

    Data/Finv/GoldQuote/{Code}/Latest.txt                        ← 本地检出, 只读
    Archive/Finv/QuoteGold/Day/{Region}_{Market}/{Code}/
        {Region}_{Market}_{Code}_MIN_{Provider}_{yyyyMMdd}.mvsv  ← Contents API 写入

源文件行格式
------------
    ts|dt|c
      ts : 秒级时间戳(UTC 纪元秒)
      dt : 北京时间 yyyyMMddHHmmss
      c  : 最新价
    兼容 4 字段变体 ts|date|time|c (date=yyyyMMdd, time=HHmmss)。

归档窗口
--------
    [今日 BJT 00:00 − 6 天, 今日 BJT 00:00), 即 D-6 … D-1 共 6 个整天。
    每次运行都重刷整个窗口(而非只归档昨天): Latest.txt 由 QuoteGoldMergeFileToLatestTxt
    工作流增量合并、历史行不可变, 重刷是幂等的; 窗口重叠还能让上游迟到或修正的数据在
    之后 6 次运行内自动收敛, 无需人工补数。
    注意: 窗口是滑动重刷的, 若工作流连续停摆超过 6 天, 停摆期间的日子不会被补归档。

时区口径
--------
    ts 是 UTC 纪元秒, dt 是北京时间(UTC+8)。切分日期与输出行的 Date/Time 列一律由 ts
    换算成北京时间得出 —— 以 ts 为唯一基准, 保证"行按 ts 排序"与"行内 Date/Time"永远
    自洽。dt 与换算结果不一致时打告警, 但取值仍以 ts 为准。

输出 .mvsv 结构(11 列, 对齐参考文件)
------------------------------------
    # 元数据区(中文一组 + 英文一组) → 空行 → 数据区(| 分隔)
    ts|Date|Time|Open|Close|Low|High|Volume|Turnover|ChangePrice|ChangeRatio

    源文件只提供 Close: Low/High/Volume/Turnover/ChangePrice/ChangeRatio 一律留空;
    Open 仅在相邻两行 ts 间隔恰为 60 秒时取上一行 Close(沿用本仓库 Quote 归档
    _expand_to_11cols 的既有口径), 否则留空。推导在**整个序列**上完成后再按日切分,
    因此某天首行若与前一天末行间隔 60 秒, 同样能取到跨日的 Open。

幂等与去重
----------
    远端已有同名 .mvsv 时先读回, 按 ts 合并(新数据优先)后整体重写, 因此不会丢失
    早先归档、但已滑出当前窗口的行。若合并结果与远端完全一致, 则跳过提交, 避免每天
    产生 12 个仅 采集时间 变化的空提交。

依赖
----
    仅标准库, 外加同仓库 .github/Python/ 下的 GitHubCommitContent(Contents API)与
    ConsoleLog(行首时间戳)。令牌经环境变量 GIT_COMMIT_TOKEN 注入。

用法
----
    python3 .github/Python/QuoteGold/ArchiveDailyMvsv.py                 # 正常归档
    python3 .github/Python/QuoteGold/ArchiveDailyMvsv.py --dry-run       # 只打印, 不提交
    python3 .github/Python/QuoteGold/ArchiveDailyMvsv.py --code GAP-CMB  # 只处理指定品种
    python3 .github/Python/QuoteGold/ArchiveDailyMvsv.py --as-of-date 20260916
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 公共模块与 QuoteGold/ 同级(位于 .github/Python/), 先入 sys.path 再导入
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ConsoleLog import enableLogTimestamps                      # noqa: E402
from GitHubCommitContent import commit_content, read_file_text # noqa: E402

# ── 时区 ──────────────────────────────────────────────────────────────────────
BJT = timezone(timedelta(hours=8))

# ── 提交目标(同分支) ──────────────────────────────────────────────────────────
# 显式写死仓库身份, 不走 Commit.json / .git/config 推断, 避免检出方式变化时提交到别处;
# 可用环境变量覆盖, 便于在别的 fork 上测试。改动须与工作流 env 中的同名变量保持一致。
TARGET_OWNER = os.environ.get("GOLD_ARCHIVE_OWNER", "ACANX")
TARGET_REPO = os.environ.get("GOLD_ARCHIVE_REPO", "Distribution")
TARGET_BRANCH = os.environ.get("GOLD_ARCHIVE_BRANCH", "quote-gold")

# 令牌环境变量(与 GitHubCommitContent.ENV_TOKEN 一致)
TOKEN_ENV = "GIT_COMMIT_TOKEN"

# ── 路径 ──────────────────────────────────────────────────────────────────────
SOURCE_ROOT = ("Data", "Finv", "GoldQuote")           # {Code}/Latest.txt
ARCHIVE_ROOT = "Archive/Finv/QuoteGold/Day"           # {Region}_{Market}/{Code}/...

# ── 归档窗口 ──────────────────────────────────────────────────────────────────
WINDOW_DAYS = 6          # 归档 D-6 … D-1, 共 6 个整天
OPEN_CONTINUITY_SECONDS = 60   # 相邻两行间隔为此值时, Open 取上一行 Close

# ── 11 列字段定义(与参考文件一致) ─────────────────────────────────────────────
FIELD = "Ts|Date|Time|Open|Close|Low|High|Volume|Turnover|ChangePrice|ChangeRatio"
FIELD_NAME = "时间戳(UTC)|日期|时间|开盘价|收盘价|最低价|最高价|成交量|成交额|涨跌值|涨跌幅(%)"
FIELD_TYPE = "int|int|int|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|str"

# ── 归档品种表 ────────────────────────────────────────────────────────────────
SYMBOLS = (
    {"code": "GAP-CMB",  "region": "CN", "market": "CNOTC",
     "provider": "CMB",  "timezone": "Asia/Shanghai"},
    {"code": "ACG-ICBC", "region": "CN", "market": "CNOTC",
     "provider": "ICBC", "timezone": "Asia/Shanghai"},
)


def repo_root() -> Path:
    """仓库根目录。本文件位于 <根>/.github/Python/QuoteGold/, 上溯三级即根。"""
    return Path(__file__).resolve().parents[3]


def archive_path(sym: dict, day: str) -> str:
    """归档文件的仓库内路径(相对仓库根, 用 / 分隔)。

    例: Archive/Finv/QuoteGold/Day/CN_CNOTC/GAP-CMB/
            CN_CNOTC_GAP-CMB_MIN_CMB_20260810.mvsv
    """
    prefix = "%s_%s_%s" % (sym["region"], sym["market"], sym["code"])
    return "%s/%s_%s/%s/%s_MIN_%s_%s.mvsv" % (
        ARCHIVE_ROOT, sym["region"], sym["market"], sym["code"],
        prefix, sym["provider"], day,
    )


def read_latest(path: Path):
    """读取行情 txt。

    :return: (records, stats)
             records — [(ts, dt_raw, close), ...], 按 ts 升序且同 ts 去重(后行覆盖前行)
             stats   — {"total": 非空行数, "skipped": 跳过行数, "dup": 重复 ts 数}
    """
    by_ts = {}
    total = skipped = dup = 0
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            total += 1
            parts = [p.strip() for p in line.split("|")]
            # 3 字段 ts|dt|c(当前格式); 4 字段 ts|date|time|c(兼容变体)
            if len(parts) not in (3, 4):
                skipped += 1
                continue
            ts_raw, dt_raw, close = parts[0], parts[1], parts[-1]
            if not ts_raw.isdigit() or not close:
                skipped += 1
                continue
            ts = int(ts_raw)
            if ts in by_ts:
                dup += 1
            by_ts[ts] = (ts, dt_raw, close)
    records = [by_ts[k] for k in sorted(by_ts)]
    return records, {"total": total, "skipped": skipped, "dup": dup}


def build_rows(records):
    """把源记录展开成 11 列数据行(尚未按日切分)。

    :return: (rows, mismatched)
             rows       — [[ts, Date, Time, Open, Close, Low, High, Volume, Turnover,
                            ChangePrice, ChangeRatio], ...], 与 records 同序
             mismatched — 源 dt 与 ts 换算出的北京时间不一致的行数
    """
    rows, mismatched = [], 0
    prev_ts = prev_close = None
    for ts, dt_raw, close in records:
        bjt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(BJT)
        date_s, time_s = bjt.strftime("%Y%m%d"), bjt.strftime("%H%M%S")
        # 源 dt 只作交叉校验: 3 字段是 yyyyMMddHHmmss, 4 字段是 yyyyMMdd(无时分秒可校)
        if len(dt_raw) == 14 and dt_raw.isdigit() and dt_raw != date_s + time_s:
            mismatched += 1
        # Open: 相邻两行间隔恰为 60 秒时取上一行 Close, 否则留空(首行无上一行, 留空)
        open_s = prev_close if (prev_ts is not None
                                and ts - prev_ts == OPEN_CONTINUITY_SECONDS) else ""
        rows.append([str(ts), date_s, time_s, open_s, close,
                     "", "", "", "", "", ""])
        prev_ts, prev_close = ts, close
    return rows, mismatched


def split_by_day(rows, window_start, window_end):
    """按北京时间自然日归组, 只保留 [window_start, window_end) 内的日期。

    :return: {date(YYYYMMDD): [row, ...]}, 按日期升序
    """
    buckets = {}
    for row in rows:
        bjt = datetime.fromtimestamp(int(row[0]), tz=timezone.utc).astimezone(BJT)
        if not (window_start <= bjt < window_end):
            continue
        buckets.setdefault(bjt.strftime("%Y%m%d"), []).append(row)
    return {d: buckets[d] for d in sorted(buckets)}


def _fmt_meta(key, value) -> str:
    """元数据行。值含 : # | " 时加双引号; 空值写裸行(对齐参考文件的 `# 备注 :`)。"""
    v = "" if value is None else str(value)
    if v and any(c in v for c in ':#|"'):
        return '# %s : "%s"' % (key, v)
    return "# %s : %s" % (key, v) if v else "# %s :" % key


def render_mvsv(sym: dict, lines, fetch_time: str) -> str:
    """渲染完整 .mvsv 文本: 元数据区 → 空行 → 数据区。LF 换行, 无结尾换行。"""
    count = str(len(lines))
    out = [
        _fmt_meta("标题", "%s 分钟级行情数据" % sym["code"]),
        _fmt_meta("数据供应商", sym["provider"]),
        _fmt_meta("字段", FIELD),
        _fmt_meta("字段名称", FIELD_NAME),
        _fmt_meta("字段类型", FIELD_TYPE),
        _fmt_meta("计数", count),
        _fmt_meta("采集时间", fetch_time),
        _fmt_meta("证券代码", sym["code"]),
        _fmt_meta("地区", sym["region"]),
        _fmt_meta("市场", sym["market"]),
        _fmt_meta("时区", sym["timezone"]),
        _fmt_meta("备注", ""),
        # —— 英文镜像 ——
        _fmt_meta("Title", "%s Minute Quote Data" % sym["code"]),
        _fmt_meta("DataProvider", sym["provider"]),
        _fmt_meta("Field", FIELD),
        _fmt_meta("FieldName", FIELD),
        _fmt_meta("FieldType", FIELD_TYPE),
        _fmt_meta("Count", count),
        _fmt_meta("FetchTime", fetch_time),
        _fmt_meta("SecuCode", sym["code"]),
        _fmt_meta("Region", sym["region"]),
        _fmt_meta("Market", sym["market"]),
        _fmt_meta("Timezone", sym["timezone"]),
    ]
    out.append("")
    out.extend(lines)
    return "\n".join(out)


def parse_mvsv(text: str):
    """拆解远端已有 .mvsv。

    :return: (rows, meta)
             rows — {ts 字符串: 数据行原文}
             meta — {键: 值}, 去掉首尾双引号; 空值键(如 `# 备注 :`)取 ""
    """
    rows, meta = {}, {}
    head, _, body = text.replace("\r\n", "\n").partition("\n\n")
    for line in head.split("\n"):
        line = line.strip()
        if not line.startswith("#") or ":" not in line:
            continue
        key, _, val = line[1:].partition(":")
        val = val.strip()
        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
            val = val[1:-1]
        meta[key.strip()] = val
    for line in body.split("\n"):
        line = line.strip()
        if not line:
            continue
        rows[line.split("|", 1)[0]] = line
    return rows, meta


def fetch_existing(path_key: str):
    """读回远端已有归档。

    :return: (rows, meta, found) —— 读取失败一律返回 ({}, {}, False), 由调用方按"首次
             归档"处理并继续; 但真正的网络/权限失败会打告警, 以免静默丢历史。
    """
    res = read_file_text(path_key, branch=TARGET_BRANCH,
                         owner=TARGET_OWNER, repo=TARGET_REPO)
    if res["success"]:
        rows, meta = parse_mvsv(res["text"])
        return rows, meta, True
    # read_file_text 在 404 时 http_status 为 None, 只能靠 message 判定"文件尚不存在"
    msg = res.get("message") or ""
    if "404" in msg:
        return {}, {}, False
    print("[WARN] 读取远端归档失败, 本次按首次归档处理(可能丢失已归档行的历史): %s"
          % msg)
    return {}, {}, False


def meta_unchanged(meta: dict, sym: dict) -> bool:
    """已有归档的品种标识字段是否与当前配置一致(用于判定能否跳过空提交)。"""
    expect = {
        "数据供应商": sym["provider"], "证券代码": sym["code"],
        "地区": sym["region"], "市场": sym["market"], "时区": sym["timezone"],
        "字段": FIELD, "字段类型": FIELD_TYPE,
    }
    return all(meta.get(k, "") == v for k, v in expect.items())


def commit_with_retry(path_key: str, content: str, commit_msg: str, attempts: int = 3):
    """Contents API 提交, 对 409(sha 过期)与网络抖动重试。

    Contents API 的 sha 是**文件级**的, 因此别的提交(如 QuoteGoldMergeFileToLatestTxt
    往 Data/ 推送)不会让我们的 sha 失效; 409 只会出现在同一个归档文件被并发改写时
    (本工作流自身的并发已由 concurrency 组挡住, 这里再兜一层, 以及人工在网页上改过
    该文件的情况)。重试时 commit_content 会重新 GET 一次 sha, 直接续上。
    """
    delay, last = 2, None
    for i in range(1, attempts + 1):
        last = commit_content(path_key, content, branch=TARGET_BRANCH,
                              commit_msg=commit_msg, owner=TARGET_OWNER,
                              repo=TARGET_REPO)
        if last["success"]:
            return last
        status = last.get("http_status")
        msg = last.get("message") or ""
        if status == 409:
            pass                                  # 并发提交导致 sha 过期, 重取即可
        elif status is None and "GIT_COMMIT_TOKEN" not in msg:
            pass                                  # 网络/超时类失败
        else:
            break                                 # 401/403/422/令牌缺失: 重试无意义
        if i < attempts:
            print("[WARN] 第 %d/%d 次提交失败(%s), %d 秒后重试"
                  % (i, attempts, msg, delay))
            time.sleep(delay)
            delay *= 2
    return last


def process_symbol(sym: dict, args, now_bjt: datetime) -> bool:
    """归档单个品种, 成功返回 True。"""
    code = sym["code"]
    log = lambda msg: print("[%s] %s" % (code, msg))   # noqa: E731

    src = repo_root().joinpath(*SOURCE_ROOT) / code / "Latest.txt"
    if not src.exists():
        print("[%s] [SKIP] 源文件不存在: %s" % (code, src))
        return True

    records, stats = read_latest(src)
    if not records:
        log("无有效数据(总 %d 行, 跳过 %d 行), 跳过" % (stats["total"], stats["skipped"]))
        return True

    rows, mismatched = build_rows(records)
    window_end = datetime.combine(now_bjt.date(), datetime.min.time(), tzinfo=BJT)
    window_start = window_end - timedelta(days=args.days)
    by_day = split_by_day(rows, window_start, window_end)

    log("源 %d 行(跳过 %d, 重复 ts %d); 窗口 [%s, %s) BJT 命中 %d 天, 共 %d 行"
        % (stats["total"], stats["skipped"], stats["dup"],
           window_start.strftime("%Y%m%d"), window_end.strftime("%Y%m%d"),
           len(by_day), sum(len(v) for v in by_day.values())))
    if mismatched:
        log("[WARN] %d 行的源 dt 与 ts 换算的北京时间不一致, 已按 ts 取值" % mismatched)

    if not by_day:
        return True

    fetch_time = now_bjt.strftime("%Y-%m-%d %H:%M:%S")
    # 无令牌的 dry-run 完全离线: 拿不到远端存量, 合并基线视为空
    offline = args.dry_run and not os.environ.get(TOKEN_ENV, "").strip()
    if offline:
        log("[DRY-RUN] 未检测到 %s, 离线运行: 不读远端存量, '新增'以空基线计算" % TOKEN_ENV)
    ok = True
    for day, day_rows in by_day.items():
        path_key = archive_path(sym, day)
        new_lines = ["|".join(r) for r in day_rows]
        new_map = {line.split("|", 1)[0]: line for line in new_lines}

        ex_rows, ex_meta, found = ({}, {}, False) if offline else fetch_existing(path_key)
        merged = dict(ex_rows)
        merged.update(new_map)                      # 新数据优先, 同时保留已滑出窗口的历史行
        merged_lines = [merged[k] for k in sorted(merged, key=int)]
        added = len(merged) - len(ex_rows)

        if found and merged_lines == [ex_rows[k] for k in sorted(ex_rows, key=int)] \
                and meta_unchanged(ex_meta, sym):
            log("%s: 无变化(%d 行), 跳过提交" % (day, len(merged_lines)))
            continue

        content = render_mvsv(sym, merged_lines, fetch_time)
        if args.dry_run:
            log("[DRY-RUN] %s: 将写 %d 行(新增 %d) -> %s"
                % (day, len(merged_lines), added, path_key))
            continue

        res = commit_with_retry(
            path_key, content,
            "[QuoteGold] archive %s %s (%d rows)" % (code, day, len(merged_lines)))
        if res["success"]:
            log("%s: 已提交 %d 行(新增 %d, HTTP %s) -> %s"
                % (day, len(merged_lines), added, res["http_status"], path_key))
        else:
            print("[%s] [ERROR] %s 提交失败: %s" % (code, day, res["message"]))
            ok = False

    return ok


def main() -> None:
    enableLogTimestamps()
    parser = argparse.ArgumentParser(
        description="QuoteGold Latest.txt 按北京时间自然日归档为 .mvsv(Contents API)")
    parser.add_argument("--code", action="append", default=None,
                        help="只处理指定品种(可重复); 默认处理全部")
    parser.add_argument("--days", type=int, default=WINDOW_DAYS,
                        help="归档窗口天数, 默认 %d(即 D-N … D-1)" % WINDOW_DAYS)
    parser.add_argument("--as-of-date", default=None, metavar="YYYYMMDD",
                        help="把'今日'固定为指定北京时间日期, 便于补数/测试")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将要提交的内容, 不实际提交")
    args = parser.parse_args()

    if args.as_of_date:
        now_bjt = datetime.strptime(args.as_of_date, "%Y%m%d").replace(tzinfo=BJT)
    else:
        now_bjt = datetime.now(BJT)

    if not args.dry_run and not os.environ.get(TOKEN_ENV, "").strip():
        print("[ERROR] 未设置环境变量 %s, 无法经 Contents API 提交(可加 --dry-run 先干跑)"
              % TOKEN_ENV)
        sys.exit(2)

    symbols = SYMBOLS
    if args.code:
        wanted = {c.strip() for c in args.code if c.strip()}
        symbols = [s for s in SYMBOLS if s["code"] in wanted]
        unknown = wanted - {s["code"] for s in SYMBOLS}
        if unknown:
            print("[ERROR] 未知品种: %s" % ", ".join(sorted(unknown)))
            sys.exit(2)

    print("=" * 72)
    print("QuoteGold 按日归档  %s" % ("[DRY-RUN]" if args.dry_run else ""))
    print("目标仓库: %s/%s @ %s" % (TARGET_OWNER, TARGET_REPO, TARGET_BRANCH))
    print("今日(BJT): %s | 窗口: D-%d … D-1" % (now_bjt.strftime("%Y%m%d"), args.days))
    print("品种: %s" % ", ".join(s["code"] for s in symbols))
    print("=" * 72)

    failed = [s["code"] for s in symbols if not process_symbol(s, args, now_bjt)]
    if failed:
        print("[ERROR] 以下品种归档未全部成功: %s" % ", ".join(failed))
        sys.exit(1)
    print("全部完成")


if __name__ == "__main__":
    main()
