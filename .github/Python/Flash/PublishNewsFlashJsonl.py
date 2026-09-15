#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻快讯 JSONL 规范命名发布(PublishNewsFlashJsonl)
==================================================

作用:
    把 Archive/Finv/News/ 下两个快讯归档目录中"历史遗留的裸名 jsonl"按规范
    命名提交到远端 news 分支的同一目录, 提交成功后删除原文件:

        Archive/Finv/News/FlashFutu/<year>/<yyyymmdd>.jsonl
          -> Archive/Finv/News/FlashFutu/<year>/News_Flash_FlashFutu_DAY_FT_<yyyymmdd>.jsonl

        Archive/Finv/News/FlashEastMoney/<year>/<yyyymmdd>.jsonl
          -> Archive/Finv/News/FlashEastMoney/<year>/News_Flash_FlashEastMoney_DAY_EM_<yyyymmdd>.jsonl

    上游的采集/转换脚本(FlashFutuConvertJSONToJSONL.py 与 MainEastMoney.py)已改为
    直接输出规范命名, 因此本脚本属"存量清理"性质: 仓库里不再产生裸名文件之后,
    每次运行都无事可做(完整幂等), 留着定时跑只是兜底。

关键行为:
    1. 只认严格 `<yyyyMMdd>.jsonl`(8 位纯数字 + .jsonl)的裸名文件, 且日期必须
       是合法日历日; 已带规范前缀的文件、.gitkeep 等一律跳过。
    2. 幂等: 工作区里规范命名的目标文件已存在且内容与源文件一致时, 视为
       "已发布", 不再重复提交(避免生成内容不变的空提交); 内容有差异则更新。
    3. 删除前提(照搬 SupabaseSyncMvsv 的口径): 必须先提交成功且远端写入无误,
       才删除源文件。提交失败的文件原样保留, 留待下次重跑补齐。
    4. 删除同时作用于远端(Contents API)与本地工作区, 两者保持一致;
       删除失败只记录, 不中断后续文件。

提交方式:
    复用 .github/Python/GitHubCommitContent.py, 经 GitHub Contents API 提交到
    本仓库(.git 解析, 即 ACANX/Distribution)的 news 分支, 不依赖本地 git 提交,
    因此不会与本仓库其它定时任务的数据推送产生 push 竞争。

配置来源(优先级从高到低):
    1. 命令行参数(--branch / --source / --date / --limit / --keep-source 等)
    2. 环境变量 NEWS_FLASH_BRANCH / NEWS_FLASH_SOURCE / NEWS_FLASH_DATE /
       NEWS_FLASH_LIMIT / NEWS_FLASH_ENABLE_DELETE / MAIN_ARCHIVE_DIR
    3. 内置默认值: branch='news', archive_dir='Archive', limit=0(不限),
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
                                                          [--keep-source]
                                                          [--force] [--dry-run] [--log]

    --source NAME : 只处理指定来源(FlashFutu / FlashEastMoney), 可重复指定;
                    缺省处理全部来源
    --date YYYYMMDD : 只处理该日期的文件; 缺省处理全部日期
    --limit N     : 单次最多处理 N 个文件(按来源、日期升序), 其余留待下次
    --keep-source : 只提交不删除源文件(关闭删除开关)
    --force       : 目标已存在且内容一致时也重新提交
    --dry-run     : 只打印将要执行的动作, 不提交也不删除
    --log         : 打印每个文件的处理详情

环境要求:
    - Python 3.8+, 仅标准库;
    - 环境变量 GIT_COMMIT_TOKEN(需本仓库 contents:write 权限), 严禁写入源码。

依赖: 同目录上一级(.github/Python/)的 GitHubCommitContent.py。
"""

import argparse
import filecmp
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

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 令牌环境变量名(与 GitHubCommitContent 约定一致)
ENV_TOKEN = "GIT_COMMIT_TOKEN"

# 裸名归档文件: 8 位纯数字日期 + .jsonl, 如 20260325.jsonl
BARE_NAME_RE = re.compile(r"^(\d{8})\.jsonl$")

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


def sameContent(path_a: str, path_b: str) -> bool:
    """比较两个文件内容是否一致(shallow=False: 逐字节比较, 不做仅元数据的快速判断)"""
    try:
        return filecmp.cmp(path_a, path_b, shallow=False)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 远端操作
# ---------------------------------------------------------------------------

def deleteBranchFile(branch: str, path_key: str, token: str,
                     owner: str, repo: str,
                     api_base: str = DEFAULT_API_BASE) -> Dict[str, Any]:
    """删除本仓库指定分支上的文件(GitHub Contents API)

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
        print("  ❌ 删除失败: %s@%s %s —— %s" % (repo, branch, path_key, msg))
        return {"success": False, "already_gone": False, "message": msg}

    sha = _get_file_sha(api_base, owner, repo, path_key, branch, token, 30)
    if sha is None:
        # 远端已无该文件(可能是上一次运行已删除), 视为达成目标
        print("  ⚠️ 远端已无该文件(可能已删除), 视为已处理: %s" % path_key)
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
        print("  ✅ 远端删除成功: %s@%s %s(HTTP %s)"
              % (repo, branch, path_key, status))
        return {"success": True, "already_gone": False, "message": None}
    reason = "HTTP %s" % status
    if isinstance(parsed, dict) and parsed.get("message"):
        reason = "%s: %s" % (reason, parsed.get("message"))
    return fail(reason)


# ---------------------------------------------------------------------------
# 任务收集
# ---------------------------------------------------------------------------

# 单个待处理文件
Task = namedtuple("Task", "source date abs_src abs_dst rel_src rel_dst")


def collectTasks(archive_dir: str, repo_root: str,
                 only_sources: Optional[List[str]],
                 only_date: Optional[str]) -> List[Task]:
    """扫描归档目录, 收集待发布(裸名)文件

    只收严格 `<yyyyMMdd>.jsonl` 且日期合法的文件; 按来源、日期升序返回,
    保证多次运行的处理顺序稳定可预期。

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
                if not m:
                    continue  # 已规范命名的 / .gitkeep / 其它文件
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
                    abs_dst=abs_dst,
                    rel_src=toRepoPath(abs_src, repo_root),
                    rel_dst=toRepoPath(abs_dst, repo_root),
                ))
    return tasks


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    _ensure_console_utf8()

    parser = argparse.ArgumentParser(
        description="把新闻快讯裸名 jsonl 按规范命名发布到 news 分支并清理原文件",
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
    parser.add_argument("--keep-source", action="store_true",
                        help="只提交不删除源文件(关闭删除开关)")
    parser.add_argument("--force", action="store_true",
                        help="目标已存在且内容一致时也重新提交")
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

    print("======== 新闻快讯 JSONL 规范命名发布 ========")
    print("仓库根目录 : %s" % repo_root)
    print("归档根目录 : %s" % archive_dir)
    print("目标分支   : %s" % branch)
    print("处理来源   : %s" % (", ".join(only_sources) if only_sources
                              else ", ".join(s.name for s in SOURCES)))
    print("日期过滤   : %s" % (only_date or "不限"))
    print("删除开关   : %s" % ("开(提交成功后删除原文件)" if enable_delete
                              else "关(只提交, 保留原文件)"))
    print("运行模式   : %s" % ("dry-run(不做任何写操作)" if args.dry_run else "实际执行"))

    # Token 只在非 dry-run 时需要(提交/删除都是写操作)
    if not args.dry_run and not token:
        print("❌ 请设置环境变量 %s(需本仓库 contents:write 权限)" % ENV_TOKEN,
              file=sys.stderr)
        return 2

    owner, repo = load_owner_repo_from_git_config()
    if not (owner and repo):
        print("❌ 未能从本仓库 .git/config 解析出 github.com 的 owner/repo",
              file=sys.stderr)
        return 2
    print("目标仓库   : %s/%s" % (owner, repo))
    if not args.dry_run:
        print("提示: 提交走 Contents API, 不经本地 git 提交, 不会与其它定时任务产生 push 竞争")

    # --- 收集 ---
    tasks = collectTasks(archive_dir, repo_root, only_sources, only_date)
    print("\n收集完成: 待处理的裸名 jsonl 共 %d 个" % len(tasks))
    if not tasks:
        print("没有需要发布的文件, 无事可做(仓库已全部为规范命名)")
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
            print("    来源=%s 日期=%s" % (t.source, t.date))

        # 1) 幂等判断: 目标已存在且内容一致 -> 远端已是这份内容
        need_commit = True
        if os.path.exists(t.abs_dst) and not args.force:
            if sameContent(t.abs_src, t.abs_dst):
                need_commit = False
                print("    目标已存在且内容一致, 跳过提交: %s" % t.rel_dst)
                already_ok.append(t.rel_dst)
            else:
                print("    目标已存在但内容有差异, 将更新: %s" % t.rel_dst)
        if not os.path.exists(t.abs_dst):
            print("    目标不存在, 将新建: %s" % t.rel_dst)

        if args.dry_run:
            action = "提交" if need_commit else "跳过提交"
            print("    (dry-run) %s -> 随后%s删除原文件"
                  % (action, "会" if enable_delete else "不会"))
            continue

        # 2) 提交(先提交, 成功才谈删除)
        if need_commit:
            result = commit_content_file(
                t.rel_dst, t.abs_src,
                branch=branch, owner=owner, repo=repo,
                commit_msg="[PublishNewsFlashJsonl] %s" % t.rel_dst,
            )
            if not result["success"]:
                print("    ❌ 提交失败, 保留原文件: %s" % result["message"])
                kept.append("%s: %s" % (t.rel_src, result["message"]))
                continue
            print("    ✅ 提交成功(HTTP %s): %s" % (result["http_status"], t.rel_dst))
            published.append(t.rel_dst)

        # 3) 删除原文件(提交成功是硬前提; 上面已 continue 掉失败分支)
        if not enable_delete:
            print("    🕐 已发布, 待删除(删除开关未开启): %s" % t.rel_src)
            kept.append("%s: 待删除(开关关闭)" % t.rel_src)
            continue

        result = deleteBranchFile(branch, t.rel_src, token, owner, repo)
        if not result["success"]:
            delete_failed.append("%s: %s" % (t.rel_src, result["message"]))
            continue
        # 远端删掉后本地同步删除, 保持工作区与远端一致
        try:
            os.remove(t.abs_src)
            print("    ✅ 本地删除成功: %s" % t.rel_src)
        except OSError as e:
            print("    ⚠️ 本地删除失败(远端已删): %s" % e)
        deleted.append(t.rel_src)

    # --- 汇总 ---
    print("\n======== 发布结束 ========")
    if args.dry_run:
        print("dry-run 结束, 未做任何写操作")
        return 0
    print("本次处理      : %d 个" % len(picked))
    print("已提交(新建/更新): %d 个" % len(published))
    print("内容一致跳过提交: %d 个" % len(already_ok))
    print("已删除原文件  : %d 个" % len(deleted))
    print("删除失败      : %d 个" % len(delete_failed))
    print("保留未删(含失败): %d 个" % len(kept))
    if delete_failed:
        print("删除失败清单(数据已发布, 重跑即可补齐删除):")
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
