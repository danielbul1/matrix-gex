"""One gamma source (#68): the dashboard's Net GEX uses Black-Scholes on each
row's IV at the spot shown, never vendor greeks computed at another price,
whatever the record's source tag. Runs matrix.js in Node with inert DOM stubs."""
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "railway-service" / "src"))
from tripity_experiment import matrix_gex as mg

MATRIX_JS = ROOT / "railway-service" / "src" / "tripity_experiment" / "web" / "matrix.js"
ASOF = "2026-10-27T10:00:00-04:00"

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const [src, payload] = process.argv.slice(1);
const inert = new Proxy(function(){}, {get:(t,k)=>k===Symbol.toPrimitive?()=>'':inert,
  apply:()=>inert, construct:()=>inert});
const ctx = {console, Math, Date, JSON, Number, String, Object, Array, Map, Set, Promise, Intl,
  URLSearchParams, setTimeout:()=>0, clearTimeout:()=>0, requestAnimationFrame:()=>0,
  window:inert, document:inert, localStorage:inert, location:inert, navigator:inert,
  history:inert, Plotly:inert, addEventListener:()=>{}, fetch:()=>new Promise(()=>{})};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(src, 'utf8') +
  '\n;globalThis.__api={buildChainRealFromRecord, calcGEX};', ctx);
const out = {};
for (const [key, rec] of Object.entries(JSON.parse(payload))) {
  const r = ctx.__api.calcGEX(ctx.__api.buildChainRealFromRecord(rec.symbol, rec));
  out[key] = r.totalGex;
}
console.log(JSON.stringify(out));
"""


def _opts():
    # Vendor gamma and delta as if computed at a much higher underlying.
    opts = []
    for k in (95, 98, 100, 102, 105):
        for t in ("C", "P"):
            opts.append({"k": float(k), "t": t, "root": "SPY", "exp": "2026-10-30", "dte": 3,
                         "iv": 0.2, "oi": 1500 if t == "P" else 1000, "vol": 0,
                         "g": 0.2 if (t == "C" and k > 100) else 0.001,
                         "d": 0.9 if t == "C" else -0.05})
    return opts


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node to run matrix.js")
def test_dashboard_net_gex_ignores_vendor_greeks_for_every_source_tag():
    opts = _opts()
    records = {
        source: {"symbol": "SPY", "spot": 100.0, "mult": 100, "asof": ASOF, "price_asof": ASOF,
                 "source": source, "opts": opts}
        for source in ("lse_live+cboe_oi", "lse_live_index+cboe_oi", "tripity_cboe")
    }
    run = subprocess.run(["node", "-e", HARNESS, str(MATRIX_JS), json.dumps(records)],
                         capture_output=True, text=True, timeout=60, check=True)
    totals = json.loads(run.stdout.strip().splitlines()[-1])

    valuation_ms = datetime.fromisoformat(ASOF).timestamp() * 1000
    rows = mg.aggregate_strikes(opts, 100.0, 100, valuation_ms)
    expected = sum(row["net_gex"] for row in rows)
    vendor = sum((1 if o["t"] == "C" else -1) * mg.gex_value(o["g"], o["oi"], 100, 100.0) for o in opts)
    assert abs(vendor - expected) > abs(expected)  # the vendor greeks would move the answer
    for source, total in totals.items():
        assert total == pytest.approx(expected, rel=1e-3), source


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node to run matrix.js")
def test_dashboard_early_closes_match_the_engine():
    # Every July 3, Thanksgiving Friday and December 24 from 2024 to 2035.
    from datetime import date, timedelta
    days = []
    for year in range(2024, 2036):
        days += [date(year, 7, 3), date(year, 12, 24)]
        days += [date(year, 11, d) for d in range(23, 30)]
    script = HARNESS.split("const out = {};")[0] + (
        "vm.runInContext('globalThis.__early=isEarlyClose;', ctx);"
        "console.log(JSON.stringify(JSON.parse(payload).map(d => ctx.__early(...d))));")
    run = subprocess.run(["node", "-e", script, str(MATRIX_JS),
                          json.dumps([[d.year, d.month, d.day] for d in days])],
                         capture_output=True, text=True, timeout=60, check=True)
    assert json.loads(run.stdout.strip().splitlines()[-1]) == [mg.is_early_close(d) for d in days]
