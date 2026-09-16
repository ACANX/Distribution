#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
APM 埋点数据按日 JSONL 归档(DailyJsonlArchive 前置工序, Contents API 提交)
================================================================================

职责分工(与 apm.DailyJsonlArchive 的主转换 APMJsonlConvert.py 配合)
--------------------------------------------------------------------
    APMJsonlConvert.DailyJsonl.py(本脚本)  —— 前置工序:
        按(类型, 日期)分组, 把每组记录与远端已有 JSONL 合并后, 经 GitHub
        Contents API 整文件提交到 apm 分支的
            Archive/Meta/WebMMCP/APM/{type}/APM_{type}_{Code}_DAY_ACANX_{yyyyMMdd}.jsonl
        每(类型, 日期)至多一个提交; 无新增记录的分组跳过提交(零提交 =
        不产生空 commit)。文件修改/新增一律走 Contents API, 不用 git push。
        分组处理完成后, 把**全部记录都已确认收录**的 Data 侧采集批次文件也经
        Contents API 删除(见下文"源文件清理")。

    .github/Python/APM/APMJsonlConvert.py  —— 主转换:
        幂等重写同一批 JSONL 到 checkout 副本后 git add/push。本前置工序
        完成后, 主转换在 checkout 副本上算出的"新增行"已经先一步经 Contents
        API 进了远端, 主转换自然 0 新增、跳过提交 —— 满足"默认要在主转换
        任务之前先执行完成"。

    分组口径与主转换完全一致: 按记录 ts(13 位毫秒时间戳)换算北京时间日历
    日; **文件名日期不可靠**(存量有 27 个批次按 UTC 命名, 系统性错日),
    一律以 ts 为准。源扫 Data/Meta/WebMMCP/APM 与 Archive/Meta/WebMMCP/APM
    两处(可用 --data-only 收窄为只扫 Data 侧)。

为什么与主转换不共用合并逻辑
----------------------------
    主转换在 checkout 副本上工作, 目标是"本地文件的最终状态", 用
    "读已有行 + append"即可; 本脚本经 Contents API 提交, 每次 PUT 都是
    整文件覆盖, 必须先 read_file_text 读回远端全量行、与本地记录合并后
    整份提交。两者的合并方向相反, 强行抽象反而费解。

行格式(与主转换严格一致)
------------------------
    json.dumps(rec, ensure_ascii=False, separators=(",", ":")), 紧凑格式,
    保留中文, 字段顺序与源一致; 远端已有行原样保留在前, 新行追加在尾部。
    因此两种流程写出的内容互为幂等基线, 重复运行零新增。

源文件清理(归档后删除采集批次)
------------------------------
    Data 侧(Data/Meta/WebMMCP/APM)的采集批次文件在**其全部记录都确认收录进
    远端 JSONL 之后**经 Contents API 删除(DELETE contents), 避免 Data 目录
    无限膨胀; Archive 侧的 merge 历史输出不在删除范围(只增不改)。

    逐记录判定, 从严保护:
        - 文件里每条可归档记录 (日期, 行文本) 都必须在本次处理过的对应
          (类型, 日期) 分组"处理后的远端全量行集合"中命中; 未处理的分组
          (--date 过滤之外)与提交失败的分组一律不删 —— 跨日文件逐条覆盖,
          任何一条未确认收录即整文件保留;
        - 含无法归档记录(JSON 解析失败 / 非对象 / ts 缺失或非法)的文件永不删除,
          删除会永久丢失未归档数据;
        - 空数组文件(无记录)按可删处理(无数据可丢); .gitkeep 不在 *.json 扫描范围。
    --keep-source 可整体关闭删除; --dry-run 只报告不删除。删除失败按 [ERROR]
    报告并以退出码 1 结束(记录已入 JSONL, 文件留待下次运行重删, 幂等安全)。

提交方式(参照 quote-gold 线)
----------------------------
    全部经 GitHub Contents API(默认 ACANX/Distribution@apm), 参照
    quote-gold.ArchiveDailyMvsv.yml + GitHubCommitContent.py 的既有模式:
    令牌经环境变量 GIT_COMMIT_TOKEN 注入(不写进任何源码); 409(sha 过期)
    与网络抖动自动重试; 无变化的分组零提交。

用法
----
    python3 .github/Python/APM/APMJsonlConvert.DailyJsonl.py               # 全量前置
    python3 .github/Python/APM/APMJsonlConvert.DailyJsonl.py --dry-run    # 只统计不提交
    python3 .github/Python/APM/APMJsonlConvert.DailyJsonl.py --type API   # 只跑 API
    python3 .github/Python/APM/APMJsonlConvert.DailyJsonl.py --date 20260629
    python3 .github/Python/APM/APMJsonlConvert.DailyJsonl.py --keep-source # 保留源文件

依赖: 仅 Python 3 标准库 + 同仓库 .github/Python/GitHubCommitContent.py;
     令牌经环境变量 GIT_COMMIT_TOKEN 注入(与 quote-gold 线一致)。
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 公共库位于 .github/Python/(与本文件所在的 .github/Python/APM/ 同级), 先入
# sys.path 再导入 —— 与 .github/Python/QuoteGold/ArchiveDailyMvsv.py 的写法一致
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from GitHubCommitContent import commit_content, delete_file, read_file_text  # noqa: E402

# ── 时区: 记录 ts 统一换算为北京时间日历日 ─────────────────────────────────────
BJT = timezone(timedelta(hours=8))

# ── 路径(相对仓库根) ──────────────────────────────────────────────────────────
DEFAULT_DATA_DIR = "Data/Meta/WebMMCP/APM"        # 采集批次
DEFAULT_ARCHIVE_DIR = "Archive/Meta/WebMMCP/APM"  # JSONL 落点(= merge 历史输出位置)

# Code 命名规则与 JSONL 文件名模板(与主转换 APMJsonlConvert.py 严格一致)
CODE_PREFIX = "MetaCms"
NAME_TMPL = "APM_{type}_{code}_DAY_ACANX_{date}.jsonl"

OUT_ENCODING = "utf-8"

# ts 字段容错阈值: 小于该值视为秒, 否则视为毫秒(与主转换一致)
_MS_EPOCH = 10 ** 12

# ── 提交目标(同分支, 显式写死, 可被环境变量覆盖) ──────────────────────────────
# 显式写死不走 Commit.json / .git/config 推断, 避免检出方式变化时提交到别处
# (参照 quote-gold 线); 改动须与工作流 env 中的同名变量保持一致。
TARGET_OWNER = os.environ.get("APM_ARCHIVE_OWNER", "ACANX")
TARGET_REPO = os.environ.get("APM_ARCHIVE_REPO", "Distribution")
TARGET_BRANCH = os.environ.get("APM_ARCHIVE_BRANCH", "apm")

TOKEN_ENV = "GIT_COMMIT_TOKEN"


def repo_root() -> Path:
    """仓库根目录。本文件位于 <根>/.github/Python/APM/, 上溯三级即根。"""
    return Path(__file__).resolve().parents[3]


def line_of(rec) -> str:
    """记录 -> JSONL 行文本(紧凑, 保留中文, 字段顺序与源一致; 与主转换一致)。"""
    return json.dumps(rec, ensure_ascii=False, separators=(",", ":"))


def ts_to_date(ts) -> str:
    """记录 ts -> 北京时间日历日(yyyyMMdd); 非法返回 None。"""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    if ts < _MS_EPOCH:          # 秒 -> 毫秒
        ts *= 1000
    try:
        return datetime.fromtimestamp(ts / 1000, BJT).strftime("%Y%m%d")
    except (OverflowError, OSError, ValueError):
        return None


def jsonl_path(archive_dir: str, type_name: str, date: str) -> str:
    """(类型, 日期)对应的远端 JSONL 仓库内路径(相对仓库根, / 分隔)。"""
    return "%s/%s/%s" % (archive_dir.rstrip("/"), type_name,
                         NAME_TMPL.format(type=type_name,
                                          code=CODE_PREFIX + type_name,
                                          date=date))


def collect_local(data_root: Path, archive_root: Path, include_archive: bool,
                  only_types, only_dates, log):
    """扫描本地 checkout 的源目录, 返回 (grouped, tracked)。

    data_root 恒扫; archive_root(merge 历史输出)仅当 include_archive=True
    (全量前置模式)时纳入 —— Data-only 模式不该把 merge 历史输出当源。
    grouped 包含与远端可能重复的记录, 由 process_group 与远端行做差。

    :param data_root: 采集批次根目录(删除判定的适用对象)
    :param archive_root: JSONL 落点根目录
    :param include_archive: 是否把 archive_root 也当源扫描
    :param only_types: 一级子目录名白名单(None=不限)
    :param only_dates: 日期白名单 set(None=不限)
    :param log: 逐文件详情输出函数
    :return: (grouped, tracked)
        grouped: {(type, date): [记录, ...]}(保持源文件与记录顺序);
        tracked: {文件路径: {"type": str, "records": [(date, 行文本), ...],
                             "undecodable": bool}}
                 —— 仅 Data 侧(data_root 下)文件, 供归档完成后判定/删除源文件;
                 undecodable=True 表示文件里有无法归档的记录(解析失败/非对象/
                 ts 缺失或非法), 这类文件绝不删除(删除会永久丢失未归档数据)。
    """
    grouped = defaultdict(list)
    tracked = {}
    roots = [data_root] + ([archive_root] if include_archive else [])
    for src_root in roots:
        if not src_root.is_dir():
            log("源目录不存在, 跳过: %s" % src_root)
            continue
        is_data_side = (src_root == data_root)   # 仅 Data 侧参与删除判定
        for type_dir in sorted(p for p in src_root.iterdir() if p.is_dir()):
            if only_types and type_dir.name not in only_types:
                continue
            for f in sorted(type_dir.rglob("*.json")):
                info = {"type": type_dir.name, "records": [], "undecodable": False}
                try:
                    with open(f, "r", encoding=OUT_ENCODING) as fh:
                        data = json.load(fh)
                except (OSError, ValueError) as exc:
                    log("[WARN] JSON 解析失败, 跳过 %s: %s" % (f, exc))
                    info["undecodable"] = True
                    if is_data_side:
                        tracked[f] = info
                    continue
                if not isinstance(data, list):
                    data = [data]           # 兼容意外写入的单对象
                for rec in data:
                    if not isinstance(rec, dict):
                        log("[WARN] 非对象记录, 跳过: %s" % f)
                        info["undecodable"] = True
                        continue
                    date = ts_to_date(rec.get("ts"))
                    if date is None:
                        log("[WARN] 记录 ts 缺失或非法, 跳过: %s" % f)
                        info["undecodable"] = True
                        continue
                    info["records"].append((date, line_of(rec)))
                    if only_dates and date not in only_dates:
                        continue
                    grouped[(type_dir.name, date)].append(rec)
                if is_data_side:
                    tracked[f] = info
    return grouped, tracked


def fetch_remote_lines(path_key: str):
    """读回远端已有 JSONL 全部行(去空行)。

    :return: (lines, found)。404 返回 ([], False) 按"首次归档"处理; 其它失败
             也返回 ([], False) 并告警 —— 与 quote-gold 线 fetch_existing 的
             容错口径一致(基线缺失时合并内容仍完整, 下次运行会以远端最新
             状态重新收敛)。
    """
    res = read_file_text(path_key, branch=TARGET_BRANCH,
                         owner=TARGET_OWNER, repo=TARGET_REPO)
    if res["success"]:
        lines = [ln for ln in (l.rstrip("\n") for l in res["text"].splitlines()) if ln]
        return lines, True
    msg = res.get("message") or ""
    if "404" in msg:
        return [], False
    print("[WARN] 读取远端 JSONL 失败, 本次按首次归档处理(基线可能缺失): %s" % msg)
    return [], False


def commit_with_retry(path_key: str, content: str, commit_msg: str, attempts: int = 3):
    """Contents API 提交, 对 409(sha 过期)与网络抖动重试(口径同 quote-gold 线)。

    Contents API 的 sha 是文件级的, 往不同文件提交互不影响; 409 只在同一
    文件被并发改写时出现(工作流并发已由 concurrency 组挡住, 这里兜底)。
    重试时 commit_content 会重新 GET 一次 sha, 直接续上。
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


def process_group(type_name: str, date: str, recs, archive_dir: str,
                  dry_run: bool, log):
    """处理一个(类型, 日期)分组。

    :return: (status, final_lines)
        - status: "committed" 已提交 / "skipped" 无新增跳过 / "dryrun" 干跑命中 /
                  "failed" 提交失败;
        - final_lines: 该分组处理完成后"远端应有的全部行"文本集合, 供源文件删除
          判定(全部记录都在集合里才允许删除); failed 时为 None(基线不可信);
          dryrun 时为模拟提交后的行集(仅用于报告"将删除", 不会真删)。
    """
    path_key = jsonl_path(archive_dir, type_name, date)

    # 1) 本地记录 -> 去重后的新行(保持首次出现顺序)
    new_lines, seen = [], set()
    for rec in recs:
        ln = line_of(rec)
        if ln not in seen:
            seen.add(ln)
            new_lines.append(ln)

    # 2) 读远端已有行, 求真正的新增
    remote_lines, found = fetch_remote_lines(path_key)
    remote_set = set(remote_lines)
    added = [ln for ln in new_lines if ln not in remote_set]
    if not added:
        # 全部记录都已在远端 —— 远端全量行即本组最终行集(可删判定依据)
        log("%s: 无新增(%d 条记录全部与远端重复), 跳过提交" % (path_key, len(recs)))
        return "skipped", remote_set

    # 3) 合并: 已有行原样保留在前, 新行追加在尾部(与主转换的 append 语义一致)
    merged = remote_lines + added
    content = "\n".join(merged) + "\n"
    if dry_run:
        log("[DRY-RUN] %s: 将提交 %d 行(新增 %d, 远端已有 %d%s)"
            % (path_key, len(merged), len(added), len(remote_lines),
               "" if found else ", 远端文件不存在将新建"))
        return "dryrun", set(merged)

    res = commit_with_retry(path_key, content,
                            "[APM] archive %s %s (%d rows, +%d)"
                            % (type_name, date, len(merged), len(added)))
    if res["success"]:
        log("%s: 已提交 %d 行(新增 %d, HTTP %s)"
            % (path_key, len(merged), len(added), res["http_status"]))
        return "committed", set(merged)
    print("[ERROR] %s 提交失败: %s" % (path_key, res["message"]))
    return "failed", None


def delete_with_retry(path_key: str, commit_msg: str, attempts: int = 3):
    """Contents API 删除源文件, 对 409(sha 过期)与网络抖动重试(口径同 commit_with_retry)。

    删除幂等: 文件已不存在时 delete_file 直接按成功返回(HTTP 404), 因此重试
    与重复运行都不会报错。
    """
    delay, last = 2, None
    for i in range(1, attempts + 1):
        last = delete_file(path_key, branch=TARGET_BRANCH, commit_msg=commit_msg,
                           owner=TARGET_OWNER, repo=TARGET_REPO)
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
            print("[WARN] 第 %d/%d 次删除失败(%s), %d 秒后重试"
                  % (i, attempts, msg, delay))
            time.sleep(delay)
            delay *= 2
    return last


def plan_deletions(tracked, group_final):
    """判定哪些 Data 侧源文件的全部记录都已确认收录进远端 JSONL。

    从严保护(任一不满足即整文件保留):
        - 含无法归档记录(undecodable)的文件直接保留(删除会永久丢失未归档数据);
        - 文件里每条可归档记录 (date, 行文本) 都必须在本次处理过的对应
          (类型, 日期) 分组的"处理后远端全量行集合"中命中 —— 未处理的分组
          (--date/--type 过滤之外)与提交失败的分组(不在 group_final 里)
          一律保护, 跨日文件逐条覆盖。
    空数组文件(records 为空)按可删处理: 无任何数据可丢。

    :param tracked: collect_local 的文件级追踪表
    :param group_final: {(type, date): 处理后远端全量行集合}
    :return: (deletable 路径列表, protected [(路径, 保留原因), ...])
    """
    deletable, protected = [], []
    for f, info in sorted(tracked.items()):
        if info["undecodable"]:
            protected.append((f, "含无法归档的记录"))
            continue
        missing = None
        for (date, line) in info["records"]:
            finals = group_final.get((info["type"], date))
            if finals is None:
                missing = "记录日期 %s 不在本次处理范围(未确认收录)" % date
                break
            if line not in finals:
                missing = "记录日期 %s 的行未在远端 JSONL 中命中" % date
                break
        if missing is None:
            deletable.append(f)
        else:
            protected.append((f, missing))
    return deletable, protected


def main() -> None:
    parser = argparse.ArgumentParser(
        description="APM 埋点按日 JSONL 归档(DailyJsonlArchive 前置工序, "
                    "Contents API 提交到 apm 分支)")
    parser.add_argument("--type", dest="types", action="append", default=None,
                        help="只处理指定的一级子目录名(可重复), 如 API; 默认全部")
    parser.add_argument("--date", dest="dates", action="append", default=None,
                        metavar="YYYYMMDD",
                        help="只处理记录时间(北京时间)为该日的记录(可重复); 缺省不限")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                        help="采集批次根目录(默认 %s)" % DEFAULT_DATA_DIR)
    parser.add_argument("--archive-dir", default=DEFAULT_ARCHIVE_DIR,
                        help="JSONL 落点根目录(默认 %s)" % DEFAULT_ARCHIVE_DIR)
    parser.add_argument("--data-only", action="store_true",
                        help="只扫采集批次目录, 不把 merge 历史输出当源"
                             "(MergeApm*.py 委托本脚本时默认开启)")
    parser.add_argument("--keep-source", action="store_true",
                        help="保留 Data 侧源文件, 不做归档后删除"
                             "(默认删除全部记录已确认收录进远端 JSONL 的源文件)")
    parser.add_argument("--dry-run", action="store_true",
                        help="只统计将要提交的内容, 不实际提交")
    parser.add_argument("--log", action="store_true",
                        help="输出每个源文件的处理详情")
    args = parser.parse_args()

    verbose = args.log

    def log(msg):
        if verbose:
            print(msg, flush=True)

    root = repo_root()
    data_root = Path(args.data_dir)
    if not data_root.is_absolute():
        data_root = root / data_root
    archive_dir_local = Path(args.archive_dir)
    if not archive_dir_local.is_absolute():
        archive_dir_local = root / archive_dir_local

    only_dates = ({d.strip() for d in (args.dates or []) if d and d.strip()}
                  or None)
    grouped, tracked = collect_local(data_root, archive_dir_local,
                                     not args.data_only, args.types, only_dates, log)

    types = sorted({t for (t, _d) in grouped})
    print("=" * 72)
    print("APM 按日 JSONL 归档(Contents API -> %s/%s@%s)%s"
          % (TARGET_OWNER, TARGET_REPO, TARGET_BRANCH,
             "  [DRY-RUN]" if args.dry_run else ""))
    print("源目录   : %s%s" % (data_root,
                               "" if args.data_only
                               else "  (+ %s)" % archive_dir_local))
    print("命名规则 : %s" % NAME_TMPL.format(type="<type>", code=CODE_PREFIX + "<type>",
                                             date="<yyyyMMdd>"))
    print("命中(type, 日期)组: %d  涉及 type: %s" % (len(grouped), ", ".join(types) or "(无)"))
    print("=" * 72)

    if not grouped:
        print("没有可归档的记录, 无事可做")
        return

    if not args.dry_run and not os.environ.get(TOKEN_ENV, "").strip():
        print("[ERROR] 未设置环境变量 %s, 无法经 Contents API 提交(可加 --dry-run 先干跑)"
              % TOKEN_ENV)
        sys.exit(2)

    total_recs = 0
    committed = skipped = failed = 0
    group_final = {}                  # (type, date) -> 处理后远端全量行集合
    for (type_name, date) in sorted(grouped):
        recs = grouped[(type_name, date)]
        total_recs += len(recs)
        status, final_lines = process_group(type_name, date, recs, args.archive_dir,
                                            args.dry_run, log)
        if final_lines is not None:
            group_final[(type_name, date)] = final_lines
        if status in ("committed", "dryrun"):
            committed += 1
        elif status == "skipped":
            skipped += 1
        else:
            failed += 1

    # —— 阶段 2: 源文件清理(全部记录已确认收录的 Data 侧采集批次) ——
    deletable, protected = plan_deletions(tracked, group_final)
    deleted = del_failed = 0
    for f, why in protected:
        log("  保留源文件 %s: %s" % (f.relative_to(root).as_posix(), why))
    if not args.keep_source:
        for f in deletable:
            rel = f.relative_to(root).as_posix()
            if args.dry_run:
                log("  [DRY-RUN] 将删除源文件: %s" % rel)
                continue
            res = delete_with_retry(rel, "[APM] 源文件已归档, 删除 %s" % rel)
            if res["success"]:
                print("  已删除源文件: %s (HTTP %s)" % (rel, res["http_status"]))
                deleted += 1
            else:
                print("[ERROR] 删除源文件失败: %s -> %s" % (rel, res["message"]))
                del_failed += 1

    print("\n======== 归档结束 %s ========" % ("(dry-run)" if args.dry_run else ""))
    print("扫描记录: %d 条" % total_recs)
    print("分组统计: %s %d / 跳过 %d / 失败 %d / 共 %d"
          % ("命中" if args.dry_run else "提交", committed, skipped, failed,
             len(grouped)))
    if args.keep_source:
        print("源文件清理: 已用 --keep-source 关闭, 保留 Data 侧源文件 %d 个"
              % len(tracked))
    elif args.dry_run:
        print("源文件清理: 将删除 %d / 保留 %d / 共 %d 个 Data 侧源文件"
              "(dry-run 不实际删除)" % (len(deletable), len(protected), len(tracked)))
    else:
        print("源文件清理: 删除 %d / 失败 %d / 保留 %d / 共 %d 个 Data 侧源文件"
              % (deleted, del_failed, len(protected), len(tracked)))
    if failed or del_failed:
        if failed:
            print("存在失败分组, 请查看上方 [ERROR] 行")
        if del_failed:
            print("存在删除失败的源文件, 请查看上方 [ERROR] 行")
        sys.exit(1)
    print("全部完成")


if __name__ == "__main__":
    main()
