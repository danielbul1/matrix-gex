/* Smoke test: load matrix.js with DOM stubs (offline), then verify
   VIX AM-settlement, the Strike Chart engine, quick-select predicates
   and the OI Volume screen. */
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
  devicePixelRatio: 1, location: { pathname: '/matrix/', search: '' },
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

// --- 2. Strike Chart engine on a small synthetic chain ---
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
const R = calcGEX(buildChainReal('SPX'));
check('strikes aggregated', R.strikes.length === 21, `${R.strikes.length} strikes`);
let drawOk = true;
try { R.expectedMove = calcExpectedMove(buildChainReal('SPX')); drawChart(R); } catch (e) { drawOk = false; console.log('draw error:', e.message); }
check('Strike Chart draws (no-throw)', drawOk);

// --- 3. Quick-select predicates ---
check('0DTE predicate', expiryQuickPredicate('0dte')(0) === true && expiryQuickPredicate('0dte')(1) === false);
check('Week predicate', expiryQuickPredicate('week')(7) === true && expiryQuickPredicate('week')(8) === false);
check('All predicate', expiryQuickPredicate('all')(45) === true);

// --- 4. OI Volume ---
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
