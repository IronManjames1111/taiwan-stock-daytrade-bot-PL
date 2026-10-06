import trade_engine as te

SET = {
    "min_target_cost_multiple": 3.0, "min_net_rr": 0.8, "sizing_mode": "fixed_risk", "risk_per_trade": 2000.0,
    "position_amount": 200000.0, "fixed_shares": 1000, "max_position_value": 700000.0,
    "max_entries_per_symbol": 5, "cooldown_minutes": 5, "cooldown_after_stop_only": True,
    "max_open_positions": 8,
}


def bar(hm, o, h, l, c, v=100):
    return {"date": f"2026-10-06T{hm}:00.000+08:00", "open": o, "high": h, "low": l, "close": c, "volume": v}


def long_pos(**kw):
    p = {"signal": "BUY", "entry_price": 52.8, "stop_loss": 52.5, "take_profit": 53.2, "shares": 1000,
         "entry_time": "09:05:30", "entry_bar_hm": "09:04", "symbol": "3481"}
    p.update(kw)
    return p


# ── 審查 #1：逐根掃描，不能只看最後一根 ─────────────────────────────
def test_scan_exit_catches_hit_in_middle_bars():
    bars = [bar("09:05", 52.8, 52.9, 52.7, 52.8),
            bar("09:06", 52.8, 53.3, 52.8, 53.0),   # ← 中間這根觸及停利
            bar("09:07", 53.0, 53.0, 52.9, 52.95),
            bar("09:08", 52.95, 53.0, 52.9, 52.95),
            bar("09:09", 52.95, 53.0, 52.9, 52.96)]  # 舊邏輯只看這根 → 漏掉
    hit = te.scan_exit(long_pos(), bars)
    assert hit == {"reason": "hit_tp", "price": 53.2, "time": "09:06:00"}


def test_scan_exit_same_bar_both_hit_is_stop():
    bars = [bar("09:05", 52.8, 53.5, 52.4, 52.8)]
    assert te.scan_exit(long_pos(), bars)["reason"] == "hit_sl"


def test_scan_exit_ignores_bars_before_or_at_entry_bar():
    bars = [bar("09:03", 52, 60, 40, 52), bar("09:04", 52, 60, 40, 52), bar("09:05", 52.8, 52.9, 52.7, 52.8)]
    assert te.scan_exit(long_pos(), bars) is None


def test_scan_exit_gap_down_fills_at_open():
    hit = te.scan_exit(long_pos(), [bar("09:05", 52.0, 52.1, 51.9, 52.0)])
    assert hit["reason"] == "hit_sl" and hit["price"] == 52.0


def test_scan_exit_short_side():
    pos = {"signal": "SHORT", "entry_price": 127, "stop_loss": 128, "take_profit": 126, "entry_bar_hm": "10:00"}
    assert te.scan_exit(pos, [bar("10:01", 127, 127.5, 125.9, 126.2)])["reason"] == "hit_tp"
    assert te.scan_exit(pos, [bar("10:01", 127, 128.1, 126.5, 127.5)])["reason"] == "hit_sl"


def test_scan_exit_last_hm_limit():
    bars = [bar("13:26", 52.8, 53.5, 52.8, 53.0)]
    assert te.scan_exit(long_pos(entry_bar_hm="13:00"), bars, last_hm="13:25") is None


def test_scan_exit_old_position_without_entry_bar_uses_entry_time():
    pos = long_pos()
    pos.pop("entry_bar_hm")
    assert te.scan_exit(pos, [bar("09:05", 52, 60, 40, 52)]) is None  # 同一分鐘不算
    assert te.scan_exit(pos, [bar("09:06", 52.8, 53.3, 52.8, 53)])["reason"] == "hit_tp"


# ── 審查 #3：成本與淨賺賠比 ────────────────────────────────────────
def test_cost_matches_review_example():
    m = te.evaluate_net_rr("BUY", 52.8, 52.5, 53.2, 1000, 0.28, True)
    assert m["cost"] == 122 and m["net_win"] == 278 and m["net_loss"] == 421
    assert abs(m["breakeven_winrate"] - 0.60) < 0.01


def test_cost_gate_rejects_review_example_and_accepts_good_trade():
    bad = te.evaluate_net_rr("BUY", 52.8, 52.5, 53.2, 1000, 0.28, True)
    ok, why = te.check_cost_gate(bad, SET)
    assert not ok and "淨賺賠比" in why
    good = te.evaluate_net_rr("BUY", 52.8, 52.5, 54.0, 1000, 0.28, True)
    assert te.check_cost_gate(good, SET)[0]


def test_short_tax_is_charged_on_sell_leg_entry():
    long_cost = te.round_trip_cost("BUY", 100, 101, 1000, 1.0)
    short_cost = te.round_trip_cost("SHORT", 100, 99, 1000, 1.0)
    assert long_cost > short_cost  # 做多賣在較高價，稅較多


# ── 固定風險部位 ──────────────────────────────────────────────────
def test_sizing_scales_with_risk_and_skips_too_risky():
    cheap, _ = te.calc_shares("BUY", 52.8, 52.5, SET, 0.28)
    assert cheap >= 2000
    expensive, why = te.calc_shares("BUY", 636, 634, SET, 0.28)
    assert expensive == 0 and "風險" in why
    # 每筆風險差距 12 倍的問題：兩者的最大虧損都不應超過上限太多
    assert te.net_pnl("BUY", 52.8, 52.5, cheap, 0.28) >= -SET["risk_per_trade"]


def test_sizing_fixed_shares_and_amount():
    assert te.calc_shares("BUY", 50, 49, {**SET, "sizing_mode": "fixed_shares", "fixed_shares": 3000})[0] == 3000
    assert te.calc_shares("BUY", 50, 49, {**SET, "sizing_mode": "fixed_amount", "position_amount": 120000})[0] == 2000
    assert te.calc_shares("BUY", 900, 890, {**SET, "sizing_mode": "fixed_amount", "position_amount": 120000})[0] == 0


# ── 重複進場（v23 放寬）──────────────────────────────────────────
def test_cooldown_only_after_stop_and_short():
    sl = [{"symbol": "3481", "status": "closed", "exit_time": "09:30:00", "exit_reason": "hit_sl"}]
    ok, why = te.can_enter("3481", sl, "09:33:00", SET)
    assert not ok and "冷卻" in why
    assert te.can_enter("3481", sl, "09:35:00", SET)[0]                       # 5 分鐘後即可
    tp = [{"symbol": "3481", "status": "closed", "exit_time": "09:30:00", "exit_reason": "hit_tp"}]
    assert te.can_enter("3481", tp, "09:31:00", SET)[0]                       # 停利後可立刻再進
    assert not te.can_enter("3481", tp, "09:31:00", {**SET, "cooldown_after_stop_only": False})[0]
    assert te.can_enter("3481", sl, "09:31:00", {**SET, "cooldown_minutes": 0})[0]


def test_daily_entry_limit_is_five_by_default():
    trades = [{"symbol": "3481", "status": "closed", "exit_time": "09:00:00", "exit_reason": "hit_tp"}] * 4
    assert te.can_enter("3481", trades, "11:00:00", SET)[0]
    ok, why = te.can_enter("3481", trades + trades[:1], "11:00:00", SET)
    assert not ok and "上限" in why
    assert te.can_enter("2330", trades, "11:00:00", SET)[0]


# ── 每日資金池 ────────────────────────────────────────────────────
def test_capital_snapshot_lock_and_release():
    open_t = {"symbol": "A", "status": "open", "signal": "BUY", "entry_price": 50.0, "shares": 2000}
    snap = te.capital_snapshot(1_000_000, [open_t], {"A": {"price": 50.0}}, 0.28)
    assert snap["locked"] == 100_000 and snap["cash"] == 900_000          # 進場先扣款
    assert snap["unrealized"] < 0                                         # 剛進場只有成本
    closed = te.close_position({**open_t, "stop_loss": 49, "take_profit": 51, "entry_time": "09:00:00"},
                               51.0, "09:10:00", "hit_tp", 0.28, True)
    snap2 = te.capital_snapshot(1_000_000, [closed], {}, 0.28)
    assert snap2["locked"] == 0 and snap2["cash"] == 1_000_000 + closed["pnl_amount"]   # 本金＋淨損益回補
    assert snap2["equity"] == snap2["cash"]


def test_capital_curve_starts_at_initial_and_dedupes():
    snap = te.capital_snapshot(1_000_000, [], {})
    cap = te.append_capital_point({}, "09:20:00", snap)
    cap = te.append_capital_point(cap, "09:20:00", snap)
    cap = te.append_capital_point(cap, "09:25:00", snap)
    assert [p["t"] for p in cap["curve"]] == ["09:00:00", "09:20:00", "09:25:00"]
    assert cap["curve"][0]["equity"] == 1_000_000


def _cand(sym, price, shares, votes, rr):
    return {"votes": votes, "position": {"symbol": sym, "signal": "BUY", "entry_price": price, "shares": shares,
                                           "stop_loss": price - 1.0, "take_profit": price + 3.0, "net_rr": rr}}


def test_allocation_priority_and_budget():
    cands = [_cand("LOW", 100.0, 4000, 1, 2.0), _cand("HIGH", 100.0, 4000, 3, 1.0), _cand("MID", 100.0, 4000, 2, 1.5)]
    out = te.allocate_candidates(cands, 900_000, 0, 8, 0.28, True, SET)
    order = [a["candidate"]["position"]["symbol"] for a in out]
    assert order == ["HIGH", "MID", "LOW"]                                # 票數多者優先
    assert out[0]["position"]["shares"] == 4000 and out[1]["position"]["shares"] == 4000
    # 剩 100,000：LOW 要 400,000 → 縮減到 1 張（買得起）
    assert out[2]["position"]["shares"] == 1000 and out[2]["position"].get("downsized")
    spent = sum(a["position"]["position_value"] for a in out if a["position"])
    assert spent <= 900_000


def test_allocation_skips_when_cannot_afford_one_lot_or_full():
    out = te.allocate_candidates([_cand("A", 100.0, 4000, 2, 1.0)], 50_000, 0, 8, 0.28, True, SET)
    assert out[0]["position"] is None and "資金不足" in out[0]["why"]
    out = te.allocate_candidates([_cand("A", 10.0, 1000, 2, 1.0)], 1_000_000, 8, 8, 0.28, True, SET)
    assert out[0]["position"] is None and "上限" in out[0]["why"]


def test_day_summary_and_upsert():
    cap = te.append_capital_point({}, "10:00:00", te.capital_snapshot(1_000_000, [], {}))
    tr = [te.close_position({"signal": "BUY", "entry_price": 50.0, "shares": 2000, "stop_loss": 49, "take_profit": 51,
                             "entry_time": "09:00:00", "symbol": "A"}, 51.0, "09:10:00", "hit_tp", 0.28, True)]
    summ = te.capital_day_summary("2026-10-06", cap, tr)
    assert summ["final"] == 1_000_000 + summ["pnl"] and summ["entries"] == 1 and summ["win_rate"] == 1.0
    days = te.upsert_day_summary([{"date": "2026-10-05", "pnl": 1}, {"date": "2026-10-06", "pnl": 99}], summ)
    assert [d["date"] for d in days] == ["2026-10-05", "2026-10-06"] and days[1]["pnl"] == summ["pnl"]


# ── K 棒 / 結算 / 壓縮 ───────────────────────────────────────────
def test_drop_incomplete_bar():
    bars = [bar("09:04", 1, 1, 1, 1), bar("09:05", 1, 1, 1, 1)]
    assert len(te.drop_incomplete_bar(bars, "09:05")) == 1   # 09:05 這根還在進行中
    assert len(te.drop_incomplete_bar(bars, "09:06")) == 2
    assert te.drop_incomplete_bar([], "09:06") == []


def test_normalize_candles_sorts_descending_input():
    bars = [bar("09:05", 1, 1, 1, 1), bar("09:04", 1, 1, 1, 1), {"junk": 1}]
    assert [te.bar_hm(b) for b in te.normalize_candles(bars)] == ["09:04", "09:05"]


def test_settle_records_include_stop_and_tp_trades():
    """這是儀表板「尚未結算」的根因：停損 / 停利的交易也必須出現在結算清單。"""
    trades = [te.close_position(long_pos(), 53.2, "09:06:00", "hit_tp", 0.28, True),
              te.close_position(long_pos(), 52.5, "09:20:00", "hit_sl", 0.28, True),
              {**long_pos(), "status": "open"}]
    recs = te.build_settle_records(trades)
    assert len(recs) == 2 and {r["exit_reason"] for r in recs} == {"hit_tp", "hit_sl"}
    assert te.summarize_trades(trades)["net_pnl"] == 278 - 421


def test_summarize_empty_has_no_division_by_zero():
    s = te.summarize_trades([])
    assert s["win_rate"] is None and s["net_pnl"] == 0


def test_compact_log_merges_consecutive_watch_only():
    log = []
    mk = lambda sig, t, r="r": {"symbol": "A", "signal": sig, "reason": r, "updated_at": t, "entry": 1}
    for t in ("09:05", "09:06", "09:07"):
        te.append_log_compact(log, mk("WATCH", t))
    assert len(log) == 1 and log[0]["repeat"] == 3 and log[0]["first_seen"] == "09:05"
    te.append_log_compact(log, mk("BUY", "09:08"))
    te.append_log_compact(log, mk("WATCH", "09:09"))
    assert len(log) == 3
