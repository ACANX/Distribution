#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Task18 —— 老行情目录（SecuQuote）切片转换为新链路目录（SecurityQuoteV5/FTMM/Min）
========================================================================================

一、工具定位
----------------------------------------------------------------------------------------
把老链路目录

    Data/Finv/SecuQuote/<证券代码>/*.mvsv        （含 Latest.mvsv 与待聚合的原始文件）

里的历史行情，按「每个输出文件最多 SLICE_ROWS 条记录（默认 1000）」切片，
逐片写到新链路对应证券目录：

    Data/Finv/SecurityQuoteV5/FTMM/Min/<证券代码>/<代码>_Min_<yyyyMMdd>_<HHmmss>.mvsv

切片文件命名与已有的新链路原始文件一致（示例：
Data/Finv/SecurityQuoteV5/FTMM/Min/TSM/TSM_Min_20261021_154205.mvsv），
因此下一次 Task11AggregateLatest（新链路聚合）会把它们并入该目录的 Latest.mvsv。

文件名里的 <yyyyMMdd>_<HHmmss> 取**来源文件的采集时刻**（待聚合文件用文件名标签；
Latest.mvsv 用头部 FetchTime/采集时间，都没有时用末行 ts，最后才用当前时刻），
同一来源的多个切片依次顺延 1 秒。这样 Task11 按文件名升序合并时，这批老数据
会落在它实际采集的时间线位置上，**后采集的新数据仍然覆盖老数据**。

**默认删除已转换的源文件**（删除开关默认开启）：每个源文件只有在
「它的全部切片都写成功且无坏行」之后才会被删除（见 §五、§六）；
只想做镜像 / 演练时用 `--no-delete-source`（或 `CONVERT_DELETE_SOURCE=false`）关闭，
或用 `--keep-latest` 保留 Latest.mvsv。

二、两种目录的格式差异（本脚本负责转换）
----------------------------------------------------------------------------------------
新链路（输出）FTMM 原生 12 列，列序固定：

    ts|d|t|o|h|l|c|v|t|cp|cr|lc
    = Timestamp|Date|Time|Open|High|Low|Close|Volume|Turnover|ChangePrice|ChangeRatio|LastClose

老链路有**两种**源文件格式（脚本按文件头的字段名自动识别，不靠列数硬编码）：

    A. 聚合结果 Latest.mvsv —— 11 列
       FieldName: Ts|Date|Time|Open|Close|Low|High|Volume|Turnover|ChangePrice|ChangeRatio
       与目标的差异：没有 LastClose；Open/Close/Low/High 的列序不同
       （老：Open,Close,Low,High → 新：Open,High,Low,Close）

    B. 待聚合原始文件 <代码>_Min_<yyyyMMdd>_<HHmmss>.mvsv —— 6 列
       FieldName: Ts|Close|Volume|Turnover|ChangePct|ChangePrice
       缺少 Date/Time/Open/High/Low/LastClose：
         - Date/Time 由 ts 推导（ts 是 UTC 秒级时间戳，+8 小时得北京时间）；
         - Open/High/Low 留空（源数据本来没有）。

    LastClose（前收价）老数据里没有这一列，但新链路的语义是「上一行（上一分钟）的
    收盘价」：对已有新链路文件做过全量校验，lc[i] == close[i-1] 精确成立
    （28.5 万行 0 例外，仅每个文件首行为空）。因此本脚本按同一规则回填：
    同一次解析的输入文件内，首行 LastClose 留空，其余行 = 上一行 Close。
    该行为可用 --no-fill-last-close 关闭（关闭后整列留空）。

三、文件头（元信息）从新链路已存在的文件读取
----------------------------------------------------------------------------------------
新链路的头包含 16 个键（标题/字段名称/DataProvider/Field/FieldName/FieldType/
TypeKLine/Count/Symbol/SecuCode/USC/Name/Region/Market/TimeZone/Currency），
其中市场、时区、币种、证券名称等信息老链路没有（老链路只有「市场 : cny」这类
粗粒度值），所以**模板一律取自新链路里同一证券目录下已有的 .mvsv 文件**
（优先 Latest.mvsv，其次任意 *_Min_*.mvsv）：

    - 沿用：字段名称 / DataProvider / Field / FieldName / FieldType / TypeKLine /
      Name / Region / Market / TimeZone / Currency；
    - 按目标证券改写：标题、Symbol、SecuCode、USC；
    - 按实际切片行数重算：Count；
    - 丢弃易变键（FetchTime / 采集时间 / Remark / 备注），与 Task11 的口径一致。

某个证券在新链路没有模板时（目录不存在或目录下没有 .mvsv），该证券失败并提示
先放一个模板文件；不会靠猜测拼 Region/Market 等字段。

四、幂等与可重复演练
----------------------------------------------------------------------------------------
    - 切片文件名时间基准：见 §一（来源采集时刻，同来源切片依次顺延 1 秒）；
    - 每个切片的内容（头 + 数据行）算 sha256，与目标目录里已有 <代码>_Min_*.mvsv
      的内容指纹比对：内容相同即视为「已转换过」，跳过不重写；
    - 内容不同时才写新文件；文件名被占用时自动顺延 1 秒，绝不覆盖已有文件；
    - 因此重复运行本脚本不会堆积重复数据，可以放心多次演练。

五、环境变量与命令行
----------------------------------------------------------------------------------------
    --dry-run              只统计不落盘（演练用）
    --codes A,B            只处理指定证券（默认处理源目录下全部证券）
    --limit N              最多处理 N 个证券（演练用，默认 0 = 不限制）
    --slice-rows N         每个输出文件最多多少条记录（默认 1000）
    --no-fill-last-close   不回填 LastClose（默认回填，见 §二）
    --root PATH            仓库根目录（默认按脚本位置自动推导）
    --delete-source        删除已转换的源文件（**默认开启**，写上也只是显式声明）
    --no-delete-source     关闭删除，只新增切片（演练 / 镜像）
                           也可用环境变量 CONVERT_DELETE_SOURCE=false 关闭
    --keep-latest          保留 Latest.mvsv，只删待聚合的增量文件
                           （也可用环境变量 CONVERT_KEEP_LATEST=true）

    删除规则（保守）：只有该源文件的**全部切片都已落盘（或目标已有同内容切片）**
    且该文件没有坏行、没有 ts 非法行时，才会删除；否则保留并在汇总里列出原因，
    退出码 1 提醒人工核对。

六、退出码
----------------------------------------------------------------------------------------
    0 = 全部证券转换成功（含"内容已存在而跳过"），且删除失败/保留清单为空；
    1 = 致命错误（源/目标目录缺失、--slice-rows 非法）或存在转换失败的证券；
        或因数据不完整 / 删除失败而保留源文件（默认开启删除时）。

【环境要求】Python 3.8+，仅标准库；不需要网络。
"""

import argparse
import datetime
import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path

TASK_NAME = "Task18SecuQuoteToV5Convert"

SCRIPT_DIR = Path(__file__).resolve().parent          # <仓库根>/.github/Python/Quote
DEFAULT_REPO_ROOT = SCRIPT_DIR.parents[2]             # <仓库根>

SOURCE_SUBDIR = Path("Data/Finv/SecuQuote")
TARGET_SUBDIR = Path("Data/Finv/SecurityQuoteV5/FTMM/Min")

DEFAULT_SLICE_ROWS = 1000
# 删除已转换源文件的开关默认值（True = 默认删除；可用 --no-delete-source 关闭）
DEFAULT_DELETE_SOURCE = True
LATEST_NAME = "Latest.mvsv"

# 新链路（输出）的 12 列固定列序
TARGET_COLUMNS = ("ts", "date", "time", "open", "high", "low", "close",
                  "volume", "turnover", "change_price", "change_ratio",
                  "last_close")
TARGET_N_COLS = len(TARGET_COLUMNS)

# 新链路文件头的键顺序（与 Task11AggregateLatest / 已有文件一致）
META_ORDER = (
    "标题", "字段名称", "DataProvider", "Field", "FieldName", "FieldType",
    "TypeKLine", "Count", "Symbol", "SecuCode", "USC", "Name",
    "Region", "Market", "TimeZone", "Currency",
)
# 易变元信息：随采集时刻变化，既不进入聚合结果，也不写进本文档
VOLATILE_META_KEYS = ("FetchTime", "采集时间", "Remark", "备注")

# 文件头里表示列名的键：英文全名优先（# FieldName），中文兜底（# 字段名称）
FIELD_HEADER_KEYS = ("FieldName", "字段名称")

# 源文件头列名（英文全名 / 中文名）→ 中间键
SOURCE_FIELD_MAP = {
    # 英文全名（# FieldName）
    "Ts": "ts", "Timestamp": "ts",
    "Date": "date",
    "Time": "time",
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Volume": "volume",
    "Turnover": "turnover",
    "ChangePrice": "change_price",
    "ChangeRatio": "change_ratio", "ChangePercent": "change_ratio",
    "ChangePct": "change_ratio",
    "LastClose": "last_close",
    # 中文名（# 字段名称）
    "时间戳": "ts", "时间戳(UTC)": "ts", "时间戳（UTC）": "ts",
    "日期": "date",
    "时间": "time",
    "开盘价": "open",
    "最高价": "high",
    "最低价": "low",
    "收盘价": "close",
    "成交量": "volume",
    "成交额": "turnover",
    "涨跌值": "change_price",
    "涨跌幅": "change_ratio", "涨跌幅(%)": "change_ratio",
    "涨跌幅（%）": "change_ratio",
    "前收价": "last_close",
}

# 北京时区偏移（中国无夏令时）
BJT_OFFSET = datetime.timedelta(hours=8)

# 来源文件名的采集时间标签（<代码>_Min_<yyyyMMdd>_<HHmmss>.mvsv）
SOURCE_TAG_RE = re.compile(r"_(\d{8})_(\d{6})\.mvsv$")
# 文件头里的采集时刻（老 Latest.mvsv 有）
FETCH_TIME_KEYS = ("FetchTime", "采集时间")
FETCH_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S")


class ConvertError(Exception):
    """单个证券转换失败（模板缺失 / 结构不可识别等）"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def log(message):
    """输出一行日志（实时刷新，便于 Actions 日志逐行可见）"""
    print(message, flush=True)


def unquote(value):
    """去掉元信息取值外层成对的双引号"""
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def env_flag(name):
    """读取布尔型环境变量（true / 1 / yes / on 为真，大小写不敏感）"""
    return os.environ.get(name, "").strip().lower() in ("true", "1", "yes", "on")


def env_flag_optional(name):
    """读取三态布尔环境变量：True / False / None（未配置）"""
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return None
    return value in ("true", "1", "yes", "on")


def now_bjt():
    """当前北京时间"""
    return datetime.datetime.now(datetime.timezone.utc) + BJT_OFFSET


def ts_to_bjt(ts_text):
    """UTC 秒级时间戳文本 → 北京时间 (yyyyMMdd, HHmmss)；非法返回 (None, None)"""
    text = (ts_text or "").strip()
    if not text:
        return None, None
    try:
        moment = datetime.datetime.fromtimestamp(int(float(text)),
                                                 datetime.timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None, None
    moment = moment + BJT_OFFSET
    return moment.strftime("%Y%m%d"), moment.strftime("%H%M%S")


def parse_moment(date_text, time_text):
    """把 yyyyMMdd + HHmmss 拼成 datetime；非法返回 None"""
    try:
        return datetime.datetime.strptime((date_text or "") + (time_text or ""),
                                          "%Y%m%d%H%M%S")
    except (TypeError, ValueError):
        return None


def source_moment(path, meta, last_ts):
    """确定一个来源文件切片时用的命名时刻（北京时间）

    优先级：文件名里的采集标签 → 文件头的 FetchTime/采集时间 → 末行 ts → 当前时刻。
    用来源时刻而不是当前时刻，是为了让切片在 Task11 按文件名升序合并时落在
    “这批数据实际采集的时刻”，从而保持「后采集覆盖先采集」的语义。

    :return: (datetime, 说明字符串)
    """
    match = SOURCE_TAG_RE.search(path.name)
    if match:
        moment = parse_moment(match.group(1), match.group(2))
        if moment:
            return moment, "文件名标签"
    for key in FETCH_TIME_KEYS:
        value = (meta.get(key) or "").strip()
        for fmt in FETCH_TIME_FORMATS:
            try:
                return datetime.datetime.strptime(value, fmt), "头部 %s" % key
            except ValueError:
                continue
    date_text, time_text = ts_to_bjt(last_ts)
    moment = parse_moment(date_text, time_text)
    if moment:
        return moment, "末行时间戳"
    return now_bjt(), "当前时刻（源文件无时间信息）"


def fmt_meta(key, value):
    """渲染一行元信息（空值或含 : # | 时加双引号，与已有文件一致）"""
    if not value or any(char in value for char in ":#|"):
        return '# %s : "%s"' % (key, value)
    return "# %s : %s" % (key, value)


# ---------------------------------------------------------------------------
# 源文件解析（老链路：11 列 / 6 列，按字段名识别）
# ---------------------------------------------------------------------------

def parse_source_mvsv(path):
    """解析老链路 mvsv 文件

    :param path: 文件路径
    :return: (meta: dict, columns: list[str], records: list[dict], bad: list[(int, str)])
             records 为中间键 dict（原始字符串，未做数值转换）；bad 为坏行说明
    :raises ConvertError: 文件没有可识别的字段头
    """
    text = Path(path).read_text(encoding="utf-8").replace("\r\n", "\n")
    meta = {}
    columns = None
    records = []
    bad = []

    for line_no, line in enumerate(text.split("\n"), 1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            body = stripped[1:].strip()
            if ":" not in body:
                continue
            key, value = body.split(":", 1)
            key = key.strip()
            value = unquote(value.strip())
            meta.setdefault(key, value)
            if key in FIELD_HEADER_KEYS:
                columns = [name.strip() for name in value.split("|")]
            continue

        if columns is None:
            bad.append((line_no, "数据行出现在字段头之前"))
            continue
        values = stripped.split("|")
        if len(values) != len(columns):
            bad.append((line_no, "列数 %d 与字段头 %d 不符"
                        % (len(values), len(columns))))
            continue

        record = {}
        for name, value in zip(columns, values):
            wire_key = SOURCE_FIELD_MAP.get(name)
            if wire_key is not None:
                record[wire_key] = value.strip()
        if not record.get("ts"):
            bad.append((line_no, "缺少时间戳 ts"))
            continue
        records.append(record)

    if columns is None:
        raise ConvertError("文件缺少 # FieldName / # 字段名称 字段头")
    if not any(name in SOURCE_FIELD_MAP for name in columns):
        raise ConvertError("字段头无法识别（支持的列名见 SOURCE_FIELD_MAP）：%s"
                           % "|".join(columns))
    return meta, columns, records, bad


# ---------------------------------------------------------------------------
# 转换为新链路 12 列
# ---------------------------------------------------------------------------

def fill_last_close(records, enabled):
    """给每条记录补充 last_close（新链路口径：上一分钟收盘价）

    :param records: parse_source_mvsv 产出的记录列表（不修改入参）
    :param enabled: False 时整列留空
    :return: 新列表，每条记录都带 last_close（首行为空字符串）
             (新记录列表, 回填行数)
    """
    filled = []
    previous_close = None
    filled_count = 0
    for record in records:
        row = dict(record)
        if enabled and previous_close is not None:
            row["last_close"] = previous_close
            filled_count += 1
        else:
            row["last_close"] = ""
        current = (record.get("close") or "").strip()
        if current:
            previous_close = current
        filled.append(row)
    return filled, filled_count


def to_target_row(record):
    """把一条中间记录转成新链路 12 列的行（日期/时间缺失时由 ts 推导）"""
    date = (record.get("date") or "").strip()
    time = (record.get("time") or "").strip()
    if not date or not time:
        derived_date, derived_time = ts_to_bjt(record.get("ts"))
        date = date or (derived_date or "")
        time = time or (derived_time or "")
    return [record.get("ts", "").strip(), date, time,
            (record.get("open") or "").strip(),
            (record.get("high") or "").strip(),
            (record.get("low") or "").strip(),
            (record.get("close") or "").strip(),
            (record.get("volume") or "").strip(),
            (record.get("turnover") or "").strip(),
            (record.get("change_price") or "").strip(),
            (record.get("change_ratio") or "").strip(),
            (record.get("last_close") or "").strip()]


def slice_rows(rows, size):
    """把行列表按 size 切片"""
    return [rows[i:i + size] for i in range(0, len(rows), size)]


# ---------------------------------------------------------------------------
# 文件头模板（从新链路已有文件读取）
# ---------------------------------------------------------------------------

def read_template(meta):
    """从已解析的新链路文件元信息里取出模板（剔除易变键）"""
    return {key: value for key, value in meta.items()
            if key not in VOLATILE_META_KEYS}


def load_template(code_dir):
    """读取新链路某证券目录下的文件头模板

    优先 Latest.mvsv（最新最全），其次目录内任意一个 *.mvsv（按文件名升序）。

    :param code_dir: 新链路 <Data/.../Min/<代码>> 目录
    :return: (meta: dict, path: Path)；没有可用模板时返回 (None, None)
    """
    if not code_dir.is_dir():
        return None, None
    candidates = []
    latest = code_dir / LATEST_NAME
    if latest.is_file():
        candidates.append(latest)
    candidates.extend(sorted(p for p in code_dir.glob("*.mvsv")
                             if p.name != LATEST_NAME))
    for path in candidates:
        try:
            meta, _columns, _records, _bad = parse_source_mvsv(path)
        except ConvertError:
            continue
        template = read_template(meta)
        if template:
            return template, path
    return None, None


def replace_code_in_text(text, old_code, code):
    """把文本里作为独立词出现的旧代码替换为新代码（标题约定：<code> 分钟级行情数据）"""
    text = (text or "").strip()
    if not text:
        return ""
    if old_code and old_code != code and old_code in text:
        return text.replace(old_code, code)
    if not old_code or old_code == code:
        return text
    return "%s 分钟级行情数据" % code


def replace_code_in_symbol(symbol, old_code, code):
    """改写 Symbol：形如 REGION.CODE 的只替换最后一段代码"""
    text = (symbol or "").strip()
    if not text:
        return code
    if old_code and old_code != code:
        if text.endswith("." + old_code):
            return text[:-len(old_code)] + code
        if text == old_code:
            return code
    return text


def build_meta(template, code, count):
    """用模板生成切片文件的文件头（改写代码相关键、重算 Count）"""
    meta = dict(template)
    old_code = (meta.get("SecuCode") or meta.get("USC") or "").strip()

    meta["标题"] = replace_code_in_text(meta.get("标题"), old_code, code) \
        or "%s 分钟级行情数据" % code
    meta["Symbol"] = replace_code_in_symbol(meta.get("Symbol"), old_code, code)
    meta["SecuCode"] = code
    meta["USC"] = code
    meta["Count"] = str(count)

    if old_code and old_code != code:
        # 模板借自其它证券时，名称不能照抄（本脚本当前只在同代码目录取模板，
        # 这里保留保护，避免将来扩展模板来源时写出错误的证券名）
        meta.pop("Name", None)
    return meta


def render_mvsv(meta, rows):
    """渲染完整的 mvsv 内容（文件头 + 空行 + 数据行），换行固定 LF

    :return: bytes（UTF-8）
    """
    ordered = [key for key in META_ORDER if key in meta]
    extras = sorted(key for key in meta if key not in META_ORDER)
    lines = [fmt_meta(key, meta[key]) for key in ordered + extras]
    lines.append("")
    lines.extend("|".join(row) for row in rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


def content_digest(data):
    """内容指纹（统一 LF 后算 sha256，避免 CRLF 检出导致误判）"""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


# ---------------------------------------------------------------------------
# 输出目录维护
# ---------------------------------------------------------------------------

def existing_digests(code_dir, code):
    """目标目录里已有切片文件的内容指纹 → 文件名"""
    digests = {}
    if not code_dir.is_dir():
        return digests
    for path in sorted(code_dir.glob("%s_Min_*.mvsv" % code)):
        try:
            digests[content_digest(path.read_bytes())] = path.name
        except OSError:
            continue
    return digests


def next_slice_name(code, start, used):
    """生成不冲突的切片文件名（时间戳冲突时顺延 1 秒）"""
    moment = start
    while True:
        name = "%s_Min_%s.mvsv" % (code, moment.strftime("%Y%m%d_%H%M%S"))
        if name not in used:
            used.add(name)
            return name
        moment = moment + datetime.timedelta(seconds=1)


def write_atomic(path, data):
    """原子写文件（tmp + rename），换行恒为 LF"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_",
                               suffix=".mvsv")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# 单证券转换
# ---------------------------------------------------------------------------

def convert_code(code, options, source_root, target_root):
    """把一个老链路证券目录切片转换到新链路目录

    :return: dict 统计（sources/slices/written/skipped_rows/skipped_slices/
             dropped_rows/bad_rows/filled_last_close/files）
    """
    source_dir = source_root / code
    target_dir = target_root / code
    files = sorted(p for p in source_dir.glob("*.mvsv") if p.is_file())
    # 历史（Latest.mvsv）先转换、增量文件后转换：切片文件名的时间戳依次递增，
    # Task11 按文件名升序合并时，增量数据会覆盖历史里的同 ts 行
    files.sort(key=lambda path: (path.name != LATEST_NAME, path.name))
    stats = {
        "code": code,
        "sources": len(files),
        "slices": 0,
        "written": 0,
        "skipped_slices": 0,
        "added_rows": 0,
        "dropped_rows": 0,
        "bad_rows": 0,
        "filled_last_close": 0,
        "files": [],
        "template": None,
        "deleted_sources": 0,
        "kept_sources": [],
    }
    if not files:
        log("  ⏭️ 没有 .mvsv 文件，跳过")
        return stats

    template, template_path = load_template(target_dir)
    if template is None:
        raise ConvertError(
            "新链路目录缺少文件头模板：%s（请先在该目录放一个 %s 或任一切片文件，"
            "本脚本的 Region/Market/TimeZone/Currency/Name 等字段一律取自模板）"
            % (target_dir, LATEST_NAME))
    stats["template"] = str(template_path)
    log("  🧩 文件头模板：%s" % template_path.name)

    digests = existing_digests(target_dir, code)
    used_names = set(digests.values())
    if target_dir.is_dir():
        used_names.update(path.name for path in target_dir.glob("*.mvsv"))

    for path in files:
        meta, columns, records, bad = parse_source_mvsv(path)
        source_code = (meta.get("SecuCode") or meta.get("USC") or "").strip()
        if source_code and source_code != code:
            log("  ⚠️ %s 的头部证券代码是 %s，与目录 %s 不一致，按目录代码转换"
                % (path.name, source_code, code))
        if bad:
            stats["bad_rows"] += len(bad)
            for line_no, reason in bad[:3]:
                log("  ⚠️ %s 第 %d 行坏行已跳过：%s" % (path.name, line_no, reason))
            if len(bad) > 3:
                log("  ⚠️ %s 其余 %d 行坏行同上" % (path.name, len(bad) - 3))
        if not records:
            log("  ⚠️ %s 没有可转换的数据行，跳过" % path)
            if options.delete_source:
                stats["kept_sources"].append((str(path), "没有可转换的数据行"))
            continue

        filled, filled_count = fill_last_close(records,
                                              options.fill_last_close)
        stats["filled_last_close"] += filled_count
        rows = [to_target_row(record) for record in filled]
        start_moment, moment_source = source_moment(
            path, meta, records[-1].get("ts"))
        log("  🕒 切片文件名时间基准：%s（%s）"
            % (start_moment.strftime("%Y-%m-%d %H:%M:%S"), moment_source))
        dropped = sum(1 for row in rows if not row[1] or not row[2])
        if dropped:
            stats["dropped_rows"] += dropped
            log("  ⚠️ %s 有 %d 行无法确定日期/时间（ts 非法），仍按空值写出"
                % (path.name, dropped))

        for rows_part in slice_rows(rows, options.slice_rows):
            data = render_mvsv(build_meta(template, code, len(rows_part)),
                              rows_part)
            digest = content_digest(data)
            stats["slices"] += 1
            if digest in digests:
                stats["skipped_slices"] += 1
                log("  ⏭️ 切片已存在（内容相同，跳过）：%s"
                    % digests[digest])
                continue
            name = next_slice_name(code, start_moment, used_names)
            if options.dry_run:
                log("  🧪 [dry-run] 将写入 %s（%d 行）" % (name, len(rows_part)))
                stats["written"] += 1
                stats["added_rows"] += len(rows_part)
                stats["files"].append(name)
                continue
            write_atomic(target_dir / name, data)
            digests[digest] = name
            stats["written"] += 1
            stats["added_rows"] += len(rows_part)
            stats["files"].append(name)
            log("  ✅ 写入 %s（%d 行）" % (name, len(rows_part)))

        log("  📄 %s：字段头=%s，数据行=%d，回填前收价=%d"
            % (path.name, "|".join(columns), len(records), filled_count))

        decide_delete_source(path, options, stats, bad, dropped)

    return stats


def decide_delete_source(path, options, stats, bad, dropped):
    """根据转换结果决定是否删除源文件（仅在开启删除开关时生效）

    删除条件（全部满足才删）：
        1) 开启 --delete-source / CONVERT_DELETE_SOURCE；
        2) 该文件的所有切片已落盘或目标已有同内容切片（本次未抛异常）；
        3) 该文件没有坏行（列数不符 / 缺 ts），没有 ts 非法行；
        4) 未开启 --keep-latest，或该文件不是 Latest.mvsv。

    :param path: 源文件路径
    :param options: 命令行参数
    :param stats: 单证券统计（就地累加 deleted_sources / kept_sources）
    :param bad: parse_source_mvsv 返回的坏行列表
    :param dropped: ts 非法导致日期/时间为空的行数
    """
    if not options.delete_source:
        return

    reasons = []
    if bad:
        reasons.append("%d 行坏行（列数不符/缺 ts）" % len(bad))
    if dropped:
        reasons.append("%d 行 ts 非法（日期/时间为空）" % dropped)
    if reasons:
        stats["kept_sources"].append((str(path), "，".join(reasons)))
        log("  ⏸️ 保留源文件 %s：%s（数据不完整，不删除）"
            % (path, "，".join(reasons)))
        return
    if options.keep_latest and path.name == LATEST_NAME:
        log("  ⏸️ 保留源文件 %s（--keep-latest）" % path)
        return
    if options.dry_run:
        stats["deleted_sources"] += 1
        log("  🧪 [dry-run] 将删除源文件：%s" % path)
        return
    try:
        os.remove(path)
    except OSError as e:
        stats["kept_sources"].append((str(path), "删除失败：%s" % e))
        log("  ⚠️ 删除源文件失败 %s：%s（已保留）" % (path, e))
        return
    stats["deleted_sources"] += 1
    log("  🗑️ 删除源文件：%s" % path)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="把 Data/Finv/SecuQuote 下的存量 .mvsv 切片转换为 "
                    "Data/Finv/SecurityQuoteV5/FTMM/Min 的新链路格式"
                    "（默认开启删除源文件，可用 --no-delete-source 关闭）")
    parser.add_argument("--root", default=str(DEFAULT_REPO_ROOT),
                        help="仓库根目录（默认按脚本位置推导：%s）"
                             % DEFAULT_REPO_ROOT)
    parser.add_argument("--codes", default="",
                        help="只处理指定证券，逗号分隔（默认全部）")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多处理 N 个证券（演练用，默认 0 = 不限制）")
    parser.add_argument("--slice-rows", type=int, default=DEFAULT_SLICE_ROWS,
                        help="每个输出文件最多多少条记录（默认 %d）"
                             % DEFAULT_SLICE_ROWS)
    parser.add_argument("--dry-run", action="store_true",
                        help="只统计不落盘")
    parser.add_argument("--no-fill-last-close", dest="fill_last_close",
                        action="store_false",
                        help="不回填 LastClose（默认回填为上一行收盘价）")
    delete_group = parser.add_mutually_exclusive_group()
    delete_group.add_argument("--delete-source", dest="delete_source",
                              action="store_true",
                              help="删除已转换的源文件（默认开启）")
    delete_group.add_argument("--no-delete-source", dest="delete_source",
                              action="store_false",
                              help="关闭删除，只新增切片（也可用 "
                                   "CONVERT_DELETE_SOURCE=false）")
    parser.add_argument("--keep-latest", action="store_true",
                        help="保留 Latest.mvsv，只删待聚合的增量文件")
    parser.set_defaults(fill_last_close=True, delete_source=None)
    return parser.parse_args(argv)


def resolve_codes(source_root, options):
    """确定要处理的证券列表"""
    if options.codes.strip():
        codes = [name.strip() for name in options.codes.split(",") if name.strip()]
    else:
        codes = sorted(path.name for path in source_root.iterdir()
                       if path.is_dir())
    if options.limit and options.limit > 0:
        codes = codes[:options.limit]
    return codes


def main(argv=None):
    """主流程：遍历老目录 → 逐证券切片转换 → 汇总"""
    options = parse_args(argv)
    if options.slice_rows <= 0:
        log("❌ --slice-rows 必须为正整数")
        return 1
    # 参数优先级：命令行 > 环境变量 > 默认值（删除开关默认为开）
    if options.delete_source is None:
        env_delete = env_flag_optional("CONVERT_DELETE_SOURCE")
        options.delete_source = DEFAULT_DELETE_SOURCE if env_delete is None \
            else env_delete
    if env_flag("CONVERT_KEEP_LATEST"):
        options.keep_latest = True
    if options.dry_run and options.delete_source:
        log("🧪 演练模式：删除开关虽为开启，也只打印「将删除」，不会真的删文件")
    if options.keep_latest and not options.delete_source:
        log("⚠️ --keep-latest 只在删除开关开启时生效，本次忽略")

    root = Path(options.root).resolve()
    source_root = root / SOURCE_SUBDIR
    target_root = root / TARGET_SUBDIR

    log("======== 老行情切片转换开始（%s）========" % TASK_NAME)
    log("仓库根 = %s" % root)
    log("源目录 = %s" % source_root)
    log("目标目录 = %s" % target_root)
    log("切片上限 = %d 行/文件，回填前收价 = %s，演练模式 = %s"
        % (options.slice_rows, "是" if options.fill_last_close else "否",
           "是" if options.dry_run else "否"))
    log("删除源文件 = %s%s"
        % ("是（默认）" if options.delete_source
           else "否（--no-delete-source / CONVERT_DELETE_SOURCE=false）",
           "（保留 Latest.mvsv）"
           if options.delete_source and options.keep_latest else ""))

    if not source_root.is_dir():
        log("❌ 源目录不存在：%s" % source_root)
        return 1
    if not target_root.is_dir() and not options.dry_run:
        log("❌ 目标目录不存在：%s（请先检出/创建新链路目录）" % target_root)
        return 1

    codes = resolve_codes(source_root, options)
    if not codes:
        log("没有待处理的证券")
        return 0
    log("待处理证券 %d 个：%s" % (len(codes), "、".join(codes)))

    totals = {"sources": 0, "slices": 0, "written": 0, "skipped_slices": 0,
              "added_rows": 0, "dropped_rows": 0, "bad_rows": 0,
              "filled_last_close": 0, "deleted_sources": 0}
    failed = []
    kept = []
    for code in codes:
        log("---- %s ----" % code)
        try:
            stats = convert_code(code, options, source_root, target_root)
        except ConvertError as e:
            log("  ❌ %s 转换失败：%s" % (code, e))
            failed.append("%s：%s" % (code, e))
            continue
        for key in totals:
            totals[key] += stats.get(key, 0)
        for source, reason in stats.get("kept_sources", []):
            kept.append("%s：%s" % (source, reason))

    log("======== 老行情切片转换结束（%s）========" % TASK_NAME)
    log("合计：证券 %d 个，源文件 %d 个，切片 %d 个（新写 %d / 已存在跳过 %d），"
        "新增数据行 %d，坏行 %d，缺日期时间 %d，回填前收价 %d 行"
        % (len(codes), totals["sources"], totals["slices"], totals["written"],
           totals["skipped_slices"], totals["added_rows"], totals["bad_rows"],
           totals["dropped_rows"], totals["filled_last_close"]))
    if options.dry_run:
        log("🧪 演练模式：本次没有写入任何文件")
    if options.delete_source:
        log("删除源文件 %d 个%s%s"
            % (totals["deleted_sources"],
               "（演练，未真正删除）" if options.dry_run else "",
               "；因数据不完整/删除失败保留 %d 个" % len(kept) if kept else ""))
    else:
        log("ℹ️ 已关闭删除开关（--no-delete-source / CONVERT_DELETE_SOURCE=false），"
            "源文件一个都没有删除")
    if kept:
        log("保留的源文件清单（数据不完整或删除失败，需人工核对后重跑）：")
        for item in kept:
            log("    - %s" % item)
    if failed:
        log("失败清单（修复后重跑即可，重复运行不会写入重复切片）：")
        for item in failed:
            log("    - %s" % item)
    if failed or kept:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
