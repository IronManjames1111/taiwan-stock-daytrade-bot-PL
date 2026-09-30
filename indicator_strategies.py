"""Deterministic intraday signals and position exits used by the cloud runner."""
from __future__ import annotations

from math import ceil, floor, sqrt
from typing import Any


STRATEGY_NAMES = {
    "vwap_momentum": "VWAP 動能突破",
    "ema_pullback": "EMA 趨勢回檔",
    "rsi_reversal": "RSI 布林反轉",
    "macd_volume": "MACD 量能確認",
}


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(value * alpha + out[-1] * (1 - alpha))
    return out


def _rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(x, 0) for x in changes[-period:]]
    losses = [max(-x, 0) for x in changes[-period:]]
    avg_gain, avg_loss = sum(gains) / period, sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def evaluate(candles: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate four independent strategies on completed/available 1-minute bars.

    A signal requires a crossover/reclaim or multi-indicator confirmation; raw
    overbought/oversold readings alone do not create an entry.
    """
    valid = [c for c in candles if all(c.get(k) is not None for k in ("close", "high", "low"))]
    if len(valid) < 26:
        return {"signal": "WATCH", "strategy": "", "reason": "K線不足（至少需要 26 根）", "price": None}
    close = [float(c["close"]) for c in valid]
    high = [float(c["high"]) for c in valid]
    low = [float(c["low"]) for c in valid]
    volume = [float(c.get("volume", 0) or 0) for c in valid]
    price = close[-1]
    e9, e20 = _ema(close, 9), _ema(close, 20)
    e12, e26 = _ema(close, 12), _ema(close, 26)
    macd = [a - b for a, b in zip(e12, e26)]
    macd_signal = _ema(macd, 9)
    rsi = _rsi(close)
    true_ranges = [max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1])) for i in range(1, len(valid))]
    atr = sum(true_ranges[-14:]) / min(14, len(true_ranges))
    vwap_den = sum(volume)
    vwap = sum(((high[i] + low[i] + close[i]) / 3) * volume[i] for i in range(len(valid))) / vwap_den if vwap_den else price
    avg_vol = sum(volume[-21:-1]) / max(1, len(volume[-21:-1]))
    vol_ok = volume[-1] >= avg_vol * 1.2 if avg_vol > 0 else False

    signals: list[tuple[str, str, str]] = []
    if price > vwap and e9[-1] > e20[-1] and close[-2] <= vwap:
        signals.append(("vwap_momentum", "BUY", "站回 VWAP 且 EMA9 高於 EMA20"))
    elif price < vwap and e9[-1] < e20[-1] and close[-2] >= vwap:
        signals.append(("vwap_momentum", "SHORT", "跌破 VWAP 且 EMA9 低於 EMA20"))

    if e9[-1] > e20[-1] and low[-1] <= e9[-1] and price > e9[-1] and rsi is not None and 50 <= rsi < 70:
        signals.append(("ema_pullback", "BUY", "多頭排列回測 EMA9 後收復，RSI 動能偏多"))
    elif e9[-1] < e20[-1] and high[-1] >= e9[-1] and price < e9[-1] and rsi is not None and 30 < rsi <= 50:
        signals.append(("ema_pullback", "SHORT", "空頭排列反彈 EMA9 後轉弱，RSI 動能偏空"))

    window = close[-20:]
    mean = sum(window) / len(window)
    std = sqrt(sum((x - mean) ** 2 for x in window) / len(window))
    if rsi is not None and close[-2] <= mean - 2 * std and price > mean - 2 * std and 25 <= rsi <= 45:
        signals.append(("rsi_reversal", "BUY", "RSI 低檔且收盤重新站回布林下緣"))
    elif rsi is not None and close[-2] >= mean + 2 * std and price < mean + 2 * std and 55 <= rsi <= 75:
        signals.append(("rsi_reversal", "SHORT", "RSI 高檔且收盤跌回布林上緣"))

    if macd[-2] <= macd_signal[-2] and macd[-1] > macd_signal[-1] and price > vwap and vol_ok:
        signals.append(("macd_volume", "BUY", "MACD 黃金交叉、價格在 VWAP 上且量能放大"))
    elif macd[-2] >= macd_signal[-2] and macd[-1] < macd_signal[-1] and price < vwap and vol_ok:
        signals.append(("macd_volume", "SHORT", "MACD 死亡交叉、價格在 VWAP 下且量能放大"))

    # Only enter when at least two strategies agree; select a stable primary strategy.
    by_side: dict[str, list[tuple[str, str]]] = {"BUY": [], "SHORT": []}
    for name, side, reason in signals:
        by_side[side].append((name, reason))
    side = max(by_side, key=lambda x: len(by_side[x]))
    selected = by_side[side]
    if len(by_side["BUY"]) == len(by_side["SHORT"]) and by_side["BUY"]:
        return {"signal": "WATCH", "strategy": "", "reason": "多空策略同時出現，方向衝突不進場", "price": price, "atr": atr, "vwap": vwap, "rsi": rsi}
    if len(selected) < 2:
        return {"signal": "WATCH", "strategy": "", "reason": "策略共識不足（至少 2 個策略同向）", "price": price, "atr": atr, "vwap": vwap, "rsi": rsi}
    strategy, _ = selected[0]
    return {
        "signal": side,
        "strategy": strategy,
        "strategy_name": STRATEGY_NAMES[strategy],
        "strategy_votes": [STRATEGY_NAMES[name] for name, _ in selected],
        "reason": "；".join(reason for _, reason in selected),
        "price": price,
        "atr": atr,
        "vwap": vwap,
        "rsi": rsi,
    }


def position_levels(side: str, entry: float, atr: float) -> tuple[float, float]:
    risk = max(atr * 1.5, entry * 0.003)
    reward = max(atr * 2.25, risk * 1.5)
    stop, target = (entry - risk, entry + reward) if side == "BUY" else (entry + risk, entry - reward)
    def snap(price: float, mode: str) -> float:
        tick = 0.01 if price < 10 else 0.05 if price < 50 else 0.1 if price < 100 else 0.5 if price < 500 else 1.0 if price < 1000 else 5.0
        units = price / tick
        return round((floor(units + 1e-9) if mode == "floor" else ceil(units - 1e-9)) * tick, 2)
    if side == "BUY":
        return snap(stop, "floor"), snap(target, "ceil")
    return snap(stop, "ceil"), snap(target, "floor")
