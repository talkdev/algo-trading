"""
Same engine trade, but with the three effects that actually kill intraday
short-gamma books and which a pure-GBM model hides:
  (1) jumps      - NIFTY gaps intraday on news/RBI/global cues
  (2) vol-of-vol - IV EXPANDS when spot moves (short vega bleeds on top of gamma)
  (3) liquidity  - quotes widen 2.5-4x exactly when your stop fires
"""
import math, random
random.seed(11)
LOT=65; STEP=50; SESSION_M=375; EXPIRY_M=375; ENTRY_M=35; HARD=345
STT=0.0015; EXCH=0.0003552; SEBI=0.000001; STAMP=0.00003; BROK=20.0
WING=150

def nd(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
def bs(s,k,t,iv,kind):
    if t<=1e-9 or iv<=0:
        i=max(s-k,0) if kind=="c" else max(k-s,0)
        return i,(1.0 if (kind=="c" and s>k) else (-1.0 if (kind=="p" and s<k) else 0.0))
    sq=iv*math.sqrt(t); d1=(math.log(s/k)+0.5*sq*sq)/sq; d2=d1-sq
    if kind=="c": return s*nd(d1)-k*nd(d2), nd(d1)
    return k*nd(-d2)-s*nd(-d1), nd(d1)-1
def costs(sv,bv,n):
    to=sv+bv
    if to<=0: return 0.0
    ex=to*EXCH; sb=to*SEBI; br=BROK*n
    return sv*STT+ex+sb+bv*STAMP+br+(br+ex+sb)*0.18

def run(sigma, iv0, stressed, proximity=40, delta_close=0.40,
        target_pct=0.50, dist_scale=1.0, stop_mult=2.5):
    sm = sigma/math.sqrt(252.0*SESSION_M)
    spot=23900.0; straddle=None; iv=iv0; spot_ref=23900.0
    def step(s, iv):
        z=random.gauss(0,1)
        s2=s*math.exp(-0.5*sm*sm+sm*z)
        if stressed:
            # (1) jump: ~1 in 250 minutes a 0.25-0.6% jolt
            if random.random()<0.004:
                s2*= math.exp(random.choice([-1,1])*random.uniform(0.0025,0.006))
            # (2) IV expands with realised movement (vol-of-vol, leverage effect)
            move=abs(s2-spot_ref)/spot_ref
            iv2=iv0*(1.0+2.2*move)
            if s2<spot_ref: iv2*=1.0+1.4*move       # put skew bid on selloffs
            iv=max(iv0*0.85, iv2)
        return s2, iv
    for m in range(1,ENTRY_M+1):
        spot,iv=step(spot,iv)
        if m==15:
            t=(EXPIRY_M-m)/(252.0*SESSION_M); atm=round(spot/STEP)*STEP
            straddle=bs(spot,atm,t,iv,"c")[0]+bs(spot,atm,t,iv,"p")[0]
    tm=math.sqrt(max((SESSION_M-ENTRY_M)/SESSION_M,0.05))
    sd=max(int(straddle*tm*dist_scale),max(int(120*tm),55))
    sd=int(round(sd/STEP)*STEP)
    sc=round((spot+sd)/STEP)*STEP; sp=round((spot-sd)/STEP)*STEP
    lc,lp=sc+WING,sp-WING
    specs=[(sc,"c",1),(sp,"p",1),(lc,"c",-1),(lp,"p",-1)]
    def book(s,t,iv):
        g=0.0; px=[]; dcs=dps=0.0
        for k,kind,sgn in specs:
            p,d=bs(s,k,t,iv,kind); px.append(p); g+=sgn*p
            if sgn>0 and kind=="c": dcs=d
            if sgn>0 and kind=="p": dps=d
        return g,px,dcs,dps
    def hs(p, stress):
        w=max(0.05,p*0.005)
        return w*(3.0 if (stressed and stress) else 1.0)
    t0=(EXPIRY_M-ENTRY_M)/(252.0*SESSION_M)
    gross,px0,_,_=book(spot,t0,iv)
    slip_in=sum(0.5*hs(p,False) for p in px0)
    sv=(px0[0]+px0[1])*LOT; bv=(px0[2]+px0[3])*LOT
    cin=costs(sv,bv,4)/LOT
    nc=gross-slip_in-cin
    if nc<=0: return None
    ps=max(int(straddle*0.30),30); psc=sc-ps; psp=sp+ps
    stopp=nc*stop_mult; tgt=nc*(1-target_pct)
    locked=False; lock=None
    for m in range(ENTRY_M+1,HARD+1):
        spot,iv=step(spot,iv)
        t=max((EXPIRY_M-m)/(252.0*SESSION_M),1e-9)
        prem,px,dcs,dps=book(spot,t,iv)
        tag=None
        if abs(dcs)>delta_close or abs(dps)>delta_close: tag="DELTA"
        elif proximity>0 and (abs(spot-sc)<=proximity or abs(spot-sp)<=proximity): tag="PROXIMITY"
        elif spot>=psc or spot<=psp: tag="PRICE_STOP"
        elif prem>=stopp: tag="PREM_STOP"
        else:
            if gross>0:
                pp=(gross-prem)/gross
                if pp>=0.40 and not locked: locked=True; lock=nc
                elif locked and prem>=lock: tag="LOCK_STOP"
            if tag is None:
                eff=tgt
                if m>=255: eff=min(eff,nc*0.60)
                if m>=315: eff=min(eff,nc*0.70)
                if prem<=eff: tag="TARGET"
        if tag:
            st=tag in ("DELTA","PROXIMITY","PRICE_STOP","PREM_STOP")
            so=sum(1.5*hs(p,st) for p in px)
            co=costs((px[2]+px[3])*LOT,(px[0]+px[1])*LOT,4)/LOT
            return (nc-prem-so-co)*LOT, tag
    t=(EXPIRY_M-HARD)/(252.0*SESSION_M)
    prem,px,_,_=book(spot,t,iv)
    so=sum(1.5*hs(p,False) for p in px)
    co=costs((px[2]+px[3])*LOT,(px[0]+px[1])*LOT,4)/LOT
    return (nc-prem-so-co)*LOT, "HARD_EXIT"

def rep(name,res):
    p=[r[0] for r in res]; tg=[r[1] for r in res]; n=len(p)
    w=[x for x in p if x>0]; l=[x for x in p if x<=0]
    ev=sum(p)/n
    print(f"\n{name}")
    print(f"   win {len(w)/n*100:5.1f}%   avgW Rs{(sum(w)/len(w) if w else 0):7,.0f}   "
          f"avgL Rs{(sum(l)/len(l) if l else 0):8,.0f}   EV/lot Rs{ev:7,.0f}   worst Rs{min(p):8,.0f}")
    d={t:tg.count(t) for t in set(tg)}
    print("   exits: "+", ".join(f"{k}={v/n*100:.0f}%" for k,v in sorted(d.items(),key=lambda x:-x[1])))
    return ev

N=4000
print("NIFTY 0DTE IC | spot 23,900 | realised 10.5% | ATM IV 12.5% (VRP +2.0pp) | lot 65")
print("="*78)
rep("BENIGN GBM      (engine's implicit world)",
    [r for r in (run(0.105,0.125,False) for _ in range(N)) if r])
rep("REALISTIC       (jumps + IV expansion + spread widening on stops)",
    [r for r in (run(0.105,0.125,True) for _ in range(N)) if r])
rep("REALISTIC, VRP +4pp (IV 14.5%) - a genuinely rich day",
    [r for r in (run(0.105,0.145,True) for _ in range(N)) if r])
rep("REALISTIC, VRP 0pp  (IV 10.5%) - engine still trades if RV mis-measured",
    [r for r in (run(0.105,0.105,True) for _ in range(N)) if r])
