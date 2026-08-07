# -*- coding: utf-8 -*-
"""
富途快讯 JSON → JSONL 归档转换程序(Main)
=========================================

作用:
    将 Data/Finv/News/FlashFutu/<year>/<yyyymmdd>.json(内部为 JSON 数组)
    转换为 JSONL 文件(数组元素一行一个), 保存到
    Archive/Finv/News/FlashFutu/<year>/<yyyymmdd>.jsonl 。

    专为 GitHub Actions 定时任务设计: 每天(北京时间 03:45)运行一次,
    增量合并转换新产生的 JSON 文件, 支持手动触发。

关键行为:
    1. 语法错误的 JSON 文件(部分文件因拼接存在语法问题)自动跳过,
       不中断整体流程。
    2. 目标 JSONL 已存在时, 按元素的 id 字段去重后追加新行(增量合并);
       已有行原样保留, 不重写。
    3. 保持每个元素的字段顺序(与原 JSON 文件一致)与数组元素顺序。
    4. 每行以紧凑格式输出(去掉冒号/逗号后多余空格),
       ensure_ascii=False 保留中文。
    5. 源 JSON 保留天数(默认关闭): 传 --keep-json-days N(或环境变量
       MAIN_KEEP_JSON_DAYS=N)后, 仅保留最近 N 天的源 JSON, 对更早的、
       已成功转换(目标 JSONL 已存在)的文件执行删除, 归档 JSONL 保留。
       -- 语法错误的文件不会被删除(后续会修复); 文件名不符合 yyyymmdd
       日期格式的文件也不会被删除(保守处理)。验证期间无需传该参数,
       即不删除任何源文件。

目录与命名:
    源   : <base_dir>/Finv/News/FlashFutu/<year>/<yyyymmdd>.json
    目标 : <archive_dir>/Finv/News/FlashFutu/<year>/<yyyymmdd>.jsonl

配置来源(优先级从高到低):
    1. 命令行参数(--base-dir / --archive-dir / --year / --keep-json-days 等)
    2. 环境变量 MAIN_BASE_DIR / MAIN_ARCHIVE_DIR / MAIN_KEEP_JSON_DAYS
    3. 配置文件 FlashFutuConvert.json(脚本同目录, 字段大驼峰命名:
       BaseDir / ArchiveDir / KeepJsonDays; 键名查找大小写不敏感)
    4. 内置默认值: base_dir='Data', archive_dir='Archive', keep_json_days=0(关闭)

用法:
    python3 FlashFutuConvertJSONToJSONL.py [--config FlashFutuConvert.json]
                                           [--base-dir DIR] [--archive-dir DIR]
                                           [--year YYYY] [--keep-json-days N]
                                           [--dry-run] [--log] [--force]

    --config : 配置文件路径(默认脚本同目录的 FlashFutuConvert.json)
    --year  : 仅处理指定年份(如 2026); 缺省处理 base_dir 下全部年份目录
    --keep-json-days N : 源 JSON 保留天数; 开启后仅保留最近 N 天的源 JSON,
                         N 天前已成功转换的源文件被删除(默认 0=关闭不删)
    --dry-run : 只统计并输出结果, 不实际写入或删除任何文件
    --log   : 打印每个文件的处理详情(成功行数 / 跳过原因)

依赖: 仅 Python 3 标准库。
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

# jsonl 输出编码
OUT_ENCODING = "utf-8"

# 确保能定位同目录的配置文件(在 GitHub Actions 中脚本位于 Python/Flash/
# 目录, 而工作目录可能是仓库根, 因此显式用脚本所在目录)
_HERE = os.path.dirname(os.path.abspath(__file__))

# 内置默认配置
DEFAULT_CONFIG_FILE = "FlashFutuConvert.json"
DEFAULT_BASE_DIR = "Data"
DEFAULT_ARCHIVE_DIR = "Archive"
DEFAULT_KEEP_JSON_DAYS = 0  # 0=关闭删除开关(验证期间默认不删除源文件)


def loadConfig(path: str) -> Dict[str, Any]:
    """读取配置文件(JSON), 返回字典; 文件不存在时返回空字典。"""
    if not os.path.exists(path):
        return {}
    with open(path, encoding=OUT_ENCODING) as fh:
        return json.load(fh)


def resolveSetting(
    key: str,
    env_name: str,
    cfg: Dict[str, Any],
    default: Any,
) -> Any:
    """
    按 环境变量 > 配置文件 > 内置默认 的优先级取值。

    配置文件键名采用大驼峰风格(如 BaseDir / KeepJsonDays), 且查找时
    大小写不敏感、忽略下划线: 传入小驼峰 key(如 base_dir)也能匹配 BaseDir
    (BaseDir / base_dir / baseDir 均等价)。
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

    # 大小写不敏感且忽略下划线地匹配配置键(兼容大驼峰/小驼峰/下划线)
    def _norm(s: Any) -> str:
        return str(s).lower().replace("_", "")

    key_norm = _norm(key)
    matched = None
    for cfg_key, cfg_val in cfg.items():
        if _norm(cfg_key) == key_norm:
            matched = cfg_val
            break
    if matched not in (None, ""):
        return matched
    return default

# 相对路径模板: Finv/News/FlashFutu
REL_DATA = ("Finv", "News", "FlashFutu")


def buildSourcePath(base_dir: str, year: str, basename: str) -> str:
    """源 JSON 路径: <base_dir>/Finv/News/FlashFutu/<year>/<basename>.json"""
    return os.path.join(base_dir, *REL_DATA, year, basename + ".json")


def buildTargetPath(archive_dir: str, year: str, basename: str) -> str:
    """目标 JSONL 路径: <archive_dir>/Finv/News/FlashFutu/<year>/<basename>.jsonl"""
    return os.path.join(archive_dir, *REL_DATA, year, basename + ".jsonl")


def loadExistingIds(jsonl_path: str) -> Set[Any]:
    """
    读取目标 JSONL 中已有的 id 集合, 用于增量去重。

    逐行解析提取每行对象的 id 字段; 解析失败的行(损坏行)忽略,
    保留原文不参与去重, 避免破坏已有数据。
    """
    ids: Set[Any] = set()
    if not os.path.exists(jsonl_path):
        return ids
    with open(jsonl_path, encoding=OUT_ENCODING) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue  # 损坏行: 忽略, 不参与去重
            if isinstance(obj, dict):
                el_id = obj.get("id")
                if el_id is not None:
                    ids.add(el_id)
    return ids


def collectNewLines(
    items: List[Any],
    existing_ids: Set[Any],
    force: bool,
) -> Tuple[List[str], int]:
    """
    对源数组元素做增量去重, 生成待追加的 JSONL 行。

    保持数组元素原始顺序与每个元素的字段顺序(dict 保序);
    已有 id 的元素跳过, 新增元素追加。缺失 id 的元素无法去重, 直接追加。

    返回 (待写入行列表, 新增行数)。
    """
    new_lines: List[str] = []
    added = 0
    for it in items:
        el_id = None
        if isinstance(it, dict):
            el_id = it.get("id")
            if not force and el_id is not None and el_id in existing_ids:
                continue  # 目标中已存在, 跳过
        new_lines.append(
            json.dumps(it, ensure_ascii=False, separators=(",", ":")) + "\n")
        added += 1
        if el_id is not None:
            existing_ids.add(el_id)
    return new_lines, added


def convertOne(
    source_path: str,
    target_path: str,
    force: bool,
    dry_run: bool,
    log_fn: Optional[Any],
) -> Dict[str, Any]:
    """
    转换单个 JSON 文件到 JSONL(增量合并)。

    返回统计字典:
        status   : 'ok'(已写入/有新增) / 'skip'(语法错误或非数组) /
                   'unchanged'(目标已存在且无新增元素)
        reason   : 跳过原因(仅 status='skip' 时)
        added    : 本次新增行数
    """
    if log_fn:
        log_fn("[%s] 源文件: %s" % (os.path.basename(source_path), source_path))

    # 读取并解析源 JSON; 语法错误直接跳过
    try:
        with open(source_path, encoding=OUT_ENCODING) as fh:
            data = json.load(fh)
    except (ValueError, OSError) as exc:
        reason = "JSON 语法错误, 跳过: %s" % exc
        if log_fn:
            log_fn("[%s] 跳过 -> %s" % (os.path.basename(source_path), reason))
        return {"status": "skip", "reason": reason, "added": 0}

    if not isinstance(data, list):
        reason = "顶层不是 JSON 数组(%s), 跳过" % type(data).__name__
        if log_fn:
            log_fn("[%s] 跳过 -> %s" % (os.path.basename(source_path), reason))
        return {"status": "skip", "reason": reason, "added": 0}

    # 增量合并去重
    existing_ids = loadExistingIds(target_path)
    new_lines, added = collectNewLines(data, existing_ids, force)

    if dry_run:
        if log_fn:
            log_fn("[%s] (dry-run) 将新增 %d 行 -> %s"
                   % (os.path.basename(source_path), added, target_path))
        return {"status": "ok" if added else "unchanged", "reason": "", "added": added}

    if not new_lines:
        return {"status": "unchanged", "reason": "", "added": 0}

    # 追加写入(a 模式自动建目录已由调用方保证; 文件不存在时自动创建)
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    with open(target_path, "a", encoding=OUT_ENCODING) as fh:
        fh.writelines(new_lines)

    if log_fn:
        log_fn("[%s] 新增 %d 行 -> %s"
               % (os.path.basename(source_path), added, target_path))
    return {"status": "ok", "reason": "", "added": added}


def maybeDeleteSource(
    source_path: str,
    basename: str,
    target_path: str,
    cutoff_date: Optional[Any],
    dry_run: bool,
    log_fn: Optional[Any],
) -> bool:
    """
    删除开关开启时, 删除"已成功转换且早于 cutoff 日期"的源 JSON 文件。

    删除前提(全部满足):
        1. 开关开启(cutoff_date 非 None);
        2. 文件名符合 yyyymmdd 日期格式, 且日期严格早于 cutoff;
        3. 目标 JSONL 已存在(即该文件曾被成功转换)。

    语法错误的文件(目标不存在, 后续待修复)与文件名格式不符的文件
    一律不删除。

    返回是否删除(dry-run 下表示"应删除但未执行")。
    """
    if cutoff_date is None:
        return False
    try:
        file_date = datetime.strptime(basename, "%Y%m%d").date()
    except ValueError:
        return False  # 文件名不符合 yyyymmdd, 保守不删
    if file_date >= cutoff_date:
        return False  # 在保留窗口内, 不删
    if not os.path.exists(target_path):
        return False  # 尚未成功转换(如语法错误待修复), 不删
    if not dry_run:
        os.remove(source_path)
    if log_fn:
        log_fn("[%s] 已%s删除(早于 %s): %s"
               % (basename, "待" if dry_run else "", cutoff_date, source_path))
    return True


def collectYearDirs(data_root: str, year: Optional[str]) -> List[str]:
    """收集待处理的年份目录列表; 未指定年份时扫描全部。"""
    if year:
        path = os.path.join(data_root, year)
        if not os.path.isdir(path):
            print("警告: 年份目录不存在: %s" % path, file=sys.stderr)
            return []
        return [path]
    return sorted(
        p for p in glob.glob(os.path.join(data_root, "*")) if os.path.isdir(p))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="将富途快讯 JSON 归档转换为 JSONL(增量合并, 按 id 去重)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config", default=None,
        help="配置文件路径(默认脚本同目录的 FlashFutuConvert.json; 字段大驼峰: "
             "BaseDir / ArchiveDir / KeepJsonDays)",
    )
    parser.add_argument(
        "--base-dir", default=None,
        help="源 JSON 根目录(优先级: 命令行 > MAIN_BASE_DIR > 配置 BaseDir > 内置)",
    )
    parser.add_argument(
        "--archive-dir", default=None,
        help="归档 JSONL 根目录(优先级: 命令行 > MAIN_ARCHIVE_DIR > 配置 "
             "ArchiveDir > 内置)",
    )
    parser.add_argument(
        "--year", default=None,
        help="仅处理指定年份(如 2026); 缺省处理全部年份目录",
    )
    parser.add_argument(
        "--keep-json-days", type=int, default=None,
        help="删除开关: 删除已成功转换、且早于 N 天前的源 JSON 文件"
             "(优先级: 命令行 > MAIN_KEEP_JSON_DAYS > 配置 "
             "KeepJsonDays > 内置 0=关闭)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="忽略去重集合, 强制追加全部元素",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只统计并输出结果, 不实际写入任何文件",
    )
    parser.add_argument(
        "--log", dest="enable_log", action="store_true",
        help="打印每个文件的处理详情",
    )
    parser.add_argument(
        "--no-log", dest="enable_log", action="store_false",
        help="不打印处理详情(默认)",
    )
    parser.set_defaults(enable_log=False)
    args = parser.parse_args(argv)

    log_fn = print if args.enable_log else None

    # 加载配置文件(默认脚本同目录 FlashFutuConvert.json, 字段大驼峰)
    config_path = args.config or os.path.join(_HERE, DEFAULT_CONFIG_FILE)
    cfg = loadConfig(config_path)
    if log_fn:
        log_fn("[配置] 使用配置文件: %s" % config_path)

    # 目录解析优先级: 命令行 > 环境变量 > 配置文件 > 内置默认
    base_dir = args.base_dir or str(
        resolveSetting("base_dir", "MAIN_BASE_DIR", cfg, DEFAULT_BASE_DIR))
    archive_dir = args.archive_dir or str(
        resolveSetting("archive_dir", "MAIN_ARCHIVE_DIR", cfg, DEFAULT_ARCHIVE_DIR))

    # 删除开关: 命令行 > 环境变量 > 配置文件 > 内置默认(0=关闭)
    keep_days = args.keep_json_days
    if keep_days is None:
        resolved = resolveSetting(
            "keep_json_days", "MAIN_KEEP_JSON_DAYS", cfg,
            DEFAULT_KEEP_JSON_DAYS)
        try:
            keep_days = int(resolved)
        except (ValueError, TypeError):
            print("警告: KeepJsonDays/MAIN_KEEP_JSON_DAYS 非整数(%r), "
                  "忽略删除开关" % resolved, file=sys.stderr)
            keep_days = DEFAULT_KEEP_JSON_DAYS
    # cutoff 日期(北京时间今天 - N 天): 删除日期严格早于该值的源 JSON
    cutoff_date = None
    if keep_days and keep_days > 0:
        today_beijing = datetime.now(timezone.utc) + timedelta(hours=8)
        cutoff_date = (today_beijing - timedelta(days=keep_days)).date()
    if log_fn and cutoff_date is not None:
        log_fn("[删除开关] 开启: 保留窗口 %s 之后的源 JSON 文件"
               % cutoff_date)
    elif log_fn:
        log_fn("[删除开关] 关闭(默认): 不删除任何源 JSON 文件")

    data_root = os.path.join(base_dir, *REL_DATA)
    if not os.path.isdir(data_root):
        print("错误: 源数据目录不存在: %s" % data_root, file=sys.stderr)
        return 1

    year_dirs = collectYearDirs(data_root, args.year)
    print("源数据根目录: %s" % data_root)
    print("归档根目录: %s" % os.path.join(archive_dir, *REL_DATA))
    print("年份目录 %d 个: %s" % (len(year_dirs),
                                ", ".join(os.path.basename(p) for p in year_dirs)))

    # 统计汇总
    total_files = 0
    converted = 0
    unchanged = 0
    skipped = 0
    deleted = 0
    added_lines = 0
    reasons: Dict[str, int] = {}

    for year_dir in year_dirs:
        year = os.path.basename(year_dir)
        for src_path in sorted(glob.glob(os.path.join(year_dir, "*.json"))):
            total_files += 1
            basename = os.path.basename(src_path)[:-len(".json")]
            target_path = buildTargetPath(archive_dir, year, basename)
            result = convertOne(src_path, target_path, args.force,
                                args.dry_run, log_fn)
            added_lines += result["added"]
            if result["status"] == "ok":
                converted += 1
            elif result["status"] == "unchanged":
                unchanged += 1
            else:
                skipped += 1
                key = result["reason"].split(", ")[0]
                reasons[key] = reasons.get(key, 0) + 1
            # 删除开关: 已成功转换且早于 cutoff 的源 JSON 文件
            if maybeDeleteSource(src_path, basename, target_path,
                                 cutoff_date, args.dry_run, log_fn):
                deleted += 1

    # 汇总输出
    print("\n===== 转换汇总(%s) =====" % ("dry-run" if args.dry_run else "已写入"))
    print("文件总数   : %d" % total_files)
    print("已转换新增 : %d 个文件, 新增 %d 行" % (converted, added_lines))
    print("无新增跳过 : %d 个文件" % unchanged)
    print("跳过(异常) : %d 个文件" % skipped)
    for reason, cnt in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print("    - %s : %d" % (reason, cnt))
    if cutoff_date is not None:
        print("删除源JSON : %d 个文件(开关开启, 保留 %s 之后的源文件)"
              % (deleted, cutoff_date))
    else:
        print("删除源JSON : 开关关闭, 未删除任何源文件")

    # 供 GitHub Actions 消费的信息(写入 $GITHUB_OUTPUT)
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding=OUT_ENCODING) as fh:
            fh.write("converted=%d\n" % converted)
            fh.write("skipped=%d\n" % skipped)
            fh.write("added=%d\n" % added_lines)
            fh.write("deleted=%d\n" % deleted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
