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
}
// Single view -> renderer dispatch, shared by renderImpl, setView and the resize handler.
const VIEW_RENDERERS = {
  'gex': R=>drawChart(R),
};
function renderImpl(R){
  window._lastR=R;
  const badge=document.getElementById('srcBadge');
  const nowClient=new Date().toLocaleTimeString('he-IL');
  badge.style.color="var(--green)"; badge.style.borderColor="var(--green)";
  badge.textContent=`LIVE - ${R.source||'Tripity'} · data ${R.asof||''} · refreshed ${nowClient}`;
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
// ---------- Wiring ----------
let activeView = 'gex';
const selectedExpirationsByView = {gex:null};
const CORE_SYMBOL_INFO = {
  NDX:'Nasdaq-100 Index',
  SPX:'S&P 500 Index',
  SPY:'SPDR S&P 500 ETF Trust',
  QQQ:'Invesco QQQ Trust',
  VIX:'Cboe Volatility Index',
};
const FAVORITES_STORE_KEY = 'matrix-symbol-favorites-v1';
const DYNAMIC_SYMBOLS = new Set();
const SYMBOL_NAMES = new Map(Object.entries(CORE_SYMBOL_INFO));
let _symbolSearchTimer=null;
let _symbolSearchSequence=0;
function currentExpirationValues(){
  return [...document.querySelectorAll('#expirationPicker input:checked')].map(input=>input.value);
}
function saveCurrentExpirationSelection(){
  if(selectedExpirationsByView.hasOwnProperty(activeView)){
    selectedExpirationsByView[activeView] = currentExpirationValues();
  }
}
function populateSymbols(){
  const sel=document.getElementById('symbol');
  const current=sel.value || DEFAULT_SYMBOL;
  sel.innerHTML="";
  Object.keys(SYMBOLS).forEach(k=>{
    const o=document.createElement('option');o.value=k;o.textContent=k;sel.appendChild(o);
  });
  readSymbolFavorites().forEach(item=>{
    SYMBOL_NAMES.set(item.symbol,item.name || item.symbol);
    ensureSymbolOption(item.symbol);
  });
  sel.value=[...sel.options].some(option=>option.value===current)?current:DEFAULT_SYMBOL;
  const search=document.getElementById('symbolSearch');
  if(search) search.value=sel.value;
  renderSymbolFavorites();
  syncSymbolFavoriteButton();
}
function normalizeSymbol(value){
  return String(value||'').trim().toUpperCase();
}
function readSymbolFavorites(){
  try{
    const rows=JSON.parse(localStorage.getItem(FAVORITES_STORE_KEY)||'[]');
    return Array.isArray(rows) ? rows.filter(row=>row&&/^[A-Z][A-Z0-9.-]{0,5}$/.test(row.symbol)).slice(0,40) : [];
  }catch(_err){
    return [];
  }
}
function writeSymbolFavorites(rows){
  localStorage.setItem(FAVORITES_STORE_KEY,JSON.stringify(rows.slice(0,40)));
}
function ensureSymbolOption(symbol){
  const sel=document.getElementById('symbol');
  if(!sel || [...sel.options].some(option=>option.value===symbol)) return;
  const option=document.createElement('option');
  option.value=symbol;
  option.textContent=symbol;
  sel.appendChild(option);
}
function renderSymbolFavorites(){
  const host=document.getElementById('symbolFavorites');
  if(!host) return;
  const current=document.getElementById('symbol')?.value;
  const favorites=readSymbolFavorites();
  const quick=[...new Set([...favorites.map(item=>item.symbol),'NDX','SPX','SPY','QQQ'])];
  host.innerHTML=quick.map(symbol=>`<button type="button" class="symbol-favorite-chip ${symbol===current?'current':''}" data-symbol-chip="${esc(symbol)}" title="${esc(SYMBOL_NAMES.get(symbol)||symbol)}">${esc(symbol)}</button>`).join('');
}
function syncSymbolFavoriteButton(){
  const button=document.getElementById('symbolFavorite');
  if(!button) return;
  const symbol=document.getElementById('symbol')?.value;
  const active=readSymbolFavorites().some(item=>item.symbol===symbol);
  button.classList.toggle('active',active);
  button.textContent=active?'★':'☆';
  button.title=active?'Remove current symbol from favorites':'Add current symbol to favorites';
  button.setAttribute('aria-label',button.title);
}
function setSymbolSearchStatus(text='',state=''){
  const status=document.getElementById('symbolSearchStatus');
  if(!status) return;
  status.textContent=text;
  status.className=`symbol-search-status${state?' '+state:''}`;
}
function localSymbolResults(query=''){
  const upper=normalizeSymbol(query);
  const symbols=[...new Set([...readSymbolFavorites().map(item=>item.symbol),...Object.keys(CORE_SYMBOL_INFO)])];
  return symbols.filter(symbol=>{
    const name=SYMBOL_NAMES.get(symbol)||'';
    return !upper || symbol.includes(upper) || name.toUpperCase().includes(upper);
  }).map(symbol=>({symbol,name:SYMBOL_NAMES.get(symbol)||symbol}));
}
function renderSymbolSearchResults(items){
  const host=document.getElementById('symbolSearchResults');
  if(!host) return;
  host.innerHTML=items.map((item,index)=>`<button type="button" class="symbol-search-result${index===0?' active':''}" role="option" data-symbol-result="${esc(item.symbol)}" data-symbol-name="${esc(item.name||item.symbol)}"><b>${esc(item.symbol)}</b><span>${esc(item.name||'Option symbol')}</span></button>`).join('');
  host.hidden=!items.length;
  document.getElementById('symbolSearch')?.setAttribute('aria-expanded',items.length?'true':'false');
}
function closeSymbolSearchResults(){
  const host=document.getElementById('symbolSearchResults');
  if(host) host.hidden=true;
  document.getElementById('symbolSearch')?.setAttribute('aria-expanded','false');
}
function searchSymbols(query){
  const text=String(query||'').trim();
  clearTimeout(_symbolSearchTimer);
  if(!text){
    renderSymbolSearchResults(localSymbolResults());
    setSymbolSearchStatus('Favorites and core symbols');
    return;
  }
  const local=localSymbolResults(text);
  renderSymbolSearchResults(local);
  setSymbolSearchStatus('Searching Cboe symbols…');
  const sequence=++_symbolSearchSequence;
  _symbolSearchTimer=setTimeout(async()=>{
    const payload=await fetchLiveData(`${MATRIX_SYMBOL_SEARCH_URL}?q=${encodeURIComponent(text)}`);
    if(sequence!==_symbolSearchSequence) return;
    const remote=payload?.ok&&Array.isArray(payload.items)?payload.items:[];
    const merged=[...local];
    remote.forEach(item=>{
      const symbol=normalizeSymbol(item.symbol);
      if(!symbol || merged.some(row=>row.symbol===symbol)) return;
      merged.push({symbol,name:item.name||symbol});
    });
    renderSymbolSearchResults(merged.slice(0,12));
    setSymbolSearchStatus(merged.length?`${merged.length} option symbols found`:'No matching option symbols','');
  },250);
}
function selectMatrixSymbol(symbol,name=''){
  symbol=normalizeSymbol(symbol);
  if(!/^[A-Z][A-Z0-9.-]{0,5}$/.test(symbol)) return Promise.resolve(false);
  _symbolSearchSequence++;
  const previousSymbol=document.getElementById('symbol')?.value || DEFAULT_SYMBOL;
  if(name) SYMBOL_NAMES.set(symbol,name);
  ensureSymbolOption(symbol);
  document.getElementById('symbol').value=symbol;
  document.getElementById('symbolSearch').value=symbol;
  document.getElementById('expirationPicker').innerHTML='';
  resetExpirationSelections();
  closeSymbolSearchResults();
  renderSymbolFavorites();
  syncSymbolFavoriteButton();
  const isCore=Object.prototype.hasOwnProperty.call(CORE_SYMBOL_INFO,symbol);
  if(isCore){
    setSymbolSearchStatus(SYMBOL_NAMES.get(symbol)||symbol);
    if(REAL[symbol]) run();
    else loadData(true);
    return Promise.resolve(true);
  }
  document.getElementById('symbol').value=previousSymbol;
  renderSymbolFavorites();
  syncSymbolFavoriteButton();
  DYNAMIC_SYMBOLS.add(symbol);
  return loadDynamicSymbol(symbol,true);
}
function toggleCurrentSymbolFavorite(){
  const symbol=document.getElementById('symbol')?.value;
  if(!symbol) return;
  const rows=readSymbolFavorites();
  const index=rows.findIndex(item=>item.symbol===symbol);
  if(index>=0) rows.splice(index,1);
  else rows.unshift({symbol,name:SYMBOL_NAMES.get(symbol)||symbol});
  writeSymbolFavorites(rows);
  renderSymbolFavorites();
  syncSymbolFavoriteButton();
}
function renderExpirationPicker(picker, expiries, byExp, selectedValues){
  const selected = new Set(selectedValues || []);
  const validSelected = expiries.filter(exp=>selected.has(exp));
  const defaultSelection = expiries.slice(0,1);
  const active = new Set(validSelected.length ? validSelected : defaultSelection);
  picker.innerHTML = expiries.length ? expiries.map(exp=>{
    const dte = byExp.get(exp);
    return `<label class="expiry-option"><input type="checkbox" value="${exp}" data-dte="${dte}" ${active.has(exp)?'checked':''}>${exp} (${dte}DTE)</label>`;
  }).join('') : '<span class="expiry-empty">No expirations available</span>';
  return [...active];
}
function populateExpirations(chain){
  const picker = document.getElementById('expirationPicker');
  const remembered = selectedExpirationsByView[activeView];
  const selectedValues = remembered || (selectedExpirationsByView.hasOwnProperty(activeView) ? [] : currentExpirationValues());
  const selected = new Set(selectedValues);
  const byExp = new Map();
  const todayIso = new Date().toISOString().slice(0,10);
  const monthIso = new Date(Date.now()+30*86400000).toISOString().slice(0,10);
  chain.quotes.forEach(q=>{
    if(q.exp && q.exp >= todayIso && !byExp.has(q.exp)) byExp.set(q.exp, q.dte);
  });
  const expiries = [...byExp.keys()].sort();
  renderExpirationPicker(picker, expiries, byExp, selectedValues);
  if(selectedExpirationsByView.hasOwnProperty(activeView) && !remembered){
    selectedExpirationsByView[activeView] = currentExpirationValues();
  }
  syncExpiryQuickButtons();
}
// Quick-select buttons (0DTE / Week / All) above the expiration picker.
function expiryQuickPredicate(kind){
  return kind==='all' ? ()=>true : kind==='week' ? dte=>dte<=7 : dte=>dte===0;
}
function syncExpiryQuickButtons(){
  const inputs=[...document.querySelectorAll('#expirationPicker input[type=checkbox]')];
  const checked=inputs.filter(i=>i.checked);
  const matches=kind=>{
    const pred=expiryQuickPredicate(kind);
    const targets=inputs.filter(i=>pred(Number(i.dataset.dte)));
    return targets.length>0 && checked.length===targets.length && targets.every(i=>i.checked);
  };
  document.querySelectorAll('.expiry-quick-btn').forEach(btn=>btn.classList.toggle('active',matches(btn.dataset.expquick)));
}
function applyExpirationQuickSelect(kind){
  const inputs=[...document.querySelectorAll('#expirationPicker input[type=checkbox]')];
  if(!inputs.length) return;
  const pred=expiryQuickPredicate(kind);
  if(!inputs.some(i=>pred(Number(i.dataset.dte)))) return;  // e.g. no 0DTE listed: keep current selection
  inputs.forEach(input=>{ input.checked=pred(Number(input.dataset.dte)); });
  // Same flow as a manual checkbox change: per-view memory, re-render, flow reload.
  syncExpiryQuickButtons();
  saveCurrentExpirationSelection();
  run();
}
function setView(view){
  if(view === activeView) return;
  saveCurrentExpirationSelection();
  activeView = view;
  document.querySelectorAll('.side-nav a[data-view]').forEach(a=>a.classList.toggle('active',a.dataset.view===view));
  document.querySelectorAll('.app-view').forEach(section=>section.classList.toggle('active',section.id===`view-${view}`));
  hideAllHoverHelpers();
  run();
  const viewRenderer=VIEW_RENDERERS[view];
  if(viewRenderer && window._lastR) requestAnimationFrame(()=>viewRenderer(window._lastR));
  window.scrollTo({top:0,left:0});
}
function run(){
  const sym=document.getElementById('symbol').value;
  if(!REAL[sym]){
    setSymbolSearchStatus(`${sym}: no data yet. Press Refresh to try again.`,'error');
    return;
  }
  let chain = buildChainReal(sym);
  populateExpirations(chain);
  const selectedExpirations = new Set(currentExpirationValues());
  if(selectedExpirations.size){
    chain = {...chain, quotes: chain.quotes.filter(q=>selectedExpirations.has(q.exp))};
  }
  const R = calcGEX(chain);
  R.expectedMove = calcExpectedMove(chain);
  R.live = true;
  R.asof = REAL[sym].asof;
  renderImpl(R);
}
function resetExpirationSelections(){
  Object.keys(selectedExpirationsByView).forEach(view=>{ selectedExpirationsByView[view]=null; });
}
bind('symbol','change',()=>{
  document.getElementById('expirationPicker').innerHTML='';
  resetExpirationSelections();
  run();
});
bind('symbolSearch','focus',event=>searchSymbols(event.currentTarget.value===document.getElementById('symbol').value?'':event.currentTarget.value));
bind('symbolSearch','input',event=>searchSymbols(event.currentTarget.value));
bind('symbolSearch','keydown',event=>{
  if(event.key==='Escape'){
    closeSymbolSearchResults();
    event.currentTarget.value=document.getElementById('symbol').value;
    return;
  }
  if(event.key!=='Enter') return;
  event.preventDefault();
  const first=document.querySelector('#symbolSearchResults .symbol-search-result.active, #symbolSearchResults .symbol-search-result');
  const symbol=first?.dataset.symbolResult || normalizeSymbol(event.currentTarget.value);
  const name=first?.dataset.symbolName || symbol;
  selectMatrixSymbol(symbol,name);
});
bind('symbolSearchResults','click',event=>{
  const result=event.target.closest('[data-symbol-result]');
  if(result) selectMatrixSymbol(result.dataset.symbolResult,result.dataset.symbolName||'');
});
bind('symbolFavorites','click',event=>{
  const chip=event.target.closest('[data-symbol-chip]');
  if(chip) selectMatrixSymbol(chip.dataset.symbolChip,SYMBOL_NAMES.get(chip.dataset.symbolChip)||'');
});
bind('symbolFavorite','click',toggleCurrentSymbolFavorite);
document.addEventListener('click',event=>{
  if(!event.target.closest('.symbol-picker')) closeSymbolSearchResults();
});
bind('expirationPicker','change',()=>{
  saveCurrentExpirationSelection();
  syncExpiryQuickButtons();
  run();
});
if(byId('run')) byId('run').textContent = 'Refresh';
bind('run','click',()=>loadData(true));
window.addEventListener('resize',()=>{
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

document.querySelectorAll('.side-nav a[data-view]').forEach(link=>{
  link.addEventListener('click',e=>{
    e.preventDefault();
    setView(link.dataset.view);
  });
});

// parameter buttons - toggle metrics on/off (multi-select overlay)
document.querySelectorAll('.pbtn').forEach(btn=>{
  btn.addEventListener('click',()=>{
    const m=btn.dataset.metric;
    if(!m) return;
    if(ACTIVE.has(m)) ACTIVE.delete(m); else ACTIVE.add(m);
    btn.classList.toggle('active', ACTIVE.has(m));
    if(window._lastR && activeView === 'gex'){
      drawChart(window._lastR);
    }
  });
});

// expiration quick-select buttons (0DTE / Week / All)
document.querySelectorAll('.expiry-quick-btn').forEach(btn=>{
  btn.addEventListener('click',()=>applyExpirationQuickSelect(btn.dataset.expquick));
});

// ---------- Data loading (only on Refresh: Search, Enter or a button) ----------
let _timerUiTimer=null, _loadingData=false;
let _lastFileLoadedAt=null, _lastLoadOk=false;
let _lastDataSource='Waiting';
const MATRIX_CBOE_DATA_URL = 'https://api.trytripity.site/api/matrix/cboe-data';
const MATRIX_SYMBOL_SEARCH_URL = 'https://api.trytripity.site/api/matrix/symbol-search';
const MATRIX_SYMBOL_DATA_URL = 'https://api.trytripity.site/api/matrix/symbol-data';
const MARKET_FRESHNESS_MAX_MS = 75 * 60 * 1000;
const MARKET_FRESHNESS_START_MIN = 9 * 60 + 45;
const MARKET_FRESHNESS_END_MIN = 16 * 60 + 30;
function fetchLiveData(url){
  return fetch(url, {cache:'no-store'})
    .then(r=>r.ok?r.json():null)
    .catch(()=>null);
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
async function loadDynamicSymbol(symbol,thenRun=true){
  symbol=normalizeSymbol(symbol);
  while(_loadingData){
    setSymbolSearchStatus(`Waiting to load ${symbol}…`);
    await new Promise(resolve=>setTimeout(resolve,150));
  }
  _loadingData=true;
  setSymbolSearchStatus(`Loading ${symbol} option chain from Cboe…`);
  updateDataTimer();
  try{
    const response=await fetch(`${MATRIX_SYMBOL_DATA_URL}?symbol=${encodeURIComponent(symbol)}`,{cache:'no-store'});
    let payload=null;
    try{ payload=await response.json(); }catch(_err){ payload=null; }
    if(!response.ok || !payload?.ok || !payload.record){
      throw new Error(payload?.detail || `${symbol} option chain is unavailable`);
    }
    dynamicSymbolConfig(symbol,payload.record);
    REAL[symbol]=payload.record;
    DYNAMIC_SYMBOLS.add(symbol);
    ensureSymbolOption(symbol);
    document.getElementById('symbol').value=symbol;
    document.getElementById('symbolSearch').value=symbol;
    document.getElementById('expirationPicker').innerHTML='';
    resetExpirationSelections();
    renderSymbolFavorites();
    syncSymbolFavoriteButton();
    _lastDataSource='Cboe on-demand';
    _lastFileLoadedAt=Date.now();
    _lastLoadOk=true;
    setSymbolSearchStatus(`${symbol}: ${(payload.record.opts||[]).length.toLocaleString('en-US')} contracts · daily Cboe OI · Cboe spot`);
    if(thenRun!==false && document.getElementById('symbol').value===symbol) run();
    return true;
  }catch(error){
    setSymbolSearchStatus(error?.message||`Could not load ${symbol}`,'error');
    if(!REAL[symbol]){
      ensureSymbolOption(DEFAULT_SYMBOL);
      document.getElementById('symbol').value=DEFAULT_SYMBOL;
      document.getElementById('symbolSearch').value=DEFAULT_SYMBOL;
      renderSymbolFavorites();
      syncSymbolFavoriteButton();
      if(REAL[DEFAULT_SYMBOL]) run();
    }
    return false;
  }finally{
    _loadingData=false;
    updateDataTimer();
  }
}
function validCboeData(d){
  return d && d.SPX && Array.isArray(d.SPX.opts) && Number.isFinite(Number(d.SPX.spot));
}
function fetchCboeDataWithSource(url, sourceName){
  return fetchLiveData(url).then(d=>validCboeData(d) ? {data:d, source:sourceName} : null);
}
function dataAsofToMs(value){
  if(!value) return null;
  const text=String(value);
  let parsed;
  if(/Z$|[+-]\d{2}:?\d{2}$/.test(text)){
    parsed=Date.parse(text);
  } else {
    const match=text.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/);
    if(match){
      const [,y,m,d,h,min]=match;
      parsed=zonedDateTimeToUtc(Number(y),Number(m),Number(d),Number(h),Number(min),'America/New_York');
    } else {
      parsed=Date.parse(text);
    }
  }
  return Number.isFinite(parsed) ? parsed : null;
}
function fmtAsofShort(value){
  if(!value) return '--';
  const ms=dataAsofToMs(value);
  if(ms==null) return String(value);
  const time=new Intl.DateTimeFormat('en-US',{
    timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'
  }).format(new Date(ms));
  return `${time} ET`;
}
function fmtDataAge(ms){
  if(ms == null) return '--';
  const minutes=Math.max(0,Math.floor(ms/60000));
  if(minutes<60) return `${minutes}m`;
  return `${Math.floor(minutes/60)}h ${minutes%60}m`;
}
function dataHealthFromAge(ageMs){
  if(_loadingData) return {key:'checking', label:'Checking'};
  if(!_lastLoadOk) return {key:'failed', label:'Failed'};
  if(ageMs == null) return {key:'failed', label:'No Time'};
  const minutes=Math.max(0,ageMs/60000);
  if(minutes<=5) return {key:'fresh', label:'Live'};
  if(minutes<=15) return {key:'delayed', label:'Recent'};
  if(!isUsMarketFreshnessWindow()) return {key:'closed', label:'Market Closed'};
  return {key:'stale', label:'Stale'};
}
function getEtClockParts(date=new Date()){
  const parts=new Intl.DateTimeFormat('en-US',{
    timeZone:'America/New_York',
    weekday:'short',
    hour:'2-digit',
    minute:'2-digit',
    hourCycle:'h23'
  }).formatToParts(date);
  const get=type=>parts.find(part=>part.type===type)?.value;
  return {weekday:get('weekday'), hour:Number(get('hour')), minute:Number(get('minute'))};
}
function isUsMarketFreshnessWindow(date=new Date()){
  const et=getEtClockParts(date);
  if(['Sat','Sun'].includes(et.weekday)) return false;
  const minutes=et.hour*60+et.minute;
  return minutes>=MARKET_FRESHNESS_START_MIN && minutes<=MARKET_FRESHNESS_END_MIN;
}
function shouldShowStaleDataBanner(ageMs, date=new Date()){
  return ageMs!=null && ageMs>MARKET_FRESHNESS_MAX_MS && isUsMarketFreshnessWindow(date);
}
function forceStaleBannerForDebug(){
  return new URLSearchParams(window.location.search).get('debugStaleBanner') === '1';
}
function updateStaleDataBanner(rec, ageMs){
  const banner=document.getElementById('staleDataBanner');
  if(!banner) return;
  const shouldShow=forceStaleBannerForDebug() || shouldShowStaleDataBanner(ageMs);
  banner.hidden=!shouldShow;
  if(!shouldShow){
    banner.textContent='';
    return;
  }
  banner.innerHTML=`<b>Live feed delayed:</b> ${rec?.price_source||'LSE'} price is ${fmtDataAge(ageMs)} old, as of ${fmtAsofShort(rec?.price_asof||rec?.asof)}. OI remains a separate daily Cboe snapshot.`;
}
function updateDataTimer(){
  const card=document.getElementById('dataHealth');
  if(!card) return;
  const symbol=document.getElementById('symbol')?.value || DEFAULT_SYMBOL;
  const rec=REAL?.[symbol] || window._lastR;
  const liveAsof=rec?.price_asof || rec?.asof;
  const asofMs=dataAsofToMs(liveAsof);
  const ageMs=asofMs==null ? null : Date.now()-asofMs;
  const health=dataHealthFromAge(ageMs);
  card.classList.remove('fresh','delayed','closed','stale','failed');
  if(health.key!=='checking') card.classList.add(health.key);
  document.getElementById('dataHealthState').textContent = health.label;
  document.getElementById('dataHealthSymbol').textContent = symbol;
  document.getElementById('dataHealthSource').textContent = rec?.price_source || _lastDataSource;
  document.getElementById('dataHealthAge').textContent = fmtDataAge(ageMs);
  document.getElementById('dataHealthContracts').textContent = rec?.opts ? rec.opts.length.toLocaleString('en-US') : '--';
  document.getElementById('dataHealthAsof').textContent = fmtAsofShort(liveAsof);
  document.getElementById('dataHealthOiSource').textContent = rec?.oi_source==='cboe_daily' ? 'Cboe daily' : 'Cboe daily';
  document.getElementById('dataHealthOiAsof').textContent = fmtAsofShort(rec?.oi_asof);
  document.getElementById('dataHealthSpot').textContent = rec?.spot ? fmtPrice(rec.spot) : '--';
  document.getElementById('dataHealthLoaded').textContent = _lastFileLoadedAt ? new Date(_lastFileLoadedAt).toLocaleTimeString('he-IL') : '--';
  updateStaleDataBanner(rec, ageMs);
  const forceBtn=document.getElementById('forceTripityRefresh');
  if(forceBtn) forceBtn.disabled = _loadingData;
}
function loadData(thenRun){
  const currentSymbol=document.getElementById('symbol')?.value;
  if(DYNAMIC_SYMBOLS.has(currentSymbol)) return loadDynamicSymbol(currentSymbol,thenRun);
  if(_loadingData) return Promise.resolve();
  _loadingData = true;
  updateDataTimer();
  return fetchCboeDataWithSource(MATRIX_CBOE_DATA_URL + '?t=' + Date.now(), 'Tripity hybrid')
    .then(result=>{
      // A failed Refresh keeps the last good data on screen.
      _lastLoadOk=!!result || validCboeData(REAL);
      if(result){
        const dynamicRecords=Object.fromEntries([...DYNAMIC_SYMBOLS].filter(symbol=>REAL[symbol]).map(symbol=>[symbol,REAL[symbol]]));
        REAL={...result.data,...dynamicRecords};
        _lastDataSource=result.source;
        _lastFileLoadedAt=Date.now();
      }
    })
    .finally(()=>{
      _loadingData = false;
      if(thenRun!==false) run();
      updateDataTimer();
    });
}

populateSymbols();
document.getElementById('symbol').value = DEFAULT_SYMBOL;
document.getElementById('forceTripityRefresh').addEventListener('click',()=>loadData(true));
_timerUiTimer=setInterval(updateDataTimer,1000);
loadData(true);
