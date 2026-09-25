#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VerifyQuoteMinuteSyncConfig 的表名/主键口径自测。

背景：富途采集配置表 2026-09 由 finv_quote_futu_collect 改名为 finv_quote_collect_futu，
**主键同时由 stockId 改为 usc**。改名只改一半最典型的后果是 —— 代码里还在用
`stockId = record.get(FUTU_KEY)` 这种「借关联键代取 stockId」的写法，于是 secu 新建时把 usc
写进 sid、sid 一致性校验退化成「sid 与 usc 比」（几乎每行都误报告警）。本自测把这些口径钉住。

两条线：
    纯逻辑   —— planRow / inspectSchema / selectableColumns / mismatchText 的键与列口径；
    离线端到端 —— 用替身顶掉 SupabaseClient._request（不起本地端口：离线、确定、跑得快），
                 把 main() 真跑一遍并断言最终发出去的 upsert body —— 这是「数据能否准确落库」
                 最直接的证据。URL 拼接、请求头、body 序列化、回读解析仍由被测代码自己完成。

被测代码只读文件、只拼请求：不联网、不写库、不碰仓库（无令牌 → 自动降级本地工作树列举）。
用法：python3 .github/Python/tests/syncQuoteCollectFutuSelfTest.py
"""

import io
import json
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
# 本文件位于 <根>/.github/Python/tests/, 其本身也在 .github/Python 之下,
# 故仓库根 = .github/Python 再上溯一级 = dirname(dirname(dirname(HERE)))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(ROOT, ".github", "Python"))

import VerifyQuoteMinuteSyncConfig as M  # noqa: E402


FAILS = []
STAMP = "2026-09-23T15:00:00+00:00"


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name, ("  ← " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------------------
# 假 PostgREST：只应答，不改被测代码的任何行为
# ---------------------------------------------------------------------------


class FakeRest:
    """顶替 SupabaseClient._request 的假 PostgREST。

    :param tables: {表名: {"columns": [列名…], "required": [列名…], "rows": [行…]}}
    """

    def __init__(self, tables):
        self.tables = tables
        self.calls = []           # 每次调用的原始参数，断言用

    def spec(self):
        """按 PostgREST OpenAPI 的形状生成表结构（definitions.*.properties / required）。"""
        definitions = {}
        for table, conf in self.tables.items():
            definitions[table] = {
                "required": list(conf.get("required") or []),
                "properties": {name: {"type": "text"} for name in conf["columns"]},
            }
        return {"swagger": "2.0", "definitions": definitions}

    def __call__(self, method, table, query, body=None, prefer=None, accept=None):
        self.calls.append({"method": method, "table": table, "query": query or "",
                           "body": body, "prefer": prefer, "accept": accept})
        if not table:                                   # GET /rest/v1/ 取 schema
            return 200, json.dumps(self.spec())
        conf = self.tables.get(table)
        if conf is None:
            return 404, '{"message":"Not Found"}'
        params = urllib.parse.parse_qs(query or "")
        if method == "GET":
            selected = (params.get("select") or [""])[0].split(",")
            wanted = set()
            for name, values in params.items():
                raw = values[0]
                if name != "select" and raw.startswith("in.(") and raw.endswith(")"):
                    wanted = set(raw[4:-1].split(","))
            rows = [{name: row.get(name) for name in selected}
                    for row in conf["rows"] if str(row.get("usc")) in wanted]
            return 200, json.dumps(rows)
        if method == "POST":
            rows = json.loads((body or b"[]").decode("utf-8"))
            merged = {str(row.get("usc")): row for row in conf["rows"]}
            merged.update({str(row.get("usc")): row for row in rows})
            conf["rows"] = list(merged.values())
            return 201, json.dumps(rows)                # Prefer: return=representation 的回读
        return 405, '{"message":"Method Not Allowed"}'

    def posts(self, table):
        """取出写向某张表的 body（已解析成行列表）。"""
        found = []
        for call in self.calls:
            if call["method"] == "POST" and call["table"] == table:
                found.append((call, json.loads(call["body"].decode("utf-8"))))
        return found

    def selects(self, table):
        """取出读某张表时请求的列清单。"""
        found = []
        for call in self.calls:
            if call["method"] == "GET" and call["table"] == table:
                params = urllib.parse.parse_qs(call["query"])
                found.append((params.get("select") or [""])[0].split(","))
        return found


def runMain(tables, extraEnv, uscs):
    """在替身 PostgREST 上真跑一遍 main()，返回 (退出码|TaskError, 假替身, 日志文本)。"""
    fake = FakeRest(tables)
    original = M.SupabaseClient._request
    M.SupabaseClient._request = fake
    keys = ["SUPABASE_PROJECT_REF", "SUPABASE_KEY", "INPUT_USC", "INPUT_BRANCH", "INPUT_OUT_DIR",
            "INPUT_SYNC_PROBABILITY", "INPUT_SYNC_BATCH_SIZE", "INPUT_SYNC_SAMPLE_LIMIT",
            "INPUT_SYNC_DELETE", "INPUT_SYNC_ALLOW_INSERT", "INPUT_SYNC_DRY_RUN", "GITHUB_COMMIT_TOKEN",
            "GITHUB_STEP_SUMMARY", "GITHUB_ACTIONS"]
    saved = {name: os.environ.get(name) for name in keys}
    env = {"SUPABASE_PROJECT_REF": "selftest", "SUPABASE_KEY": "selftest",
           "INPUT_USC": ",".join(uscs), "INPUT_SYNC_PROBABILITY": "100",
           "INPUT_SYNC_BATCH_SIZE": str(len(uscs)), "INPUT_SYNC_SAMPLE_LIMIT": "0",
           "INPUT_SYNC_DELETE": "0"}          # 关掉删除：省掉仓库令牌，也不用改任何产物
    env.update(extraEnv)
    stream, real = io.StringIO(), sys.stdout
    try:
        for name in keys:
            os.environ.pop(name, None)
        os.environ.update(env)
        sys.stdout = stream
        try:
            code = M.main()
        except M.TaskError as exc:
            code = exc
    finally:
        sys.stdout = real
        M.SupabaseClient._request = original
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return code, fake, stream.getvalue()


# ---------------------------------------------------------------------------
# 场景数据：两张表的行都取「库中现值」，故意与映射记录差一两个字段以触发更新
# ---------------------------------------------------------------------------


def loadRecord(usc):
    record = M.Mapping().lookup(usc)
    if record is None:
        raise AssertionError("映射表里没有 %s：本自测依赖 UscFutuMapping.jsonl" % usc)
    return record


def futuRow(record, **overrides):
    """按映射记录造一行「富途配置表现状」，再按需覆盖若干列。"""
    row = {"usc": record["usc"], "quote_market": record["quoteMarket"],
           "type_symbol": record["typeSymbol"], "futu_symbol": record["futuSymbol"],
           "marketType": record["marketType"], "marketCode": record["marketCode"],
           "instrumentType": record["instrumentType"],
           "subInstrumentType": record["subInstrumentType"],
           "stockId": record["stockId"], "type": "2",
           "flag_enable": "0", "dt_update": "2020-01-01T00:00:00+00:00"}
    row.update(overrides)
    return row


def secuRow(record, **overrides):
    row = {"usc": record["usc"], "sid": record["stockId"], "region": record["secuRegion"],
           "market": record["secuMarket"], "name_sc": record["nameSc"],
           "type_secu": record["typeSecu"], "flag_enable": "0",
           "dt_update": "2020-01-01T00:00:00+00:00"}
    row.update(overrides)
    return row


FUTU_COLUMNS = ["usc", "quote_market", "type_symbol", "futu_symbol", "marketType", "marketCode",
                "instrumentType", "subInstrumentType", "stockId", "type", "flag_enable",
                "dt_update", "dt_create"]
SECU_COLUMNS = ["usc", "sid", "region", "market", "name_sc", "type_secu", "flag_enable",
                "dt_update", "dt_create"]


def tablesWith(records, futuColumns=None, secuColumns=None, futu=True):
    """组装一次端到端用的假库：两张表各带这些 usc 的现状行。

    刻意把 futu 表的 quote_market / type_symbol / stockId 造成「与映射不一致」，
    这样 payload 里一定有这几列，能验证它们确实被回填成映射记录的取值。
    secu 表则带上 sid（该列后续要删）—— 用来验证脚本不读也不写它。
    """
    tables = {M.SECU_TABLE: {"columns": secuColumns or SECU_COLUMNS,
                             "required": ["usc", "region", "market"],
                             "rows": [secuRow(record) for record in records]}}
    if futu:
        tables[M.FUTU_TABLE] = {
            "columns": futuColumns or FUTU_COLUMNS, "required": ["usc"],
            "rows": [futuRow(record, quote_market="ZZ", type_symbol="ZZ",
                             stockId=(record["stockId"] or 0) + 1) for record in records]}
    return tables


# ---------------------------------------------------------------------------
# 一、纯逻辑：键与 stockId 必须各取各的字段
# ---------------------------------------------------------------------------


def testInsertUsesUscAndNoSid():
    """新建：两张表都以 usc 为键；不再补 secu.sid（该列后续删除）；futu 表仍带 stockId。"""
    record = loadRecord("02800")
    columns = {M.FUTU_TABLE: None, M.SECU_TABLE: None}          # None = schema 不可用、不过滤
    required = {M.FUTU_TABLE: set(), M.SECU_TABLE: {"region", "market"}}
    plans, misses, _warns = M.planRow(record, {}, {}, STAMP, True, required, columns)
    byTable = {plan["table"]: plan for plan in plans}
    futu, secu = byTable.get(M.FUTU_TABLE), byTable.get(M.SECU_TABLE)

    check("新建：两张表都出了计划（未命中 %s）" % misses, not misses and futu and secu, repr(misses))
    check("新建：两张表键都 = usc",
          futu["keyValue"] == record["usc"] and secu["keyValue"] == record["usc"],
          repr((futu["keyValue"], secu["keyValue"])))
    check("新建：secu 不再补 sid（该列已废弃）",
          "sid" not in secu["payload"], repr(sorted(secu["payload"])))
    check("新建：secu 该补的非空列仍然补齐",
          secu["payload"].get("region") == record["secuRegion"]
          and secu["payload"].get("market") == record["secuMarket"], repr(secu["payload"]))
    check("新建：futu 表带上了 stockId 列", futu["payload"].get("stockId") == str(record["stockId"]),
          repr(futu["payload"].get("stockId")))


def testSidLeftAlone():
    """sid 已废弃：库中 sid 取什么值都不告警、也绝不写它（列还在时不动，列删了也不受影响）。"""
    record = loadRecord("02800")
    columns = {M.FUTU_TABLE: None, M.SECU_TABLE: None}
    required = {M.FUTU_TABLE: set(), M.SECU_TABLE: set()}
    futuRows = {record["usc"]: [futuRow(record)]}
    cases = ((record["stockId"], "与 stockId 一致"), (record["usc"], "与 usc 相同"),
             ("", "为空"), (999, "是个陌生值"))
    for sidValue, label in cases:
        secuRows = {record["usc"]: [secuRow(record, sid=sidValue, region="ZZ")]}
        plans, _misses, warns = M.planRow(record, futuRows, secuRows, STAMP, False, required, columns)
        secuPlan = [plan for plan in plans if plan["table"] == M.SECU_TABLE][0]
        check("sid 脱钩：库中 sid %s 时不告警" % label,
              not [warn for warn in warns if "sid" in warn], repr(warns))
        check("sid 脱钩：库中 sid %s 时也绝不写它" % label,
              "sid" not in secuPlan["payload"], repr(sorted(secuPlan["payload"])))
        check("sid 脱钩：同一行的其它列照常更新（sid %s）" % label,
              secuPlan["payload"].get("region") == record["secuRegion"], repr(secuPlan["payload"]))


def testColumnPruning():
    """列存在性过滤：表里没有的列不进 payload，且要告警（否则整块 upsert 会 400）。"""
    record = loadRecord("02800")
    columns = {M.FUTU_TABLE: {"usc", "flag_enable", "dt_update"}, M.SECU_TABLE: None}
    required = {M.FUTU_TABLE: set(), M.SECU_TABLE: set()}
    plans, _misses, warns = M.planRow(record, {}, {}, STAMP, True, required, columns)
    futu = [plan for plan in plans if plan["table"] == M.FUTU_TABLE][0]
    check("列过滤：表里没有的列被剔除",
          set(futu["payload"]) <= {"usc", "flag_enable", "dt_update"}, repr(sorted(futu["payload"])))
    check("列过滤：剔除的列有告警",
          any("quote_market" in warn for warn in warns), repr(warns))
    check("列过滤：trim 掉的列不再出现在 changed 日志里",
          all(item[0] in futu["payload"] for item in futu["changed"]),
          repr([item[0] for item in futu["changed"]]))


def testInspectSchema():
    """表结构核对：表不存在 / 主键列缺失 → 任务级错误；字段列缺失 → 剔除并告警。"""
    class Client:
        def __init__(self, schema):
            self.schema = schema

        def tableSchema(self, table):
            return self.schema.get(table, (set(), set()))

    full = {M.FUTU_TABLE: (set(FUTU_COLUMNS), {"usc"}),
            M.SECU_TABLE: (set(SECU_COLUMNS), {"usc", "region", "market"})}
    required, columns, warns = M.inspectSchema(Client(full))
    check("核对：两张表齐全时无告警且 required 去掉主键",
          not warns and required[M.SECU_TABLE] == {"region", "market"}, repr(warns))
    check("核对：带出实际列集合", M.FUTU_TABLE in columns and "stockId" in columns[M.FUTU_TABLE],
          repr(columns.get(M.FUTU_TABLE)))

    try:
        M.inspectSchema(Client({M.SECU_TABLE: full[M.SECU_TABLE]}))
        check("核对：表不在 schema 里 → 退出码 2 的任务级错误", False, "没有抛 TaskError")
    except M.TaskError as exc:
        check("核对：表不在 schema 里 → 退出码 2 的任务级错误",
              M.FUTU_TABLE in str(exc), str(exc))

    noKey = dict(full)
    noKey[M.FUTU_TABLE] = (set(FUTU_COLUMNS) - {"usc"}, {"usc"})
    try:
        M.inspectSchema(Client(noKey))
        check("核对：主键列不在表里 → 任务级错误", False, "没有抛 TaskError")
    except M.TaskError as exc:
        check("核对：主键列不在表里 → 任务级错误", M.FUTU_KEY in str(exc), str(exc))

    thin = dict(full)
    thin[M.FUTU_TABLE] = ({"usc", "flag_enable", "dt_update"}, {"usc"})
    _required, _columns, warns = M.inspectSchema(Client(thin))
    check("核对：字段列缺失 → 只告警不中断",
          len(warns) == 1 and "quote_market" in warns[0], repr(warns))

    _required, columns, warns = M.inspectSchema(Client({M.FUTU_TABLE: (None, set()),
                                                        M.SECU_TABLE: (None, set())}))
    check("核对：schema 不可用 → 跳过核对、不过滤列",
          not warns and columns[M.FUTU_TABLE] is None, repr((warns, columns)))


def testSelectableColumns():
    """select 列过滤：表里没有的列不能进 URL（PostgREST 会 400）。"""
    names = ["usc", "stockId", "flag_enable", "stockId"]
    check("select 过滤：schema 不可用 → 原样（去重保序）",
          M.selectableColumns(names, None) == ["usc", "stockId", "flag_enable"],
          repr(M.selectableColumns(names, None)))
    check("select 过滤：按表实际列剔除",
          M.selectableColumns(names, {"usc", "flag_enable"}) == ["usc", "flag_enable"],
          repr(M.selectableColumns(names, {"usc", "flag_enable"})))


def testMismatchText():
    """留痕正文：stockId 一行必须是映射记录的 stockId，不能再借 FUTU_KEY 代取。"""
    record = loadRecord("02800")
    text = M.mismatchText("02800", record, "Verify/Success/HK/02800.mvsv",
                          [M.MISS_FUTU_ROW], "2026-09-23 23:00:00")
    check("留痕：stockId 行 = 映射记录的 stockId",
          "stockId    : %s" % record["stockId"] in text, text.splitlines()[3])
    check("留痕：FUTU_ROW_MISSING 文案说的是 usc（不再是 stockId）",
          "usc" in M.MISS_REASONS[M.MISS_FUTU_ROW] and "stockId" not in M.MISS_REASONS[M.MISS_FUTU_ROW],
          M.MISS_REASONS[M.MISS_FUTU_ROW])
    check("留痕：补建口径写的是「两张表都以 usc 关联」",
          "两张表都以 usc 关联" in text, text[-260:])


# ---------------------------------------------------------------------------
# 二、离线端到端：把 main() 真跑一遍，断言真正发出去的请求
# ---------------------------------------------------------------------------


def testEndToEndUpsertByUsc():
    """正常更新：两张表各一条 upsert，键是 usc，值取自映射记录。"""
    records = [loadRecord("02800"), loadRecord("03042")]
    code, fake, log = runMain(tablesWith(records), {}, [record["usc"] for record in records])
    check("端到端：正常一轮退出码 0", code == 0, "退出码 %r；日志尾部：%s" % (code, log[-800:]))

    futuPosts, secuPosts = fake.posts(M.FUTU_TABLE), fake.posts(M.SECU_TABLE)
    check("端到端：两张表各一条更新 upsert",
          len(futuPosts) == 1 and len(secuPosts) == 1, "%d/%d" % (len(futuPosts), len(secuPosts)))
    if not (futuPosts and secuPosts):
        return
    futuCall, futuRows = futuPosts[0]
    secuCall, secuRows = secuPosts[0]
    check("端到端：futu 表 on_conflict=usc",
          "on_conflict=usc" in futuCall["query"], futuCall["query"])
    check("端到端：secu 表 on_conflict=usc",
          "on_conflict=usc" in secuCall["query"], secuCall["query"])
    check("端到端：写入的行键就是这批 usc",
          {row["usc"] for row in futuRows} == {record["usc"] for record in records},
          repr([row.get("usc") for row in futuRows]))
    check("端到端：每行的键完全一致（PGRST102 前提）",
          len({tuple(sorted(row)) for row in futuRows}) == 1,
          repr([sorted(row) for row in futuRows]))

    byUsc = {row["usc"]: row for row in futuRows}
    want = {record["usc"]: record for record in records}
    check("端到端：body 只写表里真实存在的列",
          all(set(row) <= set(FUTU_COLUMNS) for row in futuRows), repr([sorted(row) for row in futuRows]))
    check("端到端：futu 表变更列按映射回填（quote_market / type_symbol）",
          all(byUsc[usc]["quote_market"] == record["quoteMarket"]
              and byUsc[usc]["type_symbol"] == record["typeSymbol"]
              for usc, record in want.items()), repr(byUsc))
    check("端到端：futu 表 stockId 被纠正为映射取值（库里原值故意错成 +1）",
          all(str(byUsc[usc]["stockId"]) == str(record["stockId"])
              for usc, record in want.items()), repr(byUsc))
    check("端到端：flag_enable 置 '1'",
          all(row["flag_enable"] == "1" for row in futuRows), repr(futuRows))
    check("端到端：未变更的列不进 body（省带宽、也不误刷审计时间戳）",
          all("marketCode" not in row for row in futuRows), repr(futuRows))

    secuByUsc = {row["usc"]: row for row in secuRows}
    check("端到端：secu 表 region/market 按映射回填",
          all(secuByUsc[usc]["region"] == record["secuRegion"]
              and secuByUsc[usc]["market"] == record["secuMarket"]
              for usc, record in want.items()), repr(secuByUsc))
    check("端到端：secu 表 body 里不出现 sid（该列后续删除）",
          all("sid" not in row for row in secuRows), repr(secuByUsc))
    check("端到端：secu 表 select 里也不查 sid",
          fake.selects(M.SECU_TABLE)
          and all("sid" not in names for names in fake.selects(M.SECU_TABLE)),
          repr(fake.selects(M.SECU_TABLE)))
    check("端到端：日志里不出现 sid", "sid" not in log, log[-600:])


def testEndToEndWithoutStockIdColumn():
    """新表若已没有 stockId 列：select 与 body 都不带它，且给出告警。"""
    records = [loadRecord("02800")]
    columns = [name for name in FUTU_COLUMNS if name != "stockId"]
    code, fake, log = runMain(tablesWith(records, futuColumns=columns), {}, ["02800"])
    check("端到端（无 stockId 列）：退出码 0", code == 0, "退出码 %r；日志尾部：%s" % (code, log[-800:]))
    _call, rows = (fake.posts(M.FUTU_TABLE) or [(None, [])])[0]
    check("端到端（无 stockId 列）：body 里没有 stockId",
          all("stockId" not in row for row in rows), repr(rows))
    selects = fake.selects(M.FUTU_TABLE)
    check("端到端（无 stockId 列）：select 里也没请求 stockId",
          selects and all("stockId" not in names for names in selects), repr(selects))
    check("端到端（无 stockId 列）：日志里有告警", "stockId" in log and "告警" in log, log[-600:])


def testEndToEndMissingTableIsTaskError():
    """表名写错/没暴露：当场任务级错误（退出码 2），不进入写库阶段。"""
    records = [loadRecord("02800")]
    code, fake, log = runMain(tablesWith(records, futu=False), {}, ["02800"])
    check("端到端（缺表）：TaskError",
          isinstance(code, M.TaskError) and M.FUTU_TABLE in str(code), repr(code))
    check("端到端（缺表）：没有发出任何写请求", not fake.posts(M.FUTU_TABLE), repr(fake.calls))
    check("端到端（缺表）：错误消息点明了是哪张表",
          "schema" in str(code) and M.FUTU_TABLE in str(code), str(code))


def testEndToEndInsertBranch():
    """INPUT_SYNC_ALLOW_INSERT=1：两表都没有该 usc 时走新建分支，补的列必须补对。"""
    record = loadRecord("02800")
    tables = {M.FUTU_TABLE: {"columns": FUTU_COLUMNS, "required": ["usc"], "rows": []},
              M.SECU_TABLE: {"columns": SECU_COLUMNS, "required": ["usc", "region", "market"],
                             "rows": []}}
    code, fake, log = runMain(tables, {"INPUT_SYNC_ALLOW_INSERT": "1"}, ["02800"])
    check("端到端（新建）：退出码 0", code == 0, "退出码 %r；日志尾部：%s" % (code, log[-800:]))
    futuPosts, secuPosts = fake.posts(M.FUTU_TABLE), fake.posts(M.SECU_TABLE)
    check("端到端（新建）：两张表各一条插入 upsert",
          len(futuPosts) == 1 and len(secuPosts) == 1, "%d/%d" % (len(futuPosts), len(secuPosts)))
    if not (futuPosts and secuPosts):
        return
    futuRow, secuRow = futuPosts[0][1][0], secuPosts[0][1][0]
    check("端到端（新建）：futu 行键 = usc", futuRow.get("usc") == record["usc"], repr(futuRow))
    check("端到端（新建）：futu 行补 dt_create（不补常量列）",
          bool(futuRow.get("dt_create")) and "type" not in futuRow, repr(futuRow))
    check("端到端（新建）：futu 行字段取自映射记录",
          futuRow.get("quote_market") == record["quoteMarket"]
          and futuRow.get("futu_symbol") == record["futuSymbol"], repr(futuRow))
    check("端到端（新建）：secu 行不含 sid（该列后续删除）",
          "sid" not in secuRow, repr(secuRow))
    check("端到端（新建）：secu 行补齐 region/market（NOT NULL 无默认值）",
          secuRow.get("region") == record["secuRegion"]
          and secuRow.get("market") == record["secuMarket"], repr(secuRow))
    check("端到端（新建）：新增行在日志里被单独列出", "＋" in log and "新增行" in log, log[-800:])


def testIgnoredColumnsUntouched():
    """type / sid 属刻意忽略列：不查、不写、不补非空列；表里删掉这些列后也照常跑。"""
    record = loadRecord("02800")
    check("忽略列：type 与 sid 都在 IGNORED_COLUMNS 里",
          {"type", "sid"} <= set(M.IGNORED_COLUMNS), repr(M.IGNORED_COLUMNS))
    check("忽略列：两张表都不再有新建常量列",
          not M.INSERT_CONSTANTS.get(M.FUTU_TABLE) and not M.INSERT_CONSTANTS.get(M.SECU_TABLE),
          repr(M.INSERT_CONSTANTS))
    check("忽略列：type / sid 都不在字段映射里",
          all(column not in ("type", "sid") for column, _ in M.FUTU_FIELDS + M.SECU_FIELDS),
          repr((M.FUTU_FIELDS, M.SECU_FIELDS)))
    check("忽略列：脚本里已没有 SECU_LINK_COLUMN 这个常量",
          not hasattr(M, "SECU_LINK_COLUMN"), "SECU_LINK_COLUMN 仍未删除")

    # ① 列还在、且被库端标成 NOT NULL 无默认值：仍不补列，只各给一条告警
    class Client:
        def tableSchema(self, table):
            if table == M.FUTU_TABLE:
                return set(FUTU_COLUMNS), {"usc", "type"}
            return set(SECU_COLUMNS), {"usc", "sid", "region", "market"}

    required, _columns, warns = M.inspectSchema(Client())
    check("忽略列：type / sid 都不进 NOT NULL 补列清单",
          "type" not in required[M.FUTU_TABLE] and "sid" not in required[M.SECU_TABLE],
          repr(required))
    check("忽略列：type / sid 是 NOT NULL 无默认值时各给一条告警",
          len(warns) == 2 and any("type" in warn for warn in warns)
          and any("sid" in warn for warn in warns), repr(warns))

    # ② 两列都已删除（删除之后的世界）：无告警，且既不去查也不去写
    futuColumns = [name for name in FUTU_COLUMNS if name != "type"]
    secuColumns = [name for name in SECU_COLUMNS if name != "sid"]
    code, fake, log = runMain(tablesWith([record], futuColumns=futuColumns, secuColumns=secuColumns),
                              {}, ["02800"])
    check("忽略列：type / sid 列已删除时端到端退出码 0", code == 0,
          "退出码 %r；日志尾部：%s" % (code, log[-600:]))
    futuSelects, secuSelects = fake.selects(M.FUTU_TABLE), fake.selects(M.SECU_TABLE)
    check("忽略列：type 列已删除时 select 里没有 type",
          futuSelects and all("type" not in names for names in futuSelects), repr(futuSelects))
    check("忽略列：sid 列已删除时 select 里没有 sid",
          secuSelects and all("sid" not in names for names in secuSelects), repr(secuSelects))
    _call, futuRows = (fake.posts(M.FUTU_TABLE) or [(None, [])])[0]
    _call, secuRows = (fake.posts(M.SECU_TABLE) or [(None, [])])[0]
    check("忽略列：body 里既没有 type 也没有 sid",
          all("type" not in row and "sid" not in row for row in futuRows + secuRows),
          repr((futuRows, secuRows)))
    check("忽略列：type / sid 列已删除时不产生与它们有关的告警",
          "刻意忽略列" not in log and "没有列 type" not in log and "没有列 sid" not in log,
          log[-600:])


def testEndToEndDryRunWritesNothing():
    """dry-run：只打印等价 SQL，一个写请求都不发（工作流里 dry_run 触发时也是这样）。"""
    records = [loadRecord("02800")]
    code, fake, log = runMain(tablesWith(records), {"INPUT_SYNC_DRY_RUN": "1"}, ["02800"])
    check("端到端（dry-run）：退出码 0", code == 0, repr(code))
    check("端到端（dry-run）：没有任何 POST", not fake.posts(M.FUTU_TABLE), repr(fake.calls))
    check("端到端（dry-run）：打印了等价 SQL",
          "INSERT INTO %s" % M.FUTU_TABLE in log and "ON CONFLICT (usc)" in log, log[-900:])


def main():
    testInsertUsesUscAndNoSid()
    testSidLeftAlone()
    testColumnPruning()
    testInspectSchema()
    testSelectableColumns()
    testMismatchText()
    testEndToEndUpsertByUsc()
    testEndToEndWithoutStockIdColumn()
    testEndToEndMissingTableIsTaskError()
    testEndToEndInsertBranch()
    testIgnoredColumnsUntouched()
    testEndToEndDryRunWritesNothing()
    print("-" * 60)
    if FAILS:
        print("FAILED %d: %s" % (len(FAILS), "、".join(FAILS)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
