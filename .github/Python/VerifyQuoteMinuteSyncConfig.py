#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VerifyQuoteMinuteSyncConfig.py — 把 Verify/Success/ 下证券的查询参数同步进 Supabase 配置表。

入参：环境变量 INPUT_*（由工作流 .github/workflows/VerifyQuoteMinute.yml 的「同步配置到 Supabase」
步骤注入，本地自测直接设同名环境变量即可）。无命令行参数。

    INPUT_USC                    本批 usc，逗号或空白分隔（与采集步骤同口径）
    INPUT_WATCHLIST              清单文件路径；INPUT_USC 为空时改读它
    INPUT_BRANCH                 产物分支：读它的 Verify/Success/，MisMatch 也提交到它，默认 quote-meta
    INPUT_OUT_DIR                本地镜像目录（MisMatch 本地副本落在此），默认 verify-out
    INPUT_SYNC_PROBABILITY       本轮是否执行的命中概率（0-100，默认 100）
    INPUT_SYNC_BATCH_SIZE        单轮最多同步多少条（默认 50，0 = 不限）
    INPUT_SYNC_SAMPLE_LIMIT      从 Verify/Success/ 清单头部最多补齐多少条（默认 47）
    INPUT_SYNC_CONCURRENCY       并发写库的线程数（默认 6）
    INPUT_SYNC_DRY_RUN           置真则只打印将要执行的 SQL，不写库、不提交
    SUPABASE_PROJECT_REF         Supabase 项目引用（必填）
    SUPABASE_KEY                 Supabase API 密钥（service-role；必填，不落日志）
    GITHUB_COMMIT_TOKEN          MisMatch 留痕提交用（约定变量名见 GitHubCommitContent.py）

本文档分节：
    一、工具定位        为什么需要这一步、与 VerifyQuoteMinute.py 的分工
    二、抽样规则        概率闸门 → 本批 → 清单头部补齐
    三、映射表          由 .mvsv 文件名取 usc，经 idx + JSONL 反序列化出查询参数
    四、更新规则        两张表的字段映射、flag_enable、dt_update、以及三处刻意的收窄
    五、PostgREST 调用  批量读现状 + 逐行 PATCH（含重试与并发）
    六、未命中处理      写 Verify/MisMatch/{usc}.txt 并提交到产物分支
    七、日志与摘要      逐条字段级日志、dry-run 的 SQL 计划、Actions 步骤摘要
    八、退出码
    九、尚未实现        同步成功后的删除（留位，暂不开启）

--------------------------------------------------------------------------------
一、工具定位
--------------------------------------------------------------------------------
VerifyQuoteMinute.py 负责「按 usc 验证行情能否采到」，产物落在 Verify/Success|Fail/{market}/。
本脚本负责它的**下游**：把 Success 下的证券（文件名去掉扩展名即 usc）对应的**查询参数**
回填进 Supabase 的两张配置表，并把 flag_enable 置 '1'，让新配置立即生效。

参数不从 .mvsv 文件里读，而是走**同一套映射产物**（UscFutuMapping.jsonl.idx +
UscFutuMapping.jsonl）—— .mvsv 头部只带了部分参数（缺 marketType / marketCode /
instrumentType / subInstrumentType / typeSecu / secuRegion / secuMarket），且解析正文成本更高。
本脚本因此只读文件名、不读文件内容：一次目录列举即可拿到全部待同步 usc。

--------------------------------------------------------------------------------
二、抽样规则
--------------------------------------------------------------------------------
    ① 概率闸门：抽 [0,100) 的随机数，小于 INPUT_SYNC_PROBABILITY 才继续；未命中则本轮
       什么都不做（打印日志后退出 0）。概率值刻意放在工作流文件里，便于随时调整：
       当前为 100（便于测试与追赶），稳定后按设计调回 10。
    ② 本批 usc：INPUT_USC / 清单文件解析出的 usc，**且必须已存在于目标分支的
       Verify/Success/ 下**（采失败的证券不该被 enable）；不在 Success 下的会被剔除并记日志。
    ③ 补齐抽样：从 Verify/Success/ 清单**按路径升序取前 N 个**（N = INPUT_SYNC_SAMPLE_LIMIT，
       默认 47），与 ② 合起来凑成一批（默认 50 条）。取头部是刻意的确定性行为：结果可复现、
       日志可追溯；后续开启「同步成功即删除产物」后，每轮的头部会自然前移。
    ④ 目标数上限 INPUT_SYNC_BATCH_SIZE（默认 50）：本批已超过上限时不再补齐。

一轮要写多少行，是「本批 ∪ 清单头部」去重后的结果，不是全量重刷。

--------------------------------------------------------------------------------
三、映射表
--------------------------------------------------------------------------------
与 VerifyQuoteMinute.py 完全同一套产物与格式：
    UscFutuMapping.jsonl.idx   定长索引：32 字节头 + N × 24 字节记录（16 字节 key + 偏移 + 长度）
    UscFutuMapping.jsonl       每行一条 JSON，键序固定，首键即 "usc"
先用索引二分定位（含偏移/长度与首键前缀校验），再 json.loads 出记录。

⚠️ 本地 Windows 检出踩坑：若 git 把 JSONL 的 LF 换行改写成 CRLF（本仓库无 .gitattributes
兜底），文件比索引声明的字节数每行多 1 字节，索引里的偏移随之整体失准。本脚本对此**不报错**，
而是自动降级为「按行扫描建表」，结果与索引路径等价，只在日志里告警一次。
（CI 跑在 Linux 上，一律 LF，走的是索引快路径。）

--------------------------------------------------------------------------------
四、更新规则
--------------------------------------------------------------------------------
两张表都以 stockId 关联（取值即映射记录的 stockId）：

    finv_quote_futu_collect   主键 stockId
        quote_market       ← quoteMarket
        type_symbol        ← typeSymbol
        futu_symbol        ← futuSymbol
        marketType         ← marketType
        marketCode         ← marketCode
        instrumentType     ← instrumentType
        subInstrumentType  ← subInstrumentType
        flag_enable        ← '1'（写死，不取映射记录的 flagEnable）
        dt_update          ← 本次写入时刻

    finv_quote_secu           主键 usc，本脚本按 sid = stockId 关联
        region             ← secuRegion
        market             ← secuMarket
        name_sc            ← nameSc
        type_secu          ← typeSecu
        flag_enable        ← '1'
        dt_update          ← 本次写入时刻

三处刻意的收窄（都是为了「宁可少写，不可写错」）：
    ① secu 表除 sid 外还带 usc 过滤：该表实测存在 sid 重复行（sid=800000 同时挂在 usc=800000
       与 usc=HSI 上），只按 sid 更新会连带改写同 sid 的其它证券。
    ② 映射记录里为空值的字段**不写**：整表有 30 条记录缺 nameSc，硬写空值会抹掉库里的现成值，
       故跳过该字段并告警（同一条记录的其余字段照常更新）。
    ③ 「已完全一致」的行不发写请求：字段全等且 flag_enable 已是 '1' 时整行跳过（连 dt_update
       也不动），这样重复运行几乎零写入，也不会把审计时间戳刷成无意义的噪声。

--------------------------------------------------------------------------------
五、PostgREST 调用
--------------------------------------------------------------------------------
照 .github/Python/QuoteCollect/SupabaseJobRepo.py 的 SupabaseRestClient 手法实现最小客户端：
每个请求同时带 apikey 与 Authorization: Bearer，PATCH 一律带非空过滤条件（防误全表更新），
靠 Prefer: return=representation 统计命中行数。

    GET   {table}?select=<cols>&<keyCol>=in.(...)    分块取现状（含重试）
    PATCH {table}?<过滤条件>  body=待更新字段          命中行数 ≠ 1 记失败

写请求走线程池（INPUT_SYNC_CONCURRENCY，默认 6）；429/5xx 与网络类错误按指数退避重试。

--------------------------------------------------------------------------------
六、未命中处理
--------------------------------------------------------------------------------
判为「未命中」的 usc 不滞留在日志里，而是逐一写成 Verify/MisMatch/{usc}.txt 并提交到产物分支
（INPUT_BRANCH，即 quote-meta），文件内载明 usc、stockId、来源 .mvsv、缺哪张表、解析出的全部
查询参数、检测时刻与工作流运行标识、处理建议。判为未命中的情形：

    INDEX_MISS            usc 不在 UscFutuMapping.jsonl.idx 中（无映射记录）
    FUTU_ROW_MISSING      表中不存在该 stockId 的行
    SECU_ROW_MISSING      表中不存在该 sid 的行
    SECU_USC_MISMATCH     存在该 sid，但没有任何一行的 usc 与文件名一致
    SECU_ROW_DUPLICATE    存在多行同 sid 且 usc 匹配（会只按其一带 usc 过滤更新，故留痕提示）

本脚本**不会**向这两张表插入新行：缺行只留痕，补录与否由人工决定。

--------------------------------------------------------------------------------
七、日志与摘要
--------------------------------------------------------------------------------
每条证券的日志含：来源 .mvsv、usc、quoteMarket、stockId、映射记录取到的全部参数、两张表逐字段的
「旧值 → 新值」、写入结果（HTTP 状态与命中行数）。dry-run 时改为打印等价 SQL（PostgreSQL 单引号
字面量，可直接照抄执行）。步骤结束另写 Actions 运行摘要（$GITHUB_STEP_SUMMARY）。

--------------------------------------------------------------------------------
八、退出码
--------------------------------------------------------------------------------
    0  正常结束（含「概率闸门未命中」与「有未命中记录但已留痕」这两种预期状态）
    1  有写库失败、命中行数异常或 MisMatch 提交失败
    2  任务级错误（凭据缺失、映射产物不可用、目录列举失败、参数非法）

--------------------------------------------------------------------------------
九、尚未实现
--------------------------------------------------------------------------------
「同步成功后删除对应的 Verify/Success/{market}/{usc}.mvsv」**当前刻意不做**（先只读不删）。
留待后续单独评估：删除会改变产物台账，需要一并考虑 Fail 件回写与 MisMatch 的关系。
"""

import concurrent.futures
import datetime
import json
import os
import random
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_NAME = os.path.basename(os.path.abspath(__file__))
TASK_VERSION = "1"

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 映射表产物（与 VerifyQuoteMinute.py 同一套格式，见模块 docstring 第三节）
IDX_NAME = "UscFutuMapping.jsonl.idx"
JSONL_NAME = "UscFutuMapping.jsonl"
IDX_MAGIC = b"UFI1"
IDX_HEADER, IDX_RECORD, IDX_KEY = 32, 24, 16

# 仓库内路径（相对仓库根）
SUCCESS_DIR = "Verify/Success"
MISMATCH_DIR = "Verify/MisMatch"
MVSV_SUFFIX = ".mvsv"

# 默认参数
DEFAULT_BRANCH = "quote-meta"
DEFAULT_OUT_DIR = "verify-out"
DEFAULT_PROBABILITY = 100
DEFAULT_BATCH_SIZE = 50
DEFAULT_SAMPLE_LIMIT = 47
DEFAULT_CONCURRENCY = 6

# Supabase / PostgREST
ENV_SUPABASE_REF = "SUPABASE_PROJECT_REF"
ENV_SUPABASE_KEY = "SUPABASE_KEY"
SUPABASE_REST_BASE = "https://%s.supabase.co/rest/v1"
READ_CHUNK = 150            # 单次 GET 的 id 条数（控制 URL 长度）
HTTP_TIMEOUT = 30
MAX_ATTEMPTS = 3            # 单次请求的最大尝试次数（含首次）
RETRY_BACKOFF = 2.0         # 重试退避基数（秒）：第 n 次退避 RETRY_BACKOFF * 2^(n-1)
RETRYABLE_STATUS = (429, 500, 502, 503, 504)

# 更新规则（字段口径见模块 docstring 第四节）
FUTU_TABLE = "finv_quote_futu_collect"
FUTU_KEY = "stockId"
FUTU_FIELDS = (("quote_market", "quoteMarket"), ("type_symbol", "typeSymbol"),
               ("futu_symbol", "futuSymbol"), ("marketType", "marketType"),
               ("marketCode", "marketCode"), ("instrumentType", "instrumentType"),
               ("subInstrumentType", "subInstrumentType"))

SECU_TABLE = "finv_quote_secu"
SECU_KEY = "sid"
SECU_FIELDS = (("region", "secuRegion"), ("market", "secuMarket"),
               ("name_sc", "nameSc"), ("type_secu", "typeSecu"))

#: 映射记录涉及的字段（解析输出、MisMatch 留痕、必要字段校验共用同一口径）
RECORD_FIELDS = ("stockId", "typeSymbol", "quoteMarket", "futuSymbol", "marketType",
                 "marketCode", "instrumentType", "subInstrumentType", "nameSc",
                 "typeSecu", "secuRegion", "secuMarket")

#: 缺任一字段即无法确定完整查询参数（仍可更新其余字段，缺的字段跳过并告警）
CRITICAL_FIELDS = ("stockId", "marketType", "marketCode", "instrumentType", "subInstrumentType")

ENABLE_COLUMN = "flag_enable"
ENABLE_VALUE = "1"
DT_UPDATE_COLUMN = "dt_update"

# 未命中分类（写进 MisMatch 文件的「原因」段）
MISS_INDEX = "INDEX_MISS"
MISS_RECORD_FIELD = "RECORD_FIELD_MISSING"
MISS_FUTU_ROW = "FUTU_ROW_MISSING"
MISS_SECU_ROW = "SECU_ROW_MISSING"
MISS_SECU_USC = "SECU_USC_MISMATCH"
MISS_SECU_DUP = "SECU_ROW_DUPLICATE"

MISS_REASONS = {
    MISS_INDEX: "usc 不在 UscFutuMapping.jsonl.idx 中（无映射记录，解析不出查询参数）",
    MISS_RECORD_FIELD: "映射记录缺关键字段，拼不出完整请求参数",
    MISS_FUTU_ROW: "表 finv_quote_futu_collect 中不存在该 stockId 的行（本脚本不插行）",
    MISS_SECU_ROW: "表 finv_quote_secu 中不存在该 sid 的行（本脚本不插行）",
    MISS_SECU_USC: "表 finv_quote_secu 中存在该 sid，但其行的 usc 与文件名不一致，未做更新",
    MISS_SECU_DUP: "表 finv_quote_secu 中同 sid 存在多行且 usc 匹配，已按 usc 过滤只更新该行，请复核",
}

RETURN_OK, RETURN_FAILED, RETURN_TASK_ERROR = 0, 1, 2

CHINA_TZ = datetime.timezone(datetime.timedelta(hours=8))


class TaskError(Exception):
    """任务级错误：凭据缺失、映射产物不可用、目录列举失败等，退出码 2。"""


# ---------------------------------------------------------------------------
# 环境变量与通用小工具
# ---------------------------------------------------------------------------


def env(name, default=""):
    """读环境变量，空白视为未设置。"""
    return (os.environ.get(name) or "").strip() or default


def envFlag(name, default=False):
    """读布尔型环境变量，1/true/yes/on 为真。"""
    raw = env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def envInt(name, default, minimum=None, maximum=None):
    """读整型环境变量并做区间校验；非法取值抛 TaskError。"""
    raw = env(name)
    if not raw:
        value = int(default)
    else:
        try:
            value = int(raw)
        except ValueError:
            raise TaskError("环境变量 %s 不是整数：%r" % (name, raw))
    if minimum is not None and value < minimum:
        raise TaskError("环境变量 %s 不得小于 %d：%d" % (name, minimum, value))
    if maximum is not None and value > maximum:
        raise TaskError("环境变量 %s 不得大于 %d：%d" % (name, maximum, value))
    return value


def chunks(raw):
    """把逗号 / 空白分隔的文本切成去重后的列表，保持原序。"""
    words = str(raw or "").replace("，", " ").replace(",", " ").split()
    return list(dict.fromkeys(words))


def nowChina():
    """当前 UTC+8 时刻，格式化为 2026-09-23 14:32:10。"""
    return datetime.datetime.now(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def nowUtcIso():
    """当前 UTC 时刻（ISO8601，秒精度）——写入 dt_update。"""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def cell(value):
    """取值转字符串（空值一律空串）：库里的 varchar 与映射里的 int 按同一口径比较。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def sqlLiteral(value):
    """把 Python 值渲染成 PostgreSQL 字面量（dry-run 打印的 SQL 可直接照抄执行）。

    PostgreSQL 的字符串字面量是单引号，双引号是标识符语义，故不能用 json.dumps。
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'%s'" % str(value).replace("'", "''")


def shown(value):
    """日志里展示取值：空串显示为 (空)。"""
    return sqlLiteral(value) if cell(value) else "(空)"


def annotate(message, level="warning"):
    """GitHub Actions 注解；本地运行时退化为普通打印。"""
    if env("GITHUB_ACTIONS"):
        print("::%s::%s" % (level, message.replace("\n", " ")), flush=True)
    else:
        print("[%s] %s" % (level.upper(), message), flush=True)


# ---------------------------------------------------------------------------
# 映射表：按 usc 取查询参数
# ---------------------------------------------------------------------------


class Mapping:
    """UscFutuMapping.jsonl.idx + JSONL：按 usc 取记录（索引快路径 + 行扫描兜底）。"""

    def __init__(self):
        self.idxPath = os.path.join(SCRIPT_DIR, IDX_NAME)
        self.jsonlPath = os.path.join(SCRIPT_DIR, JSONL_NAME)
        if not os.path.isfile(self.idxPath) or not os.path.isfile(self.jsonlPath):
            raise TaskError("缺少映射表产物：%s / %s" % (self.idxPath, self.jsonlPath))
        with open(self.idxPath, "rb") as handle:
            self.raw = handle.read()
        with open(self.jsonlPath, "rb") as handle:
            self.jsonl = handle.read()
        if len(self.raw) < IDX_HEADER or self.raw[0:4] != IDX_MAGIC:
            raise TaskError("索引文件头不合法：%s" % self.idxPath)
        version, keyWidth = struct.unpack_from("<HH", self.raw, 4)
        if version != 1 or keyWidth != IDX_KEY:
            raise TaskError("索引格式不支持：formatVersion=%d keyWidth=%d" % (version, keyWidth))
        self.count = struct.unpack_from("<I", self.raw, 8)[0]
        self.digest = self.raw[20:28].hex()
        if len(self.raw) != IDX_HEADER + self.count * IDX_RECORD:
            raise TaskError("索引长度与声明条数不符：声明 %d 条" % self.count)
        # 字节数不配对时**不报错**：典型成因是本地检出把 LF 换成了 CRLF（见 docstring 第三节）
        self.indexUsable = len(self.jsonl) == struct.unpack_from("<Q", self.raw, 12)[0]
        self._scanned = None

    def describe(self):
        """给日志用的一行自描述。"""
        return "%d 条，索引摘要 %s（%s）" % (
            self.count, self.digest, "索引快路径" if self.indexUsable else "行扫描兜底")

    def lookup(self, usc):
        """按 usc 取映射记录；未命中返回 None。"""
        if self.indexUsable:
            record, degraded = self._lookupByIndex(usc)
            if degraded:
                print("  [告警] 索引偏移失准（%s），本轮降级为按行扫描；"
                      "常见成因是检出时 LF 被改写为 CRLF" % usc, flush=True)
            else:
                return record
        return self._scan().get(usc)

    def _lookupByIndex(self, usc):
        """二分定位并解析；偏移失准（首键前缀不符）时返回 (None, True) 要求降级。"""
        key = usc.encode("utf-8")
        low, high = 0, self.count - 1
        while low <= high:
            mid = (low + high) >> 1
            base = IDX_HEADER + mid * IDX_RECORD
            current = self.raw[base:base + IDX_KEY].rstrip(b"\x00")
            if current == key:
                offset, length = struct.unpack_from("<II", self.raw, base + IDX_KEY)
                line = self.jsonl[offset:offset + length]
                if not line.startswith(b'{"usc":"' + key + b'"'):
                    self.indexUsable = False
                    return None, True
                return json.loads(line.decode("utf-8")), False
            low, high = (mid + 1, high) if current < key else (low, mid - 1)
        return None, False

    def _scan(self):
        """按行扫描建表（每个进程只建一次）。"""
        if self._scanned is None:
            table = {}
            for line in self.jsonl.decode("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                table[record.get("usc")] = record
            self._scanned = table
        return self._scanned


# ---------------------------------------------------------------------------
# Supabase：PostgREST 最小客户端（读现状 / 逐行 PATCH）
# ---------------------------------------------------------------------------


class SupabaseRestError(Exception):
    """Supabase Data API 调用失败。"""


class SupabaseClient:
    """PostgREST 最小客户端（仅标准库），手法同 QuoteCollect/SupabaseJobRepo.py。

    每个请求同时携带 apikey 与 Authorization: Bearer（Supabase 要求二者并存）；
    429/5xx 与网络类错误按指数退避重试，其余错误直接抛出。
    """

    def __init__(self, projectRef, apiKey, workers=DEFAULT_CONCURRENCY, timeout=HTTP_TIMEOUT):
        if not projectRef:
            raise TaskError("Supabase 项目引用（%s）不能为空" % ENV_SUPABASE_REF)
        if not apiKey:
            raise TaskError("Supabase API 密钥（%s）不能为空" % ENV_SUPABASE_KEY)
        base = env("SUPABASE_REST_BASE") or (SUPABASE_REST_BASE % projectRef)
        self.restUrl = base.rstrip("/")
        self.apiKey = apiKey
        self.workers = workers
        self.timeout = timeout
        self.requests = 0

    def _request(self, method, table, query, body=None, prefer=None):
        """发一次请求（含重试），返回 (状态码, 响应文本)。"""
        url = "%s/%s" % (self.restUrl, urllib.parse.quote(table, safe=""))
        if query:
            url += "?" + query
        headers = {"apikey": self.apiKey, "Authorization": "Bearer %s" % self.apiKey,
                   "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if prefer:
            headers["Prefer"] = prefer
        last = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            self.requests += 1
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.status, response.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
                last = "HTTP %s：%s" % (exc.code, detail.strip()[:300])
                if exc.code not in RETRYABLE_STATUS or attempt >= MAX_ATTEMPTS:
                    raise SupabaseRestError(last) from exc
            except OSError as exc:
                last = "网络错误：%s" % exc
                if attempt >= MAX_ATTEMPTS:
                    raise SupabaseRestError(last) from exc
            time.sleep(RETRY_BACKOFF * (2 ** (attempt - 1)))
        raise SupabaseRestError(last or "未知错误")

    def selectByKeys(self, table, keyColumn, keys, columns):
        """按主键批量取现状，返回 {键: [行, ...]}（同键多行时列表长度大于 1）；分块以免 URL 过长。"""
        result = {}
        ordered = sorted({str(item) for item in keys}, key=lambda item: (len(item), item))
        for start in range(0, len(ordered), READ_CHUNK):
            chunk = ordered[start:start + READ_CHUNK]
            query = "select=%s&%s=in.(%s)" % (",".join(columns), keyColumn, ",".join(chunk))
            status, body = self._request("GET", table, query)
            if status != 200:
                raise SupabaseRestError("GET %s 返回 HTTP %s：%s" % (table, status, body[:300]))
            try:
                rows = json.loads(body or "[]")
            except ValueError as exc:
                raise SupabaseRestError("GET %s 响应不是合法 JSON：%s" % (table, exc)) from exc
            if not isinstance(rows, list):
                raise SupabaseRestError("GET %s 响应不是行数组" % table)
            for row in rows:
                if not isinstance(row, dict) or row.get(keyColumn) is None:
                    continue
                result.setdefault(str(row[keyColumn]), []).append(row)
        return result

    def patch(self, table, query, payload):
        """PATCH 更新，返回受影响行数；query 必须非空（防误全表更新）。"""
        if not query:
            raise SupabaseRestError("PATCH 缺少过滤条件，拒绝执行（防止误全表更新）")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, response = self._request("PATCH", table, query, body, prefer="return=representation")
        if status not in (200, 204):
            raise SupabaseRestError("PATCH %s 返回 HTTP %s：%s" % (table, status, response[:300]))
        if not response.strip():
            return 0
        try:
            rows = json.loads(response)
        except ValueError:
            return 0
        return len(rows) if isinstance(rows, list) else 0


# ---------------------------------------------------------------------------
# 仓库侧：列举 Verify/Success/、提交 MisMatch 文件
# ---------------------------------------------------------------------------


class RepoAccess:
    """GitHub Contents API 的最小封装：递归列目录 + 单文件提交。

    复用同目录 GitHubCommitContent.py 的 _request / _auth_headers / commit_content_file，
    与仓库内其它脚本保持同一套鉴权与提交口径。
    """

    def __init__(self, branch):
        if SCRIPT_DIR not in sys.path:
            sys.path.insert(0, SCRIPT_DIR)
        try:
            import GitHubCommitContent
        except ImportError as exc:
            raise TaskError("无法导入同目录的 GitHubCommitContent.py：%s" % exc) from exc
        self.lib = GitHubCommitContent
        self.branch = branch
        owner, repo = GitHubCommitContent.load_owner_repo_from_git_config()
        combined = env("GITHUB_REPOSITORY")
        if "/" in combined:
            owner, repo = combined.split("/", 1)
        self.owner, self.repo = owner, repo
        self.token = env(GitHubCommitContent.ENV_TOKEN)
        self.apiBase = GitHubCommitContent.DEFAULT_API_BASE

    @property
    def usable(self):
        """是否有仓库身份与令牌（缺任一项都做不了线上列举/提交）。"""
        return bool(self.owner and self.repo and self.token)

    def _listDir(self, path):
        """列一个目录（不递归），返回 [{type,name,path}]；目录不存在返回 None。

        ⚠️ Contents API 对**单个目录**一次最多返回 1000 条，超出的部分必须翻页取，
        否则会静默截断（本仓库 Verify/Success/HK 与 SZ 都在 1600 条以上，实测漏掉近 4 成）。
        """
        entries, page = [], 1
        while True:
            url = "%s/%s/%s/contents/%s?ref=%s&per_page=100&page=%d" % (
                self.apiBase, self.owner, self.repo,
                urllib.parse.quote(path, safe="/"), urllib.parse.quote(self.branch, safe=""), page)
            status, body, error = self.lib._request(
                "GET", url, self.lib._auth_headers(self.token), timeout=HTTP_TIMEOUT)
            if error:
                raise TaskError("列举 %s@%s 失败：%s" % (path, self.branch, error))
            if status == 404:
                return None if page == 1 else entries
            if status != 200:
                raise TaskError("列举 %s@%s 返回 HTTP %s：%s"
                                % (path, self.branch, status, (body or "")[:200]))
            data = self.lib._parse_json(body)
            if not isinstance(data, list):
                raise TaskError("列举 %s@%s 的响应不是目录数组" % (path, self.branch))
            entries.extend(item for item in data if isinstance(item, dict))
            if len(data) < 100:
                return entries
            page += 1

    def _listByTree(self):
        """用 Git Trees API 一次取回整棵目录树（只取 blob 路径）。

        比逐目录列举快得多（1 次请求），是首选路径；返回 None 表示不可用、需降级。
        """
        url = "%s/%s/%s/git/trees/%s?recursive=1" % (
            self.apiBase, self.owner, self.repo, urllib.parse.quote(self.branch, safe=""))
        status, body, error = self.lib._request(
            "GET", url, self.lib._auth_headers(self.token), timeout=HTTP_TIMEOUT)
        if error or status != 200:
            print("  [告警] Git Trees 列举 %s@%s 不可用（%s），降级为逐目录列举"
                  % (self.branch, self.branch, error or ("HTTP %s" % status)), flush=True)
            return None
        data = self.lib._parse_json(body)
        if not isinstance(data, dict) or not isinstance(data.get("tree"), list):
            print("  [告警] Git Trees 响应不是目录树，降级为逐目录列举", flush=True)
            return None
        if data.get("truncated"):
            print("  [告警] Git Trees 响应被截断（仓库树过大），降级为逐目录列举", flush=True)
            return None
        return [item.get("path") for item in data["tree"]
                if isinstance(item, dict) and item.get("type") == "blob" and item.get("path")]

    def _listByContents(self, root):
        """逐目录递归列举（无需 Trees API 权限时的兜底路径）。"""
        found, pending = [], [root]
        while pending:
            path = pending.pop(0)
            entries = self._listDir(path)
            if entries is None:
                if path == root:
                    raise TaskError("目录不存在：%s@%s" % (root, self.branch))
                continue
            for entry in entries:
                kind, entryPath = entry.get("type"), entry.get("path") or ""
                if kind == "dir":
                    pending.append(entryPath)
                elif kind == "file" and entryPath.endswith(MVSV_SUFFIX):
                    found.append(entryPath)
        return found

    def listMvsv(self, root):
        """递归列举 root 下全部 .mvsv，返回按仓库路径升序排序的路径列表。"""
        prefix = root.rstrip("/") + "/"
        paths = self._listByTree()
        if paths is None:
            paths = self._listByContents(root)
        found = sorted(path for path in paths
                       if path.startswith(prefix) and path.endswith(MVSV_SUFFIX))
        if not found:
            raise TaskError("目录 %s@%s 下一个 .mvsv 都没有（路径口径是否变了？）" % (root, self.branch))
        return found

    def commitFile(self, path, localFile, message):
        """提交单个本地文件到产物分支；返回结果 dict。"""
        return self.lib.commit_content_file(path, localFile, branch=self.branch,
                                            commit_msg=message, owner=self.owner, repo=self.repo)


def localSuccessFiles(root):
    """本地工作树兜底：无令牌/离线时按同一口径扫描检出目录。

    仅用于本机自测与线上列举不可用时的降级；线上以 Contents API 列举为准。
    """
    base = os.path.join(os.getcwd(), root.replace("/", os.sep))
    if not os.path.isdir(base):
        return None
    found = []
    for dirPath, _dirNames, fileNames in os.walk(base):
        for name in fileNames:
            if name.endswith(MVSV_SUFFIX):
                rel = os.path.relpath(os.path.join(dirPath, name), os.getcwd())
                found.append(rel.replace(os.sep, "/"))
    found.sort()
    return found


# ---------------------------------------------------------------------------
# 差异计算与留痕正文
# ---------------------------------------------------------------------------


def loadCurrentState(client, records):
    """批量取两张表的现状行，返回 ({stockId: [行]}, {sid: [行]})。

    只取需要的列：futu_collect 取主键 + 待更新字段 + flag_enable；
    secu 额外取 usc（用于判定 sid 关联到的行是不是同一只证券）。
    """
    futuColumns = [FUTU_KEY] + [column for column, _ in FUTU_FIELDS] + [ENABLE_COLUMN]
    secuColumns = [SECU_KEY, "usc"] + [column for column, _ in SECU_FIELDS] + [ENABLE_COLUMN]
    ids = [cell(record.get(FUTU_KEY)) for record in records]
    futuRows = client.selectByKeys(FUTU_TABLE, FUTU_KEY, ids, futuColumns)
    secuRows = client.selectByKeys(SECU_TABLE, SECU_KEY, ids, secuColumns)
    return futuRows, secuRows


def buildPayload(spec, record, current, stamp):
    """算出某张表要写的字段。

    :param spec: {"table", "key", "fields"} —— 更新规则。
    :param record: 映射记录。
    :param current: 库中现状行。
    :param stamp: dt_update 取值。
    :return: (payload, changed, unchanged, skipped)
        payload 为空字典 = 无需变更；changed/unchanged/skipped 仅用于日志。
    """
    payload, changed, unchanged, skipped = {}, [], [], []
    for column, source in spec["fields"]:
        want = cell(record.get(source))
        have = cell(current.get(column))
        if not want:
            # 映射缺值：保留库中原值（见 docstring 第四节 ②）
            skipped.append((column, source, have))
        elif want == have:
            unchanged.append("%s=%s" % (column, have))
        else:
            payload[column] = want
            changed.append((column, source, have, want))
    if cell(current.get(ENABLE_COLUMN)) != ENABLE_VALUE:
        payload[ENABLE_COLUMN] = ENABLE_VALUE
        changed.append((ENABLE_COLUMN, "(固定值)", cell(current.get(ENABLE_COLUMN)), ENABLE_VALUE))
    if payload:
        # dt_update 只在确有变更时盖章：无变更的行走不到这里（见 docstring 第四节 ③）
        payload[DT_UPDATE_COLUMN] = stamp
    return payload, changed, unchanged, skipped


def futuFilter(stockId):
    """futu_collect 表的过滤条件：主键 stockId。"""
    return "%s=eq.%s" % (FUTU_KEY, stockId)


def secuFilter(record, stockId):
    """secu 表的过滤条件：sid 关联 + usc 兜底（防止同 sid 的多行证券被连带改写）。"""
    return "%s=eq.%s&usc=eq.%s" % (SECU_KEY, stockId,
                                   urllib.parse.quote(cell(record.get("usc")), safe=""))


def dryRunSql(table, filterExpr, payload, note):
    """渲染将要执行的 UPDATE（dry-run 打印 + 日志追溯两用）。"""
    sets = ", ".join("%s = %s" % (key, sqlLiteral(value)) for key, value in payload.items())
    return "UPDATE %s SET %s WHERE %s;  -- %s" % (table, sets, filterExpr, note)


def planRow(record, futuRows, secuRows, stamp):
    """算出一条证券的完整更新计划（两张表）。

    :return: (plans, misses)
        plans = [{"table","filter","payload","changed","unchanged","skipped"}]；
        misses = 未命中分类码列表（该表不写，其余表照常写）。
    """
    stockId = cell(record.get(FUTU_KEY))
    plans, misses = [], []

    futuSpec = {"table": FUTU_TABLE, "key": FUTU_KEY, "fields": FUTU_FIELDS}
    futuList = futuRows.get(stockId) or []
    if not futuList:
        misses.append(MISS_FUTU_ROW)
    else:
        if len(futuList) > 1:
            print("      ! %s 中 stockId=%s 出现 %d 行（主键重复），只按第一行计算差异"
                  % (FUTU_TABLE, stockId, len(futuList)), flush=True)
        payload, changed, unchanged, skipped = buildPayload(futuSpec, record, futuList[0], stamp)
        plans.append({"table": FUTU_TABLE, "filter": futuFilter(stockId), "payload": payload,
                      "changed": changed, "unchanged": unchanged, "skipped": skipped})

    secuSpec = {"table": SECU_TABLE, "key": SECU_KEY, "fields": SECU_FIELDS}
    secuList = secuRows.get(stockId) or []
    matched = [row for row in secuList if cell(row.get("usc")) == cell(record.get("usc"))]
    if not secuList:
        misses.append(MISS_SECU_ROW)
    elif not matched:
        misses.append(MISS_SECU_USC)
    else:
        if len(matched) > 1:
            misses.append(MISS_SECU_DUP)
        payload, changed, unchanged, skipped = buildPayload(secuSpec, record, matched[0], stamp)
        plans.append({"table": SECU_TABLE, "filter": secuFilter(record, stockId), "payload": payload,
                      "changed": changed, "unchanged": unchanged, "skipped": skipped})
    return plans, misses


def mismatchText(usc, record, source, misses, nowText):
    """未命中留痕文件的正文（含缺表清单、映射参数、处理建议）。"""
    lines = [
        "# VerifyQuoteMinute 配置同步：未命中记录",
        "",
        "usc        : %s" % usc,
        "stockId    : %s" % (cell((record or {}).get(FUTU_KEY)) or "(未知)"),
        "来源文件   : %s" % (source or "(无)"),
        "检测时刻   : %s (+08:00)" % nowText,
        "任务脚本   : %s（版本 %s）" % (SCRIPT_NAME, TASK_VERSION),
        "工作流运行 : %s" % env("GITHUB_RUN_ID", "(本地运行)"),
        "",
        "## 未命中的表 / 原因",
    ]
    for entry in misses:
        # 允许 "CODE:补充说明" 形式（如把缺失的字段名一并落进留痕文件）
        code, _, detail = entry.partition(":")
        lines.append("- [%s] %s%s"
                     % (code, MISS_REASONS.get(code, code), ("（%s）" % detail) if detail else ""))
    lines += ["", "## 映射表解析出的查询参数"]
    if record:
        for name in RECORD_FIELDS:
            lines.append("%-18s = %s" % (name, cell(record.get(name))))
    else:
        lines.append("(无映射记录)")
    lines += [
        "",
        "## 处理建议",
        "- 本脚本只更新既有记录，不会向 finv_quote_futu_collect / finv_quote_secu 插入新行。",
        "- 若该证券应纳入采集配置，请先补建对应行（前者按 stockId，后者按 sid 且 usc 一致）后重跑",
        "  本步骤；补建后本文件保留作历史留痕即可。",
        "",
    ]
    return "\n".join(lines)


def writeLocal(path, content):
    """写本地文件（自动建目录），返回绝对路径。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    return os.path.abspath(path)


def readWatchlist(path):
    """读待验证清单：每行一个 usc，一行内可多个，# 起为注释（与采集脚本同口径）。"""
    if not path or not os.path.isfile(path):
        return []
    targets = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            targets.extend(chunks(line.split("#", 1)[0]))
    return targets


# ---------------------------------------------------------------------------
# 目标合成、执行与汇总
# ---------------------------------------------------------------------------


def resolveTargets(batchUscs, successUscs, batchSize, sampleLimit):
    """按 docstring 第二节合成一轮的目标名单，返回 (targets, dropped, sampled)。

    :param batchUscs: 本批 usc（输入 / 清单解析所得，保持原序）。
    :param successUscs: 目标分支 Verify/Success/ 下的 usc 清单（路径升序）。
    :param batchSize: 单轮目标数上限；0 = 不限。
    :param sampleLimit: 从清单头部补齐的条数上限。
    """
    inSuccess = set(successUscs)
    kept = [usc for usc in batchUscs if usc in inSuccess]
    dropped = [usc for usc in batchUscs if usc not in inSuccess]
    if batchSize <= 0:
        room = sampleLimit
    else:
        room = min(sampleLimit, max(0, batchSize - len(kept)))
    taken, sampled = set(kept), []
    for usc in successUscs:
        if len(sampled) >= room:
            break
        if usc not in taken:
            sampled.append(usc)
            taken.add(usc)
    return kept + sampled, dropped, sampled


def logPlan(position, total, usc, record, source, plans, misses, dryRun):
    """打印一条证券的完整计划日志（字段级，便于排查）。"""
    print("[%d/%d] usc=%s  quoteMarket=%s  stockId=%s"
          % (position, total, usc, cell(record.get("quoteMarket")), cell(record.get(FUTU_KEY))),
          flush=True)
    print("      来源 : %s" % (source or "(无 .mvsv 来源)"), flush=True)
    print("      映射 : %s" % "  ".join("%s=%s" % (name, cell(record.get(name)))
                                        for name in RECORD_FIELDS if name != "stockId"), flush=True)
    for plan in plans:
        table = plan["table"]
        if not plan["payload"]:
            print("      · %s：无需变更（%s）" % (table, "；".join(plan["unchanged"]) or "无字段"),
                  flush=True)
            continue
        print("      · %s 待更新 %d 个字段：" % (table, len(plan["payload"])), flush=True)
        for column, sourceField, have, want in plan["changed"]:
            print("          ↑ %-18s %s → %s   (%s ← %s)"
                  % (column, shown(have), sqlLiteral(want), column, sourceField), flush=True)
        if plan["unchanged"]:
            print("          = 保持 : %s" % "；".join(plan["unchanged"]), flush=True)
        for column, sourceField, have in plan["skipped"]:
            print("          ! 跳过 : %s（映射缺 %s，保留库中现值 %s）"
                  % (column, sourceField, shown(have)), flush=True)
        if dryRun:
            print("          SQL : %s"
                  % dryRunSql(table, plan["filter"], plan["payload"], "usc=%s" % usc), flush=True)
    if misses:
        print("      ⚠ 未命中：%s（将留痕 %s/%s.txt）"
              % ("、".join(misses), MISMATCH_DIR, usc), flush=True)


def applyPlans(client, tasks):
    """并发提交写请求，返回 (成功列表, 失败列表)；失败项为 (usc, 表名, 原因)。"""
    done, failed = [], []
    if not tasks:
        return done, failed
    with concurrent.futures.ThreadPoolExecutor(max_workers=client.workers) as pool:
        futures = {pool.submit(client.patch, plan["table"], plan["filter"], plan["payload"]): (usc, plan)
                   for usc, plan in tasks}
        for future in concurrent.futures.as_completed(futures):
            usc, plan = futures[future]
            try:
                affected = future.result()
                if affected == 1:
                    done.append((usc, plan))
                    print("      ⤷ 写入 %-26s %-46s HTTP 200 命中 1 行（%d 字段）"
                          % (plan["table"], plan["filter"], len(plan["payload"])), flush=True)
                else:
                    failed.append((usc, plan["table"], "命中 %d 行（预期 1 行）" % affected))
                    print("      ✗ 写入 %-26s %-46s 命中 %d 行（预期 1 行）"
                          % (plan["table"], plan["filter"], affected), flush=True)
            except SupabaseRestError as exc:
                failed.append((usc, plan["table"], str(exc)))
                print("      ✗ 写入 %-26s %-46s 失败：%s"
                      % (plan["table"], plan["filter"], exc), flush=True)
    return done, failed


def archiveMismatch(repoAccess, usc, record, source, misses, outDir, dryRun, failures):
    """把未命中记录写成本地文件并提交到产物分支；提交失败记入 failures。"""
    content = mismatchText(usc, record, source, misses, nowChina())
    local = writeLocal(os.path.join(outDir, MISMATCH_DIR.replace("/", os.sep), "%s.txt" % usc), content)
    remote = "%s/%s.txt" % (MISMATCH_DIR, usc)
    if dryRun:
        print("      ⤷ 留痕 %s（dry-run 不提交）本地副本 %s" % (remote, local), flush=True)
        return
    if not repoAccess.usable:
        failures.append((remote, "缺少仓库令牌（GITHUB_COMMIT_TOKEN），无法提交留痕文件"))
        print("      ✗ 留痕 %s 提交失败：缺少仓库令牌" % remote, flush=True)
        return
    result = repoAccess.commitFile(remote, local, "MisMatch %s" % usc)
    if result.get("success"):
        print("      ⤷ 留痕 %s 已提交（HTTP %s）" % (remote, result.get("http_status")), flush=True)
    else:
        failures.append((remote, result.get("message") or "提交失败"))
        print("      ✗ 留痕 %s 提交失败：%s" % (remote, result.get("message")), flush=True)


def stepSummary(stats, branch, dryRun, probability, batchSize, sampleLimit):
    """写 Actions 步骤摘要（未在 Actions 中运行则跳过）。"""
    path = env("GITHUB_STEP_SUMMARY")
    if not path:
        return
    if not stats["gated"]:
        lines = ["## VerifyQuoteMinute 配置同步（Supabase）", "",
                 "| 项 | 值 |", "|---|---|",
                 "| 概率闸门 | 未命中（%d%%） |" % probability,
                 "| 本轮动作 | 无（不读库、不写库、不留痕） |"]
    else:
        lines = [
            "## VerifyQuoteMinute 配置同步（Supabase）", "",
            "| 项 | 值 | 备注 |", "|---|---|---|",
            "| 目标分支 | %s | %s |" % (branch, "dry-run 未写库" if dryRun else "已写库"),
            "| 概率闸门 | 命中（%d%%） | 阈值在工作流文件内调整 |" % probability,
            "| 批量上限 | %d | 清单头部补齐上限 %d |" % (batchSize, sampleLimit),
            "| 目标条数 | %d | 本批 %d + 补齐 %d |"
            % (stats["total"], stats["batchCount"], stats["sampleCount"]),
            "| %s | %d | %s |" % ("计划写入" if dryRun else "写库成功", stats["written"],
                                   "dry-run 未执行" if dryRun else "行级写请求命中 1 行"),
            "| 写库失败 | %d | 见步骤日志中的 ✗ 行 |" % stats["failed"],
            "| 无需变更 | %d | 表级：两表均已一致，未发写请求 |" % stats["skippedRows"],
            "| 未命中留痕 | %d | Verify/MisMatch/{usc}.txt |" % stats["mismatched"],
            "| ⤷ 映射无记录 | %d | usc 不在 UscFutuMapping.jsonl.idx 中 |" % stats["indexMiss"],
            "| ⤷ 映射缺关键字段 | %d | 记录缺 stockId/市场/类型，拼不出请求参数 |" % stats["recordMiss"],
            "| 请求总数 | %d | 含读现状与重试 |" % stats["requests"],
        ]
        if stats["dropped"]:
            lines += ["", "> 本批被剔除（不在 Verify/Success/ 下，采集未成功）：%s"
                      % "、".join(stats["dropped"])]
    if stats["failures"]:
        lines += ["", "| 失败项 | 说明 |", "|---|---|"]
        lines += ["| %s | %s |" % (item, reason) for item, reason in stats["failures"][:50]]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main():
    """入口：闸门 → 定目标 → 读现状 → 算差异 → 写库 → 留痕 → 汇总。返回退出码。"""
    branch = env("INPUT_BRANCH", DEFAULT_BRANCH)
    outDir = env("INPUT_OUT_DIR", DEFAULT_OUT_DIR)
    probability = envInt("INPUT_SYNC_PROBABILITY", DEFAULT_PROBABILITY, minimum=0, maximum=100)
    batchSize = envInt("INPUT_SYNC_BATCH_SIZE", DEFAULT_BATCH_SIZE, minimum=0)
    sampleLimit = envInt("INPUT_SYNC_SAMPLE_LIMIT", DEFAULT_SAMPLE_LIMIT, minimum=0)
    workers = envInt("INPUT_SYNC_CONCURRENCY", DEFAULT_CONCURRENCY, minimum=1, maximum=32)
    dryRun = envFlag("INPUT_SYNC_DRY_RUN")
    watchlist = env("INPUT_WATCHLIST", os.path.join(SCRIPT_DIR, "VerifyQuoteMinuteWatchlist.txt"))

    print("=" * 78, flush=True)
    print("任务脚本   %s（版本 %s）" % (SCRIPT_NAME, TASK_VERSION), flush=True)
    print("目标分支   %s%s" % (branch, "（dry-run：只打印 SQL，不写库不提交）" if dryRun else ""),
          flush=True)
    print("抽样参数   概率闸门 %d%%｜批量上限 %d｜头部补齐上限 %d｜并发 %d"
          % (probability, batchSize, sampleLimit, workers), flush=True)
    print("=" * 78, flush=True)

    stats = {"gated": False, "total": 0, "batchCount": 0, "sampleCount": 0, "written": 0,
             "skippedRows": 0, "failed": 0, "mismatched": 0, "indexMiss": 0, "recordMiss": 0,
             "requests": 0, "dropped": [], "failures": []}

    # ① 概率闸门：未命中则本轮什么都不做
    roll = random.random() * 100.0
    stats["gated"] = roll < probability
    print("概率闸门   随机数 %.2f %s %d%% → %s"
          % (roll, "<" if stats["gated"] else ">=", probability,
             "命中，继续执行" if stats["gated"] else "未命中，本轮不执行"), flush=True)
    if not stats["gated"]:
        stepSummary(stats, branch, dryRun, probability, batchSize, sampleLimit)
        return RETURN_OK

    # ② 本批 usc（与采集脚本同口径：输入优先，其次清单文件）
    batchUscs = chunks(env("INPUT_USC"))
    if batchUscs:
        print("本批来源   输入 INPUT_USC → %d 条" % len(batchUscs), flush=True)
    else:
        batchUscs = readWatchlist(watchlist)
        print("本批来源   清单文件 %s → %d 条" % (watchlist, len(batchUscs)), flush=True)

    # ③ 映射表 + 目标分支的 Success 清单（线上列举优先，失败降级到本地工作树）
    mapping = Mapping()
    print("映射表     %s" % mapping.describe(), flush=True)
    repoAccess = RepoAccess(branch)
    successPaths = None
    if repoAccess.usable:
        try:
            successPaths = repoAccess.listMvsv(SUCCESS_DIR)
        except TaskError as exc:
            print("  [告警] 线上列举失败（%s），降级为本地工作树扫描" % exc, flush=True)
    if successPaths is None:
        successPaths = localSuccessFiles(SUCCESS_DIR)
        if successPaths is None:
            raise TaskError("既无法列举 %s@%s，本地工作树也没有该目录" % (SUCCESS_DIR, branch))
        print("  [告警] 本次改用本地工作树的 %s（共 %d 个 .mvsv；线上列举不可用）"
              % (SUCCESS_DIR, len(successPaths)), flush=True)
    successUscs = [os.path.basename(path)[:-len(MVSV_SUFFIX)] for path in successPaths]
    sourceOf = {os.path.basename(path)[:-len(MVSV_SUFFIX)]: path for path in successPaths}
    print("Success 清单 %s@%s 共 %d 个 .mvsv（按路径升序）"
          % (SUCCESS_DIR, branch, len(successUscs)), flush=True)

    targets, dropped, sampled = resolveTargets(batchUscs, successUscs, batchSize, sampleLimit)
    stats.update(total=len(targets), sampleCount=len(sampled),
                 batchCount=len(targets) - len(sampled), dropped=dropped)
    if dropped:
        print("本批剔除   %d 条不在 %s 下（采集未成功 / 未产出）：%s"
              % (len(dropped), SUCCESS_DIR, "、".join(dropped)), flush=True)
    if not targets:
        print("无可同步条目（本批为空且头部补齐上限为 0），本轮结束。", flush=True)
        stepSummary(stats, branch, dryRun, probability, batchSize, sampleLimit)
        return RETURN_OK
    print("本轮目标   %d 条 = 本批 %d + 清单头部补齐 %d"
          % (len(targets), stats["batchCount"], len(sampled)), flush=True)
    if sampled:
        print("补齐名单   %s%s" % ("、".join(sampled[:20]), " …" if len(sampled) > 20 else ""),
              flush=True)
    print("-" * 78, flush=True)

    # ④ 解析映射记录：无记录 / 关键字段缺失的直接留痕（不依赖数据库，先办掉以免后续失败丢信息）
    records, earlyMismatch, indexMiss = [], [], []
    for usc in targets:
        record = mapping.lookup(usc)
        if record is None:
            indexMiss.append(usc)
            earlyMismatch.append((usc, None, [MISS_INDEX]))
            continue
        missing = [name for name in CRITICAL_FIELDS if not cell(record.get(name))]
        if missing:
            print("  [告警] %s 的映射记录缺关键字段：%s（无法拼出请求参数，转留痕）"
                  % (usc, "、".join(missing)), flush=True)
            earlyMismatch.append((usc, record, ["%s:%s" % (MISS_RECORD_FIELD, "、".join(missing))]))
            continue
        records.append(record)
    stats["indexMiss"] = len(indexMiss)
    stats["recordMiss"] = len(earlyMismatch) - len(indexMiss)
    if earlyMismatch:
        print("映射不可用 %d 条（无记录 %d / 缺关键字段 %d）：%s"
              % (len(earlyMismatch), len(indexMiss), stats["recordMiss"],
                 "、".join(item[0] for item in earlyMismatch)), flush=True)
        for usc, record, misses in earlyMismatch:
            archiveMismatch(repoAccess, usc, record, sourceOf.get(usc, ""), misses,
                            outDir, dryRun, stats["failures"])

    # ⑤ 读现状（凭据与映射表同属任务级前置条件，缺了就退出 2）
    client = SupabaseClient(env(ENV_SUPABASE_REF), env(ENV_SUPABASE_KEY), workers=workers)
    futuRows, secuRows = {}, {}
    if records:
        try:
            futuRows, secuRows = loadCurrentState(client, records)
        except SupabaseRestError as exc:
            raise TaskError("读取两张表现状失败：%s" % exc)
        print("现状读取   %s 命中 %d 个 stockId｜%s 命中 %d 个 sid（请求 %d 次）"
              % (FUTU_TABLE, len(futuRows), SECU_TABLE, len(secuRows), client.requests), flush=True)

    # ⑥ 逐条算差异并打印字段级日志
    stamp = nowUtcIso()
    print("时间戳     dt_update = %s（两张表同值）" % stamp, flush=True)
    writeTasks, pendingMismatch = [], []
    for position, record in enumerate(records, start=1):
        usc = cell(record.get("usc"))
        plans, misses = planRow(record, futuRows, secuRows, stamp)
        logPlan(position, len(records), usc, record, sourceOf.get(usc, ""), plans, misses, dryRun)
        if misses:
            pendingMismatch.append((usc, record, misses))
        for plan in plans:
            if plan["payload"]:
                writeTasks.append((usc, plan))
            else:
                stats["skippedRows"] += 1

    # ⑦ 写库（只对确有差异的行发写请求）
    print("-" * 78, flush=True)
    print("写库计划   %d 个待写请求｜%d 张表无需变更"
          % (len(writeTasks), stats["skippedRows"]), flush=True)
    if dryRun:
        print("dry-run    以上 SQL 已打印，未连接写接口。", flush=True)
        stats["written"] = len(writeTasks)
        failed = []
    else:
        done, failed = applyPlans(client, writeTasks)
        stats["written"] = len(done)
    stats["failed"] = len(failed)

    # ⑧ 未命中留痕（表内缺行 / sid 与 usc 不符等情况）
    if pendingMismatch:
        print("-" * 78, flush=True)
        print("未命中留痕 %d 条 → %s/{usc}.txt" % (len(pendingMismatch), MISMATCH_DIR), flush=True)
        for usc, record, misses in pendingMismatch:
            archiveMismatch(repoAccess, usc, record, sourceOf.get(usc, ""), misses,
                            outDir, dryRun, stats["failures"])
    stats["mismatched"] = len(pendingMismatch) + len(earlyMismatch)

    # ⑨ 汇总
    stats["requests"] = client.requests
    stats["failures"] = [("%s 写入 %s 失败" % (usc, table), reason) for usc, table, reason in failed] \
        + list(stats["failures"])
    print("=" * 78, flush=True)
    print("本轮结束：目标 %d，%s %d，写库失败 %d，无需变更 %d 张表，未命中留痕 %d"
          "（其中映射无记录 %d、映射缺字段 %d），请求 %d 次"
          % (stats["total"], "计划写入（dry-run 未执行）" if dryRun else "写库成功",
             stats["written"], stats["failed"], stats["skippedRows"], stats["mismatched"],
             stats["indexMiss"], stats["recordMiss"], client.requests), flush=True)
    for item, reason in stats["failures"][:20]:
        print("  ✗ %s：%s" % (item, reason), flush=True)
    stepSummary(stats, branch, dryRun, probability, batchSize, sampleLimit)

    if stats["failures"]:
        annotate("有 %d 项写库/提交失败，详见步骤日志与运行摘要" % len(stats["failures"]), "error")
        return RETURN_FAILED
    if stats["mismatched"]:
        annotate("有 %d 条记录未命中，已留痕到 %s/" % (stats["mismatched"], MISMATCH_DIR), "warning")
    return RETURN_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TaskError as exc:
        annotate("任务级错误：%s" % exc, "error")
        print("任务级错误：%s" % exc, file=sys.stderr, flush=True)
        sys.exit(RETURN_TASK_ERROR)
