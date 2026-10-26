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
check('dropdown lists only the six nearest dates',
  JSON.stringify(chainExpirations({ quotes: ['2099-01-08', '2099-01-01', '2099-01-03', '2099-01-02', '2099-01-05', '2099-01-04', '2099-01-07', '2099-01-06'].map(exp => ({ exp })) }))
  === JSON.stringify(['2099-01-01', '2099-01-02', '2099-01-03', '2099-01-04', '2099-01-05', '2099-01-06']));
check('AG and Power Zone share the GEX axis; OI and volume get their own',
  METRICS.ag.axis === 'gex' && METRICS.power.axis === 'gex'
  && ['call_oi', 'put_oi', 'call_vol', 'put_vol'].every(m => METRICS[m].axis === 'count'));
check('Strike Chart draws with both right axes', noThrow(() => { ['call_oi', 'power'].forEach(m => ACTIVE.add(m)); drawChart(R); ['call_oi', 'power'].forEach(m => ACTIVE.delete(m)); }));
check('Net GEX and AG start on', ACTIVE.has('net_gex') && ACTIVE.has('ag') && ACTIVE.size === 2);
// Each screen keeps its own chain: a Search elsewhere replacing REAL leaves the Strike Chart alone.
homeRecord = REAL.SPX;
REAL.SPX = { ...REAL.SPX, spot: 1234 };
activeView = HOME_VIEW;
run();
check('Strike Chart reads its own copy of the chain', window._lastR.spot === spot, `spot=${window._lastR.spot}`);
REAL.SPX = homeRecord;

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
check('Forecast: one h3 title with ticker and date', (html.match(/<h3>/g) || []).length === 1 && /<h3>SPX · \w{3} \d{1,2}, \d{4}<\/h3>/.test(html));
check('Forecast: h4 sections Current position, Key levels, Target, Scenarios',
  JSON.stringify([...html.matchAll(/<h4>([^<]+)<\/h4>/g)].map(m => m[1])) === JSON.stringify(['Current position', 'Key levels', 'Target', 'Scenarios']));
check('Forecast: one h5 per scenario', (html.match(/<h5>/g) || []).length === forecastScenarios(KR).length && forecastScenarios(KR).length === 3);
check('Forecast lists the four levels and the AG Strike target only',
  ['Call Wall', 'Put Wall', 'Zero Gamma', 'Max Gamma', 'AG Strike'].every(l => html.includes(l)) && !/max pain|P1|N1|Call Vol|Put Vol|Matrix/i.test(html));
check('Forecast gives each level its distance from spot', (html.match(/\([+-]\d+\.\d{2}%\)/g) || []).length >= 5);

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
const O = computeOiVolume('SPX', exp1, REAL.SPX);
check('OI Volume uses one expiration', O.strikes.length === 21 && O.strikes.every(s => s.callOI > 0));
const lines = oiVolumePriceLines(O.strikes);
check('price chart lines: Call OI, Put OI, Call Vol, Put Vol', JSON.stringify(lines.map(l => l.label)) === JSON.stringify(['Call OI', 'Put OI', 'Call Vol', 'Put Vol']));
check('Call Vol line sits at the top call-volume strike', lines[2].price === 6050);
let oivOk = true;
try {
  activeView = OIV_VIEW;
  oiv.symbol = 'SPX'; oiv.record = REAL.SPX; oiv.expiry = exp0;
  oiv.candles = [{ time: 1761573000, open: 5990, high: 6010, low: 5985, close: 6005 }, { time: 1761573060, open: 6005, high: 6012, low: 6000, close: 6001 }];
  for (const m of Object.keys(OIV_METRICS)) OIV_ACTIVE.add(m);
  renderOiVolume();
  oiv.candles = null; renderOiVolume();
  OIV_ACTIVE.clear(); renderOiVolume(); OIV_ACTIVE.add('volume_spread');
} catch (e) { oivOk = false; console.log('oi volume draw error:', e.stack); }
check('OI Volume strike and price charts draw (no-throw)', oivOk);
oiv.record = REAL.SPX;
REAL.SPX = synthRecord(5000, [exp0]);
check('OI Volume reads its own copy of the chain', computeOiVolume('SPX', exp1).spot === 6000);
oiv.symbol = 'SPX'; oiv.expiry = '2000-01-01';
renderOiVolume();
check('a gone expiration falls back to the nearest one', oiv.expiry === exp0, oiv.expiry);
check('every ticker may have a price chart (no ticker list)', typeof CANDLE_SYMBOLS === 'undefined');
const etSec = (h, m) => Date.UTC(2026, 9, 23, h + 4, m) / 1000;  // 2026-10-23, EDT = UTC-4
const sess = latestSessionCandles([[9, 29], [9, 30], [15, 59], [16, 0]].map(([h, m]) => ({ time: etSec(h, m), open: 1, high: 2, low: 0.5, close: 1.5 })));
check('price chart keeps 09:30-16:00 ET only', sess.length === 2 && fmtMarketTime(sess[0].time) === '09:30' && fmtMarketTime(sess[1].time) === '15:59');

// --- 7. GEX Board (two expirations, per-strike rows over the selected ones) ---
REAL.SPX = synthRecord(6000, [exp0, exp1]);
gb.symbol = 'SPX'; gb.record = REAL.SPX; gb.expiries = [exp0, exp1];
gb.selected = new Set([exp0]);
const B1 = computeGexBoard('SPX');
gb.selected = new Set([exp0, exp1]);
const B2 = computeGexBoard('SPX');
check('GEX Board has the agreed columns in order',
  GB_COLUMNS.map(c => c.label).join() === 'Strike,Net GEX,AG,Notional Delta,Total OI,Call OI,Put OI,Total Vol,Call Vol,Put Vol,%');
check('GEX Board has his four ticker buttons', GB_TICKERS.join() === 'SPX,NDX,SPY,QQQ');
check('GEX Board keeps strikes within 5% of spot', B2.rows.length > 0 && B2.rows.every(s => Math.abs(s.strike - 6000) / 6000 <= 0.05));
const s1 = B1.rows.find(s => s.strike === 6000), s2 = B2.rows.find(s => s.strike === 6000);
check('rows sum over the selected expiries', Math.abs(s2.callOI - 2 * s1.callOI) < 1e-9 && Math.abs(s2.netGex - (s2.byExpiry[exp0] + s2.byExpiry[exp1])) < 1e-6,
  `${s1.callOI} -> ${s2.callOI}`);
check('Net GEX, AG and totals add up', Math.abs(s2.netGex - (s2.callGex + s2.putGex)) < 1e-6
  && Math.abs(s2.ag - (Math.abs(s2.callGex) + Math.abs(s2.putGex))) < 1e-6 && s2.totalOI === s2.callOI + s2.putOI);
check('Notional Delta counts calls and puts as positive', s2.notionalDelta > 0
  && Math.abs(s2.notionalDelta - (0.5 * s2.callOI + 0.5 * s2.putOI) * 100 * 6000) < 1e-3, `${s2.notionalDelta}`);
const totalAg = gexBoardStrikes({ ...buildChainReal('SPX'), quotes: buildChainReal('SPX').quotes.filter(q => gb.selected.has(q.exp)) }).reduce((a, s) => a + s.ag, 0);
check('% is Net GEX over the total AG', Math.abs(s2.pct - s2.netGex / totalAg * 100) < 1e-9);
check('Total Gamma is the Net GEX of the selected expiries', Math.abs(B2.totalGex - 2 * B1.totalGex) < Math.abs(B1.totalGex) * 1e-9 + 1e-6);
const lowOi = gexBoardStrikes({ spot: 100, mult: 100, quotes: [
  { K: 100, isCall: true, oi: 3, vol: 1, exp: 'x', T: 0.01, iv: 0.2, g: { gamma: 0.05, delta: 0.5 } },
  { K: 105, isCall: true, oi: 0, vol: 9, exp: 'x', T: 0.01, iv: 0.2, g: { gamma: 0.05, delta: 0.3 } },
] });
check('the board keeps any strike with open interest and drops only OI = 0', lowOi.length === 1 && lowOi[0].strike === 100);
check('price lines are the six Price Lines (or fewer)', B2.lines.every(l => ['P1', 'N1', 'Call Vol', 'Put Vol', 'Matrix', 'AG Strike'].includes(l.label)));
check('Matrix sits within 1% of spot', B2.lines.filter(l => l.label === 'Matrix').every(l => Math.abs(l.price - 6000) / 6000 <= 0.01));
gb.sort = { key: 'strike', dir: 'desc' };
check('table runs from the highest strike down', sortedBoardRows(B2.rows)[0].strike === Math.max(...B2.rows.map(s => s.strike)));
gb.sort = { key: 'netGex', dir: 'asc' };
const byNet = sortedBoardRows(B2.rows);
check('a column header sorts by that column', byNet.every((s, i) => i === 0 || byNet[i - 1].netGex <= s.netGex));
gb.sort = { key: 'strike', dir: 'desc' };
gb.selected = new Set();
const B0 = computeGexBoard('SPX');
check('Clear all leaves no rows', B0.rows.length === 0 && B0.expiries.length === 0);
let gbOk = true;
try {
  activeView = GB_VIEW;
  gb.candles = [{ time: 1761573000, open: 5990, high: 6010, low: 5985, close: 6005 }, { time: 1761573060, open: 6005, high: 6012, low: 6000, close: 6001 }];
  gb.selected = new Set([exp0, exp1]); renderGexBoard(true);
  gb.candles = null; renderGexBoard();
  gb.selected = new Set(); renderGexBoard();
} catch (e) { gbOk = false; console.log('gex board draw error:', e.stack); }
check('GEX Board table and charts draw (no-throw)', gbOk);
gb.record = REAL.SPX;
REAL.SPX = synthRecord(5000, [exp0]);
gb.selected = new Set([exp0]);
check('GEX Board reads its own copy of the chain', computeGexBoard('SPX').spot === 6000);

// --- 9. His Zero Gamma, price label and colours (round 2) ---
{
  const zg = calcGEX({ symbol: 'SPX', spot: 6000, mult: 100, quotes: [
    { K: 5900, isCall: false, oi: 500, vol: 0, exp: 'x', g: { gamma: 0.002 } },   // far crossing below
    { K: 5950, isCall: true, oi: 500, vol: 0, exp: 'x', g: { gamma: 0.002 } },
    { K: 5990, isCall: false, oi: 300, vol: 0, exp: 'x', g: { gamma: 0.002 } },
    { K: 6010, isCall: true, oi: 100, vol: 0, exp: 'x', g: { gamma: 0.002 } },    // nearest crossing: 5990 -> 6010
  ] });
  check('Zero Gamma is the sign change nearest spot', zg.flip > 5990 && zg.flip < 6010, `${zg.flip}`);
  check('the Strike Chart price label has two decimals', /"Price: "\+R\.spot\.toFixed\(2\)/.test(fs.readFileSync('railway-service/src/tripity_experiment/web/matrix.js', 'utf8')));
  check('his button colours', [METRICS.net_gex, METRICS.ag, METRICS.call_oi, METRICS.put_oi, METRICS.call_vol, METRICS.put_vol, METRICS.power].map(m => m.color).join()
    === '#22b5ff,#915bf8,#02d432,#f32d35,#003cfe,#e55f04,#e8ca01');
}
// --- 8. Power Zone and the Price Lines (#39) ---
{
  const pzChain = { symbol: 'SPX', spot: 6000, mult: 100, quotes: [
    { K: 6000, isCall: true, oi: 100, vol: 50, exp: 'x', g: { gamma: 0.002 } },
    { K: 6000, isCall: false, oi: 80, vol: 30, exp: 'x', g: { gamma: 0.002 } },
  ] };
  const PZ = calcGEX(pzChain).strikes[0];
  const ag = (100 + 80) * 0.002 * 100 * 6000 * 6000 * 0.01;
  check('Power Zone = AG/8 + volume × S²/20000', Math.abs(PZ.ag - ag) < 1e-6 && Math.abs(PZ.powerZone - (ag / 8 + 80 * 6000 * 6000 / 20000)) < 1e-6,
    `${PZ.powerZone}`);
  check('Power Zone draws only within 1% of spot', POWER_ZONE_WINDOW === 0.01 && METRICS.power.window === 0.01
    && layerPoints(METRICS.power, [{ strike: 5900 }, { strike: 5990 }, { strike: 6060 }, { strike: 6070 }], 6000).map(p => p.s.strike).join() === '5990,6060');
  const strikes = [
    { strike: 5950, netGex: -9, callVol: 1, putVol: 40, ag: 30, powerZone: 50 },
    { strike: 5995, netGex: 3, callVol: 5, putVol: 2, ag: 10, powerZone: 90 },
    { strike: 6070, netGex: 12, callVol: 60, putVol: 1, ag: 80, powerZone: 200 },
  ];
  const PL = priceLines(strikes, 6000);
  check('six Price Lines in order', PL.map(l => l.label).join() === 'P1,N1,Call Vol,Put Vol,Matrix,AG Strike');
  check('Price Lines pick the right strikes', PL.map(l => l.price).join() === '6070,5950,6070,5950,5995,6070', PL.map(l => l.price).join());
  check('Matrix ignores strikes beyond 1% of spot', PL.find(l => l.label === 'Matrix').price === 5995);
  const zeroVol = calcGEX({ symbol: 'SPX', spot: 6000, mult: 100, quotes: [
    { K: 5990, isCall: false, oi: 400, vol: 0, exp: 'x', g: { gamma: 0.002 } },
    { K: 6000, isCall: true, oi: 900, vol: 0, exp: 'x', g: { gamma: 0.002 } },
    { K: 6010, isCall: true, oi: 300, vol: 0, exp: 'x', g: { gamma: 0.002 } },
  ] }).strikes;
  const pzPeak = zeroVol.reduce((b, s) => s.powerZone > b.powerZone ? s : b), agPeak = zeroVol.reduce((b, s) => s.ag > b.ag ? s : b);
  check('with no volume the Power Zone peak is the AG Strike', pzPeak.strike === agPeak.strike && pzPeak.strike === 6000);
  check('his chart labels and two decimals', PL.map(l => l.chartLabel).join() === 'P1 Strike,N1 Strike,Call Vol Strike,Put Vol Strike,Matrix,AG Strike'
    && fmtLineValue(6800) === '6800.00');
  check('the Strike Chart shows only strikes within 1% of spot',
    visibleStrikeData({ spot: 6000, strikes: [5900, 5945, 6000, 6060, 6100].map(strike => ({ strike })) }, POWER_ZONE_WINDOW).map(s => s.strike).join() === '5945,6000,6060');
  const prof = powerZoneProfile([{ strike: 6000, powerZone: 10 }, { strike: 6005, powerZone: 20 }]);
  check('Power Zone profile is sampled every $2.50', prof.map(p => p.price).join() === '6000,6002.5,6005' && prof[1].value === 15);
  REAL.SPX = synthRecord(6000, [exp0, exp1]);
  homeRecord = REAL.SPX; screenTickers[HOME_VIEW] = 'SPX'; activeView = HOME_VIEW;
  const HL = homePriceLevels();
  check('home price lines come from the nearest expiry', HL && HL.expiry === exp0 && HL.lines.length >= 4, HL && HL.expiry);
  PRICE.SPX = { candles: Array.from({ length: 60 }, (_, i) => ({ time: 1761573000 + i * 60, open: 5995 + i / 10, high: 5998 + i / 10, low: 5993 + i / 10, close: 5996 + i / 10 })) };
  check('home price chart draws the lines and profile (no-throw)', noThrow(() => drawPriceCharts('SPX')) && byId('priceLegend').innerHTML.includes('Matrix'));
}

process.exit(failures ? 1 : 0);
