import json
import os
import re
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
STATUS_PATH = ROOT / "data_status.json"
REQUIRED_SYMBOLS = ("NDX", "SPX", "SPY", "QQQ")
FRESHNESS_SYMBOL = "SPX"
FRESHNESS_MAX_AGE = timedelta(minutes=75)
FRESHNESS_FUTURE_TOLERANCE = timedelta(minutes=5)
FRESHNESS_MARKET_START_ET = time(9, 45)
FRESHNESS_MARKET_END_ET = time(16, 30)
ET = ZoneInfo("America/New_York")
REQUIRED_IDS = (
    "symbol",
    "expirationPicker",
    "source",
    "autorefresh",
    "mode",
    "strikeCount",
    "spotOverride",
    "run",
    "dataHealth",
    "dataHealthState",
    "dataHealthSource",
    "dataHealthAge",
    "dataHealthContracts",
    "dataHealthAsof",
    "dataHealthSpot",
    "dataHealthLoaded",
    "staleDataBanner",
    "forceTripityRefresh",
    "marketReadPanel",
    "keyLevelsPanel",
    "scenarioLadderPanel",
    "signalQualityPanel",
    "snapshotControls",
    "snapshotNote",
    "saveSnapshot",
    "matrixGexChart",
    "matrixGexTooltip",
    "matrixGexCrosshairX",
    "matrixSymLabel",
    "matrixChartLegend",
    "shockStatePanel",
    "shockUpsidePanel",
    "shockDownsidePanel",
    "shockEngineChart",
    "shockTooltip",
    "reviewSnapshotList",
    "reviewOutcomeFilter",
    "reviewStatsPanel",
    "clearSnapshots",
    "gexChart",
    "view-dex",
    "dexChart",
    "dexTooltip",
    "dexCrosshairX",
    "dexChartLegend",
    "view-market-structure",
    "marketGexChart",
    "marketGexTooltip",
    "marketGexCrosshairX",
    "marketGexLegend",
    "marketPriceCanvas",
    "marketLevelLegend",
    "marketStructureStatus",
    "optionsHeatMap",
    "netFlowChart",
    "darkPoolLevels",
    "maxPainChart",
    "matrixPriceChart",
    "matrixPriceNote",
    "marketReadCard",
    "dealerScenarioPanel",
    "dealerFlowMap",
    "edgeChart",
)


def fail(message):
    print(f"FAIL: {message}", file=sys.stderr)
    return 1


def warn(message):
    print(f"WARN: {message}", file=sys.stderr)


def require_fresh_data():
    return os.environ.get("MATRIX_REQUIRE_FRESH_DATA") == "1"


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


def validate_data():
    path = ROOT / "cboe_data.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return fail(f"{path.name} is not valid JSON: {exc}")

    missing = [sym for sym in REQUIRED_SYMBOLS if sym not in data]
    if missing:
        return fail(f"missing symbols in cboe_data.json: {', '.join(missing)}")

    for sym in REQUIRED_SYMBOLS:
        rec = data[sym]
        if not isinstance(rec, dict):
            return fail(f"{sym} record is not an object")
        if not isinstance(rec.get("opts"), list) or not rec["opts"]:
            return fail(f"{sym} has no options")
        if not isinstance(rec.get("spot"), (int, float)) or rec["spot"] <= 0:
            return fail(f"{sym} has invalid spot")
        if not rec.get("asof"):
            return fail(f"{sym} missing asof")
        sample = rec["opts"][0]
        for key in ("k", "t", "exp", "dte", "iv", "oi", "vol", "g", "d"):
            if key not in sample:
                return fail(f"{sym} option rows missing {key}")
    return 0


def validate_freshness():
    path = ROOT / "cboe_data.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    now_et = current_et_time()

    if not should_check_freshness(now_et):
        return 0

    rec = data.get(FRESHNESS_SYMBOL) or {}
    asof = parse_cboe_asof(rec.get("asof"))
    if asof is None:
        message = f"{FRESHNESS_SYMBOL} has unparseable asof for freshness check"
        if require_fresh_data():
            return fail(message)
        warn(message)
        return 0

    age = now_et - asof
    same_session = asof.date() == now_et.date()
    if asof > now_et + FRESHNESS_FUTURE_TOLERANCE:
        message = (
            f"{FRESHNESS_SYMBOL} data timestamp is in the future during market hours: "
            f"asof={asof.isoformat()} now_et={now_et.isoformat()}"
        )
        if require_fresh_data():
            return fail(message)
        warn(message)
        return 0

    fresh_enough = same_session and age <= FRESHNESS_MAX_AGE
    if fresh_enough:
        return 0

    minutes = int(age.total_seconds() // 60)
    message = (
        f"{FRESHNESS_SYMBOL} data is stale during market hours: "
        f"asof={asof.isoformat()} now_et={now_et.isoformat()} age={minutes}m"
    )
    if require_fresh_data():
        return fail(message)
    warn(message)
    return 0


def validate_status_file():
    data = json.loads((ROOT / "cboe_data.json").read_text(encoding="utf-8"))
    try:
        status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return fail(f"{STATUS_PATH.name} is not valid JSON: {exc}")

    if status.get("schema_version") != 1:
        return fail("data_status.json has unsupported schema_version")
    if status.get("reference_symbol") != FRESHNESS_SYMBOL:
        return fail("data_status.json has unexpected reference_symbol")
    if status.get("source") != "LSE options snapshot":
        return fail("data_status.json has unexpected source")
    if status.get("state") not in {"fresh", "stale", "future", "off_hours", "unknown"}:
        return fail("data_status.json has invalid state")

    symbols = status.get("symbols")
    if not isinstance(symbols, dict):
        return fail("data_status.json missing symbols object")
    missing = [sym for sym in REQUIRED_SYMBOLS if sym not in symbols]
    if missing:
        return fail(f"data_status.json missing symbols: {', '.join(missing)}")

    for sym in REQUIRED_SYMBOLS:
        rec = data[sym]
        status_rec = symbols[sym]
        opts = rec.get("opts") or []
        if status_rec.get("asof") != rec.get("asof"):
            return fail(f"data_status.json {sym} asof does not match cboe_data.json")
        if status_rec.get("spot") != rec.get("spot"):
            return fail(f"data_status.json {sym} spot does not match cboe_data.json")
        if status_rec.get("contracts") != len(opts):
            return fail(f"data_status.json {sym} contract count does not match cboe_data.json")

    total = sum(len((data[sym].get("opts") or [])) for sym in REQUIRED_SYMBOLS)
    if (status.get("summary") or {}).get("total_contracts") != total:
        return fail("data_status.json total_contracts does not match cboe_data.json")
    return 0


def validate_dashboard():
    path = ROOT / "gex_dashboard.html"
    html = path.read_text(encoding="utf-8")
    js = (ROOT / "assets" / "dashboard.js").read_text(encoding="utf-8")
    css = (ROOT / "assets" / "dashboard.css").read_text(encoding="utf-8")

    missing_ids = [item for item in REQUIRED_IDS if f'id="{item}"' not in html]
    if missing_ids:
        return fail(f"dashboard missing required ids: {', '.join(missing_ids)}")

    if 'href="assets/dashboard.css' not in html:
        return fail("dashboard does not load assets/dashboard.css")
    if 'src="assets/dashboard.js' not in html:
        return fail("dashboard does not load assets/dashboard.js")
    if ":root" not in css or "body{" not in css:
        return fail("dashboard CSS asset looks incomplete")
    if "debugStaleBanner" not in js:
        return fail("dashboard stale banner debug hook missing")

    for function_name in ("loadData", "run", "drawChart", "drawShockEngine", "renderDealerFlowMap", "updateStaleDataBanner", "shouldShowStaleDataBanner"):
        if not re.search(rf"function\s+{re.escape(function_name)}\s*\(", js):
            return fail(f"dashboard missing function {function_name}()")

    broken_user_text = ("ð", "Â", "â€”", "â†", "Î“", "Ã—", "â‰¤", "âˆ’")
    found = [token for token in broken_user_text if token in html or token in js or token in css]
    if found:
        return fail(f"dashboard still contains mojibake tokens: {', '.join(found)}")
    return 0


def validate_server_contracts():
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    if "@app.route('/status')" not in server:
        return fail("server.py missing /status route")
    if "data_status.json" not in server:
        return fail("server.py /status route does not reference data_status.json")
    return 0


def main():
    for check in (validate_data, validate_freshness, validate_status_file, validate_dashboard, validate_server_contracts):
        rc = check()
        if rc:
            return rc
    print("Smoke check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
