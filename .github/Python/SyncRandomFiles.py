#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SyncRandomFiles —— 跨仓库随机行情文件同步（供 GitHub Actions 手动工作流调用）
========================================================================================

一、工具定位
----------------------------------------------------------------------------------------
遍历本仓库当前检出分支工作区中 Archive/Finv/SecuQuote/ 下的 .json / .mvsv / .log
文件（排除 "." 开头的隐藏目录），仅保留"git 最后一次修改时间足够陈旧"的文件
（.json / .mvsv / .log 均需在 35 天以前），随机抽取其中 25 个
（不足 25 个按实际数量全取），重命名为规范格式的行情文件后，复用同目录
GitHubCommitContent.py 提供的 commit_content / commit_content_file 方法，通过
GitHub Contents API 提交到 acdnx/Distribution 仓库的同名分支下，实现免 clone 的
跨仓库随机复制，用于新仓库的数据测试。其中 .mvsv 文件在发送前会把映射到的
Region / Market 写入文件头 SecuCode 字段之后（已有旧值则覆盖）。

二、路径 / 文件名转换规则
----------------------------------------------------------------------------------------
源文件（模式）：
    Archive/Finv/SecuQuote/{Freq}/{Code}/{Code}_{Period}_{Date}.{ext}
目标文件（模式）：
    Data/Finv/SecuQuote/FT/{Freq}/{Region}_{Market}/{Code}/
        {Region}_{Market}_{Code}_{Period}_FT_{Date}.{ext}

其中：
    - {Freq}  ：源路径中 SecuQuote 的下一级目录（Day / Min 等），原样保留；
    - {Code}  ：源文件名首段（证券唯一标识）；
    - {Period}：源文件名中段（Min / Day 等），原样保留；
    - {Date}  ：源文件名尾段（yyyyMMdd，须为 8 位数字）；
    - {Region}/{Market}：以 {Code} 查同目录 SecuMetaMapping.jsonl 得到，
      并同时用于二级目录（{Region}_{Market}）与文件名前缀；
    - {ext}   ：源扩展名原样保留（.mvsv / .json / .log）。
示例：
    Archive/Finv/SecuQuote/Day/000001/000001_Min_20260609.mvsv
    → Data/Finv/SecuQuote/FT/Day/CN_SH/000001/CN_SH_000001_Min_FT_20260609.mvsv

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
from GitHubCommitContent import commit_content, commit_content_file

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 目标仓库身份（跨仓库复制的落点；与同目录 Commit.json 登记值保持一致）
TARGET_OWNER = "acdnx"
TARGET_REPO = "Distribution"

# 随机抽取的文件数量上限（不足按实际数量全取）
PICK_COUNT = 25

# 目标文件扩展名集合（统一按小写比较；.json / .mvsv / .log 均支持）
TARGET_SUFFIXES = (".json", ".mvsv", ".log")

# "陈旧"阈值（天）：仅 git 最后一次修改时间早于 该天数以前 的文件才会进入抽样；
# 未在 OVERRIDES 中登记的扩展名走默认值
STALE_DAYS_DEFAULT = 35
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
    :return: 阈值天数；.log 为 35，其余（.json / .mvsv）为默认 35
    """
    suffix = os.path.splitext(rel_path)[1].lower()
    return STALE_DAYS_OVERRIDES.get(suffix, STALE_DAYS_DEFAULT)


def filter_stale_files(root, files):
    """过滤出 git 最后修改时间足够陈旧的文件（阈值按扩展名区分：
    .json / .mvsv / .log 均为 35 天）

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
    :return: (目标相对路径, (Region, Market), None)；
             不可转存时返回 (None, None, 跳过原因)
    """
    parts = rel_path.split("/")
    # 结构: Archive/Finv/SecuQuote/{Freq}/{Code}/{文件名} 共 6 段
    if not rel_path.startswith(SECU_QUOTE_PREFIX) or len(parts) != 6:
        return None, None, "不在 %s{Freq}/{Code}/ 目录结构下" % SECU_QUOTE_PREFIX
    freq, dir_code, filename = parts[3], parts[4], parts[5]
    stem, ext = os.path.splitext(filename)
    ext = ext.lower()
    if ext not in TARGET_SUFFIXES:
        return None, None, "扩展名 %s 不在支持范围" % ext
    segs = stem.split("_")
    if len(segs) != SRC_NAME_SEGMENTS:
        return None, None, "文件名不是 {Code}_{Period}_{Date} 三段结构"
    code, period, date = segs
    if len(date) != SRC_DATE_DIGITS or not date.isdigit():
        return None, None, "文件名尾段 %s 不是 %d 位日期" % (date, SRC_DATE_DIGITS)
    meta = mapping.get(code)
    if meta is None:
        return None, None, "Code %s 在 %s 中无记录" % (code, SECU_META_MAPPING_FILE)
    region, market = meta
    target = "Data/Finv/SecuQuote/FT/%s/%s_%s/%s/%s_%s_%s_%s_FT_%s%s" % (
        freq, region, market, dir_code, region, market, code, period, date, ext)
    return target, (region, market), None


def patch_mvsv_header(content, region, market):
    """在 mvsv 文件头的 SecuCode 之后写入 Region / Market（已有则覆盖）

    中英文两段元数据对称处理：
        CN: # 证券代码 之后插入 # 地区（新增/覆盖），# 市场 覆盖为映射值
        EN: # SecuCode  之后插入 # Region（新增/覆盖），# Market 覆盖为映射值
    最终顺序固定为 锚字段 → Region → Market；值不加引号（与既有 # Market : cny
    风格一致）；仅在内存中修改字符串，不改动本地文件、不影响数据区。

    :param content: mvsv 文件完整文本（UTF-8 读取）
    :param region: 映射到的 Region（如 "CN"）
    :param market: 映射到的 Market（如 "SH"）
    :return: 修改后的完整文本；文件无元数据头（无任何 "#" 行）时原样返回
    """
    lines = content.splitlines(keepends=True)

    # 元数据头块 = 首个非 "#" 行 / 空行之前的连续 "#" 行
    header_end = 0
    for i, line in enumerate(lines):
        stripped = line.lstrip("\ufeff").strip()
        if stripped.startswith("#"):
            header_end = i + 1
        else:
            break
    if header_end == 0:
        return content  # 无元数据头，无从注入，原样返回

    def key_of(line):
        """解析 "# key : value" 行的 key；非键值形态返回 None"""
        body = line.lstrip("\ufeff").strip()
        if not body.startswith("#"):
            return None
        body = body[1:].strip()
        if ":" not in body:
            return None
        return body.split(":", 1)[0].strip()

    def eol_of(line):
        """保留原行行尾（\r\n / \n / 无）"""
        if line.endswith("\r\n"):
            return "\r\n"
        if line.endswith("\n"):
            return "\n"
        return ""

    def header_end_now():
        """实时计算元数据头块边界（注入/删除会改变行数，不能缓存）"""
        end = 0
        for i, line in enumerate(lines):
            if line.lstrip("\ufeff").strip().startswith("#"):
                end = i + 1
            else:
                break
        return end

    def patch_section(anchor_key, region_key, market_key):
        """对单语种段做 Region/Market 注入（原地修改 lines）

        已有 region/market 行 → 删除后按规范位置重插，保证覆盖且顺序稳定；
        无锚字段行时退化为插入到文件头块末尾。
        """
        region_line = "# %s : %s" % (region_key, region)
        market_line = "# %s : %s" % (market_key, market)
        # 1) 移除已有的 region/market 行（含旧值，等价"覆盖"）；头块边界实时重算
        for i in range(header_end_now() - 1, -1, -1):
            if key_of(lines[i]) in (region_key, market_key):
                del lines[i]
        # 2) 定位锚字段行（头块内从后往前找最后一个匹配，紧贴其元数据段）
        insert_at = header_end_now()
        for i in range(insert_at - 1, -1, -1):
            if key_of(lines[i]) == anchor_key:
                insert_at = i + 1
                break
        eol = eol_of(lines[insert_at - 1]) if insert_at > 0 else "\n"
        lines[insert_at:insert_at] = [region_line + eol, market_line + eol]

    patch_section("证券代码", "地区", "市场")
    patch_section("SecuCode", "Region", "Market")
    return "".join(lines)


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
          % (STALE_DAYS_DEFAULT, STALE_DAYS_OVERRIDES.get(".log", STALE_DAYS_DEFAULT),
             len(stale_files), stale_excluded))

    # 路径转换映射：仅 SecuQuote 下能解析出 Code 并查到 Region/Market 的文件可转存
    eligible = []  # [(源相对路径, 目标相对路径, (Region, Market))]
    skip_reasons = {}
    for rel_path in stale_files:
        target, meta, reason = transform_secu_path(rel_path, mapping)
        if target is None:
            skip_reasons[rel_path] = reason
        else:
            eligible.append((rel_path, target, meta))
    for rel_path, reason in skip_reasons.items():
        print("⏭️ 跳过 %s：%s" % (rel_path, reason))
    print("可转存文件：%d 个（跳过 %d 个）" % (len(eligible), len(skip_reasons)))
    if not eligible:
        print("❌ 过滤后没有可转存的行情文件，无事可做")
        return 1

    picked = random.sample(eligible, min(PICK_COUNT, len(eligible)))
    print("随机抽取 %d 个文件：" % len(picked))
    for src, target, _meta in picked:
        print("  - %s" % src)
        print("    → %s" % target)

    ok_count = 0
    fail_list = []
    for src_path, target_path, (region, market) in picked:
        local_file = os.path.join(root, src_path.replace("/", os.sep))
        # 提交说明带目标路径与来源分支，便于在目标仓库追溯
        commit_msg = "[SyncRandomFiles] %s from %s" % (target_path, branch)
        if src_path.lower().endswith(".mvsv"):
            # .mvsv：读入后在文件头 SecuCode 之后注入 Region/Market（覆盖旧值）再提交
            try:
                with open(local_file, "r", encoding="utf-8", newline="") as f:
                    content = f.read()
            except OSError as e:
                fail_list.append((target_path, "读取本地文件失败: %s" % e))
                print("❌ %s：读取本地文件失败: %s" % (target_path, e))
                continue
            patched = patch_mvsv_header(content, region, market)
            result = commit_content(
                target_path, patched,
                branch=branch, owner=TARGET_OWNER, repo=TARGET_REPO,
                commit_msg=commit_msg,
            )
        else:
            result = commit_content_file(
                target_path, local_file,
                branch=branch, owner=TARGET_OWNER, repo=TARGET_REPO,
                commit_msg=commit_msg,
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
