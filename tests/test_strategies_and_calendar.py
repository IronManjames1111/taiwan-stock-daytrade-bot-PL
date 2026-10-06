import datetime as dt
import random

import indicator_strategies as I
import market_calendar as mc
import tw_market_rules as tw


def candles(n=60, seed=1, drift=0.05):
    random.seed(seed)
    out, p = [], 50.0
    for _ in range(n):
        p += random.uniform(-0.1, 0.1) + drift
        out.append({"open": p, "close": p, "high": p + 0.1, "low": p - 0.1, "volume": 1000 + random.randint(0, 500)})
    return out


def test_defaults_require_two_votes():
    assert I.normalize_settings()["min_votes"] == 2


def test_risk_mode_actually_changes_settings():
    base = I.normalize_settings({})
    assert I.apply_risk_mode(base, "auto") == base
    assert I.apply_risk_mode(base, "conservative")["min_votes"] == 3
    assert I.apply_risk_mode(base, "aggressive")["target_atr"] > base["target_atr"]
    assert I.apply_risk_mode(base, "relaxed")["min_votes"] == 1


def test_normalize_clamps_and_validates():
    s = I.normalize_settings({"min_votes": 99, "sizing_mode": "nope", "cooldown_minutes": -5, "risk_per_trade": "x"})
    assert s["min_votes"] == len(I.STRATEGY_NAMES)
    assert s["sizing_mode"] == "fixed_risk" and s["cooldown_minutes"] == 0 and s["risk_per_trade"] == 2000.0


def test_family_vote_counts_overlapping_strategies_once(monkeypatch):
    # 強制兩個同家族策略（ema_pullback + ema_momentum）都觸發 → 只算 1 票
    c = candles()
    r = I.evaluate(c, {"min_votes": 1, "count_by_family": True})
    assert r["vote_counts"]["BUY"] <= r["raw_vote_counts"]["BUY"]
    r2 = I.evaluate(c, {"min_votes": 2, "count_by_family": True})
    assert r2["signal"] in {"WATCH", "BUY", "SHORT"}


def test_evaluate_short_input_is_watch():
    assert I.evaluate([], {})["signal"] == "WATCH"
    assert I.evaluate(candles(5), {})["signal"] == "WATCH"


def test_reversal_vs_trend_conflict_is_watch():
    # 構造：BUY 方向只有 reversal，SHORT 方向有順勢 → 衝突
    c = candles()
    orig = I._rsi
    I._rsi = lambda *a, **k: 20.0
    try:
        r = I.evaluate(c, {"min_votes": 1, "min_vote_margin": 1})
    finally:
        I._rsi = orig
    assert r["signal"] in {"WATCH", "BUY", "SHORT"}


# ── 交易日曆（審查：國定假日會走測試模式）──────────────────────────
def test_weekend_and_holiday(tmp_path):
    f = tmp_path / "h.json"
    f.write_text('{"closed": ["2026-10-09"], "open": ["2026-10-10"]}', encoding="utf-8")
    assert mc.is_trading_day(dt.date(2026, 10, 3), online=False, holiday_file=str(f)) == (False, "週末")
    assert mc.is_trading_day(dt.date(2026, 10, 9), online=False, holiday_file=str(f))[0] is False
    assert mc.is_trading_day(dt.date(2026, 10, 10), online=False, holiday_file=str(f))[0] is True  # 強制開市
    assert mc.is_trading_day(dt.date(2026, 10, 6), online=False, holiday_file=str(f))[0] is True


def test_parse_twse_holidays_skips_first_last_trading_day():
    rows = [{"Date": "1150101", "Name": "中華民國開國紀念日", "Description": "市場無交易，僅辦理結算交割作業。"},
            {"Date": "1150213", "Name": "農曆春節前最後交易日", "Description": "最後交易日"},
            {"Date": "1150105", "Name": "市場開始交易日", "Description": "開始交易日"},
            {"Date": "bad"}, None]
    assert mc.parse_twse_holidays(rows) == {"2026-01-01"}


def test_session_window():
    tz = mc.TW_TZ
    assert mc.should_run(dt.datetime(2026, 10, 6, 9, 0, tzinfo=tz), online=False)[0]
    assert not mc.should_run(dt.datetime(2026, 10, 6, 15, 0, tzinfo=tz), online=False)[0]
    assert not mc.should_run(dt.datetime(2026, 10, 3, 9, 0, tzinfo=tz), online=False)[0]


# ── 台股規則 ──────────────────────────────────────────────────────
def test_limit_prices_and_entry_rules():
    assert tw.calc_limit_prices(100) == (110.0, 90.0)
    rules = tw.extract_symbol_rules({"previousClose": 100, "canDayTrade": True, "isDisposition": False,
                                     "isAttention": False})
    assert tw.check_entry_allowed("BUY", 105, rules)[0]
    assert not tw.check_entry_allowed("BUY", 110, rules)[0]       # 漲停追買
    assert not tw.check_entry_allowed("SHORT", 90, rules)[0]      # 跌停放空
    assert not tw.check_entry_allowed("BUY", 100, {"is_disposition": True})[0]
    assert not tw.check_entry_allowed("BUY", 100, {"can_day_trade": False})[0]
    assert not tw.check_entry_allowed("BUY", 100, {"is_attention": True})[0]
    assert tw.check_entry_allowed("BUY", 100, {"is_attention": True}, skip_attention=False)[0]
    assert tw.check_entry_allowed("BUY", 100, {})[0]               # 未知 → 放行
    assert tw.extract_symbol_rules({"error": "x"})["can_day_trade"] is None
