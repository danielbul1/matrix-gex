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
  const isAmSettled=root==='SPX'||root==='NDX';
  const expiryMs=zonedDateTimeToUtc(year,month,day,isAmSettled?9:16,isAmSettled?30:0,'America/New_York');
  return Math.max((expiryMs-valuationMs)/(365.25*86400000),1/(365.25*24*60));
}

// ---------- Symbol universe ----------
const SYMBOLS = {
  NDX:{spot:30570,  step:25, mult:100, baseIV:0.170, market:"US"},
  SPX:{spot:7550,   step:5,  mult:100, baseIV:0.140, market:"US"},
  SPY:{spot:580.50, step:5,  mult:100, baseIV:0.135, market:"US"},
  QQQ:{spot:525.50, step:5,  mult:100, baseIV:0.165, market:"US"},
  IWM:{spot:228.0,  step:2,  mult:100, baseIV:0.200, market:"US"},
  NVDA:{spot:145.80,step:2.5,mult:100, baseIV:0.520, market:"US"},
  AAPL:{spot:228.0, step:5,  mult:100, baseIV:0.260, market:"US"},
  TSLA:{spot:352.0, step:10, mult:100, baseIV:0.620, market:"US"},
  NIFTY:{spot:23450,step:50, mult:50,  baseIV:0.155, market:"IN"},
  BANKNIFTY:{spot:51250,step:100,mult:15,baseIV:0.175,market:"IN"},
  FINNIFTY:{spot:23100,step:50, mult:40, baseIV:0.165,market:"IN"},
};
// Real delayed CBOE data is loaded from cboe_data.json. Empty data falls back to synthetic chains.
let REAL = {};
let REAL_ASOF = {};
let OPEN_REAL = null;
const RISK_FREE = {US:0.05, IN:0.065};
const SPX_SPY_RATIO = 10.03657299922611;
const DEFAULT_SYMBOL = 'SPY';

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

// ---------- Real chain from CBOE delayed quotes ----------
// Uses gamma/delta/IV/OI/volume from CBOE when present.
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
    // CBOE does not always provide gamma; fall back to Black-Scholes when missing.
    const modelGreeks = calcGreeks(spot, o.k, T, o.iv>0?o.iv:0.2, r, isCall);
    let g = {...modelGreeks, gamma:o.g, delta:o.d, iv:o.iv};
    if(!o.g || o.g<=0){
      g = modelGreeks;
    }
    return {K:o.k, dte:o.dte, T, exp:o.exp, root:o.root||'', isCall, iv:o.iv, oi:o.oi, vol:o.vol, ts:now, g};
  });
  return {symbol:symKey, market:"US", spot, mult:rec.mult||100, fetchTs:now, quotes, live:true};
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
        callVex:0, putVex:0, netVex:0, callCharm:0, putCharm:0, netCharm:0,
        callOI:0, putOI:0, callVol:0, putVol:0, callIV:0, putIV:0, gamma:0, ts:q.ts};
    const s = byStrike[q.K];
    const vex = (q.g.vanna || 0) * q.oi * mult * spot * 0.01;
    const charm = (q.g.charm || 0) * q.oi * mult * spot / 252;
    if(q.isCall){
      s.callGex += gex; s.callVex += vex; s.callCharm += charm;
      s.callOI+=q.oi; s.callVol+=q.vol; s.callIV=q.iv;
    } else {
      s.putGex -= gex; s.putVex -= vex; s.putCharm -= charm;
      s.putOI +=q.oi; s.putVol +=q.vol; s.putIV =q.iv;
    }
    s.gamma = Math.max(s.gamma, q.g.gamma);
    s.netGex = s.callGex + s.putGex;
    s.netVex = s.callVex + s.putVex;
    s.netCharm = s.callCharm + s.putCharm;
    s.ts = Math.max(s.ts, q.ts);
  }
  const strikes = Object.values(byStrike).sort((a,b)=>a.strike-b.strike);
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

  return {symbol:chain.symbol, market:chain.market, spot, mult, fetchTs:chain.fetchTs,
    strikes, totalGex, totalCallGex, totalPutGex,
    maxGammaStrike, flip, callWall, putWall, callWallGex:cwG, putWallGex:pwG,
    regime, strength, distPct, pcr, netCallOI, netPutOI, callGexPct,
    avgCallIV, avgPutIV, ivSkew, sentiment, atmGreeks:atmQ?atmQ.g:null,
    keptCount:kept.length, totalCount:chain.quotes.length,
    expiries:[...new Set(kept.map(q=>q.dte))].sort((a,b)=>a-b)};
}
function avg(a){return a.length?a.reduce((x,y)=>x+y,0)/a.length:0;}

const NET_GEX_CHANGE_STORE_KEY = 'matrix_net_gex_open_baselines_v1';
function etDateKeyFromMs(ms){
  const parts=new Intl.DateTimeFormat('en-CA',{
    timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit'
  }).formatToParts(new Date(ms || Date.now()));
  const get=type=>parts.find(part=>part.type===type)?.value;
  return `${get('year')}-${get('month')}-${get('day')}`;
}
function etPartsFromMs(ms){
  const parts=new Intl.DateTimeFormat('en-US',{
    timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit',
    weekday:'short',hour:'2-digit',minute:'2-digit',hourCycle:'h23'
  }).formatToParts(new Date(ms || Date.now()));
  const get=type=>parts.find(part=>part.type===type)?.value;
  return {
    year:Number(get('year')),
    month:Number(get('month')),
    day:Number(get('day')),
    weekday:get('weekday'),
    hour:Number(get('hour')),
    minute:Number(get('minute')),
  };
}
function prevTradingDateKey(year,month,day){
  const d=new Date(Date.UTC(year,month-1,day,12));
  do{ d.setUTCDate(d.getUTCDate()-1); }
  while(d.getUTCDay()===0 || d.getUTCDay()===6);
  return d.toISOString().slice(0,10);
}
function marketSessionDateKey(ms){
  const et=etPartsFromMs(ms);
  const dayKey=etDateKeyFromMs(ms);
  if(et.weekday==='Sat' || et.weekday==='Sun') return prevTradingDateKey(et.year,et.month,et.day);
  const minutes=et.hour*60+et.minute;
  if(minutes < 9*60+30) return prevTradingDateKey(et.year,et.month,et.day);
  return dayKey;
}
function netGexSessionDateKey(R){
  const asofMs=R.asof ? dataAsofToMs(R.asof) : null;
  const sourceMs=asofMs || R.fetchTs || Date.now();
  return marketSessionDateKey(sourceMs);
}
function readNetGexBaselines(){
  try{return JSON.parse(localStorage.getItem(NET_GEX_CHANGE_STORE_KEY) || '{}') || {};}
  catch{return {};}
}
function writeNetGexBaselines(store){
  try{localStorage.setItem(NET_GEX_CHANGE_STORE_KEY, JSON.stringify(store));}
  catch{}
}
function netGexBaselineKey(R,{mode,expirations}){
  const expKey=(expirations || []).slice().sort().join('|') || 'default';
  const dayKey=netGexSessionDateKey(R);
  return [dayKey,R.symbol,mode,expKey].join('::');
}
function buildNetGexChange(R,opts){
  if(opts.baselineResult && Array.isArray(opts.baselineResult.strikes)){
    const byStrike=Object.fromEntries(opts.baselineResult.strikes.map(s=>[String(s.strike),s.netGex || 0]));
    const rows=R.strikes.map(s=>{
      const start=Number(byStrike[String(s.strike)]);
      const baseline=Number.isFinite(start) ? start : 0;
      return {...s, baselineNetGex:baseline, netGexChange:(s.netGex || 0)-baseline};
    });
    const totalChange=rows.reduce((sum,s)=>sum+s.netGexChange,0);
    return {
      key:'open-data',
      source:'open_data',
      sessionDate:OPEN_REAL?.session_date || netGexSessionDateKey(R),
      createdAt:Date.parse(OPEN_REAL?.captured_at || '') || Date.now(),
      asof:OPEN_REAL?.captured_at || null,
      totalChange,
      rows,
    };
  }
  if(opts.requireOpenData){
    const rows=R.strikes.map(s=>({...s, baselineNetGex:null, netGexChange:0}));
    return {
      key:'missing-open-data',
      source:'missing_open_data',
      missingOpenData:true,
      sessionDate:netGexSessionDateKey(R),
      createdAt:null,
      asof:null,
      totalChange:0,
      rows,
    };
  }
  const key=netGexBaselineKey(R,opts);
  const store=readNetGexBaselines();
  let base=store[key];
  if(!base || !base.strikes){
    base={
      symbol:R.symbol,
      mode:opts.mode,
      sessionDate:netGexSessionDateKey(R),
      expirations:(opts.expirations || []).slice().sort(),
      createdAt:Date.now(),
      asof:R.asof || null,
      strikes:Object.fromEntries(R.strikes.map(s=>[String(s.strike),s.netGex || 0])),
    };
    store[key]=base;
    const keys=Object.keys(store).sort((a,b)=>(store[b].createdAt || 0)-(store[a].createdAt || 0));
    keys.slice(60).forEach(oldKey=>delete store[oldKey]);
    writeNetGexBaselines(store);
  }
  const byStrike=base.strikes || {};
  const rows=R.strikes.map(s=>{
    const start=Number(byStrike[String(s.strike)]);
    const baseline=Number.isFinite(start) ? start : 0;
    return {...s, baselineNetGex:baseline, netGexChange:(s.netGex || 0)-baseline};
  });
  const totalChange=rows.reduce((sum,s)=>sum+s.netGexChange,0);
  return {key,sessionDate:base.sessionDate || netGexSessionDateKey(R),createdAt:base.createdAt,asof:base.asof,totalChange,rows};
}

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
  const converted = R?.symbol === 'SPY' ? spxPriceFromSpyPrice(Number(value),R) : spyPriceFromSpxStrike(Number(value),R);
  if(converted == null) return base;
  return R.symbol === 'SPY' ? `${base} / SPX ${fmtPrice(converted)}` : `${base} / SPY ${fmtSpyConvertedPrice(converted)}`;
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
  return {
    id:`snap-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    createdAt:new Date().toISOString(),
    symbol:R.symbol,
    spot:R.spot,
    spxCalc,
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
          <strong>${esc(row.symbol)} ${fmtPrice(row.spot)}${row.spxCalc==null?'':` / SPX ${fmtPrice(row.spxCalc)}`}</strong>
        </div>
        <div class="snapshot-bias ${esc((row.bias||'').toLowerCase().replace(/[^a-z]+/g,'-'))}">${esc(row.bias || '--')} · ${Number(row.confidence||0)}%</div>
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
    const price = calcOptionPrice(chain.spot, q.K, q.T || q.dte/365, q.iv || 0.2, r, q.isCall);
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
      <span>Blue = call premium leading · Red = put premium leading</span>
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
    <div class="heatmap-tip-title">${d.symbol} · Strike ${d.strike}</div>
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
      const price = calcOptionPrice(chain.spot, q.K, q.T || q.dte/365, q.iv || 0.2, r, q.isCall);
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
    const price = calcOptionPrice(chain.spot, q.K, q.T || q.dte/365, q.iv || 0.2, r, q.isCall);
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
    <div class="heatmap-tip-title">${d.symbol} · Dark Pool Level ${d.level}</div>
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
  const spyMaxPain = R?.maxPain?.symbol === 'SPX' ? spyPriceFromSpxStrike(maxPain,R) : null;
  const spxMaxPain = R?.maxPain?.symbol === 'SPY' ? spxPriceFromSpyPrice(maxPain,R) : null;
  host.innerHTML = `
    <div class="maxpain-value" style="--c:#ffc107">
      <span class="k">Max Pain ${esc(R.maxPain.symbol)}</span>
      <span class="v">${fmtPrice(maxPain)}</span>
    </div>
    ${spyMaxPain == null ? '' : `<div class="maxpain-value spy" style="--c:#22b8ff">
      <span class="k">Max Pain SPY</span>
      <span class="v">${fmtSpyConvertedPrice(spyMaxPain)}</span>
    </div>`}
    ${spxMaxPain == null ? '' : `<div class="maxpain-value spy" style="--c:#22b8ff">
      <span class="k">Max Pain SPX calc</span>
      <span class="v">${fmtPrice(spxMaxPain)}</span>
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
              badge.textContent="LIVE - CBOE delayed · data "+(R.asof||"")+" · refreshed "+nowClient; }
  else      { badge.style.color="var(--amber)"; badge.style.borderColor="var(--amber)";
              badge.textContent="DEMO - synthetic data · refreshed "+nowClient; }
  const regClass = R.regime==="positive_gamma"?"reg-pos":R.regime==="negative_gamma"?"reg-neg":"reg-neu";
  const regHeb = {positive_gamma:"Positive Gamma",negative_gamma:"Negative Gamma",neutral:"Neutral"}[R.regime];
  const spxSpot = spxPriceFromSpyPrice(R.spot,R);
  const spxSpotLine = spxSpot == null ? '' : `<div class="k" style="margin-top:6px">SPX calc: ${fmtPrice(spxSpot)}</div>`;
  document.getElementById('topCards').innerHTML = `
    <div class="card"><div class="k">Symbol / Spot</div>
      <div class="v">${R.symbol} <small>${fmtPrice(R.spot)}</small></div>
      ${spxSpotLine}</div>
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
      <div class="k" style="margin-top:6px">${R.keptCount}/${R.totalCount} quotes · DTE: ${R.expiries.join(', ')}</div></div>
  `;

  renderCommandCenter(R);
  renderOptionsHeatMap(R);
  renderDarkPoolLevels(R);
  if(activeView === 'gex'){
    drawChart(R);
    drawNetGexChangeChart(R);
  }
  if(activeView === 'matrix-gex') drawChart(R,'matrixGexChart');
  if(activeView === 'shock-engine') drawShockEngine(R);
  if(activeView === 'net-flow') drawNetFlow(R);
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
  ag:       {label:"AG",          color:"#ab7df6", kind:"area", val:s=>Math.abs(s.callGex)+Math.abs(s.putGex)},
  call_oi:  {label:"Call OI",     color:"#26c281", kind:"area", val:s=>s.callOI},
  put_oi:   {label:"Put OI",      color:"#ef5350", kind:"area", val:s=>s.putOI},
  call_vol: {label:"Call Volume", color:"#2f73ff", kind:"area", val:s=>s.callVol},
  put_vol:  {label:"Put Volume",  color:"#ff8a3d", kind:"area", val:s=>s.putVol},
  power:    {label:"Power Zone",  color:"#fff200", kind:"area", val:s=>s.powerZone || 0},
};
function hexA(hex,a){const n=parseInt(hex.slice(1),16);return `rgba(${n>>16&255},${n>>8&255},${n&255},${a})`;}

// ---------- Canvas chart: vertical bars by strike + area overlays ----------
function chartTargets(chartId){
  const isMatrix = chartId === 'matrixGexChart';
  return {
    canvasId: chartId,
    symbolId: isMatrix ? 'matrixSymLabel' : 'symLabel',
    legendId: isMatrix ? 'matrixChartLegend' : 'chartLegend',
    tooltipId: isMatrix ? 'matrixGexTooltip' : 'chartTooltip',
    crosshairId: isMatrix ? 'matrixGexCrosshairX' : 'chartCrosshairX',
  };
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

  const order = Object.keys(METRICS);
  const active = order.filter(m=>ACTIVE.has(m));
  const hasBar = ACTIVE.has("net_gex");
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
    const vals=data.map(METRICS.net_gex.val);
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
      const v=METRICS.net_gex.val(s), yy=yL(v);
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
  const spxSpot = spxPriceFromSpyPrice(R.spot,R);
  const priceLabel = spxSpot == null ? "Price: "+fmtPrice(R.spot) : "SPY: "+fmtPrice(R.spot)+" / SPX: "+fmtPrice(spxSpot);
  const labelWidth = ctx.measureText(priceLabel).width;
  const alignRight = px>W-labelWidth-12;
  ctx.textAlign = alignRight?"right":"left";
  ctx.fillText(priceLabel, px+(alignRight?-6:6), padT-9);

  // ----- axis titles -----
  if(hasBar){ ctx.save(); ctx.translate(14,padT+plotH/2); ctx.rotate(-Math.PI/2);
    ctx.fillStyle="#d0d0d0"; ctx.font=(mobile ? "bold 11px Segoe UI" : "bold 13px Segoe UI"); ctx.textAlign="center";
    ctx.fillText("Net GEX",0,0); ctx.restore(); }
  if(areas.length){ ctx.save(); ctx.translate(W-12,padT+plotH/2); ctx.rotate(Math.PI/2);
    ctx.fillStyle="#d0d0d0"; ctx.font=(mobile ? "bold 11px Segoe UI" : "bold 13px Segoe UI"); ctx.textAlign="center";
    ctx.fillText(METRICS[dom].label,0,0); ctx.restore(); }
  ctx.fillStyle="#d0d0d0"; ctx.font=(mobile ? "bold 10px Segoe UI" : "bold 12px Segoe UI"); ctx.textAlign="center"; ctx.textBaseline="bottom";
  ctx.fillText("Strike",padL+plotW/2,H-4);
}
function drawNetGexChangeChart(R){
  const cv=document.getElementById('gexChangeChart');
  if(!cv || !R.netGexChange) return;
  const dpr=window.devicePixelRatio||1;
  const W=cv.clientWidth;
  const mobile=W<640;
  const H=mobile ? 320 : 360;
  cv.width=W*dpr; cv.height=H*dpr; cv.style.height=H+'px';
  const ctx=cv.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,W,H);

  const changeByStrike=new Map(R.netGexChange.rows.map(s=>[s.strike,s]));
  const data=visibleStrikeData(R).map(s=>changeByStrike.get(s.strike) || {...s,baselineNetGex:0,netGexChange:0});
  if(!data.length) return;

  const legend=byId('gexChangeLegend');
  const baselineTime=R.netGexChange.createdAt ? new Date(R.netGexChange.createdAt).toLocaleTimeString('he-IL',{hour:'2-digit',minute:'2-digit'}) : '--';
  if(legend){
    legend.innerHTML=R.netGexChange.missingOpenData
      ? `<span style="color:#ffc107">Waiting for open baseline file</span>`
      : `<span class="sq" style="background:#22aaf2"></span>Increase&nbsp;&nbsp;<span class="sq" style="background:#ff2417"></span>Decrease&nbsp;&nbsp;<span>${R.netGexChange.sessionDate || ''} total ${fmtNum(R.netGexChange.totalChange)} from ${baselineTime}</span>`;
  }

  const padL=mobile ? 48 : 70;
  const padR=mobile ? 18 : 42;
  const padT=mobile ? 26 : 32;
  const padB=mobile ? 62 : 76;
  const plotW=W-padL-padR, plotH=H-padT-padB;
  const top=padT, bottom=padT+plotH;
  const slot=plotW/data.length;
  const xCenter=i=>padL+slot*(i+.5);
  const vals=data.map(s=>s.netGexChange || 0);
  const maxAbs=Math.max(...vals.map(v=>Math.abs(v)),1)*1.08;
  const lMin=-maxAbs,lMax=maxAbs;
  const y=v=>top+(lMax-v)/(lMax-lMin)*plotH;
  const y0=y(0);

  ctx.font=(mobile ? 'bold 10px Segoe UI' : 'bold 12px Segoe UI');
  ctx.textBaseline='middle';
  for(let t=0;t<=4;t++){
    const f=t/4, yy=bottom-f*plotH;
    ctx.strokeStyle='rgba(255,255,255,.025)'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(padL,yy); ctx.lineTo(W-padR,yy); ctx.stroke();
    ctx.fillStyle='#c4c4c4'; ctx.textAlign='right';
    ctx.fillText(fmtAxis(lMin+(lMax-lMin)*f),padL-8,yy);
  }

  const bw=Math.max(2,Math.min(slot*.70,32));
  const hitBars=data.map((s,i)=>{
    const v=s.netGexChange || 0;
    const yy=y(v);
    const x=xCenter(i)-bw/2;
    const barY=Math.min(yy,y0);
    const h=Math.max(1,Math.abs(yy-y0));
    ctx.fillStyle=v>=0 ? '#22aaf2' : '#ff2417';
    ctx.fillRect(x,barY,bw,h);
    return {x,y:barY,w:bw,h,s};
  });
  ctx.strokeStyle='rgba(255,255,255,.20)'; ctx.lineWidth=1;
  ctx.beginPath(); ctx.moveTo(padL,y0); ctx.lineTo(W-padR,y0); ctx.stroke();

  ctx.fillStyle='#c4c4c4'; ctx.font=(mobile ? 'bold 9px Segoe UI' : 'bold 10px Segoe UI');
  data.forEach((s,i)=>{
    ctx.save();
    ctx.translate(xCenter(i),bottom+7);
    ctx.rotate(-Math.PI/2);
    ctx.textAlign='right'; ctx.textBaseline='middle';
    ctx.fillText(fmtPrice(s.strike),0,0);
    ctx.restore();
  });

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
  ctx.strokeStyle='#d58b16'; ctx.lineWidth=2;
  ctx.beginPath(); ctx.moveTo(px,padT-5); ctx.lineTo(px,bottom); ctx.stroke();
  ctx.fillStyle='#d58b16'; ctx.font=(mobile ? 'bold 10px Segoe UI' : 'bold 12px Segoe UI'); ctx.textAlign='left'; ctx.textBaseline='alphabetic';
  ctx.fillText('Price: '+fmtPrice(R.spot),px+6,padT-8);

  ctx.save(); ctx.translate(14,padT+plotH/2); ctx.rotate(-Math.PI/2);
  ctx.fillStyle='#d0d0d0'; ctx.font=(mobile ? 'bold 11px Segoe UI' : 'bold 13px Segoe UI'); ctx.textAlign='center';
  ctx.fillText('Net GEX Change',0,0); ctx.restore();
  ctx.fillStyle='#d0d0d0'; ctx.font=(mobile ? 'bold 10px Segoe UI' : 'bold 12px Segoe UI'); ctx.textAlign='center'; ctx.textBaseline='bottom';
  ctx.fillText('Strike',padL+plotW/2,H-4);

  cv._matrixBars=hitBars;
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
  const active = Object.keys(METRICS).filter(m=>ACTIVE.has(m));
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
  const spyPrice = spyPriceFromSpxStrike(s.strike,R);
  const spyRow = spyPrice == null ? '' : `<div class="tt-row spy-row"><span>SPY Price</span><span class="pos">${fmtSpyConvertedPrice(spyPrice)}</span></div>`;
  const spxPrice = spxPriceFromSpyPrice(s.strike,R);
  const spxRow = spxPrice == null ? '' : `<div class="tt-row spy-row"><span>SPX Price</span><span class="pos">${fmtPrice(spxPrice)}</span></div>`;
  tt.innerHTML = `
    <div class="tt-title">${R?.symbol || ''} Strike ${fmtPrice(s.strike)}</div>
    ${metricRows || row('No metric selected','')}
    ${spyRow}
    ${spxRow}
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
  ['chartTooltip','matrixGexTooltip','gexChangeTooltip'].forEach(id=>{const el=byId(id); if(el) el.style.display='none';});
  ['chartCrosshairX','matrixGexCrosshairX','gexChangeCrosshairX'].forEach(id=>{const el=byId(id); if(el) el.style.display='none';});
}
function showGexChangeTooltip(ev){
  const cv=ev.currentTarget || document.getElementById('gexChangeChart');
  const tt=document.getElementById('gexChangeTooltip');
  const cross=document.getElementById('gexChangeCrosshairX');
  if(!cv || !tt || !cross) return;
  const bars=cv._matrixBars || [];
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
  const cls=s.netGexChange>=0?'pos':'neg';
  const row=(label,value,rowCls='')=>`<div class="tt-row"><span>${label}</span><span class="${rowCls}">${value}</span></div>`;
  tt.innerHTML=`
    <div class="tt-title">${R?.symbol || ''} Strike ${fmtPrice(s.strike)}</div>
    ${row('Change',fmtNum(s.netGexChange || 0),cls)}
    ${row('Current Net GEX',fmtNum(s.netGex || 0),s.netGex>=0?'pos':'neg')}
    ${row('Open Baseline',fmtNum(s.baselineNetGex || 0),s.baselineNetGex>=0?'pos':'neg')}
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
  return fetchLiveData(MATRIX_CANDLES_URL + '?symbol=SPY&interval=5m&range=1d&t=' + Date.now())
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
      <b>${item ? `${item.label} · ${item.pressure}` : fallback}</b>
    </div>`;
  host.innerHTML=`
    <div class="market-read-title">
      <h3>Market Read</h3>
      <div class="market-read-bias">${bias}</div>
    </div>
    <div class="market-read-now">
      <span class="k">Current Read ${spySpot!=null ? `· SPY ${fmtSpyConvertedPrice(spySpot)}` : ''}</span>
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
  tt.innerHTML=`<div class="tt-title">${window._lastR?.symbol||''} Strike ${fmtPrice(s.strike)} · ${s.zone}</div>
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
const selectedExpirationsByView = {gex:null, 'matrix-gex':null, 'shock-engine':null, 'max-pain':null};
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
  const validSelected = expiries.filter(exp=>selected.has(exp));
  const defaultSelection = expiries.slice(0,1);
  const active = new Set(validSelected.length ? validSelected : defaultSelection);
  picker.innerHTML = expiries.length ? expiries.map(exp=>{
    const dte = byExp.get(exp);
    return `<label class="expiry-option"><input type="checkbox" value="${exp}" ${active.has(exp)?'checked':''}>${exp} (${dte}DTE)</label>`;
  }).join('') : '<span class="expiry-empty">No expirations available</span>';
  if(selectedExpirationsByView.hasOwnProperty(activeView) && !remembered){
    selectedExpirationsByView[activeView] = currentExpirationValues();
  }
}
function setView(view){
  if(view === activeView) return;
  saveCurrentExpirationSelection();
  activeView = view;
  document.querySelectorAll('.side-nav a[data-view]').forEach(a=>a.classList.toggle('active',a.dataset.view===view));
  document.querySelectorAll('.app-view').forEach(section=>section.classList.toggle('active',section.id===`view-${view}`));
  hideAllHoverHelpers();
  run();
  if(view === 'gex' && window._lastR) requestAnimationFrame(()=>{
    drawChart(window._lastR);
    drawNetGexChangeChart(window._lastR);
  });
  if(view === 'matrix-gex' && window._lastR) requestAnimationFrame(()=>drawChart(window._lastR,'matrixGexChart'));
  if(view === 'shock-engine' && window._lastR) requestAnimationFrame(()=>drawShockEngine(window._lastR));
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
  updateNetFlowHistoryFromSnapshot(chain);
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
  R.expectedMove = expectedMove;
  R.maxPain = maxPain;
  R.live = !!chain.live;
  R.asof = useLive ? REAL[sym].asof : null;
  let openR=null;
  const openRec=OPEN_REAL?.data?.[sym];
  if(openRec && OPEN_REAL.session_date === netGexSessionDateKey(R)){
    let openChain=buildChainRealFromRecord(sym,openRec,null);
    if(selectedExpirations.size){
      openChain={...openChain, quotes:openChain.quotes.filter(q=>selectedExpirations.has(q.exp))};
    }
    openR=calcGEX(openChain,mode);
  }
  R.netGexChange = buildNetGexChange(R,{
    mode,
    expirations:selectedExpirationValues,
    baselineResult:openR,
    requireOpenData:useLive,
  });
  R.marketRead = buildMarketRead(R);
  renderImpl(R);
}
bind('market','change',()=>{
  selectedExpirationsByView.gex=null;
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
  selectedExpirationsByView['matrix-gex']=null;
  selectedExpirationsByView['shock-engine']=null;
  selectedExpirationsByView['max-pain']=null;
  run();
});
bind('expirationPicker','change',()=>{
  saveCurrentExpirationSelection();
  run();
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
    drawNetGexChangeChart(window._lastR);
  }
  if(activeView === 'matrix-gex') drawChart(window._lastR,'matrixGexChart');
  if(activeView === 'shock-engine') drawShockEngine(window._lastR);
  if(activeView === 'max-pain') drawMaxPain(window._lastR);
});
bind('gexChart','mousemove',showChartTooltip);
bind('gexChart','mouseleave',hideChartTooltip);
bind('gexChart','touchstart',showChartTooltip,{passive:true});
bind('gexChart','touchmove',showChartTooltip,{passive:true});
bind('gexChart','touchend',()=>setTimeout(hideChartTooltip,1200),{passive:true});
bind('gexChangeChart','mousemove',showGexChangeTooltip);
bind('gexChangeChart','mouseleave',hideChartTooltip);
bind('gexChangeChart','touchstart',showGexChangeTooltip,{passive:true});
bind('gexChangeChart','touchmove',showGexChangeTooltip,{passive:true});
bind('gexChangeChart','touchend',()=>setTimeout(hideChartTooltip,1200),{passive:true});
bind('matrixGexChart','mousemove',showChartTooltip);
bind('matrixGexChart','mouseleave',hideChartTooltip);
bind('matrixGexChart','touchstart',showChartTooltip,{passive:true});
bind('matrixGexChart','touchmove',showChartTooltip,{passive:true});
bind('matrixGexChart','touchend',()=>setTimeout(hideChartTooltip,1200),{passive:true});
bind('shockEngineChart','mousemove',showShockTooltip);
bind('shockEngineChart','mouseleave',hideShockTooltip);
bind('shockEngineChart','touchstart',showShockTooltip,{passive:true});
bind('shockEngineChart','touchmove',showShockTooltip,{passive:true});
bind('shockEngineChart','touchend',()=>setTimeout(hideShockTooltip,1200),{passive:true});
bind('optionsHeatMap','mousemove',showHeatMapTip);
bind('optionsHeatMap','mouseleave',hideHeatMapTip);
bind('darkPoolLevels','mousemove',showDarkPoolTooltip);
bind('darkPoolLevels','mouseleave',hideDarkPoolTooltip);
bind('netFlowChart','mousemove',showNetFlowTooltip);
bind('netFlowChart','mouseleave',hideNetFlowTooltip);
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
    if(ACTIVE.has(m)) ACTIVE.delete(m); else ACTIVE.add(m);
    btn.classList.toggle('active', ACTIVE.has(m));
    if(window._lastR && activeView === 'gex'){
      drawChart(window._lastR);
      drawNetGexChangeChart(window._lastR);
    }
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
    if(window._lastR && activeView === 'gex'){
      drawChart(window._lastR);
      drawNetGexChangeChart(window._lastR);
    }
  });
});
document.querySelectorAll('[data-edge-sigma]').forEach(btn=>{
  btn.addEventListener('click',()=>{
    DISPLAY_SIGMA=btn.dataset.edgeSigma==='all' ? Infinity : Number(btn.dataset.edgeSigma);
    syncSigmaButtons();
    if(window._lastR && activeView === 'edge') drawEdge(window._lastR);
  });
});

// ---------- Data loading + auto-refresh ----------
// Reloads cboe_data.json and refreshes the display.
let _refreshTimer=null, _timerUiTimer=null, _firstLoad=true, _loadingData=false;
let _lastFileLoadedAt=null, _lastLoadOk=false, _lastTripityRetryAt=0;
let _lastDataSource='Waiting';
let _matrixChart=null, _matrixCandleSeries=null, _matrixPriceLines=[], _matrixCandles=[], _matrixCandlesLoadedAt=0;
const MATRIX_SPX_QUOTE_URL = 'https://api.trytripity.site/api/matrix/spx-quote';
const MATRIX_CBOE_DATA_URL = 'https://api.trytripity.site/api/matrix/cboe-data';
const MATRIX_CANDLES_URL = 'https://api.trytripity.site/api/matrix/candles';
const MATRIX_REPO_RAW_BASE = 'https://raw.githubusercontent.com/danielbul1/matrix-gex';
const TRIPITY_RETRY_MS = 15000;
const MARKET_FRESHNESS_MAX_MS = 75 * 60 * 1000;
const MARKET_FRESHNESS_START_MIN = 9 * 60 + 45;
const MARKET_FRESHNESS_END_MIN = 16 * 60 + 30;
function dataUrl(){
  return 'cboe_data.json?t=' + Date.now();
}
function openDataUrl(){
  return 'cboe_open_data.json?t=' + Date.now();
}
function repoDataUrl(branch){
  return `${MATRIX_REPO_RAW_BASE}/${branch}/cboe_data.json?t=${Date.now()}`;
}
function repoOpenDataUrl(branch){
  return `${MATRIX_REPO_RAW_BASE}/${branch}/cboe_open_data.json?t=${Date.now()}`;
}
function isGithubPagesHost(){
  return location.hostname.endsWith('github.io');
}
function fetchLiveData(url){
  return fetch(url, {cache:'no-store'})
    .then(r=>r.ok?r.json():null)
    .catch(()=>null);
}
function applyMatrixSpXQuote(q){
  if(!q || q.ok===false || !Number.isFinite(Number(q.spot))) return false;
  if(!REAL.SPX) return false;
  REAL.SPX = {
    ...REAL.SPX,
    spot: Number(q.spot),
    asof: q.asof || REAL.SPX.asof,
    quoteSource: q.source || 'tripity',
  };
  return true;
}
function fetchMatrixSpXQuote(){
  return fetchLiveData(MATRIX_SPX_QUOTE_URL + '?t=' + Date.now()).then(applyMatrixSpXQuote);
}
function validCboeData(d){
  return d && d.SPX && Array.isArray(d.SPX.opts) && Number.isFinite(Number(d.SPX.spot));
}
function validOpenCboeData(d){
  return d && d.session_date && validCboeData(d.data);
}
function fetchCboeDataWithSource(url, sourceName){
  return fetchLiveData(url).then(d=>validCboeData(d) ? {data:d, source:sourceName} : null);
}
function fetchFallbackCboeData(){
  const sources = isGithubPagesHost()
    ? [
        [repoDataUrl('data'), 'GitHub data'],
        [dataUrl(), 'Local'],
        [repoDataUrl('main'), 'GitHub main'],
      ]
    : [
        [dataUrl(), 'Local'],
        [repoDataUrl('data'), 'GitHub data'],
        [repoDataUrl('main'), 'GitHub main'],
      ];
  return sources.reduce(
    (chain,[url,source])=>chain.then(result=>result || fetchCboeDataWithSource(url, source)),
    Promise.resolve(null)
  );
}
function fetchOpenCboeData(){
  const sources = isGithubPagesHost()
    ? [repoOpenDataUrl('data'), openDataUrl(), repoOpenDataUrl('main')]
    : [openDataUrl(), repoOpenDataUrl('data'), repoOpenDataUrl('main')];
  return sources.reduce(
    (chain,url)=>chain.then(result=>result || fetchLiveData(url).then(d=>validOpenCboeData(d) ? d : null)),
    Promise.resolve(null)
  );
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
  const text=String(value);
  const time=(text.match(/T(\d{2}:\d{2}:\d{2})/) || text.match(/(\d{2}:\d{2}:\d{2})/))?.[1];
  return time ? `${time} ET` : text;
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
  if(minutes<=20) return {key:'fresh', label:'Fresh'};
  if(minutes<=45) return {key:'delayed', label:'Delayed'};
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
  banner.innerHTML=`<b>Stale market data:</b> SPX CBOE data is ${fmtDataAge(ageMs)} old, as of ${fmtAsofShort(rec?.asof)}. Treat pressure levels as historical until refresh succeeds.`;
}
function updateDataTimer(){
  const card=document.getElementById('dataHealth');
  if(!card) return;
  const rec=REAL?.SPX || window._lastR;
  const asofMs=dataAsofToMs(rec?.asof);
  const ageMs=asofMs==null ? null : Date.now()-asofMs;
  const health=dataHealthFromAge(ageMs);
  card.classList.remove('fresh','delayed','stale','failed');
  if(health.key!=='checking') card.classList.add(health.key);
  document.getElementById('dataHealthState').textContent = health.label;
  document.getElementById('dataHealthSource').textContent = _lastDataSource;
  document.getElementById('dataHealthAge').textContent = fmtDataAge(ageMs);
  document.getElementById('dataHealthContracts').textContent = REAL?.SPX?.opts ? REAL.SPX.opts.length.toLocaleString('en-US') : '--';
  document.getElementById('dataHealthAsof').textContent = fmtAsofShort(rec?.asof);
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
  return fetchCboeDataWithSource(MATRIX_CBOE_DATA_URL + '?t=' + Date.now(), 'Tripity')
    .then(result=>{
      if(result || !validCboeData(REAL)) return result;
      _lastDataSource = 'Tripity retry';
      return null;
    })
    .then(result=>result || (validCboeData(REAL) ? null : fetchFallbackCboeData()))
    .then(result=>{
      _lastLoadOk=!!result || validCboeData(REAL);
      if(result){ REAL=result.data; _lastDataSource=result.source; _lastFileLoadedAt=Date.now(); }
    })
    .then(()=>fetchOpenCboeData().then(openData=>{
      if(openData) OPEN_REAL=openData;
    }))
    .then(()=>fetchMatrixSpXQuote())
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

populateSymbols();
document.getElementById('symbol').value = DEFAULT_SYMBOL;
document.getElementById('mode').value = 'full';
document.getElementById('source').value = 'live';
document.getElementById('autorefresh').value = '30';
document.getElementById('forceTripityRefresh').addEventListener('click',()=>loadData(true));
_timerUiTimer=setInterval(updateDataTimer,1000);
loadData(true).then(setupAutoRefresh);
