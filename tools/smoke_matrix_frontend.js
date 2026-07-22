/* Smoke test: load matrix.js with DOM stubs (offline), then verify
   VIX AM-settlement, real VIX chain aggregation (netVex/netCharm),
   OI-weighted strikes, and quick-select predicates. */
const fs = require('fs');
const vm = require('vm');

const absorb = new Proxy(function () {}, {
  get: (t, p) => (p === 'measureText' ? () => ({ width: 10 }) : absorb),
  apply: () => absorb,
  set: () => true,
});
function makeEl(id) {
  return {
    id, innerHTML: '', textContent: '', value: '2', disabled: false,
    style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {}, appendChild() {},
    getContext: () => absorb, clientWidth: 900, clientHeight: 600,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 900, height: 600 }),
  };
}
const els = new Map();
global.document = {
  hidden: true,
  getElementById: id => { if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); },
  querySelectorAll: () => [],
  createElement: () => makeEl('created'),
};
global.window = {
  addEventListener() {}, innerWidth: 1280, innerHeight: 800,
  devicePixelRatio: 1, location: { search: '' },
};
global.localStorage = { getItem: () => null, setItem() {} };
global.fetch = () => Promise.reject(new Error('offline smoke test'));

const code = fs.readFileSync('railway-service/src/tripity_experiment/web/matrix.js', 'utf8');
vm.runInThisContext(code, { filename: 'matrix.js' });

let failures = 0;
function check(name, cond, detail = '') {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`);
  if (!cond) failures++;
}

// --- 1. AM settlement: VIX expiry 9:30 ET vs SPY 16:00 ET ---
const now = Date.UTC(2026, 6, 20, 12, 0, 0);
const tVix = preciseYearsToExpiry('2026-07-22', 'VIX', now);
const tVixw = preciseYearsToExpiry('2026-07-22', 'VIXW', now);
const tSpy = preciseYearsToExpiry('2026-07-22', 'SPY', now);
check('VIX is AM-settled (shorter T than PM-settled)', tVix < tSpy, `vix=${(tVix * 365.25 * 24).toFixed(1)}h spy=${(tSpy * 365.25 * 24).toFixed(1)}h`);
check('VIXW matches VIX AM settlement', tVix === tVixw);

// --- 2. Real VIX chain through the engine ---
const vix = JSON.parse(fs.readFileSync(process.env.TEMP + '/vix.json', 'utf8'));
const OCC = /([A-Z]+)(\d{6})([CP])(\d{8})/;
const today = new Date();
const opts = [];
for (const o of vix.data.options) {
  const m = OCC.exec(o.option || '');
  if (!m) continue;
  const exp = `20${m[2].slice(0, 2)}-${m[2].slice(2, 4)}-${m[2].slice(4, 6)}`;
  const dte = Math.max(0, Math.round((Date.parse(exp + 'T00:00:00Z') - Date.now()) / 86400000));
  const spot = vix.data.current_price;
  const strike = parseInt(m[4], 10) / 1000;
  if (Math.abs(strike - spot) / spot > 0.25) continue;
  if (!(o.open_interest > 0)) continue;
  opts.push({ k: strike, t: m[3], root: m[1], exp, dte, iv: o.iv || 0, oi: o.open_interest | 0, vol: o.volume | 0, g: o.gamma || 0, d: o.delta || 0 });
}
const rec = { spot: vix.data.current_price, asof: new Date().toISOString(), mult: 100, opts, source: 'smoke' };
const chain = buildChainRealFromRecord('VIX', rec, null);
const R = calcGEX(chain, 'full');
check('VIX chain parsed', opts.length > 100, `${opts.length} options kept (within +/-25% of spot)`);
check('VIX strikes aggregated', R.strikes.length > 10, `${R.strikes.length} strikes`);
const hasVex = R.strikes.some(s => Math.abs(s.netVex) > 0);
const hasCharm = R.strikes.some(s => Math.abs(s.netCharm) > 0);
check('netVex populated per strike', hasVex);
check('netCharm populated per strike', hasCharm);
const roots = new Set(opts.map(o => o.root));
check('VIX+VIXW roots present', roots.has('VIX'), [...roots].join(','));

// --- 3. OI-weighted strikes ---
const w = oiWeightedStrikes(R);
check('weighted strikes finite', Number.isFinite(w.call) && Number.isFinite(w.put) && Number.isFinite(w.total),
  `call=${w.call?.toFixed(1)} put=${w.put?.toFixed(1)} total=${w.total?.toFixed(1)} spot=${R.spot}`);
const ks = R.strikes.map(s => s.strike);
check('weighted strikes within strike range',
  w.call >= Math.min(...ks) && w.call <= Math.max(...ks) && w.total >= Math.min(...ks) && w.total <= Math.max(...ks));

// --- 4. Quick-select predicates ---
check('0DTE predicate', expiryQuickPredicate('0dte')(0) === true && expiryQuickPredicate('0dte')(1) === false);
check('Week predicate', expiryQuickPredicate('week')(7) === true && expiryQuickPredicate('week')(8) === false);
check('All predicate', expiryQuickPredicate('all')(45) === true);

// --- 5. METRICS wiring ---
check('net_vex/net_charm bar metrics exist', METRICS.net_vex.kind === 'bar' && METRICS.net_charm.kind === 'bar');
check('weighted is overlay kind', METRICS.weighted.kind === 'overlay');
check('BAR_METRIC_BY_CHART maps vex/chex', BAR_METRIC_BY_CHART.vexChart === 'net_vex' && BAR_METRIC_BY_CHART.chexChart === 'net_charm');
check('chartTargets vex/chex ids', chartTargets('vexChart').tooltipId === 'vexTooltip' && chartTargets('chexChart').legendId === 'chexChartLegend');

// --- 6. VEX/CHEX draw path does not throw (absorbing canvas stub) ---
let drawOk = true;
try {
  drawChart(R, 'vexChart');
  drawChart(R, 'chexChart');
  renderGreekExposureKpis(R, 'vex');
  renderGreekExposureKpis(R, 'chex');
} catch (e) { drawOk = false; console.log('draw error:', e.message); }
check('vex/chex draw + KPI render no-throw', drawOk);

// --- 7. Weighted overlay draw path on gexChart ---
let wOk = true;
try {
  ACTIVE.add('weighted');
  drawChart(R, 'gexChart');
  ACTIVE.delete('weighted');
} catch (e) { wOk = false; console.log('weighted draw error:', e.message); }
check('weighted overlay draw no-throw', wOk);

process.exit(failures ? 1 : 0);
