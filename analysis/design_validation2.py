"""Sweep VRP x structure with an honest gate. What set does a CORRECT engine admit?"""
import math
LOT=65; SESSION_M=375; EXPIRY_M=375; SPOT=23900.0
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
    z=a/s; return min(1,max(0,sum(((-1)**k)*(N((2*k+1)*z)-N((2*k-1)*z)) for k in range(-K,K+1))))
def pnt1(a,s): return max(0.0,1.0-2.0*N(-a/s))

RV=0.105; WING=150; STOP=1.75; TGT=0.50
BE=(STOP-1)/((STOP-1)+TGT)

def eval_struct(kind, entry_m, d, iv):
    t=(EXPIRY_M-entry_m)/(252.0*SESSION_M)
    sig=RV*math.sqrt((345-entry_m)/(252.0*SESSION_M))*SPOT
    sc,sp=SPOT+d,SPOT-d; lc,lp=sc+WING,sp-WING
    if kind=="IC":
        px=[bs(SPOT,sc,t,iv,"c"),bs(SPOT,sp,t,iv,"p"),bs(SPOT,lc,t,iv,"c"),bs(SPOT,lp,t,iv,"p")]
        gross=px[0]+px[1]-px[2]-px[3]; n=4; p=pnt2(d,sig)
        sv=(px[0]+px[1])*LOT; bv=(px[2]+px[3])*LOT
    else:
        px=[bs(SPOT,sc,t,iv,"c"),bs(SPOT,lc,t,iv,"c")]
        gross=px[0]-px[1]; n=2; p=pnt1(d,sig)
        sv=px[0]*LOT; bv=px[1]*LOT
    if gross<=0: return None
    si=sum(0.5*hs(x) for x in px); so=sum(3.0*hs(x) for x in px)
    ci=cost(sv,bv,n)/LOT; co=cost(bv,sv,n)/LOT
    C=gross-si-ci
    if C<=0: return None
    F=ci+co+si+so
    ev=p*(C*TGT)-(1-p)*(C*(STOP-1))-F
    return C,F,p,ev,ev>=max(C*0.03,F*0.25)

print(f"Honest gate: stop {STOP}x, target {TGT:.0%}, no proximity stop, BE p_win {BE:.2f}")
print(f"Forecast RV {RV:.1%}. Credit priced at IV = RV + VRP.\n")
for kind,label in (("IC","4-leg IRON CONDOR"),("CS","2-leg CREDIT SPREAD")):
    print(f"--- {label} ---")
    print(f"{'VRP':<6}{'entry':<8}{'short':<7}{'credit':<9}{'fric':<7}{'p_win':<8}{'EV Rs/lot':<11}{'gate'}")
    best=None
    for vrp in (0.02,0.03,0.04,0.05):
        iv=RV+vrp
        for entry_m,lbl in ((35,"09:50"),(135,"11:30"),(195,"12:30")):
            for d in (150,200,250,300):
                r=eval_struct(kind,entry_m,d,iv)
                if not r: continue
                C,F,p,ev,ok=r
                if best is None or ev>best[0]: best=(ev,vrp,lbl,d,C,p)
                if ok:
                    print(f"{vrp*100:<6.0f}{lbl:<8}{d:<7}{C:<9.1f}{F:<7.2f}{p:<8.3f}{ev*LOT:<11,.0f}PASS")
    print(f"  best overall: EV Rs{best[0]*LOT:,.0f}/lot at VRP {best[1]*100:.0f}pp, "
          f"{best[2]}, {best[3]}pt shorts, credit {best[4]:.1f}pts, p_win {best[5]:.3f}\n")
