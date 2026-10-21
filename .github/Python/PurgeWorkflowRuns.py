#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PurgeWorkflowRuns —— 批量清理某个 GitHub Actions workflow 的历史 runs（本地执行）
========================================================================================

一、用途
----------------------------------------------------------------------------------------
GitHub Actions 左侧的 workflow 列表由「历史 run 索引」渲染：把 `.github/workflows/xxx.yml`
文件删掉后，只要它名下还有 run 记录，列表里就会一直显示这个（已不存在的）workflow。
本脚本用 REST API 逐条删除指定 workflow 的 runs，全部删完后该条目即从列表消失。

它也能处理**文件已删除**的 workflow（此时用 `--workflow` 传 id 或原 path，
例如 `--workflow 295195740` / `--workflow Quote01Aggregate.yml`）。

二、删除顺序与速率
----------------------------------------------------------------------------------------
    - 默认「从最新往前删」：每轮取列表第 1 页（最新 100 条）→ 并发删除 → 再取第 1 页，
      因此网页上（只显示最近约 3000 条）能立刻看到 runs 变少；
      想反过来（最旧先删）可加 `--oldest-first`；
    - 删除是逐条 `DELETE /repos/{owner}/{repo}/actions/runs/{id}`：
      GitHub **没有**可用的「按 workflow 批量删除」接口（批量端点返回 404），
      所以受认证配额 **5000 次/小时** 限制，2.8 万条约需 5~6 小时；
    - 4 线程并发 + TLS 失败重试 + 403/429 按 `Retry-After` / `X-RateLimit-Reset` 等待；
    - 幂等：中断后重跑即可（已删除的 id 返回 404，视为已处理）。

三、鉴权
----------------------------------------------------------------------------------------
    - `--token` 参数；或环境变量 `GITHUB_TOKEN` / `GH_TOKEN`；
    - 都没有时回退 `gh auth token`（需先 `gh auth login`）；
    - 令牌需要该仓库的 `actions: write` 权限（classic PAT 的 `repo` scope 即可）。

四、安全性
----------------------------------------------------------------------------------------
    - 默认需要 `--yes` 才会真正删除（防误删），否则只显示待删数量；
    - `--dry-run` 只统计并列出最近几条，不删除；
    - `--limit N` 限制本次最多删除 N 条，便于分批（例如每小时跑一次 `--limit 4000`）；
    - 只删除指定 workflow 的 run，不会碰其它 workflow 的历史。

五、用法示例
----------------------------------------------------------------------------------------
    # 先看有多少、不删
    python3 .github/Python/PurgeWorkflowRuns.py --workflow Quote01Aggregate.yml --dry-run

    # 真正删除（默认从最新往前删）
    python3 .github/Python/PurgeWorkflowRuns.py --workflow Quote01Aggregate.yml --yes

    # 用 id 指定（文件已删除的 workflow 也能删）
    python3 .github/Python/PurgeWorkflowRuns.py --workflow 295195740 --yes

    # 分批：本次最多 4000 条（配额 5000/小时，留出余量）
    python3 .github/Python/PurgeWorkflowRuns.py --workflow 295195740 --limit 4000 --yes

    # 从最旧的开始删
    python3 .github/Python/PurgeWorkflowRuns.py --workflow 295195740 --oldest-first --yes

    # 指定仓库 / 并发
    python3 .github/Python/PurgeWorkflowRuns.py --repo ACANX/Distribution \
        --workflow 111.yml --workers 6 --yes

六、退出码
----------------------------------------------------------------------------------------
    0 = 清理完成（或列表已为空 / 已按 --limit 完成本批）；
    1 = 存在删除失败、被中断，或参数/鉴权错误。
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "https://api.github.com"
DEFAULT_REPO = "ACANX/Distribution"
DEFAULT_WORKERS = 4
DEFAULT_TIMEOUT = 20
PAGE_SIZE = 100

_LOCK = threading.Lock()
_STATS = {"deleted": 0, "skipped": 0, "failed": 0, "rate_waits": 0}


class PurgeError(Exception):
    """致命错误（鉴权 / 仓库 / workflow 找不到 / 查询失败）"""


def log(message):
    """输出一行日志（带 UTC 时间，实时刷新）"""
    print("[%s] %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), message),
          flush=True)


def resolve_token(explicit):
    """确定访问令牌：参数 → 环境变量 → gh auth token"""
    if explicit and explicit.strip():
        return explicit.strip()
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    try:
        token = subprocess.check_output(["gh", "auth", "token"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        token = ""
    if not token:
        raise PurgeError("缺少令牌：请用 --token、设置 GITHUB_TOKEN，或先 `gh auth login`")
    return token


def api(token, method, path, timeout, attempts=8):
    """调用 REST API，返回 (status, body_bytes, headers)

    网络错误 / TLS 失败 / 5xx 按退避重试；403/429 按限流头部等待后重试。
    重试耗尽返回 status=0，由调用方决定如何处理。
    """
    url = API + path
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Distribution-PurgeWorkflowRuns",
    }
    for attempt in range(attempts):
        request = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            body = error.read()
            if error.code in (403, 429):
                retry = (error.headers.get("Retry-After") or "").strip()
                reset = (error.headers.get("X-RateLimit-Reset") or "").strip()
                remain = (error.headers.get("X-RateLimit-Remaining") or "").strip()
                if retry.isdigit():
                    wait = int(retry) + 3
                elif remain == "0" and reset.isdigit():
                    wait = max(5, int(reset) - int(time.time()) + 5)
                else:
                    wait = 30
                wait = min(wait, 3600)
                with _LOCK:
                    _STATS["rate_waits"] += 1
                log("⏳ 触发限流（HTTP %s），等待 %d 秒" % (error.code, wait))
                time.sleep(wait)
                continue
            if error.code == 404:
                return 404, body, error.headers
            log("⚠️ HTTP %s，%.0f 秒后重试" % (error.code, min(2 ** attempt, 30)))
            time.sleep(min(2 ** attempt, 30))
            continue
        except OSError as error:
            log("⚠️ 网络错误（%s），%.0f 秒后重试" % (type(error).__name__,
                                              min(1 + attempt * 2, 30)))
            time.sleep(min(1 + attempt * 2, 30))
            continue
    return 0, b"", {}


def list_workflows(token, repo, timeout):
    """分页列出仓库当前的 workflows"""
    workflows, page = [], 1
    while True:
        status, body, _headers = api(
            token, "GET", "/repos/%s/actions/workflows?per_page=%d&page=%d"
                        % (repo, PAGE_SIZE, page), timeout)
        if status != 200:
            raise PurgeError("列取 workflow 失败（HTTP %s）" % status)
        batch = json.loads(body or b"{}").get("workflows") or []
        if not batch:
            return workflows
        workflows.extend(batch)
        page += 1


def resolve_workflow(token, repo, spec, timeout):
    """把 --workflow 的取值解析成 (workflow_id, 显示名)

    支持：纯数字 id、完整 path（.github/workflows/xxx.yml）、文件名（xxx.yml）、
    workflow 名称（如 Quote01Aggregate）。
    """
    spec = spec.strip()
    if spec.isdigit():
        return int(spec), "id=%s" % spec
    workflows = list_workflows(token, repo, timeout)
    for workflow in workflows:
        path = workflow.get("path", "")
        name = workflow.get("name", "")
        if spec in (path, name, os.path.basename(path)):
            return int(workflow["id"]), path or name
    suggestions = ", ".join(os.path.basename(item.get("path", ""))
                            for item in workflows[:10])
    raise PurgeError("找不到 workflow：%s（仓库里现有的前几个：%s；"
                     "已删除的 workflow 请直接传数字 id）" % (spec, suggestions))


def count_runs(token, repo, workflow_id, timeout):
    """统计该 workflow 当前剩余的 runs 数；查询失败返回 None"""
    status, body, _headers = api(
        token, "GET", "/repos/%s/actions/workflows/%d/runs?per_page=1"
                      % (repo, workflow_id), timeout)
    if status != 200:
        return None
    try:
        return int(json.loads(body or b"{}").get("total_count") or 0)
    except (ValueError, TypeError):
        return None


def page_ids(token, repo, workflow_id, timeout, page):
    """取第 page 页的 run id 列表；失败返回 None"""
    status, body, _headers = api(
        token, "GET", "/repos/%s/actions/workflows/%d/runs?per_page=%d&page=%d"
                      % (repo, workflow_id, PAGE_SIZE, page), timeout)
    if status != 200:
        return None
    runs = json.loads(body or b"{}").get("workflow_runs") or []
    return [int(run["id"]) for run in runs]


def delete_run(token, repo, run_id, timeout):
    """删除单条 run：返回 deleted / skipped（已不存在）/ failed"""
    status, _body, _headers = api(
        token, "DELETE", "/repos/%s/actions/runs/%d" % (repo, run_id), timeout)
    if status in (200, 204):
        return "deleted"
    if status == 404:
        return "skipped"
    return "failed"


def collect_all_ids(token, repo, workflow_id, timeout):
    """一次性收集该 workflow 的全部 run id（用于 --oldest-first）"""
    ids, page = [], 1
    while True:
        batch = page_ids(token, repo, workflow_id, timeout, page)
        if batch is None:
            log("⚠️ 列取第 %d 页失败，重试…" % page)
            continue
        if not batch:
            return ids
        ids.extend(batch)
        page += 1


def purge(token, repo, workflow_id, workers, limit, timeout, oldest_first,
          max_minutes=0):
    """主删除流程（从最新往前 / 或从最旧往前），返回统计 dict"""
    total = count_runs(token, repo, workflow_id, timeout)
    if total is not None:
        log("📊 当前剩余 runs：%d 条" % total)
    started = time.time()

    deadline = (started + max_minutes * 60) if max_minutes else None

    if oldest_first:
        all_ids = collect_all_ids(token, repo, workflow_id, timeout)
        all_ids.reverse()
        if limit:
            all_ids = all_ids[:limit]
        log("🗂 已取到 %d 条待删 run（从最旧开始）" % len(all_ids))
        for index in range(0, len(all_ids), PAGE_SIZE):
            _delete_batch(token, repo, all_ids[index:index + PAGE_SIZE], workers, timeout)
            if deadline and time.time() >= deadline:
                log("⏸ 已达时间上限（%d 分钟），本轮收工；重跑继续" % max_minutes)
                _STATS["timed_out"] = True
                break
    else:
        while True:
            if limit and (_STATS["deleted"] + _STATS["skipped"]) >= limit:
                break
            ids = page_ids(token, repo, workflow_id, timeout, 1)
            if ids is None:
                log("⚠️ 取列表第 1 页失败，10 秒后重试")
                time.sleep(10)
                continue
            if not ids:
                log("✅ 列表已空，删除完成")
                break
            if limit:
                remaining = limit - (_STATS["deleted"] + _STATS["skipped"])
                ids = ids[:remaining]
            _delete_batch(token, repo, ids, workers, timeout)
            elapsed = (time.time() - started) / 60
            log("🚮 累计删除 %d，跳过 %d，失败 %d，限流等待 %d 次，已耗时 %.0f 分钟"
                % (_STATS["deleted"], _STATS["skipped"], _STATS["failed"],
                   _STATS["rate_waits"], elapsed))
            if deadline and time.time() >= deadline:
                log("⏸ 已达时间上限（%d 分钟），本轮收工；重跑继续" % max_minutes)
                _STATS["timed_out"] = True
                break
            if _STATS["failed"]:
                break

    elapsed = (time.time() - started) / 60
    log("======== 结束：删除 %d 条，跳过（已不存在）%d 条，失败 %d 条，"
        "限流等待 %d 次，耗时 %.1f 分钟 ========"
        % (_STATS["deleted"], _STATS["skipped"], _STATS["failed"],
           _STATS["rate_waits"], elapsed))
    return dict(_STATS)


def _delete_batch(token, repo, ids, workers, timeout):
    """并发删除一批 run id，累加统计"""

    def worker(run_id):
        result = delete_run(token, repo, run_id, timeout)
        with _LOCK:
            _STATS[result] += 1

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, ids))


def check_admin(token, repo, timeout):
    """返回 (是否 admin, permissions dict)"""
    status, body, _headers = api(token, "GET", "/repos/%s" % repo, timeout)
    if status != 200:
        raise PurgeError("读取仓库信息失败（HTTP %s）" % status)
    permissions = json.loads(body or b"{}").get("permissions") or {}
    return bool(permissions.get("admin")), permissions


def existing_workflow_ids(token, repo, timeout):
    """当前仍存在于仓库的 workflow id 集合"""
    return {int(item["id"]) for item in list_workflows(token, repo, timeout) if "id" in item}


def list_day_runs(token, repo, day, timeout):
    """列出某一天仓库全部 runs：[(run_id, workflow_id), ...]；失败返回 None"""
    window = "%sT00:00:00Z..%sT23:59:59Z" % (day, day)
    rows, page = [], 1
    while True:
        status, body, _headers = api(
            token, "GET", "/repos/%s/actions/runs?per_page=%d&page=%d&created=%s"
                        % (repo, PAGE_SIZE, page, window), timeout)
        if status != 200:
            return None
        runs = json.loads(body or b"{}").get("workflow_runs") or []
        if not runs:
            return rows
        rows.extend((int(run["id"]), int(run["workflow_id"])) for run in runs)
        page += 1


def batch_delete_window(token, repo, day, timeout):
    """批量删除某一天的全部 runs（需要 admin 权限）"""
    window = "%sT00:00:00Z..%sT23:59:59Z" % (day, day)
    status, body, _headers = api(
        token, "DELETE", "/repos/%s/actions/runs?created=%s" % (repo, window), timeout)
    if status in (200, 204):
        return True
    if status == 404:
        raise PurgeError("批量删除返回 404：该接口要求仓库 admin 权限，"
                         "请用仓库 owner 的令牌（--token / GITHUB_TOKEN）")
    log("⚠️ 批量删除 %s 失败（HTTP %s）：%s" % (day, status, (body or b"")[:150]))
    return False


def batch_purge(token, repo, workflow_id, since, until, workers, timeout, dry_run,
                max_minutes=0):
    """按天批量清理

    一整天的 runs 若「除目标 workflow 外，只剩已删除 workflow（ghost）的 runs」，
    就用批量接口一次删掉整天；否则（当天有仍存在的其它 workflow 在跑）只逐条删除
    目标 workflow 的 runs，绝不误删。
    """
    existing = existing_workflow_ids(token, repo, timeout)
    log("📋 现存 workflow %d 个；批量模式按天扫描 %s ~ %s" % (len(existing), since, until))
    stats = {"batch_days": 0, "batch_runs": 0, "single_runs": 0, "scanned_days": 0,
             "timed_out": False}
    started = time.time()
    deadline = (started + max_minutes * 60) if max_minutes else None
    day = datetime.date.fromisoformat(since)
    end = datetime.date.fromisoformat(until)
    while day <= end:
        if deadline and time.time() >= deadline:
            stats["timed_out"] = True
            log("⏸ 已达时间上限（%d 分钟），本轮收工；重跑继续" % max_minutes)
            break
        key = day.isoformat()
        rows = list_day_runs(token, repo, key, timeout)
        day += datetime.timedelta(days=1)
        if rows is None:
            log("⚠️ %s 列取失败，跳过（可重跑补齐）" % key)
            continue
        stats["scanned_days"] += 1
        target = [run_id for run_id, wid in rows if wid == workflow_id]
        if not target:
            continue
        living = {wid for _run_id, wid in rows
                  if wid in existing and wid != workflow_id}
        if not living:
            if dry_run:
                stats["batch_days"] += 1
                stats["batch_runs"] += len(rows)
                log("🧪 [batch dry-run] %s 可整窗批量删除 %d 条（目标 %d）"
                    % (key, len(rows), len(target)))
            elif batch_delete_window(token, repo, key, timeout):
                stats["batch_days"] += 1
                stats["batch_runs"] += len(rows)
                log("🚮 %s 整窗批量删除 %d 条（目标 %d）" % (key, len(rows), len(target)))
        else:
            if dry_run:
                stats["single_runs"] += len(target)
                log("🧪 [batch dry-run] %s 需逐条删除目标 %d 条（当天另有 %d 个现存 workflow 的 runs）"
                    % (key, len(target), len(living)))
            else:
                _delete_batch(token, repo, target, workers, timeout)
                stats["single_runs"] += len(target)
                log("🚮 %s 逐条删除目标 %d 条" % (key, len(target)))
    log("======== 批量模式结束：整窗 %d 天/%d 条，逐条 %d 条，失败 %d ========"
        % (stats["batch_days"], stats["batch_runs"], stats["single_runs"],
           _STATS["failed"]))
    return stats


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="批量清理某个 GitHub Actions workflow 的历史 runs"
                    "（文件已删除的 workflow 也支持，传数字 id 即可）")
    parser.add_argument("--repo", default=DEFAULT_REPO,
                        help="仓库 owner/name（默认 %s）" % DEFAULT_REPO)
    parser.add_argument("--workflow", required=True,
                        help="workflow id / path / 名称，例如 295195740、"
                             "Quote01Aggregate.yml、Quote01Aggregate")
    parser.add_argument("--token", default="",
                        help="访问令牌（默认取 GITHUB_TOKEN / GH_TOKEN / gh auth token）")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help="并发线程数（默认 %d；过高会触发二级限流）" % DEFAULT_WORKERS)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="单请求超时秒数（默认 %d）" % DEFAULT_TIMEOUT)
    parser.add_argument("--limit", type=int, default=0,
                        help="本次最多删除多少条（默认 0 = 不限）")
    parser.add_argument("--batch", action="store_true",
                        help="按天批量删除（要求仓库 admin；整天 runs 全属于目标/"
                             "已删 workflow 时用批量接口一次删完，速度快百倍）")
    parser.add_argument("--since", default="2026-01-01",
                        help="批量模式起始日期 YYYY-MM-DD（默认 2026-01-01）")
    parser.add_argument("--until", default="",
                        help="批量模式结束日期 YYYY-MM-DD（默认今天 UTC）")
    parser.add_argument("--auto", action="store_true",
                        help="自动选择：令牌有 admin 时走批量、否则走逐条（Actions 里推荐）")
    parser.add_argument("--max-minutes", type=int, default=0,
                        help="本次最多运行多少分钟（0=不限；到点收工，重跑可继续）")
    parser.add_argument("--oldest-first", action="store_true",
                        help="从最旧的 run 开始删（默认从最新开始，网页立刻可见变化）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只统计并列出最近几条，不删除")
    parser.add_argument("--yes", action="store_true",
                        help="确认删除（不加时只提示待删数量）")
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    try:
        token = resolve_token(options.token)
        workflow_id, label = resolve_workflow(
            token, options.repo, options.workflow, options.timeout)
        total = count_runs(token, options.repo, workflow_id, options.timeout)
    except PurgeError as error:
        log("❌ %s" % error)
        return 1

    log("仓库 = %s" % options.repo)
    log("workflow = %s（%s）" % (label, workflow_id))
    log("剩余 runs = %s" % ("未知" if total is None else total))
    if total == 0:
        log("✅ 没有需要删除的 run（该条目应从 Actions 列表消失）")
        return 0

    # ---- --auto：有 admin 就批量、否则逐条 ----
    if options.auto:
        is_admin, _permissions = check_admin(token, options.repo, options.timeout)
        options.batch = is_admin
        log("🔎 auto：当前令牌 admin=%s → %s模式"
            % (is_admin, "批量" if is_admin else "逐条（受 5000 次/小时配额限制）"))

    # ---- 批量模式（需要 admin；一次性删掉整天，速度快百倍）----
    if options.batch:
        is_admin, _permissions = check_admin(token, options.repo, options.timeout)
        until = options.until or time.strftime("%Y-%m-%d", time.gmtime())
        if options.dry_run:
            if not is_admin:
                log("⚠️ 当前令牌不是仓库 admin：dry-run 只做统计；"
                    "真正批量删除需要 owner 的令牌")
            batch_purge(token, options.repo, workflow_id, options.since, until,
                        max(1, options.workers), options.timeout, True,
                        options.max_minutes)
            return 0
        if not is_admin:
            log("❌ 批量删除要求仓库 admin 权限，当前令牌没有（permissions.admin=false）。")
            log("   请用仓库 owner 的令牌：--token <PAT> 或 export GITHUB_TOKEN=<PAT>；")
            log("   或去掉 --batch 走逐条模式（受 5000 次/小时限制，2.8 万条约 6 小时）。")
            return 1
        if not options.yes:
            log("⚠️ 批量模式会删除 %s 条 run 所属时间段里的 runs（不可恢复）。"
                "确认请加 --yes 重新执行。" % ("未知数量" if total is None else total))
            return 1
        batch_purge(token, options.repo, workflow_id, options.since, until,
                    max(1, options.workers), options.timeout, False,
                    options.max_minutes)
        return 1 if _STATS["failed"] else 0

    if options.dry_run:
        for page in (1, 2, 3):
            ids = page_ids(token, options.repo, workflow_id, options.timeout, page)
            if not ids:
                break
            log("🧪 [dry-run] 第 %d 页前几条 run id：%s%s"
                % (page, ids[:5], " …" if len(ids) > 5 else ""))
        log("🧪 [dry-run] 未删除任何 run")
        return 0

    if not options.yes:
        log("⚠️ 将要删除 %s 条 run（不可恢复）。确认请加 --yes 重新执行。"
            % ("未知数量" if total is None else total))
        return 1

    stats = purge(token, options.repo, workflow_id, max(1, options.workers),
                  max(0, options.limit), options.timeout, options.oldest_first,
                  options.max_minutes)
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("⛔ 已中断（已删除的不会恢复；重新运行即可继续）")
        sys.exit(1)
