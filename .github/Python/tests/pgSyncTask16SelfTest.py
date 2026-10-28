#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task16 自建行情服务同步（PG）自测 —— 钉住与代理约定。

被测脚本：.github/Python/Quote/Task16DayMvsvFileDataSyncPG.py

本自测把口径钉死：
    字段校验   —— 只下发接口定义过的 17 列；usc/ts 缺失或非法整行丢弃；
                  越界 date/time、负 volume/turnover、NaN 字段丢弃并上报；
                  未知字段（如旧名 prev_close）会被服务端 422 / 211005，客户端一律丢弃；
                  volume 已改为 numeric(20,6)，小数成交量原样下发不截断；
    前收价     —— FTMM 的 LastClose 必须映射成接口字段 last_close（需求 10794 改名）；
    批量语义   —— 413 自动二分且落库行数守恒；5xx/429 退避重试；4xx 立即失败不重试；
                  error.retryable=true 的 4xx 会重试；
                  成功信封 {code, data, message} 的 code 仅 0/1/200/202 算成功；
                  update 模式下 affected 小于提交行数判失败，skip 模式允许；
    容错解析   —— 单行脏数据只丢该行，不放弃整份文件（状态记为 partial）；
                  短名文件头等不可映射情形直接报错，不发写请求；
    源文件删除 —— 默认安全模式不删；开关开启后只删「全部批次成功」的文件，
                  failed 不删、partial 需 PG_SYNC_ALLOW_PARTIAL 放行；
                  Contents API 先 GET sha 再 DELETE，令牌缺失 / 接口报错都按失败退出；
    退出码     —— failed → 1；partial 默认 1，PG_SYNC_ALLOW_PARTIAL=true 时 0。

跑法（仅标准库，起本地假服务端，不碰真实数据与真实服务器）：
    python3 .github/Python/tests/pgSyncTask16SelfTest.py
"""

import io
import json
import os
import sys
import tempfile
import threading
import urllib.parse
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# 本文件位于 <根>/.github/Python/tests/, 其本身也在 .github/Python 之下,
# 故仓库根 = .github/Python 再上溯一级 = dirname(dirname(dirname(HERE)))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(ROOT, ".github", "Python", "Quote"))

import Task16DayMvsvFileDataSyncPG as PG  # noqa: E402

FAILS = []
FILE_REL = "Archive/Finv/SecuQuote/FTMM/V5/HK_MAIN_01810_KLine_FTMM_Day_20261020.mvsv"


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name,
                       ("  ← " + str(detail)) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def error_envelope(code, message, request_id, retryable=False):
    """按外部 OpenAPI 契约拼失败响应信封（非 2xx 时不再套一层 data）"""
    return {
        "code": code,
        "message": message,
        "detail": message,
        "request_id": request_id,
        "error": {"code": code, "catalog_version": "finvquant-1",
                  "retryable": retryable},
    }


def mvsv_header(field_names, chinese=True):
    """按全名头（# FieldName）+ 中文名头（# 字段名称）拼文件头"""
    lines = ["# FieldName : %s" % "|".join(field_names)]
    if chinese:
        lines.append("# 字段名称 : 时间戳|日期|时间|开盘价|最高价|最低价|收盘价"
                     "|成交量|成交额|涨跌值|涨跌幅|前收价")
    lines += ['# SecuCode : "01810"', "# DataProvider : FTMM", ""]
    return "\n".join(lines) + "\n"


LONG_NAMES = ("Timestamp|Date|Time|Open|High|Low|Close|Volume|Turnover"
              "|ChangePrice|ChangeRatio|LastClose")


def data_line(ts, time=93000, date=20261020):
    return ("%d|%d|%d|10.1|10.5|9.9|10.2|1000|10200.5|0.2|2.0|10.0"
            % (ts, date, time))


class FakeServer:
    """假目标服务：可注入 503 次数 / 413 阈值 / affected / 业务错误码 / 重试标记

    响应形状按外部 OpenAPI 契约：成功统一为 {code, data, message} 信封，
    失败为非 2xx + 错误信封 {code, message, detail, request_id, error}。
    """

    def __init__(self):
        class Handler(BaseHTTPRequestHandler):
            calls = []
            fail_5xx = 0
            reject_over = None
            affected = None
            status_4xx = None
            retryable_4xx = 0
            success_code = 0
            mode = "update"

            def log_message(self, *args):
                pass

            def reply(self, status, payload):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(
                    int(self.headers.get("Content-Length") or 0)).decode())
                items = body["items"]
                Handler.calls.append((self.path, items))
                # 模拟线上严格字段校验（encoding/json DisallowUnknownFields）：
                # 未知字段 → 422 / 211005（KLineUnknownField）。白名单与契约
                # KLineItem 一致（需求 10794：last_close / pacd，旧名已废）。
                allowed = {"usc", "ts", "date", "time", "open", "high", "low",
                           "close", "last_close", "volume", "turnover",
                           "change_price", "change_ratio", "pacd", "ext_field",
                           "remark", "provider"}
                for item in items:
                    if sorted(set(item) - allowed):
                        self.reply(422, error_envelope(
                            211005, "请求包含未知字段", "rid-unknown"))
                        return
                if Handler.retryable_4xx > 0:
                    Handler.retryable_4xx -= 1
                    self.reply(422, error_envelope(
                        211303, "数据库约束冲突", "rid-retry", retryable=True))
                    return
                if Handler.status_4xx:
                    self.reply(Handler.status_4xx, error_envelope(
                        211003, "字段类型不匹配", "rid-4xx"))
                    return
                if Handler.fail_5xx > 0:
                    Handler.fail_5xx -= 1
                    self.reply(503, error_envelope(
                        211302, "数据库不可用", "rid-503", retryable=True))
                    return
                if Handler.reject_over and len(items) > Handler.reject_over:
                    self.reply(413, error_envelope(211201, "items 超过上限",
                                                   "rid-413"))
                    return
                affected = Handler.affected if Handler.affected is not None \
                    else len(items)
                self.reply(200, {"code": Handler.success_code,
                                 "data": {"affected": affected,
                                          "mode": Handler.mode},
                                 "message": "OK"})

        self.handler = Handler
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_port
        self.calls = Handler.calls

    def reset(self, fail_5xx=0, reject_over=None, affected=None,
              status_4xx=None, retryable_4xx=0, success_code=0, mode="update"):
        self.calls.clear()
        self.handler.fail_5xx = fail_5xx
        self.handler.reject_over = reject_over
        self.handler.affected = affected
        self.handler.status_4xx = status_4xx
        self.handler.retryable_4xx = retryable_4xx
        self.handler.success_code = success_code
        self.handler.mode = mode
        return self

    def stop(self):
        self.httpd.shutdown()


def test_sanitize():
    row = {"ts": 1761000000000, "date": 20261020, "time": 93000, "open": 10.1,
           "high": 10.5, "low": 9.9, "close": 10.2, "volume": 1000,
           "turnover": 10200.5, "change_price": 0.2, "change_ratio": 2.0,
           "last_close": 10.0, "usc": "01810", "provider": "FTMM"}
    item, problems = PG.sanitize_item(row)
    check("正常行无问题", problems == [] and item["usc"] == "01810"
          and item["last_close"] == 10.0)
    check("字段顺序固定",
          list(item) == [k for k in PG.ITEM_FIELDS if k in row], list(item))

    item, problems = PG.sanitize_item({"ts": 1, "usc": "A", "prev_close": 1.0,
                                       "weird": 1})
    check("未知字段（含已改名的 prev_close）被丢弃并记录",
          item == {"usc": "A", "ts": 1} and len(problems) == 2, (item, problems))

    check("缺 usc 整行丢弃", PG.sanitize_item({"ts": 1})[0] is None)
    check("缺 ts 整行丢弃", PG.sanitize_item({"usc": "A"})[0] is None)
    check("负 ts 整行丢弃", PG.sanitize_item({"usc": "A", "ts": -5})[0] is None)

    item, problems = PG.sanitize_item(
        {"usc": "A", "ts": 1, "date": 18000101, "time": 240000, "volume": -1,
         "close": float("nan"), "provider": "P" * 256})
    check("越界/非法可选字段被丢弃",
          set(item) == {"usc", "ts"} and len(problems) == 5, (item, problems))

    item, _ = PG.sanitize_item({"usc": "A", "ts": 1, "close": "10.35",
                                "time": "93000"})
    check("字符串数值可转换",
          item == {"usc": "A", "ts": 1, "close": 10.35, "time": 93000}, item)

    item, problems = PG.sanitize_item({"usc": "A", "ts": 1, "volume": 44.0})
    check("volume 为整数形式也原样保留",
          item == {"usc": "A", "ts": 1, "volume": 44.0} and problems == [],
          (item, problems))
    item, problems = PG.sanitize_item({"usc": "A", "ts": 1, "volume": 44.5})
    check("volume 小数不再截断（numeric(20,6)）",
          item["volume"] == 44.5 and problems == [], (item, problems))


def test_client(server):
    PG.RETRY_BACKOFF_SECONDS = 0.01
    client = PG.QuoteSyncClient(server.base)

    server.reset()
    result = client.upsert([{"usc": "A", "ts": 1}, {"usc": "B", "ts": 2}])
    check("正常 Upsert 结果（取自信封 data 节点）",
          result == {"affected": 2, "mode": "update", "requests": 1}, result)
    check("请求路径与 onConflict",
          server.calls[-1][0] == PG.API_UPSERT_PATH + "?onConflict=update",
          server.calls[-1][0])

    for code in (0, 1, 200, 202):
        server.reset(success_code=code)
        check("信封 code=%d 视为成功" % code,
              client.upsert([{"usc": "A", "ts": 3}])["affected"] == 1)

    server.reset(success_code=211003)
    try:
        client.upsert([{"usc": "A", "ts": 4}])
        check("2xx 但 code 非成功码判失败", False)
    except PG.SyncRequestError as e:
        check("2xx 但 code 非成功码判失败", "code=211003" in str(e), e)

    server.reset(fail_5xx=2)
    check("5xx 重试后成功", client.upsert([{"usc": "A", "ts": 3}])["affected"] == 1)

    server.reset(fail_5xx=99)
    try:
        client.upsert([{"usc": "A", "ts": 4}])
        check("重试耗尽抛错", False)
    except PG.SyncRequestError as e:
        check("重试耗尽抛错", "重试 2 次" in str(e), e)

    server.reset(reject_over=2)
    result = client.upsert([{"usc": "S%d" % i, "ts": i} for i in range(5)])
    check("413 自动二分", result["affected"] == 5, result)
    accepted = [len(c[1]) for c in server.calls if len(c[1]) <= 2]
    check("拆分后落库行数守恒", sum(accepted) == 5, accepted)

    server.reset(affected=1)
    try:
        client.upsert([{"usc": "A", "ts": 5}, {"usc": "A", "ts": 6}])
        check("update 模式 affected 偏小判失败", False)
    except PG.SyncRequestError as e:
        check("update 模式 affected 偏小判失败", "affected" in str(e), e)
    check("skip 模式允许 affected 偏小",
          PG.parse_upsert_response('{"code":0,"data":{"affected":1,'
                                   '"mode":"skip"},"message":"OK"}',
                                   3)["affected"] == 1)
    check("信封解析 affected / mode",
          PG.parse_upsert_response('{"code":0,"data":{"affected":3,'
                                   '"mode":"update"},"message":"OK"}', 3)
          == {"affected": 3, "mode": "update", "requests": 1})
    check("旧版平铺结构兼容",
          PG.parse_upsert_response('{"affected":1,"mode":"skip"}', 3)
          ["affected"] == 1)
    check("非 JSON 响应不崩",
          PG.parse_upsert_response("oops", 2)["affected"] is None)
    check("信封缺 data 节点不崩",
          PG.parse_upsert_response('{"code":0,"message":"OK"}', 2)
          ["affected"] is None)

    server.reset(status_4xx=422)
    try:
        client.upsert([{"usc": "A", "ts": 7}])
        check("422 立即失败不重试", False)
    except PG.SyncRequestError as e:
        check("422 立即失败不重试", len(server.calls) == 1 and "422" in str(e),
              (len(server.calls), e))
        check("错误信封带 code / detail / request_id",
              "code=211003" in str(e) and "字段类型不匹配" in str(e)
              and "rid-4xx" in str(e), e)

    server.reset(retryable_4xx=2)
    result = client.upsert([{"usc": "A", "ts": 8}])
    check("error.retryable=true 的 4xx 会重试",
          result["affected"] == 1 and len(server.calls) == 3,
          (result, len(server.calls)))

    detail, retryable = PG.parse_error_response(
        500, json.dumps(error_envelope(211301, "数据库错误", "rid-500",
                                       retryable=True)))
    check("错误信封解析 retryable",
          retryable and "code=211301" in detail and "rid-500" in detail, detail)
    detail, retryable = PG.parse_error_response(400, "not json")
    check("非 JSON 错误体降级不崩",
          detail == "HTTP 400：not json" and retryable is False, detail)

    server.reset()
    try:
        client.upsert([{"usc": "A", "ts": 9, "prev_close": 1.0}])
        check("旧字段名 prev_close 会被判未知字段（回归护栏）", False)
    except PG.SyncRequestError as e:
        check("旧字段名 prev_close 会被判未知字段（回归护栏）",
              "211005" in str(e), e)

    try:
        PG.QuoteSyncClient(server.base, on_conflict="bad")
        check("非法 onConflict 报错", False)
    except ValueError:
        check("非法 onConflict 报错", True)


def test_sync_file(server, tmp):
    path = os.path.join(tmp, *FILE_REL.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    client = PG.QuoteSyncClient(server.base)
    column_types = PG.build_column_types()
    check("前收价映射为 last_close",
          column_types["LastClose"][0] == "last_close"
          and column_types["前收价"][0] == "last_close")

    with open(path, "w", encoding="utf-8") as f:
        f.write(mvsv_header(LONG_NAMES.split("|")))
        for i in range(5):
            f.write(data_line(17610000000000 + i) + "\n")
    server.reset()
    outcome = PG.sync_file(client, tmp, FILE_REL, 2, column_types)
    check("完整同步状态为 ok", outcome["status"] == "ok", outcome["status"])
    check("批次数 = ceil(5/2)", len(outcome["batches"]) == 3,
          len(outcome["batches"]))
    check("提交总行数 = 5",
          sum(b["submitted"] for b in outcome["batches"]) == 5)
    check("下发字段都在接口白名单内",
          all(set(it) <= PG.ITEM_FIELD_SET for c in server.calls for it in c[1]))
    check("usc 取自文件头且去引号",
          all(it["usc"] == "01810" for c in server.calls for it in c[1]))
    check("provider 取自文件头",
          all(it["provider"] == "FTMM" for c in server.calls for it in c[1]))
    check("前收价落到 last_close",
          all(it["last_close"] == 10.0 for c in server.calls for it in c[1]))
    check("volume 按数值原样下发（支持小数）",
          all(it["volume"] == 1000.0
              for c in server.calls for it in c[1]),
          [it["volume"] for c in server.calls for it in c[1]][:5])

    with open(path, "w", encoding="utf-8") as f:
        f.write(mvsv_header(LONG_NAMES.split("|")))
        f.write("not-a-ts|20261020|93000|10.1|10.5|9.9|10.2|1000|10200.5|0.2|2.0|10.0\n")
        f.write(data_line(17610000000001, date=18000101, time=240000) + "\n")
        for i in range(2, 5):
            f.write(data_line(17610000000000 + i) + "\n")
    server.reset()
    outcome = PG.sync_file(client, tmp, FILE_REL, 2, column_types)
    check("脏行只丢该行", outcome["status"] == "partial"
          and outcome["dropped_rows"] == 1, outcome["status"])
    check("越界字段记为 partial", outcome["dropped_fields"] == 1,
          outcome["dropped_fields"])
    check("其余 4 行仍下发",
          sum(b["submitted"] for b in outcome["batches"]) == 4,
          [b["submitted"] for b in outcome["batches"]])

    with open(path, "w", encoding="utf-8") as f:
        f.write(mvsv_header(("ts", "d", "t", "o", "h", "l", "c", "v", "t", "cp",
                             "cr", "lc"), chinese=False))
        f.write(data_line(17610000000000) + "\n")
    server.reset()
    try:
        PG.sync_file(client, tmp, FILE_REL, 2, column_types)
        check("短名头无法映射时报错", False)
    except ValueError as e:
        check("短名头无法映射时报错",
              "列名无法识别" in str(e) and not server.calls, e)

    with open(path, "w", encoding="utf-8") as f:
        f.write(mvsv_header(LONG_NAMES.split("|")))
        f.write("x1|20261020|93000|10.1|10.5|9.9|10.2|1000|10200.5|0.2|2.0|10.0\n")
    server.reset()
    try:
        PG.sync_file(client, tmp, FILE_REL, 2, column_types)
        check("全部行不可用时抛错", False)
    except ValueError as e:
        check("全部行不可用时抛错", "无可下发数据" in str(e), e)


class FakeGitHub:
    """假 GitHub Contents API：GET 取 sha，DELETE 删除并记录调用"""

    def __init__(self):
        class Handler(BaseHTTPRequestHandler):
            exists = set()
            calls = []
            delete_status = 200

            def log_message(self, *args):
                pass

            def reply(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _file_path(self):
                path = urllib.parse.urlsplit(self.path).path
                marker = "/contents/"
                if marker not in path:
                    return None
                return urllib.parse.unquote(path.split(marker, 1)[1])

            def do_GET(self):
                path_key = self._file_path()
                Handler.calls.append(("GET", path_key, None))
                if path_key not in Handler.exists:
                    self.reply(404, {"message": "Not Found"})
                    return
                self.reply(200, {"sha": "sha-%s" % path_key, "path": path_key})

            def do_DELETE(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode()) if length else {}
                path_key = self._file_path()
                Handler.calls.append(("DELETE", path_key, body))
                if Handler.delete_status != 200:
                    self.reply(Handler.delete_status, {"message": "boom"})
                    return
                Handler.exists.discard(path_key)
                self.reply(200, {"commit": {"sha": "c1"}, "content": None})

        self.handler = Handler
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d/repos" % self.httpd.server_port

    def reset(self, exists=(), delete_status=200):
        self.handler.exists = set(exists)
        self.handler.delete_status = delete_status
        self.handler.calls.clear()

    def stop(self):
        self.httpd.shutdown()


def test_repo_slug(tmp):
    check("split_repo_slug 基本",
          PG.split_repo_slug("ACANX/Distribution") == ("ACANX", "Distribution"))
    check("split_repo_slug 带 .git 与斜杠",
          PG.split_repo_slug("/ACANX/Distribution.git/") == ("ACANX", "Distribution"))
    check("split_repo_slug 非法", PG.split_repo_slug("onlyone") == (None, None))
    check("https remote 解析",
          PG.split_repo_from_url("https://github.com/ACANX/Distribution.git")
          == ("ACANX", "Distribution"))
    check("ssh remote 解析",
          PG.split_repo_from_url("git@github.com:ACANX/Distribution.git")
          == ("ACANX", "Distribution"))
    check("空 remote 解析", PG.split_repo_from_url("") == (None, None))

    git_dir = os.path.join(tmp, ".git")
    os.makedirs(git_dir, exist_ok=True)
    with open(os.path.join(git_dir, "config"), "w", encoding="utf-8") as f:
        f.write('[core]\n\tbare = false\n[remote "origin"]\n'
                '\turl = git@github.com:ACANX/Distribution.git\n'
                '\tfetch = +refs/heads/*:refs/remotes/origin/*\n')
    os.environ.pop("GIT_REPO_SLUG", None)
    check("从 .git/config 解析 owner/repo",
          PG.load_owner_repo(tmp) == ("ACANX", "Distribution"),
          PG.load_owner_repo(tmp))
    os.environ["GIT_REPO_SLUG"] = "Other/Repo"
    check("GIT_REPO_SLUG 优先", PG.load_owner_repo(tmp) == ("Other", "Repo"))
    os.environ.pop("GIT_REPO_SLUG", None)
    check("无 .git/config 返回空",
          PG.load_owner_repo(os.path.join(tmp, "nope")) == (None, None))


def test_delete_remote_file(gh):
    path_key = "Archive/Finv/SecuQuote/FTMM/V5/A/G/A_G_Min_FTMM_Day_20261020.mvsv"
    gh.reset(exists={path_key})
    result = PG.delete_remote_file(gh.base, "ACANX", "Distribution", "quote",
                                   path_key, "token")
    check("删除成功", result["success"] and not result["already_gone"], result)
    check("先 GET 取 sha 再 DELETE",
          [call[0] for call in gh.handler.calls] == ["GET", "DELETE"],
          gh.handler.calls)
    body = gh.handler.calls[-1][2]
    check("DELETE body 带 message/sha/branch",
          body.get("sha") == "sha-%s" % path_key and body.get("branch") == "quote"
          and "delete synced" in body.get("message", ""), body)

    gh.reset(exists=())
    result = PG.delete_remote_file(gh.base, "ACANX", "Distribution", "quote",
                                   path_key, "token")
    check("文件已不存在视为已处理",
          result["success"] and result["already_gone"], result)

    gh.reset(exists={path_key})
    result = PG.delete_remote_file(gh.base, "ACANX", "Distribution", "quote",
                                   path_key, "")
    check("缺令牌直接失败",
          (not result["success"]) and "GIT_COMMIT_TOKEN" in result["message"],
          result)
    check("缺令牌不发请求", gh.handler.calls == [], gh.handler.calls)

    gh.reset(exists={path_key}, delete_status=500)
    result = PG.delete_remote_file(gh.base, "ACANX", "Distribution", "quote",
                                   path_key, "token")
    check("DELETE 报错按失败返回",
          (not result["success"]) and "HTTP 500" in result["message"], result)


def test_main_delete(server, gh):
    """main() 整链路：删除开关 / 删除范围（ok 才删、failed 不删、partial 需放行）"""
    PG.RETRY_BACKOFF_SECONDS = 0.01
    ok_path = "Archive/Finv/SecuQuote/FTMM/V5/A/G/A_G_Min_FTMM_Day_20261020.mvsv"
    bad_path = "Archive/Finv/SecuQuote/FTMM/V5/A/B/A_B_Min_FTMM_Day_20261020.mvsv"
    part_path = "Archive/Finv/SecuQuote/FTMM/V5/A/P/A_P_Min_FTMM_Day_20261020.mvsv"

    orig_collect, orig_sync = PG.collect_mvsv_files, PG.sync_file
    os.environ["URL_QUOTE_SYNC_BASE"] = server.base
    os.environ["GIT_API_BASE"] = gh.base
    os.environ["GIT_REPO_SLUG"] = "ACANX/Distribution"
    os.environ["CURR_BRANCH"] = "quote"

    def run(status_map, enable_delete=True, allow_partial=False, token="token"):
        def fake_sync(client, root, rel_path, batch_size, column_types):
            return {"rel_path": rel_path, "status": status_map[rel_path],
                    "failure_digest": "d"}

        PG.collect_mvsv_files = lambda root: [(path, 20261020, "x")
                                              for path in status_map]
        PG.sync_file = fake_sync
        if enable_delete is None:
            os.environ.pop("PG_SYNC_ENABLE_DELETE", None)      # 走脚本默认值
        else:
            os.environ["PG_SYNC_ENABLE_DELETE"] = "true" if enable_delete else "false"
        if allow_partial:
            os.environ["PG_SYNC_ALLOW_PARTIAL"] = "true"
        else:
            os.environ.pop("PG_SYNC_ALLOW_PARTIAL", None)
        if token is None:
            os.environ.pop("GIT_COMMIT_TOKEN", None)
        else:
            os.environ["GIT_COMMIT_TOKEN"] = token
        gh.reset(exists=status_map)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = PG.main()
        return code, buf.getvalue(), set(gh.handler.exists)

    try:
        code, out, left = run({ok_path: "ok"}, enable_delete=False)
        check("开关关闭：不删且退出 0",
              code == 0 and left == {ok_path} and "删除开关关闭" in out,
              (code, left))

        code, out, left = run({ok_path: "ok"})
        delete_bodies = [call[2] for call in gh.handler.calls if call[0] == "DELETE"]
        check("开关开启：完整同步的文件被删除",
              code == 0 and left == set() and "已删除源文件" in out, (code, left))
        check("删除打到 CURR_BRANCH 指定的分支",
              [body.get("branch") for body in delete_bodies] == ["quote"],
              delete_bodies)

        code, out, left = run({ok_path: "ok"}, enable_delete=None)
        check("未设置开关时默认删除（默认值开启）",
              code == 0 and left == set() and "删除开关 (enableDelete) = 开" in out,
              (code, left))

        code, out, left = run({ok_path: "ok"}, enable_delete=False)
        check("显式 false 时只列清单不删",
              code == 0 and left == {ok_path}, (code, left))

        code, out, left = run({ok_path: "ok", bad_path: "failed"})
        check("失败文件保留、其余删除且退出 1",
              code == 1 and left == {bad_path}, (code, left))

        code, out, left = run({part_path: "partial"})
        check("partial 默认不删且退出 1",
              code == 1 and left == {part_path}, (code, left))

        code, out, left = run({part_path: "partial"}, allow_partial=True)
        check("partial + ALLOW_PARTIAL：删除并退出 0",
              code == 0 and left == set(), (code, left))

        code, out, left = run({ok_path: "ok"}, token=None)
        check("开关开启但缺令牌：退出 1 且不删",
              code == 1 and left == {ok_path} and "GIT_COMMIT_TOKEN" in out,
              (code, left))

        gh.reset(exists={ok_path}, delete_status=500)
        PG.collect_mvsv_files = lambda root: [(ok_path, 20261020, "x")]
        PG.sync_file = lambda *a, **k: {"rel_path": ok_path, "status": "ok",
                                        "failure_digest": "d"}
        os.environ["PG_SYNC_ENABLE_DELETE"] = "true"
        os.environ["GIT_COMMIT_TOKEN"] = "token"
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = PG.main()
        check("删除失败：退出 1 且文件保留",
              code == 1 and ok_path in gh.handler.exists
              and "删除失败清单" in buf.getvalue(), code)
    finally:
        PG.collect_mvsv_files, PG.sync_file = orig_collect, orig_sync
        for name in ("GIT_API_BASE", "GIT_REPO_SLUG", "GIT_COMMIT_TOKEN",
                     "PG_SYNC_ENABLE_DELETE", "PG_SYNC_ALLOW_PARTIAL",
                     "CURR_BRANCH"):
            os.environ.pop(name, None)


def test_env_and_exit_code(server):
    check("URL 缺协议补 http",
          PG.normalize_base_url("1.2.3.4:16001") == "http://1.2.3.4:16001")
    check("URL 去尾斜杠", PG.normalize_base_url("http://h:1/") == "http://h:1")
    for bad in ("   ", "ftp://h", "http://"):
        try:
            PG.normalize_base_url(bad)
            check("非法 URL 报错：%r" % bad, False)
        except ValueError:
            check("非法 URL 报错：%r" % bad, True)

    os.environ.pop("PG_SYNC_ALLOW_PARTIAL", None)
    os.environ["URL_QUOTE_SYNC_BASE"] = ""
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = PG.main()
    check("缺 URL 退出码 1 且提示变量名",
          code == 1 and "URL_QUOTE_SYNC_BASE" in buf.getvalue(), code)

    def run_main(status, allow_partial):
        os.environ["URL_QUOTE_SYNC_BASE"] = server.base
        os.environ.pop("PG_SYNC_ALLOW_PARTIAL", None)
        # 本用例只验证退出码，显式关闭删除（脚本默认值是开）
        os.environ["PG_SYNC_ENABLE_DELETE"] = "false"
        if allow_partial:
            os.environ["PG_SYNC_ALLOW_PARTIAL"] = "true"
        orig_collect = PG.collect_mvsv_files
        orig_sync = PG.sync_file
        PG.collect_mvsv_files = lambda root: [(FILE_REL, 20261020, "X")]
        PG.sync_file = lambda *a, **k: {"status": status, "failure_digest": "d",
                                        "rel_path": FILE_REL}
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                return PG.main(), out.getvalue()
        finally:
            PG.collect_mvsv_files = orig_collect
            PG.sync_file = orig_sync
            os.environ.pop("PG_SYNC_ALLOW_PARTIAL", None)
            os.environ.pop("PG_SYNC_ENABLE_DELETE", None)

    check("ok -> 退出码 0", run_main("ok", False)[0] == 0)
    check("partial -> 退出码 1（默认严格）", run_main("partial", False)[0] == 1)
    code, out = run_main("partial", True)
    check("partial + ALLOW_PARTIAL -> 退出码 0", code == 0 and "部分同步清单" in out,
          (code, out[-200:]))
    check("failed -> 退出码 1（即便允许 partial）", run_main("failed", True)[0] == 1)


def main():
    PG._ensure_console_utf8()
    server = FakeServer()
    gh = FakeGitHub()
    tmp = tempfile.mkdtemp()
    try:
        test_sanitize()
        test_client(server)
        test_sync_file(server, tmp)
        test_repo_slug(tmp)
        test_delete_remote_file(gh)
        test_main_delete(server, gh)
        test_env_and_exit_code(server)
    finally:
        server.stop()
        gh.stop()
    print("\n%s" % ("全部通过" if not FAILS else "失败 %d 项：%s" % (len(FAILS), FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
