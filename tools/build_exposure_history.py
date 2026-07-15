"""Append compact per-strike GEX/DEX snapshots and retain seven trading days."""
import argparse
import datetime as dt
import json
from pathlib import Path


def snapshot(symbol, rec):
    spot = float(rec.get("spot") or 0)
    mult = float(rec.get("mult") or 100)
    strikes = {}
    for option in rec.get("opts", []):
        strike = float(option.get("k") or 0)
        if strike <= 0:
            continue
        row = strikes.setdefault(strike, [strike, 0.0, 0.0, 0.0, 0.0])
        oi = float(option.get("oi") or 0)
        gamma = float(option.get("g") or 0)
        delta = float(option.get("d") or 0)
        gex = gamma * oi * mult * spot * spot * 0.01
        dex = delta * oi * mult * spot * 0.01
        if option.get("t") == "C":
            row[1] += gex
            row[3] += dex
        else:
            row[2] -= gex
            row[4] += dex
    rows = [[r[0], round(r[1], 2), round(r[2], 2), round(r[3], 2), round(r[4], 2)]
            for r in sorted(strikes.values())]
    return {"asof": rec.get("asof"), "spot": spot, "rows": rows}


def bucket(value):
    text = str(value or "")
    return text[:15] if len(text) >= 15 else text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="cboe_data.json")
    parser.add_argument("--root", default="exposure_history")
    parser.add_argument("--retain", type=int, default=7)
    args = parser.parse_args()
    source = json.loads(Path(args.input).read_text())
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    day = dt.datetime.now(dt.timezone.utc).date().isoformat()
    day_dir = root / day
    day_dir.mkdir(exist_ok=True)

    for symbol, rec in source.items():
        current = snapshot(symbol, rec)
        target = day_dir / f"{symbol}.json"
        payload = {"date": day, "symbol": symbol, "interval_minutes": 5, "scope": "all_expirations", "snapshots": []}
        if target.exists():
            try:
                payload = json.loads(target.read_text())
            except (ValueError, OSError):
                pass
        snapshots = payload.setdefault("snapshots", [])
        current_bucket = bucket(current.get("asof"))
        snapshots = [item for item in snapshots if bucket(item.get("asof")) != current_bucket]
        snapshots.append(current)
        snapshots.sort(key=lambda item: str(item.get("asof") or ""))
        payload["snapshots"] = snapshots
        target.write_text(json.dumps(payload, separators=(",", ":")))

    days = sorted([p.name for p in root.iterdir() if p.is_dir() and len(p.name) == 10])
    for old_day in days[:-args.retain]:
        for child in (root / old_day).glob("*"):
            child.unlink()
        (root / old_day).rmdir()
    days = days[-args.retain:]
    index = {"updated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "days": days,
             "interval_minutes": 5, "retention_trading_days": args.retain, "scope": "all_expirations"}
    (root / "index.json").write_text(json.dumps(index, separators=(",", ":")))
    print(f"updated exposure history for {len(source)} symbols; retained {len(days)} days")


if __name__ == "__main__":
    main()
