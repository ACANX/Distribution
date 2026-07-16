# 行情 mvsv 文件聚合与归档系统 —— 实现方案与开发计划

> 对应需求详见 `prompt.txt`。
> 仓库分支：`quote`，脚本统一放置在 `Python/Quote/` 目录下。
> 触发方式：GitHub Actions（支持 `workflow_dispatch` 手动触发）。

---

## 一、需求总览与术语

| 符号 | 含义 |
| --- | --- |
| `Data/Finv/SecuQuote/{code}/` | 原始采集目录，包含最近 5 个交易日的 `*.mvsv` 采集文件 |
| `Latest.mvsv` | 每个证券目录下聚合后的 11 列滚动文件（`ts|Date|Time|Open|Close|Low|High|Volume|Turnover|ChangePrice|ChangePercent`） |
| `Archive/Finv/SecuQuote/Day/{code}/{code}_Min_yyyyMMdd.mvsv` | 按日归档，11 列格式（UTC 时间戳按北京时间切日） |
| `Archive/Finv/SecuQuote/{yyyy}/{code}/{code}_Min_yyyyMM.mvsv` | 按月归档 |
| `ts`（实测为小写，解析时大小写兼容） | 首列 UTC 秒级时间戳，全局唯一键 |
| 阈值 | `Latest 保留 14 天`（`latest_window_days`）、`日归档触发 7 天`（`daily_archive_after_days`）、`月归档延迟窗口 +2 月`（`monthly_delete_lag_months`） |
| 北京时间 | UTC+8，日切/月切规则：**`[D 00:00:00 BJT, D+1 00:00:00 BJT)` 归属北京时间日期 `D`**（0 点整归入当日；对应 UTC 区间 `[D-1 16:00:00, D 16:00:00)`） |

三个核心任务：

1. **聚合到 Latest**：14 天窗口内原始 mvsv + 现有 Latest.mvsv 合并去重（早采集先存、后采集覆盖；增量追加）。
2. **按日归档**：Latest.mvsv 中超过 7 天的数据按北京时间切日归档，已存在文件需聚合去重后再写入。
3. **按月归档**：在确认按日归档已全部完成的前提下，将上一自然月的日归档文件聚合为月归档文件；月归档文件同样遵循"不覆盖、只聚合"原则。

附带：删除操作必须满足"下一级已持久化 + 已提交 git"的安全前置条件。

---

## 二、参考文件

本方案的 `.mvsv` 文件格式规范参考以下源文件（均来自 `ACANX/MetaX` 仓库 `dev` 分支）：

| 文件 | 完整 URL | 作用 |
| --- | --- | --- |
| `mvsv_parser.py` | https://github.com/ACANX/MetaX/blob/dev/metax/base/file/mvsv_parser.py | MVSV 解析器，定义元数据字段（`# 标题`、`# 字段`、`# 字段名称`、`# 字段类型`、`# 计数` 等）与数据行按竖线 `\|` 分隔的格式约定 |
| `mvsv_serializer.py` | https://github.com/ACANX/MetaX/blob/dev/metax/base/file/mvsv_serializer.py | MVSV 序列化器，规定元数据与数据区的写回顺序、空行分隔与字段拼接方式 |

实现时 `Python/Quote/common/mvsv.py` 应兼容该解析器输出的 `MVSVData` / `MVSVMetadata` 结构，并保持写回文件能被原解析器无差别解析（round-trip）。

---

## 三、总体架构

```
Python/Quote/
├── Config.yaml              # 数据根目录、窗口天数、归档阈值、日志级别等
├── requirements.txt         # 第三方依赖（PyYAML 等），Actions 与本地共用
├── common/
│   ├── __init__.py
│   ├── config.py            # 加载配置 & 环境变量覆盖
│   ├── mvsv.py              # mvsv 文件读写、ts 排序、去重、合并
│   ├── timeutil.py          # UTC<->北京时间、日切/月切判定、节假日加载
│   ├── gitutil.py           # git add/commit/push 封装（含 push_with_retry 增量发布）
│   ├── logger.py            # 中文结构化日志
│   └── holidays/            # 交易日历
│       ├── cny.csv          #   A 股（中国大陆）
│       ├── hkd.csv          #   港股
│       ├── usd.csv          #   美股 / 美期 / 外汇
│       └── crypto.csv       #   加密货币
├── Task01AggregateLatest.py
├── Task02ArchiveDaily.py
├── Task03ArchiveMonthly.py
└── README.md
```

`.github/workflows/`：

```
Quote01Aggregate.yml     # 任务一：聚合到 Latest
Quote02ArchiveDay.yml   # 任务二：按日归档
Quote03ArchiveMonth.yml # 任务三：按月归档
```

三个 workflow 独立、可手动触发；共享 Python 代码与 `Config.yaml`。Quote01Aggregate 额外支持 `push` 事件——当 `Data/Finv/SecuQuote/**/*.mvsv` 有变更（排除 Latest.mvsv）时自动触发。

---

## 四、数据层设计（`common/mvsv.py`）

### 4.1 MVSV 文件格式（基于 MetaX 参考实现 + 仓库实测）

参考 `MetaX` 仓库的 `mvsv_parser.py` / `mvsv_serializer.py`（详见第二节），并经 `Data/Finv/SecuQuote/GCMain/*.mvsv`、`Data/Finv/SecuQuote/161116/*.mvsv` 实测校验，`.mvsv` 文件由"元数据区 + 空行 + 数据区"三段组成：

```
# 标题 : "{title}"
# 数据供应商 : {data_provider}
# 字段 : Ts|Date|Time|Open|Close|Low|High|Volume|Turnover|ChangePrice|ChangePercent
# 字段名称 : 时间戳(UTC)|日期|时间|开盘价|收盘价|最低价|最高价|成交量|成交额|涨跌值|涨跌幅(%)
# 字段类型 : int|int|int|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|str
# 计数 : {count}
# 采集时间 : "{fetch_time}"
# 证券代码 : {secu_code}
# 市场 : {market}
# 备注 : "{remark}"
# Title : "{title_en}"
# DataProvider : {data_provider_en}
# Field : Ts|Date|Time|Open|Close|Low|High|Volume|Turnover|ChangePrice|ChangePercent
# FieldName : Ts|Date|Time|Open|Close|Low|High|Volume|Turnover|ChangePrice|ChangePercent
# FieldType : int|int|int|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|str
# Count : {count}
# FetchTime : "{fetch_time}"
# SecuCode : {secu_code}
# Market : {market}
                            ← 空行
{ts}|{Date}|{Time}|{Open}|{Close}|{Low}|{High}|{Volume}|{Turnover}|{ChangePrice}|{ChangePercent}
{ts}|...
```

要点：

- **分隔符固定为竖线 `|`**（不是 CSV 的 `,` 或 `\t`）。
- **数据行无表头**，字段信息全部由元数据区 `# 字段 : ...` 承载。
- 元数据区 `# 键 : 值`；字符串值用双引号包裹，数值不加引号；中英双语并存（`标题`/`Title`、`字段`/`Field`、`字段名称`/`FieldName`、`字段类型`/`FieldType`、`计数`/`Count`、`备注`/`Remark`、`采集时间`/`FetchTime`、`证券代码`/`SecuCode`、`市场`/`Market`）。
- **首列 `ts`（小写）**：UTC 秒级整数时间戳，作为全局去重键；`# 字段类型` 对应位置为 `int`。解析时对字段名做大小写兼容，以应对未来字段重命名。
- 实测字段集为 `ts|c|v|t|r|cp`（6 列）；聚合后的 Latest.mvsv 及归档文件扩展为 `ts|Date|Time|Open|Close|Low|High|Volume|Turnover|ChangePrice|ChangePercent`（11 列），其中 Date/Time 由 ts 按北京时间（UTC+8）计算得出；实现时按 `# 字段` 元数据动态识别，不硬编码列数。
- `采集时间`/`FetchTime` 为采集落盘时间（BJT），`证券代码`/`SecuCode` 与目录名一致，**均须通过 `MVSVMetadata.extra` 保留并在序列化时原样回写**。
- **`市场`/`Market`**：取值 `cny` / `hkd` / `usd` / `crypto`，用于在月归档完备性校验时加载对应市场的节假日文件。现有 `Data/Finv/SecuQuote/` 下样本尚未包含该键，M2 阶段需批量补入；`timeutil` 在缺失时按"证券代码规则"兜底映射市场。
- 编码 UTF-8，行末 `\n`。

`mvsv.py` 提供：

```python
parse(path) -> MVSVData                                        # 元数据 + 数据行一次性读入；extra 自动收集非标准键
serialize(data: MVSVData, path)               # 原子写（tmp+rename），append 模式追加；extra 原样回写
merge_and_dedup(existing: MVSVData, incoming: MVSVData, *, now_bjt: datetime) -> MVSVData
                                                               # 同 ts 以 incoming 为准（"后采集覆盖"）；元数据按 4.3 合并
scan_source_files(code_dir) -> list                    # 列出原始采集文件，跳过 Latest.mvsv
```

**去重规则（关键）**：
- 以 `ts` 为唯一键，构建 `dict[ts, row]`。
- 当两个来源都含同一 ts 时：`incoming`（新采集 / 较新文件）覆盖 `existing`。
- "早采集先存、后采集覆盖"通过**文件 mtime 升序**依次 fold 实现：最旧的先放底层，最新的最后合并，自然覆盖。
- 输出时按 `ts` 升序排序。

### 4.2 时间切分（`common/timeutil.py`）

```python
BJT = timezone(timedelta(hours=8))
bjt_day_range_utc(date_d) -> (start_utc, end_utc)   # [D 00:00 BJT, D+1 00:00 BJT) 对应的 UTC 闭-开区间
ts_to_bjt_date(ts) -> date                          # 0 点整归入当日
ts_in_range(ts, start_utc, end_utc) -> bool         # 半开区间 [start, end)
last_complete_month(now_bjt) -> (start_ts, end_ts, yyyymm)
```

日切规则（已确认）：北京时间 `D 00:00:00`（含）至 `D+1 00:00:00`（不含）的所有时间戳归属北京时间日期 `D`。对应 UTC 区间为 `[D-1 16:00:00, D 16:00:00)`。

### 4.3 元数据合并策略（已确认）

**聚合产出的 `.mvsv` 必须保留完整的元数据区**，以 `# 字段 : ...`、`# 字段名称 : ...` 等形式承载字段信息，**严禁退化为 CSV 风格的表头行**——数据区的第一行必须是数据，不是字段名。

`merge_and_dedup(existing: MVSVData, incoming: MVSVData, *, now_bjt: datetime) -> MVSVData` 在合并数据行的同时，按以下规则合并元数据：

| 元数据键 | 合并策略 |
| --- | --- |
| `标题` / `Title` | 保留 `incoming`（聚合目标文件的标题优先；若 incoming 为空则 fallback 到 existing） |
| `数据供应商` / `DataProvider` | 保留 `incoming`（同上） |
| `字段` / `Field` | 保留 `incoming`；如 incoming 与 existing 不一致，记录 WARNING 并以 incoming 为准 |
| `字段名称` / `FieldName` | 同上 |
| `字段类型` / `FieldType` | 同上 |
| `证券代码` / `SecuCode` | 保留 `incoming`（与目录名 `{code}` 一致） |
| `市场` / `Market` | 保留 `incoming`（与证券所属市场一致：`cny`/`hkd`/`usd`/`crypto`）；若 incoming 为空则 fallback 到 existing；都为空时由 `timeutil` 按"证券代码规则"兜底映射 |
| `计数` / `Count` | **重写为合并后数据行的实际行数** `len(merged.rows)` |
| `采集时间` / `FetchTime` | **重写为本次聚合落盘时间**（`now_bjt.strftime('%Y-%m-%d %H:%M:%S')`） |
| `备注` / `Remark` | 保留 `incoming` 原值，**不追加"本次聚合来源"信息** |
| `extra`（其它扩展键） | `existing.extra` ∪ `incoming.extra`，冲突键以 incoming 为准 |

**序列化回写**时按 4.1 规定的顺序输出：中文字段组 → 英文字段组 → 空行 → 数据区；`# 计数` 与 `# 采集时间` 必须用聚合后的实际值，不得沿用源文件中的旧值。

---

## 五、任务实现要点

### 任务一：`Task01AggregateLatest.py`

流程（对每个证券目录 `{code}`）：

1. 扫描 `Data/Finv/SecuQuote/{code}/*.mvsv`（排除 `Latest.mvsv`），按 mtime 升序。
2. 读取现有 `Latest.mvsv`（若存在）作为 base；若不存在则 base = 空 `MVSVData`。
3. 逐个 fold 原始文件：`base = merge_and_dedup(base, file, now_bjt=now_bjt)`——**数据行按 ts 后采集覆盖，元数据按 4.3 规则合并**（`# 计数`、`# 采集时间` 在每次 fold 时都会以最新值重写）。
4. **扩展为 11 列**：调用 _expand_to_11cols(base) 添加 Open/Low/High 列，将 `base` 中**超过 7 天**（`daily_archive_after_days`）的数据按北京时间切日归档到 `Archive/Finv/SecuQuote/Day/{code}/{code}_Min_yyyyMMdd.mvsv`；归档同样遵循任务二的"元数据按 4.3 合并、已存在则合并而非覆盖、按证券维度单次 commit"。归档 commit 成功后方可进入下一步。
5. **清理原始采集文件**：对 ts 范围已被 Latest 完整覆盖的源文件执行 git rm（`latest_window_days`）；裁剪后重写 `# 计数` / `# 采集时间`。
6. 原子写回 `Latest.mvsv`（tmp + rename 原子替换）。
7. git add `Latest.mvsv` + commit + **按证券增量 push**；commit message 形如 `[quote] aggregate Latest for {code}`。
8. **清理原始采集文件**（`cleanup_raw_after_aggregate=true`，已确认）：
   - 回读刚落盘的 `Latest.mvsv`，取其 `min(ts)/max(ts)`；
   - 对步骤 1 的每个原始文件，若其 `min(ts)/max(ts)` 均落在 Latest 的范围内（即已被完整聚合），执行 `git rm` + commit + push；
   - 任一原始文件未被覆盖式包含，保留并记录 WARNING。

**防覆盖策略**：
- 写文件使用 tmp+rename 原子替换，进程中断不会产生半成品。
- `Latest.mvsv` 中已存在的 ts 行不会被"删除"——只会被更新的同名 ts 覆盖（后采集优先）。
- 14 天外的数据仅在任务二确认日归档已写入并 git commit 后才从 Latest 中裁剪。
- 原始采集文件仅在 Latest commit 成功、且 ts 范围校验通过后才删除；未通过的保留并告警，不做强删。

### 任务二：`Task02ArchiveDaily.py`

流程：

1. 读取 `Latest.mvsv`。
2. 筛选 `ts < now_utc - 7 days` 的行。
3. 按北京时间日期分组。
4. 对每个日期 `D`：
   - 目标路径 `Archive/Finv/SecuQuote/Day/{code}/{code}_Min_yyyyMMdd.mvsv`
   - 若已存在：`merged = merge_and_dedup(existing_on_disk, new_rows, now_bjt=now_bjt)`，其中 `existing_on_disk` 作为 base、`new_rows` 作为 incoming，**同一 `ts` 以 `new_rows` 为准（后采集覆盖）；元数据按 4.3 合并，`# 计数` / `# 采集时间` 以本次写回为准重写**；
   - 若不存在：直接以 `new_rows` 构造 `MVSVData`，元数据从 `Latest.mvsv` 继承后重写 `# 计数` / `# 采集时间`；
   - 写回采用 tmp + rename 原子替换，按 `ts` 升序排序；
   - **不逐个 commit**，仅 `git add` 到暂存区，等待步骤 5 统一 commit。
5. 同一证券的所有日期归档写完后，**一次性 git commit**，message 形如 `[quote] archive daily for {code} ({N} days)`；commit 成功后再从 `Latest.mvsv` 中裁剪已归档行并原子写回 + 再 commit（同一证券两次 commit：归档 + Latest 裁剪）。

**删除前置校验**：
- 对每个待归档日，写完后回读并校验归档文件的 `min(ts) / max(ts)` 覆盖 `Latest.mvsv` 中该日的 `min/max`。
- 校验通过后才能进入 Latest 裁剪阶段。

### 任务三：`Task03ArchiveMonthly.py`

触发时机（已确认）：每月第三周的周一（北京时间 10:00），即 cron `0 2 15-21 * 1`，归档上一自然月的日数据。

流程：

1. 计算"上一自然月"的 BJT 起止 ts。
2. 扫描 `Archive/Finv/SecuQuote/Day/{code}/{code}_Min_yyyyMMdd.mvsv`，筛选落在该月内的日文件。
3. 完备性检查：
   - 该月每一天（按**对应市场的节假日文件**扣除周末与节假日，市场信息来自 mvsv 元数据 `# 市场`/`# Market`，缺失时按"证券代码规则"兜底）都应有日归档；
   - 若缺失，仍聚合现有日归档，并在月归档元数据的 `# 备注` / `# Remark` 中记录缺失交易日；后续增量运行会重新计算该备注。
   - 节假日文件按 `Config.yaml` 的 `holidays_files` 字典定位；若对应市场的文件缺失则记录 ERROR 并跳过该证券的月归档。
4. 合并所有日文件 → `merge_and_dedup`，日文件之间按 mtime 升序 fold（mtime 较新者为 incoming，覆盖同 ts；每次 fold 按 4.3 重写 `# 计数` / `# 采集时间`）。
5. 写入 `Archive/Finv/SecuQuote/{yyyy}/{code}/{code}_Min_yyyyMM.mvsv`：
   - 若已存在：以现有文件为 base、本次合并结果为 incoming，**同一 `ts` 以 incoming 为准（后采集覆盖）；元数据按 4.3 合并**；
   - 若不存在：直接写；元数据继承自最旧的日文件并重写 `# 计数` / `# 采集时间`；
   - 写回采用 tmp + rename 原子替换，按 `ts` 升序排序。
6. 每个证券的月归档写完后单独 `git commit`，message 形如 `[quote] archive monthly {yyyyMM} for {code}`；若本次运行只有 1 个证券有数据，则自然退化为单次任务一个 commit。
7. 删除日文件的条件（保守策略）：
   - 月归档已 commit；
   - 当前 BJT 月份 `>= 目标月 + 2`（即 6 月数据最早在 8 月才能删 6 月日文件，给节假日留缓冲）；
   - 回读月归档文件并校验其 ts 范围覆盖所有将被删除的日文件。
   - 通过后同一证券的所有待删日文件批量 `git rm` + **单个 commit**，message 形如 `[quote] cleanup daily {yyyyMM} for {code} ({N} files)`。

---

## 六、安全与一致性

| 风险 | 对策 |
| --- | --- |
| 并发运行（同 workflow / 跨 workflow） | 三个 workflow 共享 `concurrency.group: quote-all`、`cancel-in-progress: false`，同一时刻只允许一个任务读写 Latest 与归档目录；后到任务排队等待 |
| 进程中断致文件损坏 | 所有写操作走 `tmp + os.replace` 原子替换 |
| git push 冲突 | **按证券增量 push**（每完成一个证券的 commit 阶段即推一次）；失败时自动 `git pull --rebase origin quote` 后重试，重试次数由 `Config.yaml.git_push_retries`（默认 1）控制；仍失败则记 ERROR 并退出非 0，本地 commit 链保留以便人工排查 |
| 数据误删 | 每条删除路径都有"下一级覆盖性校验" + "延迟窗口"两道闸 |
| 原始采集文件清理 | **默认清理**（`cleanup_raw_after_aggregate=true`）：聚合到 `Latest.mvsv` 并 commit 成功后，回读 Latest 校验 ts 范围覆盖原始文件的 `min/max ts`，通过才 `git rm` + commit；未通过的保留并记 WARNING |

---

## 七、日志规范（`common/logger.py`）

- 语言：简体中文。
- 格式：`[ISO8601][LEVEL][task{1,2,3}][{code}] 消息`。
- 关键节点必打：
  - 任务开始 / 结束（含耗时）
  - 每个证券：原始文件数、合并前行数、合并后行数、新增行数、删除行数
  - 每次 git commit 的 SHA 与 message
  - 跳过/告警/错误原因（含路径）
- 日志同时输出到 stdout（Actions 可见）与 `Python/Quote/logs/{task}_{yyyyMMdd}.log`。
- **启动时自动滚动清理**：`common/logger.py` 初始化阶段扫描 `log_dir`，删除文件名中 `yyyyMMdd` 早于 `now - log_retention_days`（默认 14 天）的 `.log` 文件；删除前记录 INFO 日志。日志目录不入 git。

---

## 八、GitHub Actions 设计

三个 workflow 结构一致，差异在 `script`：

```yaml
name: Quote01Aggregate
on:
  workflow_dispatch:
    inputs:
      codes:
        description: '仅处理指定证券（逗号分隔，留空=全部）'
        required: false
        default: ''
  schedule:
    - cron: '0 2 * * 1,3,5'   # 每周 1/3/5 10:00 BJT
concurrency:
  group: quote-all              # 三个 workflow 共享同一 group，跨 workflow 互斥
  cancel-in-progress: false
jobs:
  aggregate:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v5
        with: { ref: quote, fetch-depth: 0 }
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install -r Python/Quote/requirements.txt
      - name: Configure git identity
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
      - name: Run Task01AggregateLatest
        env:
          QUOTE_CODES: ${{ github.event.inputs.codes }}
        run: python Python/Quote/Task01AggregateLatest.py
      - name: Verify clean push
        run: |
          # 脚本已内部完成增量 push；此处确认无漏 commit / 未 push 的本地分支
          git log origin/quote..HEAD --oneline | (! read)
          git status --short | (! read)
```

- 脚本内部已通过 `common/gitutil.push_with_retry(ref=None, retries=Config.git_push_retries)` 自动检测当前分支并增量 push，**workflow 不再集中 push**；末尾 `VerifyCleanPush` 步骤仅作兜底校验。
- `QUOTE_CODES` 环境变量透传 `workflow_dispatch` 的 `codes` 入参，由 `common/config.py` 解析。
- `Quote02ArchiveDay.yml` 触发 `Task02ArchiveDaily.py`，schedule 与任务一错开 1 小时；**同样声明 `concurrency.group: quote-all`、同样保留 `workflow_dispatch.codes` 入参**。
- `Quote03ArchiveMonth.yml` 触发 `Task03ArchiveMonthly.py`，schedule `cron: '0 2 15-21 * 1'`（每月第三周的周一 10:00 BJT）；**同样声明 `concurrency.group: quote-all`、同样保留 `workflow_dispatch.codes` 入参**。
- 本地运行模式：`git checkout quote` → `python Python/Quote/Task*.py` → 本地 `git log` 复核 → `git push origin quote`。

---

## 九、配置（`Python/Quote/Config.yaml`）

```yaml
RepoRoot: .                          # 参考点声明，固定为 "."；实际以 git 仓库根为基准
DataRel: Data/Finv/SecuQuote
ArchiveRel: Archive/Finv/SecuQuote
codes: []                       # 空 = 自动发现全部证券目录
LatestWindowDays: 15
DailyArchiveAfterDays: 10
MonthlyCron: '0 2 15-21 * 1'    # 每月第三周周一 10:00 BJT 触发上一自然月归档
MonthlyDeleteLagMonths: 2
CleanupRawAfterAggregate: true
LogDir: Python/Quote/logs
LogRetentionDays: 14            # 启动时自动清理早于 now - N days 的本地日志
GitPushRetries: 1               # push 失败后 pull --rebase 重试次数
HolidaysFiles:
  cny: Python/Quote/common/holidays/cny.csv
  hkd: Python/Quote/common/holidays/hkd.csv
  usd: Python/Quote/common/holidays/usd.csv
  crypto: Python/Quote/common/holidays/crypto.csv
```

所有相对路径（`data_rel`、`archive_rel`、`log_dir`、`holidays_files[*]`）均以 **git 仓库根** 为参考点；`common/config.py` 在加载时通过 `GITHUB_WORKSPACE`（Actions）或 `git rev-parse --show-toplevel`（本地）定位仓库根后拼接为绝对路径。

---

## 十、Python 依赖（`Python/Quote/requirements.txt`）

代码锚定 Python 3.11+，标准库外仅引入最小必需第三方模块。`requirements.txt` 与 Actions `pip install -r Python/Quote/requirements.txt` 以及本地 `pip install -r requirements.txt` 共用同一份清单。

```text
# ---- 核心依赖 ----
PyYAML>=6.0.1,<7            # 解析 Config.yaml

# ---- 交易日历 ----
# 已确认：不引入 chinese_calendar，改用仓库内 Python/Quote/common/holidays/*.csv
# chinese_calendar>=1.10.0  # 已弃用
```

**依赖说明**：

| 包 | 用途 | 是否必需 |
| --- | --- | --- |
| `PyYAML` | `common/config.py` 通过 `yaml.safe_load` 读取 `Config.yaml` | **必需** |
| ~~`chinese_calendar`~~ | 月归档完备性校验所需的交易日历 | **已确认不引入**，改用仓库内 `Python/Quote/common/holidays/{cny,hkd,usd,crypto}.csv` |

**不引入**的原则：

- 不引入 `pandas` / `numpy`：数据量（单证券 14 天 ≈ 5460 行）无需重型计算框架，纯标准库 `csv` 风格的 `mvsv.py` 即可胜任；
- 不引入 `MetaX`：mvsv 模块尚未正式发布；
- 不引入 `pytest` / `black` / `ruff` 等开发期工具：如需本地测试由用户自行安装，避免污染生产依赖；
- 不引入 `requests` / `httpx`：本方案不发起任何网络请求。

Actions workflow 与本地运行均通过同一份 `requirements.txt` 安装依赖，保证环境一致性。

---

## 十一、开发计划（建议里程碑）

| # | 里程碑 | 产出 | 验收 |
|---|---|---|---|
| M1 | 脚手架 & 配置加载 | `common/config.py`、`Config.yaml`、`logger.py` | 单测：配置优先级（文件 < 环境变量） |
| M2 | mvsv 读写与合并核心 | `mvsv.py`、`timeutil.py` | 单测：去重顺序、覆盖方向、日切边界（UTC 16:00:00） |
| M3 | 任务一脚本 | `Task01AggregateLatest.py` | 集成测试：多文件合并 → Latest；重复执行幂等 |
| M4 | 任务二脚本 | `Task02ArchiveDaily.py` | 集成测试：跨日切点数据归属正确；已存在归档文件不丢失数据 |
| M5 | 任务三脚本 | `Task03ArchiveMonthly.py` | 集成测试：月内日文件缺失时跳过；月归档存在时合并而非覆盖；删除闸门严格 |
| M6 | git 工具与日志 | `gitutil.py` 完善 + 中文日志 | 集成测试：断点续跑不产生重复 commit |
| M7 | GitHub Actions | 三个 workflow 文件（Quote01/02/03） | 在 quote 分支手动 dispatch 各跑一次，验证产物与日志 |
| M8 | 文档 & 示例数据 | `Python/Quote/mvsv.md`、`问题记录.md`、`FixLatestDateCols.py` 等 | mvsv.md 覆盖格式版本演进与函数使用说明 |

建议节奏：M1–M2 用 1 个 PR 打底；M3–M5 每个任务一个 PR；M6–M8 收尾一个 PR。全部合入 quote 后再向 main 提 PR。

---

## 十二、测试策略（尚未实现）

- **单元**（`pytest`，`Python/Quote/tests/`）：
  - `test_mvsv_merge_order`：验证"后采集覆盖"。
  - `test_bjt_day_boundary`：UTC 15:59:59 与 16:00:00 落不同日。
  - `test_idempotent_latest`：同输入跑两次，Latest 字节级一致。
- **集成**：构造 `tests/fixtures/SecuQuote/{code}/` 多文件场景，跑全链路，断言 `Latest`、`Day`、月归档行数与 ts 范围，以及月内日文件缺失时仍聚合并写入缺失日期备注。
- **回归**：使用仓库内真实样例（如 `Data/Finv/SecuQuote/GCMain/` 下现有 mvsv）做只读试跑，打印 dry-run 报告。

---

## 十三、风险与缓解

- **数据量大**：单证券 14 天分钟级 ≈ 14×390 ≈ 5460 行（期货可能更长），内存合并无压力；若扩展到 tick 级再改为分块流式合并。
- **git 仓库膨胀**：mvsv 是文本，建议后续评估 Git LFS 或仅归档不删 Latest 的策略。
- **节假日延迟采集**：月归档固定在每月第三周周一触发（约 14–20 天缓冲），删除窗口 `+2 月`，已留足缓冲。
- **Actions 并发**：三个 workflow 共享 `concurrency.group: quote-all`、`cancel-in-progress: false`，同一时刻只允许一个任务运行，后到任务排队；本地手动执行时由 `{code}.lock` 文件锁兜底。
