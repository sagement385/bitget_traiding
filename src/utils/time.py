from datetime import datetime, timezone

INTERVAL_MS={'1s':1000,'3s':3000,'5s':5000,'15s':15000,'30s':30000,'1m':60000,'2m':120000,'3m':180000,'5m':300000,'15m':900000,'30m':1800000,'1H':3600000,'2H':7200000,'4H':14400000,'6H':21600000,'12H':43200000,'1D':86400000,'1W':604800000,'1M':2592000000}

def to_ms(dt: str | int) -> int:
    if isinstance(dt, int): return dt
    return int(datetime.fromisoformat(dt.replace('Z','+00:00')).replace(tzinfo=timezone.utc).timestamp()*1000) if 'T' not in dt else int(datetime.fromisoformat(dt.replace('Z','+00:00')).timestamp()*1000)

def now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp()*1000)
