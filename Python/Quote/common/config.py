"""
配置加载模块

优先级: Config.yaml < 环境变量
相对路径以 git 仓库根为参考点
"""

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import yaml


@dataclass
class Config:
    """全局配置"""
    repo_root: Path
    data_dir: Path               # 原始采集数据根目录
    archive_dir: Path            # 归档根目录
    codes: List[str] = field(default_factory=list)
    latest_window_days: int = 20
    daily_archive_after_days: int = 10
    monthly_delete_lag_months: int = 2
    cleanup_raw_after_aggregate: bool = True
    log_dir: Path = field(default_factory=lambda: Path('Python/Quote/logs'))
    log_retention_days: int = 14
    git_push_retries: int = 1
    holidays_files: Dict[str, str] = field(default_factory=dict)


def _find_repo_root() -> Path:
    """定位 git 仓库根目录"""
    workspace = os.environ.get('GITHUB_WORKSPACE')
    if workspace:
        return Path(workspace).resolve()

    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            capture_output=True, text=True, check=True,
        )
        return Path(result.stdout.strip()).resolve()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path.cwd().resolve()


def load_config() -> Config:
    """加载并解析 Config.yaml，合并环境变量覆盖"""
    repo_root = _find_repo_root()

    config_path = repo_root / 'Python' / 'Quote' / 'Config.yaml'
    if not config_path.exists():
        raise FileNotFoundError(f'配置文件不存在: {config_path}')

    with open(config_path, 'r', encoding='utf-8') as f:
        raw = yaml.safe_load(f) or {}

    # codes: 环境变量 QUOTE_CODES 优先于配置文件
    codes_env = os.environ.get('QUOTE_CODES', '')
    codes = [c.strip() for c in codes_env.split(',') if c.strip()]
    if not codes:
        codes = list(raw.get('Codes', []))

    # holidays_files: 解析相对路径为绝对路径
    hf_raw: dict = raw.get('HolidaysFiles', {}) or {}
    holidays_files = {
        market: str((repo_root / rel_path).resolve())
        for market, rel_path in hf_raw.items()
    }

    return Config(
        repo_root=repo_root,
        data_dir=(repo_root / raw.get('DataRel', 'Data/Finv/SecuQuote')).resolve(),
        archive_dir=(repo_root / raw.get('ArchiveRel', 'Archive/Finv/SecuQuote')).resolve(),
        codes=codes,
        latest_window_days=int(raw.get('LatestWindowDays', 20)),
        daily_archive_after_days=int(raw.get('DailyArchiveAfterDays', 10)),
        monthly_delete_lag_months=int(raw.get('MonthlyDeleteLagMonths', 2)),
        cleanup_raw_after_aggregate=bool(raw.get('CleanupRawAfterAggregate', True)),
        log_dir=repo_root / raw.get('LogDir', 'Python/Quote/logs'),
        log_retention_days=int(raw.get('LogRetentionDays', 14)),
        git_push_retries=int(raw.get('GitPushRetries', 1)),
        holidays_files=holidays_files,
    )
