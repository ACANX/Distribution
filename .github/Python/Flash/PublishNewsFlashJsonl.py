#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻快讯 JSONL 规范命名搬运(PublishNewsFlashJsonl)
==================================================

作用:
    把本仓库(采集暂存端) Archive/Finv/News/ 下两个快讯归档目录里的 jsonl
    文件, 按规范命名搬运到**另一个仓库**(归档端)的同一目录下, 搬运成功后
    删除本仓库的原文件:

        本仓库  Archive/Finv/News/FlashFutu/<year>/<yyyymmdd>.jsonl
        归档端  Archive/Finv/News/FlashFutu/<year>/News_Flash_FlashFutu_DAY_FT_<yyyymmdd>.jsonl

        本仓库  Archive/Finv/News/FlashEastMoney/<year>/<yyyymmdd>.jsonl
        归档端  Archive/Finv/News/FlashEastMoney/<year>/News_Flash_FlashEastMoney_DAY_EM_<yyyymmdd>.jsonl

    上游的采集/转换脚本(FlashFutuConvertJSONToJSONL.py 与 MainEastMoney.py)已改为
    直接输出规范命名, 因此本脚本的源端可能是**裸名或规范名两种形态**, 二者都收
    (见"源文件识别")。仓库里裸名清空之后本脚本仍持续生效: 后续每天上游产出的
    规范名文件同样会被搬到归档端并清理本仓库副本。

两个仓库是彼此独立的仓库(重要):
    源/删除端 : 本脚本所在的仓库(.git 解析, 即 ACANX/Distribution), 采集暂存
    提交端    : TARGET_OWNER / TARGET_REPO 显式指定的仓库(即 acdnx/Distribution), 归档

    这两个名字**不是同一个仓库的大小写变体**, 而是两个真实存在的独立仓库
    (API id 分别为 1182994715 与 1293253241, 网页互不重定向), 对应
    SyncRandomFiles.py 里"经 Contents API 提交到 acdnx/Distribution ... 用于
    新仓库的数据测试"的既有约定。因此提交目标**写死不从 .git 解析**:
    一旦误用 .git 解析结果, 提交会写回本仓库, 与"搬到新仓库"的意图完全相反。

关键行为:
    1. 源文件识别: 严格 `<yyyyMMdd>.jsonl`(裸名)与 `<模板>_<yyyyMMdd>.jsonl`
       (已规范名)都收, 日期必须是合法日历日; .gitkeep 等一律跳过。
    2. 幂等: 先算源文件的 git blob sha, 与归档端同路径文件的 sha 比较,
       一致即视为"已送达", 不再重复提交(避免生成内容不变的空提交);
       有差异则更新。比对走 sha, 不下载远端文件。
    3. 删除前提(照搬 SupabaseSyncMvsv 的口径): 必须先提交成功且远端写入无误,
       才删除本仓库的源文件。提交失败的文件原样保留, 留待下次重跑补齐。
    4. 删除同时作用于本仓库远端(Contents API)与本地工作区, 两者保持一致;
       删除失败只记录, 不中断后续文件。

提交方式:
    复用 .github/Python/GitHubCommitContent.py, 经 GitHub Contents API 提交到
    归档端仓库的 news 分支, 不依赖本地 git 提交, 因此不会与本仓库其它定时任务的
    数据推送产生 push 竞争。

配置来源(优先级从高到低):
    1. 命令行参数(--branch / --source / --date / --limit / --keep-source 等)
    2. 环境变量 NEWS_FLASH_BRANCH / NEWS_FLASH_SOURCE / NEWS_FLASH_DATE /
       NEWS_FLASH_LIMIT / NEWS_FLASH_ENABLE_DELETE / NEWS_FLASH_TARGET_OWNER /
       NEWS_FLASH_TARGET_REPO / MAIN_ARCHIVE_DIR
    3. 内置默认值: branch='news', archive_dir='Archive', limit=0(不限),
       target_owner='acdnx', target_repo='Distribution',
       enable_delete=True(见下方"删除开关")

删除开关(默认开启):
    本脚本的删除有明确的用户授权语义 —— "已成功递交且保存成功的, 可以删除原文件"。
    提交成功是删除的硬前提, 因此默认 enable_delete=True; 需要演练或核查时用
    --keep-source(或 NEWS_FLASH_ENABLE_DELETE=false)关闭, 只提交不删除。

用法:
    python3 .github/Python/Flash/PublishNewsFlashJsonl.py [--branch news]
                                                          [--source FlashFutu]
                                                          [--date 20260325]
                                                          [--limit 50]
                                                          [--target-owner acdnx]
                                                          [--target-repo Distribution]
                                                          [--keep-source]
                                                          [--force] [--dry-run] [--log]

    --source NAME : 只处理指定来源(FlashFutu / FlashEastMoney), 可重复指定;
                    缺省处理全部来源
    --date YYYYMMDD : 只处理该日期的文件; 缺省处理全部日期
    --limit N     : 单次最多处理 N 个文件(按来源、日期升序), 其余留待下次
    --target-owner / --target-repo : 归档端仓库身份(缺省 acdnx / Distribution)
    --keep-source : 只提交不删除源文件(关闭删除开关)
    --force       : 归档端已存在且内容一致时也重新提交
    --dry-run     : 只打印将要执行的动作, 不提交也不删除
    --log         : 打印每个文件的处理详情

日志格式:
    **每一行最左侧**都带东八区(UTC+8)时间戳, 格式 yyMMdd.HHmmss.SSS:
        260916.105958.123 提交端 acdnx/Distribution 上已有相同内容(sha 相同), 无需提交
    注意 GitHub Actions 页面自身标注的是 UTC 时间, 与本时间戳相差 8 小时, 别对错表。
    实现见 .github/Python/ConsoleLog.py —— news 线各脚本共用同一个模块。

    凡涉及"哪个仓库"一律写全 owner/repo。本脚本牵涉两个**同名**仓库
    (ACANX/Distribution 与 acdnx/Distribution), 只写 repo 名会完全无法区分,
    所以日志里改用"源端 / 提交端"指代:
        源端   : 本仓库。读它的文件, 搬运成功后删除 —— 且删两次:
                 先经 Contents API 删远端(持久生效), 再 os.remove 清理 runner
                 里 checkout 出来的工作副本(同属该仓库, 远端删除不会连带删掉它)
        提交端 : 归档端仓库。只写不删

环境要求:
    - Python 3.8+, 仅标准库;
    - 环境变量 GIT_COMMIT_TOKEN, **需同时具备本仓库与归档端仓库的 contents:write
      权限**(本仓库用于删除源文件, 归档端用于写入规范名文件), 严禁写入源码。

依赖: 同目录上一级(.github/Python/)的 GitHubCommitContent.py。
"""

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.parse
from collections import namedtuple
from datetime import datetime
from typing import Any, Dict, List, Optional

# 复用 GitHubCommitContent 的 HTTP 请求 / 认证头 / sha 查询 / 仓库身份解析
# (纯函数, 无副作用); 库位于上一级目录 .github/Python/
_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB_DIR = os.path.dirname(_HERE)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from GitHubCommitContent import (  # noqa: E402
    DEFAULT_API_BASE,
    _auth_headers,
    _ensure_console_utf8,
    _get_file_sha,
    _request,
    commit_content_file,
    load_owner_repo_from_git_config,
)

# 控制台行首时间戳(与 news 线其余脚本共用, 位于同一 .github/Python/)
from ConsoleLog import enableLogTimestamps  # noqa: E402

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 令牌环境变量名(与 GitHubCommitContent 约定一致)
ENV_TOKEN = "GIT_COMMIT_TOKEN"

# 裸名归档文件: 8 位纯数字日期 + .jsonl, 如 20260325.jsonl
BARE_NAME_RE = re.compile(r"^(\d{8})\.jsonl$")

# 归档端仓库(新仓库)身份: 显式写死, 不经 .git 解析。
# 本仓库是 ACANX/Distribution(采集暂存端), 归档端是 acdnx/Distribution —— 两个
# 独立仓库(API id 不同), 名字只差拼写, 极易看混, 所以不给 .git 解析留机会。
TARGET_OWNER = "acdnx"
TARGET_REPO = "Distribution"

# 内置默认配置
DEFAULT_BRANCH = "news"
DEFAULT_ARCHIVE_DIR = "Archive"

# 待发布的来源清单
#   name     : 来源名(命令行 --source 用)
#   rel      : 相对 <archive_dir> 的目录(年目录在其下)
#   template : 规范命名模板, %s 处填 <yyyyMMdd>
SourceSpec = namedtuple("SourceSpec", "name rel template")

SOURCES = (
    SourceSpec(
        name="FlashFutu",
        rel=("Finv", "News", "FlashFutu"),
        template="News_Flash_FlashFutu_DAY_FT_%s.jsonl",
    ),
    SourceSpec(
        name="FlashEastMoney",
        rel=("Finv", "News", "FlashEastMoney"),
        template="News_Flash_FlashEastMoney_DAY_EM_%s.jsonl",
    ),
)

# 单次运行的文件数上限(0 = 不限)
DEFAULT_LIMIT = 0


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def findRepoRoot() -> str:
    """从脚本位置推断仓库根目录(本文件位于 <仓库根>/.github/Python/Flash/)

    :return: 仓库根绝对路径; 层级不符时抛 RuntimeError(宁可报错也不扫错目录)
    """
    root = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
    expected = os.path.join(root, ".github", "Python", "Flash",
                            os.path.basename(__file__))
    if os.path.abspath(__file__) != os.path.abspath(expected):
        raise RuntimeError(
            "无法从脚本位置推断仓库根目录: %s(本文件所在层级可能已变更)" % root)
    return root


def resolveSetting(key: str, env_name: str, default: Any) -> Any:
    """按 环境变量 > 内置默认 的优先级取值(空串视为未设置)"""
    env_val = os.environ.get(env_name)
    if env_val in (None, ""):
        return default
    if isinstance(default, bool):
        return env_val.strip().lower() in ("1", "true", "yes", "y", "on")
    if isinstance(default, int):
        try:
            return int(env_val)
        except ValueError:
            return default
    return env_val


def envBool(env_name: str, default: bool) -> bool:
    """读取布尔型环境变量("1"/"true"/"yes"/"on" 为真, 大小写不敏感)"""
    val = os.environ.get(env_name)
    if val in (None, ""):
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on")


def toRepoPath(abs_path: str, repo_root: str) -> str:
    """绝对路径 -> 仓库内相对路径(统一用 "/" 分隔, 供 Contents API 使用)"""
    return os.path.relpath(abs_path, repo_root).replace(os.sep, "/")


def isValidDate(date_str: str) -> bool:
    """校验 8 位字符串是否为合法日历日(挡掉 20261345 这类非法日期)"""
    try:
        datetime.strptime(date_str, "%Y%m%d")
    except ValueError:
        return False
    return True


def buildCanonicalRegex(template: str) -> "re.Pattern":
    """由命名模板构造"已规范名"的匹配式(模板里 %s 的位置换成 8 位日期捕获组)

    模板形如 News_Flash_FlashFutu_DAY_FT_%s.jsonl; 用 partition 切出前后缀分别
    re.escape, 避免把模板里的 "." 之类当正则元字符。
    """
    prefix, sep, suffix = template.partition("%s")
    if not sep:
        raise ValueError("命名模板缺少 %%s 占位符: %s" % template)
    return re.compile("^%s(\\d{8})%s$" % (re.escape(prefix), re.escape(suffix)))


def localBlobSha(abs_path: str) -> Optional[str]:
    """按 git blob 语义计算本地文件的内容指纹: sha1("blob <字节数>\\0" + 内容)

    与 GitHub Contents API 返回的 sha 字段同口径, 可直接比较, 从而在**不下载**
    远端文件的前提下判断"归档端是否已是同一份内容"。

    读取方式刻意与 commit_content_file 保持一致(UTF-8 文本 + 通用换行, 再编码),
    保证算出的指纹与实际上传的字节完全相同 —— 否则 Windows 检出出的 CRLF 会让
    两边对不上, 白白多提交一次。

    :return: 40 位 sha1 十六进制串; 读取失败返回 None
    """
    try:
        with open(abs_path, "r", encoding="utf-8") as fh:
            data = fh.read().encode("utf-8")
    except OSError:
        return None
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# ---------------------------------------------------------------------------
# 远端操作
# ---------------------------------------------------------------------------

def deleteBranchFile(branch: str, path_key: str, token: str,
                     owner: str, repo: str,
                     api_base: str = DEFAULT_API_BASE) -> Dict[str, Any]:
    """删除指定仓库指定分支上的文件(GitHub Contents API)

    本脚本中该函数**只用于删除本仓库(采集暂存端)的源文件**, 不用于归档端。

    行为: GET 查 sha -> DELETE /contents/{path}(body: message + sha + branch)。
    GitHubCommitContent 未提供删除能力, 故此处按 SupabaseSyncMvsv.py 的同名实现口径
    自行调用(复用其 _get_file_sha / _auth_headers / _request)。

    :param branch: 分支名(news)
    :param path_key: 文件的仓库内相对路径
    :param token: GitHub 令牌(需本仓库 contents:write)
    :param owner: 仓库属主
    :param repo: 仓库名
    :param api_base: GitHub API 仓库集合根
    :return: dict {"success": bool, "already_gone": bool, "message": str|None}
    """
    def fail(msg: str) -> Dict[str, Any]:
        print("  ❌ 从 %s/%s@%s 删除失败: %s —— %s"
              % (owner, repo, branch, path_key, msg))
        return {"success": False, "already_gone": False, "message": msg}

    sha = _get_file_sha(api_base, owner, repo, path_key, branch, token, 30)
    if sha is None:
        # 远端已无该文件(可能是上一次运行已删除), 视为达成目标
        print("  ⚠️ %s/%s@%s 上已无该文件(可能上次已删), 视为已处理: %s"
              % (owner, repo, branch, path_key))
        return {"success": True, "already_gone": True, "message": None}

    url = "%s/%s/%s/contents/%s" % (
        api_base, owner, repo, urllib.parse.quote(path_key, safe="/"))
    body = {
        "message": "[PublishNewsFlashJsonl] delete legacy %s" % path_key,
        "sha": sha,
        "branch": branch,
    }
    status, text, err = _request(
        "DELETE", url, _auth_headers(token, with_body=True),
        json.dumps(body, ensure_ascii=False).encode("utf-8"), 30)
    if err:
        return fail(err)

    parsed = None
    try:
        parsed = json.loads(text)
    except ValueError:
        pass
    if status is not None and 200 <= status < 300:
        print("  ✅ 已从源端 %s/%s@%s 删除(远端持久生效, HTTP %s): %s"
              % (owner, repo, branch, status, path_key))
        return {"success": True, "already_gone": False, "message": None}
    reason = "HTTP %s" % status
    if isinstance(parsed, dict) and parsed.get("message"):
        reason = "%s: %s" % (reason, parsed.get("message"))
    return fail(reason)


# ---------------------------------------------------------------------------
# 任务收集
# ---------------------------------------------------------------------------

# 单个待处理文件
#   canonical: 源文件名是否已是规范名(用于日志区分"改名搬运"与"原样搬运")
Task = namedtuple("Task", "source date abs_src rel_src rel_dst canonical")


def collectTasks(archive_dir: str, repo_root: str,
                 only_sources: Optional[List[str]],
                 only_date: Optional[str]) -> List[Task]:
    """扫描归档目录, 收集待搬运文件

    源文件识别(两种形态都收, 因为上游已改为直接输出规范命名):
        - 裸名   <yyyyMMdd>.jsonl                -> 搬到归档端时改名
        - 规范名 <模板>_<yyyyMMdd>.jsonl          -> 搬过去保持同名

    只收日期合法的文件; 按来源、日期升序返回, 保证多次运行的处理顺序稳定可预期。

    :param archive_dir: 归档根目录(仓库相对或绝对路径)
    :param repo_root: 仓库根绝对路径
    :param only_sources: 只处理这些来源; None = 全部
    :param only_date: 只处理该日期(YYYYMMDD); None = 全部
    :return: Task 列表
    """
    base = archive_dir if os.path.isabs(archive_dir) \
        else os.path.join(repo_root, archive_dir)
    tasks: List[Task] = []

    for spec in SOURCES:
        if only_sources and spec.name not in only_sources:
            continue
        canonical_re = buildCanonicalRegex(spec.template)
        src_root = os.path.join(base, *spec.rel)
        if not os.path.isdir(src_root):
            print("警告: 来源目录不存在, 跳过: %s" % src_root, file=sys.stderr)
            continue
        # 年目录(如 2026); 非目录条目忽略
        for year in sorted(os.listdir(src_root)):
            year_dir = os.path.join(src_root, year)
            if not os.path.isdir(year_dir):
                continue
            for fname in sorted(os.listdir(year_dir)):
                m = BARE_NAME_RE.match(fname)
                canonical = False
                if not m:
                    m = canonical_re.match(fname)
                    canonical = m is not None
                if not m:
                    continue  # .gitkeep / 其它文件
                date = m.group(1)
                if only_date and date != only_date:
                    continue
                if not isValidDate(date):
                    print("警告: 文件名日期非法, 跳过: %s"
                          % os.path.join(year_dir, fname), file=sys.stderr)
                    continue
                abs_src = os.path.join(year_dir, fname)
                abs_dst = os.path.join(year_dir, spec.template % date)
                tasks.append(Task(
                    source=spec.name,
                    date=date,
                    abs_src=abs_src,
                    rel_src=toRepoPath(abs_src, repo_root),
                    rel_dst=toRepoPath(abs_dst, repo_root),
                    canonical=canonical,
                ))
    return tasks


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    _ensure_console_utf8()
    # 之后再打印的每一行都会带上东八区(yyMMdd.HHmmss.SSS)行首时间戳
    enableLogTimestamps()

    parser = argparse.ArgumentParser(
        description="把新闻快讯 jsonl 按规范命名搬运到归档端仓库, 并清理本仓库原文件",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--branch", default=None,
                        help="目标分支(默认 news, 可用 NEWS_FLASH_BRANCH 覆盖)")
    parser.add_argument("--archive-dir", default=None,
                        help="归档根目录(默认 Archive, 可用 MAIN_ARCHIVE_DIR 覆盖)")
    parser.add_argument("--source", action="append", default=None,
                        choices=[s.name for s in SOURCES],
                        help="只处理指定来源, 可重复; 缺省全部")
    parser.add_argument("--date", default=None,
                        help="只处理该日期 YYYYMMDD; 缺省全部")
    parser.add_argument("--limit", type=int, default=None,
                        help="单次最多处理 N 个文件(0=不限)")
    parser.add_argument("--target-owner", default=None,
                        help="归档端仓库属主(默认 acdnx, 可用 "
                             "NEWS_FLASH_TARGET_OWNER 覆盖)")
    parser.add_argument("--target-repo", default=None,
                        help="归档端仓库名(默认 Distribution, 可用 "
                             "NEWS_FLASH_TARGET_REPO 覆盖)")
    parser.add_argument("--keep-source", action="store_true",
                        help="只提交不删除源文件(关闭删除开关)")
    parser.add_argument("--force", action="store_true",
                        help="归档端已存在且内容一致时也重新提交")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将要执行的动作, 不提交也不删除")
    parser.add_argument("--log", action="store_true",
                        help="打印每个文件的处理详情")
    args = parser.parse_args(argv)

    branch = args.branch or resolveSetting("branch", "NEWS_FLASH_BRANCH", DEFAULT_BRANCH)
    archive_dir = args.archive_dir or resolveSetting(
        "archive_dir", "MAIN_ARCHIVE_DIR", DEFAULT_ARCHIVE_DIR)
    limit = args.limit if args.limit is not None else resolveSetting(
        "limit", "NEWS_FLASH_LIMIT", DEFAULT_LIMIT)
    tgt_owner = args.target_owner or resolveSetting(
        "target_owner", "NEWS_FLASH_TARGET_OWNER", TARGET_OWNER)
    tgt_repo = args.target_repo or resolveSetting(
        "target_repo", "NEWS_FLASH_TARGET_REPO", TARGET_REPO)
    # 删除开关: 命令行 --keep-source 优先, 其次环境变量, 默认开启(见模块 docstring)
    enable_delete = (not args.keep_source) and envBool("NEWS_FLASH_ENABLE_DELETE", True)
    only_date = args.date or os.environ.get("NEWS_FLASH_DATE") or None
    only_sources = args.source or None
    if only_sources is None:
        env_src = os.environ.get("NEWS_FLASH_SOURCE", "").strip()
        if env_src:
            only_sources = [s.strip() for s in env_src.split(",") if s.strip()]
    # 环境变量传入的来源名不经 argparse 的 choices 校验, 这里补齐:
    # 否则一个拼错的来源名会让所有来源都不匹配, 静默变成"无事可做"
    valid_names = [s.name for s in SOURCES]
    if only_sources:
        unknown = [s for s in only_sources if s not in valid_names]
        if unknown:
            print("❌ 未知来源名: %s(可选: %s)"
                  % (", ".join(unknown), ", ".join(valid_names)), file=sys.stderr)
            return 2

    token = os.environ.get(ENV_TOKEN, "").strip()

    try:
        repo_root = findRepoRoot()
    except RuntimeError as e:
        print("❌ %s" % e, file=sys.stderr)
        return 2

    # 源/删除端 = 本仓库(.git 解析); 提交端 = TARGET_OWNER/TARGET_REPO(显式常量)
    src_owner, src_repo = load_owner_repo_from_git_config()
    if not (src_owner and src_repo):
        print("❌ 未能从本仓库 .git/config 解析出 github.com 的 owner/repo",
              file=sys.stderr)
        return 2

    print("======== 新闻快讯 JSONL 规范命名搬运 ========")
    print("仓库根目录 : %s" % repo_root)
    print("归档根目录 : %s" % archive_dir)
    print("目标分支   : %s" % branch)
    print("处理来源   : %s" % (", ".join(only_sources) if only_sources
                              else ", ".join(s.name for s in SOURCES)))
    print("日期过滤   : %s" % (only_date or "不限"))
    print("源/删除端  : %s/%s(本仓库, 搬运成功后删除原文件)"
          % (src_owner, src_repo))
    print("提交端     : %s/%s(归档端仓库)" % (tgt_owner, tgt_repo))
    if src_repo == tgt_repo:
        print("提示: 源端与提交端仓库名相同(%s), 只有 owner 不同 —— 下文日志一律"
              "写全 owner/repo, 避免看混" % src_repo)
    if (src_owner.lower(), src_repo.lower()) == (tgt_owner.lower(), tgt_repo.lower()):
        print("⚠️ 源端与提交端是同一个仓库: 本脚本将只在单个仓库内改名, "
              "不会发生跨仓库搬运(如非本意, 请检查 --target-owner / --target-repo)")
    print("删除开关   : %s" % ("开(提交成功后删除源端原文件)" if enable_delete
                              else "关(只提交, 保留源端原文件)"))
    print("运行模式   : %s" % ("dry-run(不做任何写操作)" if args.dry_run else "实际执行"))

    # Token 只在非 dry-run 时需要(提交/删除都是写操作)
    if not args.dry_run and not token:
        print("❌ 请设置环境变量 %s(需本仓库与归档端仓库的 contents:write 权限)"
              % ENV_TOKEN, file=sys.stderr)
        return 2
    if not args.dry_run:
        print("提示: 提交走 Contents API, 不经本地 git 提交, 不会与其它定时任务产生 push 竞争")

    # --- 收集 ---
    tasks = collectTasks(archive_dir, repo_root, only_sources, only_date)
    print("\n收集完成: 待搬运的 jsonl 共 %d 个(裸名 %d, 已规范名 %d)"
          % (len(tasks), sum(1 for t in tasks if not t.canonical),
             sum(1 for t in tasks if t.canonical)))
    if not tasks:
        print("没有需要搬运的文件, 无事可做(本仓库归档目录已清空)")
        return 0

    picked = tasks
    if limit and limit > 0 and len(tasks) > limit:
        picked = tasks[:limit]
        print("本次按 limit=%d 只处理前 %d 个, 其余 %d 个留待下次:"
              % (limit, len(picked), len(tasks) - limit))
        for t in tasks[limit:]:
            print("    - %s" % t.rel_src)

    # --- 逐个处理 ---
    published, already_ok = [], []      # 已提交 / 内容一致无需提交
    deleted, delete_failed = [], []
    kept, failed = [], []               # 未提交(保留原文件) / 提交失败
    for idx, t in enumerate(picked, 1):
        print("\n[%d/%d] %s" % (idx, len(picked), t.rel_src))
        if args.log:
            print("    来源=%s 日期=%s 形态=%s"
                  % (t.source, t.date, "已规范名" if t.canonical else "裸名"))
            if t.rel_src != t.rel_dst:
                print("    将改名为: %s" % t.rel_dst)
            else:
                print("    名称不变, 仅搬到提交端 %s/%s" % (tgt_owner, tgt_repo))

        # 1) 幂等判断: 算本地 blob sha, 与归档端同路径文件的 sha 比较(不下载远端)
        need_commit = True
        if args.force:
            print("    已指定 --force, 强制重新提交: %s" % t.rel_dst)
        elif token:
            local_sha = localBlobSha(t.abs_src)
            remote_sha = _get_file_sha(DEFAULT_API_BASE, tgt_owner, tgt_repo,
                                       t.rel_dst, branch, token, 30)
            if local_sha and remote_sha and local_sha == remote_sha:
                need_commit = False
                print("    提交端 %s/%s 上已有相同内容(sha 相同), 无需提交"
                      % (tgt_owner, tgt_repo))
                already_ok.append(t.rel_dst)
            elif remote_sha is None:
                print("    提交端 %s/%s 上不存在, 将新建: %s"
                      % (tgt_owner, tgt_repo, t.rel_dst))
            else:
                print("    提交端 %s/%s 上已存在但内容有差异, 将更新: %s"
                      % (tgt_owner, tgt_repo, t.rel_dst))
        else:
            print("    (dry-run 且无令牌) 未校验提交端 %s/%s, 假定需要提交: %s"
                  % (tgt_owner, tgt_repo, t.rel_dst))

        if args.dry_run:
            action = "提交" if need_commit else "跳过提交"
            print("    (dry-run) %s -> 随后%s从源端 %s/%s 删除原文件"
                  % (action, "会" if enable_delete else "不会",
                     src_owner, src_repo))
            continue

        # 2) 提交到归档端(先提交, 成功才谈删除)
        if need_commit:
            result = commit_content_file(
                t.rel_dst, t.abs_src,
                branch=branch, owner=tgt_owner, repo=tgt_repo,
                commit_msg="[PublishNewsFlashJsonl] %s" % t.rel_dst,
            )
            if not result["success"]:
                print("    ❌ 提交到 %s/%s 失败, 保留原文件: %s"
                      % (tgt_owner, tgt_repo, result["message"]))
                kept.append("%s: %s" % (t.rel_src, result["message"]))
                continue
            print("    ✅ 已提交到 %s/%s(HTTP %s): %s"
                  % (tgt_owner, tgt_repo, result["http_status"], t.rel_dst))
            published.append(t.rel_dst)

        # 3) 删除本仓库的源文件(提交成功是硬前提; 上面已 continue 掉失败分支)
        if not enable_delete:
            print("    🕐 已送达提交端 %s/%s, 源端 %s/%s 上待删除(删除开关未开启): %s"
                  % (tgt_owner, tgt_repo, src_owner, src_repo, t.rel_src))
            kept.append("%s: 待删除(开关关闭)" % t.rel_src)
            continue

        result = deleteBranchFile(branch, t.rel_src, token, src_owner, src_repo)
        if not result["success"]:
            delete_failed.append("%s: %s" % (t.rel_src, result["message"]))
            continue
        # 远端删掉后同步清理本地工作副本。二者同属源端仓库: runner 里是
        # actions/checkout 出来的独立拷贝, 远端删除不会连带删掉它, 不清理的话
        # 工作区会留着一个"远端已不存在"的文件继续往下走。
        try:
            os.remove(t.abs_src)
            print("    ✅ 本地工作副本已同步移除(同为源端 %s/%s, 仅清理 runner "
                  "工作区, 不影响远端): %s" % (src_owner, src_repo, t.rel_src))
        except OSError as e:
            print("    ⚠️ 本地工作副本移除失败(远端已删, 不影响最终结果): %s" % e)
        deleted.append(t.rel_src)

    # --- 汇总 ---
    print("\n======== 搬运结束 ========")
    if args.dry_run:
        print("dry-run 结束, 未做任何写操作")
        return 0
    print("本次处理      : %d 个" % len(picked))
    print("已提交(新建/更新): %d 个 -> %s/%s@%s"
          % (len(published), tgt_owner, tgt_repo, branch))
    print("内容一致跳过提交: %d 个" % len(already_ok))
    print("已删除原文件  : %d 个(本仓库 %s/%s@%s)"
          % (len(deleted), src_owner, src_repo, branch))
    print("删除失败      : %d 个" % len(delete_failed))
    print("保留未删(含失败): %d 个" % len(kept))
    if delete_failed:
        print("删除失败清单(数据已送达, 重跑即可补齐删除):")
        for item in delete_failed:
            print("    - %s" % item)
    if kept:
        print("保留清单(提交未成功或删除开关关闭, 修复后重跑即可):")
        for item in kept:
            print("    - %s" % item)
    remaining = len(tasks) - len(picked)
    if remaining > 0:
        print("本次受 limit 限制未处理 %d 个, 下次运行继续" % remaining)
    return 0


if __name__ == "__main__":
    sys.exit(main())
