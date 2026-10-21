#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task16 自建行情服务同步（PG）自测 —— 钉住与代理约定。

被测脚本：.github/Python/Quote/Task16DayMvsvFileDataSyncPG.py

本自测把口径钉死：
    字段校验   —— 只下发接口定义过的 17 列；usc/ts 缺失或非法整行丢弃；
                  越界 date/time、负 volume/turnover、NaN 字段丢弃并上报；
    前收价     —— FTMM 的 LastClose 必须映射成接口字段 prev_close；
    批量语义   —— 413 自动二分且落库行数守恒；5xx/429 退避重试；4xx 立即失败不重试；
                  update 模式下 affected 小于提交行数判失败，skip 模式允许；
    容错解析   —— 单行脏数据只丢该行，不放弃整份文件（状态记为 partial）；
                  短名文件头等不可映射情形直接报错，不发写请求；
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
    """假目标服务：可注入 503 次数 / 413 阈值 / affected 回传值"""

    def __init__(self):
        class Handler(BaseHTTPRequestHandler):
            calls = []
            fail_5xx = 0
            reject_over = None
            affected = None
            status_4xx = None

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(
                    int(self.headers.get("Content-Length") or 0)).decode())
                items = body["items"]
                Handler.calls.append((self.path, items))
                if Handler.status_4xx:
                    self.send_response(Handler.status_4xx)
                    self.end_headers()
                    self.wfile.write(b'{"detail":"rejected"}')
                    return
                if Handler.fail_5xx > 0:
                    Handler.fail_5xx -= 1
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b'{"detail":"db down"}')
                    return
                if Handler.reject_over and len(items) > Handler.reject_over:
                    self.send_response(413)
                    self.end_headers()
                    self.wfile.write(b'{"detail":"too many"}')
                    return
                affected = Handler.affected if Handler.affected is not None \
                    else len(items)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"affected": affected,
                                             "mode": "update"}).encode())

        self.handler = Handler
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_port
        self.calls = Handler.calls

    def reset(self, fail_5xx=0, reject_over=None, affected=None,
              status_4xx=None):
        self.calls.clear()
        self.handler.fail_5xx = fail_5xx
        self.handler.reject_over = reject_over
        self.handler.affected = affected
        self.handler.status_4xx = status_4xx
        return self

    def stop(self):
        self.httpd.shutdown()


def test_sanitize():
    row = {"ts": 1761000000000, "date": 20261020, "time": 93000, "open": 10.1,
           "high": 10.5, "low": 9.9, "close": 10.2, "volume": 1000,
           "turnover": 10200.5, "change_price": 0.2, "change_ratio": 2.0,
           "prev_close": 10.0, "usc": "01810", "provider": "FTMM"}
    item, problems = PG.sanitize_item(row)
    check("正常行无问题", problems == [] and item["usc"] == "01810"
          and item["prev_close"] == 10.0)
    check("字段顺序固定",
          list(item) == [k for k in PG.ITEM_FIELDS if k in row], list(item))

    item, problems = PG.sanitize_item({"ts": 1, "usc": "A", "last_close": 1.0,
                                       "weird": 1})
    check("未知字段被丢弃并记录",
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


def test_client(server):
    PG.RETRY_BACKOFF_SECONDS = 0.01
    client = PG.QuoteSyncClient(server.base)

    server.reset()
    result = client.upsert([{"usc": "A", "ts": 1}, {"usc": "B", "ts": 2}])
    check("正常 Upsert 结果",
          result == {"affected": 2, "mode": "update", "requests": 1}, result)
    check("请求路径与 onConflict",
          server.calls[-1][0] == "/API/V1/Meta/Finv/Quote/Secu/KLine/Min/Upsert"
                                "?onConflict=update", server.calls[-1][0])

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
          PG.parse_upsert_response('{"affected":1,"mode":"skip"}', 3)["affected"] == 1)
    check("非 JSON 响应不崩",
          PG.parse_upsert_response("oops", 2)["affected"] is None)

    server.reset(status_4xx=422)
    try:
        client.upsert([{"usc": "A", "ts": 7}])
        check("422 立即失败不重试", False)
    except PG.SyncRequestError as e:
        check("422 立即失败不重试", len(server.calls) == 1 and "422" in str(e),
              (len(server.calls), e))

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
    check("前收价映射为 prev_close",
          column_types["LastClose"][0] == "prev_close"
          and column_types["前收价"][0] == "prev_close")

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
    check("前收价落到 prev_close",
          all(it["prev_close"] == 10.0 for c in server.calls for it in c[1]))

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


def test_env_and_exit_code(server):
    check("URL 缺协议补 http",
          PG.normalize_base_url("1.2.3.4:18001") == "http://1.2.3.4:18001")
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

    check("ok -> 退出码 0", run_main("ok", False)[0] == 0)
    check("partial -> 退出码 1（默认严格）", run_main("partial", False)[0] == 1)
    code, out = run_main("partial", True)
    check("partial + ALLOW_PARTIAL -> 退出码 0", code == 0 and "部分同步清单" in out,
          (code, out[-200:]))
    check("failed -> 退出码 1（即便允许 partial）", run_main("failed", True)[0] == 1)


def main():
    PG._ensure_console_utf8()
    server = FakeServer()
    tmp = tempfile.mkdtemp()
    try:
        test_sanitize()
        test_client(server)
        test_sync_file(server, tmp)
        test_env_and_exit_code(server)
    finally:
        server.stop()
    print("\n%s" % ("全部通过" if not FAILS else "失败 %d 项：%s" % (len(FAILS), FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
