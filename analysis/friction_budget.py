"""Where does the theta actually go? Round-trip friction vs captured edge."""
import math
LOT=65; STEP=50; SESSION_M=375; EXPIRY_M=375
STT=0.0015; EXCH=0.0003552; SEBI=0.000001; STAMP=0.00003; BROK=20.0

def nd(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
def bs(s,k,t,iv,kind):
    if t<=0: 
        return (max(s-k,0) if kind=="c" else max(k-s,0)), 0
    sq=iv*math.sqrt(t); d1=(math.log(s/k)+0.5*sq*sq)/sq; d2=d1-sq
    if kind=="c": return s*nd(d1)-k*nd(d2), nd(d1)
    return k*nd(-d2)-s*nd(-d1), nd(d1)-1
def hs(p): return max(0.05,p*0.005)
def costs(sv,bv,n):
    to=sv+bv
    if to<=0: return 0.0
    stt=sv*STT; ex=to*EXCH; sb=to*SEBI; st=bv*STAMP; br=BROK*n
    return stt+ex+sb+st+br+(br+ex+sb)*0.18

def analyse(label, spot, iv, entry_m, short_dist, wing, n_legs_short=2):
    t=(EXPIRY_M-entry_m)/(252.0*SESSION_M)
    sc=round((spot+short_dist)/STEP)*STEP; sp=round((spot-short_dist)/STEP)*STEP
    lc,lp=sc+wing,sp-wing
    if n_legs_short==2:
        specs=[(sc,"c",1),(sp,"p",1),(lc,"c",-1),(lp,"p",-1)]
    else:  # single credit spread (bear call)
        specs=[(sc,"c",1),(lc,"c",-1)]
    px={}; gross=0.0; sv=bv=0.0
    for k,kind,sgn in specs:
        p,_=bs(spot,k,t,iv,kind); px[(k,kind)]=p
        gross+=sgn*p
        if sgn>0: sv+=p*LOT
        else: bv+=p*LOT
    nlegs=len(specs)
    slip_in=sum(0.5*hs(p) for p in px.values())
    slip_out=sum(1.5*hs(p) for p in px.values())
    c_in=costs(sv,bv,nlegs)/LOT
    c_out=costs(bv,sv,nlegs)/LOT
    friction=slip_in+slip_out+c_in+c_out
    print(f"\n{label}")
    print(f"  gross credit        {gross:7.2f} pts   (Rs {gross*LOT:8,.0f} /lot)")
    print(f"  entry slippage      {slip_in:7.2f} pts")
    print(f"  exit  slippage      {slip_out:7.2f} pts")
    print(f"  entry costs         {c_in:7.2f} pts")
    print(f"  exit  costs         {c_out:7.2f} pts")
    print(f"  ROUND-TRIP FRICTION {friction:7.2f} pts   (Rs {friction*LOT:8,.0f} /lot)")
    print(f"  friction / credit   {friction/gross*100:6.1f}%")
    print(f"  credit kept at 50% target: {gross*0.5-friction:6.2f} pts "
          f"-> Rs {(gross*0.5-friction)*LOT:,.0f} /lot")
    print(f"  breakeven target: must capture {friction/gross*100:.0f}% of credit "
          f"just to cover friction")
    return friction/gross

print("NIFTY 23,900 | 0DTE Tuesday | ATM IV 12.5% | lot 65 | 2026 STT 0.15%")
print("="*70)
analyse("A. Iron condor, shorts +/-100pts, 150pt wings  [engine's actual pick @09:50]",
        23900, 0.125, 35, 100, 150)
analyse("B. Iron condor, shorts +/-150pts (~0.12 delta), 150pt wings",
        23900, 0.125, 35, 150, 150)
analyse("C. Iron condor, shorts +/-200pts (~0.06 delta), 150pt wings",
        23900, 0.125, 35, 200, 150)
analyse("D. SINGLE bear-call spread +150/+300 (2 legs, half the friction)",
        23900, 0.125, 35, 150, 150, n_legs_short=1)
analyse("E. Iron condor entered 10:45 on DTE-1 (Monday), shorts +/-150",
        23900, 0.125, -285, 150, 150)
