#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SyncRandomFiles —— 跨仓库随机文件同步（供 GitHub Actions 手动工作流调用）
========================================================================================

一、工具定位
----------------------------------------------------------------------------------------
遍历本仓库当前检出分支工作区中的所有 .json / .mvsv / .log 文件（排除 "." 开头
的隐藏目录），仅保留"git 最后一次修改时间足够陈旧"的文件（.json / .mvsv 需在
50 天以前，.log 需在 35 天以前），随机抽取其中
15 个（不足 15 个按实际数量全取），复用同目录
GitHubCommitContent.py 提供的 commit_content_file 方法，通过 GitHub Contents API
将这些文件原样提交到 acdnx/Distribution 仓库的"同名分支 + 同名路径"下，
实现免 clone 的跨仓库随机复制，用于新仓库的数据测试。

二、分支来源（currBranch 解析优先级）
----------------------------------------------------------------------------------------
    1) 命令行第 1 个参数；
    2) 环境变量 CURR_BRANCH（工作流中由 workflow_dispatch 输入 currBranch 注入，
       留空时回退为触发工作流时所选分支）；
    3) 环境变量 GITHUB_REF_NAME（Actions 检出 ref 后自动提供）；
    均缺失时报错退出（无法确定目标分支）。

三、调用方式（工作流内）
----------------------------------------------------------------------------------------
    GIT_COMMIT_TOKEN=*** CURR_BRANCH=quote python3 .github/Python/SyncRandomFiles.py

    - 令牌仅经环境变量 GIT_COMMIT_TOKEN 注入（需具备 acdnx/Distribution 的
      contents:write 权限），严禁写入源码或日志；
    - 目标 owner/repo 显式指定为 acdnx/Distribution（与 Commit.json 登记一致，
      显式传参避免 .git 解析回退到源仓库）；
    - 目标分支必须已在 acdnx/Distribution 远端存在，否则提交返回 422/404。

四、退出码
----------------------------------------------------------------------------------------
    0 = 全部提交成功；1 = 未找到任何 .json / .mvsv / .log 文件，或存在提交失败的文件。
    单文件提交失败不中断整体流程，最终以汇总结果决定退出码。

【环境要求】Python 3.8+，仅标准库；可直连 api.github.com（443）。
"""

import os
import random
import subprocess
import sys
import time

# 同目录纯函数库，直接 import（脚本所在目录自动加入 sys.path）
from GitHubCommitContent import commit_content_file

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 目标仓库身份（跨仓库复制的落点；与同目录 Commit.json 登记值保持一致）
TARGET_OWNER = "acdnx"
TARGET_REPO = "Distribution"

# 随机抽取的文件数量上限（不足按实际数量全取）
PICK_COUNT = 15

# 目标文件扩展名集合（统一按小写比较；.json / .mvsv / .log 均支持）
TARGET_SUFFIXES = (".json", ".mvsv", ".log")

# "陈旧"阈值（天）：仅 git 最后一次修改时间早于 该天数以前 的文件才会进入抽样；
# 未在 OVERRIDES 中登记的扩展名走默认值
STALE_DAYS_DEFAULT = 50
STALE_DAYS_OVERRIDES = {".log": 35}


def repo_root():
    """返回仓库根目录（绝对路径）

    本脚本固定位于 <仓库根>/.github/Python/ 下，向上两级即为仓库根。

    :return: 仓库根目录绝对路径
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(script_dir))


def collect_target_files(root):
    """遍历 root 下的所有 .json / .mvsv / .log 文件（排除 "." 开头的隐藏目录与隐藏文件）

    隐藏目录整体剪枝（不进入其子树），因此 .git / .github 及其它 "." 开头
    目录中的文件一律不会进入清单。

    :param root: 遍历起始目录（仓库根）
    :return: 相对仓库根的文件路径列表（统一使用 "/" 分隔，排序保证结果可复现）
    """
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        # 原地剪枝：跳过 "." 开头的隐藏目录（含 .git / .github 等）
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue  # 顺带排除 "." 开头的隐藏文件
            if not name.lower().endswith(TARGET_SUFFIXES):
                continue
            abs_path = os.path.join(dirpath, name)
            rel_path = os.path.relpath(abs_path, root).replace(os.sep, "/")
            files.append(rel_path)
    return sorted(files)


def get_last_commit_epoch(root, rel_path):
    """查询文件在 git 仓库中的最后一次修改时间（Unix 秒；取该路径最近一次提交）

    使用 git log 的提交时间而非文件系统 mtime——检出/复制操作会刷新 mtime，
    只有 git 提交历史才能反映"仓库中最后修改时间"。

    :param root: 仓库根目录（git -C 的工作目录）
    :param rel_path: 相对仓库根的文件路径（"/" 分隔）
    :return: 最近一次提交的 Unix 时间戳（int）；文件未被 git 跟踪或查询失败
             返回 None（调用方按"不满足陈旧条件"排除）
    """
    try:
        proc = subprocess.run(
            ["git", "-C", root, "log", "-1", "--format=%ct", "--", rel_path],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print("⚠️ 查询 git 最后修改时间失败，排除文件 %s：%s" % (rel_path, e))
        return None
    value = (proc.stdout or "").strip()
    if proc.returncode != 0 or not value:
        return None  # 未被 git 跟踪（无提交历史）→ 无法判定，排除
    try:
        return int(value)
    except ValueError:
        return None


def stale_days_for(rel_path):
    """按文件扩展名取对应的"陈旧"阈值（天）

    :param rel_path: 相对仓库根的文件路径（"/" 分隔）
    :return: 阈值天数；.log 为 35，其余（.json / .mvsv）为默认 50
    """
    suffix = os.path.splitext(rel_path)[1].lower()
    return STALE_DAYS_OVERRIDES.get(suffix, STALE_DAYS_DEFAULT)


def filter_stale_files(root, files):
    """过滤出 git 最后修改时间足够陈旧的文件（阈值按扩展名区分：
    .json / .mvsv 为 50 天，.log 为 35 天）

    :param root: 仓库根目录
    :param files: 候选文件相对路径列表（"/" 分隔）
    :return: (陈旧文件列表, 被排除的文件数)
    """
    now = time.time()
    stale = []
    excluded = 0
    for rel_path in files:
        cutoff = now - stale_days_for(rel_path) * 86400
        epoch = get_last_commit_epoch(root, rel_path)
        if epoch is not None and epoch <= cutoff:
            stale.append(rel_path)
        else:
            excluded += 1
    return stale, excluded


def resolve_branch():
    """解析当前处理的分支名（见模块 docstring 第二节的优先级）

    :return: 分支名字符串；无法解析时返回 None
    """
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    for env_key in ("CURR_BRANCH", "GITHUB_REF_NAME"):
        value = os.environ.get(env_key, "").strip()
        if value:
            return value
    return None


def main():
    """主流程：遍历 → 随机抽样 → 逐个跨仓库提交 → 汇总退出"""
    branch = resolve_branch()
    if not branch:
        print("❌ 未能确定分支名：请通过命令行参数、CURR_BRANCH 或 "
              "GITHUB_REF_NAME 提供")
        return 1
    print("当前处理分支 (currBranch) = %s" % branch)
    print("目标仓库 = %s/%s（同名分支同名路径）" % (TARGET_OWNER, TARGET_REPO))

    root = repo_root()
    all_files = collect_target_files(root)
    print("遍历完成：共发现 %d 个 .json / .mvsv / .log 文件（已排除隐藏目录）" % len(all_files))
    if not all_files:
        print("❌ 当前分支下未找到任何 .json / .mvsv / .log 文件，无事可做")
        return 1

    stale_files, excluded = filter_stale_files(root, all_files)
    print("陈旧过滤：.json/.mvsv 需 %d 天前、.log 需 %d 天前 → 满足 %d 个（排除 %d 个）"
          % (STALE_DAYS_DEFAULT, STALE_DAYS_OVERRIDES[".log"],
             len(stale_files), excluded))
    if not stale_files:
        print("❌ 过滤后没有满足陈旧条件的文件，无事可做")
        return 1

    picked = random.sample(stale_files, min(PICK_COUNT, len(stale_files)))
    print("随机抽取 %d 个文件：" % len(picked))
    for path in picked:
        print("  - %s" % path)

    ok_count = 0
    fail_list = []
    for rel_path in picked:
        local_file = os.path.join(root, rel_path.replace("/", os.sep))
        # 提交说明带来源与文件名，便于在目标仓库追溯
        result = commit_content_file(
            rel_path, local_file,
            branch=branch,
            owner=TARGET_OWNER, repo=TARGET_REPO,
            commit_msg="[SyncRandomFiles] %s from %s" % (rel_path, branch),
        )
        if result["success"]:
            ok_count += 1
            print("✅ %s（HTTP %s）" % (rel_path, result["http_status"]))
        else:
            fail_list.append((rel_path, result["message"]))
            print("❌ %s：%s" % (rel_path, result["message"]))

    print("汇总：成功 %d / 失败 %d / 共 %d" % (ok_count, len(fail_list), len(picked)))
    return 0 if not fail_list else 1


if __name__ == "__main__":
    sys.exit(main())
