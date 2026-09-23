#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VerifyQuoteMinuteSyncConfig 的本地自测：删除相关的纯逻辑 + 假 RepoAccess 跑一遍消费路径。

不联网、不写库：RepoAccess 用假的替身（记录被删路径），SupabaseClient 用假的替身。
用法：python3 tests/syncDeleteSelfTest.py
"""

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), ".github", "Python"))

import VerifyQuoteMinuteSyncConfig as M  # noqa: E402


FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name, ("  ← " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def testListMvsvCarriesSha():
    """listMvsv 必须把 blob sha 带回来（删除要用），且按路径升序。"""
    class Fake(M.RepoAccess):
        def __init__(self):
            self.branch = "quote-meta"

        def _listByTree(self_):
            return [("Verify/Success/SZ/01320.mvsv", "sha-a"),
                    ("Verify/Success/HK/00700.mvsv", "sha-b"),
                    ("Verify/Success/SZ/notes.txt", "sha-c"),
                    ("Verify/Fail/SZ/09999.mvsv", "sha-d"),
                    ("Verify/Success/SZ", "sha-tree")]
    got = Fake().listMvsv("Verify/Success")
    check("listMvsv 带 sha 且按路径升序",
          got == [("Verify/Success/HK/00700.mvsv", "sha-b"), ("Verify/Success/SZ/01320.mvsv", "sha-a")],
          repr(got))


def testLocalFallbackShaIsNone():
    """本地兜底没有 blob sha，一律 None（删除时现查）。"""
    root = tempfile.mkdtemp(prefix="mvsv-selftest-")
    cwd = os.getcwd()
    try:
        target = os.path.join(root, "Verify", "Success", "SZ")
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "01320.mvsv"), "w", encoding="utf-8") as handle:
            handle.write("x")
        os.chdir(root)
        got = M.localSuccessFiles("Verify/Success")
        check("localSuccessFiles 返回 (路径, None)",
              got == [("Verify/Success/SZ/01320.mvsv", None)], repr(got))
    finally:
        os.chdir(cwd)
        shutil.rmtree(root, ignore_errors=True)


def testFailedUscs():
    """块级失败按 uscOf 折算回 usc；折算不出的主键单独报告。"""
    batches = [
        {"table": M.FUTU_TABLE, "conflict": M.FUTU_KEY, "updates": [], "inserts": [],
         "uscOf": {"87836376173009": "01320", "111": "01322"}},
        {"table": M.SECU_TABLE, "conflict": M.SECU_KEY, "updates": [], "inserts": [],
         "uscOf": {"01316": "01316"}},
    ]
    blocked, orphan = M.failedUscs(batches, [
        (M.FUTU_TABLE, "更新", ["87836376173009"], "boom"),
        (M.SECU_TABLE, "更新", ["01316", "99999"], "boom"),
    ])
    check("failedUscs 折算 usc", blocked == {"01320", "01316"}, repr(blocked))
    check("failedUscs 报出折算不出的主键", orphan == [(M.SECU_TABLE, "99999")], repr(orphan))


def testPlanConsumption():
    """planConsumption：同步完成即删（含「无需变更」的行），未命中/写库失败/缺文件一律保留。"""
    rows = [("01320", {}, [{"table": M.FUTU_TABLE}]),
            ("01322", {}, [{"table": M.SECU_TABLE}]),
            ("01324", {}, [{"table": M.SECU_TABLE}]),
            ("01326", {}, [{"table": M.FUTU_TABLE}])]
    pending = [("01326", {}, ["MISS"])]
    early = [("01328", {}, ["MISS"])]
    sourceOf = {usc: "Verify/Success/SZ/%s.mvsv" % usc
                for usc in ("01320", "01322", "01324", "01326", "01328")}
    shaOf = {usc: "sha-" + usc for usc in sourceOf}

    consumed, retained = M.planConsumption(rows, pending, early, {"01322"}, sourceOf, shaOf)
    check("planConsumption 待删 = 已完成同步的行（含无需变更）",
          [(usc, path) for usc, path, _sha in consumed]
          == [("01320", "Verify/Success/SZ/01320.mvsv"), ("01324", "Verify/Success/SZ/01324.mvsv")],
          repr(consumed))
    check("planConsumption 带出 sha",
          [sha for _usc, _path, sha in consumed] == ["sha-01320", "sha-01324"], repr(consumed))
    check("planConsumption 写库失败 → 保留", ("01322", "写库失败，须保留待下一轮重试") in retained,
          repr(retained))
    check("planConsumption 未命中 → 保留",
          sum(1 for usc, why in retained if usc == "01326" and "MisMatch" in why) == 1, repr(retained))
    check("planConsumption 映射不可用 → 保留",
          ("01328", "映射不可用，已留痕到 %s/" % M.MISMATCH_DIR) in retained, repr(retained))

    gone = M.planConsumption([("09999", {}, [])], [], [], set(), {}, {})
    check("planConsumption 清单里找不到文件 → 保留",
          gone == ([], [("09999", "清单里找不到对应文件")]), repr(gone))


def testConsumeSuccessWithFakeRepo():
    """consumeSuccess：逐文件删除、成功计数、失败进 failures 且不中断。"""
    calls = []

    class FakeRepo:
        def deleteFile(self, path, sha, message):
            calls.append((path, sha, message))
            if path.endswith("01322.mvsv"):
                return {"success": False, "message": "HTTP 409 sha 不匹配"}
            return {"success": True, "message": "删除成功", "http_status": 200}

    failures = []
    done, bad = M.consumeSuccess(FakeRepo(),
                                 [("01320", "Verify/Success/SZ/01320.mvsv", "sha-a"),
                                  ("01322", "Verify/Success/SZ/01322.mvsv", "sha-b")],
                                 failures)
    check("consumeSuccess 计数", (done, bad) == (1, 1), "%r" % ((done, bad),))
    check("consumeSuccess 传了 sha 与提交信息",
          calls[0] == ("Verify/Success/SZ/01320.mvsv", "sha-a", "Consumed 01320.mvsv"), repr(calls[0]))
    check("consumeSuccess 失败进 failures",
          failures == [("Verify/Success/SZ/01322.mvsv", "HTTP 409 sha 不匹配")], repr(failures))


def testDeleteContentShaRequiresShaOrLookup():
    """delete_content：给了 sha 就不该再查一次（不产生 GET）。"""
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), ".github", "Python"))
    import GitHubCommitContent as G

    seen = []

    def fakeRequest(method, url, headers, data=None, timeout=30):
        seen.append(method)
        if method == "DELETE":
            return 200, '{"commit": {"sha": "deadbeef"}}', None
        return 404, "", None

    original = G._request
    G._request = fakeRequest
    try:
        result = G.delete_content("Verify/Success/SZ/01320.mvsv", branch="quote-meta",
                                  owner="ACANX", repo="Distribution", token="t",
                                  sha="sha-a")
    finally:
        G._request = original
    check("delete_content 带 sha 时只发一次 DELETE", seen == ["DELETE"], repr(seen))
    check("delete_content 判定成功", result.get("success") is True, repr(result))


def testDeleteContentMissingFileIsSuccess():
    """未传 sha 时先 GET；GET 到 404（文件已不在）按成功返回，且不再发 DELETE。"""
    import GitHubCommitContent as G

    seen = []

    def fakeRequest(method, url, headers, data=None, timeout=30):
        seen.append(method)
        return 404, '{"message": "Not Found"}', None

    original = G._request
    G._request = fakeRequest
    try:
        result = G.delete_content("Verify/Success/SZ/01320.mvsv", branch="quote-meta",
                                  owner="ACANX", repo="Distribution", token="t")
    finally:
        G._request = original
    check("文件已不在时只查一次 sha（不发 DELETE）", seen == ["GET"], repr(seen))
    check("文件已不在按成功返回", result.get("success") is True and result.get("http_status") == 404,
          repr(result))


def testDeleteContentShaMismatchIsFailure():
    """sha 失配（遭人抢先改过）：GitHub 返 409，按失败返回、不误删。"""
    import GitHubCommitContent as G

    def fakeRequest(method, url, headers, data=None, timeout=30):
        return 409, '{"message": "sha does not match"}', None

    original = G._request
    G._request = fakeRequest
    try:
        result = G.delete_content("Verify/Success/SZ/01320.mvsv", branch="quote-meta",
                                  owner="ACANX", repo="Distribution", token="t", sha="stale")
    finally:
        G._request = original
    check("sha 失配按失败返回",
          result.get("success") is False and "sha does not match" in (result.get("message") or ""),
          repr(result))


def main():
    testListMvsvCarriesSha()
    testLocalFallbackShaIsNone()
    testFailedUscs()
    testPlanConsumption()
    testConsumeSuccessWithFakeRepo()
    testDeleteContentShaRequiresShaOrLookup()
    testDeleteContentMissingFileIsSuccess()
    testDeleteContentShaMismatchIsFailure()
    print("-" * 60)
    if FAILS:
        print("FAILED %d: %s" % (len(FAILS), "、".join(FAILS)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
