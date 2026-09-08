"""
The engine gates every trade on VRP = ATM_IV - Parkinson_RV.
So Parkinson_RV must be UNBIASED or the gate is meaningless.

Test: generate paths with a KNOWN annualised vol, build 1-minute OHLC bars from
sub-minute ticks (as a real feed does), then run the engine's exact estimator:
    rv = sqrt( 1/(4 ln2) * mean(ln(H/L)^2) * 375 * 252 )   over the last 60 bars
and compare to truth.
"""
import math, random, statistics
random.seed(3)
SESSION_M=375; PARK=1.0/(4.0*math.log(2.0))

def engine_rv(bars):
    vals=[math.log(h/l)**2 for h,l in bars if h>l*1.0001]
    if len(vals)<10: return None
    return math.sqrt(PARK*(sum(vals)/len(vals))*375.0*252.0)

def trial(true_ann, ticks_per_min, nbars=60):
    sm=true_ann/math.sqrt(252.0*SESSION_M)      # per-minute sigma
    st=sm/math.sqrt(ticks_per_min)              # per-tick sigma
    s=23900.0; bars=[]
    for _ in range(nbars):
        h=l=s
        for _ in range(ticks_per_min):
            s*=math.exp(st*random.gauss(0,1))
            h=max(h,s); l=min(l,s)
        bars.append((h,l))
    return engine_rv(bars)

print("True ann vol = 10.50%.  Engine estimator over 60 one-minute bars.")
print(f"{'ticks/min':<12}{'mean RV':<12}{'bias':<12}{'std dev':<12}{'p5..p95'}")
for tpm in (2,5,20,60,200,1000):
    xs=[trial(0.105,tpm) for _ in range(600)]
    xs=[x for x in xs if x]
    m=statistics.mean(xs); sd=statistics.pstdev(xs)
    xs.sort(); p5=xs[int(.05*len(xs))]; p95=xs[int(.95*len(xs))]
    print(f"{tpm:<12}{m*100:<12.2f}{(m-0.105)*100:<+12.2f}{sd*100:<12.2f}"
          f"{p5*100:.2f}..{p95*100:.2f}")

print()
print("Interpretation:")
print("  A 1-min bar is built from a FINITE number of ticks. The observed high-low")
print("  range is always <= the true continuous range, so Parkinson reads LOW.")
print("  Reading RV low => VRP = IV - RV reads HIGH => engine sees edge that isn't there.")
print()
print("Sampling noise alone (the std-dev column) is of the same order as the")
print("entire VRP sell threshold, which for DTE0+VERY_NARROW falls to:")
print("   2.5 x 0.75 (DTE0) x 0.75 (VERY_NARROW) = 1.41pp")
