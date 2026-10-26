/* Smoke test: load matrix.js with DOM stubs (offline) and a synthetic chain,
   then check the GEX engine, the Strike Chart (one-date dropdown, price charts), the P/C Ratio table, OI Volume
   and the Key Levels screen (four levels only, the Forecast's h3/h4/h5 layout,
   the price chart). */
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
    getContext: () => absorb, clientWidth: 900, clientHeight: 600, offsetWidth: 120, offsetHeight: 80,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 900, height: 600 }),
  };
}
const els = new Map();
global.document = {
  hidden: true,
  title: '',
  getElementById: id => { if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); },
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => makeEl('created'),
};
global.window = {
  addEventListener() {}, scrollTo() {}, innerWidth: 1280, innerHeight: 800,
  devicePixelRatio: 1, location: { pathname: '/matrix/disclaimer', search: '' },
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
function noThrow(fn) {
  try { fn(); return true; } catch (e) { console.log('  error:', e.message); return false; }
}

// --- 1. AM settlement: SPX expiry 9:30 ET vs SPY 16:00 ET ---
const now = Date.UTC(2026, 6, 20, 12, 0, 0);
check('SPX is AM-settled (shorter T than SPY)',
  preciseYearsToExpiry('2026-07-22', 'SPX', now) < preciseYearsToExpiry('2026-07-22', 'SPY', now));

// --- 2. Synthetic chain through the engine ---
const spot = 7480;
const exp = new Date(Date.now() + 3 * 86400000).toISOString().slice(0, 10);
const opts = [];
for (let k = 7300; k <= 7660; k += 5) {
  for (const t of ['C', 'P']) {
    // calls heavier above spot, puts heavier below, so walls sit on either side
    const oi = Math.round(2000 * Math.exp(-Math.abs(k - (t === 'C' ? 7550 : 7400)) / 60)) + 20;
    opts.push({ k, t, root: 'SPXW', exp, dte: 3, iv: 0.15, oi, vol: Math.round(oi / 3), g: 0, d: 0 });
  }
}
REAL.SPX = { spot, asof: new Date().toISOString(), mult: 100, opts, source: 'smoke' };
const R = calcGEX(buildChainReal('SPX'));
check('strikes aggregated', R.strikes.length > 50, `${R.strikes.length} strikes`);
check('call wall above put wall', R.callWall > R.putWall, `call=${R.callWall} put=${R.putWall}`);
check('zero gamma found inside the strike range', R.flip > 7300 && R.flip < 7660, `flip=${R.flip?.toFixed(1)}`);

// --- 3. Strike Chart ---
// One expiration at a time, nearest by default
check('nearest date is the default', pickExpiration(['2026-10-27', '2026-10-28'], null) === '2026-10-27');
check('a listed date is kept', pickExpiration(['2026-10-27', '2026-10-28'], '2026-10-28') === '2026-10-28');
check('an unlisted date falls back to the nearest', pickExpiration(['2026-10-27'], '2026-07-17') === '2026-10-27');
check('no dates gives none', pickExpiration([], '2026-10-27') === null);
check('dropdown lists future dates once, sorted',
  JSON.stringify(chainExpirations({ quotes: [{ exp: '2099-01-02' }, { exp: '2000-01-01' }, { exp: '2099-01-01' }, { exp: '2099-01-01' }] }))
  === '["2099-01-01","2099-01-02"]');
check('his seven parameter buttons',
  Object.keys(METRICS).join() === 'net_gex,ag,call_oi,put_oi,call_vol,put_vol,power');
R.expectedMove = calcExpectedMove(buildChainReal('SPX'));
check('Strike Chart draws', noThrow(() => drawChart(R)));

// Price charts keep the latest regular session (09:30-16:00 ET)
const et = (day, hh, mm) => zonedDateTimeToUtc(2026, 10, day, hh, mm, 'America/New_York') / 1000;
const bar = t => ({ time: t, open: 1, high: 2, low: 0.5, close: 1.5 });
const session = latestSessionCandles([
  bar(et(23, 15, 59)), bar(et(26, 9, 0)), bar(et(26, 9, 30)), bar(et(26, 15, 59)), bar(et(26, 16, 0)),
  { time: et(26, 10, 0), open: null, high: 1, low: 1, close: 1 },
]);
check('price charts keep only the last session', session.length === 2
  && session[0].time === et(26, 9, 30) && session[1].time === et(26, 15, 59), `${session.length} candles`);
check('price chart times are New York time', fmtMarketTime(et(26, 9, 30)) === '09:30');
activeView = HOME_VIEW;
PRICE.XYZ = { error: 'No intraday prices for XYZ' };
check('a ticker with no candles says so', noThrow(() => drawPriceCharts('XYZ'))
  && byId('priceEmpty').textContent === 'No intraday prices for XYZ' && byId('priceChart')._matrixPrice === null);
PRICE.SPX = { candles: Array.from({ length: 390 }, (_, i) => ({ ...bar(et(26, 9, 30) + i * 60), close: 1 + i / 100 })) };
check('candle and close-line charts draw', noThrow(() => drawPriceCharts('SPX'))
  && byId('priceChart')._matrixPrice?.kind === 'candles' && byId('priceSimpleChart')._matrixPrice?.candles.length === 390);
check('price tooltip shows a candle', noThrow(() => showPriceTooltip({ currentTarget: byId('priceChart'), clientX: 300, clientY: 200 }))
  && byId('priceTooltip').innerHTML.includes('Close'));

// --- 4. Key Levels ---
const KR = keyLevelsGex('SPX');
const levels = keyLevels(KR);
check('exactly the four Key Levels',
  levels.map(l => l.label).sort().join() === 'Call Wall,Max Gamma,Put Wall,Zero Gamma',
  levels.map(l => `${l.label} ${fmtPrice(l.price)}`).join(', '));
check('a missing level is dropped, not drawn at 0',
  keyLevels({ ...KR, flip: null }).every(l => l.label !== 'Zero Gamma' && l.price > 0));
const below = nearestKeyLevel(KR, 'below'), above = nearestKeyLevel(KR, 'above');
check('scenario levels straddle the price', below.price < KR.spot && above.price > KR.spot,
  `below=${below.label} above=${above.label}`);

const html = forecastHtml(KR);
check('Forecast: one h3 title', (html.match(/<h3>/g) || []).length === 1 && html.includes('Forecast for SPX'));
check('Forecast: h4 sections Market Read and Scenario Ladder',
  /<h4>Market Read<\/h4>/.test(html) && /<h4>Scenario Ladder<\/h4>/.test(html));
check('Forecast: h5 sub-heads', (html.match(/<h5>/g) || []).length >= 4);
check('Forecast names no level but the four', !/max pain|P1|N1|AG Strike|Call Vol|Put Vol/i.test(html));

const candles = Array.from({ length: 120 }, (_, i) => {
  const open = spot - 20 + Math.sin(i / 9) * 15;
  return { time: 1792785540 + i * 300, open, high: open + 4, low: open - 4, close: open + (i % 2 ? 2 : -2) };
});
activeView = KEY_LEVELS_VIEW;
check('Key Levels chart draws with candles', noThrow(() => drawKeyLevelsChart({ R: KR, candles })));
check('Key Levels chart draws with no candles', noThrow(() => drawKeyLevelsChart({ R: KR, candles: [] })));
check('legend lists the four levels',
  ['Call Wall', 'Put Wall', 'Zero Gamma', 'Max Gamma'].every(l => byId('klLegend').innerHTML.includes(l)));
keyLevelsState = { symbol: 'SPX', R: KR, candles };
check('render fills the Forecast', noThrow(renderKeyLevels) && byId('forecast').innerHTML.includes('<h3>'));
check('tooltip shows a candle', noThrow(() => {
  byId('keyLevelsChart')._klPlot = { ...byId('keyLevelsChart')._klPlot, candles };
  showKeyLevelsTooltip({ clientX: 300, clientY: 200 });
}) && byId('klTooltip').innerHTML.includes('Close'));

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
  activeView = OIV_VIEW;
  oiv.symbol = 'SPX'; oiv.expiry = exp0;
  oiv.candles = [{ time: 1761573000, open: 5990, high: 6010, low: 5985, close: 6005 }, { time: 1761573060, open: 6005, high: 6012, low: 6000, close: 6001 }];
  for (const m of Object.keys(OIV_METRICS)) OIV_ACTIVE.add(m);
  renderOiVolume();
  oiv.candles = null; renderOiVolume();
  OIV_ACTIVE.clear(); renderOiVolume(); OIV_ACTIVE.add('volume_spread');
} catch (e) { oivOk = false; console.log('oi volume draw error:', e.stack); }
check('OI Volume strike and price charts draw (no-throw)', oivOk);

process.exit(failures ? 1 : 0);
