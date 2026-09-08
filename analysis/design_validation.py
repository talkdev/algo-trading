"""Does the proposed patched geometry admit a non-empty, positive-EV set?"""
import math
LOT=65; STEP=50; SESSION_M=375; EXPIRY_M=375; SPOT=23900.0
STT=0.0015; EXCH=0.0003552; SEBI=0.000001; STAMP=0.00003; BROK=20.0
def N(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
def bs(s,k,t,iv,kind):
    sq=iv*math.sqrt(t); d1=(math.log(s/k)+0.5*sq*sq)/sq; d2=d1-sq
    return (s*N(d1)-k*N(d2)) if kind=="c" else (k*N(-d2)-s*N(-d1))
def hs(p): return max(0.05,p*0.005)
def cost(sv,bv,n):
    to=sv+bv; ex=to*EXCH; sb=to*SEBI; br=BROK*n
    return sv*STT+ex+sb+bv*STAMP+br+(br+ex+sb)*0.18
def pnt2(a,s,K=4):
    if s<=0: return 1.0
    z=a/s; return min(1,max(0,sum(((-1)**k)*(N((2*k+1)*z)-N((2*k-1)*z)) for k in range(-K,K+1))))

IV=0.125; RV=0.105; WING=150; STOP_MULT=1.75; TGT=0.50
# credit is priced at IV; touch probability must use FORECAST (realised) vol.
# Using IV for both gives the risk-neutral answer, i.e. EV = -friction.
BE=(STOP_MULT-1)/((STOP_MULT-1)+TGT)
print(f"Proposed: stop {STOP_MULT}x credit, target {TGT:.0%}, NO proximity stop.")
print(f"Credit priced at IV {IV:.1%}; p_win computed at forecast RV {RV:.1%} (VRP {(IV-RV)*100:.1f}pp)")
print(f"Breakeven p_win = {BE:.3f}\n")
print(f"{'entry':<8}{'short':<7}{'credit':<9}{'friction':<10}{'p_win':<8}{'EV pts':<9}{'EV Rs/lot':<11}{'gate'}")
hits=[]
for entry_m,lbl in ((35,"09:50"),(135,"11:30"),(195,"12:30"),(225,"13:00")):
    t=(EXPIRY_M-entry_m)/(252.0*SESSION_M)
    hold=(345-entry_m)/(252.0*SESSION_M)
    sig=RV*math.sqrt(hold)*SPOT
    for d in (150,200,250,300):
        sc,sp=SPOT+d,SPOT-d; lc,lp=sc+WING,sp-WING
        px=[bs(SPOT,sc,t,IV,"c"),bs(SPOT,sp,t,IV,"p"),
            bs(SPOT,lc,t,IV,"c"),bs(SPOT,lp,t,IV,"p")]
        gross=px[0]+px[1]-px[2]-px[3]
        if gross<=0: continue
        si=sum(0.5*hs(p) for p in px); so=sum(3.0*hs(p) for p in px)
        ci=cost((px[0]+px[1])*LOT,(px[2]+px[3])*LOT,4)/LOT
        co=cost((px[2]+px[3])*LOT,(px[0]+px[1])*LOT,4)/LOT
        C=gross-si-ci
        if C<=0: continue
        F=ci+co+si+so
        p=pnt2(d,sig)
        ev=p*(C*TGT)-(1-p)*(C*(STOP_MULT-1))-F
        mev=max(C*0.03,F*0.25)
        ok=ev>=mev
        if ok: hits.append((lbl,d,ev*LOT))
        print(f"{lbl:<8}{d:<7}{C:<9.1f}{F:<10.2f}{p:<8.3f}{ev:<9.2f}{ev*LOT:<11,.0f}{'PASS' if ok else '.'}")
print(f"\n{len(hits)} of 16 combinations pass the patched gate.")
if hits:
    b=max(hits,key=lambda x:x[2])
    print(f"Best: entry {b[0]}, shorts +/-{b[1]}pts -> EV Rs{b[2]:,.0f}/lot")
print("\nNOTE: p_win here is P(no touch of the short strike) with the proximity")
print("stop REMOVED. Leaving the 40pt proximity stop in place drops p_win to")
print("~0.001-0.31 and NOTHING passes - which is why the exit ladder must change")
print("together with the gate, not after it.")
