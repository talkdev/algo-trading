Dimension | What it measures | Why it matters for trading 
Volatility regime | Is premium rich or cheap vs realized vol | Whether to sell or not sell 
Price regime | Is market trending or ranging | Which structure to use 
Positioning regime | Where is OI concentrated, which side is crowded | Where to place strikes 
Confidence | How much agreement across signals | How much size to deploy

Is price regime RANGE or DOWNTREND or UPTREND?
  RANGE → sell both sides (condor/fly)
  DOWNTREND → sell calls only (bear call spread)
  UPTREND → sell puts only (bull put spread)
  CHOPPY → no trade
  OBSERVING → no trade (OR not established)

Is vol regime SELL_PREMIUM or STRONG_SELL_PREMIUM?
  Yes → proceed
  NEUTRAL → no trade (premium not rich enough)
  BUY_OPTIONS → no trade (premium cheap, don't sell)
  ABORT → block new entries (genuine emergency only)

Is positioning regime supportive?
  RANGE/STRONG_RANGE → condor
  BULLISH → bull put spread
  BEARISH → bear call spread
  UNCLEAR → smaller size or no trade


Final size = base_size × vix_mult × confidence_mult × dte_mult × event_mult
vix_mult:
  VIX < 12.5  (SUPPRESSED) → 1.0  ← 2026 normal, full size
  VIX 12.5-16 (LOW)        → 1.0
  VIX 16-22   (NORMAL)     → 0.75
  VIX 22-28   (ELEVATED)   → 0.50
  VIX > 28    (HIGH)       → 0.25
confidence_mult:
  HIGH   → 1.0
  MEDIUM → 0.5
  LOW    → 0.25
dte_mult:
  DTE 0 (Tuesday) → 0.80 (fastest theta, sized up)
  DTE 1 (Monday)  → 0.55 (good theta, normal)
  DTE 2-5         → 0.25 (minimal theta, minimal size)
event_mult:
  Event day → 0.25 (defined risk only)
  Normal    → 1.0
  

REGIME ENGINE OUTPUT — COMPLETE STRATEGY DECISION TREE
NIFTY Intraday Options Engine v2.0 | 2026 Weekly Tuesday Expiry | Lot 65 | Defined Risk Only
═══════════════════════════════════════════════════════════════════════════════════════════════

LEGEND:
  ✅ ACTIVE    = Implemented and tradeable in this engine
  🔧 PARTIAL   = Logic exists, needs wiring
  📋 PLANNED   = Should be added, data available
  ❌ EXCLUDED  = Requires infrastructure not in this engine
  🚫 DISABLED  = Valid strategy, disabled for 2026 VIX 11 environment

═══════════════════════════════════════════════════════════════════════════════════════════════

REGIME ENGINE OUTPUT
│
├── OBSERVING ──────────────────────────────────────────────────────────────────────────────────
│   Condition: OR not yet established (before 09:30 or < 10 bars in 09:15-09:30 window)
│   Action: NO TRADE — wait for OR
│   Strategies that apply here: NONE
│   Strategies excluded:
│     ❌ All entries blocked until OR is real
│   Note: 0DTE ORB Strategy, Opening Range Breakout Option Strategy
│         are WAITING here — they fire after OR establishes
│
├── CHOPPY ──────────────────────────────────────────────────────────────────────────────────────
│   Condition: OR established BUT ≥3 wick-throughs without close confirmation in last 10min
│   Action: NO TRADE — market indecisive, fake breakouts
│   Strategies that apply here: NONE
│   Strategies excluded:
│     ❌ All sell strategies (undefined range = undefined risk)
│     ❌ ATM Straddle Scalping (requires delta hedging not available)
│     ❌ Gamma Scalping (requires futures for delta hedging)
│     ❌ Expiry-Day Gamma Scalping (same)
│
├── ABORT ───────────────────────────────────────────────────────────────────────────────────────
│   Condition: VIX up ≥15% from previous close AND VIX ≥ 14
│              OR VIX ≥ 24 (extreme absolute level)
│              OR VIX data unavailable for ≥5 consecutive cycles
│   Action: BLOCK ALL NEW ENTRIES
│   Existing positions: manage by delta/spot/time rules ONLY
│     — Short leg delta > 0.40 → close that leg
│     — Spot within 40pts of short strike → close position
│     — Hard exit time reached → close all
│     — DO NOT close on regime label alone
│   Strategies that apply here:
│     📋 Tail-Risk Hedging (buy OTM puts as hedge — not implemented)
│     📋 Collar (protect existing position — not implemented)
│   Strategies excluded:
│     ❌ All new entries blocked
│
├── Vol = NEUTRAL ───────────────────────────────────────────────────────────────────────────────
│   Condition: VRP between fair threshold (1.2pp) and sell threshold (2.5pp)
│              Premium exists but not rich enough to clear costs
│   Action: NO TRADE
│   Strategies excluded:
│     ❌ All sell strategies (edge < round-trip costs)
│   Note: This is the correct gate. Do not override with VIX level.
│
├── Vol = BUY_OPTIONS ──────────────────────────────────────────────────────────────────────────
│   Condition: VRP < 0 (realized vol > implied vol) OR straddle cheap vs expected remaining move
│   Action: 🚫 DISABLED for 2026 VIX 11 environment
│            (intraday buying bleeds theta, VIX 11 rarely produces cheap enough vol)
│   If re-enabled in future high-VIX environment:
│   │
│   ├── Price = RANGE
│   │   ├── DTE 0 → 🚫 Long Straddle (disabled — theta too fast on 0DTE)
│   │   ├── DTE 1 → 🚫 Long Strangle (disabled)
│   │   └── DTE 2-5 → 🚫 Long Iron Condor / Long Iron Fly (disabled)
│   │
│   ├── Price = DOWNTREND
│   │   ├── DTE 0/1 → 🚫 Long Put (disabled)
│   │   ├── DTE 0/1 → 🚫 Bear Put Spread / Put Debit Spread (disabled)
│   │   ├── DTE 0 → 🚫 0DTE Momentum (disabled)
│   │   └── DTE 1 → 🚫 Breakout Straddle (disabled)
│   │
│   └── Price = UPTREND
│       ├── DTE 0/1 → 🚫 Long Call (disabled)
│       ├── DTE 0/1 → 🚫 Bull Call Spread / Call Debit Spread (disabled)
│       ├── DTE 0 → 🚫 0DTE Momentum (disabled)
│       └── DTE 1 → 🚫 Volatility Expansion Straddle (disabled)
│
│   Strategies permanently excluded (require infrastructure):
│     ❌ Covered Call (requires holding underlying)
│     ❌ Protective Put (requires holding underlying)
│     ❌ Synthetic Long/Short Stock (requires futures)
│     ❌ Synthetic Long/Short Call/Put (requires futures)
│     ❌ Gamma Scalping (requires real-time delta hedging)
│
└── Vol = SELL_PREMIUM or STRONG_SELL_PREMIUM ─────────────────────────────────────────────────
    Condition: VRP > sell threshold (2.5pp) AND IV behavior STABLE or DECLINING
               AND day_move_used < 55% of opening straddle
    Action: PROCEED TO STRUCTURE SELECTION
    │
    ├── Price = RANGE ──────────────────────────────────────────────────────────────────────────
    │   Condition: ADX < 25, spot inside OR ±20pts, ORB regime = RANGE
    │   │
    │   ├── Positioning = STRONG_RANGE ──────────────────────────────────────────────────────
    │   │   Condition: OI walls strong both sides (strength ≥ 2.5)
    │   │              AND PCR 0.8-1.3 (neutral)
    │   │              AND OI building (oi_change > 0.08)
    │   │   │
    │   │   ├── DTE = 0 (Tuesday expiry) ─────────────────────────────────────────────────
    │   │   │   │
    │   │   │   ├── OR = VERY_NARROW (< 50pts) AND ADX < 15 AND |spot-ATM| < 30pts
    │   │   │   │   ├── Time 09:50-11:30 → ✅ Iron Fly (Expiry-Day Iron Fly)
    │   │   │   │   │     Shorts: ATM call + ATM put
    │   │   │   │   │     Wings: ATM ± 100pts
    │   │   │   │   │     Min credit: 28pts | Lots: 2
    │   │   │   │   │     Also known as: Expiry-Day Short Straddle (with wings)
    │   │   │   │   │                    Delta-Neutral Straddle, ATM Straddle Scalping
    │   │   │   │   │                    Gamma-Theta Optimization, Vega-Theta Optimization
    │   │   │   │   └── Time 11:30-12:30 → ✅ Iron Condor (wider shorts)
    │   │   │   │         (fly too risky after 11:30 — gamma accelerates)
    │   │   │   │
    │   │   │   ├── OR = NARROW (50-100pts) AND ADX < 20
    │   │   │   │   ├── Time 09:50-12:30 → ✅ Iron Condor (Expiry-Day Iron Condor)
    │   │   │   │   │     Shorts: spot ± 1.0× opening straddle (floor 120pts)
    │   │   │   │   │     Wings: shorts ± 150pts
    │   │   │   │   │     Min credit: 25pts | Lots: 2
    │   │   │   │   │     Also known as: Expiry-Day Short Strangle (with wings)
    │   │   │   │   │                    OTM Strangle Scalping, Expected Move Strategy
    │   │   │   │   │                    Delta-Neutral Option Selling, VRP Strategy
    │   │   │   │   │                    IV Rank Strategy, IV Percentile Strategy
    │   │   │   │   │                    Mean-Reversion Option Selling
    │   │   │   │   │                    Support-Resistance Option Selling
    │   │   │   │   │                    OI-Based Option Strategy
    │   │   │   │   │                    Systematic Delta-Hedged Short Volatility
    │   │   │   │   │                    Adaptive VRP Strategy, IV-RV Arbitrage
    │   │   │   │   │                    Dynamic Iron Condor, Multi-Leg Adaptive Strategy
    │   │   │   │   │                    Regime-Based Option Strategy
    │   │   │   │   │                    Price-Volatility Regime Matrix
    │   │   │   │   │                    0DTE Delta-Neutral Strategy
    │   │   │   │   └── Time 13:00-15:00 (max pain pin) → 🔧 Expiry-Day Iron Condor
    │   │   │   │         Condition: spot within 50pts of max pain
    │   │   │   │         Action: hold existing OR sell far side only
    │   │   │   │         Also known as: Max Pain Strategy, Expiry-Day Iron Condor
    │   │   │   │                        0DTE Mean Reversion, 0DTE VWAP Strategy
    │   │   │   │                        0DTE ORB Strategy
    │   │   │   │
    │   │   │   └── OR = MODERATE (100-150pts) AND ADX < 20
    │   │   │       └── Time 09:50-12:30 → ✅ Iron Condor (wider shorts)
    │   │   │             Shorts: spot ± 0.85× opening straddle
    │   │   │             Wings: shorts ± 150pts
    │   │   │             Min credit: 22pts | Lots: 2
    │   │   │
    │   │   ├── DTE = 1 (Monday) ──────────────────────────────────────────────────────────
    │   │   │   │
    │   │   │   ├── OR = VERY_NARROW (< 50pts) AND ADX < 15
    │   │   │   │   └── Time 10:45-13:45 → ✅ Iron Condor
    │   │   │   │         (NOT fly on DTE1 — gamma risk too high for 4hr hold)
    │   │   │   │         Shorts: spot ± 0.85× opening straddle
    │   │   │   │         Wings: shorts ± 150pts
    │   │   │   │         Min credit: 22pts | Lots: 2
    │   │   │   │
    │   │   │   ├── OR = NARROW (50-100pts) AND ADX < 20
    │   │   │   │   └── Time 10:45-13:45 → ✅ Iron Condor
    │   │   │   │         Shorts: spot ± 0.85× opening straddle (floor 150pts)
    │   │   │   │         Wings: shorts ± 150pts
    │   │   │   │         Min credit: 22pts | Lots: 2
    │   │   │   │         Also known as: Mean-Reversion Option Selling
    │   │   │   │                        VRP Strategy, IV Rank Strategy
    │   │   │   │                        Support-Resistance Option Selling
    │   │   │   │                        Dynamic Iron Condor
    │   │   │   │
    │   │   │   └── OR = MODERATE (100-150pts) AND ADX < 20
    │   │   │       └── Time 10:45-13:00 → ✅ Iron Condor (reduced size)
    │   │   │             Size multiplier: 0.75 (wider OR = more risk)
    │   │   │
    │   │   └── DTE = 2-5 (Wednesday-Friday) → NO TRADE
    │   │         Reason: same-day theta < round-trip costs on 4-leg structure
    │   │         Exception: DTE 2 + STRONG_SELL_PREMIUM + STRONG_RANGE
    │   │           → ✅ Iron Condor at 0.25 size (minimal)
    │   │
    │   ├── Positioning = RANGE ──────────────────────────────────────────────────────────────
    │   │   Condition: OI walls present but not strong
    │   │              PCR 0.7-1.4 (mild bias)
    │   │              OI neutral (oi_change between -0.08 and +0.08)
    │   │   │
    │   │   ├── DTE = 0 ──────────────────────────────────────────────────────────────────
    │   │   │   ├── OR NARROW + ADX < 20 → ✅ Iron Condor
    │   │   │   │     Same parameters as STRONG_RANGE but reduced size (×0.75)
    │   │   │   ├── Time > 13:00 + near max pain → 🔧 Iron Condor (pin trade)
    │   │   │   │     Also known as: Max Pain Strategy
    │   │   │   └── OR WIDE → NO TRADE
    │   │   │
    │   │   ├── DTE = 1 ──────────────────────────────────────────────────────────────────
    │   │   │   ├── OR NARROW + ADX < 20 → ✅ Iron Condor (size ×0.75)
    │   │   │   └── OR MODERATE/WIDE → NO TRADE
    │   │   │
    │   │   └── DTE = 2-5 → NO TRADE
    │   │
    │   ├── Positioning = BULLISH ──────────────────────────────────────────────────────────
    │   │   Condition: PCR < 0.72 (extreme greed / call buying)
    │   │              OR support OI >> resistance OI (support wall stronger)
    │   │              OR skew_ratio < 0.95 (calls more expensive than puts)
    │   │   │
    │   │   ├── DTE = 0 ──────────────────────────────────────────────────────────────────
    │   │   │   ├── Spot above OR midpoint → ✅ Bull Put Spread
    │   │   │   │     Short put: spot - 1.0× straddle (floor 120pts below)
    │   │   │   │     Long put: short - 150pts
    │   │   │   │     Min credit: 20pts | Lots: 2
    │   │   │   │     Also known as: Put Credit Spread, OI-Based Option Strategy
    │   │   │   │                    PCR-Based Strategy, Mean-Reversion Option Selling
    │   │   │   ├── Spot below OR midpoint → ✅ Bear Call Spread
    │   │   │   │     (spot below OR overrides bullish positioning)
    │   │   │   └── STRONG_SELL_PREMIUM + high confidence
    │   │   │       → 📋 Jade Lizard (Bull Put Spread + Bear Call Spread without upside risk)
    │   │   │             Not implemented — requires two-leg management
    │   │   │
    │   │   ├── DTE = 1 ──────────────────────────────────────────────────────────────────
    │   │   │   ├── Spot above OR midpoint → ✅ Bull Put Spread
    │   │   │   │     Short put: 0.19-0.22 delta (≈150-180pts below spot)
    │   │   │   │     Long put: short - 150pts
    │   │   │   │     Min credit: 18pts | Lots: 2
    │   │   │   └── Spot below OR midpoint → ✅ Bear Call Spread
    │   │   │
    │   │   └── DTE = 2-5 → NO TRADE (exception: DTE 2 + STRONG_SELL_PREMIUM → Bull Put at 0.25 size)
    │   │
    │   ├── Positioning = BEARISH ──────────────────────────────────────────────────────────
    │   │   Condition: PCR > 1.28 (fear / put buying)
    │   │              OR resistance OI >> support OI (resistance wall stronger)
    │   │              OR skew_ratio > 1.40 (puts much more expensive than calls)
    │   │   │
    │   │   ├── DTE = 0 ──────────────────────────────────────────────────────────────────
    │   │   │   ├── Spot below OR midpoint → ✅ Bear Call Spread
    │   │   │   │     Short call: spot + 1.0× straddle (floor 120pts above)
    │   │   │   │     Long call: short + 150pts
    │   │   │   │     Min credit: 20pts | Lots: 2
    │   │   │   │     Also known as: Call Credit Spread, OI Unwinding Strategy
    │   │   │   │                    PCR-Based Strategy, Trend-Following Option Strategy
    │   │   │   ├── Spot above OR midpoint → ✅ Bull Put Spread
    │   │   │   │     (spot above OR overrides bearish positioning)
    │   │   │   └── STRONG_SELL_PREMIUM + high confidence
    │   │   │       → 📋 Reverse Jade Lizard (Bear Call Spread + Bull Put Spread without downside risk)
    │   │   │             Not implemented
    │   │   │
    │   │   ├── DTE = 1 ──────────────────────────────────────────────────────────────────
    │   │   │   ├── Spot below OR midpoint → ✅ Bear Call Spread
    │   │   │   │     Short call: 0.19-0.22 delta (≈150-180pts above spot)
    │   │   │   │     Long call: short + 150pts
    │   │   │   │     Min credit: 18pts | Lots: 2
    │   │   │   └── Spot above OR midpoint → ✅ Bull Put Spread
    │   │   │
    │   │   └── DTE = 2-5 → NO TRADE (exception: DTE 2 + STRONG_SELL_PREMIUM → Bear Call at 0.25 size)
    │   │
    │   └── Positioning = UNCLEAR ─────────────────────────────────────────────────────────
    │       Condition: Mixed signals, no clear OI wall dominance
    │       ├── DTE = 0 + STRONG_SELL_PREMIUM → ✅ Iron Condor (size ×0.5)
    │       ├── DTE = 1 + STRONG_SELL_PREMIUM → ✅ Iron Condor (size ×0.5)
    │       └── Otherwise → NO TRADE
    │
    ├── Price = DOWNTREND ─────────────────────────────────────────────────────────────────────
    │   Condition: ADX ≥ 25, EMA bearish, HH/HL = DOWNTREND
    │              Spot below OR low by > 20pts
    │   Rule: SELL CALLS ONLY — never sell puts in downtrend
    │   │
    │   ├── Positioning = BEARISH or RANGE or UNCLEAR ──────────────────────────────────────
    │   │   │
    │   │   ├── DTE = 0 ──────────────────────────────────────────────────────────────────
    │   │   │   ├── ADX 25-35 (moderate downtrend) → ✅ Bear Call Spread
    │   │   │   │     Short call: spot + 1.0× straddle (floor 120pts)
    │   │   │   │     Long call: short + 150pts
    │   │   │   │     Min credit: 20pts | Lots: 2
    │   │   │   │     Also known as: Call Credit Spread
    │   │   │   │                    Trend-Following Option Strategy
    │   │   │   │                    0DTE Momentum (sell side)
    │   │   │   │                    VWAP-Based Option Strategy
    │   │   │   └── ADX > 35 (strong downtrend) → ✅ Bear Call Spread (tighter short)
    │   │   │         Short call: spot + 0.85× straddle
    │   │   │         Higher credit, more buffer used
    │   │   │
    │   │   ├── DTE = 1 ──────────────────────────────────────────────────────────────────
    │   │   │   ├── ADX 25-35 → ✅ Bear Call Spread
    │   │   │   │     Short call: 0.19-0.22 delta
    │   │   │   │     Long call: short + 150pts
    │   │   │   │     Min credit: 18pts | Lots: 2
    │   │   │   └── ADX > 35 → ✅ Bear Call Spread (tighter)
    │   │   │         Short call: 0.22-0.26 delta (closer, more credit)
    │   │   │
    │   │   └── DTE = 2-5 → NO TRADE
    │   │         Exception: DTE 2 + STRONG_SELL_PREMIUM + ADX > 30
    │   │           → ✅ Bear Call Spread (size ×0.25)
    │   │
    │   └── Positioning = BULLISH (conflict — price overrides) ────────────────────────────
    │       Rule: Price regime always overrides positioning in downtrend
    │       ├── DTE = 0/1 → ✅ Bear Call Spread (size ×0.75, reduced for conflict)
    │       └── Confidence < HIGH → NO TRADE
    │
    ├── Price = UPTREND ──────────────────────────────────────────────────────────────────────
    │   Condition: ADX ≥ 25, EMA bullish, HH/HL = UPTREND
    │              Spot above OR high by > 20pts
    │   Rule: SELL PUTS ONLY — never sell calls in uptrend
    │   │
    │   ├── Positioning = BULLISH or RANGE or UNCLEAR ─────────────────────────────────────
    │   │   │
    │   │   ├── DTE = 0 ──────────────────────────────────────────────────────────────────
    │   │   │   ├── ADX 25-35 → ✅ Bull Put Spread
    │   │   │   │     Short put: spot - 1.0× straddle (floor 120pts)
    │   │   │   │     Long put: short - 150pts
    │   │   │   │     Min credit: 20pts | Lots: 2
    │   │   │   │     Also known as: Put Credit Spread
    │   │   │   │                    Trend-Following Option Strategy
    │   │   │   │                    0DTE Momentum (sell side)
    │   │   │   └── ADX > 35 → ✅ Bull Put Spread (tighter short)
    │   │   │         Short put: spot - 0.85× straddle
    │   │   │
    │   │   ├── DTE = 1 ──────────────────────────────────────────────────────────────────
    │   │   │   ├── ADX 25-35 → ✅ Bull Put Spread
    │   │   │   │     Short put: 0.19-0.22 delta
    │   │   │   │     Long put: short - 150pts
    │   │   │   │     Min credit: 18pts | Lots: 2
    │   │   │   └── ADX > 35 → ✅ Bull Put Spread (tighter)
    │   │   │
    │   │   └── DTE = 2-5 → NO TRADE
    │   │         Exception: DTE 2 + STRONG_SELL_PREMIUM + ADX > 30
    │   │           → ✅ Bull Put Spread (size ×0.25)
    │   │
    │   └── Positioning = BEARISH (conflict — price overrides) ─────────────────────────────
    │       ├── DTE = 0/1 → ✅ Bull Put Spread (size ×0.75, reduced for conflict)
    │       └── Confidence < HIGH → NO TRADE
    │
    ├── Price = STRONG_DOWNTREND ─────────────────────────────────────────────────────────────
    │   Condition: ADX > 35, EMA strongly bearish, spot > 100pts below OR
    │   Rule: SELL CALLS ONLY, wider buffer, reduced size
    │   │
    │   ├── DTE = 0 → ✅ Bear Call Spread
    │   │     Short call: spot + 1.2× straddle (extra buffer for strong trend)
    │   │     Long call: short + 150pts
    │   │     Min credit: 18pts | Lots: 1-2 (reduced — strong trend = more risk)
    │   │     Also known as: Trend-Following Option Strategy
    │   │
    │   ├── DTE = 1 → ✅ Bear Call Spread
    │   │     Short call: 0.15-0.18 delta (further OTM for buffer)
    │   │     Long call: short + 150pts
    │   │     Min credit: 15pts | Lots: 1-2
    │   │
    │   └── DTE = 2-5 → NO TRADE
    │
    ├── Price = STRONG_UPTREND ───────────────────────────────────────────────────────────────
    │   Condition: ADX > 35, EMA strongly bullish, spot > 100pts above OR
    │   Rule: SELL PUTS ONLY, wider buffer, reduced size
    │   │
    │   ├── DTE = 0 → ✅ Bull Put Spread
    │   │     Short put: spot - 1.2× straddle (extra buffer)
    │   │     Long put: short - 150pts
    │   │     Min credit: 18pts | Lots: 1-2
    │   │
    │   ├── DTE = 1 → ✅ Bull Put Spread
    │   │     Short put: 0.15-0.18 delta (further OTM for buffer)
    │   │     Long put: short - 150pts
    │   │     Min credit: 15pts | Lots: 1-2
    │   │
    │   └── DTE = 2-5 → NO TRADE
    │
    └── SPECIAL CONTEXTS (overlay on any price regime) ──────────────────────────────────────
        │
        ├── EVENT DAY (RBI/Budget/Major event scheduled today) ──────────────────────────────
        │   Condition: event_day = True
        │   Rule: defined-risk only, size ×0.25
        │   │
        │   ├── Before event print → NO TRADE (wait)
        │   ├── 30min after event print → 🔧 Post-Event Iron Condor
        │   │     Condition: IV still elevated vs pre-event level
        │   │     Structure: Iron Condor with shorts at 1.2× straddle
        │   │     Also known as: Volatility Crush Strategy
        │   │                    Event-Driven Straddle (sell side)
        │   │                    IV-RV Arbitrage
        │   └── If event not timestamped → NO TRADE all day
        │
        ├── GAP DAY (NIFTY gaps > 0.4% from prev close) ──────────────────────────────────
        │   Condition: gap_fade_opportunity = True (currently not computed — needs fixing)
        │   Rule: sell the gap-inflated side
        │   │
        │   ├── Gap DOWN + first 20min retracing 40%+ → 📋 Bull Put Spread
        │   │     Sell puts below gap low (inflated from fear)
        │   │     Also known as: Gap Fill Strategy, VWAP-Based Option Strategy
        │   │                    Opening Range Breakout Option Strategy
        │   └── Gap UP + first 20min retracing 40%+ → 📋 Bear Call Spread
        │         Sell calls above gap high (inflated from euphoria)
        │
        └── TUESDAY AFTERNOON PIN (13:00-15:00, DTE 0) ──────────────────────────────────
            Condition: time > 13:00 AND DTE = 0
                       AND spot within 50pts of max pain
                       AND OI wall strength ≥ 1.7 (moderate)
                       AND VIX not rising
            Action: 🔧 Hold existing position OR sell far side
            Structure: Iron Condor (if no position) or one-sided spread
            Also known as: Max Pain Strategy, Expiry-Day Iron Condor
                           0DTE Mean Reversion, 0DTE VWAP Strategy

═══════════════════════════════════════════════════════════════════════════════════════════════

STRATEGIES PERMANENTLY EXCLUDED FROM THIS ENGINE
(require infrastructure not available)
═══════════════════════════════════════════════════════════════════════════════════════════════

❌ Covered Call — requires holding NIFTY ETF/futures overnight
❌ Protective Put — requires holding underlying
❌ Covered Call with Protective Put — requires underlying
❌ Collar — requires underlying
❌ Synthetic Long/Short Stock — requires futures
❌ Synthetic Long/Short Call/Put — requires futures
❌ Synthetic Straddle/Strangle — requires futures
❌ Gamma Scalping — requires real-time delta hedging via futures
❌ Dynamic Delta Hedging — requires futures
❌ Portfolio-Level Delta Hedging — single position engine
❌ Calendar Spread — requires two expiry chains simultaneously
❌ Diagonal Spread — same
❌ Double Calendar — same
❌ Call/Put Calendar Spread — same
❌ Box Spread — requires near-zero bid-ask, perfect fills
❌ Volatility Surface Arbitrage — requires full IV surface
❌ Skew Trading — requires 60+ sessions of skew history
❌ Term-Structure Trading — requires multi-expiry data
❌ Dispersion Trading — requires individual stock options
❌ Correlation Trading — same
❌ Statistical Arbitrage — requires multiple correlated instruments
❌ Dealer GEX Strategy — requires full market microstructure
❌ Vanna-Volga Strategy — requires full options surface
❌ Machine Learning Strategy — requires 1000+ labeled sessions
❌ Reinforcement Learning — same
❌ Volatility Forecasting (GARCH) — requires 500+ daily observations

═══════════════════════════════════════════════════════════════════════════════════════════════

STRATEGIES THAT ARE DESCRIPTIONS NOT STRUCTURES
(these describe the engine's approach, not separate trades)
═══════════════════════════════════════════════════════════════════════════════════════════════

These are alternative names for what the engine already does:

  Regime-Based Option Strategy        = The entire engine architecture
  Price-Volatility Regime Matrix      = classify_final() function
  Adaptive Option Selling             = AutoCalibrator adjusting thresholds
  Dynamic Strike Selection            = _dte_adjusted_delta() + straddle-based distance
  Volatility Regime Switching         = _compute_vix_regime() + size multipliers
  Trend-Regime Switching              = classify_price_from_adx_ema()
  IV-RV Regime Switching              = VRP threshold in classify_volatility()
  Multi-Regime NIFTY Options Strategy = Full engine combining all regimes
  VRP Strategy                        = VRP > sell_threshold → SELL_PREMIUM
  IV Rank Strategy                    = IVR computation in _calculate_ivr()
  IV Percentile Strategy              = Same as IVR
  IV-RV Arbitrage                     = VRP = ATM_IV - Parkinson_RV
  Adaptive VRP Strategy               = AutoCalibrator vrp_sell_threshold
  Mean-Reversion Option Selling       = RANGE regime → sell condor
  Support-Resistance Option Selling   = OI walls define strike placement
  OI-Based Option Strategy            = classify_positioning() using OI
  PCR-Based Strategy                  = PCR in direction vote
  Expected Move Strategy              = Straddle-based short distance
  Systematic Delta-Hedged Short Vol   = Engine without delta hedging
  Gamma-Theta Optimization            = DTE selection balances gamma vs theta
  Vega-Theta Optimization             = VRP sell = sell vega, collect theta
  Multi-Leg Adaptive Strategy         = Condor with tested-wing management
  OTM Strangle Scalping               = Iron Condor with 70-80% profit target
  ATM Straddle Scalping               = Iron Fly with 50% profit target
  Delta-Neutral Option Selling        = Iron Condor (approximately delta-neutral)
  0DTE Delta-Neutral Strategy         = 0DTE Iron Condor
  0DTE ORB Strategy                   = Entry after OR establishes on Tuesday
  0DTE VWAP Strategy                  = VWAP-based exit on Tuesday
  0DTE Mean Reversion                 = Tuesday afternoon pin trade
  Trend-Following Option Strategy     = Bear Call in downtrend, Bull Put in uptrend

═══════════════════════════════════════════════════════════════════════════════════════════════

SIZE MODIFIERS (VIX adjusts size only — never trade/no-trade decision)
═══════════════════════════════════════════════════════════════════════════════════════════════

  final_size = day_size × vix_mult × confidence_mult × dte_mult × event_mult

  day_size:     Monday=0.55  Tuesday=0.80  Wed=0.70  Thu=0.70  Fri=0.60
  vix_mult:     <12.5=1.0    12.5-16=1.0   16-22=0.75  22-28=0.50  >28=0.25
  confidence:   HIGH=1.0     MEDIUM=0.5    LOW=0.25
  dte_mult:     DTE0=1.0     DTE1=0.75     DTE2=0.25   DTE3+=0.10
  event_mult:   Normal=1.0   Event day=0.25

═══════════════════════════════════════════════════════════════════════════════════════════════

EXIT RULES (same for all strategies — position manages itself)
═══════════════════════════════════════════════════════════════════════════════════════════════

  Priority 1: Short leg delta > 0.40 → close that leg (tested wing management)
  Priority 2: Spot within 40pts of short strike → close position
  Priority 3: Price stop = 0.30 × opening straddle from short strike
  Priority 4: Profit lock at 40% (0DTE) or 25% (1DTE+) → move stop to breakeven
  Priority 5: Cheap buyback — any short leg ≤ 2pts after 13:00 → buy back
  Priority 6: Time target — 0DTE: 40% after 13:30, 30% after 14:30
                           1DTE: 45% after 13:00, 40% after 14:00
  Priority 7: Hard exit — Tuesday 15:00, all other days 15:00
  NEVER: Close on regime label change, VIX noise, Parkinson RV anomaly
  
  
nifty_algo_v3/
├── env.txt                    # Configuration (kept from old engine)
├── nse_holidays.json          # NSE holidays (kept from old engine)
├── high_impact_events.json    # Event calendar (kept from old engine)
├── data/
│   └── nifty_algo_v3.db      # SQLite database
├── logs/                      # Log files
├── reports/                   # EOD reports
│
├── core.py                    # Infrastructure (evolved from nifty_algo_core.py)
├── data_engine.py             # Market data (evolved from market_data_engine.py)
├── regime_engine.py           # NEW regime classifier (replaces old regime_engine.py)
├── calibration_engine.py      # NEW self-calibrating system (extracted from old)
├── strategy_engine.py         # NEW strategy selector (replaces old strategy_engine.py)
├── execution_engine.py        # Execution (evolved from old execution_engine.py)
├── main.py                    # Main loop (evolved from old main.py)
├── backtest.py                # Backtest (evolved from old backtest.py)
└── eod_report.py              # EOD report (evolved from old eod_report.py)  


Long Call, Long Put, Covered Call, Protective Put, Bull Call Spread, Bear Put Spread, Bull Put Spread, Bear Call Spread, Long Straddle, Long Strangle, Short Straddle, Short Strangle, Bull Call Ratio Spread, Bear Put Ratio Spread, Call Ratio Backspread, Put Ratio Backspread, Call Debit Spread, Put Debit Spread, Call Credit Spread, Put Credit Spread, Iron Condor, Iron Fly, Long Iron Condor, Long Iron Fly, Jade Lizard, Reverse Jade Lizard, Call Butterfly, Put Butterfly, Broken Wing Butterfly, Double Butterfly, Calendar Spread, Diagonal Spread, Double Calendar Spread, Call Calendar Spread, Put Calendar Spread, Covered Call with Protective Put, Collar, Risk Reversal, Call Ratio Spread, Put Ratio Spread, Box Spread, Synthetic Long Stock, Synthetic Short Stock, Synthetic Long Call, Synthetic Long Put, Synthetic Straddle, Synthetic Strangle, Gamma Scalping, Delta-Neutral Option Selling, Delta-Neutral Straddle, Delta-Neutral Strangle, Volatility Arbitrage, IV-RV Arbitrage, IV Rank Strategy, IV Percentile Strategy, Expected Move Strategy, Mean-Reversion Option Selling, VWAP-Based Option Strategy, Opening Range Breakout Option Strategy, Trend-Following Option Strategy, Momentum Option Buying, Breakout Straddle, Volatility Expansion Straddle, Volatility Crush Strategy, Event-Driven Straddle, OI-Based Option Strategy, OI Unwinding Strategy, PCR-Based Strategy, Max Pain Strategy, Support-Resistance Option Selling, ATM Straddle Scalping, OTM Strangle Scalping, Expiry-Day Iron Fly, Expiry-Day Iron Condor, Expiry-Day Short Straddle, Expiry-Day Short Strangle, Expiry-Day Directional Debit Spread, Expiry-Day Gamma Scalping, 0DTE Mean Reversion, 0DTE Momentum, 0DTE ORB Strategy, 0DTE VWAP Strategy, 0DTE Delta-Neutral Strategy, Regime-Based Option Strategy, Price-Volatility Regime Matrix, Adaptive Option Selling, Dynamic Strike Selection, Dynamic Delta Hedging, Dynamic Iron Fly, Dynamic Iron Condor, Dynamic Straddle Adjustment, Dynamic Strangle Adjustment, Volatility Regime Switching, Trend-Regime Switching, IV-RV Regime Switching, Multi-Leg Adaptive Strategy, Portfolio-Level Delta Hedging, Gamma-Theta Optimization, Vega-Theta Optimization, Volatility Surface Arbitrage, Skew Trading, Term-Structure Trading, Dispersion Trading, Correlation Trading, Tail-Risk Hedging, Statistical Arbitrage with Options, Machine-Learning Regime Strategy, Reinforcement-Learning Options Strategy, Volatility Forecasting Strategy, Dealer Gamma Exposure Strategy, Gamma Exposure (GEX) Strategy, Vanna-Volga Strategy, Volatility Risk Premium (VRP) Strategy, Systematic Delta-Hedged Short Volatility, Systematic Long Volatility, Adaptive VRP Strategy, Multi-Regime NIFTY Options Strategy
