#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VerifyQuoteMinute.py — 按 usc 验证 moomoo 五日分钟行情，转 .mvsv 回写仓库。

入参：环境变量 INPUT_*（由工作流 .github/workflows/VerifyQuoteMinute.yml 注入，
本地自测直接设同名环境变量即可）。无命令行参数。

    INPUT_USC                    待验证 usc，逗号或空白分隔
    INPUT_WATCHLIST              清单文件路径；INPUT_USC 为空时改读它
    INPUT_BRANCH                 产物提交目标分支，默认 quote-meta
    INPUT_OUT_DIR                本地镜像目录，默认 verify-out
    INPUT_MARKETS                只处理这些行情市场段（逗号分隔）
    INPUT_LIMIT                  本批最多处理几条，0 不限
    INPUT_INTERVAL               相邻请求最小间隔秒数，默认 10
    INPUT_PRICE_FIELD            收盘价口径：auto / cc_price / price
    INPUT_DRY_RUN                置真则只本地生成、不提交
    INPUT_ALLOW_VERIFY_FAILURE   置真则验证失败也返回退出码 0
    INPUT_THROTTLE_WRITES_FAIL   置真则限速也写 Fail 件（默认不写，避免瞬时状态污染台账）
    INPUT_ABORT_ON_THROTTLE      默认真；置假则命中限速后继续跑剩余条目
    INPUT_FIXTURE                用本地接口响应样本代替联网（自测用）

流程：按 usc 从 UscFutuMapping.jsonl.idx 取出请求参数 → 调 get-quote-minute
→ 转 .mvsv → 经 GitHubCommitContent.py 提交到 Verify/Success|Fail/{market}/{usc}.mvsv。

退出码：0 全部成功（或有失败但允许）、1 有验证失败或限速未采、2 任务级错误。
"""

import datetime
import decimal
import hashlib
import hmac
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_NAME = os.path.basename(os.path.abspath(__file__))
TASK_VERSION = "1"

IDX_NAME = "UscFutuMapping.jsonl.idx"
JSONL_NAME = "UscFutuMapping.jsonl"
IDX_MAGIC = b"UFI1"
IDX_HEADER, IDX_RECORD, IDX_KEY = 32, 24, 16

BASE_URL = "https://www.moomoo.com/quote-api/quote-v2"
QUOTE_PATH = "/get-quote-minute"
SIGN_KEY = "quote_web"
TIMESTAMP_KEY = "_"
DEFAULT_QUOTE_TYPE = "2"
DEFAULT_INTERVAL = 10.0
DEFAULT_BRANCH = "quote-meta"
DEFAULT_OUT_DIR = "verify-out"
DEFAULT_PRICE_FIELD = "auto"
THROTTLE_RETRIES = 3
THROTTLE_BACKOFF = 30.0
MAX_BACKOFF = 120.0
NETWORK_RETRIES = 3
HTTP_TIMEOUT = 30
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

# 采集时间与 Date/Time 列一律用 UTC+8
CHINA_TZ = datetime.timezone(datetime.timedelta(hours=8))
UNKNOWN_MARKET = "UNKNOWN"
OUTCOME_SUCCESS, OUTCOME_FAILED = "SUCCESS", "FAILED"
OUTCOME_THROTTLED, OUTCOME_SKIPPED = "THROTTLED", "SKIPPED"

# .mvsv 列定义，与既有 GLD_Min_*.mvsv / GCMain_Min_*.mvsv 逐列一致
MVSV_FIELDS = "Ts|Date|Time|Open|Close|Low|High|Volume|Turnover|ChangePrice|ChangePercent"
MVSV_NAMES = "时间戳(UTC)|日期|时间|开盘价|收盘价|最低价|最高价|成交量|成交额|涨跌值|涨跌幅(%)"
MVSV_TYPES = "int|int|int|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|str"


class TaskError(Exception):
    """任务级错误：索引不可用、提交失败等，退出码 2。"""


class QuoteApiError(Exception):
    """单条证券的失败，可继续处理下一条。"""

    def __init__(self, code, detail, throttled=False):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.throttled = throttled


def env(name, default=""):
    """读环境变量，空白视为未设置。"""
    return (os.environ.get(name) or "").strip() or default


def envFlag(name, default=False):
    """读布尔型环境变量，1/true/yes/on 为真。"""
    text = env(name)
    if not text:
        return default
    return text.lower() in ("1", "true", "yes", "on")


def envFloat(name, default):
    """读浮点型环境变量。"""
    text = env(name)
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        raise TaskError("环境变量 %s 不是数字：%r" % (name, text))


def envInt(name, default):
    """读整数型环境变量。"""
    return int(envFloat(name, float(default)))


def chunks(text):
    """把逗号 / 空白分隔的文本切成去重后的列表，保持原序。"""
    words = str(text or "").replace("，", " ").replace(",", " ").split()
    return list(dict.fromkeys(words))


def nowChina():
    """当前 UTC+8 时刻，格式化为 2026-09-20 15:51:27。"""
    return datetime.datetime.now(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def cell(value):
    """接口取值转 .mvsv 单元格文本；None 返回空串。"""
    if value is None:
        return ""
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


# ---------------------------------------------------------------------------
# 映射表：按 usc 取请求参数
# ---------------------------------------------------------------------------


class Mapping:
    """UscFutuMapping.jsonl.idx 索引 + JSONL 会话，按 usc 二分取记录。"""

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
        if len(self.jsonl) != struct.unpack_from("<Q", self.raw, 12)[0]:
            raise TaskError("索引与 JSONL 不配对（换过 JSONL 需重新产出索引）")

    def lookup(self, usc):
        """按 usc 二分定位并解析记录；未命中返回 None。"""
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
                    raise TaskError("索引命中 %s 但目标行前缀不符，索引与数据不同步" % usc)
                return json.loads(line.decode("utf-8"), parse_float=decimal.Decimal)
            low, high = (mid + 1, high) if current < key else (low, mid - 1)
        return None


def queryParams(record, quoteType=DEFAULT_QUOTE_TYPE):
    """由映射记录拼出 get-quote-minute 参数，键序即签名键序。"""
    fields = ("stockId", "marketType", "marketCode", "instrumentType", "subInstrumentType")
    missing = [name for name in fields if record.get(name) is None]
    if missing:
        raise QuoteApiError("INDEX_FIELD_MISSING", "映射记录缺少参数：%s" % ", ".join(missing))
    params = {"stockId": str(record["stockId"]), "marketType": str(record["marketType"]),
              "type": str(quoteType), "marketCode": str(record["marketCode"]),
              "instrumentType": str(record["instrumentType"]),
              "subInstrumentType": str(record["subInstrumentType"])}
    return params


def quoteToken(params):
    """quote-token = SHA-256( HMAC-SHA512(key="quote_web", 紧凑JSON)[:10] )[:10]。"""
    compact = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
    digest = hmac.new(SIGN_KEY.encode(), compact.encode(), hashlib.sha512).hexdigest()
    return hashlib.sha256(digest[:10].encode()).hexdigest()[:10]


class QuoteClient:
    """get-quote-minute 客户端：限速等待、限速退避重试、网络重试。"""

    def __init__(self):
        self.interval = envFloat("INPUT_INTERVAL", DEFAULT_INTERVAL)
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
# .mvsv 组装
# ---------------------------------------------------------------------------


def mvsvHeader(usc, market, record, count, fetchTime, remark, extra):
    """拼 .mvsv 元信息行：前 19 行与既有行情 .mvsv 逐键一致，其后是溯源与结论块。"""
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
    for key, field in (("富途代码", "futuSymbol"), ("URL类型段", "typeSymbol"),
                       ("行情市场段", "quoteMarket"), ("证券名称", "nameSc"),
                       ("富途证券id", "stockId")):
        value = metaValue(record.get(field)) if record else metaValue(
            market if field == "quoteMarket" else "")
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


def successMvsv(record, data, params, mapping, priceField):
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

    usc = record.get("usc", "")
    market = str(record.get("quoteMarket") or UNKNOWN_MARKET)
    fetchTime = nowChina()
    remark = "汇总: K线=%d|首根=%s|末根=%s|昨收=%s" % (
        len(rows), firstTs, lastTs, cell(data.get("last_close_price")))
    header = mvsvHeader(usc, market, record, len(rows), fetchTime, remark, [
        ("验证结论", OUTCOME_SUCCESS),
        ("请求参数", "|".join("%s=%s" % item for item in params.items())),
        ("请求地址", BASE_URL + QUOTE_PATH),
        ("映射来源", "%s @ .github/Python/%s" % (JSONL_NAME, JSONL_NAME)),
        ("索引摘要", mapping.digest),
        ("索引条数", str(mapping.count)),
        ("数据根数", str(len(rows))),
        ("昨收价", cell(data.get("last_close_price"))),
        ("任务脚本", SCRIPT_NAME),
        ("任务版本", TASK_VERSION),
        ("采集时刻", fetchTime),
    ])
    return renderMvsv(header, rows), len(rows), market


def failureMvsv(usc, market, record, error, params, mapping):
    """失败留痕：无数据行，错误原因写在元信息块。"""
    fetchTime = nowChina()
    header = mvsvHeader(usc, market, record, 0, fetchTime,
                        "汇总: 验证失败|K线=0|错误代码=%s" % error.code, [
        ("验证结论", OUTCOME_FAILED),
        ("错误代码", error.code),
        ("错误原因", error.detail),
        ("请求参数", "|".join("%s=%s" % item for item in (params or {}).items())),
        ("请求地址", BASE_URL + QUOTE_PATH),
        ("映射来源", "%s @ .github/Python/%s" % (JSONL_NAME, JSONL_NAME)),
        ("索引摘要", mapping.digest),
        ("索引条数", str(mapping.count)),
        ("任务脚本", SCRIPT_NAME),
        ("任务版本", TASK_VERSION),
        ("采集时刻", fetchTime),
        ("失效提示", "本件为失败留痕；若 Verify/Success/ 下存在同名且采集时刻更晚的文件，以成功件为准"),
    ])
    return renderMvsv(header, [])


# ---------------------------------------------------------------------------
# 产物提交
# ---------------------------------------------------------------------------


def commit(localFile, remotePath, branch, dryRun):
    """经同目录的 GitHubCommitContent.py 提交产物；返回结果 dict。"""
    if dryRun:
        return {"success": True, "message": None, "http_status": None}
    if SCRIPT_DIR not in sys.path:
        sys.path.insert(0, SCRIPT_DIR)
    try:
        import GitHubCommitContent
    except ImportError as exc:
        raise TaskError("无法导入同目录的 GitHubCommitContent.py：%s" % exc) from exc
    owner, repo = GitHubCommitContent.load_owner_repo_from_git_config()
    combined = env("GITHUB_REPOSITORY")
    if "/" in combined:
        owner, repo = combined.split("/", 1)
    return GitHubCommitContent.commit_content_file(
        remotePath, localFile, branch=branch,
        commit_msg="Verify %s" % os.path.basename(remotePath),
        owner=owner, repo=repo)


def remotePath(outcome, market, usc):
    """产物落点：Verify/Success|Fail/{quoteMarket}/{usc}.mvsv。"""
    directory = "Success" if outcome == OUTCOME_SUCCESS else "Fail"
    return "Verify/%s/%s/%s.mvsv" % (directory, market or UNKNOWN_MARKET, usc)


def writeLocal(outDir, remote, text):
    """写本地镜像副本，返回绝对路径。"""
    path = os.path.join(outDir, remote.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return path


def loadFixture(path):
    """读本地响应样本：兼容完整响应体与裸 data 两种形态。"""
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle, parse_float=decimal.Decimal)
    except (OSError, ValueError) as exc:
        raise TaskError("样本文件读取失败（%s）：%s" % (path, exc)) from exc
    if not isinstance(payload, dict):
        raise TaskError("样本文件不是 JSON 对象：%s" % path)
    return payload.get("data") if isinstance(payload.get("data"), dict) else payload


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def process(usc, record, mapping, client, cfg):
    """处理一条证券：取行情 → 转 mvsv → 本地留档 → 提交。返回结果 dict。"""
    started = time.time()
    params, market, bars, detail, uploadFailed = None, UNKNOWN_MARKET, 0, "", False
    if record:
        market = str(record.get("quoteMarket") or UNKNOWN_MARKET)

    try:
        if record is None:
            raise QuoteApiError("INDEX_MISS", "usc 不在 UscFutuMapping.jsonl.idx 中")
        params = queryParams(record)
        data = loadFixture(cfg["fixture"]) if cfg["fixture"] else client.query(params)
        text, bars, market = successMvsv(record, data, params, mapping, cfg["priceField"])
        outcome = OUTCOME_SUCCESS
    except QuoteApiError as exc:
        outcome = OUTCOME_THROTTLED if exc.throttled else OUTCOME_FAILED
        detail = "%s: %s" % (exc.code, exc.detail)
        if exc.throttled and not cfg["throttleWritesFail"]:
            result = {"usc": usc, "outcome": outcome, "market": UNKNOWN_MARKET, "bars": 0,
                      "remotePath": "", "detail": detail, "outcomeAt": int(
                          (time.time() - started) * 1000)}
            logResult(result)
            return result
        text = failureMvsv(usc, market, record, exc, params, mapping)

    path = remotePath(outcome, market, usc)
    local = writeLocal(cfg["outDir"], path, text)
    result = {"usc": usc, "outcome": outcome, "market": market, "bars": bars,
              "remotePath": path, "localPath": local, "detail": detail}
    try:
        upload = commit(local, path, cfg["branch"], cfg["dryRun"])
        if not upload.get("success"):
            uploadFailed = True
            result.update(outcome=OUTCOME_FAILED, detail="提交失败: %s" % upload.get("message"))
    except TaskError:
        raise
    result["uploadFailed"] = uploadFailed
    result["elapsedMs"] = int((time.time() - started) * 1000)
    logResult(result)
    return result


def logResult(result):
    """单行结果日志。"""
    print("  [%s] %-12s market=%-8s bars=%-5d %sms %s"
          % (result["outcome"], result["usc"], result["market"], result["bars"],
             result.get("elapsedMs", 0), result["remotePath"] or result["detail"]), flush=True)


def readWatchlist(path):
    """读待验证清单：每行一个 usc，一行内可多个，# 起为注释。"""
    if not path or not os.path.isfile(path):
        return []
    targets = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            targets.extend(chunks(line.split("#", 1)[0]))
    return targets


def annotate(message, level="warning"):
    """GitHub Actions 注解；本地运行时退化为普通打印。"""
    if env("GITHUB_ACTIONS"):
        print("::%s::%s" % (level, message.replace("\n", " ")), flush=True)
    else:
        print("[%s] %s" % (level.upper(), message), flush=True)


def stepSummary(results, branch, dryRun):
    """写 Actions 步骤摘要（未在 Actions 中运行则跳过）。"""
    path = env("GITHUB_STEP_SUMMARY")
    if not path:
        return
    counts = {name: sum(1 for r in results if r["outcome"] == name)
              for name in (OUTCOME_SUCCESS, OUTCOME_FAILED, OUTCOME_THROTTLED, OUTCOME_SKIPPED)}
    lines = ["## VerifyQuoteMinute 运行结果", "",
             "| 项 | 值 | 备注 |", "|---|---|---|",
             "| 目标分支 | %s | %s |" % (branch, "dry-run 未提交" if dryRun else "产物已提交"),
             "| 处理条数 | %d | 输入去重后的条数 |" % len(results),
             "| 成功 | %d | 落在 Verify/Success/ |" % counts[OUTCOME_SUCCESS],
             "| 失败 | %d | 落在 Verify/Fail/ |" % counts[OUTCOME_FAILED],
             "| 限速未采 | %d | 未写产物，需重跑 |" % counts[OUTCOME_THROTTLED],
             "| 跳过 | %d | 被 INPUT_MARKETS 过滤 |" % counts[OUTCOME_SKIPPED],
             "", "| USC | 结论 | 市场 | K线 | 产物路径 | 说明 |", "|---|---|---|---|---|---|"]
    for item in results:
        lines.append("| %s | %s | %s | %d | %s | %s |"
                     % (item["usc"], item["outcome"], item["market"], item["bars"],
                        item["remotePath"] or "-", dataCell(item["detail"]) or "-"))
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main():
    """入口：读入参 → 逐条采集 → 汇总。返回退出码。"""
    cfg = {
        "branch": env("INPUT_BRANCH", DEFAULT_BRANCH),
        "outDir": env("INPUT_OUT_DIR", DEFAULT_OUT_DIR),
        "priceField": env("INPUT_PRICE_FIELD", DEFAULT_PRICE_FIELD),
        "fixture": env("INPUT_FIXTURE") or None,
        "dryRun": envFlag("INPUT_DRY_RUN"),
        "allowFailure": envFlag("INPUT_ALLOW_VERIFY_FAILURE"),
        "throttleWritesFail": envFlag("INPUT_THROTTLE_WRITES_FAIL"),
        "abortOnThrottle": envFlag("INPUT_ABORT_ON_THROTTLE", True),
    }
    if cfg["priceField"] not in ("auto", "cc_price", "price"):
        raise TaskError("INPUT_PRICE_FIELD 取值非法：%r" % cfg["priceField"])

    targets = chunks(env("INPUT_USC"))
    watchlist = env("INPUT_WATCHLIST", os.path.join(SCRIPT_DIR, "VerifyQuoteMinuteWatchlist.txt"))
    if not targets:
        targets = readWatchlist(watchlist)
        if targets:
            print("已从清单 %s 读入 %d 条待验证 usc" % (watchlist, len(targets)), flush=True)
    limit = envInt("INPUT_LIMIT", 0)
    if limit > 0:
        targets = targets[:limit]
    if not targets:
        print("未指定待验证 usc（INPUT_USC 为空且清单 %s 无有效条目），本次不做任何事。"
              % watchlist, flush=True)
        return 0

    marketFilter = {item.upper() for item in chunks(env("INPUT_MARKETS"))}
    mapping = Mapping()
    client = QuoteClient()
    print("=" * 72)
    print("任务脚本   %s（版本 %s）" % (SCRIPT_NAME, TASK_VERSION))
    print("待验证     %d 条：%s" % (len(targets), ", ".join(targets)))
    print("映射表     %d 条，索引摘要 %s" % (mapping.count, mapping.digest))
    print("目标分支   %s%s" % (cfg["branch"], "（dry-run 不提交）" if cfg["dryRun"] else ""))
    print("请求间隔   %.1fs" % client.interval)
    print("=" * 72)

    results = []
    for position, usc in enumerate(targets, start=1):
        print("[%d/%d] %s" % (position, len(targets), usc), flush=True)
        try:
            record = mapping.lookup(usc)
        except TaskError as exc:
            annotate("任务级错误：%s" % exc, "error")
            print("任务级错误：%s" % exc, file=sys.stderr, flush=True)
            return 2
        market = str(record.get("quoteMarket") or UNKNOWN_MARKET) if record else UNKNOWN_MARKET
        if marketFilter and market.upper() not in marketFilter:
            results.append({"usc": usc, "outcome": OUTCOME_SKIPPED, "market": market, "bars": 0,
                            "remotePath": "", "detail": "市场 %s 不在过滤内" % market})
            print("  [SKIPPED] 市场 %s 不在过滤范围内，未发请求" % market, flush=True)
            continue
        result = process(usc, record, mapping, client, cfg)
        results.append(result)
        if result.get("uploadFailed"):
            annotate("产物提交失败，请检查令牌与分支", "error")
            return 2
        if result["outcome"] == OUTCOME_THROTTLED and cfg["abortOnThrottle"]:
            print("本条命中限速，同 IP 下后续请求大概率同样被拦，本轮提前收工"
                  "（剩余 %d 条未处理）" % (len(targets) - position), flush=True)
            break

    failed = [r for r in results if r["outcome"] == OUTCOME_FAILED]
    throttled = [r for r in results if r["outcome"] == OUTCOME_THROTTLED]
    print("-" * 72)
    print("本轮结束：成功 %d，失败 %d，限速未采 %d，跳过 %d，累计请求 %d 次，命中限速 %d 次"
          % (len(results) - len(failed) - len(throttled)
             - sum(1 for r in results if r["outcome"] == OUTCOME_SKIPPED),
             len(failed), len(throttled),
             sum(1 for r in results if r["outcome"] == OUTCOME_SKIPPED),
             client.requests, client.throttles))
    for item in results:
        if item["remotePath"]:
            print("  %s → %s" % (item["usc"], item["remotePath"]))
    stepSummary(results, cfg["branch"], cfg["dryRun"])

    if failed:
        annotate("有 %d 条证券验证失败，失败原因见 Verify/Fail/ 下的产物头部" % len(failed))
    if throttled:
        annotate("有 %d 条因限速未采（默认未写产物），建议降低频率后重跑" % len(throttled))
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
