import json
import os
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "cboe_data.json"
STATUS_PATH = ROOT / "data_status.json"
REQUIRED_SYMBOLS = ("NDX", "SPX", "SPY", "QQQ", "IWM", "AAPL", "NVDA", "TSLA")
FRESHNESS_SYMBOL = "SPX"
FRESHNESS_MAX_AGE = timedelta(minutes=75)
FRESHNESS_FUTURE_TOLERANCE = timedelta(minutes=5)
FRESHNESS_MARKET_START_ET = time(9, 45)
FRESHNESS_MARKET_END_ET = time(16, 30)
ET = ZoneInfo("America/New_York")


def current_et_time():
    override = os.environ.get("MATRIX_NOW_ET")
    if override:
        dt = datetime.fromisoformat(override)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=ET)
        return dt.astimezone(ET)
    return datetime.now(ET)


def parse_cboe_asof(value):
    if not value:
        return None
    text = str(value)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ET)
    return dt.astimezone(ET)


def should_check_freshness(now_et):
    if now_et.weekday() >= 5:
        return False
    current_time = now_et.time()
    return FRESHNESS_MARKET_START_ET <= current_time <= FRESHNESS_MARKET_END_ET


def unique_expiries(options):
    return sorted({opt.get("exp") for opt in options if opt.get("exp")})


def freshness_state(asof, now_et):
    if asof is None:
        return "unknown"
    if not should_check_freshness(now_et):
        return "off_hours"
    if asof > now_et + FRESHNESS_FUTURE_TOLERANCE:
        return "future"
    age = now_et - asof
    if asof.date() == now_et.date() and age <= FRESHNESS_MAX_AGE:
        return "fresh"
    return "stale"


def build_status(data, now_et=None):
    now_et = now_et or current_et_time()
    symbols = {}
    parsed_asofs = {}

    for sym in REQUIRED_SYMBOLS:
        rec = data.get(sym) or {}
        opts = rec.get("opts") or []
        asof = parse_cboe_asof(rec.get("asof"))
        parsed_asofs[sym] = asof
        expiries = unique_expiries(opts)
        symbols[sym] = {
            "spot": rec.get("spot"),
            "asof": rec.get("asof"),
            "asof_et": asof.isoformat() if asof else None,
            "contracts": len(opts),
            "expiries": len(expiries),
            "first_expiry": expiries[0] if expiries else None,
            "last_expiry": expiries[-1] if expiries else None,
        }

    valid_asofs = {sym: asof for sym, asof in parsed_asofs.items() if asof is not None}
    latest_symbol = max(valid_asofs, key=valid_asofs.get) if valid_asofs else None
    oldest_symbol = min(valid_asofs, key=valid_asofs.get) if valid_asofs else None
    reference_asof = valid_asofs.get(FRESHNESS_SYMBOL)
    state = freshness_state(reference_asof, now_et)

    return {
        "schema_version": 1,
        "source": "CBOE delayed quotes",
        "reference_symbol": FRESHNESS_SYMBOL,
        "state": state,
        "market_window": {
            "timezone": "America/New_York",
            "start": FRESHNESS_MARKET_START_ET.strftime("%H:%M"),
            "end": FRESHNESS_MARKET_END_ET.strftime("%H:%M"),
            "max_age_minutes": int(FRESHNESS_MAX_AGE.total_seconds() // 60),
        },
        "summary": {
            "symbols": len(symbols),
            "total_contracts": sum(item["contracts"] for item in symbols.values()),
            "latest_symbol": latest_symbol,
            "latest_asof_et": valid_asofs[latest_symbol].isoformat() if latest_symbol else None,
            "oldest_symbol": oldest_symbol,
            "oldest_asof_et": valid_asofs[oldest_symbol].isoformat() if oldest_symbol else None,
        },
        "symbols": symbols,
    }


def write_status(status):
    STATUS_PATH.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main():
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    status = build_status(data)
    write_status(status)
    print(f"wrote {STATUS_PATH.name}: state={status['state']} contracts={status['summary']['total_contracts']}")


if __name__ == "__main__":
    main()
