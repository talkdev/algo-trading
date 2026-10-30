
## Round 23 (external audit v12, 2026-10-29) — live-operations hazards + deploy pace

The fourth external audit (v12) inspected the live/paper boundary of the authored
book. P0 and P1 findings are fixed here; exact numbers the box suffered are
unobservable (user-side), so fixes are structural + selftest-verified.

Dataset NOTE: with the main-branch rewrite + task-#13 user commit, the branch's
`stock-data/tsmom.db` became the live-box snapshot (prices through 2026-10-27).
Round-23 measurements (R22) therefore live on that snapshot — V0 re-baseline on
it is 11.51% / Sharpe 0.58 / MaxDD −26.65% — NOT comparable to rounds 1-21 rows
(their end-dates differ; each json row now visibly carries its own `end`.
Ledger 121 tracks re-baselining once a clean pinned dataset is rebuilt).

| leak | finding | measurement | fix |
|---|---|---|---|
| P0-1 peak | paper-mode peak ₹10L vs live book ₹3L ⇒ phantom −70% ⇒ EXTREME sell-all on next y | structural | mode-tagged peak; paper↔live flip rebases + releases locks (selftested) |
| P0-0 order book | ledger mixed paper+live rows in one view | structural | status order book filters to current mode; hidden rows counted |
| P0-2 freshness | 3-calendar-day skip passed a mid-week-stale symbol as current; truncated feeds shifted 12M returns up to +544bp and flipped 15% of top-5 picks | Friday-bar-after-Sunday-run case; backtest doc-strings | session-aware skip + STALE reporting |
| leak-3 deploy pace | autofill ramps the signal over N sessions — at N≈21, ONLY ~33% ends invested | R22 (snapshot): lump 11.51, ramp3 10.53, ramp5 10.29, ramp21 5.55 CAGR | `AUTOFILL_MAX_SESSIONS = 3` |
| leak-4 sweep | your n declined the ONLY risk-free parking offer | structural | sweep offered on its own y/n after any declined live run |

Optional one-time cleanup on the user box (only if the stored risk peak looks
wrong after the paper era):

    sqlite3 stock-data/tsmom.db "DELETE FROM meta WHERE key='risk_peak';"

The next run then seeds the peak from the real book and tags it with the mode.

## Round 24 (external audit v13, 2026-10-30) — small correctness batch

Auditor found no justifiable strategy change (ledger confirms); five small
code/doc items fixed:

1. **Two dropped ETFs (correctness, data path).** SBIBPB and SBINEQWETF (real
   NSE "EQ" listings, SBI, listed Mar-2025 / Jul-2024) failed the exact-name
   instrument-master match → NO DATA. Added an ISIN fallback
   (`ETF_KNOWN_ISIN`); unknown ISINs still fail safe. Action 111 = verify on box.
2. **Demat DP charge on sells was missing.** Added ₹15.93 flat per sell
   (₹13.50 + GST). Measured: on the ₹10,000 experiment seed the baseline moves
   11.51% → 10.28% CAGR purely because ₹16 dwarfs a ₹1k-class sell
   (micro-ticket drag, ledger #103 family). Scaled to a ₹10L book: ~0.12pp.
3. Docstring safety tiers corrected to the real 15% / 22.5% / 30% (they claimed
   10/15/20); GLOBAL-tax prose now matches the conservative config; the
   "Peak equity" startup seed is labelled until the first confirmed run.

Auditor's operational readings confirmed as intended behavior: decline-y/n is
manual by USER-LOCK (#115); month-end trickle caps budget near re-plan;
premium blocks on MONQ50 etc. are KEPT-BY-DESIGN (#25/#26/#91). Hypothesis
"staggered rebalance tranches" logged as #127 — not yet measured.

## Round 25 (external audit v14, 2026-10-30) — live-execution correctness

Live-console audit on the user's box (LIVE, 2.8% invested, rebalance due).
Four P0 money-path bugs fixed (each could overpay / false-fill / trade blind),
plus P1/P2 correctness and a console-truth batch.  All structural fixes
selftest-verified (128/128); nothing strategy-side touched (standing rule —
v14 itself found no knob change to justify).

| finding | was | now |
|---|---|---|
| synthetic fallback wrote demo prices into the production DB (live could trade on fiction); heal left fake NAV + fake splits | **purge all three + hard SYNTHETIC live gate** | ledger 128 |
| unverifiable live fill booked as FILLED at the simulated price | UNCONFIRMED until the broker says complete/filled | ledger 129 |
| live LIMIT = px*1.002 pushed through slippage again | broker gets ref x (1+0.2%) once | ledger 130 |
| cash-sync failure silently traded on the stale wallet (user saw a Rs 0.5L gap on screen) | fail-closed | ledger 131 |
| live buys allowed off yesterday's close | same-day quote required | ledger 132 |
| partially-filled orders not "open" -> duplicates | statuses added | ledger 133 |
| sip target re-derivation skipped selection filters | real selector re-run | ledger 134 |
| adopted broker holdings had no tax lots (zero-P&L sells) | conservative lots + deferral off | ledger 135 |
| GLOBAL LTCG reported at 30% slab | configured 12.5%+cess | ledger 136 |

Ops note (not a bug): the book was 2.8% invested because a live run was
declined twice — USER-LOCK (#115) means only interactive answers deploy.

## Round 26 (external audit v15, 2026-10-30) — live-run defects, one was a real money bug

Audited FROM A LIVE RUN console.  The SIP_MODE month-end rebalance computed
SELL orders for its own #1/#2 fresh picks (held names fell into the orphan
sell map because SIP_MODE never enters chosen names into it) — only a broker
EDIS failure stopped the churn from executing.  Also: failed exits used to
seal the signal as done forever; a ₹1,000 daily budget under the ₹2,000
minimum order size printed "already fully invested"; and EDIS failures were
buried in ticket text.  All fixed + selftested (131/131).  User-side asks:
raise STARTING_CAPITAL ≥ ₹5,000 and authorise EDIS/DDPI in Upstox.

| finding | was | now |
|---|---|---|
| SIP_MODE rebalance sold held-and-still-picked ETFs | kept via sip_orphan_should_sell | ledger 139 |
| FAILED/REJECTED exits marked "up to date", never retried | signal stays open + named retry | ledger 140 |
| ₹1,000/day budget made every buy impossible, console lied | loud config error + honest line | ledger 141 |
| EDIS leads buried | loud one-glance explainer | ledger 142 |
| plan-vs-reality lag invisible (2.8% vs 52.1% for days) | 3-day persistent warning | ledger 143 |
| rotation sells read like trend breaks | labelled | ledger 144 |

## Round 27 (external audit v16, 2026-10-30) — the post-buy sync deleted what you had just bought

The 30-Oct live run placed its first real strategy buy (SML100CASE 475u
@ ₹10.54 = ₹5,006.50 against the ₹5,000/day floor) and the weekly idle-cash
sweep parked ₹2.29 L in LIQUIDCASE.  Seconds later `sync_live_book()` rebuilt
"truth" from Upstox **holdings + positions** — where a delivery buy lands only
**T+1** — and DELETED both positions.  Journal equity printed ₹16,828 against
the real ₹2.51 L, i.e. a fabricated 93.28% drawdown beside "Risk state:
NORMAL".  A `trade` re-run on that book would have triggered the EXTREME
sell-everything rule against a phantom crash.  This was the highest-stakes
defect yet; all of v16 follows from it (135/135 selftests).

| finding | was | now |
|---|---|---|
| sync wiped same-day delivery fills (T+1 gap) | `_filled_today` from the order book keeps + re-admits them; loud DO-NOT-RERUN warning; selftested keep + stale-date non-mask | ledger 145 |
| "Re-running today is safe" printed unconditionally | conditional — on a T+1-pending day it says the opposite | ledger 146 |
| 93.28% printed next to "Risk state: NORMAL" | risk engine stamps `risk_last_eval`; status errors loudly on drawdown/state contradiction | ledger 147 |
| ₹5k buy + ₹2.29 L sweep read as one "traded" number | sleeve parking split out of every spent-money panel; sweep priced off the live quote; rounding note | ledger 148 |
| cleanup line indistinguishable from data loss | names T+1 explicitly | ledger 148 |
| coverage summary silent on the feed's history cap | prints avg depth ≈ years + the candle-cap caveat | ledger 148 |

**User-side asks (ledger actions 115-116):** do NOT re-run `trade` today —
tomorrow's sync heals the box's journal automatically; and verify the
LIQUIDCASE instrument/price (₹116.69 fill vs a ~₹1,000-quoted liquid ETF —
check for a split the price series never absorbed).
