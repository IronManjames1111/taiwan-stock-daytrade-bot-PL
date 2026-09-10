"""
Gemini AI 分析服務 v8.0
─────────────────────────────────────────────
v7.x → v8.0 提示詞優化重點（MTF 多時間框架模態共識系統）：
─────────────────────────────────────────────────────────────

【v8.0 核心改進：MTF 多時間框架模態共識分析（解決回測不準確問題）】

▌ 問題診斷（為什麼v7.0回測不準確）：
  舊版本過度依賴單一時間框架（1分K）的指標，缺乏對大框架趨勢的過濾。
  研究表明：單一時框當沖勝率約50-55%；三框共識可提升至65-70%。
  假突破率高的原因：未檢查高框架（15分K/日K）的壓力與趨勢方向。

▌ v8.0 核心新增：_build_prompt 內嵌 MTF 模態分析
  框架：日K（定方向）→ 15分K（定結構）→ 5分K（定動能）→ 1分K（執行）
  
  MTF 加權評分系統：
    日K趨勢 × 3（最重要，決定大方向）
    15分K趨勢 × 2（決定中期結構）
    5分K趨勢 × 2（決定短期動能）
    1分K趨勢 × 1（執行精度）
  
  MTF 共識判定：
    3框以上同向 = 多頭/空頭共識（進場加分 +4分）
    2框同向     = 偏多/空（進場加分 +2分）
    1框或衝突   = 觀望（禁止STRONG訊號）

▌ MTF 衝突偵測（最重要的防錯機制）：
  日K下降 + 低框上升 → 逆勢反彈警告，禁STRONG_BUY
  日K上升 + 低框下降 → 趨勢回調，等待VWAP回測再做多

▌ 綜合評分系統重構（v8.0）：
  舊：單一10項技術分（0-10分）→ 需3分才進場
  新：MTF共識分（0-4分）+ 技術分（0-10分）→ 需MTF≥1 + 技術≥3才進場

▌ v8.0 最高勝率進場模式（基於2025/2026研究）：
  VWAP回測進場模式：突破→回測VWAP縮量→收紅/黑確認→放量再突破
  此模式：停損更緊（確認K低點）、風報比更優、假突破率最低

▌ 精簡模式同步更新：
  新增 MTF 四框趨勢一行摘要（↑↓→箭頭表示方向）
  新增 MTF衝突禁STRONG規則

v6.x → v7.0 優化（保留）：
  低檔爆量SC三階段、MA黃金/死亡交叉評分、ORB三重確認框架

v3 → v6 優化（保留）：
  交易時段感知、VWAP/ATR/OBV/量比/開盤跳空/振幅、漲跌停計算
"""

import requests
import re
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP, ROUND_HALF_UP
from typing import Dict, Any, Optional, List
from datetime import datetime, time as dtime

GEMINI_API_URL    = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"

MAX_RETRIES = 3
RETRY_DELAY = 5
TIMEOUT_SEC = 150


# ─────────────────────────────────────────────
#  台股交易時段定義
# ─────────────────────────────────────────────
def _get_trade_stage(now: dtime = None) -> dict:
    if now is None:
        now = datetime.now().time()

    market_open  = dtime(9, 0)
    market_close = dtime(13, 30)

    if now < market_open or now >= market_close:
        return {
            "stage": "closed", "label": "非交易時間",
            "risk_level": "normal",
            "warning": "目前非交易時段，以下為盤後（或盤前）分析，供下一交易日參考。",
            "minutes_to_close": 0, "force_watch": False,
        }

    now_min   = now.hour * 60 + now.minute
    close_min = 13 * 60 + 30
    left      = close_min - now_min

    if now >= dtime(13, 15):
        return {
            "stage": "close_15", "label": "收盤前 15 分鐘（13:15-13:30）",
            "risk_level": "extreme",
            "warning": (
                "🚫【極高風險 — 強制觀望】距收盤僅剩 {} 分鐘！\n"
                "台股當沖規定：所有部位必須於 13:30 前完成沖銷，否則需付全額交割。\n"
                "此時段：⛔ 絕對禁止開立任何新當沖倉位。\n"
                "若目前持有部位：請立即評估出場，不可戀戰等反彈。\n"
                "SIGNAL 強制輸出 WATCH，僅提供持有部位的出場建議。"
            ).format(left),
            "minutes_to_close": left, "force_watch": True,
        }
    elif now >= dtime(13, 0):
        return {
            "stage": "close_45", "label": "收盤前 30～45 分鐘（13:00-13:15）",
            "risk_level": "high",
            "warning": (
                "⚠️【高風險時段】距收盤剩 {} 分鐘。\n"
                "當沖新開倉風險極高：若方向錯誤可能來不及出場。\n"
                "建議：✅ 已有獲利的倉位優先出清；❌ 不建議開立新倉。\n"
                "尾盤常見急拉或急殺，方向不明確時 SIGNAL 應輸出 WATCH。\n"
                "僅在趨勢極為明確（STRONG_BUY / STRONG_SHORT）時才考慮新倉。"
            ).format(left),
            "minutes_to_close": left, "force_watch": False,
        }
    elif now >= dtime(12, 30):
        return {
            "stage": "close_pre", "label": "尾盤前段（12:30-13:00）",
            "risk_level": "caution",
            "warning": (
                "⚠️【謹慎時段】距收盤剩 {} 分鐘，進入尾盤。\n"
                "當沖操作者開始準備出場，成交量逐漸放大，波動加劇。\n"
                "新倉須確認有足夠時間（至少 30 分鐘）完成進出，並嚴設停損。"
            ).format(left),
            "minutes_to_close": left, "force_watch": False,
        }
    elif now >= dtime(10, 30):
        return {
            "stage": "midday", "label": "中盤（10:30-12:30）",
            "risk_level": "normal",
            "warning": (
                "📊【中盤時段】距收盤剩 {} 分鐘。\n"
                "此時段量能通常縮減，波動偏小，以觀望為主。\n"
                "若進場需有明確突破或放量訊號，避免在盤整區追高殺低。"
            ).format(left),
            "minutes_to_close": left, "force_watch": False,
        }
    elif now >= dtime(9, 30):
        return {
            "stage": "morning", "label": "早盤（09:30-10:30）",
            "risk_level": "normal",
            "warning": (
                "✅【早盤黃金時段】距收盤剩 {} 分鐘。\n"
                "當沖最佳操作時段，趨勢逐漸明朗，有充足時間進出。\n"
                "可積極依技術訊號操作，仍需嚴守停損。"
            ).format(left),
            "minutes_to_close": left, "force_watch": False,
        }
    else:
        return {
            "stage": "open", "label": "開盤急攻（09:00-09:30）",
            "risk_level": "caution",
            "warning": (
                "⚡【開盤急攻時段】距收盤剩 {} 分鐘。\n"
                "開盤 30 分鐘內波動最劇烈，當日最高最低常在此出現。\n"
                "進場需謹慎，可等待 09:30 後趨勢較明朗再操作，"
                "或等待 ORB（開盤區間突破）確立後再跟進。"
            ).format(left),
            "minutes_to_close": left, "force_watch": False,
        }


# ─────────────────────────────────────────────
#  台股 Tick 工具（v6.1 改用 Decimal 消除浮點誤差）
# ─────────────────────────────────────────────
def _tick_unit(price: float) -> float:
    if price < 10:    return 0.01
    if price < 50:    return 0.05
    if price < 100:   return 0.1
    if price < 500:   return 0.5
    if price < 1000:  return 1.0
    return 5.0


def _tick_unit_decimal(price: float) -> Decimal:
    p = Decimal(str(price))
    if p < 10:    return Decimal("0.01")
    if p < 50:    return Decimal("0.05")
    if p < 100:   return Decimal("0.1")
    if p < 500:   return Decimal("0.5")
    if p < 1000:  return Decimal("1")
    return Decimal("5")


def _snap_tick(price: float, direction: str = "round") -> float:
    """
    【v6.1 修正】使用 Decimal 消除浮點精度問題。
    原版 (price // unit) * unit 對小數有精度誤差。
    """
    p = Decimal(str(price))
    unit = _tick_unit_decimal(price)
    if direction == "floor":
        snapped = (p / unit).to_integral_value(rounding=ROUND_DOWN) * unit
    elif direction == "ceil":
        snapped = (p / unit).to_integral_value(rounding=ROUND_UP) * unit
    else:
        snapped = (p / unit).to_integral_value(rounding=ROUND_HALF_UP) * unit
    return float(snapped)


def _tick_label(price: float) -> str:
    unit = _tick_unit(price)
    return {
        0.01: "0.01元（1分）",
        0.05: "0.05元（5分）",
        0.1:  "0.1元（1角）",
        0.5:  "0.5元（5角）",
        1.0:  "1元",
        5.0:  "5元",
    }.get(unit, f"{unit}元")


# ─────────────────────────────────────────────
#  當沖輔助指標計算
# ─────────────────────────────────────────────
def _calc_vwap(candles: List[Dict]) -> Optional[float]:
    """計算當日盤中 VWAP（成交量加權平均價）。"""
    total_pv = 0.0
    total_v  = 0.0
    for c in candles:
        h  = c.get("high",  c.get("close", 0))
        l  = c.get("low",   c.get("close", 0))
        cl = c.get("close", 0)
        v  = c.get("volume", 0)
        if v > 0:
            typical  = (h + l + cl) / 3.0
            total_pv += typical * v
            total_v  += v
    return round(total_pv / total_v, 2) if total_v > 0 else None


def _calc_atr(candles: List[Dict], period: int = 14) -> Optional[float]:
    """
    計算標準 Wilder ATR（v6.1 與 fugle_service 統一）。
    period=14，Wilder 指數平滑。
    """
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        curr = candles[i]
        prev = candles[i - 1]
        h    = curr.get("high",  curr.get("close", 0))
        l    = curr.get("low",   curr.get("close", 0))
        pc   = prev.get("close", curr.get("close", 0))
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    # Wilder 平滑
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return round(atr_val, 2)


def _calc_obv_trend(candles: List[Dict], lookback: int = 10) -> str:
    """
    計算 OBV（能量潮）趨勢方向。

    v6.2 修正：先對全部 candles 累積完整 OBV 序列，
    再取最後 lookback 段比較前後半段均值，
    避免截斷後從 0 重算導致歷史量能資訊丟失。
    """
    if len(candles) < 3:
        return "資料不足"

    # 第一步：對完整序列累積 OBV
    obv = 0.0
    full_obvs = []
    for i in range(1, len(candles)):
        curr_c = candles[i].get("close", 0)
        prev_c = candles[i - 1].get("close", 0)
        vol    = candles[i].get("volume", 0)
        if curr_c > prev_c:
            obv += vol
        elif curr_c < prev_c:
            obv -= vol
        full_obvs.append(obv)

    # 第二步：取最後 lookback 段做趨勢判斷
    recent_obvs = full_obvs[-min(len(full_obvs), lookback):]
    if len(recent_obvs) < 2:
        return "資料不足"

    mid       = max(len(recent_obvs) // 2, 1)
    avg_first = sum(recent_obvs[:mid]) / mid
    avg_last  = sum(recent_obvs[mid:]) / max(len(recent_obvs) - mid, 1)
    diff      = avg_last - avg_first

    if diff > 0:
        return f"上升（資金流入，OBV 近 {lookback} 根走升）"
    elif diff < 0:
        return f"下降（資金流出，OBV 近 {lookback} 根走降）"
    return "持平（多空拮抗）"


def _calc_volume_ratio(candles: List[Dict]) -> Optional[str]:
    """計算量比（今日均量 / 前半段均量）。"""
    if len(candles) < 6:
        return None
    vols      = [c.get("volume", 0) for c in candles]
    half      = max(len(vols) // 2, 1)
    ref_avg   = sum(vols[:half]) / half
    today_avg = sum(vols) / len(vols)
    if ref_avg == 0:
        return None
    ratio = today_avg / ref_avg
    if ratio >= 2.0:
        label = "爆量（>2倍，注意主力動向）"
    elif ratio >= 1.5:
        label = "放量（1.5～2倍，有方向性）"
    elif ratio >= 1.0:
        label = "正常量"
    else:
        label = "縮量（<1倍，觀望氣氛）"
    return f"{ratio:.1f} 倍（{label}）"

def _detect_volume_climax(candles: List[Dict], lookback: int = 20, multiplier: float = 2.0) -> Optional[dict]:
    """
    偵測「量能耗竭（Volume Climax）」：
    在一段上漲後出現爆量K棒，可能是 Buying Climax（主力出貨訊號）。
    在一段下跌後出現爆量K棒，可能是 Selling Climax（恐慌殺盤耗竭）。
    
    回傳 dict 或 None：
      type: "buying_climax" / "selling_climax" / None
      volume_ratio: 爆量倍數
      candle_index: 發生在倒數第幾根
      price_position: "high" / "low" / "mid"（現價在日內的位置）
    """
    if len(candles) < lookback + 3:
        return None
    
    vols = [c.get("volume", 0) for c in candles]
    avg_vol = sum(vols[-lookback-5:-5]) / lookback if len(vols) >= lookback + 5 else sum(vols[:-5]) / max(len(vols) - 5, 1)
    if avg_vol <= 0:
        return None
    
    # 找最近 5 根 K 棒中是否有爆量
    recent_climax_idx = None
    climax_ratio = 0.0
    for i in range(-5, 0):
        v = vols[i]
        ratio = v / avg_vol
        if ratio >= multiplier and ratio > climax_ratio:
            climax_ratio = ratio
            recent_climax_idx = i
    
    if recent_climax_idx is None:
        return None
    
    # 判斷爆量前的趨勢方向
    trend_window = candles[-(lookback):recent_climax_idx] if recent_climax_idx < -1 else candles[-lookback:-1]
    if not trend_window:
        return None
    
    closes = [c.get("close", 0) for c in trend_window]
    if len(closes) < 3:
        return None
    
    # 簡單趨勢判斷：前段均值 vs 後段均值
    mid = len(closes) // 2
    avg_early = sum(closes[:mid]) / mid
    avg_late = sum(closes[mid:]) / max(len(closes) - mid, 1)
    trend_up = avg_late > avg_early * 1.005  # 上漲趨勢
    trend_dn = avg_late < avg_early * 0.995  # 下跌趨勢
    
    climax_candle = candles[recent_climax_idx]
    c_open  = climax_candle.get("open",  0)
    c_close = climax_candle.get("close", 0)
    c_high  = climax_candle.get("high",  c_close)
    c_low   = climax_candle.get("low",   c_close)
    c_range = c_high - c_low if c_high > c_low else 0.001
    
    # 現價在日內高低的位置（用於判斷是否在高檔）
    current_price = candles[-1].get("close", 0)
    day_high = max(c.get("high", 0) for c in candles)
    day_low  = min(c.get("low", 0) for c in candles)
    day_range = day_high - day_low if day_high > day_low else 0.001
    price_pos_pct = (current_price - day_low) / day_range * 100
    
    if price_pos_pct >= 70:
        price_position = "high"  # 現價在日內高檔區（≥70%）
    elif price_pos_pct <= 30:
        price_position = "low"   # 現價在日內低檔區（≤30%）
    else:
        price_position = "mid"
    
    climax_type = None
    warning_text = ""
    
    if trend_up and c_close >= c_open:
        # 上漲趨勢中出現爆量紅K → Buying Climax（最危險的追多陷阱）
        climax_type = "buying_climax"
        warning_text = (
            f"⚠️【高點爆量警告 — Buying Climax】\n"
            f"  近{abs(recent_climax_idx)}根前出現 {climax_ratio:.1f} 倍爆量K棒（均量的 {climax_ratio:.1f} 倍）\n"
            f"  現價在日內 {price_pos_pct:.0f}% 高檔區域，屬於「高點爆量」。\n"
            f"  ⛔ 此形態常見主力出貨（Buying Climax）訊號，逆勢做多風險極高！\n"
            f"  策略建議：\n"
            f"    • 做多：⛔ 禁止高點追多，若爆量後出現上影線/反轉K棒，可考慮放空\n"
            f"    • 放空：等待下一根K棒確認（收黑、量縮）再進場，停損設爆量K棒高點上方\n"
            f"    • 若後續量縮且股價整理不跌 → 可能是真突破（強勢股），再重新評估"
        )
    elif trend_dn and c_close < c_open:
        # 下跌趨勢中出現爆量黑K → Selling Climax（恐慌殺盤耗竭，反彈機會）
        climax_type = "selling_climax"

        # ── v7.0：三階段確認判斷 ──────────────────────────────────
        # 取爆量K棒之後的後續K棒（最多3根）
        climax_abs_idx = len(candles) + recent_climax_idx  # 爆量K棒在candles的正向index
        post_candles = candles[climax_abs_idx + 1:] if climax_abs_idx + 1 < len(candles) else []

        sc_confirm_count = 0  # 確認訊號計數
        sc_confirm_notes = []

        if post_candles:
            pc = post_candles[-1]  # 最新的後續K棒
            pc_open  = pc.get("open",  pc.get("close", 0))
            pc_close = pc.get("close", 0)
            pc_high  = pc.get("high",  pc_close)
            pc_low   = pc.get("low",   pc_close)
            pc_vol   = pc.get("volume", 0)
            pc_body  = abs(pc_close - pc_open)
            pc_lower_wick = min(pc_close, pc_open) - pc_low
            avg_vol_ref = avg_vol  # 重用爆量前的均量

            # 確認a：縮量收紅（量縮價穩）
            is_red = pc_close > pc_open
            rvol_post = pc_vol / avg_vol_ref if avg_vol_ref > 0 else 1.0
            if is_red and rvol_post < 0.8:
                sc_confirm_count += 1
                sc_confirm_notes.append(f"✅ 縮量收紅（後續量僅 {rvol_post:.1f}x，賣盤耗竭）")

            # 確認b：錘頭K棒（下影線 > 實體2倍）
            if pc_body > 0 and pc_lower_wick > pc_body * 2:
                sc_confirm_count += 1
                sc_confirm_notes.append(f"✅ 錘頭K棒（下影線={pc_lower_wick:.2f}，止跌訊號強）")
            elif pc_lower_wick > pc_body * 1.2:
                sc_confirm_notes.append(f"⚡ 下影線略長（{pc_lower_wick:.2f}），稍有支撐但未達錘頭標準")

        # 確認c：現價站回 VWAP 上方（在_build_prompt中注入，此處以 flag 先標記）
        sc_near_vwap_confirm = False  # 由 _build_prompt 注入時更新

        # 判斷階段
        if sc_confirm_count >= 2:
            sc_stage = "stage2_buy"   # 可進場（BUY）
            stage_label_sc = "⚡ 第二階段：確認訊號已達2項，可考慮小量試多"
        elif sc_confirm_count == 1:
            sc_stage = "stage1_watch"  # 有初步訊號，繼續等待
            stage_label_sc = "⏳ 第一階段：初步確認訊號（1項），建議繼續等待"
        else:
            sc_stage = "stage0_watch"  # 純觀望
            stage_label_sc = "⏸️ 第零階段：爆量剛發生或尚無確認，強制觀望"

        sc_confirm_str = "\n    ".join(sc_confirm_notes) if sc_confirm_notes else "（尚無確認訊號）"

        warning_text = (
            f"📣【低點爆量提示 — Selling Climax 三階段框架 v7.0】\n"
            f"  近{abs(recent_climax_idx)}根前出現 {climax_ratio:.1f} 倍爆量黑K棒\n"
            f"  現價在日內 {price_pos_pct:.0f}% 低檔區域，屬於「低點恐慌殺盤耗竭」。\n"
            f"\n"
            f"  當前階段判斷：{stage_label_sc}\n"
            f"  已出現的確認訊號（{sc_confirm_count}/4）：\n"
            f"    {sc_confirm_str}\n"
            f"\n"
            f"  【三階段進場規則】\n"
            f"  ▶ 第零階段（爆量當下）→ WATCH：\n"
            f"    爆量黑K剛出現，方向未明，禁止衝動做多，靜待確認。\n"
            f"  ▶ 第一階段（≥2項確認）→ BUY 小量試多：\n"
            f"    a) 後續縮量收紅（RVOL<0.8）→ 賣盤耗竭\n"
            f"    b) 錘頭K棒（下影線>實體2倍）→ 止跌反彈\n"
            f"    c) 現價站回 VWAP 上方 + 收紅K → 趨勢轉多\n"
            f"    d) OBV 由降轉平（不再創新低）→ 資金流止\n"
            f"    進場：試多首批（1/3倉），停損設爆量K棒低點下1 Tick\n"
            f"  ▶ 第二階段（確認+五檔偏多+RSI轉升）→ STRONG_BUY 加碼：\n"
            f"    委買量>60% + RSI由≤35上彎突破40 → 第二批加碼\n"
            f"\n"
            f"  ⚠️ 風險提示：Selling Climax 後仍可能繼續破低（假反彈），\n"
            f"     嚴守停損是關鍵！停損設爆量K棒最低點下方1 Tick。\n"
            f"     若爆量後再出現另一根爆量黑K → 訊號失效，改為觀望或放空。"
        )

        # selling_climax 分支回傳，含 v7.0 新增欄位
        return {
            "type":             climax_type,
            "volume_ratio":     round(climax_ratio, 1),
            "candle_index":     recent_climax_idx,
            "price_position":   price_position,
            "price_pos_pct":    round(price_pos_pct, 0),
            "warning_text":     warning_text,
            "sc_stage":         sc_stage,           # v7.0 新增
            "sc_confirm_count": sc_confirm_count,    # v7.0 新增
        }

    # buying_climax 或其他情況
    return {
        "type":           climax_type,
        "volume_ratio":   round(climax_ratio, 1),
        "candle_index":   recent_climax_idx,
        "price_position": price_position,
        "price_pos_pct":  round(price_pos_pct, 0),
        "warning_text":   warning_text,
        "sc_stage":       None,
        "sc_confirm_count": 0,
    } if climax_type else None

def _calc_rvol(candles: List[Dict], lookback: int = 5) -> Optional[float]:
    """
    計算相對成交量（RVOL）= 最近 N 根均量 / 前 N 根均量。
    RVOL > 1.5 = 放量；RVOL < 0.7 = 縮量。
    用於 ORB 突破確認：放量突破勝率高，縮量突破多為假突破。
    """
    if len(candles) < lookback * 2:
        return None
    vols       = [c.get("volume", 0) for c in candles]
    recent     = vols[-lookback:]
    prior      = vols[-lookback * 2:-lookback]
    avg_recent = sum(recent) / lookback
    avg_prior  = sum(prior) / lookback
    return round(avg_recent / avg_prior, 2) if avg_prior > 0 else None


def _calc_open_gap(candles: List[Dict], prev_close: float = None) -> Optional[str]:
    """計算今日開盤跳空幅度。"""
    if not candles or not prev_close or prev_close == 0:
        return None
    open_price = candles[0].get("open", candles[0].get("close", 0))
    gap_pct    = (open_price - prev_close) / prev_close * 100
    if gap_pct > 1.0:
        return f"跳空上漲 +{gap_pct:.1f}%（開:{open_price} 昨收:{prev_close}）"
    elif gap_pct < -1.0:
        return f"跳空下跌 {gap_pct:.1f}%（開:{open_price} 昨收:{prev_close}）"
    return f"平盤開出 {gap_pct:+.1f}%（開:{open_price} 昨收:{prev_close}）"


def _calc_amplitude(candles: List[Dict], prev_close: float = None) -> Optional[str]:
    """計算今日振幅。"""
    if not candles:
        return None
    day_high = max(c.get("high", c.get("close", 0)) for c in candles)
    day_low  = min(c.get("low",  c.get("close", 0)) for c in candles)
    if prev_close and prev_close > 0:
        amp_pct = (day_high - day_low) / prev_close * 100
        return f"今日振幅 {amp_pct:.1f}%（日高:{day_high} 日低:{day_low}）"
    return f"日高:{day_high} 日低:{day_low}"


def _calc_vwap_bands(candles: List[Dict]) -> Optional[dict]:
    """
    計算 VWAP ± 1倍、2倍標準差通道（VWAP Bands）。
    超過±2σ 代表極端偏離，均值回歸機率高。

    v6.2 修正：改用成交量加權標準差（Volume-Weighted Std Dev），
    以 VWAP 為基準計算，避免低量異常K棒拉偏通道寬度。
    """
    if len(candles) < 5:
        return None
    try:
        total_pv = 0.0
        total_v  = 0.0
        tps      = []
        vols     = []
        for c in candles:
            h  = c.get("high",  c.get("close", 0))
            l  = c.get("low",   c.get("close", 0))
            cl = c.get("close", 0)
            v  = c.get("volume", 0)
            tp = (h + l + cl) / 3.0
            tps.append(tp)
            vols.append(v)
            if v > 0:
                total_pv += tp * v
                total_v  += v

        if total_v <= 0:
            return None

        vwap = round(total_pv / total_v, 2)

        # 成交量加權標準差（以 VWAP 為基準）
        weighted_var = sum(
            vols[i] * (tps[i] - vwap) ** 2
            for i in range(len(tps))
            if vols[i] > 0
        ) / total_v
        std = weighted_var ** 0.5

        return {
            "vwap":   vwap,
            "upper1": round(vwap + std,     2),
            "lower1": round(vwap - std,     2),
            "upper2": round(vwap + 2 * std, 2),
            "lower2": round(vwap - 2 * std, 2),
            "std":    round(std,             2),
        }
    except Exception:
        return None


def _calc_candle_pattern(candles: List[Dict]) -> str:
    """分析最近 3 根 K 棒型態。"""
    if len(candles) < 3:
        return "資料不足（需至少3根）"
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]

    def body(c):
        return abs(c.get("close", 0) - c.get("open", 0))
    def upper_wick(c):
        return c.get("high", c.get("close", 0)) - max(c.get("close", 0), c.get("open", 0))
    def lower_wick(c):
        return min(c.get("close", 0), c.get("open", 0)) - c.get("low", c.get("close", 0))
    def is_bull(c):
        return c.get("close", 0) >= c.get("open", 0)

    patterns = []

    if (is_bull(c1) and is_bull(c2) and is_bull(c3)
            and c2["close"] > c1["close"] and c3["close"] > c2["close"]):
        patterns.append("🟢 紅三兵（連漲3根，多頭加速信號）")

    if (not is_bull(c1) and not is_bull(c2) and not is_bull(c3)
            and c2["close"] < c1["close"] and c3["close"] < c2["close"]):
        patterns.append("🔴 黑三兵（連跌3根，空頭加速信號）")

    b3 = body(c3)
    b2 = body(c2)
    if is_bull(c3) and not is_bull(c2) and b3 > 0 and b2 > 0:
        if c3["close"] > c2["open"] and c3["open"] < c2["close"]:
            patterns.append("🟢 多頭吞噬（陽線吃掉前根陰線，反轉信號）")

    if not is_bull(c3) and is_bull(c2) and b3 > 0 and b2 > 0:
        if c3["close"] < c2["open"] and c3["open"] > c2["close"]:
            patterns.append("🔴 空頭吞噬（陰線吃掉前根陽線，反轉信號）")

    lw3 = lower_wick(c3)
    uw3 = upper_wick(c3)
    b3v = body(c3) if body(c3) > 0 else 0.001
    if lw3 > b3v * 2 and uw3 < b3v * 0.5:
        patterns.append("🔨 錘頭線（下影線長，潛在止跌反彈，等確認）")

    if uw3 > b3v * 2 and lw3 < b3v * 0.5:
        patterns.append("⭐ 流星線（上影線長，潛在見頂回落，等確認）")

    if b3 < (c3.get("high", 0) - c3.get("low", 0)) * 0.1:
        patterns.append("✚ 十字線（多空拮抗，等待方向確認，謹慎進場）")

    return "、".join(patterns) if patterns else "一般K棒（無特殊型態）"


def _calc_price_momentum(candles: List[Dict], n: int = 5) -> str:
    """計算最近 N 根 K 棒的多空動能。"""
    if len(candles) < n:
        return "資料不足"
    recent = candles[-n:]
    bull_count = sum(1 for c in recent if c.get("close", 0) >= c.get("open", 0))
    bear_count = n - bull_count
    avg_vol    = sum(c.get("volume", 0) for c in recent) / n
    prev_n     = candles[-(2 * n):-n] if len(candles) >= 2 * n else candles[:-n]
    prev_avg   = sum(c.get("volume", 0) for c in prev_n) / len(prev_n) if prev_n else avg_vol
    vol_change = (avg_vol - prev_avg) / prev_avg * 100 if prev_avg else 0

    if bull_count >= 4:
        momentum = "強多頭動能"
    elif bull_count >= 3:
        momentum = "偏多動能"
    elif bear_count >= 4:
        momentum = "強空頭動能"
    elif bear_count >= 3:
        momentum = "偏空動能"
    else:
        momentum = "多空均衡"

    vol_str = f"量能{'放大' if vol_change > 10 else '縮小' if vol_change < -10 else '持平'}{vol_change:+.0f}%"
    return f"{momentum}（近{n}根 陽:{bull_count} 陰:{bear_count}），{vol_str}"


# ─────────────────────────────────────────────
#  黃金交叉 / 死亡交叉評分（v7.0 新增）
# ─────────────────────────────────────────────
def _calc_ma_cross(candles: List[Dict]) -> dict:
    """
    計算分K MA5 / MA10 / MA20 的黃金交叉與死亡交叉狀態，
    並輸出評分、訊號強度、進場方向建議。

    黃金交叉定義（以分K為基準，適合當沖）：
      - 強訊號：MA5 上穿 MA10，且 MA10 上穿 MA20（三線翻多）
      - 中訊號：MA5 上穿 MA10（短期翻多）
      - 弱訊號：MA5 由下方接近 MA10 但尚未穿越（蓄勢）

    死亡交叉定義：
      - 強訊號：MA5 下穿 MA10，且 MA10 下穿 MA20（三線翻空）
      - 中訊號：MA5 下穿 MA10（短期翻空）
      - 弱訊號：MA5 由上方接近 MA10 但尚未穿越（蓄勢）

    回傳 dict：
      cross_type   : "golden" / "death" / "none"
      strength     : "strong" / "medium" / "weak" / "none"
      score        : int（做多+1，做空+1，無0，逆向-1）
      direction    : "buy" / "short" / "watch"
      ma5          : float
      ma10         : float
      ma20         : float (若資料不足則 None)
      prev_ma5     : float（前一根）
      prev_ma10    : float（前一根）
      label        : str（給 prompt 用的說明文字）
    """
    result = {
        "cross_type": "none",
        "strength":   "none",
        "score":       0,
        "direction":  "watch",
        "ma5":         None,
        "ma10":        None,
        "ma20":        None,
        "prev_ma5":    None,
        "prev_ma10":   None,
        "label":       "均線資料不足（需至少21根K棒）",
    }

    if len(candles) < 21:
        return result

    closes = [c.get("close", 0) for c in candles]

    # 計算當前與前一根的 MA5、MA10、MA20
    def _ma(series, n, offset=0):
        idx = len(series) - 1 - offset
        if idx < n - 1:
            return None
        return round(sum(series[idx - n + 1: idx + 1]) / n, 3)

    ma5      = _ma(closes, 5,  0)
    ma10     = _ma(closes, 10, 0)
    ma20     = _ma(closes, 20, 0)
    prev_ma5 = _ma(closes, 5,  1)
    prev_ma10= _ma(closes, 10, 1)
    prev_ma20= _ma(closes, 20, 1) if len(candles) >= 22 else None

    if ma5 is None or ma10 is None or prev_ma5 is None or prev_ma10 is None:
        return result

    result.update({
        "ma5": ma5, "ma10": ma10, "ma20": ma20,
        "prev_ma5": prev_ma5, "prev_ma10": prev_ma10,
    })

    # ── 黃金交叉判斷 ──────────────────────────────────────────
    golden_cross_5_10  = prev_ma5 <= prev_ma10 and ma5 > ma10   # MA5 上穿 MA10（剛剛發生）
    above_5_10         = ma5 > ma10                              # MA5 在 MA10 上方（持續多方）
    approaching_up     = ma5 < ma10 and (ma10 - ma5) / ma10 < 0.003  # MA5 接近 MA10 但未穿越

    golden_cross_10_20 = (
        ma20 is not None and prev_ma20 is not None and
        prev_ma10 <= prev_ma20 and ma10 > ma20
    )
    above_10_20 = ma20 is not None and ma10 > ma20

    # ── 死亡交叉判斷 ──────────────────────────────────────────
    death_cross_5_10   = prev_ma5 >= prev_ma10 and ma5 < ma10   # MA5 下穿 MA10（剛剛發生）
    below_5_10         = ma5 < ma10                             # MA5 在 MA10 下方（持續空方）
    approaching_dn     = ma5 > ma10 and (ma5 - ma10) / ma10 < 0.003  # MA5 接近 MA10 由上往下

    death_cross_10_20  = (
        ma20 is not None and prev_ma20 is not None and
        prev_ma10 >= prev_ma20 and ma10 < ma20
    )
    below_10_20 = ma20 is not None and ma10 < ma20

    # ── 評分與標籤 ────────────────────────────────────────────
    if golden_cross_5_10 and above_10_20:
        # 強黃金交叉：MA5 剛上穿 MA10，且 MA10 已在 MA20 上方
        result["cross_type"] = "golden"
        result["strength"]   = "strong"
        result["score"]      = 2   # 做多 +2
        result["direction"]  = "buy"
        cross_note = "🟢🟢【強黃金交叉】MA5剛上穿MA10，MA10>MA20（三線翻多）"
        ma20_str = f"MA20={ma20}" if ma20 else ""
        result["label"] = (
            f"{cross_note}\n"
            f"  MA5={ma5} > MA10={ma10} {ma20_str}\n"
            f"  訊號：三線多頭排列，做多方向證據較強，建議搭配 ORB 或 VWAP 確認進場"        )
    elif golden_cross_5_10:
        # 中黃金交叉：MA5 剛上穿 MA10（MA20 未確認）
        result["cross_type"] = "golden"
        result["strength"]   = "medium"
        result["score"]      = 1
        result["direction"]  = "buy"
        result["label"] = (
            f"🟢【黃金交叉】MA5剛上穿MA10（短期翻多）\n"
            f"  MA5={ma5} ↑ MA10={ma10}｜MA20={ma20 or '不足'}\n"
            f"  訊號：短期多頭訊號，若 MA10 > MA20 確認則升為強訊號"
        )
    elif above_5_10 and above_10_20:
        # 持續多頭排列（未剛交叉，但多方結構完整）
        result["cross_type"] = "golden"
        result["strength"]   = "weak"
        result["score"]      = 1
        result["direction"]  = "buy"
        result["label"] = (
            f"🟡【多頭排列】MA5>MA10>MA20，多方結構持續\n"
            f"  MA5={ma5} MA10={ma10} MA20={ma20}\n"
            f"  訊號：無新交叉但多頭排列完整，順勢做多"
        )
    elif approaching_up:
        # 蓄勢黃金（MA5 接近 MA10 由下往上）
        result["cross_type"] = "none"
        result["strength"]   = "weak"
        result["score"]      = 0
        result["direction"]  = "watch"
        gap_pct = abs(ma10 - ma5) / ma10 * 100
        result["label"] = (
            f"⚡【蓄勢偏多】MA5 距 MA10 僅 {gap_pct:.2f}%，接近黃金交叉\n"
            f"  MA5={ma5}（↑逼近）MA10={ma10}｜等待正式穿越確認"
        )
    elif death_cross_5_10 and below_10_20:
        # 強死亡交叉：MA5 剛下穿 MA10，且 MA10 已在 MA20 下方
        result["cross_type"] = "death"
        result["strength"]   = "strong"
        result["score"]      = -2  # 做多 -2 / 做空 +2
        result["direction"]  = "short"
        ma20_str = f"MA20={ma20}" if ma20 else ""
        result["label"] = (
            f"🔴🔴【強死亡交叉】MA5剛下穿MA10，MA10<MA20（三線翻空）\n"
            f"  MA5={ma5} < MA10={ma10} {ma20_str}\n"
            f"  訊號：三線空頭排列，放空方向證據較強，禁止逆勢做多"        )
    elif death_cross_5_10:
        # 中死亡交叉：MA5 剛下穿 MA10
        result["cross_type"] = "death"
        result["strength"]   = "medium"
        result["score"]      = -1
        result["direction"]  = "short"
        result["label"] = (
            f"🔴【死亡交叉】MA5剛下穿MA10（短期翻空）\n"
            f"  MA5={ma5} ↓ MA10={ma10}｜MA20={ma20 or '不足'}\n"
            f"  訊號：短期空頭訊號，若 MA10 < MA20 確認則升為強訊號"
        )
    elif below_5_10 and below_10_20:
        # 持續空頭排列
        result["cross_type"] = "death"
        result["strength"]   = "weak"
        result["score"]      = -1
        result["direction"]  = "short"
        result["label"] = (
            f"🟠【空頭排列】MA5<MA10<MA20，空方結構持續\n"
            f"  MA5={ma5} MA10={ma10} MA20={ma20}\n"
            f"  訊號：無新交叉但空頭排列完整，順勢放空"
        )
    elif approaching_dn:
        # 蓄勢死亡（MA5 接近 MA10 由上往下）
        result["cross_type"] = "none"
        result["strength"]   = "weak"
        result["score"]      = 0
        result["direction"]  = "watch"
        gap_pct = abs(ma5 - ma10) / ma10 * 100
        result["label"] = (
            f"⚡【蓄勢偏空】MA5 距 MA10 僅 {gap_pct:.2f}%，接近死亡交叉\n"
            f"  MA5={ma5}（↓逼近）MA10={ma10}｜等待正式穿越確認"
        )
    else:
        # 多空拮抗，無明確交叉
        diff_pct = (ma5 - ma10) / ma10 * 100 if ma10 else 0
        result["label"] = (
            f"⚪【均線拮抗】MA5 與 MA10 差距 {diff_pct:+.2f}%，方向不明\n"
            f"  MA5={ma5} MA10={ma10} MA20={ma20 or '不足'}｜觀望為主"
        )

    return result

# ─────────────────────────────────────────────
#  K線圖形型態識別（楔形/旗形/頭肩/矩形/三角形）
# ─────────────────────────────────────────────
def _detect_chart_patterns(candles: List[Dict], lookback: int = 20) -> dict:
    """
    偵測常見技術分析型態（適合當沖1分K）：
    - 上升楔形（Rising Wedge）：看空型態
    - 下降楔形（Falling Wedge）：看多型態
    - 上升旗形（Bull Flag）：看多型態
    - 下降旗形（Bear Flag）：看空型態
    - 矩形整理（Rectangle）：突破方向待定
    - 上升三角形（Ascending Triangle）：看多型態
    - 下降三角形（Descending Triangle）：看空型態
    - 頭肩頂（Head & Shoulders Top）：看空型態
    - 頭肩底（Inverse H&S）：看多型態
    
    回傳 dict：
      patterns     : list[str]  偵測到的型態名稱列表
      bias         : "bullish" / "bearish" / "neutral"
      score        : int  做多方向加減分（-2 ~ +2）
      warning      : str  給 prompt 用的說明文字
      breakout_hint: str  建議等待的突破方向
    """
    result = {
        "patterns":      [],
        "bias":          "neutral",
        "score":         0,
        "warning":       "",
        "breakout_hint": "",
    }

    if len(candles) < 10:
        result["warning"] = "K棒資料不足（需至少10根），無法識別圖形型態"
        return result

    src = candles[-lookback:] if len(candles) >= lookback else candles
    n = len(src)

    highs  = [c.get("high",  c.get("close", 0)) for c in src]
    lows   = [c.get("low",   c.get("close", 0)) for c in src]
    closes = [c.get("close", 0) for c in src]
    vols   = [c.get("volume", 0) for c in src]

    # ── 工具：簡單線性迴歸（求斜率）──────────────────────────────
    def _slope(series: list) -> float:
        """回傳斜率（正=上升，負=下降）。"""
        xs = list(range(len(series)))
        n_ = len(series)
        if n_ < 2:
            return 0.0
        sx  = sum(xs)
        sy  = sum(series)
        sxy = sum(x * y for x, y in zip(xs, series))
        sxx = sum(x * x for x in xs)
        denom = n_ * sxx - sx * sx
        if denom == 0:
            return 0.0
        return (n_ * sxy - sx * sy) / denom

    # ── 工具：找局部高點/低點 ─────────────────────────────────────
    def _local_peaks(series: list, window: int = 3) -> list:
        """回傳局部高點的 index 列表。"""
        peaks = []
        for i in range(window, len(series) - window):
            if all(series[i] >= series[i - j] for j in range(1, window + 1)) and \
               all(series[i] >= series[i + j] for j in range(1, window + 1)):
                peaks.append(i)
        return peaks

    def _local_troughs(series: list, window: int = 3) -> list:
        """回傳局部低點的 index 列表。"""
        troughs = []
        for i in range(window, len(series) - window):
            if all(series[i] <= series[i - j] for j in range(1, window + 1)) and \
               all(series[i] <= series[i + j] for j in range(1, window + 1)):
                troughs.append(i)
        return troughs

    slope_high = _slope(highs)
    slope_low  = _slope(lows)
    slope_close = _slope(closes)

    avg_vol = sum(vols[:n//2]) / max(n//2, 1)
    recent_vol = sum(vols[n//2:]) / max(n - n//2, 1)
    vol_shrinking = recent_vol < avg_vol * 0.8   # 量能收縮
    vol_expanding = recent_vol > avg_vol * 1.3   # 量能放大

    # ── 振幅收窄判斷（用於楔形/三角形） ─────────────────────────
    first_half_range = sum(highs[i] - lows[i] for i in range(n//2)) / max(n//2, 1)
    second_half_range = sum(highs[i] - lows[i] for i in range(n//2, n)) / max(n - n//2, 1)
    range_shrinking = second_half_range < first_half_range * 0.75   # 振幅縮小25%以上

    # ── 水平線判斷（用於矩形/三角形） ───────────────────────────
    high_std = (max(highs[n//2:]) - min(highs[n//2:])) / (max(highs[n//2:]) + 0.001)
    low_std  = (max(lows[n//2:])  - min(lows[n//2:]))  / (max(lows[n//2:])  + 0.001)
    high_is_flat = high_std < 0.005   # 高點幾乎持平（水平阻力）
    low_is_flat  = low_std  < 0.005   # 低點幾乎持平（水平支撐）

    patterns_found = []
    bias_score = 0  # 正=多頭型態，負=空頭型態

    # ════════════════════════════════════════
    # 1. 上升楔形（Rising Wedge）→ 看空
    #    高點與低點都在上升，但高點斜率 < 低點斜率（收斂向上）
    # ════════════════════════════════════════
    if (slope_high > 0 and slope_low > 0 and
            slope_low > slope_high * 1.1 and   # 低點斜率 > 高點斜率
            range_shrinking and vol_shrinking):
        patterns_found.append(
            "📐【上升楔形 Rising Wedge】看空型態\n"
            "   高低點均上升但逐漸收斂，量能萎縮 → 突破下方楔形下緣時放空\n"
            "   當沖策略：等待跌破下緣（含量）→ 放空，停損設楔形頂端上方"
        )
        bias_score -= 1

    # ════════════════════════════════════════
    # 2. 下降楔形（Falling Wedge）→ 看多
    #    高點與低點都在下降，但低點斜率 > 高點斜率（收斂向下）
    # ════════════════════════════════════════
    elif (slope_high < 0 and slope_low < 0 and
            slope_high > slope_low * 1.1 and   # 高點斜率（負）大於低點斜率（負）
            range_shrinking and vol_shrinking):
        patterns_found.append(
            "📐【下降楔形 Falling Wedge】看多型態\n"
            "   高低點均下降但逐漸收斂，量能萎縮 → 突破上方楔形上緣時做多\n"
            "   當沖策略：等待突破上緣（含量）→ 做多，停損設楔形底端下方"
        )
        bias_score += 1

    # ════════════════════════════════════════
    # 3. 上升旗形（Bull Flag）→ 看多
    #    快速大漲（旗杆）後，出現短暫下斜整理（旗面）
    # ════════════════════════════════════════
    first_quarter = closes[:n//4]
    last_half = closes[n//4:]
    if first_quarter and last_half:
        initial_surge = (max(first_quarter) - first_quarter[0]) / (first_quarter[0] + 0.001) * 100
        consolidation_slope = _slope(last_half)
        if (initial_surge > 1.5 and           # 旗杆：漲幅超過1.5%
                -0.02 < consolidation_slope < 0 and  # 旗面：輕微下斜
                vol_shrinking):               # 整理量縮
            patterns_found.append(
                "🚩【上升旗形 Bull Flag】看多型態\n"
                "   急漲後量縮整理（輕微下斜），是典型多頭旗形 → 突破旗頂時追多\n"
                "   當沖策略：突破旗頂（前段高點）且放量 → 進場做多，目標旗杆等距延伸"
            )
            bias_score += 2  # 旗形為常見延續型態，+2分

    # ════════════════════════════════════════
    # 4. 下降旗形（Bear Flag）→ 看空
    #    快速大跌（旗杆）後，出現短暫上斜整理（旗面）
    # ════════════════════════════════════════
    if first_quarter and last_half:
        initial_drop = (first_quarter[0] - min(first_quarter)) / (first_quarter[0] + 0.001) * 100
        consolidation_slope_bear = _slope(last_half)
        if (initial_drop > 1.5 and                  # 旗杆：跌幅超過1.5%
                0 < consolidation_slope_bear < 0.02 and  # 旗面：輕微上斜
                vol_shrinking):                      # 整理量縮
            patterns_found.append(
                "🚩【下降旗形 Bear Flag】看空型態\n"
                "   急跌後量縮整理（輕微上斜），是典型空頭旗形 → 跌破旗底時追空\n"
                "   當沖策略：跌破旗底（前段低點）且放量 → 放空，目標旗杆等距延伸"
            )
            bias_score -= 2

    # ════════════════════════════════════════
    # 5. 矩形整理（Rectangle / Box）→ 突破方向待定
    #    高點持平、低點持平，形成盤整箱型
    # ════════════════════════════════════════
    if high_is_flat and low_is_flat and range_shrinking:
        box_high = sum(highs[n//2:]) / max(n - n//2, 1)
        box_low  = sum(lows[n//2:])  / max(n - n//2, 1)
        current_price_val = closes[-1]
        if vol_shrinking:
            breakout_dir = "上方突破（做多）" if current_price_val > box_high * 0.998 else "下方跌破（放空）"
            patterns_found.append(
                f"📦【矩形整理 Rectangle】中性型態（等待突破）\n"
                f"   高點約 {round(box_high,2)}、低點約 {round(box_low,2)} 形成箱型整理，量縮\n"
                f"   當沖策略：等待{breakout_dir}且放量確認 → 順勢追進"
            )
            # 矩形不加減分，等突破再決定

    # ════════════════════════════════════════
    # 6. 上升三角形（Ascending Triangle）→ 看多
    #    高點持平（阻力），低點持續升高（支撐上移）
    # ════════════════════════════════════════
    if high_is_flat and slope_low > 0 and range_shrinking:
        resist_price = round(sum(highs[n//2:]) / max(n - n//2, 1), 2)
        patterns_found.append(
            f"△【上升三角形 Ascending Triangle】看多型態\n"
            f"   上方阻力約 {resist_price}（持平），低點持續墊高 → 突破阻力爆量做多\n"
            f"   當沖策略：突破 {resist_price} 且 RVOL≥1.5 → 進場做多，停損設三角形最近低點"
        )
        bias_score += 1

    # ════════════════════════════════════════
    # 7. 下降三角形（Descending Triangle）→ 看空
    #    低點持平（支撐），高點持續下降（壓力下移）
    # ════════════════════════════════════════
    if low_is_flat and slope_high < 0 and range_shrinking:
        support_price = round(sum(lows[n//2:]) / max(n - n//2, 1), 2)
        patterns_found.append(
            f"▽【下降三角形 Descending Triangle】看空型態\n"
            f"   下方支撐約 {support_price}（持平），高點持續下壓 → 跌破支撐放空\n"
            f"   當沖策略：跌破 {support_price} 且 RVOL≥1.5 → 放空，停損設三角形最近高點"
        )
        bias_score -= 1

    # ════════════════════════════════════════
    # 8. 頭肩頂（Head & Shoulders Top）→ 看空
    #    三峰：中間最高（頭），兩側較低（肩），頸線支撐
    # ════════════════════════════════════════
    peaks = _local_peaks(highs, window=2)
    if len(peaks) >= 3:
        last3_peaks = peaks[-3:]
        p1, p2, p3 = highs[last3_peaks[0]], highs[last3_peaks[1]], highs[last3_peaks[2]]
        if (p2 > p1 * 1.005 and p2 > p3 * 1.005 and  # 中間最高
                abs(p1 - p3) / p2 < 0.03):             # 兩肩高度相近（誤差<3%）
            troughs = _local_troughs(lows, window=2)
            if len(troughs) >= 2:
                neckline = (lows[troughs[-2]] + lows[troughs[-1]]) / 2
                patterns_found.append(
                    f"👤【頭肩頂 Head & Shoulders】看空型態\n"
                    f"   三峰型態：左肩{round(p1,2)} 頭{round(p2,2)} 右肩{round(p3,2)}\n"
                    f"   頸線約 {round(neckline,2)} → 跌破頸線放空，目標=頭部到頸線的等距\n"
                    f"   當沖策略：跌破 {round(neckline,2)} 含量 → 放空"
                )
                bias_score -= 2

    # ════════════════════════════════════════
    # 9. 頭肩底（Inverse Head & Shoulders）→ 看多
    #    三谷：中間最低（頭），兩側較高（肩），頸線壓力
    # ════════════════════════════════════════
    troughs_all = _local_troughs(lows, window=2)
    if len(troughs_all) >= 3:
        last3_troughs = troughs_all[-3:]
        t1, t2, t3 = lows[last3_troughs[0]], lows[last3_troughs[1]], lows[last3_troughs[2]]
        if (t2 < t1 * 0.995 and t2 < t3 * 0.995 and  # 中間最低
                abs(t1 - t3) / (t2 + 0.001) < 0.03):  # 兩肩深度相近
            peaks_all = _local_peaks(highs, window=2)
            if len(peaks_all) >= 2:
                neckline_inv = (highs[peaks_all[-2]] + highs[peaks_all[-1]]) / 2
                patterns_found.append(
                    f"🙃【頭肩底 Inverse H&S】看多型態\n"
                    f"   三谷型態：左肩{round(t1,2)} 頭{round(t2,2)} 右肩{round(t3,2)}\n"
                    f"   頸線約 {round(neckline_inv,2)} → 突破頸線做多，目標=頭部到頸線的等距\n"
                    f"   當沖策略：突破 {round(neckline_inv,2)} 含量 → 做多"
                )
                bias_score += 2

    # ── 整理結果 ─────────────────────────────────────────────────
    if not patterns_found:
        result["warning"] = "無明顯圖形型態（一般K棒走勢，依其他指標判斷）"
        result["breakout_hint"] = "等待型態成形"
        return result

    result["patterns"] = patterns_found
    result["score"]    = max(-2, min(2, bias_score))  # 限制在 -2 ~ +2

    if bias_score >= 2:
        result["bias"] = "bullish"
    elif bias_score <= -2:
        result["bias"] = "bearish"
    else:
        result["bias"] = "neutral"

    # 彙整文字輸出
    pattern_summary = "\n".join(f"  {i+1}. {p}" for i, p in enumerate(patterns_found))
    bias_label = {"bullish": "偏多", "bearish": "偏空", "neutral": "中性待定"}.get(result["bias"], "中性")

    result["warning"] = (
        f"【📊 圖形型態分析】偵測到 {len(patterns_found)} 個型態（偏向：{bias_label}）\n"
        f"{pattern_summary}\n"
        f"  型態評分（相對做多方向）：{result['score']:+d}分"
    )
    result["breakout_hint"] = (
        "等待放量突破確認" if result["bias"] == "neutral"
        else ("等待向上突破" if result["bias"] == "bullish" else "等待向下跌破")
    )

    return result

# ─────────────────────────────────────────────
#  ORB 突破狀態分析（v6.1 新增）
# ─────────────────────────────────────────────
def _analyze_orb_status(
    candles: List[Dict],
    opening_range: Optional[Dict],
    current_price: float,
    rvol: Optional[float],
) -> dict:
    """
    分析 Opening Range Breakout 狀態。

    回傳 dict：
      has_or       : bool   — 是否有開盤區間資料
      or_high      : float  — 開盤區間最高（使用 or15 或 or20）
      or_low       : float  — 開盤區間最低
      or_label     : str    — 使用的 OR 區間標籤（如 "or15"）
      breakout_dir : str    — "up" / "down" / "none"
      rvol_confirm : bool   — 量能是否確認（RVOL >= 1.5）
      orb_signal   : str    — 最終 ORB 訊號描述
      orb_strength : int    — 0=無；1=弱；2=中；3=強（三重確認）
    """
    result = {
        "has_or":       False,
        "or_high":      0.0,
        "or_low":       0.0,
        "or_label":     "",
        "breakout_dir": "none",
        "rvol_confirm": False,
        "orb_signal":   "無開盤區間資料",
        "orb_strength": 0,
    }

    if not opening_range or not candles:
        return result

    # 選擇最適合的 OR 區間（優先用 or15，次選 or20 或 or30）
    or_ref   = None
    or_label = ""
    for key in ("or15", "or20", "or30", "or5"):
        if key in opening_range:
            or_ref   = opening_range[key]
            or_label = key
            break

    if not or_ref:
        return result

    or_high = or_ref["high"]
    or_low  = or_ref["low"]
    result["has_or"]   = True
    result["or_high"]  = or_high
    result["or_low"]   = or_low
    result["or_label"] = or_label

    # 判斷突破方向
    breakout_dir  = "none"
    if current_price > or_high:
        breakout_dir = "up"
    elif current_price < or_low:
        breakout_dir = "down"
    result["breakout_dir"] = breakout_dir

    # 量能確認
    rvol_confirm = (rvol is not None and rvol >= 1.5)
    result["rvol_confirm"] = rvol_confirm

    # ORB 訊號強度評估
    or_range_pct = (or_high - or_low) / or_low * 100 if or_low > 0 else 0

    if breakout_dir == "none":
        result["orb_signal"]   = f"盤整在 OR 區間內（{or_label} 高:{or_high} 低:{or_low}）"
        result["orb_strength"] = 0
    elif breakout_dir == "up":
        dist_pct = (current_price - or_high) / or_high * 100
        if rvol_confirm:
            result["orb_signal"]   = f"🟢 ORB 向上突破！突破 {or_label} 高點 {or_high}（+{dist_pct:.1f}%），RVOL={rvol}x 量能確認"
            result["orb_strength"] = 3  # 突破+量能 = 強
        else:
            result["orb_signal"]   = f"⚠️ ORB 向上突破但量能不足！突破 {or_label} 高點 {or_high}（+{dist_pct:.1f}%），RVOL={rvol or '?'}x（需≥1.5）"
            result["orb_strength"] = 1  # 突破但無量 = 弱（假突破風險高）
    else:  # down
        dist_pct = (or_low - current_price) / or_low * 100
        if rvol_confirm:
            result["orb_signal"]   = f"🔴 ORB 向下突破！跌破 {or_label} 低點 {or_low}（-{dist_pct:.1f}%），RVOL={rvol}x 量能確認"
            result["orb_strength"] = 3  # 突破+量能 = 強
        else:
            result["orb_signal"]   = f"⚠️ ORB 向下突破但量能不足！跌破 {or_label} 低點 {or_low}（-{dist_pct:.1f}%），RVOL={rvol or '?'}x（需≥1.5）"
            result["orb_strength"] = 1

    return result


# ─────────────────────────────────────────────
#  分K完整度檢查
# ─────────────────────────────────────────────
def _check_candle_completeness(candles: List[Dict], candle_minutes: int = 1) -> dict:
    """判斷最後一根分K是否「跑完整」，避免 AI 用未完成的 K 棒誤判量能。"""
    result = {
        "is_incomplete":  False,
        "elapsed_sec":    0.0,
        "completion_pct": 100.0,
        "warning_text":   "",
    }
    if not candles:
        return result

    last = candles[-1]
    t_str = last.get("time", "")
    if not t_str or len(t_str) < 5:
        return result

    try:
        now = datetime.now()
        parts = t_str.split(":")
        h, m = int(parts[0]), int(parts[1])
        candle_start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        elapsed = (now - candle_start).total_seconds()
        if elapsed < 0:
            elapsed = 0.0
        full_sec = candle_minutes * 60
        pct = min(elapsed / full_sec * 100, 100.0)

        result["elapsed_sec"]    = elapsed
        result["completion_pct"] = pct

        if pct < 70.0:
            result["is_incomplete"] = True
            result["warning_text"]  = (
                f"\n⚠️【最後一根K棒尚未完整！完成度約 {pct:.0f}%（{elapsed:.0f}秒/60秒）】\n"
                f"  • 最後一根 {t_str} 的量能與振幅可能僅有完整K棒的 {pct:.0f}%\n"
                f"  • 請以「倒數第二根（已完整）」為主要判斷依據\n"
                f"  • 不要因為最後一根縮量/振幅小就判斷量能不足或輸出 WATCH\n"
                f"  • RVOL 計算請略去最後一根，以前面已完整的 K 棒為準\n"
            )
    except Exception:
        pass

    return result


# ─────────────────────────────────────────────
#  漲跌停計算
# ─────────────────────────────────────────────
def _calc_limit_prices(prev_close: float) -> Optional[tuple]:
    """
    計算台股當日漲停 / 跌停價格。
    v6.1 修正：改用 Decimal 版 _snap_tick，消除浮點精度問題。
    """
    if not prev_close or prev_close <= 0:
        return None
    limit_up   = _snap_tick(prev_close * 1.1, "floor")
    limit_down = _snap_tick(prev_close * 0.9, "ceil")
    return limit_up, limit_down


def _check_at_limit(current_price: float, limit_up: float, limit_down: float,
                    tolerance: float = 0.005) -> str:
    if abs(current_price - limit_up)   <= limit_up   * tolerance:
        return "up"
    if abs(current_price - limit_down) <= limit_down * tolerance:
        return "down"
    return ""


# ─────────────────────────────────────────────
#  風險模式說明
# ─────────────────────────────────────────────
_RISK_MODE_PROMPT = {
    "aggressive": """
【⚡ 風險模式：高風險高暴利】
使用者偏好積極操作，請依以下原則調整建議：
• 停損距離：使用 ATR × 1.0～1.5 倍，容忍較大波動
• 目標距離：使用 ATR × 3.0～5.0 倍，追求高報酬
• 進場條件：BUY / SHORT 訊號即可，不強制等 STRONG 訊號
• 適合情境：爆量突破（RVOL≥2）、當日振幅 > 3%、趨勢明確行情
• 注意：即使積極，收盤前 45 分仍需謹慎評估是否有充足出場時間
""",
    "conservative": """
【🛡️ 風險模式：低風險低獲利】
使用者偏好穩健操作，請依以下原則調整建議：
• 停損距離：使用 ATR × 0.5 倍，快速停損保護本金
• 目標距離：使用 ATR × 1.0～1.5 倍，落袋為安
• 進場條件：僅在 STRONG_BUY 或 STRONG_SHORT 時才建議新倉，其他一律 WATCH
• ORB 要求：需三重確認（ORB突破+VWAP同向+RVOL≥1.5）才進場
• 若趨勢不夠明確（BUY 而非 STRONG_BUY），請輸出 WATCH
""",
    "auto": """
【🔵 風險模式：自動判斷（AI 自主決定）】
請根據日線支撐/壓力位、當日振幅與 ATR 自動評估合適的風險參數：
• 若振幅 > 3% 且 OBV 上升（或下降），可採用積極設定（ATR × 1.0 停損 / ATR × 3 目標）
• 若振幅 < 1.5% 或趨勢不明確，採用保守設定（ATR × 0.5 停損 / ATR × 1.5 目標）
• 進場訊號強度請依技術指標綜合評分：訊號越強（STRONG），建議越積極
""",
    "relaxed": """
【🧪 風險模式：實驗性寬鬆模式（測試用，訊號會明顯變多）】
此模式放寬 BUY / SHORT（非 STRONG）的判定門檻，讓系統更常給出方向性建議，
用於讓使用者自行比對「訊號變多後，命中率是否仍可接受」。
• 停損距離：使用 ATR × 0.8～1.2 倍
• 目標距離：使用 ATR × 2.0～3.0 倍
• 進場條件見下方 STEP 4 的寬鬆版門檻（MTF 或技術分擇一達標即可）
• 請照實判斷，不要為了迎合「訊號變多」而誇大證據強度；
  條件不足時，即使在寬鬆模式下仍應輸出 WATCH
""",
    "simplified": """
【🎯 風險模式：精簡核心版（測試用，判斷邏輯與其他模式不同）】
此模式不使用下方 STEP 2/3 的 11 項技術指標加總評分，改用少數幾個
有實證研究支持、且不需要複雜運算的核心條件直接判斷，設計目的是
降低多步驟推理鏈出錯的機率，並和多指標評分制版本做真實成效對照。

【本模式的判斷依據（僅用這 4 項，忽略下方 STEP 2/3 的指標評分）】
① 開盤區間（ORB）：現價是否站上/跌破開盤前 5~15 分鐘區間高/低點
② VWAP 方向：現價在 VWAP 之上（偏多）或之下（偏空），並留意乖離是否過大
③ 相對量能（RVOL）：今日成交量相對於近期平均是否放大（RVOL≥1.5 視為有效參與）
④ 個股是否為「今日活躍股」：開盤後量能、振幅是否明顯放大，
   而非全日平淡盤整。研究顯示，並非每檔股票的開盤波動都有意義，
   只有當日明顯活躍、有實際供需失衡的個股，訊號才較可能延續；
   平淡個股即使技術形態符合，也應優先判斷為 WATCH。

【判斷原則（誠實原則，優先於給出訊號）】
• 上述 4 項全部同向（例如站上ORB高點 + 站上VWAP + RVOL≥1.5 + 屬活躍股）
  → 可給 BUY/SHORT；若同時有明確的量價確認（如突破當根爆量），才給 STRONG
• 任一項明顯反向或缺乏依據 → 直接給 WATCH，不要用其他指標湊理由硬凹
• 務必將交易成本（手續費＋證交稅，來回約在 0.15~0.3% 量級，依券商與稅率而定）
  納入目標判斷：若進場後的合理目標距離扣除來回成本，
  淨空間已所剩無幾，即使方向正確也應優先給 WATCH 或提示「空間不足，不建議進場」
• 不追高殺低：現價已大幅偏離 VWAP（例如乖離 > 2×ATR）時，
  視為追價風險過高，即使動能訊號存在也應保守看待
• 這是一套「已知不保證獲利、但邏輯單純、避免過度擬合」的判斷方式，
  刻意不使用複雜的多框加權計分，因為推理步驟越多，愈依賴模型的
  精算能力，愈容易在中間環節出錯而非真的判斷更準確
""",
}


# ─────────────────────────────────────────────
#  GeminiService
# ─────────────────────────────────────────────
DEFAULT_MODEL_PRIORITY = [
    "gemma-4-31b-it",
    "gemma-4-26b-a4b-it",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]

class GeminiService:
    """Gemini API 封裝：多空雙向當沖分析 + 時段感知 + ORB + VWAP/ATR/OBV + 漲跌停感知 + 風險模式"""

    def __init__(self, api_key: str, model: str = None, model_priority: List[str] = None):
        self.api_key = api_key
        self.model_priority = model_priority or (list(DEFAULT_MODEL_PRIORITY) if not model else [model] + [m for m in DEFAULT_MODEL_PRIORITY if m != model])
        self.model   = self.model_priority[0]
        self.active_model = self.model
        self.force_direction = None

    def set_force_direction(self, direction: str | None):
        self.force_direction = direction

    def analyze(self, stock_data):
        if self.force_direction == "buy":
            return "STRONG_BUY"
        elif self.force_direction == "short":
            return "STRONG_SHORT"
        else:
            return self._ai_analyze(stock_data)

    def list_models(self) -> List[Dict[str, str]]:
        if not self.api_key:
            return []
        try:
            resp = requests.get(GEMINI_MODELS_URL, params={"key": self.api_key}, timeout=15)
            if resp.status_code == 200:
                models = []
                for m in resp.json().get("models", []):
                    name    = m.get("name", "")
                    display = m.get("displayName", "")
                    if "generateContent" in m.get("supportedGenerationMethods", []):
                        model_id = name.replace("models/", "")
                        models.append({"id": model_id, "display": display or model_id})
                return models
            return []
        except Exception:
            return []

    # ──────────────────────────────────────────
    #  底層 API 呼叫（支援多模型優先順序自動選擇與 Fallback）
    # ──────────────────────────────────────────
    def _call(self, prompt: str, max_tokens: int = 800) -> Optional[str]:
        if not self.api_key:
            return "❌ 尚未設定 Gemini API Key，請至設定頁面填入"

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens},
        }

        # 依優先順序輪流嘗試模型
        for model_idx, target_model in enumerate(self.model_priority):
            url = GEMINI_API_URL.format(model=target_model)
            print(f"[Gemini] 優先嘗試模型 ({model_idx+1}/{len(self.model_priority)}): {target_model}")

            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    resp = requests.post(url, json=payload,
                                         params={"key": self.api_key}, timeout=TIMEOUT_SEC)

                    if resp.status_code == 200:
                        resp_json  = resp.json()
                        candidates = resp_json.get("candidates", [])
                        if candidates:
                            candidate     = candidates[0]
                            finish_reason = candidate.get("finishReason", "UNKNOWN")
                            parts         = candidate.get("content", {}).get("parts", [])

                            text = ""
                            for part in parts:
                                if not part.get("thought", False) and part.get("text", ""):
                                    text = part["text"]
                                    break

                            self.active_model = target_model
                            self.model = target_model
                            print(f"[Gemini] 成功使用模型 [{target_model}]（第 {attempt} 次）text長度={len(text)}")

                            if finish_reason == "SAFETY":
                                print(f"[Gemini] ⚠️ 安全過濾器攔截！safetyRatings={candidate.get('safetyRatings')}")
                                return "❌ Gemini 安全過濾器攔截，請簡化 prompt 或更換模型"

                            if finish_reason == "MAX_TOKENS" or finish_reason == "RECITATION":
                                print(f"[Gemini] ⚠️ 輸出被截斷！finishReason={finish_reason}，建議提高 max_tokens")
                                # Gemma 系列模型常把 token 額度耗在內部思考(thought)過程，
                                # 導致正式輸出的 text 還沒生成就被截斷（text長度=0）。
                                # 這種情況下不要直接放棄，先用加倍的 token 上限對同一模型重打一次。
                                if not text and payload["generationConfig"]["maxOutputTokens"] < max_tokens * 4:
                                    boosted = payload["generationConfig"]["maxOutputTokens"] * 2
                                    print(f"[Gemini] 🔁 偵測到空輸出+截斷，改用 maxOutputTokens={boosted} 對 [{target_model}] 重試...")
                                    payload["generationConfig"]["maxOutputTokens"] = boosted
                                    continue  # 用新的 token 上限重打本次 attempt（不消耗下一個模型的機會）

                            if not text:
                                return "❌ Gemini 回傳空內容"

                            return text

                        print(f"[Gemini] ⚠️ candidates 為空！完整回應={resp_json}")
                        break # 跳出當前模型重試，嘗試下一個模型

                    elif resp.status_code in (404, 400):
                        print(f"[Gemini Fallback] 模型 [{target_model}] 不可用或不支援 (HTTP {resp.status_code})，自動嘗試下一個優先模型...")
                        break # 直接換下一個候選模型

                    elif resp.status_code == 429:
                        print(f"[Gemini Fallback] 模型 [{target_model}] 配額超額 (429)，自動降級嘗試下一個模型...")
                        break # 配額超限直接換下一個候選模型

                    elif resp.status_code in (500, 503):
                        time.sleep(RETRY_DELAY * attempt)
                        continue
                    else:
                        print(f"[Gemini] 模型 [{target_model}] HTTP {resp.status_code}，嘗試下一個模型...")
                        break

                except requests.exceptions.Timeout:
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_DELAY)
                        continue
                    break
                except requests.exceptions.ConnectionError:
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_DELAY)
                        continue
                    break
                except Exception as e:
                    print(f"[Gemini] 例外錯誤: {e}")
                    break

        return "❌ 所有候選模型皆無法使用，請確認 API 金鑰與配額"

    # ──────────────────────────────────────────
    #  組建分析 Prompt（v6.1 優化）
    # ──────────────────────────────────────────
    def _build_prompt(
        self,
        symbol:          str,
        candles:         List[Dict],
        indicators:      Dict,
        volumes_data:    List[Dict],
        prev_entries:    List[Dict],
        daily_candles:   List[Dict]  = None,
        prev_close:      float       = None,
        analysis_time:   dtime       = None,
        risk_mode:       str         = "auto",
        limit_up:        float       = None,
        limit_down:      float       = None,
        force_direction: str         = None,
        candle_minutes:  int         = 1,
        concise:         bool        = False,
        orderbook:       Dict        = None,
    ) -> str:
        # ── v8.0：取完整當日K線（最多60根），不再固定40根 ─────────────────
        # 完整模式：取全部當日1分K（最多60根，即完整交易日），讓AI掌握完整日內走勢
        # 精簡模式：取最近20根，節省 token
        if concise:
            recent = candles[-20:] if len(candles) >= 20 else candles
        else:
            recent = candles[-60:] if len(candles) >= 60 else candles  # 最多60根完整當日K線
        current_price = recent[-1]["close"] if recent else 0

        # ── v6.1：從 indicators 取 ORB 與 RVOL 資料 ────────────────
        opening_range = indicators.get("or") if indicators else None
        rvol_val      = indicators.get("rvol") if indicators else None

        # ── ORB 突破狀態分析（v6.1 核心新增） ────────────────────────
        orb_status = _analyze_orb_status(candles, opening_range, current_price, rvol_val)

        # ── 分K完整度檢查 ─────────────────────────────────────────
        completeness  = _check_candle_completeness(candles, candle_minutes)
        candle_warn   = completeness["warning_text"]

        # ── 強制方向區塊 ──────────────────────────────────────────
        if force_direction == "buy":
            force_dir_block = (
                "\n【🟢 使用者強制做多模式】\n"
                "使用者已確認要做多進場，請在此前提下分析：\n"
                "  • SIGNAL 必須輸出 BUY 或 STRONG_BUY（除非已達漲停板）\n"
                "  • 不輸出 WATCH / SHORT / STRONG_SHORT\n"
                "  • 重點分析：最佳進場點、停損設置、目標位\n"
                "  • 若時段或技術面有風險，請在「風險」欄明確說明，但仍提供做多建議\n"
            )
        elif force_direction == "short":
            force_dir_block = (
                "\n【🔵 使用者強制放空模式】\n"
                "使用者已確認要放空進場，請在此前提下分析：\n"
                "  • SIGNAL 必須輸出 SHORT 或 STRONG_SHORT（除非已達跌停板）\n"
                "  • 不輸出 WATCH / BUY / STRONG_BUY\n"
                "  • 重點分析：最佳放空點、停損設置、目標位\n"
                "  • 若時段或技術面有風險，請在「風險」欄明確說明，但仍提供放空建議\n"
            )
        else:
            force_dir_block = ""

        # ── 【v13 新增】SIGNAL 合法選項清單，依 force_direction 動態調整 ──
        # 原本格式規範永遠寫死列出全部5個選項（STRONG_BUY/BUY/WATCH/SHORT/
        # STRONG_SHORT），即使上面已明確要求「強制做多，不輸出SHORT系列」，
        # 格式區卻仍讓模型以為所有選項都合法，是自相矛盾的指示，
        # 容易讓模型判斷不穩定。這裡讓格式規範的選項清單跟指示保持一致。
        if force_direction == "buy":
            signal_options_text = "STRONG_BUY或BUY（強制做多模式，不可輸出WATCH/SHORT/STRONG_SHORT）"
        elif force_direction == "short":
            signal_options_text = "STRONG_SHORT或SHORT（強制放空模式，不可輸出WATCH/BUY/STRONG_BUY）"
        else:
            signal_options_text = "STRONG_BUY或BUY或WATCH或SHORT或STRONG_SHORT"

        stage_info    = _get_trade_stage(analysis_time)
        stage_label   = stage_info["label"]
        stage_warning = stage_info["warning"]
        force_watch   = stage_info["force_watch"]
        left          = stage_info["minutes_to_close"]
        now_time      = (analysis_time or datetime.now().time()).strftime("%H:%M")

        # ── VWAP 相關 ────────────────────────────────────────────
        vwap = _calc_vwap(candles)
        if vwap is not None:
            diff_pct = (current_price - vwap) / vwap * 100 if vwap else 0
            if   diff_pct >  1.5: vwap_rel = f"現價高於VWAP {diff_pct:+.1f}%（偏多，但留意過熱）"
            elif diff_pct >  0.3: vwap_rel = f"現價略高於VWAP {diff_pct:+.1f}%（偏多）"
            elif diff_pct > -0.3: vwap_rel = f"現價約等於VWAP（多空均衡）"
            elif diff_pct > -1.5: vwap_rel = f"現價略低於VWAP {diff_pct:+.1f}%（偏空）"
            else:                  vwap_rel = f"現價低於VWAP {diff_pct:+.1f}%（偏空，留意超跌反彈）"
            vwap_text = f"{vwap}　→ {vwap_rel}"
        else:
            vwap_text = "無法計算（資料不足）"

        atr       = _calc_atr(candles)
        atr_text  = f"{atr}元（Wilder 14期，可作停損/目標參考距離）" if atr else "無法計算"
        obv_text  = _calc_obv_trend(candles)
        vol_ratio = _calc_volume_ratio(candles)
        open_gap  = _calc_open_gap(candles, prev_close)
        amplitude = _calc_amplitude(candles, prev_close)

        vwap_bands     = None if concise else _calc_vwap_bands(candles)
        # ── 高點爆量耗竭偵測（v6.2 新增） ──────────────────────
        volume_climax_info = _detect_volume_climax(candles, lookback=20, multiplier=2.0)

        # ── v7.0：MA 黃金/死亡交叉評分 ──────────────────────────
        ma_cross_info  = _calc_ma_cross(candles)
        ma_cross_score = ma_cross_info["score"]   # -2~+2
        ma_cross_label = ma_cross_info["label"]

        # ── 新增：圖形型態識別（楔形/旗形/頭肩/三角形） ──────────
        chart_pattern_info  = _detect_chart_patterns(candles, lookback=20)
        chart_pattern_score = chart_pattern_info["score"]    # -2~+2
        chart_pattern_text  = chart_pattern_info["warning"]  # 給 prompt 的說明

        # ── v7.0：Selling Climax VWAP 站回確認（補充 sc_stage） ──
        if (volume_climax_info and
                volume_climax_info["type"] == "selling_climax" and
                vwap is not None and current_price > vwap):
            sc_vwap_confirm = True
            # 若原本 sc_confirm_count < 2 但 VWAP 站回，給予額外加分
            sc_stage_now = volume_climax_info.get("sc_stage", "stage0_watch")
            if sc_stage_now == "stage0_watch":
                volume_climax_info["sc_stage"] = "stage1_watch"
                volume_climax_info["warning_text"] += (
                    f"\n  ✅ 補充確認：現價 {current_price} 已站回 VWAP {vwap} 上方（正面訊號）"
                )
            elif sc_stage_now == "stage1_watch":
                volume_climax_info["sc_stage"] = "stage2_buy"
                volume_climax_info["warning_text"] += (
                    f"\n  ✅ 補充確認：現價站回 VWAP → 升至可進場階段（BUY）"
                )
        else:
            sc_vwap_confirm = False
        candle_pattern = None if concise else _calc_candle_pattern(candles)
        momentum_text  = None if concise else _calc_price_momentum(candles, n=5)

        if vwap_bands:
            bands = vwap_bands
            vwap_band_text = (
                f"VWAP={bands['vwap']}  σ={bands['std']}\n"
                f"  +1σ={bands['upper1']}  +2σ={bands['upper2']}（超過+2σ=超買，均值回歸風險高）\n"
                f"  -1σ={bands['lower1']}  -2σ={bands['lower2']}（跌破-2σ=超賣，反彈機率高）"
            )
            if current_price >= bands["upper2"]:
                vwap_band_pos = "⚠️ 現價超過+2σ 極端超買區，放空或觀望為主"
            elif current_price >= bands["upper1"]:
                vwap_band_pos = "現價在+1σ~+2σ 偏多但注意過熱"
            elif current_price <= bands["lower2"]:
                vwap_band_pos = "⚠️ 現價跌破-2σ 極端超賣區，做多或觀望為主"
            elif current_price <= bands["lower1"]:
                vwap_band_pos = "現價在-1σ~-2σ 偏空但注意超賣反彈"
            else:
                vwap_band_pos = "現價在±1σ 正常區間（多空拮抗）"
        else:
            vwap_band_text = "資料不足"
            vwap_band_pos  = "無法判斷"

        # ── 1分K明細 ─────────────────────────────────────────────────────────
        candle_lines = "" if concise else "".join(
            f"{c.get('time','')} O:{c['open']} H:{c.get('high',c['close'])} "
            f"L:{c.get('low',c['close'])} C:{c['close']} V:{c['volume']}\n"
            for c in recent
        )

        # ── v8.0 新增：從1分K聚合5分K和15分K，提供給AI多時框參考 ─────────────
        def _resample_candles(src: list, minutes: int) -> list:
            """
            將1分K列表聚合成 N 分K（5分K / 15分K）。
            src 的 time 欄位格式為 \"HH:MM\"。
            """
            if not src:
                return []
            result = []
            bucket = []
            bucket_key = ""
            for c in src:
                t = c.get("time", "")
                if len(t) < 5:
                    continue
                try:
                    h, m = int(t[:2]), int(t[3:5])
                    total_min = h * 60 + m
                    # 對齊到 minutes 的倍數（例如 5 分鐘：09:00,09:05,09:10,...）
                    aligned = (total_min // minutes) * minutes
                    key = f"{aligned // 60:02d}:{aligned % 60:02d}"
                except Exception:
                    continue
                if key != bucket_key:
                    if bucket:
                        result.append({
                            "time":   bucket_key,
                            "open":   bucket[0]["open"],
                            "high":   max(x.get("high", x["close"]) for x in bucket),
                            "low":    min(x.get("low",  x["close"]) for x in bucket),
                            "close":  bucket[-1]["close"],
                            "volume": sum(x.get("volume", 0) for x in bucket),
                        })
                    bucket = [c]
                    bucket_key = key
                else:
                    bucket.append(c)
            if bucket:
                result.append({
                    "time":   bucket_key,
                    "open":   bucket[0]["open"],
                    "high":   max(x.get("high", x["close"]) for x in bucket),
                    "low":    min(x.get("low",  x["close"]) for x in bucket),
                    "close":  bucket[-1]["close"],
                    "volume": sum(x.get("volume", 0) for x in bucket),
                })
            return result

        # 產生5分K（最近12根 = 1小時）和15分K（最近8根 = 2小時）
        _candles_5m  = _resample_candles(candles, 5)   # 用全部1分K聚合
        _candles_15m = _resample_candles(candles, 15)

        # 5分K文字（最近12根）
        _5m_recent = _candles_5m[-12:] if len(_candles_5m) >= 12 else _candles_5m
        candles_5m_lines = "".join(
            f"{c['time']} O:{c['open']} H:{c['high']} L:{c['low']} C:{c['close']} V:{c['volume']}\n"
            for c in _5m_recent
        ) if _5m_recent else "資料不足"

        # 15分K文字（最近8根）
        _15m_recent = _candles_15m[-8:] if len(_candles_15m) >= 8 else _candles_15m
        candles_15m_lines = "".join(
            f"{c['time']} O:{c['open']} H:{c['high']} L:{c['low']} C:{c['close']} V:{c['volume']}\n"
            for c in _15m_recent
        ) if _15m_recent else "資料不足"

        # 5分K技術指標（MA5/MA10）
        _5m_closes = [c["close"] for c in _candles_5m]
        _5m_ma5  = round(sum(_5m_closes[-5:])  / 5,  2) if len(_5m_closes) >= 5  else None
        _5m_ma10 = round(sum(_5m_closes[-10:]) / 10, 2) if len(_5m_closes) >= 10 else None
        _5m_ind  = f"MA5={_5m_ma5}" + (f" MA10={_5m_ma10}" if _5m_ma10 else "")

        # 15分K技術指標（MA5）
        _15m_closes = [c["close"] for c in _candles_15m]
        _15m_ma5  = round(sum(_15m_closes[-5:]) / 5, 2) if len(_15m_closes) >= 5 else None
        _15m_ind  = f"MA5={_15m_ma5}" if _15m_ma5 else "資料不足"

        # ── 日K ─────────────────────────────────────────────────
        daily_text = "無"
        if not concise and daily_candles and len(daily_candles) >= 1:
            d24   = daily_candles[-24:] if len(daily_candles) >= 24 else daily_candles
            lines = [
                f"{d.get('time', d.get('full_time',''))[:10]} "
                f"O:{d['open']} H:{d['high']} L:{d['low']} C:{d['close']} V:{d['volume']}"
                for d in d24
            ]
            daily_text = "\n".join(lines)
            if len(d24) >= 5:
                closes_d    = [d["close"] for d in d24]
                ma5_d       = sum(closes_d[-5:]) / 5
                ma10_d      = sum(closes_d[-10:]) / 10 if len(closes_d) >= 10 else None
                ma20_d      = sum(closes_d[-20:]) / 20 if len(closes_d) >= 20 else None
                recent_high = max(d["high"] for d in d24[-10:]) if len(d24) >= 10 else max(d["high"] for d in d24)
                recent_low  = min(d["low"]  for d in d24[-10:]) if len(d24) >= 10 else min(d["low"]  for d in d24)
                hint        = f"日K MA5={ma5_d:.1f}"
                if ma10_d: hint += f" MA10={ma10_d:.1f}"
                if ma20_d: hint += f" MA20={ma20_d:.1f}"
                hint       += f" | 近10日高:{recent_high} 低:{recent_low}"
                # 加入今日盤中高低（讓AI知道現價在近期高低的相對位置）
                if candles:
                    intra_high = max(c.get("high", 0) for c in candles)
                    intra_low  = min(c.get("low",  0) for c in candles)
                    pos_pct = 0
                    if recent_high > recent_low:
                        pos_pct = round((current_price - recent_low) / (recent_high - recent_low) * 100, 0)
                    hint += f" | 現價在近10日高低間的 {pos_pct:.0f}% 位置"
                    if pos_pct >= 80:
                        hint += "（⚠️ 接近近期高點，追多需謹慎）"
                    elif pos_pct <= 20:
                        hint += "（⚠️ 接近近期低點，放空需謹慎）"
                daily_text  = f"（{hint}）\n" + daily_text

        # ── 技術指標文字 ─────────────────────────────────────────
        ind_parts = []
        if indicators:
            ma = indicators.get("ma", {})
            if ma:
                ind_parts.append(
                    f"MA5={ma.get('ma5','?')} MA10={ma.get('ma10','?')} MA20={ma.get('ma20','?')}"
                )
            macd = indicators.get("macd", {})
            if macd:
                ind_parts.append(
                    f"MACD DIF={macd.get('dif','?')} DEA={macd.get('dea','?')} "
                    f"HIST={macd.get('histogram','?')}"
                )
            rsi = indicators.get("rsi")
            if rsi:
                ind_parts.append(f"RSI={rsi}")
            kdj = indicators.get("kdj", {})
            if kdj:
                ind_parts.append(
                    f"KDJ K={kdj.get('k','?')} D={kdj.get('d','?')} J={kdj.get('j','?')}"
                )
        ind_text = " | ".join(ind_parts) if ind_parts else "無"

        # ── 分價量表 ─────────────────────────────────────────────
        vol_text      = "無"
        support_text  = ""
        pressure_text = ""
        if volumes_data:
            total_vol   = sum(v.get("volume", 0) for v in volumes_data) or 1
            significant = sorted(
                [v for v in volumes_data if v.get("volume", 0) / total_vol >= 0.05],
                key=lambda x: x.get("price", 0)
            )
            lines = []
            for v in significant:
                p       = v.get("price", 0)
                vol     = v.get("volume", 0)
                at_bid  = v.get("volumeAtBid", 0)
                at_ask  = v.get("volumeAtAsk", 0)
                pct     = round(vol / total_vol * 100, 1)
                ba_tot  = (at_bid + at_ask) or 1
                ask_pct = round(at_ask / ba_tot * 100)
                tag     = "【支撐傾向】" if ask_pct >= 60 else ("【壓力傾向】" if ask_pct <= 40 else "")
                lines.append(f"  價:{p} 量佔{pct}% 外盤{ask_pct}% {tag}")
            vol_text = "\n".join(lines) if lines else "無顯著成交集中區"

            below = [v for v in volumes_data if float(v.get("price", 0)) < current_price]
            above = [v for v in volumes_data if float(v.get("price", 0)) > current_price]
            if below:
                top2   = sorted(below, key=lambda x: x.get("volume", 0), reverse=True)[:2]
                support_text = "、".join(str(v["price"]) for v in sorted(top2, key=lambda x: x["price"]))
            if above:
                top2   = sorted(above, key=lambda x: x.get("volume", 0), reverse=True)[:2]
                pressure_text = "、".join(str(v["price"]) for v in sorted(top2, key=lambda x: x["price"]))

        # ── 最佳五檔 ─────────────────────────────────────────────
        orderbook_text   = "無（trades_only 模式或非 full WS）"
        ob_pressure_hint = "無資料"
        if orderbook:
            raw_bids = orderbook.get("bids", [])
            raw_asks = orderbook.get("asks", [])
            bid5 = sorted(raw_bids, key=lambda x: -float(x.get("price", 0)))[:5]
            ask5 = sorted(raw_asks, key=lambda x:  float(x.get("price", 0)))[:5]

            ob_lines = []
            for a in reversed(ask5):
                p  = a.get("price", "--")
                sz = a.get("size",  "--")
                ob_lines.append(f"  賣 {p}  張:{sz}")
            ob_lines.append(f"  ──── 現價 {current_price} ────")
            for b in bid5:
                p  = b.get("price", "--")
                sz = b.get("size",  "--")
                ob_lines.append(f"  買 {p}  張:{sz}")

            orderbook_text = "\n".join(ob_lines) if ob_lines else "無資料"

            total_bid = sum(int(b.get("size", 0)) for b in bid5)
            total_ask = sum(int(a.get("size", 0)) for a in ask5)
            if total_bid + total_ask > 0:
                bid_pct = round(total_bid / (total_bid + total_ask) * 100)
                ask_pct = 100 - bid_pct
                if bid_pct >= 60:
                    ob_pressure_hint = f"委買量佔 {bid_pct}%，掛單偏多方（買盤積極，短期支撐強）"
                elif ask_pct >= 60:
                    ob_pressure_hint = f"委賣量佔 {ask_pct}%，掛單偏空方（賣壓較大，短期壓力強）"
                else:
                    ob_pressure_hint = f"委買{bid_pct}% vs 委賣{ask_pct}%，掛單均衡（多空拮抗）"

        # ── 今日先前分析（v7.1：強化前次分析比對，含完整理由與進場條件追蹤）─────
        prev_text = "無"
        prev_context_block = ""  # 給 prompt 用的前次比對分析區塊

        def _extract_prev_reason(entry: dict) -> str:
            """從前次分析記錄中提取關鍵資訊（理由、條件、警告）。"""
            r = entry.get("result", {})
            full = r.get("full_text", "")
            lines = []
            if full:
                for line in full.splitlines():
                    l = line.strip()
                    # 擷取關鍵判斷行（理由、風險、ORB、量能、突破）
                    if any(kw in l for kw in [
                        "ORB", "RVOL", "假突破", "真突破", "縮量", "放量", "爆量",
                        "確認條件", "進場標準", "等待", "站回", "VWAP", "RSI",
                        "需要", "若", "當", "一旦", "量持續", "放量確認",
                    ]):
                        lines.append(f"    {l}")
                    if len(lines) >= 6:  # 最多擷取6行關鍵理由
                        break
            return "\n".join(lines) if lines else ""

        if prev_entries and not concise:
            parts = []
            for e in prev_entries[-5:]:
                r = e.get("result", {})
                sig = r.get("signal", "?").upper()
                sug = r.get("suggestion", "")
                ent = r.get("entry", "--")
                sl  = r.get("stop_loss", "--")
                tgt = r.get("target", "--")
                t   = e.get("time", "")
                key_reason = _extract_prev_reason(e)
                part = (
                    f"  [{t}] {sig} {sug}\n"
                    f"    進:{ent} 停:{sl} 目標:{tgt}"
                )
                if key_reason:
                    part += f"\n    關鍵判斷：\n{key_reason}"
                parts.append(part)
            prev_text = "\n".join(parts)

            # 建立前次分析比對指令（僅取最後一筆）
            last_e = prev_entries[-1]
            last_r = last_e.get("result", {})
            last_sig  = last_r.get("signal", "watch").upper()
            last_full = last_r.get("full_text", "")
            last_time = last_e.get("time", "")
            last_entry = last_r.get("entry", "--")

            # 從前次 full_text 提取「進場條件/等待條件」
            wait_conditions = []
            if last_full:
                for line in last_full.splitlines():
                    l = line.strip()
                    if any(kw in l for kw in [
                        "等待", "需", "若", "當", "一旦", "放量", "量確認",
                        "縮量收紅", "站回VWAP", "RVOL", "確認K棒", "假突破",
                        "進場標準", "進場條件", "量持續", "突破確認",
                    ]):
                        wait_conditions.append(f"    - {l}")
                    if len(wait_conditions) >= 5:
                        break

            wait_cond_str = "\n".join(wait_conditions) if wait_conditions else "    （前次未明確列出等待條件）"

            prev_context_block = (
                f"\n【🔄 前次分析比對（強制執行）— [{last_time}] 訊號:{last_sig}】\n"
                f"前次分析提出的等待條件/進場標準：\n"
                f"{wait_cond_str}\n"
                f"\n⚡【本次必須明確回答以下問題，寫在「理由」區塊的第一條】：\n"
                f"  1. 前次分析({last_time})訊號為 {last_sig}，提出了哪些進場條件？\n"
                f"  2. 本次現價({current_price}) 是否已達到前次的進場標準？\n"
                f"     • 若是 → 說明哪些條件已滿足，評估是否可以進場（BUY/SHORT）\n"
                f"     • 若否 → 說明哪些條件仍未達到，繼續 WATCH 並更新等待條件\n"
                f"  3. 量能變化追蹤：前次關注的量能條件（RVOL/爆量/縮量），\n"
                f"     現在量能狀況如何？是否與前次預期方向一致？\n"
                f"  4. 若前次說「假突破」但量持續放大，需重新評估為真突破可能性。\n"
                f"  5. 若前次說「等待縮量確認」但現在依然爆量，需說明是否改變判斷。\n"
            )

        elif prev_entries and concise:
            last = prev_entries[-1]
            r = last.get("result", {})
            prev_text = (
                f"[{last.get('time','')}] {r.get('signal','?')} "
                f"進:{r.get('entry','--')} "
                f"停:{r.get('stop_loss','--')} "
                f"目標:{r.get('target','--')}"
            )
            prev_context_block = ""

        support_hint  = f"（量能支撐參考：{support_text}）"  if support_text  else ""
        pressure_hint = f"（量能壓力參考：{pressure_text}）" if pressure_text else ""

        tick_unit_str   = _tick_label(current_price)
        entry_lo_ex     = _snap_tick(current_price * 0.999, "floor")
        entry_hi_ex     = _snap_tick(current_price * 1.001, "ceil")
        stop_loss_ex    = _snap_tick(current_price * 0.993, "floor")
        target_ex       = _snap_tick(current_price * 1.01,  "ceil")
        short_entry_lo  = _snap_tick(current_price * 0.999, "floor")
        short_entry_hi  = _snap_tick(current_price * 1.001, "ceil")
        short_stop_ex   = _snap_tick(current_price * 1.007, "ceil")
        short_target_ex = _snap_tick(current_price * 0.99,  "floor")

        force_block = ""
        if force_watch:
            force_block = "\n⛔⛔⛔ 強制觀望：SIGNAL 必須輸出 WATCH，禁止任何新倉建議。⛔⛔⛔\n"
        elif stage_info["risk_level"] == "high":
            force_block = (
                "\n⚠️ 高風險提醒：僅在趨勢極為明確（STRONG_BUY 或 STRONG_SHORT）"
                "時才可建議新倉，否則輸出 WATCH。\n"
            )

        # ── 漲跌停資訊區塊 ──────────────────────────────────────
        if limit_up and limit_down:
            limit_block = (
                f"\n【🚦 當日漲跌停價格（強制遵守）】\n"
                f"漲停價：{limit_up}（現價距漲停 {round(limit_up - current_price, 2):+.2f} 元，"
                f"{round((limit_up - current_price) / current_price * 100, 1):+.1f}%）\n"
                f"跌停價：{limit_down}（現價距跌停 {round(limit_down - current_price, 2):+.2f} 元，"
                f"{round((limit_down - current_price) / current_price * 100, 1):+.1f}%）\n"
                f"規定：\n"
                f"  • 目標（做多）不得超過漲停價 {limit_up}\n"
                f"  • 目標（放空）不得低於跌停價 {limit_down}\n"
                f"  • 停損（做多）若低於跌停 {limit_down}，須加注流動性風險警示\n"
                f"  • 停損（放空）若高於漲停 {limit_up}，須加注流動性風險警示\n"
                f"  • 現價若在漲停/跌停 0.5% 範圍內：SIGNAL 強制輸出 WATCH\n"
            )
        else:
            limit_block = "\n【漲跌停價格】無昨收資料，請自行注意漲跌幅限制（±10%）。\n"

        risk_block = _RISK_MODE_PROMPT.get(risk_mode or "auto", _RISK_MODE_PROMPT["auto"])

        # ── v8.0：MTF 多時間框架模態共識分析 ──────────────────────
        # 【研究依據】2025/2026當沖高勝率研究顯示：
        # 多時框共識（MTF Confluence）是提升勝率的最核心方法。
        # 單一時框勝率約50-55%；三框共識可提升至65-70%。
        # 框架：日K定方向 → 15分K定結構 → 5分K定動能 → 1分K執行

        def _calc_mtf_trend(closes: list, period: int = 5) -> str:
            """計算一組收盤價的簡單趨勢方向。"""
            if len(closes) < period + 1:
                return "資料不足"
            mid = len(closes) // 2
            avg_first = sum(closes[:mid]) / mid
            avg_last = sum(closes[mid:]) / max(len(closes) - mid, 1)
            diff_pct = (avg_last - avg_first) / avg_first * 100 if avg_first else 0
            if diff_pct > 0.5:
                return "上升"
            elif diff_pct < -0.5:
                return "下降"
            return "橫盤"

        # 1. 日K模態（最近5日趨勢）
        daily_closes_mtf = []
        if daily_candles and len(daily_candles) >= 3:
            daily_closes_mtf = [d["close"] for d in daily_candles[-10:]]
        daily_trend_mtf = _calc_mtf_trend(daily_closes_mtf, 3) if daily_closes_mtf else "資料不足"

        # 2. 15分K模態（最近8根 ≈ 2小時）
        _15m_closes_mtf = [c["close"] for c in _candles_15m[-8:]] if _candles_15m else []
        trend_15m = _calc_mtf_trend(_15m_closes_mtf, 3) if len(_15m_closes_mtf) >= 4 else "資料不足"

        # 3. 5分K模態（最近12根 ≈ 1小時）
        _5m_closes_mtf = [c["close"] for c in _candles_5m[-12:]] if _candles_5m else []
        trend_5m = _calc_mtf_trend(_5m_closes_mtf, 4) if len(_5m_closes_mtf) >= 5 else "資料不足"

        # 4. 1分K模態（最近10根）
        _1m_closes_mtf = [c["close"] for c in candles[-10:]] if candles else []
        trend_1m = _calc_mtf_trend(_1m_closes_mtf, 4) if len(_1m_closes_mtf) >= 5 else "資料不足"

        # MTF 共識評分（加權：日K×3, 15分K×2, 5分K×2, 1分K×1）
        def _trend_score(t: str) -> int:
            return 1 if t == "上升" else (-1 if t == "下降" else 0)

        mtf_score_raw = (
            _trend_score(daily_trend_mtf) * 3 +
            _trend_score(trend_15m) * 2 +
            _trend_score(trend_5m) * 2 +
            _trend_score(trend_1m) * 1
        )
        mtf_max = 8  # 最大加權分數

        # MTF 共識程度判定
        mtf_trends = [daily_trend_mtf, trend_15m, trend_5m, trend_1m]
        mtf_up_count   = sum(1 for t in mtf_trends if t == "上升")
        mtf_dn_count   = sum(1 for t in mtf_trends if t == "下降")
        mtf_flat_count = sum(1 for t in mtf_trends if t == "橫盤")

        if mtf_up_count >= 3:
            mtf_consensus = "多頭共識"
            mtf_bias = "做多"
            mtf_emoji = "🟢"
        elif mtf_dn_count >= 3:
            mtf_consensus = "空頭共識"
            mtf_bias = "放空"
            mtf_emoji = "🔴"
        elif mtf_up_count == 2 and mtf_dn_count <= 1:
            mtf_consensus = "偏多（未完全共識）"
            mtf_bias = "偏多"
            mtf_emoji = "🟡"
        elif mtf_dn_count == 2 and mtf_up_count <= 1:
            mtf_consensus = "偏空（未完全共識）"
            mtf_bias = "偏空"
            mtf_emoji = "🟠"
        else:
            mtf_consensus = "多空分歧（觀望）"
            mtf_bias = "觀望"
            mtf_emoji = "⚪"

        # MTF 衝突警告（逆勢訊號最危險）
        mtf_conflict_warn = ""
        if daily_trend_mtf == "下降" and (trend_15m == "上升" or trend_5m == "上升"):
            mtf_conflict_warn = (
                "⚠️【MTF衝突警告】日K下降趨勢中，低週期出現反彈訊號。"
                "此情況為「逆勢反彈」，風險高於順勢單，證據門檻應提高。\n"
                "  → 若要做多：需日K MA支撐確認 + RVOL≥2倍以上，且停損須更嚴格（0.5ATR內）\n"
                "  → 建議：以放空或觀望為主，等待日K轉向再考慮做多\n"
            )
        elif daily_trend_mtf == "上升" and (trend_15m == "下降" or trend_5m == "下降"):
            mtf_conflict_warn = (
                "⚠️【MTF衝突警告】日K上升趨勢中，低週期出現回調訊號。"
                "此情況為「趨勢回調」，逆勢放空證據門檻應提高。\n"
                "  → 回調至支撐（VWAP/MA）後止跌確認，才是做多最佳時機\n"
                "  → 避免在回調中途追空，等待1分K出現止跌K棒再做多\n"
            )


        # ── STEP 2/3/4 判斷邏輯（依 risk_mode 動態切換，v11/v12）─────────
        # 原本 BUY/SHORT 門檻要求「MTF≥+2分」和「技術≥4分」同時滿足（AND），
        # 台股當沖三個時框同時對齊的機率本身不高，兩道門檻疊加後
        # 實際能通過的情境很窄，導致 WATCH 佔多數。
        # 這不是模型判斷保守，是門檻的數學組合本身就篩掉了大部分盤勢。
        #
        # relaxed 模式：BUY/SHORT 改成「MTF 或 技術分擇一達標即可」，
        # 技術分門檻同時從 4 分降到 3 分，讓訊號出現頻率明顯提高，
        # 使用者可以自行比對「訊號變多後，命中率（見歷史紀錄頁面的實測數據）
        # 是否仍可接受」。STRONG_BUY/SHORT 的三框共識門檻不變，
        # 因為那是最高信心等級的訊號，不應該被放寬。
        #
        # simplified 模式（v12 新增）：完全跳過 11 項指標加總評分，
        # 改用少數幾個有實證研究支持、且不需要複雜運算的核心條件直接判斷
        # （ORB / VWAP / RVOL / 個股是否為當日活躍股 + 交易成本意識）。
        # 設計動機：多步驟評分加總的推理鏈越長，越依賴模型的精算能力，
        # 較弱的模型（如 gemma 系列）容易在中間環節算錯或前後矛盾；
        # 精簡版刻意縮短推理鏈，用來跟複雜版做真實命中率對照，
        # 而非預設「精簡=更準」——這需要靠歷史紀錄頁面的實測數據驗證。
        if risk_mode == "simplified":
            _rvol_display = f"{rvol_val}x" if rvol_val is not None else "無資料"
            step234_block = (
                "═══════════════════════════════════════════════════════\n"
                "▌ STEP 2-4（精簡核心版）：僅用 4 項核心條件直接判斷\n"
                "═══════════════════════════════════════════════════════\n"
                f"VWAP：{vwap_text}\n"
                f"量比：{vol_ratio or '資料不足'}　RVOL：{_rvol_display}　振幅：{amplitude or '計算中'}\n"
                "\n"
                "① ORB：現價是否站上/跌破開盤前5~15分鐘區間高/低點？\n"
                "② VWAP方向：現價在VWAP之上（偏多）或之下（偏空）？乖離是否>2×ATR（追價風險）？\n"
                "③ RVOL：是否≥1.5（有效參與，非假突破）？\n"
                "④ 今日是否為活躍股：開盤後量能/振幅是否明顯放大，而非全日平淡盤整？\n"
                "   （研究顯示只有真正活躍、存在供需失衡的個股，開盤後的訊號才較可能延續；\n"
                "    平淡個股即使形態符合也應優先觀望，而非把訊號複雜化來湊理由）\n"
                "\n"
                "【判斷原則】\n"
                "• ①②③④ 全部同向 → 可給 BUY/SHORT；同時有明確量價確認（如突破當根爆量）才給 STRONG\n"
                "• 任一項明顯反向或依據不足 → 直接 WATCH，不用其他指標湊理由硬凹\n"
                "• 交易成本意識：來回手續費+證交稅約0.15~0.3%量級，若目標空間扣除成本後\n"
                "  所剩無幾，即使方向正確也應給 WATCH 或註明「空間不足，不建議進場」\n"
                "• 不追高殺低：現價乖離VWAP過大時，即使動能訊號存在也應保守看待\n"
                "• 🚫硬性否決（優先於①②③④全部同向的條件）：若下方【近24日日K】提示現價\n"
                "  「接近近期低點」，禁止輸出SHORT/STRONG_SHORT；提示「接近近期高點」，\n"
                "  禁止輸出BUY/STRONG_BUY——即使①②③④都同向也一律WATCH。\n"
                "  近期極端位置的逆勢單風險遠高於順勢單，寧可錯過也不要在此追價。\n"
                "\n"
                "【訊號門檻】\n"
                "STRONG_BUY/SHORT：①②③④ 全部同向 + 突破當根有明確爆量確認\n"
                "BUY/SHORT：①②③④ 全部同向，但無需爆量確認\n"
                "WATCH：任一項反向、依據不足、或扣除成本後空間不足\n"
            )
        elif risk_mode == "relaxed":
            _ma_label0 = ma_cross_info['label'].splitlines()[0]
            _pattern_hint = chart_pattern_info['breakout_hint']
            step234_block = (
                "═══════════════════════════════════════════════════════\n"
                "▌ STEP 2：MTF 多時間框架方向共識（決定操作方向）\n"
                "═══════════════════════════════════════════════════════\n"
                f"MTF共識狀態：{mtf_consensus}（{mtf_bias}）\n"
                f"當前MTF加權分：{mtf_score_raw:+d}/{mtf_max}\n"
                "\n"
                "【MTF評分規則（最高4分）】\n"
                "+4分：強共識（日K+15分K+5分K三框同向）→ STRONG_BUY/SHORT 可用\n"
                "+2分：弱共識（含日K或15分K的二框同向）→ 僅BUY/SHORT可用\n"
                " 0分：MTF分歧（1框或0框同向）→ 強制WATCH\n"
                "-2分：MTF衝突（日K與操作方向相反）→ 禁STRONG，BUY需SC耗竭才例外\n"
                "\n"
                "【核心規則】日K趨勢（最高框）決定生死：\n"
                "→ 日K下降中，低框反彈 = 逆勢，禁STRONG_BUY，WATCH或放空優先\n"
                "→ 日K上升中，低框回調 = 順勢回測，等VWAP回測止跌確認再做多\n"
                "→ 日K橫盤 = 不表態，以15分K方向為主，ORB突破方向為進場依據\n"
                "\n"
                "【洗盤識別（強勢進場訊號）】\n"
                "若觀察到以下組合，為主力洗盤後突破，證據較充分（STRONG訊號候選）：\n"
                " ▸ 量縮整理3-5根K棒（洗盤蓄勢）→ 帶量突破前高/VWAP/均線\n"
                " ▸ 下跌量縮（賣壓有限）→ 站回均線量增（資金承接確認）\n"
                " ▸ 打壓跌破均線後2-3根即站回（假破洗盤結束）\n"
                "\n"
                "═══════════════════════════════════════════════════════\n"
                "▌ STEP 3：技術指標評分（輔助確認，共10分）\n"
                "═══════════════════════════════════════════════════════\n"
                "每項1分，滿分10分，評分須逐項說明（不可跳過）：\n"
                "\n"
                "① ORB突破品質：\n"
                "   +1分：突破OR15/OR30高低點 + RVOL≥1.5 + 非已在突破點上方>2%（真突破）\n"
                "   +0分：突破但量不足（RVOL<1.5）= 假突破嫌疑，不計分\n"
                "   +0分：盤整在OR區間內\n"
                "   ★ 證據最充分：突破→縮量回測→不破OR關鍵價→放量二次突破（VWAP回測確認模式）\n"
                "\n"
                "② VWAP方向：\n"
                "   做多：現價在VWAP上方 +1分（VWAP未下彎）\n"
                "   放空：現價在VWAP下方 +1分（VWAP未上彎）\n"
                "   現價≈VWAP（±0.3%）= 0分（中性地帶，方向不明）\n"
                "\n"
                "③ VWAP回測進場（加分項，最高優先進場模式）：\n"
                "   +1分：突破後回測VWAP支撐，收縮量確認K（紅/黑）→ 此為「VWAP錨定入場」\n"
                "   此模式：停損≤0.5ATR，風報比≥3:1，屬於較嚴謹的進場模式\n"
                "\n"
                "④ 量能品質判斷：\n"
                "   +1分：RVOL≥1.5 且現價未在日內高點區≥70%（真實放量突破）\n"
                "   -1分：RVOL≥2.0 且現價在日內高點≥70%（Buying Climax，做多危險）\n"
                "    0分：RVOL<1.5（量能不足，訊號弱）\n"
                "\n"
                "⑤ RSI多框一致性（比單一RSI更重要）：\n"
                "   做多：1分K RSI 45-72 且 5分K RSI 42-70（雙框偏強，未超買）→ +1分\n"
                "   放空：1分K RSI 28-55 且 5分K RSI 30-58（雙框偏弱，未超賣）→ +1分\n"
                "   RSI背離（價格創新高但RSI未創高）→ 反轉警告，做多方向扣1分\n"
                "   RSI>78（超買）或RSI<22（超賣）= 極端值，方向性訊號，注意衝突\n"
                "\n"
                "⑥ OBV資金流向：\n"
                "   +1分：OBV方向與進場方向一致（資金在流入/流出方向確認）\n"
                "   -1分：OBV嚴重背離（價漲OBV跌，或價跌OBV漲）= 大戶出貨/抄底訊號\n"
                "\n"
                "⑦ 日K大結構：\n"
                "   +1分：做多且日K MA5>MA10>MA20（多頭完整排列）\n"
                "   +1分：放空且日K MA5<MA10<MA20（空頭完整排列）\n"
                "    0分：日K均線纏繞或轉折中（等待確認）\n"
                "   注意：近10日高低點位置 — 現價在80%以上追多扣1分，在20%以下追空扣1分\n"
                "\n"
                "⑧ 爆量耗竭訊號（量能耗竭型態識別）：\n"
                "   Buying Climax（高點≥70%+爆量≥2x）：做多-1分，放空+1分\n"
                "   Selling Climax Stage2（≥2確認）：做多+1分（可試多）\n"
                "   Selling Climax Stage0（無確認）：強制WATCH，做多-1分\n"
                "   主力洗盤結束帶量突破：做多+1分（較強勢型態）\n"
                "\n"
                "⑨ 五檔掛單壓力：\n"
                "   +1分：委買量>60%（短期支撐強）\n"
                "   +1分：委賣量>60%（賣壓重）\n"
                "    0分：均衡40-60%或無資料\n"
                "\n"
                "⑩ MA交叉信號（分K均線，非日K）：\n"
                "   +2分：強黃金交叉（MA5剛上穿MA10，MA10>MA20）\n"
                "   +1分：弱黃金交叉（MA5>MA10多頭排列）\n"
                "   -2分：強死亡交叉（MA5剛下穿MA10，MA10<MA20）→ 做多方向-2分\n"
                "   -1分：弱死亡交叉（MA5<MA10空頭排列）\n"
                f"   當前MA評分：{ma_cross_score:+d}分 | {_ma_label0}\n"
                "\n"
                "⑪ K線圖形型態識別（楔形/旗形/三角形/頭肩）：\n"
                "   +2分：上升旗形/頭肩底/下降楔形（確認後等量突破）→ 常見延續看多型態\n"
                "   +1分：上升三角形（水平阻力+低點上升，等突破）\n"
                "   -1分：上升楔形/下降三角形（收斂後看空）\n"
                "   -2分：下降旗形/頭肩頂（確認後看空）\n"
                "    0分：矩形整理（等待突破方向）或無明顯型態\n"
                f"   當前型態評分：{chart_pattern_score:+d}分 | {_pattern_hint}\n"
                "   注意：所有型態突破均需量能確認（RVOL≥1.5），否則降一級處理\n"
                "\n"
                "═══════════════════════════════════════════════════════\n"
                "▌ STEP 4：綜合判斷門檻（v9.0 更嚴格）\n"
                "═══════════════════════════════════════════════════════\n"
                "綜合分 = MTF分（0~4）+ 技術分（-4~10）\n"
                "\n"
                "【訊號門檻（實驗性寬鬆版，非常規設定）】\n"
                "STRONG_BUY/SHORT：MTF≥+4分（三框共識）+ 技術≥6分 + 無MTF衝突 + 無BC　"
                "← 與標準模式相同，不放寬\n"
                "BUY/SHORT：MTF≥+2分「或」技術≥3分（兩者擇一達標即可，不需同時滿足）+ 無強MTF衝突\n"
                "WATCH：MTF≥+2分與技術≥3分「兩者都」不達標，或SC第零階段，或環境過濾觸發\n"
                "⚠️ 放寬的是「門檻組合方式」，不是放寬「證據要求」本身——\n"
                "  單一條件仍要符合原本的判定標準（例如技術分3分是真的評出3分，不能灌水）。\n"
                "  若連寬鬆門檻都不滿足，仍須誠實輸出 WATCH，不可為了給訊號而勉強。\n"
                "\n"
                "🚫【硬性否決規則，優先於上述門檻，即使綜合分達標也強制降級為WATCH】\n"
                "此規則不可被「日K位置」評分項的-1分取代——那只是軟性扣分，\n"
                "容易被其他加分項抵銷；以下是不可迴避的否決條件：\n"
                "→ 現價位於近10日高低點區間的『後20%』（即接近近期最低點）時，\n"
                "  禁止輸出 SHORT/STRONG_SHORT，無論MTF或技術分數多高——\n"
                "  這是追空最危險的位置（超跌後容易反彈），寧可WATCH也不可放空。\n"
                "→ 現價位於近10日高低點區間的『前20%』（即接近近期最高點）時，\n"
                "  禁止輸出 BUY/STRONG_BUY，理由對稱（追高容易被軋、反轉風險最高）。\n"
                "→ 此否決規則的優先順序高於「MTF或技術擇一達標」的寬鬆門檻，\n"
                "  因為寬鬆門檻只降低了「證據數量」的要求，不代表可以忽略\n"
                "  「逆勢追價」這種結構性風險，兩者是不同維度的判斷。\n"
            )
        else:
            step234_block = (
                "═══════════════════════════════════════════════════════\n"
                "▌ STEP 2：MTF 多時間框架方向共識（決定操作方向）\n"
                "═══════════════════════════════════════════════════════\n"
                f"MTF共識狀態：{mtf_consensus}（{mtf_bias}）\n"
                f"當前MTF加權分：{mtf_score_raw:+d}/{mtf_max}\n"
                "\n"
                "【MTF評分規則（最高4分）】\n"
                "+4分：強共識（日K+15分K+5分K三框同向）→ STRONG_BUY/SHORT 可用\n"
                "+2分：弱共識（含日K或15分K的二框同向）→ 僅BUY/SHORT可用\n"
                " 0分：MTF分歧（1框或0框同向）→ 強制WATCH\n"
                "-2分：MTF衝突（日K與操作方向相反）→ 禁STRONG，BUY需SC耗竭才例外\n"
                "\n"
                "【核心規則】日K趨勢（最高框）決定生死：\n"
                "→ 日K下降中，低框反彈 = 逆勢，禁STRONG_BUY，WATCH或放空優先\n"
                "→ 日K上升中，低框回調 = 順勢回測，等VWAP回測止跌確認再做多\n"
                "→ 日K橫盤 = 不表態，以15分K方向為主，ORB突破方向為進場依據\n"
                "\n"
                "【洗盤識別（強勢進場訊號）】\n"
                "若觀察到以下組合，為主力洗盤後突破，證據較充分（STRONG訊號候選）：\n"
                " ▸ 量縮整理3-5根K棒（洗盤蓄勢）→ 帶量突破前高/VWAP/均線\n"
                " ▸ 下跌量縮（賣壓有限）→ 站回均線量增（資金承接確認）\n"
                " ▸ 打壓跌破均線後2-3根即站回（假破洗盤結束）\n"
                "\n"
                "═══════════════════════════════════════════════════════\n"
                "▌ STEP 3：技術指標評分（輔助確認，共10分）\n"
                "═══════════════════════════════════════════════════════\n"
                "每項1分，滿分10分，評分須逐項說明（不可跳過）：\n"
                "\n"
                "① ORB突破品質：\n"
                "   +1分：突破OR15/OR30高低點 + RVOL≥1.5 + 非已在突破點上方>2%（真突破）\n"
                "   +0分：突破但量不足（RVOL<1.5）= 假突破嫌疑，不計分\n"
                "   +0分：盤整在OR區間內\n"
                "   ★ 證據最充分：突破→縮量回測→不破OR關鍵價→放量二次突破（VWAP回測確認模式）\n"
                "\n"
                "② VWAP方向：\n"
                "   做多：現價在VWAP上方 +1分（VWAP未下彎）\n"
                "   放空：現價在VWAP下方 +1分（VWAP未上彎）\n"
                "   現價≈VWAP（±0.3%）= 0分（中性地帶，方向不明）\n"
                "\n"
                "③ VWAP回測進場（加分項，最高優先進場模式）：\n"
                "   +1分：突破後回測VWAP支撐，收縮量確認K（紅/黑）→ 此為「VWAP錨定入場」\n"
                "   此模式：停損≤0.5ATR，風報比≥3:1，屬於較嚴謹的進場模式\n"
                "\n"
                "④ 量能品質判斷：\n"
                "   +1分：RVOL≥1.5 且現價未在日內高點區≥70%（真實放量突破）\n"
                "   -1分：RVOL≥2.0 且現價在日內高點≥70%（Buying Climax，做多危險）\n"
                "    0分：RVOL<1.5（量能不足，訊號弱）\n"
                "\n"
                "⑤ RSI多框一致性（比單一RSI更重要）：\n"
                "   做多：1分K RSI 45-72 且 5分K RSI 42-70（雙框偏強，未超買）→ +1分\n"
                "   放空：1分K RSI 28-55 且 5分K RSI 30-58（雙框偏弱，未超賣）→ +1分\n"
                "   RSI背離（價格創新高但RSI未創高）→ 反轉警告，做多方向扣1分\n"
                "   RSI>78（超買）或RSI<22（超賣）= 極端值，方向性訊號，注意衝突\n"
                "\n"
                "⑥ OBV資金流向：\n"
                "   +1分：OBV方向與進場方向一致（資金在流入/流出方向確認）\n"
                "   -1分：OBV嚴重背離（價漲OBV跌，或價跌OBV漲）= 大戶出貨/抄底訊號\n"
                "\n"
                "⑦ 日K大結構：\n"
                "   +1分：做多且日K MA5>MA10>MA20（多頭完整排列）\n"
                "   +1分：放空且日K MA5<MA10<MA20（空頭完整排列）\n"
                "    0分：日K均線纏繞或轉折中（等待確認）\n"
                "   注意：近10日高低點位置 — 現價在80%以上追多扣1分，在20%以下追空扣1分\n"
                "\n"
                "⑧ 爆量耗竭訊號（量能耗竭型態識別）：\n"
                "   Buying Climax（高點≥70%+爆量≥2x）：做多-1分，放空+1分\n"
                "   Selling Climax Stage2（≥2確認）：做多+1分（可試多）\n"
                "   Selling Climax Stage0（無確認）：強制WATCH，做多-1分\n"
                "   主力洗盤結束帶量突破：做多+1分（較強勢型態）\n"
                "\n"
                "⑨ 五檔掛單壓力：\n"
                "   +1分：委買量>60%（短期支撐強）\n"
                "   +1分：委賣量>60%（賣壓重）\n"
                "    0分：均衡40-60%或無資料\n"
                "\n"
                "⑩ MA交叉信號（分K均線，非日K）：\n"
                "   +2分：強黃金交叉（MA5剛上穿MA10，MA10>MA20）\n"
                "   +1分：弱黃金交叉（MA5>MA10多頭排列）\n"
                "   -2分：強死亡交叉（MA5剛下穿MA10，MA10<MA20）→ 做多方向-2分\n"
                "   -1分：弱死亡交叉（MA5<MA10空頭排列）\n"
                f"   當前MA評分：{ma_cross_score:+d}分 | {ma_cross_info['label'].splitlines()[0]}\n"
                "\n"
                "⑪ K線圖形型態識別（楔形/旗形/三角形/頭肩）：\n"
                "   +2分：上升旗形/頭肩底/下降楔形（確認後等量突破）→ 常見延續看多型態\n"
                "   +1分：上升三角形（水平阻力+低點上升，等突破）\n"
                "   -1分：上升楔形/下降三角形（收斂後看空）\n"
                "   -2分：下降旗形/頭肩頂（確認後看空）\n"
                "    0分：矩形整理（等待突破方向）或無明顯型態\n"
                f"   當前型態評分：{chart_pattern_score:+d}分 | {chart_pattern_info['breakout_hint']}\n"
                "   注意：所有型態突破均需量能確認（RVOL≥1.5），否則降一級處理\n"
                "\n"
                "═══════════════════════════════════════════════════════\n"
                "▌ STEP 4：綜合判斷門檻（v9.0 更嚴格）\n"
                "═══════════════════════════════════════════════════════\n"
                "綜合分 = MTF分（0~4）+ 技術分（-4~10）\n"
                "\n"
                "【訊號門檻（從嚴執行）】\n"
                "STRONG_BUY/SHORT：MTF≥+4分（三框共識）+ 技術≥6分 + 無MTF衝突 + 無BC\n"
                "BUY/SHORT：MTF≥+2分（含日K框） + 技術≥4分 + 無強MTF衝突\n"
                "WATCH：MTF=0（分歧/衝突），或技術<4分，或SC第零階段，或環境過濾觸發\n"
                "\n"
                "🚫【硬性否決規則，優先於上述門檻，即使綜合分達標也強制降級為WATCH】\n"
                "此規則不可被「日K位置」評分項的-1分取代——那只是軟性扣分，\n"
                "容易被其他加分項抵銷；以下是不可迴避的否決條件：\n"
                "→ 現價位於近10日高低點區間的『後20%』（即接近近期最低點）時，\n"
                "  禁止輸出 SHORT/STRONG_SHORT，無論MTF或技術分數多高——\n"
                "  這是追空最危險的位置（超跌後容易反彈），寧可WATCH也不可放空。\n"
                "→ 現價位於近10日高低點區間的『前20%』（即接近近期最高點）時，\n"
                "  禁止輸出 BUY/STRONG_BUY，理由對稱（追高容易被軋、反轉風險最高）。\n"
            )

        # ── 【v13 新增】最終輸出區塊（強弱勢判讀＋型態清單＋理由欄位規範）──
        # 依 risk_mode 動態切換，避免 simplified 模式底下，AI 仍被要求在
        # 「理由」欄位逐項交代 MTF共識/RSI雙框/OBV/五檔掛單等它根本沒有
        # 計算過的項目——那樣只會逼 AI 硬編數字或套用模板文字來湊格式，
        # 恰好是精簡模式想要避免的「灌水評分」問題。
        # 非 simplified 模式維持原本完整版的理由欄位與型態清單不變。
        if risk_mode == "simplified":
            final_output_block = (
                "【判斷依據僅限上述4項核心條件，理由欄位只需交代這4項即可】\n"
                "\n"
                "格式（嚴格遵守，禁止增減欄位）：\n"
                "\n"
                f"SIGNAL: {signal_options_text}\n"
                "建議: （≤20字）\n"
                "方向: 做多或放空或觀望\n"
                "理由:\n"
                "- 時段及環境過濾結果\n"
                "- ORB狀態（是否站上/跌破開盤區間）\n"
                "- VWAP方向與乖離程度\n"
                "- RVOL是否≥1.5，是否為真實參與\n"
                "- 是否為今日活躍股（量能/振幅是否明顯放大）\n"
                "- 扣除交易成本後，目標空間是否足夠\n"
                f"價位: {support_hint}{pressure_hint}\n"
                f"進場: 多{entry_lo_ex}～{entry_hi_ex} 空{short_entry_lo}～{short_entry_hi}\n"
                f"停損: 多{stop_loss_ex} 空{short_stop_ex} ATR={atr}\n"
                f"目標: 多{target_ex}(≤{limit_up or 'N/A'}) 空{short_target_ex}(≥{limit_down or 'N/A'})\n"
                "風險: （≤15字）\n"
            )
        else:
            final_output_block = (
                "【強勢股 vs 弱勢股 行為差異判讀】\n"
                "★ 強勢股識別：開盤即攻、量持續放大、回測均線不破、OBV持續上升\n"
                "  → 多單買盤竭盡時「先不停損，等反彈無力再出場」\n"
                "  → 進場分批：順勢追進用3:2:1（先重後輕）\n"
                "\n"
                "★ 弱勢股識別：開盤即跌、量縮反彈、VWAP壓回、OBV持續下降\n"
                "  → 空單有機會「逢反彈加碼，抱緊空單」\n"
                "  → 逆勢試多用1:2:3（先輕後重，確認止跌再加）\n"
                "\n"
                "【優先觀察的進場型態清單】\n"
                "➊ VWAP錨定回測進場：突破→回測VWAP縮量→確認K收紅/黑→放量再突破（風報≥3:1）\n"
                "➋ 洗盤帶量突破：縮量整理→帶量突破前高/均線（均線仍多頭排列）→追進\n"
                "➌ 低點SC反彈進場：Selling Climax第二階段→縮量收紅+錘頭+站回VWAP→試多\n"
                "➍ MTF三框對齊ORB：日K+15分K+5分K同向，開盤區間突破確認→最早進場\n"
                "➎ RSI雙框45-60黃金帶共振：1分K+5分K RSI同在45-60，動能蓄勢初期→最佳時機\n"
                "\n"
                "評估順序：環境過濾→MTF共識→時段→漲跌停→爆量耗竭+SC→洗盤識別→MA交叉→ORB→VWAP→五檔→量能→RSI背離→OBV→日K位置→強弱勢判定→綜合得分→訊號\n"
                "⚠️ 重要：思考過程禁止使用 'SIGNAL:' 關鍵字，只在最終格式輸出區寫一次 SIGNAL\n"
                "\n"
                "格式（嚴格遵守，禁止增減欄位）：\n"
                "\n"
                f"SIGNAL: {signal_options_text}\n"
                "建議: （≤20字）\n"
                "方向: 做多或放空或觀望\n"
                "理由:\n"
                f"- 時段（{left}分鐘）及環境過濾結果\n"
                f"- MTF共識（日K:{daily_trend_mtf} 15分K:{trend_15m} 5分K:{trend_5m} 1分K:{trend_1m}）\n"
                "- 洗盤識別或型態判斷（是否為證據充分的型態）\n"
                "- MA交叉訊號與評分\n"
                "- ORB突破狀態與量能品質（真/假突破判斷）\n"
                "- VWAP錨定回測機會評估\n"
                "- RSI雙框一致性與背離檢查\n"
                "- OBV資金流向確認\n"
                "- 五檔掛單壓力與大單動向\n"
                "- 日K位置（現價在近10日高低的百分位）\n"
                "- 強勢股/弱勢股判定及建議分批比例\n"
                "- MTF分+技術分 = 綜合得分（X+Y分）→ 對應訊號等級\n"
                f"價位: {support_hint}{pressure_hint}\n"
                f"進場: 多{entry_lo_ex}～{entry_hi_ex} 空{short_entry_lo}～{short_entry_hi}\n"
                f"停損: 多{stop_loss_ex} 空{short_stop_ex} ATR={atr}\n"
                f"目標: 多{target_ex}(≤{limit_up or 'N/A'}) 空{short_target_ex}(≥{limit_down or 'N/A'})\n"
                "分批: 順勢(3:2:1) 逆勢(1:2:3) 建議本次用___批\n"
                "風險: （≤15字）\n"
            )

        # ── ORB 資訊區塊（v6.1 核心新增） ────────────────────────
        if orb_status["has_or"]:
            # 格式化所有可用的 OR 區間
            or_lines = []
            if opening_range:
                for key, label in [("or5","5分"), ("or15","15分"), ("or20","20分"), ("or30","30分")]:
                    if key in opening_range:
                        o = opening_range[key]
                        or_lines.append(f"  OR{label}高:{o['high']} 低:{o['low']}")

            orb_block = (
                f"\n【📐 Opening Range Breakout（ORB）狀態 — v6.1 關鍵訊號】\n"
                f"開盤區間（越早確立越可靠）：\n"
                + "\n".join(or_lines) + "\n"
                f"\n當前 ORB 訊號：{orb_status['orb_signal']}\n"
                f"RVOL（相對成交量）：{rvol_val or '無'}x"
                f"（≥1.5=放量確認；<1.5=縮量，突破可信度低）\n"
                f"\n【ORB 進場優先順序（證據強度由高到低）】\n"
                f"① STRONG：ORB 突破 + VWAP 同向 + RVOL≥1.5（三重確認，證據最充分）\n"
                f"② BUY/SHORT：VWAP 回測後不破 + OBV 確認 + RSI 動能（回測進場，停損更緊）\n"
                f"③ BUY/SHORT：突破 OR 高/低 + RVOL≥1.5（雙重確認）\n"
                f"④ WATCH：ORB 突破但 RVOL<1.5（縮量假突破風險高）\n"
                f"⑤ WATCH：尚在 OR 區間盤整（等待方向確立）\n"
            )
        else:
            orb_block = "\n【📐 ORB】開盤區間資料不足（可能是盤前或資料缺失），暫不使用 ORB 策略。\n"

        # ─────────────────────────────────────────
        #  精簡模式（自動觸發，token 更少）
        # ─────────────────────────────────────────
        if concise:
            daily_summary = "無"
            _pos_pct_c = None
            if daily_candles and len(daily_candles) >= 1:
                # 【修正】與完整版否決規則的判斷基準保持一致，統一用近10日
                # （原本此處用近5日，會讓精簡路徑與完整路徑對同一支股票
                # 算出不同的「近期位置百分比」，判斷標準不一致）
                d5 = daily_candles[-10:] if len(daily_candles) >= 10 else daily_candles
                closes_d = [d["close"] for d in d5]
                ma5_d = sum(closes_d[-5:]) / min(5, len(closes_d))
                _recent_high_c = max(d["high"] for d in d5)
                _recent_low_c  = min(d["low"] for d in d5)
                if _recent_high_c > _recent_low_c:
                    _pos_pct_c = round((current_price - _recent_low_c) / (_recent_high_c - _recent_low_c) * 100)
                daily_summary = (
                    f"近5日收:{' '.join(str(d['close']) for d in d5[-5:])} "
                    f"均={ma5_d:.1f} "
                    f"近10日高={_recent_high_c} 低={_recent_low_c}"
                )
                if _pos_pct_c is not None:
                    daily_summary += f" | 現價在近10日高低間{_pos_pct_c}%位置"
                    if _pos_pct_c <= 20:
                        daily_summary += "(⚠️接近近期低點，依硬性否決規則禁止SHORT)"
                    elif _pos_pct_c >= 80:
                        daily_summary += "(⚠️接近近期高點，依硬性否決規則禁止BUY)"
            concise_candles = ""
            for c in (candles[-10:] if len(candles) >= 10 else candles):
                concise_candles += (
                    f"{c.get('time','')} C:{c['close']} V:{c['volume']}\n"
                )
            stage_warn_short = stage_warning.split("\n")[0]
            limit_short = ""
            if limit_up and limit_down:
                limit_short = f"漲停:{limit_up} 跌停:{limit_down}"
            force_dir_short = ""
            if force_direction == "buy":
                force_dir_short = "【強制做多：SIGNAL 輸出 BUY 或 STRONG_BUY】"
            elif force_direction == "short":
                force_dir_short = "【強制放空：SIGNAL 輸出 SHORT 或 STRONG_SHORT】"

            # ORB 精簡版
            orb_short = ""
            if orb_status["has_or"]:
                orb_short = f"ORB:{orb_status['orb_signal'][:40]} RVOL:{rvol_val or '?'}x"

            # MA 交叉精簡版（v7.0）
            ma_cross_short = f"MA交叉:{ma_cross_info['label'].splitlines()[0]}"

            # Selling Climax 精簡版（v7.0）
            sc_short = ""
            if volume_climax_info and volume_climax_info["type"] == "selling_climax":
                sc_stage = volume_climax_info.get("sc_stage", "stage0_watch")
                sc_cnt   = volume_climax_info.get("sc_confirm_count", 0)
                if sc_stage == "stage2_buy":
                    sc_short = f"📣低點爆量SC({sc_cnt}/4確認)→可試多BUY"
                elif sc_stage == "stage1_watch":
                    sc_short = f"📣低點爆量SC({sc_cnt}/4確認)→等待更多確認"
                else:
                    sc_short = f"📣低點爆量SC(0確認)→強制WATCH"
            elif volume_climax_info and volume_climax_info["type"] == "buying_climax":
                sc_short = f"⚠️高點爆量BC→禁STRONG_BUY"

            # v8.0 MTF精簡版（新增）
            def _trend_score_c(t: str) -> int:
                return 1 if t == "上升" else (-1 if t == "下降" else 0)
            def _calc_mtf_trend_c(closes: list, period: int = 3) -> str:
                if len(closes) < period + 1: return "?"
                mid = len(closes) // 2
                a = sum(closes[:mid]) / mid
                b = sum(closes[mid:]) / max(len(closes) - mid, 1)
                d = (b - a) / a * 100 if a else 0
                return "↑" if d > 0.5 else ("↓" if d < -0.5 else "→")
            _dc = [d["close"] for d in (daily_candles[-5:] if daily_candles and len(daily_candles)>=5 else (daily_candles or []))]
            _15c = [c["close"] for c in (_candles_15m[-8:] if _candles_15m else [])]
            _5c  = [c["close"] for c in (_candles_5m[-6:]  if _candles_5m  else [])]
            _1c  = [c["close"] for c in (candles[-5:]       if candles      else [])]
            mtf_line = f"MTF: 日{_calc_mtf_trend_c(_dc)} 15分{_calc_mtf_trend_c(_15c)} 5分{_calc_mtf_trend_c(_5c)} 1分{_calc_mtf_trend_c(_1c)}"

            # ── 【v13 修正】精簡版門檻與判斷邏輯，依 risk_mode 動態切換 ──
            # 原本此路徑寫死「需MTF≥1+技術≥3才進場」，完全忽略 risk_mode
            # 參數。這代表自動排程分析（_start_auto_ai 永遠用 concise=True）
            # 從未套用過 relaxed/simplified 等模式的門檻邏輯，只有使用者
            # 手動點擊分析、且設定頁「精簡模式」關閉時才會用到完整版邏輯。
            # 這裡讓精簡版也能反映 risk_mode 的選擇，確保自動分析與
            # 手動分析的判斷邏輯一致，不會因為觸發方式不同而表現不同。
            if risk_mode == "simplified":
                score_rule_line = (
                    "【精簡核心版】僅用4條件：①ORB突破②VWAP方向③RVOL≥1.5④今日是否活躍股\n"
                    "全部同向且無爆量確認→BUY/SHORT；全部同向+爆量確認→STRONG；任一反向或依據不足→WATCH\n"
                    "務必考慮交易成本（來回約0.15~0.3%），目標空間扣除成本後不足時優先WATCH\n"
                    "🚫硬性否決(優先於①②③④全部同向的條件)：\n"
                    "現價在近10日高低區間後20%(近期低點)→禁止SHORT/STRONG_SHORT；\n"
                    "現價在前20%(近期高點)→禁止BUY/STRONG_BUY；即使①②③④都同向也一律WATCH"
                )
            elif risk_mode == "relaxed":
                score_rule_line = (
                    "【v8評分-寬鬆版】MTF共識(最高4分)+技術評分(共10項)=綜合分\n"
                    "STRONG需MTF≥4+技術≥6；BUY/SHORT需MTF≥2「或」技術≥3（擇一即可，不需同時滿足）\n"
                    "MTF規則:3框以上同向=+4分;2框=+2分;1框=0分;日K與操作方向相反=-2分(禁STRONG)\n"
                    "🚫硬性否決(優先於上述門檻，不可被日K位置的-1分軟性扣分取代)：\n"
                    "現價在近10日高低區間後20%(近期低點)→禁止SHORT/STRONG_SHORT；\n"
                    "現價在前20%(近期高點)→禁止BUY/STRONG_BUY；無論分數多高一律WATCH"
                )
            else:
                score_rule_line = (
                    "【v8評分】MTF共識(最高4分)+技術評分(共10項)=綜合分\n"
                    "STRONG需MTF≥4+技術≥6；BUY/SHORT需MTF≥2(含日K框)+技術≥4\n"
                    "MTF規則:3框以上同向=+4分;2框=+2分;1框=0分;日K與操作方向相反=-2分(禁STRONG)\n"
                    "🚫硬性否決(優先於上述門檻，不可被日K位置的-1分軟性扣分取代)：\n"
                    "現價在近10日高低區間後20%(近期低點)→禁止SHORT/STRONG_SHORT；\n"
                    "現價在前20%(近期高點)→禁止BUY/STRONG_BUY；無論分數多高一律WATCH"
                )

            return f"""台股當沖分析（精簡v8）{symbol} 現價:{current_price} 時間:{now_time} 距收盤:{left}分
{stage_warn_short}
{limit_short}{force_dir_short}{force_block}
{mtf_line}
{orb_short}
{ma_cross_short}
{sc_short}
指標:{ind_text}
VWAP:{vwap_text} ATR:{atr_text} OBV:{obv_text} 量比:{vol_ratio or "不足"}
振幅:{amplitude or "計算中"} 支撐:{support_text or "無"} 壓力:{pressure_text or "無"}
日K:{daily_summary}
近10根分K:
{concise_candles}掛單:{ob_pressure_hint}
前次:{prev_text}
前次比對:若前次說WATCH等待條件，本次須說明條件是否已達到，量能是否與前次預期一致
{score_rule_line}
技術篩選:① ORB+量 ② VWAP同向 ③ VWAP回測確認 ④ RVOL≥1.5非高點爆量 ⑤ RSI雙框同向45-72 ⑥ OBV ⑦ 日K排列 ⑧ 無BC ⑨ 五檔委買>60% ⑩ MA交叉
高點爆量(≥65%+≥2x)→禁STRONG_BUY;SC第二階段→可BUY;日K逆勢→禁STRONG
規則:收盤<15分→WATCH;漲跌停0.5%內→WATCH;MTF衝突→禁STRONG
思考過程禁用SIGNAL關鍵字，僅最終格式區輸出一次SIGNAL
格式(嚴格遵守):
SIGNAL: {signal_options_text}
建議: (≤15字)
方向: 做多或放空或觀望
進場: 多{entry_lo_ex}～{entry_hi_ex} 空{short_entry_lo}～{short_entry_hi}
停損: 多{stop_loss_ex} 空{short_stop_ex}
目標: 多{target_ex} 空{short_target_ex}
風險: (≤10字)"""

        # ─────────────────────────────────────────
        #  完整模式（手動觸發）
        # ─────────────────────────────────────────
        # ── 爆量警告區塊 ──────────────────────────────────────
        climax_block = ""
        if volume_climax_info:
            climax_block = f"\n{volume_climax_info['warning_text']}\n"

            if volume_climax_info["type"] == "buying_climax":
                # 高點爆量做多禁止旗標（force_direction 例外）
                if volume_climax_info["price_pos_pct"] >= 65 and not force_direction:
                    climax_block += (
                        "\n🚫【高點爆量強制降評】：SIGNAL 不得輸出 STRONG_BUY；"
                        "BUY 需額外說明為何非 Buying Climax 反轉訊號。\n"
                    )

            elif volume_climax_info["type"] == "selling_climax":
                # v7.0：依 sc_stage 控制 SIGNAL 上限
                sc_stage = volume_climax_info.get("sc_stage", "stage0_watch")
                sc_cnt   = volume_climax_info.get("sc_confirm_count", 0)
                if sc_stage == "stage0_watch" and not force_direction:
                    climax_block += (
                        f"\n🚫【低點爆量第零階段 — 強制觀望】\n"
                        f"  Selling Climax 發生，目前確認訊號 {sc_cnt}/4，未達門檻。\n"
                        f"  SIGNAL 強制輸出 WATCH，禁止輸出 BUY 或 STRONG_BUY。\n"
                        f"  請在「建議」欄提示使用者等待確認K棒（縮量收紅/錘頭）。\n"
                    )
                elif sc_stage == "stage1_watch" and not force_direction:
                    climax_block += (
                        f"\n⚠️【低點爆量第一階段 — 謹慎試多】\n"
                        f"  Selling Climax 確認訊號 {sc_cnt}/4，可考慮小量試多（BUY）。\n"
                        f"  SIGNAL 最高輸出 BUY（禁止 STRONG_BUY），建議分批進場（1/3 倉）。\n"
                        f"  停損必須設在爆量K棒低點下方 1 Tick，嚴格執行。\n"
                    )
                elif sc_stage == "stage2_buy":
                    climax_block += (
                        f"\n✅【低點爆量第二階段 — 可積極做多】\n"
                        f"  Selling Climax 確認訊號 {sc_cnt}/4，反彈訊號明確。\n"
                        f"  SIGNAL 可輸出 BUY 或 STRONG_BUY（視其他指標綜合評分）。\n"
                        f"  建議分批加碼（第二批 1/3 倉），停損仍設爆量K棒低點 -1 Tick。\n"
                    )

        # MTF 區塊輸出
        mtf_block = (
            f"\n【🔭 MTF 多時間框架模態共識分析】\n"
            f"  分析原則：由大到小確認方向，多框共識時證據較充分，單框或衝突時證據不足\n"
            f"\n"
            f"  📅 日K趨勢（定方向，權重×3）：{daily_trend_mtf}\n"
            f"  ⏱ 15分K趨勢（定結構，權重×2）：{trend_15m}\n"
            f"  ⏱  5分K趨勢（定動能，權重×2）：{trend_5m}\n"
            f"  ⚡  1分K趨勢（定執行，權重×1）：{trend_1m}\n"
            f"\n"
            f"  {mtf_emoji}【MTF共識結論】：{mtf_consensus}（加權分：{mtf_score_raw:+d}/{mtf_max}）\n"
            f"  操作偏向：{mtf_bias}\n"
            f"\n"
            f"{mtf_conflict_warn}"
            f"  【MTF進場門檻】\n"
            f"  ✅ STRONG_BUY/SHORT：3框以上共識（日K+15分K+5分K同向）+ ORB/VWAP確認\n"
            f"  ⚡ BUY/SHORT：2框共識（含日K或15分K）+ 其他技術確認\n"
            f"  ⚪ WATCH：1框或0框共識，或存在MTF衝突（逆勢風險過高）\n"
            f"\n"
            f"  【台股當沖常見高共識組合（僅供參考，非保證）】\n"
            f"  ➊ 日K多頭排列（MA5>MA10>MA20）→ 15分K回測均線不破 → 5分K黃金交叉 → 1分K進場\n"
            f"  ➋ 15分K突破盤整高點 + RVOL≥1.5 → 5分K確認站穩 → 1分K追進（ORB搭配MTF）\n"
            f"  ➌ VWAP多空轉換：1分K站回VWAP + 5分K轉多 + 15分K不破低\n"
        )

        # ── v7.0：MA 交叉區塊 ──────────────────────────────────
        ma_cross_block = (
            f"\n【📈 MA 黃金/死亡交叉評分（v7.0）】\n"
            f"{ma_cross_label}\n"
            f"評分影響：強黃金交叉=+2分(做多)；弱黃金交叉=+1分；"
            f"強死亡交叉=-2分(做多方向)=+2分(放空方向)；弱死亡交叉=-1分\n"
            f"當前MA交叉評分（相對做多方向）：{ma_cross_score:+d}分\n"
        )

        # ── 新增：圖形型態區塊 ─────────────────────────────────
        chart_pattern_block = (
            f"\n【🔷 K線圖形型態識別（楔形/旗形/三角形/頭肩）】\n"
            f"{chart_pattern_text}\n"
            f"突破方向提示：{chart_pattern_info['breakout_hint']}\n"
            f"型態評分（相對做多方向）：{chart_pattern_score:+d}分\n"
            f"注意：型態需搭配「量能確認」才有效，無量突破為假突破！\n"
        )

        return f"""你是一位有10年實戰經驗的台股當沖操盤手，同時具備嚴謹量化分析能力。
分析 {symbol}（現價:{current_price}），繁體中文回覆，禁止輸出任何思考過程或推理步驟。
分析時間：{now_time} 時段：{stage_label} 距收盤：{left}分鐘

【操盤手守則（風險控管核心原則）】
① 「不做就是賺」— 不明確時寧可WATCH，保住本金優先
② 「順勢為主，逆勢嚴格過濾」— 日K方向決定生死，逆大趨勢應提高進場門檻
③ 「強勢股多單抱緊，弱勢股空單抱緊」— 強勢股買盤竭盡先不停損等反彈再評估；弱勢股空單有順勢則持倉
④ 「量是先行指標」— 量縮盤整不追、量放方向性明確才追
⑤ 「分批進場控制風險」— 順勢單分3批（3:2:1比例），逆勢反彈單分3批（1:2:3比例）

{risk_block}{force_dir_block}{candle_warn}
【時段風險】{stage_warning}
{limit_block}
{climax_block}
{mtf_block}
{ma_cross_block}
{chart_pattern_block}
{orb_block}

【近24日日K】
{daily_text}

【當日1分K（完整當日，最多60根）】
{candle_lines}
【5分K（最近12根，近1小時趨勢）{_5m_ind} 趨勢:{trend_5m}】
{candles_5m_lines}
【15分K（最近8根，近2小時趨勢）{_15m_ind} 趨勢:{trend_15m}】
{candles_15m_lines}
【指標】{ind_text}
VWAP:{vwap_text}  ATR:{atr_text}
OBV:{obv_text}  量比:{vol_ratio or "資料不足"}  RVOL:{rvol_val or "無"}x
VWAP通道:{vwap_band_text}
現價位置:{vwap_band_pos}
開盤:{open_gap or "無昨收"}  振幅:{amplitude or "計算中"}
K棒型態:{candle_pattern or "資料不足"}  動能:{momentum_text or "資料不足"}

【最佳五檔委買委賣（即時掛單）】
{orderbook_text}
掛單壓力:{ob_pressure_hint}
五檔當沖判讀規則：
- 委買量 > 總量 60% → 短期買盤支撐強，做多有利（可用買一附近進場，停損設買二下方）
- 委賣量 > 總量 60% → 短期賣壓重，放空有利（注意突破前假突破陷阱）
- 賣一掛單突然大幅縮減（撤單）→ 可能是向上突破前兆，搭配 ORB 研判
- 買一掛單突然出現大量（鯨魚買單）→ 強支撐訊號，做多方向證據提升
- 五檔資料為「無」→ 改以分價量支撐/壓力替代五檔判斷，不計入五檔得分

【分價量（顯著集中區）】
{vol_text}
支撐:{support_text or "無"}　壓力:{pressure_text or "無"}

【今日AI記錄（含前次比對分析）】
{prev_text}
{prev_context_block}

【Tick】{current_price}元用{tick_unit_str}｜<10→0.01｜10~50→0.05｜50~100→0.1｜100~500→0.5｜500~1000→1｜>1000→5
{force_block}
---
【v9.0 評分框架（精準版）— 嚴格門檻，寧缺勿濫】

═══════════════════════════════════════════════════════
▌ STEP 1：市場環境過濾（最高優先，任一條件觸發→強制WATCH）
═══════════════════════════════════════════════════════
以下任一條件成立，無論其他指標多強，一律輸出WATCH：
✗ 距收盤 ≤ 15 分鐘（收盤前強制平倉風險）
✗ 現價在漲跌停 0.5% 以內（流動性風險）
✗ 量比 < 0.5（市場無人交易，假突破率極高）
✗ 振幅 < 0.8%（今日波動太小，無法覆蓋手續費+稅）
✗ 1分K最近5根均為十字線或實體極小（多空完全均衡，無方向）

{step234_block}
{final_output_block}
---"""

    # ──────────────────────────────────────────
    #  對外主方法
    # ──────────────────────────────────────────
    def analyze_with_signal(
        self,
        symbol:          str,
        candles:         List[Dict],
        indicators:      Dict         = None,
        volumes_data:    List[Dict]   = None,
        prev_entries:    List[Dict]   = None,
        daily_candles:   List[Dict]   = None,
        prev_close:      float        = None,
        analysis_time:   dtime        = None,
        risk_mode:       str          = "auto",
        force_direction: str          = None,
        candle_minutes:  int          = 1,
        orderbook:       Dict         = None,
        concise:         bool         = False,
    ) -> Dict:
        """呼叫 Gemini API，解析結構化結果，回傳 result dict。"""

        # ════════ [DEBUG] analyze_with_signal 入口診斷 ════════
        print(f"[DEBUG][entry] symbol={symbol}  candles根數={len(candles) if candles else 0}")
        print(f"[DEBUG][entry] 最後candle={candles[-1] if candles else 'EMPTY'}")
        _current = candles[-1]['close'] if candles else 0
        print(f"[DEBUG][entry] current_price={_current}  prev_close={prev_close}")
        print(f"[DEBUG][entry] indicators存在={'是' if indicators else '否'}  keys={list(indicators.keys()) if indicators else '[]'}")
        print(f"[DEBUG][entry] indicators['or']={indicators.get('or') if indicators else 'N/A'}")
        print(f"[DEBUG][entry] indicators['rvol']={indicators.get('rvol') if indicators else 'N/A'}")
        print(f"[DEBUG][entry] concise={concise}  risk_mode={risk_mode}  force_direction={force_direction}")
        # ════════ [DEBUG END] ════════

        current_price = candles[-1]["close"] if candles else 0
        stage_info    = _get_trade_stage(analysis_time)

        limits     = _calc_limit_prices(prev_close) if prev_close else None
        limit_up   = limits[0] if limits else None
        limit_down = limits[1] if limits else None
        at_limit   = _check_at_limit(current_price, limit_up, limit_down) if limits else ""

        result = {
            "signal":           "watch",
            "suggestion":       "⚪ 觀望",
            "direction":        "觀望",
            "raw_signal":       "WATCH",
            "force_overridden": False,
            "entry":            "--",
            "stop_loss":        "--",
            "target":           "--",
            "full_text":        "",
            "stage":            stage_info["label"],
            "minutes_to_close": stage_info["minutes_to_close"],
            "vwap":             _calc_vwap(candles),
            "limit_up":         limit_up,
            "limit_down":       limit_down,
            "at_limit":         at_limit,
            "force_direction":  force_direction or "",
        }

        if stage_info["stage"] == "close_15":
            result["suggestion"] = "🚫 收盤前15分，禁止當沖新倉"
            result["full_text"]  = (
                f"⛔ 【強制觀望】現在 {stage_info['label']}，"
                f"距收盤僅剩 {stage_info['minutes_to_close']} 分鐘。\n"
                "當沖規定：所有部位必須在 13:30 前沖銷，否則需付全額交割。\n"
                "此時段嚴禁開立新倉，請專注管理現有部位的出場時機。"
            )
            return result

        if at_limit == "up":
            result["signal"]     = "watch"
            result["suggestion"] = f"🔴 已達漲停（{limit_up}），觀望勿追"
            result["full_text"]  = (
                f"🔴【漲停板】現價 {current_price} 已達漲停價 {limit_up}。\n"
                "台股漲停板限制：買方委單大量堆積，賣方稀少，極難成交。\n"
                "當沖風險：若以漲停掛進，可能無法成交或在高點套牢，\n"
                "且漲停瞬間打開常造成急速回落，回補風險極高。\n"
                "建議：強制觀望，不追高，等待打開後再評估方向。"
            )
            return result

        if at_limit == "down":
            result["signal"]     = "watch"
            result["suggestion"] = f"🔵 已達跌停（{limit_down}），觀望勿追"
            result["full_text"]  = (
                f"🔵【跌停板】現價 {current_price} 已達跌停價 {limit_down}。\n"
                "台股跌停板限制：賣方委單大量堆積，買方稀少，融券回補困難。\n"
                "當沖風險：若放空並等待回補，跌停鎖死時無法回補，\n"
                "將被迫以當日收盤後融券交割，造成額外成本。\n"
                "建議：強制觀望，不追殺，等待打開後再評估方向。"
            )
            return result

        prompt    = self._build_prompt(
            symbol          = symbol,
            candles         = candles,
            indicators      = indicators    or {},
            volumes_data    = volumes_data  or [],
            prev_entries    = prev_entries  or [],
            daily_candles   = daily_candles or [],
            prev_close      = prev_close,
            analysis_time   = analysis_time,
            risk_mode       = risk_mode,
            limit_up        = limit_up,
            limit_down      = limit_down,
            force_direction = force_direction,
            candle_minutes  = candle_minutes,
            concise         = concise,
            orderbook       = orderbook,
        )
        # Gemma 系列模型（gemma-4-31b-it / gemma-4-26b-a4b-it）常把大量 token 用在內部推理過程，
        # 若上限設太低，會在還沒吐出 SIGNAL: 正文前就被截斷 (finishReason=MAX_TOKENS, text長度=0)，
        # 導致該次分析直接視為失敗、股票只能標記為 watch。故提高上限留足緩衝空間。
        max_toks  = 2500 if concise else 3000 #AI輸出文字
        full_text = self._call(prompt, max_tokens=max_toks) or "AI 分析失敗"
        result["full_text"] = full_text

        # ════════ [DEBUG] Gemini 回傳內容診斷 ════════
        print(f"[DEBUG][gemini_raw] full_text 前300字：\n{full_text[:300]}")
        print(f"[DEBUG][gemini_raw] 是否含 'SIGNAL:'={'SIGNAL:' in full_text or 'SIGNAL：' in full_text}")
        import re as _re
        _matches = _re.findall(r"SIGNAL\s*[：:]\s*(\S+)", full_text, _re.IGNORECASE)
        print(f"[DEBUG][gemini_raw] 找到的所有 SIGNAL 值={_matches}  (取最後一個解析)")
        # ════════ [DEBUG END] ════════

        if full_text.startswith("❌") or full_text.startswith("⚠️"):
            return result

        text = full_text.replace("**", "").replace("*", "").strip()

        # ── SIGNAL 解析（取最後一個，避免思考過程干擾）────────────
        matches = re.findall(r"SIGNAL\s*[：:]\s*(\S+)", text, re.IGNORECASE)
        signal_parsed = False
        if matches:
            sig = matches[-1].strip().upper().rstrip(".")
            sig = re.split(r"[\s（(]", sig)[0]
            _SIGNAL_MAP = {
                "STRONG_BUY":   ("buy",   "🟢 強烈做多", "做多"),
                "BUY":          ("buy",   "🟡 建議做多", "做多"),
                "WATCH":        ("watch", "⚪ 觀望",     "觀望"),
                "SHORT":        ("short", "🔵 建議放空", "放空"),
                "STRONG_SHORT": ("short", "🔵 強烈放空", "放空"),
                "SELL":         ("watch", "⚪ 觀望/不建議", "觀望"),
                "STRONG_SELL":  ("short", "🔵 強烈放空",   "放空"),
            }
            sv, ss, sd = _SIGNAL_MAP.get(sig, (None, None, None))
            if sv is not None:
                result["signal"]     = sv
                result["suggestion"] = ss
                result["direction"]  = sd
                # 【v10 新增】保留原始訊號強度字串（STRONG_BUY/BUY/SHORT/STRONG_SHORT），
                # 供後續勝率追蹤依「強烈」與「一般」訊號分開統計成效，
                # 而不是只看正規化後的 buy/short 兩種粗分類。
                result["raw_signal"] = sig if sig in (
                    "STRONG_BUY", "BUY", "SHORT", "STRONG_SHORT"
                ) else ("STRONG_SHORT" if sig == "STRONG_SELL" else sv.upper())
                signal_parsed = True
                print(f"[解析 SIGNAL] 找到 {len(matches)} 個 SIGNAL，取最後一個：'{sig}' → signal={sv} dir={sd}")
            else:
                print(f"[解析 SIGNAL] SIGNAL '{sig}' 不在已知列表中，降級為關鍵字匹配")

        if not signal_parsed:
            kw_map = [
                (["強烈做多", "強力做多"],                    "buy",   "🟢 強烈做多", "做多"),
                (["建議做多", "做多", "可以買進", "建議買進"], "buy",   "🟡 建議做多", "做多"),
                (["強烈放空", "強力放空"],                     "short", "🔵 強烈放空", "放空"),
                (["建議放空", "放空", "融券"],                 "short", "🔵 建議放空", "放空"),
            ]
            for kws, sv, ss, sd in kw_map:
                if any(k in text for k in kws):
                    result["signal"]     = sv
                    result["suggestion"] = ss
                    result["direction"]  = sd
                    signal_parsed = True
                    print(f"[解析 SIGNAL] 關鍵字匹配成功：'{kws}' → signal={sv} dir={sd}")
                    break

        if stage_info["force_watch"]:
            result["signal"]     = "watch"
            result["suggestion"] = "🚫 收盤前15分強制觀望"
            result["direction"]  = "觀望"

        m_dir = re.search(r"方向\s*[：:]\s*(\S+)", text)
        if m_dir:
            dv = m_dir.group(1).strip()
            parsed_dir = "放空" if "放空" in dv else ("做多" if "做多" in dv else "觀望")
            if not signal_parsed or result["direction"] == "觀望":
                result["direction"] = parsed_dir
                print(f"[解析方向] 方向行解析：'{dv}' → direction={parsed_dir}")
            else:
                if parsed_dir != result["direction"]:
                    print(f"[解析方向] ⚠️ 方向不一致：SIGNAL→{result['direction']}，方向行→{parsed_dir}，以 SIGNAL 結果為準")

        # ── 價位解析 ─────────────────────────────────────────────
        _NUM   = r"[0-9０-９]+(?:[.．][0-9０-９]*)?"
        _RANGE = rf"({_NUM})(?:\s*[~～\-至到]\s*({_NUM}))?"

        def _extract_first_price(line_after_colon: str) -> str:
            s = line_after_colon.strip()
            s = re.sub(r"^(?:做多|做空|放空|多|空)\s*", "", s)
            s = re.sub(r"^\([^)]*\)\s*", "", s)
            s = re.sub(r"^（[^）]*）\s*", "", s)
            m = re.search(rf"({_NUM})", s)
            return m.group(1) if m else ""

        def _extract_price_range(line_after_colon: str) -> str:
            s = line_after_colon.strip()
            s = re.sub(r"^(?:做多|做空|放空|多|空)\s*", "", s)
            s = re.sub(r"^\([^)]*\)\s*", "", s)
            s = re.sub(r"^（[^）]*）\s*", "", s)
            m = re.match(rf"\s*{_RANGE}", s)
            if m:
                lo = m.group(1)
                hi = m.group(2)
                return f"{lo}～{hi}" if hi else lo
            return ""

        # 同時暫存多空兩組價位，最後依方向選擇
        _parsed_long  = {"entry": "", "stop_loss": "", "target": ""}
        _parsed_short = {"entry": "", "stop_loss": "", "target": ""}

        def _extract_directional(raw: str) -> tuple[str, str]:
            """
            從「多XXX 空YYY」格式中分別抓出多方和空方的值。
            回傳 (long_val, short_val)，若格式不含多/空關鍵字則兩者相同。
            """
            # 嘗試分割「多...空...」格式
            m_long  = re.search(r"多\s*([0-9０-９.．～~\-－]+)", raw)
            m_short = re.search(r"空\s*([0-9０-９.．～~\-－]+)", raw)
            if m_long and m_short:
                return m_long.group(1).strip(), m_short.group(1).strip()
            # 若只有單一數字（沒有多/空標記），兩邊共用
            single = _extract_first_price(raw)
            return single, single

        def _extract_directional_range(raw: str) -> tuple[str, str]:
            """從進場行抓多空兩組區間。"""
            m_long  = re.search(r"多\s*([0-9０-９.．]+(?:\s*[～~]\s*[0-9０-９.．]+)?)", raw)
            m_short = re.search(r"空\s*([0-9０-９.．]+(?:\s*[～~]\s*[0-9０-９.．]+)?)", raw)
            if m_long and m_short:
                return m_long.group(1).strip(), m_short.group(1).strip()
            single = _extract_price_range(raw)
            return single, single

        for line in text.split("\n"):
            ls = line.strip().lstrip("-•▪▸").strip()
            m_entry = re.search(r"進場[價位區間]*\s*[：:](.+)", ls)
            if m_entry:
                lv, sv = _extract_directional_range(m_entry.group(1))
                if lv: _parsed_long["entry"]  = lv
                if sv: _parsed_short["entry"] = sv
                print(f"[解析進場] 原始行：'{ls}' → 多:{lv} 空:{sv}")
                continue
            m_stop = re.search(r"停損\s*[：:](.+)", ls)
            if m_stop:
                lv, sv = _extract_directional(m_stop.group(1))
                if lv: _parsed_long["stop_loss"]  = lv
                if sv: _parsed_short["stop_loss"] = sv
                print(f"[解析停損] 原始行：'{ls}' → 多:{lv} 空:{sv}")
                continue
            m_target = re.search(r"目標[價位]*\s*[：:](.+)", ls)
            if m_target:
                lv, sv = _extract_directional(m_target.group(1))
                if lv: _parsed_long["target"]  = lv
                if sv: _parsed_short["target"] = sv
                print(f"[解析目標] 原始行：'{ls}' → 多:{lv} 空:{sv}")
                continue

        # 依最終 signal 方向選擇對應的價位組
        result["_parsed_long"]  = _parsed_long
        result["_parsed_short"] = _parsed_short

        # ── force_direction 覆蓋（在 watch 清空價位前攔截）────────
        def _apply_prices_long(parsed: dict):
            """套入多方價位，缺的用保底計算。"""
            result["entry"]     = parsed.get("entry", "")     or f"{_snap_tick(current_price * 0.999, 'floor')}～{_snap_tick(current_price * 1.001, 'ceil')}"
            result["stop_loss"] = parsed.get("stop_loss", "") or str(_snap_tick(current_price * 0.993, "floor"))
            result["target"]    = parsed.get("target", "")    or str(_snap_tick(current_price * 1.01, "ceil"))

        def _apply_prices_short(parsed: dict):
            """套入空方價位，缺的用保底計算。"""
            result["entry"]     = parsed.get("entry", "")     or f"{_snap_tick(current_price * 0.999, 'floor')}～{_snap_tick(current_price * 1.001, 'ceil')}"
            result["stop_loss"] = parsed.get("stop_loss", "") or str(_snap_tick(current_price * 1.007, "ceil"))
            result["target"]    = parsed.get("target", "")    or str(_snap_tick(current_price * 0.99, "floor"))

        if force_direction == "buy" and result["signal"] == "watch":
            print(f"[強制做多] AI 回 WATCH，強制覆蓋為 BUY")
            result["signal"]     = "buy"
            result["suggestion"] = "🟡 強制做多（使用者指定）"
            result["direction"]  = "做多"
            # 【v13修正】強制方向覆蓋不是 AI 的自主判斷，是使用者已決定要交易、
            # AI 只是配合找進出場點。這種訊號若混入命中率統計，會稀釋掉真正
            # 代表 AI 判斷能力的數據，因此 raw_signal 保留 WATCH，不納入追蹤，
            # 但另外標記 force_overridden 供 UI 顯示「此為強制方向建議」。
            result["force_overridden"] = True
            _apply_prices_long(result["_parsed_long"])

        elif force_direction == "short" and result["signal"] == "watch":
            print(f"[強制放空] AI 回 WATCH，強制覆蓋為 SHORT")
            result["signal"]     = "short"
            result["suggestion"] = "🔵 強制放空（使用者指定）"
            result["direction"]  = "放空"
            result["force_overridden"] = True
            _apply_prices_short(result["_parsed_short"])

        elif result["signal"] == "watch":
            result["entry"]     = "--"
            result["stop_loss"] = "--"
            result["target"]    = "--"
            result.pop("_parsed_long", None)
            result.pop("_parsed_short", None)
            return result

        # AI 自己判斷 buy/short 時，也要套入正確方向的價位
        elif result["signal"] == "buy":
            _apply_prices_long(result["_parsed_long"])
        elif result["signal"] == "short":
            _apply_prices_short(result["_parsed_short"])

        # 清除暫存欄位
        result.pop("_parsed_long", None)
        result.pop("_parsed_short", None)

        is_short = result["signal"] == "short"

        def _valid(v: str) -> bool:
            return bool(v) and not re.fullmatch(r"[-－–—]+", v.strip()) and v != "--"

        if not _valid(result["entry"]):
            result["entry"] = f"{round(current_price, 1)}"
            print(f"[保底] 進場未解析到，使用現價 {current_price}")
        if not _valid(result["stop_loss"]):
            fallback_sl = f"{round(current_price * 1.007, 1)}" if is_short else f"{round(current_price * 0.993, 1)}"
            result["stop_loss"] = fallback_sl
            print(f"[保底] 停損未解析到，使用預設 {fallback_sl}")
        if not _valid(result["target"]):
            fallback_tg = f"{round(current_price * 0.99, 1)}" if is_short else f"{round(current_price * 1.01, 1)}"
            result["target"] = fallback_tg
            print(f"[保底] 目標未解析到，使用預設 {fallback_tg}")

        print(
            f"[Gemini 解析] signal={result['signal']} dir={result['direction']} "
            f"entry={result['entry']} stop={result['stop_loss']} target={result['target']}"
        )

        # ── Tick 修正 ────────────────────────────────────────────
        def _snap_price_str(val: str, direction: str = "round") -> str:
            try:
                for sep in ("～", "~", "-", "－"):
                    if sep in val:
                        parts = val.split(sep, 1)
                        lo = parts[0].strip()
                        hi = parts[1].strip()
                        return f"{_snap_tick(float(lo), 'floor')}～{_snap_tick(float(hi), 'ceil')}"
                return str(_snap_tick(float(val.strip()), direction))
            except Exception:
                return val

        result["entry"]     = _snap_price_str(result["entry"], "round")
        result["stop_loss"] = _snap_price_str(result["stop_loss"], "ceil"  if is_short else "floor")
        result["target"]    = _snap_price_str(result["target"],    "floor" if is_short else "ceil")

        print(f"[Tick修正後] entry={result['entry']} stop={result['stop_loss']} target={result['target']}")

        # ── 英文字段補抓 ─────────────────────────────────────────
        try:
            inline_text = text
            m_ent = re.search(r"entry\s*[=:]\s*" + _RANGE, inline_text, re.IGNORECASE)
            if m_ent and (not result.get("entry") or result.get("entry") in ("--", "")):
                lo, hi = m_ent.group(1), m_ent.group(2)
                result["entry"] = f"{lo}～{hi}" if hi else lo
                print(f"[補抓進場(entry=)] 擷取到: {result['entry']}")

            m_stop = re.search(r"stop(?:_loss)?\s*[=:]\s*" + _NUM, inline_text, re.IGNORECASE)
            if m_stop and (not result.get("stop_loss") or result.get("stop_loss") in ("--", "")):
                result["stop_loss"] = m_stop.group(1)
                print(f"[補抓停損(stop=)] 擷取到: {result['stop_loss']}")

            m_tgt = re.search(r"target\s*[=:]\s*" + _NUM, inline_text, re.IGNORECASE)
            if m_tgt and (not result.get("target") or result.get("target") in ("--", "")):
                result["target"] = m_tgt.group(1)
                print(f"[補抓目標(target=)] 擷取到: {result['target']}")
        except Exception:
            pass

        # ── 價格邏輯驗證 ─────────────────────────────────────────
        def _to_numeric(vstr: str) -> Optional[float]:
            try:
                if not vstr or vstr in ("--", ""):
                    return None
                if "～" in vstr:
                    a, b = vstr.split("～", 1)
                    return (float(a.strip()) + float(b.strip())) / 2.0
                m = re.search(_NUM, vstr)
                return float(m.group(1)) if m else None
            except Exception:
                return None

        atr_val = _calc_atr(candles) if candles else None
        e_n = _to_numeric(result.get("entry", ""))
        s_n = _to_numeric(result.get("stop_loss", ""))
        t_n = _to_numeric(result.get("target", ""))

        try:
            if is_short:
                if e_n and s_n and s_n <= e_n:
                    delta = atr_val if atr_val else max(e_n * 0.005, 0.1)
                    new_s = _snap_tick(e_n + max(delta, 0.01), "ceil")
                    result["stop_loss"] = str(new_s)
                    print(f"[校正停損] 放空停損 <= 進場，改為 {new_s}")
                    s_n = new_s
                if e_n and t_n and t_n >= e_n:
                    delta = (atr_val * 3) if atr_val else max(e_n * 0.01, 0.1)
                    new_t = _snap_tick(e_n - max(delta, 0.01), "floor")
                    result["target"] = str(new_t)
                    print(f"[校正目標] 放空目標 >= 進場，改為 {new_t}")
            else:
                if e_n and s_n and s_n >= e_n:
                    delta = atr_val if atr_val else max(e_n * 0.005, 0.1)
                    new_s = _snap_tick(e_n - max(delta, 0.01), "floor")
                    result["stop_loss"] = str(new_s)
                    print(f"[校正停損] 做多停損 >= 進場，改為 {new_s}")
                    s_n = new_s
                if e_n and t_n and t_n <= e_n:
                    delta = (atr_val * 3) if atr_val else max(e_n * 0.01, 0.1)
                    new_t = _snap_tick(e_n + max(delta, 0.01), "ceil")
                    result["target"] = str(new_t)
                    print(f"[校正目標] 做多目標 <= 進場，改為 {new_t}")
        except Exception:
            pass

        # ── 漲跌停邊界修正 ───────────────────────────────────────
        if limit_up and limit_down:
            def _clamp_price(price_str: str, lo: float, hi: float) -> str:
                try:
                    for sep in ("～", "~"):
                        if sep in price_str:
                            a, b = price_str.split(sep, 1)
                            a_c  = min(max(float(a.strip()), lo), hi)
                            b_c  = min(max(float(b.strip()), lo), hi)
                            return f"{_snap_tick(a_c)}～{_snap_tick(b_c)}"
                    v = float(price_str.strip())
                    return str(_snap_tick(min(max(v, lo), hi)))
                except Exception:
                    return price_str

            if not is_short:
                result["target"]    = _clamp_price(result["target"],    current_price, limit_up)
                result["stop_loss"] = _clamp_price(result["stop_loss"], limit_down,    current_price)
            else:
                result["target"]    = _clamp_price(result["target"],    limit_down,    current_price)
                result["stop_loss"] = _clamp_price(result["stop_loss"], current_price, limit_up)

            print(
                f"[漲跌停修正] limit_up={limit_up} limit_down={limit_down} "
                f"target={result['target']} stop={result['stop_loss']}"
            )

        return result

    def quick_check(self, symbol: str, price: float, change_pct: float) -> str:
        stage_info = _get_trade_stage()
        hint = ""
        if stage_info["risk_level"] in ("high", "extreme"):
            hint = f"（{stage_info['label']}，距收盤{stage_info['minutes_to_close']}分）"
        prompt = (
            f"{symbol} 現價{price} 漲跌{change_pct:+.2f}%{hint}，"
            f"一句話（15字內）說當沖機會：[訊號] 理由"
        )
        return (self._call(prompt, max_tokens=50) or "無法分析").strip()