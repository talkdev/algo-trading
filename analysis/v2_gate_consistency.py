"""Internal consistency of the v2 gates."""
import math
LOT=65; STEP=50; EXPIRY_M=375
STT=0.0015; EXCH=0.0003552; SEBI=0.000001; STAMP=0.00003; BROK=20.0
def nd(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
def bs(s,k,t,iv,kind):
    sq=iv*math.sqrt(t); d1=(math.log(s/k)+0.5*sq*sq)/sq; d2=d1-sq
    return (s*nd(d1)-k*nd(d2)) if kind=="c" else (k*nd(-d2)-s*nd(-d1))
def hs(p): return max(0.05,p*0.005)
def cost(sv,bv,n):
    to=sv+bv; ex=to*EXCH; sb=to*SEBI; br=BROK*n
    return sv*STT+ex+sb+bv*STAMP+br+(br+ex+sb)*0.18

s=23900.0; iv=0.125; t=340/(252*375); wing=150; d=100
sc,sp=s+d,s-d; lc,lp=sc+wing,sp-wing
px=[bs(s,sc,t,iv,"c"),bs(s,sp,t,iv,"p"),bs(s,lc,t,iv,"c"),bs(s,lp,t,iv,"p")]
gross=px[0]+px[1]-px[2]-px[3]
slip_in=sum(0.5*hs(p) for p in px)
slip_out_v2=sum(3.0*hs(p) for p in px)        # the NEW exit model
c_in=cost((px[0]+px[1])*LOT,(px[2]+px[3])*LOT,4)/LOT
c_out=cost((px[2]+px[3])*LOT,(px[0]+px[1])*LOT,4)/LOT

gate_friction = (c_in + slip_in) * 2.0        # what _compute_ev_gate uses
true_friction = c_in + c_out + slip_in + slip_out_v2

print("FRICTION USED BY THE GATE vs FRICTION THE ENGINE ITSELF NOW MODELS")
print(f"  entry costs        {c_in:6.2f}   exit costs        {c_out:6.2f}")
print(f"  entry slippage     {slip_in:6.2f}   exit slippage(3x) {slip_out_v2:6.2f}")
print(f"  gate friction = (entry_costs+entry_slip) x 2.0 = {gate_friction:6.2f} pts")
print(f"  true round trip under the v2 model            = {true_friction:6.2f} pts")
print(f"  UNDERSTATED BY {true_friction-gate_friction:.2f} pts "
      f"({(true_friction/gate_friction-1)*100:.0f}%)")
print(f"\n  The x2.0 multiplier assumes exit slippage == entry slippage.")
print(f"  The v2 patch made exit slippage 6x entry (3.0 vs 0.5 half-spreads),")
print(f"  so the correct multiplier is now ~3.5x, not 2.0x. The patch changed")
print(f"  the model but not the gate that consumes it.\n")

print("="*72)
print("REQUIRED NET CREDIT, gate friction vs true friction (DTE0 VERY_NARROW, VRP>4)")
p=0.80; tgt=0.50
slope=p*tgt-(1-p)*1.5
for name,F in (("gate friction (as coded)",gate_friction),("true v2 friction",true_friction)):
    c=max(F/(slope-0.03),(F+0.25*F)/slope)
    print(f"  {name:<28} need net credit >= {c:5.1f} pts")
print(f"\n  Achievable net credit at these strikes: {gross-slip_in-c_in:.1f} pts")
print("\n" + "="*72)
print("GATE DISAGREEMENT")
print("  regime_engine: vrp_sell floor = 2.0pp  -> emits SELL_PREMIUM at 2.0pp")
print("  regime_engine: STRONG_SELL     = 2.6pp")
print("  strategy_engine EV gate        -> needs vrp_adj=+0.08, i.e. VRP > 4.0pp")
print("  => every cycle with VRP in 2.0-4.0pp is greenlit by the regime engine")
print("     and then silently killed by the EV gate. Operator sees NO_TRADE.")
print("  Parkinson x1.15 over-corrects ~0.6pp at 1/sec index dissemination,")
print("  so a MEASURED 4.0pp needs a TRUE VRP of roughly 4.6pp.")
