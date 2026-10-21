# Task18 老行情切片转换（SecuQuote → SecurityQuoteV5/FTMM/Min）

| 项目 | 内容 |
|---|---|
| 脚本 | `.github/Python/Quote/Task18SecuQuoteToV5Convert.py` |
| 自测 | `.github/Python/tests/secuQuoteToV5ConvertSelfTest.py`（58 项断言） |
| 下游 | `Task11AggregateLatest.py`（把切片并入各证券 `Latest.mvsv`） |
| 依赖 | Python 3.8+，仅标准库，不需要网络 |
| 数据流 | `Data/Finv/SecuQuote/<代码>/*.mvsv` → 切片 → `Data/Finv/SecurityQuoteV5/FTMM/Min/<代码>/<代码>_Min_<yyyyMMdd>_<HHmmss>.mvsv` |

---

## 一、功能设计

### 1.1 背景与目标

老链路目录 `Data/Finv/SecuQuote/<代码>/` 里还留有历史行情（聚合结果 `Latest.mvsv`
与待聚合的原始文件），新链路（FTMM V5）改为按证券目录存放「12 列原生格式 + 每个文件
一次采集」的原始文件，由 Task11 增量聚合到 `Latest.mvsv`。

本脚本把老目录里的存量数据**转换成新链路格式并按 1000 行切片**，写入新链路对应证券
目录，等待下一次聚合直接消费。目标是：

1. 老数据不丢、不改语义，只做「格式迁移 + 切分」；
2. 转换结果能被 `Task11AggregateLatest` 无差别接受（12 列、文件头齐全）；
3. 反复演练安全：**默认开启删除但只删「转换完整成功」的文件**，且可用
   `--no-delete-source` 一键关闭；重复运行不产生重复切片；
4. 转换过程中的任何不确定因素都显式报错，不猜测元信息。

### 1.2 目录与命名约定

| 角色 | 路径 / 命名 |
|---|---|
| 源目录 | `Data/Finv/SecuQuote/<代码>/*.mvsv`（含 `Latest.mvsv` 与 `<代码>_Min_<yyyyMMdd>_<HHmmss>.mvsv`） |
| 目标目录 | `Data/Finv/SecurityQuoteV5/FTMM/Min/<代码>/`（与源目录**同代码**） |
| 输出文件名 | `<代码>_Min_<yyyyMMdd>_<HHmmss>.mvsv`，例：`TSM_Min_20261021_154205.mvsv` |
| 切片上限 | 每个输出文件 ≤ `--slice-rows` 行（默认 **1000**） |

输出文件名里的 `yyyyMMdd_HHmmss` **不是运行时刻**，而是「这批数据的采集时刻」：

1. 源文件名自带标签（`<代码>_Min_20261021_155645.mvsv`）→ 用 `20261021_155645`；
2. 源文件是 `Latest.mvsv` → 用文件头 `FetchTime` / `采集时间`；
3. 都没有 → 用源文件末行 `ts`（UTC+8）；
4. 仍没有 → 用当前北京时间（并在日志里标注）。

同一来源文件的多个切片依次 **+1 秒**（`155645`、`155646`、…），
与目标目录已有文件名冲突时继续顺延，**绝不覆盖已有文件**。

> 用来源时刻而非运行时刻，是为了让 Task11 的「按文件名升序合并、后写覆盖先写」
> 语义仍然按真实时间线工作：老数据排在它原本的采集位置上，新链路后采集的数据
> 依然会覆盖老数据。

### 1.3 格式差异与转换映射

新链路（输出）固定 12 列：

```
ts | d | t | o | h | l | c | v | t | cp | cr | lc
Timestamp|Date|Time|Open|High|Low|Close|Volume|Turnover|ChangePrice|ChangeRatio|LastClose
```

老链路有**两种**源格式，脚本按文件头字段名自动识别（不按列数硬编码）：

| 来源 | 列数 | 字段头（`FieldName`） | 差异处理 |
|---|---|---|---|
| `<代码>/Latest.mvsv`（聚合结果） | 11 | `Ts|Date|Time|Open|Close|Low|High|Volume|Turnover|ChangePrice|ChangeRatio` | 列序改为 `Open,High,Low,Close`；Date/Time 沿用原值 |
| `<代码>/<代码>_Min_*.mvsv`（待聚合原始文件） | 6 | `Ts|Close|Volume|Turnover|ChangePct|ChangePrice` | `Date/Time` 由 `ts`（UTC 秒级）+8 小时推导；`Open/High/Low` 留空；`ChangePct → change_ratio`、`ChangePrice → change_price` |

字段对照（`SOURCE_FIELD_MAP`，英文全名与中文名都支持）：

| 源列名 | 中间键 | 目标列 |
|---|---|---|
| `Ts` / `Timestamp` / `时间戳` / `时间戳(UTC)` | `ts` | `ts` |
| `Date` / `日期` | `date` | `date` |
| `Time` / `时间` | `time` | `time` |
| `Open` / `开盘价` | `open` | `open` |
| `High` / `最高价` | `high` | `high` |
| `Low` / `最低价` | `low` | `low` |
| `Close` / `收盘价` | `close` | `close` |
| `Volume` / `成交量` | `volume` | `volume` |
| `Turnover` / `成交额` | `turnover` | `turnover` |
| `ChangePrice` / `涨跌值` | `change_price` | `change_price` |
| `ChangeRatio` / `ChangePercent` / `ChangePct` / `涨跌幅` / `涨跌幅(%)` | `change_ratio` | `change_ratio` |
| `LastClose` / `前收价` | `last_close` | `last_close`（老数据没有，见下） |

**LastClose（前收价）回填规则**：新链路该列的语义是「上一分钟（上一行）的收盘价」。
对仓库内 60 个新链路 `Latest.mvsv` 做过全量校验：`lc[i] == close[i-1]` 精确成立
（28.5 万行 0 例外，仅每个文件首行为空）。因此脚本按同一规则回填：

- 同一次解析的**源文件内**：首行 `LastClose` 留空，其余行 = 上一行的 `Close`；
- 上一行 `Close` 为空时沿用更早的非空收盘价；
- 用 `--no-fill-last-close` 可整体关闭回填（关闭后该列全空）。

**不做的事**：不做数值换算（不复权、不换单位、不改精度），源值原样搬过去。

### 1.4 文件头（元信息）策略

新链路文件头是 16 个键，顺序与 `Task11AggregateLatest.META_ORDER` 完全一致：

```
标题 字段名称 DataProvider Field FieldName FieldType TypeKLine Count
Symbol SecuCode USC Name Region Market TimeZone Currency
```

老链路没有 `Region/Market/TimeZone/Currency/Name/Symbol` 这些信息（只有粗粒度的
`市场 : cny`），所以**模板一律取自新链路里同一证券目录下已有的 `.mvsv` 文件**
（优先 `Latest.mvsv`，其次目录内任意 `*_Min_*.mvsv`）：

| 键 | 处理方式 |
|---|---|
| `字段名称` `DataProvider` `Field` `FieldName` `FieldType` `TypeKLine` `Name` `Region` `Market` `TimeZone` `Currency` | 原样沿用模板 |
| `标题` | 沿用模板（其中的旧代码被替换为目标代码）；模板无标题时生成「`<代码> 分钟级行情数据`」 |
| `Symbol` | 形如 `SZ.000001` / `HK.01810` / `US.TSM`，只替换最后一段代码 |
| `SecuCode` `USC` | 改写为目标证券代码（即目录名） |
| `Count` | 按本切片**实际行数**重算 |
| `FetchTime` `采集时间` `Remark` `备注` | **丢弃**（易变元信息，Task11 聚合时也会剔除） |

模板缺失（目标目录不存在，或目录下没有任何可解析的 `.mvsv`）时，该证券**直接失败**
并提示先放置模板文件——不会凭猜测拼 `Region/Market/TimeZone/Currency/Name`。

### 1.5 幂等与可重复演练

- 每个切片的内容（文件头 + 数据行）先渲染成字节串，再算 `sha256`；
- 与目标目录里已有 `<代码>_Min_*.mvsv` 的内容指纹比对（比对前统一 `CRLF → LF`），
  指纹相同即视为「已转换过」，**跳过不重写**；
- 指纹不同才落盘，文件名冲突时顺延 1 秒，不覆盖已有文件；
- 因此同一个源目录可以反复转换、反复演练，不会堆积重复数据。

### 1.6 安全边界

| 边界 | 行为 |
|---|---|
| 删除源文件 | **默认开启**（`DEFAULT_DELETE_SOURCE = True`）；只删除「全部切片已落盘/已存在且无坏行」的源文件，可用 `--no-delete-source` 关闭（见 1.8） |
| 修改老目录 | 仅删除已转换的源文件（本脚本不修改文件内容） |
| 修改新链路 `Latest.mvsv` | 不做（只写新的 `<代码>_Min_*.mvsv` 文件） |
| 猜测元信息 | 不做（缺模板即失败） |
| 数值修正/换算 | 不做（原样搬运） |
| 覆盖已有切片 | 不做（同名顺延，内容相同跳过） |
| 网络访问 | 不做（纯本地文件操作） |

### 1.7 命令行参数与退出码

| 参数 | 默认 | 说明 |
|---|---|---|
| `--root PATH` | 脚本位置推导的仓库根（`parents[2]`） | 便于在临时目录做自测 |
| `--codes A,B` | 空（处理全部） | 只处理指定证券 |
| `--limit N` | `0`（不限制） | 最多处理 N 个证券，演练用 |
| `--slice-rows N` | `1000` | 每个输出文件最多行数（必须为正整数） |
| `--dry-run` | 关 | 只统计不落盘 |
| `--no-fill-last-close` | 关（即默认回填） | 关闭 LastClose 回填 |
| `--delete-source` | **开**（默认值） | 删除已转换的源文件（写上只是显式声明）；环境变量 `CONVERT_DELETE_SOURCE` 可覆盖 |
| `--no-delete-source` | — | 关闭删除，只新增切片；也可用 `CONVERT_DELETE_SOURCE=false` |
| `--keep-latest` | 关 | 保留 `Latest.mvsv`，只删待聚合的增量文件；也可用 `CONVERT_KEEP_LATEST=true` |

| 退出码 | 含义 |
|---|---|
| `0` | 全部证券转换成功（含「内容已存在而跳过」）；源目录没有待处理证券也算成功 |
| `1` | 致命错误（源目录不存在、非演练模式下目标根目录不存在、`--slice-rows <= 0`）或存在转换失败的证券（模板缺失、字段头不可识别等）；或因数据不完整 / 删除失败而保留源文件（默认开启删除时） |

### 1.8 删除源文件开关（默认开启）

`DEFAULT_DELETE_SOURCE = True`：**不传任何开关也会删除已转换的源文件**。
参数优先级：命令行 > 环境变量（`CONVERT_DELETE_SOURCE`）> 默认值。

```bash
# 默认行为：转换成功的源文件会被删除
python3 .github/Python/Quote/Task18SecuQuoteToV5Convert.py

# 只想镜像/演练：关闭删除
python3 .github/Python/Quote/Task18SecuQuoteToV5Convert.py --no-delete-source
CONVERT_DELETE_SOURCE=false python3 .github/Python/Quote/Task18SecuQuoteToV5Convert.py

# 留一份 Latest.mvsv 作回滚快（也允许显式写 --delete-source）
python3 .github/Python/Quote/Task18SecuQuoteToV5Convert.py --delete-source --keep-latest
```

删除是**逐源文件、事后**判定，不是“边转边删”：

| 条件 | 说明 |
|---|---|
| 该文件的所有切片均已落盘或被指纹判定为「目标已有同内容」 | 写入抛异常时不会走到删除，下次重跑可继续 |
| 该文件没有坏行（列数不符 / 缺 ts） | 有坏行说明数据没搬完，保留并列入清单 |
| 该文件没有 `ts` 非法行（`dropped`） | 同上，保留 |
| `--keep-latest` 未开启，或该文件不是 `Latest.mvsv` | 需要留一份历史快时可加 `--keep-latest` |
| 非 `--dry-run` | 演练模式只打印「将删除」，不动文件 |

其它行为：

- 删除使用 `os.remove()`，只删老目录下本次处理过的 `.mvsv`，不动其它文件；
- **日志打印源文件的完整路径**（绝对路径），便于直接定位/复制排障，例如：

  ```text
  🗑️ 删除源文件：/path/to/Distribution/Data/Finv/SecuQuote/01810/Latest.mvsv
  ⏸️ 保留源文件 /path/to/Distribution/Data/Finv/SecuQuote/000985/000985_Min_20261021_155645.mvsv：1 行坏行（列数不符/缺 ts）（数据不完整，不删除）
  ⚠️ 删除源文件失败 /path/to/.../Latest.mvsv：[Errno 13] Permission denied（已保留）
  ```

- 删除失败（权限/占用）不会中断脚本：该文件转为「保留」并列入清单，退出码 `1`；
- 删除是**文件系统操作**，git 提交仍需要你自己做（`git add -A` 或 `git rm`）；
- 开删除后，如果某证券的老目录变空，git 里对应目录会自然消失（空目录不入库）；
- 开启删除前后都建议先 `--dry-run` 看一眼「将删除」清单；
- 参数优先级：命令行（`--delete-source` / `--no-delete-source`）>
  环境变量 `CONVERT_DELETE_SOURCE` > 默认值 `True`。

---

## 二、代码实现逻辑详解

### 2.1 模块结构

脚本自包含（不 import 同目录模块），自上而下分四层：

```
常量与配置
  ├─ 目录/切片/列定义：SOURCE_SUBDIR, TARGET_SUBDIR, DEFAULT_SLICE_ROWS,
  │                    LATEST_NAME, TARGET_COLUMNS(12)
  ├─ 头部定义：META_ORDER(16), VOLATILE_META_KEYS, FIELD_HEADER_KEYS
  ├─ 字段映射：SOURCE_FIELD_MAP（英文全名 + 中文名）
  └─ 时间相关：BJT_OFFSET, SOURCE_TAG_RE, FETCH_TIME_KEYS/FORMATS

基础工具
  ├─ log / unquote / now_bjt
  ├─ ts_to_bjt / parse_moment / source_moment
  └─ fmt_meta

解析与转换
  ├─ parse_source_mvsv       老文件 → (meta, columns, records, bad)
  ├─ fill_last_close         补 LastClose
  ├─ to_target_row           中间记录 → 12 列行
  └─ slice_rows              按行切片

头部与落盘
  ├─ read_template / load_template
  ├─ replace_code_in_text / replace_code_in_symbol / build_meta
  ├─ render_mvsv             渲染字节串（LF）
  ├─ content_digest / existing_digests
  ├─ next_slice_name / write_atomic
  └─ convert_code            单证券主流程

入口
  ├─ parse_args / resolve_codes
  └─ main
```

### 2.2 主流程

```
main()
 ├─ 解析参数，校验 --slice-rows > 0
 ├─ 计算 source_root = <root>/Data/Finv/SecuQuote
 │             target_root = <root>/Data/Finv/SecurityQuoteV5/FTMM/Min
 ├─ 校验：源目录必须存在；非 dry-run 时目标根目录必须存在
 ├─ resolve_codes()：源目录下的子目录名（排序）；--codes 优先，--limit 截断
 └─ for code in codes:
       convert_code(code)
         ├─ files = 源目录下全部 *.mvsv
         │          排序键 (name != "Latest.mvsv", name)：Latest 先、增量后
         ├─ load_template(目标 <code> 目录)：取头部模板（缺失 → ConvertError）
         ├─ digests = existing_digests(...)     # 目标目录已有切片的内容指纹
         ├─ used_names = 目标目录已有文件名集合
         └─ for path in files:
               parse_source_mvsv(path)         # → records / bad
               fill_last_close(records)        # → rows 的 last_close
               to_target_row(record)           # → 12 列（Date/Time 缺失时由 ts 推导）
               source_moment(path, meta, last_ts)   # 命名时间基准
               for rows_part in slice_rows(rows, slice_rows):
                   data   = render_mvsv(build_meta(template, code, len(rows_part)), rows_part)
                   digest = content_digest(data)
                   digest 已存在 → 跳过（计 skipped_slices）
                   否则 → next_slice_name() + write_atomic()
               decide_delete_source(path, bad, dropped)   # 仅开启删除时生效
 └─ 打印汇总；有失败证券或有保留源文件 → 退出码 1
```

### 2.3 解析层：`parse_source_mvsv(path)`

- 读文件：`utf-8`，先把 `\r\n` 归一为 `\n`（兼容两种换行）；
- 逐行处理：
  - 空行：跳过；
  - `#` 开头：按**第一个** `:` 切成 `key: value`，`value` 去掉外层成对双引号；
    键值用 `setdefault` 保留**第一次**出现的值；
  - 遇到 `FieldName` / `字段名称` 时记录列名数组 `columns`（后出现的覆盖先出现的，
    因为老文件里 `字段名称`（中文）先、`FieldName`（英文全名）后，最终用英文全名）；
  - 其他行：按 `|` 切分，列数必须等于 `columns` 长度，否则记为坏行
    `(行号, 原因)`；每列按 `SOURCE_FIELD_MAP` 映射为中间键，值只做 `strip()`，
    **保留原始字符串**（不转 float，避免精度损失）；
  - 没有 `ts` 的行记为坏行；
- 收尾校验：没有字段头 → `ConvertError`；字段头里没有任何一个可识别列名 →
  `ConvertError`（避免「每行都缺 ts」这种二次伤害）；
- 返回 `(meta, columns, records, bad)`。

坏行的处理策略是**容错**：某一行列数不符或没 `ts` 只跳过该行，并在日志里给出
行号与原因（最多打印 3 条），不影响同文件其它行。

### 2.4 转换层

**`fill_last_close(records, enabled)`**

```text
prev = None
for r in records:
    r["last_close"] = prev if (enabled and prev is not None) else ""
    if r["close"] != "": prev = r["close"]
```

纯函数（不修改入参），返回 `(新记录列表, 回填行数)`。回填行数进入汇总统计。

**`to_target_row(record)`**

按 `TARGET_COLUMNS` 固定顺序输出 12 个字符串；`date`/`time` 任一为空时调用
`ts_to_bjt(ts)` 推导：

```python
datetime.fromtimestamp(int(ts), timezone.utc) + timedelta(hours=8)
→ (yyyyMMdd, HHmmss)
```

（中国无夏令时，固定 +8 小时；`int(float(ts))` 兼容带小数的历史值。）

**`slice_rows(rows, size)`**

```python
[rows[i:i+size] for i in range(0, len(rows), size)]
```

按**源文件**切片，不跨源文件合并：每个源文件独立切片，因此 `Latest.mvsv` 与
增量文件各自的片数与行数都能对上，便于核对。

### 2.5 命名与时间基准

**`source_moment(path, meta, last_ts)`** 依次尝试：

1. 文件名正则 `_(\d{8})_(\d{6})\.mvsv$` → 解析为 datetime（标签来自采集作业）；
2. 头部 `FetchTime` / `采集时间`，支持 `%Y-%m-%d %H:%M:%S`、`%Y/%m/%d %H:%M:%S`；
3. 末行 `ts` 转北京时间；
4. 当前北京时间。

返回 `(datetime, 说明)`，说明会打进日志（`🕒 切片文件名时间基准：…（头部 FetchTime）`），
便于事后核对命名依据。

**`next_slice_name(code, start, used)`**

从 `start` 开始按秒递增，直到找到不在 `used` 里的名字；`used` 初始包含目标目录
所有已有 `.mvsv` 文件名（含模板与历史切片）。

### 2.6 头部生成

**`load_template(code_dir)`**

按顺序尝试 `Latest.mvsv` → 目录内其它 `*.mvsv`（文件名升序）；对每个候选调用
`parse_source_mvsv` 取 `meta`，剔除易变键后作为模板。全部候选都不可用时返回
`(None, None)`，由调用方报错。

**`build_meta(template, code, count)`**

```python
meta = dict(template)
old_code = meta["SecuCode"] or meta["USC"]
meta["标题"]     = replace_code_in_text(meta["标题"], old_code, code) or "<code> 分钟级行情数据"
meta["Symbol"]   = replace_code_in_symbol(meta["Symbol"], old_code, code)   # 只替换最后一段
meta["SecuCode"] = code
meta["USC"]      = code
meta["Count"]    = str(count)
if old_code != code: meta.pop("Name", None)     # 借模板时名称不能照抄（保护性逻辑）
```

**`render_mvsv(meta, rows)`**

```python
ordered = [k for k in META_ORDER if k in meta]      # 16 键优先
extras  = sorted(k for k in meta if k not in META_ORDER)
lines   = [fmt_meta(k, meta[k]) for k in ordered + extras]
lines.append("")                                    # 头与数据之间的空行
lines.extend("|".join(row) for row in rows)
return ("\n".join(lines) + "\n").encode("utf-8")    # 恒为 LF
```

`fmt_meta` 规则与已有文件一致：值为空或含 `:`、`#`、`|` 时用双引号包裹。

### 2.7 幂等与落盘

**`content_digest(data)`**：`sha256(data.replace(b"\r\n", b"\n"))`。
把 CRLF 归一后再算，避免「文件被 git 以 CRLF 检出」导致指纹变化。

**`existing_digests(code_dir, code)`**：扫描目标目录 `glob("<代码>_Min_*.mvsv")`，
建立 `指纹 → 文件名` 映射（读不出的文件跳过）。

**`write_atomic(path, data)`**：`tempfile.mkstemp` 写临时文件 → `os.replace`
原子替换；异常时删除临时文件。因此不会出现半个文件被下游读到。

### 2.8 统计与汇总

`convert_code` 返回单证券统计：

| 字段 | 含义 |
|---|---|
| `sources` | 源文件数 |
| `slices` | 切片总数（含跳过的） |
| `written` | 实际新写文件数 |
| `skipped_slices` | 内容已存在而跳过的切片数 |
| `added_rows` | 新写切片的数据行合计 |
| `dropped_rows` | 无法确定 date/time（ts 非法）的行数 |
| `bad_rows` | 列数不符 / 缺少 ts 的坏行数 |
| `filled_last_close` | 回填 LastClose 的行数 |
| `deleted_sources` | 本次删除的源文件数（演练时 = 将删除数） |
| `kept_sources` | 因数据不完整 / 删除失败 而保留的源文件清单（元素为完整路径 + 原因） |
| `template` | 使用的模板文件 |

`main` 汇总打印：

```
合计：证券 N 个，源文件 N 个，切片 N 个（新写 N / 已存在跳过 N），
      新增数据行 N，坏行 N，缺日期时间 N，回填前收价 N 行
```

### 2.9 关键不变量（自测逐一钉住）

1. 每行严格 12 列；
2. 文件头键顺序 = `META_ORDER`，且不含易变键；
3. `Count` == 该文件实际数据行数；
4. 每片行数 ≤ `slice_rows`，且 ≥ 1；
5. 换行恒为 LF；落盘为原子替换；
6. 11 列来源的 `Open,Close,Low,High` → `Open,High,Low,Close` 位置正确；
7. 6 列来源的 `date/time` 由 `ts` 推导（UTC+8），`Open/High/Low` 为空；
8. `LastClose`：首行空、其余 = 上一行 `Close`（关闭回填时整列为空）；
9. 输出文件名满足 `^<代码>_Min_\d{8}_\d{6}\.mvsv$`，时间基准来自来源；
10. 重复运行不新增文件；`--dry-run` 不落盘；模板缺失不写任何文件；
11. 输出能被 `Task11AggregateLatest.parseMvsv / mergeRows / renderMvsv` 直接消费。

---

## 三、使用说明及示例

### 3.1 环境要求

- Python 3.8+（仅标准库：`argparse`、`datetime`、`hashlib`、`os`、`re`、`sys`、`tempfile`、`pathlib`）；
- 无需网络与凭据；
- 在仓库根目录执行（脚本默认按自身位置推导仓库根，也可用 `--root` 指定）。

### 3.2 常用命令

```bash
# 1) 全量演练（不写任何文件，只看统计）
python3 .github/Python/Quote/Task18SecuQuoteToV5Convert.py --dry-run

# 2) 试点两个证券（默认会删除已转换的源文件）
python3 .github/Python/Quote/Task18SecuQuoteToV5Convert.py --codes 01810,000985

# 3) 只处理前 3 个证券（演练阶段逐步放量）
python3 .github/Python/Quote/Task18SecuQuoteToV5Convert.py --limit 3

# 4) 调整切片大小（例如每片 500 行）
python3 .github/Python/Quote/Task18SecuQuoteToV5Convert.py --slice-rows 500

# 5) 不回填前收价（整列留空）
python3 .github/Python/Quote/Task18SecuQuoteToV5Convert.py --no-fill-last-close

# 6) 全量转换（默认：转换成功后删除已转换的源文件；可重复运行）
python3 .github/Python/Quote/Task18SecuQuoteToV5Convert.py

# 7) 先演练「删除」动作（只打印将删除的清单，不真删）
python3 .github/Python/Quote/Task18SecuQuoteToV5Convert.py --dry-run

# 8) 保留 Latest.mvsv 作回滚快（只删增量文件）
python3 .github/Python/Quote/Task18SecuQuoteToV5Convert.py --keep-latest

# 9) 关闭删除，只做镜像（不删任何源文件）
python3 .github/Python/Quote/Task18SecuQuoteToV5Convert.py --no-delete-source
CONVERT_DELETE_SOURCE=false python3 .github/Python/Quote/Task18SecuQuoteToV5Convert.py
```

典型日志：

```
======== 老行情切片转换开始（Task18SecuQuoteToV5Convert）========
仓库根 = /path/to/Distribution
源目录 = .../Data/Finv/SecuQuote
目标目录 = .../Data/Finv/SecurityQuoteV5/FTMM/Min
切片上限 = 1000 行/文件，回填前收价 = 是，演练模式 = 否
待处理证券 28 个：000001、000510、…
---- 01810 ----
  🧩 文件头模板：Latest.mvsv
  🕒 切片文件名时间基准：2026-10-21 15:01:45（头部 FetchTime）
  ✅ 写入 01810_Min_20261021_150145.mvsv（1000 行）
  ✅ 写入 01810_Min_20261021_150146.mvsv（1000 行）
  …
  📄 Latest.mvsv：字段头=Ts|Date|Time|Open|Close|Low|High|Volume|Turnover|ChangePrice|ChangePercent，数据行=11842，回填前收价=11841
```

### 3.3 输出样例

`Data/Finv/SecurityQuoteV5/FTMM/Min/01810/01810_Min_20261021_150145.mvsv`：

```
# 标题 : 01810 分钟级行情数据
# 字段名称 : "时间戳|日期|时间|开盘价|最高价|最低价|收盘价|成交量|成交额|涨跌值|涨跌幅|前收价"
# DataProvider : FTMM
# Field : "ts|d|t|o|h|l|c|v|t|cp|cr|lc"
# FieldName : "Timestamp|Date|Time|Open|High|Low|Close|Volume|Turnover|ChangePrice|ChangeRatio|LastClose"
# FieldType : "Int|Int|Int|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal"
# TypeKLine : Min
# Count : 1000
# Symbol : HK.01810
# SecuCode : 01810
# USC : 01810
# Name : 小米集团-W
# Region : HK
# Market : HK
# TimeZone : Asia/Hong_Kong
# Currency : HKD

1788139800|20260831|093000|27.82|||27.88|790000|22025200|0.06|0.22|
1788139860|20260831|093100|27.88|||27.86|1777600|49445678|0.04|0.14|27.88
1788139920|20260831|093200|27.86|||27.62|1283400|35636610.8|-0.2|-0.72|27.86
```

来自 6 列待聚合文件的切片（`Date/Time` 由 ts 推导、`Open/High/Low` 空）：

```
1792027800|20261015|093000||||17.3|64900|1122770|-0.33|-1.87|
1792027860|20261015|093100||||17.39|118100|2055529|-0.24|-1.36|17.3
```

### 3.4 与下游聚合衔接

转换得到的 `<代码>_Min_*.mvsv` 就是 Task11 的「原始文件」：

| Task11 行为 | 与本脚本的关系 |
|---|---|
| `scanSourceFiles`：目录下所有 `.mvsv`（除 `Latest.mvsv`）按文件名升序 | 我们的切片自动入选；文件名时间戳保证时间线顺序 |
| `mergeRows`：只接受 12 列，按 `ts` 去重，后读覆盖先读 | 我们的输出满足 12 列；重叠 ts 由「更晚采集」的一方胜出 |
| `renderMvsv`：按 `META_ORDER` 重排、`Count` 重算、剔除易变键 | 与我们的头部策略一致 |
| `AGG_CLEANUP_RAW`（默认关闭） | 切片文件会保留，不会被聚合顺手删掉 |

### 3.5 自测

```bash
python3 .github/Python/tests/secuQuoteToV5ConvertSelfTest.py
# → 全部通过（58 项断言）
```

自测覆盖：11 列/6 列两种来源转换、列序与 `ts` 推导、`LastClose` 回填与开关、
头 16 键顺序与模板沿用、切片与命名（来源时间基准、顺延）、幂等重跑、`--dry-run`、
模板缺失、坏行容错、`--codes/--limit`，以及「输出能被 Task11 解析/合并/渲染」。

### 3.6 实测参考（2026-10-21 本地演练）

| 指标 | 数值 |
|---|---|
| 证券数 | 28 |
| 源文件数 | 31（28 × `Latest.mvsv` + 3 个 6 列待聚合文件） |
| 切片数（1000 行/片） | 484 |
| 数据行数 | 469,335 |
| 坏行 / 缺日期时间 | 0 / 0 |
| 回填 LastClose 行数 | 469,304（= 总行数 − 31，每个源文件首行不回填） |

新旧链路重叠 `ts` 的一致性核对：24.9 万重叠行中 93.86% 的
`close/volume/turnover` 完全一致；差异集中在 `XAUUSD`（如 4378.5 vs 4378.505）
等，属于数据源精度/口径差异（老链路 FT 常见 1 位小数，新链路 FTMM 3 位小数），
并非转换引入。

---

## 四、注意事项

### 4.1 数据安全

1. **默认会删除源文件**（删除开关默认开启）：不加任何参数时，脚本会在
   「该源文件的全部切片已落盘且无坏行/无 ts 非法行」后删除它，属于“转完一份删一份”。
   删除不可恢复，建议先 `--dry-run`（看将删除清单）→ `--codes ...`（小范围试点）→ 全量；
   若想保留回滚快，加 `--keep-latest` 只删增量文件；若只想镜像不删，
   用 `--no-delete-source` 或 `CONVERT_DELETE_SOURCE=false`。
   删除/保留日志都会打印源文件的**完整路径**，可直接拿去排障或核对 diff。
2. **保留清单与退出码**：开启删除后若有文件因数据不完整/删除失败被保留，脚本会
   列出清单并返回 `1`；这些文件不会拖累已完成的转换，修好源数据后重跑即可补转。
2. **不改新链路 `Latest.mvsv`**：只写 `<代码>_Min_*.mvsv`。真正的合并由 Task11
   完成，便于「转换」和「聚合」两步分开校验、分开回滚。
3. **不覆盖已有文件**：内容相同跳过、文件名冲突顺延 1 秒；不会破坏别人刚采集的
   原始文件。
4. 演练先用 `--dry-run` 看统计，再 `--codes`/`--limit` 小范围试点，最后全量。
5. 全量转换会产生数百个新文件（实测 484 个），提交前注意 `git status` 与仓库体积；
   中途放弃可用 `git clean -n` / `git clean -f <目录>` 清理（先 `-n` 预览）。
6. **删除后的 git 操作**：脚本不做 `git add/rm`。开删除后需要自己提交：
   `git add -A Data/Finv/SecuQuote Data/Finv/SecurityQuoteV5/FTMM/Min`，
   确认 `git status` 里老文件显示为 `deleted`、新切片为 `added`。

### 4.2 语义与兼容

6. **老数据精度较低**：重叠 `ts` 上老值可能与新链路已有值不同（FT 1 位小数 vs
   FTMM 3 位小数）。因为切片文件名时间基准取「来源采集时刻」（早于新链路现有
   文件），Task11 合并时新数据后读、覆盖老值——这是期望行为；若你希望「老值优先」，
   需另行调整命名策略并同步评估影响。
7. **每个切片首行的 `LastClose`**：源文件首行留空是既定语义（没有更早一行可参考）；
   合并后会出现极少数非首行为空的情况（来自另一个源文件的切片边界），属预期。
8. **不修正业务语义**：`ChangePrice`/`ChangeRatio` 原样搬运，不做「相对昨收/相对
   上一分钟」的换算；`Open/High/Low` 在老 6 列数据里本来就没有，一律留空。
9. **文件头模板**必须来自同代码的新链路目录。若某代码在新链路还没有目录/文件，
   脚本会失败并提示——请先放置一个该证券的 `Latest.mvsv` 或任意合法切片，
   不要试图让脚本猜 `Region/Market/TimeZone/Currency/Name`。
10. **字段头可识别性**：源文件必须有 `# FieldName`（英文全名）或 `# 字段名称`
    （中文）且列名在 `SOURCE_FIELD_MAP` 内；只有短名头（如 `ts|c|v|t|r|cp`）而无
    全名的老文件会报「字段头无法识别」（短名 `t` 同时表示 Time/Turnover，无法安全
    映射）。当前仓库里的老文件都带 `FieldName`。

### 4.3 运行环境

11. 脚本无并发保护：不要在 Task11 聚合工作流正在运行时执行转换（会同时往同一目录
    写文件，虽然文件名不冲突，但会让「本次聚合」漏掉你刚写的切片）。建议先转换、
    再触发聚合。
12. 换行与编码：输出统一 UTF-8 + LF（与 Task11 一致）；源文件 CRLF/LF 都能读。
13. 只处理**子目录**：`Data/Finv/SecuQuote/` 下的一级子目录名就是证券代码；
    散落在根目录的 `.mvsv` 不会被处理。
14. 幂等的前提是「源数据不变」：如果源文件内容变了，指纹随之变化，会写入新文件
    （旧文件保留），此时需要人工确认后清理旧切片。

### 4.4 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `❌ 目标目录不存在：.../FTMM/Min` | 非 `--dry-run` 时必须已检出/存在新链路目录 |
| `❌ <代码> 转换失败：新链路目录缺少文件头模板` | 该证券在新链路没有可用 `.mvsv`，先放一个模板文件 |
| `⚠️ ... 头部证券代码是 X，与目录 Y 不一致，按目录代码转换` | 源文件头的 `SecuCode` 与目录名不一致（不常见），脚本以目录名（目标证券）为准，仅告警 |
| `⚠️ ... 有 N 行无法确定日期/时间（ts 非法），仍按空值写出` | 源行 `ts` 无法解析为时间戳；这行不参与 `LastClose` 推断，需人工核查源数据 |
| 重复跑后 `新写 0 / 已存在跳过 N` | 正常：说明这批数据已经转换过（内容指纹一致） |
| 开启删除后提示 `⏸️ 保留源文件 ...（数据不完整，不删除）` | 该文件有坏行或 `ts` 非法行，数据没搬完；修好源数据后重跑会补转并删除 |
| 开启删除后提示 `⚠️ 删除源文件失败 ...（已保留）` | 权限/占用问题；处理后重跑（已转换的切片会被指纹跳过，只补删除动作） |
| `--keep-latest` 但没开始删除 | `--keep-latest` 只在删除开关开启时起作用（默认开启；若同时传了 `--no-delete-source` 则忽略并告警） |
| 想重新生成全部切片 | 先删除目标目录下对应 `*_Min_*.mvsv`（确认不需要后），再运行；或在转换前调整 `--slice-rows`（会导致指纹不同而生成新文件） |

---

## 附：相关文件

| 文件 | 说明 |
|---|---|
| `.github/Python/Quote/Task11AggregateLatest.py` | 新链路聚合：把切片并入 `Latest.mvsv` |
| `.github/Python/Quote/Task18SecuQuoteToV5Convert.py` | 本文档描述的转换脚本 |
| `.github/Python/tests/secuQuoteToV5ConvertSelfTest.py` | 自测（58 项断言） |
| `Data/Finv/SecuQuote/<代码>/` | 老链路源目录（转换成功后默认删除；`--no-delete-source` 时保留） |
| `Data/Finv/SecurityQuoteV5/FTMM/Min/<代码>/` | 新链路目标目录（只新增切片） |
