/* ============================================================
   GEX Analytics Engine - JavaScript port of the documented backend
   Sources: OPTIONS_MATHEMATICS.md, TECHNICAL_ARCHITECTURE.md,
           GREEKS_IMPLEMENTATION_RESEARCH.md, GEX_INTRADAY_FIX.md
   ============================================================ */

// ---------- Normal distribution ----------
function normPDF(x){ return Math.exp(-0.5*x*x)/Math.sqrt(2*Math.PI); }
function normCDF(x){                       // Abramowitz & Stegun 7.1.26
  const t = 1/(1+0.2316419*Math.abs(x));
  const d = 0.3989423*Math.exp(-x*x/2);
  let p = d*t*(0.3193815+t*(-0.3565638+t*(1.781478+t*(-1.821256+t*1.330274))));
  return x>0 ? 1-p : p;
}

// ---------- Black-Scholes Greeks (OPTIONS_MATHEMATICS.md section 3) ----------
function calcGreeks(S,K,T,sigma,r,isCall){
  T = Math.max(T, 1/(365.25*24*60));       // min 1 minute; preserves precise 0DTE decay
  sigma = Math.max(sigma, 0.01);
  const sqrtT = Math.sqrt(T);
  const d1 = (Math.log(S/K)+(r+0.5*sigma*sigma)*T)/(sigma*sqrtT);
  const d2 = d1 - sigma*sqrtT;
  const nd1 = normPDF(d1);
  const delta = isCall ? normCDF(d1) : normCDF(d1)-1;
  const gamma = nd1/(S*sigma*sqrtT);       // same for calls/puts
  return {delta, gamma, iv:sigma};
}
function parseSourceTimestamp(value){
  if(!value) return Date.now();
  const text=String(value);
  const hasZone=/Z$|[+-]\d{2}:?\d{2}$/.test(text);
  const parsed=Date.parse(hasZone?text:text+'Z');
  return Number.isFinite(parsed)?parsed:Date.now();
}
function zonedDateTimeToUtc(year,month,day,hour,minute,timeZone){
  const guess=Date.UTC(year,month-1,day,hour,minute,0);
  const parts=new Intl.DateTimeFormat('en-US',{
    timeZone,year:'numeric',month:'2-digit',day:'2-digit',
    hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'
  }).formatToParts(new Date(guess));
  const get=type=>Number(parts.find(part=>part.type===type)?.value||0);
  const represented=Date.UTC(get('year'),get('month')-1,get('day'),get('hour'),get('minute'),get('second'));
  return guess-(represented-guess);
}
// NYSE early close (13:00 ET), the same rule as matrix_gex.is_early_close (#71):
// the day after Thanksgiving, and July 3 and December 24 from Monday to Thursday.
function isEarlyClose(year,month,day){
  const weekday=new Date(Date.UTC(year,month-1,day)).getUTCDay(); // 0 = Sunday
  if(month===11 && weekday===5) return day-1>=22 && day-1<=28;
  if((month===7 && day===3) || (month===12 && day===24)) return weekday>=1 && weekday<=4;
  return false;
}
function preciseYearsToExpiry(exp,root,valuationMs){
  const [year,month,day]=String(exp).split('-').map(Number);
  // AM-settled index roots (9:30 AM ET expiry): SPX/NDX, plus VIX and its weeklies.
  const isAmSettled=root==='SPX'||root==='NDX'||root==='VIX'||root==='VIXW';
  const [hour,minute]=isAmSettled ? [9,30] : isEarlyClose(year,month,day) ? [13,0] : [16,0];
  const expiryMs=zonedDateTimeToUtc(year,month,day,hour,minute,'America/New_York');
  return Math.max((expiryMs-valuationMs)/(365.25*86400000),1/(365.25*24*60));
}

// Real market data is served directly by Tripity.
let REAL = {};
const RISK_FREE = 0.05;
const SPX_SPY_RATIO = 10.03657299922611;
// User-provided conversion example: QQQ 752.00 = NDX 30,916.24.
const NDX_QQQ_RATIO = 30916.24 / 752;
const DEFAULT_SYMBOL = 'SPX';

// ---------- Real chain from Tripity ----------
// Uses official daily OI plus the freshest available gamma/delta/IV/volume fields.
function buildChainReal(symKey){
  return buildChainRealFromRecord(symKey,REAL[symKey]);
}
function buildChainRealFromRecord(symKey, rec){
  const spot = rec.spot;
  const r = RISK_FREE;
  const now = parseSourceTimestamp(rec.price_asof || rec.asof);
  const quotes = rec.opts.map(o=>{
    const isCall = o.t==="C";
    const T = preciseYearsToExpiry(o.exp,o.root||'',now);
    // Gamma and delta come from Black-Scholes on the row's IV at the spot shown (#68).
    // Cboe's own greeks are computed at another underlying price, which flipped SPX's
    // Naive GEX sign; they are used only for a row with no IV.
    // The expiry's implied carry b = r - q from put-call parity (#72), else RISK_FREE.
    const rate = Number.isFinite(o.b) ? o.b : r;
    const modelGreeks = calcGreeks(spot, o.k, T, o.iv>0?o.iv:0.2, rate, isCall);
    let g = modelGreeks;
    if(!(o.iv>0) && o.g>0){
      g = {...modelGreeks, gamma:o.g, delta:Number.isFinite(o.d) && o.d!==0 ? o.d : modelGreeks.delta};
    }
    return {K:o.k, dte:o.dte, T, exp:o.exp, root:o.root||'', isCall, iv:o.iv, oi:o.oi, vol:o.vol, g, rate};
  });
  return {symbol:symKey, spot, mult:rec.mult||100, quotes};
}

function calcExpectedMove(chain){
  const byExpiry = new Map();
  chain.quotes.forEach(q=>{
    const key=q.exp || String(q.dte);
    if(!byExpiry.has(key)) byExpiry.set(key,[]);
    byExpiry.get(key).push(q);
  });
  const hourInYears=1/(365*24);
  let widest=0;
  byExpiry.forEach(quotes=>{
    const nearest=Math.min(...quotes.map(q=>Math.abs(q.K-chain.spot)));
    const atm=quotes.filter(q=>Math.abs(q.K-chain.spot)===nearest && Number.isFinite(q.iv) && q.iv>0);
    if(!atm.length) return;
    const iv=atm.reduce((sum,q)=>sum+q.iv,0)/atm.length;
    const exp=quotes[0].exp;
    const expiryMs=exp ? Date.parse(exp+'T20:00:00Z')-Date.now() : NaN;
    const fallbackYears=Math.max(Number(quotes[0].T)||Number(quotes[0].dte)/365||0,hourInYears);
    const years=Number.isFinite(expiryMs) ? Math.max(expiryMs/(365*86400000),hourInYears) : fallbackYears;
    widest=Math.max(widest,chain.spot*iv*Math.sqrt(years));
  });
  return widest;
}

// ---------- GEX engine (TECHNICAL_ARCHITECTURE.md + GEX_INTRADAY_FIX.md) ----------
function calcGEX(chain){
  const {spot, mult} = chain;

  // 1. filter: the chosen expirations already scope the chain; drop illiquid strikes
  const kept = chain.quotes.filter(q=>q.oi >= 10);

  // 2. aggregate by strike (GEX = Gamma x OI x mult x Spot^2 x 1%, calls +, puts -)
  //    x0.01 = "per 1% move" - the standard GEX convention (SqueezeMetrics),
  //    keeps totals in the documented $-billions range.
  const byStrike = {};
  for(const q of kept){
    const gex = q.g.gamma * q.oi * mult * spot * spot * 0.01;
    if(!byStrike[q.K]) byStrike[q.K] = {strike:q.K, callGex:0, putGex:0, netGex:0,
        callOI:0, putOI:0, callVol:0, putVol:0};
    const s = byStrike[q.K];
    if(q.isCall){
      s.callGex += gex; s.callOI+=q.oi; s.callVol+=q.vol;
    } else {
      s.putGex -= gex; s.putOI +=q.oi; s.putVol +=q.vol;
    }
    s.netGex = s.callGex + s.putGex;
  }
  let strikes = Object.values(byStrike).sort((a,b)=>a.strike-b.strike);
  // Power Zone (#24, #39): AG/8 plus today's volume at the strike times S²/20000,
  // the fit a follower reports holding on every strike of his app (research Addendum 2).
  strikes.forEach(s=>{
    s.totalOI=s.callOI+s.putOI; s.totalVol=s.callVol+s.putVol;
    s.ag=Math.abs(s.callGex)+Math.abs(s.putGex);
    s.powerZone=s.ag/8 + s.totalVol*spot*spot/20000;
  });

  // 3. totals
  const totalCallGex = strikes.reduce((a,s)=>a+s.callGex,0);
  const totalPutGex  = strikes.reduce((a,s)=>a+s.putGex,0);
  const totalGex     = totalCallGex + totalPutGex;
  const netCallOI = strikes.reduce((a,s)=>a+s.callOI,0);
  const netPutOI  = strikes.reduce((a,s)=>a+s.putOI,0);
  const pcr = netCallOI>0 ? netPutOI/netCallOI : 0;

  // 4. max gamma strike
  let maxGammaStrike=spot, maxAbs=0;
  for(const s of strikes){
    if(Math.abs(s.netGex)>maxAbs){
      maxAbs=Math.abs(s.netGex); maxGammaStrike=s.strike;
    }
  }

  // 5. Zero Gamma: where Net GEX changes sign between two neighbouring strikes, the
  //    crossing nearest spot (interpolated). His charts mark it there: the boundary
  //    where the bars turn from red to blue next to the price line ("Bear territory").
  //    Strikes with exactly zero Net GEX sit on the line between their neighbours.
  let flip=null, prev=null;
  for(const b of strikes){
    if(!b.netGex) continue;
    if(prev && prev.netGex*b.netGex<0){
      const x = prev.strike + Math.abs(prev.netGex)/(Math.abs(prev.netGex)+Math.abs(b.netGex))*(b.strike-prev.strike);
      if(flip===null || Math.abs(x-spot)<Math.abs(flip-spot)) flip=x;
    }
    prev=b;
  }

  // 6. call wall (max +netGex), put wall (most -netGex)
  let callWall=null,cwG=-Infinity, putWall=null,pwG=Infinity;
  for(const s of strikes){
    if(s.netGex>cwG){cwG=s.netGex; callWall=s.strike;}
    if(s.netGex<pwG){pwG=s.netGex; putWall=s.strike;}
  }

  // 7. regime detection
  const distPct = maxGammaStrike>0 ? (spot-maxGammaStrike)/spot*100 : 0;
  const strongMag = Math.abs(totalCallGex)+Math.abs(totalPutGex);
  const regime = totalGex > strongMag*0.04 ? "positive_gamma"
    : totalGex < -strongMag*0.04 ? "negative_gamma" : "neutral";

  return {symbol:chain.symbol, spot, mult, strikes, totalGex, totalCallGex, totalPutGex,
    maxGammaStrike, flip, callWall, putWall, regime, distPct, pcr};
}

// ---------- Formatting ----------
function fmtNum(n){
  const a=Math.abs(n);
  if(a>=1e9) return (n/1e9).toFixed(2)+"B";
  if(a>=1e6) return (n/1e6).toFixed(1)+"M";
  if(a>=1e3) return (n/1e3).toFixed(1)+"K";
  return n.toFixed(0);
}
function fmtPrice(n){
  if(n >= 100) return n.toLocaleString(undefined,{maximumFractionDigits:0});
  if(n >= 10) return n.toLocaleString(undefined,{maximumFractionDigits:1});
  return n.toFixed(2);
}
function fmtSpyConvertedPrice(n){
  return Number(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
}
function spyPriceFromSpxStrike(strike,R){
  if(R?.symbol !== 'SPX') return null;
  if(!Number.isFinite(strike) || !Number.isFinite(SPX_SPY_RATIO) || SPX_SPY_RATIO <= 0) return null;
  return strike / SPX_SPY_RATIO;
}
function spxPriceFromSpyPrice(price,R){
  if(R?.symbol !== 'SPY') return null;
  if(!Number.isFinite(price) || !Number.isFinite(SPX_SPY_RATIO) || SPX_SPY_RATIO <= 0) return null;
  return price * SPX_SPY_RATIO;
}
function qqqPriceFromNdxStrike(strike,R){
  if(R?.symbol !== 'NDX') return null;
  if(!Number.isFinite(strike) || !Number.isFinite(NDX_QQQ_RATIO) || NDX_QQQ_RATIO <= 0) return null;
  return strike / NDX_QQQ_RATIO;
}
function ndxPriceFromQqqPrice(price,R){
  if(R?.symbol !== 'QQQ') return null;
  if(!Number.isFinite(price) || !Number.isFinite(NDX_QQQ_RATIO) || NDX_QQQ_RATIO <= 0) return null;
  return price * NDX_QQQ_RATIO;
}
function linkedMarketPrice(value,R){
  const price=Number(value);
  if(!Number.isFinite(price)) return null;
  if(R?.symbol === 'SPX') return {symbol:'SPY',price:spyPriceFromSpxStrike(price,R),decimals:2};
  if(R?.symbol === 'SPY') return {symbol:'SPX',price:spxPriceFromSpyPrice(price,R),decimals:0};
  if(R?.symbol === 'NDX') return {symbol:'QQQ',price:qqqPriceFromNdxStrike(price,R),decimals:2};
  if(R?.symbol === 'QQQ') return {symbol:'NDX',price:ndxPriceFromQqqPrice(price,R),decimals:0};
  return null;
}
function fmtLinkedMarketPrice(linked){
  if(!linked || !Number.isFinite(linked.price)) return '--';
  return linked.decimals === 2 ? fmtSpyConvertedPrice(linked.price) : fmtPrice(linked.price);
}
function esc(v){
  return String(v).replace(/[&<>"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[ch]));
}
function byId(id){ return document.getElementById(id); }
function bind(id,event,handler,opts){
  const el=byId(id);
  if(el) el.addEventListener(event,handler,opts);
}

function hideAllHoverHelpers(){
  hideChartTooltip();
  hidePriceTooltips();
  hideKeyLevelsTooltip();
  hideOiVolumeTooltip();
  hideOiVolumePriceTooltip();
  hideGexBoardTooltips();
}
// Single view -> renderer dispatch, shared by renderImpl, setView and the resize handler.
const VIEW_RENDERERS = {
  'gex': R=>R && drawChart(R),
  'oi-volume': ()=>renderOiVolume(),
  'key-levels': ()=>renderKeyLevels(),
  'gex-analysis': ()=>renderGexBoard(),
};
function renderImpl(R){
  window._lastR=R;
  const viewRenderer=VIEW_RENDERERS[activeView];
  if(viewRenderer) viewRenderer(R);
}

// ---------- Metric definitions (Quant-Power style param buttons) ----------
// net_gex = bars on the left axis. The others are area layers on the right: AG and
// Power Zone share one axis in GEX units (his AG sits on its own right axis, and his
// Power Zone is in AG's units); OI and volume counts get their own scale (a guess).
// Net GEX, AG and Power Zone start on (owner, 2026-10-27: Power Zone everywhere).
let ACTIVE = new Set(["net_gex","ag","power"]);
// Strikes shown: spot +/- 2 expected moves.
const DISPLAY_SIGMA = 2;
// Power Zone is drawn on, and the Matrix level is found among, only strikes within
// 1% of spot (#24, #39).
const POWER_ZONE_WINDOW = 0.01;
// Strike Window: the strikes the Strike Chart draws around spot. His 2026 screenshots
// show about 1% for SPX and about 3% for SPY; other tickers follow the nearest of the two.
const INDEX_SYMBOLS = new Set(['SPX','NDX']);
function strikeWindow(symbol){
  return INDEX_SYMBOLS.has(symbol) ? 0.01 : 0.03;
}
// The strikes an area layer is drawn on: all of them, or only those near spot.
function layerPoints(M,data,spot){
  return data.map((s,i)=>({s,i})).filter(({s})=>!M.window || Math.abs(s.strike-spot)/spot<=M.window);
}
const METRICS = {
  net_gex:  {label:"Net GEX",     color:"#22b5ff", kind:"bar",  signed:true,  val:s=>s.netGex},
  ag:       {label:"AG",          color:"#915bf8", kind:"area", axis:"gex", fill:.45, val:s=>Math.abs(s.callGex)+Math.abs(s.putGex)},
  call_oi:  {label:"Call OI",     color:"#02d432", kind:"area", axis:"count", val:s=>s.callOI},
  put_oi:   {label:"Put OI",      color:"#f32d35", kind:"area", axis:"count", val:s=>s.putOI},
  call_vol: {label:"Call Volume", color:"#003cfe", kind:"area", axis:"count", val:s=>s.callVol},
  put_vol:  {label:"Put Volume",  color:"#e55f04", kind:"area", axis:"count", val:s=>s.putVol},
  // Power Zone has its own right-hand scale, as on his chart (0-120M beside Net GEX);
  // on AG's scale it would sit at an eighth of AG and vanish.
  power:    {label:"Power Zone",  color:"#e8ca01", kind:"area", axis:"pz", fill:.5, val:s=>s.powerZone || 0, window:POWER_ZONE_WINDOW},
};
// Active metrics in button order; the Net GEX bars draw only while its button is on.
function chartActiveMetrics(){
  return {active:Object.keys(METRICS).filter(m=>ACTIVE.has(m)), hasBar:ACTIVE.has('net_gex')};
}
function hexA(hex,a){const n=parseInt(hex.slice(1),16);return `rgba(${n>>16&255},${n>>8&255},${n&255},${a})`;}

// ---------- Canvas chart: vertical bars by strike + area overlays ----------
function chartTargets(chartId){
  const map={
    gexChart:      {symbolId:'symLabel',        legendId:'chartLegend',      tooltipId:'chartTooltip',     crosshairId:'chartCrosshairX'},
  };
  return {canvasId:chartId, ...(map[chartId] || map.gexChart)};
}
// window: a fixed share of spot (the Strike Window); otherwise spot ± 2 expected moves.
function visibleStrikeData(R,window=null){
  let visibleStrikes=[...R.strikes];
  if(window){
    visibleStrikes=visibleStrikes.filter(s=>Math.abs(s.strike-R.spot)/R.spot<=window);
  } else if(R.expectedMove>0){
    const radius=DISPLAY_SIGMA*R.expectedMove;
    visibleStrikes=visibleStrikes.filter(s=>Math.abs(s.strike-R.spot)<=radius);
  }
  if(!visibleStrikes.length){
    visibleStrikes=[...R.strikes].sort((a,b)=>Math.abs(a.strike-R.spot)-Math.abs(b.strike-R.spot)).slice(0,1);
  }
  const data=visibleStrikes.sort((a,b)=>a.strike-b.strike);
  return data;
}
function drawChart(R,chartId='gexChart'){
  const targets=chartTargets(chartId);
  const cv = document.getElementById(targets.canvasId);
  if(!cv) return;
  const dpr = window.devicePixelRatio||1;
  const W = cv.clientWidth;
  const mobile = W < 640;
  const H = mobile ? Math.max(420, Math.min(560, window.innerHeight - 240)) : Math.max(620, Math.min(820, window.innerHeight - 190));
  cv.width=W*dpr; cv.height=H*dpr; cv.style.height=H+"px";
  const ctx = cv.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,W,H);

  // Like his strike chart: only the strikes of the Strike Window.
  const data=visibleStrikeData(R,strikeWindow(R.symbol));
  if(!data.length){
    const legend=byId(targets.legendId);
    if(legend) legend.textContent='';
    ctx.fillStyle="#a0a0a0"; ctx.font="bold 15px Segoe UI"; ctx.textAlign="center"; ctx.textBaseline="middle";
    ctx.fillText(selectedExpirations?.size ? "No strikes near spot" : "Select at least one expiration", W/2, H/2);
    return;
  }

  const metricsState=chartActiveMetrics();
  const active=metricsState.active;
  const hasBar=metricsState.hasBar;
  const barMetric=METRICS.net_gex;
  const areas = active.filter(m=>METRICS[m].kind==="area");

  // header symbol + legend
  const symLabel=byId(targets.symbolId);
  const legend=byId(targets.legendId);
  if(symLabel) symLabel.textContent = R.symbol;
  if(legend) legend.innerHTML = active.map(m=>{
    const M=METRICS[m];
    const sw = M.kind==="bar"
      ? `<span class="sq" style="background:${M.color}"></span>`
      : `<span class="sq" style="background:${hexA(M.color,.5)};border-top:2px solid ${M.color}"></span>`;
    return sw+M.label;
  }).join('&nbsp;&nbsp;') || '<span style="color:#7d8799">Choose a parameter</span>';

  const padL=mobile ? 48 : 70;
  // One right-hand scale per active group, side by side.
  const rightAxes=['gex','pz','count'].filter(ax=>areas.some(m=>METRICS[m].axis===ax));
  const axisW=mobile ? 42 : 62;
  const padR=(rightAxes.length ? rightAxes.length*axisW : (mobile ? 18 : 42));
  const padT=mobile ? 28 : 36;
  const padB=mobile ? 72 : 88;
  const plotW = W-padL-padR, plotH = H-padT-padB;
  const slot = plotW/data.length;
  const xCenter = i => padL + slot*(i+0.5);
  const top = padT, bottom = padT+plotH;

  // ----- VERTICAL axis (Net GEX, signed and centered on zero) -----
  let lMin=0,lMax=1,yL=v=>bottom,y0=bottom;
  if(hasBar){
    const vals=data.map(barMetric.val);
    const maxAbs=Math.max(...vals.map(v=>Math.abs(v)),1)*1.08;
    lMin=-maxAbs; lMax=maxAbs;
    yL=v=>top+(lMax-v)/(lMax-lMin)*plotH; y0=yL(0);
  }
  // ----- RIGHT axes, in real units; each title lists its active layers -----
  const axisMax={}, axisTitle={};
  rightAxes.forEach(ax=>{
    const layers=areas.filter(m=>METRICS[m].axis===ax);
    axisMax[ax]=Math.max(...layers.flatMap(m=>layerPoints(METRICS[m],data,R.spot).map(p=>METRICS[m].val(p.s))),1)*1.08;
    axisTitle[ax]=layers.map(m=>METRICS[m].label.toUpperCase()).join(' - ');
  });

  // ----- tick labels; no gridlines, like his -----
  ctx.font=(mobile ? "bold 10px Segoe UI" : "bold 13px Segoe UI"); ctx.textBaseline="middle";
  const ticks=6;
  for(let t=0;t<=ticks;t++){
    const f=t/ticks, yy=bottom-f*plotH;
    if(hasBar){ ctx.fillStyle="#c4c4c4"; ctx.textAlign="right";
      ctx.fillText(fmtAxis(lMin+(lMax-lMin)*f), padL-8, yy); }
    rightAxes.forEach((ax,i)=>{ ctx.fillStyle="#c4c4c4"; ctx.textAlign="left";
      ctx.fillText(fmtAxis(axisMax[ax]*f), W-padR+6+i*axisW, yy); });
  }

  // ----- area overlays: a smoothed spline with markers, filled to zero, like his -----
  areas.forEach(m=>{
    const M=METRICS[m];
    const yA=v=>bottom-(v/axisMax[M.axis])*plotH;
    const pts=layerPoints(M,data,R.spot).map(({s,i})=>[xCenter(i),yA(M.val(s))]);
    if(!pts.length) return;
    ctx.beginPath(); ctx.moveTo(pts[0][0],bottom); ctx.lineTo(pts[0][0],pts[0][1]);
    splineTo(ctx,pts,top,bottom);
    ctx.lineTo(pts[pts.length-1][0],bottom); ctx.closePath();
    ctx.fillStyle=hexA(M.color,M.fill||.33); ctx.fill();
    ctx.beginPath(); ctx.moveTo(pts[0][0],pts[0][1]);
    splineTo(ctx,pts,top,bottom);
    ctx.strokeStyle=M.color; ctx.lineWidth=1.6; ctx.stroke();
    ctx.fillStyle=M.color;
    pts.forEach(([x,y])=>{ctx.beginPath();ctx.arc(x,y,1.8,0,7);ctx.fill();});
  });

  // ----- Net GEX vertical bars -----
  const hitBars = data.map((s,i)=>({x:padL+slot*i,y:top,w:slot,h:plotH,s}));
  if(hasBar){
    const bw=Math.max(2,Math.min(slot*0.70,32));
    data.forEach((s,i)=>{
      const v=barMetric.val(s), yy=yL(v);
      const x=xCenter(i)-bw/2;
      const y=Math.min(yy,y0);
      const h=Math.max(1,Math.abs(yy-y0));
      ctx.fillStyle = v>=0 ? "#22b5ff" : "#ff0000";
      ctx.fillRect(x, y, bw, h);
      hitBars[i]={x,y,w:bw,h,s};
    });
    ctx.strokeStyle="rgba(255,255,255,.18)"; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(padL,y0); ctx.lineTo(W-padR,y0); ctx.stroke();
  }
  cv._matrixBars = hitBars;

  // ----- one rotated strike label below every bar -----
  ctx.fillStyle="#c4c4c4"; ctx.font=(mobile ? "bold 9px Segoe UI" : "bold 11px Segoe UI");
  data.forEach((s,i)=>{
    ctx.save();
    ctx.translate(xCenter(i),bottom+7);
    ctx.rotate(-Math.PI/2);
    ctx.textAlign="right"; ctx.textBaseline="middle";
    ctx.fillText(fmtPrice(s.strike),0,0);
    ctx.restore();
  });

  // ----- vertical price line + label -----
  const xAt=price=>{
    if(price<=data[0].strike) return padL;
    if(price>=data[data.length-1].strike) return padL+plotW;
    for(let i=0;i<data.length-1;i++){
      if(price>=data[i].strike && price<=data[i+1].strike){
        const frac=(price-data[i].strike)/(data[i+1].strike-data[i].strike);
        return xCenter(i)+frac*slot;
      }
    }
    return padL+plotW/2;
  };
  const px=xAt(R.spot);
  ctx.strokeStyle="#d58b16"; ctx.lineWidth=2;
  ctx.beginPath(); ctx.moveTo(px,padT-6); ctx.lineTo(px,bottom); ctx.stroke();
  ctx.fillStyle="#d58b16"; ctx.font=(mobile ? "bold 10px Segoe UI" : "bold 12px Segoe UI"); ctx.textBaseline="alphabetic";
  // His label is just "Price: <spot>", with two decimals ("Price: 6408.40").
  const priceLabel = "Price: "+R.spot.toFixed(2);
  const labelWidth = ctx.measureText(priceLabel).width;
  const alignRight = px>W-labelWidth-12;
  ctx.textAlign = alignRight?"right":"left";
  ctx.fillText(priceLabel, px+(alignRight?-6:6), padT-9);

  // ----- axis titles -----
  if(hasBar){ ctx.save(); ctx.translate(14,padT+plotH/2); ctx.rotate(-Math.PI/2);
    ctx.fillStyle="#d0d0d0"; ctx.font=(mobile ? "bold 11px Segoe UI" : "bold 13px Segoe UI"); ctx.textAlign="center";
    ctx.fillText(barMetric.axisLabel || barMetric.label,0,0); ctx.restore(); }
  rightAxes.forEach((ax,i)=>{ ctx.save(); ctx.translate(W-padR+(i+1)*axisW-8,padT+plotH/2); ctx.rotate(Math.PI/2);
    ctx.fillStyle="#d0d0d0"; ctx.font=(mobile ? "bold 11px Segoe UI" : "bold 13px Segoe UI"); ctx.textAlign="center";
    ctx.fillText(axisTitle[ax],0,0); ctx.restore(); });
  ctx.fillStyle="#d0d0d0"; ctx.font=(mobile ? "bold 10px Segoe UI" : "bold 12px Segoe UI"); ctx.textAlign="center"; ctx.textBaseline="bottom";
  ctx.fillText("STRIKE",padL+plotW/2,H-4);
}
// Draws a smoothed curve through pts (the pen already sits on pts[0]).
// Catmull-Rom segments as Bezier curves, kept inside the plot so the fill never dips below zero.
const SPLINE_SMOOTHING = 0.7;
function splineTo(ctx,pts,top,bottom,smoothing=SPLINE_SMOOTHING){
  const k=smoothing/6;
  const clampY=y=>Math.max(top,Math.min(bottom,y));
  for(let i=0;i<pts.length-1;i++){
    const p0=pts[Math.max(0,i-1)], p1=pts[i], p2=pts[i+1], p3=pts[Math.min(pts.length-1,i+2)];
    ctx.bezierCurveTo(
      p1[0]+(p2[0]-p0[0])*k, clampY(p1[1]+(p2[1]-p0[1])*k),
      p2[0]-(p3[0]-p1[0])*k, clampY(p2[1]-(p3[1]-p1[1])*k),
      p2[0], p2[1]);
  }
}
function fmtAxis(v){
  const a=Math.abs(v);
  if(a>=1e9) return (v/1e9).toFixed(1)+"B";
  if(a>=1e6) return (v/1e6).toFixed(1)+"M";
  if(a>=1e3) return (v/1e3).toFixed(0)+"k";
  return v.toFixed(0);
}

function showChartTooltip(ev){
  const cv=ev.currentTarget || document.getElementById('gexChart');
  const targets=chartTargets(cv?.id || 'gexChart');
  const tt=document.getElementById(targets.tooltipId);
  const cross=document.getElementById(targets.crosshairId);
  if(!cv || !tt || !cross) return;
  const bars=cv._matrixBars||[];
  if(!bars.length) return;

  const rect=cv.getBoundingClientRect();
  const point=ev.touches && ev.touches[0] ? ev.touches[0] : ev;
  const x=point.clientX-rect.left;
  const y=point.clientY-rect.top;
  let hit=bars.find(b=>x>=b.x-3 && x<=b.x+b.w+3 && y>=Math.min(b.y,b.y+b.h)-8 && y<=Math.max(b.y+b.h,b.y)+8);
  if(!hit){
    hit=bars.reduce((best,b)=>{
      const cx=b.x+b.w/2;
      const dist=Math.abs(cx-x);
      return !best || dist<best.dist ? {bar:b,dist} : best;
    },null)?.bar;
    if(!hit || Math.abs((hit.x+hit.w/2)-x)>18){ tt.style.display='none'; cross.style.display='none'; return; }
  }

  const s=hit.s;
  const R=window._lastR;
  const active=chartActiveMetrics().active;
  const row = (label,value,cls='') => `<div class="tt-row"><span>${label}</span><span class="${cls}">${value}</span></div>`;
  const metricRows = active.map(m=>{
    if(m==='net_gex') return row('Net GEX',fmtNum(s.netGex),s.netGex>=0?'pos':'neg');
    if(m==='ag') return row('AG',fmtNum(Math.abs(s.callGex)+Math.abs(s.putGex)));
    if(m==='call_oi') return row('Call OI',fmtNum(s.callOI),'pos');
    if(m==='put_oi') return row('Put OI',fmtNum(s.putOI),'neg');
    if(m==='call_vol') return row('Call Volume',fmtNum(s.callVol),'pos');
    if(m==='put_vol') return row('Put Volume',fmtNum(s.putVol),'neg');
    if(m==='power') return row('Power Zone', Math.abs(s.strike-R.spot)/R.spot<=POWER_ZONE_WINDOW ? fmtNum(s.powerZone || 0) : '—');
    return '';
  }).join('');
  tt.innerHTML = `
    <div class="tt-title">${R?.symbol || ''} Strike ${fmtPrice(s.strike)}</div>
    ${metricRows || row('No metric selected','')}
  `;
  tt.style.display='block';
  cross.style.display='block';
  cross.style.left=Math.max(0,Math.min(rect.width,hit.x+hit.w/2))+'px';
  const tw=tt.offsetWidth, th=tt.offsetHeight;
  let left=x+14, top=y+14;
  if(left+tw>rect.width) left=x-tw-14;
  if(top+th>rect.height) top=y-th-14;
  tt.style.left=Math.max(6,left)+'px';
  tt.style.top=Math.max(6,top)+'px';
}
function hideChartTooltip(){
  ['chartTooltip','chartCrosshairX'].forEach(id=>{const el=byId(id); if(el) el.style.display='none';});
}
// ---------- Price Lines ----------
// Max Power's six-level set, one line per kind, as his own price chart draws it
// (research Addendum 2) and as CONTEXT.md defines a Price Line: the magnet (highest
// Power Zone within 1% of spot, "Matrix" here, #26), P1 (largest positive Net GEX),
// N1 (most negative), the AG Strike (largest Abs GEX), and the largest call and put
// volume. His TradingView export carries more (P1-P3, N1-N3, AG1-AG2), but that is an
// off-platform indicator the owner deferred, not his dashboard. Each strike needs
// netGex, callVol, putVol, ag and powerZone.
const LEVEL_COLORS = {P:'#22b5ff', N:'#ff453a', AG:'#915bf8', 'Call Vol':'#003cfe', 'Put Vol':'#e55f04', Matrix:'#e8ca01'};
// His line values carry two decimals ("6800.00").
function fmtLineValue(v){ return Number(v).toFixed(2); }
// The n strikes with the largest val, largest first, skipping any val of 0 or less.
function topStrikes(list, val, n){
  return list.filter(s=>val(s)>0).sort((a,b)=>val(b)-val(a)).slice(0,n);
}
function priceLines(strikes, spot){
  const near = strikes.filter(s=>Math.abs(s.strike-spot)/spot<=POWER_ZONE_WINDOW);
  const line = (label, kind, s)=>({label, chartLabel:label, kind, color:LEVEL_COLORS[kind], price:s.strike});
  return [
    ...topStrikes(near, s=>s.powerZone||0, 1).map(s=>line('Matrix','Matrix',s)),
    ...topStrikes(strikes, s=>s.netGex, 1).map(s=>line('P1','P',s)),
    ...topStrikes(strikes, s=>-s.netGex, 1).map(s=>line('N1','N',s)),
    ...topStrikes(strikes, s=>s.ag, 1).map(s=>line('AG Strike','AG',s)),
    ...topStrikes(strikes, s=>s.callVol, 1).map(s=>line('Call Vol','Call Vol',s)),
    ...topStrikes(strikes, s=>s.putVol, 1).map(s=>line('Put Vol','Put Vol',s)),
  ];
}
// Price Lines on one strike share a dashed line and a label ("P1 · AG Strike 6800.00").
function groupLines(lines){
  const groups=[];
  lines.forEach(l=>{
    const g=groups.find(o=>o.price===l.price);
    if(g) g.labels.push(l.chartLabel); else groups.push({price:l.price, color:l.color, labels:[l.chartLabel]});
  });
  return groups.sort((a,b)=>b.price-a.price);
}
function drawLineGroups(ctx, groups, {y, x0, x1, mobile, align='right'}){
  ctx.font=mobile?'bold 10px Segoe UI':'bold 11px Segoe UI'; ctx.textAlign=align; ctx.textBaseline='bottom';
  const labelYs=[];
  groups.forEach(g=>{
    const yy=y(g.price);
    ctx.save(); ctx.setLineDash([6,4]); ctx.strokeStyle=g.color; ctx.lineWidth=1.5;
    ctx.beginPath(); ctx.moveTo(x0,yy); ctx.lineTo(x1,yy); ctx.stroke(); ctx.restore();
    let ly=yy-2;
    while(labelYs.some(o=>Math.abs(o-ly)<12)) ly+=12;
    labelYs.push(ly);
    ctx.fillStyle=g.color;
    ctx.fillText(`${g.labels.join(' · ')} ${fmtLineValue(g.price)}`, align==='right' ? x1-4 : x0+4, ly);
  });
}
// The Power Zone drawn sideways along the price axis from the left edge of a price
// chart, translucent yellow, as on his price chart. strikes: {strike, powerZone}.
function drawPowerZoneSideways(ctx, strikes, {left, width, y, lo, hi, top, bottom}){
  const prof=powerZoneProfile(strikes.filter(s=>s.powerZone>0)).filter(p=>p.price>=lo && p.price<=hi);
  const maxV=Math.max(...prof.map(p=>p.value),0);
  if(prof.length<2 || !(maxV>0)) return;
  const pts=prof.map(p=>[left+p.value/maxV*width, y(p.price)]);
  ctx.beginPath(); ctx.moveTo(left,pts[0][1]); ctx.lineTo(pts[0][0],pts[0][1]);
  splineTo(ctx,pts,top,bottom,0.8);
  ctx.lineTo(left,pts[pts.length-1][1]); ctx.closePath();
  ctx.fillStyle='rgba(255,193,7,0.2)'; ctx.fill();
  ctx.beginPath(); ctx.moveTo(pts[0][0],pts[0][1]);
  splineTo(ctx,pts,top,bottom,0.8);
  ctx.strokeStyle='#e8ca01'; ctx.lineWidth=2; ctx.stroke();
}

// ---------- Price charts (Strike Chart) ----------
// Two charts of the searched ticker's latest session, beside the strike chart:
// 1-minute candles, and a "simplified" close-price line. What his simplified
// chart holds is not public; a close line is our guess. Neither chart reads the
// expiration, so only a ticker button reloads them.
const PRICE_CHARTS = {
  priceChart:       {kind:'candles', symbolId:'priceSymLabel',       tooltipId:'priceTooltip',       crosshairId:'priceCrosshairX',       emptyId:'priceEmpty',       legendId:'priceLegend',       legend:'Price'},
  priceSimpleChart: {kind:'line',    symbolId:'priceSimpleSymLabel', tooltipId:'priceSimpleTooltip', crosshairId:'priceSimpleCrosshairX', emptyId:'priceSimpleEmpty', legendId:'priceSimpleLegend', legend:'Simplified price'},
};
const PRICE_UP = '#3d9970', PRICE_DOWN = '#ff4136', PRICE_LINE = '#22b5ff';
const MARKET_TZ = 'America/New_York';
// Per ticker: {candles:[...]} or {error:'...'}.
let PRICE = {};
// Lines more than this from spot stay in the legend only, so one far strike cannot
// squash a day of candles into a line.
const PRICE_LINE_DISTANCE = 0.015;
// The candle chart's Price Lines and Power Zone profile come from the chosen expirations.
function homePriceLevels(){
  if(!homeRecord || !selectedExpirations?.size) return null;
  const chain=buildChainRealFromRecord(currentSymbol(),homeRecord);
  const R=calcGEX({...chain, quotes:chain.quotes.filter(q=>selectedExpirations.has(q.exp))});
  if(!R.strikes.length) return null;
  return {spot:R.spot, lines:priceLines(R.strikes,R.spot),
    profile:R.strikes.filter(s=>Math.abs(s.strike-R.spot)/R.spot<=POWER_ZONE_WINDOW && s.powerZone>0)};
}
// Power Zone between strikes, sampled every $2.50 like his profile (research Addendum 5).
function powerZoneProfile(strikes){
  if(strikes.length<2) return strikes.map(s=>({price:s.strike, value:s.powerZone}));
  const out=[];
  for(let i=0;i<strikes.length-1;i++){
    const a=strikes[i], b=strikes[i+1];
    for(let p=a.strike; p<b.strike; p+=2.5) out.push({price:p, value:a.powerZone+(b.powerZone-a.powerZone)*(p-a.strike)/(b.strike-a.strike)});
  }
  const last=strikes[strikes.length-1];
  out.push({price:last.strike, value:last.powerZone});
  return out;
}
function marketParts(sec){
  const parts=new Intl.DateTimeFormat('en-US',{timeZone:MARKET_TZ,year:'numeric',month:'2-digit',day:'2-digit',
    hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date(sec*1000));
  const get=type=>parts.find(part=>part.type===type)?.value||'';
  return {date:`${get('year')}-${get('month')}-${get('day')}`, hour:Number(get('hour')), minute:Number(get('minute'))};
}
// Keeps the regular session (09:30-16:00 ET) of the last trading day in the feed.
function latestSessionCandles(candles){
  const valid=(candles||[]).filter(c=>c && [c.time,c.open,c.high,c.low,c.close].every(v=>v!=null && v!=='' && Number.isFinite(Number(v))))
    .map(c=>({time:Number(c.time),open:Number(c.open),high:Number(c.high),low:Number(c.low),close:Number(c.close)}))
    .sort((a,b)=>a.time-b.time);
  const inSession=valid.filter(c=>{
    const {hour,minute}=marketParts(c.time);
    const mins=hour*60+minute;
    return mins>=570 && mins<960;
  });
  if(!inSession.length) return [];
  const lastDate=marketParts(inSession[inSession.length-1].time).date;
  return inSession.filter(c=>marketParts(c.time).date===lastDate);
}
function fmtMarketTime(sec){
  const {hour,minute}=marketParts(sec);
  return `${String(hour).padStart(2,'0')}:${String(minute).padStart(2,'0')}`;
}
function priceRange(candles,kind){
  const lows=candles.map(c=>kind==='line'?c.close:c.low);
  const highs=candles.map(c=>kind==='line'?c.close:c.high);
  let lo=Math.min(...lows), hi=Math.max(...highs);
  if(hi-lo<1e-9){ lo-=Math.max(Math.abs(lo)*0.001,0.01); hi+=Math.max(Math.abs(hi)*0.001,0.01); }
  const pad=(hi-lo)*0.06;
  return {lo:lo-pad, hi:hi+pad};
}
function niceStep(span,count){
  const raw=span/Math.max(count,1);
  const pow=Math.pow(10,Math.floor(Math.log10(raw)));
  const n=raw/pow;
  return (n<=1?1:n<=2?2:n<=2.5?2.5:n<=5?5:10)*pow;
}
function drawPriceChart(chartId,symbol){
  const cfg=PRICE_CHARTS[chartId];
  const cv=byId(chartId);
  if(!cfg || !cv) return;
  const state=PRICE[symbol];
  const candles=state?.candles || [];
  const symLabel=byId(cfg.symbolId);
  const empty=byId(cfg.emptyId);
  const dpr=window.devicePixelRatio||1;
  const W=cv.clientWidth;
  const mobile=W<640;
  const H=mobile?300:Math.max(360,Math.min(460,window.innerHeight-360));
  cv.width=W*dpr; cv.height=H*dpr; cv.style.height=H+'px';
  const ctx=cv.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,W,H);
  cv._matrixPrice=null;
  if(symLabel) symLabel.textContent=symbol + (candles.length ? '  ' + marketParts(candles[0].time).date : '');
  if(empty){
    empty.textContent = candles.length ? '' : (state ? (state.error || `No intraday prices for ${symbol}`) : `Loading ${symbol}…`);
  }
  // The candle chart carries the six Price Lines and the Power Zone profile.
  const levels=cfg.kind==='candles' && symbol===currentSymbol() ? homePriceLevels() : null;
  const onChart=l=>Math.abs(l.price-levels.spot)/levels.spot<=PRICE_LINE_DISTANCE;
  const legend=byId(cfg.legendId);
  if(legend) legend.innerHTML=levels
    ? levels.lines.map(l=>`<span><span class="sq" style="background:${l.color}"></span>${esc(l.chartLabel)} ${esc(fmtLineValue(l.price))}${onChart(l)?'':' (off chart)'}</span>`).join('')
    : esc(cfg.legend);
  if(!candles.length) return;

  const padL=mobile?50:66, padR=mobile?10:18, padT=mobile?14:18, padB=mobile?30:36;
  const plotW=W-padL-padR, plotH=H-padT-padB;
  const top=padT, bottom=padT+plotH;
  let {lo,hi}=priceRange(candles,cfg.kind);
  const shownLines=levels ? levels.lines.filter(onChart) : [];
  if(shownLines.length){
    // Widen the range so every line near spot stays on the chart.
    const prices=shownLines.map(l=>l.price);
    const lo2=Math.min(lo,...prices), hi2=Math.max(hi,...prices);
    if(lo2<lo || hi2>hi){ const pad=(hi2-lo2)*0.03; lo=lo2-pad; hi=hi2+pad; }
  }
  const y=v=>top+(hi-v)/(hi-lo)*plotH;
  const slot=plotW/candles.length;
  const xCenter=i=>padL+slot*(i+0.5);

  // gridlines + price ticks
  ctx.font=mobile?'bold 10px Segoe UI':'bold 12px Segoe UI'; ctx.textBaseline='middle'; ctx.textAlign='right';
  const step=niceStep(hi-lo,mobile?4:6);
  for(let v=Math.ceil(lo/step)*step; v<=hi; v+=step){
    const yy=y(v);
    ctx.strokeStyle='rgba(255,255,255,.05)'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(padL,yy); ctx.lineTo(W-padR,yy); ctx.stroke();
    ctx.fillStyle='#c4c4c4';
    ctx.fillText(step<1?v.toFixed(2):fmtPrice(v),padL-8,yy);
  }
  // time ticks on the hour and half hour
  ctx.textAlign='center'; ctx.textBaseline='top';
  const every=mobile||plotW<520?60:30;
  candles.forEach((c,i)=>{
    const {hour,minute}=marketParts(c.time);
    if((hour*60+minute)%every!==0) return;
    const xx=xCenter(i);
    ctx.strokeStyle='rgba(255,255,255,.035)';
    ctx.beginPath(); ctx.moveTo(xx,top); ctx.lineTo(xx,bottom); ctx.stroke();
    ctx.fillStyle='#c4c4c4';
    ctx.fillText(fmtMarketTime(c.time),xx,bottom+8);
  });

  if(cfg.kind==='candles'){
    const bw=Math.max(1,Math.min(slot*0.7,9));
    candles.forEach((c,i)=>{
      const color=c.close>=c.open?PRICE_UP:PRICE_DOWN;
      const xx=xCenter(i);
      ctx.strokeStyle=color; ctx.lineWidth=1;
      ctx.beginPath(); ctx.moveTo(xx,y(c.high)); ctx.lineTo(xx,y(c.low)); ctx.stroke();
      const yo=y(c.open), yc=y(c.close);
      ctx.fillStyle=color;
      ctx.fillRect(xx-bw/2,Math.min(yo,yc),bw,Math.max(1,Math.abs(yc-yo)));
    });
  } else {
    ctx.strokeStyle=PRICE_LINE; ctx.lineWidth=1.8;
    ctx.beginPath();
    candles.forEach((c,i)=>{ const xx=xCenter(i), yy=y(c.close); i?ctx.lineTo(xx,yy):ctx.moveTo(xx,yy); });
    ctx.stroke();
  }
  if(levels){
    drawPowerZoneSideways(ctx, levels.profile, {left:padL, width:plotW*0.25, y, lo, hi, top, bottom});
    drawLineGroups(ctx, groupLines(shownLines), {y, x0:padL, x1:W-padR, mobile});
  }
  ctx.strokeStyle='rgba(255,255,255,.18)'; ctx.lineWidth=1;
  ctx.beginPath(); ctx.moveTo(padL,bottom); ctx.lineTo(W-padR,bottom); ctx.stroke();
  cv._matrixPrice={candles,padL,slot,kind:cfg.kind,symbol};
}
function drawPriceCharts(symbol=currentSymbol()){
  Object.keys(PRICE_CHARTS).forEach(id=>drawPriceChart(id,symbol));
}
function showPriceTooltip(ev){
  const cv=ev.currentTarget;
  const cfg=PRICE_CHARTS[cv?.id];
  const info=cv?._matrixPrice;
  const tt=byId(cfg?.tooltipId), cross=byId(cfg?.crosshairId);
  if(!info || !tt || !cross) return;
  const rect=cv.getBoundingClientRect();
  const point=ev.touches && ev.touches[0] ? ev.touches[0] : ev;
  const x=point.clientX-rect.left, yPos=point.clientY-rect.top;
  const i=Math.floor((x-info.padL)/info.slot);
  const c=info.candles[i];
  if(!c){ tt.style.display='none'; cross.style.display='none'; return; }
  const row=(label,value)=>`<div class="tt-row"><span>${label}</span><span>${value}</span></div>`;
  const fmt=v=>Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
  tt.innerHTML=`<div class="tt-title">${esc(info.symbol)} ${fmtMarketTime(c.time)} ET</div>`+
    (info.kind==='candles'
      ? row('Open',fmt(c.open))+row('High',fmt(c.high))+row('Low',fmt(c.low))+row('Close',fmt(c.close))
      : row('Close',fmt(c.close)));
  tt.style.display='block';
  cross.style.display='block';
  cross.style.left=(info.padL+info.slot*(i+0.5))+'px';
  const tw=tt.offsetWidth, th=tt.offsetHeight;
  let left=x+14, topPos=yPos+14;
  if(left+tw>rect.width) left=x-tw-14;
  if(topPos+th>rect.height) topPos=yPos-th-14;
  tt.style.left=Math.max(6,left)+'px';
  tt.style.top=Math.max(6,topPos)+'px';
}
function hidePriceTooltips(){
  Object.values(PRICE_CHARTS).forEach(cfg=>[cfg.tooltipId,cfg.crosshairId].forEach(id=>{const el=byId(id); if(el) el.style.display='none';}));
}
// ---------- Screens and routes ----------
// One page serves every screen: the path picks the screen and ?ticker= its ticker,
// so a copied link opens the same screen on the same ticker.
const MATRIX_BASE_PATH = '/matrix/';
const SCREENS = {
  'gex':             {path:'',                ticker:true},
  'oi-volume':       {path:'oi-volume',       ticker:true},
  'gex-analysis':    {path:'gex-analysis',    ticker:true},
  'key-levels':      {path:'key-levels',      ticker:true},
  'options-summary': {path:'options-summary', ticker:false},
  'how-to-use-gex':  {path:'how-to-use-gex',  ticker:false},
  'disclaimer':      {path:'disclaimer',      ticker:false},
};
const HOME_VIEW = 'gex';
const CORE_SYMBOL_INFO = {
  NDX:'Nasdaq-100 Index',
  SPX:'S&P 500 Index',
  SPY:'SPDR S&P 500 ETF Trust',
  QQQ:'Invesco QQQ Trust',
  VIX:'Cboe Volatility Index',
};
// Like his GEX board: every screen picks from these three buttons, with no search box.
const SCREEN_TICKERS = ['SPX','SPY','QQQ'];
let activeView = null;
// Each screen keeps its own ticker, the way each of his screens has its own ticker input.
const screenTickers = Object.fromEntries(
  Object.keys(SCREENS).filter(view=>SCREENS[view].ticker).map(view=>[view,DEFAULT_SYMBOL])
);
// The Strike Chart's chosen expirations: a Set, or null until a ticker's first load.
let selectedExpirations = null;

function normalizeSymbol(value){
  return String(value||'').trim().toUpperCase();
}
function viewFromPath(pathname){
  const rest=String(pathname||'').replace(/^\/matrix\/?/,'').replace(/\/+$/,'');
  return Object.keys(SCREENS).find(view=>SCREENS[view].path===rest) || HOME_VIEW;
}
function screenUrl(view){
  const url=MATRIX_BASE_PATH+SCREENS[view].path;
  return SCREENS[view].ticker ? `${url}?ticker=${encodeURIComponent(screenTickers[view])}` : url;
}
function currentSymbol(){
  return screenTickers[HOME_VIEW];
}
function screenForm(view){
  return document.querySelector(`.ticker-picker[data-screen="${view}"]`);
}
function setTickerStatus(view,text='',state=''){
  const status=screenForm(view)?.querySelector('.ticker-status');
  if(!status) return;
  status.textContent=text;
  status.className=`ticker-status${state?' '+state:''}`;
}
// Draws the screen's three ticker buttons with the shown one lit.
// A date control's placeholder while the first chain for its screen downloads.
function showDatesLoading(id, html){
  const el=byId(id);
  if(el) el.innerHTML=html;
}
function syncTickerInput(view,value=screenTickers[view]){
  const buttons=screenForm(view)?.querySelector('.ticker-buttons');
  if(buttons) buttons.innerHTML=SCREEN_TICKERS.map(t=>
    `<button type="button" class="gb-btn${t===value?' active':''}" data-ticker="${t}" aria-pressed="${t===value}">${t}</button>`).join('');
}

// ---------- Expirations ----------
// Every open expiry of a chain (New York date), nearest first.
function chainExpirations(chain){
  const todayIso = nyTodayIso();
  return [...new Set(chain.quotes.map(q=>q.exp).filter(exp=>exp && exp >= todayIso))].sort();
}
// ---------- Expiry picker (every screen) ----------
// Like gex_dashboard: preset buttons (0DTE, Week, Month, All) over a short list of
// dates, each "Oct 27 (0d)"; a click on a date adds or removes it. A new ticker starts
// on the last preset used (Week at first), remembered in this browser.
const EXP_PRESETS = [['0dte','0DTE',0],['week','Week',7],['month','Month',30],['all','All',Infinity]];
const EXP_PRESET_KEY = 'matrix.expiryPreset';
function nyTodayIso(){
  return new Intl.DateTimeFormat('en-CA',{timeZone:'America/New_York'}).format(new Date());
}
function daysOut(exp){
  return Math.round((Date.parse(exp+'T00:00:00Z')-Date.parse(nyTodayIso()+'T00:00:00Z'))/86400000);
}
// A preset's dates; when none fall in it (a weekend for 0DTE), the nearest date.
function presetDates(preset, expiries){
  const max = (EXP_PRESETS.find(p=>p[0]===preset) || EXP_PRESETS[1])[2];
  if(max===Infinity) return [...expiries];
  const out = expiries.filter(exp=>daysOut(exp)<=max);
  return out.length ? out : expiries.slice(0,1);
}
function savedPreset(){
  try{ return localStorage.getItem(EXP_PRESET_KEY) || 'week'; }catch(_err){ return 'week'; }
}
function savePreset(preset){
  try{ localStorage.setItem(EXP_PRESET_KEY, preset); }catch(_err){ /* private mode */ }
}
function defaultExpirySelection(expiries){
  return new Set(presetDates(savedPreset(), expiries));
}
// The preset the selection matches exactly, if any (its button is lit).
function matchingPreset(expiries, selected){
  return EXP_PRESETS.map(p=>p[0]).find(preset=>{
    const dates = presetDates(preset, expiries);
    return dates.length===selected.size && dates.every(exp=>selected.has(exp));
  }) || null;
}
function fmtExpiryLabel(exp){
  const d = new Date(exp+'T12:00:00Z');
  return `${d.toLocaleDateString('en-US',{month:'short',day:'numeric',timeZone:'UTC'})} (${daysOut(exp)}d)`;
}
// shares: optional Map of each date's share of the gamma (the GEX board shows it).
function renderExpiryPicker(id, expiries=[], selected=new Set(), shares=null){
  const el = byId(id);
  if(!el) return;
  expiries = expiries || []; selected = selected || new Set();
  if(!expiries.length){ el.innerHTML = '<span class="xp-note">No expirations</span>'; return; }
  const lit = matchingPreset(expiries, selected);
  const presets = EXP_PRESETS.map(([key,label])=>
    `<button type="button" class="xp-preset${key===lit?' active':''}" data-xp-preset="${key}" aria-pressed="${key===lit}">${label}</button>`).join('');
  const dates = expiries.map(exp=>{
    const on = selected.has(exp);
    const share = shares ? `<small>${(shares.get(exp)||0).toFixed(1)}%</small>` : '';
    return `<button type="button" class="xp-date${on?' active':''}" data-xp-date="${esc(exp)}" aria-pressed="${on}" title="${esc(exp)}">${esc(fmtExpiryLabel(exp))}${share}</button>`;
  }).join('');
  const count = selected.size ? `${selected.size} selected` : 'Pick at least one date';
  el.innerHTML = `<div class="xp-presets">${presets}<span class="xp-note">${count}</span></div><div class="xp-dates">${dates}</div>`;
}
// Wires a picker's clicks: getState() -> {expiries, selected}; onChange(newSet).
function bindExpiryPicker(id, getState, onChange){
  bind(id,'click',event=>{
    const state = getState();
    if(!state) return;
    const preset = event.target.closest?.('[data-xp-preset]')?.dataset.xpPreset;
    if(preset){ savePreset(preset); onChange(new Set(presetDates(preset, state.expiries))); return; }
    const exp = event.target.closest?.('[data-xp-date]')?.dataset.xpDate;
    if(!exp) return;
    const next = new Set(state.selected);
    if(next.has(exp)) next.delete(exp); else next.add(exp);
    onChange(next);
  });
}

// Strike Chart dates.
let homeExpiries = [];
function populateExpirations(chain){
  homeExpiries = chainExpirations(chain);
  selectedExpirations = selectedExpirations
    ? new Set([...selectedExpirations].filter(exp=>homeExpiries.includes(exp)))
    : defaultExpirySelection(homeExpiries);
  renderExpirationPicker();
}
function renderExpirationPicker(){
  renderExpiryPicker('homeDates', homeExpiries, selectedExpirations || new Set());
}

// ---------- Navigation ----------
function closeMenu(){
  byId('sidebar')?.classList.remove('menu-open');
  const toggle=byId('menuToggle');
  if(toggle){
    toggle.setAttribute('aria-expanded','false');
    toggle.setAttribute('aria-label','Open menu');
  }
}
function toggleMenu(){
  const open=byId('sidebar').classList.toggle('menu-open');
  const toggle=byId('menuToggle');
  toggle.setAttribute('aria-expanded',String(open));
  toggle.setAttribute('aria-label',open?'Close menu':'Open menu');
}
function setView(view){
  const changed = view !== activeView;
  activeView = view;
  document.querySelectorAll('a[data-view]').forEach(link=>{
    const current=link.dataset.view===view;
    link.classList.toggle('active',current);
    if(current) link.setAttribute('aria-current','page');
    else link.removeAttribute('aria-current');
  });
  document.querySelectorAll('.app-view').forEach(section=>section.classList.toggle('active',section.id===`view-${view}`));
  const title=byId(`view-${view}`)?.dataset.title;
  document.title = title ? `${title} · Matrix` : 'Matrix';
  syncTickerInput(view);
  hideAllHoverHelpers();
  exitGexBoardFullscreen();
  closeMenu();
  if(changed) window.scrollTo({top:0,left:0});
}
// Shows the screen the address bar names. The Strike Chart loads data only when
// it has none for its ticker; after that, data reloads only on a ticker button.
function applyLocation(){
  const view=viewFromPath(window.location.pathname);
  const fromUrl=normalizeSymbol(new URLSearchParams(window.location.search).get('ticker'));
  // Only the three button tickers count; an old link to another ticker opens SPX.
  const urlTicker=SCREENS[view].ticker && SCREEN_TICKERS.includes(fromUrl) ? fromUrl : null;
  if(urlTicker && view!==HOME_VIEW) screenTickers[view]=urlTicker;
  if(SCREENS[view].ticker && !SCREEN_TICKERS.includes(screenTickers[view])) screenTickers[view]=SCREEN_TICKERS[0];
  if(SCREENS[view].ticker && fromUrl && !urlTicker) history.replaceState(null,'',screenUrl(view));
  setView(view);
  if(view==='options-summary') loadPcRatio();
  if(view===OIV_VIEW){
    const symbol=screenTickers[OIV_VIEW];
    if(symbol===oiv.symbol && oiv.record) renderOiVolume();
    else refreshOiVolume(symbol, true);
    return;
  }
  if(view===KEY_LEVELS_VIEW){
    showKeyLevels();
    return;
  }
  if(view===GB_VIEW){
    showGexBoard();
    return;
  }
  if(view!==HOME_VIEW) return;
  const symbol=urlTicker || currentSymbol();
  if(symbol===currentSymbol() && homeRecord){
    run();
    drawPriceCharts(symbol);
  }
  else refreshHome(symbol, true);
}
function navigate(view){
  if(view===activeView){
    closeMenu();
    return;
  }
  history.pushState(null,'',screenUrl(view));
  applyLocation();
}
function search(view,raw){
  const symbol=normalizeSymbol(raw);
  if(!SCREEN_TICKERS.includes(symbol)) return;
  if(view===HOME_VIEW){
    refreshHome(symbol);
    return;
  }
  if(view===OIV_VIEW){
    refreshOiVolume(symbol);
    return;
  }
  if(view===KEY_LEVELS_VIEW){
    loadKeyLevels(symbol);
    return;
  }
  screenTickers[view]=symbol;
  syncTickerInput(view);
  setTickerStatus(view,'');
  if(activeView===view) history.replaceState(null,'',screenUrl(view));
}

// ---------- Strike Chart ----------
// The Strike Chart keeps its own copy of the chain it loaded, so a ticker click on
// another screen never changes what it shows (each screen has its own ticker).
let homeRecord = null;
function run(){
  const sym=currentSymbol();
  if(!homeRecord){
    setTickerStatus(HOME_VIEW,`${sym}: no data yet. Press the ticker again to retry.`,'error');
    return;
  }
  let chain = buildChainRealFromRecord(sym,homeRecord);
  populateExpirations(chain);
  chain = {...chain, quotes: chain.quotes.filter(q=>selectedExpirations.has(q.exp))};
  const R = calcGEX(chain);
  R.expectedMove = calcExpectedMove(chain);
  renderImpl(R);
}

// ---------- Data loading (only on a ticker button) ----------
const MATRIX_CBOE_DATA_URL = '/api/matrix/cboe-data';
let _homeRequest=0;
function fetchLiveData(url){
  return fetch(url, {cache:'no-store'})
    .then(r=>r.ok?r.json():null)
    .catch(()=>null);
}
function validCboeData(d){
  return d && d.SPX && Array.isArray(d.SPX.opts) && Number.isFinite(Number(d.SPX.spot));
}
// The core tickers (NDX, SPX, SPY, QQQ, VIX) come in one call. Screens share it: a
// download already on its way is joined, and opening a screen (reuse) takes one from the
// last two minutes instead of downloading again. A ticker click always downloads.
const CORE_REUSE_MS = 2*60*1000;
let _coreLoad = null, _coreLoadedAt = 0;
function loadCoreData(reuse=false){
  if(_coreLoad) return _coreLoad;
  if(reuse && Date.now()-_coreLoadedAt < CORE_REUSE_MS) return Promise.resolve({ok:true});
  _coreLoad = fetchLiveData(MATRIX_CBOE_DATA_URL + '?t=' + Date.now()).then(data=>{
    if(!validCboeData(data)) return {ok:false, detail:'Could not load market data. Press the ticker again to retry.'};
    REAL=data;
    _coreLoadedAt=Date.now();
    return {ok:true};
  }).finally(()=>{ _coreLoad=null; });
  return _coreLoad;
}
// The latest session's 1-minute candles for the two price charts.
async function loadPriceData(symbol){
  let payload=null;
  try{
    const response=await fetch(`${MATRIX_CANDLES_URL}?symbol=${encodeURIComponent(symbol)}&interval=1m&range=1d`,{cache:'no-store'});
    payload=await response.json();
  }catch(_err){
    payload=null;
  }
  if(!payload?.ok) return {error:`No intraday prices for ${symbol}`};
  const candles=latestSessionCandles(payload.candles);
  return candles.length ? {candles} : {error:`No intraday prices for ${symbol}`};
}
// Refresh of the Strike Chart: a ticker button, or opening it with no data yet.
// The chain and the price candles load together.
async function refreshHome(symbol, reuse=false){
  const request=++_homeRequest;
  const previous=currentSymbol();
  if(activeView===HOME_VIEW) syncTickerInput(HOME_VIEW,symbol);
  setTickerStatus(HOME_VIEW,`Loading ${symbol}…`);
  if(!homeRecord) showDatesLoading('homeDates','<span class="xp-note">Loading dates…</span>');
  // The dates and the strike chart draw as soon as the chain is in; the candles follow.
  const pricePromise=loadPriceData(symbol);
  const result=await loadCoreData(reuse);
  if(request!==_homeRequest) return;  // a newer click took over
  if(!REAL[symbol]){
    // Nothing to show for this ticker: fall back to the last ticker, or the default one.
    if(!REAL[previous] && symbol!==previous) await refreshHome(previous);
    setTickerStatus(HOME_VIEW,result.detail,'error');
    return;
  }
  if(symbol!==previous) selectedExpirations=null;
  homeRecord=REAL[symbol];
  screenTickers[HOME_VIEW]=symbol;
  if(activeView===HOME_VIEW){
    syncTickerInput(HOME_VIEW);
    history.replaceState(null,'',screenUrl(HOME_VIEW));
  }
  run();
  setTickerStatus(HOME_VIEW,result.ok?'':`Refresh failed. Showing the last data for ${symbol}.`,result.ok?'':'error');
  const price=await pricePromise;
  if(request!==_homeRequest) return;
  // Keep the last good candles if this refresh could not get new ones.
  if(price.candles || !PRICE[symbol]?.candles) PRICE[symbol]=price;
  drawPriceCharts(symbol);
}

// ---------- Key Levels ----------
// Price for the searched ticker with its four Key Levels, and the Forecast under it.
// One ticker click loads both, the way his Key Levels page fills chart and text together.
const KEY_LEVELS_VIEW = 'key-levels';
// Guess: his bar size and time span are not public. 5-minute bars over five days
// show where price sits against levels built from every open expiry.
const KEY_LEVELS_CANDLES = {interval:'5m', range:'5d'};
// The four Key Levels, in the colours Matrix already used for them.
const KEY_LEVEL_STYLES = [
  {key:'matrixStrike',   label:'Matrix',     color:'#e8ca01'},
  {key:'callWall',       label:'Call Wall',  color:'#22b5ff'},
  {key:'putWall',        label:'Put Wall',   color:'#ff453a'},
  {key:'flip',           label:'Zero Gamma', color:'#f5f5f5'},
  {key:'maxGammaStrike', label:'Max Gamma',  color:'#8b5fe8'},
];
// Levels further than this from spot stay in the legend but off the chart,
// so one far wall cannot squash the candles into a line.
const KEY_LEVELS_MAX_DISTANCE = 0.08;
let keyLevelsState = null;  // {symbol, R, candles, expiries, selected}
let _keyLevelsRequest = 0;

function keyLevels(R){
  return KEY_LEVEL_STYLES
    .map(style=>({...style, price:R?.[style.key]==null ? NaN : Number(R[style.key])}))
    .filter(level=>Number.isFinite(level.price) && level.price>0);
}
// The GEX profile of the chosen expirations (all open ones when none is given), with
// the Matrix level: the highest Power Zone within 1% of spot.
function keyLevelsGex(symbol, selected=null){
  const chain=buildChainReal(symbol);
  const todayIso=nyTodayIso();
  const open=chain.quotes.filter(q=>selected ? selected.has(q.exp) : (!q.exp || q.exp>=todayIso));
  const R=calcGEX(open.length ? {...chain, quotes:open} : chain);
  R.matrixStrike = matrixLine(R.strikes, R.spot)?.price ?? null;
  return R;
}

// ----- Forecast -----
// Rule-based English text built from the same GEX profile as the chart, laid out as his
// #market-forecast is: an h3 title (ticker and date), h4 sections and h5 sub-heads
// (#39). It names the levels themselves and no other price target.
function levelDisplayPrice(value,R){
  if(value == null || !Number.isFinite(Number(value))) return '--';
  const base = fmtPrice(Number(value));
  const linked = linkedMarketPrice(Number(value),R);
  if(!linked || linked.price == null) return base;
  return `${base} / ${linked.symbol} ${fmtLinkedMarketPrice(linked)}`;
}
function nearestKeyLevel(R,side){
  return keyLevels(R)
    .filter(level=>side==='above' ? level.price>R.spot : level.price<R.spot)
    .sort((a,b)=>side==='above' ? a.price-b.price : b.price-a.price)[0] || null;
}
function pctFromSpot(price,spot){
  const d=(price-spot)/spot*100;
  return `${d>=0?'+':''}${d.toFixed(2)}%`;
}
function forecastDate(){
  return new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',year:'numeric',month:'short',day:'numeric'}).format(new Date());
}
// The AG Strike: where the most hedging sits, read as the target.
function agStrike(R){
  return R.strikes.reduce((best,s)=>!best || s.ag>best.ag ? s : best, null);
}
function forecastScenarios(R){
  const level = key=>keyLevels(R).find(l=>l.key===key);
  const callWall = level('callWall'), putWall = level('putWall'), flip = level('flip');
  const positive = R.totalGex>=0;
  const p = v=>levelDisplayPrice(v,R);
  const out = [];
  if(callWall) out.push({head:`Above the Call Wall ${p(callWall.price)}`, text: positive
    ? 'Price leaves the largest positive Net GEX behind. In positive gamma dealer selling tends to pull it back toward the wall; a hold above it means the range has moved up.'
    : 'Price leaves the largest positive Net GEX behind. In negative gamma dealer buying can add to the move, so a break can run.'});
  if(callWall && putWall) out.push({head:`Between the Put Wall ${p(putWall.price)} and the Call Wall ${p(callWall.price)}`, text: positive
    ? 'Inside the walls positive gamma damps moves: rallies tend to stall near the Call Wall and dips tend to hold near the Put Wall.'
    : 'Inside the walls negative gamma lets price swing further between them; expect wider moves than usual.'});
  // Only supports still under the price; the Put Wall stays even if price already broke it.
  const floor = [putWall && `the Put Wall ${p(putWall.price)}`, flip && flip.price<R.spot && `Zero Gamma ${p(flip.price)}`].filter(Boolean);
  if(floor.length) out.push({head:`Below ${floor.join(' or ')}`, text:
    'Below the support levels gamma turns more negative: dealer selling can speed a drop up, and moves tend to get larger.'});
  return out;
}
function forecastHtml(R){
  const spot = R.spot, sym = esc(R.symbol);
  const levels = keyLevels(R);
  const flip = levels.find(l=>l.key==='flip');
  const positive = R.totalGex>=0;
  const position = flip
    ? `${sym} at ${esc(levelDisplayPrice(spot,R))} is ${spot>=flip.price?'above':'below'} Zero Gamma ${esc(levelDisplayPrice(flip.price,R))} (${esc(pctFromSpot(flip.price,spot))} from spot).`
    : `${sym} at ${esc(levelDisplayPrice(spot,R))}. Net GEX does not change sign across the chain, so there is no Zero Gamma.`;
  const regime = positive
    ? 'Gamma regime: <b class="pos">positive</b>. Dealer hedging tends to damp moves, so ranges are more likely than trends.'
    : 'Gamma regime: <b class="neg">negative</b>. Dealer hedging tends to add to moves, so breaks can run further.';
  const levelItems = levels.map(l=>`<li>${esc(l.label)}: <b>${esc(levelDisplayPrice(l.price,R))}</b> (${esc(pctFromSpot(l.price,spot))})</li>`).join('');
  const target = agStrike(R);
  const scenarios = forecastScenarios(R).map(s=>`<h5>${esc(s.head)}</h5><p>${esc(s.text)}</p>`).join('');
  return `
    <h3>${sym} · ${esc(forecastDate())}</h3>
    <h4>Current position</h4>
    <p>${position}</p>
    <p>${regime}</p>
    <h4>Key levels</h4>
    <ul>${levelItems || '<li>No Key Levels for this ticker</li>'}</ul>
    <h4>Target</h4>
    <p>${target
      ? `AG Strike <b>${esc(levelDisplayPrice(target.strike,R))}</b> (${esc(pctFromSpot(target.strike,spot))}): the strike with the most hedging, which price is often drawn toward.`
      : 'No AG Strike for this ticker.'}</p>
    <h4>Scenarios</h4>
    ${scenarios || '<p>Not enough levels for scenarios.</p>'}
  `;
}

// ----- Price chart -----
function keyLevelsTimeLabel(time,withDay){
  const opts = withDay
    ? {timeZone:'America/New_York',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false}
    : {timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',hour12:false};
  return new Date(time*1000).toLocaleString('en-US',opts);
}
function drawKeyLevelsChart(state){
  const cv = byId('keyLevelsChart');
  if(!cv || !state) return;
  const {R, candles} = state;
  const dpr = window.devicePixelRatio||1;
  const W = cv.clientWidth;
  if(!W) return;
  const mobile = W < 640;
  const H = mobile ? Math.max(380, Math.min(520, window.innerHeight - 240)) : Math.max(520, Math.min(720, window.innerHeight - 260));
  cv.width=W*dpr; cv.height=H*dpr; cv.style.height=H+"px";
  const ctx = cv.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,W,H);

  const levels = keyLevels(R);
  const onChart = levels.filter(level=>Math.abs(level.price-R.spot)/R.spot<=KEY_LEVELS_MAX_DISTANCE);
  const symLabel = byId('klSymLabel');
  if(symLabel) symLabel.textContent = R.symbol;
  const legend = byId('klLegend');
  if(legend) legend.innerHTML = levels.map(level=>
    `<span><i class="dashed" style="border-color:${level.color}"></i>${esc(level.label)} ${fmtPrice(level.price)}${onChart.includes(level)?'':' (off chart)'}</span>`
  ).join('') || '<span style="color:#7d8799">No Key Levels for this ticker</span>';

  const padL = mobile ? 50 : 72;
  const padR = mobile ? 70 : 132;
  const padT = mobile ? 20 : 26;
  const padB = mobile ? 44 : 50;
  const plotW = W-padL-padR, plotH = H-padT-padB;
  const top = padT, bottom = padT+plotH;

  const prices = [R.spot, ...candles.flatMap(c=>[c.low,c.high]), ...onChart.map(l=>l.price)].filter(Number.isFinite);
  let minPrice = Math.min(...prices), maxPrice = Math.max(...prices);
  const pad = Math.max((maxPrice-minPrice)*.06, R.spot*.002, .01);
  minPrice -= pad; maxPrice += pad;
  const y = price=>top+(maxPrice-price)/(maxPrice-minPrice)*plotH;

  // grid + price axis
  ctx.font = mobile ? "bold 10px Segoe UI" : "bold 12px Segoe UI";
  ctx.textBaseline = "middle";
  for(let t=0;t<=6;t++){
    const yy = top+plotH*t/6, price = maxPrice-(maxPrice-minPrice)*t/6;
    ctx.strokeStyle = "rgba(255,255,255,.04)"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL,yy); ctx.lineTo(padL+plotW,yy); ctx.stroke();
    ctx.fillStyle = "#c4c4c4"; ctx.textAlign = "right";
    ctx.fillText(fmtPrice(price), padL-8, yy);
  }

  // candles, one slot each so nights and weekends leave no gaps
  const slot = plotW/Math.max(candles.length,1);
  const xAt = i=>padL+slot*(i+.5);
  if(candles.length){
    // several days: a divider and the date at each day's first bar; one day: times
    const days = candles.map(c=>keyLevelsTimeLabel(c.time,true).split(',')[0]);
    const multiDay = days[0]!==days[days.length-1];
    const labelEvery = Math.max(1, Math.ceil(candles.length/(mobile?4:8)));
    ctx.textBaseline = "top"; ctx.textAlign = "left";
    ctx.fillStyle = "#b9c0c5";
    candles.forEach((c,i)=>{
      const newDay = i>0 && days[i]!==days[i-1];
      if(newDay){
        ctx.strokeStyle = "rgba(255,255,255,.08)"; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(padL+slot*i,top); ctx.lineTo(padL+slot*i,bottom); ctx.stroke();
      }
      if(multiDay ? (i===0 || newDay) : i%labelEvery===0){
        ctx.fillText(multiDay ? days[i] : keyLevelsTimeLabel(c.time,false), padL+slot*i+3, bottom+8);
      }
    });
    const bodyW = Math.max(1, Math.min(9, slot*.65));
    candles.forEach((c,i)=>{
      const x = xAt(i), color = c.close>=c.open ? '#26a69a' : '#ef5350';
      ctx.strokeStyle = color; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x,y(c.high)); ctx.lineTo(x,y(c.low)); ctx.stroke();
      const bodyTop = Math.min(y(c.open),y(c.close));
      ctx.fillStyle = color;
      ctx.fillRect(x-bodyW/2, bodyTop, bodyW, Math.max(1, Math.abs(y(c.close)-y(c.open))));
    });
  } else {
    ctx.fillStyle = "#8d969c"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.font = mobile ? "bold 12px Segoe UI" : "bold 14px Segoe UI";
    ctx.fillText(`No price data for ${R.symbol}`, padL+plotW/2, top+plotH/2);
  }

  // Power Zone sideways from the left edge, as on the other price charts
  drawPowerZoneSideways(ctx, R.strikes.filter(k=>Math.abs(k.strike-R.spot)/R.spot<=POWER_ZONE_WINDOW),
    {left:padL, width:plotW*0.2, y, lo:minPrice, hi:maxPrice, top, bottom});

  // the Key Levels across the chart, labelled on the right
  ctx.font = mobile ? "bold 10px Segoe UI" : "bold 12px Segoe UI";
  ctx.textAlign = "left"; ctx.textBaseline = "middle";
  const labelYs = [];
  [...onChart].sort((a,b)=>b.price-a.price).forEach(level=>{
    const yy = y(level.price);
    ctx.save(); ctx.setLineDash([7,5]); ctx.strokeStyle = level.color; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(padL,yy); ctx.lineTo(padL+plotW,yy); ctx.stroke(); ctx.restore();
    // nudge labels apart when two levels sit close together
    let ly = yy;
    while(labelYs.some(other=>Math.abs(other-ly)<14)) ly += 14;
    labelYs.push(ly);
    ctx.fillStyle = level.color;
    // on narrow screens the name alone; the legend above carries the prices
    ctx.fillText(mobile ? level.label : `${level.label} ${fmtPrice(level.price)}`, padL+plotW+5, ly);
  });

  cv._klPlot = {padL, plotW, top, bottom, slot, candles, symbol:R.symbol};
}
function showKeyLevelsTooltip(ev){
  const cv = byId('keyLevelsChart'), tt = byId('klTooltip'), cross = byId('klCrosshairX');
  const plot = cv?._klPlot;
  if(!tt || !cross || !plot || !plot.candles.length) return;
  const rect = cv.getBoundingClientRect();
  const point = ev.touches && ev.touches[0] ? ev.touches[0] : ev;
  const x = point.clientX-rect.left, y = point.clientY-rect.top;
  if(x<plot.padL || x>plot.padL+plot.plotW || y<plot.top || y>plot.bottom){ hideKeyLevelsTooltip(); return; }
  const i = Math.max(0, Math.min(plot.candles.length-1, Math.floor((x-plot.padL)/plot.slot)));
  const c = plot.candles[i];
  const row = (label,value) => `<div class="tt-row"><span>${label}</span><span>${value}</span></div>`;
  tt.innerHTML = `
    <div class="tt-title">${esc(plot.symbol)} · ${esc(keyLevelsTimeLabel(c.time,true))}</div>
    ${row('Open',fmtPrice(c.open))}${row('High',fmtPrice(c.high))}${row('Low',fmtPrice(c.low))}${row('Close',fmtPrice(c.close))}
  `;
  tt.style.display = 'block';
  cross.style.display = 'block';
  cross.style.left = (plot.padL+plot.slot*(i+.5))+'px';
  const tw = tt.offsetWidth, th = tt.offsetHeight;
  let left = x+14, topPx = y+14;
  if(left+tw>rect.width) left = x-tw-14;
  if(topPx+th>rect.height) topPx = y-th-14;
  tt.style.left = Math.max(6,left)+'px';
  tt.style.top = Math.max(6,topPx)+'px';
}
function hideKeyLevelsTooltip(){
  ['klTooltip','klCrosshairX'].forEach(id=>{const el=byId(id); if(el) el.style.display='none';});
}

// ----- Loading (a ticker button, or opening the screen with nothing loaded) -----
async function fetchKeyLevelsCandles(symbol){
  const {interval, range} = KEY_LEVELS_CANDLES;
  const data = await fetchLiveData(`${MATRIX_CANDLES_URL}?symbol=${encodeURIComponent(symbol)}&interval=${interval}&range=${range}&t=${Date.now()}`);
  if(!data?.ok || !Array.isArray(data.candles)) return null;
  return data.candles.filter(c=>[c.time,c.open,c.high,c.low,c.close].every(Number.isFinite));
}
function renderKeyLevels(){
  if(activeView!==KEY_LEVELS_VIEW || !keyLevelsState) return;
  renderExpiryPicker('klDates', keyLevelsState.expiries, keyLevelsState.selected);
  drawKeyLevelsChart(keyLevelsState);
  const host = byId('forecast');
  if(host) host.innerHTML = forecastHtml(keyLevelsState.R);
}
async function loadKeyLevels(symbol, reuse=false){
  const request = ++_keyLevelsRequest;
  syncTickerInput(KEY_LEVELS_VIEW,symbol);
  setTickerStatus(KEY_LEVELS_VIEW,`Loading ${symbol}…`);
  const [result, candles] = await Promise.all([
    loadCoreData(reuse),
    fetchKeyLevelsCandles(symbol),
  ]);
  if(request!==_keyLevelsRequest) return;  // a newer click took over
  if(!REAL[symbol]){
    // Nothing to show: keep the ticker that is on screen.
    if(keyLevelsState) screenTickers[KEY_LEVELS_VIEW] = keyLevelsState.symbol;
    syncTickerInput(KEY_LEVELS_VIEW);
    setTickerStatus(KEY_LEVELS_VIEW,result.detail || `${symbol}: no data.`,'error');
    return;
  }
  const expiries = chainExpirations(buildChainReal(symbol));
  const kept = symbol===keyLevelsState?.symbol ? [...keyLevelsState.selected].filter(exp=>expiries.includes(exp)) : [];
  const selected = kept.length ? new Set(kept) : defaultExpirySelection(expiries);
  keyLevelsState = {symbol, R:keyLevelsGex(symbol, selected), candles:candles || [], expiries, selected};
  screenTickers[KEY_LEVELS_VIEW] = symbol;
  if(activeView===KEY_LEVELS_VIEW){
    syncTickerInput(KEY_LEVELS_VIEW);
    history.replaceState(null,'',screenUrl(KEY_LEVELS_VIEW));
  }
  renderKeyLevels();
  if(!result.ok) setTickerStatus(KEY_LEVELS_VIEW,`Refresh failed. Showing the last data for ${symbol}.`,'error');
  else if(!candles) setTickerStatus(KEY_LEVELS_VIEW,`${symbol}: price chart is unavailable right now.`,'error');
  else setTickerStatus(KEY_LEVELS_VIEW,'');
}
function showKeyLevels(){
  const symbol = screenTickers[KEY_LEVELS_VIEW];
  if(keyLevelsState?.symbol===symbol) renderKeyLevels();
  else loadKeyLevels(symbol, true);
}

// ---------- OI Volume ----------
// His OI Volume page: ticker buttons, one expiration date, five multi-select
// buttons over a per-strike chart, and a price chart with OI/volume strike lines.
const OIV_VIEW = 'oi-volume';
// Volume Spread = call volume minus put volume per strike, for the session so far
// (map #21, #24). It is our guess: no public source defines it. Bars in Net GEX colours.
const OIV_METRICS = {
  volume_spread: {label:'Volume Spread', color:'#22b5ff', kind:'bar',  val:s=>s.volumeSpread},
  call_oi:       {label:'Call OI',       color:'#02d432', kind:'area', val:s=>s.callOI},
  put_oi:        {label:'Put OI',        color:'#f32d35', kind:'area', val:s=>s.putOI},
  call_vol:      {label:'Call Vol',      color:'#003cfe', kind:'area', val:s=>s.callVol},
  put_vol:       {label:'Put Vol',       color:'#e55f04', kind:'area', val:s=>s.putVol},
  // Power Zone within 1% of spot, on its own scale like each area here.
  power:         {label:'Power Zone',    color:'#e8ca01', kind:'area', val:s=>s.powerZone||0, window:POWER_ZONE_WINDOW},
};
const OIV_ACTIVE = new Set(['volume_spread','power']);
const MATRIX_CANDLES_URL = '/api/matrix/candles';
// record: this screen's own copy of the chain, so other screens' ticker clicks leave it alone.
// selected: the chosen expirations (a Set), null until a ticker's first load.
const oiv = {symbol:null, record:null, expiries:[], selected:null, candles:null, candlesError:''};
let _oivRequest = 0;

// One expiration's chain summed per strike. No OI floor here: a strike that
// trades today but has little open interest still counts for volume.
function aggregateOiVolume(quotes){
  const byStrike = new Map();
  for(const q of quotes){
    if(!byStrike.has(q.K)) byStrike.set(q.K,{strike:q.K, callOI:0, putOI:0, callVol:0, putVol:0});
    const s = byStrike.get(q.K);
    const oi = Number(q.oi)||0, vol = Number(q.vol)||0;
    if(q.isCall){ s.callOI += oi; s.callVol += vol; }
    else { s.putOI += oi; s.putVol += vol; }
  }
  return [...byStrike.values()]
    .filter(s=>s.callOI||s.putOI||s.callVol||s.putVol)
    .map(s=>({...s, volumeSpread:s.callVol-s.putVol}))
    .sort((a,b)=>a.strike-b.strike);
}
function oiVolumeExpiries(chain){
  return chainExpirations(chain);
}
// expiries: one date or a Set of them, summed per strike.
function computeOiVolume(symbol, expiries, record=oiv.record){
  const chosen = expiries instanceof Set ? expiries : new Set([expiries]);
  const chain = buildChainRealFromRecord(symbol, record);
  const quotes = chain.quotes.filter(q=>chosen.has(q.exp));
  const scoped = {...chain, quotes};
  // Power Zone per strike from the GEX engine, the same as the Strike Chart.
  const pz = new Map(calcGEX(scoped).strikes.map(s=>[s.strike, s.powerZone]));
  const strikes = aggregateOiVolume(quotes).map(s=>({...s, powerZone:pz.get(s.strike)||0}));
  const dates = [...chosen].sort();
  const expiry = dates.length===1 ? dates[0] : `${dates.length} expirations`;
  return {symbol, spot:chain.spot, expiry, strikes, expectedMove:calcExpectedMove(scoped)};
}
// The strike of the largest value for each OI/volume series: these are the
// lines on his price chart. Which lines he draws is not public, so this is our guess.
function oiVolumePriceLines(strikes){
  const top = key=>strikes.reduce((best,s)=>s[key]>0 && (!best || s[key]>best[key]) ? s : best, null);
  return [
    ['Call OI','callOI',OIV_METRICS.call_oi.color],
    ['Put OI','putOI',OIV_METRICS.put_oi.color],
    ['Call Vol','callVol',OIV_METRICS.call_vol.color],
    ['Put Vol','putVol',OIV_METRICS.put_vol.color],
  ].map(([label,key,color])=>{ const s=top(key); return s && {label, color, price:s.strike}; }).filter(Boolean);
}
// The Matrix level (highest Power Zone within 1% of spot) for a price chart.
function matrixLine(strikes, spot){
  const s = topStrikes(strikes.filter(k=>Math.abs(k.strike-spot)/spot<=POWER_ZONE_WINDOW), k=>k.powerZone||0, 1)[0];
  return s ? {label:'Matrix', color:LEVEL_COLORS.Matrix, price:s.strike} : null;
}
function populateOiVolumeExpiries(symbol, record=oiv.record){
  oiv.expiries = oiVolumeExpiries(buildChainRealFromRecord(symbol, record));
  oiv.selected = oiv.selected
    ? new Set([...oiv.selected].filter(exp=>oiv.expiries.includes(exp)))
    : defaultExpirySelection(oiv.expiries);
  renderExpiryPicker('oivDates', oiv.expiries, oiv.selected);
}
function renderOiVolume(){
  const symbol = oiv.symbol;
  if(!symbol || !oiv.record) return;
  // The chain may have changed since the list was built: refill it, and fall
  // back to the nearest expiration when the chosen one is gone.
  populateOiVolumeExpiries(symbol);
  const R = computeOiVolume(symbol, oiv.selected);
  window._oivR = R;
  drawOiVolumeChart(R);
  drawOiVolumePriceChart(R);
}

function drawOiVolumeChart(R){
  const cv = byId('oiVolumeChart');
  if(!cv) return;
  const dpr = window.devicePixelRatio||1;
  const W = cv.clientWidth;
  const mobile = W < 640;
  const H = mobile ? Math.max(380, Math.min(520, window.innerHeight - 260)) : Math.max(520, Math.min(720, window.innerHeight - 230));
  cv.width=W*dpr; cv.height=H*dpr; cv.style.height=H+'px';
  const ctx = cv.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,W,H);
  cv._matrixBars = [];

  const active = Object.keys(OIV_METRICS).filter(m=>OIV_ACTIVE.has(m));
  const hasBar = OIV_ACTIVE.has('volume_spread');
  const barMetric = OIV_METRICS.volume_spread;
  const areas = active.filter(m=>OIV_METRICS[m].kind==='area');
  const symLabel = byId('oivSymLabel');
  const legend = byId('oivChartLegend');
  if(symLabel) symLabel.textContent = `${R.symbol} · ${R.expiry}`;
  if(legend) legend.innerHTML = active.map(m=>{
    const M=OIV_METRICS[m];
    return (M.kind==='bar'
      ? `<span class="sq" style="background:${M.color}"></span>`
      : `<span class="sq" style="background:${hexA(M.color,.5)};border-top:2px solid ${M.color}"></span>`)+M.label;
  }).join('&nbsp;&nbsp;') || '<span style="color:#7d8799">Choose a parameter</span>';

  const data = R.strikes.length ? visibleStrikeData(R) : [];
  if(!data.length) return;

  const padL=mobile?48:70, padR=mobile?(areas.length?42:18):(areas.length?62:42);
  const padT=mobile?28:36, padB=mobile?72:88;
  const plotW=W-padL-padR, plotH=H-padT-padB;
  const slot=plotW/data.length;
  const xCenter=i=>padL+slot*(i+0.5);
  const top=padT, bottom=padT+plotH;

  let lMin=0,lMax=1,yL=()=>bottom,y0=bottom;
  if(hasBar){
    const maxAbs=Math.max(...data.map(s=>Math.abs(barMetric.val(s))),1)*1.08;
    lMin=-maxAbs; lMax=maxAbs;
    yL=v=>top+(lMax-v)/(lMax-lMin)*plotH; y0=yL(0);
  }
  const ownMax={}; areas.forEach(m=>ownMax[m]=Math.max(...layerPoints(OIV_METRICS[m],data,R.spot).map(({s})=>OIV_METRICS[m].val(s)),1));
  const dom = areas.length ? areas.reduce((a,b)=>ownMax[b]>ownMax[a]?b:a,areas[0]) : null;
  const rMax = dom ? ownMax[dom]*1.08 : 1;

  ctx.font=(mobile?'bold 10px Segoe UI':'bold 13px Segoe UI'); ctx.textBaseline='middle';
  for(let t=0;t<=6;t++){
    const f=t/6, yy=bottom-f*plotH;
    ctx.strokeStyle='rgba(255,255,255,.025)'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(padL,yy); ctx.lineTo(W-padR,yy); ctx.stroke();
    ctx.fillStyle='#c4c4c4';
    if(hasBar){ ctx.textAlign='right'; ctx.fillText(fmtAxis(lMin+(lMax-lMin)*f), padL-8, yy); }
    if(dom){ ctx.textAlign='left'; ctx.fillText(fmtAxis(rMax*f), W-padR+8, yy); }
  }

  areas.forEach(m=>{
    const M=OIV_METRICS[m], own=ownMax[m];
    const yA=v=>bottom-(v/own)*0.92*plotH;
    // Power Zone keeps to its 1% window; the other areas span every strike.
    const pts=layerPoints(M,data,R.spot);
    if(!pts.length) return;
    ctx.beginPath(); ctx.moveTo(xCenter(pts[0].i),bottom);
    pts.forEach(({s,i})=>ctx.lineTo(xCenter(i),yA(M.val(s))));
    ctx.lineTo(xCenter(pts[pts.length-1].i),bottom); ctx.closePath();
    ctx.fillStyle=hexA(M.color,.33); ctx.fill();
    ctx.beginPath();
    pts.forEach(({s,i},k)=>{const x=xCenter(i),y=yA(M.val(s)); k?ctx.lineTo(x,y):ctx.moveTo(x,y);});
    ctx.strokeStyle=M.color; ctx.lineWidth=1.6; ctx.stroke();
    ctx.fillStyle=M.color;
    pts.forEach(({s,i})=>{ctx.beginPath(); ctx.arc(xCenter(i),yA(M.val(s)),1.8,0,7); ctx.fill();});
  });

  const hitBars = data.map((s,i)=>({x:padL+slot*i,y:top,w:slot,h:plotH,s}));
  if(hasBar){
    const bw=Math.max(2,Math.min(slot*0.70,32));
    data.forEach((s,i)=>{
      const v=barMetric.val(s), yy=yL(v);
      const x=xCenter(i)-bw/2;
      ctx.fillStyle = v>=0 ? '#22b5ff' : '#ff0000';
      ctx.fillRect(x, Math.min(yy,y0), bw, Math.max(1,Math.abs(yy-y0)));
    });
    ctx.strokeStyle='rgba(255,255,255,.18)'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(padL,y0); ctx.lineTo(W-padR,y0); ctx.stroke();
  }
  cv._matrixBars = hitBars;

  ctx.fillStyle='#c4c4c4'; ctx.font=(mobile?'bold 9px Segoe UI':'bold 11px Segoe UI');
  data.forEach((s,i)=>{
    ctx.save(); ctx.translate(xCenter(i),bottom+7); ctx.rotate(-Math.PI/2);
    ctx.textAlign='right'; ctx.textBaseline='middle'; ctx.fillText(fmtPrice(s.strike),0,0);
    ctx.restore();
  });

  // spot line
  const first=data[0].strike, last=data[data.length-1].strike;
  let px=padL+plotW/2;
  if(R.spot<=first) px=padL; else if(R.spot>=last) px=padL+plotW;
  else for(let i=0;i<data.length-1;i++){
    if(R.spot>=data[i].strike && R.spot<=data[i+1].strike){
      px=xCenter(i)+(R.spot-data[i].strike)/(data[i+1].strike-data[i].strike)*slot; break;
    }
  }
  ctx.strokeStyle='#d58b16'; ctx.lineWidth=2;
  ctx.beginPath(); ctx.moveTo(px,padT-6); ctx.lineTo(px,bottom); ctx.stroke();
  ctx.fillStyle='#d58b16'; ctx.font=(mobile?'bold 10px Segoe UI':'bold 12px Segoe UI'); ctx.textBaseline='alphabetic';
  const priceLabel='Price: '+R.spot.toFixed(2);
  const alignRight=px>W-ctx.measureText(priceLabel).width-12;
  ctx.textAlign=alignRight?'right':'left';
  ctx.fillText(priceLabel, px+(alignRight?-6:6), padT-9);

  ctx.fillStyle='#d0d0d0'; ctx.font=(mobile?'bold 11px Segoe UI':'bold 13px Segoe UI'); ctx.textAlign='center';
  if(hasBar){ ctx.save(); ctx.translate(14,padT+plotH/2); ctx.rotate(-Math.PI/2); ctx.fillText(barMetric.label,0,0); ctx.restore(); }
  if(dom){ ctx.save(); ctx.translate(W-12,padT+plotH/2); ctx.rotate(Math.PI/2); ctx.fillText(OIV_METRICS[dom].label,0,0); ctx.restore(); }
  ctx.textBaseline='bottom'; ctx.fillText('Strike',padL+plotW/2,H-4);
}

function showOiVolumeTooltip(ev){
  const cv=byId('oiVolumeChart'), tt=byId('oivTooltip'), cross=byId('oivCrosshairX');
  const bars=cv?._matrixBars||[];
  if(!tt || !cross || !bars.length) return;
  const rect=cv.getBoundingClientRect();
  const point=ev.touches && ev.touches[0] ? ev.touches[0] : ev;
  const x=point.clientX-rect.left, y=point.clientY-rect.top;
  const hit=bars.find(b=>x>=b.x && x<b.x+b.w);
  if(!hit){ hideOiVolumeTooltip(); return; }
  const s=hit.s;
  const row=(label,value,cls='')=>`<div class="tt-row"><span>${label}</span><span class="${cls}">${value}</span></div>`;
  const rows=Object.keys(OIV_METRICS).filter(m=>OIV_ACTIVE.has(m)).map(m=>{
    const v=OIV_METRICS[m].val(s);
    const cls=m==='volume_spread' ? (v>=0?'pos':'neg') : m==='power' ? '' : (m.startsWith('call')?'pos':'neg');
    return row(OIV_METRICS[m].label, fmtNum(v), cls);
  }).join('');
  tt.innerHTML=`<div class="tt-title">${esc(window._oivR?.symbol||'')} Strike ${fmtPrice(s.strike)}</div>${rows || row('No metric selected','')}`;
  tt.style.display='block';
  cross.style.display='block';
  cross.style.left=Math.max(0,Math.min(rect.width,hit.x+hit.w/2))+'px';
  const tw=tt.offsetWidth, th=tt.offsetHeight;
  let left=x+14, top=y+14;
  if(left+tw>rect.width) left=x-tw-14;
  if(top+th>rect.height) top=y-th-14;
  tt.style.left=Math.max(6,left)+'px';
  tt.style.top=Math.max(6,top)+'px';
}
function hideOiVolumeTooltip(){
  ['oivTooltip','oivCrosshairX'].forEach(id=>{const el=byId(id); if(el) el.style.display='none';});
}

// Intraday candles for the ticker, with the OI/volume strike lines of the chosen date.
function drawOiVolumePriceChart(R){
  const cv=byId('oiVolumePriceChart');
  if(!cv) return;
  const empty=byId('oivPriceEmpty');
  const label=byId('oivPriceLabel');
  const legend=byId('oivPriceLegend');
  const lines=[matrixLine(R.strikes,R.spot), ...oiVolumePriceLines(R.strikes)].filter(Boolean);
  if(label) label.textContent=`${R.symbol} Price`;
  if(legend) legend.innerHTML=lines.map(l=>`<span class="sq" style="background:${l.color}"></span>${esc(l.label)} ${fmtPrice(l.price)}`).join('&nbsp;&nbsp;');
  const dpr=window.devicePixelRatio||1;
  const W=cv.clientWidth;
  const mobile=W<640;
  const H=mobile?320:420;
  cv.width=W*dpr; cv.height=H*dpr; cv.style.height=H+'px';
  const ctx=cv.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,W,H);
  cv._oivPrice=null;
  const candles=oiv.candles||[];
  if(empty){
    empty.textContent=candles.length ? '' : (oiv.candlesError || `No price data for ${R.symbol}`);
    empty.classList.toggle('show',!candles.length);
  }
  if(!candles.length) return;

  // Lines widen the price range so every one stays on the chart.
  const lo=Math.min(...candles.map(c=>c.low), ...lines.map(l=>l.price));
  const hi=Math.max(...candles.map(c=>c.high), ...lines.map(l=>l.price));
  const pad=Math.max((hi-lo)*0.04, hi*0.0005);
  const yMin=lo-pad, yMax=hi+pad;
  const padL=mobile?8:16, padR=mobile?58:74, padT=16, padB=30;
  const plotW=W-padL-padR, plotH=H-padT-padB;
  const y=v=>padT+(yMax-v)/(yMax-yMin)*plotH;
  const step=plotW/candles.length;
  const x=i=>padL+step*(i+0.5);

  ctx.font=(mobile?'bold 10px Segoe UI':'bold 12px Segoe UI'); ctx.textBaseline='middle'; ctx.textAlign='left';
  for(let t=0;t<=5;t++){
    const v=yMin+(yMax-yMin)*t/5, yy=y(v);
    ctx.strokeStyle='rgba(255,255,255,.035)'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(padL,yy); ctx.lineTo(W-padR,yy); ctx.stroke();
    ctx.fillStyle='#c4c4c4'; ctx.fillText(fmtPrice(v), W-padR+6, yy);
  }
  cv._oivPrice={candles, padL, slot:step, top:padT, bottom:padT+plotH, plotW, symbol:R.symbol};
  // Power Zone sideways from the left edge, under the candles.
  drawPowerZoneSideways(ctx, R.strikes.filter(k=>Math.abs(k.strike-R.spot)/R.spot<=POWER_ZONE_WINDOW),
    {left:padL, width:plotW*0.25, y, lo:yMin, hi:yMax, top:padT, bottom:padT+plotH});
  const bw=Math.max(1,Math.min(step*0.7,9));
  candles.forEach((c,i)=>{
    const color=c.close>=c.open ? '#17b65f' : '#ff2a17';
    ctx.strokeStyle=color; ctx.fillStyle=color; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(x(i),y(c.high)); ctx.lineTo(x(i),y(c.low)); ctx.stroke();
    ctx.fillRect(x(i)-bw/2, Math.min(y(c.open),y(c.close)), bw, Math.max(1,Math.abs(y(c.open)-y(c.close))));
  });
  lines.forEach(l=>{
    const yy=y(l.price);
    ctx.strokeStyle=l.color; ctx.lineWidth=1.5; ctx.setLineDash([6,4]);
    ctx.beginPath(); ctx.moveTo(padL,yy); ctx.lineTo(W-padR,yy); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle=l.color; ctx.textAlign='left'; ctx.textBaseline='bottom';
    ctx.fillText(`${l.label} ${fmtPrice(l.price)}`, padL+4, yy-2);
  });
  // time axis: New York clock, about six labels
  const fmtTime=new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
  ctx.fillStyle='#c4c4c4'; ctx.textAlign='center'; ctx.textBaseline='top';
  const every=Math.max(1,Math.ceil(candles.length/6));
  for(let i=0;i<candles.length;i+=every){
    ctx.textAlign=i===0?'left':'center';
    ctx.fillText(fmtTime.format(new Date(candles[i].time*1000)), i===0?padL:x(i), H-padB+8);
  }
}

// Hover on the price chart: the candle under the cursor, like the other screens' price charts.
function showOiVolumePriceTooltip(ev){
  const cv=byId('oiVolumePriceChart'), tt=byId('oivPriceTooltip'), cross=byId('oivPriceCrosshairX');
  const plot=cv?._oivPrice;
  if(!tt || !cross || !plot) return;
  const rect=cv.getBoundingClientRect();
  const point=ev.touches && ev.touches[0] ? ev.touches[0] : ev;
  const x=point.clientX-rect.left, y=point.clientY-rect.top;
  if(x<plot.padL || x>plot.padL+plot.plotW || y<plot.top || y>plot.bottom){ hideOiVolumePriceTooltip(); return; }
  const i=Math.max(0,Math.min(plot.candles.length-1,Math.floor((x-plot.padL)/plot.slot)));
  const c=plot.candles[i];
  const row=(label,value)=>`<div class="tt-row"><span>${label}</span><span>${value}</span></div>`;
  tt.innerHTML=`<div class="tt-title">${esc(plot.symbol)} ${fmtMarketTime(c.time)} ET</div>`
    +row('Open',fmtPrice(c.open))+row('High',fmtPrice(c.high))+row('Low',fmtPrice(c.low))+row('Close',fmtPrice(c.close));
  tt.style.display='block';
  cross.style.display='block';
  cross.style.left=(plot.padL+plot.slot*(i+0.5))+'px';
  const tw=tt.offsetWidth, th=tt.offsetHeight;
  let left=x+14, top=y+14;
  if(left+tw>rect.width) left=x-tw-14;
  if(top+th>rect.height) top=y-th-14;
  tt.style.left=Math.max(6,left)+'px';
  tt.style.top=Math.max(6,top)+'px';
}
function hideOiVolumePriceTooltip(){
  ['oivPriceTooltip','oivPriceCrosshairX'].forEach(id=>{const el=byId(id); if(el) el.style.display='none';});
}

async function loadOiVolumeCandles(symbol){
  const data=await fetchLiveData(`${MATRIX_CANDLES_URL}?symbol=${encodeURIComponent(symbol)}&interval=1m&range=1d`);
  // Regular session (09:30-16:00 ET) of the last trading day, as on the Strike Chart.
  const candles=latestSessionCandles(Array.isArray(data?.candles) ? data.candles : []);
  if(!candles.length) return {candles:null, error:`No intraday prices for ${symbol}`};
  return {candles, error:''};
}
// Refresh of OI Volume: a ticker button, or opening it with no data yet.
async function refreshOiVolume(symbol, reuse=false){
  const request=++_oivRequest;
  const previous=oiv.symbol;
  syncTickerInput(OIV_VIEW,symbol);
  setTickerStatus(OIV_VIEW,`Loading ${symbol}…`);
  if(!oiv.record) showDatesLoading('oivDates','<span class="xp-note">Loading dates…</span>');
  const pricePromise=loadOiVolumeCandles(symbol);
  const result=await loadCoreData(reuse);
  if(request!==_oivRequest) return;  // a newer click took over
  if(!REAL[symbol]){
    setTickerStatus(OIV_VIEW,result.detail,'error');
    if(previous) syncTickerInput(OIV_VIEW,previous);
    return;
  }
  if(symbol!==previous) oiv.selected=null;
  const record=REAL[symbol];
  // The date list fills as soon as the chain is in; the charts wait for the candles.
  populateOiVolumeExpiries(symbol, record);
  const price=await pricePromise;
  if(request!==_oivRequest) return;
  oiv.symbol=symbol;
  oiv.record=record;
  oiv.candles=price.candles;
  oiv.candlesError=price.error;
  screenTickers[OIV_VIEW]=symbol;
  if(activeView===OIV_VIEW) history.replaceState(null,'',screenUrl(OIV_VIEW));
  renderOiVolume();
  setTickerStatus(OIV_VIEW,result.ok?'':`Refresh failed. Showing the last data for ${symbol}.`,result.ok?'':'error');
}

// ---------- GEX Board ("GEX Analysis") ----------
// His GEX board (map #21, #29, #36), as his public callback graph and a screenshot from
// one of his videos show it: a "Select Ticker:" card (SPX, SPY, QQQ), then the "Options
// Chain Board" with a summary line, one button per expiry (date and its share of the
// gamma), Select All / Clear All, the strike table, and three charts in a 480px column
// (Net Gamma, Gamma Split, price), each with a full-screen button. The 5% table window
// and the 10% chart window are guesses.
const GB_VIEW = 'gex-analysis';
const GB_TICKERS = SCREEN_TICKERS;
screenTickers[GB_VIEW] = GB_TICKERS[0];
const GB_WINDOW = 0.05;         // the table: strikes within 5% of spot
const GB_CHART_WINDOW = 0.10;   // Net Gamma and Gamma Split: his draw a much wider range
// Price Lines further than this from spot stay in the legend but off the price chart,
// so one far strike cannot squash a day of candles into a line.
const GB_PRICE_LINE_DISTANCE = 0.015;
const GB_COLUMNS = [
  {key:'strike',        label:'Strike'},
  {key:'netGex',        label:'Net GEX', signed:true},
  {key:'ag',            label:'Abs GEX'},
  {key:'powerZone',     label:'Power Zone'},
  {key:'notionalDelta', label:'Notional Delta', usd:true},
  {key:'totalOI',       label:'Total OI'},
  {key:'callOI',        label:'Call OI'},
  {key:'putOI',         label:'Put OI'},
  {key:'totalVol',      label:'Total Vol'},
  {key:'callVol',       label:'cVol'},
  {key:'putVol',        label:'pVol'},
  {key:'pct',           label:'%', signed:true, pct:true},
];
// Columns whose largest value is marked, like his: a dot on the cell and the whole row
// shaded. Net GEX counts by size, so its most negative strike can be the one.
const GB_MAX_COLUMNS = ['netGex','ag','powerZone','notionalDelta','totalOI','callOI','putOI','totalVol','callVol','putVol'];
// Text colours per column, as in his screenshot: calls blue, puts red, signed columns by sign.
const GB_COLUMN_CLASS = {powerZone:'gb-pz', ag:'gb-call', callOI:'gb-call', callVol:'gb-call', putOI:'gb-put', putVol:'gb-put', totalOI:'gb-strong'};
const GB_CHARTS = {
  Total: {canvas:'gbTotalChart', wrap:'gbTotalWrap'},
  Split: {canvas:'gbSplitChart', wrap:'gbSplitWrap'},
  Price: {canvas:'gbPriceChart', wrap:'gbPriceWrap'},
};
// The screen's own copy of its chain, so a ticker click on another screen leaves it alone.
const gb = {symbol:null, record:null, candles:null, candlesError:'', expiries:[], shares:new Map(), selected:new Set(), sort:{key:'strike', dir:'desc'}};
let _gbRequest = 0;

// ----- Numbers -----
// Per-strike rows for a chain already cut to the selected expiries. Only contracts with
// no open interest are dropped: the follower's board matched a reference board's strike
// list exactly that way (Addendum 5). The Strike Chart's calcGEX keeps its own floor.
function gexBoardStrikes(chain){
  const {spot, mult} = chain;
  const byStrike = new Map();
  for(const q of chain.quotes){
    if(!(q.oi > 0)) continue;
    const gex = q.g.gamma * q.oi * mult * spot * spot * 0.01;
    const delta = Number.isFinite(q.g.delta) ? q.g.delta
      : calcGreeks(spot, q.K, q.T, q.iv>0 ? q.iv : 0.2, q.rate ?? RISK_FREE, q.isCall).delta;
    if(!byStrike.has(q.K)) byStrike.set(q.K,{strike:q.K, callGex:0, putGex:0, callOI:0, putOI:0,
      callVol:0, putVol:0, notionalDelta:0});
    const s = byStrike.get(q.K);
    const vol = Number(q.vol)||0;
    if(q.isCall){ s.callGex += gex; s.callOI += q.oi; s.callVol += vol; }
    else { s.putGex -= gex; s.putOI += q.oi; s.putVol += vol; }
    // Notional Delta: calls and puts both counted as positive, like AG.
    s.notionalDelta += Math.abs(delta) * q.oi * mult * spot;
  }
  const rows = [...byStrike.values()].map(s=>{
    const ag = Math.abs(s.callGex)+Math.abs(s.putGex), totalVol = s.callVol+s.putVol;
    // Power Zone, the same formula as the Strike Chart (calcGEX).
    return {...s, netGex:s.callGex+s.putGex, ag, totalOI:s.callOI+s.putOI, totalVol,
      powerZone: ag/8 + totalVol*spot*spot/20000};
  });
  const totalAg = rows.reduce((sum,s)=>sum+s.ag,0);
  rows.forEach(s=>{ s.pct = totalAg ? s.netGex/totalAg*100 : 0; });
  return rows.sort((a,b)=>a.strike-b.strike);
}

function computeGexBoard(symbol, record=gb.record, selected=gb.selected){
  const full = buildChainRealFromRecord(symbol, record);
  const chain = {...full, quotes:full.quotes.filter(q=>selected.has(q.exp))};
  const R = calcGEX(chain);
  const spot = chain.spot;
  const all = gexBoardStrikes(chain);
  const rows = all.filter(s=>Math.abs(s.strike-spot)/spot<=GB_WINDOW);
  const chartRows = all.filter(s=>Math.abs(s.strike-spot)/spot<=GB_CHART_WINDOW);
  // Total Gamma comes from the board's own rows, so it matches the table.
  const totalGex = all.reduce((sum,s)=>sum+s.netGex,0);
  const lines = priceLines(rows, spot);
  // Each strike's levels ("P1", "AG Strike", "Matrix"), shown in the table.
  const levelsAt = new Map();
  lines.forEach(l=>levelsAt.set(l.price, [...(levelsAt.get(l.price)||[]), l]));
  const pzStrikes = rows.filter(s=>Math.abs(s.strike-spot)/spot<=POWER_ZONE_WINDOW);
  return {symbol, spot, R, rows, chartRows, totalGex, expiries:[...selected].sort(), lines, levelsAt, pzStrikes};
}
// Each expiry's share of the gamma (AG) over all listed expiries, in percent: his expiry
// buttons carry one, summing to 100%, with far-dated expiries near 0% (a guess at the base).
function gexBoardExpiryShares(chain){
  const {spot, mult} = chain;
  const ag = new Map();
  chain.quotes.forEach(q=>{
    if(!(q.oi > 0)) return;
    ag.set(q.exp, (ag.get(q.exp)||0) + Math.abs(q.g.gamma * q.oi * mult * spot * spot * 0.01));
  });
  const total = [...ag.values()].reduce((sum,v)=>sum+v,0);
  return new Map([...ag].map(([exp,v])=>[exp, total ? v/total*100 : 0]));
}
function nearestStrike(rows, value){
  if(!Number.isFinite(value) || !rows.length) return null;
  return rows.reduce((best,s)=>Math.abs(s.strike-value)<Math.abs(best.strike-value) ? s : best).strike;
}
// ----- Formatting -----
function fmtStrike(v){
  return Number.isInteger(v) ? v.toLocaleString('en-US') : v.toLocaleString('en-US',{maximumFractionDigits:2});
}
// Whole numbers with separators, like his ("-906,337,011", "$1,871,716,994").
function fmtBoardCell(col, v){
  if(col.key==='strike') return fmtStrike(v);
  if(col.pct) return `${v>=0?'+':''}${v.toFixed(2)}%`;
  const whole = Math.round(v).toLocaleString('en-US');
  return col.usd ? (v<0 ? '-$'+whole.slice(1) : '$'+whole) : whole;
}
function fmtGammaTotal(v){
  const a = Math.abs(v), sign = v>=0 ? '+' : '-';
  return a>=1e9 ? `$${sign}${(a/1e9).toFixed(2)} Bn` : `$${sign}${(a/1e6).toFixed(1)} Mn`;
}

// ----- Controls -----
function setGbStatus(text='', state=''){
  const el = byId('gbStatus');
  if(!el) return;
  el.textContent = text;
  el.className = `gb-status${state?' '+state:''}`;
}
function renderGexBoardButtons(){
  const tickers = byId('gbTickers');
  const shown = screenTickers[GB_VIEW];
  if(tickers) tickers.innerHTML = GB_TICKERS.map(t=>
    `<button type="button" class="gb-btn${t===shown?' active':''}" data-gb-ticker="${t}" aria-pressed="${t===shown}">${t}</button>`).join('');
  // The shared date picker, each date with its share of the gamma.
  if(gb.symbol) renderExpiryPicker('gbDates', gb.expiries, gb.selected, gb.shares);
}

// ----- Spot line and table -----
function renderGexBoardSpot(B){
  const el = byId('gbSpot');
  if(!el) return;
  // His line: "Spot: 767.18 | Total Gamma: $-1.39 Bn | Regime: Negative | Ticker: SPY".
  const spot = `Spot: ${B.spot.toFixed(2)}`, ticker = `Ticker: ${B.symbol}`;
  el.textContent = B.expiries.length
    ? [spot, `Total Gamma: ${fmtGammaTotal(B.totalGex)}`, `Regime: ${B.totalGex>=0 ? 'Positive' : 'Negative'}`, ticker].join(' | ')
    : [spot, 'Select at least one expiration', ticker].join(' | ');
}
function sortedBoardRows(rows){
  const {key, dir} = gb.sort, sign = dir==='asc' ? 1 : -1;
  return [...rows].sort((a,b)=>sign*(a[key]-b[key]) || b.strike-a.strike);
}
function renderGexBoardTable(B, scrollToSpot){
  const head = byId('gbTableHead'), body = byId('gbTableBody');
  if(!head || !body) return;
  head.innerHTML = GB_COLUMNS.map(col=>{
    const sorted = gb.sort.key===col.key;
    const arrow = sorted ? (gb.sort.dir==='asc' ? ' ▲' : ' ▼') : '';
    return `<th scope="col" data-gb-sort="${col.key}"${sorted?` aria-sort="${gb.sort.dir==='asc'?'ascending':'descending'}"`:''}>${esc(col.label)}${arrow}</th>`;
  }).join('');
  if(!B.rows.length){
    const why = B.expiries.length ? 'No strikes within 5% of spot' : 'Select at least one expiration';
    body.innerHTML = `<tr><td class="gb-empty" colspan="${GB_COLUMNS.length}">${why}</td></tr>`;
    return;
  }
  const spotStrike = nearestStrike(B.rows, B.spot);
  const size = (key,v)=>key==='netGex' ? Math.abs(v) : v;
  const colMax = Object.fromEntries(GB_MAX_COLUMNS.map(k=>[k, Math.max(...B.rows.map(s=>size(k,s[k])))]));
  const isMax = (key,s)=>GB_MAX_COLUMNS.includes(key) && colMax[key]>0 && size(key,s[key])===colMax[key];
  // A row is shaded when it holds a level (P1, N1, AG Strike, Call/Put Vol, Matrix)
  // or the largest value of a column; tags beside the strike name its levels.
  body.innerHTML = sortedBoardRows(B.rows).map(s=>{
    const levels = B.levelsAt.get(s.strike) || [];
    const marked = levels.length || GB_MAX_COLUMNS.some(k=>isMax(k,s));
    const cells = GB_COLUMNS.map(col=>{
      const v = s[col.key];
      const cls = [];
      if(col.signed) cls.push(v>=0 ? 'pos' : 'neg');
      else if(GB_COLUMN_CLASS[col.key]) cls.push(GB_COLUMN_CLASS[col.key]);
      const dot = isMax(col.key,s) ? '<span class="gb-dot" aria-label="largest"></span>' : '';
      const tags = col.key==='strike'
        ? levels.map(l=>`<span class="gb-tag" style="--c:${l.color}">${esc(l.label)}</span>`).join('') : '';
      return `<td${cls.length?` class="${cls.join(' ')}"`:''}>${esc(fmtBoardCell(col, v))}${tags}${dot}</td>`;
    }).join('');
    return `<tr data-strike="${s.strike}"${marked?' class="gb-max-row"':''}>${cells}</tr>`;
  }).join('');
  if(scrollToSpot){
    const wrap = byId('gbTableWrap');
    const row = body.querySelector?.(`tr[data-strike="${spotStrike}"]`);
    if(wrap && row) wrap.scrollTop = Math.max(0, row.offsetTop - wrap.clientHeight/2);
  }
}

// ----- Charts -----
function gbCanvas(key){
  const cv = byId(GB_CHARTS[key].canvas);
  const wrap = byId(GB_CHARTS[key].wrap);
  if(!cv || !cv.clientWidth) return null;
  const full = wrap?.classList.contains('gb-full');
  const W = cv.clientWidth, mobile = W<640;
  const H = full ? Math.max(320, window.innerHeight-80) : (mobile ? 320 : 380);
  const dpr = window.devicePixelRatio||1;
  cv.width = W*dpr; cv.height = H*dpr; cv.style.height = H+'px';
  const ctx = cv.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,W,H);
  cv._gb = null;
  return {cv, ctx, W, H, mobile};
}
function setGbEmpty(key, text){
  const el = byId(`gb${key}Empty`);
  if(!el) return;
  el.textContent = text || '';
  el.classList.toggle('show', !!text);
}
// Net Gamma (Net GEX per strike) and Gamma Split (call gamma up, put gamma down), each
// with an Abs GEX line, like the two charts in his board screenshot: a title with the
// total "per 1% <ticker> Move", gridlines, a y axis in $ billions and an orange spot line.
const GB_ABS_COLOR = '#915bf8', GB_CALL_COLOR = '#02d432', GB_PUT_COLOR = '#ff0000', GB_POS_COLOR = '#22b5ff';
function gexBoardChartTitle(key, B){
  const total = `${fmtGammaTotal(B.totalGex)} per 1% ${B.symbol} Move`;
  return key==='Split' ? `Gamma Split - Total: ${total}` : `Net Gamma: ${total}`;
}
function fmtBillions(v, step){
  const digits = step>=0.5e9 ? 1 : step>=0.05e9 ? 2 : 3;
  return (v/1e9).toFixed(digits);
}
function drawGexBoardBars(key, B){
  const c = gbCanvas(key);
  if(!c) return;
  const {cv, ctx, W, H, mobile} = c;
  const split = key==='Split';
  const title = byId(`gb${key}Title`);
  if(title) title.textContent = B.expiries.length ? gexBoardChartTitle(key, B) : (split ? 'Gamma Split' : 'Net Gamma');
  const legend = byId(`gb${key}Legend`);
  const line = `<span><i class="gb-line" style="border-color:${GB_ABS_COLOR}"></i>Abs GEX</span>`;
  const sq = (color,label)=>`<span><span class="sq" style="background:${color}"></span>${label}</span>`;
  if(legend) legend.innerHTML = split
    ? line + sq(GB_PUT_COLOR,'Put Gamma') + sq(GB_CALL_COLOR,'Call Gamma')
    : line + sq(GB_POS_COLOR,'Net Gamma') + `<span><i class="gb-line" style="border-color:#e8ca01"></i>Power Zone</span>`;
  const data = B.chartRows;
  setGbEmpty(key, data.length ? '' : (B.expiries.length ? 'No strikes within 10% of spot' : 'Select at least one expiration'));
  if(!data.length) return;

  const padL = mobile?52:62, padR = 10, padT = 22, padB = mobile?50:56;
  const plotW = W-padL-padR, plotH = H-padT-padB;
  const top = padT, bottom = padT+plotH;
  const slot = plotW/data.length;
  const xCenter = i=>padL+slot*(i+0.5);
  const up = s=>split ? s.callGex : Math.max(s.netGex,0);
  const down = s=>split ? s.putGex : Math.min(s.netGex,0);
  const inPz = s=>!split && Math.abs(s.strike-B.spot)/B.spot<=POWER_ZONE_WINDOW && s.powerZone>0;
  const maxAbs = Math.max(...data.map(s=>Math.max(up(s), -down(s), inPz(s) ? s.powerZone : 0)), 1)*1.08;
  const y = v=>top+(maxAbs-v)/(2*maxAbs)*plotH;
  const y0 = y(0);
  // Abs GEX has its own scale from the bottom of the chart, as in his.
  const agMax = Math.max(...data.map(s=>s.ag), 1)*1.05;
  const yAg = v=>bottom-(v/agMax)*plotH;

  // gridlines, like his board charts
  ctx.font = mobile ? '9px Segoe UI' : '10px Segoe UI';
  ctx.textBaseline = 'middle'; ctx.textAlign = 'right';
  ctx.strokeStyle = 'rgba(255,255,255,.09)'; ctx.lineWidth = 1;
  const step = maxAbs/2;
  for(let t=-2;t<=2;t++){
    const v = step*t, yy = y(v);
    ctx.beginPath(); ctx.moveTo(padL,yy); ctx.lineTo(W-padR,yy); ctx.stroke();
    ctx.fillStyle = '#c4c4c4'; ctx.fillText(fmtBillions(v, step), padL-6, yy);
  }
  const every = Math.max(1, Math.ceil(14/Math.max(slot,1)));
  data.forEach((s,i)=>{
    if(i%every) return;
    ctx.beginPath(); ctx.moveTo(xCenter(i),top); ctx.lineTo(xCenter(i),bottom); ctx.stroke();
  });

  // Abs GEX: a filled line with markers behind the bars
  const pts = data.map((s,i)=>[xCenter(i), yAg(s.ag)]);
  ctx.beginPath(); ctx.moveTo(pts[0][0],bottom);
  pts.forEach(([x,yy])=>ctx.lineTo(x,yy));
  ctx.lineTo(pts[pts.length-1][0],bottom); ctx.closePath();
  ctx.fillStyle = hexA(GB_ABS_COLOR,.35); ctx.fill();
  ctx.beginPath(); pts.forEach(([x,yy],i)=>i ? ctx.lineTo(x,yy) : ctx.moveTo(x,yy));
  ctx.strokeStyle = GB_ABS_COLOR; ctx.lineWidth = 1.4; ctx.stroke();
  ctx.fillStyle = GB_ABS_COLOR;
  if(slot>=3) pts.forEach(([x,yy])=>{ ctx.beginPath(); ctx.arc(x,yy,1.6,0,7); ctx.fill(); });

  const bw = Math.max(1, Math.min(slot*0.8, 18));
  data.forEach((s,i)=>{
    const x = xCenter(i)-bw/2;
    const bar = (v,color)=>{ if(!v) return; const yy=y(v); ctx.fillStyle=color; ctx.fillRect(x, Math.min(yy,y0), bw, Math.max(1, Math.abs(yy-y0))); };
    if(split){ bar(s.callGex, GB_CALL_COLOR); bar(s.putGex, GB_PUT_COLOR); }
    else bar(s.netGex, s.netGex>=0 ? GB_POS_COLOR : GB_PUT_COLOR);
  });
  // Power Zone (Net Gamma chart), translucent over the bars like his: yellow, on the Net GEX scale (that is what its /8 is
  // for), within 1% of spot like his Strike Chart.
  if(!split){
    const pz = data.map((s,i)=>({s,i})).filter(({s})=>inPz(s));
    if(pz.length>1){
      const pzPts = pz.map(({s,i})=>[xCenter(i), y(s.powerZone)]);
      ctx.beginPath(); ctx.moveTo(pzPts[0][0],y0);
      pzPts.forEach(([x,yy])=>ctx.lineTo(x,yy));
      ctx.lineTo(pzPts[pzPts.length-1][0],y0); ctx.closePath();
      ctx.fillStyle = 'rgba(232,202,1,.35)'; ctx.fill();
      ctx.beginPath(); pzPts.forEach(([x,yy],i)=>i ? ctx.lineTo(x,yy) : ctx.moveTo(x,yy));
      ctx.strokeStyle = '#e8ca01'; ctx.lineWidth = 1.8; ctx.stroke();
    }
  }
  ctx.strokeStyle = 'rgba(255,255,255,.25)'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(padL,y0); ctx.lineTo(W-padR,y0); ctx.stroke();

  // strike labels, thinned so they never overlap, and the axis titles
  ctx.fillStyle = '#c4c4c4';
  data.forEach((s,i)=>{
    if(i%every) return;
    ctx.save(); ctx.translate(xCenter(i), bottom+6); ctx.rotate(-Math.PI/2);
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle'; ctx.fillText(fmtStrike(s.strike),0,0);
    ctx.restore();
  });
  ctx.fillStyle = '#d0d0d0'; ctx.font = mobile ? '10px Segoe UI' : '11px Segoe UI';
  ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
  ctx.fillText('Strike', padL+plotW/2, H-2);
  ctx.save(); ctx.translate(11, top+plotH/2); ctx.rotate(-Math.PI/2); ctx.textBaseline = 'middle';
  ctx.fillText('Gamma Exposure ($ billions / 1% move)', 0, 0); ctx.restore();

  // spot line, labelled "Spot: <price>" at its top
  const first = data[0].strike, last = data[data.length-1].strike;
  let px = padL+plotW/2;
  if(B.spot<=first) px = padL; else if(B.spot>=last) px = padL+plotW;
  else for(let i=0;i<data.length-1;i++){
    if(B.spot>=data[i].strike && B.spot<=data[i+1].strike){ px = xCenter(i)+(B.spot-data[i].strike)/(data[i+1].strike-data[i].strike)*slot; break; }
  }
  ctx.strokeStyle = '#ffa500'; ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.moveTo(px,top); ctx.lineTo(px,bottom); ctx.stroke();
  const label = `Spot: ${B.spot.toFixed(2)}`;
  ctx.font = mobile ? 'bold 10px Segoe UI' : 'bold 11px Segoe UI'; ctx.fillStyle = '#ffa500'; ctx.textBaseline = 'bottom';
  ctx.textAlign = px>W-ctx.measureText(label).width-12 ? 'right' : (px<padL+ctx.measureText(label).width/2 ? 'left' : 'center');
  ctx.fillText(label, px, top-3);
  cv._gb = {kind:'bars', data, padL, plotW, slot, top, bottom, split, B};
}
function drawGexBoardPrice(B){
  const c = gbCanvas('Price');
  if(!c) return;
  const {cv, ctx, W, H, mobile} = c;
  const legend = byId('gbPriceLegend');
  const onChart = l=>Math.abs(l.price-B.spot)/B.spot<=GB_PRICE_LINE_DISTANCE;
  if(legend) legend.innerHTML = B.lines.map(l=>`<span><span class="sq" style="background:${l.color}"></span>${esc(l.chartLabel)} ${esc(fmtLineValue(l.price))}${onChart(l)?'':' (off chart)'}</span>`).join('');
  const groups = groupLines(B.lines.filter(onChart));
  const candles = gb.candles || [];
  setGbEmpty('Price', candles.length ? '' : (gb.candlesError || `No intraday prices for ${B.symbol}`));
  if(!candles.length) return;

  const padL = 8, padR = mobile?54:62, padT = 12, padB = 26;
  const plotW = W-padL-padR, plotH = H-padT-padB;
  const lo0 = Math.min(...candles.map(k=>k.low), ...groups.map(g=>g.price));
  const hi0 = Math.max(...candles.map(k=>k.high), ...groups.map(g=>g.price));
  const pad = Math.max((hi0-lo0)*0.04, hi0*0.0005);
  const lo = lo0-pad, hi = hi0+pad;
  const y = v=>padT+(hi-v)/(hi-lo)*plotH;
  const slot = plotW/candles.length;
  const x = i=>padL+slot*(i+0.5);

  ctx.font = mobile ? 'bold 9px Segoe UI' : 'bold 10px Segoe UI';
  ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
  for(let t=0;t<=5;t++){
    const v = lo+(hi-lo)*t/5, yy = y(v);
    ctx.strokeStyle = 'rgba(255,255,255,.035)'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL,yy); ctx.lineTo(W-padR,yy); ctx.stroke();
    ctx.fillStyle = '#c4c4c4'; ctx.fillText(fmtPrice(v), W-padR+5, yy);
  }
  const bw = Math.max(1, Math.min(slot*0.7, 7));
  candles.forEach((k,i)=>{
    const color = k.close>=k.open ? '#17b65f' : '#ff2a17';
    ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x(i),y(k.high)); ctx.lineTo(x(i),y(k.low)); ctx.stroke();
    ctx.fillRect(x(i)-bw/2, Math.min(y(k.open),y(k.close)), bw, Math.max(1, Math.abs(y(k.open)-y(k.close))));
  });
  // Power Zone sideways from the left, then the Price Lines labelled on the right.
  drawPowerZoneSideways(ctx, B.pzStrikes, {left:padL, width:plotW*0.25, y, lo, hi, top:padT, bottom:padT+plotH});
  drawLineGroups(ctx, groups, {y, x0:padL, x1:W-padR, mobile});
  ctx.font = mobile ? 'bold 9px Segoe UI' : 'bold 10px Segoe UI';
  ctx.fillStyle = '#c4c4c4'; ctx.textBaseline = 'top';
  const every = Math.max(1, Math.ceil(candles.length/(mobile?4:5)));
  for(let i=0;i<candles.length;i+=every){
    ctx.textAlign = i===0 ? 'left' : 'center';
    ctx.fillText(fmtMarketTime(candles[i].time), i===0 ? padL : x(i), padT+plotH+7);
  }
  cv._gb = {kind:'price', candles, padL, plotW, slot, top:padT, bottom:padT+plotH, symbol:B.symbol};
}
function drawGexBoardCharts(B){
  drawGexBoardBars('Total', B);
  drawGexBoardBars('Split', B);
  drawGexBoardPrice(B);
}
function showGexBoardTooltip(ev){
  const cv = ev.currentTarget;
  const key = Object.keys(GB_CHARTS).find(k=>GB_CHARTS[k].canvas===cv?.id);
  const info = cv?._gb;
  const tt = byId(`gb${key}Tooltip`), cross = byId(`gb${key}CrosshairX`);
  if(!info || !tt || !cross) return;
  const rect = cv.getBoundingClientRect();
  const point = ev.touches && ev.touches[0] ? ev.touches[0] : ev;
  const x = point.clientX-rect.left, yPos = point.clientY-rect.top;
  const list = info.kind==='price' ? info.candles : info.data;
  if(x<info.padL || x>info.padL+info.plotW || yPos<info.top || yPos>info.bottom){ hideGexBoardTooltips(); return; }
  const i = Math.max(0, Math.min(list.length-1, Math.floor((x-info.padL)/info.slot)));
  const row = (label,value,cls='')=>`<div class="tt-row"><span>${label}</span><span class="${cls}">${value}</span></div>`;
  let html;
  if(info.kind==='price'){
    const k = list[i];
    html = `<div class="tt-title">${esc(info.symbol)} ${fmtMarketTime(k.time)} ET</div>`
      + row('Open',fmtPrice(k.open)) + row('High',fmtPrice(k.high)) + row('Low',fmtPrice(k.low)) + row('Close',fmtPrice(k.close));
  } else {
    const s = list[i];
    const rows = (info.split
      ? row('Call Gamma', fmtNum(s.callGex), 'pos') + row('Put Gamma', fmtNum(s.putGex), 'neg')
      : row('Net Gamma', fmtNum(s.netGex), s.netGex>=0?'pos':'neg') + row('Power Zone', fmtNum(s.powerZone||0))) + row('Abs GEX', fmtNum(s.ag));
    html = `<div class="tt-title">${esc(info.B.symbol)} Strike ${esc(fmtStrike(s.strike))}</div>${rows}`;
  }
  tt.innerHTML = html;
  tt.style.display = 'block';
  cross.style.display = 'block';
  cross.style.left = (info.padL+info.slot*(i+0.5))+'px';
  const tw = tt.offsetWidth, th = tt.offsetHeight;
  let left = x+14, top = yPos+14;
  if(left+tw>rect.width) left = x-tw-14;
  if(top+th>rect.height) top = yPos-th-14;
  tt.style.left = Math.max(6,left)+'px';
  tt.style.top = Math.max(6,top)+'px';
}
function hideGexBoardTooltips(){
  Object.keys(GB_CHARTS).forEach(k=>[`gb${k}Tooltip`,`gb${k}CrosshairX`].forEach(id=>{const el=byId(id); if(el) el.style.display='none';}));
}
// Full screen, like his expand buttons: ⛶ opens, ✕ (or Esc) closes.
function setGexBoardFullscreen(wrapId, on){
  const wrap = byId(wrapId);
  if(!wrap) return;
  wrap.classList.toggle('gb-full', on);
  const btn = wrap.querySelector?.('.gb-expand');
  if(btn){ btn.textContent = on ? '✕' : '⛶'; btn.setAttribute('aria-label', on ? 'Close full screen' : 'Full screen'); }
  document.body?.classList.toggle('gb-locked', on);
  hideGexBoardTooltips();
  if(window._gbB) drawGexBoardCharts(window._gbB);
}
function exitGexBoardFullscreen(){
  Object.values(GB_CHARTS).forEach(c=>{ if(byId(c.wrap)?.classList.contains('gb-full')) setGexBoardFullscreen(c.wrap, false); });
}

// ----- Rendering and loading -----
function renderGexBoard(scrollToSpot=false){
  renderGexBoardButtons();
  if(!gb.symbol || !gb.record) return;
  const B = computeGexBoard(gb.symbol);
  window._gbB = B;
  renderGexBoardSpot(B);
  renderGexBoardTable(B, scrollToSpot);
  if(activeView===GB_VIEW) drawGexBoardCharts(B);
}
// Refresh of the board: opening it with nothing loaded, or a ticker button.
// Expiry buttons only redraw from the loaded chain.
async function refreshGexBoard(symbol, reuse=false){
  const request = ++_gbRequest;
  screenTickers[GB_VIEW] = symbol;
  setGbStatus(`Loading ${symbol}…`);
  renderGexBoardButtons();
  if(!gb.expiries.length) showDatesLoading('gbDates','<span class="xp-note">Loading dates…</span>');
  // The dates, table and gamma charts draw as soon as the chain is in; the price chart follows.
  const pricePromise = loadOiVolumeCandles(symbol);
  const result = await loadCoreData(reuse);
  if(request!==_gbRequest) return;  // a newer click took over
  if(!REAL[symbol]){
    if(gb.symbol) screenTickers[GB_VIEW] = gb.symbol;
    renderGexBoardButtons();
    setGbStatus(result.detail || `${symbol}: no data.`, 'error');
    return;
  }
  const changed = symbol!==gb.symbol;
  gb.symbol = symbol;
  gb.record = REAL[symbol];
  if(changed){ gb.candles = null; gb.candlesError = 'Loading prices…'; }
  const chain = buildChainRealFromRecord(symbol, gb.record);
  gb.expiries = oiVolumeExpiries(chain);
  gb.shares = gexBoardExpiryShares({...chain, quotes:chain.quotes.filter(q=>gb.expiries.includes(q.exp))});
  const kept = [...gb.selected].filter(exp=>gb.expiries.includes(exp));
  // A new ticker starts on the last preset used (Week at first).
  gb.selected = changed || !kept.length ? defaultExpirySelection(gb.expiries) : new Set(kept);
  if(activeView===GB_VIEW) history.replaceState(null,'',screenUrl(GB_VIEW));
  renderGexBoard(true);
  setGbStatus(result.ok ? '' : `Refresh failed. Showing the last data for ${symbol}.`, result.ok ? '' : 'error');
  const price = await pricePromise;
  if(request!==_gbRequest) return;
  gb.candles = price.candles;
  gb.candlesError = price.error;
  renderGexBoard();
}
function showGexBoard(){
  const symbol = screenTickers[GB_VIEW];
  if(symbol===gb.symbol && gb.record) renderGexBoard();
  else refreshGexBoard(symbol, true);
}

// ---------- P/C Ratio ----------
// Like his options-summary table: loads every time the screen opens, no ticker or
// expiry input, one row per core ticker. Totals span all expiries and strikes and
// come from the server, so they are not cut by the Strike Chart's strike range.
const PC_TICKERS = SCREEN_TICKERS;  // only the three tickers, like every screen
let _pcRequest=0;
function pcRatio(put,call){
  return call>0 ? put/call : null;
}
function pcRow(symbol,rec){
  const t=rec && rec.totals;
  const spot=Number(rec?.spot);
  const count=v=>Number.isFinite(Number(v)) ? Number(v).toLocaleString('en-US') : '—';
  const ratio=v=>v===null ? '—' : v.toFixed(2);
  const cells=t ? [
    ratio(pcRatio(Number(t.put_oi),Number(t.call_oi))),
    ratio(pcRatio(Number(t.put_vol),Number(t.call_vol))),
    count(t.call_oi), count(t.put_oi), count(t.call_vol), count(t.put_vol),
  ] : Array(6).fill('—');
  cells.unshift(Number.isFinite(spot)&&spot>0 ? fmtSpyConvertedPrice(spot) : '—');
  // Column 0 is Spot, columns 1-2 are the two ratios.
  const cls=(c,i)=>c==='—' ? 'pc-missing' : (i===1||i===2 ? 'pc-ratio' : 'pc-num');
  return `<tr><td class="pc-ticker">${esc(symbol)}</td>`
    + cells.map((c,i)=>`<td class="${cls(c,i)}">${esc(c)}</td>`).join('')
    + '</tr>';
}
function setPcStatus(text='',state=''){
  const status=byId('pcStatus');
  if(!status) return;
  status.textContent=text;
  status.className=`pc-status${state?' '+state:''}`;
}
function renderPcTable(data){
  const body=byId('pcTableBody');
  if(body) body.innerHTML=PC_TICKERS.map(symbol=>pcRow(symbol,data?.[symbol])).join('');
}
async function loadPcRatio(){
  const request=++_pcRequest;
  setPcStatus('Loading…');
  const result=await loadCoreData(true);
  const data=result.ok ? REAL : null;
  if(request!==_pcRequest) return;
  if(!data || !PC_TICKERS.some(symbol=>data[symbol]?.totals)){
    setPcStatus('Could not load market data. Open the screen again to retry.','error');
    return;
  }
  renderPcTable(data);
  setPcStatus('');
}

// ---------- Wiring ----------
document.querySelectorAll('a[data-view]').forEach(link=>{
  link.addEventListener('click',event=>{
    // Let ctrl/cmd/shift/middle clicks open the screen in a new tab.
    if(event.defaultPrevented || event.button!==0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigate(link.dataset.view);
  });
});
window.addEventListener('popstate',applyLocation);
bind('menuToggle','click',toggleMenu);
// Ticker buttons: a click loads that ticker; the shown one reloads, the old Search button's job.
document.querySelectorAll('.ticker-picker').forEach(picker=>{
  picker.addEventListener('click',event=>{
    const ticker=event.target.closest?.('[data-ticker]')?.dataset.ticker;
    if(ticker) search(picker.dataset.screen,ticker);
  });
});
// Expiration picker: chips remove a date, the menu adds one, × clears them all.
// Strike Chart dates: a change redraws the strike chart and the Price Lines.
bindExpiryPicker('homeDates', ()=>selectedExpirations && {expiries:homeExpiries, selected:selectedExpirations}, next=>{
  selectedExpirations = next;
  renderExpirationPicker();
  run();
  drawPriceCharts(currentSymbol());
});
// parameter buttons - toggle metrics on/off (multi-select overlay)
document.querySelectorAll('.pbtn').forEach(btn=>{
  btn.addEventListener('click',()=>{
    const m=btn.dataset.metric;
    if(!m) return;
    if(ACTIVE.has(m)) ACTIVE.delete(m); else ACTIVE.add(m);
    btn.classList.toggle('active', ACTIVE.has(m));
    if(window._lastR && activeView === HOME_VIEW) drawChart(window._lastR);
  });
});
window.addEventListener('resize',()=>{
  if(activeView===HOME_VIEW && PRICE[currentSymbol()]) drawPriceCharts();
  const viewRenderer=VIEW_RENDERERS[activeView];
  if(viewRenderer) viewRenderer(window._lastR);
});
// OI Volume: the date and the buttons redraw from the loaded chain; only a ticker button reloads.
bindExpiryPicker('oivDates', ()=>oiv.selected && {expiries:oiv.expiries, selected:oiv.selected}, next=>{
  oiv.selected=next;
  renderOiVolume();
});
document.querySelectorAll('[data-oiv-metric]').forEach(btn=>{
  btn.addEventListener('click',()=>{
    const m=btn.dataset.oivMetric;
    if(OIV_ACTIVE.has(m)) OIV_ACTIVE.delete(m); else OIV_ACTIVE.add(m);
    btn.classList.toggle('active',OIV_ACTIVE.has(m));
    if(window._oivR && activeView===OIV_VIEW) drawOiVolumeChart(window._oivR);
  });
});
bind('oiVolumeChart','mousemove',showOiVolumeTooltip);
bind('oiVolumeChart','mouseleave',hideOiVolumeTooltip);
bind('oiVolumeChart','touchstart',showOiVolumeTooltip,{passive:true});
bind('oiVolumeChart','touchmove',showOiVolumeTooltip,{passive:true});
bind('oiVolumeChart','touchend',()=>setTimeout(hideOiVolumeTooltip,1200),{passive:true});
bind('oiVolumePriceChart','mousemove',showOiVolumePriceTooltip);
bind('oiVolumePriceChart','mouseleave',hideOiVolumePriceTooltip);
bind('oiVolumePriceChart','touchstart',showOiVolumePriceTooltip,{passive:true});
bind('oiVolumePriceChart','touchmove',showOiVolumePriceTooltip,{passive:true});
bind('oiVolumePriceChart','touchend',()=>setTimeout(hideOiVolumePriceTooltip,1200),{passive:true});
['gexChart'].forEach(id=>{
  bind(id,'mousemove',showChartTooltip);
  bind(id,'mouseleave',hideChartTooltip);
  bind(id,'touchstart',showChartTooltip,{passive:true});
  bind(id,'touchmove',showChartTooltip,{passive:true});
  bind(id,'touchend',()=>setTimeout(hideChartTooltip,1200),{passive:true});
});
Object.keys(PRICE_CHARTS).forEach(id=>{
  bind(id,'mousemove',showPriceTooltip);
  bind(id,'mouseleave',hidePriceTooltips);
  bind(id,'touchstart',showPriceTooltip,{passive:true});
  bind(id,'touchmove',showPriceTooltip,{passive:true});
  bind(id,'touchend',()=>setTimeout(hidePriceTooltips,1200),{passive:true});
});
bind('keyLevelsChart','mousemove',showKeyLevelsTooltip);
bind('keyLevelsChart','mouseleave',hideKeyLevelsTooltip);
bind('keyLevelsChart','touchstart',showKeyLevelsTooltip,{passive:true});
bind('keyLevelsChart','touchmove',showKeyLevelsTooltip,{passive:true});
bind('keyLevelsChart','touchend',()=>setTimeout(hideKeyLevelsTooltip,1200),{passive:true});
// GEX Board: a ticker button reloads; expiry buttons, Select all and Clear all redraw.
bind('gbTickers','click',event=>{
  const symbol=event.target.closest?.('[data-gb-ticker]')?.dataset.gbTicker;
  if(symbol) refreshGexBoard(symbol);
});
// Key Levels dates: the levels and the Forecast follow them; the candles stay.
bindExpiryPicker('klDates', ()=>keyLevelsState && {expiries:keyLevelsState.expiries, selected:keyLevelsState.selected}, next=>{
  keyLevelsState = {...keyLevelsState, selected:next, R:keyLevelsGex(keyLevelsState.symbol, next)};
  renderKeyLevels();
});
bindExpiryPicker('gbDates', ()=>gb.symbol && {expiries:gb.expiries, selected:gb.selected}, next=>{
  gb.selected=next;
  renderGexBoard(true);
});
bind('gbTableHead','click',event=>{
  const key=event.target.closest?.('[data-gb-sort]')?.dataset.gbSort;
  if(!key) return;
  gb.sort = gb.sort.key===key ? {key, dir:gb.sort.dir==='desc'?'asc':'desc'} : {key, dir:'desc'};
  renderGexBoard();
});
document.querySelectorAll('.gb-expand').forEach(btn=>{
  btn.addEventListener('click',()=>{
    const wrap=byId(btn.dataset.expand);
    setGexBoardFullscreen(btn.dataset.expand, !wrap?.classList.contains('gb-full'));
  });
});
window.addEventListener('keydown',event=>{ if(event.key==='Escape') exitGexBoardFullscreen(); });
Object.values(GB_CHARTS).forEach(({canvas})=>{
  bind(canvas,'mousemove',showGexBoardTooltip);
  bind(canvas,'mouseleave',hideGexBoardTooltips);
  bind(canvas,'touchstart',showGexBoardTooltip,{passive:true});
  bind(canvas,'touchmove',showGexBoardTooltip,{passive:true});
  bind(canvas,'touchend',()=>setTimeout(hideGexBoardTooltips,1200),{passive:true});
});

applyLocation();
