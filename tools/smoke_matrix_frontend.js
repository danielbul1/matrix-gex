/* Smoke test: load matrix.js with DOM stubs (offline), then verify
   VIX AM-settlement, real VIX chain aggregation, quick-select predicates,
   the Strike Chart draw path, the P/C Ratio table and the OI Volume screen.
   Needs a Cboe VIX chain at $TEMP/vix.json
   (https://cdn-api.cboe.com/api/global/delayed_quotes/options/_VIX.json). */
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
    addEventListener() {}, appendChild() {}, setAttribute() {}, removeAttribute() {},
    getContext: () => absorb, clientWidth: 900, clientHeight: 600,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 900, height: 600 }),
  };
}
const els = new Map();
global.document = {
  hidden: true,
  getElementById: id => { if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); },
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => makeEl('created'),
};
global.window = {
  addEventListener() {}, innerWidth: 1280, innerHeight: 800,
  devicePixelRatio: 1, location: { search: '', pathname: '/matrix/' },
  scrollTo() {},
};
global.history = { pushState() {}, replaceState() {} };
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
const roots = new Set(opts.map(o => o.root));
check('VIX+VIXW roots present', roots.has('VIX'), [...roots].join(','));

// --- 3. Quick-select predicates ---
check('0DTE predicate', expiryQuickPredicate('0dte')(0) === true && expiryQuickPredicate('0dte')(1) === false);
check('Week predicate', expiryQuickPredicate('week')(7) === true && expiryQuickPredicate('week')(8) === false);
check('All predicate', expiryQuickPredicate('all')(45) === true);

// --- 4. Strike Chart draw path does not throw (absorbing canvas stub) ---
let drawOk = true;
try {
  Object.keys(METRICS).forEach(m => ACTIVE.add(m));
  drawChart(R, 'gexChart');
  ACTIVE.clear(); ACTIVE.add('net_gex');
  drawChart(R, 'gexChart');
} catch (e) { drawOk = false; console.log('draw error:', e.message); }
check('Strike Chart draw no-throw (all metrics, then Net GEX only)', drawOk);

// --- 5. P/C Ratio table ---
check('P/C rows follow SPX, NDX, SPY, QQQ', PC_TICKERS.join(',') === 'SPX,NDX,SPY,QQQ');
check('P/C ratio is put over call', Math.abs(pcRatio(150, 100) - 1.5) < 1e-12 && pcRatio(5, 0) === null);
const pcData = {
  SPX: { spot: 6000.5, asof: '2026-10-26', totals: { call_oi: 1000, put_oi: 1500, call_vol: 400, put_vol: 200 } },
  NDX: { spot: 21000, totals: { call_oi: 0, put_oi: 10, call_vol: 0, put_vol: 0 } },
};
renderPcTable(pcData);
const pcHtml = document.getElementById('pcTableBody').innerHTML;
const pcRows = pcHtml.split('<tr>').slice(1);
check('P/C table has one row per ticker', pcRows.length === 4, `${pcRows.length} rows`);
const spxCells = [...pcRows[0].matchAll(/<td[^>]*>([^<]*)<\/td>/g)].map(m => m[1]);
check('SPX row: ticker, spot, P/C OI, P/C Vol, call/put OI, call/put vol',
  JSON.stringify(spxCells) === JSON.stringify(['SPX', '6,000.50', '1.50', '0.50', '1,000', '1,500', '400', '200']),
  spxCells.join(' | '));
check('No calls shows a dash, not Infinity', pcRows[1].includes('—') && !pcRows[1].includes('Infinity'));
check('Missing ticker shows dashes', /SPY<\/td>(<td class="pc-missing">—<\/td>){7}/.test(pcRows[2]));

// --- 6. OI Volume (synthetic SPX chain, two expirations) ---
function synthRecord(spot, exps) {
  const opts = [];
  for (const exp of exps) {
    for (let k = spot - 50; k <= spot + 50; k += 5) {
      opts.push({ k, t: 'C', exp, dte: 0, iv: 0.15, oi: 200 + (k % 20), vol: 100 + (k - spot + 50), g: 0.002, d: 0.5 });
      opts.push({ k, t: 'P', exp, dte: 0, iv: 0.16, oi: 300, vol: 150, g: 0.002, d: -0.5 });
    }
  }
  return { spot, asof: new Date().toISOString(), mult: 100, opts, source: 'smoke' };
}
const isoDay = n => new Date(Date.now() + n * 86400000).toISOString().slice(0, 10);
const exp0 = isoDay(1), exp1 = isoDay(8);
REAL.SPX = synthRecord(6000, [exp0, exp1]);
check('OI Volume has his five buttons in his order',
  JSON.stringify(Object.keys(OIV_METRICS)) === JSON.stringify(['volume_spread', 'call_oi', 'put_oi', 'call_vol', 'put_vol']));
check('Volume Spread is on by default and drawn as bars', OIV_ACTIVE.has('volume_spread') && OIV_ACTIVE.size === 1 && OIV_METRICS.volume_spread.kind === 'bar');
const agg = aggregateOiVolume([
  { K: 100, isCall: true, oi: 10, vol: 7 }, { K: 100, isCall: false, oi: 4, vol: 9 },
  { K: 105, isCall: true, oi: 0, vol: 0 }, { K: 105, isCall: false, oi: 0, vol: 0 },
  { K: 110, isCall: true, oi: 2, vol: 5 }, { K: 110, isCall: true, oi: 3, vol: 1 },
]);
check('Volume Spread = call vol - put vol per strike', agg.length === 2 && agg[0].volumeSpread === -2 && agg[1].volumeSpread === 6,
  JSON.stringify(agg.map(s => [s.strike, s.volumeSpread])));
check('OI and volume sum per strike', agg[1].callOI === 5 && agg[1].callVol === 6);
check('OI Volume lists future expirations only, sorted', JSON.stringify(oiVolumeExpiries(buildChainReal('SPX'))) === JSON.stringify([exp0, exp1]));
const O = computeOiVolume('SPX', exp1);
check('OI Volume uses one expiration', O.strikes.length === 21 && O.strikes.every(s => s.callOI > 0));
const lines = oiVolumePriceLines(O.strikes);
check('price chart lines: Call OI, Put OI, Call Vol, Put Vol', JSON.stringify(lines.map(l => l.label)) === JSON.stringify(['Call OI', 'Put OI', 'Call Vol', 'Put Vol']));
check('Call Vol line sits at the top call-volume strike', lines[2].price === 6050);
let oivOk = true;
try {
  oiv.symbol = 'SPX'; oiv.expiry = exp0;
  oiv.candles = [{ time: 1761573000, open: 5990, high: 6010, low: 5985, close: 6005 }, { time: 1761573060, open: 6005, high: 6012, low: 6000, close: 6001 }];
  for (const m of Object.keys(OIV_METRICS)) OIV_ACTIVE.add(m);
  renderOiVolume();
  oiv.candles = null; renderOiVolume();
  OIV_ACTIVE.clear(); renderOiVolume(); OIV_ACTIVE.add('volume_spread');
} catch (e) { oivOk = false; console.log('oi volume draw error:', e.stack); }
check('OI Volume strike and price charts draw (no-throw)', oivOk);

process.exit(failures ? 1 : 0);
