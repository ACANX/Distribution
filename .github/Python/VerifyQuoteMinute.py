#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VerifyQuoteMinute.py — 按 USC 逐条验证 moomoo 五日分钟行情并回写 .mvsv 产物。

【任务定位】
  调度侧（外部触发器 → GitHub Actions workflow_dispatch）指定一个或多个 usc，本脚本：

    1. 从同目录的 UscFutuMapping.jsonl.idx 按 usc 取出该证券的查询参数
       （stockId / marketType / marketCode / instrumentType / subInstrumentType / quoteMarket）；
    2. 调 moomoo 公开行情接口 get-quote-minute 取五日分钟行情；
    3. 转成 .mvsv 文本，经同目录的 GitHubCommitContent.py 提交到目标仓库的目标分支：

         成功 → Verify/Success/{quoteMarket}/{usc}.mvsv
         失败 → Verify/Fail/{quoteMarket}/{usc}.mvsv（错误原因写在 # 元信息块里）

【数据来源】
  UscFutuMapping.jsonl.idx —— 定长二进制索引：32 字节文件头 + N × 24 字节记录。
  记录为「usc 16 字节 NUL 右填充 + 行偏移 u32 + 行长 u32」，全部小端。
  文件头含 magic "UFI1"、formatVersion、keyWidth、recordCount、jsonlBytes、jsonlDigestHead。
  UscFutuMapping.jsonl 每行一条 JSON，字段见 UscFutuMapping.mvsv 的列序说明。

【接口与签名】
  GET https://www.moomoo.com/quote-api/quote-v2/get-quote-minute
  quote-token = SHA-256( HMAC-SHA512(key="quote_web", 紧凑JSON参数)[:10] )[:10]
  紧凑 JSON 的键序必须与 URL 查询串一致：stockId、marketType、type、marketCode、
  instrumentType、subInstrumentType、_（毫秒时间戳）。

【入参】
  命令行参数与 GitHub Actions 的 INPUT_* 环境变量二选一，同名参数以命令行为准。
  例：INPUT_USC=000001 INPUT_BRANCH=quote-meta python3 VerifyQuoteMinute.py

【用法】
  python3 VerifyQuoteMinute.py --usc 000001                      # 采一条并提交
  python3 VerifyQuoteMinute.py --usc 000001,00700 --dry-run      # 只本地生成，不提交
  python3 VerifyQuoteMinute.py --usc 000001 --no-upload          # 同上
  python3 VerifyQuoteMinute.py --watchlist VerifyQuoteMinuteWatchlist.txt
  python3 VerifyQuoteMinute.py --usc 000001 --fixture resp.json  # 用本地响应样本代替联网

【退出码】
  0  全部验证成功；或虽有失败但传了 --allow-verify-failure
  1  有证券验证失败（Fail 件已回写），或命中限速（默认不写 Fail 件，见 --throttle-writes-fail）
  2  任务级错误（索引不可用、参数非法、产物提交失败）
"""

import argparse
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
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 脚本自身信息
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_NAME = os.path.basename(os.path.abspath(__file__))
TASK_VERSION = "1"

# 映射表产物（与本脚本同目录）
IDX_NAME = "UscFutuMapping.jsonl.idx"
JSONL_NAME = "UscFutuMapping.jsonl"

# 行情接口
BASE_URL = "https://www.moomoo.com/quote-api/quote-v2"
QUOTE_MINUTE_PATH = "/get-quote-minute"
SIGN_KEY = "quote_web"
HMAC_TRUNCATE_LENGTH = 10
TOKEN_LENGTH = 10
TIMESTAMP_KEY = "_"
DEFAULT_QUOTE_TYPE = "2"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
DEFAULT_REFERER = "https://www.moomoo.com/"
DEFAULT_TIMEOUT = 30
DEFAULT_INTERVAL_SECONDS = 10.0

# 签名键序（必须与 URL 查询串顺序一致）
QUERY_FIELD_ORDER = [
    "stockId",
    "marketType",
    "type",
    "marketCode",
    "instrumentType",
    "subInstrumentType",
    "req_section",
    TIMESTAMP_KEY,
]

# 索引文件布局
IDX_MAGIC = b"UFI1"
IDX_HEADER_SIZE = 32
IDX_RECORD_SIZE = 24
IDX_KEY_WIDTH = 16
IDX_SUPPORTED_FORMAT_VERSION = 1

# 产物落点
REMOTE_ROOT = "Verify"
REMOTE_DIR_BY_OUTCOME = {"SUCCESS": "Success", "FAILED": "Fail"}
UNKNOWN_MARKET = "UNKNOWN"
DEFAULT_BRANCH = "quote-meta"
DEFAULT_OUT_DIR = "verify-out"

# 采集时间口径：与既有 .mvsv 行情文件一致，Asia/Shanghai（UTC+8）
CHINA_TIMEZONE = datetime.timezone(datetime.timedelta(hours=8))

# .mvsv 列定义（与既有 GLD_Min_*.mvsv / GCMain_Min_*.mvsv 逐列一致）
MVSV_FIELDS = [
    "Ts", "Date", "Time", "Open", "Close", "Low", "High",
    "Volume", "Turnover", "ChangePrice", "ChangePercent",
]
MVSV_FIELD_NAMES = [
    "时间戳(UTC)", "日期", "时间", "开盘价", "收盘价", "最低价", "最高价",
    "成交量", "成交额", "涨跌值", "涨跌幅(%)",
]
MVSV_FIELD_TYPES = [
    "int", "int", "int", "Decimal", "Decimal", "Decimal", "Decimal",
    "Decimal", "Decimal", "Decimal", "str",
]
MVSV_FIELDS_TEXT = "|".join(MVSV_FIELDS)
MVSV_FIELD_NAMES_TEXT = "|".join(MVSV_FIELD_NAMES)
MVSV_FIELD_TYPES_TEXT = "|".join(MVSV_FIELD_TYPES)

# 任务脚本读入的 Actions 入参环境变量名
INPUT_ENV = {
    "usc": "INPUT_USC",
    "branch": "INPUT_BRANCH",
    "markets": "INPUT_MARKETS",
    "interval": "INPUT_INTERVAL",
    "limit": "INPUT_LIMIT",
    "priceField": "INPUT_PRICE_FIELD",
    "outDir": "INPUT_OUT_DIR",
    "owner": "INPUT_OWNER",
    "repo": "INPUT_REPO",
    "dryRun": "INPUT_DRY_RUN",
    "allowVerifyFailure": "INPUT_ALLOW_VERIFY_FAILURE",
    "throttleWritesFail": "INPUT_THROTTLE_WRITES_FAIL",
    "watchlist": "INPUT_WATCHLIST",
}

# 结论取值
OUTCOME_SUCCESS = "SUCCESS"
OUTCOME_FAILED = "FAILED"
OUTCOME_THROTTLED = "THROTTLED"
OUTCOME_SKIPPED = "SKIPPED"


class TaskError(Exception):
    """任务级错误（不可继续执行，退出码 2）。

    Attributes:
        message: 中文错误描述。
    """

    def __init__(self, message: str) -> None:
        """初始化任务级错误。

        Args:
            message: 中文错误描述。

        Returns:
            无。
        """
        super().__init__(message)


class QuoteMinuteError(Exception):
    """单条证券的验证失败（可继续处理下一条）。

    Attributes:
        code: 失败分类码（如 INDEX_MISS / HTTP_ERROR / API_ERROR / EMPTY）。
        detail: 错误详情文本。
        throttled: 是否属于限速拦截。
        httpStatus: HTTP 状态码（网络层失败时为 None）。
    """

    def __init__(
        self,
        code: str,
        detail: str,
        throttled: bool = False,
        httpStatus: Optional[int] = None,
    ) -> None:
        """初始化验证失败。

        Args:
            code: 失败分类码。
            detail: 错误详情文本。
            throttled: 是否属于限速拦截。
            httpStatus: HTTP 状态码。

        Returns:
            无。
        """
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.throttled = throttled
        self.httpStatus = httpStatus


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def nowChina() -> datetime.datetime:
    """返回当前的中国时区（UTC+8）时间。

    Returns:
        带时区的当前时间。
    """
    return datetime.datetime.now(CHINA_TIMEZONE)


def stampOf(moment: Optional[datetime.datetime] = None) -> str:
    """把时刻格式化为 .mvsv 的采集时间文本。

    Args:
        moment: 时刻；为 None 时取当前时间。

    Returns:
        形如 2026-09-20 15:51:27 的文本。
    """
    return (moment or nowChina()).strftime("%Y-%m-%d %H:%M:%S")


def sanitizeMvsvValue(value: Any) -> str:
    """清洗写进 .mvsv 数据行的取值。

    数据行按 | 切分、列数固定，字段内出现竖线或换行会破坏这一前提，
    因此这里一律把竖线、回车、换行替换成可见的安全字符。

    Args:
        value: 原始取值。

    Returns:
        清洗后的文本；None 返回空串。
    """
    if value is None:
        return ""
    text = str(value)
    return (text.replace("|", "/").replace("\r", " ").replace("\n", " ")).strip()


def sanitizeLineValue(value: Any) -> str:
    """清洗写进 .mvsv 元信息行的取值。

    元信息行以「第一个冒号」分键值，取值内的竖线不影响解析，故只压平换行——
    换行会把一行拆成两行，是唯一真正会破坏结构的情况。

    Args:
        value: 原始取值。

    Returns:
        清洗后的文本；None 返回空串。
    """
    if value is None:
        return ""
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def quoteIfNeeded(value: Any) -> str:
    """按 .mvsv 惯例给含竖线的取值加上成对双引号。

    Args:
        value: 原始取值。

    Returns:
        可直接写入元信息行的取值文本。
    """
    text = sanitizeLineValue(value)
    if "|" in text:
        return '"%s"' % text
    return text


def formatCell(value: Any) -> str:
    """把接口返回的单元格值转成 .mvsv 文本。

    JSON 解析时浮点已用 Decimal 承接，故 str() 即保持原始小数位。

    Args:
        value: 接口取值（None / int / Decimal / str）。

    Returns:
        单元格文本；None 返回空串。
    """
    if value is None:
        return ""
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return repr(value)
    return str(value)


def chunksOf(text: Any) -> List[str]:
    """把逗号或空白分隔的入参切成去重后的列表。

    Args:
        text: 原始入参文本。

    Returns:
        去重且保持原序的条目列表；空入参返回空列表。
    """
    if text is None:
        return []
    normalized = str(text).replace(",", " ").replace("，", " ").replace("\n", " ")
    seen: Dict[str, None] = {}
    for item in normalized.split():
        item = item.strip()
        if item:
            seen.setdefault(item, None)
    return list(seen.keys())


def envOrNone(name: str) -> Optional[str]:
    """读取环境变量，空白视为未设置。

    Args:
        name: 环境变量名。

    Returns:
        去空白后的取值；未设置或为空时返回 None。
    """
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def envFlag(name: str) -> bool:
    """读取布尔型环境变量。

    Args:
        name: 环境变量名。

    Returns:
        取值为 1/true/yes/on（忽略大小写）时返回 True。
    """
    value = envOrNone(name)
    return value is not None and value.lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# 索引读取
# ---------------------------------------------------------------------------


class UscFutuMappingIndex:
    """UscFutuMapping.jsonl.idx 索引会话（打开一次、反复按 usc 查询）。

    Attributes:
        idxPath: 索引文件路径。
        jsonlPath: 被索引的 JSONL 路径。
        recordCount: 索引条数。
        jsonlBytes: 索引声明的 JSONL 字节长度。
        digestHead: 索引记录的 JSONL SHA-256 前 8 字节（十六进制）。
        keyWidth: 键宽。
    """

    def __init__(self, idxPath: str, jsonlPath: str) -> None:
        """初始化索引会话（不读盘）。

        Args:
            idxPath: 索引文件路径。
            jsonlPath: JSONL 文件路径。

        Returns:
            无。
        """
        self.idxPath = idxPath
        self.jsonlPath = jsonlPath
        self.recordCount = 0
        self.jsonlBytes = 0
        self.digestHead = ""
        self.keyWidth = IDX_KEY_WIDTH
        self._idx = b""
        self._jsonl = None  # type: ignore[assignment]

    def open(self, verifyDigest: bool = True) -> "UscFutuMappingIndex":
        """装载索引并做配对校验。

        校验三层中的前两层必然执行：第一层比对 jsonlBytes 与实际长度，
        第二层在每次命中后核对目标行确实以 {"usc":"<键>" 开头；
        第三层（读完整份 JSONL 比对摘要）由 verifyDigest 控制。

        Args:
            verifyDigest: 是否执行摘要校验（需读完整个 JSONL，约 155 ms）。

        Returns:
            自身，便于链式调用。

        Raises:
            TaskError: 文件缺失、文件头非法、索引与 JSONL 不配对时抛出。
        """
        if not os.path.isfile(self.idxPath):
            raise TaskError("索引文件不存在：%s" % self.idxPath)
        if not os.path.isfile(self.jsonlPath):
            raise TaskError("JSONL 文件不存在：%s" % self.jsonlPath)

        with open(self.idxPath, "rb") as handle:
            self._idx = handle.read()

        if len(self._idx) < IDX_HEADER_SIZE:
            raise TaskError("索引文件长度不足 32 字节：%s" % self.idxPath)
        if self._idx[0:4] != IDX_MAGIC:
            raise TaskError(
                "索引文件 magic 不是 UFI1（实际 %r）：%s"
                % (self._idx[0:4], self.idxPath))

        formatVersion, keyWidth = struct.unpack_from("<HH", self._idx, 4)
        if formatVersion != IDX_SUPPORTED_FORMAT_VERSION:
            raise TaskError(
                "索引格式版本不支持：formatVersion=%d（本脚本支持 %d）"
                % (formatVersion, IDX_SUPPORTED_FORMAT_VERSION))
        if keyWidth != IDX_KEY_WIDTH:
            raise TaskError(
                "索引键宽不支持：keyWidth=%d（本脚本支持 %d，键宽变化会同时改变格式版本，"
                "不应硬读）" % (keyWidth, IDX_KEY_WIDTH))

        self.keyWidth = keyWidth
        self.recordCount = struct.unpack_from("<I", self._idx, 8)[0]
        self.jsonlBytes = struct.unpack_from("<Q", self._idx, 12)[0]
        self.digestHead = self._idx[20:28].hex()

        expectedSize = IDX_HEADER_SIZE + self.recordCount * IDX_RECORD_SIZE
        if len(self._idx) != expectedSize:
            raise TaskError(
                "索引长度与声明条数不符：声明 %d 条应为 %d 字节，实际 %d 字节"
                % (self.recordCount, expectedSize, len(self._idx)))

        actualBytes = os.path.getsize(self.jsonlPath)
        if actualBytes != self.jsonlBytes:
            raise TaskError(
                "索引与 JSONL 不配对：索引声明 %d 字节，实际 %d 字节（换过 JSONL 需重新产出索引）"
                % (self.jsonlBytes, actualBytes))

        self._jsonl = open(self.jsonlPath, "rb")
        if verifyDigest:
            self._verifyDigest()
        return self

    def _verifyDigest(self) -> None:
        """比对 JSONL 的 SHA-256 前 8 字节与索引文件头记录值。

        Returns:
            无。

        Raises:
            TaskError: 摘要不一致时抛出（长度相同但换过文件）。
        """
        digest = hashlib.sha256()
        self._jsonl.seek(0)
        while True:
            block = self._jsonl.read(1 << 20)
            if not block:
                break
            digest.update(block)
        actualHead = digest.digest()[0:8].hex()
        if actualHead != self.digestHead:
            raise TaskError(
                "索引摘要与 JSONL 不符：索引记录 %s，实际 %s（索引与数据不同步）"
                % (self.digestHead, actualHead))

    def lookup(self, usc: str) -> Optional[Dict[str, Any]]:
        """按 usc 取出该证券的映射记录。

        Args:
            usc: 统一证券代码。

        Returns:
            映射记录字典；未命中返回 None。

        Raises:
            TaskError: 命中行的前缀与键不符（偏移错位或索引不同步）。
        """
        key = usc.encode("utf-8")
        low, high = 0, self.recordCount - 1
        while low <= high:
            mid = (low + high) >> 1
            base = IDX_HEADER_SIZE + mid * IDX_RECORD_SIZE
            current = self._idx[base:base + self.keyWidth].rstrip(b"\x00")
            if current == key:
                offset, length = struct.unpack_from("<II", self._idx, base + self.keyWidth)
                return self._readRecord(usc, offset, length)
            if current < key:
                low = mid + 1
            else:
                high = mid - 1
        return None

    def _readRecord(self, usc: str, offset: int, length: int) -> Dict[str, Any]:
        """按偏移定位读一行 JSONL 并解析。

        Args:
            usc: 查询键（用于命中后的前缀核对）。
            offset: 行起始字节偏移。
            length: 行字节长度（不含行结束符）。

        Returns:
            映射记录字典。

        Raises:
            TaskError: 定位到的行前缀与键不符，或行内容不是 JSON 对象。
        """
        self._jsonl.seek(offset)
        raw = self._jsonl.read(length)
        expectedPrefix = ('{"usc":"%s"' % usc).encode("utf-8")
        if not raw.startswith(expectedPrefix):
            raise TaskError(
                "索引命中 %s 但目标行前缀不符（偏移错位或索引与数据不同步）：%r"
                % (usc, raw[:48]))
        try:
            record = json.loads(raw.decode("utf-8"), parse_float=decimal.Decimal)
        except ValueError as exc:
            raise TaskError("JSONL 行解析失败（usc=%s）：%s" % (usc, exc)) from exc
        if not isinstance(record, dict):
            raise TaskError("JSONL 行不是 JSON 对象（usc=%s）" % usc)
        return record

    def close(self) -> None:
        """关闭 JSONL 文件句柄。

        Returns:
            无。
        """
        if self._jsonl is not None:
            self._jsonl.close()
            self._jsonl = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 行情接口客户端
# ---------------------------------------------------------------------------


def generateQuoteToken(params: Dict[str, Any]) -> str:
    """按前端算法生成 quote-token。

    Args:
        params: 查询参数（键序即签名键序，值全部字符串化）。

    Returns:
        10 位小写十六进制 token。
    """
    inputJson = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
    hmacHex = hmac.new(
        SIGN_KEY.encode("utf-8"), inputJson.encode("utf-8"), hashlib.sha512
    ).hexdigest()
    return hashlib.sha256(hmacHex[:HMAC_TRUNCATE_LENGTH].encode("utf-8")).hexdigest()[
        :TOKEN_LENGTH
    ]


def buildQueryParams(record: Dict[str, Any], quoteType: str) -> Dict[str, str]:
    """由映射记录拼出 get-quote-minute 的查询参数。

    Args:
        record: UscFutuMapping.jsonl 的映射记录。
        quoteType: 行情类型，2 表示五日分钟。

    Returns:
        按签名键序排列的参数字典。

    Raises:
        QuoteMinuteError: 记录缺少必填参数时抛出。
    """
    required = ("stockId", "marketType", "marketCode", "instrumentType", "subInstrumentType")
    missing = [name for name in required if record.get(name) is None]
    if missing:
        raise QuoteMinuteError(
            "INDEX_FIELD_MISSING",
            "映射记录缺少必填参数：%s" % ", ".join(missing))

    params: Dict[str, str] = {
        "stockId": str(record["stockId"]),
        "marketType": str(record["marketType"]),
        "type": str(quoteType),
        "marketCode": str(record["marketCode"]),
        "instrumentType": str(record["instrumentType"]),
        "subInstrumentType": str(record["subInstrumentType"]),
    }
    ordered: Dict[str, str] = {}
    for key in QUERY_FIELD_ORDER:
        if key in params:
            ordered[key] = params[key]
    return ordered


class QuoteMinuteClient:
    """moomoo 五日分钟行情客户端（内置限速、限速退避与网络重试）。

    Attributes:
        minIntervalSeconds: 相邻两次请求的最小间隔（秒）。
        requestCount: 累计请求次数。
        throttleCount: 命中限速的次数。
    """

    def __init__(
        self,
        minIntervalSeconds: float = DEFAULT_INTERVAL_SECONDS,
        timeout: int = DEFAULT_TIMEOUT,
        maxThrottleRetries: int = 3,
        throttleBackoffSeconds: float = 30.0,
        maxBackoffSeconds: float = 120.0,
        maxNetworkRetries: int = 3,
        networkRetrySeconds: float = 5.0,
    ) -> None:
        """初始化客户端。

        Args:
            minIntervalSeconds: 相邻两次请求的最小间隔（秒）。
            timeout: 单次请求超时（秒）。
            maxThrottleRetries: 命中限速后的最大重试次数。
            throttleBackoffSeconds: 限速退避基数（秒），按 2 的幂次递增。
            maxBackoffSeconds: 单次退避上限（秒）。
            maxNetworkRetries: 网络层瞬时错误的最大重试次数。
            networkRetrySeconds: 网络错误重试间隔基数（秒），按次数线性递增。

        Returns:
            无。
        """
        self.minIntervalSeconds = minIntervalSeconds
        self.timeout = timeout
        self.maxThrottleRetries = maxThrottleRetries
        self.throttleBackoffSeconds = throttleBackoffSeconds
        self.maxBackoffSeconds = maxBackoffSeconds
        self.maxNetworkRetries = maxNetworkRetries
        self.networkRetrySeconds = networkRetrySeconds
        self.requestCount = 0
        self.throttleCount = 0
        self.lastRequestAt = 0.0

    def _waitForInterval(self) -> None:
        """按最小间隔等待，避免请求过密触发拦截。

        Returns:
            无。
        """
        if self.lastRequestAt <= 0:
            return
        elapsed = time.time() - self.lastRequestAt
        wait = self.minIntervalSeconds - elapsed
        if wait > 0:
            print("    [限速等待 %.0fs]" % wait, flush=True)
            time.sleep(wait)

    @staticmethod
    def isThrottleResponse(raw: str) -> bool:
        """判断响应体是否为限速拦截页。

        该接口正常响应恒为 JSON；实测被限速时返回 HTML 拦截页（HTTP 200 或 403，
        正文形如 403 - Operations too frequent），但不总带固定文案，
        故凡非 JSON 一律按「可疑拦截」处理——即便实为网关错误页，
        走「不写 Fail 件、可重跑」这条路也比把瞬时状态固化成失败留痕安全。

        Args:
            raw: 原始响应文本。

        Returns:
            True 表示返回的不是业务 JSON。
        """
        return not raw.lstrip().startswith("{")

    def _send(self, url: str, token: str) -> Tuple[int, str]:
        """发起一次 HTTP 请求。

        网络层瞬时错误（TLS 中断、超时、连接重置）自动重试，避免偶发抖动污染记录。

        Args:
            url: 完整请求 URL。
            token: quote-token 签名。

        Returns:
            (HTTP 状态码, 响应文本) 二元组。

        Raises:
            QuoteMinuteError: 网络层重试耗尽后仍然失败时抛出。
        """
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "cache-control": "no-cache",
            "referer": DEFAULT_REFERER,
            "user-agent": DEFAULT_USER_AGENT,
            "quote-token": token,
        }
        request = urllib.request.Request(url, headers=headers)
        lastError: Optional[BaseException] = None
        for attempt in range(self.maxNetworkRetries + 1):
            self.lastRequestAt = time.time()
            self.requestCount += 1
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.status, response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode("utf-8", "replace")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                lastError = exc
                if attempt >= self.maxNetworkRetries:
                    break
                wait = self.networkRetrySeconds * (attempt + 1)
                print("    [网络错误 %s，%.0fs 后重试（第 %d/%d 次）]"
                      % (type(exc).__name__, wait, attempt + 1, self.maxNetworkRetries),
                      flush=True)
                time.sleep(wait)
        raise QuoteMinuteError(
            "NETWORK_ERROR",
            "网络请求失败（已重试 %d 次）：%s" % (self.maxNetworkRetries, lastError))

    def query(self, params: Dict[str, str]) -> Dict[str, Any]:
        """调用 get-quote-minute 取五日分钟行情。

        命中限速时按指数退避重试，重试耗尽才判为 THROTTLED。

        Args:
            params: 查询参数（不含时间戳，键序即签名键序）。

        Returns:
            响应 data 字典（含 stockId、list、last_close_price、time_section 等）。

        Raises:
            QuoteMinuteError: 限速重试耗尽、响应解析失败或业务码非 0 时抛出。
        """
        url = ""
        for retry in range(self.maxThrottleRetries + 1):
            self._waitForInterval()
            attemptParams = dict(params)
            attemptParams[TIMESTAMP_KEY] = str(int(time.time() * 1000))
            url = BASE_URL + QUOTE_MINUTE_PATH + "?" + urllib.parse.urlencode(attemptParams)
            status, raw = self._send(url, generateQuoteToken(attemptParams))
            if not self.isThrottleResponse(raw):
                break
            self.throttleCount += 1
            if retry >= self.maxThrottleRetries:
                raise QuoteMinuteError(
                    "THROTTLED",
                    "响应非 JSON，按限速拦截处理且重试耗尽（HTTP %s）：%s"
                    % (status, raw[:120].replace("\n", " ")),
                    throttled=True,
                    httpStatus=status)
            backoff = min(self.throttleBackoffSeconds * (2 ** retry), self.maxBackoffSeconds)
            print("    [命中限速，退避 %.0fs 后重试（第 %d/%d 次）]"
                  % (backoff, retry + 1, self.maxThrottleRetries), flush=True)
            time.sleep(backoff)

        if not raw.lstrip().startswith("{"):
            raise QuoteMinuteError(
                "BAD_RESPONSE",
                "响应非 JSON（HTTP %s）：%s" % (status, raw[:120].replace("\n", " ")),
                httpStatus=status)

        try:
            payload = json.loads(raw, parse_float=decimal.Decimal)
        except ValueError as exc:
            raise QuoteMinuteError(
                "BAD_RESPONSE", "响应解析失败：%s" % raw[:120]) from exc

        if payload.get("code") != 0:
            raise QuoteMinuteError(
                "API_ERROR",
                "接口业务失败：code=%s, message=%s"
                % (payload.get("code"), payload.get("message")),
                httpStatus=status)
        return payload.get("data") or {}


# ---------------------------------------------------------------------------
# .mvsv 组装
# ---------------------------------------------------------------------------


def renderMvsv(headerLines: List[str], dataLines: List[str]) -> str:
    """把元信息块与数据行拼成 .mvsv 文本。

    约定：UTF-8 无 BOM、只认 LF、文件头与数据之间一个空行、末尾不写换行。

    Args:
        headerLines: 以 # 开头的元信息行。
        dataLines: 数据行。

    Returns:
        .mvsv 全文。
    """
    text = "\n".join(list(headerLines) + [""] + list(dataLines))
    if text.endswith("\n"):
        text = text[:-1]
    return text


def buildMvsvHeader(
    usc: str,
    market: str,
    record: Optional[Dict[str, Any]],
    count: int,
    fetchTime: str,
    remark: str,
    extra: List[Tuple[str, str]],
) -> List[str]:
    """拼出 .mvsv 的元信息行。

    前 19 行与既有行情 .mvsv 文件逐键一致（标题、数据供应商、字段、字段名称、字段类型、
    计数、采集时间、证券代码、市场、备注及各自的英文镜像键），其后是溯源与结论块。

    Args:
        usc: 统一证券代码。
        market: 行情市场段（quoteMarket）。
        record: 映射记录；未命中索引时为 None。
        count: 数据行条数。
        fetchTime: 采集时刻文本。
        remark: 备注（含竖线时自动加引号）。
        extra: 追加的溯源/结论键值对列表。

    Returns:
        元信息行列表。
    """
    title = "%s 分钟级行情数据" % usc
    englishTitle = "%s Minute Quote Data" % usc
    lines = [
        "# 标题 : %s" % title,
        "# 数据供应商 : FT",
        "# 字段 : \"%s\"" % MVSV_FIELDS_TEXT,
        "# 字段名称 : \"%s\"" % MVSV_FIELD_NAMES_TEXT,
        "# 字段类型 : \"%s\"" % MVSV_FIELD_TYPES_TEXT,
        "# 计数 : %d" % count,
        "# 采集时间 : \"%s\"" % fetchTime,
        "# 证券代码 : %s" % quoteIfNeeded(usc),
        "# 市场 : %s" % quoteIfNeeded(market),
        "# 备注 : %s" % quoteIfNeeded(remark),
        "# Title : %s" % englishTitle,
        "# DataProvider : FT",
        "# Field : \"%s\"" % MVSV_FIELDS_TEXT,
        "# FieldName : \"%s\"" % MVSV_FIELDS_TEXT,
        "# FieldType : \"%s\"" % MVSV_FIELD_TYPES_TEXT,
        "# Count : %d" % count,
        "# FetchTime : \"%s\"" % fetchTime,
        "# SecuCode : %s" % quoteIfNeeded(usc),
        "# Market : %s" % quoteIfNeeded(market),
        "# 以下为本程序追加的元信息（读取方可忽略）",
    ]
    if record is not None:
        lines.append("# 统一证券代码 : %s" % quoteIfNeeded(record.get("usc", usc)))
        for key, field in (("富途代码", "futuSymbol"), ("URL类型段", "typeSymbol"),
                           ("行情市场段", "quoteMarket"), ("证券名称", "nameSc"),
                           ("富途证券id", "stockId")):
            value = quoteIfNeeded(record.get(field))
            if value:
                lines.append("# %s : %s" % (key, value))
    else:
        lines.append("# 统一证券代码 : %s" % quoteIfNeeded(usc))
        lines.append("# 行情市场段 : %s" % quoteIfNeeded(market))
    lines.append("# 时间口径 : Ts 为 Unix 秒（UTC）；Date/Time 为同一时刻的 UTC+8 日历值")
    lines.append("# 价格口径 : Close 取接口的复权价，Open 取上一根的 Close，首根取昨收价")
    for key, value in extra:
        text = quoteIfNeeded(value)
        if text:
            lines.append("# %s : %s" % (key, text))
    return lines


def buildSuccessMvsv(
    record: Dict[str, Any],
    data: Dict[str, Any],
    params: Dict[str, str],
    index: UscFutuMappingIndex,
    priceField: str,
) -> Tuple[str, int, str]:
    """把接口返回的分钟行情转成 .mvsv 全文。

    列映射（与既有行情 .mvsv 一致）：
      Ts ← time；Date/Time ← Ts 的 UTC+8 日历值；Open ← 上一根的 Close；
      Close ← cc_price（缺失可回退 price）；Low/High 留空；Volume ← volume；
      Turnover ← turnover；ChangePrice ← change_price；ChangePercent ← ratio。

    Args:
        record: 映射记录。
        data: 接口返回的 data 字典。
        params: 本次请求参数（写入溯源元信息）。
        index: 索引会话（取摘要与条数写溯源）。
        priceField: 收盘价取用口径：auto / cc_price / price。

    Returns:
        (mvsv 全文, 数据行条数, 采集时刻) 三元组。

    Raises:
        QuoteMinuteError: K 线为空时抛出。
    """
    bars = data.get("list") or []
    if not bars:
        raise QuoteMinuteError("EMPTY", "接口返回成功但 K 线为空")

    previousClose: Optional[Any] = data.get("last_close_price")
    rows: List[str] = []
    firstTs: Optional[int] = None
    lastTs: Optional[int] = None
    for bar in bars:
        rawTs = bar.get("time")
        try:
            ts = int(rawTs)
        except (TypeError, ValueError):
            continue
        moment = datetime.datetime.fromtimestamp(ts, CHINA_TIMEZONE)
        close = pickClose(bar, priceField)
        openValue = previousClose if previousClose is not None else close
        rows.append("|".join([
            str(ts),
            moment.strftime("%Y%m%d"),
            moment.strftime("%H%M%S"),
            formatCell(openValue),
            formatCell(close),
            "",
            "",
            formatCell(bar.get("volume")),
            formatCell(bar.get("turnover")),
            formatCell(bar.get("change_price")),
            sanitizeMvsvValue(formatCell(bar.get("ratio"))),
        ]))
        previousClose = close
        if firstTs is None:
            firstTs = ts
        lastTs = ts

    if not rows:
        raise QuoteMinuteError("EMPTY", "接口返回的 K 线均无有效时间戳")

    fetchTime = stampOf()
    market = str(record.get("quoteMarket") or UNKNOWN_MARKET)
    remark = "汇总: K线=%d|首根=%s|末根=%s|昨收=%s" % (
        len(rows), firstTs, lastTs, formatCell(data.get("last_close_price")))
    extra: List[Tuple[str, str]] = [
        ("验证结论", OUTCOME_SUCCESS),
        ("请求参数", "|".join("%s=%s" % item for item in params.items())),
        ("请求地址", BASE_URL + QUOTE_MINUTE_PATH),
        ("映射来源", "%s @ .github/Python/%s" % (JSONL_NAME, JSONL_NAME)),
        ("索引摘要", index.digestHead),
        ("索引条数", str(index.recordCount)),
        ("数据根数", str(len(rows))),
        ("昨收价", formatCell(data.get("last_close_price"))),
        ("任务脚本", SCRIPT_NAME),
        ("任务版本", TASK_VERSION),
        ("采集时刻", fetchTime),
    ]
    header = buildMvsvHeader(record.get("usc", ""), market, record, len(rows),
                             fetchTime, remark, extra)
    return renderMvsv(header, rows), len(rows), fetchTime


def pickClose(bar: Dict[str, Any], priceField: str) -> Any:
    """按指定口径取一根 K 线的收盘价。

    Args:
        bar: 分钟 K 线字典。
        priceField: auto / cc_price / price。

    Returns:
        收盘价取值。
    """
    if priceField == "cc_price":
        return bar.get("cc_price")
    if priceField == "price":
        return bar.get("price")
    value = bar.get("cc_price")
    if value is None:
        value = bar.get("price")
    return value


def buildFailureMvsv(
    usc: str,
    record: Optional[Dict[str, Any]],
    error: QuoteMinuteError,
    params: Optional[Dict[str, str]],
    index: UscFutuMappingIndex,
) -> Tuple[str, str]:
    """把一次失败的验证写成 .mvsv（无数据行，错误原因写在元信息块）。

    Args:
        usc: 统一证券代码。
        record: 映射记录；未命中索引时为 None。
        error: 失败详情。
        params: 本次请求参数；未发请求时为 None。
        index: 索引会话。

    Returns:
        (mvsv 全文, 行情市场段) 二元组。
    """
    fetchTime = stampOf()
    market = UNKNOWN_MARKET
    if record is not None:
        market = str(record.get("quoteMarket") or UNKNOWN_MARKET)
    remark = "汇总: 验证失败|K线=0|错误代码=%s" % error.code
    extra: List[Tuple[str, str]] = [
        ("验证结论", OUTCOME_FAILED),
        ("错误代码", error.code),
        ("错误原因", error.detail),
        ("请求参数", "|".join("%s=%s" % item for item in (params or {}).items())),
        ("请求地址", BASE_URL + QUOTE_MINUTE_PATH),
        ("映射来源", "%s @ .github/Python/%s" % (JSONL_NAME, JSONL_NAME)),
        ("索引摘要", index.digestHead),
        ("索引条数", str(index.recordCount)),
        ("任务脚本", SCRIPT_NAME),
        ("任务版本", TASK_VERSION),
        ("采集时刻", fetchTime),
        ("失效提示", "本件为失败留痕；若 Verify/Success/ 下存在同名且采集时刻更晚的文件，以成功件为准"),
    ]
    header = buildMvsvHeader(usc, market, record, 0, fetchTime, remark, extra)
    return renderMvsv(header, []), market


# ---------------------------------------------------------------------------
# 产物提交
# ---------------------------------------------------------------------------


def loadCommitModule() -> Any:
    """导入同目录的 GitHubCommitContent 模块。

    Returns:
        已导入的 GitHubCommitContent 模块。

    Raises:
        TaskError: 模块不在同目录或导入失败时抛出。
    """
    if SCRIPT_DIR not in sys.path:
        sys.path.insert(0, SCRIPT_DIR)
    try:
        import GitHubCommitContent  # type: ignore
    except ImportError as exc:
        raise TaskError(
            "无法导入同目录的 GitHubCommitContent.py（%s）：%s" % (SCRIPT_DIR, exc)) from exc
    return GitHubCommitContent


def resolveOwnerRepo(owner: Optional[str], repo: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """解析目标仓库属主与仓库名。

    优先级：显式入参 → GITHUB_REPOSITORY 环境变量（Actions 内置）→ .git/config。

    Args:
        owner: 显式属主。
        repo: 显式仓库名。

    Returns:
        (owner, repo) 二元组；解析不到时对应位置为 None。
    """
    resolvedOwner, resolvedRepo = owner, repo
    combined = envOrNone("GITHUB_REPOSITORY")
    if combined and "/" in combined:
        parts = combined.split("/", 1)
        resolvedOwner = resolvedOwner or parts[0]
        resolvedRepo = resolvedRepo or parts[1]
    if resolvedOwner and resolvedRepo:
        return resolvedOwner, resolvedRepo
    try:
        fromGitOwner, fromGitRepo = loadCommitModule().load_owner_repo_from_git_config()
    except TaskError:
        return resolvedOwner, resolvedRepo
    return resolvedOwner or fromGitOwner, resolvedRepo or fromGitRepo


def remotePathOf(outcome: str, market: str, usc: str) -> str:
    """算出产物的仓库内落点。

    Args:
        outcome: 结论（SUCCESS / FAILED）。
        market: 行情市场段。
        usc: 统一证券代码。

    Returns:
        形如 Verify/Success/SZ/000001.mvsv 的相对路径。
    """
    directory = REMOTE_DIR_BY_OUTCOME.get(outcome, "Fail")
    return "%s/%s/%s/%s.mvsv" % (REMOTE_ROOT, directory, market or UNKNOWN_MARKET, usc)


class ArtifactUploader:
    """产物提交器（薄包装 GitHubCommitContent）。

    Attributes:
        owner: 目标仓库属主。
        repo: 目标仓库名。
        branch: 目标分支。
        enabled: 是否真正提交（dry-run 时关闭）。
    """

    def __init__(
        self,
        owner: Optional[str],
        repo: Optional[str],
        branch: str,
        enabled: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        """初始化提交器。

        Args:
            owner: 目标仓库属主。
            repo: 目标仓库名。
            branch: 目标分支。
            enabled: 是否真正提交。
            timeout: 单次 HTTP 请求超时（秒）。

        Returns:
            无。
        """
        self.owner = owner
        self.repo = repo
        self.branch = branch
        self.enabled = enabled
        self.timeout = timeout

    def upload(self, pathKey: str, localFile: str, commitMsg: str) -> Dict[str, Any]:
        """提交本地文件到目标分支的目标路径。

        Args:
            pathKey: 仓库内目标路径。
            localFile: 本地文件路径。
            commitMsg: 提交说明。

        Returns:
            GitHubCommitContent 的结果 dict（含 success / message / http_status）。
        """
        if not self.enabled:
            return {"success": True, "message": None, "path": pathKey,
                    "http_status": None, "skipped": True}
        module = loadCommitModule()
        result = module.commit_content_file(
            pathKey, localFile, branch=self.branch, commit_msg=commitMsg,
            owner=self.owner, repo=self.repo, timeout=self.timeout)
        return result


# ---------------------------------------------------------------------------
# 单条证券的处理
# ---------------------------------------------------------------------------


def writeLocal(outDir: str, pathKey: str, content: str) -> str:
    """把 .mvsv 写到本地镜像目录，便于排查与作为 Actions 产物留档。

    Args:
        outDir: 本地镜像目录。
        pathKey: 仓库内相对路径。
        content: .mvsv 全文。

    Returns:
        本地文件绝对路径。
    """
    localPath = os.path.join(outDir, pathKey.replace("/", os.sep))
    os.makedirs(os.path.dirname(localPath), exist_ok=True)
    with open(localPath, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    return localPath


def loadFixture(path: str) -> Dict[str, Any]:
    """读取本地接口响应样本，供脱离联网自测。

    兼容两种形态：完整响应体（{"code":0,...,"data":{...}}）与裸 data 对象
    （历史留存的 {"stockId":...,"list":[...]}）。

    Args:
        path: 样本文件路径。

    Returns:
        接口 data 字典。

    Raises:
        TaskError: 文件读取失败或解析失败时抛出。
    """
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle, parse_float=decimal.Decimal)
    except (OSError, ValueError) as exc:
        raise TaskError("样本文件读取失败（%s）：%s" % (path, exc)) from exc
    if not isinstance(payload, dict):
        raise TaskError("样本文件不是 JSON 对象：%s" % path)
    if isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload


def processOne(
    usc: str,
    record: Optional[Dict[str, Any]],
    index: UscFutuMappingIndex,
    client: QuoteMinuteClient,
    uploader: ArtifactUploader,
    options: "Options",
) -> Dict[str, Any]:
    """处理一条证券：取行情 → 转 mvsv → 提交。

    Args:
        usc: 统一证券代码。
        record: 索引中查到的映射记录；未命中时为 None。
        index: 索引会话。
        client: 行情客户端。
        uploader: 产物提交器。
        options: 运行选项。

    Returns:
        结果字典（outcome / market / bars / remotePath / localPath / detail 等）。
    """
    startedAt = time.time()
    params: Optional[Dict[str, str]] = None
    result: Dict[str, Any] = {"usc": usc, "outcome": OUTCOME_FAILED, "market": UNKNOWN_MARKET,
                              "bars": 0, "remotePath": "", "localPath": "", "detail": "",
                              "commitStatus": None}

    try:
        if record is None:
            raise QuoteMinuteError("INDEX_MISS", "usc 不在 UscFutuMapping.jsonl.idx 中")
        params = buildQueryParams(record, options.quoteType)

        if options.fixture:
            data = loadFixture(options.fixture)
        else:
            data = client.query(params)

        content, barCount, _ = buildSuccessMvsv(record, data, params, index, options.priceField)
        result.update(outcome=OUTCOME_SUCCESS, bars=barCount)
    except QuoteMinuteError as exc:
        isThrottle = exc.throttled
        outcome = OUTCOME_THROTTLED if isThrottle else OUTCOME_FAILED
        result.update(outcome=outcome, detail="%s: %s" % (exc.code, exc.detail))
        if isThrottle and not options.throttleWritesFail:
            result["remotePath"] = ""
            result["elapsedMs"] = int((time.time() - startedAt) * 1000)
            _logResult(result)
            return result
        content, market = buildFailureMvsv(usc, record, exc, params, index)
        result["market"] = market

    market = str(record.get("quoteMarket") or UNKNOWN_MARKET) if record else UNKNOWN_MARKET
    result["market"] = market
    remotePath = remotePathOf(result["outcome"], market, usc)
    result["remotePath"] = remotePath
    localPath = writeLocal(options.outDir, remotePath, content)
    result["localPath"] = localPath

    if uploader.enabled:
        commitMsg = "Verify %s %s [%s]" % (result["outcome"], usc, market)
        upload = uploader.upload(remotePath, localPath, commitMsg)
        result["commitStatus"] = upload.get("http_status")
        if not upload.get("success"):
            result["outcome"] = OUTCOME_FAILED
            result["detail"] = "提交失败: %s" % upload.get("message")
            result["uploadFailed"] = True

    result["elapsedMs"] = int((time.time() - startedAt) * 1000)
    _logResult(result)
    return result


def _logResult(result: Dict[str, Any]) -> None:
    """把一条结果打印成单行日志。

    Args:
        result: 结果字典。

    Returns:
        无。
    """
    print("  [%s] %-12s market=%-8s bars=%-5d %sms %s"
          % (result["outcome"], result["usc"], result["market"], result["bars"],
             result.get("elapsedMs", 0), result["remotePath"] or result["detail"]),
          flush=True)


# ---------------------------------------------------------------------------
# 入参与主流程
# ---------------------------------------------------------------------------


class Options:
    """运行选项集合。

    Attributes:
        quoteType: 行情类型（2 = 五日分钟）。
        priceField: 收盘价取用口径。
        outDir: 本地镜像目录。
        fixture: 本地响应样本路径（替代联网）。
        throttleWritesFail: 限速时是否也写 Fail 件。
        abortOnThrottle: 命中限速后是否中止本轮剩余条目。
    """

    def __init__(self) -> None:
        """初始化空选项。

        Returns:
            无。
        """
        self.quoteType = DEFAULT_QUOTE_TYPE
        self.priceField = "auto"
        self.outDir = DEFAULT_OUT_DIR
        self.fixture = None  # type: Optional[str]
        self.throttleWritesFail = False
        self.abortOnThrottle = True


def readWatchlist(path: str) -> List[str]:
    """读取待验证清单文件（每行一个 usc，# 开头为注释）。

    Args:
        path: 清单文件路径。

    Returns:
        usc 列表；文件不存在时返回空列表。
    """
    if not path or not os.path.isfile(path):
        return []
    collected: List[str] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.split("#", 1)[0].strip()
            if line:
                collected.extend(chunksOf(line))
    return collected


def parseArgs(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """解析命令行参数（命令行优先，其次 INPUT_* 环境变量）。

    Args:
        argv: 参数列表；为 None 时取 sys.argv[1:]。

    Returns:
        解析结果命名空间。
    """
    parser = argparse.ArgumentParser(
        description="按 USC 验证 moomoo 五日分钟行情并回写 .mvsv 产物")
    parser.add_argument("--usc", default=envOrNone(INPUT_ENV["usc"]),
                        help="待验证 usc，多个用逗号或空白分隔")
    parser.add_argument("--watchlist", default=envOrNone(INPUT_ENV["watchlist"])
                        or os.path.join(SCRIPT_DIR, "VerifyQuoteMinuteWatchlist.txt"),
                        help="待验证清单文件（每行一个 usc，# 为注释）")
    parser.add_argument("--markets", default=envOrNone(INPUT_ENV["markets"]),
                        help="只处理这些行情市场段（逗号分隔，留空不限）")
    parser.add_argument("--limit", type=int,
                        default=int(envOrNone(INPUT_ENV["limit"]) or 0),
                        help="本批最多处理多少条（0 表示不限）")
    parser.add_argument("--interval", type=float,
                        default=float(envOrNone(INPUT_ENV["interval"]) or DEFAULT_INTERVAL_SECONDS),
                        help="相邻两次请求的最小间隔秒数（默认 %.0f）" % DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--quote-type", default=DEFAULT_QUOTE_TYPE,
                        help="行情类型，2 = 五日分钟（默认 2）")
    parser.add_argument("--price-field", default=envOrNone(INPUT_ENV["priceField"]) or "auto",
                        choices=["auto", "cc_price", "price"],
                        help="收盘价取用口径（默认 auto：优先复权价 cc_price）")
    parser.add_argument("--branch", default=envOrNone(INPUT_ENV["branch"]) or DEFAULT_BRANCH,
                        help="产物提交目标分支（默认 %s）" % DEFAULT_BRANCH)
    parser.add_argument("--owner", default=envOrNone(INPUT_ENV["owner"]), help="目标仓库属主")
    parser.add_argument("--repo", default=envOrNone(INPUT_ENV["repo"]), help="目标仓库名")
    parser.add_argument("--out-dir", default=envOrNone(INPUT_ENV["outDir"]) or DEFAULT_OUT_DIR,
                        help="本地镜像目录（默认 %s）" % DEFAULT_OUT_DIR)
    parser.add_argument("--fixture", default=None,
                        help="用本地接口响应样本代替联网（自测用）")
    parser.add_argument("--no-upload", action="store_true", help="只本地生成，不提交")
    parser.add_argument("--dry-run", action="store_true", help="等价于 --no-upload")
    parser.add_argument("--no-digest-check", action="store_true",
                        help="跳过索引摘要校验（少读一遍整份 JSONL）")
    parser.add_argument("--allow-verify-failure", action="store_true",
                        default=envFlag(INPUT_ENV["allowVerifyFailure"]),
                        help="即使有证券验证失败也返回退出码 0")
    parser.add_argument("--throttle-writes-fail", action="store_true",
                        default=envFlag(INPUT_ENV["throttleWritesFail"]),
                        help="命中限速时也写 Fail 件（默认不写，避免瞬时状态污染台账）")
    parser.add_argument("--no-abort-on-throttle", action="store_true",
                        help="命中限速后仍继续处理剩余条目（默认提前收工，省配额）")
    parser.add_argument("--throttle-retries", type=int, default=3,
                        help="命中限速后的最大重试次数（默认 3，设 0 表示不重试）")
    parser.add_argument("--throttle-backoff", type=float, default=30.0,
                        help="限速退避基数秒数，按 2 的幂次递增、上限 120s（默认 30）")
    parser.add_argument("--max-minutes", type=float, default=0.0,
                        help="本轮时间预算（分钟），0 表示不限")
    return parser.parse_args(argv)


def writeStepSummary(results: List[Dict[str, Any]], branch: str, dryRun: bool) -> None:
    """把结果写入 GitHub Actions 步骤摘要（未在 Actions 中运行时跳过）。

    Args:
        results: 结果列表。
        branch: 目标分支。
        dryRun: 是否 dry-run。

    Returns:
        无。
    """
    summaryPath = envOrNone("GITHUB_STEP_SUMMARY")
    if not summaryPath:
        return
    success = [item for item in results if item["outcome"] == OUTCOME_SUCCESS]
    failed = [item for item in results if item["outcome"] == OUTCOME_FAILED]
    throttled = [item for item in results if item["outcome"] == OUTCOME_THROTTLED]
    rows = [
        "## VerifyQuoteMinute 运行结果",
        "",
        "| 项 | 值 | 备注 |",
        "|---|---|---|",
        "| 目标分支 | %s | %s |" % (branch, "dry-run 未提交" if dryRun else "产物已提交"),
        "| 处理条数 | %d | 输入去重后的条数 |" % len(results),
        "| 成功 | %d | 落在 Verify/Success/ |" % len(success),
        "| 失败 | %d | 落在 Verify/Fail/ |" % len(failed),
        "| 限速未采 | %d | 未写产物，需重跑 |" % len(throttled),
        "",
        "| USC | 结论 | 市场 | K线 | 产物路径 | 说明 |",
        "|---|---|---|---|---|---|",
    ]
    for item in results:
        rows.append("| %s | %s | %s | %d | %s | %s |"
                    % (item["usc"], item["outcome"], item["market"], item["bars"],
                       item["remotePath"] or "-", summaryCell(item["detail"] or "-")))
    with open(summaryPath, "a", encoding="utf-8") as handle:
        handle.write("\n".join(rows) + "\n")


def summaryCell(value: Any) -> str:
    """把取值压成可安全放进 Markdown 表格单元格的文本。

    Args:
        value: 原始取值。

    Returns:
        单行且不含竖线的文本。
    """
    return sanitizeMvsvValue(value)


def emitAnnotation(message: str, level: str = "warning") -> None:
    """输出 GitHub Actions 注解（本地运行时退化为普通打印）。

    Args:
        message: 注解正文。
        level: warning / notice / error。

    Returns:
        无。
    """
    if envOrNone("GITHUB_ACTIONS"):
        print("::%s::%s" % (level, message.replace("\n", " ")), flush=True)
    else:
        print("[%s] %s" % (level.upper(), message), flush=True)


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口。

    Args:
        argv: 参数列表；为 None 时取 sys.argv[1:]。

    Returns:
        进程退出码：0 全成功、1 有验证失败、2 任务级错误。
    """
    args = parseArgs(argv)
    options = Options()
    options.quoteType = args.quote_type
    options.priceField = args.price_field
    options.outDir = args.out_dir
    options.fixture = args.fixture
    options.throttleWritesFail = args.throttle_writes_fail
    options.abortOnThrottle = not args.no_abort_on_throttle

    targets = chunksOf(args.usc)
    if not targets:
        targets = readWatchlist(args.watchlist)
        if targets:
            print("已从清单 %s 读入 %d 条待验证 usc" % (args.watchlist, len(targets)), flush=True)
    marketFilter = {item.upper() for item in chunksOf(args.markets)}
    if args.limit > 0:
        targets = targets[:args.limit]

    if not targets:
        print("未指定待验证 usc（--usc 为空且清单 %s 无有效条目），本次不做任何事。"
              % args.watchlist, flush=True)
        return 0

    dryRun = args.dry_run or args.no_upload
    owner, repo = resolveOwnerRepo(args.owner, args.repo)
    idxPath = os.path.join(SCRIPT_DIR, IDX_NAME)
    jsonlPath = os.path.join(SCRIPT_DIR, JSONL_NAME)

    print("=" * 72)
    print("任务脚本   %s（版本 %s）" % (SCRIPT_NAME, TASK_VERSION))
    print("待验证     %d 条：%s" % (len(targets), ", ".join(targets)))
    print("索引文件   %s" % idxPath)
    print("目标仓库   %s/%s @ %s%s"
          % (owner or "?", repo or "?", args.branch, "（dry-run 不提交）" if dryRun else ""))
    print("请求间隔   %.1fs（行情类型 type=%s）" % (args.interval, args.quote_type))
    print("=" * 72)

    index = UscFutuMappingIndex(idxPath, jsonlPath)
    client = QuoteMinuteClient(minIntervalSeconds=args.interval,
                               maxThrottleRetries=args.throttle_retries,
                               throttleBackoffSeconds=args.throttle_backoff)
    uploader = ArtifactUploader(owner, repo, args.branch, enabled=not dryRun)
    results: List[Dict[str, Any]] = []
    deadline = (time.time() + args.max_minutes * 60) if args.max_minutes else 0.0

    try:
        index.open(verifyDigest=not args.no_digest_check)
        print("索引已装载：%d 条，摘要 %s" % (index.recordCount, index.digestHead), flush=True)

        for position, usc in enumerate(targets, start=1):
            if deadline and time.time() > deadline:
                print("已达时间预算，本轮提前收工（完成 %d/%d）" % (position - 1, len(targets)),
                      flush=True)
                break
            print("[%d/%d] %s" % (position, len(targets), usc), flush=True)
            record = index.lookup(usc)
            market = str(record.get("quoteMarket") or UNKNOWN_MARKET) if record else UNKNOWN_MARKET
            if marketFilter and market.upper() not in marketFilter:
                results.append({"usc": usc, "outcome": OUTCOME_SKIPPED, "market": market,
                                "bars": 0, "remotePath": "", "localPath": "",
                                "detail": "市场 %s 不在 --markets 过滤内" % market,
                                "commitStatus": None, "elapsedMs": 0})
                print("  [SKIPPED] 市场 %s 不在过滤范围内，未发请求" % market, flush=True)
                continue
            result = processOne(usc, record, index, client, uploader, options)
            results.append(result)
            if result["outcome"] == OUTCOME_THROTTLED and options.abortOnThrottle:
                print("本条命中限速，同 IP 下后续请求大概率同样被拦，本轮提前收工"
                      "（剩余 %d 条未处理）" % (len(targets) - position), flush=True)
                break
    except TaskError as exc:
        emitAnnotation("任务级错误：%s" % exc, "error")
        print("任务级错误：%s" % exc, file=sys.stderr, flush=True)
        return 2
    finally:
        index.close()

    success = [item for item in results if item["outcome"] == OUTCOME_SUCCESS]
    failed = [item for item in results if item["outcome"] == OUTCOME_FAILED]
    throttled = [item for item in results if item["outcome"] == OUTCOME_THROTTLED]
    skipped = [item for item in results if item["outcome"] == OUTCOME_SKIPPED]

    print("-" * 72)
    print("本轮结束：成功 %d，失败 %d，限速未采 %d，跳过 %d，累计请求 %d 次，命中限速 %d 次"
          % (len(success), len(failed), len(throttled), len(skipped),
             client.requestCount, client.throttleCount))
    for item in results:
        if item["remotePath"]:
            print("  %s → %s" % (item["usc"], item["remotePath"]))
    writeStepSummary(results, args.branch, dryRun)

    uploadFailures = [item for item in results if item.get("uploadFailed")]
    if uploadFailures:
        emitAnnotation("有 %d 条产物提交失败，请检查令牌与分支" % len(uploadFailures), "error")
        return 2
    if failed:
        emitAnnotation("有 %d 条证券验证失败，失败原因见 Verify/Fail/ 下的产物头部"
                       % len(failed), "warning")
    if throttled:
        emitAnnotation("有 %d 条因限速未采（默认未写产物），建议降低频率后重跑" % len(throttled),
                       "warning")
    if (failed or throttled) and not args.allow_verify_failure:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
