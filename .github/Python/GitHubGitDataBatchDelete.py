#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHubGitDataBatchDelete —— 用 GitHub Git Data API「一次 commit」批量删除多个文件
========================================================================================

⚠️ 本文件是**试验性独立工具**：不 import 仓库内任何模块，也不被任何 workflow /
任务脚本引用。等联调验证通过后，再把 delete_paths_in_one_commit() 整合进
Quote/Task16DayMvsvFileDataSyncPG.py（替换现有的「逐文件 DELETE /contents」实现）。

一、解决什么问题
----------------------------------------------------------------------------------------
现状（Contents API）每删一个文件 = 1 次 GET 取 sha + 1 次 DELETE = 1 个 commit；
一轮删 500 份行情文件就是 1000 次 API 调用、500 条 commit、且中途失败只能删一半。

Git Data API 可以把「删除 N 个文件」压成一次 commit：

    1) GET  /repos/{o}/{r}/git/ref/heads/{branch}      取当前 commit sha（parent）
    2) GET  /repos/{o}/{r}/git/commits/{sha}           取 tree sha（base_tree）
    3) POST /repos/{o}/{r}/git/trees                   tree 条目 sha=null 即删除
    4) POST /repos/{o}/{r}/git/commits                 用新 tree + parent 建 commit
    5) PATCH /repos/{o}/{r}/git/refs/heads/{branch}    把 ref 快进到新 commit

于是 500 份文件 = 5 次调用（每批 1 条 commit），且原子：要么这批全删，要么一个不删。
删除不存在的路径不需要先探测——`sha: null` 对不存在的条目是无害的（会被忽略），
只有 --verify 时才会先拉一次 recursive tree 把不存在的路径挑出来跳过。

二、用法
----------------------------------------------------------------------------------------
令牌只从环境变量读取（严禁写进命令行 / 源码，避免泄漏到进程列表与 shell 历史）：

    export GIT_COMMIT_TOKEN=ghp_xxx        # 需 contents:write

查看帮助：

    python3 .github/Python/GitHubGitDataBatchDelete.py --help

演练（dry-run：只 GET 探测，不发任何写请求）：

    python3 .github/Python/GitHubGitDataBatchDelete.py \\
        --repo ACANX/Distribution --branch quote --dry-run \\
        --paths-file /tmp/delete-list.txt

真删（一次 commit 删除全部）：

    python3 .github/Python/GitHubGitDataBatchDelete.py \\
        --repo ACANX/Distribution --branch quote \\
        --paths-file /tmp/delete-list.txt \\
        --message "[Task16PG] delete synced files"

直接在命令行给路径（可重复）：

    python3 ... --repo ACANX/Distribution --branch quote \\
        --path Archive/Finv/SecuQuote/FTMM/V5/US_ARCA/SPY/US_ARCA_SPY_Min_FTMM_Day_20261019.mvsv

自测（起本地假 GitHub 服务，完全离线）：

    python3 .github/Python/GitHubGitDataBatchDelete.py --self-test

paths-file 规则：一行一个仓库内相对路径；空行与 `#` 开头的行忽略；自动去重保序。

三、并发与非快进重试（重要）
----------------------------------------------------------------------------------------
quote 分支常被采集任务写入，从「取 ref」到「PATCH ref」之间分支可能前进，
此时 PATCH 返回 422（Update is not a fast forward）。本工具的应对：
重新取 ref → 重算 tree → 重新建 commit → 再 PATCH，最多重试 --retries 次（默认 3）。
因为每次重试都是基于最新 ref 重算，所以不会覆盖别人刚提交的数据。

四、安全阀
----------------------------------------------------------------------------------------
    --dry-run          只打印计划，不发 POST / PATCH
    --max-delete N     单次允许删除的路径数上限（默认 1000），超过直接拒绝（退出码 3）
    --branch           必填，绝不隐式默认到某个分支
    --verify/--no-verify
                       是否先拉 recursive tree 过滤不存在的路径（默认 verify）。
                       注意：超大仓库的 tree 可能被 GitHub 截断（truncated=true），
                       此时本工具会报错并提示改用 --no-verify，不会静默误删。
    --batch-size N     每批（= 每 commit）删除的路径数，默认 500，硬上限 1000

五、退出码
----------------------------------------------------------------------------------------
    0 = 全部成功（或没有待删除路径 / dry-run 完成）
    1 = 调用失败（鉴权、网络、GitHub 返回错误）
    2 = 参数错误（缺 repo / branch / 路径来源、令牌缺失）
    3 = 超过 --max-delete 上限（拒绝执行）

【环境要求】Python 3.8+，仅标准库。
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_API_BASE = "https://api.github.com"
USER_AGENT = ("Distribution-GitDataBatchDelete/1.0 "
              "(+https://github.com/ACANX/Distribution)")

DEFAULT_BATCH_SIZE = 500
MAX_BATCH_SIZE = 1000                     # 单次 POST /git/trees 条目数硬上限（保守值）
DEFAULT_MAX_DELETE = 1000                 # 单次运行删除路径数上限
DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 3                       # 非快进冲突重试次数
RETRY_BACKOFF_SECONDS = 2.0
SAMPLE_LIMIT = 20                         # 日志中路径清单的展示条数

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BAD_ARGS = 2
EXIT_TOO_MANY = 3


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

_console_configured = False


def _ensure_console_utf8():
    """把 stdout/stderr 重配置为 UTF-8（errors=replace），仅首次调用生效

    防止 Windows 遗留控制台 / 非 UTF-8 locale 下输出中文抛 UnicodeEncodeError。
    """
    global _console_configured
    if _console_configured:
        return
    _console_configured = True
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def snippet(text, limit=300):
    """截断响应体，便于日志里给出可读的错误上下文"""
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def chunks(source, size):
    """把列表切成固定大小的片"""
    return [source[i:i + size] for i in range(0, len(source), size)]


def log_path_list(title, paths, limit=SAMPLE_LIMIT):
    """打印路径清单（超过 limit 只给总数与首几项）"""
    if not paths:
        return
    print("%s（%d 个）：" % (title, len(paths)))
    for path in paths[:limit]:
        print("    - %s" % path)
    if len(paths) > limit:
        print("    - ……其余 %d 个同上" % (len(paths) - limit))


def normalize_api_base(value):
    """规范化 API 根：去尾部斜杠；为空时用默认值"""
    text = (value or "").strip() or DEFAULT_API_BASE
    return text.rstrip("/")


def split_repo_slug(text):
    """把 "owner/repo"（可带 .git / 前后斜杠）拆成 (owner, repo)"""
    slug = (text or "").strip().strip("/")
    if slug.endswith(".git"):
        slug = slug[:-4]
    parts = [part for part in slug.split("/") if part]
    if len(parts) != 2:
        raise ValueError("--repo 必须是 owner/repo 形式，当前为 %r" % text)
    return parts[0], parts[1]


def read_paths_from_file(path):
    """从文本文件读取路径清单（忽略空行与 # 注释）"""
    paths = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            paths.append(line)
    return paths


def dedupe(paths):
    """去重且保持原顺序"""
    seen = set()
    result = []
    for path in paths:
        key = path.strip().lstrip("/")
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


# ---------------------------------------------------------------------------
# HTTP 层（仅标准库）
# ---------------------------------------------------------------------------

class GitHubError(Exception):
    """GitHub API 调用失败（不可重试）"""


class NonFastForwardError(GitHubError):
    """PATCH ref 非快进冲突（可重试：重取 ref、重算 tree）"""


def http_request(method, url, token, body_bytes=None, timeout=DEFAULT_TIMEOUT):
    """最小 GitHub REST 调用

    :return: (status, text)；网络层失败直接抛 GitHubError
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = "Bearer %s" % token
    if body_bytes is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body_bytes, method=method,
                                     headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except OSError as error:                       # 含 URLError / TimeoutError
        raise GitHubError("%s %s 网络错误：%s" % (method, url, error))


def parse_json(text, context):
    """把响应体解析为 JSON 对象；失败抛 GitHubError"""
    try:
        payload = json.loads(text)
    except ValueError as error:
        raise GitHubError("%s 响应不是合法 JSON：%s" % (context, error))
    if not isinstance(payload, dict):
        raise GitHubError("%s 响应不是 JSON 对象" % context)
    return payload


def github_message(status, text):
    """从未成功的响应里提取可读原因"""
    reason = "HTTP %s" % status
    try:
        payload = json.loads(text)
        if isinstance(payload, dict) and payload.get("message"):
            reason = "%s：%s" % (reason, payload["message"])
        elif isinstance(payload, dict) and payload.get("errors"):
            reason = "%s：%s" % (reason, payload["errors"])
    except ValueError:
        if (text or "").strip():
            reason = "%s：%s" % (reason, snippet(text))
    return reason


# ---------------------------------------------------------------------------
# Git Data API 客户端
# ---------------------------------------------------------------------------

class GitDataDeleter:
    """用 Git Data API 一次 commit 删除一批文件

    只暴露本工具需要的 5 个动作，便于后续整合时按需裁剪。
    """

    def __init__(self, owner, repo, branch, token, api_base=DEFAULT_API_BASE,
                 timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES):
        self.owner = owner
        self.repo = repo
        self.branch = branch
        self.token = token
        self.api_base = normalize_api_base(api_base)
        self.timeout = timeout
        self.retries = max(0, retries)

    # -- 内部 ---------------------------------------------------------------

    def _url(self, path):
        return "%s/repos/%s/%s%s" % (
            self.api_base, urllib.parse.quote(self.owner, safe=""),
            urllib.parse.quote(self.repo, safe=""), path)

    def _branch_path(self):
        return urllib.parse.quote(self.branch, safe="/")

    def _call(self, method, path, body=None):
        """发一次请求并返回 (status, text)"""
        url = self._url(path)
        body_bytes = None
        if body is not None:
            body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return http_request(method, url, self.token, body_bytes, self.timeout)

    def _must(self, method, path, body=None, expect=(200, 201)):
        status, text = self._call(method, path, body)
        if status not in expect:
            raise GitHubError("%s %s 失败：%s"
                              % (method, path, github_message(status, text)))
        return parse_json(text, "%s %s" % (method, path))

    # -- 读 -----------------------------------------------------------------

    def get_head_commit(self):
        """取分支当前 commit sha（作为新 commit 的 parent）"""
        payload = self._must("GET", "/git/ref/heads/%s" % self._branch_path())
        sha = (payload.get("object") or {}).get("sha")
        if not sha:
            raise GitHubError("GET ref 响应缺少 object.sha")
        return sha

    def get_commit_tree(self, commit_sha):
        """取 commit 对应的 tree sha（作为 base_tree）"""
        payload = self._must("GET", "/git/commits/%s" % commit_sha)
        sha = (payload.get("tree") or {}).get("sha")
        if not sha:
            raise GitHubError("GET commit 响应缺少 tree.sha")
        return sha

    def list_tree_paths(self, tree_sha):
        """列出 tree 下所有 blob 路径（recursive=1）

        tree 响应被截断（truncated=true）时抛 GitHubError：此时路径集合不完整，
        继续用于过滤会误判「文件不存在」。
        """
        payload = self._must(
            "GET", "/git/trees/%s?recursive=1" % tree_sha)
        if payload.get("truncated"):
            raise GitHubError(
                "tree %s 响应被 GitHub 截断（truncated=true），"
                "无法可靠判断路径是否存在，请改用 --no-verify" % tree_sha)
        return {entry.get("path") for entry in payload.get("tree") or []
                if entry.get("type") == "blob"}

    # -- 写 -----------------------------------------------------------------

    def create_tree(self, base_tree, paths):
        """在新 tree 里把 paths 全部标记为删除（sha=null），返回新 tree sha"""
        entries = [{"path": path, "mode": "100644", "type": "blob", "sha": None}
                   for path in paths]
        payload = self._must("POST", "/git/trees",
                             {"base_tree": base_tree, "tree": entries})
        sha = payload.get("sha")
        if not sha:
            raise GitHubError("POST trees 响应缺少 sha")
        return sha

    def create_commit(self, tree_sha, parent_sha, message):
        """基于新 tree 与 parent 建 commit，返回 commit sha"""
        payload = self._must("POST", "/git/commits", {
            "message": message,
            "tree": tree_sha,
            "parents": [parent_sha],
        })
        sha = payload.get("sha")
        if not sha:
            raise GitHubError("POST commits 响应缺少 sha")
        return sha

    def update_branch(self, commit_sha):
        """把分支 ref 快进到新 commit；非快进（422）抛 NonFastForwardError"""
        status, text = self._call("PATCH",
                                  "/git/refs/heads/%s" % self._branch_path(),
                                  {"sha": commit_sha, "force": False})
        if status in (200, 201):
            return
        if status == 422:
            raise NonFastForwardError(
                "PATCH ref 被拒（分支已前进）：%s" % github_message(status, text))
        raise GitHubError("PATCH ref 失败：%s" % github_message(status, text))


# ---------------------------------------------------------------------------
# 编排：把一批路径压成一次（或几次）commit
# ---------------------------------------------------------------------------

def delete_paths_in_one_commit(deleter, paths, message=None,
                               batch_size=DEFAULT_BATCH_SIZE, verify=True,
                               dry_run=False):
    """删除 paths：每 batch_size 条路径产生一个 commit

    :param deleter: GitDataDeleter
    :param paths: 仓库内相对路径列表（内部会去重）
    :param message: commit 说明；缺省按删除数量生成
    :param batch_size: 每个 commit 删除多少条（1..MAX_BATCH_SIZE）
    :param verify: True 时先拉 recursive tree，跳过不存在的路径（不产生空 commit）
    :param dry_run: True 时只探测与打印，不发 POST / PATCH
    :return: dict {"requested", "deleted", "missing", "batches", "commits"}
    :raises GitHubError: 调用失败 / 重试耗尽
    """
    targets = dedupe(paths)
    result = {"requested": len(targets), "deleted": [], "missing": [],
              "batches": [], "commits": []}
    if not targets:
        print("没有待删除路径，无事可做")
        return result

    total_batches = (len(targets) + batch_size - 1) // batch_size
    for index, chunk in enumerate(chunks(targets, batch_size), 1):
        print("-------- 第 %d/%d 批：%d 个路径 --------"
              % (index, total_batches, len(chunk)))
        attempt = 0
        while True:
            attempt += 1
            head = deleter.get_head_commit()
            tree_sha = deleter.get_commit_tree(head)
            batch_paths = chunk
            missing = []
            if verify:
                existing = deleter.list_tree_paths(tree_sha)
                batch_paths = [path for path in chunk if path in existing]
                missing = [path for path in chunk if path not in existing]
                if missing:
                    print("⚠️ 本批有 %d 个路径在 %s@%s 上不存在，跳过（不产生空 commit）"
                          % (len(missing), deleter.repo, deleter.branch))
                    log_path_list("不存在的路径", missing)
            if not batch_paths:
                result["missing"].extend(missing)
                break

            commit_message = message or ("[BatchDelete] delete %d files"
                                         % len(batch_paths))
            if dry_run:
                print("🧪 [dry-run] 将基于 commit=%s tree=%s 删除 %d 个文件"
                      "（不发写请求）：" % (head[:8], tree_sha[:8], len(batch_paths)))
                log_path_list("待删除路径", batch_paths)
                result["deleted"].extend(batch_paths)
                result["missing"].extend(missing)
                result["batches"].append({"attempts": attempt,
                                          "deleted": len(batch_paths),
                                          "tree": None, "commit": None,
                                          "dry_run": True})
                break

            new_tree = deleter.create_tree(tree_sha, batch_paths)
            new_commit = deleter.create_commit(new_tree, head, commit_message)
            try:
                deleter.update_branch(new_commit)
            except NonFastForwardError as error:
                if attempt > deleter.retries:
                    raise GitHubError(
                        "非快进冲突重试 %d 次仍失败：%s" % (deleter.retries, error))
                wait = RETRY_BACKOFF_SECONDS * attempt
                print("⚠️ %s；重取 ref 后重试（第 %d/%d 次，等待 %.1fs）"
                      % (error, attempt, deleter.retries, wait))
                time.sleep(wait)
                continue

            print("🗑️ 已提交删除：commit=%s，删除 %d 个文件（tree=%s，尝试 %d 次）"
                  % (new_commit[:8], len(batch_paths), new_tree[:8], attempt))
            result["deleted"].extend(batch_paths)
            result["missing"].extend(missing)
            result["batches"].append({"attempts": attempt,
                                      "deleted": len(batch_paths),
                                      "tree": new_tree, "commit": new_commit})
            result["commits"].append(new_commit)
            break
    return result


def print_summary(result, dry_run=False):
    """输出汇总（与 Task16 现有日志风格保持一致）"""
    print("======== 批量删除结束 ========")
    print("合计：请求 %d 个路径，%s %d 个，跳过（不存在）%d 个，commit %d 条"
          % (result["requested"],
             "计划删除" if dry_run else "已删除",
             len(result["deleted"]), len(result["missing"]),
             0 if dry_run else len(result["commits"])))
    if dry_run:
        log_path_list("计划删除清单（dry-run，未执行）", result["deleted"])
        return
    log_path_list("删除成功清单", result["deleted"])
    for item in result["batches"]:
        print("    批次明细：commit=%s 删除 %d 个（尝试 %d 次）"
              % ((item["commit"] or "-")[:8], item["deleted"], item["attempts"]))
    if result["commits"]:
        print("提示：本次共 %d 条 commit，可在分支历史里按 commit message 追溯"
              % len(result["commits"]))


# ---------------------------------------------------------------------------
# 自测：本地假 GitHub 服务（完全离线）
# ---------------------------------------------------------------------------

def run_self_test():
    """起一个内存版 GitHub Git Data API，验证 4 步流程 / 分批 / 冲突重试 / dry-run

    :return: 进程退出码（0 = 全部通过）
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    failures = []

    def check(name, condition, detail=""):
        print("%s %s%s" % ("PASS" if condition else "FAIL", name,
                           ("  ← " + str(detail)) if detail and not condition else ""))
        if not condition:
            failures.append(name)

    class Handler(BaseHTTPRequestHandler):
        files = {}                 # path -> blob sha
        pending = {}               # tree sha -> 待 PATCH 成功后才生效的删除路径
        commit_sha = "commit-0"
        tree_sha = "tree-0"
        commits = []               # [{sha, message, tree, parents}]
        calls = []                 # [(method, path)]
        conflict_times = 0         # >0 时 PATCH 返回 422（模拟分支前进）
        truncate_tree = False

        def log_message(self, *args):
            pass

        def reply(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode())

        def _path(self):
            return urllib.parse.urlsplit(self.path).path

        def do_GET(self):
            path = self._path()
            Handler.calls.append(("GET", path))
            if "/git/ref/heads/" in path:
                self.reply(200, {"ref": "refs/heads/quote",
                                 "object": {"sha": Handler.commit_sha}})
                return
            if "/git/commits/" in path:
                sha = path.rsplit("/", 1)[1]
                if sha != Handler.commit_sha:
                    self.reply(404, {"message": "Not Found"})
                    return
                self.reply(200, {"sha": sha, "tree": {"sha": Handler.tree_sha}})
                return
            if "/git/trees/" in path:
                tree = [{"path": name, "type": "blob", "mode": "100644", "sha": sha}
                        for name, sha in sorted(Handler.files.items())]
                self.reply(200, {"sha": Handler.tree_sha, "tree": tree,
                                 "truncated": Handler.truncate_tree})
                return
            self.reply(404, {"message": "Not Found"})

        def do_POST(self):
            path = self._path()
            body = self._body()
            Handler.calls.append(("POST", path))
            if path.endswith("/git/trees"):
                Handler.tree_sha = "tree-%d" % (len(Handler.calls))
                # 真 GitHub：改 tree 不影响分支内容，只有 PATCH ref 成功才落地；
                # 假服务用 pending 模拟这个时序（否则重试用例会误判为已删除）
                Handler.pending[Handler.tree_sha] = [
                    entry.get("path") for entry in body.get("tree") or []
                    if entry.get("sha") is None]
                self.reply(201, {"sha": Handler.tree_sha})
                return
            if path.endswith("/git/commits"):
                Handler.commit_sha = "commit-%d" % (len(Handler.calls))
                Handler.commits.append({"sha": Handler.commit_sha,
                                        "message": body.get("message"),
                                        "tree": body.get("tree"),
                                        "parents": body.get("parents")})
                self.reply(201, {"sha": Handler.commit_sha})
                return
            self.reply(404, {"message": "Not Found"})

        def do_PATCH(self):
            path = self._path()
            body = self._body()
            Handler.calls.append(("PATCH", path))
            if Handler.conflict_times > 0:
                Handler.conflict_times -= 1
                self.reply(422, {"message": "Update is not a fast forward"})
                return
            # PATCH 成功 = 新 commit 落地：此时才把该 tree 的删除应用到工作区
            for commit in Handler.commits:
                if commit["sha"] == body.get("sha"):
                    for path in Handler.pending.pop(commit["tree"], []):
                        Handler.files.pop(path, None)
                    break
            self.reply(200, {"ref": "refs/heads/quote",
                             "object": {"sha": body.get("sha")}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    api_base = "http://127.0.0.1:%d" % server.server_port

    def reset(files, conflict_times=0, truncate_tree=False):
        Handler.files = dict(files)
        Handler.pending = {}
        Handler.commit_sha = "commit-0"
        Handler.tree_sha = "tree-0"
        Handler.commits = []
        Handler.calls = []
        Handler.conflict_times = conflict_times
        Handler.truncate_tree = truncate_tree

    def new_deleter(retries=DEFAULT_RETRIES):
        return GitDataDeleter("ACANX", "Distribution", "quote", "token",
                              api_base=api_base, retries=retries)

    try:
        # 1) 正常：3 个文件一次 commit 删完
        reset({"a.mvsv": "s1", "b.mvsv": "s2", "keep.mvsv": "s3"})
        result = delete_paths_in_one_commit(
            new_deleter(), ["a.mvsv", "b.mvsv"], "delete two")
        check("一次 commit 删多个文件",
              len(result["commits"]) == 1 and sorted(Handler.files) == ["keep.mvsv"],
              (result["commits"], sorted(Handler.files)))
        methods = [call[0] for call in Handler.calls]
        check("调用序列 = GET ref/commit/tree + POST tree/commit + PATCH",
              methods == ["GET", "GET", "GET", "POST", "POST", "PATCH"], methods)
        check("未使用 Contents API",
              all("/contents/" not in call[1] for call in Handler.calls),
              Handler.calls)
        check("commit message 生效",
              Handler.commits and Handler.commits[-1]["message"] == "delete two",
              Handler.commits)

        # 2) 虚构路径（--verify 默认开）→ 跳过，不产生空 commit
        reset({"a.mvsv": "s1"})
        result = delete_paths_in_one_commit(
            new_deleter(), ["a.mvsv", "not-exist.mvsv"])
        check("verify 跳过不存在的路径",
              result["missing"] == ["not-exist.mvsv"]
              and result["deleted"] == ["a.mvsv"]
              and len(result["commits"]) == 1, result)
        reset({"a.mvsv": "s1"})
        result = delete_paths_in_one_commit(new_deleter(), ["ghost.mvsv"])
        check("全部路径都不存在时不产生空 commit",
              result["commits"] == [] and result["missing"] == ["ghost.mvsv"], result)

        # 3) 分批：7 个路径 + batch_size=3 → 3 个 commit
        reset({"f%d.mvsv" % i: "s%d" % i for i in range(7)})
        result = delete_paths_in_one_commit(
            new_deleter(), ["f%d.mvsv" % i for i in range(7)], batch_size=3)
        check("分批删除：3 批 = 3 条 commit",
              len(result["commits"]) == 3 and Handler.files == {}, result["commits"])

        # 4) 非快进冲突 → 重试后成功
        reset({"a.mvsv": "s1"}, conflict_times=1)
        result = delete_paths_in_one_commit(new_deleter(), ["a.mvsv"])
        check("非快进冲突重试后成功",
              len(result["commits"]) == 1
              and result["batches"][0]["attempts"] == 2, result["batches"])

        # 5) dry-run：不发 POST / PATCH
        reset({"a.mvsv": "s1"})
        result = delete_paths_in_one_commit(
            new_deleter(), ["a.mvsv"], dry_run=True)
        check("dry-run 不发写请求",
              [call[0] for call in Handler.calls] == ["GET", "GET", "GET"]
              and "a.mvsv" in Handler.files
              and result["deleted"] == ["a.mvsv"], Handler.calls)

        # 6) tree 被截断 → 报错（不会静默误删）
        reset({"a.mvsv": "s1"}, truncate_tree=True)
        try:
            delete_paths_in_one_commit(new_deleter(), ["a.mvsv"])
            check("tree 截断时报错", False)
        except GitHubError as error:
            check("tree 截断时报错", "truncated" in str(error), error)
        reset({"a.mvsv": "s1"})
        result = delete_paths_in_one_commit(new_deleter(), ["a.mvsv"], verify=False)
        check("--no-verify 时不拉 tree 直接删",
              result["commits"] and Handler.files == {}, result)

        # 7) 去重：同一路径重复出现只删一次
        reset({"a.mvsv": "s1", "b.mvsv": "s2"})
        result = delete_paths_in_one_commit(
            new_deleter(), ["a.mvsv", "a.mvsv", " b.mvsv "])
        check("路径去重（含首尾空格）",
              result["requested"] == 2 and len(result["deleted"]) == 2, result)
    finally:
        server.shutdown()

    print("\n%s" % ("自测全部通过" if not failures
                    else "自测失败 %d 项：%s" % (len(failures), failures)))
    return EXIT_OK if not failures else EXIT_FAILED


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="GitHubGitDataBatchDelete.py",
        description="用 GitHub Git Data API 一次 commit 批量删除多个文件（试验工具）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="令牌从环境变量 GIT_COMMIT_TOKEN 读取（需 contents:write）。")
    parser.add_argument("--repo", help="owner/repo，如 ACANX/Distribution")
    parser.add_argument("--branch", help="要删除文件的分支，如 quote（必填）")
    parser.add_argument("--path", action="append", default=[],
                        help="仓库内相对路径，可重复")
    parser.add_argument("--paths-file",
                        help="路径清单文件（一行一个，忽略空行与 # 注释）")
    parser.add_argument("--message",
                        help="commit 说明（默认按删除数量自动生成）")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help="每批（= 每 commit）删除数量，默认 %d，上限 %d"
                             % (DEFAULT_BATCH_SIZE, MAX_BATCH_SIZE))
    parser.add_argument("--max-delete", type=int, default=DEFAULT_MAX_DELETE,
                        help="单次允许删除的路径数上限，默认 %d" % DEFAULT_MAX_DELETE)
    parser.add_argument("--verify", dest="verify", action="store_true",
                        default=True,
                        help="先拉 recursive tree 过滤不存在的路径（默认开启）")
    parser.add_argument("--no-verify", dest="verify", action="store_false",
                        help="跳过存在性校验（tree 过大 / 被截断时使用）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印计划，不发写请求")
    parser.add_argument("--api-base", default=os.environ.get("GIT_API_BASE", ""),
                        help="API 根（默认 %s，也可用 GIT_API_BASE）"
                             % DEFAULT_API_BASE)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="单请求超时秒数，默认 %d" % DEFAULT_TIMEOUT)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                        help="非快进冲突重试次数，默认 %d" % DEFAULT_RETRIES)
    parser.add_argument("--self-test", action="store_true",
                        help="运行本地离线自测（不访问 GitHub）")
    return parser


def main(argv=None):
    _ensure_console_utf8()
    args = build_parser().parse_args(argv)

    if args.self_test:
        return run_self_test()

    if not args.repo:
        print("❌ 缺少 --repo owner/repo")
        return EXIT_BAD_ARGS
    if not args.branch:
        print("❌ 缺少 --branch（不提供默认分支，避免误删）")
        return EXIT_BAD_ARGS
    try:
        owner, repo = split_repo_slug(args.repo)
    except ValueError as error:
        print("❌ %s" % error)
        return EXIT_BAD_ARGS

    paths = list(args.path)
    if args.paths_file:
        try:
            paths.extend(read_paths_from_file(args.paths_file))
        except OSError as error:
            print("❌ 读取 --paths-file 失败：%s" % error)
            return EXIT_BAD_ARGS
    paths = dedupe(paths)
    if not paths:
        print("没有待删除路径（--path / --paths-file 均为空），无事可做")
        return EXIT_OK

    if args.batch_size < 1:
        print("❌ --batch-size 必须 ≥ 1")
        return EXIT_BAD_ARGS
    if args.batch_size > MAX_BATCH_SIZE:
        print("⚠️ --batch-size=%d 超过上限，按 %d 处理"
              % (args.batch_size, MAX_BATCH_SIZE))
        args.batch_size = MAX_BATCH_SIZE
    if len(paths) > args.max_delete:
        print("❌ 待删除 %d 个路径，超过 --max-delete=%d；"
              "确认无误后调大该上限（或分批执行）"
              % (len(paths), args.max_delete))
        return EXIT_TOO_MANY

    api_base = normalize_api_base(args.api_base)

    print("======== Git Data API 批量删除 ========")
    print("仓库 = %s/%s，分支 = %s" % (owner, repo, args.branch))
    print("待删除路径 = %d 个，每批 %d 个，存在性校验 = %s，dry-run = %s"
          % (len(paths), args.batch_size,
             "开" if args.verify else "关", "是" if args.dry_run else "否"))
    print("API 根 = %s" % api_base)
    log_path_list("待删除路径清单", paths)

    token = os.environ.get("GIT_COMMIT_TOKEN", "").strip()
    if args.dry_run:
        print("🧪 dry-run：不会发送任何 POST / PATCH 请求")
    elif not token:
        print("❌ 缺少 GIT_COMMIT_TOKEN 环境变量（需要 contents:write）")
        return EXIT_BAD_ARGS

    deleter = GitDataDeleter(owner, repo, args.branch, token or "-",
                             api_base=api_base, timeout=args.timeout,
                             retries=args.retries)

    try:
        result = delete_paths_in_one_commit(
            deleter, paths, message=args.message,
            batch_size=args.batch_size, verify=args.verify,
            dry_run=args.dry_run)
    except GitHubError as error:
        print("❌ %s" % error)
        return EXIT_FAILED

    print_summary(result, dry_run=args.dry_run)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
