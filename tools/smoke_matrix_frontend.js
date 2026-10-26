/* Smoke test: load matrix.js with DOM stubs (offline), then check the Strike
   Chart: AM settlement, a real SPX chain through the GEX engine, the
   one-date expiration dropdown, the latest-session price candles and the
   draw paths. Run from the repo root: node tools/smoke_matrix_frontend.js */
const fs = require('fs');
const vm = require('vm');

const absorb = new Proxy(function () {}, {
  get: (t, p) => (p === 'measureText' ? () => ({ width: 10 }) : absorb),
  apply: () => absorb,
  set: () => true,
});
function makeEl(id) {
  return {
    id, innerHTML: '', textContent: '', value: '', disabled: false,
    style: {}, dataset: {}, elements: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {}, appendChild() {}, setAttribute() {}, removeAttribute() {},
    querySelector: () => null,
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
  addEventListener() {}, scrollTo() {}, innerWidth: 1280, innerHeight: 800,
  devicePixelRatio: 1, location: { pathname: '/matrix/', search: '' },
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
check('VIX is AM-settled (shorter T than PM-settled)', tVix < tSpy);
check('VIXW matches VIX AM settlement', tVix === tVixw);

// --- 2. Real SPX chain (repo snapshot) through the engine ---
const snapshot = JSON.parse(fs.readFileSync('cboe_data.json', 'utf8'));
const chain = buildChainRealFromRecord('SPX', snapshot.SPX);
const R = calcGEX(chain);
R.expectedMove = calcExpectedMove(chain);
check('SPX chain parsed', chain.quotes.length > 1000, `${chain.quotes.length} quotes`);
check('SPX strikes aggregated', R.strikes.length > 10, `${R.strikes.length} strikes`);
check('Power Zone set per strike', R.strikes.every(s => Number.isFinite(s.powerZone)));

// --- 3. One expiration at a time, nearest by default ---
check('nearest date is the default', pickExpiration(['2026-10-27', '2026-10-28'], null) === '2026-10-27');
check('a listed date is kept', pickExpiration(['2026-10-27', '2026-10-28'], '2026-10-28') === '2026-10-28');
check('an unlisted date falls back to the nearest', pickExpiration(['2026-10-27'], '2026-07-17') === '2026-10-27');
check('no dates gives none', pickExpiration([], '2026-10-27') === null);
const futureExp = chainExpirations({ quotes: [{ exp: '2099-01-02' }, { exp: '2000-01-01' }, { exp: '2099-01-01' }, { exp: '2099-01-01' }] });
check('dropdown lists future dates once, sorted', JSON.stringify(futureExp) === '["2099-01-01","2099-01-02"]');

// --- 4. Price charts keep the latest regular session ---
const et = (day, hh, mm) => zonedDateTimeToUtc(2026, 10, day, hh, mm, 'America/New_York') / 1000;
const bar = t => ({ time: t, open: 1, high: 2, low: 0.5, close: 1.5 });
const session = latestSessionCandles([
  bar(et(23, 15, 59)), bar(et(26, 9, 0)), bar(et(26, 9, 30)), bar(et(26, 15, 59)), bar(et(26, 16, 0)),
  { time: et(26, 10, 0), open: null, high: 1, low: 1, close: 1 },
]);
check('only the last day, 09:30-16:00 ET', session.length === 2 && session[0].time === et(26, 9, 30) && session[1].time === et(26, 15, 59),
  `${session.length} candles`);
check('time label is New York time', fmtMarketTime(et(26, 9, 30)) === '09:30');

// --- 5. Strike Chart buttons ---
check('seven parameter buttons', JSON.stringify(Object.keys(METRICS)) ===
  '["net_gex","ag","call_oi","put_oi","call_vol","put_vol","power"]');

// --- 6. Draw paths do not throw (absorbing canvas stub) ---
let drawOk = true;
try {
  ['ag', 'call_oi', 'put_oi', 'call_vol', 'put_vol', 'power'].forEach(m => ACTIVE.add(m));
  drawChart(R, 'gexChart');
  PRICE.SPX = { candles: latestSessionCandles(JSON.parse(fs.readFileSync('candles_data.json', 'utf8')).SPX.candles) };
  drawPriceCharts('SPX');
  PRICE.XYZ = { error: 'No intraday prices for XYZ' };
  drawPriceCharts('XYZ');
  check('a ticker with no candles says so', byId('priceEmpty').textContent === 'No intraday prices for XYZ' && byId('priceChart')._matrixPrice === null);
  drawPriceCharts('SPX');
} catch (e) { drawOk = false; console.log('draw error:', e.message); }
check('strike and price charts draw without errors', drawOk);
check('price charts keep their candles for hover', PRICE.SPX.candles.length > 0 &&
  byId('priceChart')._matrixPrice?.candles.length === PRICE.SPX.candles.length &&
  byId('priceSimpleChart')._matrixPrice?.kind === 'line');

process.exit(failures ? 1 : 0);
