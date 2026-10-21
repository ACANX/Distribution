#!/usr/bin/env python3
"""Task 11: Aggregate FTMM minute raw files into Latest.mvsv (incremental)

适用目录: Data/Finv/SecurityQuoteV5/FTMM/Min/<Code>/*.mvsv
原始文件命名: {Code}_Min_YYYYMMDD_HHMMSS.mvsv (多次采集生成, 增量覆盖)

数据格式 (FTMM 原生 12 列, 与 SecuQuote 的 11 列格式不兼容):
  ts|d|t|o|h|l|c|v|t|cp|cr|lc
  = Timestamp|Date|Time|Open|High|Low|Close|Volume|Turnover|ChangePrice|ChangeRatio|LastClose

递交方式: 优先走 GitHub Contents REST API 直写远端分支。
  本目录下的原始文件由外部作业高频提交, 本地 checkout 的 HEAD 随时可能落后,
  `git push` 会因 non-fast-forward 被拒且重试无效; Contents API 以服务端最新
  状态为准 (携带当前 blob sha), sha 冲突时重新 GET 取新 sha 再重试。
  无 GITHUB_TOKEN 时降级为本地 git commit + push (仅用于本地/离线场景)。

本脚本完全自包含, 不引用 Task01AggregateLatest.py 与 common 包的任何代码。
"""
import os
import base64
import hashlib
import json
import subprocess
import sys
import time
import logging
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TASK_NAME = 'Task11AggregateLatest'

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
DATA_DIR = REPO_ROOT / 'Data' / 'Finv' / 'SecurityQuoteV5' / 'FTMM' / 'Min'

LATEST_NAME = 'Latest.mvsv'
N_COLS = 12
TS_IDX = 0

# Latest 文件元数据输出顺序 (与 FTMM 原始文件头部一致)
META_ORDER = [
    '标题', '字段名称', 'DataProvider', 'Field', 'FieldName', 'FieldType',
    'TypeKLine', 'Count', 'Symbol', 'SecuCode', 'USC', 'Name',
    'Region', 'Market', 'TimeZone', 'Currency',
]
# 易变元数据: 随采集时刻变化, 不进入聚合结果
VOLATILE_META_KEYS = ('FetchTime', '采集时间', 'Remark', '备注')

# 原始文件清理开关 (默认关闭): 置 1/true 时, ts 范围被 Latest 完整覆盖的原始文件会被删除
CLEANUP_RAW_ENV = 'AGG_CLEANUP_RAW'

DEFAULT_BRANCH = 'quote'
API_BASE_ENV = 'GITHUB_API_BASE'
API_RETRY = 3
API_RETRY_INTERVAL = 5
GIT_PUSH_RETRIES = 3
GIT_RETRY_INTERVAL = 5

log = logging.getLogger(TASK_NAME)


# ---------- MVSV 解析与序列化 (FTMM 12 列格式专用) ----------

def parseMvsv(path):
    """解析 mvsv 文件, 返回 (meta: dict, rows: list[list[str]])。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f'文件不存在: {p}')
    text = p.read_text(encoding='utf-8').replace('\r\n', '\n')
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


def _fmtMeta(key, value):
    if not value or any(c in value for c in ':#|'):
        return f'# {key} : "{value}"'
    return f'# {key} : {value}'


def renderMvsv(meta, rows):
    """渲染 MVSV 正文并返回规范字节串 (LF)。Count 按实际行数写入。"""
    m = dict(meta)
    m['Count'] = str(len(rows))
    ordered = [k for k in META_ORDER if k in m]
    extras = sorted(k for k in m if k not in META_ORDER)
    lines = [_fmtMeta(k, m[k]) for k in ordered + extras]
    lines.append('')
    lines.extend('|'.join(r) for r in rows)
    return '\n'.join(lines).encode('utf-8')


def serializeMvsv(path, meta, rows, *, onlyIfChanged=False):
    """原子写 mvsv 文件 (tmp + rename)。返回 (是否落盘, 规范字节内容)。

    字节内容恒为 LF; 即便 onlyIfChanged 判定无需落盘, 也返回规范内容供远端比对
    (工作区文件可能被 core.autocrlf 转换过, 不能直接拿它算 sha)。
    """
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
        # newline='\n': 关闭平台换行转换, 保证落盘字节恒为 LF
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


def scanSourceFiles(codeDir):
    """扫描原始文件, 按文件名升序 (文件名内嵌采集时间戳, 升序即从旧到新)。"""
    p = Path(codeDir)
    if not p.is_dir():
        return []
    return sorted(
        str(f) for f in p.iterdir()
        if f.is_file() and f.suffix == '.mvsv' and f.name != LATEST_NAME
    )


# ---------- 聚合 ----------

def mergeRows(existingRows, sourceRowsList):
    """按 ts 合并去重: 同 ts 后写入者 (更新的采集文件) 覆盖先写入者, 结果按 ts 升序。

    仅接受 12 列的行, 其余记为坏行跳过。返回 (mergedRows, badCount)。
    """
    rowMap = {}
    bad = 0
    for r in existingRows:
        if len(r) == N_COLS:
            rowMap[r[TS_IDX]] = r
        else:
            bad += 1
    for srows in sourceRowsList:
        for r in srows:
            if len(r) == N_COLS:
                rowMap[r[TS_IDX]] = r
            else:
                bad += 1
    merged = sorted(rowMap.values(), key=lambda r: int(r[TS_IDX]))
    return merged, bad


# ---------- GitHub Contents REST API ----------

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


def _remotePath(localPath):
    rel = Path(localPath).resolve().relative_to(REPO_ROOT)
    return urllib.parse.quote(rel.as_posix(), safe='/')


def _apiRequest(method, remotePath, *, payload=None, params=None):
    base = resolveApiBase()
    repo = resolveRepoSlug()
    token = os.environ.get('GITHUB_TOKEN', '')
    url = f'{base}/repos/{repo}/contents/{remotePath}'
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


def getRemoteFile(remotePath, branch):
    """GET 远端文件元信息, 不存在返回 None。"""
    status, data = _apiRequest('GET', remotePath, params={'ref': branch})
    if status == 200:
        return data.get('sha')
    if status == 404:
        return None
    raise RuntimeError(f'GET {remotePath} 失败 ({status}): {data.get("message", "")}')


def putRemoteFile(localPath, data, message, branch):
    """通过 Contents API 创建/更新远端文件。返回 True 表示产生了新 commit。"""
    remotePath = _remotePath(localPath)
    localSha = blobSha(data)
    for attempt in range(1, API_RETRY + 1):
        remoteSha = getRemoteFile(remotePath, branch)
        if remoteSha == localSha:
            log.info(f'远端已是最新, 跳过更新: {Path(localPath).name}')
            return False
        payload = {
            'message': message,
            'content': base64.b64encode(data).decode('ascii'),
            'branch': branch,
        }
        if remoteSha:
            payload['sha'] = remoteSha
        status, resp = _apiRequest('PUT', remotePath, payload=payload)
        if status in (200, 201):
            log.info(f'已递交: {Path(localPath).name} -> {resp.get("commit", {}).get("sha", "")[:9]}')
            return True
        if status == 409:
            log.warning(f'{Path(localPath).name} sha 冲突, 第 {attempt}/{API_RETRY} 次重试')
            time.sleep(API_RETRY_INTERVAL)
            continue
        raise RuntimeError(f'PUT {remotePath} 失败 ({status}): {resp.get("message", "")}')
    raise RuntimeError(f'PUT {remotePath} 重试 {API_RETRY} 次后仍冲突')


def deleteRemoteFile(localPath, message, branch):
    """通过 Contents API 删除远端文件并删除本地副本。"""
    remotePath = _remotePath(localPath)
    remoteSha = getRemoteFile(remotePath, branch)
    if remoteSha is None:
        log.warning(f'远端不存在, 仅删除本地: {Path(localPath).name}')
    else:
        payload = {'message': message, 'sha': remoteSha, 'branch': branch}
        status, resp = _apiRequest('DELETE', remotePath, payload=payload)
        if status not in (200, 201):
            raise RuntimeError(f'DELETE {remotePath} 失败 ({status}): {resp.get("message", "")}')
        log.info(f'已删除: {Path(localPath).name}')
    os.remove(localPath)


def syncLocalToRemote(branch):
    """递交完成后把本地分支/索引对齐远端, 使工作区干净且无未推送 commit。"""
    cp = runGit(['fetch', 'origin', branch])
    if cp.returncode != 0:
        log.warning(f'git fetch 失败: {cp.stderr.strip()}')
        return
    anc = runGit(['merge-base', '--is-ancestor', 'HEAD', 'FETCH_HEAD'])
    if anc.returncode != 0:
        log.warning('本地 HEAD 与远端存在分叉, 跳过本地对齐')
        return
    # reset --hard: 一次性把本地分支/索引/工作区都对齐到 FETCH_HEAD
    # (比 reset --mixed + checkout-index 更确定: 远端新增的文件会被真正签出到本地)
    # 前置的祖先校验保证本地不存在未被推送的 commit, 因此不会丢失任何已产生的数据
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
        return True
    # 显式指定目标分支, 避免 push.default 策略或当前 checkout 分支导致推错分支
    refspec = f'HEAD:refs/heads/{branch}'
    for i in range(1, GIT_PUSH_RETRIES + 1):
        cp = runGit(['push', 'origin', refspec])
        if cp.returncode == 0:
            return True
        log.warning(f'git push 第 {i}/{GIT_PUSH_RETRIES} 次失败: {cp.stderr.strip()}')
        if i < GIT_PUSH_RETRIES:
            time.sleep(GIT_RETRY_INTERVAL)
    raise RuntimeError(f'git push 重试 {GIT_PUSH_RETRIES} 次后仍失败')


# ---------- 流程 ----------

def processCode(code, branch):
    """聚合单个证券目录。"""
    codeDir = DATA_DIR / code
    if not codeDir.is_dir():
        log.warning(f'目录不存在: {codeDir}')
        return
    latestPath = codeDir / LATEST_NAME
    exMeta, exRows = ({}, [])
    if latestPath.exists():
        exMeta, exRows = parseMvsv(latestPath)
    for k in VOLATILE_META_KEYS:
        exMeta.pop(k, None)

    sourceFiles = scanSourceFiles(codeDir)
    if not sourceFiles:
        log.info('无原始文件, 跳过')
        return
    log.info(f'原始文件: {len(sourceFiles)}')

    srcMeta, srcRowsList = {}, []
    for sf in sourceFiles:
        m, r = parseMvsv(sf)
        for k in VOLATILE_META_KEYS:
            m.pop(k, None)
        srcMeta.update(m)
        srcRowsList.append(r)

    before = len(exRows)
    merged, bad = mergeRows(exRows, srcRowsList)
    if bad:
        log.warning(f'坏行跳过: {bad} (列数 != {N_COLS})')
    if not merged:
        log.warning('无有效数据行, 跳过')
        return

    # 元数据: 原始文件头覆盖 Latest 旧头 (同名键), Count 按实际行数重算
    newMeta = {**exMeta, **srcMeta}
    newMeta.pop('Count', None)
    localChanged, data = serializeMvsv(latestPath, newMeta, merged, onlyIfChanged=True)
    if localChanged:
        log.info(f'合并: {before} -> {len(merged)} 行')
    log.info(f'Latest: {len(merged)} 行 (ts 范围: {merged[0][TS_IDX]} ~ {merged[-1][TS_IDX]})')

    if apiEnabled():
        putRemoteFile(latestPath, data, f'[Quote] Aggregate FTMM Latest for {code}', branch)
    elif localChanged:
        runGit(['add', str(latestPath)])
        sha = gitCommit(f'[Quote] Aggregate FTMM Latest for {code}')
        if sha:
            log.info(f'Latest commit: {sha}')
        gitPush(branch)
    else:
        log.info('Latest 无变化')

    if cleanupEnabled():
        cleanupRaw(sourceFiles, latestPath, code, branch)
    else:
        log.info(f'原始文件保留 {len(sourceFiles)} 个 ({CLEANUP_RAW_ENV} 未开启)')


def cleanupEnabled():
    return os.environ.get(CLEANUP_RAW_ENV, '').lower() in ('1', 'true', 'yes')


def cleanupRaw(sourceFiles, latestPath, code, branch):
    """删除 ts 范围被 Latest 完整覆盖的原始文件。返回清理数量。"""
    _, latestRows = parseMvsv(latestPath)
    if not latestRows:
        return 0
    mn, mx = int(latestRows[0][TS_IDX]), int(latestRows[-1][TS_IDX])
    removed = 0
    removedLocal = 0
    for sf in sourceFiles:
        try:
            _, rows = parseMvsv(sf)
        except Exception as e:
            log.warning(f'{Path(sf).name} 解析失败, 保留: {e}')
            continue
        if not rows:
            continue
        fmn, fmx = int(rows[0][TS_IDX]), int(rows[-1][TS_IDX])
        if fmn < mn or fmx > mx:
            log.warning(
                f'{Path(sf).name} 未被清理, '
                f'文件 ts 范围 [{fmn}, {fmx}] 未被 Latest [{mn}, {mx}] 完整覆盖'
            )
            continue
        if apiEnabled():
            deleteRemoteFile(sf, f'[Quote] Cleanup FTMM raw for {code}', branch)
        else:
            os.remove(sf)
            runGit(['add', '-A', str(sf)])
            removedLocal += 1
            log.info(f'清理: {Path(sf).name}')
        removed += 1
    # 降级路径下删除动作必须自己提交, 否则会残留未提交的删除
    if removedLocal:
        sha = gitCommit(f'[Quote] Cleanup FTMM raw for {code}')
        if sha:
            log.info(f'清理 commit: {sha}')
        gitPush(branch)
    if removed:
        log.info(f'清理原始文件: {removed} 个')
    return removed


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
    branch = resolveBranch()
    log.info('任务十一: FTMM 分钟原始文件增量聚合到 Latest')
    log.info(f'递交方式: {"Contents API" if apiEnabled() else "本地 git (降级)"} / 分支: {branch}')
    codes = resolveCodes()
    if not codes:
        log.info('无待处理证券')
        return
    log.info(f'证券: {codes}')
    ok = fail = 0
    for code in codes:
        log.info(f'--- {code} 开始 ---')
        try:
            processCode(code, branch)
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
