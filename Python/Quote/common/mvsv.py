"""
MVSV 文件读写、去重与合并

格式: 元数据区 -> 空行 -> 数据区 (| 分隔)
"""

import os as _os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


class MVSVParseError(Exception):
    pass


class MVSVMetadata:
    CN_FIELDS = [
        "标题", "数据供应商",
        "字段", "字段名称", "字段类型",
        "计数", "采集时间",
        "证券代码", "市场", "备注"
    ]
    EN_FIELDS = [
        "Title", "DataProvider", "Field", "FieldName", "FieldType",
        "Count", "FetchTime", "SecuCode", "Market", "Remark"
    ]
    STANDARD_KEYS = set(CN_FIELDS + EN_FIELDS)

    def __init__(self):
        self.values: Dict[str, str] = {}
        self.extra: Dict[str, str] = {}

    def get(self, key: str, default=None):
        return self.values.get(key, default)

    def __getitem__(self, key: str):
        return self.values.get(key)

    def __setitem__(self, key: str, value: str):
        self.values[key] = value

    @classmethod
    def from_lines(cls, lines: List[str]):
        meta = cls()
        for line in lines:
            line = line.strip()
            if not line.startswith("#"):
                continue
            rest = line[1:].strip()
            colon = rest.find(":")
            if colon < 0:
                continue
            key = rest[:colon].strip()
            val = rest[colon + 1:].strip()
            if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                val = val[1:-1]
            if key in cls.STANDARD_KEYS:
                meta.values[key] = val
            else:
                meta.extra[key] = val
        return meta

    def to_lines(self):
        result = []
        for k in self.CN_FIELDS:
            if k in self.values:
                result.append(_fmt_meta(k, self.values[k]))
        for k in self.EN_FIELDS:
            if k in self.values:
                result.append(_fmt_meta(k, self.values[k]))
        for k in sorted(self.extra.keys()):
            result.append(_fmt_meta(k, self.extra[k]))
        return result


def _fmt_meta(key: str, value: str) -> str:
    if not value or any(c in value for c in ":#|"):
        return f'# {key} : "{value}"'
    return f"# {key} : {value}"

def _ensure_datetime_cols(rows, now_bjt):
    """Normalize each row to exactly 8 columns (ts|Date|Time|c|v|t|r|cp).
    - 6 cols → insert Date/Time (BJT) after ts
    - 8 cols → unchanged
    - 11 cols → unchanged (already expanded)
    - other → reconstruct: ts + Date/Time(BJT) + last 5 cols (c,v,t,r,cp)
    Returns (new_rows, added) where added=True if any row was modified.
    """
    if not rows:
        return rows, False
    from common.timeutil import ts_to_bjt_dt
    new_rows = []
    any_added = False
    for r in rows:
        n = len(r)
        if n == 8 or n == 11:
            new_rows.append(r)
        elif n == 6:
            bjt_dt = ts_to_bjt_dt(int(r[0]))
            r_new = [
                r[0],
                bjt_dt.strftime("%Y%m%d"),
                bjt_dt.strftime("%H%M%S"),
            ] + r[1:]
            new_rows.append(r_new)
            any_added = True
        else:
            bjt_dt = ts_to_bjt_dt(int(r[0]))
            r_new = [
                r[0],
                bjt_dt.strftime("%Y%m%d"),
                bjt_dt.strftime("%H%M%S"),
            ] + r[-5:]
            new_rows.append(r_new)
            any_added = True
    return new_rows, any_added


def _expand_to_11cols(data):
    """Expand 8-col rows to 11-col: ts|Date|Time|Open|Close|Low|High|Volume|Turnover|ChangePrice|ChangePercent.
    Open:
      - First row: Close - ChangePrice (precision matched to input)
      - Subsequent rows with ts diff == 60s: prev Close
      - Otherwise: '' (gap in data)
    Low/High = '' (no upstream data yet).
    Idempotent for 11-col rows (only first-row Open is recalculated).
    """
    if not data.rows:
        return data

    def _calc_open(close_str, cp_str):
        from decimal import Decimal
        close_d = Decimal(close_str)
        cp_d = Decimal(cp_str)
        result = close_d - cp_d
        prec = max(
            -close_d.as_tuple().exponent if close_d.as_tuple().exponent < 0 else 0,
            -cp_d.as_tuple().exponent if cp_d.as_tuple().exponent < 0 else 0,
        )
        if prec:
            result = result.quantize(Decimal('0.' + '0' * prec))
        return str(result)

    new_rows = []
    prev_close = None
    prev_ts = None

    for r in data.rows:
        n = len(r)
        if n == 11:
            ts = int(r[0])
            close = r[4]
            change_price = r[9]
            if prev_close is None:
                r[3] = _calc_open(close, change_price)
            new_rows.append(r)
            prev_close = close
            prev_ts = ts
            continue
        elif n == 8:
            ts = int(r[0])
            close = r[3]
            if prev_close is not None and prev_ts is not None and (ts - prev_ts) == 60:
                open_val = prev_close
            elif prev_close is None:
                open_val = _calc_open(close, r[7])
            else:
                open_val = ""
        new_row = [
            r[0], r[1], r[2],
            open_val,          # Open
            close,             # Close
            "",                # Low
            "",                # High
            r[4],              # Volume
            r[5],              # Turnover
            r[7],              # ChangePrice
            r[6],              # ChangePercent
        ]
        new_rows.append(new_row)
        prev_close = close
        prev_ts = ts

    data.rows = new_rows
    _set_meta_11cols(data.metadata)
    return data

_11_FIELD = "ts|Date|Time|Open|Close|Low|High|Volume|Turnover|ChangePrice|ChangePercent"
_11_FIELD_CN = _11_FIELD
_11_NAME = "时间戳(UTC)|日期|时间|开盘价|收盘价|最低价|最高价|成交量|成交额|涨跌值|涨跌幅(%)"
_11_NAME_EN = "Ts|Date|Time|Open|Close|Low|High|Volume|Turnover|ChangePrice|ChangePercent"
_11_TYPE = "int|int|int|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|str"


def _set_meta_11cols(meta):
    """Set metadata to 11-column format."""
    meta["字段"] = _11_FIELD_CN
    meta["Field"] = _11_FIELD
    meta["字段名称"] = _11_NAME
    meta["FieldName"] = _11_NAME_EN
    meta["字段类型"] = _11_TYPE
    meta["FieldType"] = _11_TYPE


# 8-column format constants (intermediate, with Date/Time)
_DT_FIELD = "ts|Date|Time|c|v|t|r|cp"
_DT_FIELD_CN = "ts|Date|Time|c|v|t|r|cp"
_DT_NAME = "时间戳(UTC)|日期|时间|收盘价|成交量|成交额|涨跌幅(%)|涨跌值"
_DT_NAME_EN = "Ts|Date|Time|Close|Volume|Turnover|ChangePercent|ChangePrice"
_DT_TYPE = "int|int|int|Decimal|Decimal|Decimal|str|Decimal"


def _update_meta_with_datetime(meta):
    """Update 6-col metadata to 8-col (with Date/Time)."""
    if meta.get("字段") and "Date|Time" not in meta["字段"]:
        meta["字段"] = _DT_FIELD_CN
    if meta.get("Field") and "Date|Time" not in meta["Field"]:
        meta["Field"] = _DT_FIELD
    if meta.get("字段名称") and meta["字段名称"].count("|") + 1 == 6:
        meta["字段名称"] = _DT_NAME
    if meta.get("FieldName") and meta["FieldName"].count("|") + 1 == 6:
        meta["FieldName"] = _DT_NAME_EN
    if meta.get("字段类型") and meta["字段类型"].count("|") + 1 == 6:
        meta["字段类型"] = _DT_TYPE
    if meta.get("FieldType") and meta["FieldType"].count("|") + 1 == 6:
        meta["FieldType"] = _DT_TYPE



class MVSVData:
    def __init__(self, metadata=None, rows=None):
        self.metadata = metadata or MVSVMetadata()
        self.rows = rows or []


def _split_meta_data(text: str):
    """Split into (meta_lines, data_lines)."""
    for sep in ("\n\n", "\r\n\r\n"):
        parts = text.split(sep, 1)
        if len(parts) == 2:
            meta_l = [l for l in parts[0].split("\n") if l.strip().startswith("#")]
            data_l = [l.strip() for l in parts[1].strip().split("\n") if l.strip()]
            return meta_l, data_l
    meta_l = [l for l in text.split("\n") if l.strip().startswith("#")]
    return meta_l, []


def parse(path: str) -> MVSVData:
    p = Path(path)
    if not p.exists():
        raise MVSVParseError(f"文件不存在: {path}")
    with open(p, "r", encoding="utf-8") as f:
        raw = f.read()
    meta_lines, data_lines = _split_meta_data(raw)
    metadata = MVSVMetadata.from_lines(meta_lines)
    rows = [row.split("|") for row in data_lines]
    return MVSVData(metadata=metadata, rows=rows)


def serialize(data: MVSVData, path: str):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data.metadata["计数"] = str(len(data.rows))
    data.metadata["Count"] = str(len(data.rows))
    lines_out = data.metadata.to_lines()
    lines_out.append("")
    for r in data.rows:
        lines_out.append("|".join(r))
    body = "\n".join(lines_out)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp_", suffix=".mvsv")
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        _os.replace(tmp, str(p))
    except BaseException:
        try:
            _os.unlink(tmp)
        except OSError:
            pass
        raise


def merge_and_dedup(existing, incoming, *, now_bjt):
    existing.rows, ex_dt = _ensure_datetime_cols(existing.rows, now_bjt)
    incoming.rows, in_dt = _ensure_datetime_cols(incoming.rows, now_bjt)
    dt_added = ex_dt or in_dt
    rows_dict = {}
    for r in existing.rows:
        rows_dict[r[0]] = r
    for r in incoming.rows:
        rows_dict[r[0]] = r
    sorted_rows = sorted(rows_dict.values(), key=lambda r: int(r[0]))
    inc = incoming.metadata
    ex = existing.metadata
    md = MVSVMetadata()

    md["标题"] = inc.get("标题") or ex.get("标题") or ""
    md["Title"] = inc.get("Title") or ex.get("Title") or ""
    md["数据供应商"] = inc.get("数据供应商") or ex.get("数据供应商") or ""
    md["DataProvider"] = inc.get("DataProvider") or ex.get("DataProvider") or ""
    md["字段"] = inc.get("字段") or ex.get("字段") or ""
    md["Field"] = inc.get("Field") or ex.get("Field") or ""
    md["字段名称"] = inc.get("字段名称") or ex.get("字段名称") or ""
    md["FieldName"] = inc.get("FieldName") or ex.get("FieldName") or ""
    md["字段类型"] = inc.get("字段类型") or ex.get("字段类型") or ""
    md["FieldType"] = inc.get("FieldType") or ex.get("FieldType") or ""
    md["证券代码"] = inc.get("证券代码") or ex.get("证券代码") or ""
    md["SecuCode"] = inc.get("SecuCode") or ex.get("SecuCode") or ""

    from common.timeutil import infer_market_from_code
    m = inc.get("市场") or inc.get("Market") or ex.get("市场") or ex.get("Market") or ""
    if not m:
        code = md.get("证券代码") or md.get("SecuCode") or ""
        m = infer_market_from_code(code)
    md["市场"] = m
    md["Market"] = m

    md["备注"] = inc.get("备注") or ex.get("备注") or ""
    ren = inc.get("Remark") or ex.get("Remark")
    if ren:
        md["Remark"] = ren

    md["计数"] = str(len(sorted_rows))
    md["Count"] = str(len(sorted_rows))
    ft = now_bjt.strftime("%Y-%m-%d %H:%M:%S")
    md["采集时间"] = ft
    md["FetchTime"] = ft
    if dt_added or len(sorted_rows[0]) >= 8:
        _update_meta_with_datetime(md)
    md.extra = {**ex.extra, **inc.extra}
    return MVSVData(metadata=md, rows=sorted_rows)


def scan_source_files(code_dir: str):
    p = Path(code_dir)
    if not p.is_dir():
        return []
    items = []
    for f in p.iterdir():
        if f.is_file() and f.suffix == ".mvsv" and f.name != "Latest.mvsv":
            items.append((f.stat().st_mtime_ns, str(f)))
    items.sort(key=lambda x: x[0])
    return [f[1] for f in items]
