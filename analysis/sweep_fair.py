"""
Fair test: realised vol held at 10.5% in BOTH worlds.
  benign  = pure diffusion at 10.5%
  jumpy   = diffusion 7.03% + jumps, which also realises 10.5%
Same realised vol, same IV, same VRP. Only the PATH SHAPE differs.
IV is set honestly to realised + intended VRP.
"""
import importlib.util, io, contextlib, random
spec=importlib.util.spec_from_file_location("mc","/home/user/algo-trading/analysis/mc_realistic.py")
mc=importlib.util.module_from_spec(spec)
with contextlib.redirect_stdout(io.StringIO()): spec.loader.exec_module(mc)

RV=0.105; BASE_JUMPY=0.0703
def go(vrp, jumpy, N=3000, seed=11, **kw):
    random.seed(seed)
    iv=RV+vrp
    sig=BASE_JUMPY if jumpy else RV
    res=[r for r in (mc.run(sig,iv,jumpy,**kw) for _ in range(N)) if r]
    p=[r[0] for r in res]
    return (sum(p)/len(p), sum(1 for x in p if x>0)/len(p)*100, min(p), len(p))

print("Realised vol 10.5% in every row. IV = 10.5% + VRP.  Per lot, 0DTE IC.")
print(f"\n{'VRP':<7}{'path':<9}{'win%':<8}{'EV/lot':<11}{'worst'}")
for vrp in (0.01,0.02,0.04):
    for jumpy in (False,True):
        ev,wr,wo,n=go(vrp,jumpy)
        print(f"{vrp*100:<7.0f}{'jumpy' if jumpy else 'smooth':<9}{wr:<8.1f}{ev:<11,.0f}{wo:,.0f}")

print("\n--- ENGINE CONFIG vs PROFESSIONAL CONFIG (jumpy path, honest VRP) ---")
cfgs = {
 "engine: 1.0x dist, 40pt prox, 0.40 delta stop, 50% target":
     dict(dist_scale=1.0, proximity=40, delta_close=0.40, target_pct=0.50, stop_mult=2.5),
 "no proximity stop":
     dict(dist_scale=1.0, proximity=0,  delta_close=0.40, target_pct=0.50, stop_mult=2.5),
 "no prox + delta stop widened to 0.60":
     dict(dist_scale=1.0, proximity=0,  delta_close=0.60, target_pct=0.50, stop_mult=2.5),
 "strikes 1.5x out (~0.15 delta), no prox, 0.60 delta stop":
     dict(dist_scale=1.5, proximity=0,  delta_close=0.60, target_pct=0.50, stop_mult=2.5),
 "1.5x out, premium stop 2.0x only, target 35%":
     dict(dist_scale=1.5, proximity=0,  delta_close=0.99, target_pct=0.35, stop_mult=2.0),
 "1.5x out, premium stop 1.75x only, target 30%":
     dict(dist_scale=1.5, proximity=0,  delta_close=0.99, target_pct=0.30, stop_mult=1.75),
}
for vrp in (0.02,0.04):
    print(f"\n  VRP = +{vrp*100:.0f}pp")
    for name,kw in cfgs.items():
        ev,wr,wo,n=go(vrp,True,**kw)
        print(f"    {name:<58}win {wr:5.1f}%  EV Rs{ev:7,.0f}  worst Rs{wo:8,.0f}")
