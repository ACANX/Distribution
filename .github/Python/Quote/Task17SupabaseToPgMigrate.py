#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Task17 —— Supabase 存量行情数据分批迁移到自建行情服务（Upsert 成功后删除源表数据）
========================================================================================

一、工具定位
----------------------------------------------------------------------------------------
把 Supabase 表 public.finv_quote_secu_kline_min 里的存量行情分批迁出：每批通过
自建行情服务的 Upsert 接口
（POST {BASE}/API/V1/Meta/Finv/Quote/Secu/KLine/Min/Upsert?onConflict=update）
写入其 PostgreSQL 表 public.finv_quote_secu_kline_min，
**整批写入成功后再从 Supabase 删除这批已同步的行**，直到存量清空、
或达到批次上限（默认 50000 批）为止。

接口约定见 .github/Python/ApiUpsertBatchDelete.md §2：
    - 请求体 {"items": [...]}，字段集合固定 17 列，接口侧 extra="forbid"，
      多传任何未定义字段都会 422；
    - 主键 (usc, ts)；onConflict=skip 只补缺 / onConflict=update 覆盖（默认）；
    - 返回 {"affected": n, "mode": "..."}；单次上限 Limits.MaxBatchRows（默认 5000）；
    - 无鉴权，靠安全组/白名单限制来源 IP。

二、分批规则（优先级：时间越早越先；每批只含单个证券）
----------------------------------------------------------------------------------------
    1) 先取证券清单：用主键 (usc, ts) 做索引扫描（不走聚合、不按 ts 全局排序），
          select=usc,ts&order=usc.asc,ts.asc&limit=1000[&usc=gt.上一页末行证券]
       每个 usc 首次出现的那一行就是该证券的最早 ts；清单再按「各证券最早数据的 ts」
       升序（ts 相同按 usc 升序），因此越早的行情越先迁移。
       （Supabase 默认禁用聚合函数，会报 PGRST123；大表又没有 ts 索引，
       按 ts 排序会触发 statement timeout，所以两种都不采用，见 §七）
    2) 逐个证券处理：证券之间串行，一次只处理一个 usc；
    3) 单批单证券：证券内部按 ts 升序，每批最多 MIGRATE_BATCH_SIZE 行（默认 100），
       一个批次只包含一个 usc，便于目标库按证券维度定位问题；
    4) 证券内用 ts 键集分页（ts > 上一批末行 ts），不用 offset：
       即使某批没有被删除（写失败 / 关闭删除开关），分页也能前进，
       不会在同一批上反复打转；关闭删除时也能在批次上限内把证券清单跑完。

    不落任何状态文件：中途失败直接重跑即可 —— 证券清单与分页都从源表重新算，
    已迁移的行已从 Supabase 删除，未迁移的会被重新读出，Upsert 幂等，既不重复也不丢数据。
    证券清单在开跑时只算一次：运行期间新写入的证券（如另一个 Task16 任务正在入库）
    不会进入本轮清单，留待下次运行处理。

三、删除范围与保护
----------------------------------------------------------------------------------------
    - 只删除「本批成功写入目标库」的那些行，按键 (usc, ts) 精确删除
      （PostgREST DELETE + or=(and(usc.eq.X,ts.eq.Y),...)），单次请求最多
      DELETE_KEYS_PER_REQUEST 个条件（控制 URL 长度，超出自动拆成多次请求）；
    - 删除行数与已同步行数不一致 → 默认判失败并停止（可设
      MIGRATE_ALLOW_DELETE_MISMATCH=true 降级为告警）；
    - 行级校验不通过（缺 usc/ts、数值非法等）的行**不写入也不删除**，留在 Supabase，
      日志逐条给出原因，默认让整次运行返回失败（可设
      MIGRATE_ALLOW_SKIPPED_ROWS=true 降级为告警）；
    - MIGRATE_ENABLE_DELETE=false 时只迁移不删除（演练 / 只做镜像）。

四、与其它任务的关系
----------------------------------------------------------------------------------------
    - 与 Task16DayMvsvFileDataSyncPG 的区别：Task16 处理的是「quote 分支上的增量
      文件」，本脚本处理的是「Supabase 里已经入库的存量行」，二者写入同一个目标表；
    - 与 Task16DayMvsvFileDataSyncSupabase 的区别：那个脚本写 Supabase，
      本脚本读 Supabase 并删除已迁移的行；
    - 本脚本不处理 quote 分支上的任何文件，只在两张表之间搬数据。

五、环境变量（凭据与配置一律经环境注入，严禁写进源码或日志）
----------------------------------------------------------------------------------------
    SUPABASE_PROJECT_REF        源 Supabase 项目引用（Dashboard 地址中 .supabase.co 之前一段）
    SUPABASE_KEY                Supabase API 密钥（service-role；不落日志）
    URL_QUOTE_SYNC_BASE         目标服务基础地址（含协议与端口，如 http://1.2.3.4:18001）
    MIGRATE_BATCH_SIZE          单批行数（默认 100，上限 5000，超出自动收敛；单批只含一个证券）
    MIGRATE_MAX_BATCHES         单次运行的批次上限（默认 50000，达到即停止，重跑可继续）
    MIGRATE_ON_CONFLICT         冲突策略 skip / update（默认 update）
    MIGRATE_ENABLE_DELETE       "false"/"0" 时只迁移不删除（默认 true：同步成功即删源数据）
    MIGRATE_CONTINUE_ON_ERROR   "true"/"1" 时单批失败后继续往后处理（默认立即停止）
    MIGRATE_ALLOW_SKIPPED_ROWS  "true"/"1" 时行级校验失败只告警、不算失败
    MIGRATE_ALLOW_DELETE_MISMATCH "true"/"1" 时删除行数不符只告警、不算失败
    MIGRATE_TIMEOUT             目标服务单请求超时秒数（默认 60）
    MIGRATE_SUPABASE_TIMEOUT    Supabase 单请求超时秒数（默认 60）
    MIGRATE_RETRIES             失败重试次数（默认 2；仅对网络错误 / 5xx / 429 重试）

六、退出码
----------------------------------------------------------------------------------------
    0 = 证券清单已跑完；或达到 MIGRATE_MAX_BATCHES 正常收工；或源表本来就没有数据；
        以及全部「降级为告警」的情形（skipped rows / delete mismatch 已放行）；
    1 = 致命错误（凭据缺失 / 地址非法 / 源表缺 usc 或 ts 列 / 无法定位证券或推进分页）；
        存在失败批次；或仍有行级校验失败且未开启 MIGRATE_ALLOW_SKIPPED_ROWS；
        或存在删除行数不符且未开启 MIGRATE_ALLOW_DELETE_MISMATCH。

七、实现约束（自包含 / 查询必须走索引）
----------------------------------------------------------------------------------------
    本文件不依赖同目录任何 Python 模块，只用标准库（urllib / json / math）。

    Supabase 侧对「全表扫描 / 未索引排序 / 聚合」的容忍度很低：
        - 聚合函数默认禁用 → PGRST123 "Use of aggregate functions is not allowed"；
        - 大表按未索引列（如 ts）排序会 statement timeout → PostgreSQL 57014。
    因此所有查询都命中主键 (usc, ts)：
        - 证券清单：order=usc.asc,ts.asc + usc=gt.游标（主键前缀索引扫描）；
        - 证券内分页：usc=eq.X + order=ts.asc + ts=gt.游标（主键范围扫描）；
        - 删除：or=(and(usc.eq.X,ts.eq.Y),...)（主键等值条件）。
    遇到 57014 不重试（重试无意义），直接报错并提示索引问题。

    注意：本脚本会**删除 Supabase 中的存量数据**，属不可逆操作。建议先在
    MIGRATE_ENABLE_DELETE=false 下演练、核对日志（读了多少、写了多少、跳过了哪些行），
    确认无误后再开删除。

【环境要求】Python 3.8+，仅标准库；需能直连 *.supabase.co 与 URL_QUOTE_SYNC_BASE。
"""

import json
import math
import os
import sys
import time as time_module
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 目标服务接口（见 .github/Python/ApiUpsertBatchDelete.md §1）
API_BASE_PATH = "/API/V1/Meta/Finv/Quote/Secu/KLine/Min"
API_UPSERT_PATH = API_BASE_PATH + "/Upsert"

# Supabase / PostgREST 源表（与 Task16 入库表一致）
TABLE = "finv_quote_secu_kline_min"
REST_URL_TEMPLATE = "https://%s.supabase.co/rest/v1"

# 目标接口允许的 items 字段（extra="forbid"，多传即 422），顺序固定以便复现请求体
ITEM_FIELDS = (
    "usc", "ts", "date", "time", "prev_close", "open", "high", "low", "close",
    "paocd", "volume", "turnover", "ext_field", "remark", "change_price",
    "change_ratio", "provider",
)
INT_FIELDS = ("ts", "date", "time")
NON_NEGATIVE_FIELDS = ("volume", "turnover")
TEXT_FIELDS = ("usc", "ext_field", "remark", "provider")
TEXT_LIMITS = {"usc": 255, "provider": 255}    # 接口约束：usc / provider ≤ 255

# 接口字段取值范围
DATE_MIN, DATE_MAX = 19500101, 21001231         # yyyyMMdd
TIME_MIN, TIME_MAX = 0, 235959                  # HHmmss

# 前收价：源表可能叫 prev_close 或 last_close，目标接口固定叫 prev_close
LAST_CLOSE_SOURCE_COLUMNS = ("prev_close", "last_close")
LAST_CLOSE_TARGET_FIELD = "prev_close"

# 分页 / 删除所需的主键列（源表必须存在）
KEY_COLUMNS = ("usc", "ts")
SECURITY_ORDER = "usc.asc,ts.asc"              # 主键 (usc, ts) 顺序扫描（走索引）
USC_ORDER = "ts.asc"                            # 单证券内部按时间升序

# 默认配置（可被环境变量覆盖）
DEFAULT_BATCH_SIZE = 100
DEFAULT_MAX_BATCHES = 50000
DEFAULT_ON_CONFLICT = "update"
ON_CONFLICT_CHOICES = ("skip", "update")
DEFAULT_TIMEOUT = 60
DEFAULT_SUPABASE_TIMEOUT = 60
DEFAULT_RETRIES = 2
RETRY_BACKOFF_SECONDS = 3.0                     # 第 n 次重试前等待 n * backoff 秒

MAX_BATCH_ROWS = 5000                           # 目标接口 Limits.MaxBatchRows 默认值
DELETE_KEYS_PER_REQUEST = 40                    # 单次 DELETE 的 (usc, ts) 条件数上限

# 证券清单：按主键顺序扫描的单页行数 / 证券数安全阀
# （每页最多 1000 行索引记录，跨页用 usc=gt.上一页末行证券 推进）
SECURITY_SCAN_PAGE = 1000
MAX_SECURITIES = 200000

# 日志中单次展示的异常样例条数（避免刷屏）
SAMPLE_LIMIT = 5

USER_AGENT = "Distribution-QuoteMigrate/1.0 (+https://github.com/ACANX/Distribution)"

# 过滤器里需要用双引号包裹的保留字符（逗号 / 括号 / 双引号 / 空格）
FILTER_RESERVED_CHARS = ' ,()"'


# ---------------------------------------------------------------------------
# 通用工具（自包含实现，不依赖同目录脚本）
# ---------------------------------------------------------------------------

_console_configured = False


def _ensure_console_utf8():
    """把 stdout/stderr 重配置为 UTF-8（errors=replace），仅首次调用生效

    防止 Windows 遗留控制台 / 非 UTF-8 locale 下输出中文时抛 UnicodeEncodeError
    导致静默崩溃。重配置失败（如流已被重定向接管）时静默跳过。
    """
    global _console_configured
    if _console_configured:
        return
    _console_configured = True
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
        except (AttributeError, ValueError, OSError):
            pass


def env_int(name, default):
    """读取正整数型环境变量；非法值告警后取默认值"""
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
        if parsed <= 0:
            raise ValueError
        return parsed
    except ValueError:
        print("⚠️ 环境变量 %s=%r 不是正整数，回退默认值 %d" % (name, value, default))
        return default


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


def env_bool(name, default=False):
    """读取布尔型环境变量（"true"/"1"/"yes"/"on" 为真，大小写不敏感）"""
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in ("true", "1", "yes", "on")


def resolve_on_conflict():
    """读取冲突策略（skip / update），非法值回落 update 并告警"""
    value = os.environ.get("MIGRATE_ON_CONFLICT", "").strip().lower()
    if not value:
        return DEFAULT_ON_CONFLICT
    if value not in ON_CONFLICT_CHOICES:
        print("⚠️ MIGRATE_ON_CONFLICT=%r 非法（只接受 skip / update），回退 %s"
              % (value, DEFAULT_ON_CONFLICT))
        return DEFAULT_ON_CONFLICT
    return value


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


def chunks(source, size):
    """把列表切成固定大小的片"""
    return [source[i:i + size] for i in range(0, len(source), size)]


def snippet(text, limit=300):
    """截断响应体，避免错误日志刷屏"""
    body = (text or "").strip().replace("\n", " ")
    return body[:limit] + ("..." if len(body) > limit else "")


def filter_value(value):
    """把取值转成 PostgREST 过滤表达式里的字面量

    含保留字符（逗号 / 括号 / 双引号 / 空格）或为空时用双引号包裹，
    其余情况原样输出（如 usc=000001.SZ 不需要引号）。
    """
    text = "" if value is None else str(value)
    if not text or any(ch in text for ch in FILTER_RESERVED_CHARS):
        return '"%s"' % text.replace('"', '\\"')
    return text


def encode_fragment(text):
    """对拼好的过滤片段做 URL 编码，保留 PostgREST 的结构字符"""
    return urllib.parse.quote(text, safe="=(),.")


def cursor_key(record):
    """取一行记录的 (ts:int, usc:str)；无法定位时返回 None"""
    if not isinstance(record, dict):
        return None
    ts = _as_int(record.get("ts"))
    if ts is None:
        return None
    usc = record.get("usc")
    text = "" if usc is None else str(usc)
    if not text.strip():
        return None
    return ts, text


# ---------------------------------------------------------------------------
# 记录校验（源表行 → 目标接口 items 元素）
# ---------------------------------------------------------------------------

def build_item(record):
    """把一行 Supabase 记录转成接口 items 元素（就地校验，返回丢弃原因）

    与 Task16DayMvsvFileDataSyncPG 的 sanitize_item 的差异：源表里的 NULL 列是
    正常现象（增量入库时未写入的列），因此「缺列 / 空值」一律静默跳过，
    只有「取到了但非法」的值才记入 problems。

    :param record: PostgREST 返回的行 dict
    :return: (item 或 None, 问题描述列表)
             item 为 None 表示整行不可下发（必填 usc/ts 缺失或非法）；
             问题描述非空但 item 非 None 表示个别可选字段被丢弃。
    """
    item = {}
    problems = []

    for key in ITEM_FIELDS:
        if key == LAST_CLOSE_TARGET_FIELD:
            value = record.get("prev_close")
            if value is None:
                value = record.get("last_close")
        else:
            value = record.get(key)
        if value is None:
            continue

        if key in TEXT_FIELDS:
            text = str(value).strip()
            if not text:
                continue                      # 空字符串等同缺列，静默跳过
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
                problems.append("ts=%d 为负，整行保留不迁移" % number)
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
        problems.append("缺少必填字段 usc，整行保留不迁移")
        return None, problems
    if "ts" not in item:
        problems.append("缺少必填字段 ts，整行保留不迁移")
        return None, problems
    return item, problems


def resolve_columns(discovered):
    """从源表实际列集合里挑出需要读取的列（目标字段白名单 ∩ 实际列）

    :param discovered: discover_columns() 得到的实际列集合
    :return: 读取列名列表（保证含 usc / ts；前收价自动识别 last_close）
    :raises MigrationError: 源表缺少 usc / ts
    """
    missing = [name for name in KEY_COLUMNS if name not in discovered]
    if missing:
        raise MigrationError("源表 %s 缺少必需列：%s（无法按键集分页与删除）"
                             % (TABLE, "、".join(missing)))
    columns = [name for name in ITEM_FIELDS if name in discovered]
    if "prev_close" not in columns:
        for name in LAST_CLOSE_SOURCE_COLUMNS:
            if name in discovered:
                columns.append(name)
                break
    return columns


def security_row(row):
    """取一行清单记录的 (usc, first_ts)；无法定位时返回 None"""
    key = cursor_key(row)
    if key is None:
        return None
    return key[1], key[0]


# ---------------------------------------------------------------------------
# HTTP 基础
# ---------------------------------------------------------------------------

def http_request(url, method, body_bytes=None, headers=None, timeout=30):
    """发送 HTTP 请求，返回 (status, text, headers)；网络层失败抛 OSError

    HTTP 错误码属于服务端正常应答，原样返回给调用方判定。
    """
    request_headers = dict(headers or {})
    request_headers.setdefault("User-Agent", USER_AGENT)
    request = urllib.request.Request(
        url, data=body_bytes, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return (resp.status, resp.read().decode("utf-8", errors="replace"),
                    resp.headers)
    except urllib.error.HTTPError as e:      # 先捕 HTTPError（URLError 的子类）
        return (e.code, e.read().decode("utf-8", errors="replace"), e.headers)


class MigrationError(Exception):
    """不可重试的致命错误（凭据 / 结构 / 服务端 4xx / 数量校验不符）"""


class PayloadTooLargeError(Exception):
    """413：批量超过服务端上限，需拆分后重发"""


class SyncRequestError(Exception):
    """目标服务返回不可重试的错误（4xx / 校验失败 / affected 不符）"""


# ---------------------------------------------------------------------------
# Supabase（PostgREST）源表客户端
# ---------------------------------------------------------------------------

class SupabaseTable:
    """Supabase 源表的最小客户端：探测列、列证券清单、证券内分页、按键批量删除

    只用标准库 urllib；每个请求同时携带 apikey 与 Authorization: Bearer
    （Supabase 要求二者并存）；密钥不落日志，URL 中的项目引用一律遮蔽后输出。
    """

    def __init__(self, project_ref, api_key, table=TABLE,
                 timeout=DEFAULT_SUPABASE_TIMEOUT, retries=DEFAULT_RETRIES):
        ref = (project_ref or "").strip()
        if not ref:
            raise ValueError("Supabase 项目引用（SUPABASE_PROJECT_REF）不能为空")
        if "/" in ref or ":" in ref:
            raise ValueError("项目引用只填 ref 本身，不要带协议或域名")
        if not (api_key or "").strip():
            raise ValueError("Supabase API 密钥（SUPABASE_KEY）不能为空")
        self.project_ref = ref
        self.rest_url = REST_URL_TEMPLATE % ref
        self.api_key = api_key.strip()
        self.table = table
        self.timeout = timeout
        self.retries = max(0, retries)

    def _redact(self, text):
        """把文本中的项目引用（嵌在主机名里）替换为 ***，供日志输出"""
        return text.replace(self.project_ref, "***")

    def _send(self, method, query, body=None, prefer=None, operation="请求"):
        """发送请求并返回 (text, headers)；网络错误 / 429 / 5xx 退避重试

        :raises MigrationError: 重试耗尽或服务端返回 4xx
        """
        url = "%s/%s" % (self.rest_url, urllib.parse.quote(self.table, safe=""))
        if query:
            url = url + "?" + query
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8") \
            if body is not None else None
        headers = {
            "apikey": self.api_key,
            "Authorization": "Bearer %s" % self.api_key,
            "Accept": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        if body_bytes is not None:
            headers["Content-Type"] = "application/json"

        last_error = None
        for attempt in range(self.retries + 1):
            if attempt:
                wait = RETRY_BACKOFF_SECONDS * attempt
                print("⏳ %s 第 %d/%d 次重试前等待 %.1fs"
                      % (operation, attempt, self.retries, wait))
                time_module.sleep(wait)
            try:
                status, text, resp_headers = http_request(
                    url, method, body_bytes, headers, self.timeout)
            except OSError as e:                       # 含 URLError / TimeoutError
                last_error = "网络错误: %s" % e
                print("⚠️ %s失败：%s" % (operation, last_error))
                continue
            if status == 429 or status >= 500:
                if "57014" in (text or ""):
                    # PostgreSQL statement_timeout：重试没有意义，直接给可行动提示
                    raise MigrationError(
                        "%s失败：语句超时（HTTP %s / PostgreSQL 57014），"
                        "通常是查询无法走索引（大表按未索引列排序或聚合）；"
                        "重试无效。URL %s，响应: %s"
                        % (operation, status, self._redact(url), snippet(text)))
                last_error = "HTTP %s：%s" % (status, snippet(text))
                print("⚠️ %s失败：%s" % (operation, last_error))
                continue
            if not (200 <= status < 300):
                raise MigrationError("%s失败，HTTP %s，URL %s，响应: %s"
                                     % (operation, status, self._redact(url),
                                        snippet(text)))
            return text, resp_headers

        raise MigrationError("%s重试 %d 次后仍失败（%s）"
                             % (operation, self.retries, last_error))

    def discover_columns(self):
        """探测源表实际列集合（select=* limit=1）；空表返回空集合"""
        text, _headers = self._send("GET", "select=*&limit=1",
                                    operation="探测源表列")
        data = json.loads(text or "[]")
        if not isinstance(data, list):
            raise MigrationError("探测源表列失败：响应不是 JSON 数组")
        if not data:
            return set()
        row = data[0]
        if not isinstance(row, dict):
            raise MigrationError("探测源表列失败：首行不是 JSON 对象")
        return set(row)

    def list_securities(self):
        """列出源表所有证券，按「该证券最早数据的 ts」升序（ts 相同按 usc 升序）

        实现：用主键 (usc, ts) 做**索引扫描**，按 usc、ts 升序分页
        （order=usc.asc,ts.asc + usc=gt.上一页末行 usc），每个 usc 首次出现
        的那一行就是它在源表的最早 ts。不依赖聚合函数（Supabase 默认禁用，
        会报 PGRST123），也不按 ts 做全局排序（大表没有 ts 索引时
        会因全表排序而 statement timeout）。

        :return: [(usc, first_ts), ...]；first_ts 为该证券在源表的最早 ts
        """
        securities = []
        last_usc = None            # 已记录清单里的最后一个证券
        cursor = None              # 分页游标：上一页最后一个 usc
        unusable = 0               # usc/ts 不可用的行（无法定位，仅告警）

        while True:
            query = "select=usc,ts&order=%s&limit=%d" % (SECURITY_ORDER,
                                                         SECURITY_SCAN_PAGE)
            if cursor is not None:
                query += "&usc=gt.%s" % encode_fragment(filter_value(cursor))
            text, _headers = self._send("GET", query, operation="列出证券")
            data = json.loads(text or "[]")
            if not isinstance(data, list):
                raise MigrationError("列出证券失败：响应不是 JSON 数组")
            if not data:
                break

            page_cursor = None
            for row in data:
                parsed = security_row(row)
                if parsed is None:
                    unusable += 1
                    continue
                usc, first_ts = parsed
                page_cursor = usc
                if usc == last_usc:
                    continue       # 同一证券的后续行：它的最早 ts 已记录
                securities.append((usc, first_ts))
                last_usc = usc

            if page_cursor is None:
                raise MigrationError("列出证券时遇到缺少可用 usc/ts 的行，"
                                     "请先人工清理这些行")
            if page_cursor == cursor:
                raise MigrationError("列出证券时分页未能推进（游标 %r）" % cursor)
            cursor = page_cursor
            if len(securities) > MAX_SECURITIES:
                raise MigrationError("证券数量超过安全上限 %d，请分批迁移"
                                     % MAX_SECURITIES)

        if unusable:
            print("⚠️ 源表有 %d 行缺少可用 usc/ts（无法定位证券），已跳过；"
                  "这些行不会被迁移，需人工清理" % unusable)
        securities.sort(key=lambda item: (item[1], item[0]))
        return securities

    def fetch_page(self, columns, limit, usc, after_ts=None):
        """读单证券的一页数据（按 ts 升序，键集分页）

        :param columns: 需要读取的列名列表
        :param limit: 本页行数上限
        :param usc: 证券标识（只读该证券）
        :param after_ts: 上一批末行的 ts；None 表示从该证券最早一行开始
        :return: 行 dict 列表（最多 limit 条）
        """
        select = urllib.parse.quote(",".join(columns), safe=",")
        query = "select=%s&order=%s&limit=%d&usc=eq.%s" % (
            select, USC_ORDER, limit, encode_fragment(filter_value(usc)))
        if after_ts is not None:
            query += "&ts=gt.%d" % after_ts
        text, _headers = self._send("GET", query, operation="读取存量数据")
        data = json.loads(text or "[]")
        if not isinstance(data, list):
            raise MigrationError("读取存量数据失败：响应不是 JSON 数组")
        return data

    def delete_keys(self, keys):
        """按键 (usc, ts) 精确删除，返回实际删除行数

        :param keys: [(usc, ts), ...]（单次条件数由调用方控制）
        :return: 删除行数（已不存在的记录不计入）
        """
        conditions = ["and(usc.eq.%s,ts.eq.%d)" % (filter_value(usc), ts)
                      for usc, ts in keys]
        query = "or=(%s)" % encode_fragment(",".join(conditions))
        text, _headers = self._send("DELETE", query,
                                    prefer="return=representation",
                                    operation="删除已同步数据")
        if not (text or "").strip():
            return 0
        data = json.loads(text)
        if not isinstance(data, list):
            raise MigrationError("删除已同步数据失败：响应不是 JSON 数组")
        return len(data)


# ---------------------------------------------------------------------------
# 目标服务（自建行情服务 Upsert）客户端
# ---------------------------------------------------------------------------

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


class QuoteUpsertClient:
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

        :param items: 已通过 build_item 校验的记录列表（同一批次只含一个证券）
        :return: dict {"affected": int|None, "mode": str|None, "requests": int}
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
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json"}
        last_error = None

        for attempt in range(self.retries + 1):
            if attempt:
                wait = RETRY_BACKOFF_SECONDS * attempt
                print("⏳ 第 %d/%d 次重试前等待 %.1fs" % (attempt, self.retries, wait))
                time_module.sleep(wait)
            try:
                status, text, _headers = http_request(
                    url, "POST", body, headers, self.timeout)
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


# ---------------------------------------------------------------------------
# 迁移流程
# ---------------------------------------------------------------------------

def plan_batch(rows, stats, samples, batch_no):
    """把一页源表记录拆成「可迁移 items + 对应删除键 + 跳过行」

    本页只含一个证券；keys 取源表原始取值（usc 原样、ts 转 int），
    保证删除时精确命中刚读到的行。

    :param rows: 本页记录列表
    :param stats: 累计统计 dict（就地累加 skipped 计数）
    :param samples: 异常样例列表（就地追加，最多 SAMPLE_LIMIT 条）
    :param batch_no: 批次序号（用于日志）
    :return: (items, keys, cursor_ts, skipped_count)
             keys 与 items 一一对应，元素为 (usc, ts)（源表原始取值，用于精确删除）；
             cursor_ts 为本页可用于继续分页的最大 ts（无法推进时为 None）
    """
    items = []
    keys = []
    cursor_ts = None
    skipped = 0

    for index, row in enumerate(rows, 1):
        key = cursor_key(row)
        if key is not None:
            cursor_ts = key[0]
        item, problems = build_item(row)
        if item is None or key is None:
            skipped += 1
            if key is None and item is not None:
                problems.append("usc/ts 无法用于主键定位，整行保留不迁移")
            if len(samples) < SAMPLE_LIMIT:
                samples.append("第 %d 批第 %d 行：%s"
                               % (batch_no, index, "；".join(problems)))
            continue
        if problems and len(samples) < SAMPLE_LIMIT:
            samples.append("第 %d 批第 %d 行（仍迁移）：%s"
                           % (batch_no, index, "；".join(problems)))
        items.append(item)
        keys.append((key[1], key[0]))          # 删除键用 (usc, ts) 顺序

    stats["skipped_rows"] += skipped
    return items, keys, cursor_ts, skipped


def delete_synced_rows(reader, keys, allow_mismatch):
    """按 (usc, ts) 分批删除已同步的行，返回删除总数

    :raises MigrationError: 删除行数与已同步行数不符且未允许不一致
    """
    deleted = 0
    for part in chunks(keys, DELETE_KEYS_PER_REQUEST):
        deleted += reader.delete_keys(part)
    if deleted != len(keys):
        message = ("源表实际删除 %d 行，与已同步行数 %d 不一致"
                   % (deleted, len(keys)))
        if allow_mismatch:
            print("⚠️ %s（MIGRATE_ALLOW_DELETE_MISMATCH 已开启，按告警处理）"
                  % message)
        else:
            raise MigrationError(message + "（可设 MIGRATE_ALLOW_DELETE_MISMATCH=true "
                                           "降级为告警）")
    return deleted


def run_security(target, reader, columns, usc, batch_size, max_batches, stats,
                 samples, failures, enable_delete, continue_on_error,
                 allow_delete_mismatch):
    """处理单个证券：按 ts 升序分批写入 + 删除，直到读完或达到批次上限

    :return: (skipped_count, failed_batch_count, stopped, truncated)；
             stopped 为 True 表示因失败按「失败即停」中止了整个流程；
             truncated 为 True 表示未读完该证券就碰到了批次上限
    """
    after_ts = None
    skipped_total = 0
    failed_total = 0
    truncated = False

    while True:
        if stats["batches"] >= max_batches:
            truncated = True
            break
        rows = reader.fetch_page(columns, batch_size, usc, after_ts=after_ts)
        if not rows:
            break

        stats["batches"] += 1
        batch_no = stats["batches"]
        stats["rows_read"] += len(rows)

        items, keys, cursor_ts, skipped = plan_batch(rows, stats, samples, batch_no)
        skipped_total += skipped
        if cursor_ts is None:
            raise MigrationError(
                "第 %d 批（证券 %s）全部行都缺少可用的 ts，无法继续分页"
                % (batch_no, usc))
        after_ts = cursor_ts

        error = None
        affected = None
        deleted = 0
        try:
            if items:
                outcome = target.upsert(items)
                affected = outcome["affected"]
                if affected is None and enable_delete:
                    print("⚠️ 第 %d 批未回传 affected（HTTP 2xx 视为已提交），"
                          "继续删除已同步行" % batch_no)
            if enable_delete and keys:
                deleted = delete_synced_rows(reader, keys, allow_delete_mismatch)
        except Exception as e:                     # 单批失败只记录，不中断循环
            error = "%s" % e

        if error is None:
            stats["upserted"] += len(items)
            stats["deleted"] += deleted
            print("✅ 第 %d 批（证券 %s，ts %d~%d）：读取 %d 行｜写入 %d 行"
                  "（affected=%s）｜删除 %d 行%s"
                  % (batch_no, usc, keys[0][1] if keys else 0,
                     keys[-1][1] if keys else 0, len(rows), len(items),
                     "未知" if affected is None else affected, deleted,
                     "｜跳过 %d 行" % skipped if skipped else ""))
            continue

        stats["failed_batches"] += 1
        failed_total += 1
        failures.append("第 %d 批（证券 %s）：%s" % (batch_no, usc, error))
        print("❌ 第 %d 批（证券 %s）：读取 %d 行｜写入 %d 行｜失败：%s"
              "（本批数据未删除，保留在源表）"
              % (batch_no, usc, len(rows), len(items), error))
        if not continue_on_error:
            print("⛔ 默认遇到失败立即停止（MIGRATE_CONTINUE_ON_ERROR=false）；"
                  "修复后重跑即可，Upsert 幂等")
            return skipped_total, failed_total, True, truncated

    return skipped_total, failed_total, False, truncated


def migrate(target, reader, columns, batch_size, max_batches,
            enable_delete, continue_on_error, allow_delete_mismatch):
    """主循环：证券清单（按最早 ts 升序）→ 逐证券按 ts 升序分批 → 写后删

    :param target: QuoteUpsertClient（或同形接口的替身）
    :param reader: SupabaseTable（或同形接口的替身）
    :param columns: 读取列名列表
    :param batch_size: 单批行数（单批只含一个证券）
    :param max_batches: 批次上限
    :param enable_delete: 是否在写入成功后删除源表行
    :param continue_on_error: 单批失败是否继续往后处理
    :param allow_delete_mismatch: 删除行数不符是否只告警
    :return: dict {"stats": {...}, "failures": [...], "samples": [...],
                   "hit_limit": bool, "pending_uscs": [...]}
    """
    stats = {
        "batches": 0,
        "usc_count": 0,
        "rows_read": 0,
        "upserted": 0,
        "deleted": 0,
        "skipped_rows": 0,
        "failed_batches": 0,
    }
    failures = []
    samples = []
    pending_uscs = []          # 仍有未迁移行的证券（校验失败 / 写入失败）
    hit_limit = False

    print("🔎 正在按主键 (usc, ts) 扫描证券清单（大表需要几分钟，请稍候）...")
    securities = reader.list_securities()
    print("📋 证券清单 %d 个（按各证券最早数据的 ts 升序，先迁移时间最早的证券）"
          % len(securities))
    if not securities:
        result = {"stats": stats, "failures": failures, "samples": samples,
                  "hit_limit": False, "pending_uscs": pending_uscs}
        print_summary(result)
        return result

    for position, (usc, first_ts) in enumerate(securities, 1):
        if stats["batches"] >= max_batches:
            hit_limit = True
            print("⏸ 已达批次上限 %d，本次停止；如仍有剩余数据，重跑可继续"
                  % max_batches)
            break

        stats["usc_count"] += 1
        print("---- 证券 %s（第 %d/%d 个，最早 ts=%s）----"
              % (usc, position, len(securities),
                 "未知" if first_ts is None else first_ts))
        if first_ts is None:
            print("⚠️ 证券 %s 的行缺少可用 ts，无法分批迁移，保留在源表" % usc)
            pending_uscs.append(usc)
            continue

        skipped, failed, stopped, truncated = run_security(
            target, reader, columns, usc, batch_size, max_batches, stats,
            samples, failures, enable_delete, continue_on_error,
            allow_delete_mismatch)

        if skipped or failed:
            pending_uscs.append(usc)
            print("⚠️ 证券 %s 仍有未迁移的行（跳过 %d 行，失败批次 %d 个），"
                  "保留在源表；修复后重跑会再次尝试" % (usc, skipped, failed))
        if truncated:
            hit_limit = True
            print("⏸ 已达批次上限 %d，本次停止；如仍有剩余数据，重跑可继续"
                  % max_batches)
            break
        if stopped:
            break

    result = {
        "stats": stats,
        "failures": failures,
        "samples": samples,
        "hit_limit": hit_limit,
        "pending_uscs": pending_uscs,
    }
    print_summary(result)
    return result


def print_summary(result):
    """打印迁移汇总（证券 / 批次 / 读取 / 写入 / 删除 / 跳过 / 失败）"""
    stats = result["stats"]
    print("======== Supabase 存量迁移结束（Task17）========")
    print("合计：证券 %d 个，批次 %d，读取 %d 行，写入 %d 行，删除 %d 行，"
          "跳过 %d 行，失败批次 %d"
          % (stats["usc_count"], stats["batches"], stats["rows_read"],
             stats["upserted"], stats["deleted"], stats["skipped_rows"],
             stats["failed_batches"]))
    if result["samples"]:
        print("跳过行样例（最多 %d 条，这些行保留在源表）：" % SAMPLE_LIMIT)
        for sample in result["samples"]:
            print("    - %s" % sample)
        if stats["skipped_rows"] > len(result["samples"]):
            print("    其余 %d 行同上，不再逐条打印"
                  % (stats["skipped_rows"] - len(result["samples"])))
    if result["pending_uscs"]:
        shown = result["pending_uscs"][:SAMPLE_LIMIT]
        print("仍有未迁移行的证券（保留在源表，需人工核对）：%s%s"
              % ("、".join(shown),
                 " 等 %d 个" % len(result["pending_uscs"])
                 if len(result["pending_uscs"]) > SAMPLE_LIMIT else ""))
    if result["failures"]:
        print("失败批次清单（修复后重跑即可，Upsert 幂等）：")
        for item in result["failures"][:SAMPLE_LIMIT]:
            print("    - %s" % item)
        if len(result["failures"]) > SAMPLE_LIMIT:
            print("    其余 %d 条同上，不再逐条打印"
                  % (len(result["failures"]) - SAMPLE_LIMIT))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    """主流程：配置 → 探测源表列 → 逐证券分批迁移 → 汇总与退出码"""
    _ensure_console_utf8()

    batch_size = min(env_int("MIGRATE_BATCH_SIZE", DEFAULT_BATCH_SIZE),
                     MAX_BATCH_ROWS)
    max_batches = env_int("MIGRATE_MAX_BATCHES", DEFAULT_MAX_BATCHES)
    enable_delete = env_bool("MIGRATE_ENABLE_DELETE", True)
    continue_on_error = env_bool("MIGRATE_CONTINUE_ON_ERROR", False)
    allow_skipped = env_bool("MIGRATE_ALLOW_SKIPPED_ROWS", False)
    allow_mismatch = env_bool("MIGRATE_ALLOW_DELETE_MISMATCH", False)
    timeout = env_int("MIGRATE_TIMEOUT", DEFAULT_TIMEOUT)
    supabase_timeout = env_int("MIGRATE_SUPABASE_TIMEOUT", DEFAULT_SUPABASE_TIMEOUT)
    retries = env_int_zero("MIGRATE_RETRIES", DEFAULT_RETRIES)
    on_conflict = resolve_on_conflict()

    print("======== Supabase 存量迁移开始（Task17）========")
    print("批大小 (batchSize) = %d（单批只含一个证券），批次上限 (maxBatches) = %d，"
          "冲突策略 (onConflict) = %s" % (batch_size, max_batches, on_conflict))
    print("删除源数据 (enableDelete) = %s，失败即停 (stopOnError) = %s，"
          "允许跳过行 (allowSkippedRows) = %s"
          % ("是（写入成功即删除）" if enable_delete else "否（只迁移不删除）",
             "否" if continue_on_error else "是",
             "是" if allow_skipped else "否（默认严格）"))
    print("目标超时 (timeout) = %ds，Supabase 超时 = %ds，重试 (retries) = %d"
          % (timeout, supabase_timeout, retries))

    # 凭据：只从环境变量读取，缺失即致命退出（不打印取值）
    project_ref = os.environ.get("SUPABASE_PROJECT_REF", "").strip()
    api_key = os.environ.get("SUPABASE_KEY", "").strip()
    if not project_ref or not api_key:
        print("❌ 缺少 SUPABASE_PROJECT_REF / SUPABASE_KEY 环境变量，无法读取源表")
        return 1
    base_url = os.environ.get("URL_QUOTE_SYNC_BASE", "").strip()
    if not base_url:
        print("❌ 缺少 URL_QUOTE_SYNC_BASE 环境变量（目标服务基础地址，"
              "如 http://1.2.3.4:18001），无法迁移")
        return 1

    try:
        reader = SupabaseTable(project_ref, api_key, timeout=supabase_timeout,
                               retries=retries)
        target = QuoteUpsertClient(base_url, on_conflict=on_conflict,
                                   timeout=timeout, retries=retries)
    except ValueError as e:
        print("❌ %s" % e)
        return 1
    print("源表 = %s（项目引用已遮蔽），目标服务 = %s" % (TABLE, target.base_url))

    try:
        discovered = reader.discover_columns()
        if not discovered:
            print("✅ 源表没有数据，无需迁移")
            return 0
        columns = resolve_columns(discovered)
        print("读取列 = %s" % ",".join(columns))
        if not any(name in columns for name in LAST_CLOSE_SOURCE_COLUMNS):
            print("⚠️ 源表没有 prev_close / last_close 列，前收价不下发")
        result = migrate(target, reader, columns, batch_size, max_batches,
                         enable_delete, continue_on_error, allow_mismatch)
    except MigrationError as e:
        print("❌ %s" % e)
        return 1
    except SyncRequestError as e:
        print("❌ 目标服务不可用：%s" % e)
        return 1

    stats = result["stats"]
    if result["failures"]:
        print("❌ 存在失败批次 %d 个，退出码 1" % len(result["failures"]))
        return 1
    if stats["skipped_rows"] and not allow_skipped:
        print("❌ 仍有 %d 行未通过校验、保留在源表：默认严格模式按失败返回；"
              "若确认可接受，设 MIGRATE_ALLOW_SKIPPED_ROWS=true 降级为告警"
              % stats["skipped_rows"])
        return 1
    if result["hit_limit"]:
        print("⏸ 本次因批次上限停止，迁移未完成；再次触发本工作流可继续")
    return 0


if __name__ == "__main__":
    sys.exit(main())
