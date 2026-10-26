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
function preciseYearsToExpiry(exp,root,valuationMs){
  const [year,month,day]=String(exp).split('-').map(Number);
  // AM-settled index roots (9:30 AM ET expiry): SPX/NDX, plus VIX and its weeklies.
  const isAmSettled=root==='SPX'||root==='NDX'||root==='VIX'||root==='VIXW';
  const expiryMs=zonedDateTimeToUtc(year,month,day,isAmSettled?9:16,isAmSettled?30:0,'America/New_York');
  return Math.max((expiryMs-valuationMs)/(365.25*86400000),1/(365.25*24*60));
}

// Real market data is served directly by Tripity.
let REAL = {};
const RISK_FREE = 0.05;
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
  const r = RISK_FREE;
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
    return {K:o.k, dte:o.dte, T, exp:o.exp, root:o.root||'', isCall, iv:o.iv, oi:o.oi, vol:o.vol, g};
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
  strikes.forEach(s=>{ s.totalOI=s.callOI+s.putOI; s.totalVol=s.callVol+s.putVol; });
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
}
// Single view -> renderer dispatch, shared by renderImpl, setView and the resize handler.
const VIEW_RENDERERS = {
  'gex': R=>R && drawChart(R),
  'oi-volume': ()=>renderOiVolume(),
  'key-levels': ()=>renderKeyLevels(),
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
// Net GEX and AG start on, as in his 2026 screenshots (a guess at his default).
let ACTIVE = new Set(["net_gex","ag"]);
// Strikes shown: spot +/- 2 expected moves.
const DISPLAY_SIGMA = 2;
const METRICS = {
  net_gex:  {label:"Net GEX",     color:"#22b8ff", kind:"bar",  signed:true,  val:s=>s.netGex},
  ag:       {label:"AG",          color:"#ab7df6", kind:"area", axis:"gex", val:s=>Math.abs(s.callGex)+Math.abs(s.putGex)},
  call_oi:  {label:"Call OI",     color:"#26c281", kind:"area", axis:"count", val:s=>s.callOI},
  put_oi:   {label:"Put OI",      color:"#ef5350", kind:"area", axis:"count", val:s=>s.putOI},
  call_vol: {label:"Call Volume", color:"#2f73ff", kind:"area", axis:"count", val:s=>s.callVol},
  put_vol:  {label:"Put Volume",  color:"#ff8a3d", kind:"area", axis:"count", val:s=>s.putVol},
  power:    {label:"Power Zone",  color:"#ffc107", kind:"area", axis:"gex", val:s=>s.powerZone || 0},
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
  // One right-hand scale per active group, side by side.
  const rightAxes=['gex','count'].filter(ax=>areas.some(m=>METRICS[m].axis===ax));
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
    axisMax[ax]=Math.max(...layers.flatMap(m=>data.map(METRICS[m].val)),1)*1.08;
    axisTitle[ax]=layers.map(m=>METRICS[m].label.toUpperCase()).join(' - ');
  });

  // ----- gridlines + tick labels -----
  ctx.font=(mobile ? "bold 10px Segoe UI" : "bold 13px Segoe UI"); ctx.textBaseline="middle";
  const ticks=6;
  for(let t=0;t<=ticks;t++){
    const f=t/ticks, yy=bottom-f*plotH;
    ctx.strokeStyle="rgba(255,255,255,.025)"; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(padL,yy); ctx.lineTo(W-padR,yy); ctx.stroke();
    if(hasBar){ ctx.fillStyle="#c4c4c4"; ctx.textAlign="right";
      ctx.fillText(fmtAxis(lMin+(lMax-lMin)*f), padL-8, yy); }
    rightAxes.forEach((ax,i)=>{ ctx.fillStyle="#c4c4c4"; ctx.textAlign="left";
      ctx.fillText(fmtAxis(axisMax[ax]*f), W-padR+6+i*axisW, yy); });
  }

  // ----- area overlays: a smoothed spline with markers, filled to zero, like his -----
  areas.forEach(m=>{
    const M=METRICS[m];
    const yA=v=>bottom-(v/axisMax[M.axis])*plotH;
    const pts=data.map((s,i)=>[xCenter(i),yA(M.val(s))]);
    ctx.beginPath(); ctx.moveTo(pts[0][0],bottom); ctx.lineTo(pts[0][0],pts[0][1]);
    splineTo(ctx,pts,top,bottom);
    ctx.lineTo(pts[pts.length-1][0],bottom); ctx.closePath();
    ctx.fillStyle=hexA(M.color,.33); ctx.fill();
    ctx.beginPath(); ctx.moveTo(pts[0][0],pts[0][1]);
    splineTo(ctx,pts,top,bottom);
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
  // His label is just "Price: <spot>".
  const priceLabel = "Price: "+fmtPrice(R.spot);
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
function splineTo(ctx,pts,top,bottom){
  const k=SPLINE_SMOOTHING/6;
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
    if(m==='power') return row('Power Zone',fmtNum(s.powerZone || 0));
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
// It lists only the six nearest dates (a guess from one sample of his list) and
// starts on the nearest one (also a guess).
const EXPIRATION_COUNT = 6;
function currentExpiration(){
  return byId('expirationSelect')?.value || null;
}
function chainExpirations(chain){
  const todayIso = new Date().toISOString().slice(0,10);
  return [...new Set(chain.quotes.map(q=>q.exp).filter(exp=>exp && exp >= todayIso))].sort().slice(0,EXPIRATION_COUNT);
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
  if(view==='options-summary') loadPcRatio();
  if(view===OIV_VIEW){
    const symbol=screenTickers[OIV_VIEW];
    if(symbol===oiv.symbol && oiv.record) renderOiVolume();
    else refreshOiVolume(symbol);
    return;
  }
  if(view===KEY_LEVELS_VIEW){
    showKeyLevels();
    return;
  }
  if(view!==HOME_VIEW) return;
  const symbol=urlTicker || currentSymbol();
  if(symbol===currentSymbol() && homeRecord){
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
// The Strike Chart keeps its own copy of the chain it loaded, so a Search on
// another screen never changes what it shows (each screen has its own ticker).
let homeRecord = null;
function run(){
  const sym=currentSymbol();
  if(!homeRecord){
    setTickerStatus(HOME_VIEW,`${sym}: no data yet. Press Search to try again.`,'error');
    return;
  }
  let chain = buildChainRealFromRecord(sym,homeRecord);
  populateExpirations(chain);
  if(selectedExpiration){
    chain = {...chain, quotes: chain.quotes.filter(q=>q.exp===selectedExpiration)};
  }
  const R = calcGEX(chain);
  R.expectedMove = calcExpectedMove(chain);
  renderImpl(R);
}

// ---------- Data loading (only on Search or Enter) ----------
const MATRIX_CBOE_DATA_URL = 'https://api.trytripity.site/api/matrix/cboe-data';
const MATRIX_SYMBOL_DATA_URL = 'https://api.trytripity.site/api/matrix/symbol-data';
let _homeRequest=0;
function fetchLiveData(url){
  return fetch(url, {cache:'no-store'})
    .then(r=>r.ok?r.json():null)
    .catch(()=>null);
}
function validCboeData(d){
  return d && d.SPX && Array.isArray(d.SPX.opts) && Number.isFinite(Number(d.SPX.spot));
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
  homeRecord=REAL[symbol];
  screenTickers[HOME_VIEW]=symbol;
  if(activeView===HOME_VIEW){
    syncTickerInput(HOME_VIEW);
    history.replaceState(null,'',screenUrl(HOME_VIEW));
  }
  run();
  drawPriceCharts(symbol);
  setTickerStatus(HOME_VIEW,result.ok?'':`Refresh failed. Showing the last data for ${symbol}.`,result.ok?'':'error');
}

// ---------- Key Levels ----------
// Price for the searched ticker with its four Key Levels, and the Forecast under it.
// One Search loads both, the way his Key Levels page fills chart and text together.
const KEY_LEVELS_VIEW = 'key-levels';
// Guess: his bar size and time span are not public. 5-minute bars over five days
// show where price sits against levels built from every open expiry.
const KEY_LEVELS_CANDLES = {interval:'5m', range:'5d'};
// The four Key Levels, in the colours Matrix already used for them.
const KEY_LEVEL_STYLES = [
  {key:'callWall',       label:'Call Wall',  color:'#22b8ff'},
  {key:'putWall',        label:'Put Wall',   color:'#ff453a'},
  {key:'flip',           label:'Zero Gamma', color:'#f5f5f5'},
  {key:'maxGammaStrike', label:'Max Gamma',  color:'#8b5fe8'},
];
// Levels further than this from spot stay in the legend but off the chart,
// so one far wall cannot squash the candles into a line.
const KEY_LEVELS_MAX_DISTANCE = 0.08;
let keyLevelsState = null;  // {symbol, R, candles}
let _keyLevelsRequest = 0;

function keyLevels(R){
  return KEY_LEVEL_STYLES
    .map(style=>({...style, price:R?.[style.key]==null ? NaN : Number(R[style.key])}))
    .filter(level=>Number.isFinite(level.price) && level.price>0);
}
// No expiry choice on this screen: the GEX profile spans every open expiry (a guess;
// his page has no expiry control).
function keyLevelsGex(symbol){
  const chain=buildChainReal(symbol);
  const todayIso=new Date().toISOString().slice(0,10);
  const open=chain.quotes.filter(q=>!q.exp || q.exp>=todayIso);
  const R=calcGEX(open.length ? {...chain, quotes:open} : chain);
  return R;
}

// ----- Forecast -----
// Placeholder text: the rule-based Market Read and Scenario Ladder Matrix had before,
// laid out as his #market-forecast is (h3 title, h4 sections, h5 sub-heads).
// Build 9 replaces the content.
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
function buildForecast(R){
  const gross = Math.abs(R.totalCallGex)+Math.abs(R.totalPutGex)||1;
  const gexRatio = R.totalGex/gross;
  let bias = 'Neutral';
  let biasClass = 'neutral';
  if(R.regime === 'positive_gamma'){
    bias = 'Range / Mean Reversion';
    biasClass = 'range';
  } else if(R.regime === 'negative_gamma'){
    bias = 'Trend Risk / Momentum';
    biasClass = 'trend';
  }
  if(Math.abs(gexRatio) < 0.04){
    bias = 'Chop / Low Signal';
    biasClass = 'neutral';
  }
  const confidence = Math.min(100,Math.round(Math.abs(gexRatio)*260 + Math.min(35,Math.abs(R.distPct)*7)));
  const summary = biasClass === 'range'
    ? 'Dealers are more likely to dampen moves near current levels. Favor level-to-level reads until a wall breaks.'
    : biasClass === 'trend'
      ? 'Dealer hedging can amplify direction. Watch breaks above resistance or below support for acceleration.'
      : 'Current gamma signal is weak. Treat levels as reference zones and wait for confirmation.';
  const below = nearestKeyLevel(R,'below');
  const above = nearestKeyLevel(R,'above');
  const negative = R.regime === 'negative_gamma';
  return {
    bias, confidence, summary,
    scenarios:[
      {label:'Below', level:below, text:below
        ? `Below ${below.label} ${levelDisplayPrice(below.price,R)}: ${negative ? 'sell pressure can accelerate' : 'support hedge may appear'}.`
        : 'No Key Level below the current price.'},
      {label:'Now', level:{label:'Price', price:R.spot}, text:`At ${levelDisplayPrice(R.spot,R)}: ${bias}. Confidence ${confidence}%.`},
      {label:'Above', level:above, text:above
        ? `Above ${above.label} ${levelDisplayPrice(above.price,R)}: ${negative ? 'buy pressure can chase' : 'resistance hedge may cap'}.`
        : 'No Key Level above the current price.'},
    ],
  };
}
function forecastHtml(R){
  const f = buildForecast(R);
  const regime = String(R.regime||'').replace('_',' ');
  const scenarios = f.scenarios.map(s=>{
    const head = s.level ? `${s.label}: ${s.level.label} ${levelDisplayPrice(s.level.price,R)}` : s.label;
    return `<h5>${esc(head)}</h5><p>${esc(s.text)}</p>`;
  }).join('');
  return `
    <h3>Forecast for ${esc(R.symbol)}</h3>
    <h4>Market Read</h4>
    <h5>${esc(f.bias)}</h5>
    <p>${esc(f.summary)}</p>
    <ul>
      <li>Confidence: <b>${f.confidence}%</b></li>
      <li>Regime: <b>${esc(regime)}</b></li>
      <li>Net GEX: <b class="${R.totalGex>=0?'pos':'neg'}">${fmtNum(R.totalGex)}</b></li>
      <li>PCR: <b>${R.pcr.toFixed(2)}</b></li>
    </ul>
    <h4>Scenario Ladder</h4>
    ${scenarios}
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

  // the four Key Levels across the chart, labelled on the right
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

  // current price
  const py = y(R.spot);
  ctx.save(); ctx.setLineDash([3,4]); ctx.strokeStyle = "#d58b16"; ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.moveTo(padL,py); ctx.lineTo(padL+plotW,py); ctx.stroke(); ctx.restore();
  ctx.fillStyle = "#d58b16"; ctx.textAlign = "left"; ctx.textBaseline = "bottom";
  ctx.fillText(`Price ${fmtPrice(R.spot)}`, padL+4, py-3);

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

// ----- Loading (Search, Enter, or opening the screen with nothing loaded) -----
async function fetchKeyLevelsCandles(symbol){
  const {interval, range} = KEY_LEVELS_CANDLES;
  const data = await fetchLiveData(`${MATRIX_CANDLES_URL}?symbol=${encodeURIComponent(symbol)}&interval=${interval}&range=${range}&t=${Date.now()}`);
  if(!data?.ok || !Array.isArray(data.candles)) return null;
  return data.candles.filter(c=>[c.time,c.open,c.high,c.low,c.close].every(Number.isFinite));
}
function renderKeyLevels(){
  if(activeView!==KEY_LEVELS_VIEW || !keyLevelsState) return;
  drawKeyLevelsChart(keyLevelsState);
  const host = byId('forecast');
  if(host) host.innerHTML = forecastHtml(keyLevelsState.R);
}
async function loadKeyLevels(symbol){
  const request = ++_keyLevelsRequest;
  syncTickerInput(KEY_LEVELS_VIEW,symbol);
  setTickerStatus(KEY_LEVELS_VIEW,`Loading ${symbol}…`);
  const [result, candles] = await Promise.all([
    isCoreSymbol(symbol) ? loadCoreData() : loadDynamicSymbol(symbol),
    fetchKeyLevelsCandles(symbol),
  ]);
  if(request!==_keyLevelsRequest) return;  // a newer Search took over
  if(!REAL[symbol]){
    // Nothing to show: keep the ticker that is on screen.
    if(keyLevelsState) screenTickers[KEY_LEVELS_VIEW] = keyLevelsState.symbol;
    syncTickerInput(KEY_LEVELS_VIEW);
    setTickerStatus(KEY_LEVELS_VIEW,result.detail || `${symbol}: no data.`,'error');
    return;
  }
  keyLevelsState = {symbol, R:keyLevelsGex(symbol), candles:candles || []};
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
  else loadKeyLevels(symbol);
}

// ---------- OI Volume ----------
// His OI Volume page: ticker + Search, one expiration date, five multi-select
// buttons over a per-strike chart, and a price chart with OI/volume strike lines.
const OIV_VIEW = 'oi-volume';
// Volume Spread = call volume minus put volume per strike, for the session so far
// (map #21, #24). It is our guess: no public source defines it. Bars in Net GEX colours.
const OIV_METRICS = {
  volume_spread: {label:'Volume Spread', color:'#22b8ff', kind:'bar',  val:s=>s.volumeSpread},
  call_oi:       {label:'Call OI',       color:'#26c281', kind:'area', val:s=>s.callOI},
  put_oi:        {label:'Put OI',        color:'#ef5350', kind:'area', val:s=>s.putOI},
  call_vol:      {label:'Call Vol',      color:'#2f73ff', kind:'area', val:s=>s.callVol},
  put_vol:       {label:'Put Vol',       color:'#ff8a3d', kind:'area', val:s=>s.putVol},
};
const OIV_ACTIVE = new Set(['volume_spread']);
const MATRIX_CANDLES_URL = 'https://api.trytripity.site/api/matrix/candles';
// record: this screen's own copy of the chain, so other screens' Searches leave it alone.
const oiv = {symbol:null, record:null, expiry:null, candles:null, candlesError:''};
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
  const todayIso = new Date().toISOString().slice(0,10);
  return [...new Set(chain.quotes.map(q=>q.exp).filter(exp=>exp && exp>=todayIso))].sort();
}
function computeOiVolume(symbol, expiry, record=oiv.record){
  const chain = buildChainRealFromRecord(symbol, record);
  const quotes = chain.quotes.filter(q=>q.exp===expiry);
  const scoped = {...chain, quotes};
  return {symbol, spot:chain.spot, expiry, strikes:aggregateOiVolume(quotes), expectedMove:calcExpectedMove(scoped)};
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
function populateOiVolumeExpiries(symbol){
  const select = byId('oivExpiry');
  const expiries = oiVolumeExpiries(buildChainRealFromRecord(symbol, oiv.record));
  if(!expiries.includes(oiv.expiry)) oiv.expiry = expiries[0] || null;
  if(select){
    select.innerHTML = expiries.length
      ? expiries.map(exp=>`<option value="${esc(exp)}"${exp===oiv.expiry?' selected':''}>${esc(exp)}</option>`).join('')
      : '<option value="">No expirations</option>';
    select.disabled = !expiries.length;
  }
}
function renderOiVolume(){
  const symbol = oiv.symbol;
  if(!symbol || !oiv.record) return;
  // The chain may have changed since the list was built: refill it, and fall
  // back to the nearest expiration when the chosen one is gone.
  populateOiVolumeExpiries(symbol);
  if(!oiv.expiry) return;
  const R = computeOiVolume(symbol, oiv.expiry);
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
  const ownMax={}; areas.forEach(m=>ownMax[m]=Math.max(...data.map(OIV_METRICS[m].val),1));
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
    ctx.beginPath(); ctx.moveTo(xCenter(0),bottom);
    data.forEach((s,i)=>ctx.lineTo(xCenter(i),yA(M.val(s))));
    ctx.lineTo(xCenter(data.length-1),bottom); ctx.closePath();
    ctx.fillStyle=hexA(M.color,.33); ctx.fill();
    ctx.beginPath();
    data.forEach((s,i)=>{const x=xCenter(i),y=yA(M.val(s)); i?ctx.lineTo(x,y):ctx.moveTo(x,y);});
    ctx.strokeStyle=M.color; ctx.lineWidth=1.6; ctx.stroke();
    ctx.fillStyle=M.color;
    data.forEach((s,i)=>{ctx.beginPath(); ctx.arc(xCenter(i),yA(M.val(s)),1.8,0,7); ctx.fill();});
  });

  const hitBars = data.map((s,i)=>({x:padL+slot*i,y:top,w:slot,h:plotH,s}));
  if(hasBar){
    const bw=Math.max(2,Math.min(slot*0.70,32));
    data.forEach((s,i)=>{
      const v=barMetric.val(s), yy=yL(v);
      const x=xCenter(i)-bw/2;
      ctx.fillStyle = v>=0 ? '#22aaf2' : '#ff2417';
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
  const priceLabel='Price: '+fmtPrice(R.spot);
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
    const cls=m==='volume_spread' ? (v>=0?'pos':'neg') : (m.startsWith('call')?'pos':'neg');
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
  const lines=oiVolumePriceLines(R.strikes);
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
// Refresh of OI Volume: Search, Enter, or opening it with no data yet.
async function refreshOiVolume(symbol){
  const request=++_oivRequest;
  const previous=oiv.symbol;
  syncTickerInput(OIV_VIEW,symbol);
  setTickerStatus(OIV_VIEW,`Loading ${symbol}…`);
  const [result,price]=await Promise.all([
    isCoreSymbol(symbol) ? loadCoreData() : loadDynamicSymbol(symbol),
    loadOiVolumeCandles(symbol),
  ]);
  if(request!==_oivRequest) return;  // a newer Search took over
  if(!REAL[symbol]){
    setTickerStatus(OIV_VIEW,result.detail,'error');
    if(previous) syncTickerInput(OIV_VIEW,previous);
    return;
  }
  if(symbol!==previous) oiv.expiry=null;
  oiv.symbol=symbol;
  oiv.record=REAL[symbol];
  oiv.candles=price.candles;
  oiv.candlesError=price.error;
  screenTickers[OIV_VIEW]=symbol;
  if(activeView===OIV_VIEW) history.replaceState(null,'',screenUrl(OIV_VIEW));
  populateOiVolumeExpiries(symbol);
  renderOiVolume();
  setTickerStatus(OIV_VIEW,result.ok?'':`Refresh failed. Showing the last data for ${symbol}.`,result.ok?'':'error');
}

// ---------- P/C Ratio ----------
// Like his options-summary table: loads every time the screen opens, no ticker or
// expiry input, one row per core ticker. Totals span all expiries and strikes and
// come from the server, so they are not cut by the Strike Chart's strike range.
const PC_TICKERS = ['SPX','NDX','SPY','QQQ'];
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
  const data=await fetchLiveData(MATRIX_CBOE_DATA_URL + '?t=' + Date.now());
  if(request!==_pcRequest) return;
  if(!data || !PC_TICKERS.some(symbol=>data[symbol]?.totals)){
    setPcStatus('Could not load market data. Open the screen again to retry.','error');
    return;
  }
  renderPcTable(data);
  const asof=PC_TICKERS.map(symbol=>data[symbol]?.asof).find(Boolean);
  setPcStatus(asof ? `As of ${String(asof).replace('T',' ')}` : '');
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
  const viewRenderer=VIEW_RENDERERS[activeView];
  if(viewRenderer) viewRenderer(window._lastR);
});
// OI Volume: the date and the buttons redraw from the loaded chain; only Search reloads.
bind('oivExpiry','change',event=>{
  oiv.expiry=event.target.value || null;
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

applyLocation();
