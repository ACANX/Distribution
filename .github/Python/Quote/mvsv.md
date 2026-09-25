# MVSV 格式规范

## 概述

MVSV 是 MetaX 定义的竖线分隔值文件格式，由元数据区 + 空行 + 数据区三部分组成。
本模块 `.github/Python/Quote/common/mvsv.py` 实现了 MVSV 的解析、序列化、合并去重、列扩展等功能。

---

## 格式版本演进

### V1 — 原始采集格式（6 列）

原始采集文件使用此格式，**只读不改**。

```
# 字段 : Ts|c|v|t|r|cp
# 字段名称 : 时间戳(UTC)|收盘价|成交量|成交额|涨跌幅(%)|涨跌值
# 字段类型 : int|Decimal|int|Decimal|str|Decimal
```

| 列 | 名称 | 类型 | 说明 |
|---|---|---|---|
| ts | 时间戳(UTC) | int | UTC 秒级整数时间戳，全局去重键 |
| c | 收盘价 | Decimal | — |
| v | 成交量 | int | — |
| t | 成交额 | Decimal | — |
| r | 涨跌幅(%) | str | 百分比字符串 |
| cp | 涨跌值 | Decimal | 收盘价相对前收盘的差值 |

**文件命名**：`{Code}_{yyyyMMdd}_{HHmmss}.mvsv` 或 `{Code}_Min_{yyyyMMdd}_{HHmmss}.mvsv`

### V2 — 聚合中间格式（8 列）

经 `merge_and_dedup` 内部使用的中间格式，添加了 Date/Time 列。

```
# 字段 : Ts|Date|Time|Close|Volume|Turnover|ChangePercent|ChangePrice
# 字段名称 : 时间戳(UTC)|日期|时间|收盘价|成交量|成交额|涨跌幅(%)|涨跌值
# 字段类型 : int|int|int|Decimal|Decimal|Decimal|str|Decimal
```

| 列 | 名称 | 类型 | 来源 |
|---|---|---|---|
| ts | 时间戳(UTC) | int | 原始 ts |
| Date | 日期 | int | ts → BJT yyyyMMdd |
| Time | 时间 | int | ts → BJT HHmmss |
| Close | 收盘价 | Decimal | 原始 c |
| Volume | 成交量 | Decimal | 原始 v |
| Turnover | 成交额 | Decimal | 原始 t |
| ChangePercent | 涨跌幅(%) | str | 原始 r |
| ChangePrice | 涨跌值 | Decimal | 原始 cp |

**注意**：V2 是内部格式，正常情况下不会出现在磁盘上。

### V3 — Latest/归档完整格式（11 列）

`Latest.mvsv` 及 `Archive/` 目录下的归档文件使用此格式。

```
# 字段 : Ts|Date|Time|Open|Close|Low|High|Volume|Turnover|ChangePrice|ChangePercent
# 字段名称 : 时间戳(UTC)|日期|时间|开盘价|收盘价|最低价|最高价|成交量|成交额|涨跌值|涨跌幅(%)
# 字段类型 : int|int|int|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|str
```

| 列 | 名称 | 类型 | 来源 |
|---|---|---|---|
| ts | 时间戳(UTC) | int | 原始 ts |
| Date | 日期 | int | ts → BJT yyyyMMdd |
| Time | 时间 | int | ts → BJT HHmmss |
| Open | 开盘价 | Decimal | 见下方说明 |
| Close | 收盘价 | Decimal | 原始 c |
| Low | 最低价 | Decimal | 暂缺省 |
| High | 最高价 | Decimal | 暂缺省 |
| Volume | 成交量 | Decimal | 原始 v |
| Turnover | 成交额 | Decimal | 原始 t |
| ChangePrice | 涨跌值 | Decimal | 原始 cp |
| ChangePercent | 涨跌幅(%) | str | 原始 r |

#### Open 计算规则

| 行 | 条件 | Open 值 |
|---|---|---|
| 第一条 | 无前一条记录 | `Close - ChangePrice`（Decimal 精确计算） |
| 后续 | `current_ts - prev_ts == 60` | `prev.Close`（前一条收盘价） |
| 后续 | `current_ts - prev_ts != 60` | 缺省（数据不连续） |

精度：Open 的小数位数取 Close 和 ChangePrice 两者最大位数，用 `Decimal.quantize` 确保精确输出。

---

## 版本升级路径

```
V1 (6列) ──merge_and_dedup──▶ V2 (8列) ──_expand_to_11cols──▶ V3 (11列)
  原始采集       去重+加Date/Time         Latest/归档
```

---

## 核心函数说明

### `parse(path) → MVSVData`

解析 mvsv 文件。

```python
from common.mvsv import parse
data = parse("Data/Finv/SecuQuote/GCMain/Latest.mvsv")
print(f"{len(data.rows)} 行, {len(data.rows[0])} 列")
print(f"字段: {data.metadata['字段']}")
```

### `render(data) → str`

把 `MVSVData` 渲染成 MVSV 文本，不落盘（`serialize` 的纯函数部分）。
会自动把 `# 计数` / `# Count` 改写成实际行数。

### `serialize(data, path, *, only_if_changed=False) → bool`

原子写 mvsv 文件（tmp + rename）。
自动更新 `# 计数` / `# Count` 为实际行数。

`only_if_changed=True` 时，磁盘内容与待写内容完全一致就**不碰文件**并返回 `False`；
返回 `True` 表示文件已写入。归档这类「可以反复重跑」的场景应带上它。

```python
from common.mvsv import serialize
if serialize(data, path, only_if_changed=True):
    gitutil.add(path)          # 只有真变了才进暂存区
```

### `strip_volatile_meta(meta, keys=None) → list[str]`

移除元数据中的易变字段（中文/英文写法都清），返回实际移除的键。
`keys` 缺省为 `VOLATILE_META_KEYS`，可传入更窄的集合（月归档只清时间戳时用）。

### `merge_and_dedup(existing, incoming, *, now_bjt) → MVSVData`

合并去重：同 ts 以 incoming 为准。

```python
from datetime import datetime
from common.timeutil import BJT
base = merge_and_dedup(base, new_file, now_bjt=datetime.now(BJT))
```

### `_expand_to_11cols(data) → MVSVData`

将 8 列数据扩展为 11 列（添加 Open/Low/High，重新排序列）。

```python
from common.mvsv import _expand_to_11cols
_expand_to_11cols(data)
serialize(data, "Latest.mvsv")
```

### `scan_source_files(code_dir) → list[str]`

扫描原始采集文件，排除 `Latest.mvsv`，按 mtime 升序返回。

```python
files = scan_source_files("Data/Finv/SecuQuote/GCMain")
# → [".../GCMain_20260612_184956.mvsv", ...]
```

---

## 元数据合并策略

`merge_and_dedup` 合并两个来源时，元数据按以下规则处理：

| 键 | 策略 |
|---|---|
| 标题 / Title | incoming 优先 |
| 数据供应商 / DataProvider | incoming 优先 |
| 字段 / Field | incoming 优先 |
| 字段名称 / FieldName | incoming 优先 |
| 字段类型 / FieldType | incoming 优先 |
| 证券代码 / SecuCode | incoming 优先 |
| 市场 / Market | incoming → existing → 代码推断 |
| 计数 / Count | 重写为实际行数 |
| 其它（含采集时间 / FetchTime、备注 / Remark） | extra：existing ∪ incoming，冲突以 incoming 为准 |

### 易变字段与归档文件头

`FetchTime` / `采集时间` / `Remark` / `备注`（`common.mvsv.VOLATILE_META_KEYS`）描述的是
**「什么时候抓的」而非「文件里是什么」**：前者随采集时刻变化，后者的汇总统计的是 Latest
全体而非本文件数据。

`Latest.mvsv` 保留它们（采集侧需要），**归档文件头必须剔除**。原因是 Latest 的保留窗口
（`LatestWindowDays`，36 天）大于归档线（`DailyArchiveAfterDays`，9 天）——每天都有一批已
归档的日文件被重新归档；头里留着这些字段，数据一行未变也会改写已归档文件，产生无意义的
提交。

| 位置 | 规则 |
|---|---|
| 日归档 `Archive/Finv/SecuQuote/Day/` | 剔除全部四个字段 |
| 月归档 `Archive/Finv/SecuQuote/<yyyy>/` | 只剔除 `FetchTime` / `采集时间`；`备注` / `Remark` 由 Task03 记「缺失交易日」，保留 |

归档写入一律走 `serialize(..., only_if_changed=True)`：内容不变就不落盘、不 `git add`，
重复归档不会改动已归档文件。存量文件用
`python3 .github/Python/Quote/FixArchiveMeta.py [--monthly]` 清理。

### 备注 / Remark 必须读写 extra 区

`备注` / `Remark` 不在 `STANDARD_KEYS` 里，`parse()` 把它们读进 **extra**，`to_lines()`
也只从 extra 输出。因此：

- 读用 `meta.extra.get('备注')`；`meta.get('备注')` 只查 values，永远拿不到值
- 写要落到 `meta.extra['备注']`；写进 `meta.values` 的不会被序列化，**静默丢失**

Task03 的「缺失交易日」备注曾栽在这里：读不到旧值、写出去也不落盘，功能整个失效 ——
文件里那些备注只是历次 parse→serialize 搬运下来的化石，看着像在工作而已。

---

## 类型说明

所有 Decimal 类型字段在 mvsv 中以字符串存储，解析后使用 `decimal.Decimal` 类型处理，避免浮点精度问题。`str` 类型用于涨跌幅（如 `"-2.71"`），因其可能有特殊格式化需求。

---

## 相关工具

### FixLatestDateCols.py

扫描并修复/升级所有文件到最新格式。

```bash
# 仅 Latest.mvsv
python3 .github/Python/Quote/FixLatestDateCols.py

# 包括归档文件
python3 .github/Python/Quote/FixLatestDateCols.py --all
```

### Task01AggregateLatest.py

合并原始文件 → _expand_to_11cols → 写 Latest.mvsv（自动触发）。
