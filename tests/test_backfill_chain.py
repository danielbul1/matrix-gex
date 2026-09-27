"""Tests for the ThetaData backfill (tools/backfill_chain.py).

Every test drives the backfill with a fake data client that returns canned
vendor frames, shaped like the `thetadata` library's pandas output, and
asserts on what lands in the chain store or what the command prints.
"""
import importlib.util
import json
import math
import sys
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "railway-service" / "src"))
from tripity_experiment import chain_store as cs
from tripity_experiment import matrix_gex as mg

spec = importlib.util.spec_from_file_location(
    "backfill_chain", ROOT / "tools" / "backfill_chain.py")
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)

ET = mg.ET
MONDAY = date(2026, 3, 2)
SPOT = 5000.0
IV = 0.15
STRIKES = (4950.0, 4975.0, 5000.0, 5025.0, 5050.0)


def _ms(day, hour, minute=0):
    return int(datetime(day.year, day.month, day.day, hour, minute,
                        tzinfo=ET).timestamp() * 1000)


def _stamp(day, hour, minute=0):
    """A vendor timestamp string, ET wall-clock ('YYYY-MM-DDTHH:mm:ss.SSS')."""
    return f"{day.isoformat()}T{hour:02d}:{minute:02d}:00.000"


class FakeClient:
    """Canned vendor frames for a few sessions.

    Every option quote is Black-Scholes at the canonical engine's rate from
    `spot`, +/- 0.05. Open interest per contract is 1000 plus the day of the
    month of the day the vendor reports it for, so alignment is visible.
    oi_stamping: "sod" stamps each day's OI at 06:30 that day (positions at the
    previous close); "eod" stamps it at 17:15 on the day whose close it is.
    """

    def __init__(self, sessions, expiry_offsets=(0, 4, 7), times=((10, 0), (10, 30)),
                 oi_stamping="sod", spot=SPOT, fail_on=(), delay=0.0,
                 missing_oi=()):
        self.sessions = set(sessions)
        self.expiry_offsets = expiry_offsets
        self.times = times
        self.oi_stamping = oi_stamping
        self.spot = spot
        self.fail_on = set(fail_on)  # sessions whose greeks call raises once
        self.delay = delay
        self.missing_oi = set(missing_oi)  # (strike, right) absent from OI frames
        self.calls = []
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0

    def _enter(self, *call):
        with self._lock:
            self.calls.append(call)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        time.sleep(self.delay)

    def _leave(self):
        with self._lock:
            self.in_flight -= 1

    def _expiries(self):
        return sorted({s + timedelta(days=o) for s in self.sessions
                       for o in self.expiry_offsets} | {MONDAY + timedelta(days=12)})

    def list_expirations(self, root):
        self._enter("list_expirations", root)
        self._leave()
        return pd.DataFrame({"symbol": root, "expiration": [
            e.isoformat() for e in self._expiries()]})

    def list_dates(self, root, expiration):
        self._enter("list_dates", root, expiration)
        self._leave()
        return pd.DataFrame({"date": [s.isoformat() for s in sorted(self.sessions)
                                      if s <= expiration]})

    def greeks(self, root, expiration, day, interval):
        self._enter("greeks", root, expiration, day, interval)
        try:
            if day in self.fail_on:
                self.fail_on.discard(day)
                raise ConnectionError("simulated vendor failure")
            if day not in self.sessions:
                return pd.DataFrame()
            records = []
            for hour, minute in ((9, 30),) + tuple(self.times):
                stamp_ms = _ms(day, hour, minute)
                T = mg.years_to_expiry(expiration.isoformat(), root, stamp_ms)
                for strike in STRIKES:
                    for right in ("call", "put"):
                        is_call = right == "call"
                        price = mg.bs_price(self.spot, strike, T, IV, is_call=is_call)
                        records.append({
                            "symbol": root, "expiration": expiration.isoformat(),
                            "strike": strike, "right": right,
                            "timestamp": _stamp(day, hour, minute),
                            "bid": round(max(price - 0.05, 0.0), 2),
                            "ask": round(price + 0.05, 2),
                            "delta": mg.bs_delta(self.spot, strike, T, IV, is_call=is_call),
                            "theta": -1.0, "vega": 2.0, "rho": 0.1,
                            "implied_vol": IV, "iv_error": 0.0,
                            "underlying_timestamp": _stamp(day, hour, minute),
                            "underlying_price": 0.0,
                        })
            return pd.DataFrame(records)
        finally:
            self._leave()

    def _oi_value(self, reported_for):
        return 1000.0 + reported_for.day

    def open_interest(self, root, day, max_dte):
        self._enter("open_interest", root, day, max_dte)
        try:
            if day.weekday() >= 5:
                return pd.DataFrame()
            if self.oi_stamping == "sod":
                stamp, value = _stamp(day, 6, 30), self._oi_value(day - timedelta(days=1))
            else:
                stamp, value = _stamp(day, 17, 15), self._oi_value(day)
            records = [{"symbol": root, "expiration": e.isoformat(), "strike": strike,
                        "right": right, "timestamp": stamp, "open_interest": value}
                       for e in self._expiries() if 0 <= (e - day).days <= max_dte
                       for strike in STRIKES for right in ("call", "put")
                       if (strike, right) not in self.missing_oi]
            return pd.DataFrame(records)
        finally:
            self._leave()


def _config(**overrides):
    fields = dict(roots=("SPXW",), times=((10, 0), (10, 30)), max_dte=10,
                  concurrency=1)
    return bf.BackfillConfig(**{**fields, **overrides})


def _rows(path, **filters):
    connection = cs.connect(path)
    try:
        return cs.read_rows(connection, **filters)
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
def test_vendor_rows_are_normalized_into_the_chain_store(tmp_path):
    path = tmp_path / "chain.sqlite3"
    client = FakeClient([MONDAY])
    bf.run_backfill(client, path, [MONDAY], _config())

    rows = _rows(path)
    # 3 expiries x 5 strikes x 2 rights x 2 grid times; the 09:30 row is off-grid.
    assert len(rows) == 60
    assert {r.snapshot_ms for r in rows} == {_ms(MONDAY, 10), _ms(MONDAY, 10, 30)}
    row = next(r for r in rows if r.expiry == "2026-03-09" and r.strike == 4975.0
               and r.right == "P" and r.snapshot_ms == _ms(MONDAY, 10))
    T = mg.years_to_expiry("2026-03-09", "SPXW", _ms(MONDAY, 10))
    price = mg.bs_price(SPOT, 4975.0, T, IV, is_call=False)
    assert row.root == "SPXW"
    assert row.bid == pytest.approx(round(price - 0.05, 2))
    assert row.ask == pytest.approx(round(price + 0.05, 2))
    assert row.iv == IV
    assert row.vendor_delta == pytest.approx(mg.bs_delta(SPOT, 4975.0, T, IV, is_call=False))
    assert row.vendor_gamma is None  # first-order greeks carry no gamma
    assert {r.right for r in rows} == {"C", "P"}


def test_only_expiries_within_max_dte_are_pulled(tmp_path):
    path = tmp_path / "chain.sqlite3"
    client = FakeClient([MONDAY], expiry_offsets=(0, 7, 11))
    bf.run_backfill(client, path, [MONDAY], _config())
    assert {r.expiry for r in _rows(path)} == {"2026-03-02", "2026-03-09"}
    pulled = {c[2] for c in client.calls if c[0] == "greeks"}
    assert pulled == {MONDAY, MONDAY + timedelta(days=7)}


def test_grid_times_pick_the_vendor_interval():
    assert bf.vendor_interval(bf.DEFAULT_TIMES) == "30m"
    assert bf.vendor_interval(((10, 0), (15, 45))) == "15m"
    assert bf.vendor_interval(((9, 35),)) == "5m"


def test_both_roots_are_pulled(tmp_path):
    path = tmp_path / "chain.sqlite3"
    bf.run_backfill(FakeClient([MONDAY]), path, [MONDAY], _config(roots=("SPX", "SPXW")))
    assert {r.root for r in _rows(path)} == {"SPX", "SPXW"}


# ---------------------------------------------------------------------------
# SOD Open Interest
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stamping", ["sod", "eod"])
def test_open_interest_is_the_close_of_the_previous_session(tmp_path, stamping):
    tuesday = MONDAY + timedelta(days=1)
    path = tmp_path / "chain.sqlite3"
    bf.run_backfill(FakeClient([tuesday], oi_stamping=stamping), path, [tuesday],
                    _config())
    # Positions at Monday's close, whichever way the vendor stamps them.
    assert {r.sod_oi for r in _rows(path)} == {1000.0 + MONDAY.day}


def test_open_interest_after_a_weekend_comes_from_friday(tmp_path):
    friday = MONDAY - timedelta(days=3)
    path = tmp_path / "chain.sqlite3"
    bf.run_backfill(FakeClient([MONDAY], oi_stamping="eod"), path, [MONDAY], _config())
    assert {r.sod_oi for r in _rows(path)} == {1000.0 + friday.day}


def test_contract_missing_from_open_interest_has_zero(tmp_path):
    # The vendor sends no open-interest message for a contract with none.
    path = tmp_path / "chain.sqlite3"
    client = FakeClient([MONDAY], missing_oi={(5050.0, "call")})
    bf.run_backfill(client, path, [MONDAY], _config())
    by_contract = {(r.strike, r.right): r.sod_oi for r in _rows(path)}
    assert by_contract[(5050.0, "C")] == 0.0
    assert by_contract[(5050.0, "P")] == 1000.0 + 1  # Sunday's OI for Monday


# ---------------------------------------------------------------------------
# Underlying from put-call parity
# ---------------------------------------------------------------------------
def test_underlying_is_derived_from_put_call_parity(tmp_path):
    path = tmp_path / "chain.sqlite3"
    bf.run_backfill(FakeClient([MONDAY], spot=5012.34), path, [MONDAY], _config())
    for stamp in (_ms(MONDAY, 10), _ms(MONDAY, 10, 30)):
        levels = {r.underlying for r in _rows(path, start_ms=stamp, end_ms=stamp)}
        (level,) = levels  # one level per snapshot, across expiries
        assert level == pytest.approx(5012.34, abs=0.1)


def test_parity_level_is_cross_checked_against_the_cboe_close(tmp_path):
    path = tmp_path / "chain.sqlite3"
    tuesday = MONDAY + timedelta(days=1)
    connection = cs.connect(path)
    cs.write_daily_closes(connection, "SPX", {MONDAY.isoformat(): 5001.0,
                                              tuesday.isoformat(): 5200.0})
    connection.close()
    summary = bf.run_backfill(FakeClient([MONDAY, tuesday]), path, [MONDAY, tuesday],
                              _config())
    (mismatch,) = summary["parity_mismatches"]
    assert mismatch["session"] == tuesday.isoformat()
    assert mismatch["cboe_close"] == 5200.0
    assert mismatch["parity_level"] == pytest.approx(SPOT, abs=0.1)


# ---------------------------------------------------------------------------
# Checkpoint / resume and concurrency
# ---------------------------------------------------------------------------
def test_backfill_resumes_after_a_failed_session(tmp_path):
    sessions = [MONDAY + timedelta(days=i) for i in range(3)]
    path = tmp_path / "chain.sqlite3"

    first = bf.run_backfill(FakeClient(sessions, fail_on={sessions[1]}), path,
                            sessions, _config())
    assert [f["session"] for f in first["failed"]] == [sessions[1].isoformat()]
    assert first["completed"] == [sessions[0].isoformat(), sessions[2].isoformat()]

    retry = FakeClient(sessions)
    second = bf.run_backfill(retry, path, sessions, _config())
    assert second["failed"] == []
    assert second["completed"] == [sessions[1].isoformat()]
    assert second["already_done"] == 2
    assert {c[3] for c in retry.calls if c[0] == "greeks"} == {sessions[1]}

    clean = tmp_path / "clean.sqlite3"
    bf.run_backfill(FakeClient(sessions), clean, sessions, _config())
    assert _rows(path) == _rows(clean)


def test_concurrency_never_exceeds_the_limit(tmp_path):
    sessions = [MONDAY + timedelta(days=i) for i in range(8)]
    client = FakeClient(sessions, delay=0.01)
    summary = bf.run_backfill(client, tmp_path / "chain.sqlite3", sessions,
                              _config(concurrency=3))
    assert len(summary["completed"]) == 8
    assert client.max_in_flight == 3


# ---------------------------------------------------------------------------
# Probe mode
# ---------------------------------------------------------------------------
class ProbeClient(FakeClient):
    def __init__(self, spx_underlying, **kwargs):
        super().__init__([MONDAY, date(2021, 6, 14)], **kwargs)
        self.spx_underlying = spx_underlying

    def list_dates(self, root, expiration):
        self._enter("list_dates", root, expiration)
        self._leave()
        return pd.DataFrame({"date": ["2016-10-03", "2016-10-04"]})

    def greeks(self, root, expiration, day, interval):
        frame = super().greeks(root, expiration, day, interval)
        return frame.assign(underlying_price=self.spx_underlying)


@pytest.mark.parametrize("stamping, answer", [("sod", "start of day"),
                                              ("eod", "end of day")])
def test_probe_answers_the_three_day_one_questions(stamping, answer):
    probe = bf.run_probe(ProbeClient(4246.44, oi_stamping=stamping), probe_date=MONDAY)
    assert probe["earliest_spxw_date"] == "2016-10-03"
    assert probe["open_interest_stamped"] == answer
    assert probe["spx_underlying_before_2022"] is True


def test_probe_reports_a_missing_spx_underlying():
    probe = bf.run_probe(ProbeClient(0.0), probe_date=MONDAY)
    assert probe["spx_underlying_before_2022"] is False


def test_probe_mode_prints_the_three_answers(monkeypatch, capsys):
    monkeypatch.setenv("THETADATA_API_KEY", "td-test-key")
    code = bf.main(["--probe", "--probe-date", MONDAY.isoformat()],
                   client_factory=lambda key: ProbeClient(4246.44))
    out = capsys.readouterr().out
    assert code == 0
    assert "Earliest SPXW date: 2016-10-03" in out
    assert "Open interest stamped at: start of day" in out
    assert "SPX underlying price before 2022: yes" in out


# ---------------------------------------------------------------------------
# CLI, Cboe closes and credentials
# ---------------------------------------------------------------------------
VIX_CSV = ("DATE,OPEN,HIGH,LOW,CLOSE\n"
           "03/02/2026,18.0,19.0,17.0,18.5\n"
           "03/03/2026,18.5,20.0,18.0,19.25\n")
SPX_CSV = "DATE,SPX\n03/02/2026,5001.000000\n03/03/2026,5012.500000\n"


def _cboe_fetch(url):
    return VIX_CSV if "VIX" in url else SPX_CSV


def test_cboe_daily_closes_are_loaded(tmp_path):
    connection = cs.connect(tmp_path / "chain.sqlite3")
    counts = bf.load_cboe_closes(connection, fetch_text=_cboe_fetch)
    assert counts == {"VIX": 2, "SPX": 2}
    assert cs.read_daily_closes(connection, "VIX") == {"2026-03-02": 18.5,
                                                      "2026-03-03": 19.25}
    assert cs.read_daily_closes(connection, "SPX") == {"2026-03-02": 5001.0,
                                                      "2026-03-03": 5012.5}


def test_backfill_command_loads_closes_and_chains(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("THETADATA_API_KEY", "td-test-key")
    path = tmp_path / "chain.sqlite3"
    tuesday = MONDAY + timedelta(days=1)
    code = bf.main(["--store", str(path), "--start", MONDAY.isoformat(),
                    "--end", (MONDAY + timedelta(days=6)).isoformat(),
                    "--roots", "SPXW", "--times", "10:00,10:30"],
                   client_factory=lambda key: FakeClient([MONDAY, tuesday]),
                   fetch_text=_cboe_fetch)
    summary = json.loads(capsys.readouterr().out)
    assert code == 0
    # Weekdays only; the three sessions with no vendor data complete empty.
    assert summary["completed"] == [(MONDAY + timedelta(days=i)).isoformat()
                                    for i in range(5)]
    # Monday sees six expiries within 10 DTE (both sessions' 0/4/7), Tuesday
    # five; 5 strikes x 2 rights x 2 grid times each.
    assert summary["rows_written"] == (6 + 5) * 20
    assert summary["parity_mismatches"] == []
    connection = cs.connect(path)
    assert cs.read_daily_closes(connection, "SPX")["2026-03-03"] == 5012.5
    connection.close()


def test_missing_api_key_is_reported_without_running(monkeypatch, capsys):
    monkeypatch.delenv("THETADATA_API_KEY", raising=False)
    code = bf.main(["--probe"], client_factory=lambda key: pytest.fail("no client"))
    assert code == 2
    assert "THETADATA_API_KEY" in capsys.readouterr().err


def test_api_key_never_reaches_the_store_or_output(tmp_path, monkeypatch, capsys, caplog):
    secret = "td-secret-4f9a1c"
    monkeypatch.setenv("THETADATA_API_KEY", secret)

    class LeakyClient(FakeClient):
        def greeks(self, root, expiration, day, interval):
            raise RuntimeError(f"auth rejected for key {secret}")

    path = tmp_path / "chain.sqlite3"
    caplog.set_level("DEBUG")
    code = bf.main(["--store", str(path), "--start", MONDAY.isoformat(),
                    "--end", MONDAY.isoformat(), "--roots", "SPXW"],
                   client_factory=lambda key: LeakyClient([MONDAY]),
                   fetch_text=_cboe_fetch)
    captured = capsys.readouterr()
    assert code == 1
    assert "auth rejected" in captured.out
    for text in (captured.out, captured.err, caplog.text):
        assert secret not in text
    assert secret.encode() not in path.read_bytes()
