# VerifyQuoteMinute 使用说明

> 适用目录：`.github/Python/`
> 关联工作流：`.github/workflows/VerifyQuoteMinute.yml`

## 1 构成

| 项 | 值 | 备注 |
| --- | --- | --- |
| `VerifyQuoteMinute.py` | 定时任务主体 | 读索引取参数、调行情接口、转 `.mvsv`、按结论回写仓库 |
| `TriggerVerifyQuoteMinute.py` | 外部触发脚本示例 | 在仓库外部经 workflow_dispatch 调起工作流并传入 `usc` |
| `VerifyQuoteMinuteWatchlist.txt` | 待验证清单 | 未指定 `usc` 时（如 schedule 触发）从此文件取 usc |
| `GitHubCommitContent.py` | 提交库 | 既有脚本，负责把本地文件写进仓库指定分支的指定路径 |
| `UscFutuMapping.jsonl.idx` | 证券参数索引 | 定长二进制索引，按 usc 二分定位到 JSONL 中的行 |
| `UscFutuMapping.jsonl` | 证券映射表 | 每行一条 JSON，含该证券调行情接口所需的全部参数 |

## 2 数据流

```text
外部触发器（本机调度器 / 人工）
  └─ POST /repos/{owner}/{repo}/actions/workflows/VerifyQuoteMinute.yml/dispatches
       body: {"ref": "...", "inputs": {"usc": "000001", ...}}
         └─ GitHub Actions: VerifyQuoteMinute.yml
              └─ env INPUT_USC=… → python3 .github/Python/VerifyQuoteMinute.py
                   ├─ UscFutuMapping.jsonl.idx 按 usc 取 stockId / marketType / marketCode /
                   │  instrumentType / subInstrumentType / quoteMarket
                   ├─ GET https://www.moomoo.com/quote-api/quote-v2/get-quote-minute
                   ├─ 转 .mvsv（成功带 K 线，失败只带元信息与错误原因）
                   └─ GitHubCommitContent.commit_content_file → 目标分支目标路径
```

## 3 产物落点

| 结论 | 仓库内路径 | 备注 |
| --- | --- | --- |
| `SUCCESS` | `Verify/Success/{quoteMarket}/{usc}.mvsv` | 头部元信息 + 分钟 K 线数据行 |
| `FAILED` | `Verify/Fail/{quoteMarket}/{usc}.mvsv` | 只有头部元信息，错误原因写在 `# 错误原因` |
| `THROTTLED` | 不写文件 | 瞬时限速，默认不留痕，重跑即可 |
| `SKIPPED` | 不写文件 | 被 `--markets` 过滤，未发请求 |

`quoteMarket` 取映射记录里的行情市场段（`HK`、`US`、`SH`、`SZ`、`FX`、`CC`、`BJ`）；
usc 不在索引中时取 `UNKNOWN`。

失败件与成功件分属两个目录，互不覆盖：同名 usc 若两边都存在，**以 `采集时刻` 更晚的一份为准**。

## 4 任务脚本入参

### 4.1 取值型参数

| 参数名称 | 类型 | 是否必须 | 示例 | 备注 |
| --- | --- | --- | --- | --- |
| `--usc` | string | 否 | `000001,00700` | 待验证 usc，逗号或空白分隔；留空则读 `--watchlist` |
| `--watchlist` | string | 否 | `VerifyQuoteMinuteWatchlist.txt` | 清单文件，默认取脚本同目录下的同名文件 |
| `--markets` | string | 否 | `SZ,SH` | 只处理这些行情市场段，留空不限 |
| `--limit` | int | 否 | `0` | 本批最多处理条数，`0` 表示不限 |
| `--interval` | float | 否 | `10` | 相邻两次请求的最小间隔秒数，过密会被限速 |
| `--throttle-retries` | int | 否 | `3` | 命中限速后的最大重试次数 |
| `--throttle-backoff` | float | 否 | `30` | 限速退避基数秒数，按 2 的幂次递增、上限 120 秒 |
| `--quote-type` | string | 否 | `2` | 接口 `type` 参数，`2` 表示五日分钟 |
| `--price-field` | string | 否 | `auto` | 收盘价取用口径，取值见 §4.2 |
| `--branch` | string | 否 | `quote-meta` | 产物提交目标分支 |
| `--owner` | string | 否 | `ACANX` | 目标仓库属主，留空按环境变量与 `.git/config` 解析 |
| `--repo` | string | 否 | `Repo` | 目标仓库名，解析顺序同上 |
| `--out-dir` | string | 否 | `verify-out` | 本地镜像目录，提交前先落到这里 |
| `--fixture` | string | 否 | `fixture.json` | 用本地接口响应样本代替联网，仅自测用 |
| `--max-minutes` | float | 否 | `20` | 本轮时间预算分钟数，`0` 表示不限 |

### 4.2 `--price-field` 取值

| 取值 | 含义 | 备注 |
| --- | --- | --- |
| `auto` | 优先复权价，缺失回退成交价 | 默认值 |
| `cc_price` | 只取复权价 | 缺该字段时该格写空 |
| `price` | 只取成交价 | 富途对部分品种按放大整数返回，按需使用 |

### 4.3 开关型参数

| 参数名称 | 类型 | 是否必须 | 示例 | 备注 |
| --- | --- | --- | --- | --- |
| `--dry-run` | bool | 否 | `--dry-run` | 只本地生成 `.mvsv`，不提交 |
| `--no-upload` | bool | 否 | `--no-upload` | 与 `--dry-run` 等价 |
| `--allow-verify-failure` | bool | 否 | `--allow-verify-failure` | 有证券验证失败时仍返回退出码 0 |
| `--throttle-writes-fail` | bool | 否 | `--throttle-writes-fail` | 命中限速时也写失败件，默认不写 |
| `--no-abort-on-throttle` | bool | 否 | `--no-abort-on-throttle` | 命中限速后继续处理剩余条目，默认提前收工 |
| `--no-digest-check` | bool | 否 | `--no-digest-check` | 跳过索引摘要校验，省一次整份 JSONL 读取 |

### 4.4 退出码

| 退出码 | 含义 | 备注 |
| --- | --- | --- |
| `0` | 全部验证成功 | 或虽有失败但传了 `--allow-verify-failure` |
| `1` | 有证券验证失败或限速未采 | 失败件已回写 |
| `2` | 任务级错误 | 索引不可用、入参非法、产物提交失败 |

## 5 工作流入参

| 入参 | 类型 | 是否必须 | 示例 | 备注 |
| --- | --- | --- | --- | --- |
| `usc` | string | 否 | `000001,00700` | **外部触发指定的证券**；留空则读仓库内清单文件 |
| `branch` | string | 否 | `quote-meta` | 产物提交目标分支，默认 `quote-meta` |
| `checkout_ref` | string | 否 | `quote-meta` | 检出哪个分支上的任务脚本，留空取本次 dispatch 的 ref |
| `markets` | string | 否 | `SZ` | 只处理这些行情市场段 |
| `interval` | string | 否 | `10` | 相邻两次请求的最小间隔秒数 |
| `limit` | string | 否 | `0` | 本批最多处理条数 |
| `price_field` | choice | 否 | `auto` | 取值 `auto`、`cc_price`、`price` 三选一 |
| `dry_run` | boolean | 否 | `false` | 置真则只本地生成、不提交 |
| `allow_verify_failure` | boolean | 否 | `false` | 置真则验证失败也判为执行成功 |

工作流把这些入参经 `INPUT_*` 环境变量透传给脚本，二者一一对应：

| 环境变量 | 对应入参 | 备注 |
| --- | --- | --- |
| `INPUT_USC` | `usc` | 外部触发指定的请求参数 |
| `INPUT_BRANCH` | `branch` | 产物提交目标分支 |
| `INPUT_MARKETS` | `markets` | 市场过滤 |
| `INPUT_INTERVAL` | `interval` | 请求间隔 |
| `INPUT_LIMIT` | `limit` | 本批条数上限 |
| `INPUT_PRICE_FIELD` | `price_field` | 收盘价口径 |
| `INPUT_OUT_DIR` | 无 | 工作流固定给 `verify-out`，用于留档为 Actions 产物 |
| `INPUT_DRY_RUN` | `dry_run` | 取值 `1`、`true`、`yes`、`on` 视为真 |
| `INPUT_ALLOW_VERIFY_FAILURE` | `allow_verify_failure` | 同上 |
| `INPUT_THROTTLE_WRITES_FAIL` | 无 | 本地调试用，工作流未暴露 |
| `INPUT_OWNER` | 无 | 覆盖目标仓库属主，工作流未暴露 |
| `INPUT_REPO` | 无 | 覆盖目标仓库名，工作流未暴露 |
| `INPUT_WATCHLIST` | 无 | 覆盖清单文件路径，工作流未暴露 |

脚本也可脱离工作流直接跑，命令行参数优先级高于同名 `INPUT_*` 环境变量。

## 6 部署前提

| 项 | 值 | 备注 |
| --- | --- | --- |
| Actions 权限 | `contents: write` | 工作流已声明；产物要提交回仓库 |
| 提交令牌 | 仓库密钥 `GITHUB_COMMIT_TOKEN` | 未配置时回落到 `github.token` |
| 触发令牌 | 细粒度 PAT 的 Actions 读写权限 | 仅外部触发器需要，供 workflow_dispatch 调用 |
| 工作流所在分支 | `quote-meta` | workflow_dispatch 的 `ref` 必须指向该分支 |
| 定时触发 | 需工作流位于默认分支 | Actions 的 `schedule` 只在默认分支上的工作流文件生效 |

令牌一律经环境变量注入，禁止写进源码、提交信息或日志。

PowerShell 下传多个 usc **必须加引号**，否则 `000001,00700` 会被 PowerShell 当数组解析、
前导零被吞掉：

```powershell
$env:GITHUB_COMMIT_TOKEN = "ghp_xxx"
python3 TriggerVerifyQuoteMinute.py --repo ACANX/Repo --ref quote-meta --usc "000001,00700"
```

## 7 外部触发示例

### 7.1 用随附脚本触发（推荐）

```bash
export GITHUB_COMMIT_TOKEN=ghp_xxx

# 触发一次，验证 000001
python3 TriggerVerifyQuoteMinute.py \
    --repo ACANX/Repo --ref quote-meta --usc 000001

# 多个 usc，产物提交到 quote-meta，只生成不提交
python3 TriggerVerifyQuoteMinute.py \
    --repo ACANX/Repo --ref quote-meta --branch quote-meta \
    --usc "000001,00700,600519" --dry-run

# 触发并等待本次运行结束，打印结论
python3 TriggerVerifyQuoteMinute.py \
    --repo ACANX/Repo --ref quote-meta --usc 000001 --wait

# 只看将要发出的请求，不真的触发
python3 TriggerVerifyQuoteMinute.py \
    --repo ACANX/Repo --ref quote-meta --usc 000001 --print-only
```

### 7.2 用 curl 触发

```bash
curl -X POST \
  -H "Authorization: Bearer $GITHUB_COMMIT_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/ACANX/Repo/actions/workflows/VerifyQuoteMinute.yml/dispatches \
  -d '{"ref":"quote-meta","inputs":{"usc":"000001,00700","branch":"quote-meta"}}'
```

请求体各字段：

| 字段 | 类型 | 是否必须 | 示例 | 备注 |
| --- | --- | --- | --- | --- |
| `ref` | string | 是 | `quote-meta` | 该 ref 上必须有工作流文件，否则返回 404 |
| `inputs.usc` | string | 是 | `000001,00700` | 请求参数：要验证哪个证券 |
| `inputs.branch` | string | 否 | `quote-meta` | 产物提交目标分支 |
| `inputs.dry_run` | string | 否 | `true` | 传 `"true"` 只生成不提交 |

`inputs` 里的值一律是**字符串**（布尔写 `"true"`），且只放需要覆盖的项——
整份 `inputs` 会替换工作流默认值，传空串会把默认值顶掉。

## 8 `.mvsv` 产物格式

头部沿用既有行情 `.mvsv` 约定（与 `GLD_Min_*.mvsv`、`GCMain_Min_*.mvsv` 逐键一致），
数据行以 `|` 分隔、列序写在头部的 `# 字段` 里：

| 项 | 值 | 备注 |
| --- | --- | --- |
| 编码 | UTF-8 | 不带 BOM |
| 行结束符 | `LF` | 末尾不写换行 |
| 元信息行前缀 | `# ` | 形如 `# 键 : 值` |
| 含竖线的取值 | 成对双引号包住 | 如 `# 备注 : "汇总: K线=241\|..."` |
| 头与数据分界 | 一个空行 | 失败件无数据行 |
| 字段分隔符 | `\|` | 不做转义，字段内出现竖线一律换成 `/` |

列序与取数口径：

| 序号 | 列名 | 取数来源 | 备注 |
| --- | --- | --- | --- |
| 1 | `Ts` | 接口 `time` | Unix 秒 |
| 2 | `Date` | `Ts` 的 UTC+8 日历日 | 形如 `20260612` |
| 3 | `Time` | `Ts` 的 UTC+8 时刻 | 形如 `000000` |
| 4 | `Open` | 上一根的 `Close` | 首根取接口 `last_close_price` |
| 5 | `Close` | 接口 `cc_price` | 缺失时按 `--price-field` 口径回退 |
| 6 | `Low` | 无 | 接口不下发分钟最低价，留空 |
| 7 | `High` | 无 | 接口不下发分钟最高价，留空 |
| 8 | `Volume` | 接口 `volume` |  |
| 9 | `Turnover` | 接口 `turnover` |  |
| 10 | `ChangePrice` | 接口 `change_price` |  |
| 11 | `ChangePercent` | 接口 `ratio` | 接口已是百分数文本，原样写入 |

`Date` 与 `Time` 用 UTC+8（Asia/Shanghai）日历值，与既有 `.mvsv` 行情文件口径一致；
`Ts` 保持 UTC 的 Unix 秒原值。该口径写在产物头部的 `# 时间口径` 一行里。

头部除既有键外还追加一段溯源与结论（`# 验证结论`、`# 错误代码`、`# 错误原因`、
`# 请求参数`、`# 索引摘要`、`# 采集时刻` 等），读取方按「已知键 `putIfAbsent`、
未知键忽略」的既有实现即可无感兼容。

## 9 失败分类与处理

| 错误代码 | 含义 | 处理建议 |
| --- | --- | --- |
| `INDEX_MISS` | usc 不在 `UscFutuMapping.jsonl.idx` 中 | 核对 usc 拼写；确认索引与清单同批次 |
| `INDEX_FIELD_MISSING` | 映射记录缺参数 | 重新产出映射表 |
| `EMPTY` | 接口成功但 K 线为空 | 该证券可能长期无成交或已停牌 |
| `API_ERROR` | 接口返回业务错误码 | 看产物头部的 `message` 原文 |
| `BAD_RESPONSE` | 响应无法解析为 JSON 业务体 | 多为网关错误页，重跑一次 |
| `THROTTLED` | 响应非 JSON，按限速拦截处理 | 降低频率或换出口 IP 后重跑，不写失败件 |
| `NETWORK_ERROR` | 网络层重试耗尽 | 检查出口网络后重跑 |
| 提交失败 | `Verify/Fail/` 或 `Verify/Success/` 未落地 | 检查令牌权限与目标分支是否存在 |

## 10 本地自测

```bash
cd .github/Python

# 只查索引 + 真连接口，不提交
python3 VerifyQuoteMinute.py --usc 000001 --dry-run

# 完全离线：用本地响应样本代替联网
python3 VerifyQuoteMinute.py --usc 000001 --dry-run --fixture fixture.json

# 只看外部触发会发什么请求
python3 TriggerVerifyQuoteMinute.py --repo ACANX/Repo --ref quote-meta \
    --usc 000001 --print-only
```
