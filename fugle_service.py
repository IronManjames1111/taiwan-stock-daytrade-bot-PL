"""
富果行情服務 v4.1
─────────────────────────────────
• FugleService    : REST API 封裝（技術指標、歷史K線等）
• PollingService  : 原有輪詢服務（WS 降級備用）
• parse_candles_for_chart : K線格式轉換（共用工具）

v4 → v4.1 技術指標修正項目：
─────────────────────────────────────────────────────────
【修正 1：ATR 改為標準 Wilder 14 期平滑】
  原版：簡單平均最近 10 根 TR，期數偏短且非 Wilder。
  修正：改為 14 期 Wilder 指數平滑（與 TradingView / Bloomberg 一致）。
  影響：ATR 數值更穩定，AI 停損建議距離更準確。

【修正 2：RSI 最少資料門檻從 15 提高到 30】
  原版：n >= 15 時即計算 RSI，Wilder 平滑只迭代 1 次，初期值偏差大。
  修正：n >= 30，確保 Wilder 平滑有足夠迭代次數收斂。

【修正 3：Opening Range 時間計算邏輯清晰化】
  原版：`9 + minutes // 60` 在 minutes < 60 時正確，但超過 60 會錯。
  修正：改為明確的小時/分鐘格式字串比對，支援任意時間範圍。

【修正 4：Tick 對齊改用 Decimal 消除浮點精度問題】
  原版：`(price // unit) * unit` 對小數會有浮點誤差（如 4.95//0.05=98 而非99）。
  修正：使用 decimal.Decimal 做精確除法，確保漲跌停計算正確。

WebSocket 訂閱由 fugle_ws_service.FugleWSService 負責，
在 app.py 中統一管理並注入到各頁面。

v3 → v4 快取跨日修正（保留）：
─────────────────────────────────────────────────────────
"""

import math
import requests
import threading
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP, ROUND_HALF_UP
from typing import Callable, Optional, List, Dict, Any
from datetime import datetime, timedelta, date, time as dtime

from cache_service import get_cache
from config import load_config

BASE_URL = "https://api.fugle.tw/marketdata/v1.0/stock"
MAX_WATCH = 5


# ─────────────────────────────────────────────
#  快取 TTL 工具
# ─────────────────────────────────────────────

def _seconds_until_midnight() -> float:
    """計算距今晚午夜的秒數（確保跨日一定清快取）。"""
    now = datetime.now()
    midnight = datetime.combine(now.date() + timedelta(days=1), dtime.min)
    return max(60.0, (midnight - now).total_seconds())


def _make_daily_ttl() -> float:
    """
    智慧計算歷史 K 線（日K）快取時效：

    - 盤中 09:00–13:30：TTL = 300s（5 分鐘）
      → 開盤後今日K線尚在生成，需頻繁更新
    - 盤後 13:30+：TTL = 距午夜秒數
      → 今日已收盤，資料穩定，但午夜後必須清除
    - 盤前 ~09:00：TTL = 距午夜秒數
      → 前一日資料已定案，但不讓隔夜快取撐過今天開盤
    """
    now = datetime.now().time()
    market_open  = dtime(9, 0)
    market_close = dtime(13, 30)

    if market_open <= now < market_close:
        return 300.0
    else:
        return _seconds_until_midnight()


def _make_intraday_ttl() -> float:
    """
    智慧計算盤中 K 線（分K）快取時效：

    - 盤中：60s
    - 盤外：到午夜（確保隔日開盤時重新拉取，不顯示昨日分K）
    """
    now = datetime.now().time()
    market_open  = dtime(9, 0)
    market_close = dtime(13, 30)

    if market_open <= now < market_close:
        return 60.0
    else:
        return _seconds_until_midnight()


def _is_today_in_data(data: dict) -> bool:
    """
    檢查回傳的 K 線資料最後一筆是否是今日。
    若不是今日（例如快取殘留前一交易日），回傳 False。
    """
    try:
        today = date.today()
        today_str = today.isoformat()
        if today.weekday() >= 5:
            return True
        candles = data.get("data", []) or data.get("candles", [])
        if not candles:
            return True
        last = candles[-1]
        date_str = last.get("date", last.get("time", ""))
        return today_str in date_str
    except Exception:
        return True


def _last_candle_date(data: dict) -> Optional[str]:
    """取得歷史 K 線資料中最後一筆的日期字串（YYYY-MM-DD 格式）。"""
    try:
        candles = data.get("data", []) or data.get("candles", [])
        if not candles:
            return None
        last     = candles[-1]
        date_str = last.get("date", last.get("time", ""))
        return date_str[:10] if len(date_str) >= 10 else None
    except Exception:
        return None


# ─────────────────────────────────────────────
#  Tick 對齊工具（使用 Decimal 消除浮點誤差）
# ─────────────────────────────────────────────

def _tick_unit_decimal(price: float) -> Decimal:
    """回傳台股對應價格的最小跳動單位（Decimal 精確版）。"""
    p = Decimal(str(price))
    if p < 10:    return Decimal("0.01")
    if p < 50:    return Decimal("0.05")
    if p < 100:   return Decimal("0.1")
    if p < 500:   return Decimal("0.5")
    if p < 1000:  return Decimal("1")
    return Decimal("5")


def _snap_tick_decimal(price: float, direction: str = "round") -> float:
    """
    【v4.1 修正】將價格對齊到台股 Tick 單位。
    使用 Decimal 避免浮點精度問題（如 4.95 // 0.05 = 98 的錯誤）。

    direction:
      "floor" → 向下對齊（用於漲停計算、做多停損）
      "ceil"  → 向上對齊（用於跌停計算、放空停損）
      "round" → 四捨五入到最近 Tick
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


# ─────────────────────────────────────────────
#  全域 API 節流器（v5 新增）
# ─────────────────────────────────────────────
#
# 問題背景：
#   富果 API 免費額度通常是「每分鐘 N 次」的全域總量限制，
#   而不是每支股票各自 N 次。過去架構下，主頁面背景輪詢、
#   切換自選股群組、點入個股詳細頁（一次觸發 quote/ticker/
#   candles/daily_candles/volumes 共 5 支 API）三者互不協調、
#   各自平行發送請求，很容易在同一分鐘內疊加超過額度，
#   導致 429，且原本收到 429 後不會退避、不會重試，
#   只會直接把錯誤丟給 UI 顯示「資料無法顯示」。
#
# 解法：
#   1. 用一個全域 token-bucket 限制「每分鐘最多送出幾次請求」，
#      所有 FugleService 呼叫（不管來自主頁面或詳細頁）共用同一個
#      限制器，超過額度時「排隊等待」而不是硬打過去被 429。
#   2. 收到 429 時自動退避重試（exponential backoff），
#      而不是直接回傳錯誤給使用者。
#   3. 額度可在 config.json 設定（對應你的方案上限），
#      並保留安全緩衝（預設抓 80%），避免卡在剛好的臨界值。
#
class _RateLimiter:
    """全域滑動視窗節流器：確保每 60 秒內送出的請求數不超過上限。"""

    def __init__(self, max_per_minute: int = 55):
        # 預設 55（低於常見的 60/分鐘額度，保留安全緩衝）
        self.max_per_minute = max_per_minute
        self._timestamps: List[float] = []
        self._lock = threading.Lock()

    def set_limit(self, max_per_minute: int):
        with self._lock:
            self.max_per_minute = max(1, int(max_per_minute))

    def acquire(self, timeout: float = 15.0) -> bool:
        """
        取得一個請求額度；若目前額度已滿，會阻塞等待直到有空位或逾時。
        回傳 False 代表逾時仍未取得（呼叫端應放棄或稍後再試，不應硬打）。
        """
        deadline = time.time() + timeout
        while True:
            with self._lock:
                now = time.time()
                # 清除 60 秒前的紀錄（滑動視窗）
                self._timestamps = [t for t in self._timestamps if now - t < 60.0]
                if len(self._timestamps) < self.max_per_minute:
                    self._timestamps.append(now)
                    return True
                # 算出最早的請求何時會過期，作為建議等待時間
                wait_for = 60.0 - (now - self._timestamps[0])
            if time.time() >= deadline:
                return False
            time.sleep(min(max(wait_for, 0.05), 1.0))

    def remaining(self) -> int:
        with self._lock:
            now = time.time()
            self._timestamps = [t for t in self._timestamps if now - t < 60.0]
            return max(0, self.max_per_minute - len(self._timestamps))


# 全域單例：所有 FugleService 實例共用同一個節流器
_rate_limiter = _RateLimiter(max_per_minute=55)


def configure_rate_limit(max_per_minute: int):
    """供 settings_page 依方案上限調整節流閾值。"""
    _rate_limiter.set_limit(max_per_minute)


def get_rate_limit_remaining() -> int:
    """回傳本分鐘視窗內剩餘可用請求數，供 UI 顯示用量狀態。"""
    return _rate_limiter.remaining()


class FugleService:
    """富果行情 REST API 封裝 - 含最佳五檔、分價量表"""

    def __init__(self, api_key: str):
        self.api_key  = api_key
        self.headers  = {"X-API-KEY": api_key}
        self._cache   = get_cache()
        self._refresh_interval = load_config().get("refresh_interval", 10)
        # 依 config 的 api_rate_limit 設定節流上限（若有）
        configured_limit = load_config().get("api_rate_limit", 55)
        _rate_limiter.set_limit(configured_limit)

    # ── 即時報價 ─────────────────────────────
    def get_intraday_quote(self, symbol: str, force_refresh: bool = False) -> Optional[dict]:
        """取得即時報價（含最佳五檔 bids/asks）"""
        key = f"quote:{symbol}"
        if not force_refresh:
            cached = self._cache.get(key)
            if cached:
                return cached
        data = self._get(f"/intraday/quote/{symbol}")
        if data and "error" not in data:
            # ★ 修復：REST API 盤後回傳的 bids/asks 可能為空，
            #   從舊快取補回，避免五檔資料消失
            old = self._cache.get(key) or {}
            if not data.get("asks") and old.get("asks"):
                data["asks"] = old["asks"]
            if not data.get("bids") and old.get("bids"):
                data["bids"] = old["bids"]
            self._cache.set(key, data, ttl=float(self._refresh_interval))
        return data

    def merge_ws_quote(self, symbol: str, ws_data: dict):
        """
        將 WebSocket 推送資料合入 REST 快取。
        若 WS 資料缺少五檔，從舊快取補齊。

        ★ v4.2 修復：原版用 merged 判斷會永遠失效。
          {**cached, **ws_data} 合併後若 ws_data 帶了空的 bids:[],
          merged.get("bids") 就是 []（falsy），但 cached 的五檔已被蓋掉。
          改成判斷 ws_data 本身有無帶五檔，才能正確從舊快取補回。
        """
        key    = f"quote:{symbol}"
        cached = self._cache.get(key) or {}
        merged = {**cached, **ws_data}
        # ★ 關鍵修正：判斷 ws_data（而非 merged）有無帶五檔
        if not ws_data.get("asks") and cached.get("asks"):
            merged["asks"] = cached["asks"]
        if not ws_data.get("bids") and cached.get("bids"):
            merged["bids"] = cached["bids"]
        self._cache.set(key, merged, ttl=float(self._refresh_interval))

    # ── 快取工具 ─────────────────────────────
    def get_cache_remaining(self, symbol: str, data_type: str) -> float:
        return self._cache.remaining_ttl(f"{data_type}:{symbol}")

    def invalidate_symbol(self, symbol: str):
        for prefix in ("quote", "candles_intraday", "candles_daily", "volumes"):
            self._cache.invalidate_prefix(f"{prefix}:{symbol}")

    def invalidate_daily_cache(self, symbol: str = None):
        """手動清除日K快取（換日時或需要強制更新時使用）。"""
        if symbol:
            self._cache.invalidate_prefix(f"candles_daily:{symbol}")
        else:
            self._cache.invalidate_prefix("candles_daily:")

    # ── 技術指標（本地計算）───────────────────
    def get_technical_indicators(self, candles: list, is_intraday: bool = True) -> dict:
        if not candles or len(candles) < 2:
            return {}

        closes  = [c["close"]  for c in candles]
        highs   = [c["high"]   for c in candles]
        lows    = [c["low"]    for c in candles]
        volumes = [c.get("volume", 0) for c in candles]
        n       = len(closes)

        # ── SMA ──────────────────────────────────────────────────────
        def sma(data: list, period: int) -> Optional[float]:
            if len(data) < period:
                return None
            return round(sum(data[-period:]) / period, 2)

        # ── EMA 序列（標準指數加權） ──────────────────────────────────
        def ema_series(data: list, period: int) -> list:
            """
            回傳與 data 等長的 EMA 序列。
            前 period-1 個為 None；第 period 個為前 period 期的 SMA（種子值）；
            之後每根用標準 EMA 公式：EMA = price * k + prev_EMA * (1-k)。
            """
            if len(data) < period:
                return [None] * len(data)
            result = [None] * (period - 1)
            k   = 2.0 / (period + 1)
            val = sum(data[:period]) / period
            result.append(val)
            for price in data[period:]:
                val = price * k + val * (1 - k)
                result.append(val)
            return result

        def ema_last(data: list, period: int) -> Optional[float]:
            s = ema_series(data, period)
            return s[-1] if s and s[-1] is not None else None

        ma5  = sma(closes, 5)
        ma10 = sma(closes, 10)
        ma20 = sma(closes, 20)

        ema5_val  = ema_last(closes, 5)
        ema10_val = ema_last(closes, 10)
        ema12_val = ema_last(closes, 12)
        ema26_val = ema_last(closes, 26)

        # ── MACD（標準 EMA12/26/DEA9） ────────────────────────────────
        macd_result = None
        if n >= 26:
            ema12_s = ema_series(closes, 12)
            ema26_s = ema_series(closes, 26)
            # DIF = EMA12 - EMA26（從 index=25 起才有效值）
            dif_vals = []
            for e12, e26 in zip(ema12_s, ema26_s):
                if e12 is not None and e26 is not None:
                    dif_vals.append(e12 - e26)
            # 對連續有效 DIF 序列計算 DEA（EMA9 of DIF）
            if len(dif_vals) >= 9:
                dea_vals = ema_series(dif_vals, 9)
                dif_last = dif_vals[-1]
                dea_last = dea_vals[-1]
                if dea_last is not None:
                    hist = (dif_last - dea_last) * 2
                    macd_result = {
                        "dif":       round(dif_last, 3),
                        "dea":       round(dea_last, 3),
                        "histogram": round(hist,     3),
                    }

        # ── RSI（Wilder 14 期，門檻提高到 30 確保平滑收斂） ──────────
        rsi_result = None
        rsi_period = 14
        # v4.1 修正：原 n>=15 門檻太低（只有 1 次 Wilder 平滑，誤差大）
        # 改為 n>=30，確保 Wilder 平滑有足夠迭代次數（至少 16 次）收斂
        if n >= 30:
            deltas = [closes[i] - closes[i-1] for i in range(1, n)]
            gains  = [max(d, 0) for d in deltas]
            losses = [max(-d, 0) for d in deltas]
            # Wilder 平滑：前 14 期用 SMA 作種子，之後用 Wilder EMA
            avg_gain = sum(gains[:rsi_period]) / rsi_period
            avg_loss = sum(losses[:rsi_period]) / rsi_period
            for g, l in zip(gains[rsi_period:], losses[rsi_period:]):
                avg_gain = (avg_gain * (rsi_period - 1) + g) / rsi_period
                avg_loss = (avg_loss * (rsi_period - 1) + l) / rsi_period
            rsi_result = 100.0 if avg_loss == 0 else round(100 - 100 / (1 + avg_gain / avg_loss), 2)

        # ── KDJ（9 期 RSV，1/3 加權） ─────────────────────────────────
        kdj_result  = None
        period_kdj  = 9
        if n >= period_kdj:
            k_val = 50.0
            d_val = 50.0
            for i in range(period_kdj - 1, n):
                h9  = max(highs[i - period_kdj + 1: i + 1])
                l9  = min(lows[i  - period_kdj + 1: i + 1])
                rsv = (closes[i] - l9) / (h9 - l9) * 100 if h9 != l9 else 50.0
                k_val = rsv * (1/3) + k_val * (2/3)
                d_val = k_val * (1/3) + d_val * (2/3)
            j_val = 3 * k_val - 2 * d_val
            kdj_result = {
                "k": round(k_val, 2),
                "d": round(d_val, 2),
                "j": round(j_val, 2),
            }

        # ── VWAP（成交量加權平均價） ──────────────────────────────────
        vwap = None
        try:
            total_pv = total_v = 0.0
            for c in candles:
                h  = c.get("high",  c.get("close", 0))
                l  = c.get("low",   c.get("close", 0))
                cl = c.get("close", 0)
                v  = c.get("volume", 0)
                if v > 0:
                    typical   = (h + l + cl) / 3.0 if is_intraday else cl
                    total_pv += typical * v
                    total_v  += v
            if total_v > 0:
                vwap = round(total_pv / total_v, 4)
        except Exception:
            pass

        vwap_deviation = None
        if vwap and closes:
            try:
                vwap_deviation = round((closes[-1] - vwap) / vwap * 100, 2)
            except Exception:
                pass

        # ── 布林通道（20 期，±2σ） ────────────────────────────────────
        bollinger = None
        period_boll = 20
        if n >= period_boll:
            try:
                window = closes[-period_boll:]
                mid    = sum(window) / period_boll
                std    = math.sqrt(sum((x - mid) ** 2 for x in window) / period_boll)
                upper  = round(mid + 2 * std, 2)
                lower  = round(mid - 2 * std, 2)
                bw     = round((upper - lower) / mid * 100, 2) if mid else None
                pb     = round((closes[-1] - lower) / (upper - lower), 4) if (upper - lower) else None
                bollinger = {
                    "upper": upper,
                    "mid":   round(mid, 2),
                    "lower": lower,
                    "bandwidth": bw,
                    "pct_b":    pb,
                }
            except Exception:
                pass

        # ── ATR（v4.1 修正：標準 Wilder 14 期平滑） ───────────────────
        # 原版：簡單平均最近 10 根 TR → 期數偏短、非 Wilder，ATR 偏低
        # 修正：14 期 Wilder 指數平滑（與 TradingView / Bloomberg 一致）
        # 公式：ATR_今 = (ATR_昨 × 13 + TR_今) / 14
        atr = None
        atr_period = 14
        if n >= atr_period + 1:
            try:
                trs = []
                for i in range(1, n):
                    h  = highs[i]
                    l  = lows[i]
                    pc = closes[i - 1]
                    trs.append(max(h - l, abs(h - pc), abs(l - pc)))
                # Wilder 平滑：前 14 根 TR 用 SMA 作種子
                atr_val = sum(trs[:atr_period]) / atr_period
                for tr in trs[atr_period:]:
                    atr_val = (atr_val * (atr_period - 1) + tr) / atr_period
                atr = round(atr_val, 2)
            except Exception:
                pass

        # ── OBV（能量潮） ─────────────────────────────────────────────
        obv = None
        if n >= 2:
            try:
                obv = float(volumes[0]) if volumes else 0.0
                for i in range(1, n):
                    if closes[i] > closes[i - 1]:
                        obv += volumes[i]
                    elif closes[i] < closes[i - 1]:
                        obv -= volumes[i]
                obv = round(obv, 0)
            except Exception:
                pass

        # ── ROC（變化率，10 期） ──────────────────────────────────────
        roc = None
        roc_period = 10
        if n > roc_period:
            try:
                # closes[-11] = 10 期前的收盤；closes[-1] = 今日
                roc = round((closes[-1] - closes[-roc_period - 1]) / closes[-roc_period - 1] * 100, 2)
            except Exception:
                pass

        # ── 量價背離偵測 ─────────────────────────────────────────────
        volume_divergence = None
        if n >= 3:
            try:
                prev_vol   = volumes[-2] if len(volumes) >= 2 else 0
                curr_vol   = volumes[-1]
                prev_close = closes[-2]
                curr_close = closes[-1]
                vol_shrink = curr_vol < prev_vol * 0.7
                price_up   = curr_close > prev_close
                price_down = curr_close < prev_close
                if vol_shrink and price_up:
                    volume_divergence = "量縮價漲"
                elif vol_shrink and price_down:
                    volume_divergence = "量縮價跌"
            except Exception:
                pass

        # ── 開盤區間（Opening Range）──────────────────────────────────
        # v4.1 修正：時間比對改為明確字串格式，避免 minutes >= 60 時計算錯誤
        opening_range = None
        if is_intraday and n >= 5:
            try:
                def _candles_before(target_time_str: str) -> list:
                    """
                    取出 time <= target_time_str 的所有 K 棒。
                    target_time_str 格式為 "HH:MM"（如 "09:05"）。
                    只比對前 5 字元，相容 "HH:MM" 與 "HH:MM:SS"。
                    """
                    return [
                        c for c in candles
                        if c.get("time", "")[:5] <= target_time_str
                    ]

                opening_range = {}
                # OR5：開盤後 5 分鐘區間（09:00–09:05）
                subset5 = _candles_before("09:05")
                if len(subset5) >= 1:
                    opening_range["or5"] = {
                        "high": max(c["high"] for c in subset5),
                        "low":  min(c["low"]  for c in subset5),
                    }
                # OR15：開盤後 15 分鐘區間（09:00–09:15）
                subset15 = _candles_before("09:15")
                if len(subset15) >= 1:
                    opening_range["or15"] = {
                        "high": max(c["high"] for c in subset15),
                        "low":  min(c["low"]  for c in subset15),
                    }
                # OR20：開盤後 20 分鐘區間（09:00–09:20）
                subset20 = _candles_before("09:20")
                if len(subset20) >= 1:
                    opening_range["or20"] = {
                        "high": max(c["high"] for c in subset20),
                        "low":  min(c["low"]  for c in subset20),
                    }
                # OR30：開盤後 30 分鐘區間（09:00–09:30），新增供 ORB 策略使用
                subset30 = _candles_before("09:30")
                if len(subset30) >= 1:
                    opening_range["or30"] = {
                        "high": max(c["high"] for c in subset30),
                        "low":  min(c["low"]  for c in subset30),
                    }
                if not opening_range:
                    opening_range = None
            except Exception:
                opening_range = None

        # ── 相對成交量（RVOL）── v4.1 新增，供 ORB 策略使用 ─────────
        # RVOL = 最近 N 根均量 / 前半段均量，判斷當前成交是否放大
        rvol = None
        if n >= 10:
            try:
                recent_5  = volumes[-5:]
                prior_5   = volumes[-10:-5]
                avg_recent = sum(recent_5) / len(recent_5)
                avg_prior  = sum(prior_5)  / len(prior_5)
                if avg_prior > 0:
                    rvol = round(avg_recent / avg_prior, 2)
            except Exception:
                pass

        # ── 當沖評分（盤中快速評估） ──────────────────────────────────
        intraday_score = None
        if is_intraday:
            try:
                score = 0
                hints = []
                if rsi_result is not None:
                    if rsi_result > 70:
                        score -= 15
                        hints.append(f"RSI 超買({rsi_result:.1f})")
                    elif rsi_result > 60:
                        score += 10
                        hints.append(f"RSI 偏強({rsi_result:.1f})")
                    elif rsi_result < 30:
                        score += 15
                        hints.append(f"RSI 超賣({rsi_result:.1f})")
                    elif rsi_result < 40:
                        score -= 10
                        hints.append(f"RSI 偏弱({rsi_result:.1f})")
                if macd_result:
                    if macd_result["histogram"] > 0:
                        score += 10
                        hints.append("MACD 柱正")
                    else:
                        score -= 10
                        hints.append("MACD 柱負")
                if vwap and closes:
                    if closes[-1] > vwap:
                        score += 10
                        hints.append("現價>VWAP")
                    else:
                        score -= 10
                        hints.append("現價<VWAP")
                if kdj_result:
                    j = kdj_result.get("j", 50)
                    if j > 80:
                        score -= 10
                        hints.append(f"KDJ J超買({j:.0f})")
                    elif j < 20:
                        score += 10
                        hints.append(f"KDJ J超賣({j:.0f})")
                if "量縮價跌" in (volume_divergence or ""):
                    score -= 10
                elif "量縮價漲" in (volume_divergence or ""):
                    score -= 5

                # ORB 突破加分（v4.1 新增）
                if opening_range and closes:
                    current = closes[-1]
                    or_ref  = opening_range.get("or15") or opening_range.get("or20")
                    if or_ref:
                        if current > or_ref["high"] and (rvol or 1.0) >= 1.5:
                            score += 15
                            hints.append(f"ORB向上突破(量比{rvol}x)")
                        elif current < or_ref["low"] and (rvol or 1.0) >= 1.5:
                            score -= 15
                            hints.append(f"ORB向下突破(量比{rvol}x)")

                intraday_score = {
                    "score": max(-100, min(100, score)),
                    "bias":  "多" if score > 15 else ("空" if score < -15 else "中性"),
                    "hints": hints[:5],
                }
            except Exception:
                intraday_score = None

        # ════════ [DEBUG] get_technical_indicators 診斷 ════════
        print(f"[DEBUG][indicators] candles 總根數={len(candles)}  is_intraday={is_intraday}")
        print(f"[DEBUG][indicators] 最後一根 candle={candles[-1] if candles else 'EMPTY'}")
        print(f"[DEBUG][indicators] rvol={rvol}  (需>=1.5 才算放量確認)")
        print(f"[DEBUG][indicators] opening_range keys={list(opening_range.keys()) if opening_range else 'None'}")
        if opening_range:
            for k, v in opening_range.items():
                print(f"[DEBUG][indicators]   {k}: high={v['high']} low={v['low']}")
        print(f"[DEBUG][indicators] rsi={rsi_result}  macd={macd_result}  vwap={vwap}")
        # ════════ [DEBUG END] ════════

        return {
            "ma":        {"ma5": ma5, "ma10": ma10, "ma20": ma20},
            "ema":       {
                "ema5":  round(ema5_val,  4) if ema5_val  is not None else None,
                "ema10": round(ema10_val, 4) if ema10_val is not None else None,
                "ema12": round(ema12_val, 4) if ema12_val is not None else None,
                "ema26": round(ema26_val, 4) if ema26_val is not None else None,
            },
            "macd":      macd_result,
            "rsi":       rsi_result,
            "kdj":       kdj_result,
            "vwap":      vwap,
            "vwap_dev":  vwap_deviation,
            "bollinger": bollinger,
            "atr":       atr,
            "obv":       obv,
            "roc":       roc,
            "rvol":      rvol,       # v4.1 新增：相對成交量
            "vol_div":   volume_divergence,
            "or":        opening_range,
            "score":     intraday_score,
        }

    # ── REST 資料取得 ─────────────────────────
    def _get(self, path: str, params: dict = None, _retry: int = 0) -> Optional[dict]:
        # ── 全域節流：所有請求先排隊取得額度，避免疊加超過每分鐘上限 ──
        if not _rate_limiter.acquire(timeout=15.0):
            # 排隊 15 秒仍拿不到額度，代表目前請求量真的過大，
            # 直接告知使用者「忙碌中」而非硬打去被 429。
            return {"error": "目前請求較多，請稍候幾秒再試", "rate_limited": True}

        url = f"{BASE_URL}{path}"
        try:
            resp = requests.get(url, headers=self.headers, params=params, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                # 自動退避重試（最多 2 次：等 1.5s、再等 3s）
                if _retry < 2:
                    time.sleep(1.5 * (_retry + 1))
                    return self._get(path, params, _retry=_retry + 1)
                return {"error": "超過 API 速率限制，請稍後再試", "rate_limited": True}
            elif resp.status_code == 401:
                return {"error": "API 金鑰無效或已過期"}
            else:
                return {"error": f"HTTP {resp.status_code}: {resp.text[:100]}"}
        except requests.exceptions.ConnectionError:
            return {"error": "網路連線失敗"}
        except requests.exceptions.Timeout:
            return {"error": "請求逾時"}
        except Exception as e:
            return {"error": str(e)}

    def get_intraday_candles(self, symbol: str, force_refresh: bool = False) -> Optional[dict]:
        key = f"candles_intraday:{symbol}"
        if not force_refresh:
            cached = self._cache.get(key)
            if cached:
                return cached
        data = self._get(f"/intraday/candles/{symbol}")
        if data and "error" not in data:
            self._cache.set(key, data, ttl=_make_intraday_ttl())
        return data

    def get_intraday_ticker(self, symbol: str) -> Optional[dict]:
        key    = f"ticker:{symbol}"
        cached = self._cache.get(key)
        if cached:
            return cached
        data = self._get(f"/intraday/ticker/{symbol}")
        if data and "error" not in data:
            self._cache.set(key, data, ttl=3600.0)
        return data

    def get_historical_intraday_candles(
        self,
        symbol: str,
        date_str: str,
        force_refresh: bool = False,
    ) -> Optional[dict]:
        """
        取得「指定過去日期」的完整1分K線（v15新增，供跨日補結算使用）。

        背景：get_intraday_candles() 呼叫的 /intraday/candles/{symbol}
        端點永遠只回傳「今天」的盤中分K，無法查詢過去某一天的分鐘級
        走勢。如果 App 在收盤前到隔天開盤這段時間完全沒開機，
        analysis_history 中那天的「待結算」紀錄就會抓不到當天的分K，
        永遠卡在 pending。

        解法：改用 /historical/candles/{symbol} 這支端點，它同時支援
        指定 timeframe=1（1分K）與 from/to 日期區間，可以精確查回
        「過去某一天」的完整分鐘級K線（分K歷史資料自2023-05-23起提供），
        讓收盤結算機制即使跨日補跑，依然能用當天真實的分K資料回放，
        不必退而求其次改用日K收盤價粗略估算。

        date_str 格式為 "yyyy-MM-dd"。
        """
        params = {
            "timeframe": "1",
            "from": date_str,
            "to": date_str,
            # 【修正】明確指定要回傳的欄位。原本沒帶這個參數，依賴富果
            # API的預設行為——但實測發現省略fields時，回傳的每根K棒
            # 可能缺少high/low（只給了close），導致後面
            # parse_candles_for_chart() 解析出來的high/low用預設值
            # 頂替，結算判斷永遠比對不到真正的最高/最低價，使用者
            # 重新整理時明明有看到分K被抓下來，結算結果卻還是照舊用
            # 收盤價強制平倉，而非正確判斷是否曾觸及停利/停損。
            # 這裡明確要open/high/low/close/volume，確保回傳一定完整。
            "fields": "open,high,low,close,volume",
        }
        key = f"candles_hist_intraday:{symbol}:{date_str}"

        if not force_refresh:
            cached = self._cache.get(key)
            if cached:
                return cached

        data = self._get(f"/historical/candles/{symbol}", params)
        if data and "error" not in data:
            # 補結算是過去已經走完的交易日，資料不會再變動，可長期快取
            self._cache.set(key, data, ttl=86400.0)
        return data

    def get_historical_candles(
        self,
        symbol: str,
        from_date: str = None,
        to_date: str   = None,
        force_refresh: bool = False,
    ) -> Optional[dict]:
        params = {}
        if not from_date:
            from_dt        = datetime.now() - timedelta(days=120)
            params["from"] = from_dt.strftime("%Y-%m-%d")
        else:
            params["from"] = from_date
        params["to"] = to_date or datetime.now().strftime("%Y-%m-%d")
        # 同上，明確指定欄位，避免依賴API預設行為而漏掉high/low
        params["fields"] = "open,high,low,close,volume"

        key = f"candles_daily:{symbol}:{params['from']}:{params['to']}"

        if not force_refresh:
            cached = self._cache.get(key)
            if cached:
                now = datetime.now().time()
                is_market_hours = dtime(9, 0) <= now < dtime(13, 30)
                if is_market_hours and not _is_today_in_data(cached):
                    print(f"[cache] {symbol} 日K快取不含今日，強制重拉")
                    self._cache.invalidate(key)
                else:
                    return cached

        data = self._get(f"/historical/candles/{symbol}", params)
        if data and "error" not in data:
            ttl = _make_daily_ttl()
            self._cache.set(key, data, ttl=ttl)
            print(f"[cache] {symbol} 日K快取更新，TTL={ttl:.0f}s")
        return data

    def get_taiex_quote(self, force_refresh: bool = False) -> Optional[dict]:
        key = "quote:IX0001"
        if not force_refresh:
            cached = self._cache.get(key)
            if cached:
                return cached
        data = self._get("/intraday/quote/IX0001")
        if data and "error" not in data:
            now = datetime.now().time()
            is_market = dtime(9, 0) <= now < dtime(13, 30)
            ttl = 60.0 if is_market else _seconds_until_midnight()
            self._cache.set(key, data, ttl=ttl)
        return data

    def get_intraday_volumes(self, symbol: str, force_refresh: bool = False) -> Optional[dict]:
        key = f"volumes:{symbol}"
        if not force_refresh:
            cached = self._cache.get(key)
            if cached:
                return cached
        data = self._get(f"/intraday/volumes/{symbol}")
        if data and "error" not in data:
            self._cache.set(key, data, ttl=float(self._refresh_interval))
        return data


# ──────────────────────────────────────────────────────────
#  PollingService（REST 輪詢，作為 WS 的降級備援）
# ──────────────────────────────────────────────────────────

class PollingService:
    """定時輪詢服務（WS 不可用時的備用方案）"""

    def __init__(self, fugle: FugleService, interval: int = 10):
        self.fugle     = fugle
        self.interval  = interval
        self._running  = False
        self._thread: Optional[threading.Thread] = None
        self._callbacks: Dict[str, List[Callable]] = {}
        self._watchlist: List[str] = []

    def set_watchlist(self, symbols: List[str]):
        self._watchlist = list(symbols)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _poll_loop(self):
        """
        全域唯一的自選股輪詢路徑（v8）。

        每支股票之間 stagger 一小段時間，避免同一瞬間對 N 支股票
        同時發送請求造成 burst；實際的「每分鐘總量」上限則交給
        fugle_service._rate_limiter 全域節流器統一把關，
        即使這裡跑得快，也不會真的超過額度打去給伺服器。
        """
        while self._running:
            watch_now = self._watchlist[:]
            n = len(watch_now)
            for i, symbol in enumerate(watch_now):
                if not self._running:
                    return
                try:
                    data = self.fugle.get_intraday_quote(symbol, force_refresh=True)
                    if data and "error" not in data:
                        self._fire(symbol, data)
                        self._fire("*", {"symbol": symbol, "data": data})
                    # rate_limited 時安靜跳過本次，下一輪 interval 再試，
                    # 不需要特別處理：_get() 已經做過重試與排隊。
                except Exception:
                    pass
                # 均勻分散在 interval 秒內完成整輪，而不是一次性打完，
                # 這樣即使股票數變多，也不會瞬間衝出一波尖峰請求。
                if n > 1 and i < n - 1:
                    time.sleep(max(0.05, min(1.0, self.interval / max(n, 1))))
            time.sleep(self.interval)

    def _fire(self, key: str, data: Any):
        if key not in self._callbacks:
            return
        for cb in self._callbacks[key][:]:
            try:
                cb(data)
            except Exception:
                pass

    def on_update(self, symbol: str, callback: Callable):
        if symbol not in self._callbacks:
            self._callbacks[symbol] = []
        if callback not in self._callbacks[symbol]:
            self._callbacks[symbol].append(callback)

    def unsubscribe(self, symbol: str, callback: Callable):
        if symbol in self._callbacks:
            if callback in self._callbacks[symbol]:
                self._callbacks[symbol].remove(callback)
            if not self._callbacks[symbol]:
                del self._callbacks[symbol]

    def remove_symbol(self, symbol: str):
        if symbol in self._watchlist:
            self._watchlist.remove(symbol)
        if symbol in self._callbacks:
            del self._callbacks[symbol]

    def remove_symbol_from_list(self, symbol: str):
        self.remove_symbol(symbol)


# ──────────────────────────────────────────────────────────
#  K線格式轉換（共用工具）
# ──────────────────────────────────────────────────────────

def parse_candles_for_chart(candles_data: dict, is_intraday: bool = True) -> List[Dict]:
    """將富果 K 線資料轉換為圖表用格式"""
    result = []
    if not candles_data or "error" in candles_data:
        return result

    candles = candles_data.get("data", []) or candles_data.get("candles", [])

    for c in candles:
        try:
            t_str = c.get("date", c.get("time", ""))
            if "T" in t_str:
                parts        = t_str.split("T")
                time_part    = parts[1][:5] if len(parts) > 1 else t_str
                display_time = time_part
            else:
                display_time = t_str[-5:] if len(t_str) >= 5 else t_str

            result.append({
                "time":      display_time,
                "full_time": t_str,
                "open":      float(c.get("open",  1)),
                "high":      float(c.get("high",  1)),
                "low":       float(c.get("low",   1)),
                "close":     float(c.get("close", 1)),
                "volume":    int(c.get("volume",  0)),
            })
        except (ValueError, TypeError):
            continue

    if result and len(result) > 1:
        try:
            if result[0]["full_time"] > result[-1]["full_time"]:
                result.reverse()
        except Exception:
            pass

    if is_intraday and result:
        result = [r for r in result if "09:00" <= r["time"] <= "13:30"]

    return result