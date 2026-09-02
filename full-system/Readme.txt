1. Pre-market gate
09:15 IST — session eligibility
Holiday check
today in NSE_MARKET_HOLIDAYS? → if yes, engine idle all day
ALLOW_NON_TRADING_DAY_RUN
False → hard stop on holidays
Trading window
EXEC_START_TIME 09:30 → EXEC_END_TIME 14:00 (entries only)
Reason for 09:30 start
avoids opening-auction volatility (first 15 min)
VIX snapshot time
ENTRY_VIX_TIME 09:15, STRIKE_SELECT_TIME 09:17

2. Data ingestion loop
spot, VIX, chain, candles
Spot + VIX
via websocket ltpc feed, continuous
Option chain
EP_OPTION_CHAIN, refreshed each regime cycle
30-min candles
ADX_CANDLE_TIMEFRAME=30minute, refreshed every 1800s
Candle lookback
45 days (buffer for RV_LOOKBACK_DAYS=20 after holidays)
WS kill switch
no data for 300s → treated as feed-down, halts new entries
Gate
if spot, VIX, or chain missing → regime refresh skipped entirely

3a. Vol module
term spread + 25-delta skew z
Term spread
far-month ATM IV − near-month ATM IV (falls back to VIX if no near IV)
Contango threshold
> +1.5pp → term_score = +1 (sell vol)
Backwardation threshold
< −1.5pp → term_score = −1 (buy vol)
else
term_score = 0 (flat)
Skew leg selection
25-delta call & put, tolerance ±0.05 delta
Skew fear z
> 1.5 → skew_score = −1 (fear, buy vol)
Skew complacent z
< −1.0 → skew_score = +1 (complacent, sell vol)
Skew lookback
60 days, min 3 days before z is trusted
vol_score
0.5×term_score + 0.5×skew_score, range −1..+1


3b. Edge module
IV(ATM) vs realized vol(20d)
edge = IV_atm − RV20
RV computed from daily closes, annualised
Rich threshold
edge > 5.0 → raw = +1 (sell vol, premium overpriced)
Cheap threshold
edge < 0.0 → raw = −1 (buy vol, premium underpriced)
else
raw = 0 (fair)
Min history gate
needs 20 sessions of RV data, else neutral (score 0)
Estimated-RV cap
if RV is VIX-derived estimate (not actual), score forced to 0 — avoids circular signal

3c. Trend module
ADX(14) + EMA-50 slope, 30m bars
ADX period
14, on 30-minute candles (needs 75 bars minimum)
ADX trend threshold
adx > 20 required for a "trending" read
EMA slope threshold
|slope| > 0.15% of spot (~37.5 pts) over last 21 bars
Slope window
today session bars only (overnight gap excluded)
Bullish trend
spot>EMA50 AND slope up AND +DI>−DI → raw = −1
Bearish trend
spot+DI → raw = −1
Mixed signals
no 3-way agreement → raw = 0
Range-bound (default)
ADX/slope below threshold → raw = +1 (favorable for selling)

3d. Flow module
delta-weighted ΔOI + spread ratio
DTE guard
skipped entirely if DTE < 3 (expiry rollover noise)
Net ΔOI window
compares snapshot 10-15 min old vs now, delta-weighted by strike
3rd OTM put spread
(ask−bid)/mid, tracked vs 1h median baseline
Widening threshold
spread > 1.10× baseline → WIDENING
Contracting threshold
spread < 0.90× baseline → CONTRACTING
Bullish flow
net_flow>0 AND spread CONTRACTING → raw = +1
Defensive/panic flow
net_flow<0 AND spread WIDENING → raw = −1
else
raw = 0 (mixed)

4. Persistence filter
3 consecutive matching readings
PERSISTENCE_READINGS
3 — a raw module score must repeat 3 cycles running to be "confirmed"
Refresh cadence
REGIME_REFRESH_SECONDS = 60
Effect
confirmed_vol/edge/trend/flow only change after ~3 min of consistent raw signal
Startup warmup
3 refresh cycles forced to NEUTRAL after restart, so stale SQLite state can't drive a trade

5. Macro override
high-impact event days
Trigger dates
Union Budget, RBI policy days (config.HIGH_IMPACT_EVENTS)
Window
6h before event → 2h after (EVENT_WINDOW_BEFORE/AFTER_HOURS)
Action
forces regime = EVENT_HEDGE, bypassing the composite score entirely
Priority
checked before the warmup gate — a restart during an event still gets EVENT_HEDGE

6. Composite score
weighted sum of 4 confirmed modules
Formula
0.30×vol + 0.30×edge + 0.25×trend + 0.15×flow
Clamped range
−1.0 to +1.0
STRONG_SELL_VOL
composite > 0.45
MILD_SELL_VOL
0.15 ≤ composite ≤ 0.45
NEUTRAL
−0.15 < composite < 0.15
BUY_VOL
−0.45 ≤ composite ≤ −0.15
STRONG_BUY_VOL
composite < −0.45
Hysteresis band
0.05-wide enter/exit gap per regime to stop flip-flopping at boundaries

7. Strategy per regime
capital + lot caps per regime
STRONG_SELL_VOL
iron condor / short straddle — 20% capital, max 8 lots
MILD_SELL_VOL
credit spreads — 20% capital, max 6 lots
NEUTRAL
small/no premium selling — 10% capital, max 3 lots
BUY_VOL
long straddle / ratio spread — 10% capital, max 3 lots
STRONG_BUY_VOL
backspread (directional) — 15% capital, max 4 lots
EVENT_HEDGE
long strangle / defensive hedge — 5% capital, max 2 lots
Entry DTE window
3-10 days depending on strategy, entries only Wed-Fri ideally

8. Pre-trade risk checks
gates before any order fires
Max risk / trade
2% of capital (₹20,000 on ₹10L)
Max combined risk
20% of capital across open positions
Max daily loss
3% of capital → halts new trades for the day
Max drawdown
10% of capital → kill switch
Max concurrent positions
4, plus max 2 tranches of the same strategy
Liquidity filter
min OI 50 lots, max spread 3pts (ATM) / 5pts (OTM)
Min credit floor
condor 22% of wing width, spreads 25% of wing width
Re-entry cooldown
300s AND spot move < 0.2% since last close

9. Order execution
leg building + fills
Order type
limit at bid/ask ± 1 tick (0.05), market on timeout paths
Fill timeout
core legs 15s, hedge legs 10s, SL orders 15s
Partial fill handling
cancels remainder, adjusts leg qty down, marks PARTIAL
Paper slippage model
20 ticks on short legs, 40 ticks on hedge legs
Brokerage
₹20 flat per order + STT 0.15% (sell side) + exchange + GST 18% on fees

10. Position exit logic
stop / target / time / regime
Profit target
65% of credit captured (condor/spread), 50% (straddle base)
Stop loss
straddle 2.0× credit; condor/spread ~1.25×; static fallback 10%
Trailing stop
arms at 55% profit, retains 85% of peak gain thereafter
DTE exit
close 1 day before expiry for most strategies (EXIT_DTE=1)
Time exit — expiry day
15:10 IST (near-zero theta left)
Time exit — normal EOD
15:15 IST
Regime change
can force close if new regime conflicts with position's Greeks limits

11. Circuit breakers
portfolio-level kill switches
Level 1 — 2% loss
close the triggering position
Level 2 — 3% loss
halt new trades for the day
Level 3 — 8% loss
reduce all positions by 50%
Level 4 — 10% loss
full stop, manual review required
Level 5 — VIX ≥ 25
force STRONG_BUY regime (defensive posture)
Note
these are reactive — they fire after a loss is booked, not pre-emptive

12. End of day / expiry
reconciliation + cleanup
EOD reconcile
15:30 IST — matches broker positions vs internal state
Expiry-day close
skips deep-OTM legs already worthless, closes the rest at market
Cancel sweep
cancels all unfilled session orders via tag-based registry
State persisted
capital, P&L, circuit-breaker flags saved to SQLite for next-day restart