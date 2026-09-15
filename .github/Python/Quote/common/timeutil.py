"""
Time and timezone utilities.

UTC/BJT conversion, date slicing, market inference, trading calendar.
"""

import calendar
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

UTC = timezone.utc
BJT = timezone(timedelta(hours=8))

_holidays_cache: Dict[str, Set[str]] = {}


def _load_holidays(path: Optional[str]) -> Set[str]:
    if not path:
        return set()
    p = Path(path)
    if not p.exists():
        return set()
    holidays = set()
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                holidays.add(line)
    return holidays


def get_holidays(market: str, holidays_files: Dict[str, str]) -> Set[str]:
    if market == "crypto":
        return set()
    if market not in _holidays_cache:
        _holidays_cache[market] = _load_holidays(holidays_files.get(market))
    return _holidays_cache[market]


def ts_to_bjt_dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC).astimezone(BJT)


def ts_to_bjt_date(ts: int) -> date:
    return ts_to_bjt_dt(ts).date()


def bjt_day_range_utc(d: date) -> Tuple[datetime, datetime]:
    day_start = datetime.combine(d, time(0, 0, 0), tzinfo=BJT)
    day_end = day_start + timedelta(days=1)
    return (day_start.astimezone(UTC), day_end.astimezone(UTC))


def ts_in_range(ts: int, start_utc: datetime, end_utc: datetime) -> bool:
    dt = datetime.fromtimestamp(ts, tz=UTC)
    return start_utc <= dt < end_utc


def last_complete_month(now_bjt: Optional[datetime] = None) -> Tuple[int, int, str]:
    if now_bjt is None:
        now_bjt = datetime.now(BJT)
    year = now_bjt.year
    month = now_bjt.month
    if month == 1:
        year -= 1
        month = 12
    else:
        month -= 1
    month_start = datetime(year, month, 1, tzinfo=BJT)
    if month == 12:
        month_end = datetime(year + 1, 1, 1, tzinfo=BJT)
    else:
        month_end = datetime(year, month + 1, 1, tzinfo=BJT)
    return (
        int(month_start.astimezone(UTC).timestamp()),
        int(month_end.astimezone(UTC).timestamp()),
        f"{year}{month:02d}",
    )


def infer_market_from_code(code: str) -> str:
    if not code:
        return "usd"
    if code.isdigit():
        return "cny"
    code_upper = code.upper()
    crypto_set = {"BTC", "ETH", "PAXG", "USDT", "USDC", "DAI", "SOL", "XRP"}
    if code_upper in crypto_set:
        return "crypto"
    forex_pairs = {"USDCNY", "EURUSD", "GBPUSD", "AUDUSD", "USDCAD", "USDJPY", "USDSGD"}
    if code_upper in forex_pairs:
        return "usd"
    metal_codes = {"XAUUSD", "XAGUSD", "XPTUSD", "XPDUSD"}
    if code_upper in metal_codes:
        return "usd"
    return "usd"


def is_trading_day(d: date, market: str, holidays_files: Dict[str, str]) -> bool:
    if market == "crypto":
        return True
    if d.weekday() >= 5:
        return False
    holidays = get_holidays(market, holidays_files)
    return d.strftime("%Y%m%d") not in holidays


def get_trading_days_in_month(year: int, month: int, market: str, holidays_files: Dict[str, str]) -> List[date]:
    trading_days = []
    for day in range(1, calendar.monthrange(year, month)[1] + 1):
        d = date(year, month, day)
        if is_trading_day(d, market, holidays_files):
            trading_days.append(d)
    return trading_days
