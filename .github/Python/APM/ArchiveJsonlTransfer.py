#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ArchiveJsonlTransfer.py —— APM 归档 JSONL 跨仓库转存
================================================================================

把**本仓库**(ACANX/Distribution, 采集归档端) apm 分支
Archive/Meta/WebMMCP/APM/ 下已归档的 .jsonl, 经 GitHub Contents API 转存到
**另一个仓库** acdnx/Distribution(长期归档端)的 apm 分支**同路径**下:

    本仓库  Archive/Meta/WebMMCP/APM/{API,Page}/APM_{type}_{Code}_DAY_ACANX_{yyyyMMdd}.jsonl
    转存端  Archive/Meta/WebMMCP/APM/{API,Page}/APM_{type}_{Code}_DAY_ACANX_{yyyyMMdd}.jsonl

    文件名与目录结构原样保留, 转存端落点 = 源路径。

与 quote-gold.GoldQuoteArchiveTransfer / news.PublishNewsFlashJsonl 同一套跨仓库
约定(重要):
    * ACANX/Distribution 与 acdnx/Distribution 是两个真实存在的**独立仓库**, 不是
      同一仓库的大小写变体。转存目标必须显式写死(脚本内置默认 + 工作流 env 注入),
      **绝不从 .git 解析** —— 一旦误用 .git 解析结果, 提交会写回本仓库, 与"转到
      另一个仓库"的意图正好相反。
    * GIT_COMMIT_TOKEN 需同时具备两个仓库的权限: 读本仓库(checkout 检出用),
      写 acdnx/Distribution 的 apm 分支(Contents API 提交用)。
    * 本脚本只从本仓库读数据、只写 acdnx, 不与本仓库其它数据推送产生 push 竞争。
    * 源文件不删除(由本仓库归档侧的产出自然覆盖), 转存端多余的文件也不删。

幂等
----
    转存前算源文件的 git blob sha, 与转存端同路径文件的 sha 比较(**不下载**远端
    内容): 一致即视为"已送达", 跳过; 不一致或不存在才提交。因此可以放心定期重跑,
    不会产生内容不变的空提交。

    注意: 源文件在本仓库会被归档工作流当日重刷(当天那份还在增长), 此时转存端会
    跟着更新 —— 这是期望行为, 转存端永远与源端最终版一致。

提交方式
--------
    复用同仓 .github/Python/GitHubCommitContent.py, 经 GitHub Contents API 提交到
    转存端仓库的 apm 分支(不依赖本地 git 提交, 因此不会与本仓库其它定时任务的
    数据推送产生 push 竞争)。

配置来源(优先级从高到低)
------------------------
    1. 命令行参数(--date / --limit / --force / --dry-run)
    2. 环境变量 ARCHIVE_TRANSFER_TARGET_OWNER / _TARGET_REPO / _BRANCH / _DATE / _LIMIT
    3. 脚本内置默认(acdnx / Distribution / apm)

用法
----
    python3 .github/Python/APM/ArchiveJsonlTransfer.py --dry-run
    python3 .github/Python/APM/ArchiveJsonlTransfer.py
    python3 .github/Python/APM/ArchiveJsonlTransfer.py --date 20260618

依赖: 仅 Python 3 标准库 + 同仓 .github/Python/GitHubCommitContent.py
"""

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

# 同目录的上一级(.github/Python/)放着公共 Contents API 封装, 加进搜索路径后 import
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
from GitHubCommitContent import DEFAULT_API_BASE, _get_file_sha, commit_content_file  # noqa: E402

# ── 转存端(写死, 绝不从 .git 解析 —— 两个仓库是彼此独立的真实仓库) ──────────
TARGET_OWNER = os.environ.get("ARCHIVE_TRANSFER_TARGET_OWNER", "acdnx")
TARGET_REPO = os.environ.get("ARCHIVE_TRANSFER_TARGET_REPO", "Distribution")
TARGET_BRANCH = os.environ.get("ARCHIVE_TRANSFER_BRANCH", "apm")
TOKEN_ENV = "GIT_COMMIT_TOKEN"

# 源目录(仓库内相对路径); 文件名末尾的 yyyyMMdd 作为日期
ARCHIVE_ROOT = Path("Archive") / "Meta" / "WebMMCP" / "APM"
FNAME_DATE_RE = re.compile(r"_(\d{8})\.jsonl$")

TAG = "APM"


def repo_root() -> Path:
    """仓库根目录。本文件位于 <根>/.github/Python/APM/, 上溯三级即根。"""
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


def collect_files(src_root: Path, only_date=None):
    """递归收集归档根下所有 .jsonl。

    文件名末尾应为 _yyyyMMdd.jsonl; 解析不出合法日期的文件跳过并告警(宁可不搬
    也不误搬)。返回按 (日期, 路径) 升序的 [(绝对路径, 仓库内相对路径, 日期)]。
    """
    if not src_root.is_dir():
        print("[WARN] 源目录不存在: %s" % src_root)
        return []
    tasks = []
    for f in sorted(src_root.rglob("*.jsonl")):
        if not f.is_file():
            continue
        m = FNAME_DATE_RE.search(f.name)
        if not m:
            print("[WARN] 文件名末尾不是 _yyyyMMdd.jsonl, 跳过: %s" % f.name)
            continue
        date = m.group(1)
        if only_date and date != only_date:
            continue
        rel = (ARCHIVE_ROOT / f.relative_to(src_root)).as_posix()
        tasks.append((f, rel, date))
    tasks.sort(key=lambda t: (t[2], t[1]))
    return tasks


def main() -> None:
    """入口: 扫描归档目录, 逐文件与转存端比对 blob sha, 有差异才经 Contents API 提交"""
    parser = argparse.ArgumentParser(
        description="%s 归档 JSONL 跨仓库转存到 %s/%s(Contents API)"
                    % (TAG, TARGET_OWNER, TARGET_REPO))
    parser.add_argument("--date", default=None, metavar="YYYYMMDD",
                        help="只转存该日期的文件; 缺省不限")
    parser.add_argument("--limit", type=int, default=None,
                        help="单次最多处理 N 个文件(0=不限)")
    parser.add_argument("--force", action="store_true",
                        help="转存端已存在且内容一致时也重新提交")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将要执行的动作, 不提交")
    args = parser.parse_args()

    only_date = args.date or os.environ.get("ARCHIVE_TRANSFER_DATE") or None
    limit = args.limit if args.limit is not None else int(
        os.environ.get("ARCHIVE_TRANSFER_LIMIT", 0))

    token = os.environ.get(TOKEN_ENV, "").strip()
    if not args.dry_run and not token:
        print("[ERROR] 未设置环境变量 %s, 无法经 Contents API 提交(可加 --dry-run 先干跑)"
              % TOKEN_ENV)
        sys.exit(2)

    src_root = repo_root() / ARCHIVE_ROOT
    tasks = collect_files(src_root, only_date)

    print("=" * 72)
    print("%s 归档 JSONL 跨仓库转存  %s" % (TAG, "[DRY-RUN]" if args.dry_run else ""))
    print("源端     : 本仓库 @ 检出分支(本地工作区)")
    print("转存端   : %s/%s @ %s" % (TARGET_OWNER, TARGET_REPO, TARGET_BRANCH))
    print("源根目录 : %s" % src_root)
    print("日期过滤 : %s" % (only_date or "不限"))
    print("命中文件 : %d 个" % len(tasks))
    print("=" * 72)
    if not tasks:
        print("没有可转存的归档文件, 无事可做")
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
            commit_msg="[%s] publish %s -> %s/%s@%s"
                       % (TAG, rel, TARGET_OWNER, TARGET_REPO, TARGET_BRANCH))
        if res["success"]:
            print("    已提交(HTTP %s) -> %s/%s@%s"
                  % (res["http_status"], TARGET_OWNER, TARGET_REPO, TARGET_BRANCH))
            published.append(rel)
        else:
            print("    [ERROR] 提交失败, 留待下次重跑: %s" % res["message"])
            failed.append("%s: %s" % (rel, res["message"]))

    print("\n======== 转存结束 %s ========" % ("(dry-run)" if args.dry_run else ""))
    if not args.dry_run:
        print("本次处理    : %d 个" % len(picked))
        print("已提交      : %d 个 -> %s/%s@%s"
              % (len(published), TARGET_OWNER, TARGET_REPO, TARGET_BRANCH))
        print("内容一致跳过: %d 个" % len(skipped))
        print("失败        : %d 个" % len(failed))
        for item in failed:
            print("    - %s" % item)
        if failed:
            sys.exit(1)
    print("全部完成")


if __name__ == "__main__":
    main()
