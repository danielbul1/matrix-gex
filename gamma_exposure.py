import pandas as pd
import numpy as np
from scipy.stats import norm
import matplotlib.pyplot as plt
from datetime import datetime, timedelta

pd.options.display.float_format = '{:,.4f}'.format

filename = r'C:\Users\user\Downloads\spy_quotedata.csv'

def calcGammaEx(S, K, vol, T, r, q, optType, OI):
    if T == 0 or vol == 0:
        return 0
    dp = (np.log(S/K) + (r - q + 0.5*vol**2)*T) / (vol*np.sqrt(T))
    dm = dp - vol*np.sqrt(T)
    if optType == 'call':
        gamma = np.exp(-q*T) * norm.pdf(dp) / (S * vol * np.sqrt(T))
    else:
        gamma = K * np.exp(-r*T) * norm.pdf(dm) / (S * S * vol * np.sqrt(T))
    return OI * 100 * S * S * 0.01 * gamma

def isThirdFriday(d):
    return d.weekday() == 4 and 15 <= d.day <= 21

with open(filename) as f:
    lines = f.readlines()

# Parse spot price
spotPrice = float(lines[1].split('Last:')[1].split(',')[0].strip())
fromStrike = 0.8 * spotPrice
toStrike   = 1.2 * spotPrice

# Parse date — handles "June 3, 2026 at 11:17 AM EDT" format
datePart = lines[2].split('Date: ')[1].split(',')
monthDay = datePart[0].strip().split(' ')
if len(monthDay) == 2:
    month, day = monthDay[0], int(monthDay[1])
    year = int(datePart[1].strip().split(' ')[0])
else:
    day, month, year = int(monthDay[0]), monthDay[1], int(monthDay[2])
todayDate = datetime.strptime(month, '%B').replace(day=day, year=year)

# Load options chain
df = pd.read_csv(filename, sep=',', header=None, skiprows=4)
df.columns = [
    'ExpirationDate','Calls','CallLastSale','CallNet','CallBid','CallAsk','CallVol',
    'CallIV','CallDelta','CallGamma','CallOpenInt',
    'StrikePrice',
    'Puts','PutLastSale','PutNet','PutBid','PutAsk','PutVol',
    'PutIV','PutDelta','PutGamma','PutOpenInt'
]
df.dropna(subset=['StrikePrice'], inplace=True)

df['ExpirationDate'] = pd.to_datetime(df['ExpirationDate'], format='%a %b %d %Y') + timedelta(hours=16)
for col in ['StrikePrice','CallIV','PutIV','CallGamma','PutGamma','CallOpenInt','PutOpenInt']:
    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

# ── SPOT GAMMA ──────────────────────────────────────────────────────────────
df['CallGEX'] = df['CallGamma'] * df['CallOpenInt'] * 100 * spotPrice**2 * 0.01
df['PutGEX']  = df['PutGamma']  * df['PutOpenInt']  * 100 * spotPrice**2 * 0.01 * -1
df['TotalGamma'] = (df['CallGEX'] + df['PutGEX']) / 1e9

dfAgg   = df.groupby('StrikePrice').sum(numeric_only=True)
strikes = dfAgg.index.values

fig, axes = plt.subplots(1, 2, figsize=(16, 6))

ax = axes[0]
ax.grid()
ax.bar(strikes, dfAgg['TotalGamma'], width=0.5, linewidth=0.1, edgecolor='k', label='Gamma Exposure')
ax.set_xlim([fromStrike, toStrike])
ax.set_title(f"Total Gamma: ${df['TotalGamma'].sum():.2f} Bn per 1% SPY Move", fontweight='bold')
ax.set_xlabel('Strike', fontweight='bold')
ax.set_ylabel('Spot GEX ($ Bn / 1% move)', fontweight='bold')
ax.axvline(spotPrice, color='r', lw=1.5, label=f'SPY Spot: {spotPrice:,.2f}')
ax.legend()

ax = axes[1]
ax.grid()
ax.bar(strikes, dfAgg['CallGEX'] / 1e9, width=0.5, linewidth=0.1, edgecolor='k', label='Call Gamma', color='green', alpha=0.7)
ax.bar(strikes, dfAgg['PutGEX']  / 1e9, width=0.5, linewidth=0.1, edgecolor='k', label='Put Gamma',  color='red',   alpha=0.7)
ax.set_xlim([fromStrike, toStrike])
ax.set_title(f"Calls vs Puts GEX — SPY {todayDate.strftime('%d %b %Y')}", fontweight='bold')
ax.set_xlabel('Strike', fontweight='bold')
ax.set_ylabel('Spot GEX ($ Bn / 1% move)', fontweight='bold')
ax.axvline(spotPrice, color='r', lw=1.5, label=f'SPY Spot: {spotPrice:,.2f}')
ax.legend()

plt.tight_layout()
plt.show()

# ── GAMMA PROFILE ────────────────────────────────────────────────────────────
levels = np.linspace(fromStrike, toStrike, 60)

df['daysTillExp'] = [
    1/262 if np.busday_count(todayDate.date(), x.date()) == 0
    else np.busday_count(todayDate.date(), x.date()) / 262
    for x in df.ExpirationDate
]

nextExpiry     = df['ExpirationDate'].min()
thirdFridays   = df.loc[[isThirdFriday(x) for x in df.ExpirationDate]]
nextMonthlyExp = thirdFridays['ExpirationDate'].min() if not thirdFridays.empty else nextExpiry

totalGamma      = []
totalGammaExNxt = []
totalGammaExFri = []

for level in levels:
    df['cGEX'] = df.apply(lambda r: calcGammaEx(level, r['StrikePrice'], r['CallIV'], r['daysTillExp'], 0, 0, 'call', r['CallOpenInt']), axis=1)
    df['pGEX'] = df.apply(lambda r: calcGammaEx(level, r['StrikePrice'], r['PutIV'],  r['daysTillExp'], 0, 0, 'put',  r['PutOpenInt']),  axis=1)
    totalGamma.append(df['cGEX'].sum() - df['pGEX'].sum())
    exNxt = df[df['ExpirationDate'] != nextExpiry]
    totalGammaExNxt.append(exNxt['cGEX'].sum() - exNxt['pGEX'].sum())
    exFri = df[df['ExpirationDate'] != nextMonthlyExp]
    totalGammaExFri.append(exFri['cGEX'].sum() - exFri['pGEX'].sum())

totalGamma      = np.array(totalGamma)      / 1e9
totalGammaExNxt = np.array(totalGammaExNxt) / 1e9
totalGammaExFri = np.array(totalGammaExFri) / 1e9

# Zero Gamma (Flip) level
zeroCrossIdx = np.where(np.diff(np.sign(totalGamma)))[0]
if len(zeroCrossIdx):
    negG, posG   = totalGamma[zeroCrossIdx], totalGamma[zeroCrossIdx+1]
    negS, posS   = levels[zeroCrossIdx],     levels[zeroCrossIdx+1]
    zeroGamma    = float(posS[0] - (posS[0]-negS[0]) * posG[0]/(posG[0]-negG[0]))
    flipLabel    = f'Gamma Flip: {zeroGamma:,.1f}'
else:
    zeroGamma    = None
    flipLabel    = 'No flip in range'

fig, ax = plt.subplots(figsize=(12, 6))
ax.grid()
ax.plot(levels, totalGamma,      label='All Expiries')
ax.plot(levels, totalGammaExNxt, label='Ex-Next Expiry',         linestyle='--')
ax.plot(levels, totalGammaExFri, label='Ex-Next Monthly Expiry', linestyle=':')
ax.set_title(f"Gamma Exposure Profile — SPY  {todayDate.strftime('%d %b %Y')}", fontweight='bold', fontsize=14)
ax.set_xlabel('SPY Price', fontweight='bold')
ax.set_ylabel('GEX ($ Bn / 1% move)', fontweight='bold')
ax.axvline(spotPrice, color='r',  lw=1.5, label=f'SPY Spot: {spotPrice:,.2f}')
if zeroGamma:
    ax.axvline(zeroGamma, color='g', lw=1.5, label=flipLabel)
    trans = ax.get_xaxis_transform()
    ax.fill_between([fromStrike, zeroGamma], min(totalGamma), max(totalGamma), facecolor='red',   alpha=0.08, transform=trans)
    ax.fill_between([zeroGamma,  toStrike],  min(totalGamma), max(totalGamma), facecolor='green', alpha=0.08, transform=trans)
ax.axhline(0, color='grey', lw=1)
ax.set_xlim([fromStrike, toStrike])
ax.legend()
plt.tight_layout()
plt.show()

print(f"\nSPY Spot:          {spotPrice:,.2f}")
print(f"Total Spot GEX:    ${df['TotalGamma'].sum():.2f} Bn per 1% move")
if zeroGamma:
    print(f"Zero Gamma Level:  {zeroGamma:,.1f}")
    print(f"Current vs Flip:   {'ABOVE (stabilizing)' if spotPrice > zeroGamma else 'BELOW (destabilizing)'}")
