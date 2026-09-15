# -*- coding: utf-8 -*-
"""
东方财富 7x24 快讯抓取工具(四大能力封装)
========================================

接口原型(JSONP):
    https://np-weblist.eastmoney.com/comm/web/getFastNewsList
        ?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize=50
        &req_trace=<毫秒时间戳>&_=<毫秒时间戳>&callback=<回调名>

封装的四大能力:
    1. getLatestNews()  查询最新一页快讯(最多 100 条), 以数组返回
    2. getDayNews()     查询某一天(北京时间 00:00:00 为界)全天快讯,
                        结构化去重后返回数组(可附加日期字段)
    3. toJsonl()        将快讯结构化数据导出为 jsonl 文本(字符串)
    4. writeJsonl()     将 jsonl 文本(或快讯数据)保存到本地指定路径文件

另外提供底层接口:
    fetchFastnews()       请求一次接口, 返回 data 字典
    fetchFastnewsMany()   连续翻页抓取并去重
    stripJsonp()          去掉 JSONP 回调包裹还原纯 JSON
    decodeRealSort()      解析 realSort 为 (毫秒时间戳, 序号, datetime)

时间基准说明:
    realSort / sortEnd 的前 13 位为 epoch 基准的毫秒时间戳(UTC);
    北京时间 = UTC + 8 小时。showTime 字段即北京时间字符串。

依赖: 仅 Python 3 标准库, 无第三方包。
"""

import argparse
import json
import logging
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Union

# 快讯接口地址(去掉 query 部分, 参数用 dict 拼装)
FASTNEWS_URL = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"

# 固定业务参数(与官网 kuaixun.eastmoney.com 一致)
BASE_PARAMS = {
    "client": "web",
    "biz": "web_724",
    "fastColumn": "102",
}

# 请求头: UA + Referer 尽量模拟浏览器, 降低被风控拦截的概率
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://kuaixun.eastmoney.com/",
    "Accept": "*/*",
}

# 抓取失败时的重试次数与间隔
RETRY_TIMES = 3
RETRY_DELAY = 1.0

# jsonl 输出文件默认编码
OUT_ENCODING = "utf-8"

# 默认每页条数 / 最新一页上限
DEFAULT_PAGE_SIZE = 50
LATEST_MAX = 100

# 北京时间 = UTC+8
TZ_OFFSET = timedelta(hours=8)

# 导出 jsonl 时 JSON 对象的字段: 仅导出以下 9 个字段, 按此顺序输出
# 字段名统一为小驼峰; 注意接口原始字段名为 pinglun_Num, 映射为 pinglunNum
EXPORT_ORDER = [
    "code", "realSort", "showTime", "titleColor",
    "summary", "share", "pinglunNum", "stockList", "image",
]

# 接口原始字段名 -> 导出字段名映射
FIELD_RENAME = {"pinglun_Num": "pinglunNum"}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def nowMillis() -> int:
    """当前毫秒时间戳, 用于 req_trace / _ 防缓存参数。"""
    return int(time.time() * 1000)


def utcnowBeijing() -> datetime:
    """当前北京时间(datetime, 无 tzinfo 的 naive 对象, 供本模块算术使用)。"""
    return datetime.now(timezone.utc).astimezone(
        timezone(TZ_OFFSET)
    ).replace(tzinfo=None)


def parseBeijing(s: str) -> datetime:
    """解析北京时间字符串(如 '2026-08-07 10:33:53')为 datetime。"""
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")


def beijingToMillis(dt: datetime) -> int:
    """
    北京时间 datetime -> epoch 毫秒时间戳(UTC 基准)。

    由于 realSort 前 13 位是 epoch(UTC)毫秒, 北京时间需先减去 8 小时:
        epoch毫秒 = (北京时间 - 1970-01-01 00:00:00) - 8h
    """
    return int((dt - datetime(1970, 1, 1) - TZ_OFFSET).total_seconds() * 1000)


def stripJsonp(text: str) -> str:
    """
    去掉 JSONP 回调包裹, 还原成纯 JSON 字符串。

    示例: jQuery_123({"a":1})  ->  {"a":1}
    若输入本身不是 JSONP(无圆括号包裹), 则原样返回。
    """
    text = text.strip()
    if text.startswith(("(",)) or text.startswith(("jQuery",)):
        # 找第一个左括号与最后一个右括号, 取中间内容
        lpos = text.find("(")
        rpos = text.rfind(")")
        if lpos != -1 and rpos > lpos:
            return text[lpos + 1:rpos].strip()
    return text


def decodeRealSort(real_sort: str) -> Dict[str, Any]:
    """
    解析 realSort / sortEnd 游标, 返回:
        {'millis': epoch 毫秒时间戳, 'seq': 序号, 'datetime': 北京时间 datetime}

    说明: realSort 形如 16 位字符串, 前 13 位为 epoch 毫秒(UTC), 后 3 位为序号。
    """
    rs = str(real_sort)
    seq = ""
    millis = None
    dt = None
    if rs.isdigit() and len(rs) >= 13:
        millis = int(rs[:13])
        seq = rs[13:]
        dt = datetime(1970, 1, 1) + timedelta(milliseconds=millis) + TZ_OFFSET
    else:
        # 无法拆解时退化为整串时间戳
        try:
            millis = int(rs)
            dt = datetime(1970, 1, 1) + timedelta(milliseconds=millis) + TZ_OFFSET
        except ValueError:
            pass
    return {"millis": millis, "seq": seq, "datetime": dt}


# ---------------------------------------------------------------------------
# 底层抓取
# ---------------------------------------------------------------------------

def fetchFastnews(
    sort_end: Optional[str] = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    req_trace: Optional[str] = None,
    timeout: float = 15.0,
    log_fn: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    请求一次东方财富快讯接口。

    参数:
        sort_end : 翻页游标。None 或空串 = 获取最新一页; 取上一页响应的
                   data.sortEnd 可继续向后翻页。
        page_size: 每页条数(接口可返回较大值, 但建议控制在 50 内)。
        req_trace: 请求追踪号, 默认用当前毫秒时间戳生成。
        timeout  : 请求超时秒数。
        log_fn   : 请求日志回调。传入后每次请求输出一行
                   "[时间] URL | 结果摘要(状态/条数/游标/耗时)"。
                   可传 print / logging.info / logger.info 等;
                   默认 None 不输出(兼容性)。返回的 data 里含
                   _requestLog 字段, 记录本次请求日志文本。

    返回:
        解析后的 data 字典(含 fastNewsList / sortEnd / total,
        及附加字段 _requestLog)。

    异常:
        ValueError : 接口返回 code != "1"(业务失败)。
        OSError    : 网络/超时类错误。
    """
    params = dict(BASE_PARAMS)
    params["sortEnd"] = sort_end or ""
    params["pageSize"] = str(page_size)
    params["req_trace"] = req_trace or str(nowMillis())
    params["_"] = str(nowMillis())

    url = FASTNEWS_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=DEFAULT_HEADERS)

    last_err: Optional[Exception] = None
    for attempt in range(RETRY_TIMES):
        req_start = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(stripJsonp(text))
            # 业务状态码: 字符串 "1" 表示成功
            if str(payload.get("code")) != "1":
                raise ValueError(
                    "接口返回业务失败: code=%r message=%r" % (
                        payload.get("code"), payload.get("message"),
                    )
                )
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ValueError("接口返回缺少 data 字段: %r" % payload)
            # 组装请求日志(一行): [时间] URL | 状态 | 条数 | sortEnd | 耗时
            lst = data.get("fastNewsList") or []
            log_text = "[%s] %s | status=%s | count=%d | sortEnd=%s | %.2fs" % (
                time.strftime("%Y-%m-%d %H:%M:%S"),
                url,
                payload.get("code"),
                len(lst),
                data.get("sortEnd") or "",
                time.time() - req_start,
            )
            data["_requestLog"] = log_text
            if log_fn is not None:
                log_fn(log_text)
            return data
        except Exception as exc:  # noqa: BLE001 - 网络与解析错误统一重试
            last_err = exc
            # 失败也输出一行日志, 方便追踪
            log_err = "[%s] %s | ERROR: %s" % (
                time.strftime("%Y-%m-%d %H:%M:%S"),
                url,
                exc,
            )
            if log_fn is not None:
                log_fn(log_err)
            if attempt < RETRY_TIMES - 1:
                time.sleep(RETRY_DELAY)
    raise OSError("连续 %d 次请求东方财富快讯失败: %r" % (RETRY_TIMES, last_err))


def fetchFastnewsMany(
    sort_end: Optional[str] = None,
    pages: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_count: Optional[int] = None,
    sleep_between: float = 0.3,
    log_fn: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """
    连续抓取多页快讯并去重(以 code 为准, 保留首次出现的顺序)。

    参数:
        sort_end      : 起始游标, None 表示从最新开始。
        pages         : 最多抓取页数。
        page_size     : 每页条数。
        max_count     : 最多保留条数上限(None 不限)。
        sleep_between : 相邻两次请求的间隔秒数, 防止请求过快。
        log_fn        : 请求日志回调, 透传给 fetchFastnews, 见其说明。

    返回:
        去重后的快讯字典列表(每项含 code/title/summary/showTime/...)。
    """
    items: List[Dict[str, Any]] = []
    seen: set = set()
    cursor = sort_end
    for _ in range(pages):
        data = fetchFastnews(sort_end=cursor, page_size=page_size, log_fn=log_fn)
        for it in data.get("fastNewsList") or []:
            code = it.get("code")
            if code and code not in seen:
                seen.add(code)
                items.append(it)
        # 该页没有数据或没有下一页游标 => 已抓到底
        new_cursor = data.get("sortEnd")
        if not new_cursor or not data.get("fastNewsList"):
            break
        cursor = new_cursor
        if sleep_between > 0:
            time.sleep(sleep_between)
        if max_count is not None and len(items) >= max_count:
            break
    if max_count is not None:
        items = items[:max_count]
    return items


# ---------------------------------------------------------------------------
# 四大能力
# ---------------------------------------------------------------------------

def getLatestNews(max_count: int = LATEST_MAX) -> List[Dict[str, Any]]:
    """
    能力1: 查询最新一页快讯, 以数组返回。

    参数:
        max_count : 最多返回条数(最大 100, 接口单次最多约 100 条)。

    返回:
        最新快讯列表(新->旧), 每项含 code/title/summary/showTime/...。
    """
    count = LATEST_MAX if max_count is None else min(int(max_count), LATEST_MAX)
    data = fetchFastnews(page_size=count)
    return list(data.get("fastNewsList") or [])


def getDayNews(
    day: Optional[Union[str, datetime]] = None,
    attach_date: bool = True,
    sleep_between: float = 0.3,
    log_fn: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """
    能力2: 查询某一天(北京时间 00:00:00 为界)全天的所有东方财富快讯,
           结构化去重后返回数组。

    抓取逻辑:
        1. 计算目标日北京时间 [00:00:00, 次日 00:00:00) 的 epoch 毫秒边界;
        2. 从最新一页开始, 依据 realSort 逐页向后翻;
        3. 本页第一条已早于当日开始边界 => 结束;
        4. 跨边界截断, 只保留当日条目, 以 code 去重, 新->旧排序返回。

    参数:
        day          : 目标日期。None 表示昨天(北京时间 00:00 为界, 即自然日)。
                       也可传 'YYYY-MM-DD' 字符串或 datetime/date。
        attach_date  : True 时每条快讯附带 'date' 字段(形如 '2026-08-07'),
                       便于按日分组; False 则返回原始字段。
        sleep_between: 翻页请求间隔秒数。
        log_fn       : 请求日志回调, 透传给 fetchFastnews(每次请求一行),
                       见 fetchFastnews 说明。

    返回:
        当日快讯列表(新->旧), 去重后。若该日无数据返回空列表 []。
    """
    # ---- 解析目标日期的北京时间零点 ----
    if day is None:
        # 昨天零点 = 今天零点 - 1 天
        today_midnight = utcnowBeijing().replace(
            hour=0, minute=0, second=0, microsecond=0)
        day_start = today_midnight - timedelta(days=1)
    elif isinstance(day, str):
        day_start = datetime.strptime(day[:10], "%Y-%m-%d")
    elif isinstance(day, datetime):
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    else:  # date
        day_start = datetime(day.year, day.month, day.day)

    day_end = day_start + timedelta(days=1)
    day_label = day_start.strftime("%Y-%m-%d")
    start_millis = beijingToMillis(day_start)
    end_millis = beijingToMillis(day_end)

    items: List[Dict[str, Any]] = []
    seen: set = set()
    cursor: Optional[str] = None  # 从最新一页开始翻

    while True:
        data = fetchFastnews(sort_end=cursor, page_size=DEFAULT_PAGE_SIZE,
                             log_fn=log_fn)
        lst = data.get("fastNewsList") or []

        for it in lst:
            rs = it.get("realSort")
            if not rs:
                continue
            millis = decodeRealSort(rs)["millis"]
            if millis is None:
                continue
            if millis >= end_millis:
                continue          # 早于今天区间起点(尚未进入当日)的跳过
            if millis < start_millis:
                # 已翻过当日, 结束整个抓取
                if attach_date:
                    for d in items:
                        d.setdefault("date", day_label)
                return items
            # 当日条目: 以 code 去重
            code = it.get("code")
            if code and code not in seen:
                seen.add(code)
                items.append(it)

        # 没有下一页或当前页已到底 => 结束
        new_cursor = data.get("sortEnd")
        if not new_cursor or not lst:
            break
        # 当前页最后一条已经早于当日开始 => 无需再翻
        last_rs = lst[-1].get("realSort")
        if last_rs and decodeRealSort(last_rs)["millis"] < start_millis:
            break
        cursor = new_cursor
        if sleep_between > 0:
            time.sleep(sleep_between)

    if attach_date:
        for d in items:
            d.setdefault("date", day_label)
    return items


def _orderedItem(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    将单条快讯重排为导出结构: 仅保留 EXPORT_ORDER 中列出的字段,
    按 EXPORT_ORDER 顺序输出, 并做字段名映射(pinglun_Num -> pinglunNum)。
    """
    renamed = {
        FIELD_RENAME.get(k, k): v
        for k, v in item.items() if k in FIELD_RENAME or k in EXPORT_ORDER
    }
    return {k: renamed[k] for k in EXPORT_ORDER if k in renamed}


def toJsonl(
    items: Union[Iterable[Dict[str, Any]], str],
    ensure_ascii: bool = False,
    ordered: bool = True,
    ascending: bool = True,
) -> str:
    """
    能力3: 将快讯结构化数据导出为 jsonl 文本(字符串), 每行一条 JSON,
           以紧凑格式输出(去掉冒号/逗号后多余空格)。

    参数:
        items        : 快讯字典列表; 或已存在的 jsonl 文本(原样返回)。
        ensure_ascii : True 时转义非 ASCII 为 \\uXXXX(默认 False 保留中文)。
        ordered      : True 时仅按 EXPORT_ORDER 的 9 个字段输出并映射字段名
                       (code realSort showTime titleColor summary share
                       pinglunNum stockList image, 小驼峰命名);
                       False 时保留原始字段名与原始字段序。
        ascending    : True(默认)时按时间先后(realSort 升序, 旧->新)排列;
                       False 时保持传入顺序(接口返回为 新->旧)。

    返回:
        jsonl 文本字符串(末尾带一个换行符)。默认按时间先后排序输出。
    """
    if isinstance(items, str):
        return items if items.endswith("\n") else items + "\n"
    if not isinstance(items, list):
        items = list(items)
    if ascending:
        # 按 realSort 升序(时间先后): 缺失 realSort 的条目排到最后
        items = sorted(
            items,
            key=lambda it: it.get("realSort") if it.get("realSort") else "",
        )
    if ordered:
        items = [_orderedItem(it) for it in items]
    # 紧凑格式: 去掉键值对冒号后、元素逗号后的空格
    return "".join(
        json.dumps(it, ensure_ascii=ensure_ascii, separators=(",", ":")) + "\n"
        for it in items
    )


def writeJsonl(
    items: Union[Iterable[Dict[str, Any]], str],
    file_path: str,
    ensure_ascii: bool = False,
    ordered: bool = True,
    ascending: bool = True,
) -> int:
    """
    能力4: 将 jsonl 文本(或快讯数据)保存到本地指定路径的文件中。

    参数:
        items        : 快讯字典列表; 或 jsonl 文本字符串。
        file_path    : 目标文件路径(UTF-8 编码)。
        ensure_ascii : 仅当 items 为列表时生效, 见 toJsonl()。
        ordered      : 仅当 items 为列表时生效, 见 toJsonl()。
        ascending    : 仅当 items 为列表时生效, 见 toJsonl()。
                       默认 True 表示按时间先后(旧->新)排序后写入。
    返回:
        写入的条目行数。
    """
    if isinstance(items, str):
        text = items if items.endswith("\n") else items + "\n"
        count = len(text.splitlines())
    else:
        text = toJsonl(items, ensure_ascii=ensure_ascii,
                       ordered=ordered, ascending=ascending)
        count = len(list(items))
    with open(file_path, "w", encoding=OUT_ENCODING) as fh:
        fh.write(text)
    return count


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="东方财富 7x24 快讯抓取工具: 最新一页 / 全天快讯 / 导出 jsonl",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-o", "--output", default="FlashEastMoney.jsonl",
        help="输出 jsonl 文件路径",
    )
    parser.add_argument(
        "-m", "--mode", choices=["latest", "day"], default="latest",
        help="latest=最新一页; day=按北京时间 0 点为界抓取某天全天",
    )
    parser.add_argument(
        "-d", "--day", default=None,
        help="mode=day 时指定日期 YYYY-MM-DD(默认昨天)",
    )
    parser.add_argument(
        "-n", "--count", type=int, default=LATEST_MAX,
        help="mode=latest 时最多条数(上限 100)",
    )
    parser.add_argument(
        "--no-date", action="store_true",
        help="mode=day 时不附加 date 字段",
    )
    parser.add_argument(
        "--sleep", type=float, default=0.3,
        help="翻页请求间的间隔秒数",
    )
    parser.add_argument(
        "--print", dest="show", action="store_true",
        help="完成后预览输出文件前 5 条(时间先后序, 含时间与标题)",
    )
    args = parser.parse_args(argv)

    try:
        if args.mode == "day":
            items = getDayNews(day=args.day, attach_date=not args.no_date,
                               sleep_between=args.sleep)
        else:
            items = getLatestNews(max_count=args.count)
    except OSError as exc:
        print("抓取失败: %s" % exc, file=sys.stderr)
        return 1

    if not items:
        print("未获取到任何快讯。", file=sys.stderr)
        return 1

    written = writeJsonl(items, args.output)
    print("已写入 %d 条快讯 -> %s" % (written, args.output))

    if args.show:
        # 文件行序为时间先后(旧->新), 预览前 5 条即当天最早的 5 条。
        # 导出 JSON 不含 title, 这里从 summary 提取标题展示(【】或首段)。
        print("\n--- 预览前 5 条(文件首行起, 时间先后) ---")
        try:
            with open(args.output, encoding=OUT_ENCODING) as fh:
                for i, line in enumerate(fh):
                    if i >= 5 or not line.strip():
                        break
                    obj = json.loads(line)
                    title = obj.get("title") or ""
                    if not title:
                        sm = obj.get("summary") or ""
                        # 优先取【...】段作为标题, 否则取首句
                        if sm.startswith("【") and "】" in sm:
                            title = sm[1:sm.index("】")]
                        else:
                            title = sm[:20]
                    print("[%s] %s" % (obj.get("showTime", "?"), title))
        except OSError as exc:
            print("读取输出文件失败: %s" % exc, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
