#!/usr/bin/env python3
"""
PATCH_V17 - every identified fix (V15 + V16) in one self-contained file.

Run from the repository root:
    python patch17.py --verify      first: is this tree intact?
    python patch17.py               apply (atomic, UTF-8, rollback on error)
    python patch17.py --check       dry run: which edits would apply

WINDOWS / ENCODING SAFETY (read this if a patch ever broke your tree)
--------------------------------------------------------------------
This script reads and writes every source file as UTF-8 explicitly, writes
through a temporary file with os.replace() (a reader sees the old file or the
complete new one, never a half-written stump), keeps one .pwbak of each file
it touches, verifies ALL anchors before writing ANY file, and rolls back
automatically if a patched module fails to compile.

A patch that dies with `UnicodeEncodeError: 'charmap' codec can't encode ...`
is the same bug in reverse: under a Windows cp1252 default, write_text()
truncates the file, then raises while encoding the new text, and the module
is left as a partial copy. If that has already happened to you, restore the
file from git (`git restore -- data_engine.py`) or from the branch you got
this patch from, then run `python patch17.py --verify` to confirm the tree.

WHAT IT FIXES (PATCH_V15) - the 2026-09-16 range session
---------------------------------------------------------
Three independent defects stacked up on the one session that shows the
problem most cleanly:
  a) compute_parkinson_rv() measured "realised vol" off a rolling window that
     contains only the opening minutes before ~10:45. The 09:15-09:35 spike
     printed RV 18.3% against a 13.7% ATM IV and a 9.8% session anchor -> VRP
     went NEGATIVE -> vol_regime = BUY_OPTIONS -> the sell side was banned for
     61 cycles (09:36-10:35).
  b) the hard-gate day-move block measured the session on its TIME-SCALED
     speed (126-182% from 09:45 to 10:18) although the realised range - 165
     pts - was 52% of the 319 pts the opening straddle priced for the whole
     day.
  c) with NEUTRAL vol there is no VRP to harvest, so the dte3/4 range path
     could only build the delta-neutral condor, while the structure that was
     actually profitable (a cushioned put spread below the failed
     opening-range low: +9.8 pts to 10:50) had no route at all.
Fix: session-anchored RV; a RANGE-verdict exemption on the day-move block
reconstructed from the gauge's own two fields; a failed-break reclaim rule
(dte>=2, NEUTRAL vol, contained OR, 0.5 size) with its thesis stop, its OR-mid
treatment and a market-weighted p_win; day extremes carried in the signals;
the candle loader clamped to the session window.

WHAT IT FIXES (PATCH_V16) - live gets no trades, the replay gets several
-----------------------------------------------------------------------
  a) The replay is not the engine that traded: sessions 08-15 Sep were
     recorded by an older build (old refusal grammar, a 4x-friction gate that
     no longer exists) which charged EV = -32..-37 pts on structures today's
     model scores at ~0. Only 09-16 was recorded with the current build.
  b) The live engine loses the session it is trading. Four gaps in six
     sessions (1,189s, 4,496s, 330s, 8s). The replay never restarts; live
     does, and the keys that die with a restart are exactly the anchors the
     rest of the day is measured against. Measured: 2026-09-15 12:27:50, one
     cycle after the 8s gap, iv_change_pct_from_open flipped -8.59
     (DECLINING) -> +28.52 (SPIKING) and premium selling was refused for the
     remaining 427 cycles; 2026-09-08 the opening-straddle anchor read 280.4
     pts before the 1,189s gap and 175.0 pts after it, re-scaling the
     day-move gauge from 36.31% to 62.46% on the same tape.
Fix: the in-memory-only anchors are persisted in one JSON overflow column
(aux_json) and restored on load; a 0DTE IV change with no baseline fraction
returns UNKNOWN (not a hard block) instead of a raw read that turns time
decay into SPIKING; the opening-straddle anchor is immutable once set.

VERIFIED AFTER APPLYING (replays)
--------------------------------
  08-Sep BCS +2,544 | 09-Sep +3,798 | 10-Sep +1,981 | 11-Sep +17,181
  15-Sep +8,367 | 16-Sep BPS +685  ->  7 trades, Rs 34,556, 100% win
With an artificial mid-session restart injected at the recorded gap:
  09-08 (11:43:41) before the fix: 0 trades, 559 SPIKING cycles
                   after  the fix: BCS 12:03:30 -> +Rs 2,544
  09-15 (12:27:50) before: SPIKING (+28.66%) for the rest of the session
                   after : DECLINING/CRUSHING, 10 of 10 anchors restored
"""
import hashlib, os, py_compile, shutil, sys
from pathlib import Path

try:                       # never let a console codec break the run
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MODULES = ("core.py", "data_engine.py", "regime_engine.py", "strategy_engine.py",
           "execution_engine.py", "calibration_engine.py", "backtest_engine.py",
           "main.py", "telegram_reporter.py", "bot_controller.py")
KEY_SYMBOLS = (("data_engine.py", "class MarketDataEngine"),
               ("regime_engine.py", "class RegimeEngine"),
               ("strategy_engine.py", "class StrategyEngine"),
               ("execution_engine.py", "class ExecutionEngine"),
               ("core.py", "class ExpiryCalendar"),
               ("core.py", "class Database"))


def _read(path: Path) -> str:
    """Read source as UTF-8, never with the locale codec.

    On Windows the default is cp1252. A locale READ mangles every non-ASCII
    comment in the tree; a locale WRITE raises UnicodeEncodeError part-way
    through - and because write_text() truncates before it writes, the file
    is left as a stump. That is how a working tree gets destroyed by a patch
    script, and it is what this function pair exists to prevent.
    """
    return path.read_text(encoding="utf-8-sig")


def _write_atomic(path: Path, text: str) -> None:
    """Replace the file atomically, keeping one .pwbak of the original.

    The reader either sees the old file or the complete new one; an encoding
    error or a crash cannot leave a half-written module behind.
    """
    tmp = path.with_name(path.name + ".pwtmp")
    bak = path.with_name(path.name + ".pwbak")
    with open(str(tmp), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    if not bak.exists():
        shutil.copy2(str(path), str(bak))
    os.replace(str(tmp), str(path))


def _restore(root: Path, names) -> None:
    for name in names:
        bak = root / (name + ".pwbak")
        if bak.exists():
            shutil.copy2(str(bak), str(root / name))
            print(f"     restored {name} from {name}.pwbak")


def _verify(root: Path) -> int:
    """Report whether this tree is intact: compile + key symbols + markers."""
    print("TREE VERIFICATION")
    print("-" * 70)
    bad = []
    for name in MODULES:
        path = root / name
        if not path.exists():
            print(f"  [MISSING ] {name}")
            bad.append(name)
            continue
        try:
            py_compile.compile(str(path), doraise=True)
            print(f"  [compiles] {name}")
        except py_compile.PyCompileError as exc:
            msg = str(exc).splitlines()[-1][:80]
            print(f"  [BROKEN  ] {name}   {msg}")
            bad.append(name)
    for name, symbol in KEY_SYMBOLS:
        path = root / name
        if not path.exists():
            continue
        try:
            text = _read(path)
        except Exception as exc:
            print(f"  [UNREADABLE] {name}: {exc}")
            bad.append(name)
            continue
        if symbol not in text:
            print(f"  [TRUNCATED ] {name} is missing '{symbol}'")
            bad.append(name)
    for marker in ("PATCH_V12", "PATCH_V13", "PATCH_V14", "PATCH_V15", "PATCH_V16"):
        hits = 0
        for name in MODULES:
            path = root / name
            if path.exists():
                try:
                    if marker in _read(path):
                        hits += 1
                except Exception:
                    pass
        print(f"  [{'present' if hits else ' absent'}] {marker} markers: {hits} module(s)")
    print("-" * 70)
    if bad:
        print(f"  {len(bad)} problem file(s): {', '.join(sorted(set(bad)))}")
        print("  Restore them from git and re-run this patch:")
        print("      git restore -- " + " ".join(sorted(set(bad))))
        print("  (or, if the folder is not a git clone, download the same files")
        print("   from the branch you took this patch from.)")
        return 1
    print("  Tree looks intact.")
    return 0

EDITS = [('data_engine.py', '                "WHERE trading_date=? AND interval_min=1 "\n', '                "WHERE trading_date=? AND interval_min=1 "\n                "AND candle_time >= \'09:15:00\' AND candle_time <= \'15:30:00\' "\n', 'E1'), ('data_engine.py', '                    # Valid RV\n                    self.state["parkinson_rv_pct"]            = rv\n                    self.state["parkinson_rv_computed_date"]  = today_str\n                    return rv, "rolling_intraday"\n', '                    # ── PATCH_V15: session-anchored RV ────────────────\n                    # The rolling window is SHORT at the open (bar count,\n                    # not clock time, decides: tail(90) on dte>=2 is the\n                    # whole session before 10:45). A violent 09:15-09:45\n                    # therefore sets the "realized" vol for hours and\n                    # differenced against a forward IV it produced a\n                    # NEGATIVE VRP - the engine read 2026-09-16 as BUY\n                    # (RV 18.3% vs IV 13.7%) while the session\'s own range\n                    # was half the priced straddle. Cap the spike against\n                    # the session anchor and shrink the estimate toward\n                    # that anchor while the window is incomplete.\n                    _anchor_v15 = float(self.state.get("rv_anchor_pct") or 0.0)\n                    if _anchor_v15 < rv_floor:\n                        _anchor_v15 = float(cached_rv or 0.0) \\\n                            if self.state.get("parkinson_rv_computed_date") == today_str \\\n                            else 0.0\n                    if _anchor_v15 >= rv_floor:\n                        if rv > _anchor_v15 * 1.35:\n                            rv = _anchor_v15 * 1.35\n                        _w_v15 = min(len(log_hl_sq) / 60.0, 1.0)\n                        rv = _w_v15 * rv + (1.0 - _w_v15) * _anchor_v15\n\n                    # Valid RV\n                    self.state["parkinson_rv_pct"]            = rv\n                    self.state["parkinson_rv_computed_date"]  = today_str\n                    return rv, "rolling_intraday"\n', 'E2'), ('data_engine.py', '            if rv_floor * 0.5 < vix_implied < rv_ceil:\n                self.logger.debug(\n', '            if rv_floor * 0.5 < vix_implied < rv_ceil:\n                # PATCH_V15: remember the session anchor (persisted) so the\n                # first rolling estimate is measured against the prior the\n                # session opened with, not against a stale cross-session RV.\n                self.state["rv_anchor_pct"] = vix_implied\n                self.state["rv_anchor_date"] = today_str\n                self.logger.debug(\n', 'E3'), ('data_engine.py', '                if not market_bars.empty:\n                    day_high = float(market_bars["high"].max())\n                    day_low  = float(market_bars["low"].min())\n                    return round((day_high - day_low) / _straddle_ref * 100.0, 2)\n', '                if not market_bars.empty:\n                    day_high = float(market_bars["high"].max())\n                    day_low  = float(market_bars["low"].min())\n                    # PATCH_V15: expose the session extremes - the range\n                    # gates and the failed-break structure read need them.\n                    self.state["day_high_so_far"] = day_high\n                    self.state["day_low_so_far"]  = day_low\n                    return round((day_high - day_low) / _straddle_ref * 100.0, 2)\n', 'E4'), ('data_engine.py', '            "day_move_used_pct":        day_move_used_pct,\n', '            "day_move_used_pct":        day_move_used_pct,\n            # PATCH_V15: session extremes for the failed-break structure\n            "day_high_so_far":          self.state.get("day_high_so_far"),\n            "day_low_so_far":           self.state.get("day_low_so_far"),\n', 'E5'), ('regime_engine.py', '        if dte in (3, 4):\n            if vol == VolatilityRegime.BUY_OPTIONS:\n                return (\n                    FinalRegime.NO_TRADE,\n                    f"RANGE_DTE{dte}_REQUIRES_NO_BUY_OPTIONS",\n                )\n', '        if dte in (3, 4):\n            if vol == VolatilityRegime.BUY_OPTIONS:\n                return (\n                    FinalRegime.NO_TRADE,\n                    f"RANGE_DTE{dte}_REQUIRES_NO_BUY_OPTIONS",\n                )\n            # ── PATCH_V15: failed-break, cushioned vertical ──────────────\n            # A range session that broke one edge of the opening range and\n            # RECLAIMED it is the highest-quality premium sell of the day:\n            # the break flushed the stops, the reclaim proves the edge held,\n            # and the sold strike can sit beyond the failed extreme. This is\n            # how intraday NIFTY premium sellers trade a range day, and it\n            # is the read the engine was missing: with NEUTRAL vol (no VRP\n            # edge to harvest) it could only build a delta-neutral condor,\n            # which needs rich vol by design. Measured 2026-09-16: the tape\n            # broke the 23,186.55 opening-range low to 23,125 at 09:45,\n            # reclaimed it by 09:53, and a cushioned 23,050/22,750 put\n            # spread sold there returned +9.8pts by 10:50 while the condor\n            # of the same vintage returned -1.8pts. Sized DOWN (0.5) - the\n            # structure is directional, the vol edge is not there, and the\n            # cushion is what carries it.\n            try:\n                _v15_orl = float(signals.get("or_low") or 0.0)\n                _v15_orh = float(signals.get("or_high") or 0.0)\n                _v15_dlo = float(signals.get("day_low_so_far") or 0.0)\n                _v15_orw = max(_v15_orh - _v15_orl, 1.0)\n            except (TypeError, ValueError):\n                _v15_orl = _v15_orh = _v15_dlo = 0.0\n                _v15_orw = 1.0\n            _v15_orh_s = float(signals.get("day_high_so_far") or 0.0)\n            _v15_broke_lo = _v15_orl > 0 and _v15_dlo > 0 and \\\n                _v15_dlo <= _v15_orl - 0.15 * _v15_orw\n            _v15_broke_hi = _v15_orh > 0 and _v15_orh_s > 0 and \\\n                _v15_orh_s >= _v15_orh + 0.15 * _v15_orw\n            _v15_reclaim = max(\n                float(getattr(self.config, "failed_break_reclaim_pts", 10.0)),\n                0.10 * _v15_orw,\n            )\n            _v15_quiet = (\n                bool(signals.get("or_computed"))\n                and vol == VolatilityRegime.NEUTRAL\n                and conf in (ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM)\n                and 0.0 <= adx_15 < float(\n                    getattr(self.config, "adx_trend_threshold", 20.0))\n                and or_condition in ("VERY_NARROW", "NARROW", "MODERATE")\n                and spot > 0\n                and float(signals.get("day_move_used_pct") or 0.0)\n                    < float(getattr(self.config, "failed_break_dmu_max", 200.0))\n                and not bool(signals.get("event_day"))\n            )\n            _v15_fb = (\n                _v15_quiet\n                and pos in (PositioningRegime.RANGE,\n                            PositioningRegime.STRONG_RANGE,\n                            PositioningRegime.UNCLEAR)\n                and ((_v15_broke_lo and spot >= _v15_orl + _v15_reclaim)\n                     or (_v15_broke_hi and spot <= _v15_orh - _v15_reclaim))\n            )\n            if _v15_fb:\n                signals["neutral_range_vertical"] = True\n                signals["weekly_range_size_discount"] = float(\n                    getattr(self.config, "failed_break_size", 0.50))\n                if _v15_broke_lo and spot >= _v15_orl + _v15_reclaim:\n                    return (\n                        FinalRegime.PREMIUM_SELL_BULL,\n                        f"RANGE_DTE{dte}_FAILED_BREAK_RECLAIM_BULL_PUT",\n                    )\n                return (\n                    FinalRegime.PREMIUM_SELL_BEAR,\n                    f"RANGE_DTE{dte}_FAILED_BREAK_RECLAIM_BEAR_CALL",\n                )\n', 'E6a'), ('regime_engine.py', '                if vol not in (VolatilityRegime.SELL_PREMIUM,\n                               VolatilityRegime.STRONG_SELL_PREMIUM):\n                    return (\n                        FinalRegime.NO_TRADE,\n                        f"RANGE_DTE{dte}_CONDOR_REQUIRES_SELL_PREMIUM"\n                        f"_GOT_{vol.value}",\n                    )\n', '                if vol not in (VolatilityRegime.SELL_PREMIUM,\n                               VolatilityRegime.STRONG_SELL_PREMIUM):\n                    # ── PATCH_V15: range-confirmed WIDE condor on NEUTRAL vol\n                    _dmu_v15 = float(signals.get("day_move_used_pct") or 0.0)\n                    _wide_ok_v15 = (\n                        int(dte) >= 2\n                        and not _v15_broke_lo\n                        and not _v15_broke_hi\n                        and vol == VolatilityRegime.NEUTRAL\n                        and conf in (ConfidenceLevel.HIGH,\n                                     ConfidenceLevel.MEDIUM)\n                        and 0.0 <= adx_15 < float(\n                            getattr(self.config, "adx_trend_threshold", 20.0))\n                        and _dmu_v15 < float(getattr(\n                            self.config, "neutral_range_dmu_max", 200.0))\n                        and not bool(signals.get("event_day"))\n                    )\n                    if _wide_ok_v15:\n                        signals["neutral_range_condor"] = True\n                        signals["weekly_range_size_discount"] = float(\n                            getattr(self.config, "neutral_range_size_weekly",\n                                    0.60))\n                        return (\n                            FinalRegime.PREMIUM_SELL_RANGE,\n                            f"RANGE_DTE{dte}_WIDE_CONDOR_NEUTRAL_VOL",\n                        )\n                    return (\n                        FinalRegime.NO_TRADE,\n                        f"RANGE_DTE{dte}_CONDOR_REQUIRES_SELL_PREMIUM"\n                        f"_GOT_{vol.value}",\n                    )\n', 'E6b'), ('strategy_engine.py', '        if _dm_threat >= self.config.day_move_used_block_pct and not _dm_trend_confirmed:\n            return "NO_TRADE", (\n                f"day_move_used_{_dm_threat:.0f}pct_of_opening_straddle_no_edge"\n            )\n', '        # ── PATCH_V15: the range verdict is judged on RANGE, not speed ───\n        # The time-scaled gauge above says how fast the session has moved\n        # for the clock; that is the right question for a directional\n        # vertical (an exhausted with-trend move is its thesis) but the\n        # wrong one for a delta-neutral condor. What kills a condor is the\n        # day\'s RANGE running beyond the range the straddle priced for the\n        # WHOLE session - a front-loaded morning that then goes nowhere is\n        # the condor\'s best tape, not its worst. Measured 2026-09-16: the\n        # gauge sat at 126-156% from 09:45 to 10:18 and refused the\n        # session, while the realised range (165pts) was 52% of the 319pt\n        # the 4-day 330.7 straddle priced for the day. Reconstructed from\n        # the same two fields the gauge itself is built from, so the\n        # exemption can never disagree with the metric that raised it.\n        _dm_range_confirmed = False\n        if final_regime == "PREMIUM_SELL_RANGE":\n            try:\n                _dm_es = float(signals.get("expected_range_so_far_pts") or 0.0)\n                _dm_os = float(signals.get("opening_straddle_pts") or 0.0)\n                _dm_dte_v15 = int(actual_dte or 0)\n                _dm_range_pts = (\n                    day_move_used / 100.0 * _dm_es if _dm_es > 0 else 0.0\n                )\n                _dm_theta_v15 = (\n                    (1.0 / max(_dm_dte_v15, 1)) ** 0.5\n                    if _dm_dte_v15 >= 2 else 1.0\n                )\n                _dm_full_ref = _dm_os * _dm_theta_v15 * 1.93\n                _dm_frac_range = (\n                    _dm_range_pts / _dm_full_ref if _dm_full_ref > 0 else 9.9\n                )\n            except (TypeError, ValueError):\n                _dm_frac_range = 9.9\n            _dm_range_confirmed = (\n                _dm_px in ("RANGE", "STRONG_RANGE")\n                and 0.0 <= float(signals.get("adx_15") or 0.0)\n                < float(self.config.adx_trend_threshold)\n                and _dm_frac_range < float(\n                    getattr(self.config, "day_range_frac_block_condor", 0.75))\n            )\n        # PATCH_V15: the failed-break vertical is the same argument - its\n        # thesis IS the exhausted excursion the gauge is measuring, and its\n        # strike sits beyond the failed extreme.\n        if bool(signals.get("neutral_range_vertical")):\n            _dm_range_confirmed = True\n        if _dm_threat >= self.config.day_move_used_block_pct and not (\n            _dm_trend_confirmed or _dm_range_confirmed\n        ):\n            return "NO_TRADE", (\n                f"day_move_used_{_dm_threat:.0f}pct_of_opening_straddle_no_edge"\n            )\n', 'E7'), ('strategy_engine.py', '            if or_high > 0 and or_low > 0:\n                or_mid    = (or_high + or_low) / 2.0\n                or_buffer = 30 if dte == 0 else 15\n                if spot < or_mid - or_buffer:\n', '            # PATCH_V15: the failed-break vertical confirmed itself by\n            # RECLAIMING the range low, so the OR-mid veto is satisfied by\n            # a different measurement than the one it was written for.\n            if or_high > 0 and or_low > 0 and not signals.get(\n                    "neutral_range_vertical"):\n                or_mid    = (or_high + or_low) / 2.0\n                or_buffer = 30 if dte == 0 else 15\n                if spot < or_mid - or_buffer:\n', 'E8'), ('strategy_engine.py', '        _stop_premium_pre = max(_stop_premium_pre, net_credit * 1.10)\n', '        _stop_premium_pre = max(_stop_premium_pre, net_credit * 1.10)\n        # ── PATCH_V15: failed-break vertical is stopped by its thesis ────\n        # The structure is sold because the failed extreme held; if the\n        # tape revisits it the reason for the trade is gone. Pricing that\n        # as a multiple of the credit (1.7x on dte2+) overstates the risk\n        # and made every such trade fail the EV gate.\n        if signals.get("neutral_range_vertical"):\n            try:\n                if strategy_name == BULL_PUT_SPREAD:\n                    _fb_ref_pre = float(signals.get("day_low_so_far") or 0.0)\n                else:\n                    _fb_ref_pre = float(signals.get("day_high_so_far") or 0.0)\n                _fb_spot_pre = float(signals.get("spot") or 0.0)\n                if _fb_ref_pre > 0 and _fb_spot_pre > 0:\n                    _fb_risk_pre = max(\n                        0.25 * abs(_fb_spot_pre - _fb_ref_pre),\n                        0.15 * net_credit,\n                    )\n                    _stop_premium_pre = min(\n                        _stop_premium_pre, net_credit + _fb_risk_pre\n                    )\n            except (TypeError, ValueError):\n                pass\n', 'E9'), ('strategy_engine.py', '        _wk = float(getattr(self.config, "ev_blend_market_w", 0.30))\n', '        _wk = float(getattr(self.config, "ev_blend_market_w", 0.30))\n        # PATCH_V15: for the failed-break vertical the market\'s own\n        # per-strike delta is the sharpest touch estimate - the edge IS\n        # the cushion, and delta prices exactly that. The DTE x OR prior\n        # was built around ATM-ish shorts; weight the quote instead.\n        if signals.get("neutral_range_vertical"):\n            _wm, _wp, _wk = 0.25, 0.20, 0.55\n', 'E10'), ('data_engine.py', '            self.logger.info(\n                f"Session state loaded for {today_str} "\n                f"(mid-day restart recovery, entries={row.get(\'entry_count\', 0)})"\n            )\n            return dict(row)\n', '            # ── PATCH_V16: restore the in-memory-only session anchors ────\n            # A restart must not restart the SESSION. The columns above\n            # survive a restart; these do not, and every one of them is an\n            # anchor that the rest of the day is measured against:\n            #   opening_iv_rem_frac   the fraction of the session remaining\n            #                         when the IV baseline was taken - the\n            #                         only thing that makes a 0DTE IV change\n            #                         comparable across the day\n            #   opening_iv_expiry     which series that baseline belongs to\n            #   _straddle_open_expiry which series the opening straddle\n            #                         belongs to\n            #   _prev_close_for_gap   the gap/level reference\n            #   _last_atm_straddle    the previous cycle\'s ATM straddle\n            #   _straddle_hist        the 10-minute straddle-expansion window\n            #   rv_anchor_pct         the session\'s realised-vol anchor\n            # Measured cost of losing them (2026-09-15, 8s gap at 12:27:42):\n            # iv_change_pct_from_open flipped from -8.59 (DECLINING) to\n            # +28.52 (SPIKING) and premium selling was refused for the rest\n            # of the session. They are persisted as JSON (see\n            # _save_session_state) so a resume is a resume.\n            try:\n                _aux = json.loads(row.get("aux_json") or "{}")\n            except Exception:\n                _aux = {}\n            if isinstance(_aux, dict) and _aux:\n                for _k, _v in _aux.items():\n                    try:\n                        if (_k == "_straddle_hist"\n                                and isinstance(_v, list)):\n                            row[_k] = [\n                                (float(_p[0]), float(_p[1]))\n                                for _p in _v\n                                if isinstance(_p, (list, tuple))\n                                and len(_p) == 2\n                            ]\n                        elif _k not in row or row.get(_k) in (None, 0, 0.0):\n                            row[_k] = _v\n                    except (TypeError, ValueError, IndexError):\n                        continue\n\n            self.logger.info(\n                f"Session state loaded for {today_str} "\n                f"(mid-day restart recovery, entries={row.get(\'entry_count\', 0)})"\n            )\n            return dict(row)\n', 'V16-A persist the session anchors (load)'), ('data_engine.py', '        # Only update columns that exist in the table\n        try:\n            existing_cols = {\n                row[1] for row in\n                self.db.get_connection().execute(\n                    "PRAGMA table_info(session_state)"\n                ).fetchall()\n            }\n            data = {k: v for k, v in data.items() if k in existing_cols}\n        except Exception:\n            pass\n\n        self.db.update("session_state", data, {"trading_date": trading_date})\n', '        # Only update columns that exist in the table\n        existing_cols = set()\n        try:\n            existing_cols = {\n                row[1] for row in\n                self.db.get_connection().execute(\n                    "PRAGMA table_info(session_state)"\n                ).fetchall()\n            }\n        except Exception:\n            pass\n\n        # ── PATCH_V16: persist the anchors that have no column of their own ─\n        # PATCH_V15 and the v3.5 IV normalisation both depend on state that\n        # used to exist only in the process\'s memory, so a restart silently\n        # re-based the session: the IV change went back to a raw read, the\n        # gap reference went to 0, the straddle history emptied. They are\n        # written to a single JSON overflow column instead of six new\n        # columns - one ALTER, no migration ordering to get wrong, and any\n        # future in-memory-only key has somewhere to live. The column is\n        # added lazily on first save so an existing DB needs no separate\n        # migration step.\n        if existing_cols and "aux_json" not in existing_cols:\n            try:\n                self.db.get_connection().execute(\n                    "ALTER TABLE session_state ADD COLUMN aux_json TEXT"\n                )\n                self.db.get_connection().commit()\n                existing_cols.add("aux_json")\n            except Exception as _aux_err:\n                self.logger.debug(f"aux_json column add skipped: {_aux_err}")\n        if "aux_json" in existing_cols:\n            _aux_out = {}\n            for _k in (\n                "opening_iv_expiry", "opening_iv_rem_frac",\n                "_straddle_open_expiry", "_straddle_open_for_regime",\n                "_straddle_open_for_summary", "_last_atm_straddle",\n                "_prev_close_for_gap", "_straddle_hist",\n                "rv_anchor_pct", "rv_anchor_date", "first_bar_close",\n            ):\n                if _k not in data:\n                    continue\n                _v = data[_k]\n                try:\n                    if _k == "_straddle_hist" and isinstance(_v, (list, tuple)):\n                        _aux_out[_k] = [\n                            [float(_p[0]), float(_p[1])]\n                            for _p in list(_v)[-40:]\n                            if isinstance(_p, (list, tuple)) and len(_p) == 2\n                        ]\n                    elif _v is None or isinstance(_v, (str, int, float)):\n                        _aux_out[_k] = _v\n                except (TypeError, ValueError, IndexError):\n                    continue\n            try:\n                data["aux_json"] = json.dumps(_aux_out)\n            except Exception as _aux_dump_err:\n                self.logger.debug(f"aux_json not written: {_aux_dump_err}")\n\n        if existing_cols:\n            data = {k: v for k, v in data.items() if k in existing_cols}\n\n        self.db.update("session_state", data, {"trading_date": trading_date})\n', 'V16-B persist the session anchors (save)'), ('data_engine.py', '        _dte_iv   = self.state.get("actual_dte", 0)\n        _rem_base = self.state.get("opening_iv_rem_frac")\n        _tol = 1.0\n\n        if _dte_iv == 0 and _rem_base:\n', '        _dte_iv   = self.state.get("actual_dte", 0)\n        _rem_base = self.state.get("opening_iv_rem_frac")\n        _tol = 1.0\n\n        # ── PATCH_V16: a lost baseline is not a volatility spike ─────────\n        # On a 0DTE series the raw "IV against the open" read is\n        # meaningless: as T collapses the annualised IV rises on its own -\n        # this module\'s own v3.5 note measures +536% across one afternoon on\n        # a 90-point day. The normalised path (IV * sqrt(T_remaining))\n        # exists to remove exactly that, and it needs `opening_iv_rem_frac`,\n        # which used to live only in memory. A mid-session restart lost it\n        # and fell through to the RAW branch, outside every tolerance band:\n        # measured 2026-09-15 at 12:27:50, one cycle after an 8s gap,\n        # iv_change_pct_from_open went from -8.59 (DECLINING) to +28.52\n        # (SPIKING) with nothing happening in the market, and BOTH the\n        # strategy gate (iv_spiking) and the regime Gate 3 hard-block then\n        # refused premium selling for the remaining 427 cycles of a session\n        # this replay trades for +Rs 8,367. The fraction is persisted now\n        # (see _save_session_state); if it is still missing - an old row, a\n        # hand-built caller, a process started mid-session - report UNKNOWN\n        # (not a hard block) rather than a spike that is really time decay.\n        if _dte_iv == 0 and not _rem_base:\n            if not self.state.get("_iv_baseline_missing_logged"):\n                self.state["_iv_baseline_missing_logged"] = True\n                self.logger.warning(\n                    "IV baseline fraction missing on a 0DTE session - "\n                    "reporting iv_behavior=UNKNOWN this cycle instead of a "\n                    "raw read that turns time decay into a spike"\n                )\n            return "UNKNOWN", 0.0\n\n        if _dte_iv == 0 and _rem_base:\n', 'V16-C a lost IV baseline is UNKNOWN, not SPIKING'), ('data_engine.py', '        # Record opening straddle (once per session, after 09:30)\n        current_time = now_ist().time()\n        if (atm_straddle > 20 and\n                self.state.get("_straddle_open_for_regime", 0) == 0 and\n                current_time >= dtime(9, 30) and\n                not chain_stale and\n                atm_ce > 0 and atm_pe > 0):\n', '        # Record opening straddle (once per session, after 09:30)\n        current_time = now_ist().time()\n        # ── PATCH_V16: the opening straddle is an OPENING, not a snapshot ──\n        # The guard below is only as good as the row it was loaded from.\n        # Measured 2026-09-08: the anchor read 280.4pts at 11:43 and 175.0pts\n        # at 12:03 (the first cycle after a 1,189s gap) while\n        # `_straddle_open_for_regime` still held 280.4 - one session, two\n        # "openings". The day-move gauge divides by this number, so the same\n        # tape read 36.31% before the gap and 62.46% after it, and the block\n        # fires at 125: adopting a noon straddle as "the open" silently\n        # re-scales every remaining cycle of the session.\n        # Two changes: never re-record when EITHER anchor already carries a\n        # number, and - when the series the open came from is unknown - only\n        # adopt a straddle inside the opening-range window. Outside it the\n        # anchor stays 0, and a gauge with no denominator returns 0.0 (see\n        # _compute_day_move_used), which disables the gate instead of\n        # mis-scaling it: the conservative direction.\n        _open_series     = self.state.get("_straddle_open_expiry")\n        _open_window_v16 = current_time <= dtime(9, 45)\n        _no_anchor_v16   = (\n            self.state.get("_straddle_open_for_regime", 0) == 0\n            and float(self.state.get("opening_straddle_pts") or 0.0) <= 0.0\n        )\n        if (atm_straddle > 20 and\n                _no_anchor_v16 and\n                (_open_series or _open_window_v16) and\n                current_time >= dtime(9, 30) and\n                not chain_stale and\n                atm_ce > 0 and atm_pe > 0):\n', 'V16-D the opening straddle anchor is immutable')]




def _apply(check_only=False, verify_only=False):
    root = Path(__file__).resolve().parent
    if verify_only:
        return _verify(root)

    # ── 1. read every target and check every anchor BEFORE writing anything ──
    # A patch that writes file 1 and then fails on file 2 leaves a tree that
    # compiles nowhere and trades nothing. Nothing is touched until the whole
    # edit set has been shown to apply.
    files = {}
    staged = []
    skipped = 0
    for fname, old, new, tag in EDITS:
        path = root / fname
        if not path.exists():
            print(f"FAIL {tag}: {fname} not found - run this from the repository root")
            return 2
        if fname not in files:
            files[fname] = _read(path)
        text = files[fname]
        if new and new in text:
            print(f"skip {tag}: already applied")
            skipped += 1
            continue
        hits = text.count(old)
        if hits != 1:
            print(f"FAIL {tag}: anchor found {hits} time(s) in {fname} - not applying")
            print("     nothing has been written. Run  python patch17.py --verify"
                  "  to see if the tree is damaged, and restore it from git first.")
            return 3
        files[fname] = text.replace(old, new, 1)
        staged.append((fname, tag))
        print(f"ok   {tag}: anchored in {fname}")

    if not staged:
        print(f"\nNothing to do - all {skipped} edit(s) already present.")
        return _verify(root)
    if check_only:
        print(f"\ncheck complete: {len(staged)} edit(s) applicable, {skipped} present")
        return 0

    # ── 2. write atomically, then compile; roll back if anything is wrong ────
    touched = sorted({f for f, _ in staged})
    for fname in touched:
        _write_atomic(root / fname, files[fname])
        print(f"ok   wrote {fname} (backup: {fname}.pwbak)")
    failed = []
    for fname in touched:
        try:
            py_compile.compile(str(root / fname), doraise=True)
        except py_compile.PyCompileError as exc:
            print(f"FAIL {fname} does not compile after patching:")
            print(str(exc).splitlines()[-1])
            failed.append(fname)
    if failed:
        print("\nRolling back - the tree is being left exactly as it was:")
        _restore(root, touched)
        return 4

    print(f"\nPATCH applied ({len(staged)} edit(s), {skipped} already present); "
          f"{len(touched)} module(s) written, all compile.")
    print("Verify the tree and then replay:")
    print("  python patch17.py --verify")
    print("  python backtest_engine.py --db data/per_day --from 2026-09-08 --to 2026-09-16")
    return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    sys.exit(_apply(check_only="--check" in argv, verify_only="--verify" in argv))