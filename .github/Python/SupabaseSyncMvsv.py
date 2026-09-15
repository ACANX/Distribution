#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SupabaseSyncMvsv —— 行情文件分批增量入库 Supabase PG + 验证后删除源文件
========================================================================================

一、工具定位
----------------------------------------------------------------------------------------
把本仓库 quote 分支 Archive/Finv/SecuQuote/Day/ 下的 .mvsv 行情文件读取解析后，
按批次（默认 50 行/批，可配置）以 PostgREST Upsert（on_conflict=ts,usc +
merge-duplicates）做字段级增量更新（无记录则插入）到 Supabase PostgreSQL 表
public.finv_quote_secu_kline_min。

删除安全语义（与 Java 版 BatchUpdateThenFullSuccessDeleteTest 一致）：
    - 单个文件的"全部批次"都成功，该文件才被标记为"验证通过"；
    - 验证通过 且 删除开关（ENABLE_DELETE）开启 → 才真正删除 quote 分支上的源文件；
    - 验证通过但开关关闭 → 只打印"待删除"清单，不动文件（安全模式）；
    - 任一批次失败 → 保留源文件，单批失败不阻塞后续批次与后续文件；
    - Upsert 幂等，失败文件保留后直接重跑即可补齐，不会产生重复行。

单次运行最多处理 FILE_LIMIT 个文件（默认 50，可配置）；文件顺序按文件名中的
日期升序，同日期按证券代码自然排序升序（数字段按数值比较）。

二、翻译来源
----------------------------------------------------------------------------------------
本脚本为 Java 版 BatchUpdateThenFullSuccessDeleteTest 的 Python 适配实现，参考：
    - MvsvQuoteReader（解析 / 字段签名分组 / 分批）
    - SupabasePostgresClient + SupabasePostgresDataServiceImpl（PostgREST HTTP 细节）
    - FinvQuoteSecuKlineMin（表 public.finv_quote_secu_kline_min，主键 (usc, ts)）

三、git 源文件删除
----------------------------------------------------------------------------------------
GitHubCommitContent.py 是纯提交/读取库，不含删除功能（其文档明确"删除不属本工具
范围"），故删除在本脚本内实现：复用其 _request / _auth_headers / _get_file_sha，
先 GET 查 sha，再 DELETE /repos/{owner}/{repo}/contents/{path}（body 含
message + sha + branch）。目标仓库 = 本脚本所在仓库（.git 解析，本仓库即
ACANX/Distribution，不经 Commit.json），令牌走环境变量 GIT_COMMIT_TOKEN。

四、环境变量（凭据与配置一律经环境注入，严禁写进源码或日志）
----------------------------------------------------------------------------------------
    SUPABASE_PROJECT_REF  Supabase 项目引用（Dashboard 地址中 .supabase.co 之前一段）
    SUPABASE_KEY          Supabase API 密钥（service-role；不落日志）
    SUPABASE_BATCH_SIZE   单批提交行数（默认 50）
    SUPABASE_FILE_LIMIT   单次运行的文件数上限（默认 50）
    SUPABASE_ENABLE_DELETE 删除开关（"true"/"1" 开启，默认关闭）
    GIT_COMMIT_TOKEN      删除 quote 分支源文件用的 GitHub 令牌（contents:write）
    CURR_BRANCH / GITHUB_REF_NAME  或命令行第 1 参数：要处理的分支名（默认 quote）

五、退出码
----------------------------------------------------------------------------------------
    0 = 流程正常结束（含"有文件验证未通过而保留"的预期状态，详见日志汇总）；
    1 = 致命错误（凭据缺失 / 分支未定 / 源仓库身份解析失败）。

【环境要求】Python 3.8+，仅标准库；可直连 api.github.com 与 *.supabase.co。
"""

import json
import os
import re
import sys
import urllib.parse

# 同目录纯函数库：复用其 HTTP 请求 / 认证头 / sha 查询 / 仓库身份解析（纯函数，无副作用）
from GitHubCommitContent import (
    DEFAULT_API_BASE,
    _auth_headers,
    _ensure_console_utf8,
    _get_file_sha,
    _request,
    load_owner_repo_from_git_config,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# Supabase / PostgREST 目标表与冲突列（与 Java 版 FinvQuoteSecuKlineMin 一致）
TABLE = "finv_quote_secu_kline_min"
CONFLICT_COLUMNS = "ts,usc"

# 默认配置（可被环境变量覆盖）
DEFAULT_BATCH_SIZE = 50
DEFAULT_FILE_LIMIT = 50
DEFAULT_BRANCH = "quote"

# 源文件目录（quote 分支内相对路径）
SOURCE_DIR = "Archive/Finv/SecuQuote/Day"

# 文件名模式 {Code}_{Period}_{yyyyMMdd}.mvsv 的日期位数
SRC_DATE_DIGITS = 8

# "新增/更新拆分未知"时的占位值（与 Java 版 UNKNOWN 语义一致）
UNKNOWN = -1

# mvsv 数据列 → 表字段（wire 键）映射；数值列类型：int / float
COLUMN_TYPES = {
    "Ts": ("ts", "int"),
    "Date": ("date", "int"),
    "Time": ("time", "int"),
    "Open": ("open", "float"),
    "Close": ("close", "float"),
    "Low": ("low", "float"),
    "High": ("high", "float"),
    "Volume": ("volume", "int"),
    "Turnover": ("turnover", "float"),
    "ChangePrice": ("change_price", "float"),
    # 文件头标注为"涨跌幅(%)"，按原值直存、不做单位换算（与 Java 版一致）
    "ChangePercent": ("change_ratio", "float"),
}

# 字段签名统计顺序（仅列会出现在解析结果中的 wire 键；顺序固定保证可复现）
SIGNATURE_ORDER = ("open", "high", "low", "close", "volume", "turnover",
                   "change_price", "change_ratio", "provider")


def unquote(value):
    """去掉元信息取值外层成对的双引号（与 Java 版 unquote 一致）"""
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def parse_mvsv(path):
    """解析一份 .mvsv 行情文件（翻译 MvsvQuoteReader.read）

    列序取自文件头 `# 字段` / `# Field`（不写死）；usc 由 `# 证券代码`
    （缺失时退回 `# SecuCode`）回填，provider 由 `# 数据供应商` 回填；
    空字段一律不写入 dict（序列化跳过 → 落库即"该列不写"，符合增量更新语义）。

    :param path: 文件路径
    :return: (记录列表, 元数据 dict)；记录为 wire 键 dict，保持文件内顺序
    :raises ValueError: 数据行出现在字段头之前 / 字段头缺失 / 列数不符等解析错误
    """
    columns = None
    meta = {}
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line_no, raw in enumerate(f, 1):
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                body = line[1:].strip()
                if ":" not in body:
                    continue
                key, value = body.split(":", 1)
                key = key.strip()
                value = unquote(value.strip())
                meta.setdefault(key, value)
                if key in ("字段", "Field"):
                    columns = value.split("|")
                continue
            if columns is None:
                raise ValueError("第 %d 行数据出现在 # 字段 头之前" % line_no)
            values = line.split("|")
            if len(values) != len(columns):
                raise ValueError("第 %d 行列数 %d 与字段头 %d 不符"
                                 % (line_no, len(values), len(columns)))
            rows.append(to_row(columns, values))

    if columns is None:
        raise ValueError("文件缺少 # 字段 / # Field 元信息头")
    usc = meta.get("证券代码") or meta.get("SecuCode") or ""
    if not usc:
        raise ValueError("文件头缺少 证券代码 / SecuCode")
    provider = meta.get("数据供应商") or ""
    for row in rows:
        row["usc"] = usc
        if provider:
            row["provider"] = provider
    return rows, meta


def to_row(columns, values):
    """把一行数据按文件头列序转为 wire 键 dict（翻译 MvsvQuoteReader.toEntity）

    :param columns: 列名列表（来自文件头 # 字段）
    :param values: 该行的各列取值（未经 strip）
    :return: 已赋值的记录 dict（空列不出现在 dict 中）
    :raises ValueError: 数值列内容非法时抛出
    """
    row = {}
    for i, raw_value in enumerate(values):
        column = columns[i]
        mapping = COLUMN_TYPES.get(column)
        if mapping is None:
            continue
        wire_key, value_type = mapping
        value = raw_value.strip()
        if not value:
            continue  # 空列留空：不写入 dict，落库即"该列不写"
        if value_type == "int":
            try:
                row[wire_key] = int(value)
            except ValueError:
                row[wire_key] = int(float(value))
        else:
            row[wire_key] = float(value)
    return row


def field_signature(row):
    """计算一条记录"非 null 字段集合"的签名（翻译 MvsvQuoteReader.fieldSignature）

    PostgREST 要求同一批里每个对象的字段集合一致，按本签名分组后再分批才不会触发
    PGRST102 "All object keys must match"。

    :param row: 记录 dict
    :return: 以逗号结尾的字段名拼接（同一集合的生成顺序固定）
    """
    parts = []
    for name in SIGNATURE_ORDER:
        if name in row:
            parts.append(name)
    return ",".join(parts) + ","


def chunks(source, size):
    """把列表切成固定大小的片（翻译 MvsvQuoteReader.chunks）"""
    return [source[i:i + size] for i in range(0, len(source), size)]


class SupabaseRestError(Exception):
    """PostgREST 非 2xx / 网络错误（message 已做项目引用遮蔽）"""


class SupabaseRestClient:
    """Supabase Data API（PostgREST）最小客户端（翻译 service impl 的 query / upsertBatch）

    仅标准库 urllib；每个请求同时携带 apikey 与 Authorization: Bearer 两个头
    （Supabase 要求二者并存）；密钥不落日志，URL 中的项目引用一律遮蔽后再输出。
    """

    def __init__(self, project_ref, api_key, timezone="Asia/Shanghai", timeout=30):
        ref = (project_ref or "").strip()
        if not ref:
            raise ValueError("Supabase 项目引用（SUPABASE_PROJECT_REF）不能为空")
        if "/" in ref or ":" in ref:
            raise ValueError("项目引用只填 ref 本身，不要带协议或域名")
        if not (api_key or "").strip():
            raise ValueError("Supabase API 密钥（SUPABASE_KEY）不能为空")
        self.rest_url = "https://%s.supabase.co/rest/v1" % ref
        self.api_key = api_key.strip()
        self.timezone = (timezone or "").strip() or None
        self.timeout = timeout

    def _redact(self, text):
        """把文本中的项目引用（嵌在主机名里）替换为 ***，供日志输出"""
        return text.replace(self.rest_url.split("//")[1].split(".")[0], "***")

    def _headers(self, prefer=None, with_body=False):
        """构造认证头；prefer 为 PostgREST 的 Prefer 取值（可 None）"""
        headers = {
            "apikey": self.api_key,
            "Authorization": "Bearer %s" % self.api_key,
            "Accept": "application/json",
        }
        if self.timezone:
            headers["Prefer"] = ("timezone=" + self.timezone) if not prefer \
                else (prefer + ",timezone=" + self.timezone)
        elif prefer:
            headers["Prefer"] = prefer
        if with_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _send(self, method, url, body_json=None, prefer=None, operation="请求"):
        """发送请求并校验 2xx（翻译 requireSuccess；URL 遮蔽后进日志）"""
        body_bytes = json.dumps(body_json, ensure_ascii=False).encode("utf-8") \
            if body_json is not None else None
        headers = self._headers(prefer=prefer, with_body=body_bytes is not None)
        status, text, err = _request(method, url, headers, body_bytes, self.timeout)
        if err:
            raise SupabaseRestError("%s网络错误: %s" % (operation, err))
        if status is None or not (200 <= status < 300):
            raise SupabaseRestError("%s失败，HTTP %s，URL %s，响应: %s"
                                    % (operation, status, self._redact(url),
                                       (text or "")[:300]))
        return text

    def query(self, table, query_string=None):
        """查询行（GET），返回 JSON 数组原文；无命中行为 []（翻译 QueryRo 链路）"""
        url = "%s/%s" % (self.rest_url, urllib.parse.quote(table, safe=""))
        if query_string:
            url = url + "?" + query_string
        return self._send("GET", url, operation="查询")

    def upsert_batch(self, table, rows, conflict_columns):
        """批量 Upsert（POST + merge-duplicates），返回写入后的行数组原文 JSON

        rows 中每个对象的字段集合必须一致（调用方按签名分组保证）；更新是增量的：
        只覆盖载荷中出现过的列（dict 序列化天然跳过未赋值列）。
        """
        url = "%s/%s?on_conflict=%s" % (
            self.rest_url, urllib.parse.quote(table, safe=""),
            urllib.parse.quote(conflict_columns.strip(), safe=""))
        prefer = "resolution=merge-duplicates,return=representation"
        return self._send("POST", url, body_json=rows, prefer=prefer,
                          operation="批量 Upsert")

    def count_existing(self, rows):
        """统计这批记录里主键 (usc, ts) 已存在于库中的行数（翻译 countExisting）

        按 usc 分组后走 ts=in.(...) 过滤；预查询失败由调用方决定降级（拆分数记未知）。

        :param rows: 记录列表
        :return: 已存在的行数
        :raises SupabaseRestError: 网络异常或响应非 2xx 时抛出
        """
        by_usc = {}
        for row in rows:
            by_usc.setdefault(row["usc"], []).append(str(row["ts"]))
        existed = 0
        for usc, ts_list in by_usc.items():
            query = ("select=ts"
                     "&usc=eq.%s"
                     "&ts=in.(%s)"
                     "&limit=%d" % (urllib.parse.quote(usc, safe=""),
                                    ",".join(ts_list), len(ts_list)))
            text = self.query(TABLE, query)
            data = json.loads(text)
            if not isinstance(data, list):
                raise SupabaseRestError("预查询响应不是 JSON 数组")
            existed += len(data)
        return existed


def delete_branch_file(branch, path_key, token, api_base=DEFAULT_API_BASE):
    """删除本仓库指定分支上的文件（GitHub Contents API；GitHubCommitContent 无删除功能）

    行为：GET 查 sha → DELETE /contents/{path}（body: message + sha + branch）。
    仓库身份用 .git 解析（本脚本所在仓库，即 ACANX/Distribution），不经 Commit.json
    （其登记的目标是 acdnx/Distribution，与本删除操作的目标不同）。

    :param branch: 分支名（quote）
    :param path_key: 文件的仓库内相对路径
    :param token: GitHub 令牌（需本仓库 contents:write）
    :param api_base: GitHub API 仓库集合根
    :return: dict {"success": bool, "already_gone": bool, "message": str|None}
    """
    owner, repo = load_owner_repo_from_git_config()
    if not (owner and repo):
        return {"success": False, "already_gone": False,
                "message": "未能从 .git/config 解析出本仓库 owner/repo"}

    def fail(msg):
        print("❌ 删除失败：%s/%s@%s %s —— %s" % (owner, repo, branch, path_key, msg))
        return {"success": False, "already_gone": False, "message": msg}

    if not (token or "").strip():
        return fail("令牌缺失（GIT_COMMIT_TOKEN）")

    sha = _get_file_sha(api_base, owner, repo, path_key, branch, token, 30)
    if sha is None:
        print("⚠️ 文件在远端不存在（可能已删除），视为已处理：%s" % path_key)
        return {"success": True, "already_gone": True, "message": None}

    url = "%s/%s/%s/contents/%s" % (
        api_base, owner, repo, urllib.parse.quote(path_key, safe="/"))
    body = {
        "message": "[SupabaseSyncMvsv] delete synced %s" % path_key,
        "sha": sha,
        "branch": branch,
    }
    status, text, err = _request(
        "DELETE", url, _auth_headers(token, with_body=True),
        json.dumps(body, ensure_ascii=False).encode("utf-8"), 30)
    if err:
        return fail(err)
    parsed = None
    try:
        parsed = json.loads(text)
    except ValueError:
        pass
    if status is not None and 200 <= status < 300:
        print("✅ 删除成功：%s/%s@%s %s（HTTP %s）"
              % (owner, repo, branch, path_key, status))
        return {"success": True, "already_gone": False, "message": None}
    reason = "HTTP %s" % status
    if isinstance(parsed, dict) and parsed.get("message"):
        reason = "%s: %s" % (reason, parsed.get("message"))
    return fail(reason)


def natural_key(code):
    """证券代码的自然排序键：数字段按数值比较，其余按小写字符串比较"""
    parts = re.split(r"(\d+)", code)
    key = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part), ""))
        else:
            key.append((1, 0, part.lower()))
    return key


def collect_mvsv_files(root):
    """收集 SOURCE_DIR 下可同步的 .mvsv 文件并按（日期, 代码自然排序）升序排列

    递归遍历、排除 "." 开头的隐藏目录与隐藏文件；文件名须为
    {Code}_{Period}_{yyyyMMdd}.mvsv（尾段 8 位数字日期），不满足者跳过并记日志。

    :param root: 仓库根目录
    :return: [(rel_path, date, code), ...] 已按日期升序、代码自然排序升序
    """
    base = os.path.join(root, *SOURCE_DIR.split("/"))
    entries = []
    if not os.path.isdir(base):
        print("⚠️ 源目录不存在：%s" % base)
        return entries
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith(".") or not name.lower().endswith(".mvsv"):
                continue
            rel_path = os.path.relpath(os.path.join(dirpath, name), root)
            rel_path = rel_path.replace(os.sep, "/")
            stem = os.path.splitext(name)[0]
            segs = stem.split("_")
            date_str = segs[-1] if segs else ""
            code = segs[0] if len(segs) >= 3 else ""
            if len(segs) < 3 or len(date_str) != SRC_DATE_DIGITS or not date_str.isdigit():
                print("⏭️ 跳过（文件名不符合 {Code}_{Period}_{8位日期} 模式）：%s" % rel_path)
                continue
            entries.append((rel_path, int(date_str), code))
    entries.sort(key=lambda e: (e[1], natural_key(e[2])))
    return entries


def ingest_file(client, root, rel_path, batch_size):
    """把一份行情文件的全部记录分批增量入库（翻译 ingest），逐批记录明细

    单批失败不中断后续批次——跑完才能给出完整失败清单；Upsert 幂等，重试无副作用。

    :param client: SupabaseRestClient（或同形接口的替身，便于测试）
    :param root: 仓库根目录
    :param rel_path: 文件相对路径
    :param batch_size: 单批行数
    :return: 文件级汇总 dict：
             {"rel_path", "parsed", "batches": [{"batch_no","signature","submitted",
              "inserted","updated","failed","error"}...], "all_succeeded",
              "failure_digest"}
    :raises Exception: 解析阶段失败时抛出（未发过写请求，源文件必须保留）
    """
    local_path = os.path.join(root, rel_path.replace("/", os.sep))
    rows, _meta = parse_mvsv(local_path)
    if not rows:
        raise ValueError("解析结果为空，文件可能已损坏")

    # 按"非 null 字段集合"分组：同组内键集一致，才允许放进同一个批量请求
    groups = {}
    for row in rows:
        groups.setdefault(field_signature(row), []).append(row)
    print("📄 %s：解析 %d 行，按字段集合分成 %d 组"
          % (rel_path, len(rows), len(groups)))

    batches = []
    batch_no = 0
    for signature, group_rows in groups.items():
        for chunk in chunks(group_rows, batch_size):
            batch_no += 1
            batches.append(upsert_batch(client, batch_no, signature, chunk))
    return summarize_file(rel_path, len(rows), batches)


def upsert_batch(client, batch_no, signature, chunk):
    """提交一个批次并返回明细（翻译 upsertBatch）

    先查主键命中情况以拆出新增/更新；该查询失败不影响写入，只把拆分数记为 UNKNOWN。

    :param client: PostgREST 客户端
    :param batch_no: 批次序号（文件内从 1 开始）
    :param signature: 字段集合签名（说明该批为什么这样分组）
    :param chunk: 该批记录
    :return: 批次明细 dict
    """
    inserted = UNKNOWN
    try:
        existed = client.count_existing(chunk)
        inserted = len(chunk) - existed
    except Exception as e:  # 预查询失败不阻塞写入
        print("⚠️ 第 %d 批 新增/更新拆分的预查询失败（不影响写入，拆分数记为未知）：%s"
              % (batch_no, e))

    try:
        response = client.upsert_batch(TABLE, chunk, CONFLICT_COLUMNS)
        returned = len(json.loads(response))
        failed = max(0, len(chunk) - returned)
        error = None if returned == len(chunk) else \
            "回传 %d 行，与提交的 %d 行不一致" % (returned, len(chunk))
        updated = UNKNOWN if inserted == UNKNOWN else len(chunk) - inserted
    except Exception as e:
        error = "%s" % e
        inserted = UNKNOWN
        updated = UNKNOWN
        failed = len(chunk)

    outcome = {
        "batch_no": batch_no,
        "signature": signature,
        "submitted": len(chunk),
        "inserted": inserted,
        "updated": updated,
        "failed": failed,
        "error": error,
    }
    print("%s 第 %d 批 %s" % ("✅" if error is None else "❌", batch_no,
                             describe_batch(outcome)))
    return outcome


def describe_batch(outcome):
    """生成一行可读的批次明细（翻译 BatchOutcome.describe）"""
    if outcome["inserted"] == UNKNOWN or outcome["updated"] == UNKNOWN:
        split = "新增/更新=未知"
    else:
        split = "新增 %d、更新 %d" % (outcome["inserted"], outcome["updated"])
    suffix = "" if outcome["error"] is None else \
        "，失败 %d，原因：%s" % (outcome["failed"], outcome["error"])
    return ("提交 %d 行：%s、失败 %d%s；字段集合=[%s]"
            % (outcome["submitted"], split, outcome["failed"], suffix,
               outcome["signature"]))


def summarize_file(rel_path, parsed, batches):
    """生成文件级汇总（翻译 FileOutcome）"""
    inserted = sum(b["inserted"] for b in batches if b["inserted"] > 0)
    updated = sum(b["updated"] for b in batches if b["updated"] > 0)
    failed = sum(b["failed"] for b in batches)
    all_succeeded = all(b["error"] is None for b in batches)
    split_unknown = any(b["inserted"] == UNKNOWN for b in batches)
    ins_text = ("至少 %d" % inserted) if split_unknown else str(inserted)
    upd_text = ("至少 %d" % updated) if split_unknown else str(updated)
    digest = "；".join("第 %d 批: %s" % (b["batch_no"], b["error"])
                      for b in batches if b["error"] is not None)
    print("%s %s 汇总：解析 %d 行，分 %d 批，提交 %d 行，新增 %s，更新 %s，失败 %d%s"
          % ("✅" if all_succeeded else "⚠️", rel_path, parsed, len(batches),
             sum(b["submitted"] for b in batches), ins_text, upd_text, failed,
             "（全部成功）" if all_succeeded else "（有批次失败，详见上方 ❌ 日志）"))
    return {
        "rel_path": rel_path,
        "parsed": parsed,
        "batches": batches,
        "all_succeeded": all_succeeded,
        "failure_digest": digest,
    }


def repo_root():
    """返回仓库根目录（绝对路径）；本脚本固定位于 <仓库根>/.github/Python/ 下"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def resolve_branch():
    """解析要处理的分支名：命令行参数 → CURR_BRANCH → GITHUB_REF_NAME → 默认 quote"""
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    for env_key in ("CURR_BRANCH", "GITHUB_REF_NAME"):
        value = os.environ.get(env_key, "").strip()
        if value:
            return value
    return DEFAULT_BRANCH


def env_int(name, default):
    """读取整数型环境变量；非法值告警后取默认值"""
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
        if parsed <= 0:
            raise ValueError
        return parsed
    except ValueError:
        print("⚠️ 环境变量 %s=%r 不是正整数，回退默认值 %d" % (name, value, default))
        return default


def env_bool(name, default=False):
    """读取布尔型环境变量（"true"/"1"/"yes" 为真，大小写不敏感）"""
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in ("true", "1", "yes", "on")


def main():
    """主流程：配置 → 收集排序 → 逐文件分批入库 → 删除决策 → 汇总"""
    _ensure_console_utf8()
    branch = resolve_branch()
    batch_size = env_int("SUPABASE_BATCH_SIZE", DEFAULT_BATCH_SIZE)
    file_limit = env_int("SUPABASE_FILE_LIMIT", DEFAULT_FILE_LIMIT)
    enable_delete = env_bool("SUPABASE_ENABLE_DELETE", False)
    git_token = os.environ.get("GIT_COMMIT_TOKEN", "").strip()

    print("======== Supabase 行情同步开始 ========")
    print("分支 (branch) = %s" % branch)
    print("批大小 (batchSize) = %d，文件数上限 (fileLimit) = %d，删除开关 (enableDelete) = %s"
          % (batch_size, file_limit, "开" if enable_delete else "关（安全模式）"))

    # 凭据：只从环境变量读取，缺失即致命退出（不打印取值）
    project_ref = os.environ.get("SUPABASE_PROJECT_REF", "").strip()
    api_key = os.environ.get("SUPABASE_KEY", "").strip()
    if not project_ref or not api_key:
        print("❌ 缺少 SUPABASE_PROJECT_REF / SUPABASE_KEY 环境变量，无法连接 Supabase")
        return 1
    try:
        client = SupabaseRestClient(project_ref, api_key)
    except ValueError as e:
        print("❌ %s" % e)
        return 1

    root = repo_root()
    print("仓库根 = %s" % root)

    all_files = collect_mvsv_files(root)
    print("收集完成：Day 目录下可同步的 .mvsv 共 %d 个（按日期升序、代码自然排序）" % len(all_files))
    if not all_files:
        print("没有可同步的 .mvsv 文件，无事可做")
        return 0
    picked = all_files[:file_limit]
    if len(picked) < len(all_files):
        print("本次按 fileLimit=%d 只处理前 %d 个，其余 %d 个留待下次："
              % (file_limit, len(picked), len(all_files) - len(picked)))
        for rel_path, _d, _c in all_files[file_limit:]:
            print("    - %s" % rel_path)
    print("-------- 本次处理清单（%d 个）--------" % len(picked))
    for idx, (rel_path, date, code) in enumerate(picked, 1):
        print("  %2d. %s（日期=%d，代码=%s）" % (idx, rel_path, date, code))

    deleted, delete_failed = [], []
    pending_delete, kept = [], []
    for rel_path, _date, _code in picked:
        try:
            outcome = ingest_file(client, root, rel_path, batch_size)
        except Exception as e:
            # 解析阶段就失败：没发过写请求，源文件必须保留
            print("❌ 文件 %s 解析/入库过程异常，保留源文件：%s" % (rel_path, e))
            kept.append("%s：%s" % (rel_path, e))
            continue

        if not outcome["all_succeeded"]:
            print("🔒 决定：保留源文件（%s）——未满足删除条件：%s"
                  % (rel_path, outcome["failure_digest"]))
            kept.append("%s：%s" % (rel_path, outcome["failure_digest"]))
            continue

        if not enable_delete:
            print("🕐 验证通过，待删除（删除开关未开启，本次不删）：%s" % rel_path)
            pending_delete.append(rel_path)
            continue

        print("🗑️ 决定：删除源文件（%s）——该文件全部批次成功且删除开关已开启" % rel_path)
        result = delete_branch_file(branch, rel_path, git_token)
        if result["success"]:
            deleted.append(rel_path)
        else:
            delete_failed.append("%s：%s" % (rel_path, result["message"]))

    print("======== Supabase 行情同步结束 ========")
    print("合计：文件 %d 份，已删除 %d 份，验证通过待删除 %d 份，验证未通过保留 %d 份，删除失败 %d 份"
          % (len(picked), len(deleted), len(pending_delete), len(kept), len(delete_failed)))
    if pending_delete:
        print("待删除清单（开启 enableDelete 开关重跑即会删除）：")
        for rel_path in pending_delete:
            print("    - %s" % rel_path)
    if kept:
        print("保留清单（验证未通过，修复后重跑即可补齐）：")
        for item in kept:
            print("    - %s" % item)
    if delete_failed:
        print("删除失败清单（已入库成功，可手动删除或重跑）：")
        for item in delete_failed:
            print("    - %s" % item)
    return 0


if __name__ == "__main__":
    sys.exit(main())
