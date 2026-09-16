#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PublishToArchiveRepo.py —— QuoteGold 归档 .mvsv 跨仓库转存
================================================================================

作用
----
把**本仓库**(ACANX/Distribution, 采集归档端) quote-gold 分支
Archive/Finv/QuoteGold/Day/ 下的 .mvsv 归档文件, 转存到**另一个仓库**
acdnx/Distribution(长期归档端)的 quote-gold 分支**同路径**下:

    本仓库  Archive/Finv/QuoteGold/Day/CN_CNOTC/{Code}/{前缀}_{yyyyMMdd}.mvsv
    转存端  Archive/Finv/QuoteGold/Day/CN_CNOTC/{Code}/{前缀}_{yyyyMMdd}.mvsv

    文件名与目录结构原样保留, 转存端落点 = 源路径。

转存资格(按文件名日期, 北京时间)
--------------------------------
    只转存** 4 天以前 **的文件: 文件名末尾的 yyyyMMdd(北京时间日历日)满足

        <yyyyMMdd> <= 今日(BJT) - 4 天

    才会转存。设计原因: 归档工作流(quote-gold.ArchiveDailyMvsv)每天重刷最近 6 天
    的窗口, 滑出窗口之前的文件内容才完全定稿; 4 天的缓冲保证转存过去的一定是
    不再变化的最终版, 转存端无需跟随本仓库的窗口重刷而反复改写。

    注意 20260910 之类的文件名必须解析为合法日历日; 非法或无法解析的文件名一律
    跳过并告警, 宁可不搬也不误搬。

幂等
----
    转存前先算源文件的 git blob sha, 与转存端同路径文件的 sha 比较(不下载远端
    内容): 一致即视为"已送达", 跳过; 不一致或不存在才提交。因此本脚本可以放心
    定期重跑, 不会产生内容不变的空提交。

    注意: 源文件在本仓库可能因归档工作流的重刷而更新(虽然 4 天前的理论上已定稿,
    但不排除人工修正), 此时转存端会跟着更新 —— 这是期望行为, 转存端永远与源端
    最终版一致。

提交方式
--------
    复用 .github/Python/GitHubCommitContent.py, 经 GitHub Contents API 提交到
    转存端仓库的 quote-gold 分支。不依赖本地 git 提交, 因此不会与本仓库其它
    定时任务的数据推送产生 push 竞争。

两个仓库是彼此独立的仓库(重要)
------------------------------
    源端 : 本脚本所在的仓库(quote-gold 分支检出, 即 ACANX/Distribution)
    转存端: TARGET_OWNER / TARGET_REPO 显式指定的仓库(即 acdnx/Distribution)

    这两个名字**不是同一个仓库的大小写变体**, 而是两个真实存在的独立仓库
    (API id 分别为 1182994715 与 1293253241, 网页互不重定向), 对应
    news 线 PublishNewsFlashJsonl.py 的既有约定。转存目标**写死不从 .git 解析**:
    一旦误用 .git 解析结果, 提交会写回本仓库, 与"转到另一个仓库"的意图正好相反。

配置来源(优先级从高到低)
------------------------
    1. 命令行参数(--days / --code / --date / --limit 等)
    2. 环境变量 QUOTE_GOLD_PUBLISH_DAYS / QUOTE_GOLD_PUBLISH_CODE /
       QUOTE_GOLD_PUBLISH_DATE / QUOTE_GOLD_PUBLISH_LIMIT /
       QUOTE_GOLD_PUBLISH_TARGET_OWNER / QUOTE_GOLD_PUBLISH_TARGET_REPO /
       QUOTE_GOLD_PUBLISH_BRANCH / QUOTE_GOLD_PUBLISH_SRC_BRANCH
    3. 内置默认值: days=4, code=全部, date=不限, limit=0(不限),
       target_owner='acdnx', target_repo='Distribution',
       branch='quote-gold'(转存端), src_branch='quote-gold'(源端)

用法
----
    python3 .github/Python/QuoteGold/PublishToArchiveRepo.py                    # 常规转存
    python3 .github/Python/QuoteGold/PublishToArchiveRepo.py --dry-run          # 只打印
    python3 .github/Python/QuoteGold/PublishToArchiveRepo.py --code GAP-CMB     # 指定品种
    python3 .github/Python/QuoteGold/PublishToArchiveRepo.py --date 20260910    # 指定日期
    python3 .github/Python/QuoteGold/PublishToArchiveRepo.py --days 6           # 改缓冲天数
    python3 .github/Python/QuoteGold/PublishToArchiveRepo.py --force            # 强制重交

依赖
----
    仅标准库, 外加同仓库 .github/Python/ 下的 GitHubCommitContent(Contents API)与
    ConsoleLog(行首时间戳)。令牌经环境变量 GIT_COMMIT_TOKEN 注入, **需具备转存端
    acdnx/Distribution 的 contents:write 权限**(本仓库只读检出, 不写)。
"""

import argparse
import hashlib
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 公共模块与 QuoteGold/ 同级(位于 .github/Python/), 先入 sys.path 再导入
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ConsoleLog import enableLogTimestamps                      # noqa: E402
from GitHubCommitContent import (                              # noqa: E402
    DEFAULT_API_BASE,
    _get_file_sha,
    commit_content_file,
)

# ── 时区 ──────────────────────────────────────────────────────────────────────
BJT = timezone(timedelta(hours=8))

# ── 转存端仓库身份: 显式写死, 不经 .git 解析(理由见模块 docstring) ─────────────
# 可用环境变量覆盖, 便于在别的 fork 上测试。改动须与工作流 env 中的同名变量保持一致。
TARGET_OWNER = os.environ.get("QUOTE_GOLD_PUBLISH_TARGET_OWNER", "acdnx")
TARGET_REPO = os.environ.get("QUOTE_GOLD_PUBLISH_TARGET_REPO", "Distribution")
TARGET_BRANCH = os.environ.get("QUOTE_GOLD_PUBLISH_BRANCH", "quote-gold")

# 令牌环境变量(与 GitHubCommitContent.ENV_TOKEN 一致)
TOKEN_ENV = "GIT_COMMIT_TOKEN"

# ── 路径 ──────────────────────────────────────────────────────────────────────
ARCHIVE_ROOT = Path("Archive") / "Finv" / "QuoteGold" / "Day"
# 任务范围: 只转存 Day/CN_CNOTC/ 这一个 Region_Market 目录(其下为品种目录)。
# 将来若要扩展到其它市场, 只需改这一个常量。
REGION_MARKET = "CN_CNOTC"

# ── 转存缓冲 ──────────────────────────────────────────────────────────────────
DEFAULT_DAYS = 4   # 只转存 文件名日期 <= 今日(BJT) - 4 天 的文件

# 源文件名: {Region}_{Market}_{Code}_MIN_{Provider}_{yyyyMMdd}.mvsv
# 只用末尾的 8 位日期做资格判断, 前缀不严格校验(保留对未来品种的兼容)。
# 注意品种名含连字符(GAP-CMB / ACG-ICBC), 前缀不能用 \w+, 用宽松的 .+ 贪婪回溯
# 到最后一个 _MIN_ 即可。
FNAME_DATE_RE = re.compile(r"^(.+)_MIN_[A-Za-z0-9]+_(\d{8})\.mvsv$")


def repo_root() -> Path:
    """仓库根目录。本文件位于 <根>/.github/Python/QuoteGold/, 上溯三级即根。"""
    return Path(__file__).resolve().parents[3]


def local_blob_sha(path: Path):
    """按 git blob 语义计算本地文件的内容指纹: sha1("blob <字节数>\\0" + 内容)。

    与 GitHub Contents API 返回的 sha 字段同口径, 可直接比较, 从而在**不下载**
    远端文件的前提下判断"转存端是否已是同一份内容"。
    与 commit_content_file 一致按 UTF-8 文本读入再编码, 保证指纹与实际上传的字节
    完全相同 —— 否则 Windows 检出出的 CRLF 会让两边对不上, 白白多提交一次。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = fh.read().encode("utf-8")
    except OSError:
        return None
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def collect_files(src_root: Path, cutoff: str, only_code, only_date):
    """扫描源目录, 收集满足转存资格的文件。

    目录层级为 Day/{Region_Market}/{Code}/{文件}; 扫描范围限定 REGION_MARKET 子目录。
    资格: 文件名匹配 FNAME_DATE_RE, 日期合法, 且 日期 <= cutoff(ISO yyyymmdd 字符串
    直接字典序比较, 等价数值比较)。返回按品种、日期升序排列的 [(path, rel, date)]。
    """
    tasks = []
    region_dir = src_root / REGION_MARKET
    if not region_dir.is_dir():
        print("[WARN] 源目录不存在: %s" % region_dir)
        return tasks
    for code_dir in sorted(p for p in region_dir.iterdir() if p.is_dir()):
        if only_code and code_dir.name not in only_code:
            continue
        for f in sorted(code_dir.iterdir()):
            if not f.is_file():
                continue
            m = FNAME_DATE_RE.match(f.name)
            if not m:
                continue
            date = m.group(2)
            if only_date and date != only_date:
                continue
            try:
                datetime.strptime(date, "%Y%m%d")
            except ValueError:
                print("[WARN] 文件名日期非法, 跳过: %s" % f.name)
                continue
            if date > cutoff:                     # 字符串字典序 == 日期数值序
                continue
            # 仓库内相对路径(POSIX 斜杠) —— Contents API 的 path 必须是这种形式;
            # 从常量拼接而非 relative_to, 与传入 src_root 的绝对/相对形态无关
            rel = "%s/%s/%s/%s" % (ARCHIVE_ROOT.as_posix(), REGION_MARKET,
                                   code_dir.name, f.name)
            tasks.append((f, rel, date))
    return tasks


def main() -> None:
    enableLogTimestamps()
    parser = argparse.ArgumentParser(
        description="QuoteGold 归档 .mvsv 跨仓库转存到 acdnx/Distribution(Contents API)")
    parser.add_argument("--days", type=int, default=None,
                        help="转存缓冲天数, 只搬文件名日期 <= 今日-该值 的文件, 默认 4")
    parser.add_argument("--code", action="append", default=None,
                        help="只处理指定品种目录名(可重复), 如 GAP-CMB; 默认全部")
    parser.add_argument("--date", default=None, metavar="YYYYMMDD",
                        help="只处理该日期的文件; 缺省不限(仍受 --days 缓冲约束)")
    parser.add_argument("--limit", type=int, default=None,
                        help="单次最多处理 N 个文件(0=不限)")
    parser.add_argument("--force", action="store_true",
                        help="转存端已存在且内容一致时也重新提交")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将要执行的动作, 不提交")
    args = parser.parse_args()

    days = args.days if args.days is not None else int(
        os.environ.get("QUOTE_GOLD_PUBLISH_DAYS", DEFAULT_DAYS))
    limit = args.limit if args.limit is not None else int(
        os.environ.get("QUOTE_GOLD_PUBLISH_LIMIT", 0))
    only_code = args.code or None
    if only_code is None:
        env_code = os.environ.get("QUOTE_GOLD_PUBLISH_CODE", "").strip()
        if env_code:
            only_code = [c.strip() for c in env_code.split(",") if c.strip()]
    only_date = args.date or os.environ.get("QUOTE_GOLD_PUBLISH_DATE") or None

    token = os.environ.get(TOKEN_ENV, "").strip()
    if not args.dry_run and not token:
        print("[ERROR] 未设置环境变量 %s, 无法经 Contents API 提交(可加 --dry-run 先干跑)"
              % TOKEN_ENV)
        sys.exit(2)

    now_bjt = datetime.now(BJT)
    cutoff = (now_bjt.date() - timedelta(days=days)).strftime("%Y%m%d")

    src_root = repo_root() / ARCHIVE_ROOT
    tasks = collect_files(src_root, cutoff, only_code, only_date)

    print("=" * 72)
    print("QuoteGold 归档转存  %s" % ("[DRY-RUN]" if args.dry_run else ""))
    print("源端     : 本仓库 @ 检出分支(本地工作区)")
    print("转存端   : %s/%s @ %s" % (TARGET_OWNER, TARGET_REPO, TARGET_BRANCH))
    print("源根目录 : %s" % src_root)
    print("转存资格 : 文件名日期 <= %s(今日 %s - %d 天)"
          % (cutoff, now_bjt.strftime("%Y%m%d"), days))
    print("品种过滤 : %s" % (", ".join(only_code) if only_code else "全部"))
    print("日期过滤 : %s" % (only_date or "不限"))
    print("命中文件 : %d 个" % len(tasks))
    print("=" * 72)

    if not tasks:
        print("没有满足转存资格的文件, 无事可做")
        return

    picked = tasks
    if limit and limit > 0 and len(tasks) > limit:
        picked = tasks[:limit]
        print("本次按 limit=%d 只处理前 %d 个, 其余 %d 个留待下次"
              % (limit, len(picked), len(tasks) - limit))

    published, skipped, failed = [], [], []
    for i, (path, rel, date) in enumerate(picked, 1):
        print("\n[%d/%d] %s" % (i, len(picked), rel))
        # 幂等: 本地 blob sha vs 转存端同路径文件 sha(不下载远端内容)
        need = True
        if not args.force and token:
            local_sha = local_blob_sha(path)
            remote_sha = _get_file_sha(DEFAULT_API_BASE, TARGET_OWNER, TARGET_REPO,
                                       rel, TARGET_BRANCH, token, 30)
            if local_sha and remote_sha and local_sha == remote_sha:
                need = False
                print("    转存端 %s/%s 已有相同内容(sha 相同), 跳过"
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
            print("    [DRY-RUN] %s" % ("将提交" if need else "跳过提交"))
            continue
        if not need:
            continue

        res = commit_content_file(
            rel, str(path), branch=TARGET_BRANCH, owner=TARGET_OWNER,
            repo=TARGET_REPO,
            commit_msg="[QuoteGold] publish %s -> %s/%s@%s"
                       % (rel, TARGET_OWNER, TARGET_REPO, TARGET_BRANCH))
        if res["success"]:
            print("    已提交(HTTP %s) -> %s/%s@%s"
                  % (res["http_status"], TARGET_OWNER, TARGET_REPO, TARGET_BRANCH))
            published.append(rel)
        else:
            print("    [ERROR] 提交失败, 留待下次重跑: %s" % res["message"])
            failed.append("%s: %s" % (rel, res["message"]))

    print("\n======== 转存结束 %s ========" % ("(dry-run)" if args.dry_run else ""))
    if not args.dry_run:
        print("本次处理: %d 个" % len(picked))
        print("已提交  : %d 个 -> %s/%s@%s"
              % (len(published), TARGET_OWNER, TARGET_REPO, TARGET_BRANCH))
        print("内容一致跳过: %d 个" % len(skipped))
        print("失败    : %d 个" % len(failed))
        for item in failed:
            print("    - %s" % item)
        if failed:
            sys.exit(1)
    print("全部完成")


if __name__ == "__main__":
    main()
