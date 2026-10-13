"""Local intraday technical strategies and configurable risk levels."""
from __future__ import annotations

import json
from math import ceil, floor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

STRATEGY_NAMES = {
    "vwap_momentum": "VWAP 動能突破",
    "ema_pullback": "EMA 趨勢回檔",
    "rsi_reversal": "RSI 布林反轉",
    "macd_volume": "MACD 量能確認",
    "orb_breakout": "開盤區間突破",
    "ema_momentum": "EMA 快慢線動能",
    "stochastic_trend": "KD 順勢交叉",
    "range_breakout": "區間高低突破",
}

# 策略所屬「家族」：同一家族的策略高度重疊（多數都含 price > VWAP / EMA 方向），
# 多個同家族策略同時觸發只算 1 票，才是真正的「多重確認」。
STRATEGY_FAMILY = {
    "vwap_momentum": "vwap",
    "ema_pullback": "ema_trend",
    "ema_momentum": "ema_trend",
    "macd_volume": "momentum",
    "stochastic_trend": "momentum",
    "orb_breakout": "breakout",
    "range_breakout": "breakout",
    "rsi_reversal": "reversal",  # 逆勢策略，與其他順勢家族分開處理
}
REVERSAL_FAMILY = "reversal"

# 每日五組影子策略：組內所選訊號模組必須同方向同時成立才進場。
# 這些是可由 strategy_settings.json / 看板下載檔自由調整的起始搭配，並非宣稱高勝率保證。
DEFAULT_EXPERIMENT_STRATEGIES = [
    {"id": "strategy_1", "name": "VWAP + MACD 放量突破", "indicators": ["vwap_momentum", "macd_volume"]},
    {"id": "strategy_2", "name": "EMA 回檔 + KD 趨勢確認", "indicators": ["ema_pullback", "stochastic_trend"]},
    {"id": "strategy_3", "name": "開盤區間 + 區間突破", "indicators": ["orb_breakout", "range_breakout"]},
    {"id": "strategy_4", "name": "EMA 快慢線 + MACD 動能", "indicators": ["ema_momentum", "macd_volume"]},
    {"id": "strategy_5", "name": "RSI 布林反轉 + VWAP 確認", "indicators": ["rsi_reversal", "vwap_momentum"]},
]

DEFAULT_SETTINGS = {
    "enabled_strategies": {key: True for key in STRATEGY_NAMES},
    "min_votes": 2,              # 至少幾個「獨立家族」同向才進場（原為 1：任一策略觸發就進場）
    "min_vote_margin": 1,
    "count_by_family": True,     # True=同家族只算 1 票；False=沿用舊的逐策略計票
    "volume_multiple": 1.2,
    "min_bars": 14,
    "stop_atr": 1.5,             # 停損 ATR 倍數（v24：1.25→1.5，再與結構停損、最小停損%取大者）
    "target_atr": 1.8,           # 停利最低 ATR 倍數（v24 起僅作下限；主要由 target_rr 決定）
    "max_open_positions": 8,
    # ── 成本與風險控管（新增）──
    "min_target_cost_multiple": 3.0,  # 停利價差（毛）至少是來回成本的幾倍
    "min_net_rr": 1.0,                # 扣成本後 淨賺 / 淨賠至少 1:1
    "sizing_mode": "fixed_risk",      # fixed_risk / fixed_amount / fixed_shares
    "risk_per_trade": 2000.0,         # fixed_risk：每筆最大虧損（元，含成本）
    "position_amount": 200000.0,      # fixed_amount：每筆投入金額（元）
    "fixed_shares": 1000,             # fixed_shares：固定股數（舊行為）
    "max_position_value": 700000.0,   # 任一模式的單筆部位金額上限（元）
    "max_entries_per_symbol": 6,      # 0=不限制每日進場次數
    "cooldown_minutes": 10,           # 停損後同一檔冷卻分鐘（0=不冷卻）
    "cooldown_after_stop_only": True,
    "max_stops_per_symbol": 2,        # v24：同一檔當日停損達此次數就不再進場（0=不限制）
    "daily_loss_limit_pct": 1.0,      # v24：當日權益虧損達本金此 % 就停止新進場（0=不啟用）
    # ── v24 訊號品質與頻率 ──
    "volume_floor": 0.8,              # 量能下限：近 2 根均量 / 近 20 根均量 ≥ 此值（回檔類訊號不要求爆量）
    "signal_lookback": 3,             # 訊號有效視窗（根）：多個獨立確認不必落在同一根 K
    "min_efficiency": 0.12,           # 順勢訊號的「趨勢效率比」下限（過濾來回震盪；0=停用）
    "min_atr_pct": 0.10,              # 1 分 K ATR 占股價 % 的下限（波動太小不值得做）
    "solo_strategies": ["ema_pullback", "range_breakout", "orb_breakout", "vwap_momentum"],
    # ── v24 停損 / 停利 / 出場管理 ──
    "target_rr": 2.0,                 # 停利 = 停損距離 × 此倍數
    "min_target_rr": 1.3,             # 受當日高低點壓縮後，停利至少仍為停損的幾倍
    "min_stop_pct": 0.6,              # 停損距離下限（占進場價 %）：避免落在 1 分 K 雜訊內
    "max_stop_pct": 1.8,              # 停損距離上限（占進場價 %）
    "swing_bars": 8,                  # 結構停損：參考最近幾根 K 的高低點
    "swing_buffer_atr": 0.25,         # 結構停損再留的 ATR 緩衝
    "use_day_extreme_cap": True,      # 停利遇到當日高/低點時略為收斂
    "breakeven_trigger_r": 1.2,       # 浮盈達 1.2R 後把停損移到「保本＋成本」（0=停用）
    "trail_gap_r": 1.0,               # 保本啟動後，停損追在最有利價 −1.0R（0=不移動停損）
    "daily_capital": 1_000_000.0,     # 每天可用於當沖的本金（元）；每天開盤重置
    "allow_short": True,
    "skip_attention": True,           # 注意股一律不進場
    "experiment_strategies": DEFAULT_EXPERIMENT_STRATEGIES,
}

SIZING_MODES = ("fixed_risk", "fixed_amount", "fixed_shares")

# 風險模式（環境變數 / 工作流程選單 RISK_MODE）真正會改變的參數。
# auto = 完全使用 strategy_settings.json 的設定，不覆蓋。
RISK_MODE_PRESETS = {
    "auto": {},
    # v24：各模式不再把冷卻 / 每檔次數歸零（那會讓「停損後立刻同向再進」的來回洗刀失去保護）
    "aggressive": {"min_votes": 1, "volume_multiple": 1.1, "volume_floor": 0.7, "stop_atr": 1.5, "target_rr": 2.4,
                   "signal_lookback": 4, "min_net_rr": 1.0, "cooldown_minutes": 5, "max_stops_per_symbol": 3},
    "conservative": {"min_votes": 3, "stop_atr": 1.5, "target_rr": 1.8, "min_net_rr": 1.0, "signal_lookback": 2,
                     "cooldown_minutes": 15, "max_stops_per_symbol": 1, "min_efficiency": 0.2},
    "relaxed": {"min_votes": 1, "min_vote_margin": 1, "volume_multiple": 1.1, "volume_floor": 0.7, "min_net_rr": 1.0,
                "min_target_cost_multiple": 3.0, "signal_lookback": 3, "cooldown_minutes": 10,
                "max_stops_per_symbol": 2},
}

def normalize_experiment_strategies(raw=None) -> List[Dict]:
    """正規化恰好五組可編輯的指標組合，丟棄未知指標並去除重複值。"""
    source = raw if isinstance(raw, (list, tuple)) else []
    result = []
    for index, default in enumerate(DEFAULT_EXPERIMENT_STRATEGIES):
        item = source[index] if index < len(source) and isinstance(source[index], dict) else {}
        raw_indicators = item.get("indicators", item.get("enabled_indicators", default["indicators"]))
        if not isinstance(raw_indicators, (list, tuple)):
            raw_indicators = default["indicators"]
        indicators = list(dict.fromkeys(str(key) for key in raw_indicators if str(key) in STRATEGY_NAMES))
        name = str(item.get("name") or default["name"]).strip()[:48] or default["name"]
        result.append({"id": default["id"], "name": name, "indicators": indicators})
    return result


def experiment_settings(settings: Optional[Dict] = None) -> Dict[str, Dict]:
    """建立五組獨立影子設定；被勾選的訊號模組必須全數同方向成立。"""
    base = normalize_settings(settings)
    result = {}
    for combo in base["experiment_strategies"]:
        selected = combo["indicators"]
        enabled = {key: key in selected for key in STRATEGY_NAMES}
        mode_settings = dict(base)
        mode_settings.update({
            "enabled_strategies": enabled,
            "min_votes": max(2, len(selected)),
            "min_vote_margin": 1,
            "count_by_family": False,
            "max_open_positions": 20,
            "max_entries_per_symbol": 0,
            "cooldown_minutes": 0,
            "cooldown_after_stop_only": False,
        })
        result[combo["id"]] = {
            "name": combo["name"],
            "indicators": list(selected),
            "settings": normalize_settings(mode_settings),
        }
    return result


def apply_risk_mode(settings: Dict, mode: str) -> Dict:
    """把風險模式預設疊加到設定上（再經 normalize 夾住合法範圍）。"""
    merged = dict(settings or {})
    merged.update(RISK_MODE_PRESETS.get(mode, {}))
    return normalize_settings(merged)


def normalize_settings(settings: Optional[Dict] = None) -> Dict:
    source = settings or {}
    out = dict(DEFAULT_SETTINGS)
    enabled = dict(DEFAULT_SETTINGS["enabled_strategies"])
    enabled.update({k: bool(v) for k, v in (source.get("enabled_strategies") or {}).items() if k in enabled})
    out["enabled_strategies"] = enabled
    out["experiment_strategies"] = normalize_experiment_strategies(source.get("experiment_strategies"))
    bounds = {
        "min_votes": (1, len(STRATEGY_NAMES), int),
        "min_vote_margin": (1, len(STRATEGY_NAMES), int),
        "volume_multiple": (0.5, 5.0, float),
        "min_bars": (14, 120, int),
        "stop_atr": (0.5, 5.0, float),
        "target_atr": (0.5, 10.0, float),
        "max_open_positions": (1, 20, int),
        "min_target_cost_multiple": (0.0, 20.0, float),
        "min_net_rr": (0.0, 10.0, float),
        "risk_per_trade": (100.0, 1_000_000.0, float),
        "position_amount": (10_000.0, 10_000_000.0, float),
        "fixed_shares": (1, 100_000, int),
        "max_position_value": (10_000.0, 20_000_000.0, float),
        "max_entries_per_symbol": (0, 20, int),
        "cooldown_minutes": (0, 240, int),
        "daily_capital": (100_000.0, 1_000_000_000.0, float),
        "max_stops_per_symbol": (0, 20, int),
        "daily_loss_limit_pct": (0.0, 20.0, float),
        "volume_floor": (0.0, 3.0, float),
        "signal_lookback": (1, 10, int),
        "min_efficiency": (0.0, 1.0, float),
        "min_atr_pct": (0.0, 5.0, float),
        "target_rr": (0.5, 10.0, float),
        "min_target_rr": (0.5, 10.0, float),
        "min_stop_pct": (0.1, 5.0, float),
        "max_stop_pct": (0.2, 10.0, float),
        "swing_bars": (3, 60, int),
        "swing_buffer_atr": (0.0, 2.0, float),
        "breakeven_trigger_r": (0.0, 5.0, float),
        "trail_gap_r": (0.0, 5.0, float),
    }
    for key, (low, high, cast) in bounds.items():
        try:
            out[key] = max(low, min(high, cast(source.get(key, out[key]))))
        except (ValueError, TypeError):
            pass
    if out["max_stop_pct"] < out["min_stop_pct"]:
        out["max_stop_pct"] = out["min_stop_pct"]
    solo = source.get("solo_strategies", out["solo_strategies"])
    out["solo_strategies"] = [k for k in dict.fromkeys(solo) if k in STRATEGY_NAMES] if isinstance(solo, (list, tuple)) else list(DEFAULT_SETTINGS["solo_strategies"])
    for key in ("count_by_family", "allow_short", "skip_attention", "cooldown_after_stop_only", "use_day_extreme_cap"):
        if key in source:
            out[key] = bool(source[key])
    mode = source.get("sizing_mode", out["sizing_mode"])
    out["sizing_mode"] = mode if mode in SIZING_MODES else DEFAULT_SETTINGS["sizing_mode"]
    return out


def load_strategy_settings(path=None) -> Dict:
    path = Path(path) if path else Path(__file__).with_name("strategy_settings.json")
    try:
        with path.open(encoding="utf-8") as f:
            return normalize_settings(json.load(f))
    except (OSError, ValueError, TypeError):
        return normalize_settings()


def _ema(values: List[float], period: int) -> List[float]:
    alpha = 2 / (period + 1)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * float(value) + (1 - alpha) * result[-1])
    return result


def _rsi(values: List[float], period: int = 14) -> float:
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]
    if len(changes) < period:
        return 50.0
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    return 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)


def _bar_hm(bar: Dict) -> Optional[str]:
    t = str(bar.get("date") or bar.get("time") or "")
    if "T" in t:
        t = t.split("T", 1)[1]
    t = t.strip()
    return t[:5] if len(t) >= 5 and t[2] == ":" else None


def _efficiency(close: List[float], n: int = 20) -> float:
    """Kaufman 效率比：淨位移 / 路徑總長。接近 0 = 來回震盪，接近 1 = 單邊趨勢。"""
    if len(close) < n + 1:
        return 1.0
    window = close[-(n + 1):]
    path = sum(abs(window[i] - window[i - 1]) for i in range(1, len(window)))
    return abs(window[-1] - window[0]) / path if path > 0 else 0.0


def _snapshot(candles: List[Dict], cfg: Dict) -> Optional[Dict]:
    """以 candles 的最後一根為「當下」，算出所有指標與各策略當根觸發的票。"""
    try:
        close = [float(c["close"]) for c in candles]
        high = [float(c.get("high", c["close"])) for c in candles]
        low = [float(c.get("low", c["close"])) for c in candles]
        volume = [float(c.get("volume", 0) or 0) for c in candles]
    except (KeyError, TypeError, ValueError):
        return None
    price = close[-1]
    tr = [high[0] - low[0]] + [max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1])) for i in range(1, len(close))]
    atr = sum(tr[-14:]) / min(14, len(tr))
    typical = [(high[i] + low[i] + close[i]) / 3 for i in range(len(close))]
    total_volume = sum(volume)
    vwap = sum(typical[i] * volume[i] for i in range(len(close))) / total_volume if total_volume else price
    e9, e20 = _ema(close, 9), _ema(close, 20)
    e12, e26 = _ema(close, 12), _ema(close, 26)
    macd_line = [a - b for a, b in zip(e12, e26)]
    signal_line = _ema(macd_line, 9)
    rsi = _rsi(close)
    avg_vol = sum(volume[-21:-1]) / max(1, len(volume[-21:-1]))
    # v24：改用「近 2 根均量」比較，避免單根 1 分 K 的量忽大忽小造成大量訊號被「量能不足」擋掉
    vol_ratio = (sum(volume[-2:]) / len(volume[-2:])) / avg_vol if avg_vol > 0 else 9.9
    vol_ok = vol_ratio >= cfg["volume_multiple"]
    prev_price = close[-2]
    prev_vwap = sum(typical[i] * volume[i] for i in range(len(close)-1)) / max(1, sum(volume[:-1]))
    votes = {"BUY": [], "SHORT": []}

    def add(key, side, reason):
        if cfg["enabled_strategies"].get(key):
            votes[side].append({"key": key, "name": STRATEGY_NAMES[key], "reason": reason})

    if prev_price <= prev_vwap and price > vwap and price > e20[-1] and vol_ok:
        add("vwap_momentum", "BUY", "站回 VWAP 與 EMA20，量能確認")
    if prev_price >= prev_vwap and price < vwap and price < e20[-1] and vol_ok:
        add("vwap_momentum", "SHORT", "跌回 VWAP 與 EMA20 下方，量能確認")
    if e9[-1] > e20[-1] and price > vwap and low[-1] <= e9[-1] and close[-1] >= e9[-1]:
        add("ema_pullback", "BUY", "多頭排列中回測 EMA9 後收復")
    if e9[-1] < e20[-1] and price < vwap and high[-1] >= e9[-1] and close[-1] <= e9[-1]:
        add("ema_pullback", "SHORT", "空頭排列中反彈 EMA9 後轉弱")
    if len(close) >= 20:
        window = close[-20:]
        mean = sum(window) / 20
        sd = (sum((x-mean)**2 for x in window) / 20) ** 0.5
        if price < mean - 2*sd and rsi < 35:
            add("rsi_reversal", "BUY", "RSI 超賣且收盤低於布林下軌")
        if price > mean + 2*sd and rsi > 65:
            add("rsi_reversal", "SHORT", "RSI 過熱且收盤高於布林上軌")
    if macd_line[-2] <= signal_line[-2] and macd_line[-1] > signal_line[-1] and price > vwap and vol_ok:
        add("macd_volume", "BUY", "MACD 黃金交叉、VWAP 上方且量能確認")
    if macd_line[-2] >= signal_line[-2] and macd_line[-1] < signal_line[-1] and price < vwap and vol_ok:
        add("macd_volume", "SHORT", "MACD 死亡交叉、VWAP 下方且量能確認")
    if len(close) >= 17:
        opening_high, opening_low = max(high[:15]), min(low[:15])
        if close[-2] <= opening_high < price and price > vwap and vol_ok:
            add("orb_breakout", "BUY", "突破前15根開盤區間高點")
        if close[-2] >= opening_low > price and price < vwap and vol_ok:
            add("orb_breakout", "SHORT", "跌破前15根開盤區間低點")
    if e9[-2] <= e20[-2] and e9[-1] > e20[-1] and price > vwap:
        add("ema_momentum", "BUY", "EMA9 上穿 EMA20 且位於 VWAP 上方")
    if e9[-2] >= e20[-2] and e9[-1] < e20[-1] and price < vwap:
        add("ema_momentum", "SHORT", "EMA9 下穿 EMA20 且位於 VWAP 下方")
    if len(close) >= 16:
        raw_k = []
        for end in range(13, len(close)):
            lo, hi = min(low[end-13:end+1]), max(high[end-13:end+1])
            raw_k.append(50.0 if hi == lo else (close[end]-lo)/(hi-lo)*100)
        smooth_k = [sum(raw_k[max(0, i-2):i+1]) / len(raw_k[max(0, i-2):i+1]) for i in range(len(raw_k))]
        d_line = [sum(smooth_k[max(0, i-2):i+1]) / len(smooth_k[max(0, i-2):i+1]) for i in range(len(smooth_k))]
        k_now, k_prev = smooth_k[-1], smooth_k[-2]
        d_now, d_prev = d_line[-1], d_line[-2]
        if k_prev <= d_prev and k_now > d_now and k_now < 55 and e9[-1] > e20[-1] and price > vwap:
            add("stochastic_trend", "BUY", "KD 向上交叉並符合多頭趨勢")
        if k_prev >= d_prev and k_now < d_now and k_now > 45 and e9[-1] < e20[-1] and price < vwap:
            add("stochastic_trend", "SHORT", "KD 向下交叉並符合空頭趨勢")
    if len(close) >= 21:
        prior_high, prior_low = max(high[-21:-1]), min(low[-21:-1])
        if price > prior_high and close[-2] <= prior_high and price > vwap and vol_ok:
            add("range_breakout", "BUY", "放量突破前20根區間高點")
        if price < prior_low and close[-2] >= prior_low and price < vwap and vol_ok:
            add("range_breakout", "SHORT", "放量跌破前20根區間低點")
    return {"votes": votes, "price": price, "atr": atr, "vwap": vwap, "rsi": rsi, "e9": e9[-1], "e20": e20[-1],
            "vol_ratio": vol_ratio, "vol_ok": vol_ok, "efficiency": _efficiency(close), "hm": _bar_hm(candles[-1])}


def _stale_vote_ok(side: str, key: str, now: Dict, then: Dict) -> bool:
    """舊 K 觸發的訊號（age>0）仍可採用的條件：趨勢結構沒壞、價格沒有追太遠或回吐太多。"""
    if STRATEGY_FAMILY.get(key) == REVERSAL_FAMILY:
        return False  # 逆勢反轉只認當根
    sign = 1 if side == "BUY" else -1
    if sign * (now["price"] - now["vwap"]) <= 0 or sign * (now["e9"] - now["e20"]) <= 0:
        return False
    drift = sign * (now["price"] - then["price"])
    atr = max(now["atr"], 1e-9)
    return -0.5 * atr <= drift <= 1.0 * atr


def evaluate(candles: List[Dict], settings: Optional[Dict] = None) -> Dict:
    cfg = normalize_settings(settings)
    result = {"signal": "WATCH", "strategy_name": "", "strategy_matches": [], "strategy_votes": [],
              "strategy_votes_by_side": {"BUY": [], "SHORT": []}, "reason": "", "price": None, "atr": 0.0, "vwap": None}
    if not candles or len(candles) < cfg["min_bars"]:
        result["reason"] = f"K線不足（需至少 {cfg['min_bars']} 根）"
        return result
    now = _snapshot(candles, cfg)
    if now is None:
        result["reason"] = "K線資料格式無效"
        return result
    price, atr, vwap, rsi = now["price"], now["atr"], now["vwap"], now["rsi"]

    # ── v24：訊號視窗。金叉 / 突破 / 回測這類事件只會在「那一根」成立，多個獨立確認幾乎不可能剛好同一根，
    #    而且每輪只看最後一根時，輪詢稍有延遲就整根漏掉。這裡回看 signal_lookback 根，
    #    只要事件發生在視窗內、且現在趨勢結構仍在、價格沒追太遠，就算數。
    votes = {"BUY": [], "SHORT": []}
    seen = {"BUY": set(), "SHORT": set()}
    signal_hm = {"BUY": None, "SHORT": None}
    for age in range(cfg["signal_lookback"]):
        sub = candles[:len(candles) - age]
        if len(sub) < cfg["min_bars"]:
            break
        snap = now if age == 0 else _snapshot(sub, cfg)
        if snap is None:
            continue
        for side in ("BUY", "SHORT"):
            for v in snap["votes"][side]:
                if v["key"] in seen[side]:
                    continue
                if age > 0 and not _stale_vote_ok(side, v["key"], now, snap):
                    continue
                seen[side].add(v["key"])
                votes[side].append({**v, "age": age, "bar_hm": snap["hm"]})
                if snap["hm"] and (signal_hm[side] is None or snap["hm"] > signal_hm[side]):
                    signal_hm[side] = snap["hm"]

    def _count(side_key):
        if cfg["count_by_family"]:
            return len({STRATEGY_FAMILY.get(v["key"], v["key"]) for v in votes[side_key]})
        return len(votes[side_key])

    raw_buy, raw_short = len(votes["BUY"]), len(votes["SHORT"])
    buy_n, short_n = _count("BUY"), _count("SHORT")
    side = "BUY" if buy_n > short_n else "SHORT" if short_n > buy_n else "WATCH"
    count = max(buy_n, short_n)
    conflict = ""
    # 逆勢（RSI 反轉）與順勢策略方向相反時互相抵觸，寧可觀望，不讓它們互相「湊票」。
    for key_side, other in (("BUY", "SHORT"), ("SHORT", "BUY")):
        has_rev = any(STRATEGY_FAMILY.get(v["key"]) == REVERSAL_FAMILY for v in votes[key_side])
        other_trend = any(STRATEGY_FAMILY.get(v["key"]) != REVERSAL_FAMILY for v in votes[other])
        if has_rev and other_trend:
            conflict = "逆勢反轉訊號與順勢訊號方向相反，觀望"
    chosen_preview = votes.get(side, []) if side != "WATCH" else []
    only_reversal = bool(chosen_preview) and all(STRATEGY_FAMILY.get(v["key"]) == REVERSAL_FAMILY for v in chosen_preview)
    if side in {"BUY", "SHORT"} and not conflict:
        # 量能下限（v24：由「單根 ≥1.2 倍均量」改為「近 2 根均量 ≥ volume_floor 倍」；爆量要求只留在突破 / 動能類策略本身）
        if now["vol_ratio"] < cfg["volume_floor"]:
            conflict = f"量能不足（近2根均量僅達近期均量 {now['vol_ratio']:.2f} 倍，需 ≥ {cfg['volume_floor']:.1f}），觀望"
        elif price > 0 and atr / price * 100 < cfg["min_atr_pct"]:
            conflict = f"波動太小（1分K ATR 僅 {atr / price * 100:.2f}% < {cfg['min_atr_pct']}%），扣成本後沒空間，觀望"
        elif not only_reversal and now["efficiency"] < cfg["min_efficiency"]:
            conflict = f"盤整來回震盪（趨勢效率比 {now['efficiency']:.2f} < {cfg['min_efficiency']}），順勢訊號不做"
        elif count < 2 and chosen_preview and not all(v["key"] in cfg["solo_strategies"] for v in chosen_preview):
            weak = "、".join(v["name"] for v in chosen_preview if v["key"] not in cfg["solo_strategies"])
            conflict = f"{weak}屬落後型訊號，需第二個獨立確認才進場"
    if conflict or count < cfg["min_votes"] or side == "WATCH" or abs(buy_n - short_n) < cfg["min_vote_margin"]:
        side = "WATCH"
    chosen = votes.get(side, []) if side != "WATCH" else []
    unit = "家族" if cfg["count_by_family"] else "策略"
    wait_reason = conflict or f"獨立{unit}票數多空 {buy_n}:{short_n}（原始策略 {raw_buy}:{raw_short}），未達門檻 {cfg['min_votes']}"
    result.update(signal=side, strategy_name=chosen[0]["name"] if chosen else "", strategy_matches=[v["name"] for v in chosen], strategy_votes=chosen,
                  strategy_votes_by_side={"BUY": votes["BUY"], "SHORT": votes["SHORT"]},
                  reason="；".join(v["reason"] + (f"（{v['age']}根前）" if v.get("age") else "") for v in chosen) or wait_reason,
                  price=round(price, 2), atr=round(atr, 4), vwap=round(vwap, 2), rsi=round(rsi, 2),
                  vote_counts={"BUY": buy_n, "SHORT": short_n}, raw_vote_counts={"BUY": raw_buy, "SHORT": raw_short},
                  signal_bar_hm=signal_hm.get(side) if side != "WATCH" else None,
                  efficiency=round(now["efficiency"], 3), vol_ratio=round(now["vol_ratio"], 2))
    return result


def _tick(price: float) -> float:
    return 0.01 if price < 10 else 0.05 if price < 50 else 0.1 if price < 100 else 0.5 if price < 500 else 1.0 if price < 1000 else 5.0


def _snap(price: float, mode: str) -> float:
    tick = _tick(price)
    units = price / tick
    return round((floor(units + 1e-9) if mode == "floor" else ceil(units - 1e-9)) * tick, 2)


def position_plan(side: str, entry: float, atr: float, settings: Optional[Dict] = None,
                  candles: Optional[List[Dict]] = None) -> Dict:
    """
    v24 停損 / 停利計算。
      停損距離 = clamp( max(ATR×stop_atr, 近 swing_bars 根高低點結構停損), 最小%, 最大% )
        · 舊版只用 1 分 K ATR（約 0.3~0.4%），落在雜訊內，10/7 共 23 次停損中位數持有僅約 6 分鐘。
        · 結構停損 = 做多放在近期低點下方（做空相反）再加 ATR 緩衝：價格若真的跌破結構，原本的進場理由就失效。
      停利距離 = 停損距離 × target_rr（同時不低於 target_atr×ATR）；若當日高 / 低點就在前方且空間
        ≥ min_target_rr，就收斂到「當日極值＋0.5ATR」，避免掛在上方沒有人會買的價位。
    回傳 dict：stop / target / risk_dist / reward_dist / rr / risk_pct / basis（停損依據）。
    """
    cfg = normalize_settings(settings)
    entry = float(entry)
    long_side = side == "BUY"
    atr_eff = max(float(atr or 0), entry * 0.001)
    swing_dist = 0.0
    day_extreme = None
    if candles:
        try:
            highs = [float(c.get("high", c["close"])) for c in candles]
            lows = [float(c.get("low", c["close"])) for c in candles]
            closes = [float(c["close"]) for c in candles]
            trs = [highs[0] - lows[0]] + [max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
                                         for i in range(1, len(closes))]
            atr_eff = max(atr_eff, sum(trs[-30:]) / len(trs[-30:]))   # 取 14 根與 30 根 ATR 較大者，波動剛放大時不被低估
            n = cfg["swing_bars"]
            if long_side:
                swing_dist = entry - (min(lows[-n:]) - cfg["swing_buffer_atr"] * atr_eff)
                day_extreme = max(highs)
            else:
                swing_dist = (max(highs[-n:]) + cfg["swing_buffer_atr"] * atr_eff) - entry
                day_extreme = min(lows)
        except (KeyError, TypeError, ValueError):
            swing_dist, day_extreme = 0.0, None
    atr_dist = cfg["stop_atr"] * atr_eff
    lo, hi = cfg["min_stop_pct"] / 100 * entry, cfg["max_stop_pct"] / 100 * entry
    raw = max(atr_dist, swing_dist, lo)
    basis = "結構" if swing_dist >= max(atr_dist, lo) else "ATR" if atr_dist >= lo else "最小幅度"
    if raw > hi:
        raw, basis = hi, basis + "（已封頂）"
    stop = _snap(entry - raw, "floor") if long_side else _snap(entry + raw, "ceil")
    if abs(entry - stop) < 2 * _tick(entry):   # 至少 2 檔
        stop = _snap(entry - 2 * _tick(entry), "floor") if long_side else _snap(entry + 2 * _tick(entry), "ceil")
    risk = abs(entry - stop)

    reward = max(cfg["target_rr"] * risk, cfg["target_atr"] * atr_eff)
    if cfg["use_day_extreme_cap"] and day_extreme is not None:
        room = (day_extreme - entry) if long_side else (entry - day_extreme)
        if room >= cfg["min_target_rr"] * risk and room + 0.5 * atr_eff < reward:
            reward = room + 0.5 * atr_eff
            basis += "；停利收斂至當日極值"
    reward = max(reward, cfg["min_target_rr"] * risk)
    target = _snap(entry + reward, "floor") if long_side else _snap(entry - reward, "ceil")
    reward = abs(target - entry)
    return {"stop": stop, "target": target, "risk_dist": round(risk, 4), "reward_dist": round(reward, 4),
            "rr": round(reward / risk, 2) if risk else 0.0, "risk_pct": round(risk / entry * 100, 3), "basis": basis,
            "atr_eff": round(atr_eff, 4)}


def position_levels(side: str, entry: float, atr: float, settings: Optional[Dict] = None,
                    candles: Optional[List[Dict]] = None) -> Tuple[float, float]:
    plan = position_plan(side, entry, atr, settings, candles)
    return plan["stop"], plan["target"]
