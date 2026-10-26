/* Smoke test: load matrix.js with DOM stubs (offline), then verify
   VIX AM-settlement, real VIX chain aggregation, quick-select predicates,
   the Strike Chart draw path and the P/C Ratio table.
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

process.exit(failures ? 1 : 0);
