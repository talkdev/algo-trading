LOT=65; SPOT=23900.0
print("A) POSITION-SIZING GATE  (strategy_engine.compute_params, lines ~1063-1098)")
print("   risk_pct_map = {0:0.005, 1:0.004, 2:0.003}")
print("   structural_loss_per_lot = max((wing - net_credit)*65, net_credit*65)")
print("   IRON_CONDOR is REJECTED unless raw_lots*size_mult >= 1.0, and is then forced to 2 lots.\n")
print(f"   {'capital':>12} {'DTE':>4} {'max_risk':>10} {'loss/lot':>10} {'raw_lots':>9} {'verdict'}")
for cap in (1_000_000, 1_560_000, 2_000_000, 3_120_000, 5_000_000):
    for dte,rp in ((0,0.005),(1,0.004)):
        credit=33.0; wing=150
        loss=max((wing-credit)*LOT, credit*LOT)
        mr=cap*rp; raw=mr/loss
        # size_mult is <=1 in practice (vix*conf*dte*event multipliers)
        v = "IC BLOCKED (raw<1)" if raw<1.0 else ("1 lot -> forced to 2" if raw<2 else f"{int(raw)} lots")
        print(f"   {cap:>12,} {dte:>4} {mr:>10,.0f} {loss:>10,.0f} {raw:>9.2f}  {v}")
print("\n   => at the shipped STARTING_CAPITAL=1,000,000 the engine's primary")
print("      structure can never be sized. Min capital for 1 lot ~= Rs15.6L,")
print("      and the hard 2-lot minimum needs ~Rs31.2L (DTE0), Rs39L (DTE1).")
print("      And that is BEFORE size_mult (vix x confidence x dte x event) < 1.0.")

print("\n\nB) MARGIN MODEL  (strategy_engine line 1106: wing * 65 * 1.10)")
wing=150; credit=33.0
engine_m = wing*LOT*1.10
span_like = (wing-credit)*LOT
notional  = SPOT*LOT
elm_expiry= 0.02*notional
print(f"   engine estimate                      Rs {engine_m:10,.0f} /lot")
print(f"   defined-risk SPAN component (approx) Rs {span_like:10,.0f} /lot")
print(f"   contract notional (23,900 x 65)      Rs {notional:10,.0f}")
print(f"   SEBI expiry-day additional ELM @2%   Rs {elm_expiry:10,.0f} per SHORT leg")
print(f"   iron condor has 2 short legs         Rs {2*elm_expiry:10,.0f} /lot on expiry day")
print(f"   REALISTIC 0DTE margin                Rs {span_like+2*elm_expiry:10,.0f} /lot")
print(f"   understatement factor                {(span_like+2*elm_expiry)/engine_m:10.1f}x")
print("\n   Return-on-margin consequence, 2 lots, credit 33pts, 50% target:")
gross=credit*0.5*LOT*2
print(f"     modelled margin  Rs {engine_m*2:>9,.0f}  -> gross target profit Rs {gross:,.0f}"
      f"  = {gross/(engine_m*2)*100:.1f}%")
real=(span_like+2*elm_expiry)*2
print(f"     real 0DTE margin Rs {real:>9,.0f}  -> gross target profit Rs {gross:,.0f}"
      f"  = {gross/real*100:.1f}%")
print(f"\n   With MAX_CONCURRENT_POSITIONS=1 and Rs{real:,.0f} blocked, a Rs10L account")
print(f"   cannot even fund 2 lots on expiry day (needs Rs{real:,.0f}).")
