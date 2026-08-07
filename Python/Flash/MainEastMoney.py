# -*- coding: utf-8 -*-
"""
东方财富 7x24 快讯日报采集程序(Main)
====================================

作用:
    采集前一天(北京时间自然日, 00:00:00 分界)的东方财富快讯,
    保存为 jsonl 文件, 按日期命名(如 20260806.jsonl),
    存放到可配置的目录: <base_dir>/Finv/FlashEastMoney/<year>/ 。

    专为 GitHub Actions 定时任务设计: 支持从配置与环境变量读取参数,
    输出可被 workflow 消费的结果信息。

目录与命名:
    默认目标: Archive/Finv/News/FlashEastMoney/2026/20260806.jsonl
    其中 Archive 为 base_dir(仓库相对路径), year 为前一天年份。

配置来源(优先级从高到低):
    1. 环境变量 MAIN_BASE_DIR / MAIN_SLEEP_BETWEEN
    2. Config.json 中的 BaseDir / SleepBetween(大驼峰命名, 大小写不敏感)
    3. 内置默认值: base_dir='Archive', sleep_between=0.3

用法:
    python3 Main.py [--config Config.json] [--date YYYY-MM-DD]
                    [--base-dir DIR] [--sleep N] [--log] [--print] [--dry-run]

    --log : 打印每次接口请求的 URL 及结果摘要(每请求一行), 便于排查

依赖: 同目录下的 FlashEastMoney.py 及其四大能力(getDayNews / toJsonl /
      writeJsonl)。仅 Python 3 标准库。
"""

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, Optional

# 确保能 import 同目录的 FlashEastMoney(在 GitHub Actions 中脚本位于
# Python/ 目录, 而工作目录可能是仓库根, 因此显式把脚本目录加入 sys.path)
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from FlashEastMoney import getDayNews, writeJsonl  # noqa: E402

# 内置默认配置
DEFAULT_CONFIG_FILE = "Config.json"
DEFAULT_BASE_DIR = "Archive"
DEFAULT_SLEEP = 0.3


def loadConfig(path: str) -> Dict[str, Any]:
    """读取配置文件(JSON), 返回字典; 文件不存在时返回空字典。"""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def resolveSetting(
    key: str,
    env_name: str,
    cfg: Dict[str, Any],
    default: Any,
) -> Any:
    """
    按 环境变量 > 配置文件 > 内置默认 的优先级取值。

    配置文件键名采用大驼峰风格(如 BaseDir / SleepBetween), 且查找时
    大小写不敏感: 传入小驼峰 key(如 base_dir)也能匹配 BaseDir。
    """
    env_val = os.environ.get(env_name)
    if env_val not in (None, ""):
        # 数值型配置做类型转换
        if isinstance(default, float):
            try:
                return float(env_val)
            except ValueError:
                pass
        elif isinstance(default, int):
            try:
                return int(env_val)
            except ValueError:
                pass
        return env_val

    # 大小写不敏感匹配配置键(兼容 BaseDir / base_dir 等写法)
    key_lower = str(key).lower()
    matched = None
    for cfg_key, cfg_val in cfg.items():
        if str(cfg_key).lower() == key_lower:
            matched = cfg_val
            break
    if matched not in (None, ""):
        return matched
    return default


def buildTargetPath(base_dir: str, day: datetime) -> str:
    """
    根据 base_dir 与目标日期生成文件路径:
        <base_dir>/Finv/News/FlashEastMoney/<year>/<yyyymmdd>.jsonl
    """
    return os.path.join(
        base_dir,
        "Finv",
        "News",
        "FlashEastMoney",
        day.strftime("%Y"),
        day.strftime("%Y%m%d") + ".jsonl",
    )


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="采集前一天东方财富快讯并保存为按日期命名的 jsonl 文件",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG_FILE,
        help="配置文件路径(JSON)",
    )
    parser.add_argument(
        "--date", default=None,
        help="目标日期 YYYY-MM-DD(默认=昨天, 北京时间自然日)",
    )
    parser.add_argument(
        "--base-dir", default=None,
        help="数据根目录(优先级高于 config.json 与内置默认)",
    )
    parser.add_argument(
        "--sleep", type=float, default=None,
        help="翻页请求间隔秒数",
    )
    parser.add_argument(
        "--print", dest="show", action="store_true",
        help="预览文件前 5 条(时间先后序)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只计算目标路径并输出, 不实际请求接口",
    )
    parser.add_argument(
        "--log", dest="enable_log", action="store_true",
        help="打印每次接口请求的 URL 及结果摘要(每请求一行)",
    )
    parser.add_argument(
        "--no-log", dest="enable_log", action="store_false",
        help="不打印请求日志(默认)",
    )
    parser.set_defaults(enable_log=False)
    args = parser.parse_args(argv)

    # 目标日期: 默认昨天(北京时间), 供配置解析与 dry-run 使用
    if args.date:
        target = datetime.strptime(args.date[:10], "%Y-%m-%d")
    else:
        from datetime import timedelta
        from FlashEastMoney import utcnowBeijing
        today = utcnowBeijing().replace(hour=0, minute=0, second=0, microsecond=0)
        target = today - timedelta(days=1)

    # 配置解析
    cfg = loadConfig(args.config)
    base_dir = args.base_dir or str(
        resolveSetting("base_dir", "MAIN_BASE_DIR", cfg, DEFAULT_BASE_DIR))
    sleep_between = args.sleep if args.sleep is not None else float(
        resolveSetting("sleep_between", "MAIN_SLEEP_BETWEEN", cfg, DEFAULT_SLEEP))

    target_path = buildTargetPath(base_dir, target)
    day_label = target.strftime("%Y-%m-%d")
    print("目标日期(前一天): %s" % day_label)
    print("目标文件的完整的路径: %s" % target_path)

    if args.dry_run:
        print("[dry-run] 未请求接口, 未写入文件。")
        return 0

    # 采集前一天全天快讯
    try:
        items = getDayNews(
            day=day_label,
            attach_date=False,
            sleep_between=sleep_between,
            log_fn=print if args.enable_log else None,
        )
    except OSError as exc:
        print("采集失败: %s" % exc, file=sys.stderr)
        return 1

    if not items:
        print("未获取到任何快讯。", file=sys.stderr)
        return 1

    # 保存文件(自动建目录; toJsonl 内部按时间先后排序)
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    written = writeJsonl(items, target_path)
    print("已保存->  %s  快讯 %d 条" % (target_path , written))

    if args.show:
        import json as _json
        print("\n--- 预览前 5 条(文件首行起, 时间先后) ---")
        with open(target_path, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i >= 5 or not line.strip():
                    break
                obj = _json.loads(line)
                title = obj.get("title") or ""
                if not title:
                    sm = obj.get("summary") or ""
                    if sm.startswith("【") and "】" in sm:
                        title = sm[1:sm.index("】")]
                    else:
                        title = sm[:20]
                print("[%s] %s" % (obj.get("showTime", "?"), title))

    # 供 GitHub Actions 消费的信息(写入 $GITHUB_OUTPUT, 比废弃的
    # ::set-output 更可靠)
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write("day=%s\n" % day_label)
            fh.write("path=%s\n" % target_path.replace(os.sep, "/"))
            fh.write("count=%d\n" % written)
    else:
        print("采集到->  %s  记录 %s 条" % (target_path, written))
    return 0


if __name__ == "__main__":
    sys.exit(main())
