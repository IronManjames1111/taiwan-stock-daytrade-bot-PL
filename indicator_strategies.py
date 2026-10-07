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
    "stop_atr": 1.25,
    "target_atr": 1.8,
    "max_open_positions": 8,
    # ── 成本與風險控管（新增）──
    "min_target_cost_multiple": 3.0,  # 停利價差（毛）至少是來回成本的幾倍
    "min_net_rr": 1.0,                # 扣成本後 淨賺 / 淨賠至少 1:1
    "sizing_mode": "fixed_risk",      # fixed_risk / fixed_amount / fixed_shares
    "risk_per_trade": 2000.0,         # fixed_risk：每筆最大虧損（元，含成本）
    "position_amount": 200000.0,      # fixed_amount：每筆投入金額（元）
    "fixed_shares": 1000,             # fixed_shares：固定股數（舊行為）
    "max_position_value": 700000.0,   # 任一模式的單筆部位金額上限（元）
    "max_entries_per_symbol": 0,      # 0=不限制每日進場次數
    "cooldown_minutes": 0,            # 0=不限制出場後冷卻
    "cooldown_after_stop_only": False,
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
    "aggressive": {"min_votes": 2, "volume_multiple": 1.2, "stop_atr": 1.5, "target_atr": 3.0, "min_net_rr": 1.0,
                    "max_entries_per_symbol": 0, "cooldown_minutes": 0, "cooldown_after_stop_only": False},
    "conservative": {"min_votes": 3, "stop_atr": 0.8, "target_atr": 1.6, "min_net_rr": 1.0,
                     "max_entries_per_symbol": 0, "cooldown_minutes": 0, "cooldown_after_stop_only": False},
    "relaxed": {"min_votes": 1, "min_vote_margin": 1, "volume_multiple": 1.2, "min_net_rr": 1.0,
                "min_target_cost_multiple": 3.0, "max_entries_per_symbol": 0,
                "cooldown_minutes": 0, "cooldown_after_stop_only": False},
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
    }
    for key, (low, high, cast) in bounds.items():
        try:
            out[key] = max(low, min(high, cast(source.get(key, out[key]))))
        except (ValueError, TypeError):
            pass
    for key in ("count_by_family", "allow_short", "skip_attention", "cooldown_after_stop_only"):
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


def evaluate(candles: List[Dict], settings: Optional[Dict] = None) -> Dict:
    cfg = normalize_settings(settings)
    result = {"signal": "WATCH", "strategy_name": "", "strategy_matches": [], "strategy_votes": [],
              "strategy_votes_by_side": {"BUY": [], "SHORT": []}, "reason": "", "price": None, "atr": 0.0, "vwap": None}
    if not candles or len(candles) < cfg["min_bars"]:
        result["reason"] = f"K線不足（需至少 {cfg['min_bars']} 根）"
        return result
    try:
        close = [float(c["close"]) for c in candles]
        high = [float(c.get("high", c["close"])) for c in candles]
        low = [float(c.get("low", c["close"])) for c in candles]
        volume = [float(c.get("volume", 0) or 0) for c in candles]
    except (KeyError, TypeError, ValueError):
        result["reason"] = "K線資料格式無效"
        return result
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
    vol_ok = volume[-1] >= avg_vol * cfg["volume_multiple"] if avg_vol > 0 else True
    prev_price, prev_vwap = close[-2], sum(typical[i] * volume[i] for i in range(len(close)-1)) / max(1, sum(volume[:-1]))
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
    # 所有方向訊號都必須先通過量能確認，避免 EMA/KD 等未個別檢查量能的策略
    # 在成交量偏低、容易來回震盪時單獨觸發進場。
    if side in {"BUY", "SHORT"} and not vol_ok:
        conflict = f"量能不足（需達近期均量 {cfg['volume_multiple']:.1f} 倍），觀望"
    if conflict or count < cfg["min_votes"] or side == "WATCH" or abs(buy_n - short_n) < cfg["min_vote_margin"]:
        side = "WATCH"
    chosen = votes.get(side, []) if side != "WATCH" else []
    unit = "家族" if cfg["count_by_family"] else "策略"
    wait_reason = conflict or f"獨立{unit}票數多空 {buy_n}:{short_n}（原始策略 {raw_buy}:{raw_short}），未達門檻 {cfg['min_votes']}"
    result.update(signal=side, strategy_name=chosen[0]["name"] if chosen else "", strategy_matches=[v["name"] for v in chosen], strategy_votes=chosen,
                  strategy_votes_by_side={"BUY": votes["BUY"], "SHORT": votes["SHORT"]},
                  reason="；".join(v["reason"] for v in chosen) or wait_reason, price=round(price, 2), atr=round(atr, 4), vwap=round(vwap, 2), rsi=round(rsi, 2), vote_counts={"BUY": buy_n, "SHORT": short_n}, raw_vote_counts={"BUY": raw_buy, "SHORT": raw_short})
    return result


def position_levels(side: str, entry: float, atr: float, settings: Optional[Dict] = None) -> Tuple[float, float]:
    cfg = normalize_settings(settings)
    risk = max(float(atr or 0), float(entry) * 0.003)
    stop_distance, target_distance = risk * cfg["stop_atr"], risk * cfg["target_atr"]
    stop = entry-stop_distance if side == "BUY" else entry+stop_distance
    target = entry+target_distance if side == "BUY" else entry-target_distance
    def snap(price: float, mode: str) -> float:
        tick = 0.01 if price < 10 else 0.05 if price < 50 else 0.1 if price < 100 else 0.5 if price < 500 else 1.0 if price < 1000 else 5.0
        units = price / tick
        return round((floor(units + 1e-9) if mode == "floor" else ceil(units - 1e-9)) * tick, 2)
    if side == "BUY":
        return snap(stop, "floor"), snap(target, "ceil")
    return snap(stop, "ceil"), snap(target, "floor")
