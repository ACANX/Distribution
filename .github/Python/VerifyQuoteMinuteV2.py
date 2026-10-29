#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VerifyQuoteMinuteV2.py — 从 Supabase 视图取「待启用」证券，按时间片轮流验证并回写配置。

与 V1（VerifyQuoteMinute.py + VerifyQuoteMinuteSyncConfig.py 两段式）的差异
--------------------------------------------------------------------------------
V1：候选集 = Verify/Success/ 目录下的 .mvsv 清单；参数 = UscFutuMapping.jsonl.idx 映射表；
    轮换 = 同步完成后删文件、让清单头部前移（隐式 FIFO 队列）。

V2：候选集 = Supabase 视图 finv_quote_collect_futu_view 中 flag_enable <> '1' 的行；
    参数 = 视图同名列（stock_id / market_type / market_code / instrument_type /
           sub_instrument_type），不再需要 idx/jsonl；
    轮换 = **时间片确定性映射**（见下），完全不依赖仓库文件、不产生状态；
    回写 = 验证成功后直接 UPDATE finv_quote_collect_futu SET flag_enable='1'。

时间片映射（核心设计）
--------------------------------------------------------------------------------
    slot       = floor(now_epoch / INPUT_SLOT_SECONDS)     默认 300 秒 = 5 分钟
    offset     = (slot × INPUT_BATCH_SIZE) mod N            N = 候选池行数，默认每片 3 条
    targets    = 候选池（按 usc 升序）从 offset 起循环取 3 条

为什么不用「先分批、再取 slot % 批数」：N 不整除 batchSize 时，下一次会从池头重开，
相邻两片必然重叠（N=10、batch=3 时第 4 片取 [9,0,1]、第 5 片又取 [0,1,2]）。按偏移步进
则相邻两片恒不重叠（步长 3 < N），且长期遍历整池。

为什么这样比 V1 的队列稳：
    * 无状态：不需要进度文件、不需要删产物、不依赖 git 树内容；
    * 不会卡死：V1 的未命中件会永久钉在队头（实测头部 47 条里 44 条已是重复留痕），
      V2 的失败件留在池里，但下一片自然轮到别的批次；
    * 可复现：给定 slot 就能复算出同一批（可用 INPUT_SLOT 重放某一片）；
    * 天然收敛：验证成功的行 flag_enable 置 1 后离开候选池，池子只减不增。

已知取舍：GitHub Actions 的 cron 会延迟数分钟，个别时间片会被跳过（不是漏验证，
只是该片对应的批次顺延到下一次 slot 落在别处）。需要严格覆盖时改用外部调度器，
或按需 dispatch 并显式传 INPUT_SLOT。

产物布局（与 V1 完全隔离，V1 的消费逻辑不会碰）
--------------------------------------------------------------------------------
    VerifyV2/Success/{quote_market}/{usc}.mvsv   采集成功（头部含 K 线数据行）
    VerifyV2/Fail/{quote_market}/{usc}.mvsv      采集失败（只有头部与错误原因）
    VerifyV2/MisMatch/{usc}.txt                  视图无行 / 参数不全，无法发起请求

不做的事（与 V1 的刻意差异）
--------------------------------------------------------------------------------
    * 不 upsert、不插新行：候选行本来就在 finv_quote_collect_futu 里，只做 UPDATE；
      因此不存在 V1 的「表内缺行 → FUTU_ROW_MISSING」场景，也无需 MisMatch 记录它。
    * 不读 UscFutuMapping.jsonl(.idx)、不读 Verify/Success/。
    * 不删产物：V2 不靠删文件推进，产物纯留档。

入参（环境变量，由 .github/workflows/VerifyQuoteMinuteV2.yml 注入）
--------------------------------------------------------------------------------
    INPUT_USC                  手工指定 usc（逗号/空白分隔）；置空才走时间片候选池
    INPUT_BRANCH               产物提交目标分支，默认 quote-meta
    INPUT_OUT_DIR              本地镜像目录，默认 verify-v2-out
    INPUT_SLOT_SECONDS         时间片长度（秒），默认 300
    INPUT_BATCH_SIZE           每个时间片验证几个证券，默认 3
    INPUT_SLOT                 手工指定时间片序号（默认按当前时间算），用于重放
    INPUT_INTERVAL             相邻两次请求的最小间隔秒数，默认 10
    INPUT_MARKETS              只处理这些行情市场段（逗号分隔，留空不限）
    INPUT_PRICE_FIELD          收盘价口径：auto / cc_price / price
    INPUT_DRY_RUN              置真则只本地生成、不写库、不提交
    INPUT_ALLOW_VERIFY_FAILURE 置真则验证失败也返回退出码 0
    INPUT_FIXTURE              用本地接口响应样本代替联网（自测用）
    SUPABASE_PROJECT_REF       Supabase 项目引用（必填）
    SUPABASE_KEY               Supabase 密钥（service-role，需 SELECT 视图 + UPDATE 表）
    GITHUB_COMMIT_TOKEN        产物提交用（dry-run 或本地自测可缺）

退出码：0 全部成功或无候选；1 有证券验证失败 / 配置回写失败；2 任务级错误。
"""

import datetime
import decimal
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_NAME = os.path.basename(os.path.abspath(__file__))
#: 与 V1 的 TASK_VERSION 各自独立编号：V2 首次落地即 1，口径变更时递增
TASK_VERSION = "1"

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 候选池视图（用户提供的建视图 SQL，含 finv_quote_collect_futu ⋈ finv_quote_secu，
#: 只保留 provider='FTMM' 的证券；flag_enable 取自 finv_quote_collect_futu，即采集开关）
VIEW_TABLE = "finv_quote_collect_futu_view"
#: 回写目标：视图不可写，只更新底表这一列（候选行必然存在，故不插入）
COLLECT_TABLE = "finv_quote_collect_futu"
KEY_COLUMN = "usc"
ENABLE_COLUMN = "flag_enable"
ENABLE_VALUE = "1"
DT_UPDATE_COLUMN = "dt_update"

#: 视图查询列：参数取自同名列，其余仅用于产物头部与日志
VIEW_COLUMNS = ("usc", "name_sc", "region", "market", "timezone", "provider",
                "symbol", "type_secu", "type_symbol", "quote_market", "futu_symbol",
                "stock_id", "market_type", "market_code", "instrument_type",
                "sub_instrument_type", "req_section", "flag_enable")

#: 拼接 get-quote-minute 请求参数：接口字段名 → 视图列名
PARAM_MAP = (("stockId", "stock_id"), ("marketType", "market_type"),
             ("marketCode", "market_code"), ("instrumentType", "instrument_type"),
             ("subInstrumentType", "sub_instrument_type"))

# 仓库内路径（相对仓库根）；与 V1 的 Verify/ 完全隔离
SUCCESS_DIR = "VerifyV2/Success"
FAIL_DIR = "VerifyV2/Fail"
MISMATCH_DIR = "VerifyV2/MisMatch"
MVSV_SUFFIX = ".mvsv"

# 默认参数
DEFAULT_BRANCH = "quote-meta"
DEFAULT_OUT_DIR = "verify-v2-out"
DEFAULT_SLOT_SECONDS = 300
DEFAULT_BATCH_SIZE = 3
DEFAULT_INTERVAL = 10.0
DEFAULT_PRICE_FIELD = "auto"
DEFAULT_QUOTE_TYPE = "2"

# Supabase / PostgREST
ENV_SUPABASE_REF = "SUPABASE_PROJECT_REF"
ENV_SUPABASE_KEY = "SUPABASE_KEY"
SUPABASE_REST_BASE = "https://%s.supabase.co/rest/v1"
PAGE_SIZE = 1000            # 单次 GET 的行数（Supabase 默认 db-max-rows 上限）
HTTP_TIMEOUT = 30
MAX_ATTEMPTS = 3
RETRY_BACKOFF = 2.0
RETRYABLE_STATUS = (429, 500, 502, 503, 504)

# moomoo 行情接口
BASE_URL = "https://www.moomoo.com/quote-api/quote-v2"
QUOTE_PATH = "/get-quote-minute"
SIGN_KEY = "quote_web"
TIMESTAMP_KEY = "_"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
THROTTLE_RETRIES = 3
THROTTLE_BACKOFF = 30.0
MAX_BACKOFF = 120.0
NETWORK_RETRIES = 3

# 采集时间与 Date/Time 列一律用 UTC+8
CHINA_TZ = datetime.timezone(datetime.timedelta(hours=8))
UNKNOWN_MARKET = "UNKNOWN"
OUTCOME_SUCCESS, OUTCOME_FAILED = "SUCCESS", "FAILED"
OUTCOME_THROTTLED, OUTCOME_SKIPPED = "THROTTLED", "SKIPPED"

# .mvsv 列定义，与既有 GLD_Min_*.mvsv / V1 产物逐列一致
MVSV_FIELDS = "Ts|Date|Time|Open|Close|Low|High|Volume|Turnover|ChangePrice|ChangePercent"
MVSV_NAMES = "时间戳(UTC)|日期|时间|开盘价|收盘价|最低价|最高价|成交量|成交额|涨跌值|涨跌幅(%)"
MVSV_TYPES = "int|int|int|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|str"


class TaskError(Exception):
    """任务级错误：凭据缺失、视图不可用、入参非法、提交失败等，退出码 2。"""


class QuoteApiError(Exception):
    """单条证券的失败，可继续处理下一条。"""

    def __init__(self, code, detail, throttled=False):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.throttled = throttled


class SupabaseError(Exception):
    """PostgREST 调用失败。"""


# ---------------------------------------------------------------------------
# 基础工具
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


def envFloat(name, default):
    """读浮点型环境变量。"""
    raw = env(name)
    if not raw:
        return float(default)
    try:
        return float(raw)
    except ValueError:
        raise TaskError("环境变量 %s 不是数字：%r" % (name, raw))


def chunks(raw):
    """把逗号 / 空白分隔的文本切成去重后的列表，保持原序。"""
    words = str(raw or "").replace("，", " ").replace(",", " ").split()
    return list(dict.fromkeys(words))


def cell(value):
    """取值转字符串（空值一律空串）：库里的 int / varchar 按同一口径比较。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    return str(value)


def oneLine(value):
    """压平换行：元信息取值内出现换行会把一行拆成两行。"""
    return "" if value is None else str(value).replace("\r", " ").replace("\n", " ").strip()


def metaValue(value):
    """元信息取值：含竖线时按 .mvsv 惯例加成对双引号。"""
    text = oneLine(value)
    return '"%s"' % text if "|" in text else text


def dataCell(value):
    """数据行取值：竖线是分隔符，一律换成斜杠。"""
    return oneLine(value).replace("|", "/")


def nowChina():
    """当前 UTC+8 时刻，格式化为 2026-10-29 20:15:00。"""
    return datetime.datetime.now(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def nowUtcIso():
    """当前 UTC 时刻（ISO8601，秒精度）——写入 dt_update。"""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def isEnabled(value):
    """flag_enable 是否已是「启用」：只有 1 视为已启用，其余都要验证。"""
    return cell(value).strip() == ENABLE_VALUE


def annotate(message, level="warning"):
    """GitHub Actions 注解；本地运行时退化为普通打印。"""
    if env("GITHUB_ACTIONS"):
        print("::%s::%s" % (level, message.replace("\n", " ")), flush=True)
    else:
        print("[%s] %s" % (level.upper(), message), flush=True)


def writeLocal(path, content):
    """把文本落到本地镜像目录（UTF-8 无 BOM、LF），返回路径。"""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    return path


def localPath(outDir, remotePath):
    """仓库路径 → 本地镜像路径。"""
    return os.path.join(outDir, remotePath.replace("/", os.sep))


# ---------------------------------------------------------------------------
# .mvsv 组装
# ---------------------------------------------------------------------------


def mvsvHeader(record, market, count, fetchTime, remark, extra):
    """拼 .mvsv 元信息行：前 19 行与既有行情 .mvsv 逐键一致，其后是溯源与结论块。"""
    usc = cell(record.get("usc"))
    lines = [
        "# 标题 : %s 分钟级行情数据" % usc,
        "# 数据供应商 : FT",
        '# 字段 : "%s"' % MVSV_FIELDS,
        '# 字段名称 : "%s"' % MVSV_NAMES,
        '# 字段类型 : "%s"' % MVSV_TYPES,
        "# 计数 : %d" % count,
        '# 采集时间 : "%s"' % fetchTime,
        "# 证券代码 : %s" % metaValue(usc),
        "# 市场 : %s" % metaValue(market),
        "# 备注 : %s" % metaValue(remark),
        "# Title : %s Minute Quote Data" % usc,
        "# DataProvider : FT",
        '# Field : "%s"' % MVSV_FIELDS,
        '# FieldName : "%s"' % MVSV_FIELDS,
        '# FieldType : "%s"' % MVSV_TYPES,
        "# Count : %d" % count,
        '# FetchTime : "%s"' % fetchTime,
        "# SecuCode : %s" % metaValue(usc),
        "# Market : %s" % metaValue(market),
        "# 以下为本程序追加的元信息（读取方可忽略）",
        "# 统一证券代码 : %s" % metaValue(usc),
    ]
    for key, column in (("富途代码", "futu_symbol"), ("URL类型段", "type_symbol"),
                        ("行情市场段", "quote_market"), ("证券名称", "name_sc"),
                        ("富途证券id", "stock_id"), ("视图标识", "symbol")):
        value = metaValue(record.get(column))
        if value:
            lines.append("# %s : %s" % (key, value))
    lines.append("# 时间口径 : Ts 为 Unix 秒（UTC）；Date/Time 为同一时刻的 UTC+8 日历值")
    lines.append("# 价格口径 : Close 取接口的复权价，Open 取上一根的 Close，首根取昨收价")
    for key, value in extra:
        text = metaValue(value)
        if text:
            lines.append("# %s : %s" % (key, text))
    return lines


def renderMvsv(headerLines, dataLines):
    """拼全文：UTF-8 无 BOM、只认 LF、头与数据之间恰好一个空行、末尾不写换行。"""
    text = "\n".join(list(headerLines) + [""] + list(dataLines))
    return text[:-1] if text.endswith("\n") else text


def pickClose(bar, priceField):
    """按口径取收盘价：auto 优先复权价 cc_price，缺失回退 price。"""
    if priceField == "cc_price":
        return bar.get("cc_price")
    if priceField == "price":
        return bar.get("price")
    return bar.get("cc_price") if bar.get("cc_price") is not None else bar.get("price")


def successMvsv(record, data, params, priceField, slot):
    """接口行情 → .mvsv 全文。

    Ts ← time；Date/Time ← Ts 的 UTC+8 日历值；Open ← 上一根 Close（首根取昨收）；
    Close ← cc_price；Low/High 空；Volume/Turnover/ChangePrice/ChangePercent ←
    volume/turnover/change_price/ratio。
    """
    bars = data.get("list") or []
    if not bars:
        raise QuoteApiError("EMPTY", "接口返回成功但 K 线为空")

    previous = data.get("last_close_price")
    rows, firstTs, lastTs = [], None, None
    for bar in bars:
        try:
            ts = int(bar.get("time"))
        except (TypeError, ValueError):
            continue
        moment = datetime.datetime.fromtimestamp(ts, CHINA_TZ)
        close = pickClose(bar, priceField)
        rows.append("|".join([
            str(ts), moment.strftime("%Y%m%d"), moment.strftime("%H%M%S"),
            cell(previous if previous is not None else close), cell(close), "", "",
            cell(bar.get("volume")), cell(bar.get("turnover")),
            cell(bar.get("change_price")), dataCell(cell(bar.get("ratio"))),
        ]))
        previous = close
        firstTs = ts if firstTs is None else firstTs
        lastTs = ts
    if not rows:
        raise QuoteApiError("EMPTY", "接口返回的 K 线均无有效时间戳")

    market = cell(record.get("quote_market")) or UNKNOWN_MARKET
    fetchTime = nowChina()
    remark = "汇总: K线=%d|首根=%s|末根=%s|昨收=%s" % (
        len(rows), firstTs, lastTs, cell(data.get("last_close_price")))
    header = mvsvHeader(record, market, len(rows), fetchTime, remark, [
        ("验证结论", OUTCOME_SUCCESS),
        ("请求参数", "|".join("%s=%s" % item for item in params.items())),
        ("请求地址", BASE_URL + QUOTE_PATH),
        ("数据来源", "%s @ Supabase（flag_enable<>%s 的待启用行）" % (VIEW_TABLE, ENABLE_VALUE)),
        ("时间片", str(slot)),
        ("数据根数", str(len(rows))),
        ("昨收价", cell(data.get("last_close_price"))),
        ("任务脚本", SCRIPT_NAME),
        ("任务版本", TASK_VERSION),
        ("采集时刻", fetchTime),
    ])
    return renderMvsv(header, rows), len(rows), market


def failureMvsv(record, error, params, slot):
    """失败留痕：无数据行，错误原因写在元信息块。"""
    usc = cell(record.get("usc"))
    market = cell(record.get("quote_market")) or UNKNOWN_MARKET
    fetchTime = nowChina()
    header = mvsvHeader(record, market, 0, fetchTime,
                        "汇总: 验证失败|K线=0|错误代码=%s" % error.code, [
        ("验证结论", OUTCOME_FAILED),
        ("错误代码", error.code),
        ("错误原因", error.detail),
        ("请求参数", "|".join("%s=%s" % item for item in (params or {}).items())),
        ("请求地址", BASE_URL + QUOTE_PATH),
        ("数据来源", "%s @ Supabase（flag_enable<>%s 的待启用行）" % (VIEW_TABLE, ENABLE_VALUE)),
        ("时间片", str(slot)),
        ("任务脚本", SCRIPT_NAME),
        ("任务版本", TASK_VERSION),
        ("采集时刻", fetchTime),
        ("处置提示", "本件为失败留痕；该证券的 flag_enable 未被置 1，后续时间片会自动重试"),
    ])
    return renderMvsv(header, [])


def mismatchText(record, reason, slot, nowText):
    """视图无行 / 参数不全时的留痕文本（V2 的 MisMatch，与 V1 格式相近但独立）。"""
    usc = cell((record or {}).get("usc"))
    lines = [
        "# VerifyQuoteMinuteV2 未命中记录",
        "",
        "usc        : %s" % usc,
        "原因       : %s" % reason,
        "检测时刻   : %s (+08:00)" % nowText,
        "时间片     : %s" % slot,
        "任务脚本   : %s（版本 %s）" % (SCRIPT_NAME, TASK_VERSION),
        "视图       : %s（筛选条件 flag_enable<>%s）" % (VIEW_TABLE, ENABLE_VALUE),
        "工作流运行 : %s" % (env("GITHUB_RUN_ID") or "(本地运行)"),
        "",
        "## 视图行快照",
    ]
    if not record:
        lines.append("(视图里没有该 usc 的行)")
    else:
        for column in VIEW_COLUMNS:
            lines.append("%-20s = %s" % (column, cell(record.get(column))))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# moomoo 行情客户端
# ---------------------------------------------------------------------------


def queryParams(record):
    """由视图行拼出 get-quote-minute 参数，键序即签名键序。

    缺任一必需列都无法发起请求（视图本应保证有值，真缺了说明视图/表口径已变）。
    """
    missing = [name for name, column in PARAM_MAP if not cell(record.get(column))]
    if missing:
        raise QuoteApiError("VIEW_FIELD_MISSING", "视图行缺少参数：%s" % ", ".join(missing))
    params = {"stockId": cell(record.get("stock_id")),
              "marketType": cell(record.get("market_type")),
              "type": DEFAULT_QUOTE_TYPE,
              "marketCode": cell(record.get("market_code")),
              "instrumentType": cell(record.get("instrument_type")),
              "subInstrumentType": cell(record.get("sub_instrument_type"))}
    return params


def quoteToken(params):
    """quote-token = SHA-256( HMAC-SHA512(key="quote_web", 紧凑JSON)[:10] )[:10]。"""
    compact = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
    digest = hmac.new(SIGN_KEY.encode(), compact.encode(), hashlib.sha512).hexdigest()
    return hashlib.sha256(digest[:10].encode()).hexdigest()[:10]


class QuoteClient:
    """get-quote-minute 客户端：限速等待、限速退避重试、网络重试。"""

    def __init__(self, interval):
        self.interval = interval
        self.requests = 0
        self.throttles = 0
        self.lastAt = 0.0

    def _send(self, url, params):
        """发一次请求；网络层瞬时错误自动重试。"""
        headers = {"accept": "application/json, text/plain, */*",
                   "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                   "cache-control": "no-cache", "referer": "https://www.moomoo.com/",
                   "user-agent": USER_AGENT, "quote-token": quoteToken(params)}
        request = urllib.request.Request(url, headers=headers)
        last = None
        for attempt in range(NETWORK_RETRIES + 1):
            self.requests += 1
            self.lastAt = time.time()
            try:
                with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                    return response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                return exc.read().decode("utf-8", "replace")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
                if attempt < NETWORK_RETRIES:
                    time.sleep(5.0 * (attempt + 1))
        raise QuoteApiError("NETWORK_ERROR", "网络请求失败：%s" % last)

    def query(self, params):
        """取五日分钟行情，返回响应 data 字典。"""
        raw = ""
        for retry in range(THROTTLE_RETRIES + 1):
            if self.lastAt:
                wait = self.interval - (time.time() - self.lastAt)
                if wait > 0:
                    time.sleep(wait)
            attempt = dict(params, **{TIMESTAMP_KEY: str(int(time.time() * 1000))})
            url = BASE_URL + QUOTE_PATH + "?" + urllib.parse.urlencode(attempt)
            raw = self._send(url, attempt)
            if raw.lstrip().startswith("{"):
                break
            # 接口正常响应恒为 JSON；非 JSON 一律按限速拦截处理（实测为 HTML 拦截页）
            self.throttles += 1
            if retry >= THROTTLE_RETRIES:
                raise QuoteApiError("THROTTLED", "响应非 JSON，按限速拦截处理：%s"
                                    % raw[:120].replace("\n", " "), throttled=True)
            backoff = min(THROTTLE_BACKOFF * (2 ** retry), MAX_BACKOFF)
            print("    [命中限速，退避 %.0fs 后重试（第 %d/%d 次）]"
                  % (backoff, retry + 1, THROTTLE_RETRIES), flush=True)
            time.sleep(backoff)

        try:
            payload = json.loads(raw, parse_float=decimal.Decimal)
        except ValueError as exc:
            raise QuoteApiError("BAD_RESPONSE", "响应解析失败：%s" % raw[:120]) from exc
        if payload.get("code") != 0:
            raise QuoteApiError("API_ERROR", "接口业务失败：code=%s, message=%s"
                                % (payload.get("code"), payload.get("message")))
        return payload.get("data") or {}


# ---------------------------------------------------------------------------
# Supabase：读视图候选池 / 回写 flag_enable
# ---------------------------------------------------------------------------


class SupabaseClient:
    """PostgREST 最小客户端（仅标准库）：分页读视图 + 批量 UPDATE。"""

    def __init__(self, projectRef, apiKey, timeout=HTTP_TIMEOUT):
        if not projectRef:
            raise TaskError("Supabase 项目引用（%s）不能为空" % ENV_SUPABASE_REF)
        if not apiKey:
            raise TaskError("Supabase API 密钥（%s）不能为空" % ENV_SUPABASE_KEY)
        self.restUrl = (env("SUPABASE_REST_BASE") or (SUPABASE_REST_BASE % projectRef)).rstrip("/")
        self.apiKey = apiKey
        self.timeout = timeout
        self.requests = 0

    def _request(self, method, table, query, body=None, prefer=None):
        """发一次请求（429/5xx 与网络错误按指数退避重试），返回 (状态码, 响应文本)。"""
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
                    raise SupabaseError(last) from exc
            except OSError as exc:
                last = "网络错误：%s" % exc
                if attempt >= MAX_ATTEMPTS:
                    raise SupabaseError(last) from exc
            time.sleep(RETRY_BACKOFF * (2 ** (attempt - 1)))
        raise SupabaseError(last or "未知错误")

    def pendingRows(self):
        """分页读候选池：视图里 flag_enable <> '1' 的行，按 usc 升序。

        服务端先按 flag_enable=neq.1 过滤（减少传输），本地再用 isEnabled 兜底剔除
        「看起来是 1 的变体」（如 '1 '、'true' 之类库端脏值），避免重复验证已启用的证券。
        """
        columns = ",".join(VIEW_COLUMNS)
        rows, offset = [], 0
        while True:
            query = ("select=%s&%s=neq.%s&order=%s.asc&limit=%d&offset=%d"
                     % (columns, ENABLE_COLUMN, ENABLE_VALUE, KEY_COLUMN, PAGE_SIZE, offset))
            status, body = self._request("GET", VIEW_TABLE, query)
            if status != 200:
                raise SupabaseError("GET %s 返回 HTTP %s：%s" % (VIEW_TABLE, status, body[:300]))
            try:
                page = json.loads(body or "[]")
            except ValueError as exc:
                raise SupabaseError("GET %s 响应不是合法 JSON：%s" % (VIEW_TABLE, exc)) from exc
            if not isinstance(page, list):
                raise SupabaseError("GET %s 响应不是行数组" % VIEW_TABLE)
            rows.extend(row for row in page if isinstance(row, dict))
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return [row for row in rows if cell(row.get(KEY_COLUMN)) and not isEnabled(row.get(ENABLE_COLUMN))]

    def rowsByUscs(self, uscs):
        """按 usc 从视图取指定行（手工验证通道；不受 flag_enable 过滤）。"""
        if not uscs:
            return []
        columns = ",".join(VIEW_COLUMNS)
        keys = ",".join(urllib.parse.quote(usc, safe=".-_") for usc in uscs)
        query = "select=%s&%s=in.(%s)" % (columns, KEY_COLUMN, keys)
        status, body = self._request("GET", VIEW_TABLE, query)
        if status != 200:
            raise SupabaseError("GET %s 返回 HTTP %s：%s" % (VIEW_TABLE, status, body[:300]))
        try:
            rows = json.loads(body or "[]")
        except ValueError as exc:
            raise SupabaseError("GET %s 响应不是合法 JSON：%s" % (VIEW_TABLE, exc)) from exc
        return [row for row in rows if isinstance(row, dict)]

    def enableRows(self, uscs, stamp):
        """把指定 usc 的 flag_enable 置 '1'（一条 PATCH 命中多行）。

        与 V1 的 upsert 不同：这里的每行**写入值完全相同**（都是 flag_enable='1' + 同一时间戳），
        所以一份 body 命中多行正是想要的效果，不存在「每行取值不同」的问题。
        返回 (回读行数, 失败的 usc 列表 或 None)。
        """
        if not uscs:
            return 0, None
        keys = ",".join(urllib.parse.quote(usc, safe=".-_") for usc in uscs)
        query = "%s=in.(%s)" % (KEY_COLUMN, keys)
        body = json.dumps({ENABLE_COLUMN: ENABLE_VALUE, DT_UPDATE_COLUMN: stamp},
                          ensure_ascii=False).encode("utf-8")
        try:
            status, response = self._request("PATCH", COLLECT_TABLE, query, body,
                                             prefer="return=representation")
        except SupabaseError as exc:
            return 0, list(uscs), str(exc)
        if status not in (200, 204):
            return 0, list(uscs), "PATCH 返回 HTTP %s：%s" % (status, response[:300])
        try:
            back = json.loads(response) if response.strip() else []
        except ValueError:
            back = []
        return len(back), None


# ---------------------------------------------------------------------------
# 仓库侧：提交产物 / 留痕
# ---------------------------------------------------------------------------


class RepoAccess:
    """GitHub Contents API 的最小封装（复用同目录 GitHubCommitContent.py）。"""

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

    @property
    def usable(self):
        """是否有仓库身份与令牌。"""
        return bool(self.owner and self.repo and self.token)

    def commitFile(self, path, localFile, message):
        """提交单个本地文件到产物分支。"""
        return self.lib.commit_content_file(path, localFile, branch=self.branch,
                                            commit_msg=message, owner=self.owner,
                                            repo=self.repo, token=self.token or None)


# ---------------------------------------------------------------------------
# 时间片映射
# ---------------------------------------------------------------------------


def slotOf(timestamp, slotSeconds):
    """把 epoch 秒映射成时间片序号（单调递增，默认 300 秒一片）。"""
    return int(timestamp // slotSeconds)


def slotRange(slot, slotSeconds):
    """时间片的起止时刻（UTC+8），用于日志展示。"""
    start = datetime.datetime.fromtimestamp(slot * slotSeconds, CHINA_TZ)
    end = start + datetime.timedelta(seconds=slotSeconds)
    return start.strftime("%Y-%m-%d %H:%M"), end.strftime("%H:%M")


def pickBatch(pool, slot, batchSize):
    """按时间片确定性地挑一批：返回 (批次行, 池内偏移, 池大小)。

    偏移 = (slot × batchSize) mod N，从偏移处循环取 batchSize 行。

    为什么不用「先分批次、再 slot % 批数」：那样尾批不整除时，下一片会从池头重开，
    相邻两片必然重叠（N=10、batch=3 时第 4 片取 [9,0,1]、第 5 片又取 [0,1,2]）。
    按偏移步进则相邻两片恒不重叠（步长 batchSize < N），且长期遍历整池。
    池子会随「验证成功即置 1」而只减不增，N 变化时偏移自动重新落位。
    """
    size = len(pool)
    if size == 0:
        return [], 0, 0
    offset = (slot * batchSize) % size
    batch = [pool[(offset + index) % size] for index in range(min(batchSize, size))]
    return batch, offset, size


def poolDigest(pool):
    """候选池摘要（usc 升序拼接后取 sha256 前 16 位）——写进产物头部做溯源。"""
    joined = "\n".join(sorted(cell(row.get(KEY_COLUMN)) for row in pool))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16], len(pool)


# ---------------------------------------------------------------------------
# 单条处理
# ---------------------------------------------------------------------------


def process(row, client, cfg, slot):
    """处理一条证券：取行情 → 转 mvsv → 本地留档 → 提交。返回结果 dict。"""
    usc = cell(row.get(KEY_COLUMN))
    market = cell(row.get("quote_market")) or UNKNOWN_MARKET
    started = time.time()
    params, bars, detail, outcome = None, 0, "", OUTCOME_SUCCESS

    try:
        params = queryParams(row)
        data = client.query(params) if not cfg["fixture"] else cfg["fixture"]
        text, bars, market = successMvsv(row, data, params, cfg["priceField"], slot)
    except QuoteApiError as exc:
        if exc.throttled:
            # 限速是瞬时状态：不写产物、不留痕、不回写配置，重跑即可
            result = {"usc": usc, "outcome": OUTCOME_THROTTLED, "market": UNKNOWN_MARKET,
                      "bars": 0, "remotePath": "", "localPath": "",
                      "detail": "%s: %s（限速未采，未写产物）" % (exc.code, exc.detail),
                      "uploadFailed": False,
                      "elapsedMs": int((time.time() - started) * 1000)}
            logResult(result)
            return result
        outcome = OUTCOME_FAILED
        detail = "%s: %s" % (exc.code, exc.detail)
        text = failureMvsv(row, exc, params, slot)

    remote = "%s/%s/%s%s" % (SUCCESS_DIR if outcome == OUTCOME_SUCCESS else FAIL_DIR,
                             market, usc, MVSV_SUFFIX)
    local = writeLocal(localPath(cfg["outDir"], remote), text)
    result = {"usc": usc, "outcome": outcome, "market": market, "bars": bars,
              "remotePath": remote, "localPath": local, "detail": detail,
              "uploadFailed": False, "elapsedMs": int((time.time() - started) * 1000)}

    if cfg["dryRun"]:
        result["detail"] = (detail + "；" if detail else "") + "dry-run 未提交"
        logResult(result)
        return result
    if not cfg["repo"].usable:
        result.update(outcome=OUTCOME_FAILED, uploadFailed=True,
                      detail="缺少仓库令牌（GITHUB_COMMIT_TOKEN），无法提交产物")
        logResult(result)
        return result
    upload = cfg["repo"].commitFile(remote, local, "VerifyV2 %s" % usc)
    if not upload.get("success"):
        result.update(outcome=OUTCOME_FAILED, uploadFailed=True,
                      detail="提交失败: %s" % upload.get("message"))
    logResult(result)
    return result


def logResult(result):
    """单行结果日志。"""
    print("  [%s] %-12s market=%-8s bars=%-5d %sms %s"
          % (result["outcome"], result["usc"], result["market"], result["bars"],
             result.get("elapsedMs", 0), result["remotePath"] or result["detail"]), flush=True)


def archiveMismatch(cfg, usc, row, reason, slot):
    """写 VerifyV2/MisMatch/{usc}.txt 并提交（dry-run 只留本地副本）。"""
    remote = "%s/%s.txt" % (MISMATCH_DIR, usc)
    local = writeLocal(localPath(cfg["outDir"], remote),
                       mismatchText(row, reason, slot, nowChina()))
    if cfg["dryRun"]:
        print("      ⤷ 留痕 %s（dry-run 不提交）本地副本 %s" % (remote, local), flush=True)
        return False
    if not cfg["repo"].usable:
        print("      ✗ 留痕 %s 提交失败：缺少仓库令牌" % remote, flush=True)
        return True
    result = cfg["repo"].commitFile(remote, local, "MisMatchV2 %s" % usc)
    if result.get("success"):
        print("      ⤷ 留痕 %s 已提交（HTTP %s）" % (remote, result.get("http_status")), flush=True)
        return False
    print("      ✗ 留痕 %s 提交失败：%s" % (remote, result.get("message")), flush=True)
    return True


# ---------------------------------------------------------------------------
# 步骤摘要
# ---------------------------------------------------------------------------


def stepSummary(ctx, stats):
    """写 Actions 运行摘要（未在 Actions 中运行则跳过）。"""
    path = env("GITHUB_STEP_SUMMARY")
    if not path:
        return
    counts = {name: sum(1 for r in stats["results"] if r["outcome"] == name)
              for name in (OUTCOME_SUCCESS, OUTCOME_FAILED, OUTCOME_THROTTLED, OUTCOME_SKIPPED)}
    lines = ["## VerifyQuoteMinuteV2 运行结果", "",
             "| 项 | 值 | 备注 |", "|---|---|---|",
             "| 时间片 | %s | %s ~ %s（%d 秒/片） |"
             % (ctx["slot"], ctx["slotStart"], ctx["slotEnd"], ctx["slotSeconds"]),
             "| 池内偏移 | %s | %s |"
             % (ctx["poolOffset"] if ctx["poolSize"] else "-",
                "手工指定 INPUT_USC" if not ctx["slotSeconds"]
                else "slot=%d，每片 %d 分钟"
                     % (ctx["slot"], max(1, ctx["slotSeconds"] // 60))),
             "| 候选池 | %d 条 | %s（flag_enable<>%s） |"
             % (ctx["poolSize"], ctx["poolDigest"], ENABLE_VALUE),
             "| 本轮目标 | %d 条 | %s |"
             % (len(stats["results"]), ctx["targetSource"]),
             "| 成功 | %d | 落在 %s/ 并已置 flag_enable=1 |" % (counts[OUTCOME_SUCCESS], SUCCESS_DIR),
             "| 失败 | %d | 落在 %s/，下个时间片会再试 |" % (counts[OUTCOME_FAILED], FAIL_DIR),
             "| 限速未采 | %d | 未写产物 |" % counts[OUTCOME_THROTTLED],
             "| 跳过 | %d | 被 INPUT_MARKETS 过滤或视图无参数 |" % counts[OUTCOME_SKIPPED],
             "| 配置回写 | %d 行 | %s |"
             % (stats["enabled"], "dry-run 未写库" if ctx["dryRun"] else COLLECT_TABLE),
             "", "| USC | 结论 | 市场 | K线 | 产物路径 | 说明 |", "|---|---|---|---|---|---|"]
    for item in stats["results"]:
        lines.append("| %s | %s | %s | %d | %s | %s |"
                     % (item["usc"], item["outcome"], item["market"], item["bars"],
                        item["remotePath"] or "-", dataCell(item["detail"]) or "-"))
    if stats["mismatches"]:
        lines += ["", "### 未命中留痕（视图无行 / 参数不全）", ""]
        lines += ["- %s：%s" % (usc, reason) for usc, reason in stats["mismatches"]]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def pickTargets(client, cfg):
    """定本轮目标：返回 (目标行, 上下文, 无法采集的留痕列表)。

    优先级：INPUT_USC（手工验证）> 时间片候选池。
    """
    explicit = chunks(env("INPUT_USC"))
    if explicit:
        rows = client.rowsByUscs(explicit)
        found = {cell(row.get(KEY_COLUMN)): row for row in rows}
        targets, missing = [], []
        for usc in explicit:
            row = found.get(usc)
            if row is None:
                missing.append((usc, "视图 %s 里没有该 usc 的行（provider 可能不是 FTMM）" % VIEW_TABLE))
                continue
            targets.append(row)
        digest, size = poolDigest(rows)
        return targets, {"slot": 0, "slotStart": "-", "slotEnd": "-", "slotSeconds": 0,
                         "poolOffset": 0, "poolSize": size, "poolDigest": digest,
                         "targetSource": "手工指定 INPUT_USC → %d 条" % len(explicit)}, missing

    pool = client.pendingRows()
    digest, size = poolDigest(pool)
    slot = envInt("INPUT_SLOT", slotOf(time.time(), cfg["slotSeconds"]), minimum=0)
    start, end = slotRange(slot, cfg["slotSeconds"])
    targets, offset, _size = pickBatch(pool, slot, cfg["batchSize"])
    ctx = {"slot": slot, "slotStart": start, "slotEnd": end, "slotSeconds": cfg["slotSeconds"],
           "poolOffset": offset, "poolSize": size, "poolDigest": digest,
           "targetSource": "时间片候选池"}
    return targets, ctx, []


def main():
    """入口：定目标 → 逐条采集 → 回写配置 → 留痕 → 汇总。返回退出码。"""
    cfg = {
        "branch": env("INPUT_BRANCH", DEFAULT_BRANCH),
        "outDir": env("INPUT_OUT_DIR", DEFAULT_OUT_DIR),
        "priceField": env("INPUT_PRICE_FIELD", DEFAULT_PRICE_FIELD),
        "slotSeconds": envInt("INPUT_SLOT_SECONDS", DEFAULT_SLOT_SECONDS, minimum=60),
        "batchSize": envInt("INPUT_BATCH_SIZE", DEFAULT_BATCH_SIZE, minimum=1, maximum=50),
        "interval": envFloat("INPUT_INTERVAL", DEFAULT_INTERVAL),
        "dryRun": envFlag("INPUT_DRY_RUN"),
        "allowFailure": envFlag("INPUT_ALLOW_VERIFY_FAILURE", True),
        "fixture": None,
        "repo": None,
    }
    if cfg["priceField"] not in ("auto", "cc_price", "price"):
        raise TaskError("INPUT_PRICE_FIELD 取值非法：%r" % cfg["priceField"])

    fixturePath = env("INPUT_FIXTURE")
    if fixturePath:
        if not os.path.isfile(fixturePath):
            raise TaskError("样本文件不存在：%s" % fixturePath)
        with open(fixturePath, encoding="utf-8") as handle:
            payload = json.load(handle, parse_float=decimal.Decimal)
        if not isinstance(payload, dict):
            raise TaskError("样本文件不是 JSON 对象：%s" % fixturePath)
        cfg["fixture"] = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    cfg["repo"] = RepoAccess(cfg["branch"])
    client = SupabaseClient(env(ENV_SUPABASE_REF), env(ENV_SUPABASE_KEY))
    quoteClient = QuoteClient(cfg["interval"])

    print("=" * 78, flush=True)
    print("任务脚本   %s（版本 %s）" % (SCRIPT_NAME, TASK_VERSION), flush=True)
    print("候选来源   %s 中 %s<>%s 的行" % (VIEW_TABLE, ENABLE_COLUMN, ENABLE_VALUE), flush=True)
    print("时间片     %d 秒／片，每片 %d 条%s"
          % (cfg["slotSeconds"], cfg["batchSize"], "（dry-run）" if cfg["dryRun"] else ""), flush=True)
    print("产物分支   %s → %s|%s|%s" % (cfg["branch"], SUCCESS_DIR, FAIL_DIR, MISMATCH_DIR),
          flush=True)
    print("仓库令牌   %s" % ("已提供" if cfg["repo"].usable else "缺失（dry-run 可忽略）"), flush=True)
    print("=" * 78, flush=True)

    stats = {"results": [], "mismatches": [], "enabled": 0, "failures": []}
    targets, ctx, earlyMissing = pickTargets(client, cfg)
    for usc, reason in earlyMissing:
        print("  [告警] %s：%s" % (usc, reason), flush=True)
        stats["mismatches"].append((usc, reason))
        if archiveMismatch(cfg, usc, None, reason, ctx["slot"]):
            stats["failures"].append(usc)

    print("时间片     第 %s 片（%s ~ %s UTC+8）" % (ctx["slot"], ctx["slotStart"], ctx["slotEnd"]),
          flush=True)
    print("候选池     %d 条，摘要 %s" % (ctx["poolSize"], ctx["poolDigest"]), flush=True)
    if ctx["poolSize"]:
        print("偏移映射   池内偏移 %d/%d（slot × 每片条数 mod 池大小）→ 本轮 %d 条"
              % (ctx["poolOffset"], ctx["poolSize"], len(targets)), flush=True)
    print("本轮目标   %d 条：%s" % (len(targets),
                                   "、".join(cell(row.get(KEY_COLUMN)) for row in targets) or "（空）"),
          flush=True)
    if not targets:
        print("候选池为空（全部证券的 flag_enable 都已是 1），本轮结束。", flush=True)
        stepSummary(ctx, stats)
        return 0
    print("-" * 78, flush=True)

    marketFilter = {item.upper() for item in chunks(env("INPUT_MARKETS"))}
    succeeded = []
    for position, row in enumerate(targets, start=1):
        usc = cell(row.get(KEY_COLUMN))
        print("[%d/%d] %s" % (position, len(targets), usc), flush=True)
        market = (cell(row.get("quote_market")) or UNKNOWN_MARKET).upper()
        if marketFilter and market not in marketFilter:
            print("  [SKIPPED] 市场 %s 不在过滤范围内，未发请求" % market, flush=True)
            stats["results"].append({"usc": usc, "outcome": OUTCOME_SKIPPED, "market": market,
                                     "bars": 0, "remotePath": "",
                                     "detail": "市场 %s 不在过滤内" % market, "elapsedMs": 0})
            continue
        try:
            params = queryParams(row)
        except QuoteApiError as exc:
            reason = "%s: %s" % (exc.code, exc.detail)
            print("  [告警] %s %s（转留痕）" % (usc, reason), flush=True)
            stats["mismatches"].append((usc, reason))
            stats["results"].append({"usc": usc, "outcome": OUTCOME_SKIPPED, "market": market,
                                     "bars": 0, "remotePath": "", "detail": reason,
                                     "elapsedMs": 0})
            if archiveMismatch(cfg, usc, row, reason, ctx["slot"]):
                stats["failures"].append(usc)
            continue
        result = process(row, quoteClient, cfg, ctx["slot"])
        stats["results"].append(result)
        if result["outcome"] == OUTCOME_SUCCESS:
            succeeded.append(usc)
        if result.get("uploadFailed"):
            annotate("产物提交失败，请检查令牌与分支", "error")
            stats["failures"].append(usc)

    # 配置回写：只写成功的；一条 PATCH 命中多行（每行取值相同）
    stamp = nowUtcIso()
    if succeeded and not cfg["dryRun"]:
        count, bad = client.enableRows(succeeded, stamp)
        stats["enabled"] = count
        if bad is None:
            print("配置回写   %s：%d 行 → %s='%s'（dt_update=%s，请求 %d 次）"
                  % (COLLECT_TABLE, count, ENABLE_COLUMN, ENABLE_VALUE, stamp,
                     client.requests), flush=True)
            if count != len(succeeded):
                annotate("配置回写命中 %d 行，少于本轮成功的 %d 条（有 usc 不在表里？）"
                         % (count, len(succeeded)))
        else:
            badKeys, reason = bad
            print("  ✗ 配置回写失败：%s" % reason, flush=True)
            stats["failures"].extend(badKeys)
    elif succeeded:
        stats["enabled"] = 0
        print("配置回写   dry-run：本应更新 %d 行（%s）"
              % (len(succeeded), "、".join(succeeded)), flush=True)

    failed = [r for r in stats["results"] if r["outcome"] == OUTCOME_FAILED]
    throttled = [r for r in stats["results"] if r["outcome"] == OUTCOME_THROTTLED]
    skipped = [r for r in stats["results"] if r["outcome"] == OUTCOME_SKIPPED]
    print("-" * 78, flush=True)
    for item in stats["results"]:
        if item["remotePath"]:
            print("  %s → %s" % (item["usc"], item["remotePath"]), flush=True)
    print("本轮结束   目标 %d，成功 %d，失败 %d，限速未采 %d，跳过 %d，"
          "配置回写 %d 行，留痕 %d 条，累计请求 %d 次（限速 %d 次）"
          % (len(stats["results"]), len(stats["results"]) - len(failed) - len(throttled)
             - len(skipped), len(failed), len(throttled), len(skipped),
             stats["enabled"], len(stats["mismatches"]),
             quoteClient.requests + client.requests, quoteClient.throttles), flush=True)
    stepSummary(ctx, stats)

    if failed:
        annotate("有 %d 条证券验证失败，失败原因见 %s/ 下的产物头部" % (len(failed), FAIL_DIR))
    if throttled:
        annotate("有 %d 条因限速未采，建议降低频率后重跑" % len(throttled))
    if stats["failures"]:
        annotate("有提交/回写失败：%s" % "、".join(stats["failures"][:10]), "error")
    if stats["failures"]:
        return 2
    if (failed or throttled) and not cfg["allowFailure"]:
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TaskError as exc:
        annotate("任务级错误：%s" % exc, "error")
        print("任务级错误：%s" % exc, file=sys.stderr, flush=True)
        sys.exit(2)
    except SupabaseError as exc:
        annotate("Supabase 调用失败：%s" % exc, "error")
        print("Supabase 调用失败：%s" % exc, file=sys.stderr, flush=True)
        sys.exit(2)
