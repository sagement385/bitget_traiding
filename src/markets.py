from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo


CRYPTO = "CRYPTO"
KOR_STOCK = "KOR_STOCK"
US_STOCK = "US_STOCK"
MARKET_TYPES = frozenset({CRYPTO, KOR_STOCK, US_STOCK})


@dataclass(frozen=True)
class MarketSpec:
    market_type: str
    timezone_name: str
    session_open: time | None = None
    session_close: time | None = None

    @property
    def timezone(self):
        return ZoneInfo(self.timezone_name)


MARKET_SPECS = {
    # Bitget daily/week/month candles use the exchange's UTC+8 session basis.
    CRYPTO: MarketSpec(CRYPTO, "Asia/Shanghai"),
    KOR_STOCK: MarketSpec(KOR_STOCK, "Asia/Seoul", time(9, 0), time(15, 30)),
    US_STOCK: MarketSpec(US_STOCK, "America/New_York", time(9, 30), time(16, 0)),
}


def normalize_market_type(value: str | None, category: str | None = None) -> str:
    if value is not None and value != value:  # NaN without importing pandas.
        value = None
    text = str(value or "").upper().strip()
    aliases = {
        "CRYPTO": CRYPTO,
        "BITGET": CRYPTO,
        "KOR": KOR_STOCK,
        "KR": KOR_STOCK,
        "KOR_STOCK": KOR_STOCK,
        "KOREAN_STOCK": KOR_STOCK,
        "US": US_STOCK,
        "USA": US_STOCK,
        "US_STOCK": US_STOCK,
        "AMERICAN_STOCK": US_STOCK,
    }
    if text in aliases:
        return aliases[text]
    # Existing Bitget categories must remain crypto by default for backwards compatibility.
    if str(category or "").upper().endswith("FUTURES"):
        return CRYPTO
    if not text:
        return CRYPTO
    raise ValueError(f"Unsupported market_type: {value}")


def market_spec(market_type: str | None, category: str | None = None) -> MarketSpec:
    return MARKET_SPECS[normalize_market_type(market_type, category)]


def market_local_datetime(timestamp_ms: int, market_type: str | None, category: str | None = None) -> datetime:
    utc = datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=timezone.utc)
    return utc.astimezone(market_spec(market_type, category).timezone)


def is_regular_session_timestamp(timestamp_ms: int, market_type: str | None, category: str | None = None) -> bool:
    spec = market_spec(market_type, category)
    if spec.market_type == CRYPTO:
        return True
    local = market_local_datetime(timestamp_ms, spec.market_type)
    if local.weekday() >= 5:
        return False
    current = local.timetz().replace(tzinfo=None)
    # Candle timestamps represent the beginning of a minute. The final regular
    # one-minute bar begins one minute before the published closing time.
    return bool(spec.session_open <= current < spec.session_close)
