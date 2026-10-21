#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task17 自测 —— Supabase 存量迁移到自建行情服务（PG）的编排与安全口径。

被测脚本：.github/Python/Quote/Task17SupabaseToPgMigrate.py

本自测把口径钉死：
    分批规则   —— 先按「各证券最早 ts」升序取证券清单，再逐证券处理；
                  证券内按 ts 升序、每批只含一个 usc；证券内用 ts 键集分页推进，
                  关闭删除时也能在批次上限内跑完，不在同一批上打转；
    删除语义   —— 只有写入成功的行才按 (usc, ts) 删除；写入失败该批一行不删；
                  删除行数不符默认判失败；
    行校验     —— NULL/缺列静默跳过；非法值记问题；缺 usc/ts 整行保留不迁移，
                  并让该证券记为「待人工处理」；
    批次上限   —— 达到 MIGRATE_MAX_BATCHES 即停，剩余数据留待下次；
    客户端     —— 413 自动二分；5xx 退避重试；4xx 立即失败；update 模式
                  affected 偏小判失败；URL / onConflict / 聚合与回退查询符合约定。

跑法（仅标准库，起本地假目标服务，不碰真实数据与真实服务器）：
    python3 .github/Python/tests/pgMigrateTask17SelfTest.py
"""

import io
import json
import os
import sys
import threading
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(ROOT, ".github", "Python", "Quote"))

import Task17SupabaseToPgMigrate as M  # noqa: E402

FAILS = []
BIG_TS = 10 ** 30


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name,
                       ("  ← " + str(detail)) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def make_row(usc, ts, **extra):
    """构造一条源表记录（默认字段都合法）"""
    row = {"usc": usc, "ts": ts, "date": 20261020, "time": 93000,
           "prev_close": 10.0, "open": 10.1, "high": 10.5, "low": 9.9,
           "close": 10.2, "volume": 1000, "turnover": 10200.5,
           "change_price": 0.2, "change_ratio": 2.0, "provider": "FTMM"}
    row.update(extra)
    return row


class FakeReader:
    """内存版源表：实现 discover_columns / list_securities / fetch_page / delete_keys

    对齐 PostgREST 语义：list_securities 给出每个 usc 的 min(ts)（ts 为空的行排最后）；
    页查询按 ts 升序、ts > after_ts 过滤（因此 ts 为空的行不会被页查询读到）。
    """

    def __init__(self, rows, columns=None):
        self.rows = [dict(r) for r in rows]
        self.columns = columns or self._discover()
        self.list_calls = 0
        self.page_calls = []
        self.delete_calls = []

    def _discover(self):
        columns = set()
        for row in self.rows:
            columns.update(row)
        return columns or {"usc", "ts"}

    def discover_columns(self):
        return set(self.columns)

    def list_securities(self):
        self.list_calls += 1
        first = {}
        for row in self.rows:
            usc = "" if row.get("usc") is None else str(row.get("usc"))
            if not usc.strip():
                continue
            ts = M._as_int(row.get("ts"))
            if usc not in first:
                first[usc] = ts
            elif ts is not None and (first[usc] is None or ts < first[usc]):
                first[usc] = ts
        return sorted(first.items(),
                      key=lambda item: (BIG_TS if item[1] is None else item[1],
                                        item[0]))

    def fetch_page(self, columns, limit, usc, after_ts=None):
        self.page_calls.append({"usc": usc, "after_ts": after_ts, "limit": limit})
        rows = []
        for row in self.rows:
            if str(row.get("usc")) != usc:
                continue
            ts = M._as_int(row.get("ts"))
            if ts is None:
                continue                     # 模拟 ts=gt.X 过滤掉空 ts
            if after_ts is not None and ts <= after_ts:
                continue
            rows.append((ts, row))
        rows.sort(key=lambda item: item[0])
        return [dict(row) for _ts, row in rows[:limit]]

    def delete_keys(self, keys):
        self.delete_calls.append(list(keys))
        wanted = {(str(usc), int(ts)) for usc, ts in keys}
        before = len(self.rows)
        self.rows = [r for r in self.rows
                     if (str(r.get("usc")), M._as_int(r.get("ts"))) not in wanted]
        return before - len(self.rows)


class FakeTarget:
    """内存版目标服务：可注入失败批次 / affected 偏小"""

    def __init__(self):
        self.base_url = "http://fake-target"
        self.batches = []
        self.fail_on = set()          # 第 n 个批次（1 起）抛错
        self.short_affected = False   # 模拟 affected 小于提交行数（update 模式）

    def upsert(self, items):
        self.batches.append([dict(item) for item in items])
        number = len(self.batches)
        submitted = len(items)
        if number in self.fail_on:
            raise M.SyncRequestError("第 %d 次写入模拟失败" % number)
        if self.short_affected:
            raise M.SyncRequestError("回传 affected=%d 小于提交行数 %d"
                                     % (submitted - 1, submitted))
        return {"affected": submitted, "mode": "update", "requests": 1}


def run_migrate(rows, batch_size=100, max_batches=100, enable_delete=True,
                continue_on_error=False, allow_mismatch=False, columns=None):
    """跑一次 migrate，返回 (result, reader, target, 控制台输出)"""
    reader = FakeReader(rows, columns=columns)
    target = FakeTarget()
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        result = M.migrate(target, reader, M.resolve_columns(reader.columns),
                           batch_size, max_batches, enable_delete,
                           continue_on_error, allow_mismatch)
    return result, reader, target, buffer.getvalue()


# ---------------------------------------------------------------------------
# 行校验
# ---------------------------------------------------------------------------

def test_build_item():
    item, problems = M.build_item(make_row("A", 1))
    check("正常行无问题", problems == [] and item["usc"] == "A"
          and item["prev_close"] == 10.0, (item, problems))
    check("字段顺序与接口一致",
          list(item) == [k for k in M.ITEM_FIELDS if k in item], list(item))

    item, problems = M.build_item({"usc": "A", "ts": 1, "close": None,
                                  "remark": None, "gmt_update": "x"})
    check("NULL/元数据列静默跳过",
          item == {"usc": "A", "ts": 1} and problems == [], (item, problems))

    item, _ = M.build_item({"usc": "A", "ts": 1, "last_close": 9.5})
    check("last_close 映射为 prev_close", item.get("prev_close") == 9.5, item)

    item, problems = M.build_item({"usc": "A", "ts": 1, "close": "abc",
                                  "volume": -1, "date": 18000101,
                                  "time": 240000, "provider": "P" * 256})
    check("非法可选字段被丢弃并记录",
          set(item) == {"usc", "ts"} and len(problems) == 5, (item, problems))

    check("缺 ts 整行保留不迁移", M.build_item({"usc": "A"})[0] is None)
    check("空 usc 整行保留不迁移",
          M.build_item({"usc": "   ", "ts": 1})[0] is None)
    check("负 ts 整行保留不迁移",
          M.build_item({"usc": "A", "ts": -5})[0] is None)

    item, _ = M.build_item({"usc": "A", "ts": 1, "close": "10.35",
                            "time": "93000"})
    check("字符串数值可转换",
          item == {"usc": "A", "ts": 1, "close": 10.35, "time": 93000}, item)


# ---------------------------------------------------------------------------
# 源表客户端：查询构造与响应解析（monkeypatch http_request）
# ---------------------------------------------------------------------------

def test_supabase_queries():
    captured = []
    pages = {
        None: [{"usc": "A", "ts": 50}, {"usc": "A", "ts": 60},
               {"usc": "B", "ts": 100}],
        "B": [{"usc": "C", "ts": 70}],
        "C": [],
    }

    def fake_http(url, method, body_bytes=None, headers=None, timeout=30):
        captured.append({"method": method, "url": url,
                         "headers": dict(headers or {})})
        if method == "DELETE":
            return 200, '[{"usc": "A", "ts": 1}]', {}
        if "select=*" in url:
            return 200, '[{"usc": "A", "ts": 1, "close": 10.2}]', {}
        if "select=usc,ts" in url:
            cursor = None
            if "usc=gt." in url:
                cursor = url.split("usc=gt.", 1)[1].split("&", 1)[0]
            return 200, json.dumps(pages.get(cursor, [])), {}
        return 200, '[{"usc": "01810", "ts": 200}]', {}

    original = M.http_request
    M.http_request = fake_http
    try:
        reader = M.SupabaseTable("proj", "key", retries=0)
        check("探测列返回列集合",
              reader.discover_columns() == {"usc", "ts", "close"})

        securities = reader.list_securities()
        check("证券清单按最早 ts 升序",
              securities == [("A", 50), ("C", 70), ("B", 100)], securities)
        scan_urls = [item["url"] for item in captured
                     if "select=usc,ts" in item["url"]]
        check("证券清单：主键顺序扫描 + usc 键集分页",
              len(scan_urls) == 3
              and "order=usc.asc,ts.asc" in scan_urls[0]
              and "limit=1000" in scan_urls[0]
              and "usc=gt." not in scan_urls[0]
              and "usc=gt.B" in scan_urls[1]
              and "usc=gt.C" in scan_urls[2], scan_urls)
        check("证券清单不再使用聚合函数",
              all("ts.min()" not in url for url in scan_urls), scan_urls)

        rows = reader.fetch_page(["usc", "ts"], 100, "01810", after_ts=100)
        url = captured[-1]["url"]
        check("页查询：单证券 + ts 升序 + 键集过滤",
              "usc=eq.01810" in url and "order=ts.asc" in url
              and "limit=100" in url and "ts=gt.100" in url and rows, url)

        reader.fetch_page(["usc", "ts"], 10, "A,B")
        check("特殊字符 usc 用双引号包裹",
              "usc=eq.%22A,B%22" in captured[-1]["url"], captured[-1]["url"])

        check("删除返回删除行数",
              reader.delete_keys([("A", 1), ("B", 2)]) == 1)
        delete_url = captured[-1]["url"]
        check("删除条件为 (usc, ts) 精确匹配",
              "or=(and(usc.eq.A,ts.eq.1),and(usc.eq.B,ts.eq.2))" in delete_url,
              delete_url)
        check("删除请求带 return=representation",
              captured[-1]["headers"].get("Prefer") == "return=representation",
              captured[-1]["headers"])
        check("密钥不出现在 URL 中",
              all("key" not in item["url"] for item in captured))

        # statement timeout（57014）不重试，直接给可行动提示
        attempts = []

        def fake_timeout(url, method, body_bytes=None, headers=None, timeout=30):
            attempts.append(url)
            return 500, ('{"code":"57014","message":"canceling statement '
                         'due to statement timeout"}'), {}

        M.http_request = fake_timeout
        timeout_reader = M.SupabaseTable("proj", "key", retries=2)
        try:
            timeout_reader.list_securities()
            check("语句超时不重试并给出提示", False)
        except M.MigrationError as e:
            check("语句超时不重试并给出提示",
                  "57014" in str(e) and len(attempts) == 1, (str(e), attempts))
    finally:
        M.http_request = original


# ---------------------------------------------------------------------------
# 迁移编排：时间优先、单批单证券、删除语义
# ---------------------------------------------------------------------------

def test_time_priority_and_single_usc_per_batch():
    rows = [make_row("B", 100), make_row("B", 110),
            make_row("A", 50), make_row("A", 60), make_row("A", 70),
            make_row("C", 40)]
    result, reader, target, out = run_migrate(rows, batch_size=2)

    usc_sequence = []
    for batch in target.batches:
        uscs = {item["usc"] for item in batch}
        check("单批只含一个证券：%s" % sorted(uscs), len(uscs) == 1, uscs)
        usc_sequence.append(batch[0]["usc"])
    check("证券处理顺序按最早 ts：C → A → B",
          usc_sequence == ["C", "A", "A", "B"], usc_sequence)
    check("证券内按 ts 升序",
          [item["ts"] for item in target.batches[1]] == [50, 60]
          and [item["ts"] for item in target.batches[2]] == [70],
          [[item["ts"] for item in b] for b in target.batches])
    stats = result["stats"]
    check("批次 = 4（覆盖 6 行）", stats["batches"] == 4, stats)
    check("写入/删除 6 行，源表清空",
          stats["upserted"] == 6 and stats["deleted"] == 6
          and reader.rows == [], (stats, reader.rows))
    check("证券数 = 3", stats["usc_count"] == 3, stats["usc_count"])
    check("无失败/跳过", not result["failures"] and stats["skipped_rows"] == 0)
    check("日志给出证券清单与顺序", "证券清单 3 个" in out)


def test_skipped_row_keeps_usc_and_no_dead_loop():
    rows = [make_row("A", -5), make_row("A", 10), make_row("B", 20)]
    result, reader, target, out = run_migrate(rows, batch_size=10)
    stats = result["stats"]
    check("负 ts 行被跳过且不写入",
          stats["skipped_rows"] == 1 and stats["upserted"] == 2, stats)
    check("只删除成功写入的行",
          stats["deleted"] == 2 and len(reader.rows) == 1, reader.rows)
    check("有跳过行的证券记为待人工处理",
          result["pending_uscs"] == ["A"], result["pending_uscs"])
    check("另一个证券照常迁移", "B" in [b[0]["usc"] for b in target.batches])
    check("每个证券只处理一轮（无死循环）",
          stats["usc_count"] == 2 and reader.list_calls == 1, stats)
    check("日志说明保留在源表", "保留在源表" in out)


def test_upsert_failure_keeps_rows_and_stops():
    rows = [make_row("A", 10), make_row("B", 20)]
    reader = FakeReader(rows)
    target = FakeTarget()
    target.fail_on = {2}
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        result = M.migrate(target, reader, M.resolve_columns(reader.columns),
                           5, 100, True, False, False)
    stats = result["stats"]
    check("失败批次记为失败并停止",
          stats["failed_batches"] == 1 and len(result["failures"]) == 1,
          (stats, result["failures"]))
    check("失败批次不删除（B 的原始行仍在源表）",
          [r["usc"] for r in reader.rows] == ["B"], reader.rows)
    check("失败前已成功的批次已删除",
          stats["deleted"] == 1 and stats["upserted"] == 1, stats)


def test_continue_on_error_keeps_running():
    rows = [make_row("A", 10), make_row("B", 20), make_row("C", 30)]
    reader = FakeReader(rows)
    target = FakeTarget()
    target.fail_on = {1}
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        result = M.migrate(target, reader, M.resolve_columns(reader.columns),
                           5, 100, True, True, False)
    stats = result["stats"]
    check("允许继续时后续证券照常迁移",
          stats["failed_batches"] == 1 and stats["deleted"] == 2
          and [r["usc"] for r in reader.rows] == ["A"], (stats, reader.rows))


def test_affected_short_means_no_delete():
    rows = [make_row("A", 10), make_row("A", 20)]
    reader = FakeReader(rows)
    target = FakeTarget()
    target.short_affected = True
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        result = M.migrate(target, reader, M.resolve_columns(reader.columns),
                           10, 100, True, False, False)
    check("affected 偏小 → 判失败且不删除",
          result["stats"]["failed_batches"] == 1
          and len(reader.rows) == 2 and not reader.delete_calls,
          (result["stats"], len(reader.rows)))


def test_delete_disabled_still_completes():
    rows = [make_row("A", 10), make_row("A", 20), make_row("B", 30)]
    result, reader, target, _out = run_migrate(
        rows, batch_size=2, enable_delete=False)
    stats = result["stats"]
    check("关闭删除：写入但源表不动",
          stats["upserted"] == 3 and stats["deleted"] == 0
          and len(reader.rows) == 3, (stats, len(reader.rows)))
    check("关闭删除：每个证券只处理一次且不重复迁移",
          stats["batches"] == 2 and stats["usc_count"] == 2
          and sum(len(b) for b in target.batches) == 3, stats)


def test_max_batches_limit():
    rows = []
    for index in range(6):
        rows.append(make_row("S%d" % index, 10 + index * 10))
        rows.append(make_row("S%d" % index, 11 + index * 10))
    result, reader, target, out = run_migrate(
        rows, batch_size=2, max_batches=3)
    check("达到批次上限即停（3 批）",
          result["stats"]["batches"] == 3 and result["hit_limit"], result["stats"])
    check("剩余数据保留在源表", len(reader.rows) == 6, len(reader.rows))
    check("日志提示重跑可继续", "批次上限" in out)


def test_null_ts_security_is_pending():
    rows = [make_row("A", None), make_row("B", 20)]
    result, reader, target, out = run_migrate(rows)
    check("全空 ts 的证券保留并记为待人工处理",
          result["pending_uscs"] == ["A"]
          and "缺少可用 ts" in out, result["pending_uscs"])
    check("其它证券照常迁移",
          [b[0]["usc"] for b in target.batches] == ["B"] and len(reader.rows) == 1,
          (target.batches, reader.rows))


def test_missing_key_columns():
    try:
        M.resolve_columns({"close", "open"})
        check("源表缺 usc/ts 时明确报错", False)
    except M.MigrationError as e:
        check("源表缺 usc/ts 时明确报错", "缺少必需列" in str(e), e)


# ---------------------------------------------------------------------------
# 目标服务客户端（本地假服务）
# ---------------------------------------------------------------------------

class FakeServer:
    """假目标服务：可注入 503 次数 / 413 阈值 / affected 回传值 / 4xx"""

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


def test_client(server):
    M.RETRY_BACKOFF_SECONDS = 0.01
    client = M.QuoteUpsertClient(server.base)

    server.reset()
    result = client.upsert([{"usc": "A", "ts": 1}, {"usc": "A", "ts": 2}])
    check("正常 Upsert 结果",
          result == {"affected": 2, "mode": "update", "requests": 1}, result)
    check("请求路径与 onConflict",
          server.calls[-1][0] == "/API/V1/Meta/Finv/Quote/Secu/KLine/Min/Upsert"
                                "?onConflict=update", server.calls[-1][0])

    server.reset(fail_5xx=2)
    check("5xx 重试后成功",
          client.upsert([{"usc": "A", "ts": 3}])["affected"] == 1)

    server.reset(fail_5xx=99)
    try:
        client.upsert([{"usc": "A", "ts": 4}])
        check("重试耗尽抛错", False)
    except M.SyncRequestError as e:
        check("重试耗尽抛错", "重试 2 次" in str(e), e)

    server.reset(reject_over=1)
    result = client.upsert([{"usc": "A", "ts": i} for i in range(4)])
    check("413 自动二分", result["affected"] == 4, result)

    server.reset(affected=1)
    try:
        client.upsert([{"usc": "A", "ts": 5}, {"usc": "A", "ts": 6}])
        check("update 模式 affected 偏小判失败", False)
    except M.SyncRequestError as e:
        check("update 模式 affected 偏小判失败", "affected" in str(e), e)
    check("skip 模式允许 affected 偏小",
          M.parse_upsert_response('{"affected":1,"mode":"skip"}', 3)["affected"] == 1)
    check("非 JSON 响应不崩",
          M.parse_upsert_response("oops", 2)["affected"] is None)

    server.reset(status_4xx=422)
    try:
        client.upsert([{"usc": "A", "ts": 7}])
        check("422 立即失败不重试", False)
    except M.SyncRequestError as e:
        check("422 立即失败不重试", len(server.calls) == 1 and "422" in str(e),
              (len(server.calls), e))

    try:
        M.QuoteUpsertClient(server.base, on_conflict="bad")
        check("非法 onConflict 报错", False)
    except ValueError:
        check("非法 onConflict 报错", True)

    check("URL 缺协议补 http",
          M.normalize_base_url("1.2.3.4:18001") == "http://1.2.3.4:18001")
    for bad in ("   ", "ftp://h", "http://"):
        try:
            M.normalize_base_url(bad)
            check("非法 URL 报错：%r" % bad, False)
        except ValueError:
            check("非法 URL 报错：%r" % bad, True)


# ---------------------------------------------------------------------------
# 入口：环境变量、退出码
# ---------------------------------------------------------------------------

def test_main(server):
    """main(): 凭据缺失 / 正常跑完 / 跳过行严格 vs 放宽 / 失败批次 / 空表"""
    env_keys = ("SUPABASE_PROJECT_REF", "SUPABASE_KEY", "URL_QUOTE_SYNC_BASE",
                "MIGRATE_ENABLE_DELETE", "MIGRATE_ALLOW_SKIPPED_ROWS",
                "MIGRATE_MAX_BATCHES", "MIGRATE_ALLOW_DELETE_MISMATCH")
    saved = {key: os.environ.get(key) for key in env_keys}
    original_reader, original_target = M.SupabaseTable, M.QuoteUpsertClient
    try:
        for key in env_keys:
            os.environ.pop(key, None)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = M.main()
        check("缺 Supabase 凭据退出码 1",
              code == 1 and "SUPABASE_PROJECT_REF" in buffer.getvalue(), code)

        os.environ["SUPABASE_PROJECT_REF"] = "proj"
        os.environ["SUPABASE_KEY"] = "key"
        os.environ["URL_QUOTE_SYNC_BASE"] = ""
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = M.main()
        check("缺目标地址退出码 1",
              code == 1 and "URL_QUOTE_SYNC_BASE" in buffer.getvalue(), code)

        def run_main(rows, env=None, fail_on=None):
            os.environ["URL_QUOTE_SYNC_BASE"] = server.base
            for key, value in (env or {}).items():
                os.environ[key] = value
            reader_holder = FakeReader(rows)
            target_holder = FakeTarget()
            if fail_on:
                target_holder.fail_on = set(fail_on)
            M.SupabaseTable = lambda *a, **k: reader_holder
            M.QuoteUpsertClient = lambda *a, **k: target_holder
            out = io.StringIO()
            with redirect_stdout(out):
                code = M.main()
            for key in (env or {}):
                os.environ.pop(key, None)
            return code, out.getvalue(), reader_holder, target_holder

        code, out, reader_holder, _target = run_main(
            [make_row("A", 10), make_row("B", 20)])
        check("正常跑完退出码 0 且源表清空",
              code == 0 and reader_holder.rows == [], (code, reader_holder.rows))
        check("汇总含证券/批次统计", "合计：证券" in out)

        code, out, reader_holder, _target = run_main(
            [make_row("A", -1), make_row("A", 10)])
        check("有跳过行默认退出码 1",
              code == 1 and "MIGRATE_ALLOW_SKIPPED_ROWS" in out, code)
        code, out, reader_holder, _target = run_main(
            [make_row("A", -1), make_row("A", 10)],
            env={"MIGRATE_ALLOW_SKIPPED_ROWS": "true"})
        check("允许跳过行时退出码 0",
              code == 0 and len(reader_holder.rows) == 1, code)

        code, out, reader_holder, _target = run_main(
            [make_row("A", 10), make_row("B", 20)], fail_on={1})
        check("失败批次退出码 1 且行未删除",
              code == 1 and len(reader_holder.rows) == 2, (code, reader_holder.rows))

        code, out, _reader, _target = run_main(
            [], env={"MIGRATE_MAX_BATCHES": "1"})
        check("源表为空退出码 0",
              code == 0 and "证券清单 0 个" in out, (code, out[-200:]))
    finally:
        M.SupabaseTable, M.QuoteUpsertClient = original_reader, original_target
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main():
    M._ensure_console_utf8()
    server = FakeServer()
    try:
        test_build_item()
        test_supabase_queries()
        test_time_priority_and_single_usc_per_batch()
        test_skipped_row_keeps_usc_and_no_dead_loop()
        test_upsert_failure_keeps_rows_and_stops()
        test_continue_on_error_keeps_running()
        test_affected_short_means_no_delete()
        test_delete_disabled_still_completes()
        test_max_batches_limit()
        test_null_ts_security_is_pending()
        test_missing_key_columns()
        test_client(server)
        test_main(server)
    finally:
        server.stop()
    print("\n%s" % ("全部通过" if not FAILS else "失败 %d 项：%s" % (len(FAILS), FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
