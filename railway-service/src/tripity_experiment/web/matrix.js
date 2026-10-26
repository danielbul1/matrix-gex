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
  const vega  = S*sqrtT*nd1/100;           // per 1% vol
  const vanna = -nd1*d2/sigma;             // dDelta / dVol
  const charm = -nd1*(2*r*T-d2*sigma*sqrtT)/(2*T*sigma*sqrtT); // call/put dDelta / dt
  let theta;
  if(isCall) theta = -(S*nd1*sigma)/(2*sqrtT) - r*K*Math.exp(-r*T)*normCDF(d2);
  else       theta = -(S*nd1*sigma)/(2*sqrtT) + r*K*Math.exp(-r*T)*normCDF(-d2);
  return {delta, gamma, theta:theta/365, vega, vanna, charm, iv:sigma};
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
function preciseYearsToExpiry(exp,root,valuationMs){
  const [year,month,day]=String(exp).split('-').map(Number);
  // AM-settled index roots (9:30 AM ET expiry): SPX/NDX, plus VIX and its weeklies.
  const isAmSettled=root==='SPX'||root==='NDX'||root==='VIX'||root==='VIXW';
  const expiryMs=zonedDateTimeToUtc(year,month,day,isAmSettled?9:16,isAmSettled?30:0,'America/New_York');
  return Math.max((expiryMs-valuationMs)/(365.25*86400000),1/(365.25*24*60));
}

// ---------- Symbol universe ----------
const SYMBOLS = {
  NDX:{spot:29252.56, step:25, mult:100, baseIV:0.170, market:"US"},
  SPX:{spot:7482.71,  step:5,  mult:100, baseIV:0.140, market:"US"},
  SPY:{spot:746.58,  step:5,  mult:100, baseIV:0.135, market:"US"},
  QQQ:{spot:715.71,  step:1,  mult:100, baseIV:0.165, market:"US"},
  VIX:{spot:20,     step:1,  mult:100, baseIV:0.80,  market:"US"},
};
// Real market data is served directly by Tripity.
let REAL = {};
let REAL_ASOF = {};
const RISK_FREE = {US:0.05, IN:0.065};
const SPX_SPY_RATIO = 10.03657299922611;
// User-provided conversion example: QQQ 752.00 = NDX 30,916.24.
const NDX_QQQ_RATIO = 30916.24 / 752;
const DEFAULT_SYMBOL = 'NDX';

// ---------- Real chain from Tripity ----------
// Uses official daily OI plus the freshest available gamma/delta/IV/volume fields.
function buildChainReal(symKey){
  return buildChainRealFromRecord(symKey,REAL[symKey]);
}
function buildChainRealFromRecord(symKey, rec){
  const spot = rec.spot;
  const r = RISK_FREE[SYMBOLS[symKey] ? SYMBOLS[symKey].market : "US"] || 0.05;
  const now = parseSourceTimestamp(rec.price_asof || rec.asof);
  const quotes = rec.opts.map(o=>{
    const isCall = o.t==="C";
    const T = preciseYearsToExpiry(o.exp,o.root||'',now);
    // The upstream does not always provide gamma; fall back to Black-Scholes when missing.
    const modelGreeks = calcGreeks(spot, o.k, T, o.iv>0?o.iv:0.2, r, isCall);
    const recalcFromLiveIndex = String(rec.source||'').includes('lse_live_index') || rec.dynamic===true;
    let g = {...modelGreeks, gamma:o.g, delta:o.d, iv:o.iv};
    if(recalcFromLiveIndex || !o.g || o.g<=0){
      g = modelGreeks;
    }
    return {K:o.k, dte:o.dte, T, exp:o.exp, root:o.root||'', isCall, iv:o.iv, oi:o.oi, vol:o.vol,
      last:Number(o.last), bid:Number(o.bid), ask:Number(o.ask), ts:now, g};
  });
  return {symbol:symKey, market:"US", spot, mult:rec.mult||100, fetchTs:now, quotes, live:true,
    source:rec.source||'tripity'};
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
        callOI:0, putOI:0, callVol:0, putVol:0, callIV:0, putIV:0, gamma:0, ts:q.ts};
    const s = byStrike[q.K];
    if(q.isCall){
      s.callGex += gex; s.callOI+=q.oi; s.callVol+=q.vol; s.callIV=q.iv;
    } else {
      s.putGex -= gex; s.putOI +=q.oi; s.putVol +=q.vol; s.putIV =q.iv;
    }
    s.gamma = Math.max(s.gamma, q.g.gamma);
    s.netGex = s.callGex + s.putGex;
    s.ts = Math.max(s.ts, q.ts);
  }
  let strikes = Object.values(byStrike).sort((a,b)=>a.strike-b.strike);
  strikes.forEach(s=>{ s.totalOI=s.callOI+s.putOI; s.totalVol=s.callVol+s.putVol;
                       s.pcr=s.callOI>0?s.putOI/s.callOI:0; });
  const maxAG = Math.max(...strikes.map(s=>Math.abs(s.callGex)+Math.abs(s.putGex)), 1);
  const maxOI = Math.max(...strikes.map(s=>s.totalOI), 1);
  const maxVol = Math.max(...strikes.map(s=>s.totalVol), 1);
  strikes.forEach(s=>{
    const agScore = (Math.abs(s.callGex)+Math.abs(s.putGex)) / maxAG;
    const oiScore = s.totalOI / maxOI;
    const volScore = s.totalVol / maxVol;
    s.powerZone = (0.58*agScore + 0.28*oiScore + 0.14*volScore) * maxAG;
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

  // 5. zero-gamma flip - cumulative net GEX zero crossing + interpolation
  let flip=null;
  let cum=0, prevCum=null, prevK=null;
  for(const s of strikes){
    const newCum = cum + s.netGex;
    if(prevCum!==null && prevCum*newCum<0){
      const ratio = Math.abs(prevCum)/(Math.abs(prevCum)+Math.abs(newCum));
      flip = prevK + ratio*(s.strike-prevK); break;
    }
    prevCum=newCum; prevK=s.strike; cum=newCum;
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
  let regime,strength;
  if(totalGex > strongMag*0.04){
    regime="positive_gamma";
    strength=Math.abs(distPct)>2?"strong":(Math.abs(distPct)>0.5?"moderate":"weak");
  } else if(totalGex < -strongMag*0.04){
    regime="negative_gamma";
    strength=Math.abs(distPct)>2?"strong":(Math.abs(distPct)>0.5?"moderate":"weak");
  } else { regime="neutral"; strength="very_weak"; }

  // 8. IV skew & sentiment
  const near = strikes.filter(s=>Math.abs(s.strike-spot)/spot<=0.05);
  const avgCallIV = avg(near.map(s=>s.callIV).filter(x=>x>0));
  const avgPutIV  = avg(near.map(s=>s.putIV).filter(x=>x>0));
  const ivSkew = avgPutIV - avgCallIV;
  const callGexPct = totalCallGex/(Math.abs(totalCallGex)+Math.abs(totalPutGex))*100;
  const sentiment = pcr<0.7?"bullish_positioning":(pcr>1.3?"bearish_positioning":"neutral_positioning");

  const atmQ = kept.filter(q=>q.isCall).sort((a,b)=>Math.abs(a.K-spot)-Math.abs(b.K-spot))[0];

  return {symbol:chain.symbol, market:chain.market, spot, mult, fetchTs:chain.fetchTs, source:chain.source,
    strikes, totalGex, totalCallGex, totalPutGex,
    maxGammaStrike, flip, callWall, putWall, callWallGex:cwG, putWallGex:pwG,
    regime, strength, distPct, pcr, netCallOI, netPutOI, callGexPct,
    avgCallIV, avgPutIV, ivSkew, sentiment, atmGreeks:atmQ?atmQ.g:null,
    keptCount:kept.length, totalCount:chain.quotes.length,
    expiries:[...new Set(kept.map(q=>q.dte))].sort((a,b)=>a-b)};
}
function avg(a){return a.length?a.reduce((x,y)=>x+y,0)/a.length:0;}

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
}
// Single view -> renderer dispatch, shared by renderImpl, setView and the resize handler.
const VIEW_RENDERERS = {
  'gex': R=>drawChart(R),
};
function renderImpl(R){
  window._lastR=R;
  const viewRenderer=VIEW_RENDERERS[activeView];
  if(viewRenderer) viewRenderer(R);
}

// ---------- Metric definitions (Quant-Power style param buttons) ----------
// net_gex = bars. Other metrics are stacked area layers.
let ACTIVE = new Set(["net_gex"]);
// Strikes shown: spot +/- 2 expected moves.
const DISPLAY_SIGMA = 2;
const METRICS = {
  net_gex:  {label:"Net GEX",     color:"#22b8ff", kind:"bar",  signed:true,  val:s=>s.netGex},
  ag:       {label:"AG",          color:"#ab7df6", kind:"area", val:s=>Math.abs(s.callGex)+Math.abs(s.putGex)},
  call_oi:  {label:"Call OI",     color:"#26c281", kind:"area", val:s=>s.callOI},
  put_oi:   {label:"Put OI",      color:"#ef5350", kind:"area", val:s=>s.putOI},
  call_vol: {label:"Call Volume", color:"#2f73ff", kind:"area", val:s=>s.callVol},
  put_vol:  {label:"Put Volume",  color:"#ff8a3d", kind:"area", val:s=>s.putVol},
  power:    {label:"Power Zone",  color:"#fff200", kind:"area", val:s=>s.powerZone || 0},
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
function visibleStrikeData(R){
  let visibleStrikes=[...R.strikes];
  if(R.expectedMove>0){
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

  const data=visibleStrikeData(R);
  if(!data.length) return;

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
  const padR=mobile ? (areas.length ? 42 : 18) : (areas.length?62:42);
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
  // ----- RIGHT axis (dominant area metric) -----
  const ownMax={}; areas.forEach(m=>ownMax[m]=Math.max(...data.map(METRICS[m].val),1));
  let dom=areas[0], rMax=1;
  if(areas.length){ dom=areas.reduce((a,b)=>ownMax[b]>ownMax[a]?b:a,areas[0]); rMax=ownMax[dom]*1.08; }

  // ----- gridlines + tick labels -----
  ctx.font=(mobile ? "bold 10px Segoe UI" : "bold 13px Segoe UI"); ctx.textBaseline="middle";
  const ticks=6;
  for(let t=0;t<=ticks;t++){
    const f=t/ticks, yy=bottom-f*plotH;
    ctx.strokeStyle="rgba(255,255,255,.025)"; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(padL,yy); ctx.lineTo(W-padR,yy); ctx.stroke();
    if(hasBar){ ctx.fillStyle="#c4c4c4"; ctx.textAlign="right";
      ctx.fillText(fmtAxis(lMin+(lMax-lMin)*f), padL-8, yy); }
    if(areas.length){ ctx.fillStyle="#c4c4c4"; ctx.textAlign="left";
      ctx.fillText(fmtAxis(rMax*f), W-padR+8, yy); }
  }

  // ----- area overlays -----
  areas.forEach(m=>{
    const M=METRICS[m], own=ownMax[m];
    const yA=v=>bottom-(v/own)*0.92*plotH;
    ctx.beginPath(); ctx.moveTo(xCenter(0),bottom);
    data.forEach((s,i)=>ctx.lineTo(xCenter(i),yA(M.val(s))));
    ctx.lineTo(xCenter(data.length-1),bottom); ctx.closePath();
    ctx.fillStyle=hexA(M.color,.33); ctx.fill();
    ctx.beginPath();
    data.forEach((s,i)=>{const x=xCenter(i),y=yA(M.val(s)); i?ctx.lineTo(x,y):ctx.moveTo(x,y);});
    ctx.strokeStyle=M.color; ctx.lineWidth=1.6; ctx.stroke();
    ctx.fillStyle=M.color;
    data.forEach((s,i)=>{ctx.beginPath();ctx.arc(xCenter(i),yA(M.val(s)),1.8,0,7);ctx.fill();});
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
      ctx.fillStyle = v>=0 ? "#22aaf2" : "#ff2417";
      ctx.fillRect(x, y, bw, h);
      hitBars[i]={x,y,w:bw,h,s};
    });
    ctx.strokeStyle="rgba(255,255,255,.18)"; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(padL,y0); ctx.lineTo(W-padR,y0); ctx.stroke();
  }
  cv._matrixBars = hitBars;
  cv._matrixPlot = {top,bottom,padL,padR,W,H};

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
  const linkedSpot = linkedMarketPrice(R.spot,R);
  const priceLabel = linkedSpot == null
    ? "Price: "+fmtPrice(R.spot)
    : `${R.symbol}: ${fmtPrice(R.spot)} / ${linkedSpot.symbol}: ${fmtLinkedMarketPrice(linkedSpot)}`;
  const labelWidth = ctx.measureText(priceLabel).width;
  const alignRight = px>W-labelWidth-12;
  ctx.textAlign = alignRight?"right":"left";
  ctx.fillText(priceLabel, px+(alignRight?-6:6), padT-9);

  // ----- axis titles -----
  if(hasBar){ ctx.save(); ctx.translate(14,padT+plotH/2); ctx.rotate(-Math.PI/2);
    ctx.fillStyle="#d0d0d0"; ctx.font=(mobile ? "bold 11px Segoe UI" : "bold 13px Segoe UI"); ctx.textAlign="center";
    ctx.fillText(barMetric.axisLabel || barMetric.label,0,0); ctx.restore(); }
  if(areas.length){ ctx.save(); ctx.translate(W-12,padT+plotH/2); ctx.rotate(Math.PI/2);
    ctx.fillStyle="#d0d0d0"; ctx.font=(mobile ? "bold 11px Segoe UI" : "bold 13px Segoe UI"); ctx.textAlign="center";
    ctx.fillText(METRICS[dom].label,0,0); ctx.restore(); }
  ctx.fillStyle="#d0d0d0"; ctx.font=(mobile ? "bold 10px Segoe UI" : "bold 12px Segoe UI"); ctx.textAlign="center"; ctx.textBaseline="bottom";
  ctx.fillText("Strike",padL+plotW/2,H-4);
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
    if(m==='power') return row('Power Zone',fmtNum(s.powerZone || 0));
    return '';
  }).join('');
  const linkedStrike = linkedMarketPrice(s.strike,R);
  const linkedRow = linkedStrike == null ? '' : `<div class="tt-row spy-row"><span>${esc(linkedStrike.symbol)} Price</span><span class="pos">${fmtLinkedMarketPrice(linkedStrike)}</span></div>`;
  tt.innerHTML = `
    <div class="tt-title">${R?.symbol || ''} Strike ${fmtPrice(s.strike)}</div>
    ${metricRows || row('No metric selected','')}
    ${linkedRow}
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
// ---------- Price charts (Strike Chart) ----------
// Two charts of the searched ticker's latest session, beside the strike chart:
// 1-minute candles, and a "simplified" close-price line. What his simplified
// chart holds is not public; a close line is our guess. Neither chart reads the
// expiration, so only Search or Enter reloads them.
const PRICE_CHARTS = {
  priceChart:       {kind:'candles', symbolId:'priceSymLabel',       tooltipId:'priceTooltip',       crosshairId:'priceCrosshairX',       emptyId:'priceEmpty'},
  priceSimpleChart: {kind:'line',    symbolId:'priceSimpleSymLabel', tooltipId:'priceSimpleTooltip', crosshairId:'priceSimpleCrosshairX', emptyId:'priceSimpleEmpty'},
};
const PRICE_UP = '#3d9970', PRICE_DOWN = '#ff4136', PRICE_LINE = '#22b8ff';
const MARKET_TZ = 'America/New_York';
// Per ticker: {candles:[...]} or {error:'...'}.
let PRICE = {};
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
  if(!candles.length) return;

  const padL=mobile?50:66, padR=mobile?10:18, padT=mobile?14:18, padB=mobile?30:36;
  const plotW=W-padL-padR, plotH=H-padT-padB;
  const top=padT, bottom=padT+plotH;
  const {lo,hi}=priceRange(candles,cfg.kind);
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
const TICKER_PATTERN = /^[A-Z][A-Z0-9.-]{0,5}$/;
const DYNAMIC_SYMBOLS = new Set();
let activeView = null;
// Each screen keeps its own ticker, the way each of his screens has its own ticker input.
const screenTickers = Object.fromEntries(
  Object.keys(SCREENS).filter(view=>SCREENS[view].ticker).map(view=>[view,DEFAULT_SYMBOL])
);
let selectedExpiration = null;

function normalizeSymbol(value){
  return String(value||'').trim().toUpperCase();
}
function validTicker(value){
  return TICKER_PATTERN.test(value);
}
function isCoreSymbol(symbol){
  return Object.prototype.hasOwnProperty.call(CORE_SYMBOL_INFO,symbol);
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
  return document.querySelector(`.ticker-form[data-screen="${view}"]`);
}
function setTickerStatus(view,text='',state=''){
  const status=screenForm(view)?.querySelector('.ticker-status');
  if(!status) return;
  status.textContent=text;
  status.className=`ticker-status${state?' '+state:''}`;
}
function syncTickerInput(view,value=screenTickers[view]){
  const input=screenForm(view)?.elements.ticker;
  if(input && value) input.value=value;
}

// ---------- Expiration (Strike Chart) ----------
// One date at a time from a dropdown filled per ticker, like his date dropdown.
// It starts on the nearest date (guess: his default is not public).
function currentExpiration(){
  return byId('expirationSelect')?.value || null;
}
function chainExpirations(chain){
  const todayIso = new Date().toISOString().slice(0,10);
  return [...new Set(chain.quotes.map(q=>q.exp).filter(exp=>exp && exp >= todayIso))].sort();
}
function pickExpiration(expiries, wanted){
  return expiries.includes(wanted) ? wanted : (expiries[0] || null);
}
function populateExpirations(chain){
  const select = byId('expirationSelect');
  const expiries = chainExpirations(chain);
  selectedExpiration = pickExpiration(expiries, selectedExpiration);
  if(!select) return;
  select.innerHTML = expiries.length
    ? expiries.map(exp=>`<option value="${esc(exp)}"${exp===selectedExpiration?' selected':''}>${esc(exp)}</option>`).join('')
    : '<option value="">No expirations available</option>';
  select.disabled = !expiries.length;
  if(selectedExpiration) select.value = selectedExpiration;
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
  closeMenu();
  if(changed) window.scrollTo({top:0,left:0});
}
// Shows the screen the address bar names. The Strike Chart loads data only when
// it has none for its ticker; after that, data reloads only on Search or Enter.
function applyLocation(){
  const view=viewFromPath(window.location.pathname);
  const fromUrl=normalizeSymbol(new URLSearchParams(window.location.search).get('ticker'));
  const urlTicker=SCREENS[view].ticker && validTicker(fromUrl) ? fromUrl : null;
  if(urlTicker && view!==HOME_VIEW) screenTickers[view]=urlTicker;
  setView(view);
  if(view!==HOME_VIEW) return;
  const symbol=urlTicker || currentSymbol();
  if(symbol===currentSymbol() && REAL[symbol]){
    run();
    drawPriceCharts(symbol);
  }
  else refreshHome(symbol);
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
  if(!validTicker(symbol)){
    setTickerStatus(view,'Enter a ticker, e.g. SPX','error');
    return;
  }
  if(view===HOME_VIEW){
    refreshHome(symbol);
    return;
  }
  screenTickers[view]=symbol;
  syncTickerInput(view);
  setTickerStatus(view,'');
  if(activeView===view) history.replaceState(null,'',screenUrl(view));
}

// ---------- Strike Chart ----------
function run(){
  const sym=currentSymbol();
  if(!REAL[sym]){
    setTickerStatus(HOME_VIEW,`${sym}: no data yet. Press Search to try again.`,'error');
    return;
  }
  let chain = buildChainReal(sym);
  populateExpirations(chain);
  if(selectedExpiration){
    chain = {...chain, quotes: chain.quotes.filter(q=>q.exp===selectedExpiration)};
  }
  const R = calcGEX(chain);
  R.expectedMove = calcExpectedMove(chain);
  R.live = true;
  R.asof = REAL[sym].asof;
  renderImpl(R);
}

// ---------- Data loading (only on Search or Enter) ----------
const MATRIX_CBOE_DATA_URL = 'https://api.trytripity.site/api/matrix/cboe-data';
const MATRIX_SYMBOL_DATA_URL = 'https://api.trytripity.site/api/matrix/symbol-data';
const MATRIX_CANDLES_URL = 'https://api.trytripity.site/api/matrix/candles';
let _homeRequest=0;
function fetchLiveData(url){
  return fetch(url, {cache:'no-store'})
    .then(r=>r.ok?r.json():null)
    .catch(()=>null);
}
function validCboeData(d){
  return d && d.SPX && Array.isArray(d.SPX.opts) && Number.isFinite(Number(d.SPX.spot));
}
function dynamicSymbolConfig(symbol,record){
  const strikes=[...new Set((record.opts||[]).map(option=>Number(option.k)).filter(Number.isFinite))].sort((a,b)=>a-b);
  let step=1;
  for(let i=1;i<strikes.length;i++){
    const diff=strikes[i]-strikes[i-1];
    if(diff>0 && (step===1 || diff<step)) step=diff;
  }
  const ivs=(record.opts||[]).map(option=>Number(option.iv)).filter(iv=>Number.isFinite(iv)&&iv>0);
  const baseIV=ivs.length?ivs.reduce((sum,iv)=>sum+iv,0)/ivs.length:.25;
  SYMBOLS[symbol]={
    spot:Number(record.spot),
    step:Math.max(.01,step),
    mult:Number(record.mult)||100,
    baseIV,
    market:'US',
    dynamic:true,
  };
}
// The core tickers (NDX, SPX, SPY, QQQ, VIX) come in one call.
async function loadCoreData(){
  const data=await fetchLiveData(MATRIX_CBOE_DATA_URL + '?t=' + Date.now());
  if(!validCboeData(data)) return {ok:false, detail:'Could not load market data. Press Search to try again.'};
  const dynamicRecords=Object.fromEntries([...DYNAMIC_SYMBOLS].filter(symbol=>REAL[symbol]).map(symbol=>[symbol,REAL[symbol]]));
  REAL={...data,...dynamicRecords};
  return {ok:true};
}
// Any other ticker loads its own Cboe chain through symbol-data.
async function loadDynamicSymbol(symbol){
  let response=null, payload=null;
  try{
    response=await fetch(`${MATRIX_SYMBOL_DATA_URL}?symbol=${encodeURIComponent(symbol)}`,{cache:'no-store'});
    payload=await response.json();
  }catch(_err){
    payload=null;
  }
  if(!response?.ok || !payload?.ok || !payload.record){
    return {ok:false, detail:payload?.detail || `${symbol}: option chain is unavailable`};
  }
  dynamicSymbolConfig(symbol,payload.record);
  REAL[symbol]=payload.record;
  DYNAMIC_SYMBOLS.add(symbol);
  return {ok:true};
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
// Refresh of the Strike Chart: Search, Enter, or opening it with no data yet.
// The chain and the price candles load together.
async function refreshHome(symbol){
  const request=++_homeRequest;
  const previous=currentSymbol();
  if(activeView===HOME_VIEW) syncTickerInput(HOME_VIEW,symbol);
  setTickerStatus(HOME_VIEW,`Loading ${symbol}…`);
  const [result,price]=await Promise.all([
    isCoreSymbol(symbol) ? loadCoreData() : loadDynamicSymbol(symbol),
    loadPriceData(symbol),
  ]);
  if(request!==_homeRequest) return;  // a newer Search took over
  // Keep the last good candles if this refresh could not get new ones.
  if(price.candles || !PRICE[symbol]?.candles) PRICE[symbol]=price;
  if(!REAL[symbol]){
    // Nothing to show for this ticker: fall back to the last ticker, or the default one.
    if(!REAL[previous] && symbol!==previous) await refreshHome(previous);
    setTickerStatus(HOME_VIEW,result.detail,'error');
    return;
  }
  if(symbol!==previous) selectedExpiration=null;
  screenTickers[HOME_VIEW]=symbol;
  if(activeView===HOME_VIEW){
    syncTickerInput(HOME_VIEW);
    history.replaceState(null,'',screenUrl(HOME_VIEW));
  }
  run();
  drawPriceCharts(symbol);
  setTickerStatus(HOME_VIEW,result.ok?'':`Refresh failed. Showing the last data for ${symbol}.`,result.ok?'':'error');
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
document.querySelectorAll('.ticker-form').forEach(form=>{
  form.addEventListener('submit',event=>{
    event.preventDefault();
    search(form.dataset.screen,form.elements.ticker.value);
  });
});
// A new date redraws the strike chart only; the price charts do not read it.
bind('expirationSelect','change',()=>{
  selectedExpiration=currentExpiration();
  run();
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
  if(!window._lastR) return;
  const viewRenderer=VIEW_RENDERERS[activeView];
  if(viewRenderer) viewRenderer(window._lastR);
});
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

applyLocation();
