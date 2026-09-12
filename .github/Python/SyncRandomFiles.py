#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SyncRandomFiles —— 跨仓库随机行情文件同步（供 GitHub Actions 手动工作流调用）
========================================================================================

一、工具定位
----------------------------------------------------------------------------------------
遍历本仓库当前检出分支工作区中 Archive/Finv/SecuQuote/ 下的 .json / .mvsv / .log
文件（排除 "." 开头的隐藏目录），仅保留"git 最后一次修改时间足够陈旧"的文件
（.json / .mvsv 需在 50 天以前，.log 需在 35 天以前），随机抽取其中 15 个
（不足 15 个按实际数量全取），重命名为规范格式的行情文件后，复用同目录
GitHubCommitContent.py 提供的 commit_content_file 方法，通过 GitHub Contents API
提交到 acdnx/Distribution 仓库的同名分支下，实现免 clone 的跨仓库随机复制，
用于新仓库的数据测试。

二、路径 / 文件名转换规则
----------------------------------------------------------------------------------------
源文件（模式）：
    Archive/Finv/SecuQuote/{Freq}/{Code}/{Code}_{Period}_{Date}.{ext}
目标文件（模式）：
    Data/Finv/SecuQuote/FT/{Freq}/{Code}/{Region}_{Market}_{Code}_{Period}_FT_{Date}.{ext}

其中：
    - {Freq}  ：源路径中 SecuQuote 的下一级目录（Day / Min 等），原样保留；
    - {Code}  ：源文件名首段（证券唯一标识）；
    - {Period}：源文件名中段（Min / Day 等），原样保留；
    - {Date}  ：源文件名尾段（yyyyMMdd，须为 8 位数字）；
    - {Region}/{Market}：以 {Code} 查同目录 SecuMetaMapping.jsonl 得到；
    - {ext}   ：源扩展名原样保留（.mvsv / .json / .log）。
示例：
    Archive/Finv/SecuQuote/Day/000001/000001_Min_20260609.mvsv
    → Data/Finv/SecuQuote/FT/Day/000001/CN_SH_000001_Min_FT_20260609.mvsv

不满足以下条件的文件一律跳过（不入库，仅日志记录）：
    - 相对路径不是 Archive/Finv/SecuQuote/{Freq}/{Code}/{文件名} 四级结构；
    - 文件名不是 {Code}_{Period}_{Date}.{ext} 三段结构或 {Date} 非 8 位数字；
    - {Code} 在 SecuMetaMapping.jsonl 中无记录（拿不到 Region / Market）；
    - 不在 Archive/Finv/SecuQuote/ 目录下的文件（本次只转存行情文件）。

三、SecuMetaMapping.jsonl 格式（与本脚本同目录）
----------------------------------------------------------------------------------------
JSON Lines，一行一个 JSON 对象，字段：Code / Region / Market，例如：
    {"Code": "000001", "Region": "CN", "Market": "SH"}
文件缺失或某行非法 JSON 时：文件缺失直接报错退出；单行非法仅告警跳过该行。

四、分支来源（currBranch 解析优先级）
----------------------------------------------------------------------------------------
    1) 命令行第 1 个参数；
    2) 环境变量 CURR_BRANCH（工作流中由 workflow_dispatch 输入 currBranch 注入，
       留空时回退为触发工作流时所选分支）；
    3) 环境变量 GITHUB_REF_NAME（Actions 检出 ref 后自动提供）；
    均缺失时报错退出（无法确定目标分支）。

五、调用方式（工作流内）
----------------------------------------------------------------------------------------
    GIT_COMMIT_TOKEN=*** CURR_BRANCH=quote python3 .github/Python/SyncRandomFiles.py

    - 令牌仅经环境变量 GIT_COMMIT_TOKEN 注入（需具备 acdnx/Distribution 的
      contents:write 权限），严禁写入源码或日志；
    - 目标 owner/repo 显式指定为 acdnx/Distribution（与 Commit.json 登记一致，
      显式传参避免 .git 解析回退到源仓库）；
    - 目标分支必须已在 acdnx/Distribution 远端存在，否则提交返回 422/404。

六、退出码
----------------------------------------------------------------------------------------
    0 = 全部提交成功；1 = 未找到可转存文件，或存在提交失败的文件，
        或 SecuMetaMapping.jsonl 缺失 / 无法确定分支。
    单文件提交失败不中断整体流程，最终以汇总结果决定退出码。

【环境要求】Python 3.8+，仅标准库；可直连 api.github.com（443）。
"""

import json
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

# 只转存该目录前缀下的行情文件（其余文件跳过）
SECU_QUOTE_PREFIX = "Archive/Finv/SecuQuote/"

# 证券元数据映射文件（与本脚本同目录；一行一个 {Code, Region, Market} JSON）
SECU_META_MAPPING_FILE = "SecuMetaMapping.jsonl"

# 源文件名模式 {Code}_{Period}_{Date}.{ext} 的段数与日期位数要求
SRC_NAME_SEGMENTS = 3
SRC_DATE_DIGITS = 8


def script_dir():
    """返回本脚本所在目录（绝对路径）"""
    return os.path.dirname(os.path.abspath(__file__))


def repo_root():
    """返回仓库根目录（绝对路径）

    本脚本固定位于 <仓库根>/.github/Python/ 下，向上两级即为仓库根。

    :return: 仓库根目录绝对路径
    """
    return os.path.dirname(os.path.dirname(script_dir()))


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


def load_secu_meta_mapping():
    """加载同目录 SecuMetaMapping.jsonl（一行一个 {Code, Region, Market} JSON）

    :return: dict {Code: (Region, Market)}；文件缺失返回 None（调用方报错退出），
             单行非法 / 缺字段仅 stderr 告警并跳过该行
    """
    path = os.path.join(script_dir(), SECU_META_MAPPING_FILE)
    if not os.path.isfile(path):
        print("❌ 证券元数据映射文件缺失: %s" % path)
        return None
    mapping = {}
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError as e:
                print("⚠️ %s 第 %d 行非合法 JSON，已跳过: %s"
                      % (SECU_META_MAPPING_FILE, line_no, e))
                continue
            if not isinstance(data, dict):
                print("⚠️ %s 第 %d 行顶层应为 JSON 对象，已跳过" % (
                    SECU_META_MAPPING_FILE, line_no))
                continue
            code = str(data.get("Code", "")).strip()
            region = str(data.get("Region", "")).strip()
            market = str(data.get("Market", "")).strip()
            if not (code and region and market):
                print("⚠️ %s 第 %d 行缺少 Code/Region/Market 字段，已跳过" % (
                    SECU_META_MAPPING_FILE, line_no))
                continue
            mapping[code] = (region, market)
    print("已加载证券元数据映射: %d 条" % len(mapping))
    return mapping


def transform_secu_path(rel_path, mapping):
    """把源行情文件路径转换为规范命名的目标路径（见模块 docstring 第二节）

    :param rel_path: 源文件相对路径（"/" 分隔）
    :param mapping: 证券元数据映射 dict（load_secu_meta_mapping 的返回值）
    :return: (目标相对路径, None)；不可转存时返回 (None, 跳过原因)
    """
    parts = rel_path.split("/")
    # 结构: Archive/Finv/SecuQuote/{Freq}/{Code}/{文件名} 共 6 段
    if not rel_path.startswith(SECU_QUOTE_PREFIX) or len(parts) != 6:
        return None, "不在 %s{Freq}/{Code}/ 目录结构下" % SECU_QUOTE_PREFIX
    freq, dir_code, filename = parts[3], parts[4], parts[5]
    stem, ext = os.path.splitext(filename)
    ext = ext.lower()
    if ext not in TARGET_SUFFIXES:
        return None, "扩展名 %s 不在支持范围" % ext
    segs = stem.split("_")
    if len(segs) != SRC_NAME_SEGMENTS:
        return None, "文件名不是 {Code}_{Period}_{Date} 三段结构"
    code, period, date = segs
    if len(date) != SRC_DATE_DIGITS or not date.isdigit():
        return None, "文件名尾段 %s 不是 %d 位日期" % (date, SRC_DATE_DIGITS)
    meta = mapping.get(code)
    if meta is None:
        return None, "Code %s 在 %s 中无记录" % (code, SECU_META_MAPPING_FILE)
    region, market = meta
    target = "Data/Finv/SecuQuote/FT/%s/%s/%s_%s_%s_%s_FT_%s%s" % (
        freq, dir_code, region, market, code, period, date, ext)
    return target, None


def resolve_branch():
    """解析当前处理的分支名（见模块 docstring 第四节的优先级）

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
    """主流程：遍历 → 陈旧过滤 → 路径转换映射 → 随机抽样 → 逐个跨仓库提交 → 汇总"""
    branch = resolve_branch()
    if not branch:
        print("❌ 未能确定分支名：请通过命令行参数、CURR_BRANCH 或 "
              "GITHUB_REF_NAME 提供")
        return 1
    print("当前处理分支 (currBranch) = %s" % branch)
    print("目标仓库 = %s/%s" % (TARGET_OWNER, TARGET_REPO))

    mapping = load_secu_meta_mapping()
    if mapping is None:
        return 1

    root = repo_root()
    all_files = collect_target_files(root)
    print("遍历完成：共发现 %d 个 .json / .mvsv / .log 文件（已排除隐藏目录）" % len(all_files))
    if not all_files:
        print("❌ 当前分支下未找到任何 .json / .mvsv / .log 文件，无事可做")
        return 1

    stale_files, stale_excluded = filter_stale_files(root, all_files)
    print("陈旧过滤：.json/.mvsv 需 %d 天前、.log 需 %d 天前 → 满足 %d 个（排除 %d 个）"
          % (STALE_DAYS_DEFAULT, STALE_DAYS_OVERRIDES[".log"],
             len(stale_files), stale_excluded))

    # 路径转换映射：仅 SecuQuote 下能解析出 Code 并查到 Region/Market 的文件可转存
    eligible = []  # [(源相对路径, 目标相对路径)]
    skip_reasons = {}
    for rel_path in stale_files:
        target, reason = transform_secu_path(rel_path, mapping)
        if target is None:
            skip_reasons[rel_path] = reason
        else:
            eligible.append((rel_path, target))
    for rel_path, reason in skip_reasons.items():
        print("⏭️ 跳过 %s：%s" % (rel_path, reason))
    print("可转存文件：%d 个（跳过 %d 个）" % (len(eligible), len(skip_reasons)))
    if not eligible:
        print("❌ 过滤后没有可转存的行情文件，无事可做")
        return 1

    picked = random.sample(eligible, min(PICK_COUNT, len(eligible)))
    print("随机抽取 %d 个文件：" % len(picked))
    for src, target in picked:
        print("  - %s" % src)
        print("    → %s" % target)

    ok_count = 0
    fail_list = []
    for src_path, target_path in picked:
        local_file = os.path.join(root, src_path.replace("/", os.sep))
        # 提交说明带目标路径与来源分支，便于在目标仓库追溯
        result = commit_content_file(
            target_path, local_file,
            branch=branch,
            owner=TARGET_OWNER, repo=TARGET_REPO,
            commit_msg="[SyncRandomFiles] %s from %s" % (target_path, branch),
        )
        if result["success"]:
            ok_count += 1
            print("✅ %s（HTTP %s）" % (target_path, result["http_status"]))
        else:
            fail_list.append((target_path, result["message"]))
            print("❌ %s：%s" % (target_path, result["message"]))

    print("汇总：成功 %d / 失败 %d / 共 %d" % (ok_count, len(fail_list), len(picked)))
    return 0 if not fail_list else 1


if __name__ == "__main__":
    sys.exit(main())
