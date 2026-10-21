#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Task07 —— 月归档 MVSV 跨仓库转存
================================================================================

把**本仓库**(ACANX/Distribution, 采集归档端) quote 分支
`Archive/Finv/SecuQuote/{年}/{Code}/` 下已生成的**月归档 .mvsv** 文件, 经 GitHub
Contents API 转存到**另一个仓库** acdnx/Distribution(长期归档端)的 quote 分支
**同路径**下:

    本仓库  Archive/Finv/SecuQuote/{年}/{Code}/{Code}_Min_{yyyyMM}.mvsv
    转存端  Archive/Finv/SecuQuote/{年}/{Code}/{Code}_Min_{yyyyMM}.mvsv

    文件名与目录结构原样保留, 转存端落点 = 源路径。

    源侧由 .github/Python/Quote/Task03ArchiveMonthly.py 每月生成(从日归档
    合并去重得到, 当月中途也会被重写补数据), 本脚本按同一命名接收 —— 两边改名
    须同步。

    转存确认送达后, 该源文件经 Contents API 从**本仓库**删除(见"源端清理")。

与 Task05ArchiveSecuQuoteExecLogJsonlTransfer / quote-gold.GoldQuoteArchiveTransfer /
news.PublishNewsFlashJsonl 同一套跨仓库约定(重要):
    * ACANX/Distribution 与 acdnx/Distribution 是两个真实存在的**独立仓库**, 不是
      同一仓库的大小写变体。转存目标必须显式写死(脚本内置默认 + 工作流 env 注入),
      **绝不从 .git 解析** —— 一旦误用 .git 解析结果, 提交会写回本仓库, 与"转到
      另一个仓库"的意图正好相反。
    * GIT_COMMIT_TOKEN 需同时具备**两个仓库**的权限: 读本仓库(checkout 检出用) +
      写本仓库的 quote 分支(删除已转存的源文件用), 写 acdnx/Distribution 的 quote
      分支(Contents API 提交用)。
    * 转存只读本仓库、只写 acdnx; 源端清理只写本仓库、绝不碰 acdnx。两件事用的
      仓库身份来源完全不同, 见"源端清理"。
    * 转存端**多余的文件不删**(源端已无、转存端仍在的不做清理) —— 转存端是长期
      归档, 只增不删。

幂等
----
    转存前算源文件的 git blob sha, 与转存端同路径文件的 sha 比较(**不下载**远端
    内容): 一致即视为"已送达", 跳过; 不一致或不存在才提交。因此可以放心定期重跑,
    不会产生内容不变的空提交。

    与 Task05 的差异: 月归档在**当月中途仍会变化**(Task03 每次都从日归档重新
    合并去重, 补进新交易日的数据)。本脚本默认只处理**往月**(月份 < 当月, 按北京
    时间判定)的归档 —— 当月文件要到下个月才定稿, 提前转存只会造成"转存端有旧版、
    源端已删"的尴尬局面。用 `--month` 显式指定月份可绕过该保护(见"配置来源")。

源端清理
--------
    转存**确认送达**后(本次提交成功, 或转存端已有同一份内容而跳过提交), 该源文件
    随即经 Contents API 从**源端**删除, 源端归档目录不再随月份无限增长; 提交失败的
    源文件一律保留, 留待下次重跑。

        * 送达判据是内容层面的(blob sha 相同), 不看是否本次提交所得 —— 转存端已有
          同一份内容, 转存目的即已达成, 源文件同样可删。
        * 幂等: 远端已无此文件视为删除成功, 重复运行不报错。
        * 源端身份**必须**经 load_owner_repo_from_git_config() 从本仓库 .git/config
          解析。绝不能复用 TARGET_OWNER/TARGET_REPO, 也不能走 _resolve_target ——
          两者指向的都是转存端 acdnx/Distribution(Commit.json 登记的正是 acdnx),
          拿它当删除目标会把转存端删掉。
        * 只删远端, **不碰本地工作区**: 与本脚本"不依赖本地 git 提交"的设计一致,
          本地文件在下次 pull/checkout 时自然消失, 因此不会与本地 git push 竞争。
          本地跑时看到"文件还在"属正常现象。

    注意: 月归档被删(转存后)意味着 Task03 下个月再从日归档重建该月时, 会因源端
    已无该文件而重新生成 —— 这与 Task05 处理 ExecLog 的语义一致, 属预期行为。

提交方式
--------
    复用同仓 .github/Python/GitHubCommitContent.py, 经 GitHub Contents API 提交到
    转存端仓库的 quote 分支; 删除同样经 Contents API(提交是 PUT, 删除是 DELETE)。
    两者都不依赖本地 git 提交, 因此不与本仓库其它定时任务的数据推送产生 push 竞争。

配置来源(优先级从高到低)
------------------------
    1. 命令行参数(--month / --limit / --force / --dry-run / --include-current)
    2. 环境变量 ARCHIVE_TRANSFER_TARGET_OWNER / _TARGET_REPO / _BRANCH / _MONTH / _LIMIT;
       以及源端删除的目标分支 ARCHIVE_TRANSFER_SOURCE_BRANCH
    3. 脚本内置默认(acdnx / Distribution / quote); 源端删除分支缺省取本地当前检出
       分支, 取不到(detached HEAD / 非 git 环境)回退 quote

用法
----
    python3 .github/Python/Quote/Task07MonthlyMvsvTransfer.py --dry-run
    python3 .github/Python/Quote/Task07MonthlyMvsvTransfer.py
    python3 .github/Python/Quote/Task07MonthlyMvsvTransfer.py --month 202608
    python3 .github/Python/Quote/Task07MonthlyMvsvTransfer.py --include-current

依赖: 仅 Python 3 标准库 + 同仓 .github/Python/GitHubCommitContent.py
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 同目录的上一级(.github/Python/)放着公共 Contents API 封装, 加进搜索路径后 import
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
from GitHubCommitContent import (  # noqa: E402
    DEFAULT_API_BASE, _auth_headers, _get_file_sha, _parse_json, _request,
    commit_content_file, load_owner_repo_from_git_config,
)

# ── 转存端(写死, 绝不从 .git 解析 —— 两个仓库是彼此独立的真实仓库) ──────────
TARGET_OWNER = os.environ.get("ARCHIVE_TRANSFER_TARGET_OWNER", "acdnx")
TARGET_REPO = os.environ.get("ARCHIVE_TRANSFER_TARGET_REPO", "Distribution")
TARGET_BRANCH = os.environ.get("ARCHIVE_TRANSFER_BRANCH", "quote")
TOKEN_ENV = "GIT_COMMIT_TOKEN"

# ── 源端(本仓库, 删除用) ────────────────────────────────────────────────────
# 注意: 源端身份只能从本仓库 .git/config 解析(load_owner_repo_from_git_config),
# 绝不能复用上面的 TARGET_* —— 那是转存端, 拿它当删除目标会删错仓库。
# 源端归档就落在检出分支上, 故删除目标分支取当前检出分支, 取不到回退 quote。
SOURCE_BRANCH_ENV = "ARCHIVE_TRANSFER_SOURCE_BRANCH"
DEFAULT_SOURCE_BRANCH = "quote"

# 源目录(仓库内相对路径); 结构 Archive/Finv/SecuQuote/{年}/{Code}/{文件名}
ARCHIVE_ROOT = Path("Archive") / "Finv" / "SecuQuote"

# 月归档文件名: {Code}_Min_{yyyyMM}.mvsv(如 000001_Min_202606.mvsv)
FNAME_MONTH_RE = re.compile(r"^(.+)_([A-Za-z0-9]+)_(\d{6})\.mvsv$")

# 源路径深度: Archive / Finv / SecuQuote / {年} / {Code} / {文件名}
SRC_DEPTH = 6

# 北京时间(UTC+8), 用于判定"往月"; 不依赖 tzdata, 直接固定偏移
BJT = timezone(timedelta(hours=8))

TAG = "Quote"


def repo_root() -> Path:
    """仓库根目录。本文件位于 <根>/.github/Python/Quote/, 上溯三级即根。"""
    return Path(__file__).resolve().parents[3]


def current_month_bjt() -> str:
    """当前月份(yyyyMM, 按北京时间)。

    月归档的"当月中途会变"判定以此为准 —— 与 GitHub Actions 的 UTC 触发时点无关,
    避免月初 0-8 点(北京)刚过而 UTC 仍是上月造成的误判。
    """
    return datetime.now(BJT).strftime("%Y%m")


def source_branch() -> str:
    """源端(本仓库)删除操作的目标分支。

    源端取的就是检出分支上的本地文件(见模块 docstring 的"源端"说明), 删除也照此:
    环境变量 ARCHIVE_TRANSFER_SOURCE_BRANCH 优先, 其次本地当前检出分支, 最后回退
    quote。detached HEAD 取不到分支名时同样回退。
    """
    env = os.environ.get(SOURCE_BRANCH_ENV, "").strip()
    if env:
        return env
    try:
        out = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                             cwd=str(repo_root()), capture_output=True,
                             text=True, timeout=10)
        name = (out.stdout or "").strip() if out.returncode == 0 else ""
        if name and name != "HEAD":
            return name
    except (OSError, subprocess.SubprocessError):
        pass
    return DEFAULT_SOURCE_BRANCH


def local_blob_sha(path: Path):
    """按 git blob 语义计算本地文件的内容指纹: sha1("blob <字节数>\\0" + 内容)。

    与 GitHub Contents API 返回的 sha 字段同口径, 可直接比较, 从而在**不下载**
    远端文件的前提下判断"转存端是否已是同一份内容"。
    与 commit_content_file 一致按 UTF-8 文本读入再编码, 保证指纹与实际上传的字节
    完全相同 —— 否则 Windows 检出出的 CRLF 会让两边对不上, 白白多提交一次。

    :param path: 本地文件路径
    :return: 40 位 sha1 十六进制字符串; 读取失败返回 None
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = fh.read().encode("utf-8")
    except OSError:
        return None
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def parse_month_file(rel_path: str):
    """解析月归档相对路径, 校验命名与目录结构, 抽出 (年月, 证券代码)。

    只接受 Archive/Finv/SecuQuote/{年}/{Code}/{Code}_Min_{yyyyMM}.mvsv 这一种结构:

        * 路径深度必须是 6 段, 且首段为 Archive/Finv/SecuQuote;
        * 文件名必须匹配 {Code}_{Period}_{yyyyMM}.mvsv, 且 {yyyyMM} 是 6 位数字;
        * 第 4 段({年})必须是 4 位数字;
        * 文件名里的 {Code} 必须与所在目录的 {Code} 一致(防止目录/文件名错配)。

    与 Task05(按日, 文件名尾段 yyyyMMdd)不同: 月归档按**月**, 且**不含日期段**,
    所以这里用显式结构校验而不是简单正则收尾。

    :param rel_path: 相对仓库根的文件路径("/" 分隔)
    :return: (yyyyMM, Code); 不符合结构返回 (None, None)
    """
    parts = rel_path.split("/")
    if len(parts) != SRC_DEPTH:
        return None, None
    if parts[0] != "Archive" or parts[1] != "Finv" or parts[2] != "SecuQuote":
        return None, None
    year, dir_code, filename = parts[3], parts[4], parts[5]
    if not (len(year) == 4 and year.isdigit()):
        return None, None
    m = FNAME_MONTH_RE.match(filename)
    if not m:
        return None, None
    file_code, period, month = m.group(1), m.group(2), m.group(3)
    if file_code != dir_code:
        return None, None  # 目录与文件名里的 Code 不一致, 宁可不搬也不误搬
    if not (len(month) == 6 and month.isdigit()):
        return None, None
    return month, file_code


def collect_files(src_root: Path, only_month=None, include_current=False):
    """递归收集月归档根下所有合规的 .mvsv。

    结构/命名不符合 parse_month_file 的候选一律跳过并告警(宁可不搬也不误搬)。
    默认只收**往月**(month < 当月北京时间); include_current=True 时当月也收,
    显式 --month 指定时按指定月份过滤(不受往月限制)。

    返回按 (年月, 路径) 升序的 [(绝对路径, 仓库内相对路径, 年月)]。

    :param src_root: 源目录绝对路径(仓库内 Archive/Finv/SecuQuote)
    :param only_month: 只收该月份(yyyyMM); None = 不限(但受 include_current 约束)
    :param include_current: 是否把当月也算作可转存
    """
    if not src_root.is_dir():
        print("[WARN] 源目录不存在: %s" % src_root)
        return []
    cur = current_month_bjt()
    tasks = []
    for f in sorted(src_root.rglob("*.mvsv")):
        if not f.is_file():
            continue
        rel = (ARCHIVE_ROOT / f.relative_to(src_root)).as_posix()
        month, code = parse_month_file(rel)
        if month is None:
            print("[WARN] 不符合月归档结构与命名, 跳过: %s" % rel)
            continue
        if only_month and month != only_month:
            continue
        # 未显式指定月份时, 默认排除当月(当月中途仍会变, 提前转存会产生"源端已删、
        # 转存端是旧版"的局面); 显式 --month 视为调用方明确知情, 不再拦。
        if not only_month and not include_current and month >= cur:
            continue
        tasks.append((f, rel, month))
    tasks.sort(key=lambda t: (t[2], t[1]))
    return tasks


def delete_source_file(rel, token, branch, owner, repo, timeout=30):
    """经 Contents API 删除**源端**(本仓库)指定分支上的文件。

    只在转存确认送达后调用。owner/repo 必须由调用方经
    load_owner_repo_from_git_config() 从本仓库 .git 解析后传入 —— 绝不能复用
    TARGET_OWNER/TARGET_REPO, 那是转存端 acdnx/Distribution, 拿它当删除目标会把
    转存端删掉(Commit.json 登记的也正是 acdnx, 因此也不能走 _resolve_target)。

    行为: GET 查 sha → DELETE /contents/{path}(body: message + sha + branch)。

    幂等: 远端已无此文件(查 sha 得 None)视为删除成功, 重复运行不报错。
    只删远端, **不碰本地工作区** —— 与本脚本"不依赖本地 git 提交"的设计一致,
    本地文件在下次 pull/checkout 时自然消失, 因此不会与本地 git push 竞争。

    :param rel: 文件在仓库内的相对路径
    :param token: GitHub 令牌(需源端仓库 contents:write)
    :param branch: 源端分支名
    :param owner: 源端仓库属主(来自本仓库 .git 解析)
    :param repo: 源端仓库名(来自本仓库 .git 解析)
    :param timeout: 单次 HTTP 请求超时秒数
    :return: dict {"success": bool, "already_gone": bool, "message": str|None}
    """
    def fail(msg):
        print("    [ERROR] 删除源文件失败: %s" % msg)
        return {"success": False, "already_gone": False, "message": msg}

    sha = _get_file_sha(DEFAULT_API_BASE, owner, repo, rel, branch, token, timeout)
    if sha is None:
        print("    源端已无此文件(可能上次已删), 视为已删除")
        return {"success": True, "already_gone": True, "message": None}

    url = "%s/%s/%s/contents/%s" % (
        DEFAULT_API_BASE, owner, repo, urllib.parse.quote(rel, safe="/"))
    body = {
        "message": "[%s] delete transferred %s" % (TAG, rel),
        "sha": sha,
        "branch": branch,
    }
    status, text, err = _request(
        "DELETE", url, _auth_headers(token, with_body=True),
        json.dumps(body, ensure_ascii=False).encode("utf-8"), timeout)
    if err:
        return fail(err)
    if status is not None and 200 <= status < 300:
        print("    已删除源端 %s/%s@%s: %s (HTTP %s)"
              % (owner, repo, branch, rel, status))
        return {"success": True, "already_gone": False, "message": None}
    parsed = _parse_json(text)
    if isinstance(parsed, dict) and parsed.get("message"):
        return fail("HTTP %s: %s" % (status, parsed["message"]))
    snippet = (text or "").strip().replace("\n", " ")[:200]
    return fail("HTTP %s: %s" % (status, snippet or "空响应体"))


def main() -> None:
    """入口: 扫描月归档目录, 逐文件与转存端比对 blob sha, 有差异才经 Contents API 提交"""
    parser = argparse.ArgumentParser(
        description="%s 月归档 MVSV 跨仓库转存到 %s/%s, 并删除源端已送达的文件(Contents API)"
                    % (TAG, TARGET_OWNER, TARGET_REPO))
    parser.add_argument("--month", default=None, metavar="YYYYMM",
                        help="只转存该月份的归档; 缺省不限(默认排除当月, 见 --include-current)")
    parser.add_argument("--include-current", action="store_true",
                        help="未指定 --month 时也把当月归档纳入(默认排除, 当月仍在变化)")
    parser.add_argument("--limit", type=int, default=None,
                        help="单次最多处理 N 个文件(0=不限)")
    parser.add_argument("--force", action="store_true",
                        help="转存端已存在且内容一致时也重新提交")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将要执行的动作, 不提交")
    args = parser.parse_args()

    only_month = args.month or os.environ.get("ARCHIVE_TRANSFER_MONTH") or None
    if only_month and not (len(only_month) == 6 and only_month.isdigit()):
        print("[ERROR] --month 必须是 6 位数字(yyyyMM): %s" % only_month)
        sys.exit(2)
    limit = args.limit if args.limit is not None else int(
        os.environ.get("ARCHIVE_TRANSFER_LIMIT", 0))

    token = os.environ.get(TOKEN_ENV, "").strip()
    if not args.dry_run and not token:
        print("[ERROR] 未设置环境变量 %s, 无法经 Contents API 提交(可加 --dry-run 先干跑)"
              % TOKEN_ENV)
        sys.exit(2)

    src_root = repo_root() / ARCHIVE_ROOT
    tasks = collect_files(src_root, only_month, args.include_current)

    print("=" * 72)
    print("%s 月归档 MVSV 跨仓库转存  %s" % (TAG, "[DRY-RUN]" if args.dry_run else ""))
    print("源端     : 本仓库 @ 检出分支(本地工作区)")
    print("转存端   : %s/%s @ %s" % (TARGET_OWNER, TARGET_REPO, TARGET_BRANCH))
    print("源根目录 : %s" % src_root)
    print("月份过滤 : %s" % (only_month or "不限(默认排除当月 %s)" % current_month_bjt()))
    print("命中文件 : %d 个" % len(tasks))
    print("=" * 72)
    if not tasks:
        print("没有可转存的月归档文件, 无事可做")
        return

    picked = tasks
    if limit and limit > 0 and len(tasks) > limit:
        picked = tasks[:limit]
        print("本次按 limit=%d 只处理前 %d 个, 其余 %d 个留待下次"
              % (limit, len(picked), len(tasks) - limit))

    published, skipped, failed = [], [], []
    delivered = []      # 已确认送达转存端(本次提交成功 / 转存端已有同一份内容) → 源文件可删
    for i, (path, rel, month) in enumerate(picked, 1):
        print("\n[%d/%d] %s" % (i, len(picked), rel))
        # 幂等: 本地 blob sha vs 转存端同路径文件 sha(不下载远端内容)
        need = True
        if not args.force and token:
            local_sha = local_blob_sha(path)
            remote_sha = _get_file_sha(DEFAULT_API_BASE, TARGET_OWNER, TARGET_REPO,
                                       rel, TARGET_BRANCH, token, 30)
            if local_sha and remote_sha and local_sha == remote_sha:
                need = False
                print("    转存端 %s/%s 已有相同内容(sha 相同), 跳过提交"
                      % (TARGET_OWNER, TARGET_REPO))
                skipped.append(rel)
            elif remote_sha is None:
                print("    转存端 %s/%s 不存在, 将新建" % (TARGET_OWNER, TARGET_REPO))
            else:
                print("    转存端 %s/%s 已存在但内容有差异, 将更新"
                      % (TARGET_OWNER, TARGET_REPO))
        elif args.force:
            print("    已指定 --force, 强制重新提交")

        if args.dry_run:
            print("    [DRY-RUN] %s" % (
                "将提交, 成功后删除源文件" if need else "跳过提交, 直接删除源文件"))
            continue
        if not need:
            print("    转存端已有同一份内容, 改为删除源文件")
            delivered.append(rel)
            continue

        res = commit_content_file(
            rel, str(path), branch=TARGET_BRANCH, owner=TARGET_OWNER,
            repo=TARGET_REPO,
            commit_msg="[%s] publish %s -> %s/%s@%s"
                       % (TAG, rel, TARGET_OWNER, TARGET_REPO, TARGET_BRANCH))
        if res["success"]:
            print("    已提交(HTTP %s) -> %s/%s@%s"
                  % (res["http_status"], TARGET_OWNER, TARGET_REPO, TARGET_BRANCH))
            published.append(rel)
            delivered.append(rel)
        else:
            print("    [ERROR] 提交失败, 留待下次重跑: %s" % res["message"])
            failed.append("%s: %s" % (rel, res["message"]))

    # ── 删除源端已送达的归档 ────────────────────────────────────────────────
    # 转存确认送达后, 经 Contents API 从**源端**(本仓库)删除该文件, 源端归档目录
    # 不再随月份无限增长。只删已送达的; 提交失败的源文件保留, 留待下次重跑。
    deleted, gone = [], []
    if delivered and not args.dry_run:
        print("\n---- 删除源端已转存文件 (%d 个) ----" % len(delivered))
        # 源端身份必须从本仓库 .git 解析: TARGET_OWNER/TARGET_REPO 指向转存端
        # acdnx/Distribution, 拿它当删除目标会把转存端删掉(Commit.json 登记的也是
        # acdnx, 所以同样不能走 _resolve_target)
        src_owner, src_repo = load_owner_repo_from_git_config()
        if not (src_owner and src_repo):
            print("    [ERROR] 未能从本仓库 .git/config 解析出 owner/repo, "
                  "跳过删除(源文件全部保留, 留待下次重跑)")
            failed.extend("%s: 源端身份解析失败" % rel for rel in delivered)
        else:
            src_branch = source_branch()
            print("    源端: %s/%s @ %s" % (src_owner, src_repo, src_branch))
            for rel in delivered:
                res = delete_source_file(rel, token, src_branch, src_owner, src_repo)
                if not res["success"]:
                    failed.append("%s: 删除失败 %s" % (rel, res["message"]))
                elif res["already_gone"]:
                    gone.append(rel)
                else:
                    deleted.append(rel)

    print("\n======== 转存结束 %s ========" % ("(dry-run)" if args.dry_run else ""))
    if not args.dry_run:
        print("本次处理    : %d 个" % len(picked))
        print("已提交      : %d 个 -> %s/%s@%s"
              % (len(published), TARGET_OWNER, TARGET_REPO, TARGET_BRANCH))
        print("内容一致跳过: %d 个" % len(skipped))
        print("已删源端    : %d 个" % len(deleted))
        if gone:
            print("源端已不存在: %d 个(上次已删)" % len(gone))
        print("失败        : %d 个" % len(failed))
        for item in failed:
            print("    - %s" % item)
        if failed:
            sys.exit(1)
    print("全部完成")


if __name__ == "__main__":
    main()
