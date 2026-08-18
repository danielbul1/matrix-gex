"""Tests for the deterministic regime engine (tripity_experiment.matrix_regime).

Covers: every label reachable from crafted inputs, agreement counting,
contango/backwardation detection from a synthetic chain, Parkinson and
Garman-Klass estimators against hand-computed series, monthly OpEx proximity,
reasoning strings, and host integration (snapshot scales + endpoint wiring).
"""
import importlib.util
import math
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "railway-service" / "src"))
from tripity_experiment import matrix_regime as mr

ET = mr.ET
SPOT = 100.0
OPEX = mr.third_friday(2026, 8)  # monthly OpEx used throughout


def _ms(day):
    """Epoch ms for noon ET on a given date (the engine's valuation clock)."""
    return int(datetime(day.year, day.month, day.day, 12, 0, tzinfo=ET).timestamp() * 1000)


def _chain(front_iv, back_iv, expiries, spot=SPOT):
    """Synthetic chain: one call + one put ATM per expiry."""
    rows = []
    for i, exp in enumerate(expiries):
        iv = front_iv if i == 0 else back_iv
        rows.append({"t": "C", "k": spot, "oi": 1000, "iv": iv, "exp": exp, "root": "SPY"})
        rows.append({"t": "P", "k": spot, "oi": 1000, "iv": iv, "exp": exp, "root": "SPY"})
    return rows


def _inputs(today, **overrides):
    """Baseline: positive GEX, short vanna, positive charm, backwardated chain,
    OpEx 4 days out, spot far from the walls. Override per test."""
    expiries = [(OPEX - timedelta(days=21)).isoformat(), OPEX.isoformat()]
    base = {
        "spot": SPOT,
        "total_gex": 5e6, "gex_scale": 10e6,
        "total_vex": -3e6, "vex_scale": 6e6,
        "total_chex": 2e6, "chex_scale": 4e6,
        "flip": 95.0, "call_wall": 120.0, "put_wall": 80.0,
        "options": _chain(0.22, 0.20, expiries),  # slope +2 vol pts: backwardation
        "candles": [],
        "valuation_ms": _ms(OPEX - timedelta(days=4)),
    }
    base.update(overrides)
    base["valuation_ms"] = _ms(today)
    return base


# ---------------------------------------------------------------------------
# Term structure: contango vs backwardation from a synthetic chain
# ---------------------------------------------------------------------------
def test_term_structure_backwardation_and_contango():
    exps = ["2026-08-21", "2026-09-18"]
    back = mr.atm_iv_term_structure(_chain(0.30, 0.20, exps), SPOT)
    assert back["state"] == "backwardation"
    assert back["front_iv"] == pytest.approx(0.30)
    assert back["back_iv"] == pytest.approx(0.20)
    assert back["slope"] == pytest.approx(0.10)

    cont = mr.atm_iv_term_structure(_chain(0.15, 0.25, exps), SPOT)
    assert cont["state"] == "contango"
    assert cont["slope"] == pytest.approx(-0.10)

    flat = mr.atm_iv_term_structure(_chain(0.20, 0.202, exps), SPOT)
    assert flat["state"] == "flat"
    single = mr.atm_iv_term_structure(_chain(0.20, 0.0, exps[:1]), SPOT)
    assert single["state"] == "flat" and single["slope"] == pytest.approx(0.0)
    empty = mr.atm_iv_term_structure([], SPOT)
    assert empty["state"] == "unavailable" and empty["slope"] is None


def test_term_structure_picks_atm_strike_and_averages_call_put():
    rows = [
        {"t": "C", "k": 90, "iv": 0.50, "exp": "2026-08-21"},
        {"t": "C", "k": 100, "iv": 0.20, "exp": "2026-08-21"},
        {"t": "P", "k": 100, "iv": 0.40, "exp": "2026-08-21"},
    ]
    term = mr.atm_iv_term_structure(rows, SPOT)
    assert term["expiries"]["2026-08-21"] == pytest.approx(0.30)  # ATM only, (0.20+0.40)/2


# ---------------------------------------------------------------------------
# Parkinson / Garman-Klass against hand-computed series
# ---------------------------------------------------------------------------
def _flat_bars(n, o=100.0, h=105.0, l=100.0, c=102.0):
    return [{"open": o, "high": h, "low": l, "close": c} for _ in range(n)]


def test_parkinson_hand_computed():
    bars = _flat_bars(6)
    a = math.log(105.0 / 100.0)
    expected = math.sqrt(252 * a * a / (4 * math.log(2)))
    assert mr.parkinson_volatility(bars) == pytest.approx(expected, rel=1e-12)
    assert mr.parkinson_volatility(bars[:4]) is None  # below MIN_CANDLES


def test_garman_klass_hand_computed():
    bars = _flat_bars(8)
    hl = math.log(105.0 / 100.0)
    co = math.log(102.0 / 100.0)
    per_bar = 0.5 * hl * hl - (2 * math.log(2) - 1) * co * co
    expected = math.sqrt(252 * per_bar)
    assert mr.garman_klass_volatility(bars) == pytest.approx(expected, rel=1e-12)
    assert mr.garman_klass_volatility(bars[:2]) is None


def test_daily_ohlc_aggregates_intraday_bars():
    day1 = datetime(2026, 8, 10, 10, 0, tzinfo=ET).timestamp()
    day1b = datetime(2026, 8, 10, 15, 0, tzinfo=ET).timestamp()
    day2 = datetime(2026, 8, 11, 10, 0, tzinfo=ET).timestamp()
    candles = [
        {"time": day1, "open": 100, "high": 101, "low": 99, "close": 100.5},
        {"time": day1b, "open": 100.5, "high": 103, "low": 100, "close": 102},
        {"time": day2, "open": 102, "high": 104, "low": 101, "close": 103},
    ]
    bars = mr.daily_ohlc(candles)
    assert len(bars) == 2
    assert bars[0] == {"date": "2026-08-10", "open": 100, "high": 103, "low": 99, "close": 102}
    assert bars[1]["date"] == "2026-08-11"


# ---------------------------------------------------------------------------
# Monthly OpEx proximity
# ---------------------------------------------------------------------------
def test_third_friday():
    assert mr.third_friday(2026, 1) == date(2026, 1, 16)
    assert OPEX.weekday() == 4 and 15 <= OPEX.day <= 21 and OPEX.month == 8


def test_days_to_monthly_opex():
    assert mr.days_to_monthly_opex([OPEX.isoformat()], OPEX - timedelta(days=2)) == 2
    assert mr.days_to_monthly_opex([OPEX.isoformat()], OPEX) == 0
    # Non-monthly expiries are ignored; falls back to the calendar 3rd Friday.
    weekly = (OPEX - timedelta(days=7)).isoformat()
    assert mr.days_to_monthly_opex([weekly], OPEX - timedelta(days=2)) == 2
    # Past monthly expiries roll to the next one.
    nxt = mr.third_friday(2026, 9)
    assert mr.days_to_monthly_opex([OPEX.isoformat()], OPEX + timedelta(days=1)) == (nxt - OPEX).days - 1


# ---------------------------------------------------------------------------
# Labels: each reachable with crafted inputs
# ---------------------------------------------------------------------------
def test_grind_up_all_forces_aligned():
    out = mr.compute_regime(_inputs(OPEX - timedelta(days=4)))
    assert out["label"] == "GRIND_UP"
    assert out["agreement"] == 3 and out["direction"] == 1
    assert [out["forces"][k]["sign"] for k in ("gex", "vex", "chex")] == [1, 1, 1]


def test_trap_door_negative_gamma_rising_iv():
    front = (OPEX - timedelta(days=14)).isoformat()
    out = mr.compute_regime(_inputs(
        OPEX - timedelta(days=20),
        total_gex=-5e6,
        options=_chain(0.15, 0.25, [front, OPEX.isoformat()]),  # contango: IV rising
    ))
    assert out["label"] == "TRAP_DOOR"
    assert out["agreement"] == 2 and out["direction"] == -1
    assert out["forces"]["gex"]["sign"] == -1
    assert out["forces"]["vex"]["sign"] == -1


def test_squeeze_iv_collapsing_into_vanna_bid():
    front = (OPEX - timedelta(days=14)).isoformat()
    out = mr.compute_regime(_inputs(
        OPEX - timedelta(days=20),
        total_gex=-5e6,
        options=_chain(0.45, 0.30, [front, OPEX.isoformat()]),  # 15-pt backwardation
    ))
    assert out["label"] == "SQUEEZE"
    assert out["forces"]["vex"]["sign"] == 1
    assert out["forces"]["gex"]["sign"] == -1


def test_pin_near_opex_at_wall_quiet_vol():
    out = mr.compute_regime(_inputs(
        OPEX - timedelta(days=2),
        call_wall=100.5,  # within 1% of spot
        options=_chain(0.18, 0.22, [OPEX.isoformat(), mr.third_friday(2026, 9).isoformat()]),  # contango: quiet
    ))
    assert out["label"] == "PIN"
    assert out["forces"]["chex"]["days_to_opex"] == 2
    assert out["forces"]["chex"]["weight"] == 1.0


def test_mixed_when_forces_disagree():
    out = mr.compute_regime(_inputs(
        OPEX - timedelta(days=4),
        total_chex=0.0,  # charm neutral; +gex against a vex headwind
        options=_chain(0.15, 0.25, [(OPEX - timedelta(days=21)).isoformat(), OPEX.isoformat()]),  # vex headwind
    ))
    assert out["label"] == "MIXED"
    assert out["agreement"] == 1
    # gex +1 / vex -1 / chex -1: majority of two, but no label rule matches.
    split = mr.compute_regime(_inputs(
        OPEX - timedelta(days=4),
        total_chex=-2e6,
        options=_chain(0.15, 0.25, [(OPEX - timedelta(days=21)).isoformat(), OPEX.isoformat()]),
    ))
    assert split["label"] == "MIXED" and split["agreement"] == 2 and split["direction"] == -1


def test_pin_beats_grind_up_when_at_wall_near_opex():
    # All three winds aligned AND pinned at a wall 2 days from OpEx → PIN wins.
    # Calm candles keep vol "quiet" (VRP > 0) even though the chain is backwardated.
    calm = _flat_bars(10, h=100.5, l=99.5, c=100.0)
    out = mr.compute_regime(_inputs(OPEX - timedelta(days=2), call_wall=100.2, candles=calm))
    assert out["label"] == "PIN"
    assert [out["forces"][k]["sign"] for k in ("gex", "vex", "chex")] == [1, 1, 1]


# ---------------------------------------------------------------------------
# Agreement counting
# ---------------------------------------------------------------------------
def test_agreement_counts_majority_sign():
    assert mr.compute_regime(_inputs(OPEX - timedelta(days=4)))["agreement"] == 3
    two = mr.compute_regime(_inputs(OPEX - timedelta(days=20)))  # chex drops out (far from OpEx)
    assert two["agreement"] == 2 and two["forces"]["chex"]["sign"] == 0
    zero = mr.compute_regime({"spot": SPOT, "valuation_ms": _ms(OPEX)})
    assert zero["agreement"] == 0 and zero["label"] == "MIXED"


def test_charm_weight_decays_with_opex_distance():
    near = mr.compute_regime(_inputs(OPEX - timedelta(days=7)))
    assert near["forces"]["chex"]["sign"] == 1
    assert near["forces"]["chex"]["weight"] == pytest.approx(0.6)
    far = mr.compute_regime(_inputs(OPEX - timedelta(days=12)))
    assert far["forces"]["chex"]["sign"] == 0
    assert far["forces"]["chex"]["weight"] == 0.0


def test_noise_band_neutralizes_tiny_totals():
    out = mr.compute_regime(_inputs(OPEX - timedelta(days=4), total_gex=1e5))  # < 4% of scale
    assert out["forces"]["gex"]["sign"] == 0
    assert out["forces"]["gex"]["mode"] == "neutral"


# ---------------------------------------------------------------------------
# VRP context
# ---------------------------------------------------------------------------
def test_vrp_rich_and_cheap():
    calm = _flat_bars(10, h=100.5, l=99.5, c=100.0)  # tiny realized vol
    rich = mr.compute_regime(_inputs(OPEX - timedelta(days=4), candles=calm))
    vrp = rich["forces"]["vrp"]
    assert vrp["state"] == "rich" and vrp["vrp"] > mr.VRP_RICH
    cheap = mr.compute_regime(_inputs(OPEX - timedelta(days=4), candles=calm,
                                      options=_chain(0.02, 0.02, [(OPEX - timedelta(days=21)).isoformat(), OPEX.isoformat()])))
    assert cheap["forces"]["vrp"]["state"] in ("cheap", "fair")
    missing = mr.compute_regime(_inputs(OPEX - timedelta(days=4), candles=[]))
    assert missing["forces"]["vrp"]["state"] == "unavailable"


# ---------------------------------------------------------------------------
# Explainability + determinism
# ---------------------------------------------------------------------------
def test_reasoning_mentions_driving_forces():
    out = mr.compute_regime(_inputs(OPEX - timedelta(days=4)))
    lines = out["reasoning"]
    assert len(lines) == 5  # one per force (gex/vex/chex/vrp) + verdict
    assert "GEX" in lines[0]
    assert "vanna" in lines[1].lower() or "VEX" in lines[1]
    assert "charm" in lines[2].lower() or "CHEX" in lines[2]
    assert "IV" in lines[3]
    assert out["label"] in lines[4]
    for key in ("gex", "vex", "chex", "vrp"):
        assert out["forces"][key]["reasoning"]


def test_deterministic_same_inputs_same_output():
    args = _inputs(OPEX - timedelta(days=4))
    assert mr.compute_regime(args) == mr.compute_regime(dict(args))


# ---------------------------------------------------------------------------
# Host integration (stubs for third-party deps, Phase-2 approach)
# ---------------------------------------------------------------------------
def _install_third_party_stubs():
    class _Any:
        def __init__(self, *a, **k): pass
        def __call__(self, *a, **k): return _Any()
        def __getattr__(self, name): return _Any()
        def __mro_entries__(self, bases): return (object,)

    for name in (
        "uvicorn", "mcp", "lse_data",
        "fastapi", "fastapi.responses",
        "fastmcp", "fastmcp.client", "fastmcp.client.auth",
        "fastmcp.server", "fastmcp.server.providers", "fastmcp.server.providers.openapi",
        "fastmcp.utilities", "fastmcp.utilities.openapi",
    ):
        if name in sys.modules:
            continue
        try:
            if importlib.util.find_spec(name) is not None:
                continue
        except (ModuleNotFoundError, ValueError, TypeError):
            pass
        module = types.ModuleType(name)
        module.__getattr__ = lambda attr: _Any
        sys.modules[name] = module


def test_host_snapshot_feeds_regime_engine():
    _install_third_party_stubs()
    from tripity_experiment import public_company_host as host

    assert hasattr(host.PublicCompanyApp, "_matrix_regime")
    record = {
        "mult": 100,
        "opts": [
            {"t": "C", "k": 100, "oi": 5000, "iv": 0.2, "g": 0.0, "d": 0.0, "exp": OPEX.isoformat(), "root": "SPY"},
            {"t": "P", "k": 95, "oi": 8000, "iv": 0.25, "g": 0.0, "d": 0.0, "exp": OPEX.isoformat(), "root": "SPY"},
        ],
    }
    snap = host._compute_matrix_gex_snapshot("SPY", record, SPOT, _ms(OPEX - timedelta(days=4)))
    # Snapshot now carries the additive scale fields the engine consumes.
    assert snap["gex_scale"] > 0 and snap["vex_scale"] >= 0 and snap["chex_scale"] >= 0
    out = mr.compute_regime({**snap, "options": record["opts"], "valuation_ms": _ms(OPEX - timedelta(days=4))})
    assert out["label"] in mr.LABELS
    assert out["reasoning"]
