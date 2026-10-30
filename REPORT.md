
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
