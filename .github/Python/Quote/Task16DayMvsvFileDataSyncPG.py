#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Task16 —— FTMM V5 行情文件增量同步到自建行情服务（Upsert REST API）
========================================================================================

一、工具定位
----------------------------------------------------------------------------------------
把本仓库 quote 分支 Archive/Finv/SecuQuote/FTMM/V5/ 下的 .mvsv 行情文件读取解析后，
按批次（默认 500 行/批，可配置）以 HTTP Upsert
（POST {BASE}/API/V1/Meta/Finv/Quote/Secu/KLine/Min/Upsert?onConflict=update）
增量写到另一台自建行情服务（FinvQuoteDataDeDup）的 PostgreSQL 表
public.finv_quote_secu_kline_min，作为 Supabase 入库数据的镜像副本。

接口约定见 .github/Python/ApiUpsertBatchDelete.md：
    - 请求体 {"items": [...]}，字段集合固定 17 列，接口侧 extra="forbid"，
      多传任何未定义字段都会 422；
    - 主键 (usc, ts)；onConflict=skip 只补缺 / onConflict=update 覆盖（默认）；
    - 返回 {"affected": n, "mode": "..."}；单次上限 Limits.MaxBatchRows（默认 5000）；
    - 无鉴权，靠安全组/白名单限制来源 IP（GitHub Actions 出口 IP 需放行）。

与 Task16DayMvsvFileDataSyncSupabase 的关系（同一份数据、两个目的地）：
    - 二者读取的目录、文件排序、fileLimit 完全一致（本脚本复用其
      collect_mvsv_files / to_row / chunks / 环境变量工具，保证口径不漂移；
      仅数据行解析改为容错版 parse_mvsv_rows，见 §三）；
    - 本脚本只写数据、**不删除任何源文件**：源文件的删除仍由 Task16
      Supabase 任务在入库成功且删除开关打开时执行；
    - 工作流里本步骤排在 Supabase 步骤之前，因此本步骤失败即中断后续步骤，
      源文件不会被删除，下一轮（Upsert 幂等）重跑即可补齐，不会丢数据。

二、与 Supabase 入库的字段差异
----------------------------------------------------------------------------------------
    - 前收价：FTMM 的 LastClose 在 Supabase 表里探测 last_close / prev_close 落库，
      本接口只有 prev_close，故固定映射为 prev_close；
    - Supabase 走 PostgREST 的字段级 merge-duplicates（缺列不写），本接口
      onConflict=update 是整行覆盖 —— 两者的载荷都来自同一份完整行，结果一致；
    - 本接口不存在 paocd（当日累计均价），文件里也没有，故不下发；
    - 文件头缺列（空值）的行：Supabase 不写该列，本接口会写成 NULL（整行覆盖语义），
      属于接口既有行为，已在文档 §2.5 中说明。

三、数据合法性（下发前就地校验，绝不把脏数据打到目标库）
----------------------------------------------------------------------------------------
    - 必填：usc 非空且 ≤255、ts 为 ≥0 的整数；缺失/非法 → 整行丢弃并计为异常；
    - date ∈ [19500101, 21001231]、time ∈ [0, 235959]；
    - volume / turnover ≥ 0；数值列必须有限（NaN / Infinity 会被丢弃，
      否则 json 序列化会生成非法 JSON）；
    - provider ≤255；未定义的键一律丢弃（接口 extra="forbid"）。
    数据行解析采用「容错模式」（parse_mvsv_rows）：某一行数值非法只丢该行，
    不放弃整份文件，并逐行报出行号与原因。

    文件状态分三档：
        ok       全部批次成功，无行/字段被丢弃；
        partial  批次全部成功，但有行或字段被丢弃（数据不完整）；
        failed   存在批次失败（网络/服务端/affected 不符）或文件不可解析。

四、环境变量（凭据与配置一律经环境注入，严禁写进源码或日志）
----------------------------------------------------------------------------------------
    URL_QUOTE_SYNC_BASE   目标服务基础地址（含协议与端口，如 http://1.2.3.4:18001；
                          缺协议时按 http:// 补全）；缺失即致命退出
    PG_SYNC_BATCH_SIZE    单请求提交行数（默认 500，上限 5000，超出自动收敛）
    PG_SYNC_FILE_LIMIT    单次运行处理的文件数上限（默认 50，与 Supabase 任务对齐）
    PG_SYNC_ON_CONFLICT   冲突策略 skip / update（默认 update，只接受这两个值）
    PG_SYNC_TIMEOUT       单请求超时秒数（默认 60）
    PG_SYNC_RETRIES       失败重试次数（默认 2；仅对网络错误 / 5xx / 429 重试）
    PG_SYNC_ALLOW_PARTIAL "true"/"1" 时 partial 文件不算失败（默认严格：算失败）
    CURR_BRANCH / GITHUB_REF_NAME  或命令行第 1 参数：要处理的分支名（默认 quote）

五、退出码
----------------------------------------------------------------------------------------
    0 = 全部文件完整镜像成功；或目录里没有待同步文件；或仅有 partial 文件且
        PG_SYNC_ALLOW_PARTIAL 已开启；
    1 = 致命错误（URL 缺失 / 地址非法 / 分支未定）；存在 failed 文件；
        或存在 partial 文件而未开启 PG_SYNC_ALLOW_PARTIAL。
    说明：本步骤排在 Supabase 入库步骤之前，退出码 1 会中断后续步骤，
    从而保证「源文件在镜像写入未完整成功时不会被删除」。

【环境要求】Python 3.8+，仅标准库；需能直连 URL_QUOTE_SYNC_BASE 指定的服务。
"""

import json
import math
import os
import sys
import time as time_module
import urllib.error
import urllib.parse
import urllib.request

# 本文件位于 <仓库根>/.github/Python/Quote/ 下，与 Task16 Supabase 脚本同级；
# 同目录的模块直接 import（脚本运行时目录已在 sys.path，-m / 测试场景下补一次）。
_HERE = os.path.dirname(os.path.abspath(__file__))       # <仓库根>/.github/Python/Quote
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 复用 FTMM 解析 / 文件收集 / 分批 / 环境变量工具，避免两条链路口径分叉。
# 该模块同时会把 .github/Python 加入 sys.path 并引入 GitHubCommitContent（纯标准库）。
import Task16DayMvsvFileDataSyncSupabase as ftmm_task  # noqa: E402

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 目标服务接口（见 .github/Python/ApiUpsertBatchDelete.md §2）
API_UPSERT_PATH = "/API/V1/Meta/Finv/Quote/Secu/KLine/Min/Upsert"

# 接口允许的 items 字段（extra="forbid"，多传即 422），顺序固定以便复现请求体
ITEM_FIELDS = (
    "usc", "ts", "date", "time", "prev_close", "open", "high", "low", "close",
    "paocd", "volume", "turnover", "ext_field", "remark", "change_price",
    "change_ratio", "provider",
)
ITEM_FIELD_SET = frozenset(ITEM_FIELDS)
INT_FIELDS = ("ts", "date", "time")
NON_NEGATIVE_FIELDS = ("volume", "turnover")
TEXT_FIELDS = ("usc", "ext_field", "remark", "provider")
TEXT_LIMITS = {"usc": 255, "provider": 255}   # 接口约束：usc / provider ≤ 255

# 接口字段取值范围
DATE_MIN, DATE_MAX = 19500101, 21001231        # yyyyMMdd
TIME_MIN, TIME_MAX = 0, 235959                 # HHmmss

# 前收价在目标接口里的字段名（FTMM 文件里叫 LastClose / 前收价）
LAST_CLOSE_TARGET_FIELD = "prev_close"

# 默认配置（可被环境变量覆盖）
DEFAULT_BATCH_SIZE = 100
DEFAULT_TIMEOUT = 60
DEFAULT_RETRIES = 2
DEFAULT_ON_CONFLICT = "update"
ON_CONFLICT_CHOICES = ("skip", "update")
MAX_BATCH_ROWS = 11000                          # Limits.MaxBatchRows 默认值
RETRY_BACKOFF_SECONDS = 3.0                    # 第 n 次重试前等待 n * backoff 秒

# 日志中单次展示的异常样例条数（避免刷屏）
SAMPLE_LIMIT = 5

USER_AGENT = "Distribution-QuoteSync/1.0 (+https://github.com/ACANX/Distribution)"


# ---------------------------------------------------------------------------
# 数值/字段校验
# ---------------------------------------------------------------------------

def _as_int(value):
    """尽量把取值转为 int；无法转换或非有限值返回 None"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number)


def _as_float(value):
    """尽量把取值转为 float；无法转换或非有限值返回 None"""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def build_column_types():
    """解析前确定列映射：把 FTMM 的前收价映射到接口字段 prev_close

    与 Supabase 链路不同，这里不需要探测目标表列（接口字段名由文档固定）。

    :return: 可供 parse_mvsv/to_row 使用的列映射 dict
    """
    column_types = dict(ftmm_task.COLUMN_TYPES)
    for key in ftmm_task.LAST_CLOSE_KEYS:
        column_types[key] = (LAST_CLOSE_TARGET_FIELD, "float")
    return column_types


def sanitize_item(row):
    """把一条 mvsv 记录转成接口 items 元素（就地校验，返回丢弃原因）

    校验规则见模块文档 §三。返回的 item 里只含接口定义的字段。

    :param row: parse_mvsv 产出的 wire 键 dict
    :return: (item 或 None, 问题描述列表)
             item 为 None 表示整行不可下发（必填 usc/ts 缺失或非法）；
             问题描述非空但 item 非 None 表示个别可选字段被丢弃。
    """
    item = {}
    problems = []

    for key in row:
        if key not in ITEM_FIELD_SET:
            problems.append("未知字段 %s 已丢弃（接口 extra=forbid）" % key)

    for key in ITEM_FIELDS:
        if key not in row:
            continue
        value = row[key]

        if key in TEXT_FIELDS:
            text = "" if value is None else str(value).strip()
            if not text:
                problems.append("%s 为空，已丢弃" % key)
                continue
            limit = TEXT_LIMITS.get(key)
            if limit is not None and len(text) > limit:
                problems.append("%s 长度 %d 超过 %d，已丢弃"
                                % (key, len(text), limit))
                continue
            item[key] = text
            continue

        if key in INT_FIELDS:
            number = _as_int(value)
            if number is None:
                problems.append("%s=%r 不是整数，已丢弃" % (key, value))
                continue
            if key == "ts" and number < 0:
                problems.append("ts=%d 为负，整行丢弃" % number)
                return None, problems
            if key == "date" and not (DATE_MIN <= number <= DATE_MAX):
                problems.append("date=%d 超出 [%d, %d]，已丢弃"
                                % (number, DATE_MIN, DATE_MAX))
                continue
            if key == "time" and not (TIME_MIN <= number <= TIME_MAX):
                problems.append("time=%d 超出 [%d, %d]，已丢弃"
                                % (number, TIME_MIN, TIME_MAX))
                continue
            item[key] = number
            continue

        # 其余均为数值列（prev_close/open/high/low/close/paocd/volume/turnover/...）
        number = _as_float(value)
        if number is None:
            problems.append("%s=%r 不是有效数值，已丢弃" % (key, value))
            continue
        if key in NON_NEGATIVE_FIELDS and number < 0:
            problems.append("%s=%s 为负，已丢弃" % (key, number))
            continue
        item[key] = number

    # 必填项校验（接口 422 的直接来源）
    if not item.get("usc"):
        problems.append("缺少必填字段 usc，整行丢弃")
        return None, problems
    if "ts" not in item:
        problems.append("缺少必填字段 ts，整行丢弃")
        return None, problems
    return item, problems


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------

class SyncRequestError(Exception):
    """目标服务返回不可重试的错误（4xx / 校验失败 / affected 不符）"""


class RetryableError(Exception):
    """网络错误 / 5xx / 429：可以退避后重试"""


class PayloadTooLargeError(Exception):
    """413：批量超过服务端上限，需拆分后重发"""


def normalize_base_url(value):
    """规范化目标服务基础地址：补协议、去尾部斜杠、校验主机名

    :param value: URL_QUOTE_SYNC_BASE 原始取值
    :return: 可拼接路径的基础地址（如 http://1.2.3.4:18001）
    :raises ValueError: 地址为空或无法解析
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("目标服务地址为空（URL_QUOTE_SYNC_BASE）")
    if "://" not in text:
        print("⚠️ URL_QUOTE_SYNC_BASE 未带协议，按 http:// 补全")
        text = "http://" + text
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("目标服务地址不合法（URL_QUOTE_SYNC_BASE=%r）" % text)
    return text.rstrip("/")


def post_json(url, body_bytes, timeout):
    """发送 POST JSON 请求，返回 (status, text)；网络层失败抛 OSError

    HTTP 错误码属于服务端正常应答，原样返回给调用方判定。
    """
    request = urllib.request.Request(
        url, data=body_bytes, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:      # 先捕 HTTPError（URLError 的子类）
        return e.code, e.read().decode("utf-8", errors="replace")


class QuoteSyncClient:
    """目标行情服务 Upsert 接口的最小客户端（仅标准库 urllib）

    负责：URL 拼接、请求体序列化、状态码语义（413 拆分、5xx/429 退避重试）、
    响应解析（affected / mode）。无鉴权，服务端靠来源 IP 白名单。
    """

    def __init__(self, base_url, on_conflict=DEFAULT_ON_CONFLICT,
                 timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES):
        self.base_url = normalize_base_url(base_url)
        if on_conflict not in ON_CONFLICT_CHOICES:
            raise ValueError("onConflict 只接受 %s，当前为 %r"
                             % ("/".join(ON_CONFLICT_CHOICES), on_conflict))
        self.on_conflict = on_conflict
        self.timeout = timeout
        self.retries = max(0, retries)

    def upsert_url(self):
        """拼接 Upsert 完整 URL（含 onConflict 查询参数）"""
        return "%s%s?onConflict=%s" % (
            self.base_url, API_UPSERT_PATH,
            urllib.parse.quote(self.on_conflict, safe=""))

    def upsert(self, items):
        """提交一批 items；遇到 413 自动二分重试

        :param items: 已通过 sanitize_item 校验的记录列表
        :return: dict {"affected": int|None, "mode": str|None, "requests": int}
                 affected 为 None 表示服务端未回传可解析的数值
        :raises SyncRequestError: 不可重试错误或重试耗尽
        """
        try:
            return self._upsert_once(items)
        except PayloadTooLargeError:
            if len(items) <= 1:
                raise SyncRequestError("单行请求仍返回 413，服务端批量上限过小")
            middle = len(items) // 2
            print("⚠️ 目标服务返回 413（批量过大），拆分为 %d + %d 行重试"
                  % (middle, len(items) - middle))
            left = self.upsert(items[:middle])
            right = self.upsert(items[middle:])
            return merge_upsert_results(left, right)

    def _upsert_once(self, items):
        """单批提交（内部按 retries 做退避重试）"""
        body = json.dumps({"items": items}, ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
        url = self.upsert_url()
        last_error = None

        for attempt in range(self.retries + 1):
            if attempt:
                wait = RETRY_BACKOFF_SECONDS * attempt
                print("⏳ 第 %d/%d 次重试前等待 %.1fs" % (attempt, self.retries, wait))
                time_module.sleep(wait)
            try:
                status, text = post_json(url, body, self.timeout)
            except OSError as e:                       # 含 URLError / TimeoutError
                last_error = "网络错误: %s" % e
                print("⚠️ 提交 %d 行失败：%s" % (len(items), last_error))
                continue

            if 200 <= status < 300:
                return parse_upsert_response(text, len(items))
            if status == 413:
                raise PayloadTooLargeError("HTTP 413：请求过大")
            if status == 429 or status >= 500:
                last_error = "HTTP %s：%s" % (status, snippet(text))
                print("⚠️ 提交 %d 行失败：%s" % (len(items), last_error))
                continue
            raise SyncRequestError("HTTP %s：%s" % (status, snippet(text)))

        raise SyncRequestError("重试 %d 次后仍失败（%s）" % (self.retries, last_error))


def snippet(text, limit=300):
    """截断响应体，避免错误日志刷屏"""
    body = (text or "").strip().replace("\n", " ")
    return body[:limit] + ("..." if len(body) > limit else "")


def parse_upsert_response(text, submitted):
    """解析 Upsert 响应 {"affected": n, "mode": "skip|update"}

    update 模式下 affected 应当等于提交行数；若服务端明确回传更小的值，
    说明有行没有落库，按失败处理（skip 模式的小值是预期行为）。

    :param text: 响应体
    :param submitted: 本次提交的行数
    :return: dict {"affected": int|None, "mode": str|None, "requests": 1}
    :raises SyncRequestError: 回传 affected 与 update 语义不符
    """
    data = None
    if (text or "").strip():
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    if not isinstance(data, dict):
        return {"affected": None, "mode": None, "requests": 1}

    affected = data.get("affected")
    if isinstance(affected, bool) or not isinstance(affected, int):
        affected = None
    mode = data.get("mode")
    if not isinstance(mode, str):
        mode = None

    if (mode or "update") != "skip" and affected is not None \
            and affected < submitted:
        raise SyncRequestError("回传 affected=%d 小于提交行数 %d"
                               % (affected, submitted))
    return {"affected": affected, "mode": mode, "requests": 1}


def merge_upsert_results(left, right):
    """合并一次 413 拆分后的两次写入结果（affected 未知则整体未知）"""
    if left["affected"] is None or right["affected"] is None:
        affected = None
    else:
        affected = left["affected"] + right["affected"]
    return {
        "affected": affected,
        "mode": left["mode"] or right["mode"],
        "requests": left["requests"] + right["requests"],
    }


# ---------------------------------------------------------------------------
# 解析（容错版）
# ---------------------------------------------------------------------------

def parse_mvsv_rows(path, column_types):
    """解析 FTMM V5 行情文件：单行数值非法只丢该行，不放弃整份文件

    列头 / 元信息规则与 ftmm_task.parse_mvsv 完全一致（复用其 unquote /
    FIELD_HEADER_KEYS / to_row），差异只在于数据行的数值解析失败时不抛异常，
    而是把该行记入 bad_rows 继续解析——行情文件是机器生成的，个别脏行不应让整份
    文件长期卡在队列里；但脏行仍会进日志并影响文件状态（partial）。

    :param path: 文件路径
    :param column_types: 文件头列名 → (wire 键, 类型) 映射
    :return: (记录列表, 脏行列表 [(行号, 原因)], 元数据 dict)
    :raises ValueError: 文件头缺失 / 数据行出现在字段头之前 / 证券代码缺失
    """
    columns = None
    meta = {}
    rows = []
    bad_rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line_no, raw in enumerate(f, 1):
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                body = line[1:].strip()
                if ":" not in body:
                    continue
                key, value = body.split(":", 1)
                key = key.strip()
                value = ftmm_task.unquote(value.strip())
                meta.setdefault(key, value)
                if key in ftmm_task.FIELD_HEADER_KEYS:
                    columns = value.split("|")
                continue
            if columns is None:
                raise ValueError("第 %d 行数据出现在 # FieldName 头之前" % line_no)
            values = line.split("|")
            if len(values) != len(columns):
                bad_rows.append((line_no, "列数 %d 与字段头 %d 不符"
                                 % (len(values), len(columns))))
                continue
            try:
                rows.append(ftmm_task.to_row(columns, values, column_types))
            except (ValueError, TypeError) as e:
                bad_rows.append((line_no, "数值非法（%s）" % e))

    if columns is None:
        raise ValueError("文件缺少 # FieldName / # 字段名称 元信息头")
    if all(column not in column_types for column in columns):
        # 典型情形：只有短名头（# 字段 : ts|d|t|...）——短名 t 同时表示 Time/Turnover，
        # 无法安全映射；直接报清楚，避免“每行都缺 ts”这种二次伤害的日志
        raise ValueError("文件头列名无法识别（需要 # FieldName 英文全名或 "
                         "# 字段名称 中文名）：%s" % "|".join(columns))
    usc = (meta.get("证券代码") or meta.get("SecuCode") or meta.get("USC") or "")
    if not usc:
        raise ValueError("文件头缺少 证券代码 / SecuCode / USC")
    provider = meta.get("数据供应商") or meta.get("DataProvider") or ""
    for row in rows:
        row["usc"] = usc
        if provider:
            row["provider"] = provider
    return rows, bad_rows, meta


# ---------------------------------------------------------------------------
# 单文件同步
# ---------------------------------------------------------------------------

def sync_file(client, root, rel_path, batch_size, column_types):
    """把一份行情文件的全部记录分批同步到目标服务，逐批记录明细

    单批失败不中断后续批次——跑完才能给出完整失败清单；Upsert 幂等，重跑无副作用。

    :param client: QuoteSyncClient
    :param root: 仓库根目录
    :param rel_path: 文件相对路径（quote 分支内）
    :param batch_size: 单批行数
    :param column_types: 文件头列名 → (wire 键, 类型) 映射
    :return: 文件级汇总 dict：
             {"rel_path", "parsed", "dropped_rows", "dropped_fields", "batches",
              "status", "failure_digest"}，status 取 ok / partial / failed
    :raises Exception: 文件头不可解析 / 无可下发数据时抛出（未发过写请求）
    """
    local_path = os.path.join(root, *rel_path.split("/"))
    rows, bad_rows, _meta = parse_mvsv_rows(local_path, column_types)
    if not rows and not bad_rows:
        raise ValueError("解析结果为空，文件可能已损坏")

    items = []
    dropped_records = []
    dropped_fields = []
    for record_no, row in enumerate(rows, 1):
        item, problems = sanitize_item(row)
        if item is None:
            dropped_records.append((record_no, problems))
            continue
        if problems:
            dropped_fields.append((record_no, problems))
        items.append(item)

    dropped_rows = len(bad_rows) + len(dropped_records)
    print("📄 %s：解析 %d 行，脏行 %d 行，可同步 %d 行，整行丢弃 %d 行（含脏行），"
          "字段丢弃 %d 行"
          % (rel_path, len(rows) + len(bad_rows), len(bad_rows), len(items),
             dropped_rows, len(dropped_fields)))
    log_samples("脏行", bad_rows)
    log_samples("整行丢弃", dropped_records, unit="条记录")
    log_samples("字段丢弃", dropped_fields, unit="条记录")

    if not items:
        raise ValueError("全部行都未通过校验，无可下发数据")

    batches = []
    for batch_no, chunk in enumerate(ftmm_task.chunks(items, batch_size), 1):
        batches.append(upsert_batch(client, batch_no, chunk))
    return summarize_file(rel_path, len(rows) + len(bad_rows), dropped_rows,
                          len(dropped_fields), batches)


def log_samples(title, entries, unit="行"):
    """打印异常样例（最多 SAMPLE_LIMIT 条），其余仅给总数

    :param title: 分类名（脏行 / 整行丢弃 / 字段丢弃）
    :param entries: [(编号, 原因字符串或原因列表), ...]
    :param unit: 编号的含义（行号 / 记录序号）
    """
    if not entries:
        return
    for number, problems in entries[:SAMPLE_LIMIT]:
        detail = problems if isinstance(problems, str) else "；".join(problems)
        print("   ⚠️ %s 第 %d %s：%s" % (title, number, unit, detail))
    if len(entries) > SAMPLE_LIMIT:
        print("   ⚠️ %s 其余 %d 条同上，不再逐条打印"
              % (title, len(entries) - SAMPLE_LIMIT))


def upsert_batch(client, batch_no, chunk):
    """提交一个批次并返回明细（失败只记录，不抛出）"""
    try:
        result = client.upsert(chunk)
        error = None
    except Exception as e:
        result = {"affected": None, "mode": None, "requests": 0}
        error = "%s" % e

    outcome = {
        "batch_no": batch_no,
        "submitted": len(chunk),
        "affected": result["affected"],
        "mode": result["mode"],
        "requests": result["requests"],
        "error": error,
    }
    print("%s 第 %d 批 %s" % ("✅" if error is None else "❌", batch_no,
                             describe_batch(outcome)))
    return outcome


def describe_batch(outcome):
    """生成一行可读的批次明细"""
    affected = outcome["affected"]
    affected_text = "未知" if affected is None else str(affected)
    suffix = "" if outcome["error"] is None else "，原因：%s" % outcome["error"]
    return ("提交 %d 行：affected=%s（mode=%s，请求 %d 次）%s"
            % (outcome["submitted"], affected_text, outcome["mode"] or "未回传",
               outcome["requests"], suffix))


def summarize_file(rel_path, parsed, dropped_rows, dropped_fields, batches):
    """生成文件级汇总与状态（ok / partial / failed）

    - failed：任一批次失败（网络/服务端/affected 不符）；
    - partial：批次均成功但有行或字段被丢弃；
    - ok：完整镜像。
    """
    failed_batches = [b for b in batches if b["error"] is not None]
    if failed_batches:
        status = "failed"
    elif dropped_rows or dropped_fields:
        status = "partial"
    else:
        status = "ok"

    submitted = sum(b["submitted"] for b in batches)
    affected_known = [b["affected"] for b in batches if b["affected"] is not None]
    affected_unknown = any(b["affected"] is None for b in batches)
    affected_text = ("至少 %d" % sum(affected_known)) if affected_unknown \
        else str(sum(affected_known))

    digest_parts = []
    if failed_batches:
        digest_parts.append("；".join("第 %d 批: %s" % (b["batch_no"], b["error"])
                                      for b in failed_batches))
    if dropped_rows:
        digest_parts.append("整行丢弃 %d 行" % dropped_rows)
    if dropped_fields:
        digest_parts.append("字段丢弃 %d 行" % dropped_fields)
    digest = "；".join(digest_parts)

    icon = {"ok": "✅", "partial": "⚠️", "failed": "❌"}[status]
    print("%s %s 汇总：解析 %d 行，提交 %d 行，affected %s，批次 %d（失败 %d）→ %s%s"
          % (icon, rel_path, parsed, submitted, affected_text, len(batches),
             len(failed_batches), status.upper(),
             "（完整同步）" if status == "ok" else "（详见上方 ⚠️ / ❌ 日志）"))
    return {
        "rel_path": rel_path,
        "parsed": parsed,
        "dropped_rows": dropped_rows,
        "dropped_fields": dropped_fields,
        "batches": batches,
        "status": status,
        "failure_digest": digest,
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def env_int_zero(name, default):
    """读取非负整数型环境变量；非法值告警后取默认值"""
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
        if parsed < 0:
            raise ValueError
        return parsed
    except ValueError:
        print("⚠️ 环境变量 %s=%r 不是非负整数，回退默认值 %d" % (name, value, default))
        return default


def resolve_on_conflict():
    """读取冲突策略（skip / update），非法值回落 update 并告警"""
    value = os.environ.get("PG_SYNC_ON_CONFLICT", "").strip().lower()
    if not value:
        return DEFAULT_ON_CONFLICT
    if value not in ON_CONFLICT_CHOICES:
        print("⚠️ PG_SYNC_ON_CONFLICT=%r 非法（只接受 skip / update），回退 %s"
              % (value, DEFAULT_ON_CONFLICT))
        return DEFAULT_ON_CONFLICT
    return value


def main():
    """主流程：配置 → 收集排序 → 逐文件分批 Upsert → 汇总"""
    ftmm_task._ensure_console_utf8()
    branch = ftmm_task.resolve_branch()
    batch_size = min(ftmm_task.env_int("PG_SYNC_BATCH_SIZE", DEFAULT_BATCH_SIZE),
                     MAX_BATCH_ROWS)
    file_limit = ftmm_task.env_int("PG_SYNC_FILE_LIMIT", ftmm_task.DEFAULT_FILE_LIMIT)
    timeout = ftmm_task.env_int("PG_SYNC_TIMEOUT", DEFAULT_TIMEOUT)
    retries = env_int_zero("PG_SYNC_RETRIES", DEFAULT_RETRIES)
    on_conflict = resolve_on_conflict()
    allow_partial = ftmm_task.env_bool("PG_SYNC_ALLOW_PARTIAL", False)

    print("======== 自建行情服务同步开始（Task16 / FTMM V5 / PG）========")
    print("分支 (branch) = %s" % branch)
    print("批大小 (batchSize) = %d，文件数上限 (fileLimit) = %d，冲突策略 (onConflict) = %s"
          % (batch_size, file_limit, on_conflict))
    print("超时 (timeout) = %ds，重试 (retries) = %d，允许部分同步 (allowPartial) = %s"
          % (timeout, retries, "是" if allow_partial else "否（默认严格）"))

    base_url = os.environ.get("URL_QUOTE_SYNC_BASE", "").strip()
    if not base_url:
        print("❌ 缺少 URL_QUOTE_SYNC_BASE 环境变量（目标服务基础地址，"
              "如 http://1.2.3.4:18001），无法同步")
        return 1
    try:
        client = QuoteSyncClient(base_url, on_conflict=on_conflict,
                                 timeout=timeout, retries=retries)
    except ValueError as e:
        print("❌ %s" % e)
        return 1
    print("目标服务 = %s" % client.base_url)

    column_types = build_column_types()
    root = ftmm_task.repo_root()
    print("仓库根 = %s" % root)

    all_files = ftmm_task.collect_mvsv_files(root)
    print("收集完成：FTMM/V5 目录下可同步的 .mvsv 共 %d 个（按文件名日期升序、证券标识自然排序）"
          % len(all_files))
    if not all_files:
        print("没有可同步的 .mvsv 文件，无事可做")
        return 0
    picked = all_files[:file_limit]
    if len(picked) < len(all_files):
        print("本次按 fileLimit=%d 只处理前 %d 个，其余 %d 个留待下次："
              % (file_limit, len(picked), len(all_files) - len(picked)))
        for rel_path, _d, _c in all_files[file_limit:]:
            print("    - %s" % rel_path)
    print("-------- 本次处理清单（%d 个）--------" % len(picked))
    for idx, (rel_path, date, code) in enumerate(picked, 1):
        print("  %2d. %s（日期=%d，标识=%s）" % (idx, rel_path, date, code))

    failed_files, partial_files = [], []
    for rel_path, _date, _code in picked:
        try:
            outcome = sync_file(client, root, rel_path, batch_size, column_types)
        except Exception as e:
            # 文件头不可解析 / 无可下发数据：没发过写请求，源文件必须保留
            print("❌ 文件 %s 解析/同步过程异常：%s" % (rel_path, e))
            failed_files.append("%s：%s" % (rel_path, e))
            continue
        if outcome["status"] == "failed":
            print("🔒 %s 同步失败（批次未全部成功）：%s"
                  % (rel_path, outcome["failure_digest"]))
            failed_files.append("%s：%s" % (rel_path, outcome["failure_digest"]))
        elif outcome["status"] == "partial":
            print("⚠️ %s 部分同步（有行/字段被丢弃）：%s"
                  % (rel_path, outcome["failure_digest"]))
            partial_files.append("%s：%s" % (rel_path, outcome["failure_digest"]))

    synced = len(picked) - len(failed_files) - len(partial_files)
    print("======== 自建行情服务同步结束（Task16 / FTMM V5 / PG）========")
    print("合计：文件 %d 份，完整同步 %d 份，部分同步 %d 份，失败 %d 份"
          % (len(picked), synced, len(partial_files), len(failed_files)))
    if partial_files:
        print("部分同步清单（有行/字段被丢弃，数据不完整）：")
        for item in partial_files:
            print("    - %s" % item)
    if failed_files:
        print("失败清单（修复后重跑即可补齐，Upsert 幂等；"
              "本步骤失败会阻断后续 Supabase 步骤，源文件不会被删除）：")
        for item in failed_files:
            print("    - %s" % item)

    if failed_files:
        return 1
    if partial_files and not allow_partial:
        print("❌ 存在部分同步的文件：默认严格模式按失败返回；"
              "若确认可接受，设 PG_SYNC_ALLOW_PARTIAL=true 降级为告警")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
