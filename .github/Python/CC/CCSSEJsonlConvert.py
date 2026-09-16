#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CCSSEJsonlConvert —— CC/SSE 流量镜像批次按日聚合为 JSONL(Contents API 提交)
================================================================================

把 Data/CC/SSE/ 下采集端落盘的 CC 流量镜像批次(文件名
yyyyMMdd_HHmmss[_SSS]_{type}.json, type 为 CCRequestHeader / CCRequestBody /
CCResponseHeader / CCResponseBody 之一)封装成一行一条记录, 按天落盘:

    Archive/CC/SSE/CC_SSE_CCSSEStreamReqResp_{yyyyMMdd}.jsonl

单行结构(与源 JSON 文件一一对应):

    {"dt":"20260618224143000","type":"CCRequestBody","data":{ ... }}

    - dt:   文件名时间前缀压平成 17 位(yyyyMMdd + HHmmss + SSS), 文件名不带
            毫秒时补 "000" 占位(20260618_224143 → 20260618224143000);
    - type: 文件名后缀(不含扩展名), 即上述四种之一;
    - data: 文件内容 —— 能解析成 JSON 的(CCRequestHeader / CCRequestBody /
            CCResponseHeader)直接放解析结果; **CCResponseBody 是 SSE 流原文
            (event: / data: 行), 不是 JSON**, 按 SSE 规范拆成事件数组
            [{"event":"message_start","data":{...}}, ...], 单事件也是数组。

排序: 整份 JSONL 按 (dt, 类型序) 升序 —— 类型序固定为
CCRequestHeader → CCRequestBody → CCResponseHeader → CCResponseBody,
即 "dt 相同时按请求头 → 请求体 → 响应头 → 响应体" 逐行排列; 未知类型排在
四种之后(按类型名), 并输出告警, 不丢数据。

增量与幂等: 每次运行以源文件为准重算目标 JSONL —— 同 (dt, type) 的行按源文件
内容新增/覆盖, 其余既有行原样保留, 因此定期重跑不会产生重复行, 也不会因源文件
已被删掉而丢行; 内容无变化则跳过提交(不产生空提交)。

提交与删除一律走 GitHub Contents API(不 git push, 复用同仓
.github/Python/GitHubCommitContent.py):
    - 目标 JSONL 有变化才 PUT contents 提交(每个日期一个提交);
    - Data/CC/SSE/ 下源批次**在其记录确认收录进目标 JSONL 之后**经
      DELETE contents 删除(远端已不存在 → 404, 按幂等成功处理); 文件名不合
      模式 / 读取失败 / 解析失败 / 未确认收录的源文件一律保留;
    - 令牌经环境变量 GIT_COMMIT_TOKEN 注入; 失败不静默(退出码 1)。

注: 目标 JSONL 的基线(既有行)从工作区读取, 不额外走 API —— Contents API 的
GET 对 >1MB 的文件不再返回 content 字段(只给 download_url), 而 CC 的
RequestBody 单条就近 1MB, 目标 JSONL 很快会越过这个门槛; 提交/删除所需的 sha
查询不受此限(只取 sha 字段)。

用法
----
    python3 .github/Python/CC/CCSSEJsonlConvert.py --dry-run    # 只推演, 不调 API
    python3 .github/Python/CC/CCSSEJsonlConvert.py              # 提交并清理源文件
    python3 .github/Python/CC/CCSSEJsonlConvert.py --keep-source

    目标仓库/分支可用环境变量 CC_SSE_OWNER / CC_SSE_REPO / CC_SSE_BRANCH 覆盖
    (缺省 ACANX / Distribution / cc-sse), 也可用同名命令行参数显式传入。

依赖: 仅 Python 3 标准库 + 同仓 .github/Python/GitHubCommitContent.py
"""

import argparse
import functools
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

# 公共 Contents API 封装(GitHubCommitContent.py)在 .github/Python/ 下, 与
# 本脚本的 CC/ 子目录相隔一层, 按文件位置把那一层加进搜索路径后 import
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
from GitHubCommitContent import commit_content, delete_file  # noqa: E402

# ── 路径与命名(相对仓库根) ───────────────────────────────────────────────────
SRC_DIR = Path("Data/CC/SSE")
ARCHIVE_DIR = Path("Archive/CC/SSE")
TARGET_STEM = "CC_SSE_CCSSEStreamReqResp"

# 同一 dt 下的行序: 请求头 → 请求体 → 响应头 → 响应体
TYPE_ORDER = ("CCRequestHeader", "CCRequestBody", "CCResponseHeader", "CCResponseBody")

# 源文件名: yyyyMMdd_HHmmss[_SSS]_{type}.json(毫秒段缺省时按 000 补位)
FNAME_RE = re.compile(r"^(\d{8})_(\d{6})(\d{3})?_([A-Za-z][A-Za-z0-9]*)\.json$")

# 目标仓库身份缺省值(工作流会显式传入环境变量, 保持两边一致)
DEFAULT_OWNER = "ACANX"
DEFAULT_REPO = "Distribution"
DEFAULT_BRANCH = "cc-sse"
ENV_TOKEN = "GIT_COMMIT_TOKEN"
ENV_OWNER, ENV_REPO, ENV_BRANCH = "CC_SSE_OWNER", "CC_SSE_REPO", "CC_SSE_BRANCH"

API_TIMEOUT = 120                       # 单次 Contents API 超时(目标 JSONL 可能较大)
API_ATTEMPTS = 3                        # 重试次数(409 sha 过期 / 5xx / 网络抖动)
RETRY_STATUS = (409, 500, 502, 503, 504)


def log(msg):
    """常规进度输出"""
    print(msg, flush=True)


def warn(msg):
    """告警(继续执行)"""
    print("[WARN] " + msg, flush=True)


def error(msg):
    """错误(计入退出码)"""
    print("[ERROR] " + msg, file=sys.stderr, flush=True)


# ── 解析 ────────────────────────────────────────────────────────────────────

def parse_source_name(name):
    """解析源文件名 → (date8, dt, type); 不符合命名模式返回 None

    :param name: 文件名(不含目录)
    :return: (yyyyMMdd, 17 位 dt, type) 或 None
    """
    m = FNAME_RE.match(name)
    if not m:
        return None
    date8, hms, msec, typ = m.groups()
    return date8, date8 + hms + (msec or "000"), typ


def parse_sse_events(text):
    """把 SSE 流原文拆成事件数组 [{"event": 事件名, "data": 载荷}, ...]

    按 SSE 规范的分块口径解析: 行分隔符 \r\n / \r / \n 皆可; 空行结束一个
    事件; ":" 开头的是注释(如 ": keep-alive", 丢弃); event: 给出事件名
    (缺省 "message"); data: 可多行, 以 "\n" 连接后作为该事件的载荷; 其余
    字段(id: / retry:)与归档无关, 忽略。

    载荷优先按 JSON 解析; 解析不动时原样保留字符串, 保证不丢内容。

    :param text: SSE 流原文
    :return: 事件列表(可能为空)
    """
    events = []
    name = None
    data_lines = []

    def flush():
        """结束当前事件并追加到结果"""
        nonlocal name, data_lines
        if name is not None or data_lines:
            raw = "\n".join(data_lines)
            if not raw:
                payload = None
            else:
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = raw
            events.append({"event": name or "message", "data": payload})
        name, data_lines = None, []

    for line in re.split(r"\r\n|\r|\n", text):
        if not line:
            flush()
        elif line.startswith(":"):
            continue
        else:
            field, sep, value = line.partition(":")
            if not sep:
                continue                # 无冒号的裸字段: 规范里值为空, 无可取内容
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                name = value
            elif field == "data":
                data_lines.append(value)
    flush()
    return events


def load_payload(path):
    """读取源文件并解出 data 载荷

    :param path: 源文件路径
    :return: (data, kind, err) —— kind 为 "json"(整体是 JSON)/ "sse"(SSE 流原文);
             err 非 None 表示不可归档(调用方保留该文件不删)
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return None, None, "读取失败: %s" % e
    body = text.strip()
    if body:
        try:
            return json.loads(body), "json", None
        except ValueError:
            pass
    events = parse_sse_events(text)
    if events:
        return events, "sse", None
    return None, None, "内容既不是合法 JSON, 也不是可解析的 SSE 流"


def make_line(dt, typ, data):
    """拼一行 JSONL 文本(键序固定 dt → type → data, 紧凑分隔符)"""
    return json.dumps({"dt": dt, "type": typ, "data": data},
                      ensure_ascii=False, separators=(",", ":"))


def sort_key(dt, typ):
    """排序键: dt 升序; 同 dt 内按类型序, 未知类型排在已知四种之后"""
    try:
        pos = TYPE_ORDER.index(typ)
    except ValueError:
        pos = len(TYPE_ORDER)
    return (dt, pos, typ)


def parse_line_key(line):
    """从既有 JSONL 行里取出 (dt, type) 覆盖键; 取不到返回 None"""
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    dt, typ = obj.get("dt"), obj.get("type")
    if not isinstance(dt, str) or not isinstance(typ, str):
        return None
    return dt, typ


# ── 目标 JSONL ──────────────────────────────────────────────────────────────

def read_lines(path):
    """读目标 JSONL 的既有行(文件不存在 → 空列表; 末尾空行不计)"""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def merge_day(baseline_lines, new_entries):
    """把本次从源文件解出的行并进既有行

    口径: 同 (dt, type) 以源文件为准新增/覆盖; 其余既有行原样保留 —— 尤其
    源文件已被删除的那些天, 行照旧留在 JSONL 里。既有行里解析不出 (dt, type)
    的(异常数据)原样保留在文件开头, 只告警不丢弃。

    :param baseline_lines: 既有行文本列表
    :param new_entries: [(dt, type), 行文本] 列表
    :return: (最终行文本列表(已排序), 新增行数, 更新行数, 无法解析的既有行数)
    """
    kept = []                   # [[排序键, 行文本], ...]
    unknown = []                # 既有行里解析不出 dt/type 的: 原样保留
    index = {}                  # (dt, type) -> kept 下标
    for line in baseline_lines:
        key = parse_line_key(line)
        if key is None:
            unknown.append(line)
            continue
        if key in index:
            warn("目标 JSONL 内已有重复的 dt/type 行, 只保留首条: dt=%s type=%s" % key)
            continue
        index[key] = len(kept)
        kept.append([sort_key(*key), line])
    added = updated = 0
    for key, line in new_entries:
        if key in index:
            slot = kept[index[key]]
            if slot[1] != line:
                slot[0], slot[1] = sort_key(*key), line
                updated += 1
        else:
            index[key] = len(kept)
            kept.append([sort_key(*key), line])
            added += 1
    kept.sort(key=lambda e: e[0])
    return unknown + [e[1] for e in kept], added, updated, len(unknown)


# ── Contents API(带重试) ─────────────────────────────────────────────────────

def api_with_retry(call, what, path_key):
    """执行一次 Contents API 调用, 按需重试(409 sha 过期 / 5xx / 网络抖动)

    :param call: 零参可调用对象, 返回 GitHubCommitContent 的结果 dict
    :param what: 动作名(用于日志): "提交" / "删除"
    :param path_key: 目标路径(用于日志)
    :return: 最后一次的结果 dict
    """
    res = {}
    for attempt in range(1, API_ATTEMPTS + 1):
        res = call()
        if res.get("success"):
            return res
        status = res.get("http_status")
        warn("%s失败(第 %d/%d 次) %s: HTTP %s %s"
             % (what, attempt, API_ATTEMPTS, path_key, status, res.get("message")))
        if status is not None and status not in RETRY_STATUS:
            break               # 明确不可重试(如 403 无权限 / 422), 立即收手
    return res


# ── 主流程 ──────────────────────────────────────────────────────────────────

def parse_args(argv=None):
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="CC/SSE 流量镜像批次按日聚合为 JSONL(经 GitHub Contents API 提交)")
    parser.add_argument("--dry-run", action="store_true",
                        help="只推演将提交/删除什么, 不调用 API、不改动任何文件")
    parser.add_argument("--keep-source", action="store_true",
                        help="保留 Data/CC/SSE/ 下的源文件(默认删除已确认收录的)")
    parser.add_argument("--owner", default=None, help="仓库属主(缺省 %s)" % DEFAULT_OWNER)
    parser.add_argument("--repo", default=None, help="仓库名(缺省 %s)" % DEFAULT_REPO)
    parser.add_argument("--branch", default=None, help="目标分支(缺省 %s)" % DEFAULT_BRANCH)
    return parser.parse_args(argv)


def main(argv=None):
    """入口: 扫描源文件 → 合并并按日提交 JSONL → 删除已确认收录的源文件

    :return: 进程退出码(0 成功; 提交或删除有失败 → 1)
    """
    args = parse_args(argv)
    owner = (args.owner or os.environ.get(ENV_OWNER) or DEFAULT_OWNER).strip()
    repo = (args.repo or os.environ.get(ENV_REPO) or DEFAULT_REPO).strip()
    branch = (args.branch or os.environ.get(ENV_BRANCH) or DEFAULT_BRANCH).strip()
    token = os.environ.get(ENV_TOKEN, "").strip()
    if not args.dry_run and not token:
        error("未设置环境变量 %s, 无法提交/删除(加 --dry-run 可只做本地推演)" % ENV_TOKEN)
        return 1

    log("目标: %s/%s@%s%s" % (owner, repo, branch, "  [DRY-RUN]" if args.dry_run else ""))

    # ── 1) 扫描源文件, 解析成待写入的行 ────────────────────────────────
    src_files = sorted(SRC_DIR.glob("*.json")) if SRC_DIR.is_dir() else []
    if not SRC_DIR.is_dir():
        warn("源目录不存在, 视为无源文件: %s" % SRC_DIR)
    log("扫描源文件: %s → %d 个 *.json" % (SRC_DIR.as_posix(), len(src_files)))

    by_date = defaultdict(list)         # yyyyMMdd -> [(排序键, (dt,type), 行文本, 源路径)]
    bad = []                            # [(源路径, 原因)] —— 一律保留不删
    kinds = defaultdict(int)
    for p in src_files:
        parsed = parse_source_name(p.name)
        if parsed is None:
            bad.append((p, "文件名不符合 yyyyMMdd_HHmmss[_SSS]_{type}.json 模式"))
            continue
        date8, dt, typ = parsed
        data, kind, err = load_payload(p)
        if err:
            bad.append((p, err))
            continue
        if typ not in TYPE_ORDER:
            warn("未知类型(在 JSONL 中排在已知四种之后): %s ← %s" % (typ, p.name))
        kinds[kind] += 1
        by_date[date8].append((sort_key(dt, typ), (dt, typ), make_line(dt, typ, data), p))
    for p, why in bad:
        warn("不可归档, 保留不删: %s —— %s" % (p.name, why))
    parsed_count = sum(len(v) for v in by_date.values())
    if kinds:
        log("解析成功: %d 个(JSON %d / SSE %d)"
            % (parsed_count, kinds.get("json", 0), kinds.get("sse", 0)))

    if not parsed_count:
        log("没有可归档的源文件, 结束")
        return 0

    # ── 2) 逐日期合并目标 JSONL, 经 Contents API 提交 ──────────────────
    confirmed = {}                      # yyyyMMdd -> set(行文本): 已确认收录的行
    commit_failed = 0
    total_added = total_updated = 0
    for date8 in sorted(by_date):
        target = ARCHIVE_DIR / ("%s_%s.jsonl" % (TARGET_STEM, date8))
        rel = target.as_posix()
        entries = sorted(by_date[date8], key=lambda e: e[0])
        baseline = read_lines(target)
        merged, added, updated, unknown = merge_day(baseline, [(k, line) for _sk, k, line, _p in entries])
        total_added += added
        total_updated += updated
        log("%s: 新增 %d 行 / 更新 %d 行 / 既有 %d 行%s"
            % (rel, added, updated, len(baseline) - unknown,
               "(含 %d 行无法解析, 原样保留)" % unknown if unknown else ""))
        if merged == baseline:
            log("  内容无变化, 跳过提交")
            confirmed[date8] = set(merged)
            continue
        content = "\n".join(merged) + "\n"
        msg = "chore(data): CC/SSE 请求响应JSON按日聚合为JSONL %s" % date8
        if args.dry_run:
            log("  [DRY-RUN] 将提交 %s(+%d 行, 提交后共 %d 行)"
                % (rel, added + updated, len(merged)))
            confirmed[date8] = set(merged)
            continue
        res = api_with_retry(
            functools.partial(commit_content, rel, content, branch=branch, commit_msg=msg,
                              owner=owner, repo=repo, token=token, timeout=API_TIMEOUT),
            "提交", rel)
        if not res.get("success"):
            error("提交失败, 该日期源文件全部保留: %s —— %s" % (rel, res.get("message")))
            commit_failed += 1
            continue
        # 提交成功: 同步落一份本地副本, 让同分支的重复运行读到最新基线(幂等)
        try:
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as e:
            warn("本地副本写入失败(不影响远端已提交内容): %s" % e)
        confirmed[date8] = set(merged)
        log("  已提交(%s)" % res.get("http_status"))

    # ── 3) 源文件清理: 记录已确认收录的经 Contents API 删除 ────────────
    deletable = []                      # [(源路径, yyyyMMdd)]
    protected = []                      # [(源路径, 原因)]
    for date8, items in sorted(by_date.items()):
        done = confirmed.get(date8)
        for _sk, _key, line, p in items:
            if done is not None and line in done:
                deletable.append((p, date8))
            else:
                protected.append((p, "记录未确认收录进目标 JSONL"))
    protected.extend((p, why) for p, why in bad)

    del_ok = del_failed = 0
    if args.keep_source:
        log("--keep-source: 保留全部 %d 个源文件" % (len(deletable) + len(protected)))
    else:
        for p, date8 in deletable:
            rel = p.as_posix()
            if args.dry_run:
                log("  [DRY-RUN] 将删除源文件: %s" % rel)
                continue
            msg = ("chore(data): CC/SSE 源文件已收录进 %s_%s.jsonl, 删除 %s"
                   % (TARGET_STEM, date8, p.name))
            res = api_with_retry(
                functools.partial(delete_file, rel, branch=branch, commit_msg=msg,
                                  owner=owner, repo=repo, token=token, timeout=API_TIMEOUT),
                "删除", rel)
            if res.get("success"):
                del_ok += 1
                try:
                    p.unlink()          # 本地副本一并清掉, 免得重跑再查一次
                except OSError as e:
                    warn("本地源文件删除失败(远端已删): %s —— %s" % (rel, e))
            else:
                del_failed += 1
                error("源文件删除失败, 保留待下次重试: %s —— %s" % (rel, res.get("message")))

    # ── 4) 汇总 ────────────────────────────────────────────────────────
    log("=" * 64)
    log("源文件    : 共 %d 个(可归档 %d / 不可归档 %d)" % (len(src_files), parsed_count, len(bad)))
    log("目标 JSONL: %d 个日期, 新增 %d 行 / 更新 %d 行 / 提交失败 %d"
        % (len(by_date), total_added, total_updated, commit_failed))
    if args.dry_run:
        log("源文件清理: [DRY-RUN] 将删除 %d 个 / 保留 %d 个" % (len(deletable), len(protected)))
    elif args.keep_source:
        log("源文件清理: 已跳过(--keep-source), 保留 %d 个" % len(protected))
    else:
        log("源文件清理: 删除 %d 个 / 失败 %d 个 / 保留 %d 个"
            % (del_ok, del_failed, len(protected)))
    return 1 if (commit_failed or del_failed) else 0


if __name__ == "__main__":
    sys.exit(main())
