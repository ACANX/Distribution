#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VerifyQuoteMinuteSyncConfig.py — 把 Verify/Success/ 下证券的查询参数同步进 Supabase 配置表。

目标表（2026-09 改名，务必按新口径理解）：
    finv_quote_collect_futu   富途采集配置表，**主键 usc**（原名 finv_quote_futu_collect，主键 stockId）
    finv_quote_secu           证券元数据登记表，主键 usc（其 sid 列已废弃，脚本不读不写）

入参：环境变量 INPUT_*（由工作流 .github/workflows/VerifyQuoteMinute.yml 的「同步配置到 Supabase」
步骤注入，本地自测直接设同名环境变量即可）。无命令行参数。

    INPUT_USC                    本批 usc，逗号或空白分隔（与采集步骤同口径）
    INPUT_WATCHLIST              清单文件路径；INPUT_USC 为空时改读它
    INPUT_BRANCH                 产物分支：读它的 Verify/Success/，MisMatch 也提交到它，默认 quote-meta
    INPUT_OUT_DIR                本地镜像目录（MisMatch 本地副本落在此），默认 verify-out
    INPUT_SYNC_PROBABILITY       本轮是否执行的命中概率（0-100，默认 100）
    INPUT_SYNC_BATCH_SIZE        单轮最多同步多少条（默认 50，0 = 不限）
    INPUT_SYNC_SAMPLE_LIMIT      从 Verify/Success/ 清单头部最多补齐多少条（默认 47）
    INPUT_SYNC_ALLOW_INSERT      【可选覆盖】缺行的证券是否允许新建；**定论见常量 ALLOW_INSERT**
    INPUT_SYNC_DELETE            【可选覆盖】同步完成的记录是否删除其 Verify/Success/{market}/
                                 {usc}.mvsv；**定论见常量 CONSUME_SUCCESS**（默认开）。
                                 这两个是业务定论、不是调试旋钮，故写死在脚本常量里，不依赖
                                 各分支工作流是否带了同名 env（缺 env 时会静默回落到关，
                                 曾因此出现「以为删了其实没删」）。运行时头部会打印取值与来源。
    INPUT_SYNC_DRY_RUN           置真则只打印将要执行的 SQL 与待删文件，不写库、不提交、不删除
    SUPABASE_PROJECT_REF         Supabase 项目引用（必填）
    SUPABASE_KEY                 Supabase API 密钥（service-role；必填，不落日志）
    GITHUB_COMMIT_TOKEN          MisMatch 留痕提交、.mvsv 删除用（约定变量名见 GitHubCommitContent.py）

本文档分节：
    一、工具定位        为什么需要这一步、与 VerifyQuoteMinute.py 的分工
    二、抽样规则        概率闸门 → 本批 → 清单头部补齐
    三、映射表          由 .mvsv 文件名取 usc，经 idx + JSONL 反序列化出查询参数
    四、更新规则        两张表按 usc 关联的字段映射、flag_enable、dt_update、批量 upsert，
                        以及 upsert 的三个硬性前提（非空列、列真实存在、行键一致）与三处刻意的收窄
    五、PostgREST 调用  批量读现状 + 每表一条批量 upsert（含重试与分块）
    六、未命中与插入    缺行时留痕还是新建（INPUT_SYNC_ALLOW_INSERT）、新增行如何被单独列出
    七、日志与摘要      逐条字段级日志、dry-run 的 SQL 计划、Actions 步骤摘要
    八、退出码
    九、消费产物        同步完成后删除对应的 .mvsv（INPUT_SYNC_DELETE），让清单头部前移

--------------------------------------------------------------------------------
一、工具定位
--------------------------------------------------------------------------------
VerifyQuoteMinute.py 负责「按 usc 验证行情能否采到」，产物落在 Verify/Success|Fail/{market}/。
本脚本负责它的**下游**：把 Success 下的证券（文件名去掉扩展名即 usc）对应的**查询参数**
回填进 Supabase 的两张配置表，并把 flag_enable 置 '1'，让新配置立即生效。

参数不从 .mvsv 文件里读，而是走**同一套映射产物**（UscFutuMapping.jsonl.idx +
UscFutuMapping.jsonl）—— .mvsv 头部只带了部分参数（缺 marketType / marketCode /
instrumentType / subInstrumentType / typeSecu / secuRegion / secuMarket），且解析正文成本更高。
本脚本因此只读文件名、不读文件内容：一次目录列举即可拿到全部待同步 usc。

--------------------------------------------------------------------------------
二、抽样规则
--------------------------------------------------------------------------------
    ① 概率闸门：抽 [0,100) 的随机数，小于 INPUT_SYNC_PROBABILITY 才继续；未命中则本轮
       什么都不做（打印日志后退出 0）。概率值刻意放在工作流文件里，便于随时调整：
       当前为 100（便于测试与追赶），稳定后按设计调回 10。
    ② 本批 usc：INPUT_USC / 清单文件解析出的 usc，**且必须已存在于目标分支的
       Verify/Success/ 下**（采失败的证券不该被 enable）；不在 Success 下的会被剔除并记日志。
    ③ 补齐抽样：从 Verify/Success/ 清单**按路径升序取前 N 个**（N = INPUT_SYNC_SAMPLE_LIMIT，
       默认 47），与 ② 合起来凑成一批（默认 50 条）。取头部是刻意的确定性行为：结果可复现、
       日志可追溯；同步完成即删除产物（第九节）后，每轮的头部会自然前移。
    ④ 目标数上限 INPUT_SYNC_BATCH_SIZE（默认 50）：本批已超过上限时不再补齐。

一轮要写多少行，是「本批 ∪ 清单头部」去重后的结果，不是全量重刷。

--------------------------------------------------------------------------------
三、映射表
--------------------------------------------------------------------------------
与 VerifyQuoteMinute.py 完全同一套产物与格式：
    UscFutuMapping.jsonl.idx   定长索引：32 字节头 + N × 24 字节记录（16 字节 key + 偏移 + 长度）
    UscFutuMapping.jsonl       每行一条 JSON，键序固定，首键即 "usc"
先用索引二分定位（含偏移/长度与首键前缀校验），再 json.loads 出记录。

⚠️ 本地 Windows 检出踩坑：若 git 把 JSONL 的 LF 换行改写成 CRLF（本仓库无 .gitattributes
兜底），文件比索引声明的字节数每行多 1 字节，索引里的偏移随之整体失准。本脚本对此**不报错**，
而是自动降级为「按行扫描建表」，结果与索引路径等价，只在日志里告警一次。
（CI 跑在 Linux 上，一律 LF，走的是索引快路径。）

--------------------------------------------------------------------------------
四、更新规则
--------------------------------------------------------------------------------
两张表都以 usc 关联（取值即映射记录的 usc；futu 表 2026-09 由 finv_quote_futu_collect 改名，
**主键同时由 stockId 改为 usc**）：

    finv_quote_collect_futu   主键 usc     —— 富途采集配置表
        quote_market       ← quoteMarket
        type_symbol        ← typeSymbol
        futu_symbol        ← futuSymbol
        marketType         ← marketType
        marketCode         ← marketCode
        instrumentType     ← instrumentType
        subInstrumentType  ← subInstrumentType
        stockId            ← stockId    （改主键后它只是普通业务列：该表还有这一列才写，见下文核对）
        flag_enable        ← '1'（写死，不取映射记录的 flagEnable）
        dt_update          ← 本次写入时刻

    finv_quote_secu           主键 usc          —— 证券元数据登记表
        region             ← secuRegion
        market             ← secuMarket
        name_sc            ← nameSc
        type_secu          ← typeSecu
        flag_enable        ← '1'
        dt_update          ← 本次写入时刻

⚠️ 两张表都只按 usc 关联（各自的主键，取值即映射记录的 usc），放进 upsert body 的就是这一列。
stockId 只用于富途配置表的 stockId 列与日志 / 留痕展示 —— 它曾经有两个身份，现在都没了：
是那张表的写键（随改名改成 usc）、是 secu 表 `sid` 的取值来源（随 sid 列废弃一并脱钩）。
（历史坑：那时写的是 `stockId = cell(record.get(FUTU_KEY))`，FUTU_KEY 改成 usc 之后它取到的其实是
usc，于是 secu 新建会把 usc 写进 sid、sid 校验也退化成「sid 与 usc 比」，几乎每行都误报。）

写库方式：**每张表一条批量 upsert**（POST + Prefer: resolution=merge-duplicates），不逐行 PATCH ——
每行的取值各不相同，PATCH 只能「一份 body 命中多行」，而 upsert 的多行 body 正是「N 行不同值、
一次请求」的唯一表达方式：

    POST {table}?on_conflict=<主键>
         Prefer: resolution=merge-duplicates,return=representation
         body = [{主键: ..., 待更新字段...}, ...]

    等价 SQL：INSERT INTO {table} (列…) VALUES (…), (…)
              ON CONFLICT (主键) DO UPDATE SET 列 = EXCLUDED.列, …;

提交时按「已在册 / 缺行」分两批（用写入前的现状快照判定，不靠 SQL 里的 CASE 区分）：

    已在册的行 → upsert → 走 DO UPDATE 分支，创建时间不被改写；
    缺行的行   → 依 INPUT_SYNC_ALLOW_INSERT：
                 真：单独一条 upsert 插入（补 dt_create = 本次时刻），插入的行在日志与摘要里
                     **单独列出**，便于事后核查与修正；
                 假（默认）：不插入，写 Verify/MisMatch/{usc}.txt 留痕，人工决定是否补录。

两批不合一条 body 的原因：PostgREST 的 INSERT 与 DO UPDATE SET 共用同一份列清单，body 里带
dt_create 会让**更新**也把创建时间刷成现在。拆开后各自语义干净，且缺行是极少数
（旧表按 stockId 关联时实测 3370 个 Success usc 里只有 11 条缺行；改成按 usc 关联后请以本轮
日志的「表内缺行」计数为准），不会明显增加请求数。

⚠️ upsert 的硬性前提 —— 非空列（实测教训）：INSERT … ON CONFLICT 是**先按 INSERT 分支校验约束、
再判定冲突**的，缺一列「NOT NULL 且无默认值」的列就直接报 23502，即使该行其实命中冲突、
本该只走更新分支。实测 finv_quote_secu 的 region / market 就是这样：只带 flag_enable + dt_update
的 upsert 会 400 失败（旧的逐行 PATCH 写法没有这个约束，所以此前没暴露）。因此：

    启动时读一次 PostgREST OpenAPI（GET /rest/v1/，Accept: application/openapi+json），
    取每张表 required 列里**没有默认值**的那些（即 INSERT 分支必须出现的列，主键除外）：
        finv_quote_collect_futu  → 以实测 schema 为准（改名前是「无」，主键当时是 stockId）
        finv_quote_secu          → region、market
    更新分支：payload 里缺的这些列按**库中现值原样回写**（值不变，只为让约束通过，
              日志里以「= 补列」单列一行）；
    新建分支：这些列必须由映射凑齐，凑不齐就不插入、转留痕（INSERT_REQUIRED_MISSING），
              免得一条坏行把整块 upsert 拖失败。

⚠️ upsert 的第二个前提 —— 列必须真实存在（改名/改建表后最容易踩）：body 或 select 里带上一个
表里没有的列，PostgREST 直接 400（PGRST204 "Could not find the '<列>' column"；select 则是
column does not exist），**整块**（乃至整个读现状阶段）一起失败。故启动时按同一份 schema 核对：

    表不在 schema 里 / 主键列不存在 / flag_enable 或 dt_update 不存在 → 任务级错误（退出 2），
        此时继续跑只会「写不进去」或「写错行」，且失败点散落在一块块 upsert 里，不好排查；
    字段映射里表里没有的列 → 从本轮 payload 与 select 里剔除，并打告警（其余列照常同步）；
    NOT NULL 列不在字段映射里 → 告警（更新分支按库中现值回写，新建分支会转未命中留痕）。
    schema 读不到时以上核对全部跳过，退回既有口径：交给库端约束兜底，失败按块记录在册。

⚠️ upsert 的第三个前提 —— 行键必须一致：PostgREST 的批量 body 要求**每行的键完全相同**，
否则整条请求 400（PGRST102 "All object keys must match"，实测）。而各行的变更字段本来就不同
（A 行改了行情市场、B 行没改），故提交前按「同一批次内 payload 的并集」补列：更新批补库中
现值（值不变），新建批补 null（该列此时必为可空）。这不改变任何真实取值，只让 body 形状一致。

三处刻意的收窄（都是为了「宁可少写，不可写错」）：
    ① 只按主键写、不用业务键：两张表的写键都是各自的主键 usc（唯一的业务标识），不用
       sid / stockId 这类可能重复或已废弃的键去找行 —— 按主键写不可能改错行。
    ② 映射记录里为空值的字段**不写**：整表有 30 条记录缺 nameSc，硬写空值会抹掉库里的现成值，
       故跳过该字段并告警（同一条记录的其余字段照常更新）。
    ③ 「已完全一致」的行不入批：字段全等且 flag_enable 已是 '1' 时整行跳过（连 dt_update
       也不动），这样重复运行几乎零写入，也不会把审计时间戳刷成无意义的噪声。

刻意不碰的列（IGNORED_COLUMNS = type / sid）：
    finv_quote_collect_futu 的 `type` 恒为 '2'；finv_quote_secu 的 `sid` 此前只被当作「按 stockId
    关联」的关联列。两列都**后续计划删除**，故脚本对它们**不读不写** —— 不在 select 里查、不放进
    payload、不做 NOT NULL 补列（新建时同样不补，留库端默认值）、也不做任何一致性校验。
    这样做是为了「脱钩」：列还在时不去动它（本来也不需要本脚本维护）；列一旦删掉，脚本毫无感知、
    照常跑 —— 否则删列那一刻起，每轮 upsert 都会以 PGRST204「找不到列」整块失败。
    唯一的例外是 schema 显示某列**确实是 NOT NULL 且无默认值**：那时只告警提示「新建行可能被库端
    拒绝（23502）」，仍不擅自补写 —— 补了就等于把脚本重新绑死在这一列上。
    将来若还有类似的「库端自维护、脚本不该碰」的列，往 IGNORED_COLUMNS 里加名字即可。

--------------------------------------------------------------------------------
五、PostgREST 调用
--------------------------------------------------------------------------------
照 .github/Python/QuoteCollect/SupabaseJobRepo.py 的 SupabaseRestClient 手法实现最小客户端：
每个请求同时带 apikey 与 Authorization: Bearer；写请求靠 Prefer: return=representation 回读
写入结果，用于统计行数与区分「新增 / 更新」。缺省一轮 6 个请求：

    GET  /rest/v1/  Accept: application/openapi+json   取表结构：列存在性与非空列约束（每轮 1 次，
                                                       两张表共用一份，见第四节的两处 ⚠️）
    GET  {table}?select=<列…>&<主键>=in.(…)            分块取现状（READ_CHUNK = 150 键/请求）
    POST {table}?on_conflict=<主键>                    批量 upsert（WRITE_CHUNK = 200 行/请求）

429/5xx 与网络类错误按指数退避重试（MAX_ATTEMPTS = 3）。一条 INSERT … ON CONFLICT 语句是
**单个事务**：分块内的行要么全写入、要么全不写 —— 与逐行 PATCH 的「每行独立成败」不同，
故失败按「块」记录（表名、动作、该块的键、HTTP 详情），一块失败不影响其余块。

--------------------------------------------------------------------------------
六、未命中与插入
--------------------------------------------------------------------------------
「未命中」的 usc 不滞留在日志里，而是逐一写成 Verify/MisMatch/{usc}.txt 并提交到产物分支
（INPUT_BRANCH，即 quote-meta），文件内载明 usc、stockId、来源 .mvsv、缺哪张表、解析出的全部
查询参数、检测时刻与工作流运行标识、处理建议。判为未命中的情形：

    INDEX_MISS            usc 不在 UscFutuMapping.jsonl.idx 中（无映射记录）
    RECORD_FIELD_MISSING  映射记录缺关键字段（stockId/marketType/marketCode/instrumentType/subInstrumentType）
    FUTU_ROW_MISSING      表里没有该 usc 的行，且本轮未开启自动插入
    SECU_ROW_MISSING      表里没有该 usc 的行，且本轮未开启自动插入
    INSERT_REQUIRED_MISSING  表里缺行、且映射凑不齐该表的非空列（INSERT 分支必填），故本轮不插入

INPUT_SYNC_ALLOW_INSERT 置真时，缺行的证券改为**新建**（不算未命中）。新增行会被单独盯住：
「表内原本没有」由写入前的现状快照判定，落进该表的 inserts 批；写完后按回读结果逐条列出
（步骤日志里以 ＋ 开头、末尾单列一行，运行摘要里也单列一节），便于事后核查与修正。

「主键重复」「表结构漂移导致某列被剔除」等可疑但不阻塞的情况只打告警（⚠），计入摘要的「告警」行，
不产生留痕文件。

--------------------------------------------------------------------------------
七、日志与摘要
--------------------------------------------------------------------------------
每条证券的日志含：来源 .mvsv、usc、quoteMarket、stockId、映射记录取到的全部参数、两张表逐字段的
「旧值 → 新值」、以及「= 保持 / = 补列 / ! 跳过」三类未参与变更的字段。写库阶段改为按**批次**
打印（每表每条语句一行：动作、行数、回读行数、该块的键）；dry-run 时打印等价 SQL
（PostgreSQL 单引号字面量，多行只展开首行 + 「…共 N 行…」，可直接照抄执行）。
步骤结束另写 Actions 运行摘要（$GITHUB_STEP_SUMMARY）。

--------------------------------------------------------------------------------
八、退出码
--------------------------------------------------------------------------------
    0  正常结束（含「概率闸门未命中」与「有未命中记录但已留痕」这两种预期状态）
    1  有写库失败（按块记录，含整块失败）或 MisMatch 提交失败
    2  任务级错误（凭据缺失、映射产物不可用、目录列举失败、参数非法）

--------------------------------------------------------------------------------
九、消费产物
--------------------------------------------------------------------------------
Verify/Success/ 下的 .mvsv 是本脚本的**输入队列**：每轮固定从清单头部取 N 个补齐，若文件用完
不删，下一轮取到的还是同一批（头部永远是那几十个已同步好的证券），队列后面的记录永远轮不上。
故同步完成后要把已消费的文件删掉，让头部自然前移 —— 由 INPUT_SYNC_DELETE 控制（默认关，
工作流里已置 1），删除走 GitHub Contents API（GitHubCommitContent.delete_content）。

删除判据（**同步完成**即删，包含「本来就已经一致、本轮无需变更」的记录，否则它们同样滞留在队头）：

    删：映射可用 → 两表都已写入成功（含「已一致、无需变更」）→ 该记录本轮没有任何失败
    留：未命中（已写 MisMatch 留痕）、所在 upsert 块失败、dry-run、INPUT_SYNC_DELETE 未开启
    留：清单里找不到对应文件（理论上不会发生，只在日志里说明）

删除是**逐个文件一次提交**（与采集脚本的 "Verify xxx.mvsv" 同风格，消息为 "Consumed xxx.mvsv"）：
sha 在目录列举时就已随路径一起取回，直接用于 DELETE，不必再查一次；shas 失配（文件被人抢先改过）
时 GitHub 返 409，按失败记录、不误删。单个文件删除失败不阻塞其余文件，也不会回滚已写入的库行 ——
库是事实来源，文件删不掉只会让该条下一轮再同步一次（幂等，无害）。开启了删除却没有仓库令牌时
直接退出 2（避免「以为删了其实没删」而队列永远不前进）。

⚠️ dry-run 只列出待删文件，不执行删除；INPUT_SYNC_DELETE 未开启时同样只报告「有多少个可消费」。
"""

import datetime
import json
import os
import random
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_NAME = os.path.basename(os.path.abspath(__file__))
#: 与 MisMatch 留痕文件里的「任务脚本（版本 …）」同源：口径变更时递增，便于事后分辨
#: 某份留痕是哪一版写下的。2 = 富途配置表改名 finv_quote_collect_futu / 主键改 usc 之后的版本。
TASK_VERSION = "2"

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 映射表产物（与 VerifyQuoteMinute.py 同一套格式，见模块 docstring 第三节）
IDX_NAME = "UscFutuMapping.jsonl.idx"
JSONL_NAME = "UscFutuMapping.jsonl"
IDX_MAGIC = b"UFI1"
IDX_HEADER, IDX_RECORD, IDX_KEY = 32, 24, 16

# 仓库内路径（相对仓库根）
SUCCESS_DIR = "Verify/Success"
MISMATCH_DIR = "Verify/MisMatch"
MVSV_SUFFIX = ".mvsv"

# 默认参数
DEFAULT_BRANCH = "quote-meta"
DEFAULT_OUT_DIR = "verify-out"
DEFAULT_PROBABILITY = 100
DEFAULT_BATCH_SIZE = 50
DEFAULT_SAMPLE_LIMIT = 47

# ---- 业务策略开关（**定论写在这里**，不依赖各分支工作流里的 INPUT_* env）----
# 删除开关与缺行开关是「定论」而非「调试旋钮」：工作流每个分支各一份、内容还会漂移，
# 把定论放在 env 里会出现「从另一个分支触发 → 静默回落到默认 → 以为开了其实没开」。
# 故此处直接给定论值；同名的 INPUT_* env 仍可覆盖（仅供本地调试，运行时会打印来源）。
#   CONSUME_SUCCESS    同步完成后删除对应的 Verify/Success/{market}/{usc}.mvsv，
#                      让清单头部前移（见 docstring 第九节）
#   ALLOW_INSERT       表内缺行的证券是否允许新建（False = 只写 MisMatch 留痕）
CONSUME_SUCCESS = True
ALLOW_INSERT = False

# 消费 .mvsv 时的提交信息（逐个文件一次提交，与采集脚本 "Verify xxx.mvsv" 同风格）
DELETE_COMMIT_MSG = "Consumed %s"

# Supabase / PostgREST
ENV_SUPABASE_REF = "SUPABASE_PROJECT_REF"
ENV_SUPABASE_KEY = "SUPABASE_KEY"
SUPABASE_REST_BASE = "https://%s.supabase.co/rest/v1"
READ_CHUNK = 150            # 单次 GET 的 id 条数（控制 URL 长度）
WRITE_CHUNK = 200           # 单次 POST 的行数（控制请求体大小；每条语句即一个事务）
HTTP_TIMEOUT = 30
MAX_ATTEMPTS = 3            # 单次请求的最大尝试次数（含首次）
RETRY_BACKOFF = 2.0         # 重试退避基数（秒）：第 n 次退避 RETRY_BACKOFF * 2^(n-1)
RETRYABLE_STATUS = (429, 500, 502, 503, 504)

#: 映射记录里的富途股票 id 字段名。与关联键无关：两张表都按 usc 写，它只用于富途配置表的
#: stockId 列、以及日志 / 留痕展示 —— 取值必须是映射记录的 stockId，不能再借 FUTU_KEY 代取
#: （FUTU_KEY 是 usc，`record.get(FUTU_KEY)` 拿到的是 usc，不是 stockId）。
STOCK_ID_FIELD = "stockId"

# 更新规则（字段口径见模块 docstring 第四节）
# ⚠️ 富途采集配置表 2026-09 由 finv_quote_futu_collect 改名为 finv_quote_collect_futu，
#    **主键同时由 stockId 改为 usc**：故 FUTU_KEY 与 SECU_KEY 同为 "usc"，两张表都以 usc 关联。
#    stockId 随之降级为普通业务列（映射记录里仍有），该表确实还有这一列时才同步。
FUTU_TABLE = "finv_quote_collect_futu"
FUTU_KEY = "usc"
FUTU_FIELDS = (("usc", "usc"), ("quote_market", "quoteMarket"), ("type_symbol", "typeSymbol"),
               ("futu_symbol", "futuSymbol"), ("marketType", "marketType"),
               ("marketCode", "marketCode"), ("instrumentType", "instrumentType"),
               ("subInstrumentType", "subInstrumentType"),
               # 不再是关联键，但仍是该表的业务列：有就同步，没有则由列存在性核对剔除
               (STOCK_ID_FIELD, STOCK_ID_FIELD))

SECU_TABLE = "finv_quote_secu"
SECU_KEY = "usc"
SECU_FIELDS = (("region", "secuRegion"), ("market", "secuMarket"),
               ("name_sc", "nameSc"), ("type_secu", "typeSecu"))

#: 映射记录涉及的字段（解析输出、MisMatch 留痕、必要字段校验共用同一口径）
RECORD_FIELDS = ("usc", "stockId", "typeSymbol", "quoteMarket", "futuSymbol", "marketType",
                 "marketCode", "instrumentType", "subInstrumentType", "nameSc",
                 "typeSecu", "secuRegion", "secuMarket")

#: 缺任一字段即无法确定完整查询参数（stockId 另用于富途配置表的 stockId 列）：
#: 命中即整条转 MisMatch 留痕、不做部分更新；非关键字段缺值只在单列层面跳过并告警。
CRITICAL_FIELDS = ("stockId", "marketType", "marketCode", "instrumentType", "subInstrumentType")

ENABLE_COLUMN = "flag_enable"
ENABLE_VALUE = "1"
DT_UPDATE_COLUMN = "dt_update"
#: 仅在**新建**时写入：创建时间（更新时写它会抹掉首次登记时间，见 docstring 第四节）
DT_CREATE_COLUMN = "dt_create"
#: 刻意**不读不写**的列：由库端 / 其它流程维护，本脚本一律不碰 ——
#: 既不出现在 select 里、也不进 upsert 的 body，更不做 NOT NULL 补列。
#:   type —— finv_quote_collect_futu 该列恒为 '2'，且后续计划删除；
#:   sid  —— finv_quote_secu 该列后续计划删除（此前只被当作「按 stockId 关联」的关联列）。
#: 脚本若还引用它们，删列之后每轮都会以 PGRST204（找不到列）整块失败，故提前脱钩：
#: 列还在时不动它（本来也不需要本脚本维护），列删掉后也毫无影响。
IGNORED_COLUMNS = ("type", "sid")

#: 各表新建时补的常量列（全表同值；更新时不写）。
#: 目前两张表都没有要补的常量列：type / sid 已归入 IGNORED_COLUMNS（都计划删除），
#: 不再由本脚本补 —— 留库端默认值即可。保留这张表作为扩展点：
#: ⚠️ 写在这里的列名是按**库中现状**写死的，表里若已没有该列，会被启动时的列存在性核对
#:    剔除并告警，不会让整块 upsert 因 PGRST204（找不到列）而 400。
INSERT_CONSTANTS = {
    FUTU_TABLE: {},                 # 无
    SECU_TABLE: {},                 # secu 表其余列（timezone/provider/day_incr_max 等）留库端默认
}

# 未命中分类（写进 MisMatch 文件的「原因」段）
MISS_INDEX = "INDEX_MISS"
MISS_RECORD_FIELD = "RECORD_FIELD_MISSING"
MISS_FUTU_ROW = "FUTU_ROW_MISSING"
MISS_SECU_ROW = "SECU_ROW_MISSING"
MISS_INSERT_REQUIRED = "INSERT_REQUIRED_MISSING"

MISS_REASONS = {
    MISS_INDEX: "usc 不在 UscFutuMapping.jsonl.idx 中（无映射记录，解析不出查询参数）",
    MISS_RECORD_FIELD: "映射记录缺关键字段，拼不出完整请求参数",
    MISS_FUTU_ROW: "表 finv_quote_collect_futu 中不存在该 usc 的行（本轮未开启自动插入）",
    MISS_SECU_ROW: "表 finv_quote_secu 中不存在该 usc 的行（本轮未开启自动插入）",
    MISS_INSERT_REQUIRED: "表内缺行且映射凑不齐非空列（该表本轮不写，避免整块 upsert 失败）",
}

RETURN_OK, RETURN_FAILED, RETURN_TASK_ERROR = 0, 1, 2

CHINA_TZ = datetime.timezone(datetime.timedelta(hours=8))


class TaskError(Exception):
    """任务级错误：凭据缺失、映射产物不可用、目录列举失败等，退出码 2。"""


# ---------------------------------------------------------------------------
# 环境变量与通用小工具
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


def policyFlag(name, constant):
    """读「业务策略」开关：**常量即定论**，同名 env 仅供本地调试覆盖。

    工作流每个分支各一份、内容还会漂移，把定论放在 env 里会出现
    「从另一个分支触发 → 静默回落到默认 → 以为开了其实没开」。故这里以常量常量值为准，
    env 只在**显式存在**时覆盖；同时把「值是多少、从哪儿来」一并返回，供启动日志打印。

    :return: (取值 bool, 来源 str)；来源形如 "脚本常量 CONSUME_SUCCESS=True"
             或 "env INPUT_SYNC_DELETE=1（覆盖脚本常量 CONSUME_SUCCESS=True）"。
    """
    raw = env(name)
    if not raw:
        return constant, "脚本常量 %s=%s" % (name, "1" if constant else "0")
    value = raw.lower() in ("1", "true", "yes", "on")
    return value, "env %s=%s（覆盖脚本常量）" % (name, raw)


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


def chunks(raw):
    """把逗号 / 空白分隔的文本切成去重后的列表，保持原序。"""
    words = str(raw or "").replace("，", " ").replace(",", " ").split()
    return list(dict.fromkeys(words))


def nowChina():
    """当前 UTC+8 时刻，格式化为 2026-09-23 14:32:10。"""
    return datetime.datetime.now(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def nowUtcIso():
    """当前 UTC 时刻（ISO8601，秒精度）——写入 dt_update。"""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def cell(value):
    """取值转字符串（空值一律空串）：库里的 varchar 与映射里的 int 按同一口径比较。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def sqlLiteral(value):
    """把 Python 值渲染成 PostgreSQL 字面量（dry-run 打印的 SQL 可直接照抄执行）。

    PostgreSQL 的字符串字面量是单引号，双引号是标识符语义，故不能用 json.dumps。
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'%s'" % str(value).replace("'", "''")


def shown(value):
    """日志里展示取值：空串显示为 (空)。"""
    return sqlLiteral(value) if cell(value) else "(空)"


def annotate(message, level="warning"):
    """GitHub Actions 注解；本地运行时退化为普通打印。"""
    if env("GITHUB_ACTIONS"):
        print("::%s::%s" % (level, message.replace("\n", " ")), flush=True)
    else:
        print("[%s] %s" % (level.upper(), message), flush=True)


# ---------------------------------------------------------------------------
# 映射表：按 usc 取查询参数
# ---------------------------------------------------------------------------


class Mapping:
    """UscFutuMapping.jsonl.idx + JSONL：按 usc 取记录（索引快路径 + 行扫描兜底）。"""

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
        # 字节数不配对时**不报错**：典型成因是本地检出把 LF 换成了 CRLF（见 docstring 第三节）
        self.indexUsable = len(self.jsonl) == struct.unpack_from("<Q", self.raw, 12)[0]
        self._scanned = None

    def describe(self):
        """给日志用的一行自描述。"""
        return "%d 条，索引摘要 %s（%s）" % (
            self.count, self.digest, "索引快路径" if self.indexUsable else "行扫描兜底")

    def lookup(self, usc):
        """按 usc 取映射记录；未命中返回 None。"""
        if self.indexUsable:
            record, degraded = self._lookupByIndex(usc)
            if degraded:
                print("  [告警] 索引偏移失准（%s），本轮降级为按行扫描；"
                      "常见成因是检出时 LF 被改写为 CRLF" % usc, flush=True)
            else:
                return record
        return self._scan().get(usc)

    def _lookupByIndex(self, usc):
        """二分定位并解析；偏移失准（首键前缀不符）时返回 (None, True) 要求降级。"""
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
                    self.indexUsable = False
                    return None, True
                return json.loads(line.decode("utf-8")), False
            low, high = (mid + 1, high) if current < key else (low, mid - 1)
        return None, False

    def _scan(self):
        """按行扫描建表（每个进程只建一次）。"""
        if self._scanned is None:
            table = {}
            for line in self.jsonl.decode("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                table[record.get("usc")] = record
            self._scanned = table
        return self._scanned


# ---------------------------------------------------------------------------
# Supabase：PostgREST 最小客户端（读现状 / 逐行 PATCH）
# ---------------------------------------------------------------------------


class SupabaseRestError(Exception):
    """Supabase Data API 调用失败。"""


class SupabaseClient:
    """PostgREST 最小客户端（仅标准库），手法同 QuoteCollect/SupabaseJobRepo.py。

    每个请求同时携带 apikey 与 Authorization: Bearer（Supabase 要求二者并存）；
    429/5xx 与网络类错误按指数退避重试，其余错误直接抛出。
    """

    def __init__(self, projectRef, apiKey, timeout=HTTP_TIMEOUT):
        if not projectRef:
            raise TaskError("Supabase 项目引用（%s）不能为空" % ENV_SUPABASE_REF)
        if not apiKey:
            raise TaskError("Supabase API 密钥（%s）不能为空" % ENV_SUPABASE_KEY)
        base = env("SUPABASE_REST_BASE") or (SUPABASE_REST_BASE % projectRef)
        self.restUrl = base.rstrip("/")
        self.apiKey = apiKey
        self.timeout = timeout
        self.requests = 0
        #: PostgREST OpenAPI 缓存：None = 还没读；False = 读不到（本轮不做列核对）；dict = 读到了
        self._spec = None

    def _request(self, method, table, query, body=None, prefer=None, accept=None):
        """发一次请求（含重试），返回 (状态码, 响应文本)。"""
        url = "%s/%s" % (self.restUrl, urllib.parse.quote(table, safe=""))
        if query:
            url += "?" + query
        headers = {"apikey": self.apiKey, "Authorization": "Bearer %s" % self.apiKey,
                   "Accept": accept or "application/json"}
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
                    raise SupabaseRestError(last) from exc
            except OSError as exc:
                last = "网络错误：%s" % exc
                if attempt >= MAX_ATTEMPTS:
                    raise SupabaseRestError(last) from exc
            time.sleep(RETRY_BACKOFF * (2 ** (attempt - 1)))
        raise SupabaseRestError(last or "未知错误")

    def selectByKeys(self, table, keyColumn, keys, columns):
        """按主键批量取现状，返回 {键: [行, ...]}（同键多行时列表长度大于 1）；分块以免 URL 过长。"""
        result = {}
        ordered = sorted({str(item) for item in keys}, key=lambda item: (len(item), item))
        for start in range(0, len(ordered), READ_CHUNK):
            chunk = ordered[start:start + READ_CHUNK]
            query = "select=%s&%s=in.(%s)" % (",".join(columns), keyColumn, ",".join(chunk))
            status, body = self._request("GET", table, query)
            if status != 200:
                raise SupabaseRestError("GET %s 返回 HTTP %s：%s" % (table, status, body[:300]))
            try:
                rows = json.loads(body or "[]")
            except ValueError as exc:
                raise SupabaseRestError("GET %s 响应不是合法 JSON：%s" % (table, exc)) from exc
            if not isinstance(rows, list):
                raise SupabaseRestError("GET %s 响应不是行数组" % table)
            for row in rows:
                if not isinstance(row, dict) or row.get(keyColumn) is None:
                    continue
                result.setdefault(str(row[keyColumn]), []).append(row)
        return result

    def _openapi(self):
        """读一次 PostgREST OpenAPI（整轮缓存），返回 spec；读不到返回 False。

        整轮只读一次：写库前要用它核对列口径（inspectSchema），而补列与列过滤都要用同一份。
        读不到（权限/网络/网关异常）不抛错：本轮降级为「不做列核对」，交给库端约束兜底。
        """
        if self._spec is None:
            try:
                _status, body = self._request("GET", "", None, accept="application/openapi+json")
                spec = json.loads(body or "{}")
                self._spec = spec if isinstance(spec, dict) else False
            except (SupabaseRestError, ValueError) as exc:
                print("  [告警] 读取 PostgREST OpenAPI 失败（%s）：本轮跳过列口径核对"
                      "（表名/主键/列存在性都不校验），交给库端约束兜底" % exc, flush=True)
                self._spec = False
        return self._spec

    def tableSchema(self, table):
        """取一张表的 (实际列集合, NOT NULL 且无默认值的列集合)。

        返回值里 columns 的三种形态含义不同，调用方必须区分：
            列集合（非空）  schema 里找到了这张表；
            空集合          schema 可用但**没有这张表**（表名错/未暴露给 Data API）；
            None            schema 根本读不到（本轮不做任何列核对）。
        """
        spec = self._openapi()
        if not spec:
            return None, set()
        definition = (spec.get("definitions") or {}).get(table) or {}
        properties = definition.get("properties") or {}
        if not properties:
            return set(), set()
        required = {name for name in (definition.get("required") or [])
                    if name in properties and properties[name].get("default") is None}
        return set(properties), required

    def requiredColumns(self, table, keyColumn):
        """取表里「NOT NULL 且无默认值」的列（PostgREST OpenAPI 的 required − 有默认值 − 主键）。

        UPSERT 的硬性前提（主流程入口是 inspectSchema，它一次拿全 columns + required）：
        批量 upsert 是先按下 **INSERT 分支校验约束**、再判定冲突的：只要缺一列 NOT NULL 且无默认值
        的列，即使该行其实命中冲突、本该只走更新分支，也会直接报 23502（实测 finv_quote_secu 的
        region / market 就是这样）。所以带上这些列是 upsert 的硬性前提，更新分支也一样带，
        取值用库中现值原样回写（等价于不动该列）。

        取不到 schema（权限/网络/表未暴露）时返回空集：交给库端约束兜底，失败按块记录在册。
        """
        _columns, required = self.tableSchema(table)
        return {name for name in required if name != keyColumn}   # 主键由写请求自己带上

    def upsert(self, table, rows, conflictColumn):
        """批量 upsert（POST + Prefer: resolution=merge-duplicates），返回 (写入行, 失败块)。

        有则按主键更新、无则新建，两者是同一条语句的两个分支。分块提交（WRITE_CHUNK 行/请求），
        单条 SQL 即一个事务：某块失败只丢该块（记入失败列表后继续下一块），不会影响其它块。
        """
        written, failed = [], []
        for start in range(0, len(rows), WRITE_CHUNK):
            chunk = rows[start:start + WRITE_CHUNK]
            keys = [cell(row.get(conflictColumn)) for row in chunk]
            body = json.dumps(chunk, ensure_ascii=False).encode("utf-8")
            query = "on_conflict=%s" % urllib.parse.quote(conflictColumn, safe="")
            try:
                status, response = self._request(
                    "POST", table, query, body,
                    prefer="resolution=merge-duplicates,return=representation")
                if status not in (200, 201, 204):
                    raise SupabaseRestError("POST 返回 HTTP %s：%s" % (status, response[:300]))
                back = json.loads(response) if response.strip() else []
                if not isinstance(back, list):
                    raise SupabaseRestError("响应不是行数组")
            except (SupabaseRestError, ValueError) as exc:
                failed.append((keys, str(exc)))
                continue
            written.extend(row for row in back if isinstance(row, dict))
        return written, failed


# ---------------------------------------------------------------------------
# 仓库侧：列举 Verify/Success/、提交 MisMatch 文件
# ---------------------------------------------------------------------------


class RepoAccess:
    """GitHub Contents API 的最小封装：递归列目录 + 单文件提交 + 单文件删除。

    复用同目录 GitHubCommitContent.py 的 _request / _auth_headers / commit_content_file /
    delete_content，与仓库内其它脚本保持同一套鉴权与提交口径。
    列举一律返回 [(仓库路径, blob sha)]：sha 是删除文件时必须回传的版本凭据，
    顺手从目录树带回来，省掉删除前逐文件再查一次的开销。
    """

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
        self.apiBase = GitHubCommitContent.DEFAULT_API_BASE

    @property
    def usable(self):
        """是否有仓库身份与令牌（缺任一项都做不了线上列举/提交）。"""
        return bool(self.owner and self.repo and self.token)

    def _listDir(self, path):
        """列一个目录（不递归），返回 [{type,name,path}]；目录不存在返回 None。

        ⚠️ Contents API 对**单个目录**一次最多返回 1000 条，超出的部分必须翻页取，
        否则会静默截断（本仓库 Verify/Success/HK 与 SZ 都在 1600 条以上，实测漏掉近 4 成）。
        """
        entries, page = [], 1
        while True:
            url = "%s/%s/%s/contents/%s?ref=%s&per_page=100&page=%d" % (
                self.apiBase, self.owner, self.repo,
                urllib.parse.quote(path, safe="/"), urllib.parse.quote(self.branch, safe=""), page)
            status, body, error = self.lib._request(
                "GET", url, self.lib._auth_headers(self.token), timeout=HTTP_TIMEOUT)
            if error:
                raise TaskError("列举 %s@%s 失败：%s" % (path, self.branch, error))
            if status == 404:
                return None if page == 1 else entries
            if status != 200:
                raise TaskError("列举 %s@%s 返回 HTTP %s：%s"
                                % (path, self.branch, status, (body or "")[:200]))
            data = self.lib._parse_json(body)
            if not isinstance(data, list):
                raise TaskError("列举 %s@%s 的响应不是目录数组" % (path, self.branch))
            entries.extend(item for item in data if isinstance(item, dict))
            if len(data) < 100:
                return entries
            page += 1

    def _listByTree(self):
        """用 Git Trees API 一次取回整棵目录树（只取 blob），返回 [(路径, sha)]。

        比逐目录列举快得多（1 次请求），是首选路径；返回 None 表示不可用、需降级。
        sha 一并带回来：删除文件时 Contents API 必须带上它，免得再逐文件查一次。
        """
        url = "%s/%s/%s/git/trees/%s?recursive=1" % (
            self.apiBase, self.owner, self.repo, urllib.parse.quote(self.branch, safe=""))
        status, body, error = self.lib._request(
            "GET", url, self.lib._auth_headers(self.token), timeout=HTTP_TIMEOUT)
        if error or status != 200:
            print("  [告警] Git Trees 列举 %s@%s 不可用（%s），降级为逐目录列举"
                  % (self.branch, self.branch, error or ("HTTP %s" % status)), flush=True)
            return None
        data = self.lib._parse_json(body)
        if not isinstance(data, dict) or not isinstance(data.get("tree"), list):
            print("  [告警] Git Trees 响应不是目录树，降级为逐目录列举", flush=True)
            return None
        if data.get("truncated"):
            print("  [告警] Git Trees 响应被截断（仓库树过大），降级为逐目录列举", flush=True)
            return None
        return [(item.get("path"), item.get("sha")) for item in data["tree"]
                if isinstance(item, dict) and item.get("type") == "blob" and item.get("path")]

    def _listByContents(self, root):
        """逐目录递归列举（无需 Trees API 权限时的兜底路径），返回 [(路径, sha)]。"""
        found, pending = [], [root]
        while pending:
            path = pending.pop(0)
            entries = self._listDir(path)
            if entries is None:
                if path == root:
                    raise TaskError("目录不存在：%s@%s" % (root, self.branch))
                continue
            for entry in entries:
                kind, entryPath = entry.get("type"), entry.get("path") or ""
                if kind == "dir":
                    pending.append(entryPath)
                elif kind == "file" and entryPath.endswith(MVSV_SUFFIX):
                    found.append((entryPath, entry.get("sha")))
        return found

    def listMvsv(self, root):
        """递归列举 root 下全部 .mvsv，返回按仓库路径升序的 [(路径, blob sha)]。"""
        prefix = root.rstrip("/") + "/"
        entries = self._listByTree()
        if entries is None:
            entries = self._listByContents(root)
        found = sorted(((path, sha) for path, sha in entries
                        if path.startswith(prefix) and path.endswith(MVSV_SUFFIX)),
                       key=lambda item: item[0])
        if not found:
            raise TaskError("目录 %s@%s 下一个 .mvsv 都没有（路径口径是否变了？）" % (root, self.branch))
        return found

    def commitFile(self, path, localFile, message):
        """提交单个本地文件到产物分支；返回结果 dict。"""
        return self.lib.commit_content_file(path, localFile, branch=self.branch,
                                            commit_msg=message, owner=self.owner, repo=self.repo)

    def deleteFile(self, path, sha, message):
        """删除产物分支上的单个文件（Contents API DELETE）；返回结果 dict。

        调用方已从目录树拿到 sha，故无需再查一次；sha 对不上（被人抢先改过）时 GitHub 返 409，
        按失败返回、由调用方记录，不会误删。
        """
        return self.lib.delete_content(path, branch=self.branch, commit_msg=message,
                                       sha=sha, owner=self.owner, repo=self.repo)


def localSuccessFiles(root):
    """本地工作树兜底：无令牌/离线时按同一口径扫描检出目录。

    仅用于本机自测与线上列举不可用时的降级；线上以 Contents API 列举为准。
    与线上口径一致返回 [(仓库路径, sha)]，本地拿不到 blob sha，故 sha 一律为 None
    （删除时由 GitHubCommitContent 现查一次，见其 delete_content）。
    """
    base = os.path.join(os.getcwd(), root.replace("/", os.sep))
    if not os.path.isdir(base):
        return None
    found = []
    for dirPath, _dirNames, fileNames in os.walk(base):
        for name in fileNames:
            if name.endswith(MVSV_SUFFIX):
                rel = os.path.relpath(os.path.join(dirPath, name), os.getcwd())
                found.append((rel.replace(os.sep, "/"), None))
    found.sort()
    return found


# ---------------------------------------------------------------------------
# 差异计算与留痕正文
# ---------------------------------------------------------------------------


def inspectSchema(client):
    """核对两张表的列口径，返回 ({表名: NOT NULL 列}, {表名: 实际列集合|None}, [告警])。

    为什么要在写库前先核对一次表结构：表名 / 主键 / 列口径一旦与脚本不一致，继续跑只有两种下场 ——
    「根本写不进去」（每块 upsert 400 / 23502，失败点散落在一块块日志里）或「写错行」。
    富途配置表刚由 finv_quote_futu_collect 改名并换主键（stockId → usc），正是最该当场核对的时候：

        表不在 schema 里           → 任务级错误（退出 2）：表名写错，或没暴露给 Data API；
        主键列不在表里             → 任务级错误：upsert 的 on_conflict 必须落在真实主键上；
        flag_enable/dt_update 缺列 → 任务级错误：写入口径与库完全不符，继续跑只会写出错误结论；
        字段映射里的列不存在       → 从本轮写入中剔除并告警（其余列照常同步，不会整块失败）；
        NOT NULL 列不在字段映射里  → 告警（更新分支按库中现值回写，新建分支会转成未命中留痕）。

    例外是 IGNORED_COLUMNS（如 type）：它们既不进 select 也不进 payload，连 NOT NULL 补列都不做 ——
    库端该列有默认值时一切照旧；真要是「NOT NULL 且无默认值」，补列反而会把脚本和这一列绑死，
    删列时立刻报 PGRST204。故只对这种情况留一条告警（新建行可能被库端拒绝），由人工决定。

    schema 读不到时一律返回 None 并跳过全部核对 —— 与既有降级口径一致，交给库端约束兜底。
    """
    required, columns, warns = {}, {}, []
    for table, key, fields in ((FUTU_TABLE, FUTU_KEY, FUTU_FIELDS),
                               (SECU_TABLE, SECU_KEY, SECU_FIELDS)):
        actual, tableRequired = client.tableSchema(table)
        columns[table] = actual
        required[table] = {name for name in tableRequired
                           if name != key and name not in IGNORED_COLUMNS}
        if actual is None:
            continue
        wanted = [column for column, _ in fields]
        if not actual:
            raise TaskError("PostgREST schema 里没有表 %s（脚本期望主键 %s、列 %s）："
                            "表是否已改名、或未暴露给 Data API？"
                            % (table, key, "、".join(wanted)))
        if key not in actual:
            raise TaskError("表 %s 没有主键列 %s（实际列：%s）—— 脚本按该列 upsert，口径必须一致"
                            % (table, key, "、".join(sorted(actual))))
        for column in (ENABLE_COLUMN, DT_UPDATE_COLUMN):
            if column not in actual:
                raise TaskError("表 %s 没有列 %s（实际列：%s）—— 写入口径与库不符，"
                                "继续跑只会写出无法判定的结果"
                                % (table, column, "、".join(sorted(actual))))
        missing = [column for column in wanted if column not in actual]
        if len(missing) == len(wanted):
            raise TaskError("表 %s 里没有一个期望列存在（期望 %s；实际 %s）—— 表结构是否已大改？"
                            % (table, "、".join(wanted), "、".join(sorted(actual))))
        if missing:
            warns.append("表 %s 缺列 %s：本轮跳过这些字段（表结构已漂移，请核对脚本里的字段映射）"
                         % (table, "、".join(missing)))
        for column in sorted(required[table]):
            if column not in wanted:
                warns.append("表 %s 的 %s 是 NOT NULL 且无默认值，却不在字段映射里："
                             "更新分支按库中现值回写、新建分支会转成未命中留痕" % (table, column))
        for column in sorted(set(tableRequired) & set(IGNORED_COLUMNS)):
            warns.append("表 %s 的 %s 属刻意忽略列（本脚本不读不写）：它现在是 NOT NULL 且无默认值，"
                         "新建行可能被库端拒绝（23502）—— 请确认库端已给它默认值或允许为空"
                         % (table, column))
    return required, columns, warns


def describeTable(table, key, columns, required):
    """给日志用的一行表结构自描述：主键、列数、NOT NULL 无默认值的列。"""
    actual = columns.get(table)
    if actual is None:
        return "schema 不可用，本轮跳过列核对（主键 %s）" % key
    return "主键 %s，%d 列，NOT NULL 无默认值：%s" % (
        key, len(actual), "、".join(sorted(required.get(table, ()))) or "无")


def selectableColumns(names, allowed):
    """按表实际列过滤 select 清单（allowed=None = schema 不可用，一律不过滤）。

    ⚠️ select 里只要带一个表里不存在的列，PostgREST 就直接 400（column does not exist），
    整轮连现状都读不回来。故列口径漂移时先过滤再发请求：读不到该列，顶多当作「库里没这个字段」，
    比整轮读失败好排查得多。主键与 flag_enable / dt_update 已由 inspectSchema 保证存在。
    """
    ordered = list(dict.fromkeys(names))
    if allowed is None:
        return ordered
    return [name for name in ordered if name in allowed]


def loadCurrentState(client, records, required, columns=None):
    """批量取两张表的现状行，返回 ({主键: [行]}, {主键: [行]})。

    两张表的键都是**主键 usc**（futu 表改名前是 stockId，随改名一并改口径）—— upsert 的冲突判定
    也走同一个键，读与写的口径因此完全一致。多取的列只用于校验与日志，其中 required
    （NOT NULL 且无默认值）必须读回来，更新分支要把它们的现值一并写回去
    （见 SupabaseClient.requiredColumns）。

    刻意不查 IGNORED_COLUMNS 里的列（type / sid）：它们都由库端维护、且计划删除，
    读回来既没用，删列之后还会让这条 select 直接 400。
    """
    columns = columns or {}
    futuColumns = [FUTU_KEY, DT_UPDATE_COLUMN] + [column for column, _ in FUTU_FIELDS] + [ENABLE_COLUMN]
    secuColumns = [SECU_KEY, DT_UPDATE_COLUMN] \
        + [column for column, _ in SECU_FIELDS] + [ENABLE_COLUMN]
    futuColumns += sorted(required.get(FUTU_TABLE, ()))
    secuColumns += sorted(required.get(SECU_TABLE, ()))
    futuKeys = [cell(record.get(FUTU_KEY)) for record in records]
    secuKeys = [cell(record.get(SECU_KEY)) for record in records]
    futuRows = client.selectByKeys(FUTU_TABLE, FUTU_KEY, futuKeys,
                                   selectableColumns(futuColumns, columns.get(FUTU_TABLE)))
    secuRows = client.selectByKeys(SECU_TABLE, SECU_KEY, secuKeys,
                                   selectableColumns(secuColumns, columns.get(SECU_TABLE)))
    return futuRows, secuRows


def buildPayload(spec, record, current, stamp):
    """算出某张表要写的字段（不含主键、不含 dt_create）。

    :param spec: {"table", "key", "fields"} —— 更新规则。
    :param record: 映射记录。
    :param current: 库中现状行；**本次要新建的行传 {}**（所有有值的字段都会进 payload）。
    :param stamp: dt_update 取值。
    :return: (payload, changed, unchanged, skipped)
        payload 为空字典 = 无需变更；changed/unchanged/skipped 仅用于日志。
    """
    payload, changed, unchanged, skipped = {}, [], [], []
    for column, source in spec["fields"]:
        want = cell(record.get(source))
        have = cell(current.get(column))
        if not want:
            # 映射缺值：保留库中原值（见 docstring 第四节 ②）
            skipped.append((column, source, have))
        elif want == have:
            unchanged.append("%s=%s" % (column, have))
        else:
            payload[column] = want
            changed.append((column, source, have, want))
    if cell(current.get(ENABLE_COLUMN)) != ENABLE_VALUE:
        payload[ENABLE_COLUMN] = ENABLE_VALUE
        changed.append((ENABLE_COLUMN, "(固定值)", cell(current.get(ENABLE_COLUMN)), ENABLE_VALUE))
    if payload:
        # dt_update 只在确有变更时盖章：无变更的行走不到这里（见 docstring 第四节 ③）
        payload[DT_UPDATE_COLUMN] = stamp
        changed.append((DT_UPDATE_COLUMN, "(本次时间戳)", cell(current.get(DT_UPDATE_COLUMN)), stamp))
    return payload, changed, unchanged, skipped


def upsertSql(table, keyColumn, rows):
    """渲染批量 upsert 的等价 SQL（dry-run 打印 + 日志追溯两用）。

    行数多时只展开首行：全量展开在日志里没有可读性，而 upsert 的语义就是「同一形状的 N 行」。
    """
    if not rows:
        return ""
    columns = [keyColumn] + [name for name in rows[0] if name != keyColumn]
    tuples = []
    for row in rows[:1]:
        tuples.append("(%s)" % ", ".join(sqlLiteral(row.get(name)) for name in columns))
    if len(rows) > 1:
        tuples.append("(…共 %d 行…)" % len(rows))
    sets = ", ".join("%s = EXCLUDED.%s" % (name, name) for name in columns if name != keyColumn)
    return ("INSERT INTO %s (%s) VALUES %s ON CONFLICT (%s) DO UPDATE SET %s;"
            % (table, ", ".join(columns), ", ".join(tuples), keyColumn, sets))


def planRow(record, futuRows, secuRows, stamp, allowInsert, required, columns=None):
    """算出一条证券对两张表的写计划。

    :param required: {表名: {NOT NULL 且无默认值的列}}，见 SupabaseClient.requiredColumns。
    :param columns: {表名: 该表实际列集合 | None}，见 inspectSchema；None = schema 不可用、不过滤。
    :return: (plans, misses, warns)
        plans  = [{"table","key","keyValue","payload","changed","unchanged","skipped","fill","insert"}]
                 payload 为空 = 无需变更（不入批）；insert = 该行表里没有，走新建分支；
        misses = 未命中分类码列表（该表不写，其余表照常写）；
        warns  = 可疑但不阻塞的告警（如主键重复、表结构漂移导致某列被剔除）。

    ⚠️ 两张表都只按 **usc**（各自的主键）关联，取值即映射记录的 usc。曾经这里写的是
    `stockId = cell(record.get(FUTU_KEY))` 并拿它当两张表的键：FUTU_KEY 由 stockId 改成 usc 之后，
    它取到的其实是 usc，于是 secu 新建会把 usc 写进 sid、sid 校验也变成「sid 与 usc 比」（几乎
    每行都误报）。现在 sid 已随「后续删除该列」一并脱钩（见 IGNORED_COLUMNS），本函数不再碰它：
    不查、不写、也不做一致性校验；stockId 只用于富途配置表的 stockId 列与日志 / 留痕展示。
    """
    futuKey = cell(record.get(FUTU_KEY))
    secuKey = cell(record.get(SECU_KEY))
    columns = columns or {}
    plans, misses, warns = [], [], []

    for spec in ({"table": FUTU_TABLE, "key": FUTU_KEY, "fields": FUTU_FIELDS,
                  "keyValue": futuKey, "current": futuRows},
                 {"table": SECU_TABLE, "key": SECU_KEY, "fields": SECU_FIELDS,
                  "keyValue": secuKey, "current": secuRows}):
        table, key, keyValue = spec["table"], spec["key"], spec["keyValue"]
        allowed = columns.get(table)            # None = schema 不可用，不做列存在性过滤
        rows = spec["current"].get(keyValue) or []
        if not rows and not allowInsert:
            misses.append(MISS_FUTU_ROW if table == FUTU_TABLE else MISS_SECU_ROW)
            continue
        if len(rows) > 1:
            warns.append("%s 中 %s=%s 出现 %d 行（主键重复，库端约束异常），按第一行算差异"
                         % (table, key, keyValue, len(rows)))
        current = rows[0] if rows else {}
        payload, changed, unchanged, skipped = buildPayload(spec, record, current, stamp)
        fill = []
        if payload and rows:
            # upsert 的 INSERT 分支要求这些列在场：按库中现值原样回写（值不变，只是让约束通过）
            for column in sorted(required.get(table, ())):
                if column in payload or not cell(current.get(column)):
                    continue
                payload[column] = cell(current.get(column))
                fill.append((column, cell(current.get(column))))
        if not rows:
            # 新建：只补创建时间与常量列（NOT NULL 是否凑得齐在过滤之后统一校验）
            payload[DT_CREATE_COLUMN] = stamp
            changed.append((DT_CREATE_COLUMN, "(新建补值)", "", stamp))
            for column, value in sorted(INSERT_CONSTANTS.get(table, {}).items()):
                payload[column] = value
                changed.append((column, "(固定值)", "", value))
        if allowed is not None:
            # 列存在性过滤：表里没有的列一律不写，否则整块 upsert 会 400（PGRST204 找不到列）
            for column in sorted(set(payload) - allowed):
                payload.pop(column)
                changed = [item for item in changed if item[0] != column]
                warns.append("表 %s 里没有列 %s：本轮不写该列（表结构口径可能已变，请核对）"
                             % (table, column))
        if not rows:
            lack = sorted(column for column in required.get(table, ())
                          if column not in payload or not cell(payload.get(column)))
            if lack:
                # 凑不齐就得整块 upsert 失败（23502），不如按条留痕
                misses.append("%s:%s" % (MISS_INSERT_REQUIRED, "、".join(lack)))
                continue
        plans.append({"table": table, "key": key, "keyValue": keyValue, "usc": secuKey,
                      "payload": payload, "changed": changed, "unchanged": unchanged,
                      "skipped": skipped, "fill": fill, "insert": not rows, "current": current})
    return plans, misses, warns


def buildBatches(rowPlans):
    """把逐行计划汇总成「每表两批」的写入批次。

    :param rowPlans: [(usc, record, plans), ...]
    :return: [{"table","conflict","updates":[行,…],"inserts":[行,…],"uscOf":{主键: usc}}]
        已在册的行进 updates，缺行的行进 inserts（新建要补的常量列已在 planRow 里补齐）。

    ⚠️ PostgREST 的批量 body 要求**每行的键完全一致**，否则整条请求 400（PGRST102
    "All object keys must match" —— 实测）。而各行的变更字段本就各不相同（A 行改了行情市场、
    B 行没改），故同一个批次内按各行 payload 的**并集**补列：更新批补库中现值（值不变），
    新建批补 null（该列此时必为可空 —— 非空列凑不齐的行已在 planRow 里转留痕；代价是这些列
    拿不到库端默认值，故开启插入时务必复核新增行）。补列只改 body 形状，不改真实取值。
    """
    batches = {}
    for usc, _record, plans in rowPlans:
        for plan in plans:
            if not plan["payload"]:
                continue                        # 已完全一致：不入批，连 dt_update 也不动
            batch = batches.setdefault(plan["table"], {"table": plan["table"],
                                                       "conflict": plan["key"],
                                                       "updates": [], "inserts": [],
                                                       "uscOf": {}})
            batch["inserts" if plan["insert"] else "updates"].append(plan)
            batch["uscOf"][plan["keyValue"]] = usc
    result = []
    for table in (FUTU_TABLE, SECU_TABLE):
        batch = batches.get(table)
        if not batch:
            continue
        for bucket in ("updates", "inserts"):
            entries = batch[bucket]
            columns = []
            for plan in entries:
                columns += [name for name in plan["payload"] if name not in columns]
            rows = []
            for plan in entries:
                row = {}
                for name in columns:
                    if name in plan["payload"]:
                        row[name] = plan["payload"][name]
                    elif plan["insert"]:
                        row[name] = None
                    else:
                        row[name] = plan["current"].get(name)
                row[batch["conflict"]] = plan["keyValue"]
                rows.append(row)
            batch[bucket] = rows
        result.append(batch)
    return result


def mismatchText(usc, record, source, misses, nowText):
    """未命中留痕文件的正文（含缺表清单、映射参数、处理建议）。"""
    lines = [
        "# VerifyQuoteMinute 配置同步：未命中记录",
        "",
        "usc        : %s" % usc,
        "stockId    : %s" % (cell((record or {}).get(STOCK_ID_FIELD)) or "(未知)"),
        "来源文件   : %s" % (source or "(无)"),
        "检测时刻   : %s (+08:00)" % nowText,
        "任务脚本   : %s（版本 %s）" % (SCRIPT_NAME, TASK_VERSION),
        "工作流运行 : %s" % env("GITHUB_RUN_ID", "(本地运行)"),
        "",
        "## 未命中的表 / 原因",
    ]
    for entry in misses:
        # 允许 "CODE:补充说明" 形式（如把缺失的字段名一并落进留痕文件）
        code, _, detail = entry.partition(":")
        lines.append("- [%s] %s%s"
                     % (code, MISS_REASONS.get(code, code), ("（%s）" % detail) if detail else ""))
    lines += ["", "## 映射表解析出的查询参数"]
    if record:
        for name in RECORD_FIELDS:
            lines.append("%-18s = %s" % (name, cell(record.get(name))))
    else:
        lines.append("(无映射记录)")
    lines += [
        "",
        "## 处理建议",
        "- 表内缺行时本脚本默认**只留痕不新建**（脚本常量 ALLOW_INSERT = False）：可先补建对应行",
        "  再重跑本步骤，或把常量 ALLOW_INSERT 置 True（或临时用 env INPUT_SYNC_ALLOW_INSERT=1",
        "  （新建的行会在运行摘要与步骤日志里单独列出，务必人工复核）。",
        "- 补建口径：两张表都以 usc 关联（主键即 usc，%s 改名后尤其如此）。" % FUTU_TABLE,
        "- 补建后本文件保留作历史留痕即可。",
        "",
    ]
    return "\n".join(lines)


def writeLocal(path, content):
    """写本地文件（自动建目录），返回绝对路径。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    return os.path.abspath(path)


def readWatchlist(path):
    """读待验证清单：每行一个 usc，一行内可多个，# 起为注释（与采集脚本同口径）。"""
    if not path or not os.path.isfile(path):
        return []
    targets = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            targets.extend(chunks(line.split("#", 1)[0]))
    return targets


# ---------------------------------------------------------------------------
# 目标合成、执行与汇总
# ---------------------------------------------------------------------------


def resolveTargets(batchUscs, successUscs, batchSize, sampleLimit):
    """按 docstring 第二节合成一轮的目标名单，返回 (targets, dropped, sampled)。

    :param batchUscs: 本批 usc（输入 / 清单解析所得，保持原序）。
    :param successUscs: 目标分支 Verify/Success/ 下的 usc 清单（路径升序）。
    :param batchSize: 单轮目标数上限；0 = 不限。
    :param sampleLimit: 从清单头部补齐的条数上限。
    """
    inSuccess = set(successUscs)
    kept = [usc for usc in batchUscs if usc in inSuccess]
    dropped = [usc for usc in batchUscs if usc not in inSuccess]
    if batchSize <= 0:
        room = sampleLimit
    else:
        room = min(sampleLimit, max(0, batchSize - len(kept)))
    taken, sampled = set(kept), []
    for usc in successUscs:
        if len(sampled) >= room:
            break
        if usc not in taken:
            sampled.append(usc)
            taken.add(usc)
    return kept + sampled, dropped, sampled


def logPlan(position, total, usc, record, source, plans, misses, warns):
    """打印一条证券的完整计划日志（字段级，便于排查）。"""
    # 头部这行的 stockId 必须取映射记录的 stockId（不是 FUTU_KEY：它现在是 usc，会与前面的 usc 重复）
    print("[%d/%d] usc=%s  quoteMarket=%s  stockId=%s"
          % (position, total, usc, cell(record.get("quoteMarket")),
             cell(record.get(STOCK_ID_FIELD))), flush=True)
    print("      来源 : %s" % (source or "(无 .mvsv 来源)"), flush=True)
    print("      映射 : %s" % "  ".join("%s=%s" % (name, cell(record.get(name)))
                                        for name in RECORD_FIELDS if name != "stockId"), flush=True)
    for plan in plans:
        table = plan["table"]
        if not plan["payload"]:
            print("      · %s：已在册且 %s"
                  % (table, "；".join(plan["unchanged"]) or "无需变更"), flush=True)
            continue
        print("      · %s 待%s %d 个字段（并入本表批量 upsert）："
              % (table, "新建（表内无此行）" if plan["insert"] else "更新", len(plan["payload"])),
              flush=True)
        for column, sourceField, have, want in plan["changed"]:
            print("          ↑ %-18s %s → %s   (%s ← %s)"
                  % (column, shown(have), sqlLiteral(want), column, sourceField), flush=True)
        if plan["unchanged"]:
            print("          = 保持 : %s" % "；".join(plan["unchanged"]), flush=True)
        if plan["fill"]:
            print("          = 补列 : %s（NOT NULL 且无默认值，按库中现值回写，值不变）"
                  % "；".join("%s=%s" % (column, sqlLiteral(value))
                             for column, value in plan["fill"]), flush=True)
        for column, sourceField, have in plan["skipped"]:
            print("          ! 跳过 : %s（映射缺 %s，保留库中现值 %s）"
                  % (column, sourceField, shown(have)), flush=True)
    for warn in warns:
        print("      ⚠ 告警：%s" % warn, flush=True)
    if misses:
        print("      ⚠ 未命中：%s（将留痕 %s/%s.txt）"
              % ("、".join(misses), MISMATCH_DIR, usc), flush=True)


def briefKeys(keys, limit=6):
    """把主键列表折成一行短串（日志用）。"""
    head = "、".join(cell(key) for key in keys[:limit])
    return head + ("…（共 %d）" % len(keys) if len(keys) > limit else "")


def applyBatches(client, batches):
    """按表提交批量 upsert，返回 (已写行, 新建行, 失败项)。

    每张表两条语句：在册行 → UPDATE 分支（不含 dt_create），缺行 → INSERT 分支（补 dt_create）。
    失败项 = (表名, 动作, 主键列表, 原因)；一条语句即一个事务，失败以块为单位。
    """
    written, inserted, failed = [], [], []
    for batch in batches:
        for bucket, action in (("updates", "更新"), ("inserts", "新建")):
            rows = batch[bucket]
            if not rows:
                continue
            table, conflict = batch["table"], batch["conflict"]
            keys = [row[conflict] for row in rows]
            back, blocks = client.upsert(table, rows, conflict)
            if blocks:
                for badKeys, reason in blocks:
                    failed.append((table, action, badKeys, reason))
                    print("      ✗ %s %s %d 行失败：%s（块内 %s）"
                          % (table, action, len(badKeys), reason, briefKeys(badKeys)), flush=True)
            if back:
                written.extend(cell(row.get(conflict)) for row in back)
                if bucket == "inserts":
                    inserted.extend(cell(row.get(conflict)) for row in back)
            done = len(keys) - sum(len(badKeys) for badKeys, _ in blocks)
            if done:
                print("      ⤷ %s %s %d 行 → HTTP 2xx，回读 %d 行（%s）"
                      % (table, action, done, len(back), briefKeys(keys)), flush=True)
    return written, inserted, failed


def failedUscs(batches, failed):
    """把「块级写库失败」折算成 usc 集合，返回 (usc 集合, 折算不出的主键)。

    失败是按块记录的（表名 + 该块的主键），而要不要删文件是按 usc 判断的，故用 batch["uscOf"]
    （主键 → usc）换算回来。折算不出的主键理论上不会出现；真出现了就保守处理 —— 该表本轮
    一并不删，见 consumeSuccess 的调用方。
    """
    lookup = {batch["table"]: batch["uscOf"] for batch in batches}
    blocked, orphan = set(), []
    for table, _action, keys, _reason in failed:
        byKey = lookup.get(table, {})
        for key in keys:
            usc = byKey.get(cell(key))
            if usc:
                blocked.add(usc)
            else:
                orphan.append((table, cell(key)))
    return blocked, orphan


def planConsumption(rowPlans, pendingMismatch, earlyMismatch, blockedUsc, sourceOf, shaOf):
    """定「哪些 .mvsv 本轮删、哪些留」，返回 (待删, 保留)。

    :param rowPlans: [(usc, record, plans)]，已算完差异的行（含「无需变更」的行）。
    :param pendingMismatch: [(usc, record, misses)]，算差异时判为未命中的行。
    :param earlyMismatch: [(usc, record, misses)]，映射不可用、连差异都没算的行。
    :param blockedUsc: 写库失败涉及的 usc（按块折算而来，见 failedUscs）。
    :param sourceOf: {usc: 仓库路径}；shaOf: {usc: blob sha}。
    :return: (consumed, retained)：consumed = [(usc, 路径, sha)]；retained = [(usc, 原因)]。

    判据见 docstring 第九节：**同步完成即删** —— 包含「本来就已经一致、本轮无需变更」的行，
    否则它们同样滞留在队头、把队列卡死；未命中（已留痕）与写库失败的一律保留。
    """
    reasonOf = {}
    for usc, _record, _misses in pendingMismatch:
        reasonOf[usc] = "未命中，已留痕到 %s/" % MISMATCH_DIR
    for usc in sorted(blockedUsc):
        reasonOf.setdefault(usc, "写库失败，须保留待下一轮重试")
    consumed = []
    retained = [(usc, "映射不可用，已留痕到 %s/" % MISMATCH_DIR)
                for usc, _record, _misses in earlyMismatch]
    for usc, _record, _plans in rowPlans:
        if usc in reasonOf:
            retained.append((usc, reasonOf[usc]))
        elif not sourceOf.get(usc):
            retained.append((usc, "清单里找不到对应文件"))
        else:
            consumed.append((usc, sourceOf[usc], shaOf.get(usc)))
    return consumed, retained


def consumeSuccess(repoAccess, entries, failures):
    """逐个删除已同步完成的 .mvsv，返回 (已删, 删除失败) 计数。

    :param entries: [(usc, 仓库路径, blob sha)]，sha 为 None 时由 delete_content 现查。
    :param failures: 调用方的失败清单，删除失败按 (路径, 原因) 追加进去。
    逐个文件一次提交：一个失败不影响其余文件；库行已写入即不再回滚（见 docstring 第九节）。
    """
    done, bad = 0, 0
    for usc, path, sha in entries:
        result = repoAccess.deleteFile(path, sha, DELETE_COMMIT_MSG % os.path.basename(path))
        if result.get("success"):
            done += 1
            print("      ⤷ 已消费 %s（usc=%s，HTTP %s）"
                  % (path, usc, result.get("http_status")), flush=True)
        else:
            bad += 1
            failures.append((path, result.get("message") or "删除失败"))
            print("      ✗ 消费 %s 失败（usc=%s）：%s（该文件下一轮会再同步一次）"
                  % (path, usc, result.get("message")), flush=True)
    return done, bad


def archiveMismatch(repoAccess, usc, record, source, misses, outDir, dryRun, failures):
    """把未命中记录写成本地文件并提交到产物分支；提交失败记入 failures。"""
    content = mismatchText(usc, record, source, misses, nowChina())
    local = writeLocal(os.path.join(outDir, MISMATCH_DIR.replace("/", os.sep), "%s.txt" % usc), content)
    remote = "%s/%s.txt" % (MISMATCH_DIR, usc)
    if dryRun:
        print("      ⤷ 留痕 %s（dry-run 不提交）本地副本 %s" % (remote, local), flush=True)
        return
    if not repoAccess.usable:
        failures.append((remote, "缺少仓库令牌（GITHUB_COMMIT_TOKEN），无法提交留痕文件"))
        print("      ✗ 留痕 %s 提交失败：缺少仓库令牌" % remote, flush=True)
        return
    result = repoAccess.commitFile(remote, local, "MisMatch %s" % usc)
    if result.get("success"):
        print("      ⤷ 留痕 %s 已提交（HTTP %s）" % (remote, result.get("http_status")), flush=True)
    else:
        failures.append((remote, result.get("message") or "提交失败"))
        print("      ✗ 留痕 %s 提交失败：%s" % (remote, result.get("message")), flush=True)


def stepSummary(stats, branch, dryRun, probability, batchSize, sampleLimit, deleteEnabled):
    """写 Actions 步骤摘要（未在 Actions 中运行则跳过）。"""
    path = env("GITHUB_STEP_SUMMARY")
    if not path:
        return
    if deleteEnabled:
        consumeNote = "dry-run 只列出，未删除" if dryRun else "已从 %s 删除，头部随之前移" % SUCCESS_DIR
    else:
        consumeNote = "关闭（CONSUME_SUCCESS=False 或被 env 覆盖为 0），下一轮会再同步一次"
    if not stats["gated"]:
        lines = ["## VerifyQuoteMinute 配置同步（Supabase）", "",
                 "| 项 | 值 |", "|---|---|",
                 "| 概率闸门 | 未命中（%d%%） |" % probability,
                 "| 本轮动作 | 无（不读库、不写库、不留痕、不删文件） |"]
    else:
        lines = [
            "## VerifyQuoteMinute 配置同步（Supabase）", "",
            "| 项 | 值 | 备注 |", "|---|---|---|",
            "| 目标分支 | %s | %s |" % (branch, "dry-run 未写库" if dryRun else "已写库"),
            "| 概率闸门 | 命中（%d%%） | 阈值在工作流文件内调整 |" % probability,
            "| 批量上限 | %d | 清单头部补齐上限 %d |" % (batchSize, sampleLimit),
            "| 目标条数 | %d | 本批 %d + 补齐 %d |"
            % (stats["total"], stats["batchCount"], stats["sampleCount"]),
            "| %s | %d | %s |" % ("计划写入" if dryRun else "写库成功", stats["written"],
                                   "dry-run 未执行" if dryRun else "批量 upsert 回读行数"),
            "| ⤷ 其中新建 | %d | 表内原无此行（已补 dt_create；须人工复核） |" % stats["inserted"],
            "| 写库失败 | %d | 见步骤日志中的 ✗ 行 |" % stats["failed"],
            "| 无需变更 | %d | 行×表：该表该行已完全一致，不入批 |" % stats["skippedRows"],
            "| 消费 .mvsv | %d | %s |" % (stats["deleted"], consumeNote),
            "| ⤷ 删除失败 | %d | 该条下一轮会再同步一次（幂等） |" % stats["deleteFailed"],
            "| 告警 | %d | 不阻塞：如主键重复、表结构漂移导致某列被剔除 |" % len(stats["warns"]),
            "| 未命中留痕 | %d | Verify/MisMatch/{usc}.txt |" % stats["mismatched"],
            "| ⤷ 映射无记录 | %d | usc 不在 UscFutuMapping.jsonl.idx 中 |" % stats["indexMiss"],
            "| ⤷ 映射缺关键字段 | %d | 记录缺 stockId/市场/类型，拼不出请求参数 |" % stats["recordMiss"],
            "| ⤷ 表内缺行 | %d | 本轮未开启自动插入，只留痕 |" % stats["rowMiss"],
            "| ⤷ 新建凑不齐非空列 | %d | 映射缺值时无法新建，避免整块 upsert 失败 |" % stats["insertMiss"],
            "| 请求总数 | %d | 含读现状与重试 |" % stats["requests"],
        ]
        if stats["inserted"]:
            lines += ["", "> 本次新建行（表内原无，已补 dt_create，须人工复核）：%s"
                      % "、".join(stats["insertedKeys"][:50])]
        if stats["warns"]:
            lines += ["", "> 告警（不阻塞，供人工复核）："] + ["- %s" % item for item in stats["warns"][:20]]
        if stats["dropped"]:
            lines += ["", "> 本批被剔除（不在 Verify/Success/ 下，采集未成功）：%s"
                      % "、".join(stats["dropped"])]
    if stats["failures"]:
        lines += ["", "| 失败项 | 说明 |", "|---|---|"]
        lines += ["| %s | %s |" % (item, reason) for item, reason in stats["failures"][:50]]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main():
    """入口：闸门 → 定目标 → 读现状 → 算差异 → 写库 → 留痕 → 汇总。返回退出码。"""
    branch = env("INPUT_BRANCH", DEFAULT_BRANCH)
    outDir = env("INPUT_OUT_DIR", DEFAULT_OUT_DIR)
    probability = envInt("INPUT_SYNC_PROBABILITY", DEFAULT_PROBABILITY, minimum=0, maximum=100)
    batchSize = envInt("INPUT_SYNC_BATCH_SIZE", DEFAULT_BATCH_SIZE, minimum=0)
    sampleLimit = envInt("INPUT_SYNC_SAMPLE_LIMIT", DEFAULT_SAMPLE_LIMIT, minimum=0)
    allowInsert, allowInsertFrom = policyFlag("INPUT_SYNC_ALLOW_INSERT", ALLOW_INSERT)
    deleteEnabled, deleteFrom = policyFlag("INPUT_SYNC_DELETE", CONSUME_SUCCESS)
    dryRun = envFlag("INPUT_SYNC_DRY_RUN")
    watchlist = env("INPUT_WATCHLIST", os.path.join(SCRIPT_DIR, "VerifyQuoteMinuteWatchlist.txt"))

    print("=" * 78, flush=True)
    print("任务脚本   %s（版本 %s）" % (SCRIPT_NAME, TASK_VERSION), flush=True)
    print("目标分支   %s%s" % (branch, "（dry-run：只打印 SQL，不写库不提交）" if dryRun else ""),
          flush=True)
    print("抽样参数   概率闸门 %d%%｜批量上限 %d｜头部补齐上限 %d｜表内缺行 %s"
          % (probability, batchSize, sampleLimit, "允许新建" if allowInsert else "只留痕"),
          flush=True)
    print("文件消费   %s" % ("同步完成的记录删除其 .mvsv（清单头部随之前移）" if deleteEnabled
                             else "未开启：.mvsv 保留，下一轮会再同步一次"),
          flush=True)
    # 策略开关的「当前取值 + 来源」：从哪个分支、哪份工作流触发都一目了然，
    # 免得再出现「以为开了其实没开」（工作流里的 env 缺失时不再静默回落到关）。
    print("策略开关   删除 .mvsv（INPUT_SYNC_DELETE）= %s ← %s"
          % ("1" if deleteEnabled else "0", deleteFrom), flush=True)
    print("           缺行新建（INPUT_SYNC_ALLOW_INSERT）= %s ← %s"
          % ("1" if allowInsert else "0", allowInsertFrom), flush=True)
    print("=" * 78, flush=True)

    stats = {"gated": False, "total": 0, "batchCount": 0, "sampleCount": 0, "written": 0,
             "inserted": 0, "insertedKeys": [], "skippedRows": 0, "failed": 0, "mismatched": 0,
             "indexMiss": 0, "recordMiss": 0, "rowMiss": 0, "insertMiss": 0, "warns": [],
             "requests": 0, "deleted": 0, "deleteFailed": 0,
             "dropped": [], "failures": []}

    # ① 概率闸门：未命中则本轮什么都不做
    roll = random.random() * 100.0
    stats["gated"] = roll < probability
    print("概率闸门   随机数 %.2f %s %d%% → %s"
          % (roll, "<" if stats["gated"] else ">=", probability,
             "命中，继续执行" if stats["gated"] else "未命中，本轮不执行"), flush=True)
    if not stats["gated"]:
        stepSummary(stats, branch, dryRun, probability, batchSize, sampleLimit, deleteEnabled)
        return RETURN_OK

    # ② 本批 usc（与采集脚本同口径：输入优先，其次清单文件）
    batchUscs = chunks(env("INPUT_USC"))
    if batchUscs:
        print("本批来源   输入 INPUT_USC → %d 条" % len(batchUscs), flush=True)
    else:
        batchUscs = readWatchlist(watchlist)
        print("本批来源   清单文件 %s → %d 条" % (watchlist, len(batchUscs)), flush=True)

    # ③ 映射表 + 目标分支的 Success 清单（线上列举优先，失败降级到本地工作树）
    mapping = Mapping()
    print("映射表     %s" % mapping.describe(), flush=True)
    repoAccess = RepoAccess(branch)
    # 开了删除却没有令牌：当场退出 2，免得「以为删了其实没删」而清单头部永远不前进
    if deleteEnabled and not dryRun and not repoAccess.usable:
        raise TaskError("已开启 INPUT_SYNC_DELETE，但缺少仓库身份或令牌（GITHUB_COMMIT_TOKEN），"
                        "无法删除 .mvsv；请提供令牌或把 INPUT_SYNC_DELETE 置 0")
    successPaths = None
    if repoAccess.usable:
        try:
            successPaths = repoAccess.listMvsv(SUCCESS_DIR)
        except TaskError as exc:
            print("  [告警] 线上列举失败（%s），降级为本地工作树扫描" % exc, flush=True)
    if successPaths is None:
        successPaths = localSuccessFiles(SUCCESS_DIR)
        if successPaths is None:
            raise TaskError("既无法列举 %s@%s，本地工作树也没有该目录" % (SUCCESS_DIR, branch))
        print("  [告警] 本次改用本地工作树的 %s（共 %d 个 .mvsv；线上列举不可用；"
              "该路径下无 blob sha，删除时逐个现查）" % (SUCCESS_DIR, len(successPaths)), flush=True)
    # 同一 usc 出现在多个市场目录时按路径升序取第一个（与 successUscs 同序，日志因此可复现）
    successUscs, sourceOf, shaOf, dupUscs = [], {}, {}, []
    for path, sha in successPaths:
        usc = os.path.basename(path)[:-len(MVSV_SUFFIX)]
        if usc in sourceOf:
            dupUscs.append(usc)
            continue
        successUscs.append(usc)
        sourceOf[usc] = path
        shaOf[usc] = sha
    print("Success 清单 %s@%s 共 %d 个 .mvsv（去重后 %d 个 usc，按路径升序）"
          % (SUCCESS_DIR, branch, len(successPaths), len(successUscs)), flush=True)
    if dupUscs:
        print("  [告警] 同一 usc 出现在多个市场目录（取路径升序里第一个）：%s"
              % "、".join(sorted(set(dupUscs))), flush=True)

    targets, dropped, sampled = resolveTargets(batchUscs, successUscs, batchSize, sampleLimit)
    stats.update(total=len(targets), sampleCount=len(sampled),
                 batchCount=len(targets) - len(sampled), dropped=dropped)
    if dropped:
        print("本批剔除   %d 条不在 %s 下（采集未成功 / 未产出）：%s"
              % (len(dropped), SUCCESS_DIR, "、".join(dropped)), flush=True)
    if not targets:
        print("无可同步条目（本批为空且头部补齐上限为 0），本轮结束。", flush=True)
        stepSummary(stats, branch, dryRun, probability, batchSize, sampleLimit, deleteEnabled)
        return RETURN_OK
    print("本轮目标   %d 条 = 本批 %d + 清单头部补齐 %d"
          % (len(targets), stats["batchCount"], len(sampled)), flush=True)
    if sampled:
        print("补齐名单   %s%s" % ("、".join(sampled[:20]), " …" if len(sampled) > 20 else ""),
              flush=True)
    print("-" * 78, flush=True)

    # ④ 解析映射记录：无记录 / 关键字段缺失的直接留痕（不依赖数据库，先办掉以免后续失败丢信息）
    records, earlyMismatch, indexMiss = [], [], []
    for usc in targets:
        record = mapping.lookup(usc)
        if record is None:
            indexMiss.append(usc)
            earlyMismatch.append((usc, None, [MISS_INDEX]))
            continue
        missing = [name for name in CRITICAL_FIELDS if not cell(record.get(name))]
        if missing:
            print("  [告警] %s 的映射记录缺关键字段：%s（无法拼出请求参数，转留痕）"
                  % (usc, "、".join(missing)), flush=True)
            earlyMismatch.append((usc, record, ["%s:%s" % (MISS_RECORD_FIELD, "、".join(missing))]))
            continue
        records.append(record)
    stats["indexMiss"] = len(indexMiss)
    stats["recordMiss"] = len(earlyMismatch) - len(indexMiss)
    if earlyMismatch:
        print("映射不可用 %d 条（无记录 %d / 缺关键字段 %d）：%s"
              % (len(earlyMismatch), len(indexMiss), stats["recordMiss"],
                 "、".join(item[0] for item in earlyMismatch)), flush=True)
        for usc, record, misses in earlyMismatch:
            archiveMismatch(repoAccess, usc, record, sourceOf.get(usc, ""), misses,
                            outDir, dryRun, stats["failures"])

    # ⑤ 读现状（凭据与映射表同属任务级前置条件，缺了就退出 2）
    client = SupabaseClient(env(ENV_SUPABASE_REF), env(ENV_SUPABASE_KEY))
    futuRows, secuRows = {}, {}
    required, columns = {}, {}
    if records:
        # 先核对表名 / 主键 / 列口径，再读现状：口径不符时当场退出 2，而不是等到一块块 upsert 报错
        required, columns, schemaWarns = inspectSchema(client)
        for warn in schemaWarns:
            print("  [告警] %s" % warn, flush=True)
        stats["warns"] += schemaWarns
        print("表结构核对 %s：%s｜%s：%s" % (
            FUTU_TABLE, describeTable(FUTU_TABLE, FUTU_KEY, columns, required),
            SECU_TABLE, describeTable(SECU_TABLE, SECU_KEY, columns, required)), flush=True)
        try:
            futuRows, secuRows = loadCurrentState(client, records, required, columns)
        except SupabaseRestError as exc:
            raise TaskError("读取两张表现状失败：%s" % exc)
        print("现状读取   %s 命中 %d 个 %s｜%s 命中 %d 个 %s（请求 %d 次）"
              % (FUTU_TABLE, len(futuRows), FUTU_KEY, SECU_TABLE, len(secuRows), SECU_KEY,
                 client.requests), flush=True)

    # ⑥ 逐条算差异并打印字段级日志（不写库，先出计划）
    stamp = nowUtcIso()
    print("时间戳     dt_update = %s（两张表同值）" % stamp, flush=True)
    rowPlans, pendingMismatch = [], []
    for position, record in enumerate(records, start=1):
        usc = cell(record.get(SECU_KEY))
        plans, misses, warns = planRow(record, futuRows, secuRows, stamp, allowInsert,
                                       required, columns)
        logPlan(position, len(records), usc, record, sourceOf.get(usc, ""), plans, misses, warns)
        rowPlans.append((usc, record, plans))
        stats["warns"] += ["usc=%s：%s" % (usc, warn) for warn in warns]
        if misses:
            pendingMismatch.append((usc, record, misses))
        for plan in plans:
            if plan["payload"]:
                if plan["insert"]:
                    print("      ＋ %s 表内原无 %s=%s，本行将走新建分支"
                          % (plan["table"], plan["key"], plan["keyValue"]), flush=True)
            else:
                stats["skippedRows"] += 1

    # ⑦ 写库：每张表两条 upsert 语句（在册行更新 / 缺行新建），无差异的行不入批
    batches = buildBatches(rowPlans)
    updRows = sum(len(batch["updates"]) for batch in batches)
    insRows = sum(len(batch["inserts"]) for batch in batches)
    print("-" * 78, flush=True)
    print("写库计划   %d 张表｜更新 %d 行｜新建 %d 行｜无需变更 %d 项（行×表，两表均已一致）"
          % (len(batches), updRows, insRows, stats["skippedRows"]), flush=True)
    for batch in batches:
        for bucket, action in (("updates", "更新"), ("inserts", "新建")):
            if batch[bucket]:
                print("  · %s %s %d 行、%d 列（on_conflict=%s）"
                      % (batch["table"], action, len(batch[bucket]),
                         len(batch[bucket][0]), batch["conflict"]), flush=True)
        if dryRun:
            for bucket in ("updates", "inserts"):
                if batch[bucket]:
                    print("  [dry-run] %s" % upsertSql(batch["table"], batch["conflict"],
                                                      batch[bucket]), flush=True)
    if dryRun:
        print("dry-run    以上为等价 SQL（首行展开），未连接写接口。", flush=True)
        stats["written"] = updRows + insRows
        stats["inserted"] = insRows
        failed = []
    else:
        written, inserted, failed = applyBatches(client, batches)
        stats["written"], stats["inserted"] = len(written), len(inserted)
        stats["insertedKeys"] = sorted(set(inserted))
        if stats["insertedKeys"]:
            print("新增行     %d 行：%s" % (len(stats["insertedKeys"]),
                                          "、".join(stats["insertedKeys"])), flush=True)
    stats["failed"] = sum(len(keys) for _table, _action, keys, _reason in failed)

    # ⑧ 未命中留痕（表内缺行 / 映射不可用等情况）
    if pendingMismatch:
        print("-" * 78, flush=True)
        print("未命中留痕 %d 条 → %s/{usc}.txt" % (len(pendingMismatch), MISMATCH_DIR), flush=True)
        for usc, record, misses in pendingMismatch:
            archiveMismatch(repoAccess, usc, record, sourceOf.get(usc, ""), misses,
                            outDir, dryRun, stats["failures"])
    stats["mismatched"] = len(pendingMismatch) + len(earlyMismatch)
    stats["rowMiss"] = sum(1 for _usc, _record, misses in pendingMismatch
                           if MISS_FUTU_ROW in misses or MISS_SECU_ROW in misses)
    stats["insertMiss"] = sum(1 for _usc, _record, misses in pendingMismatch
                              if any(str(code).startswith(MISS_INSERT_REQUIRED)
                                     for code in misses))

    # ⑨ 消费 .mvsv：同步完成的记录，其 Success 文件从产物分支删掉（Contents API DELETE）。
    #    不删的话清单头部每轮取到的都是同一批文件，队列后面的记录永远排不到（见 docstring 第九节）。
    blockedUsc, orphanKeys = failedUscs(batches, failed)   # dry-run 时 failed 恒为空
    if orphanKeys:
        # 主键折算不出 usc：无从判断该留哪些文件，保守起见涉及的表本轮一并不删
        orphans = sorted(set(table for table, _key in orphanKeys))
        print("  [告警] 这些失败主键折算不出 usc：%s（涉及的表 %s 本轮不删任何文件）"
              % ("、".join("%s=%s" % (table, key) for table, key in orphanKeys[:10]),
                 "、".join(orphans)), flush=True)
        blockedUsc |= {usc for usc, _record, _plans in rowPlans
                       if any(plan["table"] in orphans for plan in _plans)}
    consumed, retained = planConsumption(rowPlans, pendingMismatch, earlyMismatch,
                                         blockedUsc, sourceOf, shaOf)
    if consumed or retained:
        print("-" * 78, flush=True)
        if not deleteEnabled:
            print("文件消费   关闭（%s）：%d 个已完成同步的 .mvsv 保留在 %s，"
                  "下一轮会再同步一次" % (deleteFrom, len(consumed), SUCCESS_DIR), flush=True)
        elif dryRun:
            print("文件消费   [dry-run] 应删除 %d 个已完成同步的 .mvsv：" % len(consumed), flush=True)
            for usc, path, _sha in consumed:
                print("      · %s（usc=%s）" % (path, usc), flush=True)
        else:
            print("文件消费   删除 %d 个已完成同步的 .mvsv（逐个文件一次提交）：" % len(consumed),
                  flush=True)
            stats["deleted"], stats["deleteFailed"] = consumeSuccess(
                repoAccess, consumed, stats["failures"])
        if retained:
            print("文件保留   %d 个：%s%s"
                  % (len(retained), "；".join("%s（%s）" % (usc, why) for usc, why in retained[:20]),
                     " …" if len(retained) > 20 else ""), flush=True)

    # ⑩ 汇总
    stats["requests"] = client.requests
    stats["failures"] = [("%s %s %d 行失败" % (table, action, len(keys)), reason)
                         for table, action, keys, reason in failed] + list(stats["failures"])
    consumeNote = ""
    if deleteEnabled:
        consumeNote = "，消费 .mvsv %d 个（删除失败 %d）" % (stats["deleted"], stats["deleteFailed"])
    print("=" * 78, flush=True)
    print("本轮结束：目标 %d，%s %d 行（其中新建 %d 行），写库失败 %d 行，无需变更 %d 项，"
          "未命中留痕 %d（其中映射无记录 %d、映射缺字段 %d、表内缺行 %d、新建缺列 %d），"
          "告警 %d，请求 %d 次%s"
          % (stats["total"], "计划写入（dry-run 未执行）" if dryRun else "写库成功",
             stats["written"], stats["inserted"], stats["failed"], stats["skippedRows"],
             stats["mismatched"], stats["indexMiss"], stats["recordMiss"], stats["rowMiss"],
             stats["insertMiss"], len(stats["warns"]), client.requests, consumeNote), flush=True)
    for item, reason in stats["failures"][:20]:
        print("  ✗ %s：%s" % (item, reason), flush=True)
    stepSummary(stats, branch, dryRun, probability, batchSize, sampleLimit, deleteEnabled)

    if stats["failures"]:
        annotate("有 %d 项写库/提交/删除失败，详见步骤日志与运行摘要" % len(stats["failures"]), "error")
        return RETURN_FAILED
    if stats["inserted"]:
        annotate("本轮新建了 %d 行（表内原无），请人工复核" % stats["inserted"], "warning")
    if stats["mismatched"]:
        annotate("有 %d 条记录未命中，已留痕到 %s/" % (stats["mismatched"], MISMATCH_DIR), "warning")
    if stats["deleted"]:
        annotate("已消费 %d 个 .mvsv（配置已同步完成），清单头部随之前移" % stats["deleted"], "notice")
    return RETURN_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TaskError as exc:
        annotate("任务级错误：%s" % exc, "error")
        print("任务级错误：%s" % exc, file=sys.stderr, flush=True)
        sys.exit(RETURN_TASK_ERROR)
