import importlib.util
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("tasty_probe", ROOT / "tools" / "tasty_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
trade_side, count_sweeps = probe.trade_side, probe.count_sweeps


def test_trade_side_against_quote():
    assert trade_side(D("2.10"), D("2.00"), D("2.10")) == "buy"
    assert trade_side(D("2.00"), D("2.00"), D("2.10")) == "sell"
    assert trade_side(D("2.08"), D("2.00"), D("2.10")) == "lean_buy"
    assert trade_side(D("2.05"), D("2.00"), D("2.10")) == "mid"
    assert trade_side(D("2.05"), None, D("2.10")) == "unknown"
    assert trade_side(D("2.05"), D("0"), D("2.10")) == "unknown"


def _p(t, ex, side="buy", sym=".SPXW1"):
    return {"symbol": sym, "time": t, "exchange": ex, "side": side}


def test_sweep_needs_two_exchanges_within_window():
    assert count_sweeps([_p(0, "C"), _p(200, "X")]) == 1
    assert count_sweeps([_p(0, "C"), _p(200, "C")]) == 0
    assert count_sweeps([_p(0, "C"), _p(900, "X")]) == 0
    assert count_sweeps([_p(0, "C"), _p(100, "X", side="sell")]) == 0
