#!/usr/bin/env python3
"""Task 12: FTMM 分钟 Latest.mvsv 开盘价/前收价回填 + 按日归档 + 窗口裁剪

适用目录: Data/Finv/SecurityQuoteV5/FTMM/Min/<Code>/Latest.mvsv
归档输出: {ArchiveRel}/FTMM/V5/{Region}_{Market}/{USC}/{Region}_{Market}_{USC}_{TypeKLine}_{Provider}_Day_yyyyMMdd.mvsv
  - ArchiveRel / LatestWindowDays / DailyArchiveAfterDays / DailyArchivePlusDays
    读取自 .github/Python/Quote/Config.yaml;
  - Region / Market / USC / TypeKLine / Provider(=DataProvider) 解析自 Latest.mvsv
    头部 (# 开头行); yyyyMMdd 来自被切分数据行的 d 字段。

数据格式 (FTMM 原生 12 列, 与 SecuQuote 的 11 列格式不兼容):
  ts|d|t|o|h|l|c|v|t|cp|cr|lc
  = Timestamp|Date|Time|Open|High|Low|Close|Volume|Turnover|ChangePrice|ChangeRatio|LastClose

处理流程 (单个证券, 顺序固定为 回填 -> 归档 -> 裁剪):
  1. 回填: 相邻两行 ts 恰好相差 60 秒时, 用上一条的 Close 回填当前行的 Open 与
     LastClose (无论原值是否为空均覆盖; 上一条 Close 为空或间隔不为 60 秒则保持原值);
  2. 归档: 行日期落在 [今天-DailyArchiveAfterDays-DailyArchivePlusDays, 今天-DailyArchiveAfterDays)
     自然日窗口内的数据, 按日切分、与已有归档文件 merge 去重后写入归档路径;
     内容无变化则不提交 —— 同一文件会被连续多天重复归档, 无新增数据时必须保持
     已归档文件原样, 保证 git 修改历史干净 (更早的数据由 Task02 覆盖, 本任务不补归档);
  3. 裁剪: 移除 ts 早于 今天-LatestWindowDays (证券时区 00:00) 的数据, 更新 Latest.mvsv。

日期/时区口径: "今天" 与所有自然日边界均按证券时区 (头部 TimeZone; 缺失或非法时
回退 Asia/Shanghai 并打日志)。按日切分以数据行的 d 字段为准, 同时校验 ts 按证券
时区换算的日期与 d 是否一致, 不一致打 WARNING (上游步骤可能算错, 仅提醒不改数)。

递交方式: 同 Task11 —— 优先走 GitHub Contents REST API 直写远端分支。
  本目录数据由外部作业高频提交, 本地 checkout 的 HEAD 随时可能落后, `git push`
  会因 non-fast-forward 被拒; Contents API 以服务端最新状态为处理基准
  (GET 远端内容后再计算), sha 冲突时重新拉取内容重算再重试。
  同一工作流内 Task02 先一步执行并通过本地 git add/commit/push 独立递交;
  本任务在 API 模式下不产生任何本地 commit、不写工作区, 不干扰 Task02 的递交
  原子性, 两个任务严格串行。无 GITHUB_TOKEN 时降级为本地 git commit + push
  (仅用于本地/离线场景)。

本脚本完全自包含, 不引用 Task01AggregateLatest.py / Task02ArchiveDaily.py /
Task11AggregateLatest.py / common 包 / GitHubCommitContent.py 的任何代码
(Task02 后续将退役, 两者 mvsv 头部与数据列不兼容, 代码刻意隔离)。
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import logging
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, date, time as dt_time, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 兜底 (工作流程使用 3.13, 正常不会走到)
    ZoneInfo = None

try:
    import yaml
except ImportError:  # 离线/无依赖环境兜底: 用内置的扁平键解析
    yaml = None

TASK_NAME = 'Task12ArchiveDaily'

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
CONFIG_PATH = SCRIPT_DIR / 'Config.yaml'

DATA_REL = 'Data/Finv/SecurityQuoteV5/FTMM/Min'
DATA_DIR = REPO_ROOT / 'Data' / 'Finv' / 'SecurityQuoteV5' / 'FTMM' / 'Min'

LATEST_NAME = 'Latest.mvsv'
N_COLS = 12
TS_IDX = 0        # Timestamp (UTC 秒)
DATE_IDX = 1      # d  (yyyyMMdd, 证券时区自然日)
TIME_IDX = 2      # t  (HHMMSS)
OPEN_IDX = 3      # o
HIGH_IDX = 4      # h
LOW_IDX = 5       # l
CLOSE_IDX = 6     # c
VOLUME_IDX = 7    # v
TURNOVER_IDX = 8  # t (成交额, 与时间字段短名重复, 位置区分)
CP_IDX = 9        # cp
CR_IDX = 10       # cr
LC_IDX = 11       # lc (前收价)

# Latest/归档文件元数据输出顺序 (与 FTMM 原始文件头部一致)
META_ORDER = [
    '标题', '字段名称', 'DataProvider', 'Field', 'FieldName', 'FieldType',
    'TypeKLine', 'Count', 'Symbol', 'SecuCode', 'USC', 'Name',
    'Region', 'Market', 'TimeZone', 'Currency',
]
# 易变元数据: 随采集时刻变化, 不进入 Latest 写回与归档结果 (避免无数据变化时反复改写)
VOLATILE_META_KEYS = ('FetchTime', '采集时间', 'Remark', '备注')

# 归档文件名所需的头部键 (缺失则无法拼路径, 该证券直接失败)
ARCHIVE_META_KEYS = ('Region', 'Market', 'USC', 'TypeKLine', 'DataProvider')

# 配置默认值 (Config.yaml 缺键时兜底; quote 分支 Config.yaml 已登记全部 4 键)
DEFAULT_ARCHIVE_REL = 'Archive/Finv/SecuQuote'
DEFAULT_LATEST_WINDOW_DAYS = 36
DEFAULT_DAILY_ARCHIVE_AFTER_DAYS = 1
DEFAULT_DAILY_ARCHIVE_PLUS_DAYS = 3

FALLBACK_TZ_NAME = 'Asia/Shanghai'
UTC = timezone.utc
BJT_FIXED = timezone(timedelta(hours=8))  # 无 tzdata 时的最终兜底

DEFAULT_BRANCH = 'quote'
API_BASE_ENV = 'GITHUB_API_BASE'
API_RETRY = 3
API_RETRY_INTERVAL = 5
GIT_PUSH_RETRIES = 3
GIT_RETRY_INTERVAL = 5

log = logging.getLogger(TASK_NAME)


# ---------- 配置 (.github/Python/Quote/Config.yaml) ----------

def loadConfig():
    """读取 Config.yaml, 返回本任务所需的 4 个参数 (缺键用默认值兜底)。"""
    data = {}
    if CONFIG_PATH.exists():
        text = CONFIG_PATH.read_text(encoding='utf-8')
        if yaml is not None:
            raw = yaml.safe_load(text) or {}
            data = {str(k): v for k, v in raw.items()}
        else:
            # 极简扁平键解析 (仅支持顶层 Key: Value, 本任务所需键均为顶层键)
            for line in text.split('\n'):
                s = line.strip()
                if not s or s.startswith('#') or ':' not in s:
                    continue
                k, v = s.split(':', 1)
                data[k.strip()] = v.strip().strip('\'"')
    else:
        log.warning(f'配置文件不存在: {CONFIG_PATH}, 全部使用默认值')

    def toInt(value, default):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    return {
        'archive_rel': str(data.get('ArchiveRel') or DEFAULT_ARCHIVE_REL).strip().strip('/'),
        'latest_window_days': toInt(data.get('LatestWindowDays'), DEFAULT_LATEST_WINDOW_DAYS),
        'daily_archive_after_days': toInt(data.get('DailyArchiveAfterDays'), DEFAULT_DAILY_ARCHIVE_AFTER_DAYS),
        'daily_archive_plus_days': toInt(data.get('DailyArchivePlusDays'), DEFAULT_DAILY_ARCHIVE_PLUS_DAYS),
    }


# ---------- 时区 ----------

def resolveTz(tzKey, clog):
    """按头部 TimeZone 解析证券时区; 缺失/非法时回退 Asia/Shanghai。

    :param tzKey: 头部 TimeZone 值 (如 "Asia/Shanghai"; FX 类可能为空串)
    :param clog: 日志器
    :return: tzinfo
    """
    key = (tzKey or '').strip()
    if key and ZoneInfo is not None:
        try:
            return ZoneInfo(key)
        except Exception:
            clog.warning(f'TimeZone "{key}" 无法解析, 回退 {FALLBACK_TZ_NAME}')
    elif not key:
        clog.info(f'头部 TimeZone 为空, 按 {FALLBACK_TZ_NAME} 处理')
    if ZoneInfo is not None:
        try:
            return ZoneInfo(FALLBACK_TZ_NAME)
        except Exception:
            pass
    clog.warning(f'tzdata 不可用, 使用固定 UTC+8')
    return BJT_FIXED


def dateFromTs(ts, tz):
    """UTC 秒时间戳 -> tz 下的自然日。"""
    return datetime.fromtimestamp(int(ts), UTC).astimezone(tz).date()


def parseRowDate(dStr):
    """解析 d 字段 (yyyyMMdd) 为 date; 非法返回 None。"""
    s = (dStr or '').strip()
    if len(s) == 8 and s.isdigit():
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            return None
    return None


# ---------- MVSV 解析与序列化 (FTMM 12 列格式专用) ----------

def parseMvsvText(text):
    """解析 mvsv 文本, 返回 (meta: dict, rows: list[list[str]])。"""
    text = text.replace('\r\n', '\n')
    meta, rows = {}, []
    in_data = False
    for line in text.split('\n'):
        stripped = line.strip()
        if not stripped:
            if meta:
                in_data = True
            continue
        if not in_data and stripped.startswith('#'):
            rest = stripped[1:].strip()
            colon = rest.find(':')
            if colon < 0:
                continue
            key = rest[:colon].strip()
            val = rest[colon + 1:].strip()
            if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                val = val[1:-1]
            meta[key] = val
        else:
            rows.append(stripped.split('|'))
    return meta, rows


def parseMvsv(path):
    """解析本地 mvsv 文件, 返回 (meta, rows)。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f'文件不存在: {p}')
    return parseMvsvText(p.read_text(encoding='utf-8'))


def _fmtMeta(key, value):
    if not value or any(c in value for c in ':#|'):
        return f'# {key} : "{value}"'
    return f'# {key} : {value}'


def renderMvsv(meta, rows):
    """渲染 MVSV 正文并返回规范字节串 (LF, 末尾无多余换行)。Count 按实际行数写入。"""
    m = dict(meta)
    m['Count'] = str(len(rows))
    ordered = [k for k in META_ORDER if k in m]
    extras = sorted(k for k in m if k not in META_ORDER)
    lines = [_fmtMeta(k, m[k]) for k in ordered + extras]
    lines.append('')
    lines.extend('|'.join(r) for r in rows)
    return '\n'.join(lines).encode('utf-8')


def serializeMvsv(path, meta, rows, *, onlyIfChanged=False):
    """原子写本地 mvsv 文件 (tmp + rename)。返回 (是否落盘, 规范字节内容)。"""
    p = Path(path)
    data = renderMvsv(meta, rows)
    if onlyIfChanged and p.exists():
        try:
            if p.read_bytes() == data:
                return False, data
        except OSError:
            pass
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix='.tmp_', suffix='.mvsv')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            f.write(data.decode('utf-8'))
        os.replace(tmp, str(p))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True, data


def normalizeRows(rows, clog):
    """过滤坏行 (列数 != 12 或 ts 非整数) 并按 ts 升序排序。返回有效行。"""
    ok, bad = [], 0
    for r in rows:
        if len(r) != N_COLS:
            bad += 1
            continue
        try:
            int(r[TS_IDX])
        except ValueError:
            bad += 1
            continue
        ok.append(r)
    if bad:
        clog.warning(f'坏行跳过: {bad} (列数 != {N_COLS} 或 ts 非整数)')
    ok.sort(key=lambda r: int(r[TS_IDX]))
    return ok


def mergeRows(existingRows, newRows):
    """按 ts 合并去重: 同 ts 新行 (来自最新 Latest) 覆盖旧行, 结果按 ts 升序。"""
    rowMap = {}
    for r in existingRows:
        if len(r) == N_COLS:
            rowMap[r[TS_IDX]] = r
    for r in newRows:
        if len(r) == N_COLS:
            rowMap[r[TS_IDX]] = r
    return sorted(rowMap.values(), key=lambda r: int(r[TS_IDX]))


# ---------- ① 回填 Open / LastClose ----------

def backfillOpenLastClose(rows):
    """上一条 ts 恰好为当前 ts-60 时, 用其 Close 回填当前行 Open 与 LastClose。

    无论原值是否为空均覆盖 (采集端可能给出不可靠的旧值); 上一条 Close 为空时
    无法取值, 跳过以免清掉已有值。返回实际发生变更的行数。
    """
    filled = 0
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]
        if int(cur[TS_IDX]) - int(prev[TS_IDX]) != 60:
            continue
        close = prev[CLOSE_IDX].strip()
        if not close:
            continue
        if cur[OPEN_IDX] != close or cur[LC_IDX] != close:
            cur[OPEN_IDX] = close
            cur[LC_IDX] = close
            filled += 1
    return filled


# ---------- ③ 按日归档 ----------

def splitRowsByDate(rows, tz, clog):
    """按行 d 字段分组 (d 为准), 校验 ts 按证券时区换算的日期与 d 的一致性。

    d 缺失/非法时回退 ts 换算值; 两者不一致时计数并打 WARNING (上游日期字段
    可能与时区不匹配, 仅提醒, 分组仍以 d 字段为准)。
    """
    byDate = {}
    mismatch = dMissing = 0
    samples = []
    for r in rows:
        derived = dateFromTs(r[TS_IDX], tz)
        d = parseRowDate(r[DATE_IDX])
        if d is None:
            dMissing += 1
            d = derived
        elif d != derived:
            mismatch += 1
            if len(samples) < 3:
                samples.append(f'ts={r[TS_IDX]} d={r[DATE_IDX]} 时区换算={derived.strftime("%Y%m%d")}')
        byDate.setdefault(d, []).append(r)
    if dMissing:
        clog.warning(f'd 字段缺失/非法: {dMissing} 行 (已按 ts 时区换算日期分组)')
    if mismatch:
        clog.warning(
            f'd 字段与时区换算日期不一致: {mismatch} 行 (按 d 分组), 样例: {"; ".join(samples)}'
        )
    return byDate


def archiveRelPath(archiveRel, meta, day):
    """拼归档仓库内路径: {ArchiveRel}/FTMM/V5/{Region}_{Market}/{Region}_{Market}_{USC}_{TypeKLine}_{Provider}_Day_yyyyMMdd.mvsv"""
    values = {k: (meta.get(k) or '').strip() for k in ARCHIVE_META_KEYS}
    missing = [k for k, v in values.items() if not v]
    if missing:
        raise RuntimeError(f'头部缺少归档命名所需字段: {missing}')
    folder = f'{values["Region"]}_{values["Market"]}/{values["USC"]}'
    name = (f'{values["Region"]}_{values["Market"]}_{values["USC"]}_'
            f'{values["TypeKLine"]}_{values["DataProvider"]}_Day_{day.strftime("%Y%m%d")}.mvsv')
    return f'{archiveRel}/FTMM/V5/{folder}/{name}'


def archiveWindow(today, afterDays, plusDays):
    """归档自然日窗口 [今天-after-plus, 今天-after), 返回 (start, endExclusive)。"""
    return today - timedelta(days=afterDays + plusDays), today - timedelta(days=afterDays)


def archiveMetaFor(meta):
    """归档文件头: 复制 Latest 头部并剔除易变字段 (Count 由 render 重算)。"""
    m = dict(meta)
    for k in VOLATILE_META_KEYS:
        m.pop(k, None)
    m.pop('Count', None)
    return m


# ---------- ② 裁剪 Latest 窗口 ----------

def trimRows(rows, today, windowDays, tz):
    """移除 ts 早于 今天-windowDays (tz 00:00) 的行。返回 (保留行, 裁剪行数)。"""
    cutoffDate = today - timedelta(days=windowDays)
    cutoffTs = int(datetime.combine(cutoffDate, dt_time(0, 0), tzinfo=tz).astimezone(UTC).timestamp())
    kept = [r for r in rows if int(r[TS_IDX]) >= cutoffTs]
    return kept, len(rows) - len(kept), cutoffDate


# ---------- GitHub Contents REST API ----------

class ShaConflict(Exception):
    """PUT 时远端 sha 已变化 (409), 调用方重新拉取内容重算后重试。"""


def apiEnabled():
    """存在 token 且能解析出 owner/repo 时走 API 递交。"""
    return bool(os.environ.get('GITHUB_TOKEN')) and bool(resolveRepoSlug())


def resolveRepoSlug():
    envRepo = os.environ.get('GITHUB_REPOSITORY', '').strip()
    if envRepo:
        return envRepo
    cp = runGit(['remote', 'get-url', 'origin'])
    url = cp.stdout.strip() if cp.returncode == 0 else ''
    if not url:
        return ''
    url = url.rstrip('/')
    if url.startswith('git@'):
        url = url.split(':', 1)[-1]
    for prefix in ('https://github.com/', 'http://github.com/', 'ssh://github.com/'):
        if url.startswith(prefix):
            url = url[len(prefix):]
            break
    return url[:-4] if url.endswith('.git') else url


def resolveBranch():
    """目标分支: 仅取显式配置, 缺省固定在 DEFAULT_BRANCH。

    刻意不读 GITHUB_REF_NAME: 本流水线归属 quote 分支 (checkout 步骤 ref: quote),
    而 workflow_dispatch 从 Actions 页面触发时所在的是仓库默认分支 dev —— 若跟随
    触发 ref 漂移, 就会出现「拉 quote 的内容、却递交到 dev」的错配。
    """
    v = os.environ.get('QUOTE_BRANCH', '').strip()
    if v:
        return v
    ref = os.environ.get('GITHUB_REF_NAME', '').strip()
    if ref and ref != DEFAULT_BRANCH:
        log.warning(
            f'当前触发 ref 为 {ref}, 与固定目标分支 {DEFAULT_BRANCH} 不一致; '
            f'如需递交到其它分支请显式设置 QUOTE_BRANCH'
        )
    return DEFAULT_BRANCH


def resolveApiBase():
    return os.environ.get(API_BASE_ENV, '').strip() or 'https://api.github.com'


def blobSha(data):
    """计算 git blob sha1, 与 Contents API 返回的 sha 可直接比对判断是否已有改动。"""
    h = hashlib.sha1()
    h.update(b'blob %d\0' % len(data))
    h.update(data)
    return h.hexdigest()


def _apiRequest(method, path, *, payload=None, params=None):
    base = resolveApiBase()
    repo = resolveRepoSlug()
    token = os.environ.get('GITHUB_TOKEN', '')
    url = f'{base}/repos/{repo}/{path}'
    if params:
        url += '?' + urllib.parse.urlencode(params)
    body = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header('Accept', 'application/vnd.github+json')
    req.add_header('X-GitHub-Api-Version', '2022-11-28')
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    if body is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as e:
        return e.code, json.loads((e.read() or b'{}').decode('utf-8') or '{}')


def _apiPath(relPosix):
    return 'contents/' + urllib.parse.quote(relPosix, safe='/')


def getRemoteFile(relPosix, branch):
    """GET 远端文件, 返回 (sha, 内容字节); 不存在返回 (None, None)。

    内容缺失 (>1MB 时 contents 接口不内联 content) 时回退 git blob API。
    """
    status, data = _apiRequest('GET', _apiPath(relPosix), params={'ref': branch})
    if status == 404:
        return None, None
    if status != 200:
        raise RuntimeError(f'GET {relPosix} 失败 ({status}): {data.get("message", "")}')
    sha = data.get('sha')
    content = data.get('content')
    if content:
        try:
            return sha, base64.b64decode(content)
        except (TypeError, ValueError):
            pass
    if not sha:
        raise RuntimeError(f'GET {relPosix} 响应异常 (无 sha/content, 可能指向目录)')
    bstatus, bdata = _apiRequest('GET', f'git/blobs/{sha}')
    if bstatus != 200 or not bdata.get('content'):
        raise RuntimeError(f'GET blob {sha} 失败 ({bstatus}): {bdata.get("message", "")}')
    return sha, base64.b64decode(bdata['content'])


def putRemoteFile(relPosix, data, message, branch, sha):
    """PUT 创建/更新远端文件 (sha=None 表示新建)。409 抛 ShaConflict 交由调用方重试。"""
    payload = {
        'message': message,
        'content': base64.b64encode(data).decode('ascii'),
        'branch': branch,
    }
    if sha:
        payload['sha'] = sha
    status, resp = _apiRequest('PUT', _apiPath(relPosix), payload=payload)
    if status in (200, 201):
        log.info(f'已递交: {relPosix} -> {resp.get("commit", {}).get("sha", "")[:9]}')
        return
    if status == 409:
        raise ShaConflict(relPosix)
    raise RuntimeError(f'PUT {relPosix} 失败 ({status}): {resp.get("message", "")}')


def syncLocalToRemote(branch):
    """递交完成后把本地分支/索引对齐远端, 使工作区干净且与远端一致 (供后续 VerifyCleanPush)。

    前置祖先校验保证本地不存在未推送 commit; API 模式下本任务不写工作区,
    此处仅快进本地分支引用, 不会影响 Task02 已完成的递交。
    """
    cp = runGit(['fetch', 'origin', branch])
    if cp.returncode != 0:
        log.warning(f'git fetch 失败: {cp.stderr.strip()}')
        return
    anc = runGit(['merge-base', '--is-ancestor', 'HEAD', 'FETCH_HEAD'])
    if anc.returncode != 0:
        log.warning('本地 HEAD 与远端存在分叉, 跳过本地对齐')
        return
    cp = runGit(['reset', '--hard', 'FETCH_HEAD'])
    if cp.returncode != 0:
        log.warning(f'git reset 失败: {cp.stderr.strip()}')
        return
    log.info(f'本地已对齐远端 origin/{branch}')


# ---------- 本地 git (无 token 时的降级路径) ----------

def runGit(args):
    return subprocess.run(
        ['git', *args], cwd=str(REPO_ROOT), capture_output=True, text=True
    )


def gitCommit(message):
    cp = runGit(['commit', '-m', message])
    if cp.returncode != 0:
        if 'nothing to commit' in (cp.stdout + cp.stderr):
            return ''
        log.error(f'git commit 失败: {cp.stderr.strip()}')
        raise RuntimeError(f'git commit 失败: {cp.stderr.strip()}')
    sha = runGit(['rev-parse', '--short', 'HEAD'])
    return sha.stdout.strip() if sha.returncode == 0 else ''


def gitPush(branch):
    cp = runGit(['remote'])
    if not cp.stdout.strip():
        log.warning('未配置 git remote, 跳过 push')
        return
    # 显式指定目标分支, 避免 push.default 策略或当前 checkout 分支导致推错分支
    refspec = f'HEAD:refs/heads/{branch}'
    for i in range(1, GIT_PUSH_RETRIES + 1):
        cp = runGit(['push', 'origin', refspec])
        if cp.returncode == 0:
            return
        log.warning(f'git push 第 {i}/{GIT_PUSH_RETRIES} 次失败: {cp.stderr.strip()}')
        if i < GIT_PUSH_RETRIES:
            time.sleep(GIT_RETRY_INTERVAL)
    raise RuntimeError(f'git push 重试 {GIT_PUSH_RETRIES} 次后仍失败')


# ---------- 单证券处理 ----------

def prepareRows(meta, rows, clog):
    """公共预处理: 剔除易变元数据 + 过滤坏行排序 + 回填。返回 (meta, rows, filled)。"""
    m = dict(meta)
    for k in VOLATILE_META_KEYS:
        m.pop(k, None)
    m.pop('Count', None)
    valid = normalizeRows(rows, clog)
    filled = backfillOpenLastClose(valid)
    if filled:
        clog.info(f'回填 Open/LastClose: {filled} 行')
    return m, valid, filled


def selectArchiveGroups(meta, rows, cfg, tz, today, clog):
    """③ 归档候选: 按 d 分组后筛出窗口内日期。返回 {date: rows} (已按日升序 dict)。"""
    start, end = archiveWindow(today, cfg['daily_archive_after_days'], cfg['daily_archive_plus_days'])
    byDate = splitRowsByDate(rows, tz, clog)
    groups = {d: rs for d, rs in sorted(byDate.items()) if start <= d < end}
    clog.info(
        f'归档窗口: [{start.strftime("%Y%m%d")}, {end.strftime("%Y%m%d")}) '
        f'(After={cfg["daily_archive_after_days"]} + Plus={cfg["daily_archive_plus_days"]}), '
        f'命中 {len(groups)} 天 / {sum(len(v) for v in groups.values())} 行'
    )
    return groups


def processCodeApi(code, cfg, branch, clog):
    """API 模式: 以远端最新内容为基准处理, 全部经 Contents API 递交, 不写工作区。"""
    latestRel = f'{DATA_REL}/{code}/{LATEST_NAME}'
    archived = []
    latestCommitted = False

    # Latest 冲突重试: 重新拉取远端内容, 重算回填+裁剪后再 PUT (归档只做一遍)
    for attempt in range(1, API_RETRY + 1):
        sha, baseBytes = getRemoteFile(latestRel, branch)
        if sha is None:
            clog.info('远端 Latest.mvsv 不存在, 跳过')
            return
        meta, rows = parseMvsvText(baseBytes.decode('utf-8'))
        meta, rows, filled = prepareRows(meta, rows, clog)
        if not rows:
            clog.info('Latest.mvsv 无有效数据行, 跳过')
            return
        tz = resolveTz(meta.get('TimeZone'), clog)
        today = datetime.now(tz).date()

        # ③ 归档 (仅首轮执行; PUT 冲突在归档内部自行重试)
        if attempt == 1:
            archived = archiveDaysApi(code, meta, rows, cfg, tz, today, branch, clog)

        # ② 裁剪 + 写回 Latest
        kept, trimmed, cutoffDate = trimRows(rows, today, cfg['latest_window_days'], tz)
        data = renderMvsv(meta, kept)
        if data == baseBytes:
            clog.info(f'Latest 无变化 ({len(kept)} 行), 跳过递交')
            return
        if trimmed:
            clog.info(f'Latest 裁剪: {trimmed} 行 (cutoff={cutoffDate.strftime("%Y%m%d")} 证券时区 00:00, 窗口 {cfg["latest_window_days"]} 天)')
        parts = []
        if filled:
            parts.append(f'backfill {filled} rows')
        if trimmed:
            parts.append(f'trim {trimmed} rows')
        detail = '; '.join(parts) if parts else 'meta update'
        try:
            putRemoteFile(latestRel, data, f'[Quote] Maintain FTMM Latest for {code} ({detail})', branch, sha)
            latestCommitted = True
        except ShaConflict:
            clog.warning(f'Latest sha 冲突, 第 {attempt}/{API_RETRY} 次重拉重算')
            time.sleep(API_RETRY_INTERVAL)
            continue
        break
    else:
        raise RuntimeError(f'Latest PUT 重试 {API_RETRY} 次后仍冲突')
    if not archived and not latestCommitted:
        clog.info('归档与 Latest 均无变化')


def archiveDaysApi(code, meta, rows, cfg, tz, today, branch, clog):
    """③ 归档 (API 模式): 逐日 merge 远端已归档文件, 内容无变化不递交。返回已递交日期列表。"""
    groups = selectArchiveGroups(meta, rows, cfg, tz, today, clog)
    committed = []
    dayMeta = archiveMetaFor(meta)
    for day, dayRows in groups.items():
        ds = day.strftime('%Y%m%d')
        rel = archiveRelPath(cfg['archive_rel'], meta, day)
        for attempt in range(1, API_RETRY + 1):
            sha, baseBytes = getRemoteFile(rel, branch)
            if baseBytes is not None:
                _, exRows = parseMvsvText(baseBytes.decode('utf-8'))
            else:
                exRows = []
            merged = mergeRows(exRows, dayRows)
            data = renderMvsv(dayMeta, merged)
            if baseBytes is not None and data == baseBytes:
                clog.info(f'无变化跳过: {Path(rel).name} ({len(merged)} 行)')
                break
            try:
                putRemoteFile(rel, data, f'[Quote] Archive FTMM daily for {code} ({ds})', branch, sha)
                clog.info(f'归档: {Path(rel).name} ({len(dayRows)} 行 -> 合并后 {len(merged)} 行)')
                committed.append(ds)
            except ShaConflict:
                clog.warning(f'{Path(rel).name} sha 冲突, 第 {attempt}/{API_RETRY} 次重试')
                time.sleep(API_RETRY_INTERVAL)
                continue
            break
        else:
            raise RuntimeError(f'{rel} PUT 重试 {API_RETRY} 次后仍冲突')
    return committed


def processCodeLocal(code, cfg, branch, clog):
    """降级模式 (无 GITHUB_TOKEN): 本地读写 + git add/commit/push, 仅本地/离线场景。"""
    latestPath = DATA_DIR / code / LATEST_NAME
    if not latestPath.exists():
        clog.info('Latest.mvsv 不存在, 跳过')
        return
    meta, rows = parseMvsv(latestPath)
    meta, rows, filled = prepareRows(meta, rows, clog)
    if not rows:
        clog.info('Latest.mvsv 无有效数据行, 跳过')
        return
    tz = resolveTz(meta.get('TimeZone'), clog)
    today = datetime.now(tz).date()

    changedFiles = []
    # ③ 归档
    groups = selectArchiveGroups(meta, rows, cfg, tz, today, clog)
    dayMeta = archiveMetaFor(meta)
    for day, dayRows in groups.items():
        rel = archiveRelPath(cfg['archive_rel'], meta, day)
        ap = REPO_ROOT / rel
        exRows = []
        if ap.exists():
            _, exRows = parseMvsv(ap)
        merged = mergeRows(exRows, dayRows)
        wrote, _ = serializeMvsv(ap, dayMeta, merged, onlyIfChanged=True)
        if wrote:
            clog.info(f'归档: {ap.name} ({len(dayRows)} 行 -> 合并后 {len(merged)} 行)')
            changedFiles.append(rel)
        else:
            clog.info(f'无变化跳过: {ap.name} ({len(merged)} 行)')

    # ② 裁剪 + 写回 Latest
    kept, trimmed, cutoffDate = trimRows(rows, today, cfg['latest_window_days'], tz)
    if trimmed:
        clog.info(f'Latest 裁剪: {trimmed} 行 (cutoff={cutoffDate.strftime("%Y%m%d")}, 窗口 {cfg["latest_window_days"]} 天)')
    wrote, _ = serializeMvsv(latestPath, meta, kept, onlyIfChanged=True)
    if wrote:
        clog.info(f'Latest 写回: {len(kept)} 行')
        changedFiles.append(f'{DATA_REL}/{code}/{LATEST_NAME}')

    if not changedFiles:
        clog.info('归档与 Latest 均无变化, 跳过提交')
        return
    for rel in changedFiles:
        runGit(['add', '--', rel])
    parts = []
    if filled:
        parts.append(f'backfill {filled} rows')
    if trimmed:
        parts.append(f'trim {trimmed} rows')
    detail = '; '.join(parts) if parts else 'archive'
    sha = gitCommit(f'[Quote] Archive FTMM daily + Maintain Latest for {code} ({detail})')
    if sha:
        clog.info(f'commit: {sha}')
    gitPush(branch)


def processCode(code, cfg, branch):
    clog = logging.getLogger(f'{TASK_NAME}.{code}')
    if apiEnabled():
        processCodeApi(code, cfg, branch, clog)
    else:
        processCodeLocal(code, cfg, branch, clog)


# ---------- 入口 ----------

def resolveCodes():
    envCodes = os.environ.get('QUOTE_CODES', '').strip()
    if envCodes:
        return [c.strip() for c in envCodes.split(',') if c.strip()]
    if not DATA_DIR.is_dir():
        return []
    return sorted(d.name for d in DATA_DIR.iterdir() if d.is_dir())


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        stream=sys.stdout,
    )
    log.info('=' * 50)
    cfg = loadConfig()
    branch = resolveBranch()
    log.info('任务十二: FTMM Latest 回填 + 按日归档 + 窗口裁剪')
    log.info(f'递交方式: {"Contents API" if apiEnabled() else "本地 git (降级)"} / 分支: {branch}')
    log.info(
        f'参数: ArchiveRel={cfg["archive_rel"]} LatestWindowDays={cfg["latest_window_days"]} '
        f'DailyArchiveAfterDays={cfg["daily_archive_after_days"]} '
        f'DailyArchivePlusDays={cfg["daily_archive_plus_days"]}'
    )
    codes = resolveCodes()
    if not codes:
        log.info('无待处理证券')
        return
    log.info(f'证券: {codes}')
    ok = fail = 0
    for code in codes:
        log.info(f'--- {code} 开始 ---')
        try:
            processCode(code, cfg, branch)
            log.info(f'--- {code} 完成 ---')
            ok += 1
        except Exception as e:
            log.error(f'{code} 失败: {e}')
            fail += 1
    if apiEnabled() and ok and not fail:
        syncLocalToRemote(branch)
    log.info(f'完成: 成功 {ok}, 失败 {fail}')
    if fail:
        sys.exit(1)


if __name__ == '__main__':
    main()
