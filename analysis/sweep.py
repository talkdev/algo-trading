import importlib.util, sys, io, contextlib
spec=importlib.util.spec_from_file_location("mc","/home/user/algo-trading/analysis/mc_realistic.py")
mc=importlib.util.module_from_spec(spec)
with contextlib.redirect_stdout(io.StringIO()):
    spec.loader.exec_module(mc)
import random
N=3000
print("Entry delta of engine's own pick (100pt OTM, 09:50, IV12.5%):",
      f"{abs(mc.bs(23900,24000,340/(252*375),0.125,'c')[1]):.3f}")
print()
print("REALISTIC model (jumps + IV expansion + stress spreads), VRP +2pp, per lot")
print(f"{'dist':<7}{'prox':<7}{'dstop':<8}{'tgt':<7}{'stop':<7}{'win%':<8}{'EV/lot':<10}{'worst'}")
best=None
for dist in (1.0,1.4,1.8,2.2):
  for prox,dstop in ((40,0.40),(0,0.40),(0,0.60),(0,0.99)):
    for tgt in (0.25,0.35,0.50):
      for sm in (2.0,2.5):
        random.seed(11)
        res=[r for r in (mc.run(0.105,0.125,True,proximity=prox,delta_close=dstop,
                                target_pct=tgt,dist_scale=dist,stop_mult=sm)
                         for _ in range(N)) if r]
        if not res: continue
        p=[r[0] for r in res]; ev=sum(p)/len(p)
        wr=sum(1 for x in p if x>0)/len(p)*100
        row=(dist,prox,dstop,tgt,sm,wr,ev,min(p))
        if best is None or ev>best[6]: best=row
        if ev>-40:
            print(f"{dist:<7}{prox:<7}{dstop:<8}{tgt:<7}{sm:<7}{wr:<8.1f}{ev:<10,.0f}{min(p):,.0f}")
print()
print("BEST:",f"dist={best[0]}x prox={best[1]} deltastop={best[2]} target={best[3]} "
      f"stop={best[4]}x -> win {best[5]:.1f}% EV Rs{best[6]:,.0f}/lot worst Rs{best[7]:,.0f}")
