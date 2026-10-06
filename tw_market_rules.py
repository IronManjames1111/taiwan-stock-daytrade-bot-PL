# -*- coding: utf-8 -*-
"""
tw_market_rules.py - 台股交易規則小工具（從 gemini_service.py 抽出）

原本 headless_trader.py 只為了 _calc_limit_prices / _check_at_limit 兩個小函式，
就 import 了 181KB 的 gemini_service.py（連帶載入一大堆用不到的 AI 提示詞）。
這裡把「升降單位、漲跌停價、能不能當沖/放空」這些規則獨立成純函式，
不依賴任何第三方套件，方便單元測試。

注意：處置股、注意股、可否先賣後買等規則會隨主管機關公告調整，
本模組只採用語意明確的旗標（可否現股當沖、處置股、注意股、漲跌停）；
「可否先賣後買（放空）」的資格各券商 / 證交所公告不同，本模組不替你判斷，
請以證交所與券商公告為準。欄位缺漏時視為「未知」並放行。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from typing import Dict, Optional, Tuple


def tick_unit(price: float) -> Decimal:
    """台股升降單位（一般股票）。"""
    p = Decimal(str(price))
    if p < 10:
        return Decimal("0.01")
    if p < 50:
        return Decimal("0.05")
    if p < 100:
        return Decimal("0.1")
    if p < 500:
        return Decimal("0.5")
    if p < 1000:
        return Decimal("1")
    return Decimal("5")


def snap_tick(price: float, direction: str = "round") -> float:
    """把價格對齊到合法升降單位；direction = floor / ceil / round。"""
    p = Decimal(str(price))
    unit = tick_unit(price)
    rounding = {"floor": ROUND_DOWN, "ceil": ROUND_UP}.get(direction, ROUND_HALF_UP)
    return float((p / unit).to_integral_value(rounding=rounding) * unit)


def calc_limit_prices(prev_close: float) -> Optional[Tuple[float, float]]:
    """依昨收計算 (漲停價, 跌停價)。"""
    if not prev_close or prev_close <= 0:
        return None
    return snap_tick(prev_close * 1.1, "floor"), snap_tick(prev_close * 0.9, "ceil")


def check_at_limit(current_price: float, limit_up: float, limit_down: float,
                   tolerance: float = 0.005) -> str:
    """回傳 'up' / 'down' / ''（未在漲跌停附近）。"""
    if not current_price or not limit_up or not limit_down:
        return ""
    if abs(current_price - limit_up) <= limit_up * tolerance:
        return "up"
    if abs(current_price - limit_down) <= limit_down * tolerance:
        return "down"
    return ""


# ── 個股交易限制（取自 Fugle ticker）──────────────────────────────

def extract_symbol_rules(ticker: Optional[Dict], quote: Optional[Dict] = None) -> Dict:
    """
    把 Fugle ticker / quote 回傳整理成固定格式，欄位缺漏一律視為 None（未知）。

    Fugle v1.0 ticker 常見欄位：
      canDayTrade            是否可現股當沖
      isAttention            注意股
      isDisposition          處置股
      previousClose / referencePrice 昨收 / 參考價
    """
    t = ticker if isinstance(ticker, dict) and "error" not in ticker else {}
    q = quote if isinstance(quote, dict) and "error" not in quote else {}

    def _flag(key):
        v = t.get(key)
        return v if isinstance(v, bool) else None

    prev_close = (t.get("previousClose") or t.get("referencePrice")
                  or q.get("previousClose") or q.get("referencePrice"))
    try:
        prev_close = float(prev_close) if prev_close else None
    except (TypeError, ValueError):
        prev_close = None

    limits = calc_limit_prices(prev_close) if prev_close else None
    return {
        "can_day_trade": _flag("canDayTrade"),
        "is_attention": _flag("isAttention"),
        "is_disposition": _flag("isDisposition"),
        "prev_close": prev_close,
        "limit_up": limits[0] if limits else None,
        "limit_down": limits[1] if limits else None,
    }


def check_entry_allowed(side: str, price: float, rules: Optional[Dict],
                        skip_attention: bool = True) -> Tuple[bool, str]:
    """
    判斷此檔股票此刻能不能進場。回傳 (是否允許, 原因)。
    規則未知（None）時放行，避免 API 欄位缺漏就把全部訊號擋掉。
    """
    rules = rules or {}
    if rules.get("is_disposition"):
        return False, "處置股（交易受限）"
    if rules.get("can_day_trade") is False:
        return False, "不可現股當沖"
    if skip_attention and rules.get("is_attention"):
        return False, "注意股（可能被列為處置股，保守不進場）"
    lu, ld = rules.get("limit_up"), rules.get("limit_down")
    if lu and ld and price:
        at = check_at_limit(price, lu, ld)
        if at == "up" and side == "BUY":
            return False, f"價格已在漲停 {lu} 附近，追買難成交且出場風險高"
        if at == "down" and side == "SHORT":
            return False, f"價格已在跌停 {ld} 附近，放空難成交且回補風險高"
        if at == "down" and side == "BUY":
            return False, f"價格已在跌停 {ld} 附近，接刀風險高"
        if at == "up" and side == "SHORT":
            return False, f"價格已在漲停 {lu} 附近，放空風險高"
    return True, ""
