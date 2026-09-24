#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task05 源端清理自测 —— 转存送达后删除源文件, 且绝不删错仓库。

背景：Task05 把本仓库(ACANX/Distribution)归档的 .jsonl 转存到**另一个仓库**
(acdnx/Distribution)，转存送达后要把源文件从本仓库删掉。这里有三处容易出错：

    1. 仓库身份。.github/Python/Commit.json 登记的 Owner/Repo 正是
       **acdnx/Distribution**(转存端)，而 GitHubCommitContent._resolve_target 的
       默认解析链会取到它 —— 删除目标若走那条链，删掉的是转存端，与"清理源端"正好
       相反。源端身份只能从本仓库 .git/config 解析。
    2. 送达判据。转存端已有同一份内容(sha 相同)而跳过提交的，同样算已送达，源文件
       该删；提交失败的则一律保留，留待下次重跑。
    3. 幂等。源端已无此文件时视为删除成功，重复运行不报错。

本自测把 _request / _get_file_sha / commit_content_file 全换成假实现，配合临时 git
仓库运行：**不发任何网络请求，不碰真实数据**。

跑法：
    python3 tests/archiveTransferDeleteSourceSelfTest.py
"""

import contextlib
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".github", "Python", "Quote"))

T5 = importlib.import_module("Task05ArchiveSecuQuoteExecLogJsonlTransfer")

FAILS = []

# Contents API URL: {base}/{owner}/{repo}/contents/{path}[?ref={branch}]
_URL_RE = re.compile(r"/repos/([^/]+)/([^/]+)/contents/(.+?)(?:\?ref=(.*))?$")


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name,
                       ("  ← " + str(detail)) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------------------
# 假 GitHub：只实现本脚本用到的三个动作（查 sha / PUT 提交 / DELETE 删除）
# ---------------------------------------------------------------------------


class FakeGitHub:
    def __init__(self):
        self.remote = {}            # 转存端: path -> blob sha
        self.source = {}            # 源端:   path -> blob sha
        self.puts = []              # (owner, repo, path)
        self.deleted = []           # (owner, repo, branch, path) 实际发出的 DELETE
        self.delete_attempts = []   # 所有 DELETE 尝试(含失败的)
        self.fail_put = set()       # 这些 path 的 PUT 返回失败
        self.fail_delete = set()    # 这些 path 的 DELETE 返回失败

    # 对应 GitHubCommitContent._get_file_sha(api_base, owner, repo, path, branch, token, timeout)
    def get_sha(self, api_base, owner, repo, path_key, branch, token, timeout):
        if (owner, repo) == (T5.TARGET_OWNER, T5.TARGET_REPO):
            return self.remote.get(path_key)
        return self.source.get(path_key)

    # 对应 GitHubCommitContent.commit_content_file(...)
    def commit(self, path_key, local_file, branch=None, commit_msg=None,
               owner=None, repo=None, token=None, api_base=None, timeout=30):
        if path_key in self.fail_put:
            return {"success": False, "message": "模拟提交失败",
                    "path": path_key, "http_status": 500}
        self.puts.append((owner, repo, path_key))
        self.remote[path_key] = T5.local_blob_sha(Path(local_file))
        return {"success": True, "message": None,
                "path": path_key, "http_status": 201}

    # 对应 GitHubCommitContent._request(method, url, headers, body_bytes, timeout)
    def request(self, method, url, headers, body_bytes=None, timeout=30):
        m = _URL_RE.search(url)
        owner, repo = m.group(1), m.group(2)
        path = urllib.parse.unquote(m.group(3))
        body = json.loads(body_bytes) if body_bytes else {}
        if method != "DELETE":
            return 200, "{}", None
        self.delete_attempts.append((owner, repo, body.get("branch"), path))
        if path in self.fail_delete:
            return 403, json.dumps({"message": "模拟删除失败"}), None
        if self.source.get(path) != body.get("sha"):
            return 409, json.dumps({"message": "sha 不匹配"}), None
        del self.source[path]
        self.deleted.append((owner, repo, body.get("branch"), path))
        return 200, "{}", None


# ---------------------------------------------------------------------------
# 隔离环境
# ---------------------------------------------------------------------------


def make_repo():
    """临时 git 仓库（分支 quote，含一个提交），充当"本仓库"的检出。"""
    root = Path(tempfile.mkdtemp(prefix="archive_transfer_selftest_")).resolve()
    git = lambda *a: subprocess.run(["git"] + list(a), cwd=str(root), check=True,
                                    capture_output=True, text=True)
    git("init")
    git("checkout", "-b", "quote")
    git("config", "user.email", "selftest@local")
    git("config", "user.name", "selftest")
    (root / "README.md").write_text("x\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "init")
    return root


def write_source(root, name, body="{\"ts\": 1}\n"):
    """在临时仓库的归档目录下放一个源文件。"""
    d = root / T5.ARCHIVE_ROOT
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(body, encoding="utf-8")
    return p


def rel_of(name):
    return (T5.ARCHIVE_ROOT / name).as_posix()


def run_transfer(gh, root, argv=()):
    """在假 GitHub + 临时仓库上跑一次 main()，返回退出码。

    main() 的 stdout 收进 gh.output —— 免得刷屏，也便于断言脚本自己报了什么。
    """
    saved = (T5._request, T5._get_file_sha, T5.commit_content_file, T5.repo_root)
    saved_argv = sys.argv
    saved_token = os.environ.get(T5.TOKEN_ENV)
    T5._request = gh.request
    T5._get_file_sha = gh.get_sha
    T5.commit_content_file = gh.commit
    T5.repo_root = lambda: root
    sys.argv = ["Task05"] + list(argv)
    os.environ[T5.TOKEN_ENV] = "selftest-token"
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            T5.main()
        return 0
    except SystemExit as e:
        return e.code if e.code is not None else 0
    finally:
        gh.output = buf.getvalue()
        (T5._request, T5._get_file_sha, T5.commit_content_file, T5.repo_root) = saved
        sys.argv = saved_argv
        if saved_token is None:
            os.environ.pop(T5.TOKEN_ENV, None)
        else:
            os.environ[T5.TOKEN_ENV] = saved_token


def no_wrong_repo(gh, label):
    """所有 DELETE 都必须打在源端(本仓库)，一条都不能落到转存端 acdnx。"""
    wrong = [c for c in gh.delete_attempts
             if (c[0], c[1]) == (T5.TARGET_OWNER, T5.TARGET_REPO)]
    check("%s：删除目标全部落在源端(没有打到转存端 %s/%s)"
          % (label, T5.TARGET_OWNER, T5.TARGET_REPO), not wrong, wrong)


# ---------------------------------------------------------------------------
# 一、送达即删
# ---------------------------------------------------------------------------


def testDeliveredSourceDeleted():
    gh, root = FakeGitHub(), make_repo()
    try:
        name = "LOG_Finv_SecuQuoteExecLog_DAY_Lambda_20260915.jsonl"
        write_source(root, name)
        gh.source[rel_of(name)] = "deadbeef"

        code = run_transfer(gh, root)
        check("送达即删：退出码 0", code == 0, code)
        check("送达即删：转存端收到提交", gh.puts and gh.puts[0][2] == rel_of(name), gh.puts)
        check("送达即删：源文件已从源端删除", rel_of(name) not in gh.source,
              sorted(gh.source))
        check("送达即删：删除打在源端且带分支名",
              gh.deleted and gh.deleted[0][3] == rel_of(name) and gh.deleted[0][2] == "quote",
              gh.deleted)
        check("送达即删：脚本自报了删除动作", "已删除源端" in gh.output, gh.output[-300:])
        no_wrong_repo(gh, "送达即删")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def testSkippedAlsoDeleted():
    """转存端已有同一份内容(跳过提交)也算送达，源文件同样要删。"""
    gh, root = FakeGitHub(), make_repo()
    try:
        name = "LOG_Finv_SecuQuoteExecLog_DAY_Lambda_20260916.jsonl"
        p = write_source(root, name)
        gh.remote[rel_of(name)] = T5.local_blob_sha(p)   # 转存端内容与本地一致
        gh.source[rel_of(name)] = "deadbeef"

        code = run_transfer(gh, root)
        check("已送达跳过提交：退出码 0", code == 0, code)
        check("已送达跳过提交：没有再 PUT 一次", not gh.puts, gh.puts)
        check("已送达跳过提交：源文件仍被删除", rel_of(name) not in gh.source,
              sorted(gh.source))
        no_wrong_repo(gh, "已送达跳过提交")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def testFailedPutKeepsSource():
    gh, root = FakeGitHub(), make_repo()
    try:
        bad = "LOG_Finv_SecuQuoteExecLog_DAY_Lambda_20260917.jsonl"
        ok = "LOG_Finv_SecuQuoteExecLog_DAY_Lambda_20260918.jsonl"
        write_source(root, bad)
        write_source(root, ok)
        gh.source[rel_of(bad)] = "deadbeef"
        gh.source[rel_of(ok)] = "deadbeef"
        gh.fail_put.add(rel_of(bad))

        code = run_transfer(gh, root)
        check("提交失败：退出码 1(留待下次重跑)", code == 1, code)
        check("提交失败：失败的源文件被保留", rel_of(bad) in gh.source, sorted(gh.source))
        check("提交失败：同轮成功的源文件照常删除", rel_of(ok) not in gh.source,
              sorted(gh.source))
        check("提交失败：没有对未送达文件发 DELETE",
              rel_of(bad) not in [c[3] for c in gh.delete_attempts],
              gh.delete_attempts)
        no_wrong_repo(gh, "提交失败")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 二、幂等与失败处理
# ---------------------------------------------------------------------------


def testAlreadyGoneIsIdempotent():
    """源端已经没有了(上次删过)：视为已删除，不发 DELETE，也不算失败。"""
    gh, root = FakeGitHub(), make_repo()
    try:
        name = "LOG_Finv_SecuQuoteExecLog_DAY_Lambda_20260919.jsonl"
        write_source(root, name)          # 本地还在(脚本不碰本地工作区)
        gh.remote[rel_of(name)] = T5.local_blob_sha(root / T5.ARCHIVE_ROOT / name)
        # gh.source 里没有它 —— 模拟远端已删

        code = run_transfer(gh, root)
        check("源端已无：退出码 0(幂等)", code == 0, code)
        check("源端已无：不重复发 DELETE", not gh.delete_attempts, gh.delete_attempts)
        check("源端已无：本地工作区文件未被脚本碰掉",
              (root / T5.ARCHIVE_ROOT / name).exists())
    finally:
        shutil.rmtree(root, ignore_errors=True)


def testRepeatRunIsIdempotent():
    """连跑两轮：第二轮源端和转存端都已是终态，不再有任何动作。"""
    gh, root = FakeGitHub(), make_repo()
    try:
        name = "LOG_Finv_SecuQuoteExecLog_DAY_Lambda_20260920.jsonl"
        write_source(root, name)
        gh.source[rel_of(name)] = "deadbeef"

        run_transfer(gh, root)
        check("连跑两轮：第一轮删掉了源文件", rel_of(name) not in gh.source)
        puts_after_first = len(gh.puts)

        # 第二轮：转存端已是同一份内容 → 跳过提交；源端已无 → 不发 DELETE
        gh.delete_attempts.clear()
        code = run_transfer(gh, root)
        check("连跑两轮：第二轮退出码 0", code == 0, code)
        check("连跑两轮：第二轮不再提交", len(gh.puts) == puts_after_first, gh.puts)
        check("连跑两轮：第二轮不再发 DELETE", not gh.delete_attempts, gh.delete_attempts)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def testDeleteFailureReported():
    gh, root = FakeGitHub(), make_repo()
    try:
        name = "LOG_Finv_SecuQuoteExecLog_DAY_Lambda_20260921.jsonl"
        write_source(root, name)
        gh.source[rel_of(name)] = "deadbeef"
        gh.fail_delete.add(rel_of(name))

        code = run_transfer(gh, root)
        check("删除失败：退出码 1(源文件会留到下次重跑)", code == 1, code)
        check("删除失败：源文件确实还在", rel_of(name) in gh.source, sorted(gh.source))
        no_wrong_repo(gh, "删除失败")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 三、dry-run 与 limit
# ---------------------------------------------------------------------------


def testDryRunDeletesNothing():
    gh, root = FakeGitHub(), make_repo()
    try:
        name = "LOG_Finv_SecuQuoteExecLog_DAY_Lambda_20260922.jsonl"
        write_source(root, name)
        gh.source[rel_of(name)] = "deadbeef"

        code = run_transfer(gh, root, ["--dry-run"])
        check("dry-run：退出码 0", code == 0, code)
        check("dry-run：不提交", not gh.puts, gh.puts)
        check("dry-run：不删除", not gh.delete_attempts, gh.delete_attempts)
        check("dry-run：源文件原样保留", rel_of(name) in gh.source)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def testLimitOnlyDeletesProcessed():
    gh, root = FakeGitHub(), make_repo()
    try:
        names = ["LOG_Finv_SecuQuoteExecLog_DAY_Lambda_2026092%d.jsonl" % i
                 for i in (3, 4, 5)]
        for n in names:
            write_source(root, n)
            gh.source[rel_of(n)] = "deadbeef"

        code = run_transfer(gh, root, ["--limit", "1"])
        check("limit：退出码 0", code == 0, code)
        check("limit：只处理了 1 个(只提交 1 个)", len(gh.puts) == 1, gh.puts)
        check("limit：只删了处理过的那个", len(gh.deleted) == 1, gh.deleted)
        check("limit：未处理的源文件保留",
              rel_of(names[1]) in gh.source and rel_of(names[2]) in gh.source,
              sorted(gh.source))
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 四、源端身份来源
# ---------------------------------------------------------------------------


def testSourceBranchResolvesToRepoBranch():
    """源端分支取当前检出分支(临时仓库建成 quote)。"""
    root = make_repo()
    saved_root = T5.repo_root
    T5.repo_root = lambda: root
    try:
        check("源端分支：取当前检出分支 quote", T5.source_branch() == "quote",
              T5.source_branch())
    finally:
        T5.repo_root = saved_root
        shutil.rmtree(root, ignore_errors=True)


def testSourceOwnerIsThisRepo():
    """源端身份必须是本仓库，且不能等于转存端 acdnx/Distribution。

    直接调用真实的 load_owner_repo_from_git_config()（读本仓库 .git/config），
    确认它能给出与 TARGET_* 不同的身份 —— 这正是"必须用它而不是 TARGET_*"的理由。
    """
    owner, repo = T5.load_owner_repo_from_git_config()
    check("源端身份：能从本仓库 .git/config 解析出 owner/repo", bool(owner and repo),
          (owner, repo))
    check("源端身份：源端 != 转存端(证明两者是彼此独立的仓库)",
          (owner, repo) != (T5.TARGET_OWNER, T5.TARGET_REPO),
          "源端 %s/%s vs 转存端 %s/%s"
          % (owner, repo, T5.TARGET_OWNER, T5.TARGET_REPO))


def main():
    testDeliveredSourceDeleted()
    testSkippedAlsoDeleted()
    testFailedPutKeepsSource()
    testAlreadyGoneIsIdempotent()
    testRepeatRunIsIdempotent()
    testDeleteFailureReported()
    testDryRunDeletesNothing()
    testLimitOnlyDeletesProcessed()
    testSourceBranchResolvesToRepoBranch()
    testSourceOwnerIsThisRepo()
    print("-" * 60)
    if FAILS:
        print("FAILED %d: %s" % (len(FAILS), "、".join(FAILS)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
