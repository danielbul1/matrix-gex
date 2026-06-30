from flask import Flask, jsonify
from flask_cors import CORS
import requests
import numpy as np
from scipy.stats import norm
from datetime import datetime
from zoneinfo import ZoneInfo
import time
import re

app = Flask(__name__)
CORS(app)

TICKER  = 'SPY'
URL     = f'https://cdn.cboe.com/api/global/delayed_quotes/options/{TICKER}.json'
CACHE_S = 5

_cache = {'data': None, 'ts': 0}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept':     'application/json',
    'Referer':    'https://www.cboe.com/',
    'Origin':     'https://www.cboe.com',
}

OPT_RE = re.compile(r'^([A-Z]{1,6})(\d{6})([CP])(\d{8,})$')

def parse_sym(sym):
    m = OPT_RE.match(str(sym).strip())
    if not m:
        return None
    _, date_s, kind, strike_s = m.groups()
    try:
        exp = datetime.strptime(date_s, '%y%m%d').replace(hour=16)
    except:
        return None
    strike = int(strike_s[:8]) / 1000.0
    return {'exp': exp, 'kind': kind, 'strike': strike}

def safe(v, d=0.0):
    try:
        f = float(str(v).replace('%','').replace(',','').strip())
        return f if f == f else d
    except:
        return d

def norm_iv(iv):
    if iv > 1: iv /= 100
    return iv if 0 < iv < 5 else 0.0

def is_third_friday(d):
    return d.weekday() == 4 and 15 <= d.day <= 21

def minutes_to_market_close():
    now_et = datetime.now(ZoneInfo('America/New_York'))
    close_et = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if now_et.weekday() >= 5:
        return 390
    if now_et <= open_et:
        return 390
    if now_et >= close_et:
        return 0
    return int((close_et - now_et).total_seconds() // 60)

# ── Black-Scholes greeks ──────────────────────────────────────────────────────
def bs_d1d2(S, K, vol, T):
    sq = np.sqrt(T)
    d1 = (np.log(S / K) + 0.5 * vol**2 * T) / (vol * sq)
    d2 = d1 - vol * sq
    return d1, d2

def bs_gamma_scalar(S, K, vol, T):
    if T <= 0 or vol <= 0: return 0.0
    d1, _ = bs_d1d2(S, K, vol, T)
    return norm.pdf(d1) / (S * vol * np.sqrt(T))

def bs_vanna(S, K, vol, T):
    """Vanna = dDelta/dVol = -norm.pdf(d1)*d2/vol"""
    if T <= 0 or vol <= 0: return 0.0
    d1, d2 = bs_d1d2(S, K, vol, T)
    return -norm.pdf(d1) * d2 / vol

def bs_charm_call(S, K, vol, T, r=0):
    """Charm for call = dDelta/dt"""
    if T <= 0 or vol <= 0: return 0.0
    sq = np.sqrt(T)
    d1, d2 = bs_d1d2(S, K, vol, T)
    return -norm.pdf(d1) * (2*r*T - d2*vol*sq) / (2*T*vol*sq)

# ── vectorized GEX at a spot level ───────────────────────────────────────────
def gex_at(S, Kc, vc, Tc, OIc, Kp, vp, Tp, OIp):
    def side(K, vol, T, OI):
        if len(K) == 0: return 0.0
        mask = (T > 0) & (vol > 0) & (OI > 0)
        if not mask.any(): return 0.0
        K_, v_, T_, OI_ = K[mask], vol[mask], T[mask], OI[mask]
        sq = np.sqrt(T_)
        d1 = (np.log(S / K_) + 0.5*v_**2*T_) / (v_*sq)
        g  = norm.pdf(d1) / (S * v_ * sq)
        return (OI_ * 100 * S * S * 0.01 * g).sum()
    return (side(Kc,vc,Tc,OIc) - side(Kp,vp,Tp,OIp)) / 1e9

def make_np(opts, exclude_exp=None):
    c_rows, p_rows = [], []
    for o in opts:
        if exclude_exp and o['exp'] == exclude_exp: continue
        c, p = o.get('C',{}), o.get('P',{})
        if c.get('iv',0)>0 and c.get('oi',0)>0:
            c_rows.append((o['strike'], c['iv'], o['T'], c['oi']))
        if p.get('iv',0)>0 and p.get('oi',0)>0:
            p_rows.append((o['strike'], p['iv'], o['T'], p['oi']))
    def to_np(rows):
        if not rows: return np.array([]),np.array([]),np.array([]),np.array([])
        a = np.array(rows)
        return a[:,0],a[:,1],a[:,2],a[:,3]
    return to_np(c_rows), to_np(p_rows)

# ── main computation ──────────────────────────────────────────────────────────
def compute():
    now = time.time()
    if _cache['data'] and now - _cache['ts'] < CACHE_S:
        return _cache['data']

    r = requests.get(URL, headers=HEADERS, timeout=15)
    r.raise_for_status()
    raw = r.json()

    spot   = safe(raw['data']['current_price'])
    ts     = str(raw.get('timestamp', datetime.now().isoformat()))
    today  = datetime.now().replace(hour=0,minute=0,second=0,microsecond=0)
    lo, hi = 0.8*spot, 1.2*spot

    # ── parse ─────────────────────────────────────────────────────────────────
    pairs = {}
    for o in raw['data']['options']:
        parsed = parse_sym(o.get('option',''))
        if not parsed: continue
        strike = parsed['strike']
        if not (lo <= strike <= hi): continue

        exp  = parsed['exp']
        kind = parsed['kind']
        key  = (exp, strike)

        if key not in pairs:
            bd = int(np.busday_count(today.date(), exp.date()))
            T  = max(bd, 1) / 262.0
            is_0dte = (bd == 0)
            pairs[key] = {'exp':exp,'strike':strike,'T':T,'is_0dte':is_0dte,'C':{},'P':{}}

        iv  = norm_iv(safe(o.get('iv',0)))
        gam = safe(o.get('gamma',0))
        oi  = safe(o.get('open_interest',0))
        dlt = safe(o.get('delta',0))
        pairs[key][kind] = {'iv':iv,'gamma':gam,'oi':oi,'delta':dlt}

    if not pairs:
        raise RuntimeError('No options parsed. See /raw to inspect CBOE response.')

    opts = list(pairs.values())

    # ── spot GEX, DEX, Vanna, Charm by strike ────────────────────────────────
    by_k     = {}
    by_exp   = {}
    by_k_0dte = {}

    for o in opts:
        k  = o['strike']
        el = o['exp'].strftime('%b %d')
        c, p = o.get('C',{}), o.get('P',{})
        T    = o['T']

        # GEX
        c_gex = c.get('gamma',0) * c.get('oi',0) * 100 * spot**2 * 0.01
        p_gex = p.get('gamma',0) * p.get('oi',0) * 100 * spot**2 * 0.01

        # DEX (delta exposure per 1% move)
        c_dex = c.get('delta',0) * c.get('oi',0) * 100 * spot * 0.01
        p_dex = p.get('delta',0) * p.get('oi',0) * 100 * spot * 0.01

        # Vanna exposure per 1% IV move
        c_vanna_unit = bs_vanna(spot, k, c.get('iv',0), T) if c.get('iv',0)>0 else 0
        p_vanna_unit = bs_vanna(spot, k, p.get('iv',0), T) if p.get('iv',0)>0 else 0
        c_vex =  c_vanna_unit * c.get('oi',0) * 100 * spot * 0.01
        p_vex = -p_vanna_unit * p.get('oi',0) * 100 * spot * 0.01

        # Charm exposure per 1 day
        c_charm_unit = bs_charm_call(spot, k, c.get('iv',0), T) if c.get('iv',0)>0 else 0
        p_charm_unit = -bs_charm_call(spot, k, p.get('iv',0), T) if p.get('iv',0)>0 else 0  # put charm ≈ -call charm
        c_charm =  c_charm_unit * c.get('oi',0) * 100 * spot / 252
        p_charm = -p_charm_unit * p.get('oi',0) * 100 * spot / 252

        by_k.setdefault(k, {'cgex':0,'pgex':0,'cdex':0,'pdex':0,'cvex':0,'pvex':0,'ccharm':0,'pcharm':0})
        by_k[k]['cgex']   += c_gex
        by_k[k]['pgex']   += p_gex
        by_k[k]['cdex']   += c_dex
        by_k[k]['pdex']   += p_dex
        by_k[k]['cvex']   += c_vex
        by_k[k]['pvex']   += p_vex
        by_k[k]['ccharm'] += c_charm
        by_k[k]['pcharm'] += p_charm

        by_exp.setdefault(el,{'c':0,'p':0,'date':o['exp']})
        by_exp[el]['c'] += c_gex
        by_exp[el]['p'] += p_gex

        if o['is_0dte']:
            by_k_0dte.setdefault(k,{'cgex':0,'pgex':0})
            by_k_0dte[k]['cgex'] += c_gex
            by_k_0dte[k]['pgex'] += p_gex

    total_gex = sum(v['cgex']-v['pgex'] for v in by_k.values()) / 1e9

    # ── key levels ────────────────────────────────────────────────────────────
    call_wall = max(by_k, key=lambda k: by_k[k]['cgex'])
    put_wall  = max(by_k, key=lambda k: by_k[k]['pgex'])
    peak_gex  = max(by_k, key=lambda k: abs(by_k[k]['cgex']-by_k[k]['pgex']))

    # ── formatted outputs ─────────────────────────────────────────────────────
    def make_by_strike(d):
        return sorted([{
            'strike':  k,
            'callGEX': v['cgex']/1e9, 'putGEX': -v['pgex']/1e9, 'netGEX': (v['cgex']-v['pgex'])/1e9,
            'callDEX': v['cdex']/1e6, 'putDEX':  v['pdex']/1e6,  'netDEX': (v['cdex']+v['pdex'])/1e6,
            'callVEX': v['cvex']/1e6, 'putVEX':   v['pvex']/1e6,  'netVEX': (v['cvex']+v['pvex'])/1e6,
            'callCharm': v['ccharm']/1e6, 'putCharm': v['pcharm']/1e6, 'netCharm': (v['ccharm']+v['pcharm'])/1e6,
        } for k,v in d.items()], key=lambda x: x['strike'])

    by_strike = make_by_strike(by_k)

    def add_order_flow_imbalance(rows, total_gex_bn):
        minutes_left = minutes_to_market_close()
        time_factor = 1.0 - (minutes_left / 390.0)
        if not rows:
            return {
                'ofi': 0, 'combinedFlow': 0, 'side': 'neutral', 'strength': 'none',
                'regime': 'GAMMA_TRAP', 'message': 'No usable option flow parsed.',
                'minutesToClose': minutes_left, 'timeFactor': round(time_factor, 4),
                'topStrike': None, 'powerZones': [], 'components': {}
            }
        max_g = max(max(abs(r['netGEX']) for r in rows), 1e-9)
        max_v = max(max(abs(r['netVEX']) for r in rows), 1e-9)
        max_c = max(max(abs(r['netCharm']) for r in rows), 1e-9)
        gamma_accel = 0.85 if total_gex_bn >= 0 else 1.25
        weights = {'gex': 0.50, 'vanna': 0.32, 'charm': 0.28}
        weighted_sum = 0.0
        liquidity_sum = 0.0
        for r in rows:
            g = r['netGEX'] / max_g
            v = r['netVEX'] / max_v
            c = r['netCharm'] / max_c
            raw = weights['gex']*g + gamma_accel*(weights['vanna']*v + weights['charm']*c)
            r['ofi'] = round(float(np.tanh(raw) * 100), 2)
            r['vannaPressure'] = round(float(v * 100), 2)
            r['charmPressure'] = round(float(c * 100), 2)
            r['powerZone'] = round(float(
                0.42*abs(g) + 0.24*abs(v) + 0.20*abs(c) + 0.14*min(1.0, abs(r['netDEX'])/max_g)
            ), 4)
            liq = max(1.0, abs(r['netGEX']) + abs(r['netVEX']) + abs(r['netCharm']))
            weighted_sum += r['ofi'] * liq
            liquidity_sum += liq
        ofi = weighted_sum / liquidity_sum if liquidity_sum else 0.0
        vanna_total = sum(r['netVEX'] for r in rows)
        charm_total = sum(r['netCharm'] for r in rows)
        vanna_score = float(np.clip(vanna_total / 500.0 * 100.0, -100.0, 100.0))
        charm_score = float(np.clip(charm_total / 500.0 * 100.0, -100.0, 100.0))
        combined_flow = (ofi * 0.4) + (vanna_score * 0.3) + (charm_score * 0.3 * time_factor)
        if combined_flow > 15 and vanna_total > 0:
            regime = 'VANNA_SQUEEZE'
            message = 'Makers aggressively buying futures as IV pressure fades. Bullish acceleration.'
        elif combined_flow < -15 and vanna_total < 0:
            regime = 'LIQUIDITY_CASCADE'
            message = 'Negative flow acceleration. Market makers reinforcing downside hedges.'
        elif abs(combined_flow) <= 10:
            regime = 'GAMMA_TRAP'
            message = 'Market pinned between high GEX walls. Scalp boundaries only. Avoid breakouts.'
        else:
            regime = 'STANDARD_FLOW'
            message = 'Flow is operational. Trade the PowerZones normally.'
        side = 'buy' if combined_flow > 8 else 'sell' if combined_flow < -8 else 'neutral'
        strength = 'extreme' if abs(combined_flow) >= 65 else 'strong' if abs(combined_flow) >= 35 else 'moderate' if abs(combined_flow) >= 15 else 'light'
        power_zones = sorted(rows, key=lambda r: r['powerZone'], reverse=True)[:5]
        return {
            'ofi': round(float(ofi), 2),
            'combinedFlow': round(float(combined_flow), 2),
            'side': side,
            'strength': strength,
            'regime': regime,
            'message': message,
            'minutesToClose': minutes_left,
            'timeFactor': round(float(time_factor), 4),
            'topStrike': power_zones[0]['strike'] if power_zones else None,
            'powerZones': [{
                'strike': r['strike'], 'ofi': r['ofi'], 'netGEX': r['netGEX'],
                'netVEX': r['netVEX'], 'netCharm': r['netCharm'], 'score': r['powerZone']
            } for r in power_zones],
            'components': {
                'netGEX': round(sum(r['netGEX'] for r in rows), 4),
                'netVEX': round(vanna_total, 4),
                'netCharm': round(charm_total, 4),
                'vannaScore': round(vanna_score, 2),
                'charmScore': round(charm_score, 2),
                'gammaRegime': 'positive' if total_gex_bn >= 0 else 'negative',
                'weights': weights,
            }
        }

    institutional_pressure = add_order_flow_imbalance(by_strike, total_gex)

    by_k_0dte_full = {k: {'cgex':v['cgex'],'pgex':v['pgex'],
                           'cdex':0,'pdex':0,'cvex':0,'pvex':0,'ccharm':0,'pcharm':0}
                      for k,v in by_k_0dte.items()}
    by_strike_0dte = make_by_strike(by_k_0dte_full)

    by_expiry = sorted([{
        'expiry': k, 'callGEX': v['c']/1e9, 'putGEX': -v['p']/1e9, 'netGEX': (v['c']-v['p'])/1e9
    } for k,v in by_exp.items()], key=lambda x: by_exp[x['expiry']]['date'])

    # ── gamma profile ─────────────────────────────────────────────────────────
    levels   = np.linspace(lo, hi, 60)
    all_dates = sorted({o['exp'] for o in opts})
    next_exp  = all_dates[0] if all_dates else None
    tf_dates  = [d for d in all_dates if is_third_friday(d)]
    next_fri  = tf_dates[0] if tf_dates else next_exp

    (Kc,vc,Tc,OIc),(Kp,vp,Tp,OIp) = make_np(opts)
    (Kc_xn,vc_xn,Tc_xn,OIc_xn),(Kp_xn,vp_xn,Tp_xn,OIp_xn) = make_np(opts,next_exp)
    (Kc_xf,vc_xf,Tc_xf,OIc_xf),(Kp_xf,vp_xf,Tp_xf,OIp_xf) = make_np(opts,next_fri)

    prof_all = np.array([gex_at(lv,Kc,vc,Tc,OIc,Kp,vp,Tp,OIp)             for lv in levels])
    prof_xn  = np.array([gex_at(lv,Kc_xn,vc_xn,Tc_xn,OIc_xn,Kp_xn,vp_xn,Tp_xn,OIp_xn) for lv in levels])
    prof_xf  = np.array([gex_at(lv,Kc_xf,vc_xf,Tc_xf,OIc_xf,Kp_xf,vp_xf,Tp_xf,OIp_xf) for lv in levels])

    zero_gamma = None
    idx = np.where(np.diff(np.sign(prof_all)))[0]
    if len(idx):
        i = idx[0]
        ng,pg_ = prof_all[i],prof_all[i+1]
        ns,ps  = levels[i],levels[i+1]
        zero_gamma = float(ps-(ps-ns)*pg_/(pg_-ng))

    # ── GEX heatmap (strikes × expiries within 60 days) ──────────────────────
    hm_lo, hm_hi = 0.9*spot, 1.1*spot    # tighter range for readability
    cutoff = today.replace(hour=0,minute=0,second=0,microsecond=0)
    from datetime import timedelta
    cutoff60 = cutoff + timedelta(days=60)

    hm_opts = [o for o in opts if hm_lo <= o['strike'] <= hm_hi and o['exp'].replace(tzinfo=None) <= cutoff60.replace(tzinfo=None)]

    hm_strikes = sorted({o['strike'] for o in hm_opts})
    hm_exps    = sorted({o['exp'] for o in hm_opts})
    hm_labels  = [e.strftime('%b %d') for e in hm_exps]

    si_map = {s:i for i,s in enumerate(hm_strikes)}
    ei_map = {e:j for j,e in enumerate(hm_exps)}
    z = [[0.0]*len(hm_exps) for _ in range(len(hm_strikes))]

    for o in hm_opts:
        si = si_map.get(o['strike'])
        ei = ei_map.get(o['exp'])
        if si is None or ei is None: continue
        c, p = o.get('C',{}), o.get('P',{})
        net = (c.get('gamma',0)*c.get('oi',0) - p.get('gamma',0)*p.get('oi',0)) * 100*spot**2*0.01/1e6
        z[si][ei] = round(net, 3)

    # ── raw options table (includes all greeks for client-side filtering) ──────
    raw_opts = []
    for o in opts:
        ks  = o['strike']
        T_  = o['T']
        c, p = o.get('C',{}), o.get('P',{})
        c_iv, p_iv = c.get('iv',0), p.get('iv',0)
        c_oi, p_oi = c.get('oi',0), p.get('oi',0)
        c_dlt, p_dlt = c.get('delta',0), p.get('delta',0)
        raw_opts.append({
            'expiration': o['exp'].strftime('%Y-%m-%d'),
            'strike':     ks,
            'is0dte':     o['is_0dte'],
            # call
            'callIV':    round(c_iv*100, 2),
            'callGamma': round(c.get('gamma',0), 4),
            'callOI':    int(c_oi),
            'callDelta': round(c_dlt, 4),
            'callGEX':   round(c.get('gamma',0)*c_oi*100*spot**2*0.01/1e6, 3),
            'callDEX':   round(c_dlt*c_oi*100*spot*0.01/1e6, 4),
            'callVEX':   round(bs_vanna(spot,ks,c_iv,T_)*c_oi*100*spot*0.01/1e6, 4) if c_iv>0 else 0,
            'callCharm': round(bs_charm_call(spot,ks,c_iv,T_)*c_oi*100*spot/252/1e6, 4) if c_iv>0 else 0,
            # put
            'putIV':     round(p_iv*100, 2),
            'putGamma':  round(p.get('gamma',0), 4),
            'putOI':     int(p_oi),
            'putDelta':  round(p_dlt, 4),
            'putGEX':    round(-p.get('gamma',0)*p_oi*100*spot**2*0.01/1e6, 3),
            'putDEX':    round(p_dlt*p_oi*100*spot*0.01/1e6, 4),
            'putVEX':    round(-bs_vanna(spot,ks,p_iv,T_)*p_oi*100*spot*0.01/1e6, 4) if p_iv>0 else 0,
            'putCharm':  round(-bs_charm_call(spot,ks,p_iv,T_)*p_oi*100*spot/252/1e6, 4) if p_iv>0 else 0,
        })
    raw_opts.sort(key=lambda x: (x['expiration'], x['strike']))

    result = {
        'spot':       spot,
        'timestamp':  ts,
        'ticker':     TICKER,
        'totalGEX':   round(total_gex, 4),
        'zeroGamma':  round(zero_gamma,2) if zero_gamma else None,
        'callWall':   call_wall,
        'putWall':    put_wall,
        'peakGEX':    peak_gex,
        'regime':     'positive' if total_gex >= 0 else 'negative',
        'institutionalPressure': institutional_pressure,
        'byStrike':   by_strike,
        'byStrike0dte': by_strike_0dte,
        'byExpiry':   by_expiry,
        'options':    raw_opts,
        'profile': {
            'levels': levels.tolist(),
            'all':    prof_all.tolist(),
            'exNext': prof_xn.tolist(),
            'exFri':  prof_xf.tolist(),
        },
        'heatmap': {
            'strikes':  hm_strikes,
            'expiries': hm_labels,
            'z':        z,
        },
        'count': len(opts),
    }
    _cache['data'] = result
    _cache['ts']   = now
    return result

# ── routes ────────────────────────────────────────────────────────────────────
@app.route('/gex')
def gex():
    try:
        return jsonify(compute())
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/raw')
def raw_view():
    try:
        r = requests.get(URL, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        opts = data.get('data',{}).get('options',[])
        return jsonify({
            'top_keys':    list(data.keys()),
            'data_keys':   list(data.get('data',{}).keys()),
            'options_len': len(opts),
            'first_5':     opts[:5] if isinstance(opts,list) else [],
            'spot':        data.get('data',{}).get('current_price'),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health')
def health():
    return jsonify({'ok': True, 'time': datetime.now().isoformat()})

if __name__ == '__main__':
    print('╔════════════════════════════════════╗')
    print('║   GEX Live Server  ·  SPY  v2      ║')
    print('║   http://localhost:5000/gex        ║')
    print('╚════════════════════════════════════╝')
    app.run(host='127.0.0.1', port=5000, debug=False)
