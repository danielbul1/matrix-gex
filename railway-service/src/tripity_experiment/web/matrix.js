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
function calcOptionPrice(S,K,T,sigma,r,isCall){
  T = Math.max(T, 1/(365.25*24*60));
  sigma = Math.max(sigma, 0.01);
  const sqrtT = Math.sqrt(T);
  const d1 = (Math.log(S/K)+(r+0.5*sigma*sigma)*T)/(sigma*sqrtT);
  const d2 = d1 - sigma*sqrtT;
  if(isCall) return S*normCDF(d1) - K*Math.exp(-r*T)*normCDF(d2);
  return K*Math.exp(-r*T)*normCDF(-d2) - S*normCDF(-d1);
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
  NDX:{spot:30570,  step:25, mult:100, baseIV:0.170, market:"US"},
  SPX:{spot:7550,   step:5,  mult:100, baseIV:0.140, market:"US"},
  SPY:{spot:580.50, step:5,  mult:100, baseIV:0.135, market:"US"},
  QQQ:{spot:525.50, step:1,  mult:100, baseIV:0.165, market:"US"},
  VIX:{spot:20,     step:1,  mult:100, baseIV:0.80,  market:"US"},
};
// Real market data is served directly by Tripity. Empty data falls back to synthetic chains.
let REAL = {};
let REAL_ASOF = {};
const RISK_FREE = {US:0.05, IN:0.065};
const SPX_SPY_RATIO = 10.03657299922611;
// User-provided conversion example: QQQ 752.00 = NDX 30,916.24.
const NDX_QQQ_RATIO = 30916.24 / 752;
const DEFAULT_SYMBOL = 'NDX';

function mulberry32(a){return function(){a|=0;a=a+0x6D2B79F5|0;let t=Math.imul(a^a>>>15,1|a);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296;};}

// ---------- Synthetic option chain (stands in for Alpaca/Dhan connector) ----------
function buildChain(symKey, spot){
  const cfg = SYMBOLS[symKey];
  const rnd = mulberry32(symKey.split('').reduce((a,c)=>a+c.charCodeAt(0),0) + Math.round(spot));
  const r = RISK_FREE[cfg.market];
  const N = 22;
  const atm = Math.round(spot/cfg.step)*cfg.step;
  const expiries = cfg.market==="US" ? [0/365, 2/365, 6/365] : [3/365, 9/365];
  const quotes = [];
  const now = Date.now();

  for(let i=-N;i<=N;i++){
    const K = atm + i*cfg.step;
    if(K<=0) continue;
    const moneyness = (K-spot)/spot;
    const smile = cfg.baseIV*(1 + 1.4*moneyness*moneyness);   // volatility smile
    const putSkew = 0.020*Math.max(0,-moneyness)/0.1;         // puts richer

    expiries.forEach((T,ei)=>{
      const dte = Math.round(T*365);
      const exp = new Date(now + dte*86400000).toISOString().slice(0,10);
      const decay = Math.exp(-Math.pow(i/8,2));               // OI concentrates near ATM
      const expiryW = ei===0?1.0:(ei===1?0.85:0.5);
      const callIV = Math.max(0.05, smile - putSkew*0.3 + (rnd()-0.5)*0.01);
      const putIV  = Math.max(0.05, smile + putSkew + (rnd()-0.5)*0.01);
      const callOI = Math.round((6000 + 50000*decay*expiryW)*(moneyness>0?1.15:0.85)*(0.7+rnd()*0.6));
      const putOI  = Math.round((6000 + 50000*decay*expiryW)*(moneyness<0?1.15:0.80)*(0.7+rnd()*0.6));
      const callVol= Math.round(callOI*(0.15+rnd()*0.4));
      const putVol = Math.round(putOI *(0.15+rnd()*0.4));
      const ts = now - Math.round(rnd()*12000);              // per-strike quote timestamp

      quotes.push({K, dte, T, exp, isCall:true,  iv:callIV, oi:callOI, vol:callVol, ts,
                   g:calcGreeks(spot,K,T,callIV,r,true)});
      quotes.push({K, dte, T, exp, isCall:false, iv:putIV,  oi:putOI,  vol:putVol,  ts,
                   g:calcGreeks(spot,K,T,putIV,r,false)});
    });
  }
  return {symbol:symKey, market:cfg.market, spot, mult:cfg.mult, fetchTs:now, quotes};
}

// ---------- Real chain from Tripity ----------
// Uses official daily OI plus the freshest available gamma/delta/IV/volume fields.
function buildChainReal(symKey, spotOverride){
  const rec = REAL[symKey];
  return buildChainRealFromRecord(symKey,rec,spotOverride);
}
function buildChainRealFromRecord(symKey, rec, spotOverride){
  const spot = rec.spot || spotOverride;
  const r = RISK_FREE[SYMBOLS[symKey] ? SYMBOLS[symKey].market : "US"] || 0.05;
  const now = parseSourceTimestamp(rec.asof);
  const quotes = rec.opts.map(o=>{
    const isCall = o.t==="C";
    const T = preciseYearsToExpiry(o.exp,o.root||'',now);
    // The upstream does not always provide gamma; fall back to Black-Scholes when missing.
    const modelGreeks = calcGreeks(spot, o.k, T, o.iv>0?o.iv:0.2, r, isCall);
    const recalcFromLiveIndex = String(rec.source||'').includes('lse_live_index');
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

function analyticsOptionPrice(chain,q){
  if(Number.isFinite(q.last) && q.last>0) return q.last;
  const r = RISK_FREE[chain.market] || 0.05;
  return calcOptionPrice(chain.spot, q.K, q.T || q.dte/365, q.iv || 0.2, r, q.isCall);
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
function calcGEX(chain, mode){
  const {spot, mult} = chain;
  const cfg = {
    intraday:{dte:7,  range:0.20, maxR:0.10, flipR:0.15},
    "0dte":  {dte:0,  range:0.15, maxR:0.08, flipR:0.12},
    full:    {dte:999,range:5.0,  maxR:5.0,  flipR:5.0},
  }[mode];

  // 1. filter (DTE + strike range + liquidity)
  let kept = chain.quotes.filter(q=>{
    if(mode==="0dte"){ if(q.dte>0) return false; }
    else if(q.dte>cfg.dte) return false;
    if(Math.abs(q.K-spot)/spot > cfg.range) return false;
    if(q.oi < 10) return false;
    return true;
  });
  if(mode==="0dte" && kept.length===0){          // fallback to shortest expiry
    const minDte = Math.min(...chain.quotes.map(q=>q.dte));
    kept = chain.quotes.filter(q=>q.dte===minDte && Math.abs(q.K-spot)/spot<=cfg.range && q.oi>=10);
  }

  // 2. aggregate by strike (GEX = Gamma x OI x mult x Spot^2 x 1%, calls +, puts -)
  //    x0.01 = "per 1% move" - the standard GEX convention (SqueezeMetrics),
  //    keeps totals in the documented $-billions range.
  const byStrike = {};
  for(const q of kept){
    const gex = q.g.gamma * q.oi * mult * spot * spot * 0.01;
    if(!byStrike[q.K]) byStrike[q.K] = {strike:q.K, callGex:0, putGex:0, netGex:0,
        callDex:0, putDex:0, netDex:0,
        callVex:0, putVex:0, netVex:0, callCharm:0, putCharm:0, netCharm:0,
        callOI:0, putOI:0, callVol:0, putVol:0, callIV:0, putIV:0, gamma:0, ts:q.ts};
    const s = byStrike[q.K];
    const dex = (q.g.delta || 0) * q.oi * mult * spot * 0.01;
    const vex = (q.g.vanna || 0) * q.oi * mult * spot * 0.01;
    const charm = (q.g.charm || 0) * q.oi * mult * spot / 252;
    if(q.isCall){
      s.callGex += gex; s.callDex += dex; s.callVex += vex; s.callCharm += charm;
      s.callOI+=q.oi; s.callVol+=q.vol; s.callIV=q.iv;
    } else {
      s.putGex -= gex; s.putDex += dex; s.putVex -= vex; s.putCharm -= charm;
      s.putOI +=q.oi; s.putVol +=q.vol; s.putIV =q.iv;
    }
    s.gamma = Math.max(s.gamma, q.g.gamma);
    s.netGex = s.callGex + s.putGex;
    s.netDex = s.callDex + s.putDex;
    s.netVex = s.callVex + s.putVex;
    s.netCharm = s.callCharm + s.putCharm;
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
  const totalCallDex = strikes.reduce((a,s)=>a+s.callDex,0);
  const totalPutDex  = strikes.reduce((a,s)=>a+s.putDex,0);
  const totalDex     = totalCallDex + totalPutDex;
  const netCallOI = strikes.reduce((a,s)=>a+s.callOI,0);
  const netPutOI  = strikes.reduce((a,s)=>a+s.putOI,0);
  const pcr = netCallOI>0 ? netPutOI/netCallOI : 0;

  // 4. max gamma strike (within +/-maxR of spot)
  let maxGammaStrike=spot, maxAbs=0;
  for(const s of strikes){
    if(Math.abs(s.strike-spot)/spot<=cfg.maxR && Math.abs(s.netGex)>maxAbs){
      maxAbs=Math.abs(s.netGex); maxGammaStrike=s.strike;
    }
  }

  // 5. zero-gamma flip - cumulative net GEX zero crossing + interpolation
  let flip=null;
  const inFlip = strikes.filter(s=>Math.abs(s.strike-spot)/spot<=cfg.flipR);
  let cum=0, prevCum=null, prevK=null;
  for(const s of inFlip){
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
    strikes, totalGex, totalCallGex, totalPutGex, totalDex, totalCallDex, totalPutDex,
    maxGammaStrike, flip, callWall, putWall, callWallGex:cwG, putWallGex:pwG,
    regime, strength, distPct, pcr, netCallOI, netPutOI, callGexPct,
    avgCallIV, avgPutIV, ivSkew, sentiment, atmGreeks:atmQ?atmQ.g:null,
    keptCount:kept.length, totalCount:chain.quotes.length,
    expiries:[...new Set(kept.map(q=>q.dte))].sort((a,b)=>a-b)};
}
function avg(a){return a.length?a.reduce((x,y)=>x+y,0)/a.length:0;}

function scenarioStepForSymbol(symbol){
  if(symbol==='SPX') return 10;
  if(symbol==='NDX') return 50;
  const step=SYMBOLS[symbol]?.step || 5;
  return Math.max(step,1);
}
function rebuildChainAtSpot(chain, scenarioSpot){
  const r = RISK_FREE[chain.market || "US"] || 0.05;
  return {
    ...chain,
    spot: scenarioSpot,
    quotes: chain.quotes.map(q=>{
      const iv = q.iv>0 ? q.iv : 0.2;
      return {...q, g: calcGreeks(scenarioSpot,q.K,q.T || q.dte/365,iv,r,q.isCall)};
    })
  };
}
function clamp(n,min,max){ return Math.max(min,Math.min(max,n)); }
function signedBookDelta(chain, scenarioSpot, {ivShift=0, hoursForward=0}={}){
  const r = RISK_FREE[chain.market || "US"] || 0.05;
  const yearsForward=Math.max(0,hoursForward)/(365.25*24);
  return chain.quotes.reduce((sum,q)=>{
    const T=Math.max((q.T || q.dte/365 || 0)-yearsForward,1/(365.25*24*60));
    const iv=Math.max((q.iv || 0.2)+ivShift,0.01);
    const g=calcGreeks(scenarioSpot,q.K,T,iv,r,q.isCall);
    const directionalSign=q.isCall ? 1 : -1;
    return sum + directionalSign*g.delta*q.oi*(chain.mult||100);
  },0);
}
function dealerHedgeDelta(chain, scenarioSpot, opts){
  return -signedBookDelta(chain,scenarioSpot,opts);
}
function buildDealerFlowMap(chain){
  const step=scenarioStepForSymbol(chain.symbol);
  const base=Math.round(chain.spot/step)*step;
  const offsets=[-5,-3,-1,0,1,3,5].map(x=>x*step);
  const currentHedge=dealerHedgeDelta(chain,chain.spot);
  const currentBook=signedBookDelta(chain,chain.spot);
  const rows=offsets.map(offset=>base+offset)
    .filter((spot,i,arr)=>spot>0 && arr.indexOf(spot)===i)
    .map(spot=>{
      const pctMove=(spot-chain.spot)/chain.spot;
      const ivShift=clamp(-pctMove,-0.025,0.025);
      const hedgeAtSpot=dealerHedgeDelta(chain,spot);
      const hedgeAtSpotVol=dealerHedgeDelta(chain,spot,{ivShift});
      const hedgeAtSpotTime=dealerHedgeDelta(chain,spot,{hoursForward:1});
      const hedgeFull=dealerHedgeDelta(chain,spot,{ivShift,hoursForward:1});
      const gammaFlow=hedgeAtSpot-currentHedge;
      const vannaFlow=hedgeAtSpotVol-hedgeAtSpot;
      const charmFlow=hedgeAtSpotTime-hedgeAtSpot;
      const netFlow=hedgeFull-currentHedge;
      const abs=Math.abs(netFlow);
      const cls=Math.abs(spot-chain.spot)<=step/2 ? 'pin' : (netFlow>=0?'buy':'sell');
      const label=cls==='pin' ? 'Pin / Now' : (netFlow>0 ? 'Forced Buy' : 'Forced Sell');
      return {spot,delta:spot-chain.spot,pctMove,ivShift,gammaFlow,vannaFlow,charmFlow,netFlow,abs,cls,label,currentBook};
    });
  const maxFlow=Math.max(...rows.map(r=>r.abs),1);
  rows.forEach(r=>{
    r.score=r.netFlow/maxFlow;
    r.intensity=Math.min(1,r.abs/maxFlow);
  });
  const above=rows.filter(r=>r.spot>chain.spot).sort((a,b)=>a.spot-b.spot)[0];
  const below=rows.filter(r=>r.spot<chain.spot).sort((a,b)=>b.spot-a.spot)[0];
  const regime=Math.abs(currentBook)<1000 ? 'Balanced dealer book'
    : currentBook>0 ? 'Dealers likely hedged short futures' : 'Dealers likely hedged long futures';
  return {rows,currentHedge,currentBook,regime,above,below};
}
function classifyScenario(currentSpot, scenarioSpot, gexResult){
  const delta=scenarioSpot-currentSpot;
  const isAbove=delta>0;
  const isBelow=delta<0;
  const mag=Math.abs(gexResult.totalGex);
  const gross=Math.abs(gexResult.totalCallGex)+Math.abs(gexResult.totalPutGex)||1;
  const strength=mag/gross;
  if(Math.abs(delta)<=scenarioStepForSymbol(gexResult.symbol)/2){
    return {label:'Pin / Current', cls:'pin', pressure:'Balanced'};
  }
  if(gexResult.totalGex<0 && isAbove) return {label:'Acceleration Up', cls:'accel-up', pressure:'Buy pressure'};
  if(gexResult.totalGex<0 && isBelow) return {label:'Acceleration Down', cls:'accel-down', pressure:'Sell pressure'};
  if(gexResult.totalGex>0 && isBelow) return {label:'Support', cls:'support', pressure:'Dip-buy hedge'};
  if(gexResult.totalGex>0 && isAbove) return {label:'Resistance', cls:'resistance', pressure:'Rally-sell hedge'};
  return {label:strength<.04?'Chop':'Mixed', cls:'pin', pressure:'Mixed'};
}
function buildDealerScenarios(chain, mode){
  const step=scenarioStepForSymbol(chain.symbol);
  const base=Math.round(chain.spot/step)*step;
  const offsets=[-5,-3,-1,0,1,3,5].map(x=>x*step);
  return offsets.map(offset=>base+offset)
    .filter((spot,i,arr)=>spot>0 && arr.indexOf(spot)===i)
    .map(spot=>{
      const scenarioR=calcGEX(rebuildChainAtSpot(chain,spot),mode);
      const meta=classifyScenario(chain.spot,spot,scenarioR);
      return {
        spot,
        delta:spot-chain.spot,
        totalGex:scenarioR.totalGex,
        regime:scenarioR.regime,
        flip:scenarioR.flip,
        callWall:scenarioR.callWall,
        putWall:scenarioR.putWall,
        ...meta,
      };
    });
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
function fmtMove(n){
  const sign=n>=0?'+':'-';
  return sign+fmtPrice(Math.abs(n));
}
function fmtMoney(n){
  const sign = n < 0 ? "-" : "";
  const a = Math.abs(n);
  if(a>=1e9) return `${sign}$${(a/1e9).toFixed(2)} B`;
  if(a>=1e6) return `${sign}$${(a/1e6).toFixed(2)} M`;
  if(a>=1e3) return `${sign}$${(a/1e3).toFixed(2)} K`;
  return `${sign}$${a.toFixed(0)}`;
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
function ts(t){return new Date(t).toISOString().substring(11,19)+"Z";}
function byId(id){ return document.getElementById(id); }
function bind(id,event,handler,opts){
  const el=byId(id);
  if(el) el.addEventListener(event,handler,opts);
}
function safeText(value,fallback='--'){
  return value == null || value === '' ? fallback : value;
}

// ---------- Market read layer ----------
function levelDisplayPrice(value,R){
  if(value == null || !Number.isFinite(Number(value))) return '--';
  const base = fmtPrice(Number(value));
  const linked = linkedMarketPrice(Number(value),R);
  if(!linked || linked.price == null) return base;
  return `${base} / ${linked.symbol} ${fmtLinkedMarketPrice(linked)}`;
}
function nearestStrikeLevel(R,side){
  if(!R?.strikes?.length) return null;
  const rows = R.strikes
    .filter(s=>side === 'above' ? s.strike > R.spot : s.strike < R.spot)
    .sort((a,b)=>side === 'above' ? a.strike-b.strike : b.strike-a.strike);
  return rows[0] || null;
}
function dataQualityForMarketRead(R){
  const asofMs = dataAsofToMs(R?.asof);
  const ageMs = asofMs == null ? null : Date.now()-asofMs;
  const health = dataHealthFromAge(ageMs);
  return {
    state: R?.live ? health.label : 'Synthetic',
    cls: R?.live ? health.key : 'delayed',
    age: fmtDataAge(ageMs),
    asof: fmtAsofShort(R?.asof),
    source: _lastDataSource || (R?.live ? 'CBOE' : 'Synthetic'),
  };
}
function buildMarketRead(R){
  const gross = Math.abs(R.totalCallGex)+Math.abs(R.totalPutGex)||1;
  const gexRatio = R.totalGex/gross;
  const above = nearestStrikeLevel(R,'above');
  const below = nearestStrikeLevel(R,'below');
  const maxPain = R.maxPain?.active?.maxPain ?? null;
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
  return {
    bias,biasClass,confidence,summary,
    dataQuality:dataQualityForMarketRead(R),
    quality:[
      {label:'Data', value:dataQualityForMarketRead(R).state, tone:dataQualityForMarketRead(R).cls},
      {label:'Quotes', value:`${R.keptCount}/${R.totalCount}`, tone:R.keptCount>=500?'good':R.keptCount>=100?'warn':'bad'},
      {label:'Gamma Strength', value:`${Math.round(Math.abs(gexRatio)*100)}%`, tone:Math.abs(gexRatio)>=0.08?'good':'warn'},
      {label:'Expiry', value:(R.expiries || []).join(', ') || '--', tone:(R.expiries || []).length?'good':'warn'},
    ],
    keyLevels:[
      {label:'Spot', value:R.spot, note:'Current reference price'},
      {label:'Zero Gamma', value:R.flip, note:'Regime flip zone'},
      {label:'Max Gamma', value:R.maxGammaStrike, note:'Pin / pivot area'},
      {label:'Call Wall', value:R.callWall, note:'Upside resistance'},
      {label:'Put Wall', value:R.putWall, note:'Downside support'},
      {label:'Max Pain', value:maxPain, note:'Expiration pain point'},
    ],
    scenarios:[
      {label:'Below', level:below?.strike, tone:'down', text:below ? `Below ${levelDisplayPrice(below.strike,R)}: ${R.regime === 'negative_gamma' ? 'sell pressure can accelerate' : 'support hedge may appear'}.` : 'No lower strike in filtered range.'},
      {label:'Now', level:R.spot, tone:'now', text:`At ${levelDisplayPrice(R.spot,R)}: ${bias}. Confidence ${confidence}%.`},
      {label:'Above', level:above?.strike, tone:'up', text:above ? `Above ${levelDisplayPrice(above.strike,R)}: ${R.regime === 'negative_gamma' ? 'buy pressure can chase' : 'resistance hedge may cap'}.` : 'No upper strike in filtered range.'},
    ],
  };
}
function renderCommandCenter(R){
  const read = R.marketRead;
  const readHost = byId('marketReadPanel');
  if(readHost){
    readHost.innerHTML = `
      <div class="read-main ${read.biasClass}">
        <span class="read-kicker">Market Read</span>
        <strong>${esc(read.bias)}</strong>
        <p>${esc(read.summary)}</p>
      </div>
      <div class="read-stats">
        <div><span>Confidence</span><b>${read.confidence}%</b></div>
        <div><span>Regime</span><b>${esc(R.regime.replace('_',' '))}</b></div>
        <div><span>Net GEX</span><b class="${R.totalGex>=0?'pos':'neg'}">${fmtNum(R.totalGex)}</b></div>
        <div><span>PCR</span><b>${R.pcr.toFixed(2)}</b></div>
        <div class="data-state ${read.dataQuality.cls}"><span>Data</span><b>${esc(read.dataQuality.state)}</b><small>${esc(read.dataQuality.age)} / ${esc(read.dataQuality.source)}</small></div>
      </div>
    `;
  }
  const levelsHost = byId('keyLevelsPanel');
  if(levelsHost){
    levelsHost.innerHTML = read.keyLevels.map(item=>`
      <div class="level-tile">
        <span>${esc(item.label)}</span>
        <b>${levelDisplayPrice(item.value,R)}</b>
        <small>${esc(item.note)}</small>
      </div>
    `).join('');
  }
  const scenarioHost = byId('scenarioLadderPanel');
  if(scenarioHost){
    scenarioHost.innerHTML = read.scenarios.map(item=>`
      <div class="scenario-tile ${item.tone}">
        <span>${esc(item.label)}</span>
        <b>${levelDisplayPrice(item.level,R)}</b>
        <p>${esc(item.text)}</p>
      </div>
    `).join('');
  }
  const qualityHost = byId('signalQualityPanel');
  if(qualityHost){
    qualityHost.innerHTML = read.quality.map(item=>`
      <div class="quality-tile ${esc(item.tone || '')}">
        <span>${esc(item.label)}</span>
        <b>${esc(item.value)}</b>
      </div>
    `).join('');
  }
}

// ---------- Shock engine ----------
function shockPriceRange(R){
  const em = Number(R.expectedMove) || R.spot * 0.01;
  const candidates = [R.spot, R.callWall, R.putWall, R.flip, R.maxGammaStrike]
    .filter(v=>Number.isFinite(Number(v))).map(Number);
  const low = Math.min(...candidates, R.spot - em * 1.8);
  const high = Math.max(...candidates, R.spot + em * 1.8);
  const pad = Math.max((high-low)*0.18, R.spot*0.003);
  return {low:low-pad, high:high+pad, em};
}
function interpolateStrikeMetric(strikes, price, key){
  if(!strikes?.length) return 0;
  if(price <= strikes[0].strike) return Number(strikes[0][key]) || 0;
  const last = strikes[strikes.length-1];
  if(price >= last.strike) return Number(last[key]) || 0;
  for(let i=1;i<strikes.length;i++){
    const a=strikes[i-1], b=strikes[i];
    if(price <= b.strike){
      const span = b.strike-a.strike || 1;
      const t = (price-a.strike)/span;
      return (Number(a[key])||0) + ((Number(b[key])||0)-(Number(a[key])||0))*t;
    }
  }
  return 0;
}
function buildShockEngine(R){
  const {low,high,em}=shockPriceRange(R);
  const steps=90;
  const maxAbsStrikeGex=Math.max(...R.strikes.map(s=>Math.abs(s.netGex)),1);
  const gross=Math.abs(R.totalCallGex)+Math.abs(R.totalPutGex)||1;
  const points=[];
  for(let i=0;i<=steps;i++){
    const price=low+(high-low)*i/steps;
    const localGex=interpolateStrikeMetric(R.strikes,price,'netGex');
    const localVex=interpolateStrikeMetric(R.strikes,price,'netVex');
    const localCharm=interpolateStrikeMetric(R.strikes,price,'netCharm');
    const dist=(price-R.spot)/Math.max(em,R.spot*0.002);
    const direction = price>=R.spot ? 1 : -1;
    const normalizedGex=localGex/maxAbsStrikeGex;
    const flow=-normalizedGex*direction*100;
    const volShock=(localVex/maxAbsStrikeGex)*35;
    const timeDrift=(localCharm/maxAbsStrikeGex)*25;
    const force=Math.max(-100,Math.min(100,flow+volShock+timeDrift));
    const vacuum=1-Math.min(1,Math.abs(localGex)/maxAbsStrikeGex);
    const distanceScore=Math.min(18,Math.abs(dist)*4);
    const acceleration=Math.max(0,Math.min(100,Math.abs(force)*0.62+vacuum*20+distanceScore));
    const behavior = Math.abs(force)<18 ? 'compression' : force>0 ? 'upside chase' : 'downside sell pressure';
    points.push({price,localGex,localVex,localCharm,force,acceleration,vacuum,behavior});
  }
  function nearestPoint(price){
    return points.reduce((best,p)=>Math.abs(p.price-price)<Math.abs(best.price-price)?p:best,points[0]);
  }
  function sideSummary(side){
    const isUp=side==='up';
    const rows=points.filter(p=>isUp ? p.price>=R.spot : p.price<=R.spot);
    const ranked=[...rows].sort((a,b)=>b.acceleration-a.acceleration);
    const pocket=ranked[0] || nearestPoint(R.spot);
    const wall=isUp ? R.callWall : R.putWall;
    const wallPoint=Number.isFinite(Number(wall)) ? nearestPoint(Number(wall)) : pocket;
    const target1=wall || pocket.price;
    const target2=isUp ? Math.min(high,R.spot+em) : Math.max(low,R.spot-em);
    const score=Math.round(Math.min(100,pocket.acceleration));
    const action=score>=70 ? 'Breakout risk' : score>=42 ? 'Conditional move' : 'Likely fade/chop';
    return {side,score,action,pocket,wallPoint,target1,target2};
  }
  const up=sideSummary('up');
  const down=sideSummary('down');
  const stateScore=Math.max(up.score,down.score);
  const state=stateScore<42 ? 'Compression' : up.score>down.score ? 'Upside Shock Risk' : 'Downside Shock Risk';
  const stateClass=stateScore<42 ? 'compression' : up.score>down.score ? 'trend' : 'breakout';
  return {points,low,high,em,up,down,state,stateClass,stateScore,gross};
}
function renderShockPanels(R,shock){
  const stateHost=byId('shockStatePanel');
  if(stateHost){
    stateHost.className=`shock-panel shock-state ${shock.stateClass}`;
    stateHost.innerHTML=`
      <span>Current State</span>
      <strong>${esc(shock.state)}</strong>
      <p>${shock.stateScore<42 ? 'The structure is compressed. Wait for price to leave the active pocket before trusting direction.' : 'One side has stronger mechanical pressure. Use the chart to see where force expands or fades.'}</p>
      <div class="shock-metrics">
        <b>Spot ${levelDisplayPrice(R.spot,R)}</b>
        <b>Expected Move ${fmtPrice(shock.em)}</b>
      </div>
    `;
  }
  const upHost=byId('shockUpsidePanel');
  if(upHost){
    upHost.innerHTML=`
      <span>Upside Path</span>
      <strong>${shock.up.score}/100</strong>
      <p>${esc(shock.up.action)} above ${levelDisplayPrice(shock.up.target1,R)}. Target pocket ${levelDisplayPrice(shock.up.target2,R)}.</p>
      <div class="shock-metrics">
        <b>Force ${Math.round(shock.up.pocket.force)}</b>
        <b>Accel ${Math.round(shock.up.pocket.acceleration)}%</b>
      </div>
    `;
  }
  const downHost=byId('shockDownsidePanel');
  if(downHost){
    downHost.innerHTML=`
      <span>Downside Path</span>
      <strong>${shock.down.score}/100</strong>
      <p>${esc(shock.down.action)} below ${levelDisplayPrice(shock.down.target1,R)}. Target pocket ${levelDisplayPrice(shock.down.target2,R)}.</p>
      <div class="shock-metrics">
        <b>Force ${Math.round(shock.down.pocket.force)}</b>
        <b>Accel ${Math.round(shock.down.pocket.acceleration)}%</b>
      </div>
    `;
  }
}
function drawShockEngine(R){
  const cv=byId('shockEngineChart');
  if(!cv) return;
  const shock=buildShockEngine(R);
  R.shock=shock;
  renderShockPanels(R,shock);
  const ctx=cv.getContext('2d');
  const dpr=window.devicePixelRatio||1;
  const W=cv.clientWidth;
  const H=Math.max(560,Math.min(760,window.innerHeight-170));
  cv.width=W*dpr; cv.height=H*dpr; cv.style.height=H+'px';
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle='#181818'; ctx.fillRect(0,0,W,H);
  const padL=70,padR=28,padT=52,padB=72,plotW=W-padL-padR,plotH=H-padT-padB;
  const x=p=>padL+((p-shock.low)/(shock.high-shock.low))*plotW;
  const yForce=f=>padT+plotH/2-(f/100)*(plotH*.42);
  const yAccel=a=>padT+plotH-(a/100)*(plotH*.36);
  ctx.strokeStyle='rgba(255,255,255,.08)'; ctx.lineWidth=1;
  ctx.fillStyle='#8d989f'; ctx.font='12px Segoe UI'; ctx.textAlign='right'; ctx.textBaseline='middle';
  [-100,-50,0,50,100].forEach(v=>{
    const yy=yForce(v);
    ctx.beginPath(); ctx.moveTo(padL,yy); ctx.lineTo(W-padR,yy); ctx.stroke();
    ctx.fillText(String(v),padL-10,yy);
  });
  function band(from,to,color,label){
    const l=Math.max(padL,x(from)), r=Math.min(W-padR,x(to));
    if(r<=l) return;
    ctx.fillStyle=color; ctx.fillRect(l,padT,r-l,plotH);
    ctx.fillStyle='rgba(255,255,255,.62)'; ctx.font='900 11px Segoe UI'; ctx.textAlign='center';
    ctx.fillText(label,l+(r-l)/2,padT+18);
  }
  band(shock.low,R.putWall || shock.low,'rgba(255,69,58,.11)','Downside pocket');
  band(R.callWall || shock.high,shock.high,'rgba(34,184,255,.10)','Upside pocket');
  if(R.putWall && R.callWall) band(R.putWall,R.callWall,'rgba(255,193,7,.06)','Compression');
  function path(key,yFn,color,width=2.5){
    ctx.strokeStyle=color; ctx.lineWidth=width; ctx.beginPath();
    shock.points.forEach((p,i)=>i?ctx.lineTo(x(p.price),yFn(p[key])):ctx.moveTo(x(p.price),yFn(p[key])));
    ctx.stroke();
  }
  path('force',yForce,'#d5dde3',2);
  ctx.beginPath();
  shock.points.forEach((p,i)=>{
    const yy=yForce(p.force), xx=x(p.price);
    if(i) ctx.lineTo(xx,yy); else ctx.moveTo(xx,yy);
  });
  ctx.lineTo(x(shock.high),yForce(0)); ctx.lineTo(x(shock.low),yForce(0)); ctx.closePath();
  const grad=ctx.createLinearGradient(0,padT,0,padT+plotH);
  grad.addColorStop(0,'rgba(34,184,255,.20)');
  grad.addColorStop(.5,'rgba(255,255,255,.02)');
  grad.addColorStop(1,'rgba(255,69,58,.22)');
  ctx.fillStyle=grad; ctx.fill();
  path('acceleration',yAccel,'#ffc107',2);
  function vLine(price,color,label,dashed=false){
    if(!Number.isFinite(Number(price))) return;
    const xx=x(Number(price));
    ctx.save();
    if(dashed) ctx.setLineDash([6,5]);
    ctx.strokeStyle=color; ctx.lineWidth=2;
    ctx.beginPath(); ctx.moveTo(xx,padT); ctx.lineTo(xx,padT+plotH); ctx.stroke();
    ctx.setLineDash([]);
    ctx.translate(xx+7,padT+8); ctx.rotate(Math.PI/2);
    ctx.fillStyle=color; ctx.font='900 12px Segoe UI'; ctx.textAlign='left'; ctx.textBaseline='middle';
    ctx.fillText(label,0,0);
    ctx.restore();
  }
  vLine(R.spot,'#ffc107',`Spot ${fmtPrice(R.spot)}`);
  vLine(R.callWall,'#22b8ff',`Call Wall ${fmtPrice(R.callWall)}`,true);
  vLine(R.putWall,'#ff453a',`Put Wall ${fmtPrice(R.putWall)}`,true);
  vLine(R.flip,'#f5f5f5',`Zero Gamma ${R.flip?fmtPrice(R.flip):'--'}`,true);
  vLine(R.maxGammaStrike,'#8b5fe8',`Max Gamma ${fmtPrice(R.maxGammaStrike)}`,true);
  ctx.fillStyle='#fff'; ctx.font='900 18px Segoe UI'; ctx.textAlign='center';
  ctx.fillText(`Matrix Shock Map - ${R.symbol}`,W/2,30);
  ctx.fillStyle='#9fa8ae'; ctx.font='12px Segoe UI';
  const ticks=8;
  for(let i=0;i<=ticks;i++){
    const price=shock.low+(shock.high-shock.low)*i/ticks;
    ctx.fillText(fmtPrice(price),x(price),H-28);
  }
  ctx.textAlign='center'; ctx.fillText('Price path',padL+plotW/2,H-10);
  ctx.save(); ctx.translate(24,padT+plotH/2); ctx.rotate(-Math.PI/2); ctx.fillText('Mechanical force / acceleration',0,0); ctx.restore();
  window._shockHit={R,shock,x,yForce,yAccel,padL,padR,padT,padB,plotW,plotH};
}
function showShockTooltip(ev){
  const h=window._shockHit, tt=byId('shockTooltip');
  if(!h || !tt) return;
  const rect=ev.currentTarget.getBoundingClientRect();
  const mx=ev.clientX-rect.left, my=ev.clientY-rect.top;
  if(mx<h.padL || mx>h.padL+h.plotW || my<h.padT || my>h.padT+h.plotH){ tt.style.display='none'; return; }
  const price=h.shock.low+(mx-h.padL)/h.plotW*(h.shock.high-h.shock.low);
  const p=h.shock.points.reduce((best,row)=>Math.abs(row.price-price)<Math.abs(best.price-price)?row:best,h.shock.points[0]);
  tt.innerHTML=`
    <div class="tt-title">${levelDisplayPrice(p.price,h.R)}</div>
    <div class="tt-row"><span>Behavior</span><span>${esc(p.behavior)}</span></div>
    <div class="tt-row"><span>Force</span><span>${Math.round(p.force)}</span></div>
    <div class="tt-row"><span>Acceleration</span><span>${Math.round(p.acceleration)}%</span></div>
    <div class="tt-row"><span>Local GEX</span><span>${fmtNum(p.localGex)}</span></div>
  `;
  tt.style.display='block';
  tt.style.left=Math.min(rect.width-tt.offsetWidth-6,mx+14)+'px';
  tt.style.top=Math.max(6,Math.min(rect.height-tt.offsetHeight-6,my+14))+'px';
}
function hideShockTooltip(){
  const tt=byId('shockTooltip');
  if(tt) tt.style.display='none';
}

const SNAPSHOT_STORAGE_KEY = 'matrix.marketSnapshots.v1';
const SNAPSHOT_OUTCOMES = ['Worked','Failed','Early','Late','No Trade','Noise'];
function loadMarketSnapshots(){
  try{
    const raw = localStorage.getItem(SNAPSHOT_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}
function writeMarketSnapshots(rows){
  localStorage.setItem(SNAPSHOT_STORAGE_KEY,JSON.stringify(rows));
}
function snapshotFromResult(R,note=''){
  const read = R.marketRead || buildMarketRead(R);
  const spxCalc = spxPriceFromSpyPrice(R.spot,R);
  const linkedSpot = linkedMarketPrice(R.spot,R);
  return {
    id:`snap-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    createdAt:new Date().toISOString(),
    symbol:R.symbol,
    spot:R.spot,
    spxCalc,
    linkedSymbol:linkedSpot?.symbol || null,
    linkedSpot:linkedSpot?.price ?? null,
    selectedExpirations:R.maxPain?.active?.selectedExps || [],
    dataState:read.dataQuality?.state || '',
    dataAge:read.dataQuality?.age || '',
    regime:R.regime,
    bias:read.bias,
    confidence:read.confidence,
    totalGex:R.totalGex,
    pcr:R.pcr,
    flip:R.flip,
    maxGammaStrike:R.maxGammaStrike,
    callWall:R.callWall,
    putWall:R.putWall,
    maxPain:R.maxPain?.active?.maxPain ?? null,
    expectedMove:R.expectedMove,
    scenarios:read.scenarios,
    userNote:note,
    outcome:'',
  };
}
function saveMarketSnapshot(){
  if(!window._lastR) return;
  const note = byId('snapshotNote')?.value || '';
  const rows = loadMarketSnapshots();
  rows.unshift(snapshotFromResult(window._lastR,note.trim()));
  writeMarketSnapshots(rows.slice(0,250));
  if(byId('snapshotNote')) byId('snapshotNote').value='';
  renderReviewPage();
  const btn=byId('saveSnapshot');
  if(btn){
    const old=btn.textContent;
    btn.textContent='Saved';
    setTimeout(()=>btn.textContent=old,900);
  }
}
function setSnapshotOutcome(id,outcome){
  const rows = loadMarketSnapshots().map(row=>row.id===id ? {...row,outcome} : row);
  writeMarketSnapshots(rows);
  renderReviewPage();
}
function deleteSnapshot(id){
  writeMarketSnapshots(loadMarketSnapshots().filter(row=>row.id!==id));
  renderReviewPage();
}
function fmtSnapshotDate(value){
  const d = new Date(value);
  if(Number.isNaN(d.getTime())) return '--';
  return d.toLocaleString('en-US',{month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit'});
}
function percent(n,d){
  return d ? `${Math.round((n/d)*100)}%` : '--';
}
function countBy(rows,keyFn){
  const out = new Map();
  rows.forEach(row=>{
    const key = keyFn(row) || 'Unknown';
    out.set(key,(out.get(key)||0)+1);
  });
  return [...out.entries()].sort((a,b)=>b[1]-a[1]);
}
function renderReviewBars(items,total,limit=6){
  if(!items.length) return '<div class="review-empty">No reviewed data yet.</div>';
  return `<div class="review-bars">${items.slice(0,limit).map(([label,count])=>`
    <div class="review-bar">
      <b>${esc(label)}</b>
      <div class="review-bar-track"><div class="review-bar-fill" style="width:${total ? Math.max(4,Math.round((count/total)*100)) : 0}%"></div></div>
      <em>${count}</em>
    </div>
  `).join('')}</div>`;
}
function renderReviewStats(allRows,filteredRows){
  const host=byId('reviewStatsPanel');
  if(!host) return;
  if(!allRows.length){
    host.innerHTML=`
      <div class="review-stat"><span>Total Reads</span><strong>0</strong><small>Save Matrix GEX reads to build history.</small></div>
      <div class="review-stat"><span>Reviewed</span><strong>--</strong><small>No outcomes marked yet.</small></div>
      <div class="review-stat"><span>Win Rate</span><strong>--</strong><small>Marks counted as Worked.</small></div>
      <div class="review-stat"><span>Avg Confidence</span><strong>--</strong><small>From saved market reads.</small></div>
    `;
    return;
  }
  const reviewed = allRows.filter(row=>row.outcome);
  const worked = reviewed.filter(row=>row.outcome === 'Worked').length;
  const failed = reviewed.filter(row=>row.outcome === 'Failed' || row.outcome === 'Noise').length;
  const avgConfidence = Math.round(allRows.reduce((sum,row)=>sum+(Number(row.confidence)||0),0)/allRows.length);
  const winRateValue = reviewed.length ? Math.round((worked/reviewed.length)*100) : null;
  const tone = winRateValue == null ? 'warn' : winRateValue >= 55 ? 'good' : winRateValue >= 40 ? 'warn' : 'bad';
  const outcomeItems = countBy(reviewed,row=>row.outcome);
  const biasItems = countBy(allRows,row=>row.bias);
  host.innerHTML=`
    <div class="review-stat">
      <span>Total Reads</span>
      <strong>${allRows.length}</strong>
      <small>${filteredRows.length} shown with current filter.</small>
    </div>
    <div class="review-stat ${reviewed.length ? 'good' : 'warn'}">
      <span>Reviewed</span>
      <strong>${percent(reviewed.length,allRows.length)}</strong>
      <small>${reviewed.length}/${allRows.length} snapshots have outcomes.</small>
    </div>
    <div class="review-stat ${tone}">
      <span>Win Rate</span>
      <strong>${winRateValue == null ? '--' : `${winRateValue}%`}</strong>
      <small>${worked} worked / ${failed} failed or noise.</small>
    </div>
    <div class="review-stat">
      <span>Avg Confidence</span>
      <strong>${avgConfidence}%</strong>
      <small>Use this against actual outcomes, not as truth.</small>
    </div>
    <div class="review-breakdown">
      <span>Outcome Breakdown</span>
      ${renderReviewBars(outcomeItems,reviewed.length)}
    </div>
    <div class="review-breakdown">
      <span>Bias Mix</span>
      ${renderReviewBars(biasItems,allRows.length)}
    </div>
  `;
}
function renderReviewPage(){
  const host=byId('reviewSnapshotList');
  if(!host) return;
  const filter=byId('reviewOutcomeFilter')?.value || '';
  const allRows=loadMarketSnapshots();
  const rows=allRows.filter(row=>!filter || row.outcome===filter);
  renderReviewStats(allRows,rows);
  if(!rows.length){
    host.innerHTML='<div class="review-empty">No saved Matrix GEX snapshots yet. Open Matrix GEX and save a read during the session.</div>';
    return;
  }
  host.innerHTML=rows.map(row=>`
    <article class="snapshot-card" data-snapshot-id="${esc(row.id)}">
      <div class="snapshot-head">
        <div>
          <span>${esc(fmtSnapshotDate(row.createdAt))}</span>
          <strong>${esc(row.symbol)} ${fmtPrice(row.spot)}${row.linkedSpot==null
            ? (row.spxCalc==null?'':` / SPX ${fmtPrice(row.spxCalc)}`)
            : ` / ${esc(row.linkedSymbol)} ${esc(fmtLinkedMarketPrice({symbol:row.linkedSymbol,price:Number(row.linkedSpot),decimals:row.linkedSymbol==='SPY'||row.linkedSymbol==='QQQ'?2:0}))}`}</strong>
        </div>
        <div class="snapshot-bias ${esc((row.bias||'').toLowerCase().replace(/[^a-z]+/g,'-'))}">${esc(row.bias || '--')} Â· ${Number(row.confidence||0)}%</div>
      </div>
      <div class="snapshot-grid">
        <span><b>Regime</b>${esc(row.regime || '--')}</span>
        <span><b>Net GEX</b>${fmtNum(Number(row.totalGex)||0)}</span>
        <span><b>PCR</b>${Number(row.pcr||0).toFixed(2)}</span>
        <span><b>Data</b>${esc(row.dataState || '--')} ${esc(row.dataAge || '')}</span>
        <span><b>Call Wall</b>${levelDisplayPrice(row.callWall,{symbol:row.symbol})}</span>
        <span><b>Put Wall</b>${levelDisplayPrice(row.putWall,{symbol:row.symbol})}</span>
        <span><b>Max Pain</b>${levelDisplayPrice(row.maxPain,{symbol:row.symbol})}</span>
        <span><b>Expected Move</b>${row.expectedMove?fmtPrice(row.expectedMove):'--'}</span>
      </div>
      ${row.userNote ? `<p class="snapshot-note">${esc(row.userNote)}</p>` : ''}
      <div class="snapshot-outcomes">
        ${SNAPSHOT_OUTCOMES.map(outcome=>`<button type="button" class="${row.outcome===outcome?'active':''}" data-outcome="${esc(outcome)}">${esc(outcome)}</button>`).join('')}
        <button type="button" class="delete" data-delete-snapshot="1">Delete</button>
      </div>
    </article>
  `).join('');
}

// ---------- Rendering ----------
function buildOptionsHeatMap(chain){
  const r = RISK_FREE[chain.market] || 0.05;
  const byCell = new Map();
  for(const q of chain.quotes){
    const price = analyticsOptionPrice(chain,q);
    const premium = price * (q.vol || 0) * chain.mult;
    const key = `${q.exp}|${q.K}`;
    if(!byCell.has(key)){
      byCell.set(key,{exp:q.exp,K:q.K,callPremium:0,putPremium:0,callVol:0,putVol:0,callOI:0,putOI:0});
    }
    const cell = byCell.get(key);
    if(q.isCall){ cell.callPremium += premium; cell.callVol += q.vol || 0; cell.callOI += q.oi || 0; }
    else        { cell.putPremium  += premium; cell.putVol  += q.vol || 0; cell.putOI  += q.oi || 0; }
  }
  const cells = [...byCell.values()].map(c=>({
    ...c,
    netPremium:c.callPremium-c.putPremium,
    netVolume:c.callVol-c.putVol,
    netOI:c.callOI-c.putOI
  }));
  const expiries = [...new Set(cells.map(c=>c.exp).filter(Boolean))].sort();
  let strikes = [...new Set(cells.map(c=>c.K))].sort((a,b)=>a-b);
  const near = strikes.filter(K=>Math.abs(K-chain.spot) <= Math.max(chain.spot*0.08, 1));
  if(near.length >= 8) strikes = near;
  const absPremiums = cells.map(c=>Math.abs(c.netPremium)).filter(v=>v>0).sort((a,b)=>a-b);
  const scaleIndex = Math.max(0, Math.floor(absPremiums.length*0.92)-1);
  const maxAbs = Math.max(absPremiums[scaleIndex] || absPremiums.at(-1) || 1, 1);
  return {expiries,strikes,cells,maxAbs,spot:chain.spot,symbol:chain.symbol};
}
function heatColor(value,maxAbs){
  if(!value) return '#202020';
  const t = Math.min(1, Math.abs(value)/maxAbs);
  const intensity = Math.pow(t, 0.62);
  const a = 0.12 + intensity*0.62;
  return value > 0 ? `rgba(34,184,255,${a})` : `rgba(255,42,23,${a})`;
}
function renderOptionsHeatMap(R){
  const host = document.getElementById('optionsHeatMap');
  if(!host || !R.optionsHeatMap) return;
  const H = R.optionsHeatMap;
  if(!H.expiries.length || !H.strikes.length){
    host.innerHTML = '<div class="heatmap-note">No options heat map data available for the current selection.</div>';
    return;
  }
  const cellByKey = new Map(H.cells.map(c=>[`${c.exp}|${c.K}`,c]));
  const spotIndex = H.strikes.reduce((best,K,i)=>Math.abs(K-H.spot)<Math.abs(H.strikes[best]-H.spot)?i:best,0);
  const rows = H.expiries.map(exp=>{
    const cells = H.strikes.map((K,i)=>{
      const c = cellByKey.get(`${exp}|${K}`);
      const spotClass = i===spotIndex ? ' heatmap-spot-col' : '';
      if(!c) return `<td class="heatmap-empty${spotClass}">$0</td>`;
      const cls = c.netPremium>=0 ? 'heatmap-pos' : 'heatmap-neg';
      const payload = esc(JSON.stringify({
        symbol:H.symbol, strike:fmtPrice(c.K), exp:c.exp,
        callPremium:fmtMoney(c.callPremium), putPremium:fmtMoney(c.putPremium), netPremium:fmtMoney(c.netPremium),
        callVol:fmtNum(c.callVol), putVol:fmtNum(c.putVol), netVol:fmtNum(c.netVolume),
        callOI:fmtNum(c.callOI), putOI:fmtNum(c.putOI), netOI:fmtNum(c.netOI)
      }));
      return `<td class="heatmap-cell ${cls}${spotClass}" style="background:${heatColor(c.netPremium,H.maxAbs)}" data-heatmap="${payload}">${fmtMoney(c.netPremium)}</td>`;
    }).join('');
    return `<tr><td class="heatmap-exp">${esc(exp)}</td>${cells}</tr>`;
  }).join('');
  host.innerHTML = `
    <table class="heatmap-table">
      <thead><tr><th>Expiration Date</th>${H.strikes.map((K,i)=>`<th class="${i===spotIndex?'heatmap-spot-col':''}">${fmtPrice(K)}</th>`).join('')}</tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="heatmap-hover-line" data-heatmap-hover-line></div>
    <div class="heatmap-price-line" data-heatmap-price-line><span class="heatmap-price-label">Price: ${fmtPrice(H.spot)}</span></div>
    <div class="heatmap-note">
      <span>Blue = call premium leading Â· Red = put premium leading</span>
      <span>Orange line = current price ${fmtPrice(H.spot)}</span>
    </div>
  `;
  requestAnimationFrame(()=>{
    positionHeatMapPriceLine(host);
    centerHeatMapOnSpot(host);
  });
}
function positionHeatMapPriceLine(host){
  const spotCell = host.querySelector('tbody .heatmap-spot-col');
  const line = host.querySelector('[data-heatmap-price-line]');
  if(!spotCell || !line) return;
  const left = spotCell.offsetLeft + 2;
  line.style.left = `${left}px`;
  const label = line.querySelector('.heatmap-price-label');
  if(label){
    label.style.left = '8px';
    label.style.right = 'auto';
    requestAnimationFrame(()=>{
      if(left + label.offsetWidth + 18 > host.scrollWidth){
        label.style.left = 'auto';
        label.style.right = '8px';
      }
    });
  }
}
function centerHeatMapOnSpot(host){
  const spotCell = host.querySelector('tbody .heatmap-spot-col');
  if(!spotCell) return;
  const target = spotCell.offsetLeft + spotCell.offsetWidth/2 - host.clientWidth/2;
  host.scrollLeft = Math.max(0,target);
}
function showHeatMapTip(e){
  const cell = e.target.closest('.heatmap-cell[data-heatmap]');
  const tip = document.getElementById('heatMapTooltip');
  document.querySelectorAll('.heatmap-cell-hover').forEach(el=>el.classList.remove('heatmap-cell-hover'));
  document.querySelectorAll('.heatmap-row-hover').forEach(el=>el.classList.remove('heatmap-row-hover'));
  if(!cell || !tip){
    if(tip) tip.style.display='none';
    const host = document.getElementById('optionsHeatMap');
    const line = host ? host.querySelector('[data-heatmap-hover-line]') : null;
    if(line) line.style.display = 'none';
    return;
  }
  cell.classList.add('heatmap-cell-hover');
  const tr = cell.closest('tr');
  if(tr) tr.classList.add('heatmap-row-hover');
  const host = document.getElementById('optionsHeatMap');
  const line = host ? host.querySelector('[data-heatmap-hover-line]') : null;
  if(line){
    line.style.display = 'block';
    line.style.left = `${cell.offsetLeft + cell.offsetWidth/2}px`;
  }
  const d = JSON.parse(cell.dataset.heatmap);
  const row = (label,value,cls='') => `<div class="heatmap-tip-row"><span>${label}</span><span class="${cls}">${value}</span></div>`;
  tip.innerHTML = `
    <div class="heatmap-tip-title">${d.symbol} Â· Strike ${d.strike}</div>
    ${row('Expires',d.exp)}
    ${row('Call Premium',d.callPremium,'heatmap-pos')}
    ${row('Put Premium',d.putPremium,'heatmap-neg')}
    ${row('Net Premium',d.netPremium,d.netPremium.startsWith('-')?'heatmap-neg':'heatmap-pos')}
    ${row('Call Volume',d.callVol,'heatmap-pos')}
    ${row('Put Volume',d.putVol,'heatmap-neg')}
    ${row('Net Volume',d.netVol)}
    ${row('Call Open Interest',d.callOI,'heatmap-pos')}
    ${row('Put Open Interest',d.putOI,'heatmap-neg')}
    ${row('Net Open Interest',d.netOI)}
  `;
  tip.style.display='block';
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  let left = e.clientX + 14, top = e.clientY + 14;
  if(left + tw > window.innerWidth) left = e.clientX - tw - 14;
  if(top + th > window.innerHeight) top = e.clientY - th - 14;
  tip.style.left = Math.max(6,left)+'px';
  tip.style.top = Math.max(6,top)+'px';
}
function hideHeatMapTip(){
  const tip = document.getElementById('heatMapTooltip');
  if(tip) tip.style.display='none';
  const host = document.getElementById('optionsHeatMap');
  const line = host ? host.querySelector('[data-heatmap-hover-line]') : null;
  if(line) line.style.display = 'none';
  document.querySelectorAll('.heatmap-cell-hover').forEach(el=>el.classList.remove('heatmap-cell-hover'));
  document.querySelectorAll('.heatmap-row-hover').forEach(el=>el.classList.remove('heatmap-row-hover'));
}
const NETFLOW_STORE_KEY = 'matrix_netflow_volume_delta_v1';
function readNetFlowStore(){
  try{
    return JSON.parse(localStorage.getItem(NETFLOW_STORE_KEY)) || {lastVolumes:{},history:[]};
  }catch(e){
    return {lastVolumes:{},history:[]};
  }
}
function writeNetFlowStore(store){
  try{ localStorage.setItem(NETFLOW_STORE_KEY, JSON.stringify(store)); }catch(e){}
}
function optionFlowKey(chain,q){
  return `${chain.symbol}|${q.exp}|${q.K}|${q.isCall?'C':'P'}`;
}
function updateNetFlowHistoryFromSnapshot(chain){
  if(!chain.live) return;
  const r = RISK_FREE[chain.market] || 0.05;
  const store = readNetFlowStore();
  const collectedTs = Date.now();
  const marketTs = chain.fetchTs || collectedTs;
  let added = 0;
  for(const q of chain.quotes){
    const key = optionFlowKey(chain,q);
    const currentVol = Math.max(0, Number(q.vol)||0);
    const prevVol = store.lastVolumes[key];
    if(prevVol !== undefined && currentVol > prevVol){
      const deltaVol = currentVol - prevVol;
      const price = analyticsOptionPrice(chain,q);
      const premium = price * deltaVol * chain.mult;
      if(premium > 0){
        store.history.push({
          ts:collectedTs, marketTs, symbol:chain.symbol, exp:q.exp, K:q.K, side:q.isCall?'C':'P',
          premium, vol:deltaVol, spot:chain.spot
        });
        added++;
      }
    }
    store.lastVolumes[key] = currentVol;
  }
  const cutoff = Date.now() - 36*60*60*1000;
  store.history = store.history.filter(e=>e.ts>=cutoff).slice(-6000);
  writeNetFlowStore(store);
  return added;
}
function buildNetFlow(chain){
  const buckets = 79; // 9:30 to 16:00, 5-minute style points
  const points = Array.from({length:buckets},(_,i)=>{
    const min = 9*60+30+i*5;
    const hh = Math.floor(min/60), mm = min%60;
    return {i,time:`${String(hh).padStart(2,'0')}:${String(mm).padStart(2,'0')}`,call:0,put:0,price:chain.spot};
  });
  const allowed = new Set(chain.quotes.map(q=>optionFlowKey(chain,q)));
  const store = readNetFlowStore();
  const events = store.history.filter(e=>e.symbol===chain.symbol && allowed.has(`${e.symbol}|${e.exp}|${e.K}|${e.side}`));
  for(const e of events){
    const d = new Date(e.marketTs || e.ts);
    const mins = d.getHours()*60 + d.getMinutes();
    const marketMins = Math.max(9*60+30, Math.min(16*60, mins));
    const idx = Math.max(0, Math.min(buckets-1, Math.round((marketMins-(9*60+30))/5)));
    if(e.side==='C') points[idx].call += e.premium;
    else points[idx].put += e.premium;
    points[idx].price = e.spot || points[idx].price;
  }
  const maxPrem = Math.max(...points.map(p=>Math.max(p.call,p.put)),1);
  let lastPrice = chain.spot;
  points.forEach((p,i)=>{
    if(p.price) lastPrice = p.price;
    p.price = lastPrice;
  });
  return {symbol:chain.symbol,spot:chain.spot,points,maxPrem,events:events.length,collecting:events.length===0};
}
function drawNetFlow(R){
  const cv = document.getElementById('netFlowChart');
  if(!cv || !R.netFlow) return;
  const ctx = cv.getContext('2d');
  const dpr = window.devicePixelRatio||1;
  const W = cv.clientWidth;
  const H = Math.max(520, Math.min(760, window.innerHeight-170));
  cv.width = W*dpr; cv.height = H*dpr; cv.style.height = H+'px';
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,W,H);
  const pts = R.netFlow.points;
  const padL=72,padR=82,padT=46,padB=62, plotW=W-padL-padR, plotH=H-padT-padB;
  const maxPrem = niceMax(R.netFlow.maxPrem);
  const minPrice = Math.min(...pts.map(p=>p.price));
  const maxPrice = Math.max(...pts.map(p=>p.price));
  const pricePad = Math.max((maxPrice-minPrice)*0.16, R.netFlow.spot*0.002);
  const pMin = minPrice-pricePad, pMax = maxPrice+pricePad;
  const x = i => padL + (i/(pts.length-1))*plotW;
  const yPrem = v => padT + plotH - (v/maxPrem)*plotH;
  const yPrice = v => padT + plotH - ((v-pMin)/(pMax-pMin))*plotH;

  ctx.fillStyle='#202020'; ctx.fillRect(0,0,W,H);
  ctx.strokeStyle='rgba(255,255,255,.09)'; ctx.lineWidth=1;
  ctx.fillStyle='#00b7ff'; ctx.font='12px Segoe UI'; ctx.textAlign='right'; ctx.textBaseline='middle';
  for(let g=0;g<=4;g++){
    const yy=padT+plotH*g/4;
    ctx.beginPath(); ctx.moveTo(padL,yy); ctx.lineTo(W-padR,yy); ctx.stroke();
    const prem=maxPrem*(1-g/4);
    ctx.fillText(fmtMoney(prem).replace('.00',''),padL-12,yy);
  }
  ctx.textAlign='left';
  for(let g=0;g<=4;g++){
    const yy=padT+plotH*g/4;
    const pr=pMax-(pMax-pMin)*g/4;
    ctx.fillText('$'+fmtPrice(pr),W-padR+12,yy);
  }
  ctx.save(); ctx.translate(24,padT+plotH/2); ctx.rotate(-Math.PI/2); ctx.textAlign='center'; ctx.fillText('Premium',0,0); ctx.restore();
  ctx.save(); ctx.translate(W-22,padT+plotH/2); ctx.rotate(Math.PI/2); ctx.textAlign='center'; ctx.fillText('Reference Price',0,0); ctx.restore();

  function area(key,color){
    ctx.beginPath(); ctx.moveTo(x(0),yPrem(0));
    pts.forEach((p,i)=>ctx.lineTo(x(i),yPrem(p[key])));
    ctx.lineTo(x(pts.length-1),yPrem(0)); ctx.closePath();
    ctx.fillStyle=color; ctx.fill();
    ctx.strokeStyle=color.replace('.42','.95'); ctx.lineWidth=2;
    ctx.beginPath(); pts.forEach((p,i)=>i?ctx.lineTo(x(i),yPrem(p[key])):ctx.moveTo(x(i),yPrem(p[key]))); ctx.stroke();
  }
  area('call','rgba(23,182,95,.42)');
  area('put','rgba(255,42,23,.42)');
  ctx.strokeStyle='#22b8ff'; ctx.lineWidth=2.3; ctx.beginPath();
  pts.forEach((p,i)=>i?ctx.lineTo(x(i),yPrice(p.price)):ctx.moveTo(x(i),yPrice(p.price))); ctx.stroke();

  ctx.fillStyle='#f0f0f0'; ctx.font='800 16px Segoe UI'; ctx.textAlign='center'; ctx.fillText(`Net Flow (Premium) - ${R.netFlow.symbol}`,W/2,24);
  if(R.netFlow.collecting){
    ctx.fillStyle='rgba(255,255,255,.78)';
    ctx.font='800 18px Segoe UI';
    ctx.fillText('Collecting volume-delta flow...', W/2, padT + plotH/2 - 12);
    ctx.font='13px Segoe UI';
    ctx.fillStyle='rgba(255,255,255,.55)';
    ctx.fillText('Leave auto-refresh running. New call/put volume changes will appear here as real flow spikes.', W/2, padT + plotH/2 + 16);
  }
  ctx.font='12px Segoe UI'; ctx.fillStyle='#00b7ff'; ctx.textBaseline='top';
  for(let i=0;i<pts.length;i+=9){
    ctx.save(); ctx.translate(x(i),H-padB+16); ctx.rotate(-Math.PI/4); ctx.textAlign='right'; ctx.fillText(pts[i].time,0,0); ctx.restore();
  }
  ctx.fillStyle='#00b7ff'; ctx.textAlign='center'; ctx.fillText('Time',padL+plotW/2,H-18);
  window._netFlowHit = {pts,x,yPrem,yPrice,padL,padR,padT,padB,plotW,plotH,maxPrem,pMin,pMax};
}
function niceMax(v){
  const p=Math.pow(10,Math.floor(Math.log10(v||1)));
  return Math.ceil(v/p)*p;
}
function showNetFlowTooltip(e){
  const h=window._netFlowHit, tt=document.getElementById('netFlowTooltip');
  if(!h || !tt) return;
  const cross = document.getElementById('netFlowCrosshair');
  const rect=e.currentTarget.getBoundingClientRect();
  const mx=e.clientX-rect.left, my=e.clientY-rect.top;
  if(mx<h.padL || mx>h.padL+h.plotW || my<h.padT || my>h.padT+h.plotH){
    tt.style.display='none';
    if(cross) cross.style.display='none';
    return;
  }
  const idx=Math.max(0,Math.min(h.pts.length-1,Math.round((mx-h.padL)/h.plotW*(h.pts.length-1))));
  const p=h.pts[idx];
  if(cross){
    cross.style.display='block';
    cross.style.left = `${h.x(idx)}px`;
    cross.style.top = `${h.padT}px`;
    cross.style.bottom = `${h.padB}px`;
  }
  tt.innerHTML=`<div class="heatmap-tip-title">${p.time}</div>
    <div class="heatmap-tip-row"><span>Call Premium</span><span style="color:var(--green);font-weight:800">${fmtMoney(p.call)}</span></div>
    <div class="heatmap-tip-row"><span>Put Premium</span><span style="color:var(--red);font-weight:800">${fmtMoney(p.put)}</span></div>
    <div class="heatmap-tip-row"><span>Reference Price</span><span style="color:#22b8ff;font-weight:800">$${fmtPrice(p.price)}</span></div>`;
  tt.style.display='block';
  tt.style.left=Math.min(rect.width-tt.offsetWidth-6,mx+14)+'px';
  tt.style.top=Math.max(6,Math.min(rect.height-tt.offsetHeight-6,my+14))+'px';
}
function hideNetFlowTooltip(){
  const tt=document.getElementById('netFlowTooltip');
  if(tt) tt.style.display='none';
  const cross = document.getElementById('netFlowCrosshair');
  if(cross) cross.style.display='none';
}
let LIVE_FLOW=null, LIVE_FLOW_LOADING=false, LIVE_FLOW_KEY='',FLOW_RETRY_TIMER=null;
const FLOW_HITS={},FLOW_HOVER={};
const FLOW_RESET_KEY='matrix_flow_visible_from_v1';
function flowSessionDateForTime(value){
  const raw=Number(value),date=new Date(raw<1e12?raw*1000:raw);
  const parts=new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(date);
  const part=type=>parts.find(item=>item.type===type)?.value||'';
  return `${part('year')}-${part('month')}-${part('day')}`;
}
function fallbackFlowSessionDates(){
  const nowParts=new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());
  const value=type=>Number(nowParts.find(item=>item.type===type)?.value||0);
  const cursor=new Date(Date.UTC(value('year'),value('month')-1,value('day'),12));
  const dates=[];
  while(dates.length<3){
    if(cursor.getUTCDay()!==0&&cursor.getUTCDay()!==6)dates.push(cursor.toISOString().slice(0,10));
    cursor.setUTCDate(cursor.getUTCDate()-1);
  }
  return dates;
}
function populateFlowSessionDates(dates=fallbackFlowSessionDates(),preferred=''){
  const select=document.getElementById('flowSession');if(!select)return '';
  const clean=[...new Set(dates.filter(date=>/^\d{4}-\d{2}-\d{2}$/.test(String(date))))];
  const sessions=clean.length?clean:fallbackFlowSessionDates(),selected=sessions.includes(preferred)?preferred:sessions[0];
  select.innerHTML=sessions.map((date,index)=>`<option value="${date}">${date}${index===0?' · Latest session':''}</option>`).join('');
  select.value=selected;
  return selected;
}
function flowTimeLabel(ms){
  return new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(ms));
}
function fmtFlowPrice(value){
  const number=Number(value);
  return Number.isFinite(number)?number.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}):'--';
}
function selectedFlowExpirations(){
  return [...document.querySelectorAll('#expirationPicker input:checked')].map(input=>input.value);
}
async function loadFlowData(force=false){
  if(activeView!=='net-flow') return;
  const symbol=document.getElementById('symbol').value;
  const interval=document.getElementById('flowInterval')?.value||'1m';
  const sessionDate=document.getElementById('flowSession')?.value||populateFlowSessionDates();
  const expirations=selectedFlowExpirations();
  const key=`${symbol}|${interval}|${sessionDate}|${expirations.join(',')}`;
  const status=document.getElementById('flowDataStatus');
  if(!['SPY','QQQ'].includes(symbol)){
    LIVE_FLOW={ok:false,detail:'Choose SPY or QQQ — LSE options prints are not available for SPX/NDX.'};
    LIVE_FLOW_KEY=key; drawFlowWorkspace(); return;
  }
  if(LIVE_FLOW_LOADING || (!force && LIVE_FLOW_KEY===key && LIVE_FLOW)) return drawFlowWorkspace();
  LIVE_FLOW_LOADING=true;
  if(status){status.className='flow-status';status.textContent='Loading LSE prints…';}
  const params=new URLSearchParams({symbol,interval,session_date:sessionDate,t:String(Date.now())});
  expirations.forEach(exp=>params.append('expiration',exp));
  try{
    const flowRequest=fetch(`${MATRIX_FLOW_URL}?${params}`,{cache:'no-store'}).then(async response=>{
      const payload=await response.json();
      if(!response.ok||!payload.ok) throw new Error(payload.detail||`Flow request failed (${response.status})`);
      return payload;
    });
    const candleRequest=fetch(`${MATRIX_CANDLES_URL}?symbol=${encodeURIComponent(symbol)}&interval=1m&range=5d&t=${Date.now()}`,{cache:'no-store'}).then(async response=>{
      if(!response.ok) throw new Error(`Price request failed (${response.status})`);
      const payload=await response.json();
      if(!payload.ok) throw new Error(payload.detail||'Price request failed');
      payload.candles=(Array.isArray(payload.candles)?payload.candles:[]).filter(candle=>flowSessionDateForTime(candle.time)===sessionDate);
      return payload;
    });
    const [flowResult,candleResult]=await Promise.allSettled([flowRequest,candleRequest]);
    if(flowResult.status==='rejected'){
      if(candleResult.status==='rejected') throw flowResult.reason;
      const candles=Array.isArray(candleResult.value.candles)?candleResult.value.candles:[];
      const lastCandle=candles.at(-1),lastTime=Number(lastCandle?.time)||0;
      LIVE_FLOW={
        ok:true,symbol,source:'price_only_fallback',asof:lastTime?new Date(lastTime*1000).toISOString():new Date().toISOString(),
        points:[],trades:0,classified_trades:0,classification_coverage:0,partial:true,
        collector:{state:'retrying',quality:'disconnected'},price_points:candles,
        load_warning:'Options flow reconnecting; live price remains available.'
      };
      LIVE_FLOW_KEY=key;
      throw new Error('Options flow reconnecting');
    }
    const payload=flowResult.value;
    populateFlowSessionDates(payload.available_sessions||fallbackFlowSessionDates(),payload.session_date||sessionDate);
    if(candleResult.status==='fulfilled'){
      payload.price_points=Array.isArray(candleResult.value.candles)?candleResult.value.candles:[];
    }else{
      payload.price_points=[];
      payload.load_warning='Price candles reconnecting; option flow remains available.';
    }
    LIVE_FLOW=payload; LIVE_FLOW_KEY=key;
    if(FLOW_RETRY_TIMER){clearTimeout(FLOW_RETRY_TIMER);FLOW_RETRY_TIMER=null;}
    if(payload.load_warning){
      FLOW_RETRY_TIMER=setTimeout(()=>{FLOW_RETRY_TIMER=null;loadFlowData(true);},5000);
    }
  }catch(error){
    if(!LIVE_FLOW?.ok||LIVE_FLOW_KEY!==key){LIVE_FLOW={ok:false,detail:error.message||'Flow data unavailable'};LIVE_FLOW_KEY=key;}
    if(!FLOW_RETRY_TIMER){
      FLOW_RETRY_TIMER=setTimeout(()=>{FLOW_RETRY_TIMER=null;loadFlowData(true);},5000);
    }
  }finally{
    LIVE_FLOW_LOADING=false; drawFlowWorkspace();
    const currentSymbol=document.getElementById('symbol').value;
    const currentInterval=document.getElementById('flowInterval')?.value||'1m';
    const currentSession=document.getElementById('flowSession')?.value||'';
    const currentKey=`${currentSymbol}|${currentInterval}|${currentSession}|${selectedFlowExpirations().join(',')}`;
    if(currentKey!==key)setTimeout(()=>loadFlowData(true),0);
  }
}
function flowCanvasSetup(canvasId){
  const cv=document.getElementById(canvasId); if(!cv) return null;
  const dpr=window.devicePixelRatio||1,W=cv.clientWidth,H=W<560?350:Math.max(500,Math.min(680,window.innerHeight-210));
  cv.width=W*dpr;cv.height=H*dpr;cv.style.height=H+'px';
  const ctx=cv.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,W,H);ctx.fillStyle='#202020';ctx.fillRect(0,0,W,H);
  return {cv,ctx,W,H};
}
function expandFlowSession(points,pricePoints,sessionDate){
  const match=String(sessionDate||'').match(/^(\d{4})-(\d{2})-(\d{2})$/);
  const fallback=flowSessionDateForTime(Number(points.at(-1)?.time)||Date.now());
  const [year,month,day]=(match?match.slice(1):fallback.split('-')).map(Number);
  const start=zonedDateTimeToUtc(year,month,day,9,30,'America/New_York');
  const flowByMinute=new Map((points||[]).map(point=>[Math.floor(Number(point.time)/60000)*60000,point]));
  const priceByMinute=new Map((pricePoints||[]).map(candle=>{
    const raw=Number(candle.time),ms=raw<1e12?raw*1000:raw;
    return [Math.floor(ms/60000)*60000,Number(candle.close)];
  }));
  const marketClose=start+390*60000;
  const observedTimes=[...flowByMinute.keys(),...priceByMinute.keys()].filter(time=>time>=start&&time<=marketClose);
  const lastObserved=observedTimes.length?Math.max(...observedTimes):start;
  const visibleMinutes=Math.max(0,Math.min(390,Math.floor((lastObserved-start)/60000)));
  const session=[];let lastPrice=null;
  for(let i=0;i<=390;i++){
    const time=start+i*60000,source=flowByMinute.get(time)||{};
    const candlePrice=priceByMinute.get(time);
    if(Number.isFinite(candlePrice))lastPrice=candlePrice;
    else if(Number.isFinite(Number(source.spot)))lastPrice=Number(source.spot);
    session.push({
      time,callPremium:0,putPremium:0,callVolume:0,putVolume:0,
      netCallPremium:0,netPutPremium:0,netCallVolume:0,netPutVolume:0,trades:0,
      ...source,spot:i<=visibleMinutes?lastPrice:null,future:i>visibleMinutes
    });
  }
  return session;
}
function drawFlowEmpty(canvasId,message){
  const setup=flowCanvasSetup(canvasId); if(!setup)return;
  const {ctx,W,H}=setup;ctx.fillStyle='#91a9ba';ctx.font='700 14px Segoe UI';ctx.textAlign='center';ctx.fillText(message,W/2,H/2);
}
function drawFlowCanvas(canvasId,points,mode,hover=FLOW_HOVER[canvasId]||null){
  const setup=flowCanvasSetup(canvasId);if(!setup)return;
  const {ctx,W,H}=setup,padL=62,padR=62,padT=22,padB=46;
  const subH=mode==='drift'?94:0,gap=mode==='drift'?22:0,plotH=H-padT-padB-subH-gap,plotW=W-padL-padR;
  const rows=[];let callCum=0,putCum=0,lastSpot=null;
  points.forEach(p=>{
    if(p.spot!==null&&p.spot!==''&&Number.isFinite(Number(p.spot))) lastSpot=Number(p.spot);
    if(!p.future&&mode==='drift'){callCum+=Number(p.netCallPremium)||0;putCum+=Number(p.netPutPremium)||0;}
    rows.push({...p,label:flowTimeLabel(p.time),call:p.future?null:(mode==='drift'?callCum:Number(p.callVolume)||0),put:p.future?null:(mode==='drift'?putCum:Number(p.putVolume)||0),price:p.future?null:lastSpot});
  });
  if(!rows.length)return drawFlowEmpty(canvasId,'No option prints for this session / expiration selection');
  const xs=i=>padL+(rows.length===1 ? .5 : i/(rows.length-1))*plotW;
  const values=rows.flatMap(p=>[p.call,p.put]).filter(Number.isFinite);
  let yMin=mode==='drift'?Math.min(0,...values):0,yMax=Math.max(0,...values);
  if(yMax===yMin){yMax=yMin+1;} const valPad=mode==='drift'?(yMax-yMin)*.12:0;yMin-=valPad;yMax+=valPad;
  const y=v=>padT+plotH-(v-yMin)/(yMax-yMin)*plotH;
  const prices=rows.map(p=>p.price).filter(Number.isFinite),pLow=Math.min(...prices),pHigh=Math.max(...prices);
  const pPad=Math.max((pHigh-pLow)*.15,(prices[0]||1)*.001),pMin=pLow-pPad,pMax=pHigh+pPad;
  const yp=v=>padT+plotH-(v-pMin)/(pMax-pMin||1)*plotH;
  ctx.strokeStyle='rgba(255,255,255,.09)';ctx.fillStyle='#a0a0a0';ctx.font='10px Segoe UI';ctx.textBaseline='middle';
  for(let g=0;g<=4;g++){const yy=padT+g*plotH/4;ctx.beginPath();ctx.moveTo(padL,yy);ctx.lineTo(W-padR,yy);ctx.stroke();ctx.textAlign='right';const axisValue=yMax-(yMax-yMin)*g/4;ctx.fillText(mode==='flow'?fmtNum(axisValue):fmtMoney(axisValue).replace('.00',''),padL-7,yy);if(prices.length){ctx.textAlign='left';ctx.fillStyle='#5daee0';ctx.fillText('$'+fmtFlowPrice(pMax-(pMax-pMin)*g/4),W-padR+7,yy);ctx.fillStyle='#a0a0a0';}}
  ctx.save();ctx.translate(13,padT+plotH/2);ctx.rotate(-Math.PI/2);ctx.textAlign='center';ctx.fillStyle='#a0a0a0';ctx.fillText(mode==='flow'?'Volume':'Premium',0,0);ctx.restore();
  ctx.save();ctx.translate(W-10,padT+plotH/2);ctx.rotate(Math.PI/2);ctx.textAlign='center';ctx.fillStyle='#5daee0';ctx.fillText('Underlying',0,0);ctx.restore();
  if(mode==='drift'){ctx.strokeStyle='rgba(255,255,255,.28)';ctx.beginPath();ctx.moveTo(padL,y(0));ctx.lineTo(W-padR,y(0));ctx.stroke();}
  const active=hover?.series||null;
  const brightColors={call:'92,255,151',put:'255,82,101',price:'76,207,255'};
  function seriesStyle(key,rgb){const focused=!active||active===key,activeColor=brightColors[key]||rgb;return {color:`rgba(${active===key?activeColor:rgb},${focused?(active?1:.94):.12})`,width:focused?(active?4.2:1.8):1};}
  function line(key,rgb){const style=seriesStyle(key,rgb);ctx.save();ctx.strokeStyle=style.color;ctx.lineWidth=style.width;ctx.lineJoin='round';ctx.lineCap='round';if(active===key){ctx.shadowColor=style.color;ctx.shadowBlur=16;ctx.globalCompositeOperation='lighter';}ctx.beginPath();let started=false;rows.forEach((p,i)=>{if(!Number.isFinite(p[key])){started=false;return;}if(started)ctx.lineTo(xs(i),y(p[key]));else{ctx.moveTo(xs(i),y(p[key]));started=true;}});ctx.stroke();ctx.restore();}
  line('call','39,214,110');line('put','255,53,69');
  if(prices.length){const style=seriesStyle('price','34,184,255');ctx.save();ctx.strokeStyle=style.color;ctx.lineWidth=style.width;ctx.lineJoin='round';ctx.lineCap='round';if(active==='price'){ctx.shadowColor=style.color;ctx.shadowBlur=16;ctx.globalCompositeOperation='lighter';}ctx.beginPath();let started=false;rows.forEach((p,i)=>{if(!Number.isFinite(p.price)){started=false;return;}if(started)ctx.lineTo(xs(i),yp(p.price));else{ctx.moveTo(xs(i),yp(p.price));started=true;}});ctx.stroke();ctx.restore();}
  if(mode==='drift'){
    const subTop=padT+plotH+gap,net=rows.map(p=>(Number(p.netCallVolume)||0)-(Number(p.netPutVolume)||0)),abs=Math.max(...net.map(Math.abs),1),mid=subTop+subH/2;
    ctx.strokeStyle='rgba(255,255,255,.12)';ctx.beginPath();ctx.moveTo(padL,mid);ctx.lineTo(W-padR,mid);ctx.stroke();
    const barW=Math.max(1,plotW/rows.length*.72);rows.forEach((p,i)=>{const v=net[i],h=v/abs*(subH/2-5);ctx.fillStyle=v>=0?'rgba(39,214,110,.72)':'rgba(255,53,69,.72)';ctx.fillRect(xs(i)-barW/2,mid-Math.max(h,0),barW,Math.abs(h));});
    ctx.fillStyle='#9a9a9a';ctx.textAlign='right';ctx.fillText('NET VOL',padL-7,mid);
  }
  ctx.fillStyle='#a0a0a0';ctx.textBaseline='top';ctx.textAlign='center';const step=Math.max(1,Math.ceil(rows.length/7));for(let i=0;i<rows.length;i+=step)ctx.fillText(rows[i].label,xs(i),H-padB+14);
  if(hover&&rows[hover.idx]){
    const p=rows[hover.idx],markers=[['call','#27d66e',y],['put','#ff3545',y],['price','#22b8ff',yp]];
    markers.forEach(([key,color,axis])=>{if(!Number.isFinite(p[key]))return;ctx.beginPath();ctx.arc(xs(hover.idx),axis(p[key]),active===key?5:3,0,Math.PI*2);ctx.fillStyle=color;ctx.fill();ctx.lineWidth=2;ctx.strokeStyle='#202020';ctx.stroke();});
  }
  FLOW_HITS[canvasId]={rows,points,x:xs,y,yp,padL,padR,padT,padB,plotW,plotH,mode};
}
function drawFlowWorkspace(){
  const status=document.getElementById('flowDataStatus'),qualityBadge=document.getElementById('flowQuality'),volumeTotal=document.getElementById('flowVolumeTotal'),driftTotal=document.getElementById('driftPremiumTotal'),coverage=document.getElementById('flowCoverage');
  if(!LIVE_FLOW?.ok){const msg=LIVE_FLOW?.detail||'Waiting for flow data';if(status){status.className='flow-status error';status.textContent=msg;}if(qualityBadge){qualityBadge.className='flow-quality bad';qualityBadge.textContent='Data Error';}if(volumeTotal)volumeTotal.textContent='--';if(driftTotal)driftTotal.textContent='--';if(coverage)coverage.textContent='-- classified';drawFlowEmpty('netFlowChart',msg);drawFlowEmpty('netDriftChart',msg);return;}
  if(LIVE_FLOW.history_available===false){
    const msg=`No saved history yet for ${LIVE_FLOW.session_date||'this session'}`;
    if(status){status.className='flow-status';status.textContent=msg;}
    if(qualityBadge){qualityBadge.className='flow-quality';qualityBadge.textContent='Historical Session';}
    if(volumeTotal)volumeTotal.textContent='--';if(driftTotal)driftTotal.textContent='--';if(coverage)coverage.textContent='Collector history begins after activation';
    drawFlowEmpty('netFlowChart',msg);drawFlowEmpty('netDriftChart',msg);return;
  }
  const pct=Math.round((LIVE_FLOW.classification_coverage||0)*100),visibleFrom=Number(localStorage.getItem(FLOW_RESET_KEY)||0);
  const resetApplies=visibleFrom>0&&flowSessionDateForTime(visibleFrom)===LIVE_FLOW.session_date;
  const visiblePoints=(LIVE_FLOW.points||[]).filter(point=>!resetApplies||Number(point.time)>=visibleFrom);
  const visibleTrades=visiblePoints.reduce((sum,point)=>sum+(Number(point.trades)||0),0);
  const points=expandFlowSession(visiblePoints,LIVE_FLOW.price_points||[],LIVE_FLOW.session_date);
  const callVolume=visiblePoints.reduce((sum,point)=>sum+(Number(point.callVolume)||0),0),putVolume=visiblePoints.reduce((sum,point)=>sum+(Number(point.putVolume)||0),0);
  const callDrift=visiblePoints.reduce((sum,point)=>sum+(Number(point.netCallPremium)||0),0),putDrift=visiblePoints.reduce((sum,point)=>sum+(Number(point.netPutPremium)||0),0);
  const midPremium=visiblePoints.reduce((sum,point)=>sum+(Number(point.midCallPremium)||0)+(Number(point.midPutPremium)||0),0);
  const lastSpot=[...points].reverse().find(point=>point.spot!==null&&point.spot!==''&&Number.isFinite(Number(point.spot)))?.spot;
  if(status){
    const collector=LIVE_FLOW.collector||{},collectorLabel=collector.state==='connected'?'Collector LIVE':collector.state==='retrying'?'Collector reconnecting':`Collector ${collector.state||'waiting'}`;
    status.className=`flow-status ${collector.state==='error'?'error':'live'}`;
    const sessionLabel=LIVE_FLOW.session_date?` · ${LIVE_FLOW.session_date}`:'';
    const windowLabel=points.length?`${flowTimeLabel(points[0].time)}–${flowTimeLabel(points.at(-1).time)} ET`:'waiting for first minute';
    status.textContent=LIVE_FLOW.load_warning||`${LIVE_FLOW.symbol}${sessionLabel} · ${collectorLabel} · ${visibleTrades.toLocaleString()} prints · ${windowLabel}`;
  }
  if(qualityBadge){
    const quality=LIVE_FLOW.collector?.quality||'unknown';
    const qualityLabels={healthy:'Data Healthy',market_closed:'Awaiting Market',no_option_ticks:'No Option Ticks',stale:'Feed Stale',disconnected:'Reconnecting',volume_warning:'Volume Warning',unknown:'Checking Data'};
    qualityBadge.textContent=qualityLabels[quality]||quality.replaceAll('_',' ');
    qualityBadge.className=`flow-quality ${quality==='healthy'?'healthy':quality==='market_closed'||quality==='unknown'?'':quality==='volume_warning'?'warning':'bad'}`;
    const collector=LIVE_FLOW.collector||{};
    qualityBadge.title=`Ticks ${collector.ticks||0} · Underlying ${collector.underlying_ticks||0} · Duplicates ${collector.duplicates||0} · Reconnects ${collector.reconnects||0}`;
  }
  if(volumeTotal)volumeTotal.textContent=`Calls ${fmtNum(callVolume)} · Puts ${fmtNum(putVolume)}${Number.isFinite(Number(lastSpot))?` · Price $${fmtFlowPrice(lastSpot)}`:''}`;
  if(driftTotal)driftTotal.textContent=`Calls ${fmtMoney(callDrift)} · Puts ${fmtMoney(putDrift)}${Number.isFinite(Number(lastSpot))?` · Price $${fmtFlowPrice(lastSpot)}`:''}`;
  if(coverage)coverage.textContent=`${pct}% classified · Mid ${fmtMoney(midPremium)}`;
  drawFlowCanvas('netFlowChart',points,'flow');drawFlowCanvas('netDriftChart',points,'drift');
}
function showFlowTooltip(e){
  const id=e.currentTarget.id,h=FLOW_HITS[id],drift=id==='netDriftChart',tt=document.getElementById(drift?'netDriftTooltip':'netFlowTooltip'),cross=document.getElementById(drift?'netDriftCrosshair':'netFlowCrosshair'),crossY=document.getElementById(drift?'netDriftCrosshairY':'netFlowCrosshairY');
  if(!h||!tt)return;const rect=e.currentTarget.getBoundingClientRect(),mx=e.clientX-rect.left,my=e.clientY-rect.top;
  if(mx<h.padL||mx>h.padL+h.plotW||my<h.padT||my>h.padT+h.plotH){hideFlowTooltip(e);return;}
  const idx=Math.max(0,Math.min(h.rows.length-1,Math.round((mx-h.padL)/h.plotW*(h.rows.length-1)))),p=h.rows[idx];
  if(p.future){hideFlowTooltip(e);return;}
  const exactIndex=(mx-h.padL)/h.plotW*(h.rows.length-1),left=Math.floor(exactIndex),right=Math.min(h.rows.length-1,Math.ceil(exactIndex)),mix=exactIndex-left;
  const interpolatedY=(key,axis)=>{const a=h.rows[left]?.[key],b=h.rows[right]?.[key];if(!Number.isFinite(a)||!Number.isFinite(b))return NaN;return axis(a)+(axis(b)-axis(a))*mix;};
  const candidates=[['call',interpolatedY('call',h.y)],['put',interpolatedY('put',h.y)],['price',interpolatedY('price',h.yp)]].filter(([,yy])=>Number.isFinite(yy));
  const nearest=candidates.sort((a,b)=>Math.abs(a[1]-my)-Math.abs(b[1]-my))[0],series=nearest&&Math.abs(nearest[1]-my)<=10?nearest[0]:null;
  const seriesY=series?nearest[1]:my;
  const previous=FLOW_HOVER[id];FLOW_HOVER[id]={idx,series};
  if(!previous||previous.idx!==idx||previous.series!==series)drawFlowCanvas(id,h.points,h.mode,FLOW_HOVER[id]);
  if(cross){cross.style.display='block';cross.style.left=h.x(idx)+'px';cross.style.top=h.padT+'px';cross.style.bottom=h.padB+'px';}
  if(crossY){crossY.style.display='block';crossY.style.left=h.padL+'px';crossY.style.right=h.padR+'px';crossY.style.top=seriesY+'px';}
  const otherId=id==='netDriftChart'?'netFlowChart':'netDriftChart',otherHit=FLOW_HITS[otherId],otherCross=document.getElementById(otherId==='netDriftChart'?'netDriftCrosshair':'netFlowCrosshair');
  if(otherHit&&otherCross){const ratio=h.rows.length>1?idx/(h.rows.length-1):0,otherIdx=Math.round(ratio*(otherHit.rows.length-1));otherCross.style.display='block';otherCross.style.left=otherHit.x(otherIdx)+'px';otherCross.style.top=otherHit.padT+'px';otherCross.style.bottom=otherHit.padB+'px';}
  const callValue=h.mode==='drift'?fmtMoney(p.call):`${fmtNum(p.call)} contracts`,putValue=h.mode==='drift'?fmtMoney(p.put):`${fmtNum(p.put)} contracts`;
  const rowOpacity=key=>series&&series!==key?'.38':'1';
  tt.innerHTML=`<div class="heatmap-tip-title">${p.label} ET</div><div class="heatmap-tip-row" style="opacity:${rowOpacity('call')}"><span>${h.mode==='drift'?'Call drift':'Call volume'}</span><b style="color:#27d66e">${callValue}</b></div><div class="heatmap-tip-row" style="opacity:${rowOpacity('put')}"><span>${h.mode==='drift'?'Put drift':'Put volume'}</span><b style="color:#ff3545">${putValue}</b></div><div class="heatmap-tip-row" style="opacity:${rowOpacity('price')}"><span>Price</span><b style="color:#22b8ff">${Number.isFinite(p.price)?'$'+fmtFlowPrice(p.price):'--'}</b></div><div class="heatmap-tip-row"><span>Prints</span><b>${p.trades||0}</b></div>`;
  tt.style.display='block';tt.style.left=Math.min(rect.width-tt.offsetWidth-6,mx+12)+'px';tt.style.top=Math.max(6,Math.min(rect.height-tt.offsetHeight-6,my+12))+'px';
}
function hideFlowTooltip(e){const id=e?.currentTarget?.id,drift=id==='netDriftChart';const tt=document.getElementById(drift?'netDriftTooltip':'netFlowTooltip');if(tt)tt.style.display='none';if(id&&FLOW_HOVER[id]){delete FLOW_HOVER[id];const h=FLOW_HITS[id];if(h)drawFlowCanvas(id,h.points,h.mode,null);}['netFlowCrosshair','netDriftCrosshair','netFlowCrosshairY','netDriftCrosshairY'].forEach(crossId=>{const cross=document.getElementById(crossId);if(cross)cross.style.display='none';});}
function hideAllHoverHelpers(){
  hideChartTooltip();
  hideHeatMapTip();
  hideNetFlowTooltip();
  hideDarkPoolTooltip();
  hideMaxPainTooltip();
  hideEdgeTooltip();
}
function buildDarkPoolLevels(chain){
  const r = RISK_FREE[chain.market] || 0.05;
  const byStrike = new Map();
  for(const q of chain.quotes){
    if(Math.abs(q.K-chain.spot)/chain.spot > 0.12) continue;
    const price = analyticsOptionPrice(chain,q);
    const premiumNotional = price * ((q.vol || 0) + (q.oi || 0)*0.08) * chain.mult;
    const size = (q.vol || 0) + (q.oi || 0);
    if(!byStrike.has(q.K)){
      byStrike.set(q.K,{level:q.K,size:0,notional:0,callPremium:0,putPremium:0});
    }
    const row = byStrike.get(q.K);
    row.size += size;
    row.notional += premiumNotional;
    if(q.isCall) row.callPremium += premiumNotional;
    else row.putPremium += premiumNotional;
  }
  const rows = [...byStrike.values()]
    .filter(r=>r.notional>0 && r.size>0)
    .sort((a,b)=>b.notional-a.notional)
    .slice(0,34)
    .sort((a,b)=>b.level-a.level);
  const maxNotional = Math.max(...rows.map(r=>r.notional),1);
  const nearest = rows.reduce((best,row)=>!best || Math.abs(row.level-chain.spot)<Math.abs(best.level-chain.spot)?row:best,null);
  return {symbol:chain.symbol,spot:chain.spot,rows,maxNotional,currentLevel:nearest?nearest.level:null};
}
function renderDarkPoolLevels(R){
  const host = document.getElementById('darkPoolLevels');
  if(!host || !R.darkPoolLevels) return;
  const D = R.darkPoolLevels;
  if(!D.rows.length){
    host.innerHTML = '<div class="darkpool-empty">No estimated levels available for the current selection.</div>';
    return;
  }
  const rows = D.rows.map(row=>{
    const strength = Math.min(1, row.notional/D.maxNotional);
    const intensity = Math.pow(strength, 0.58);
    const pct = Math.max(3, Math.min(100, strength*100));
    const rowBg = `rgba(34,184,255,${0.10 + intensity*0.30})`;
    const barBg = `rgba(34,184,255,${0.28 + intensity*0.58})`;
    const current = row.level===D.currentLevel ? ' current' : '';
    const payload = esc(JSON.stringify({
      symbol:D.symbol, level:`$${fmtPrice(row.level)}`, size:Math.round(row.size).toLocaleString(),
      notional:fmtMoney(row.notional), strength:`${Math.round(strength*100)}%`,
      spot:`$${fmtPrice(D.spot)}`
    }));
    return `<div class="darkpool-row${current}" style="background:${rowBg}" data-darkpool="${payload}">
      <div class="darkpool-level">$${fmtPrice(row.level)}</div>
      <div class="darkpool-track"><i class="darkpool-bar" style="width:${pct}%;background:${barBg}"></i></div>
      <div class="darkpool-size">${Math.round(row.size).toLocaleString()}</div>
      <div class="darkpool-notional">${fmtMoney(row.notional)}</div>
    </div>`;
  });
  const spotIndex = D.rows.findIndex(row=>row.level < D.spot);
  const priceLine = `<div class="darkpool-price-line"><span class="darkpool-price-label">Price: $${fmtPrice(D.spot)}</span></div>`;
  const displayRows = [...rows];
  if(spotIndex <= 0) displayRows.unshift(priceLine);
  else if(spotIndex >= rows.length) displayRows.push(priceLine);
  else displayRows.splice(spotIndex,0,priceLine);
  host.innerHTML = `
    <div class="darkpool-head">
      <div>Level</div>
      <div></div>
      <div>Size</div>
      <div>Notional Value</div>
    </div>
    ${displayRows.join('')}
  `;
}
function showDarkPoolTooltip(e){
  const rowEl = e.target.closest('.darkpool-row[data-darkpool]');
  const tip = document.getElementById('heatMapTooltip');
  document.querySelectorAll('.darkpool-row-hover').forEach(el=>el.classList.remove('darkpool-row-hover'));
  if(!rowEl || !tip){ if(tip) tip.style.display='none'; return; }
  rowEl.classList.add('darkpool-row-hover');
  const d = JSON.parse(rowEl.dataset.darkpool);
  const row = (label,value,cls='') => `<div class="heatmap-tip-row"><span>${label}</span><span class="${cls}">${value}</span></div>`;
  tip.innerHTML = `
    <div class="heatmap-tip-title">${d.symbol} Â· Dark Pool Level ${d.level}</div>
    ${row('Size',d.size)}
    ${row('Notional Value',d.notional,'heatmap-pos')}
    ${row('Relative Strength',d.strength)}
    ${row('Current Price',d.spot)}
  `;
  tip.style.display='block';
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  let left = e.clientX + 14, top = e.clientY + 14;
  if(left + tw > window.innerWidth) left = e.clientX - tw - 14;
  if(top + th > window.innerHeight) top = e.clientY - th - 14;
  tip.style.left = Math.max(6,left)+'px';
  tip.style.top = Math.max(6,top)+'px';
}
function hideDarkPoolTooltip(){
  const tip = document.getElementById('heatMapTooltip');
  if(tip) tip.style.display='none';
  document.querySelectorAll('.darkpool-row-hover').forEach(el=>el.classList.remove('darkpool-row-hover'));
}
function buildMaxPain(chain, selectedExpirations){
  const byExp = new Map();
  for(const q of chain.quotes){
    if(!q.exp) continue;
    if(!byExp.has(q.exp)) byExp.set(q.exp,new Map());
    const expMap = byExp.get(q.exp);
    if(!expMap.has(q.K)) expMap.set(q.K,{K:q.K,callOI:0,putOI:0});
    const row = expMap.get(q.K);
    if(q.isCall) row.callOI += q.oi || 0;
    else row.putOI += q.oi || 0;
  }
  const expiries = [...byExp.keys()].sort();
  const selected = [...selectedExpirations].filter(exp=>byExp.has(exp)).sort();
  function calcRows(rows, exp=null, selectedExps=[]){
    rows.sort((a,b)=>a.K-b.K);
    const strikes = rows.map(r=>r.K);
    const painRows = strikes.map(S=>{
      let callPain = 0, putPain = 0;
      for(const r of rows){
        callPain += Math.max(0, S-r.K) * r.callOI * chain.mult;
        putPain += Math.max(0, r.K-S) * r.putOI * chain.mult;
      }
      return {strike:S,callPain,putPain,totalPain:callPain+putPain};
    });
    const maxPain = painRows.reduce((best,row)=>!best || row.totalPain<best.totalPain ? row : best, null);
    return {exp,selectedExps,rows:painRows,maxPain:maxPain?maxPain.strike:null,maxPainValue:maxPain?maxPain.totalPain:0};
  }
  function calcForExp(exp){
    return calcRows([...(byExp.get(exp) || new Map()).values()],exp,[exp]);
  }
  function calcCombined(expList){
    const combined = new Map();
    for(const exp of expList){
      for(const source of (byExp.get(exp) || new Map()).values()){
        if(!combined.has(source.K)) combined.set(source.K,{K:source.K,callOI:0,putOI:0});
        const row = combined.get(source.K);
        row.callOI += source.callOI;
        row.putOI += source.putOI;
      }
    }
    return calcRows([...combined.values()],expList.length===1 ? expList[0] : null,expList);
  }
  const activeExpiries = selected.length ? selected : (expiries[0] ? [expiries[0]] : []);
  const active = activeExpiries.length
    ? calcCombined(activeExpiries)
    : {exp:null,selectedExps:[],rows:[],maxPain:null,maxPainValue:0};
  const time = expiries.map(calcForExp).filter(x=>x.maxPain!=null);
  return {symbol:chain.symbol,spot:chain.spot,active,time,selectedExpirations:selected,expiries};
}
let maxPainView = 'chart';
function renderMaxPainValues(R){
  const host = document.getElementById('maxPainValues');
  if(!host) return;
  const maxPain = R?.maxPain?.active?.maxPain;
  if(maxPain == null){
    host.innerHTML = '';
    return;
  }
  const linkedMaxPain = linkedMarketPrice(maxPain,{symbol:R?.maxPain?.symbol});
  host.innerHTML = `
    <div class="maxpain-value" style="--c:#ffc107">
      <span class="k">Max Pain ${esc(R.maxPain.symbol)}</span>
      <span class="v">${fmtPrice(maxPain)}</span>
    </div>
    ${linkedMaxPain == null ? '' : `<div class="maxpain-value spy" style="--c:#22b8ff">
      <span class="k">Max Pain ${esc(linkedMaxPain.symbol)} calc</span>
      <span class="v">${fmtLinkedMarketPrice(linkedMaxPain)}</span>
    </div>`}
  `;
}
function drawMaxPain(R){
  const cv = document.getElementById('maxPainChart');
  if(!cv || !R.maxPain) return;
  renderMaxPainValues(R);
  if(maxPainView === 'time') return drawMaxPainTime(R);
  const ctx = cv.getContext('2d');
  const dpr = window.devicePixelRatio||1;
  const W = cv.clientWidth;
  const H = Math.max(520, Math.min(760, window.innerHeight-170));
  cv.width = W*dpr; cv.height = H*dpr; cv.style.height = H+'px';
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle='#202020'; ctx.fillRect(0,0,W,H);
  const M = R.maxPain.active;
  const expiryLabel = M.selectedExps?.length > 1
    ? `, ${M.selectedExps.length} Selected Expirations`
    : (M.exp ? `, Expires on ${M.exp}` : '');
  const chartTitle = `Max Pain (${R.maxPain.symbol}${expiryLabel})`;
  const title = document.getElementById('maxPainTitle');
  if(title) title.textContent = chartTitle;
  const legend = document.getElementById('maxPainLegend');
  if(legend) legend.style.display = 'flex';
  if(!M.rows.length){
    ctx.fillStyle='rgba(255,255,255,.75)';
    ctx.font='800 18px Segoe UI';
    ctx.textAlign='center';
    ctx.fillText('No Max Pain data available for the current selection.', W/2, H/2);
    return;
  }
  const rows = M.rows;
  const maxValue = niceMax(Math.max(...rows.map(r=>r.totalPain),1));
  const padL=78,padR=36,padT=62,padB=78, plotW=W-padL-padR, plotH=H-padT-padB;
  const x = i => padL + (i/(rows.length-1 || 1))*plotW;
  const y = v => padT + plotH - (v/maxValue)*plotH;
  ctx.strokeStyle='rgba(255,255,255,.09)'; ctx.lineWidth=1;
  ctx.fillStyle='#22b8ff'; ctx.font='12px Segoe UI'; ctx.textAlign='right'; ctx.textBaseline='middle';
  for(let g=0;g<=4;g++){
    const yy=padT+plotH*g/4;
    ctx.beginPath(); ctx.moveTo(padL,yy); ctx.lineTo(W-padR,yy); ctx.stroke();
    ctx.fillText(fmtMoney(maxValue*(1-g/4)).replace('.00',''), padL-12, yy);
  }
  const barW = Math.max(2, plotW/rows.length*0.72);
  rows.forEach((row,i)=>{
    const xx=x(i)-barW/2;
    const yy=y(row.totalPain);
    const h=padT+plotH-yy;
    ctx.fillStyle = row.strike < M.maxPain ? 'rgba(255,42,23,.64)' : 'rgba(23,182,95,.64)';
    ctx.fillRect(xx,yy,barW,h);
  });
  function vLine(price,color,label){
    if(price == null) return;
    let idx = rows.reduce((best,row,i)=>Math.abs(row.strike-price)<Math.abs(rows[best].strike-price)?i:best,0);
    const xx = x(idx);
    ctx.strokeStyle=color; ctx.lineWidth=3;
    ctx.beginPath(); ctx.moveTo(xx,padT); ctx.lineTo(xx,padT+plotH); ctx.stroke();
    ctx.save();
    ctx.translate(xx+8,padT+10);
    ctx.rotate(Math.PI/2);
    ctx.fillStyle=color;
    ctx.font='900 13px Segoe UI';
    ctx.textAlign='left';
    ctx.textBaseline='middle';
    ctx.fillText(label,0,0);
    ctx.restore();
  }
  vLine(R.maxPain.spot,'#f5f5f5',`Last Price (${fmtPrice(R.maxPain.spot)})`);
  vLine(M.maxPain,'#ffc107',`Max Pain (${fmtPrice(M.maxPain)})`);
  ctx.fillStyle='#f0f0f0'; ctx.font='900 18px Segoe UI'; ctx.textAlign='center'; ctx.textBaseline='alphabetic';
  ctx.fillText(chartTitle, W/2, 30);
  ctx.fillStyle='#22b8ff'; ctx.font='12px Segoe UI';
  const step = Math.max(1, Math.ceil(rows.length/24));
  rows.forEach((row,i)=>{
    if(i%step && i!==rows.length-1) return;
    ctx.save(); ctx.translate(x(i),H-padB+22); ctx.rotate(-Math.PI/4); ctx.textAlign='right'; ctx.fillText('$'+fmtPrice(row.strike),0,0); ctx.restore();
  });
  ctx.textAlign='center'; ctx.fillText('Strike Price',padL+plotW/2,H-20);
  ctx.save(); ctx.translate(24,padT+plotH/2); ctx.rotate(-Math.PI/2); ctx.fillText('Intrinsic Value',0,0); ctx.restore();
  window._maxPainHit = {type:'chart',rows,x,y,padL,padR,padT,padB,plotW,plotH,maxValue,maxPain:M.maxPain,spot:R.maxPain.spot};
}
function drawMaxPainTime(R){
  const cv = document.getElementById('maxPainChart');
  renderMaxPainValues(R);
  const ctx = cv.getContext('2d');
  const dpr = window.devicePixelRatio||1;
  const W = cv.clientWidth;
  const H = Math.max(520, Math.min(760, window.innerHeight-170));
  cv.width = W*dpr; cv.height = H*dpr; cv.style.height = H+'px';
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle='#202020'; ctx.fillRect(0,0,W,H);
  const title = document.getElementById('maxPainTitle');
  if(title) title.textContent = `Max Pain / Time (${R.maxPain.symbol})`;
  const legend = document.getElementById('maxPainLegend');
  if(legend) legend.style.display = 'none';
  const pts = R.maxPain.time;
  if(!pts.length){
    ctx.fillStyle='rgba(255,255,255,.75)';
    ctx.font='800 18px Segoe UI';
    ctx.textAlign='center';
    ctx.fillText('No Max Pain / Time data available.', W/2, H/2);
    return;
  }
  const padL=78,padR=36,padT=58,padB=92, plotW=W-padL-padR, plotH=H-padT-padB;
  const minP = Math.min(...pts.map(p=>p.maxPain), R.maxPain.spot);
  const maxP = Math.max(...pts.map(p=>p.maxPain), R.maxPain.spot);
  const pricePad = Math.max((maxP-minP)*0.18, R.maxPain.spot*0.01);
  const yMin = minP-pricePad, yMax=maxP+pricePad;
  const x = i => padL + (i/(pts.length-1 || 1))*plotW;
  const y = v => padT + plotH - ((v-yMin)/(yMax-yMin))*plotH;
  ctx.strokeStyle='rgba(255,255,255,.09)'; ctx.lineWidth=1;
  ctx.fillStyle='#22b8ff'; ctx.font='12px Segoe UI'; ctx.textAlign='right'; ctx.textBaseline='middle';
  for(let g=0;g<=5;g++){
    const yy=padT+plotH*g/5;
    ctx.beginPath(); ctx.moveTo(padL,yy); ctx.lineTo(W-padR,yy); ctx.stroke();
    ctx.fillText('$'+fmtPrice(yMax-(yMax-yMin)*g/5), padL-12, yy);
  }
  const spotY = y(R.maxPain.spot);
  ctx.strokeStyle='#f5f5f5'; ctx.lineWidth=2;
  ctx.beginPath(); ctx.moveTo(padL,spotY); ctx.lineTo(W-padR,spotY); ctx.stroke();
  ctx.fillStyle='#f5f5f5'; ctx.font='900 12px Segoe UI'; ctx.textAlign='left'; ctx.fillText(`Last Price (${fmtPrice(R.maxPain.spot)})`, padL+6, spotY-8);
  ctx.strokeStyle='#8ecbff'; ctx.lineWidth=2.5; ctx.beginPath();
  pts.forEach((p,i)=>i?ctx.lineTo(x(i),y(p.maxPain)):ctx.moveTo(x(i),y(p.maxPain))); ctx.stroke();
  pts.forEach((p,i)=>{
    ctx.fillStyle='#8ecbff'; ctx.beginPath(); ctx.arc(x(i),y(p.maxPain),4,0,Math.PI*2); ctx.fill();
    if(i%Math.max(1,Math.ceil(pts.length/14))===0 || i===pts.length-1){
      ctx.fillStyle='#fff'; ctx.font='900 13px Segoe UI'; ctx.textAlign='center';
      ctx.fillText('$'+fmtPrice(p.maxPain),x(i),y(p.maxPain)-10);
    }
  });
  ctx.fillStyle='#f0f0f0'; ctx.font='900 18px Segoe UI'; ctx.textAlign='center';
  ctx.fillText(`Max Pain / Time (${R.maxPain.symbol})`,W/2,30);
  ctx.fillStyle='#22b8ff'; ctx.font='12px Segoe UI';
  const step = Math.max(1, Math.ceil(pts.length/18));
  pts.forEach((p,i)=>{
    if(i%step && i!==pts.length-1) return;
    ctx.save(); ctx.translate(x(i),H-padB+28); ctx.rotate(-Math.PI/4); ctx.textAlign='right'; ctx.fillText(p.exp,0,0); ctx.restore();
  });
  ctx.textAlign='center'; ctx.fillText('Expiration Date',padL+plotW/2,H-20);
  ctx.save(); ctx.translate(24,padT+plotH/2); ctx.rotate(-Math.PI/2); ctx.fillText('Strike Price',0,0); ctx.restore();
  window._maxPainHit = {type:'time',pts,x,y,padL,padR,padT,padB,plotW,plotH,yMin,yMax,spot:R.maxPain.spot};
}
function showMaxPainTooltip(e){
  const h=window._maxPainHit, tt=document.getElementById('maxPainTooltip');
  if(!h || !tt) return;
  const cross = document.getElementById('maxPainCrosshair');
  const rect=e.currentTarget.getBoundingClientRect();
  const mx=e.clientX-rect.left, my=e.clientY-rect.top;
  if(mx<h.padL || mx>h.padL+h.plotW || my<h.padT || my>h.padT+h.plotH){
    tt.style.display='none';
    if(cross) cross.style.display='none';
    return;
  }
  let crossX = mx;
  if(h.type==='chart'){
    const idx=Math.max(0,Math.min(h.rows.length-1,Math.round((mx-h.padL)/h.plotW*(h.rows.length-1))));
    const p=h.rows[idx];
    crossX = h.x(idx);
    tt.innerHTML=`<div class="heatmap-tip-title">Strike $${fmtPrice(p.strike)}</div>
      <div class="heatmap-tip-row"><span>Total Pain</span><span>${fmtMoney(p.totalPain)}</span></div>
      <div class="heatmap-tip-row"><span>Call Pain</span><span style="color:#17b65f;font-weight:800">${fmtMoney(p.callPain)}</span></div>
      <div class="heatmap-tip-row"><span>Put Pain</span><span style="color:#ff2a17;font-weight:800">${fmtMoney(p.putPain)}</span></div>`;
  }else{
    const idx=Math.max(0,Math.min(h.pts.length-1,Math.round((mx-h.padL)/h.plotW*(h.pts.length-1))));
    const p=h.pts[idx];
    crossX = h.x(idx);
    tt.innerHTML=`<div class="heatmap-tip-title">${p.exp}</div>
      <div class="heatmap-tip-row"><span>Max Pain</span><span style="color:#8ecbff;font-weight:800">$${fmtPrice(p.maxPain)}</span></div>
      <div class="heatmap-tip-row"><span>Last Price</span><span>$${fmtPrice(h.spot)}</span></div>`;
  }
  if(cross){
    cross.style.display='block';
    cross.style.left = `${crossX}px`;
    cross.style.top = `${h.padT}px`;
    cross.style.bottom = `${h.padB}px`;
  }
  tt.style.display='block';
  tt.style.left=Math.min(rect.width-tt.offsetWidth-6,mx+14)+'px';
  tt.style.top=Math.max(6,Math.min(rect.height-tt.offsetHeight-6,my+14))+'px';
}
function hideMaxPainTooltip(){
  const tt=document.getElementById('maxPainTooltip');
  if(tt) tt.style.display='none';
  const cross = document.getElementById('maxPainCrosshair');
  if(cross) cross.style.display='none';
}
function renderImpl(R){
  window._lastR=R;
  const badge=document.getElementById('srcBadge');
  const nowClient=new Date().toLocaleTimeString('he-IL');
  if(R.live){ badge.style.color="var(--green)"; badge.style.borderColor="var(--green)";
              badge.textContent=`LIVE - ${R.source||'Tripity'} · data ${R.asof||''} · refreshed ${nowClient}`; }
  else      { badge.style.color="var(--amber)"; badge.style.borderColor="var(--amber)";
              badge.textContent="DEMO - synthetic data Â· refreshed "+nowClient; }
  const regClass = R.regime==="positive_gamma"?"reg-pos":R.regime==="negative_gamma"?"reg-neg":"reg-neu";
  const regHeb = {positive_gamma:"Positive Gamma",negative_gamma:"Negative Gamma",neutral:"Neutral"}[R.regime];
  const linkedSpot = linkedMarketPrice(R.spot,R);
  const linkedSpotLine = linkedSpot == null ? '' : `<div class="k" style="margin-top:6px">${esc(linkedSpot.symbol)} calc: ${fmtLinkedMarketPrice(linkedSpot)}</div>`;
  document.getElementById('topCards').innerHTML = `
    <div class="card"><div class="k">Symbol / Spot</div>
      <div class="v">${R.symbol} <small>${fmtPrice(R.spot)}</small></div>
      ${linkedSpotLine}</div>
    <div class="card"><div class="k">Regime</div>
      <div class="v"><span class="regime-badge ${regClass}">${regHeb}</span></div>
      <div class="k" style="margin-top:8px">strength: ${R.strength}</div></div>
    <div class="card"><div class="k">Total Net GEX</div>
      <div class="v" style="color:${R.totalGex>=0?'var(--green)':'var(--red)'}">${fmtNum(R.totalGex)}</div>
      <div class="k" style="margin-top:6px">$ per 1 point move</div></div>
    <div class="card"><div class="k">PCR (OI)</div>
      <div class="v">${R.pcr.toFixed(2)}</div>
      <div class="k" style="margin-top:6px">${R.sentiment.replace(/_/g,' ')}</div></div>
    <div class="card"><div class="k">Broker Timestamp</div>
      <div class="v" style="font-size:16px">${ts(R.fetchTs)}</div>
      <div class="k" style="margin-top:6px">${R.keptCount}/${R.totalCount} quotes Â· DTE: ${R.expiries.join(', ')}</div></div>
  `;

  renderCommandCenter(R);
  renderOptionsHeatMap(R);
  renderDarkPoolLevels(R);
  if(activeView === 'gex'){
    drawChart(R);
  }
  if(activeView === 'dex') drawChart(R,'dexChart');
  if(activeView === 'vex'){ drawChart(R,'vexChart'); renderGreekExposureKpis(R,'vex'); }
  if(activeView === 'chex'){ drawChart(R,'chexChart'); renderGreekExposureKpis(R,'chex'); }
  if(activeView === 'market-structure') renderMarketStructure(R);
  if(activeView === 'exposure-lab') drawExposureLab(R);
  if(activeView === 'matrix-gex') drawChart(R,'matrixGexChart');
  if(activeView === 'shock-engine') drawShockEngine(R);
  if(activeView === 'net-flow') loadFlowData();
  if(activeView === 'max-pain') drawMaxPain(R);
  if(activeView === 'edge') drawEdge(R);
  if(activeView === 'review') renderReviewPage();
  return;

  document.getElementById('levels').innerHTML = `
    <div class="lvl maxg"><span class="name">Max Gamma Strike (pivot)</span><span class="val">${fmtPrice(R.maxGammaStrike)}</span></div>
    <div class="lvl flip"><span class="name">Zero-Gamma Flip</span><span class="val">${R.flip?fmtPrice(R.flip):'-'}</span></div>
    <div class="lvl call"><span class="name">Call Wall (resistance)</span><span class="val">${fmtPrice(R.callWall)} <small style="color:var(--muted)">${fmtNum(R.callWallGex)}</small></span></div>
    <div class="lvl put"><span class="name">Put Wall (support)</span><span class="val">${fmtPrice(R.putWall)} <small style="color:var(--muted)">${fmtNum(R.putWallGex)}</small></span></div>
  `;

  const above = R.spot>=R.maxGammaStrike;
  let txt;
  if(R.regime==="positive_gamma"){
    txt = `<b>Positive Gamma</b>: dealers are likely selling into strength and buying into weakness, which favors <b>mean reversion</b> and lower realized volatility.
           Spot is ${above?'above':'below'} Max Gamma (${fmtPrice(R.maxGammaStrike)}).
           Expected trading zone is between Put Wall ${fmtPrice(R.putWall)} and Call Wall ${fmtPrice(R.callWall)}. Strategy: premium selling / range trading.`;
  } else if(R.regime==="negative_gamma"){
    txt = `<b>Negative Gamma</b>: dealers are likely buying into strength and selling into weakness, which favors <b>momentum and higher volatility</b>.
           A break of the Flip (${R.flip?fmtPrice(R.flip):'-'}) with volume may accelerate the move. Strategy: trend following / long options.`;
  } else {
    txt = `<b>Neutral</b>: Total GEX is low, so the hedge pressure signal is weak. Wait for clearer structure and reduce risk.`;
  }
  txt += ` PCR=${R.pcr.toFixed(2)} (${R.sentiment.replace(/_/g,' ')}), IV skew=${(R.ivSkew*100).toFixed(1)}% (${R.ivSkew>0.005?'puts richer':R.ivSkew<-0.005?'calls richer':'balanced'}).`;
  document.getElementById('interp').innerHTML = txt;

  const N = +document.getElementById('strikeCount').value;
  const nearest = R.strikes.reduce((a,s)=>Math.abs(s.strike-R.spot)<Math.abs(a-R.spot)?s.strike:a, R.strikes[0].strike);
  const sorted = [...R.strikes].sort((a,b)=>Math.abs(a.strike-R.spot)-Math.abs(b.strike-R.spot)).slice(0,N*2+1)
                   .sort((a,b)=>b.strike-a.strike);
  let rows = `<tr><th>Strike</th><th>Net GEX</th><th>Call GEX</th><th>Put GEX</th>
              <th>Call OI</th><th>Put OI</th><th>PCR</th><th>Call IV</th><th>Put IV</th><th>Vol</th></tr>`;
  for(const s of sorted){
    const isSpot = s.strike===nearest;
    rows += `<tr class="${isSpot?'spotrow':''}">
      <td>${fmtPrice(s.strike)}</td>
      <td class="${s.netGex>=0?'pos':'neg'}">${fmtNum(s.netGex)}</td>
      <td class="pos">${fmtNum(s.callGex)}</td>
      <td class="neg">${fmtNum(s.putGex)}</td>
      <td>${fmtNum(s.callOI)}</td>
      <td>${fmtNum(s.putOI)}</td>
      <td>${s.pcr.toFixed(2)}</td>
      <td>${(s.callIV*100).toFixed(1)}%</td>
      <td>${(s.putIV*100).toFixed(1)}%</td>
      <td>${fmtNum(s.totalVol)}</td></tr>`;
  }
  document.getElementById('strikeTable').innerHTML = rows;

  document.getElementById('flow').innerHTML = `
    <div class="flowitem"><div class="k">Total Call GEX</div><div class="v" style="color:var(--green)">${fmtNum(R.totalCallGex)}</div></div>
    <div class="flowitem"><div class="k">Total Put GEX</div><div class="v" style="color:var(--red)">${fmtNum(R.totalPutGex)}</div></div>
    <div class="flowitem"><div class="k">Net Call OI</div><div class="v">${fmtNum(R.netCallOI)}</div></div>
    <div class="flowitem"><div class="k">Net Put OI</div><div class="v">${fmtNum(R.netPutOI)}</div></div>
    <div class="flowitem"><div class="k">Call GEX %</div><div class="v">${R.callGexPct.toFixed(1)}%</div></div>
    <div class="flowitem"><div class="k">IV Skew</div><div class="v">${(R.ivSkew*100).toFixed(2)}%</div></div>
    <div class="flowitem"><div class="k">Avg Call IV</div><div class="v">${(R.avgCallIV*100).toFixed(1)}%</div></div>
    <div class="flowitem"><div class="k">Avg Put IV</div><div class="v">${(R.avgPutIV*100).toFixed(1)}%</div></div>
  `;

  if(R.atmGreeks){
    const g=R.atmGreeks;
    document.getElementById('greeks').innerHTML = `
      <div class="flowitem"><div class="k">Delta</div><div class="v">${g.delta.toFixed(3)}</div></div>
      <div class="flowitem"><div class="k">Gamma</div><div class="v">${g.gamma.toFixed(5)}</div></div>
      <div class="flowitem"><div class="k">Theta /day</div><div class="v">${g.theta.toFixed(3)}</div></div>
      <div class="flowitem"><div class="k">Vega /1%</div><div class="v">${g.vega.toFixed(3)}</div></div>
    `;
  }
  drawChart(R);
}

// ---------- Metric definitions (Quant-Power style param buttons) ----------
// net_gex = bars. Other metrics are stacked area layers.
let ACTIVE = new Set(["net_gex"]);
let DISPLAY_SIGMA = 2;
const METRICS = {
  net_gex:  {label:"Net GEX",     color:"#22b8ff", kind:"bar",  signed:true,  val:s=>s.netGex},
  net_dex:  {label:"Net DEX",     color:"#22b8ff", kind:"bar",  signed:true,  val:s=>s.netDex},
  net_vex:  {label:"Net VEX",     color:"#22b8ff", kind:"bar",  signed:true,  val:s=>s.netVex,  axisLabel:"Net VEX ($ per 1% vol move)"},
  net_charm:{label:"Net CHEX",    color:"#22b8ff", kind:"bar",  signed:true,  val:s=>s.netCharm,axisLabel:"Net CHEX ($ per day)"},
  ag:       {label:"AG",          color:"#ab7df6", kind:"area", val:s=>Math.abs(s.callGex)+Math.abs(s.putGex)},
  call_oi:  {label:"Call OI",     color:"#26c281", kind:"area", val:s=>s.callOI},
  put_oi:   {label:"Put OI",      color:"#ef5350", kind:"area", val:s=>s.putOI},
  call_vol: {label:"Call Volume", color:"#2f73ff", kind:"area", val:s=>s.callVol},
  put_vol:  {label:"Put Volume",  color:"#ff8a3d", kind:"area", val:s=>s.putVol},
  power:    {label:"Power Zone",  color:"#fff200", kind:"area", val:s=>s.powerZone || 0},
  avg_power:{label:"AVG Power Zone",color:"#00cdb7",kind:"reference",val:s=>s.powerZone || 0},
  weighted: {label:"Weighted",    color:"#ff5fa2", kind:"overlay", val:()=>0},
};
// Charts whose bars are a fixed Greek metric instead of the GEX param buttons.
const BAR_METRIC_BY_CHART = {dexChart:'net_dex', vexChart:'net_vex', chexChart:'net_charm'};
const BAR_METRIC_KEYS = Object.values(BAR_METRIC_BY_CHART);
// Primary bar metric of the main GEX chart, switched by the DEX|GEX|VEX|CHEX
// segmented control in the chart card. Module-level so it survives re-renders
// (symbol/expiration changes, live spot poller). Default: Net GEX.
let GEX_CHART_BAR_METRIC = 'net_gex';
function chartBarMetricKey(chartId){
  return chartId==='gexChart' ? GEX_CHART_BAR_METRIC : (BAR_METRIC_BY_CHART[chartId] || null);
}
// Active metric list for a chart: bar metric first (unless the user toggled the
// Net GEX pbtn off while GEX is the selected bar metric), then non-bar overlays.
function chartActiveMetrics(chartId){
  const barKey=chartBarMetricKey(chartId);
  const order=Object.keys(METRICS).filter(m=>!BAR_METRIC_KEYS.includes(m));
  if(chartId==='gexChart'){
    const barOn = GEX_CHART_BAR_METRIC!=='net_gex' || ACTIVE.has('net_gex');
    return {active:[...(barOn?[barKey]:[]), ...order.filter(m=>ACTIVE.has(m) && METRICS[m].kind!=='bar')], hasBar:barOn};
  }
  if(barKey){
    return {active:[barKey,...(ACTIVE.has('avg_power')?['avg_power']:[])], hasBar:true};
  }
  return {active:order.filter(m=>ACTIVE.has(m)), hasBar:ACTIVE.has('net_gex')};
}
// OI-weighted average strikes over the currently selected expirations.
function oiWeightedStrikes(R){
  const rows=R?.strikes||[];
  let callOI=0,callSum=0,putOI=0,putSum=0;
  rows.forEach(s=>{
    const c=Number(s.callOI)||0, p=Number(s.putOI)||0;
    callOI+=c; callSum+=(Number(s.strike)||0)*c;
    putOI+=p;  putSum+=(Number(s.strike)||0)*p;
  });
  const div=(sum,w)=>w>0?sum/w:null;
  return {call:div(callSum,callOI), put:div(putSum,putOI), total:div(callSum+putSum,callOI+putOI)};
}
const WEIGHTED_LINES = [
  {key:'total', name:'Total W', color:'#ab7df6'},
];
function hexA(hex,a){const n=parseInt(hex.slice(1),16);return `rgba(${n>>16&255},${n>>8&255},${n&255},${a})`;}

// ---------- Canvas chart: vertical bars by strike + area overlays ----------
function chartTargets(chartId){
  const map={
    gexChart:      {symbolId:'symLabel',        legendId:'chartLegend',      tooltipId:'chartTooltip',     crosshairId:'chartCrosshairX'},
    dexChart:      {symbolId:'dexSymLabel',     legendId:'dexChartLegend',   tooltipId:'dexTooltip',       crosshairId:'dexCrosshairX'},
    vexChart:      {symbolId:'vexSymLabel',     legendId:'vexChartLegend',   tooltipId:'vexTooltip',       crosshairId:'vexCrosshairX'},
    chexChart:     {symbolId:'chexSymLabel',    legendId:'chexChartLegend',  tooltipId:'chexTooltip',      crosshairId:'chexCrosshairX'},
    matrixGexChart:{symbolId:'matrixSymLabel',  legendId:'matrixChartLegend',tooltipId:'matrixGexTooltip', crosshairId:'matrixGexCrosshairX'},
    marketGexChart:{symbolId:'marketGexSymLabel',legendId:'marketGexLegend', tooltipId:'marketGexTooltip', crosshairId:'marketGexCrosshairX'},
  };
  return {canvasId:chartId, ...(map[chartId] || map.gexChart)};
}
function visibleStrikeData(R){
  let visibleStrikes=[...R.strikes];
  if(Number.isFinite(DISPLAY_SIGMA) && R.expectedMove>0){
    const radius=DISPLAY_SIGMA*R.expectedMove;
    visibleStrikes=visibleStrikes.filter(s=>Math.abs(s.strike-R.spot)<=radius);
  }
  if(!visibleStrikes.length){
    visibleStrikes=[...R.strikes].sort((a,b)=>Math.abs(a.strike-R.spot)-Math.abs(b.strike-R.spot)).slice(0,1);
  }
  const data=visibleStrikes.sort((a,b)=>a.strike-b.strike);
  return data;
}
function avgPowerZoneStrike(data){
  const weighted=(data||[]).reduce((acc,row)=>{
    const weight=Math.max(0,Number(row.powerZone)||0);
    acc.weight+=weight;acc.total+=(Number(row.strike)||0)*weight;return acc;
  },{weight:0,total:0});
  return weighted.weight>0?weighted.total/weighted.weight:null;
}
function buildDayBasedAvgPowerZone(chain,mode){
  const byExpiry=new Map();
  (chain?.quotes||[]).forEach(q=>{
    const expiry=String(q.exp||'');
    if(!expiry) return;
    if(!byExpiry.has(expiry)) byExpiry.set(expiry,[]);
    byExpiry.get(expiry).push(q);
  });
  const days=[...byExpiry.entries()].sort(([a],[b])=>a.localeCompare(b)).map(([expiry,quotes])=>{
    const dayResult=calcGEX({...chain,quotes},mode);
    const strike=avgPowerZoneStrike(dayResult.strikes);
    return {expiry,strike,contracts:quotes.length};
  }).filter(day=>Number.isFinite(day.strike));
  return {
    strike:days.length?days.reduce((sum,day)=>sum+day.strike,0)/days.length:null,
    dayCount:days.length,
    start:days[0]?.expiry||null,
    end:days[days.length-1]?.expiry||null,
    days,
    method:'equal_weighted_expiry_days',
  };
}
function avgPowerZoneForResult(R){
  return Number.isFinite(R?.avgPowerZone?.strike)
    ? R.avgPowerZone.strike
    : avgPowerZoneStrike(R?.strikes||[]);
}
function avgPowerZoneChartLabel(R){
  const count=Number(R?.avgPowerZone?.dayCount)||1;
  return `AVG Power Zone (${count} ${count===1?'day':'days'})`;
}
function drawChart(R,chartId='gexChart'){
  const targets=chartTargets(chartId);
  const chartBarKey=chartBarMetricKey(chartId);
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

  const metricsState=chartActiveMetrics(chartId);
  const active=metricsState.active;
  const hasBar=metricsState.hasBar;
  const barMetric=METRICS[chartBarKey] || METRICS.net_gex;
  const areas = active.filter(m=>METRICS[m].kind==="area");

  // header symbol + legend
  const symLabel=byId(targets.symbolId);
  const legend=byId(targets.legendId);
  if(symLabel) symLabel.textContent = R.symbol;
  if(legend) legend.innerHTML = active.map(m=>{
    const M=METRICS[m];
    if(M.kind==="overlay"){
      const w=oiWeightedStrikes(R);
      return WEIGHTED_LINES.map(line=>
        `<span class="sq" style="height:2px;background:${line.color};border-radius:0"></span>${line.name} ${Number.isFinite(w[line.key])?fmtPrice(w[line.key]):'--'}`
      ).join('&nbsp;&nbsp;');
    }
    const sw = M.kind==="bar"
      ? `<span class="sq" style="background:${M.color}"></span>`
      : M.kind==="reference"
        ? `<span class="sq" style="height:2px;background:${M.color};border-radius:0"></span>`
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

  // ----- weighted average Power Zone strike -----
  if(active.includes('avg_power')){
    const avgStrike=avgPowerZoneForResult(R);
    if(Number.isFinite(avgStrike)){
      const avgX=xAt(avgStrike),color=METRICS.avg_power.color;
      ctx.save();ctx.strokeStyle=color;ctx.lineWidth=2;ctx.setLineDash([7,5]);
      ctx.beginPath();ctx.moveTo(avgX,padT-6);ctx.lineTo(avgX,bottom);ctx.stroke();ctx.restore();
      const avgLabel=`${avgPowerZoneChartLabel(R)}: ${fmtPrice(avgStrike)}`;
      ctx.font=(mobile?"bold 10px Segoe UI":"bold 12px Segoe UI");
      const avgWidth=ctx.measureText(avgLabel).width,avgRight=avgX>W-avgWidth-12;
      ctx.fillStyle=color;ctx.textAlign=avgRight?'right':'left';ctx.textBaseline='alphabetic';
      ctx.fillText(avgLabel,avgX+(avgRight?-6:6),padT+12);
    }
  }

  // ----- OI-weighted average strikes overlay (GEX view "Weighted" toggle) -----
  if(chartId==='gexChart' && ACTIVE.has('weighted')){
    const w=oiWeightedStrikes(R);
    WEIGHTED_LINES.forEach((line,idx)=>{
      const val=w[line.key];
      if(!Number.isFinite(val)) return;
      const lx=xAt(val);
      ctx.save();ctx.strokeStyle=line.color;ctx.lineWidth=2;ctx.setLineDash([4,4]);
      ctx.beginPath();ctx.moveTo(lx,padT-6);ctx.lineTo(lx,bottom);ctx.stroke();ctx.restore();
      const wLabel=`${line.name}: ${fmtPrice(val)}`;
      ctx.font=(mobile?"bold 10px Segoe UI":"bold 12px Segoe UI");
      const wWidth=ctx.measureText(wLabel).width,wRight=lx>W-wWidth-12;
      ctx.fillStyle=line.color;ctx.textAlign=wRight?'right':'left';ctx.textBaseline='alphabetic';
      ctx.fillText(wLabel,lx+(wRight?-6:6),padT+26+idx*14);
    });
  }

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

// ---------- Exposure Lab (isolated workspace; original GEX view is unchanged) ----------
let LAB_SIGMA=2;
function labVisibleData(R){
  let data=[...R.strikes].sort((a,b)=>a.strike-b.strike);
  if(Number.isFinite(LAB_SIGMA) && R.expectedMove>0){
    const radius=LAB_SIGMA*R.expectedMove;
    data=data.filter(s=>Math.abs(s.strike-R.spot)<=radius);
  }
  if(!data.length) data=[...R.strikes].sort((a,b)=>Math.abs(a.strike-R.spot)-Math.abs(b.strike-R.spot)).slice(0,1);
  return data.sort((a,b)=>a.strike-b.strike);
}
function labExposureColor(v,alpha=1){
  return v>=0 ? `rgba(24,229,91,${alpha})` : `rgba(255,48,48,${alpha})`;
}
function drawLabExposureBars(R,{canvasId,valueKey,label}){
  const cv=byId(canvasId);
  if(!cv) return;
  const data=labVisibleData(R);
  const dpr=window.devicePixelRatio||1,W=cv.clientWidth,H=W<560?330:360;
  cv.width=W*dpr;cv.height=H*dpr;cv.style.height=H+'px';
  const ctx=cv.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,W,H);
  const mobile=W<560,padL=mobile?48:62,padR=18,padT=28,padB=55;
  const plotW=W-padL-padR,plotH=H-padT-padB,bottom=padT+plotH,slot=plotW/data.length;
  const vals=data.map(s=>Number(s[valueKey])||0),maxAbs=Math.max(...vals.map(Math.abs),1)*1.08;
  const y=v=>padT+(maxAbs-v)/(maxAbs*2)*plotH,y0=y(0),x=i=>padL+slot*(i+.5);
  ctx.font='bold 10px Segoe UI';ctx.textBaseline='middle';
  for(let i=0;i<=4;i++){
    const yy=padT+plotH*i/4,v=maxAbs-(maxAbs*2*i/4);
    ctx.strokeStyle='rgba(78,132,190,.13)';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(padL,yy);ctx.lineTo(W-padR,yy);ctx.stroke();
    ctx.fillStyle='#7897ba';ctx.textAlign='right';ctx.fillText(fmtAxis(v),padL-7,yy);
  }
  const bw=Math.max(3,Math.min(slot*.66,22));
  const hits=data.map((s,i)=>{
    const v=Number(s[valueKey])||0,yy=y(v),barY=Math.min(yy,y0),h=Math.max(1,Math.abs(yy-y0)),barX=x(i)-bw/2;
    ctx.fillStyle=labExposureColor(v,.96);ctx.fillRect(barX,barY,bw,h);
    return {x:barX,y:barY,w:bw,h,s,html:`<strong>${R.symbol} Â· Strike ${fmtPrice(s.strike)}</strong><div><span>${label}</span><b class="${v>=0?'pos':'neg'}">${fmtNum(v)}</b></div>`};
  });
  ctx.strokeStyle='rgba(197,222,250,.32)';ctx.beginPath();ctx.moveTo(padL,y0);ctx.lineTo(W-padR,y0);ctx.stroke();
  const every=Math.max(1,Math.ceil(data.length/(mobile?7:11)));
  ctx.fillStyle='#7f9dbd';ctx.font='bold 9px Segoe UI';ctx.textAlign='center';ctx.textBaseline='top';
  data.forEach((s,i)=>{if(i%every===0||i===data.length-1)ctx.fillText(fmtPrice(s.strike),x(i),bottom+9);});
  const xSpot=data.length===1?x(0):padL+((R.spot-data[0].strike)/(data.at(-1).strike-data[0].strike))*plotW;
  if(xSpot>=padL&&xSpot<=W-padR){
    ctx.strokeStyle='#268cff';ctx.lineWidth=1.5;ctx.setLineDash([5,4]);ctx.beginPath();ctx.moveTo(xSpot,padT);ctx.lineTo(xSpot,bottom);ctx.stroke();ctx.setLineDash([]);
    ctx.fillStyle='#57a8ff';ctx.textAlign=xSpot>W-100?'right':'left';ctx.textBaseline='bottom';ctx.font='bold 9px Segoe UI';ctx.fillText('Spot '+fmtPrice(R.spot),xSpot+(xSpot>W-100?-5:5),padT-4);
  }
  cv._labHits=hits;
}
function drawLabMap(R){
  const cv=byId('labMapChart');if(!cv)return;
  let data=labVisibleData(R);
  if(data.length>17){const step=Math.ceil(data.length/17);data=data.filter((_,i)=>i%step===0||i===data.length-1);}
  const metrics=[['GEX','netGex'],['DEX','netDex'],['VEX','netVex'],['CHEX','netCharm']];
  const dpr=window.devicePixelRatio||1,W=cv.clientWidth,H=W<560?390:410;
  cv.width=W*dpr;cv.height=H*dpr;cv.style.height=H+'px';
  const ctx=cv.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,W,H);
  const padL=54,padR=18,padT=38,padB=28,plotW=W-padL-padR,plotH=H-padT-padB;
  const x=i=>padL+plotW*(i+.5)/metrics.length;
  const minK=data[0]?.strike||R.spot,maxK=data.at(-1)?.strike||R.spot+1;
  const y=K=>padT+(maxK-K)/Math.max(maxK-minK,1)*plotH;
  const maxByKey=Object.fromEntries(metrics.map(([,key])=>[key,Math.max(...data.map(s=>Math.abs(Number(s[key])||0)),1)]));
  ctx.font='bold 10px Segoe UI';ctx.textAlign='center';ctx.textBaseline='bottom';
  metrics.forEach(([name],i)=>{ctx.fillStyle='#a9c5e6';ctx.fillText(name,x(i),padT-10);ctx.strokeStyle='rgba(67,118,172,.12)';ctx.beginPath();ctx.moveTo(x(i),padT);ctx.lineTo(x(i),H-padB);ctx.stroke();});
  const hits=[];
  data.forEach(s=>metrics.forEach(([name,key],i)=>{
    const v=Number(s[key])||0,r=2+Math.sqrt(Math.abs(v)/maxByKey[key])*12,cx=x(i),cy=y(s.strike);
    ctx.fillStyle=labExposureColor(v,.82);ctx.beginPath();ctx.arc(cx,cy,r,0,Math.PI*2);ctx.fill();
    hits.push({cx,cy,r,s,html:`<strong>${R.symbol} Â· Strike ${fmtPrice(s.strike)}</strong><div><span>${name}</span><b class="${v>=0?'pos':'neg'}">${fmtNum(v)}</b></div>`});
  }));
  const ySpot=y(R.spot);if(ySpot>=padT&&ySpot<=H-padB){ctx.strokeStyle='#268cff';ctx.lineWidth=1.5;ctx.beginPath();ctx.moveTo(padL,ySpot);ctx.lineTo(W-padR,ySpot);ctx.stroke();ctx.fillStyle='#57a8ff';ctx.textAlign='left';ctx.textBaseline='bottom';ctx.fillText('Spot '+fmtPrice(R.spot),padL+4,ySpot-4);}
  ctx.fillStyle='#7897ba';ctx.textAlign='right';ctx.textBaseline='middle';
  const tickCount=Math.min(7,data.length);for(let i=0;i<tickCount;i++){const K=minK+(maxK-minK)*i/Math.max(tickCount-1,1);ctx.fillText(fmtPrice(K),padL-7,y(K));}
  cv._labHits=hits;
}
function renderLabHeatMap(R){
  const host=byId('labHeatMap');if(!host)return;
  let data=labVisibleData(R);
  if(data.length>15){data=[...data].sort((a,b)=>Math.abs(a.strike-R.spot)-Math.abs(b.strike-R.spot)).slice(0,15).sort((a,b)=>b.strike-a.strike);}else data=[...data].sort((a,b)=>b.strike-a.strike);
  const metrics=[['GEX','netGex'],['DEX','netDex'],['VEX','netVex'],['CHEX','netCharm']];
  const max=Object.fromEntries(metrics.map(([,key])=>[key,Math.max(...data.map(s=>Math.abs(Number(s[key])||0)),1)]));
  const cell=(v,key)=>{const t=Math.pow(Math.min(1,Math.abs(v)/max[key]),.55),bg=v>=0?`rgba(11,142,58,${.22+t*.7})`:`rgba(190,24,35,${.22+t*.7})`;return `<td style="background:${bg}" data-lab-tip="${esc(key)}|${v}">${fmtAxis(v)}</td>`;};
  host.innerHTML=`<table class="lab-heat-table"><thead><tr><th>Strike</th>${metrics.map(([n])=>`<th>${n}</th>`).join('')}</tr></thead><tbody>${data.map(s=>`<tr class="${Math.abs(s.strike-R.spot)===Math.min(...data.map(x=>Math.abs(x.strike-R.spot)))?'at-spot':''}"><td>${fmtPrice(s.strike)}</td>${metrics.map(([,key])=>cell(Number(s[key])||0,key)).join('')}</tr>`).join('')}</tbody></table>`;
}
function drawExposureLab(R){
  const kpis=byId('labKpis');if(!kpis)return;
  const regime=R.regime==='positive_gamma'?'Positive Gamma':R.regime==='negative_gamma'?'Negative Gamma':'Neutral Gamma';
  const kpi=(label,value,note,cls='')=>`<div class="lab-kpi ${cls}"><span>${label}</span><strong>${value}</strong><small>${note}</small></div>`;
  kpis.innerHTML=kpi('Underlying',`${R.symbol} ${fmtPrice(R.spot)}`,'Live reference price','spot')+kpi('Total Net GEX',fmtNum(R.totalGex),'Per 1% underlying move',R.totalGex>=0?'positive':'negative')+kpi('Total Net DEX',fmtNum(R.totalDex),'Directional dealer exposure',R.totalDex>=0?'positive':'negative')+kpi('Gamma regime',regime,`Strength: ${R.strength}`,R.totalGex>=0?'positive':'negative');
  byId('labGexTitle').textContent=`Net Gamma Exposure â€” ${R.symbol}`;byId('labDexTitle').textContent=`Net Delta Exposure â€” ${R.symbol}`;
  drawLabExposureBars(R,{canvasId:'labGexChart',valueKey:'netGex',label:'Net GEX'});
  drawLabExposureBars(R,{canvasId:'labDexChart',valueKey:'netDex',label:'Net DEX'});
  drawLabMap(R);renderLabHeatMap(R);
}
function showLabTooltip(ev){
  const cv=ev.currentTarget,tt=byId('labTooltip'),rect=cv.getBoundingClientRect(),x=ev.clientX-rect.left,y=ev.clientY-rect.top,hits=cv._labHits||[];
  const hit=hits.find(h=>h.cx!=null?Math.hypot(h.cx-x,h.cy-y)<=h.r+5:x>=h.x-4&&x<=h.x+h.w+4&&y>=h.y-6&&y<=h.y+h.h+6);
  if(!hit){tt.style.display='none';return;}tt.innerHTML=hit.html;tt.style.display='block';tt.style.left=Math.min(window.innerWidth-tt.offsetWidth-8,ev.clientX+13)+'px';tt.style.top=Math.min(window.innerHeight-tt.offsetHeight-8,ev.clientY+13)+'px';
}
function hideLabTooltip(){const tt=byId('labTooltip');if(tt)tt.style.display='none';}
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
  const chartBarKey=chartBarMetricKey(cv.id);
  const active=chartActiveMetrics(cv.id).active;
  const row = (label,value,cls='') => `<div class="tt-row"><span>${label}</span><span class="${cls}">${value}</span></div>`;
  const metricRows = active.map(m=>{
    if(m==='net_gex') return row('Net GEX',fmtNum(s.netGex),s.netGex>=0?'pos':'neg');
    if(m==='net_dex') return [
      row('Net DEX',fmtNum(s.netDex),s.netDex>=0?'pos':'neg'),
      row('Call DEX',fmtNum(s.callDex),'pos'),
      row('Put DEX',fmtNum(s.putDex),'neg'),
    ].join('');
    if(m==='net_vex') return [
      row('Net VEX',fmtNum(s.netVex),s.netVex>=0?'pos':'neg'),
      row('Call VEX',fmtNum(s.callVex),'pos'),
      row('Put VEX',fmtNum(s.putVex),'neg'),
      row('Units','$ per 1% vol move'),
    ].join('');
    if(m==='net_charm') return [
      row('Net CHEX',fmtNum(s.netCharm),s.netCharm>=0?'pos':'neg'),
      row('Call CHEX',fmtNum(s.callCharm),'pos'),
      row('Put CHEX',fmtNum(s.putCharm),'neg'),
      row('Units','$ per day'),
    ].join('');
    if(m==='weighted') return '';
    if(m==='ag') return row('AG',fmtNum(Math.abs(s.callGex)+Math.abs(s.putGex)));
    if(m==='call_oi') return row('Call OI',fmtNum(s.callOI),'pos');
    if(m==='put_oi') return row('Put OI',fmtNum(s.putOI),'neg');
    if(m==='call_vol') return row('Call Volume',fmtNum(s.callVol),'pos');
    if(m==='put_vol') return row('Put Volume',fmtNum(s.putVol),'neg');
    if(m==='power') return row('Power Zone',fmtNum(s.powerZone || 0));
    if(m==='avg_power') return row(avgPowerZoneChartLabel(R),fmtPrice(avgPowerZoneForResult(R)));
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
  ['chartTooltip','matrixGexTooltip','dexTooltip','vexTooltip','chexTooltip'].forEach(id=>{const el=byId(id); if(el) el.style.display='none';});
  ['chartCrosshairX','matrixGexCrosshairX','dexCrosshairX','vexCrosshairX','chexCrosshairX'].forEach(id=>{const el=byId(id); if(el) el.style.display='none';});
}
// KPI cards for the VEX / CHEX views (totals over the selected expirations).
function renderGreekExposureKpis(R,kind){
  const host=byId(kind+'Kpis');
  if(!host) return;
  const isVex=kind==='vex';
  const netKey=isVex?'netVex':'netCharm', callKey=isVex?'callVex':'callCharm', putKey=isVex?'putVex':'putCharm';
  const label=isVex?'VEX':'CHEX';
  const unit=isVex?'$ per 1% vol move':'$ per day';
  const strikes=R.strikes||[];
  const sum=key=>strikes.reduce((a,s)=>a+(Number(s[key])||0),0);
  const total=sum(netKey), calls=sum(callKey), puts=sum(putKey);
  const top=strikes.reduce((best,s)=>!best||Math.abs(Number(s[netKey])||0)>Math.abs(Number(best[netKey])||0)?s:best,null);
  host.innerHTML=`
    <div class="card"><div class="k">Total Net ${label}</div>
      <div class="v" style="color:${total>=0?'var(--green)':'var(--red)'}">${fmtNum(total)}</div>
      <div class="k" style="margin-top:6px">${unit}</div></div>
    <div class="card"><div class="k">Total Call ${label}</div>
      <div class="v" style="color:${calls>=0?'var(--green)':'var(--red)'}">${fmtNum(calls)}</div>
      <div class="k" style="margin-top:6px">${unit}</div></div>
    <div class="card"><div class="k">Total Put ${label}</div>
      <div class="v" style="color:${puts>=0?'var(--green)':'var(--red)'}">${fmtNum(puts)}</div>
      <div class="k" style="margin-top:6px">${unit}</div></div>
    <div class="card"><div class="k">Largest |Net ${label}| Strike</div>
      <div class="v">${top?fmtPrice(top.strike):'--'} <small>${top?fmtNum(Number(top[netKey])||0):''}</small></div>
      <div class="k" style="margin-top:6px">${unit}</div></div>`;
}
// ---------- Dealer Pressure: GEX / Vanna / Charm pressure map ----------
function edgeVisibleStrikes(R){
  let data=[...R.strikes];
  if(Number.isFinite(DISPLAY_SIGMA) && R.expectedMove>0){
    const radius=DISPLAY_SIGMA*R.expectedMove;
    data=data.filter(s=>Math.abs(s.strike-R.spot)<=radius);
  }
  if(!data.length){
    data=[...R.strikes].sort((a,b)=>Math.abs(a.strike-R.spot)-Math.abs(b.strike-R.spot)).slice(0,1);
  }
  return data.sort((a,b)=>a.strike-b.strike);
}
function buildDealerPressureData(R){
  const data=edgeVisibleStrikes(R);
  const maxG=Math.max(...data.map(s=>Math.abs(s.netGex||0)),1);
  const maxV=Math.max(...data.map(s=>Math.abs(s.netVex||0)),1);
  const maxC=Math.max(...data.map(s=>Math.abs(s.netCharm||0)),1);
  const maxAG=Math.max(...data.map(s=>Math.abs(s.callGex||0)+Math.abs(s.putGex||0)),1);
  const rows=data.map((s,i)=>{
    const g=(s.netGex||0)/maxG;
    const v=(s.netVex||0)/maxV;
    const c=(s.netCharm||0)/maxC;
    const pressure=.62*g+.23*v+.15*c;
    const ag=(Math.abs(s.callGex||0)+Math.abs(s.putGex||0))/maxAG;
    return {...s,gexN:g,vannaN:v,charmN:c,pressure,ag,idx:i};
  });
  rows.forEach((row,i)=>{
    const prev=rows[Math.max(0,i-1)].pressure;
    const next=rows[Math.min(rows.length-1,i+1)].pressure;
    row.slope=(next-prev)/2;
    const nearSpot=R.expectedMove>0 ? Math.abs(row.strike-R.spot)/R.expectedMove : Math.abs(row.strike-R.spot)/(R.spot*.01);
    if(Math.abs(row.pressure)<.18 && row.ag>.45 && nearSpot<.9) row.zone='Pin';
    else if(Math.abs(row.slope)>.28 && Math.abs(row.pressure)>.25) row.zone='Acceleration';
    else if(row.strike<R.spot && row.pressure>0) row.zone='Support';
    else if(row.strike>R.spot && row.pressure<0) row.zone='Resistance';
    else row.zone=row.pressure>=0?'Stabilizing':'Fragile';
  });
  return rows;
}
function pressureColor(row,alpha=1){
  if(row.zone==='Pin') return `rgba(255,193,7,${alpha})`;
  if(row.zone==='Acceleration') return row.pressure>=0 ? `rgba(34,170,242,${alpha})` : `rgba(255,42,23,${alpha})`;
  if(row.zone==='Support') return `rgba(23,182,95,${alpha})`;
  if(row.zone==='Resistance') return `rgba(255,126,50,${alpha})`;
  return row.pressure>=0 ? `rgba(34,170,242,${alpha})` : `rgba(255,42,23,${alpha})`;
}
function renderDealerScenarios(R){
  const host=document.getElementById('dealerScenarioPanel');
  if(!host) return;
  const scenarios=R.dealerScenarios || [];
  if(!scenarios.length){ host.innerHTML=''; return; }
  host.innerHTML=scenarios.map(s=>`
    <div class="scenario-card ${s.cls}">
      <div class="scenario-head">
        <span>${fmtPrice(s.spot)}</span>
        <span class="scenario-tag">${s.label}</span>
      </div>
      <div class="scenario-meta">
        <span>Move <b>${fmtMove(s.delta)}</b></span>
        <span>Flow <b>${s.pressure}</b></span>
        <span>GEX <b>${fmtNum(s.totalGex)}</b></span>
        <span>Regime <b>${s.regime.replace('_',' ')}</b></span>
      </div>
    </div>
  `).join('');
}
function fmtFlow(n){
  const sign=n>0?'+':(n<0?'-':'');
  return sign+fmtNum(Math.abs(n));
}
function flowPart(label,value){
  const color=value>0?'#22aaf2':(value<0?'#ff453a':'#cfd4d8');
  return `<span><em>${label}</em><b style="color:${color}">${fmtFlow(value)}</b></span>`;
}
function renderDealerFlowMap(R){
  const host=document.getElementById('dealerFlowMap');
  if(!host) return;
  const map=R.dealerFlowMap;
  if(!map?.rows?.length){ host.innerHTML=''; return; }
  const current=map.rows.find(r=>r.cls==='pin') || nearestScenario(map.rows,()=>true,R.spot);
  const strongest=[...map.rows].sort((a,b)=>b.abs-a.abs)[0];
  const bias=strongest?.netFlow>0 ? 'Net forced buy zone' : strongest?.netFlow<0 ? 'Net forced sell zone' : 'Pinned / neutral';
  host.innerHTML=`
    <div class="flow-map-head">
      <div>
        <h3>Dealer Flow Map</h3>
        <p>Estimated hedge change from current spot. Gamma = spot move, Vanna = IV response, Charm = next 1 hour decay.</p>
      </div>
      <div class="flow-map-bias">${bias}</div>
    </div>
    <div class="flow-map-grid">
      ${map.rows.map(row=>`
        <div class="flow-node ${row.cls}" style="opacity:${0.72+row.intensity*.28}">
          <div class="strike">
            <span>${fmtPrice(row.spot)}</span>
            <span class="tag">${row.label}</span>
          </div>
          <div class="net">${fmtFlow(row.netFlow)}</div>
          <div class="flow-parts">
            ${flowPart('Gamma',row.gammaFlow)}
            ${flowPart('Vanna',row.vannaFlow)}
            ${flowPart('Charm',row.charmFlow)}
          </div>
        </div>
      `).join('')}
    </div>
    <div class="market-read-note">Book read: ${map.regime}. Current node: ${current ? `${fmtPrice(current.spot)} ${fmtFlow(current.netFlow)}` : '--'}.</div>
  `;
}
function nearestScenario(scenarios,predicate,spot,dir){
  const filtered=scenarios.filter(predicate);
  if(!filtered.length) return null;
  return filtered.sort((a,b)=>Math.abs(a.spot-spot)-Math.abs(b.spot-spot))[0];
}
function updateMatrixPriceNote(text){
  const note=document.getElementById('matrixPriceNote');
  if(note) note.textContent=text;
}
function fetchMatrixCandles(){
  return fetchLiveData(MATRIX_CANDLES_URL + '?symbol=SPY&interval=1m&range=1d&t=' + Date.now())
    .then(d=>d && d.ok && Array.isArray(d.candles) ? d.candles : []);
}
function ensureMatrixPriceChart(){
  const el=document.getElementById('matrixPriceChart');
  if(!el) return null;
  if(!window.LightweightCharts){
    updateMatrixPriceNote('Loading chart engine...');
    if(window.__loadMatrixChartLib) window.__loadMatrixChartLib();
    return null;
  }
  if(_matrixChart && _matrixCandleSeries) return {chart:_matrixChart,series:_matrixCandleSeries};
  try{
    _matrixChart=LightweightCharts.createChart(el,{
      width:Math.max(320,el.clientWidth||640),
      height:Math.max(260,el.clientHeight||430),
      layout:{background:{color:'#191919'},textColor:'#cfd4d8',fontFamily:'Segoe UI, Arial, sans-serif'},
      grid:{vertLines:{color:'rgba(255,255,255,.045)'},horzLines:{color:'rgba(255,255,255,.06)'}},
      rightPriceScale:{borderColor:'#343434',scaleMargins:{top:.12,bottom:.18}},
      timeScale:{borderColor:'#343434',timeVisible:true,secondsVisible:false},
      crosshair:{mode:0},
    });
    const candleOptions={
      upColor:'#22aaf2',
      downColor:'#ff2a17',
      borderUpColor:'#22aaf2',
      borderDownColor:'#ff2a17',
      wickUpColor:'#72cfff',
      wickDownColor:'#ff7a70',
    };
    _matrixCandleSeries=_matrixChart.addCandlestickSeries
      ? _matrixChart.addCandlestickSeries(candleOptions)
      : _matrixChart.addSeries(LightweightCharts.CandlestickSeries,candleOptions);
    if(window.ResizeObserver){
      const ro=new ResizeObserver(()=>_matrixChart.applyOptions({
        width:Math.max(320,el.clientWidth||640),
        height:Math.max(260,el.clientHeight||430),
      }));
      ro.observe(el);
    }
  } catch(e){
    updateMatrixPriceNote(`Chart error: ${e.message || 'could not initialize'}`);
    return null;
  }
  return {chart:_matrixChart,series:_matrixCandleSeries};
}
function clearMatrixPriceLines(){
  if(!_matrixCandleSeries) return;
  _matrixPriceLines.forEach(line=>{
    try{ _matrixCandleSeries.removePriceLine(line); }catch(e){}
  });
  _matrixPriceLines=[];
}
function chartLineColor(s){
  if(!s) return '#ffc107';
  if(s.cls==='support') return '#17b65f';
  if(s.cls==='resistance') return '#ff7e32';
  if(s.cls==='accel-up') return '#22aaf2';
  if(s.cls==='accel-down') return '#ff2a17';
  if(s.cls==='pin') return '#ffc107';
  return '#cfd4d8';
}
function chartLevelName(s){
  if(!s) return 'Level';
  if(s.cls==='support') return 'Support';
  if(s.cls==='resistance') return 'Resistance';
  if(s.cls==='accel-up') return 'Accel Up';
  if(s.cls==='accel-down') return 'Accel Down';
  if(s.cls==='pin') return 'Pin';
  return s.label || 'Level';
}
function selectedChartScenarios(R){
  const scenarios=R.dealerScenarios || [];
  const spot=R.spot;
  const now=nearestScenario(scenarios,s=>Math.abs(s.delta)<=scenarioStepForSymbol(R.symbol)/2,spot) || nearestScenario(scenarios,()=>true,spot);
  const below=nearestScenario(scenarios,s=>s.spot<spot && (s.cls==='accel-down' || s.cls==='support'),spot);
  const above=nearestScenario(scenarios,s=>s.spot>spot && (s.cls==='accel-up' || s.cls==='resistance'),spot);
  const resistance=nearestScenario(scenarios,s=>s.spot>spot && s.cls==='resistance',spot);
  const seen=new Set();
  return [below,now,above,resistance].filter(s=>{
    if(!s || seen.has(s.spot)) return false;
    seen.add(s.spot);
    return true;
  });
}
function renderMatrixPriceChart(R){
  const kit=ensureMatrixPriceChart();
  if(!kit){
    if(window.LightweightCharts) return;
    setTimeout(()=>renderMatrixPriceChart(R),500);
    return;
  }
  const {chart,series}=kit;
  clearMatrixPriceLines();
  selectedChartScenarios(R).forEach(s=>{
    const spy=spyPriceFromSpxStrike(s.spot,R);
    if(spy==null) return;
    const line=series.createPriceLine({
      price:spy,
      color:chartLineColor(s),
      lineWidth:s.cls==='pin'?2:1,
      lineStyle:LightweightCharts.LineStyle.Solid,
      axisLabelVisible:true,
      title:`${chartLevelName(s)} ${fmtSpyConvertedPrice(spy)}`,
    });
    _matrixPriceLines.push(line);
  });
  const now=Date.now();
  const needsCandles=!_matrixCandles.length || now-_matrixCandlesLoadedAt>60000;
  const draw=()=>{
    if(_matrixCandles.length){
      series.setData(_matrixCandles);
      const last=_matrixCandles[_matrixCandles.length-1];
      if(last && Number.isFinite(last.close)){
        const currentLine=series.createPriceLine({
          price:last.close,
          color:'#3f6fff',
          lineWidth:1,
          lineStyle:LightweightCharts.LineStyle.Dotted,
          axisLabelVisible:true,
          title:`Current SPY ${fmtSpyConvertedPrice(last.close)}`,
        });
        _matrixPriceLines.push(currentLine);
      }
      chart.timeScale().fitContent();
      updateMatrixPriceNote('SPY candles. Colored lines are SPX pressure levels converted to SPY.');
    } else {
      updateMatrixPriceNote('Waiting for SPY candles from Tripity...');
    }
  };
  if(!needsCandles){ draw(); return; }
  updateMatrixPriceNote('Loading SPY candles...');
  fetchMatrixCandles().then(candles=>{
    const byTime=new Map();
    candles.map(c=>({
      time:Number(c.time),
      open:Number(c.open),
      high:Number(c.high),
      low:Number(c.low),
      close:Number(c.close),
    })).filter(c=>Number.isFinite(c.time) && Number.isFinite(c.open) && Number.isFinite(c.high) && Number.isFinite(c.low) && Number.isFinite(c.close))
      .forEach(c=>byTime.set(c.time,c));
    _matrixCandles=[...byTime.values()].sort((a,b)=>a.time-b.time);
    _matrixCandlesLoadedAt=Date.now();
    draw();
  }).catch(err=>{
    updateMatrixPriceNote(`Could not draw SPY candles: ${err?.message || 'data load failed'}. Pressure levels below still update.`);
  });
}

// ---------- Market Structure: GEX followed by intraday candles ----------
function fetchMarketStructureCandles(symbol){
  const safeSymbol=encodeURIComponent(String(symbol || 'SPY').toUpperCase());
  const feed=database=>{
    const record=database?.[symbol];
    return Array.isArray(record?.candles)&&record.candles.length
      ? {raw:record.candles,source:record.source||'Market data',asof:record.asof,proxy:false} : null;
  };
  return fetchLiveData(`${MATRIX_CANDLES_URL}?symbol=${safeSymbol}&interval=1m&range=1d&t=${Date.now()}`)
    .then(d=>({raw:d&&d.ok&&Array.isArray(d.candles)?d.candles:[],source:'Tripity',asof:d?.asof,proxy:false}));
}
function setMarketStructureStatus(text,state='loading'){
  const el=document.getElementById('marketStructureStatus');
  if(!el) return;
  el.textContent=text;
  el.dataset.state=state;
}
function marketStrongest(rows,value){
  if(!rows?.length) return null;
  return rows.reduce((best,row)=>value(row)>value(best)?row:best,rows[0]);
}
function marketStructureRows(R){
  const expected=Number(R.expectedMove)||0;
  const radius=Math.max(R.spot*.02,Math.min(R.spot*.035,expected*2||R.spot*.02));
  const rows=(R.strikes||[]).filter(s=>Math.abs(Number(s.strike)-R.spot)<=radius);
  return rows.length?rows:(R.strikes||[]).slice().sort((a,b)=>Math.abs(a.strike-R.spot)-Math.abs(b.strike-R.spot)).slice(0,6).sort((a,b)=>a.strike-b.strike);
}
function marketStructureLevels(R){
  const rows=marketStructureRows(R);
  const above=rows.filter(s=>s.strike>=R.spot),below=rows.filter(s=>s.strike<=R.spot);
  const p1=marketStrongest(above.length?above:rows,s=>Number(s.netGex)||0);
  const n1=marketStrongest(below.length?below:rows,s=>-(Number(s.netGex)||0));
  const callVol=marketStrongest(rows,s=>Number(s.callVol)||0);
  const putVol=marketStrongest(rows,s=>Number(s.putVol)||0);
  const power=marketStrongest(rows,s=>Number(s.powerZone)||0);
  const ag=marketStrongest(rows,s=>Math.abs(Number(s.callGex)||0)+Math.abs(Number(s.putGex)||0));
  return [
    {price:p1?.strike,label:'P1 Strike',color:'#20c941',width:3},
    {price:n1?.strike,label:'N1 Strike',color:'#ff2417',width:3},
    {price:callVol?.strike,label:'Call Vol Strike',color:'#16a9f4',width:2},
    {price:putVol?.strike,label:'Put Vol Strike',color:'#b8733e',width:2},
    {price:power?.strike,label:'Matrix Power',color:'#f2d51b',width:2},
    {price:ag?.strike,label:'AG Strike',color:'#b85ac7',width:2,dashed:true},
  ].filter(level=>Number.isFinite(level.price) && level.price>0);
}
function updateMarketPriceTooltip(param){
  const tt=document.getElementById('marketPriceTooltip');
  if(!tt || !_marketCandleSeries || !param?.point || param.point.x<0 || param.point.y<0){
    if(tt) tt.style.display='none';
    return;
  }
  const bar=param.seriesData?.get(_marketCandleSeries);
  if(!bar){tt.style.display='none';return;}
  const date=param.time ? new Date(Number(param.time)*1000) : null;
  const time=date && Number.isFinite(date.getTime())
    ? date.toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit'}) : '';
  tt.innerHTML=`<strong>${esc(_marketPriceSymbol)} ${time}</strong>
    <span>O ${fmtPrice(bar.open)}</span><span>H ${fmtPrice(bar.high)}</span>
    <span>L ${fmtPrice(bar.low)}</span><span>C ${fmtPrice(bar.close)}</span>`;
  tt.style.display='flex';
  tt.style.left=Math.max(8,Math.min(param.point.x+14,(_marketChartElement?.clientWidth||600)-tt.offsetWidth-8))+'px';
  tt.style.top=Math.max(8,Math.min(param.point.y+14,(_marketChartElement?.clientHeight||520)-tt.offsetHeight-8))+'px';
}
function ensureMarketPriceChart(){
  const el=document.getElementById('marketPriceChart');
  if(!el) return null;
  _marketChartElement=el;
  if(!window.LightweightCharts){
    setMarketStructureStatus('Loading chart engineâ€¦');
    if(window.__loadMatrixChartLib) window.__loadMatrixChartLib();
    return null;
  }
  if(_marketPriceChart && _marketCandleSeries) return {chart:_marketPriceChart,series:_marketCandleSeries};
  try{
    _marketPriceChart=LightweightCharts.createChart(el,{
      width:Math.max(320,el.clientWidth||800),
      height:Math.max(390,el.clientHeight||540),
      layout:{background:{color:'#191919'},textColor:'#d3d7db',fontFamily:'Segoe UI, Arial, sans-serif'},
      grid:{vertLines:{color:'rgba(255,255,255,.055)'},horzLines:{color:'rgba(255,255,255,.07)'}},
      rightPriceScale:{borderColor:'#3b3b3b',scaleMargins:{top:.08,bottom:.10}},
      timeScale:{borderColor:'#3b3b3b',timeVisible:true,secondsVisible:false,rightOffset:5},
      crosshair:{mode:0,vertLine:{color:'rgba(255,255,255,.48)',style:2},horzLine:{color:'rgba(255,255,255,.34)',style:2}},
    });
    const candleOptions={
      upColor:'#26a69a',downColor:'#ef5350',borderVisible:false,
      wickUpColor:'#58c7bb',wickDownColor:'#ff756f',
    };
    _marketCandleSeries=_marketPriceChart.addCandlestickSeries
      ? _marketPriceChart.addCandlestickSeries(candleOptions)
      : _marketPriceChart.addSeries(LightweightCharts.CandlestickSeries,candleOptions);
    _marketPriceChart.subscribeCrosshairMove(updateMarketPriceTooltip);
    if(window.ResizeObserver){
      _marketResizeObserver=new ResizeObserver(()=>{
        _marketPriceChart.applyOptions({
          width:Math.max(320,el.clientWidth||800),height:Math.max(390,el.clientHeight||540),
        });
        requestAnimationFrame(()=>drawMarketPowerProfile(window._lastR));
      });
      _marketResizeObserver.observe(el);
    }
  }catch(e){
    setMarketStructureStatus(`Chart error: ${e.message || 'initialization failed'}`,'error');
    return null;
  }
  return {chart:_marketPriceChart,series:_marketCandleSeries};
}
function clearMarketPriceLines(){
  if(!_marketCandleSeries) return;
  _marketPriceLines.forEach(line=>{try{_marketCandleSeries.removePriceLine(line);}catch(e){}});
  _marketPriceLines=[];
}
function drawMarketPowerProfile(R){
  const cv=document.getElementById('marketPowerProfile');
  const stage=cv?.parentElement;
  if(!cv || !stage || !_marketCandleSeries || !R?.strikes?.length) return;
  const W=stage.clientWidth,H=stage.clientHeight,dpr=window.devicePixelRatio||1;
  cv.width=Math.max(1,W*dpr);cv.height=Math.max(1,H*dpr);cv.style.width=W+'px';cv.style.height=H+'px';
  const ctx=cv.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,W,H);
  const rows=visibleStrikeData(R).map(s=>({
    strike:Number(s.strike),power:Number(s.powerZone)||0,
    y:_marketCandleSeries.priceToCoordinate(Number(s.strike)),
  })).filter(p=>Number.isFinite(p.y) && p.y>=0 && p.y<=H);
  if(rows.length<2) return;
  const maxPower=Math.max(...rows.map(p=>p.power),1);
  const baseX=18,maxWidth=Math.min(230,Math.max(105,W*.18));
  ctx.beginPath();ctx.moveTo(baseX,rows[0].y);
  rows.forEach(p=>ctx.lineTo(baseX+(p.power/maxPower)*maxWidth,p.y));
  ctx.lineTo(baseX,rows[rows.length-1].y);ctx.closePath();
  const gradient=ctx.createLinearGradient(baseX,0,baseX+maxWidth,0);
  gradient.addColorStop(0,'rgba(255,230,0,.30)');gradient.addColorStop(1,'rgba(255,230,0,.08)');
  ctx.fillStyle=gradient;ctx.fill();
  ctx.beginPath();rows.forEach((p,i)=>{const x=baseX+(p.power/maxPower)*maxWidth;i?ctx.lineTo(x,p.y):ctx.moveTo(x,p.y);});
  ctx.strokeStyle='#ffe600';ctx.lineWidth=1.7;ctx.stroke();
  ctx.fillStyle='rgba(255,230,0,.92)';ctx.font='800 10px Segoe UI';ctx.textAlign='left';ctx.fillText('POWER PROFILE',baseX+4,18);
}
function renderMarketLevelLegend(levels){
  const host=document.getElementById('marketLevelLegend');
  if(!host) return;
  host.innerHTML=levels.map(level=>`<span><i style="background:${level.color}"></i>${esc(level.label)} ${fmtPrice(level.price)}</span>`).join('');
}
function drawMarketPriceCanvas(R,candles,meta={}){
  const cv=document.getElementById('marketPriceCanvas');
  if(!cv || !candles.length) return;
  const stage=cv.parentElement,W=stage.clientWidth,H=stage.clientHeight,dpr=window.devicePixelRatio||1;
  cv.width=Math.max(1,W*dpr);cv.height=Math.max(1,H*dpr);cv.style.width=W+'px';cv.style.height=H+'px';
  const ctx=cv.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,W,H);
  const mobile=W<700,padL=mobile?48:68,padR=mobile?24:48,padT=28,padB=45;
  const plotW=W-padL-padR,plotH=H-padT-padB;
  const levels=marketStructureLevels(R);
  const profile=marketStructureRows(R).map(s=>({strike:Number(s.strike),power:Number(s.powerZone)||0}));
  const prices=[...candles.flatMap(c=>[c.low,c.high]),...levels.map(l=>l.price),...profile.map(p=>p.strike)].filter(Number.isFinite);
  let minPrice=Math.min(...prices),maxPrice=Math.max(...prices);
  const pricePad=Math.max((maxPrice-minPrice)*.045,R.spot*.0015,1);minPrice-=pricePad;maxPrice+=pricePad;
  const y=price=>padT+(maxPrice-price)/(maxPrice-minPrice)*plotH;
  const tMin=candles[0].time,tMax=candles[candles.length-1].time||tMin+1;
  const x=time=>padL+(time-tMin)/Math.max(1,tMax-tMin)*plotW;
  ctx.fillStyle='#191919';ctx.fillRect(0,0,W,H);
  ctx.font=(mobile?'700 9px':'700 10px')+' Segoe UI';ctx.textBaseline='middle';
  for(let i=0;i<=6;i++){
    const yy=padT+plotH*i/6,price=maxPrice-(maxPrice-minPrice)*i/6;
    ctx.strokeStyle='rgba(255,255,255,.075)';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(padL,yy);ctx.lineTo(W-padR,yy);ctx.stroke();
    ctx.fillStyle='#c8cdd1';ctx.textAlign='right';ctx.fillText(fmtPrice(price),padL-7,yy);
  }
  for(let i=0;i<=7;i++){
    const time=tMin+(tMax-tMin)*i/7,xx=x(time);
    ctx.strokeStyle='rgba(255,255,255,.05)';ctx.beginPath();ctx.moveTo(xx,padT);ctx.lineTo(xx,H-padB);ctx.stroke();
    const label=new Date(time*1000).toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',hour12:false});
    ctx.fillStyle='#b9c0c5';ctx.textAlign='center';ctx.textBaseline='top';ctx.fillText(label,xx,H-padB+9);
  }
  const maxPower=Math.max(...profile.map(p=>p.power),1),profileWidth=Math.min(250,Math.max(100,plotW*.19));
  const profilePoints=profile.map(p=>({x:padL+(p.power/maxPower)*profileWidth,y:y(p.strike)})).filter(p=>p.y>=padT&&p.y<=H-padB);
  if(profilePoints.length>1){
    ctx.beginPath();ctx.moveTo(padL,profilePoints[0].y);profilePoints.forEach(p=>ctx.lineTo(p.x,p.y));ctx.lineTo(padL,profilePoints[profilePoints.length-1].y);ctx.closePath();
    const gradient=ctx.createLinearGradient(padL,0,padL+profileWidth,0);gradient.addColorStop(0,'rgba(255,230,0,.31)');gradient.addColorStop(1,'rgba(255,230,0,.07)');
    ctx.fillStyle=gradient;ctx.fill();ctx.beginPath();profilePoints.forEach((p,i)=>i?ctx.lineTo(p.x,p.y):ctx.moveTo(p.x,p.y));ctx.strokeStyle='#ffe600';ctx.lineWidth=1.8;ctx.stroke();
  }
  levels.forEach(level=>{
    const yy=y(level.price);if(yy<padT||yy>H-padB)return;
    ctx.save();ctx.strokeStyle=level.color;ctx.lineWidth=level.width;ctx.setLineDash(level.dashed?[8,5]:[]);ctx.beginPath();ctx.moveTo(padL,yy);ctx.lineTo(W-padR,yy);ctx.stroke();ctx.restore();
  });
  const candleSlot=plotW/Math.max(candles.length,1),bodyW=Math.max(2,Math.min(9,candleSlot*.62));
  candles.forEach(c=>{
    const xx=x(c.time),color=c.close>=c.open?'#26a69a':'#ef5350';ctx.strokeStyle=color;ctx.lineWidth=1;
    ctx.beginPath();ctx.moveTo(xx,y(c.high));ctx.lineTo(xx,y(c.low));ctx.stroke();
    const top=Math.min(y(c.open),y(c.close)),height=Math.max(1.5,Math.abs(y(c.close)-y(c.open)));ctx.fillStyle=color;ctx.fillRect(xx-bodyW/2,top,bodyW,height);
  });
  const last=candles[candles.length-1],lastY=y(last.close);ctx.save();ctx.strokeStyle='rgba(255,255,255,.55)';ctx.setLineDash([3,4]);ctx.beginPath();ctx.moveTo(padL,lastY);ctx.lineTo(W-padR,lastY);ctx.stroke();ctx.restore();
  ctx.fillStyle='#ffe600';ctx.font='900 10px Segoe UI';ctx.textAlign='left';ctx.textBaseline='top';ctx.fillText('POWER ZONE',padL+5,padT+5);
  ctx.fillStyle='#d9dde0';ctx.textAlign='center';ctx.fillText('Time',padL+plotW/2,H-13);ctx.save();ctx.translate(13,padT+plotH/2);ctx.rotate(-Math.PI/2);ctx.fillText('Price',0,0);ctx.restore();
  _marketCanvasHit={cv,candles,padL,padR,padT,padB,plotW,plotH,symbol:R.symbol,meta};
}
function showMarketCanvasTooltip(ev){
  const h=_marketCanvasHit,tt=document.getElementById('marketPriceTooltip');if(!h||!tt)return;
  const rect=h.cv.getBoundingClientRect(),mx=(ev.touches?.[0]?.clientX??ev.clientX)-rect.left,my=(ev.touches?.[0]?.clientY??ev.clientY)-rect.top;
  if(mx<h.padL||mx>rect.width-h.padR||my<h.padT||my>rect.height-h.padB){tt.style.display='none';return;}
  const index=Math.max(0,Math.min(h.candles.length-1,Math.round((mx-h.padL)/h.plotW*(h.candles.length-1)))),c=h.candles[index];
  const time=new Date(c.time*1000).toLocaleString('en-US',{timeZone:'America/New_York',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
  tt.innerHTML=`<strong>${esc(h.symbol)} Â· ${time}</strong><span>O ${fmtPrice(c.open)}</span><span>H ${fmtPrice(c.high)}</span><span>L ${fmtPrice(c.low)}</span><span>C ${fmtPrice(c.close)}</span>`;
  tt.style.display='flex';tt.style.left=Math.max(8,Math.min(mx+14,rect.width-tt.offsetWidth-8))+'px';tt.style.top=Math.max(8,Math.min(my+14,rect.height-tt.offsetHeight-8))+'px';
}
function hideMarketCanvasTooltip(){const tt=document.getElementById('marketPriceTooltip');if(tt)tt.style.display='none';}
function renderMarketPriceChart(R){
  const symbol=String(R.symbol||'SPY').toUpperCase();
  _marketPriceSymbol=symbol;
  document.getElementById('marketPriceTitle').textContent=`${symbol} Price + Options Power Profile`;
  const levels=marketStructureLevels(R);
  renderMarketLevelLegend(levels);
  const cached=_marketCandlesBySymbol.get(symbol);
  const needsLoad=!cached || Date.now()-cached.loadedAt>60000;
  const draw=(candles,meta={})=>{
    if(!candles.length){
      document.getElementById('marketPriceNote').textContent=`No ${symbol} candles were returned. GEX and options levels are still live.`;
      setMarketStructureStatus('Options live Â· candles unavailable','warning');
      return;
    }
    drawMarketPriceCanvas(R,candles,meta);
    document.getElementById('marketPriceNote').textContent=`${symbol} 1-minute OHLC Â· ${meta.source||'market feed'}${meta.asof?' Â· as of '+new Date(Number(meta.asof)*1000).toLocaleString('en-US'):''}. Yellow profile and colored levels use the selected option expirations.`;
    setMarketStructureStatus(`${symbol} Â· ${candles.length} candles Â· ${levels.length} levels`,'ready');
  };
  if(!needsLoad){draw(cached.candles,cached);return;}
  setMarketStructureStatus(`Loading ${symbol} candlesâ€¦`);
  document.getElementById('marketPriceNote').textContent=`Loading ${symbol} intraday candlesâ€¦`;
  fetchMarketStructureCandles(symbol).then(feed=>{
    const raw=feed.raw || [];
    const byTime=new Map();
    raw.map(c=>({time:Number(c.time),open:Number(c.open),high:Number(c.high),low:Number(c.low),close:Number(c.close)}))
      .filter(c=>Number.isFinite(c.time)&&Number.isFinite(c.open)&&Number.isFinite(c.high)&&Number.isFinite(c.low)&&Number.isFinite(c.close))
      .forEach(c=>byTime.set(c.time,c));
    const candles=[...byTime.values()].sort((a,b)=>a.time-b.time);
    _marketCandlesBySymbol.set(symbol,{candles,loadedAt:Date.now(),source:feed.source,asof:feed.asof});
    draw(candles,feed);
  }).catch(err=>{
    document.getElementById('marketPriceNote').textContent=`Could not load ${symbol} candles: ${err?.message || 'request failed'}.`;
    setMarketStructureStatus('Candle feed unavailable','error');
  });
}
function renderMarketStructure(R){
  drawChart(R,'marketGexChart');
  renderMarketPriceChart(R);
}
function renderMarketRead(R){
  const host=document.getElementById('marketReadCard');
  if(!host) return;
  const scenarios=R.dealerScenarios || [];
  if(!scenarios.length){ host.innerHTML=''; return; }
  const spot=R.spot;
  const now=nearestScenario(scenarios,s=>Math.abs(s.delta)<=scenarioStepForSymbol(R.symbol)/2,spot) || nearestScenario(scenarios,()=>true,spot);
  const below=nearestScenario(scenarios,s=>s.spot<spot && (s.cls==='accel-down' || s.cls==='support'),spot);
  const above=nearestScenario(scenarios,s=>s.spot>spot && (s.cls==='accel-up' || s.cls==='resistance'),spot);
  const resistance=nearestScenario(scenarios,s=>s.spot>spot && s.cls==='resistance',spot) || above;
  let bias='Chop / Mixed';
  if(below?.cls==='accel-down' && above?.cls==='accel-up') bias='Fragile two-way acceleration';
  else if(above?.cls==='accel-up') bias='Upside squeeze risk';
  else if(below?.cls==='accel-down') bias='Downside air pocket';
  else if(now?.cls==='pin') bias='Pin / balance';
  const spySpot=spyPriceFromSpxStrike(spot,R);
  const belowSpy=below ? spyPriceFromSpxStrike(below.spot,R) : null;
  const aboveSpy=above ? spyPriceFromSpxStrike(above.spot,R) : null;
  const readTitle=now?.cls==='pin' ? 'Price is sitting near the pin zone' : bias;
  const readSub=[
    above && aboveSpy!=null ? `Above ${fmtSpyConvertedPrice(aboveSpy)} SPY: ${above.label}` : null,
    below && belowSpy!=null ? `Below ${fmtSpyConvertedPrice(belowSpy)} SPY: ${below.label}` : null,
  ].filter(Boolean).join(' | ');
  const line=(cls,label,item,fallback)=>`
    <div class="market-read-line ${cls}">
      <span><em>${label}</em><strong>${item ? fmtPrice(item.spot) : '--'}</strong></span>
      <b>${item ? `${item.label} Â· ${item.pressure}` : fallback}</b>
    </div>`;
  host.innerHTML=`
    <div class="market-read-title">
      <h3>Market Read</h3>
      <div class="market-read-bias">${bias}</div>
    </div>
    <div class="market-read-now">
      <span class="k">Current Read ${spySpot!=null ? `Â· SPY ${fmtSpyConvertedPrice(spySpot)}` : ''}</span>
      <span class="v">${readTitle}</span>
      <span class="s">${readSub || 'Waiting for the next nearby pressure trigger.'}</span>
    </div>
    ${line('below','Watch Below',below,'No nearby downside trigger')}
    ${line('now','Now',now,'Waiting for scenario data')}
    ${line('above','Watch Above',above,'No nearby upside trigger')}
    ${line('resistance','Next Brake',resistance,'No resistance scenario nearby')}
    <div class="market-read-note">SPY candles are shown with SPX levels converted to SPY. The model zones are derived from options pressure and update with your selected expirations.</div>
  `;
}
function drawEdge(R){
  const cv=document.getElementById('edgeChart');
  if(!cv) return;
  renderDealerScenarios(R);
  renderDealerFlowMap(R);
  renderMarketRead(R);
  renderMatrixPriceChart(R);
  const data=buildDealerPressureData(R);
  if(!data.length) return;
  const dpr=window.devicePixelRatio||1;
  const W=cv.clientWidth;
  const mobile=W<760;
  const H=mobile?760:Math.max(520,Math.min(650,window.innerHeight-185));
  cv.width=W*dpr; cv.height=H*dpr; cv.style.height=H+'px';
  const ctx=cv.getContext('2d');
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle='#202020'; ctx.fillRect(0,0,W,H);

  const padL=mobile?54:66, padR=mobile?18:28, padT=mobile?82:90, padB=mobile?86:92;
  const plotW=W-padL-padR, plotH=H-padT-padB;
  const slot=plotW/data.length;
  const xCenter=i=>padL+slot*(i+.5);
  const y=v=>padT+(1-v)/2*plotH;
  const zero=y(0);

  ctx.fillStyle='#f0f0f0';
  ctx.font=(mobile?'900 16px':'900 19px')+' Segoe UI';
  ctx.textAlign='left'; ctx.textBaseline='top';
  ctx.fillText('Dealer Pressure Map',padL,18);
  ctx.fillStyle='#aeb3b8';
  ctx.font=(mobile?'700 10px':'700 12px')+' Segoe UI';
  ctx.fillText('Combined score: GEX 62% + Vanna 23% + Charm 15% | zones are directional pressure, not reversal promises.',padL,44);
  const legend=[
    ['Pin','#ffc107'],['Support','#17b65f'],['Resistance','#ff7e32'],['Acceleration +','#22aaf2'],['Acceleration -','#ff2a17']
  ];
  let lx=W-padR-520;
  if(mobile) lx=padL;
  legend.forEach(([label,color],i)=>{
    const x=lx+i*(mobile?92:102), y0=mobile?62:24;
    ctx.fillStyle=color; ctx.fillRect(x,y0,10,10);
    ctx.fillStyle='#cfd4d8'; ctx.font='800 10px Segoe UI'; ctx.textAlign='left'; ctx.fillText(label,x+15,y0-2);
  });

  for(let t=0;t<=4;t++){
    const yy=padT+t*plotH/4;
    const value=1-(t/4)*2;
    ctx.strokeStyle=t===2?'rgba(255,255,255,.18)':'rgba(255,255,255,.045)';
    ctx.lineWidth=t===2?1.3:1;
    ctx.beginPath(); ctx.moveTo(padL,yy); ctx.lineTo(W-padR,yy); ctx.stroke();
    ctx.fillStyle='#aeb3b8'; ctx.font='bold 10px Segoe UI'; ctx.textAlign='right'; ctx.textBaseline='middle';
    ctx.fillText(value.toFixed(1),padL-8,yy);
  }

  const bw=Math.max(2,Math.min(slot*.72,24));
  data.forEach((row,i)=>{
    const yy=y(row.pressure);
    const alpha=.42+Math.min(.55,Math.abs(row.pressure)*.55+row.ag*.18);
    if(row.zone==='Pin'){
      ctx.fillStyle='rgba(255,193,7,.10)';
      ctx.fillRect(xCenter(i)-slot/2,padT,slot,plotH);
    }
    ctx.fillStyle=pressureColor(row,alpha);
    ctx.fillRect(xCenter(i)-bw/2,Math.min(yy,zero),bw,Math.max(2,Math.abs(yy-zero)));
    if(row.zone==='Acceleration'){
      ctx.strokeStyle=pressureColor(row,.95);
      ctx.lineWidth=2;
      ctx.beginPath(); ctx.moveTo(xCenter(i),padT+4); ctx.lineTo(xCenter(i),padT+18); ctx.stroke();
    }
  });

  const xAt=price=>{
    if(price<=data[0].strike) return padL;
    if(price>=data[data.length-1].strike) return W-padR;
    for(let i=0;i<data.length-1;i++){
      if(price>=data[i].strike&&price<=data[i+1].strike){
        return xCenter(i)+(price-data[i].strike)/(data[i+1].strike-data[i].strike)*slot;
      }
    }
    return padL+plotW/2;
  };
  const px=xAt(R.spot);
  ctx.strokeStyle='#d8921f'; ctx.lineWidth=2.5;
  ctx.beginPath(); ctx.moveTo(px,padT-8); ctx.lineTo(px,padT+plotH); ctx.stroke();
  ctx.fillStyle='#ffb12b'; ctx.font='900 11px Segoe UI'; ctx.textAlign=px>W-135?'right':'left'; ctx.textBaseline='bottom';
  ctx.fillText('Price '+fmtPrice(R.spot),px+(px>W-135?-6:6),padT-10);

  const labelStep=Math.max(1,Math.ceil(data.length/(mobile?18:30)));
  ctx.fillStyle='#aeb3b8'; ctx.font='bold 9px Segoe UI';
  data.forEach((row,i)=>{
    if(i%labelStep&&i!==data.length-1) return;
    ctx.save(); ctx.translate(xCenter(i),padT+plotH+12); ctx.rotate(-Math.PI/2);
    ctx.textAlign='right'; ctx.textBaseline='middle'; ctx.fillText(fmtPrice(row.strike),0,0); ctx.restore();
  });
  ctx.fillStyle='#aeb3b8'; ctx.font='bold 11px Segoe UI'; ctx.textAlign='center';
  ctx.fillText('Strike',padL+plotW/2,H-12);
  ctx.save(); ctx.translate(16,padT+plotH/2); ctx.rotate(-Math.PI/2); ctx.fillText('Dealer Pressure Score',0,0); ctx.restore();

  const strongest=[...data].sort((a,b)=>Math.abs(b.pressure)-Math.abs(a.pressure)).slice(0,4);
  ctx.fillStyle='rgba(20,20,20,.72)';
  ctx.fillRect(W-padR-260,padT+12,238,92);
  ctx.strokeStyle='rgba(255,255,255,.12)'; ctx.strokeRect(W-padR-260+.5,padT+12.5,237,91);
  ctx.fillStyle='#fff'; ctx.font='900 12px Segoe UI'; ctx.textAlign='left'; ctx.fillText('Active Pressure Nodes',W-padR-248,padT+24);
  strongest.forEach((row,i)=>{
    ctx.fillStyle=pressureColor(row,.95);
    ctx.fillText(`${fmtPrice(row.strike)}  ${row.zone}`,W-padR-248,padT+44+i*15);
    ctx.fillStyle='#aeb3b8'; ctx.textAlign='right'; ctx.fillText(row.pressure.toFixed(2),W-padR-34,padT+44+i*15); ctx.textAlign='left';
  });

  cv._edgePanels=[{x:padL,y:padT,w:plotW,h:plotH,plotLeft:padL,plotRight:W-padR,top:padT,bottom:padT+plotH,slot,data}];
}
function showEdgeTooltip(ev){
  const cv=document.getElementById('edgeChart');
  const tt=document.getElementById('edgeTooltip');
  const panels=cv?cv._edgePanels||[]:[];
  if(!panels.length) return;
  const rect=cv.getBoundingClientRect();
  const point=ev.touches&&ev.touches[0]?ev.touches[0]:ev;
  const x=point.clientX-rect.left, y=point.clientY-rect.top;
  const panel=panels.find(p=>x>=p.x&&x<=p.x+p.w&&y>=p.y&&y<=p.y+p.h);
  if(!panel||x<panel.plotLeft||x>panel.plotRight){ hideEdgeTooltip(); return; }
  const index=Math.max(0,Math.min(panel.data.length-1,Math.floor((x-panel.plotLeft)/panel.slot)));
  const s=panel.data[index];
  const valueRow=(label,value,color)=>`<div class="tt-row"><span>${label}</span><span style="color:${color};font-weight:900">${fmtNum(value)}</span></div>`;
  tt.innerHTML=`<div class="tt-title">${window._lastR?.symbol||''} Strike ${fmtPrice(s.strike)} Â· ${s.zone}</div>
    <div class="tt-row"><span>Pressure Score</span><span style="color:${pressureColor(s,.95)};font-weight:900">${s.pressure.toFixed(2)}</span></div>
    <div class="tt-row"><span>Pressure Slope</span><span>${s.slope.toFixed(2)}</span></div>
    <div class="tt-section">Drivers</div>
    <div class="tt-row"><span>GEX weight</span><span>${s.gexN.toFixed(2)}</span></div>
    <div class="tt-row"><span>Vanna weight</span><span>${s.vannaN.toFixed(2)}</span></div>
    <div class="tt-row"><span>Charm weight</span><span>${s.charmN.toFixed(2)}</span></div>
    <div class="tt-section">Raw Exposure</div>
    ${valueRow('Net GEX',s.netGex,s.netGex>=0?'#22aaf2':'#ff2a17')}
    ${valueRow('Net Vanna',s.netVex,s.netVex>=0?'#22aaf2':'#ff2a17')}
    ${valueRow('Net Charm',s.netCharm,s.netCharm>=0?'#22aaf2':'#ff2a17')}`;
  tt.style.display='block';
  panels.forEach((targetPanel,i)=>{
    const cross=document.getElementById(`edgeCrosshair${i}`);
    if(!cross) return;
    const targetX=targetPanel.plotLeft+targetPanel.slot*(index+.5);
    cross.style.display='block';
    cross.style.left=targetX+'px';
    cross.style.top=targetPanel.top+'px';
    cross.style.bottom='auto';
    cross.style.height=(targetPanel.bottom-targetPanel.top)+'px';
  });
  let left=x+14, top=y+14;
  if(left+tt.offsetWidth>rect.width) left=x-tt.offsetWidth-14;
  if(top+tt.offsetHeight>rect.height) top=y-tt.offsetHeight-14;
  tt.style.left=Math.max(6,left)+'px'; tt.style.top=Math.max(6,top)+'px';
}
function hideEdgeTooltip(){
  const tt=document.getElementById('edgeTooltip');
  if(tt) tt.style.display='none';
  document.querySelectorAll('.edge-crosshair').forEach(cross=>cross.style.display='none');
}

// ---------- Wiring ----------
let activeView = 'gex';
const selectedExpirationsByView = {gex:null,dex:null,vex:null,chex:null,'market-structure':null,'exposure-lab':null,'matrix-gex':null,'shock-engine':null,'max-pain':null};
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
  const mkt=document.getElementById('market').value;
  sel.innerHTML="";
  Object.keys(SYMBOLS).filter(k=>SYMBOLS[k].market===mkt).forEach(k=>{
    const o=document.createElement('option');o.value=k;o.textContent=k;sel.appendChild(o);
  });
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
  if(activeView==='net-flow') loadFlowData(true);
}
function setView(view){
  if(view === activeView) return;
  saveCurrentExpirationSelection();
  activeView = view;
  if(view==='net-flow' && !['SPY','QQQ'].includes(document.getElementById('symbol').value)){
    document.getElementById('symbol').value='SPY';
  }
  document.querySelectorAll('.side-nav a[data-view]').forEach(a=>a.classList.toggle('active',a.dataset.view===view));
  document.querySelectorAll('.app-view').forEach(section=>section.classList.toggle('active',section.id===`view-${view}`));
  hideAllHoverHelpers();
  run();
  if(view === 'gex' && window._lastR) requestAnimationFrame(()=>{
    drawChart(window._lastR);
  });
  if(view === 'dex' && window._lastR) requestAnimationFrame(()=>drawChart(window._lastR,'dexChart'));
  if(view === 'vex' && window._lastR) requestAnimationFrame(()=>{ drawChart(window._lastR,'vexChart'); renderGreekExposureKpis(window._lastR,'vex'); });
  if(view === 'chex' && window._lastR) requestAnimationFrame(()=>{ drawChart(window._lastR,'chexChart'); renderGreekExposureKpis(window._lastR,'chex'); });
  if(view === 'market-structure' && window._lastR) requestAnimationFrame(()=>renderMarketStructure(window._lastR));
  if(view === 'exposure-lab' && window._lastR) requestAnimationFrame(()=>drawExposureLab(window._lastR));
  if(view === 'matrix-gex' && window._lastR) requestAnimationFrame(()=>drawChart(window._lastR,'matrixGexChart'));
  if(view === 'shock-engine' && window._lastR) requestAnimationFrame(()=>drawShockEngine(window._lastR));
  if(view === 'net-flow') requestAnimationFrame(()=>loadFlowData());
  if(view === 'max-pain' && window._lastR) requestAnimationFrame(()=>drawMaxPain(window._lastR));
  if(view === 'review') renderReviewPage();
  window.scrollTo({top:0,left:0});
}
function run(){
  const sym=document.getElementById('symbol').value;
  const mode=document.getElementById('mode').value;
  const src=document.getElementById('source').value;
  const ov=parseFloat(document.getElementById('spotOverride').value);
  const useLive = src==="live" && REAL[sym];
  let chain;
  if(useLive){
    chain = buildChainReal(sym, null);
  } else {
    const spot=!isNaN(ov)&&ov>0?ov:SYMBOLS[sym].spot;
    chain = buildChain(sym,spot);
  }
  populateExpirations(chain);
  const selectedExpirationValues = [...document.querySelectorAll('#expirationPicker input:checked')].map(input=>input.value);
  const selectedExpirations = new Set(selectedExpirationValues);
  const fullChain = chain;
  if(selectedExpirations.size){
    chain = {...chain, quotes: chain.quotes.filter(q=>selectedExpirations.has(q.exp))};
  }
  const expectedMove = calcExpectedMove(chain);
  const maxPain = buildMaxPain(fullChain, selectedExpirations);
  const R = calcGEX(chain,mode);
  R.avgPowerZone = buildDayBasedAvgPowerZone(chain,mode);
  R.expectedMove = expectedMove;
  R.maxPain = maxPain;
  R.live = !!chain.live;
  R.asof = useLive ? REAL[sym].asof : null;
  R.marketRead = buildMarketRead(R);
  renderImpl(R);
}
bind('market','change',()=>{
  selectedExpirationsByView.gex=null;
  selectedExpirationsByView.dex=null;
  selectedExpirationsByView.vex=null;
  selectedExpirationsByView.chex=null;
  selectedExpirationsByView['market-structure']=null;
  selectedExpirationsByView['exposure-lab']=null;
  selectedExpirationsByView['matrix-gex']=null;
  selectedExpirationsByView['shock-engine']=null;
  selectedExpirationsByView['max-pain']=null;
  populateSymbols();
  run();
});
bind('symbol','change',()=>{
  document.getElementById('spotOverride').value='';
  document.getElementById('expirationPicker').innerHTML='';
  selectedExpirationsByView.gex=null;
  selectedExpirationsByView.dex=null;
  selectedExpirationsByView.vex=null;
  selectedExpirationsByView.chex=null;
  selectedExpirationsByView['market-structure']=null;
  selectedExpirationsByView['exposure-lab']=null;
  selectedExpirationsByView['matrix-gex']=null;
  selectedExpirationsByView['shock-engine']=null;
  selectedExpirationsByView['max-pain']=null;
  run();
});
bind('expirationPicker','change',()=>{
  saveCurrentExpirationSelection();
  syncExpiryQuickButtons();
  run();
  if(activeView==='net-flow') loadFlowData(true);
});
bind('mode','change',run);
bind('source','change',run);
bind('autorefresh','change',setupAutoRefresh);
bind('strikeCount','change',run);
if(byId('run')) byId('run').textContent = 'Refresh';
bind('run','click',()=>loadData(true));
window.addEventListener('resize',()=>{
  if(!window._lastR) return;
  if(activeView === 'gex'){
    drawChart(window._lastR);
  }
  if(activeView === 'dex') drawChart(window._lastR,'dexChart');
  if(activeView === 'vex'){ drawChart(window._lastR,'vexChart'); renderGreekExposureKpis(window._lastR,'vex'); }
  if(activeView === 'chex'){ drawChart(window._lastR,'chexChart'); renderGreekExposureKpis(window._lastR,'chex'); }
  if(activeView === 'market-structure') renderMarketStructure(window._lastR);
  if(activeView === 'exposure-lab') drawExposureLab(window._lastR);
  if(activeView === 'matrix-gex') drawChart(window._lastR,'matrixGexChart');
  if(activeView === 'shock-engine') drawShockEngine(window._lastR);
  if(activeView === 'max-pain') drawMaxPain(window._lastR);
  if(activeView === 'net-flow') drawFlowWorkspace();
});
bind('gexChart','mousemove',showChartTooltip);
bind('gexChart','mouseleave',hideChartTooltip);
bind('gexChart','touchstart',showChartTooltip,{passive:true});
bind('gexChart','touchmove',showChartTooltip,{passive:true});
bind('gexChart','touchend',()=>setTimeout(hideChartTooltip,1200),{passive:true});
bind('dexChart','mousemove',showChartTooltip);
bind('dexChart','mouseleave',hideChartTooltip);
bind('dexChart','touchstart',showChartTooltip,{passive:true});
bind('dexChart','touchmove',showChartTooltip,{passive:true});
bind('dexChart','touchend',()=>setTimeout(hideChartTooltip,1200),{passive:true});
bind('vexChart','mousemove',showChartTooltip);
bind('vexChart','mouseleave',hideChartTooltip);
bind('vexChart','touchstart',showChartTooltip,{passive:true});
bind('vexChart','touchmove',showChartTooltip,{passive:true});
bind('vexChart','touchend',()=>setTimeout(hideChartTooltip,1200),{passive:true});
bind('chexChart','mousemove',showChartTooltip);
bind('chexChart','mouseleave',hideChartTooltip);
bind('chexChart','touchstart',showChartTooltip,{passive:true});
bind('chexChart','touchmove',showChartTooltip,{passive:true});
bind('chexChart','touchend',()=>setTimeout(hideChartTooltip,1200),{passive:true});
bind('marketGexChart','mousemove',showChartTooltip);
bind('marketGexChart','mouseleave',hideChartTooltip);
bind('marketGexChart','touchstart',showChartTooltip,{passive:true});
bind('marketGexChart','touchmove',showChartTooltip,{passive:true});
bind('marketGexChart','touchend',()=>setTimeout(hideChartTooltip,1200),{passive:true});
bind('marketPriceCanvas','mousemove',showMarketCanvasTooltip);
bind('marketPriceCanvas','mouseleave',hideMarketCanvasTooltip);
bind('marketPriceCanvas','touchstart',showMarketCanvasTooltip,{passive:true});
bind('marketPriceCanvas','touchmove',showMarketCanvasTooltip,{passive:true});
bind('marketPriceCanvas','touchend',()=>setTimeout(hideMarketCanvasTooltip,1200),{passive:true});
bind('matrixGexChart','mousemove',showChartTooltip);
bind('matrixGexChart','mouseleave',hideChartTooltip);
bind('matrixGexChart','touchstart',showChartTooltip,{passive:true});
bind('matrixGexChart','touchmove',showChartTooltip,{passive:true});
bind('matrixGexChart','touchend',()=>setTimeout(hideChartTooltip,1200),{passive:true});
['labGexChart','labDexChart','labMapChart'].forEach(id=>{
  bind(id,'mousemove',showLabTooltip);
  bind(id,'mouseleave',hideLabTooltip);
});
bind('shockEngineChart','mousemove',showShockTooltip);
bind('shockEngineChart','mouseleave',hideShockTooltip);
bind('shockEngineChart','touchstart',showShockTooltip,{passive:true});
bind('shockEngineChart','touchmove',showShockTooltip,{passive:true});
bind('shockEngineChart','touchend',()=>setTimeout(hideShockTooltip,1200),{passive:true});
bind('optionsHeatMap','mousemove',showHeatMapTip);
bind('optionsHeatMap','mouseleave',hideHeatMapTip);
bind('darkPoolLevels','mousemove',showDarkPoolTooltip);
bind('darkPoolLevels','mouseleave',hideDarkPoolTooltip);
bind('netFlowChart','mousemove',showFlowTooltip);
bind('netFlowChart','mouseleave',hideFlowTooltip);
bind('netDriftChart','mousemove',showFlowTooltip);
bind('netDriftChart','mouseleave',hideFlowTooltip);
bind('flowInterval','change',()=>loadFlowData(true));
bind('flowSession','change',()=>loadFlowData(true));
bind('flowStartFresh','click',()=>{
  if(!window.confirm('Start a fresh Flow view from now? Saved server history will remain protected.'))return;
  localStorage.setItem(FLOW_RESET_KEY,String(Date.now()));
  const session=document.getElementById('flowSession');if(session)session.value=fallbackFlowSessionDates()[0];
  loadFlowData(true);
});
bind('maxPainChart','mousemove',showMaxPainTooltip);
bind('maxPainChart','mouseleave',hideMaxPainTooltip);
bind('edgeChart','mousemove',showEdgeTooltip);
bind('edgeChart','mouseleave',hideEdgeTooltip);
bind('edgeChart','touchstart',showEdgeTooltip,{passive:true});
bind('edgeChart','touchmove',showEdgeTooltip,{passive:true});
bind('edgeChart','touchend',()=>setTimeout(hideEdgeTooltip,1200),{passive:true});
bind('saveSnapshot','click',saveMarketSnapshot);
bind('reviewOutcomeFilter','change',renderReviewPage);
bind('clearSnapshots','click',()=>{
  if(!window.confirm('Clear all saved Matrix GEX snapshots?')) return;
  writeMarketSnapshots([]);
  renderReviewPage();
});
bind('reviewSnapshotList','click',ev=>{
  const card=ev.target.closest?.('[data-snapshot-id]');
  if(!card) return;
  const id=card.dataset.snapshotId;
  const outcome=ev.target.dataset?.outcome;
  if(outcome){ setSnapshotOutcome(id,outcome); return; }
  if(ev.target.dataset?.deleteSnapshot){ deleteSnapshot(id); }
});

document.querySelectorAll('.side-nav a[data-view]').forEach(link=>{
  link.addEventListener('click',e=>{
    e.preventDefault();
    setView(link.dataset.view);
  });
});
document.querySelectorAll('.maxpain-tab').forEach(btn=>{
  btn.addEventListener('click',()=>{
    maxPainView = btn.dataset.maxpainView;
    document.querySelectorAll('.maxpain-tab').forEach(b=>b.classList.toggle('active',b===btn));
    hideMaxPainTooltip();
    if(window._lastR) drawMaxPain(window._lastR);
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

// DEX | GEX | VEX | CHEX segmented switcher on the main GEX chart
document.querySelectorAll('[data-gexbar]').forEach(btn=>{
  btn.addEventListener('click',()=>{
    const key=btn.dataset.gexbar;
    if(!METRICS[key]) return;
    GEX_CHART_BAR_METRIC=key;
    document.querySelectorAll('[data-gexbar]').forEach(b=>b.classList.toggle('active',b===btn));
    if(window._lastR && activeView === 'gex') drawChart(window._lastR);
  });
});

function syncSigmaButtons(){
  const value=Number.isFinite(DISPLAY_SIGMA)?String(DISPLAY_SIGMA):'all';
  document.querySelectorAll('[data-sigma]').forEach(btn=>btn.classList.toggle('active',btn.dataset.sigma===value));
  document.querySelectorAll('[data-edge-sigma]').forEach(btn=>btn.classList.toggle('active',btn.dataset.edgeSigma===value));
}
document.querySelectorAll('[data-sigma]').forEach(btn=>{
  btn.addEventListener('click',()=>{
    DISPLAY_SIGMA=btn.dataset.sigma==='all' ? Infinity : Number(btn.dataset.sigma);
    syncSigmaButtons();
    if(window._lastR && activeView === 'gex') drawChart(window._lastR);
    if(window._lastR && activeView === 'dex') drawChart(window._lastR,'dexChart');
    if(window._lastR && activeView === 'vex') drawChart(window._lastR,'vexChart');
    if(window._lastR && activeView === 'chex') drawChart(window._lastR,'chexChart');
    if(window._lastR && activeView === 'market-structure') renderMarketStructure(window._lastR);
  });
});
document.querySelectorAll('[data-edge-sigma]').forEach(btn=>{
  btn.addEventListener('click',()=>{
    DISPLAY_SIGMA=btn.dataset.edgeSigma==='all' ? Infinity : Number(btn.dataset.edgeSigma);
    syncSigmaButtons();
    if(window._lastR && activeView === 'edge') drawEdge(window._lastR);
  });
});
document.querySelectorAll('[data-lab-sigma]').forEach(btn=>{
  btn.addEventListener('click',()=>{
    LAB_SIGMA=btn.dataset.labSigma==='all'?Infinity:Number(btn.dataset.labSigma);
    document.querySelectorAll('[data-lab-sigma]').forEach(b=>b.classList.toggle('active',b===btn));
    hideLabTooltip();
    if(window._lastR&&activeView==='exposure-lab') drawExposureLab(window._lastR);
  });
});

// ---------- Data loading + auto-refresh ----------
// Reloads cboe_data.json and refreshes the display.
let _refreshTimer=null, _timerUiTimer=null, _firstLoad=true, _loadingData=false;
let _lastFileLoadedAt=null, _lastLoadOk=false, _lastTripityRetryAt=0;
let _lastDataSource='Waiting';
let _matrixChart=null, _matrixCandleSeries=null, _matrixPriceLines=[], _matrixCandles=[], _matrixCandlesLoadedAt=0;
let _marketPriceChart=null,_marketCandleSeries=null,_marketPriceLines=[],_marketChartElement=null,_marketResizeObserver=null;
let _marketPriceSymbol='',_marketCandlesBySymbol=new Map(),_marketCanvasHit=null;
const MATRIX_CBOE_DATA_URL = 'https://api.trytripity.site/api/matrix/cboe-data';
const MATRIX_CANDLES_URL = 'https://api.trytripity.site/api/matrix/candles';
const MATRIX_FLOW_URL = 'https://api.trytripity.site/api/matrix/flow';
const TRIPITY_RETRY_MS = 15000;
const MARKET_FRESHNESS_MAX_MS = 75 * 60 * 1000;
const MARKET_FRESHNESS_START_MIN = 9 * 60 + 45;
const MARKET_FRESHNESS_END_MIN = 16 * 60 + 30;
function fetchLiveData(url){
  return fetch(url, {cache:'no-store'})
    .then(r=>r.ok?r.json():null)
    .catch(()=>null);
}
function validCboeData(d){
  return d && d.SPX && Array.isArray(d.SPX.opts) && Number.isFinite(Number(d.SPX.spot));
}
function fetchCboeDataWithSource(url, sourceName){
  return fetchLiveData(url).then(d=>validCboeData(d) ? {data:d, source:sourceName} : null);
}
function getRefreshSeconds(){
  return Math.max(0,+document.getElementById('autorefresh').value || 0);
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
  if(_loadingData) return Promise.resolve();
  _loadingData = true;
  updateDataTimer();
  return fetchCboeDataWithSource(MATRIX_CBOE_DATA_URL + '?t=' + Date.now(), 'Tripity hybrid')
    .then(result=>{
      if(result || !validCboeData(REAL)) return result;
      _lastDataSource = 'Tripity retry';
      return null;
    })
    .then(result=>{
      _lastLoadOk=!!result || validCboeData(REAL);
      if(result){ REAL=result.data; _lastDataSource=result.source; _lastFileLoadedAt=Date.now(); }
    })
    .finally(()=>{
      if(_firstLoad){
        _firstLoad=false;
        if(Object.keys(REAL).length===0){
          document.getElementById('source').value="synthetic";
          document.querySelector('#source option[value=live]').textContent="LIVE - unavailable (open through a server)";
        }
      }
      _loadingData = false;
      if(thenRun!==false) run();
      updateDataTimer();
      if(_lastDataSource==='Tripity retry' && Date.now()-_lastTripityRetryAt>=TRIPITY_RETRY_MS){
        _lastTripityRetryAt=Date.now();
        setTimeout(()=>loadData(true), TRIPITY_RETRY_MS);
      }
    });
}
function setupAutoRefresh(){
  if(_refreshTimer){ clearInterval(_refreshTimer); _refreshTimer=null; }
  const sec=getRefreshSeconds();
  if(sec>0){ _refreshTimer=setInterval(()=>loadData(true), sec*1000); }
  updateDataTimer();
}

// ---------- Live spot poller (5s) + GEX replay ----------
// Spot ticks ride the existing WebSocket collector via /api/matrix/spots, so
// GEX/DEX levels can move live without extra upstream API usage.
const MATRIX_SPOTS_URL = 'https://api.trytripity.site/api/matrix/spots';
const MATRIX_GEX_HISTORY_URL = 'https://api.trytripity.site/api/matrix/gex-history';
let _spotsTimer = null;

async function pollLiveSpots(){
  if(document.hidden || _loadingData || GEX_REPLAY.active) return;
  const payload = await fetchLiveData(`${MATRIX_SPOTS_URL}?t=${Date.now()}`);
  if(!payload || !payload.ok || !payload.symbols) return;
  const currentSymbol = document.getElementById('symbol')?.value || DEFAULT_SYMBOL;
  let currentChanged = false;
  for(const [sym, entry] of Object.entries(payload.symbols)){
    const rec = REAL[sym];
    if(!rec || !entry) continue;
    const spot = Number(entry.spot);
    if(!Number.isFinite(spot) || spot <= 0) continue;
    const tolerance = Math.max(0.01, spot * 1e-5);  // ignore sub-tick jitter
    if(!Number.isFinite(Number(rec.spot)) || Math.abs(spot - Number(rec.spot)) > tolerance){
      rec.spot = spot;
      if(entry.asof) rec.price_asof = entry.asof;
      if(sym === currentSymbol) currentChanged = true;
    }
  }
  if(currentChanged) run();
}

function setupSpotsPoller(){
  if(_spotsTimer) clearInterval(_spotsTimer);
  _spotsTimer = setInterval(pollLiveSpots, 5000);
}

// GEX replay: browse per-minute snapshots of prior completed sessions,
// mirroring the Net Drift & Flow session navigation.
let GEX_REPLAY = {active:false, session:'', points:[], index:0, loading:false};

function gexReplaySessionsFallback(){
  return fallbackFlowSessionDates().slice(1);  // prior sessions, not today
}

function populateGexReplaySessions(dates, preferred=''){
  const select = document.getElementById('gexReplaySession');
  if(!select) return '';
  const clean = [...new Set((dates||[]).filter(d=>/^\d{4}-\d{2}-\d{2}$/.test(String(d))))];
  select.innerHTML = '<option value="">Live</option>' +
    clean.map(d=>`<option value="${d}">Replay ${d}</option>`).join('');
  select.value = clean.includes(preferred) ? preferred : '';
  return select.value;
}

function fmtReplayLevel(value){
  return Number.isFinite(Number(value)) ? fmtPrice(Number(value)) : '--';
}

async function loadGexReplay(sessionDate){
  const symbol = document.getElementById('symbol').value;
  const banner = document.getElementById('gexReplayBanner');
  GEX_REPLAY.loading = true;
  try{
    const params = new URLSearchParams({symbol, session_date: sessionDate, t: String(Date.now())});
    const response = await fetch(`${MATRIX_GEX_HISTORY_URL}?${params}`, {cache:'no-store'});
    const payload = await response.json();
    if(!response.ok || !payload.ok) throw new Error(payload.detail || `GEX history failed (${response.status})`);
    if(payload.available_sessions) populateGexReplaySessions(payload.available_sessions, sessionDate);
    GEX_REPLAY.active = true;
    GEX_REPLAY.session = sessionDate;
    GEX_REPLAY.points = Array.isArray(payload.points) ? payload.points : [];
    GEX_REPLAY.index = GEX_REPLAY.points.length ? GEX_REPLAY.points.length - 1 : 0;
    const slider = document.getElementById('gexReplaySlider');
    if(slider){
      slider.max = Math.max(0, GEX_REPLAY.points.length - 1);
      slider.value = GEX_REPLAY.index;
      slider.style.display = GEX_REPLAY.points.length ? '' : 'none';
    }
    applyGexReplaySnapshot();
  }catch(error){
    if(banner){ banner.hidden = false; banner.textContent = `Replay unavailable: ${error.message}`; }
  }finally{
    GEX_REPLAY.loading = false;
  }
}

function exitGexReplay(){
  GEX_REPLAY = {active:false, session:'', points:[], index:0, loading:false};
  const banner = document.getElementById('gexReplayBanner');
  if(banner) banner.hidden = true;
  const slider = document.getElementById('gexReplaySlider');
  if(slider) slider.style.display = 'none';
  loadData(true);  // restore the live spot before returning to live mode
}

function applyGexReplaySnapshot(){
  const banner = document.getElementById('gexReplayBanner');
  const point = GEX_REPLAY.points[GEX_REPLAY.index];
  if(!point){
    if(banner){ banner.hidden = false; banner.textContent = `Replay ${GEX_REPLAY.session}: no snapshots captured for this session.`; }
    return;
  }
  const timeLabel = flowTimeLabel(Number(point.time));
  if(banner){
    banner.hidden = false;
    banner.textContent = `Replay: ${GEX_REPLAY.session} ${timeLabel} ET · Spot ${fmtReplayLevel(point.spot)} · Total GEX ${fmtNum(Number(point.totalGex)||0)} · Total DEX ${fmtNum(Number(point.totalDex)||0)} · Flip ${fmtReplayLevel(point.flip)} · Call Wall ${fmtReplayLevel(point.callWall)} · Put Wall ${fmtReplayLevel(point.putWall)} · ${point.regime || ''}`;
  }
  const symbol = document.getElementById('symbol').value;
  if(REAL[symbol] && Number.isFinite(Number(point.spot))){
    REAL[symbol] = {...REAL[symbol], spot: Number(point.spot)};
  }
  run();
}

bind('gexReplaySession', 'change', event=>{
  const value = event.target.value;
  if(!value) exitGexReplay();
  else loadGexReplay(value);
});
bind('gexReplaySlider', 'input', event=>{
  GEX_REPLAY.index = Number(event.target.value) || 0;
  applyGexReplaySnapshot();
});
bind('symbol', 'change', ()=>{
  if(GEX_REPLAY.active && GEX_REPLAY.session) loadGexReplay(GEX_REPLAY.session);
});

populateSymbols();
document.getElementById('symbol').value = DEFAULT_SYMBOL;
document.getElementById('mode').value = 'full';
document.getElementById('source').value = 'live';
document.getElementById('autorefresh').value = '60';
document.getElementById('forceTripityRefresh').addEventListener('click',()=>loadData(true));
_timerUiTimer=setInterval(updateDataTimer,1000);
populateGexReplaySessions(gexReplaySessionsFallback());
setupSpotsPoller();
loadData(true).then(setupAutoRefresh);
