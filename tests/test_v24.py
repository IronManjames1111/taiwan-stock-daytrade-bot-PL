"""v24：結構式停損停利、保本 / 移動停損、訊號視窗、停損次數上限、當日熔斷。"""
import indicator_strategies as I
import trade_engine as te


def bar(hm, o, h, l, c, v=100):
    return {"date": f"2026-10-12T{hm}:00.000+08:00", "open": o, "high": h, "low": l, "close": c, "volume": v}


def bars_flat(n, price=50.0, start=540, rng=0.1):
    return [bar(f"{(start + i) // 60:02d}:{(start + i) % 60:02d}", price, price + rng, price - rng, price) for i in range(n)]


# ── 停損 / 停利計算 ─────────────────────────────────────────────
def test_stop_never_inside_noise_floor_and_target_is_2r():
    p = I.position_plan("BUY", 51.3, 0.05, None, None)           # ATR 極小（10/7 的典型情況）
    assert p["risk_pct"] >= 0.6                                     # 下限 0.6%（舊版約 0.39%）
    assert abs(p["rr"] - 2.0) < 0.15
    assert p["stop"] < 51.3 < p["target"]


def test_short_plan_is_mirrored():
    b = I.position_plan("BUY", 83.8, 0.3, None, None)
    s = I.position_plan("SHORT", 83.8, 0.3, None, None)
    assert s["stop"] > 83.8 > s["target"]
    assert abs(b["risk_dist"] - s["risk_dist"]) < 1e-6


def test_structure_stop_uses_recent_swing_low_with_buffer():
    c = bars_flat(20, 50.0)
    c[-4] = bar("09:16", 50, 50.1, 49.0, 50.0)                    # 近 8 根內有一個 49.0 的低點
    p = I.position_plan("BUY", 50.0, 0.1, {"min_stop_pct": 0.3, "max_stop_pct": 3.0}, c)
    assert p["stop"] <= 48.9 and "結構" in p["basis"]


def test_stop_capped_at_max_pct():
    c = bars_flat(20, 50.0)
    c[-3] = bar("09:17", 50, 50.1, 40.0, 50.0)
    p = I.position_plan("BUY", 50.0, 0.1, {"max_stop_pct": 1.5}, c)
    assert p["risk_pct"] <= 1.6 and "封頂" in p["basis"]


def test_target_compresses_to_day_high_when_resistance_is_in_range():
    c = bars_flat(30, 50.0)
    c[5] = bar("09:05", 50, 51.6, 49.9, 50.0)                      # 當日高 51.6，距進場 1.6
    p = I.position_plan("BUY", 50.0, 0.1, {"min_stop_pct": 0.8, "target_rr": 5.0, "min_target_rr": 1.3}, c)
    assert 50.0 + 1.3 * p["risk_dist"] <= p["target"] < 50.0 + 5.0 * p["risk_dist"]
    assert p["target"] <= 51.75 and "收斂" in p["basis"]
    p2 = I.position_plan("BUY", 50.0, 0.1, {"min_stop_pct": 0.8, "target_rr": 5.0, "use_day_extreme_cap": False}, c)
    assert p2["target"] > p["target"]


def test_new_levels_pass_the_cost_gate_that_blocked_old_ones():
    """10/12 有 68 次被「停利僅成本 2.5 倍」、18 次被「淨賺賠比<1」擋下；新的距離應該能過關。"""
    cfg = I.normalize_settings({})
    for side, price, atr in (("BUY", 83.8, 0.3), ("SHORT", 82.0, 0.25), ("BUY", 120.5, 0.5), ("SHORT", 26.8, 0.06)):
        p = I.position_plan(side, price, atr, cfg, None)
        shares, why = te.calc_shares(side, price, p["stop"], cfg, 0.28, True)
        assert shares > 0, why
        m = te.evaluate_net_rr(side, price, p["stop"], p["target"], shares, 0.28, True)
        ok, why = te.check_cost_gate(m, cfg)
        assert ok, (side, price, m, why)


# ── 出場管理 ───────────────────────────────────────────────────
def managed(**kw):
    p = {"signal": "BUY", "entry_price": 50.0, "stop_loss": 49.5, "take_profit": 51.0, "shares": 1000,
         "entry_bar_hm": "09:20", "entry_time": "09:20:30", "symbol": "X",
         "risk_dist": 0.5, "be_trigger_r": 1.0, "trail_gap_r": 0.8,
         "lock_dist": te.breakeven_lock_dist("BUY", 50.0, 0.28, True)}
    p.update(kw)
    return p


def test_breakeven_lock_turns_a_would_be_loser_into_small_win():
    bars = [bar("09:21", 50.0, 50.55, 49.95, 50.4),   # 浮盈 1.1R，停損下一根起移到「進場＋成本」≈50.2
            bar("09:22", 50.4, 50.45, 49.9, 50.0)]    # 回落穿過 → 保本出場（舊版會一路掉到 49.5 停損）
    r = te.replay_position(managed(), bars)
    assert r["hit"]["reason"] == "hit_trail" and r["hit"]["price"] >= 50.15 and r["moved"]
    closed = te.close_position(managed(), r["hit"]["price"], r["hit"]["time"], r["hit"]["reason"], 0.28, True)
    assert closed["pnl_amount"] > 0 and closed["result"] == "win"
    legacy = te.scan_exit({k: v for k, v in managed().items() if k not in ("be_trigger_r", "trail_gap_r", "lock_dist", "risk_dist")}, bars)
    assert legacy is None   # 舊版：持倉繼續抱


def test_stop_move_only_takes_effect_next_bar_and_same_bar_hit_is_stop():
    bars = [bar("09:21", 50.0, 50.6, 49.4, 50.0)]       # 同一根：先創高又跌破原停損 → 判原停損
    assert te.replay_position(managed(), bars)["hit"]["reason"] == "hit_sl"


def test_trailing_follows_best_price_and_never_moves_down():
    bars = [bar("09:21", 50.0, 50.6, 50.0, 50.5),       # best 50.6 → 停損 max(50.1, 50.6-0.4=50.2)=50.2
            bar("09:22", 50.5, 50.9, 50.4, 50.8),       # best 50.9 → 停損 50.5
            bar("09:23", 50.8, 50.85, 50.45, 50.5)]     # 跌破 50.5
    r = te.replay_position(managed(), bars)
    assert r["hit"]["reason"] == "hit_trail" and 50.45 <= r["hit"]["price"] <= 50.55
    assert r["mfe"] >= 0.9


def test_take_profit_still_works_and_old_positions_unchanged():
    assert te.replay_position(managed(), [bar("09:21", 50, 51.2, 50, 51)])["hit"]["reason"] == "hit_tp"
    old = {k: v for k, v in managed().items() if k not in ("be_trigger_r", "trail_gap_r", "lock_dist", "risk_dist")}
    assert te.scan_exit(old, [bar("09:21", 50, 50.9, 50.0, 50.8), bar("09:22", 50.8, 50.9, 49.4, 49.5)])["reason"] == "hit_sl"


def test_short_side_breakeven():
    pos = managed(signal="SHORT", stop_loss=50.5, take_profit=49.0)
    bars = [bar("09:21", 50.0, 50.05, 49.45, 49.6), bar("09:22", 49.6, 50.0, 49.55, 49.9)]
    r = te.replay_position(pos, bars)
    assert r["hit"]["reason"] == "hit_trail" and r["hit"]["price"] <= 49.9


def test_excursions_recorded():
    x = te.excursion_extra(managed(), [bar("09:21", 50, 50.4, 49.8, 50.2), bar("09:22", 50.2, 50.3, 49.9, 50.1)])
    assert x["mfe_pct"] == 0.8 and x["mae_pct"] == 0.4


# ── 重複進場控管 / 熔斷 ─────────────────────────────────────────
SET = {"max_entries_per_symbol": 6, "cooldown_minutes": 10, "cooldown_after_stop_only": True, "max_stops_per_symbol": 2}


def closed(sym, reason, exit_time):
    return {"symbol": sym, "status": "closed", "exit_reason": reason, "exit_time": exit_time}


def test_stop_limit_per_symbol():
    t = [closed("A", "hit_sl", "09:30:00"), closed("A", "hit_sl", "10:00:00")]
    ok, why = te.can_enter("A", t, "11:00:00", SET)
    assert not ok and "停損" in why
    assert te.can_enter("B", t, "11:00:00", SET)[0]
    assert te.can_enter("A", t, "11:00:00", {**SET, "max_stops_per_symbol": 0})[0]


def test_stale_signal_older_than_last_exit_is_rejected():
    t = [closed("A", "hit_sl", "09:30:00")]
    ok, why = te.can_enter("A", t, "09:45:00", SET, signal_bar_hm="09:29")
    assert not ok and "早於" in why
    assert te.can_enter("A", t, "09:45:00", SET, signal_bar_hm="09:41")[0]


def test_daily_loss_breaker():
    losers = [{"symbol": "A", "status": "closed", "pnl_amount": -6000.0}, {"symbol": "B", "status": "closed", "pnl_amount": -4500.0}]
    hit, why = te.daily_loss_breached(1_000_000, losers, {}, 1.0)
    assert hit and "停止新進場" in why
    assert not te.daily_loss_breached(1_000_000, losers[:1], {}, 1.0)[0]
    assert not te.daily_loss_breached(1_000_000, losers, {}, 0)[0]


# ── 訊號視窗 ───────────────────────────────────────────────────
def _fake_snapshot_factory(events):
    """events: {距離最後一根的 age: [(side, key)]}；以 len(candles) 反推 age。"""
    def fake(candles, cfg):
        age = FAKE["n"] - len(candles)
        votes = {"BUY": [], "SHORT": []}
        for side, key in events.get(age, []):
            votes[side].append({"key": key, "name": I.STRATEGY_NAMES[key], "reason": "測試"})
        return {"votes": votes, "price": 50.0, "atr": 0.3, "vwap": 49.9, "rsi": 50.0, "e9": 50.1, "e20": 49.9,
                "vol_ratio": 1.5, "vol_ok": True, "efficiency": 0.5, "hm": candles[-1]["date"][11:16]}
    return fake


FAKE = {"n": 0}


def test_confirmation_may_arrive_a_few_bars_apart(monkeypatch):
    c = bars_flat(40)
    FAKE["n"] = len(c)
    monkeypatch.setattr(I, "_snapshot", _fake_snapshot_factory({2: [("BUY", "macd_volume")], 0: [("BUY", "ema_pullback")]}))
    r = I.evaluate(c, {"min_votes": 2, "signal_lookback": 3})
    assert r["signal"] == "BUY" and r["vote_counts"]["BUY"] == 2          # 不同根 K 的兩個獨立家族 → 成立
    assert I.evaluate(c, {"min_votes": 2, "signal_lookback": 1})["signal"] == "WATCH"   # 舊行為：必須同一根


def test_weak_lagging_signal_needs_confirmation_even_with_min_votes_1(monkeypatch):
    c = bars_flat(40)
    FAKE["n"] = len(c)
    monkeypatch.setattr(I, "_snapshot", _fake_snapshot_factory({0: [("BUY", "ema_momentum")]}))
    r = I.evaluate(c, {"min_votes": 1})
    assert r["signal"] == "WATCH" and "落後型" in r["reason"]
    monkeypatch.setattr(I, "_snapshot", _fake_snapshot_factory({0: [("BUY", "ema_pullback")]}))
    assert I.evaluate(c, {"min_votes": 1})["signal"] == "BUY"             # 回檔屬 solo 允許


def test_signal_bar_hm_is_freshest_vote_bar(monkeypatch):
    c = bars_flat(40)
    FAKE["n"] = len(c)
    monkeypatch.setattr(I, "_snapshot", _fake_snapshot_factory({2: [("BUY", "macd_volume")], 1: [("BUY", "ema_pullback")]}))
    r = I.evaluate(c, {"min_votes": 2})
    assert r["signal_bar_hm"] == c[-2]["date"][11:16]


def test_choppy_market_blocks_trend_signals(monkeypatch):
    c = bars_flat(40)
    FAKE["n"] = len(c)
    fake = _fake_snapshot_factory({0: [("BUY", "ema_pullback")]})
    monkeypatch.setattr(I, "_snapshot", lambda cs, cfg: {**fake(cs, cfg), "efficiency": 0.03})
    r = I.evaluate(c, {"min_votes": 1})
    assert r["signal"] == "WATCH" and "震盪" in r["reason"]


def test_stale_vote_dropped_when_price_ran_away(monkeypatch):
    c = bars_flat(40)
    FAKE["n"] = len(c)
    base = _fake_snapshot_factory({2: [("BUY", "range_breakout")]})
    monkeypatch.setattr(I, "_snapshot", lambda cs, cfg: {**base(cs, cfg), "price": 50.0 if len(cs) == len(c) else 48.0})
    assert I.evaluate(c, {"min_votes": 1})["signal"] == "WATCH"            # 現價比事件當根高 2.0 > 1 ATR，不追
