"""整合測試：用假的 Fugle 與假時鐘跑完整流程；全部在暫存目錄執行，不碰真實檔案。"""
import datetime as dt
import json
import os

import pytest

import headless_trader as ht
import trade_engine as te

TZ = ht.TW_TZ


def bar(hm, o, h, l, c, v=100):
    return {"date": f"2026-10-06T{hm}:00.000+08:00", "open": o, "high": h, "low": l, "close": c, "volume": v}


class FakeFugle:
    def __init__(self, candles):
        self.candles = candles   # symbol -> list
        self.ticker_calls = 0

    def get_intraday_candles(self, sym, force_refresh=False):
        return {"data": list(self.candles.get(sym, []))}

    def get_intraday_ticker(self, sym):
        self.ticker_calls += 1
        return {"previousClose": 50.0, "canDayTrade": True, "isDisposition": False, "isAttention": False}

    def get_intraday_quote(self, sym, force_refresh=False):
        return {"previousClose": 50.0}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clock = {"now": TZ.localize(dt.datetime(2026, 10, 6, 9, 10, 20))}
    monkeypatch.setattr(ht, "get_tw_now", lambda: clock["now"])
    monkeypatch.setattr(ht.market_calendar, "is_trading_day", lambda d, **k: (True, "交易日"))
    monkeypatch.setattr(ht, "get_free_top_volume_stocks",
                        lambda limit=8, **k: [{"symbol": "3481", "name": "群創", "price": 52.8, "volume": 9999, "rank": "1"}])
    return clock


def ctx_with(candles):
    fugle = FakeFugle(candles)
    return {"cfg": {"broker_discount": 0.28, "is_day_trade_tax": True}, "fugle": fugle, "gemini": ht._StrategyDisplay(),
            "risk_mode": "auto", "discount": 0.28, "day_tax": True, "force": False}, fugle


def flat_bars(start_min, n, price=52.8):
    out = []
    for i in range(n):
        m = start_min + i
        out.append(bar(f"{m // 60:02d}:{m % 60:02d}", price, price + 0.05, price - 0.05, price))
    return out


def force_signal(monkeypatch, sig="BUY", price=52.8, atr=0.6):
    monkeypatch.setattr(ht, "evaluate_strategies", lambda candles, s=None: {
        "signal": sig, "strategy_name": "測試策略", "reason": "測試", "price": price, "atr": atr,
        "strategy_votes": [], "strategy_matches": []})


def test_full_day_stop_and_tp_trades_appear_in_settlement_even_with_no_open_position(env, monkeypatch):
    """重現截圖：收盤時沒有未平倉部位，但今天有停損/停利出場 → 結算清單與損益卡不可是空的。"""
    candles = {"3481": flat_bars(9 * 60, 20)}          # 09:00 ~ 09:19
    ctx, fugle = ctx_with(candles)
    force_signal(monkeypatch)

    assert ht.run_once(ctx) == "ok"                    # 選股
    env["now"] = TZ.localize(dt.datetime(2026, 10, 6, 9, 20, 5))
    assert ht.run_once(ctx) == "ok"                    # 進場
    state = json.load(open("dashboard_state.json", encoding="utf-8"))
    assert len(state["open_positions"]) == 1, state["analysis_log"][-1]
    pos = state["open_positions"][0]
    assert pos["shares"] >= 1000 and pos["entry_bar_hm"] == "09:19"

    # 5 分鐘後：中間第 2 根 K 觸及停利，最後一根又回到區間內（舊邏輯只看最後一根會漏掉）
    tp = pos["take_profit"]
    candles["3481"] += [bar("09:20", 52.8, 52.9, 52.7, 52.8), bar("09:21", 52.8, tp + 0.2, 52.8, tp),
                        bar("09:22", tp, tp, 52.9, 52.95), bar("09:23", 52.95, 53.0, 52.9, 52.95),
                        bar("09:24", 52.95, 53.0, 52.9, 52.95), bar("09:25", 52.95, 53.0, 52.9, 52.95)]
    env["now"] = TZ.localize(dt.datetime(2026, 10, 6, 9, 25, 30))
    monkeypatch.setattr(ht, "evaluate_strategies", lambda c, s=None: {"signal": "WATCH", "reason": "-", "price": 52.9,
                                                                    "atr": 0.5, "strategy_name": ""})
    ht.run_once(ctx)
    state = json.load(open("dashboard_state.json", encoding="utf-8"))
    assert state["open_positions"] == []
    t = state["strategy_trades"][0]
    assert t["exit_reason"] == "hit_tp" and t["exit_time"] == "09:21:00" and t["pnl_amount"] > 0

    # 13:30 收盤結算：沒有未平倉，但清單必須有那筆停利
    env["now"] = TZ.localize(dt.datetime(2026, 10, 6, 13, 30, 46))
    candles["3481"] += flat_bars(9 * 60 + 26, 240, 52.95)
    assert ht.run_once(ctx) == "done"
    state = json.load(open("dashboard_state.json", encoding="utf-8"))
    assert state["settled_today"] is True
    snap = json.load(open("history_records/analysis_2026-10-06.json", encoding="utf-8"))
    assert len(snap["settle_records"]) == 1 and snap["settle_records"][0]["exit_reason"] == "hit_tp"
    html = open("index.html", encoding="utf-8").read()
    assert "尚未達到收盤結算時間" not in html.split('id="settle-tbody"')[1].split("</tbody>")[0]
    assert "已收盤結算完成" in html
    # 單一份交易明細
    assert os.path.exists("history_records/backtest_2026-10-06.csv")
    assert not os.path.exists("history_records/strategy_trades_2026-10-06.csv")
    # 結算後再被觸發不會重做
    assert ht.run_once(ctx) == "done"


def test_settlement_with_zero_trades_says_so_instead_of_not_yet(env, monkeypatch):
    ctx, _ = ctx_with({"3481": flat_bars(540, 250)})
    force_signal(monkeypatch, "WATCH")
    ht.run_once(ctx)                                   # 選股
    env["now"] = TZ.localize(dt.datetime(2026, 10, 6, 13, 26, 0))
    assert ht.run_once(ctx) == "done"
    html = open("index.html", encoding="utf-8").read()
    body = html.split('id="settle-tbody"')[1].split("</tbody>")[0]
    assert "沒有任何已平倉" in body and "尚未達到" not in body
    assert "今日無交易" in html


def test_forced_close_uses_fresh_candles_not_stale_quote(env, monkeypatch):
    candles = {"3481": flat_bars(540, 30)}
    ctx, fugle = ctx_with(candles)
    force_signal(monkeypatch)
    ht.run_once(ctx)
    env["now"] = TZ.localize(dt.datetime(2026, 10, 6, 9, 31, 5))
    ht.run_once(ctx)
    state = json.load(open("dashboard_state.json", encoding="utf-8"))
    assert state["open_positions"], "應該已進場"
    pos = state["open_positions"][0]
    # 之後價格在停損停利之間走到 52.9（live_quotes 仍停在舊價）
    candles["3481"] += flat_bars(570, 260, 52.9)
    env["now"] = TZ.localize(dt.datetime(2026, 10, 6, 13, 27, 0))
    assert ht.run_once(ctx) == "done"
    state = json.load(open("dashboard_state.json", encoding="utf-8"))
    t = state["strategy_trades"][0]
    assert t["exit_reason"] == "forced_close" and t["exit_price"] == 52.9 and t["exit_time"] == "13:25:00"


def test_cost_gate_blocks_marginal_trade_and_logs_reason(env, monkeypatch):
    ctx, _ = ctx_with({"3481": flat_bars(540, 30)})
    force_signal(monkeypatch, atr=0.12)
    # v24 起停損有 0.6% 下限、停利為 2 倍停損，不會再出現「只差幾檔」；改把成本倍數門檻拉高來驗證擋單與理由
    strict = {**ht.load_strategy_settings(), "min_target_cost_multiple": 12.0}
    monkeypatch.setattr(ht, "load_strategy_settings", lambda *a, **k: ht.normalize_settings(strict))
    ht.run_once(ctx)
    env["now"] = TZ.localize(dt.datetime(2026, 10, 6, 9, 31, 5))
    ht.run_once(ctx)
    state = json.load(open("dashboard_state.json", encoding="utf-8"))
    assert state["open_positions"] == []
    assert "未進場" in state["latest_analysis_records"][0]["reason"]


def test_no_reentry_during_cooldown_after_stop(env, monkeypatch):
    candles = {"3481": flat_bars(540, 30)}
    ctx, _ = ctx_with(candles)
    force_signal(monkeypatch)
    ht.run_once(ctx)
    env["now"] = TZ.localize(dt.datetime(2026, 10, 6, 9, 31, 5))
    ht.run_once(ctx)
    pos = json.load(open("dashboard_state.json", encoding="utf-8"))["open_positions"][0]
    # 09:32 K 棒跌破停損
    candles["3481"] += [bar("09:30", 52.8, 52.85, 52.75, 52.8), bar("09:31", 52.8, 52.85, 52.75, 52.8),
                        bar("09:32", 52.8, 52.8, pos["stop_loss"] - 0.3, pos["stop_loss"] - 0.2)]
    env["now"] = TZ.localize(dt.datetime(2026, 10, 6, 9, 33, 5))
    ht.run_once(ctx)                                   # 這輪：出場，且訊號仍是 BUY → 不得立刻再進
    state = json.load(open("dashboard_state.json", encoding="utf-8"))
    assert state["strategy_trades"][0]["exit_reason"] == "hit_sl"
    assert len(state["strategy_trades"]) == 1 and state["open_positions"] == []
    assert "冷卻" in state["latest_analysis_records"][0]["reason"]


def test_render_failure_does_not_lose_state(env, monkeypatch):
    ctx, _ = ctx_with({"3481": flat_bars(540, 30)})
    force_signal(monkeypatch)
    monkeypatch.setattr(ht, "render_html_dashboard", lambda **k: 1 / 0)   # 模擬渲染崩潰（除零）
    ht.run_once(ctx)
    env["now"] = TZ.localize(dt.datetime(2026, 10, 6, 9, 31, 5))
    ht.run_once(ctx)                                    # 不能拋出例外
    state = json.load(open("dashboard_state.json", encoding="utf-8"))
    assert len(state["open_positions"]) == 1            # 進場紀錄仍然存檔（審查 #2）


def test_holiday_does_not_overwrite_index(env, monkeypatch):
    monkeypatch.setattr(ht.market_calendar, "is_trading_day", lambda d, **k: (False, "國定假日"))
    ctx, _ = ctx_with({})
    open("index.html", "w").write("KEEP")
    assert ht.run_once(ctx) == "idle"
    assert open("index.html").read() == "KEEP"


def test_symbol_rules_cached_in_state_and_block_disposition(env, monkeypatch):
    ctx, fugle = ctx_with({"3481": flat_bars(540, 30)})
    fugle.get_intraday_ticker = lambda s: {"previousClose": 50.0, "isDisposition": True}
    force_signal(monkeypatch)
    ht.run_once(ctx)
    env["now"] = TZ.localize(dt.datetime(2026, 10, 6, 9, 31, 5))
    ht.run_once(ctx)
    state = json.load(open("dashboard_state.json", encoding="utf-8"))
    assert state["open_positions"] == [] and "處置股" in state["latest_analysis_records"][0]["reason"]
    assert "3481" in state["symbol_rules"]


def test_stale_state_rescue_does_not_refetch_candles(env, tmp_path):
    ctx, fugle = ctx_with({})
    fugle.get_intraday_candles = lambda *a, **k: (_ for _ in ()).throw(AssertionError("不可抓昨天的K線"))
    stale = {"date": "2026-10-05", "wave1_stocks": [{"symbol": "3481", "name": "群創", "price": 1}],
             "open_positions": [{"id": "x", "symbol": "3481", "signal": "BUY", "entry_price": 50.0, "stop_loss": 49.0,
                                 "take_profit": 52.0, "shares": 1000, "entry_time": "10:00:00"}],
             "strategy_trades": [{"id": "x", "symbol": "3481", "status": "open"}],
             "live_quotes": {"3481": {"price": 50.5}}, "settled_today": False}
    json.dump(stale, open("dashboard_state.json", "w", encoding="utf-8"))
    st = ht.load_dashboard_state("2026-10-06", ctx["gemini"], fugle, ctx["cfg"])
    assert st["date"] == "2026-10-06" and st["open_positions"] == []
    snap = json.load(open("history_records/analysis_2026-10-05.json", encoding="utf-8"))
    assert snap["settle_records"][0]["exit_price"] == 50.5


# ── 渲染邊界（審查：0 筆平倉、0 筆進場、引號）──────────────────────────
@pytest.mark.parametrize("kwargs", [
    {},
    {"strategy_trades": [{"symbol": "A", "status": "open", "strategy_name": "x"}]},                  # 0 筆平倉
    {"strategy_trades": [], "settle_records": [], "settled": True},                                     # 0 筆進場
    {"latest_analysis": [{"symbol": "A", "name": 'He said "hi" </script>', "signal": "BUY", "entry": 1,
                          "stop_loss": 1, "target": 2, "reason": "it's \"quoted\" ' \\ </script>", "updated_at": "09:00:00"}]},
])
def test_render_edge_cases(tmp_path, monkeypatch, kwargs):
    monkeypatch.chdir(tmp_path)
    ht.render_html_dashboard(**kwargs)
    html = open("index.html", encoding="utf-8").read()
    assert "<!DOCTYPE html>" in html
    assert html.count("</script>") == html.count("<script")  # 沒有被內容提前截斷


def test_loop_mode_stops_when_settled_and_survives_errors(env, monkeypatch):
    ctx, _ = ctx_with({"3481": flat_bars(540, 250)})
    calls = []
    seq = iter(["ok", RuntimeError("boom"), "ok", "done", "ok"])

    def fake_run_once(c):
        calls.append(1)
        v = next(seq)
        if isinstance(v, Exception):
            raise v
        return v

    monkeypatch.setattr(ht, "run_once", fake_run_once)
    monkeypatch.setattr(ht.time, "sleep", lambda s: None)
    monkeypatch.setenv("COMMIT_EVERY_MIN", "0")
    ht.run_loop(ctx)
    assert len(calls) == 4          # 出錯那一輪不會讓迴圈死掉；遇到 done 就結束


# ── v23：資金池 ──────────────────────────────────────────────────
def two_symbol_env(env, monkeypatch, stocks):
    monkeypatch.setattr(ht, "get_free_top_volume_stocks", lambda limit=8, **k: stocks)


def test_capital_deducted_on_entry_returned_on_exit_and_history_written(env, monkeypatch):
    candles = {"3481": flat_bars(540, 30)}
    ctx, _ = ctx_with(candles)
    force_signal(monkeypatch)
    ht.run_once(ctx)
    env["now"] = TZ.localize(dt.datetime(2026, 10, 6, 9, 31, 5))
    ht.run_once(ctx)
    st = json.load(open("dashboard_state.json", encoding="utf-8"))
    pos = st["open_positions"][0]
    cap = st["capital"]
    assert cap["initial"] == 1_000_000
    assert cap["locked"] == pos["position_value"] == round(pos["entry_price"] * pos["shares"])
    assert cap["cash"] == cap["initial"] - cap["locked"] + cap["realized"]          # 進場先扣款

    tp = pos["take_profit"]
    candles["3481"] += [bar("09:30", 52.8, 52.9, 52.7, 52.8), bar("09:31", 52.8, tp + 0.3, 52.8, tp)]
    env["now"] = TZ.localize(dt.datetime(2026, 10, 6, 9, 33, 5))
    monkeypatch.setattr(ht, "evaluate_strategies", lambda c, s=None: {"signal": "WATCH", "reason": "-", "price": 52.9,
                                                                    "atr": 0.5, "strategy_name": ""})
    ht.run_once(ctx)
    st = json.load(open("dashboard_state.json", encoding="utf-8"))
    cap = st["capital"]
    pnl = st["strategy_trades"][0]["pnl_amount"]
    assert cap["locked"] == 0 and cap["cash"] == 1_000_000 + pnl                    # 出場：本金＋淨損益回補
    assert len(cap["curve"]) >= 3

    env["now"] = TZ.localize(dt.datetime(2026, 10, 6, 13, 30, 40))
    candles["3481"] += flat_bars(572, 240, 52.9)
    assert ht.run_once(ctx) == "done"
    hist = json.load(open("history_records/capital_history.json", encoding="utf-8"))["days"]
    assert hist[-1]["date"] == "2026-10-06" and hist[-1]["pnl"] == pnl and hist[-1]["initial"] == 1_000_000
    snap = json.load(open("history_records/analysis_2026-10-06.json", encoding="utf-8"))
    assert snap["capital"]["equity"] == 1_000_000 + pnl
    html = open("index.html", encoding="utf-8").read()
    assert 'id="capital-card"' in html and "cap-daily-chart" in html and '"capital_history"' in html


def test_multiple_signals_share_one_capital_pool(env, monkeypatch):
    """資金只夠買一部分：優先買票數多的，其餘縮減或略過，總佔用不超過本金。"""
    stocks = [{"symbol": s, "name": s, "price": 100.0, "volume": 1, "rank": "1"} for s in ("1111", "2222", "3333")]
    two_symbol_env(env, monkeypatch, stocks)
    px = {"1111": 100.0, "2222": 100.1, "3333": 100.2}
    candles = {s: flat_bars(540, 30, px[s]) for s in px}
    ctx, fugle = ctx_with(candles)
    monkeypatch.setenv("DAILY_CAPITAL", "300000")                                     # 本金只有 30 萬
    votes = {"1111": 1, "2222": 3, "3333": 2}
    import indicator_strategies as I
    monkeypatch.setattr(ht, "evaluate_strategies", lambda c, s=None: None)
    ht.run_once(ctx)                                                                   # 選股

    def fake_eval(c, s=None):
        sym = [k for k, v in px.items() if abs(v - c[-1]["close"]) < 1e-6][0]
        return {"signal": "BUY", "strategy_name": "T", "reason": "r", "price": c[-1]["close"], "atr": 1.0,
                "vote_counts": {"BUY": votes[sym], "SHORT": 0}, "strategy_votes": []}
    monkeypatch.setattr(ht, "evaluate_strategies", fake_eval)
    monkeypatch.setattr(ht, "load_strategy_settings",
                        lambda: I.normalize_settings({"min_net_rr": 0, "min_target_cost_multiple": 0,
                                                      "risk_per_trade": 100000, "target_atr": 3.0}))
    env["now"] = TZ.localize(dt.datetime(2026, 10, 6, 9, 31, 5))
    ht.run_once(ctx)
    st = json.load(open("dashboard_state.json", encoding="utf-8"))
    syms = [p["symbol"] for p in st["open_positions"]]
    assert syms and syms[0] == "2222"                                                  # 票數最多先買
    assert st["capital"]["locked"] <= 300_000 and st["capital"]["cash"] >= 0
    skipped = [r for r in st["latest_analysis_records"] if "資金不足" in r["reason"] or "縮減" in r["reason"]]
    assert skipped, "資金用完後，其餘標的應註明未進場原因"


# ── v24 ──────────────────────────────────────────────────────────
def _open_one(env, monkeypatch, candles, **settings_override):
    base = ht.load_strategy_settings()
    ht_settings = ht.normalize_settings({**base, **settings_override})
    monkeypatch.setattr(ht, "load_strategy_settings", lambda *a, **k: ht_settings)
    ctx, _ = ctx_with(candles)
    force_signal(monkeypatch)
    ht.run_once(ctx)                                    # 選股
    env["now"] = TZ.localize(dt.datetime(2026, 10, 6, 9, 31, 5))
    ht.run_once(ctx)                                    # 進場
    return ctx, json.load(open("dashboard_state.json", encoding="utf-8"))


def test_position_carries_v24_levels_and_management_fields(env, monkeypatch):
    _, state = _open_one(env, monkeypatch, {"3481": flat_bars(540, 30)})
    pos = state["open_positions"][0]
    assert pos["risk_pct"] >= 0.6 and abs(pos["plan_rr"] - 2.0) < 0.2
    assert pos["be_trigger_r"] == 1.2 and pos["lock_dist"] > 0 and pos["risk_dist"] > 0
    assert pos["stop_loss"] < pos["entry_price"] < pos["take_profit"]


def test_breakeven_exit_in_full_flow_is_small_win_with_excursions(env, monkeypatch):
    candles = {"3481": flat_bars(540, 30)}
    ctx, state = _open_one(env, monkeypatch, candles)
    pos = state["open_positions"][0]
    entry, risk = pos["entry_price"], pos["risk_dist"]
    up = entry + 1.4 * risk                              # 浮盈 1.4R（已過保本啟動點、尚未到 2R 停利）
    candles["3481"] += [bar("09:30", 52.8, 52.85, 52.75, 52.8), bar("09:31", 52.8, 52.85, 52.75, 52.8),
                        bar("09:32", 52.8, up, 52.8, up - 0.05),
                        bar("09:33", up - 0.05, up - 0.05, entry - 0.01, entry)]   # 回到進場價 → 觸發保本停損
    monkeypatch.setattr(ht, "evaluate_strategies", lambda c, s=None: {"signal": "WATCH", "reason": "-", "price": 52.8,
                                                                    "atr": 0.5, "strategy_name": ""})
    env["now"] = TZ.localize(dt.datetime(2026, 10, 6, 9, 35, 5))
    ht.run_once(ctx)
    t = json.load(open("dashboard_state.json", encoding="utf-8"))["strategy_trades"][0]
    assert t["exit_reason"] == "hit_trail" and t["pnl_amount"] > 0 and t["result"] == "win"
    assert t["mfe_pct"] > 0 and "mae_pct" in t


def test_daily_loss_breaker_blocks_new_entries(env, monkeypatch):
    candles = {"3481": flat_bars(540, 30)}
    ctx, state = _open_one(env, monkeypatch, candles, cooldown_minutes=0, max_stops_per_symbol=0,
                           daily_loss_limit_pct=0.05)   # 0.05% = 500 元
    pos = state["open_positions"][0]
    candles["3481"] += [bar("09:30", 52.8, 52.85, 52.75, 52.8), bar("09:31", 52.8, 52.85, 52.75, 52.8),
                        bar("09:32", 52.8, 52.8, pos["stop_loss"] - 0.3, pos["stop_loss"] - 0.2)]
    env["now"] = TZ.localize(dt.datetime(2026, 10, 6, 9, 33, 5))
    ht.run_once(ctx)
    state = json.load(open("dashboard_state.json", encoding="utf-8"))
    assert state["strategy_trades"][0]["exit_reason"] == "hit_sl" and state["open_positions"] == []
    assert len(state["strategy_trades"]) == 1
    assert "停止新進場" in state["latest_analysis_records"][0]["reason"]


def test_settlement_saves_candle_snapshot(env, monkeypatch):
    import gzip
    ctx, _ = ctx_with({"3481": flat_bars(540, 250)})
    force_signal(monkeypatch, "WATCH")
    ht.run_once(ctx)
    env["now"] = TZ.localize(dt.datetime(2026, 10, 6, 13, 26, 0))
    assert ht.run_once(ctx) == "done"
    data = json.load(gzip.open("history_records/candles_2026-10-06.json.gz", "rt", encoding="utf-8"))
    assert data["cols"][0] == "hm" and len(data["candles"]["3481"]) == 250


# ── v25：盤中即時顯示已平倉 / 持倉 ──────────────────────────────
def _settle_tbody(html):
    return html.split('id="settle-tbody"')[1].split("</tbody>")[0]


def test_closed_trade_visible_before_settlement_and_open_one_in_monitor(env, monkeypatch):
    candles = {"3481": flat_bars(540, 30)}
    ctx, state = _open_one(env, monkeypatch, candles)
    pos = state["open_positions"][0]
    # 剛進場、尚未平倉：結算區是空的提示，持倉監控資料有這筆
    html = open("index.html", encoding="utf-8").read()
    assert "目前還沒有已平倉" in _settle_tbody(html) and "尚無已平倉" in html
    assert '"open_positions": [{' in html.replace("\n", "").replace("  ", "") or pos["symbol"] in html
    # 停利出場後（尚未到 13:25）：結算區立刻出現這筆，損益卡顯示盤中已實現
    tp = pos["take_profit"]
    candles["3481"] += [bar("09:30", 52.8, 52.85, 52.75, 52.8), bar("09:31", 52.8, 52.85, 52.75, 52.8),
                        bar("09:32", 52.8, tp + 0.1, 52.8, tp)]
    monkeypatch.setattr(ht, "evaluate_strategies", lambda c, s=None: {"signal": "WATCH", "reason": "-", "price": 52.8,
                                                                    "atr": 0.5, "strategy_name": ""})
    env["now"] = TZ.localize(dt.datetime(2026, 10, 6, 9, 35, 5))
    ht.run_once(ctx)
    html = open("index.html", encoding="utf-8").read()
    body = _settle_tbody(html)
    assert "3481" in body and "觸及停利" in body and "目前還沒有已平倉" not in body
    assert "已實現損益（盤中）" in html and "盤中即時" in html


def test_open_position_records_effective_stop_after_trailing(env, monkeypatch):
    candles = {"3481": flat_bars(540, 30)}
    ctx, state = _open_one(env, monkeypatch, candles)
    pos = state["open_positions"][0]
    entry, risk = pos["entry_price"], pos["risk_dist"]
    up = entry + 1.4 * risk
    candles["3481"] += [bar("09:30", 52.8, 52.85, 52.75, 52.8), bar("09:31", 52.8, 52.85, 52.75, 52.8),
                        bar("09:32", 52.8, up, 52.8, up - 0.05), bar("09:33", up - 0.05, up, up - 0.1, up - 0.05)]
    monkeypatch.setattr(ht, "evaluate_strategies", lambda c, s=None: {"signal": "WATCH", "reason": "-", "price": 52.8,
                                                                    "atr": 0.5, "strategy_name": ""})
    env["now"] = TZ.localize(dt.datetime(2026, 10, 6, 9, 35, 5))
    ht.run_once(ctx)
    p = json.load(open("dashboard_state.json", encoding="utf-8"))["open_positions"][0]
    assert p["stop_moved"] is True and p["current_stop"] > p["stop_loss"] and p["mfe_pct"] > 0


def test_page_shows_data_time_for_open_positions(env, monkeypatch):
    _open_one(env, monkeypatch, {"3481": flat_bars(540, 30)})
    html = open("index.html", encoding="utf-8").read()
    assert 'id="open-position-freshness"' in html and '"generated_at": "2026-10-06 09:31:05"' in html
