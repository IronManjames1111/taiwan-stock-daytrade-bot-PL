"""
快取服務 v5
記憶體快取（行情/K線/Ticker）+ 持久化 AI 分析快取（JSON）

v4 → v5 修正：

【問題：切換日期後「本日分析記錄」仍然空白，或讀到舊日資料】

  根本原因：
    v4 雖然加了 for_today 參數，但 JSON 仍保留多日資料，
    且 _purge_old_entries 是「30天才清除」，不是「當天結束就清除」。
    結果：
    1. 新的一天開始時，舊日快取還在，容易被誤讀。
    2. UI 切換日期時，若傳入非今日的 date，for_today=True 直接回 None，
       導致「本日分析記錄」永遠顯示空白。

  v5 解決策略：
    ★ AI 快取只保留「今日」資料，每次寫入或讀取前自動清除非今日的所有記錄。
    ★ 簡化所有 load 函式，移除 for_today 參數（邏輯內建，永遠只看今日）。
    ★ 移除 MAX_KEEP_DAYS / _purge_old_entries，改為 _purge_non_today()。
    ★ list_ai_cache_dates 改為只回傳今日（若有快取）。
    ★ 保留備份寫入機制（防損毀）。

AI 快取結構（簡化）：
{
  "2330_2025-01-15": {
    "symbol": "2330",
    "date":   "2025-01-15",
    "entries": [
      { "time": "09:32:11", "result": {...} },
      ...
    ]
  }
  // 只會有今日的 key，昨日及以前的 key 在下次寫入時自動清除
}
"""
import time
import threading
import json
import os
import shutil
from typing import Any, Optional, Dict, List
from datetime import datetime


# ─────────────────────────────────────────────
#  記憶體快取
# ─────────────────────────────────────────────
class CacheEntry:
    def __init__(self, data: Any, ttl: float):
        self.data       = data
        self.expires_at = time.time() + ttl

    def is_valid(self) -> bool:
        return time.time() < self.expires_at


class CacheService:
    def __init__(self):
        self._store: Dict[str, CacheEntry] = {}
        self._lock  = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry and entry.is_valid():
                return entry.data
            if entry:
                del self._store[key]
            return None

    def set(self, key: str, data: Any, ttl: float):
        with self._lock:
            self._store[key] = CacheEntry(data, ttl)

    def invalidate(self, key: str):
        with self._lock:
            self._store.pop(key, None)

    def invalidate_prefix(self, prefix: str):
        with self._lock:
            for k in [k for k in self._store if k.startswith(prefix)]:
                del self._store[k]

    def has_valid(self, key: str) -> bool:
        return self.get(key) is not None

    def remaining_ttl(self, key: str) -> float:
        with self._lock:
            entry = self._store.get(key)
            if entry and entry.is_valid():
                return max(0.0, entry.expires_at - time.time())
            return 0.0

    def clear_all(self):
        with self._lock:
            self._store.clear()


# ─────────────────────────────────────────────
#  AI 持久化快取（僅保留今日）
# ─────────────────────────────────────────────
from pathlib import Path

_storage_data = os.getenv("FLET_APP_STORAGE_DATA")
if _storage_data:
    _BASE_DIR = Path(_storage_data)
else:
    _BASE_DIR = Path(__file__).parent

AI_CACHE_FILE        = str(_BASE_DIR / "ai_analysis_cache.json")
AI_CACHE_BACKUP_FILE = str(_BASE_DIR / "ai_analysis_cache.bak.json")
_ai_lock             = threading.Lock()
MAX_ENTRIES_PER_DAY  = 50    # 每支股票每天最多保留幾筆


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _read() -> dict:
    if os.path.exists(AI_CACHE_FILE):
        try:
            with open(AI_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _write(data: dict):
    """
    寫入 AI 快取 JSON。
    先備份舊檔，寫入成功後再刪除備份，防止寫入到一半損毀。
    """
    try:
        if os.path.exists(AI_CACHE_FILE):
            shutil.copy2(AI_CACHE_FILE, AI_CACHE_BACKUP_FILE)

        with open(AI_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        if os.path.exists(AI_CACHE_BACKUP_FILE):
            os.remove(AI_CACHE_BACKUP_FILE)

    except Exception as e:
        print(f"[AI Cache] 寫入失敗: {e}")
        if os.path.exists(AI_CACHE_BACKUP_FILE):
            try:
                shutil.copy2(AI_CACHE_BACKUP_FILE, AI_CACHE_FILE)
                print(f"[AI Cache] 已從備份還原")
            except Exception as e2:
                print(f"[AI Cache] 備份還原失敗: {e2}")


def _day_key(symbol: str, date: str) -> str:
    return f"{symbol}_{date}"


def _purge_non_today(data: dict) -> dict:
    """
    v5 核心：清除所有非今日的快取記錄。
    每次讀寫前呼叫，確保 JSON 只存今日資料。
    """
    today = _today()
    to_delete = [
        k for k, v in data.items()
        if isinstance(v, dict) and v.get("date", "") != today
    ]
    if to_delete:
        for k in to_delete:
            del data[k]
        print(f"[AI Cache] 清除 {len(to_delete)} 筆非今日（{today}）的舊記錄")
    return data


# ── 寫入 ─────────────────────────────────────
def save_ai_analysis(symbol: str, result: dict, sim_time=None):
    """
    新增一筆分析到當日記錄（append）。
    每支股票每天最多保留 MAX_ENTRIES_PER_DAY 筆。

    sim_time: datetime.time 物件，若提供則使用模擬時間（HH:MM:SS）。
    """
    today = _today()
    if sim_time is not None:
        now = sim_time.strftime("%H:%M:%S") if hasattr(sim_time, 'strftime') else str(sim_time)
    else:
        now = datetime.now().strftime("%H:%M:%S")

    key = _day_key(symbol, today)

    with _ai_lock:
        data    = _read()
        data    = _purge_non_today(data)          # ★ 清除非今日記錄
        day_rec = data.get(key, {"symbol": symbol, "date": today, "entries": []})
        entries = day_rec.get("entries", [])
        entries.append({"time": now, "result": result})
        if len(entries) > MAX_ENTRIES_PER_DAY:
            entries = entries[-MAX_ENTRIES_PER_DAY:]
        day_rec["entries"] = entries
        data[key]          = day_rec
        _write(data)


# ── 讀取 ─────────────────────────────────────
def load_ai_day(symbol: str) -> Optional[dict]:
    """
    讀取今日整筆記錄（含所有 entries）。
    回傳 {"symbol", "date", "entries": [...]} 或 None。

    v5：固定只讀今日，不接受 date 參數（避免誤讀舊日資料）。
    """
    today = _today()
    with _ai_lock:
        data = _read()
        data = _purge_non_today(data)             # ★ 順便清理
        return data.get(_day_key(symbol, today))


def load_ai_latest(symbol: str) -> Optional[dict]:
    """
    回傳今日最新一筆 entry，或 None。

    v5：固定只讀今日，移除 date / for_today 參數。
    """
    day = load_ai_day(symbol)
    if day and day.get("entries"):
        return day["entries"][-1]
    return None


def load_ai_history(symbol: str) -> List[dict]:
    """
    回傳今日所有 entries 列表（供 AI 回顧用）。

    v5：固定只讀今日，移除 date 參數。
    """
    day = load_ai_day(symbol)
    if day:
        return day.get("entries", [])
    return []


def list_ai_cache_dates(symbol: str) -> List[str]:
    """
    列出今日是否有快取（v5 只保留今日）。
    回傳 [today] 若有快取，否則回傳 []。
    """
    today = _today()
    with _ai_lock:
        data = _read()
        key  = _day_key(symbol, today)
        if key in data and isinstance(data[key], dict) and data[key].get("entries"):
            return [today]
        return []


def clear_ai_cache_by_date(symbol: str, date: str = None):
    """
    清除特定股票的今日快取（v5 只有今日可清除）。
    date 參數保留以維持向後相容，但實際固定清除今日。
    """
    today = _today()
    with _ai_lock:
        data = _read()
        data.pop(_day_key(symbol, today), None)
        _write(data)


def clear_ai_cache_all(symbol: str = None):
    """清除全部（或特定股票）的 AI 快取。"""
    with _ai_lock:
        if symbol is None:
            _write({})
        else:
            today = _today()
            data  = _read()
            _write({k: v for k, v in data.items() if not k.startswith(f"{symbol}_")})


def purge_old_ai_cache():
    """
    v5：強制清除所有非今日快取（可在 app 啟動時呼叫）。
    v4 的 keep_days 參數已移除，固定清除非今日資料。
    """
    with _ai_lock:
        data = _read()
        data = _purge_non_today(data)
        _write(data)


def _timedelta_days(days: int):
    from datetime import timedelta
    return timedelta(days=days)


# ═════════════════════════════════════════════════════════════
#  分析歷史紀錄（v14 新增，v15 起為唯一的成效驗證機制）
# ═════════════════════════════════════════════════════════════
#
# 背景：
#   使用者每次按下「AI分析」後，希望能看到一份完整的歷史紀錄——
#   用了哪個模型、哪個風險模式、以及這筆分析最終是賺是賠。
#
#   v14 時，這個模組是跟另一套「signal_track」（30分鐘後抓單一
#   時間點報價，快速比對一次）並行的機制。v15 移除了 signal_track：
#   單點驗證容易抓到誤導性的瞬間值（例如股價30分鐘內先衝停利又
#   跌破停損，單點只會抓到其中一種結果），而且兩套機制記錄的
#   幾乎是同一份資料，維護意義不大。現在統一只用本模組的
#   「收盤前分K回放」判斷，分母資料只有一份、判斷邏輯只有一套。
#
# 設計：
#   1. 每次 AI 分析完成、且訊號非 WATCH 時，stock_detail_page.py 呼叫
#      add_history_record() 記錄一筆「待結算」的完整快照。
#   2. app.py 背景執行緒在每個交易日的收盤前結算時間
#      （預設 13:25）之後，對當天所有「待結算」記錄呼叫
#      settle_history_record_with_candles()：抓該股票當天完整分K線，
#      依時間順序掃描每根K棒的高低區間，判斷先觸及停利還是停損；
#      若整天都沒觸及，用最後一根K棒的收盤價強制平倉計算損益。
#   3. 歷史紀錄頁面（history_page.py）呼叫 list_history()/
#      delete_history_records() 提供清單顯示、篩選、多選刪除。
#   4. get_history_stats() 提供聚合命中率，供個股詳細頁的
#      「近30天實測命中率」標籤使用（取代原本 signal_track 版本）。
#      因為是收盤後才結算，命中率只反映「已收盤結算」的交易日，
#      當天盤中新分析的訊號要等到當天收盤結算後才會計入。
#
# 資料檔案：analysis_history.json，結構：
# {
#   "records": [
#     {
#       "id": "2330_20260905_093211_123456",
#       "symbol": "2330", "date": "2026-09-05", "time": "09:32:11",
#       "model": "gemini-2.0-flash", "risk_mode": "relaxed",
#       "signal": "STRONG_BUY", "direction": "做多",
#       "entry_price": 1015.0, "stop_loss": 1008.0, "take_profit": 1030.0,
#       "settle_status": "pending" | "settled",
#       "result": null | "win" | "loss" | "breakeven",
#       "exit_price": null | float,
#       "exit_reason": null | "hit_tp" | "hit_sl" | "forced_close",
#       "exit_time": null | "13:25:00",
#       "pnl_pct": null | float   # 損益百分比，正負皆可
#     }, ...
#   ]
# }
#
ANALYSIS_HISTORY_FILE   = str(_BASE_DIR / "analysis_history.json")
_history_lock            = threading.Lock()
MAX_HISTORY_RECORDS      = 5000   # 避免檔案無限增長
HISTORY_TRACKED_SIGNALS  = {"STRONG_BUY", "BUY", "SHORT", "STRONG_SHORT"}  # WATCH 不記錄


def _read_history() -> dict:
    if os.path.exists(ANALYSIS_HISTORY_FILE):
        try:
            with open(ANALYSIS_HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"records": []}


def _write_history(data: dict):
    try:
        with open(ANALYSIS_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Analysis History] 寫入失敗: {e}")


def add_history_record(
    symbol: str,
    model: str,
    risk_mode: str,
    signal: str,
    direction: str,
    entry_price: Optional[float],
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    sim_time=None,
    shares: Optional[int] = None,
    analysis_reason: Optional[str] = None,
    force_direction: Optional[str] = None,
) -> Optional[str]:
    """
    記錄一筆分析歷史。僅對有實際方向的訊號記錄
    （WATCH 沒有下單動作，依使用者需求不列入歷史）。

    sim_time：可選的 datetime.time 物件。當使用者用「模擬時間」功能
    分析時傳入，記錄的時間戳會採用「今天日期 + 這個模擬時間點」，
    而不是呼叫當下的系統時間。這是必要的，因為收盤結算機制是依
    分K線的時間欄位回放比對，若記錄時間跟模擬分析當下想重播的
    時間點對不上，之後結算時抓到的K線區間就會錯位。
    模擬時間功能本身不模擬「過去某一天」，只模擬「今天盤中某時刻」，
    因此日期仍固定用系統的今天日期，只有時分秒採用 sim_time。

    shares（v16新增）：這筆交易採用的股數快照。記錄「當下」設定值
    而非之後即時讀取，是因為使用者之後可能調整預設股數，若損益金額
    改用最新設定回推，會讓舊紀錄的損益跟著背景設定改變而失真——
    每筆紀錄應該反映「當初分析時打算用多少股數」。

    analysis_reason（v16新增）：這次AI分析的完整原始回覆文字
    （對應 gemini_service 回傳的 result["full_text"]），保留AI給出
    這個訊號時的完整理由（技術指標評分、MTF共識、風險提示等），
    供之後匯出資料餵給AI檢討分析品質時使用。不做結構化拆解，
    因為完整原文比事後解析出來的片段更不會遺漏資訊、也不受
    prompt格式微調影響而解析失敗。

    force_direction（v20新增）：這次分析當下是否由使用者強制指定
    方向（"buy"＝強制做多 / "short"＝強制放空 / None＝一般分析，
    AI自行判斷方向）。記錄「當下」是否為強制模式，是因為使用者
    之後可能關閉強制模式，若之後才回頭讀取設定，會讓舊紀錄的
    強制狀態跟著背景設定改變而失真——每筆紀錄應該反映「當初這次
    分析實際是不是被強制」。供歷史紀錄頁面顯示與匯出JSON使用。

    回傳這筆記錄的 id；訊號類型不需記錄時（WATCH）回傳 None。
    """
    if signal not in HISTORY_TRACKED_SIGNALS:
        return None
    if not entry_price or entry_price <= 0:
        return None

    now = datetime.now()
    if sim_time is not None:
        try:
            now = datetime.combine(now.date(), sim_time)
        except Exception:
            pass
    record_id = f"{symbol}_{now.strftime('%Y%m%d_%H%M%S_%f')}"

    record = {
        "id": record_id,
        "symbol": symbol,
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "model": model or "未知",
        "risk_mode": risk_mode or "auto",
        "signal": signal,
        "direction": direction or ("做多" if signal in ("BUY", "STRONG_BUY") else "放空"),
        "entry_price": float(entry_price),
        "stop_loss": float(stop_loss) if stop_loss else None,
        "take_profit": float(take_profit) if take_profit else None,
        "shares": int(shares) if shares else 1000,
        "analysis_reason": analysis_reason or "",
        "force_direction": force_direction or None,
        "is_simulated": sim_time is not None,
        "settle_status": "pending",
        "settle_attempts": 0,
        "result": None,
        "exit_price": None,
        "exit_reason": None,
        "exit_time": None,
        "pnl_pct": None,
        "pnl_amount": None,
    }

    with _history_lock:
        data = _read_history()
        data["records"].append(record)
        if len(data["records"]) > MAX_HISTORY_RECORDS:
            data["records"] = data["records"][-MAX_HISTORY_RECORDS:]
        _write_history(data)

    return record_id


def get_pending_history_for_date(date_str: str) -> List[dict]:
    """取得指定日期中，尚未結算的歷史紀錄（供收盤後結算執行緒使用）。"""
    with _history_lock:
        data = _read_history()
        return [
            r for r in data.get("records", [])
            if r.get("date") == date_str and r.get("settle_status") == "pending"
        ]


def get_stale_pending_history(exclude_date: str) -> List[dict]:
    """
    取得「非指定日期（通常是今天）」的所有待結算歷史紀錄（v15新增）。

    背景：如果 App 在收盤前到隔天開盤這段時間完全沒開機，那天的
    「待結算」紀錄的 date 欄位仍是那一天，但收盤結算迴圈重新啟動後
    只會用「今天」的日期去查 get_pending_history_for_date()，永遠查
    不到這批「昨天以前」的舊紀錄，導致它們永久卡在 pending。

    這個函式讓補結算迴圈能找出這些被遺漏的舊紀錄，交給
    fugle_service.get_historical_intraday_candles() 用指定日期查詢
    當天的完整分K線回放結算，而不是讓它們一直卡住或改用不精確的
    日K收盤價估算。已標記為 stale_unresolvable（見
    mark_settle_attempt_failed）的紀錄不會再被回傳，避免無限重試。
    """
    with _history_lock:
        data = _read_history()
        return [
            r for r in data.get("records", [])
            if r.get("date") != exclude_date and r.get("settle_status") == "pending"
        ]


MAX_SETTLE_ATTEMPTS = 5  # 補結算查無資料的重試上限，超過就放棄避免無限耗用API


def mark_settle_attempt_failed(record_id: str) -> None:
    """
    補結算時查無該日分K資料，記錄一次失敗嘗試。連續失敗次數達到
    MAX_SETTLE_ATTEMPTS 後，標記為 stale_unresolvable，不再嘗試
    （可能是資料商尚未收錄該日、股票已下市、或超出分K歷史資料
    起始日 2023-05-23 等情況），避免每輪都重複對同一批資料發送
    注定失敗的請求。使用者仍可在歷史紀錄頁面看到這筆紀錄，
    但賺賠會顯示為「無法結算」而非強制用不精確的資料硬湊結果。
    """
    with _history_lock:
        data = _read_history()
        for r in data.get("records", []):
            if r.get("id") == record_id:
                r["settle_attempts"] = r.get("settle_attempts", 0) + 1
                if r["settle_attempts"] >= MAX_SETTLE_ATTEMPTS:
                    r["settle_status"] = "stale_unresolvable"
                break
        _write_history(data)


def _compute_settle_result(target: dict, day_candles: List[dict]) -> Optional[dict]:
    """
    純計算函式：依「當天完整分K線」回放判斷一筆歷史紀錄該如何結算，
    不碰資料檔讀寫、不檢查 settle_status。被
    settle_history_record_with_candles()（首次結算，僅限pending）與
    resettle_history_record_with_candles()（強制重新結算，供單筆
    「重新確認」按鈕使用，可覆寫已結算結果）共用，確保兩邊用的是
    完全同一套判斷規則，不會出現「重新整理後用了不同邏輯」的落差。

    判斷規則（依時間順序掃描每根K棒）：
      【修正】只掃描「建倉時間之後（含建倉當下那一分鐘）」的K棒。
      原本的版本會直接從當天開盤（09:00）第一根K棒開始掃描，完全
      沒有考慮這筆紀錄實際的建倉時間——例如某筆是 09:25 才進場，
      舊邏輯卻可能因為 09:00 那根K棒的high/low剛好碰到停損/停利
      價位，就把它誤判成「09:00觸及停損」，但建倉前根本還沒有部位，
      不可能被停損。這裡改成先用target["time"]（建倉時間，格式
      HH:MM:SS）過濾出建倉之後的K棒，只拿這些去比對，才是「進場後
      實際發生了什麼」的正確回放。

      做多（entry做多方向）：
        - 若某根K棒的 high ≥ take_profit → 在該根觸及停利，win
        - 若某根K棒的 low  ≤ stop_loss   → 在該根觸及停損，loss
        - 同一根K棒兩者都可能觸及時，保守假設「先觸及對己不利的停損」
          （無法得知盤中價格路徑的精確順序，寧可保守估計）
        - 建倉後都沒觸及 → 用當天最後一根K棒收盤價強制平倉，依損益
          正負判定
      放空方向邏輯對稱（low看停利、high看停損）。

    day_candles 需為時間正序排列的K棒列表，每筆至少含 high/low/close/time
    （time 格式為 HH:MM）。回傳計算出的欄位 dict，資料不足時回傳 None。
    """
    entry  = target.get("entry_price")
    sl     = target.get("stop_loss")
    tp     = target.get("take_profit")
    is_long = target.get("signal") in ("BUY", "STRONG_BUY")

    if not entry or not day_candles:
        return None

    # 【修正】依建倉時間過濾K棒，只保留建倉當下那一分鐘及之後的資料。
    # target["time"] 格式為 "HH:MM:SS"（見add_history_record），K棒的
    # time 是 "HH:MM"（分鐘精度）；取前5碼比對「所屬分鐘」，確保建倉
    # 當下那根K棒（可能建倉發生在該分鐘中途）也會被納入計算，不會漏掉
    # 建倉瞬間到那根K棒結束前的價格波動。若找不到建倉時間或格式異常，
    # 為安全起見退回使用完整當天K棒（等同原本行為），避免因為缺欄位
    # 而讓這筆紀錄完全無法結算。
    entry_time_str = target.get("time")
    scan_candles = day_candles
    if entry_time_str and len(entry_time_str) >= 5:
        entry_minute = entry_time_str[:5]  # "HH:MM"
        filtered = [c for c in day_candles if (c.get("time") or "") >= entry_minute]
        if filtered:
            scan_candles = filtered

    exit_price  = None
    exit_reason = None
    exit_time   = None

    for c in scan_candles:
        high = c.get("high")
        low  = c.get("low")
        if high is None or low is None:
            continue

        if is_long:
            hit_sl = sl is not None and low <= sl
            hit_tp = tp is not None and high >= tp
            if hit_sl and hit_tp:
                # 同根K棒內兩者都可能觸及，保守判定為先觸及停損
                exit_price, exit_reason = sl, "hit_sl"
            elif hit_sl:
                exit_price, exit_reason = sl, "hit_sl"
            elif hit_tp:
                exit_price, exit_reason = tp, "hit_tp"
        else:
            hit_sl = sl is not None and high >= sl
            hit_tp = tp is not None and low <= tp
            if hit_sl and hit_tp:
                exit_price, exit_reason = sl, "hit_sl"
            elif hit_sl:
                exit_price, exit_reason = sl, "hit_sl"
            elif hit_tp:
                exit_price, exit_reason = tp, "hit_tp"

        if exit_reason:
            exit_time = c.get("time")
            break

    if exit_reason is None:
        # 建倉後都沒觸及停利/停損 → 用當天最後一根K棒收盤價強制平倉
        last = scan_candles[-1]
        exit_price  = last.get("close", entry)
        exit_reason = "forced_close"
        exit_time   = last.get("time")

    pnl_pct = (
        (exit_price - entry) / entry * 100 if is_long
        else (entry - exit_price) / entry * 100
    )

    if pnl_pct > 0.01:
        result = "win"
    elif pnl_pct < -0.01:
        result = "loss"
    else:
        result = "breakeven"

    # 【v16新增】依這筆紀錄當時的股數快照，計算實際損益金額。
    # 只計算「進出場價差 × 股數」的毛損益，不扣除手續費與證交稅——
    # 這兩項費用依券商方案、放空/做多而異，若自行估算一個固定費率
    # 硬套用，反而可能讓數字失真，不如誠實只呈現price-based的毛額，
    # 使用者自己心裡有數這還沒扣手續費。
    shares = target.get("shares", 1000)
    pnl_amount = (
        (exit_price - entry) * shares if is_long
        else (entry - exit_price) * shares
    )

    return {
        "settle_status": "settled",
        "result":        result,
        "exit_price":    float(exit_price),
        "exit_reason":   exit_reason,
        "exit_time":     exit_time,
        "pnl_pct":       round(pnl_pct, 3),
        "pnl_amount":    round(pnl_amount, 0),
    }


def settle_history_record_with_candles(record_id: str, day_candles: List[dict]) -> bool:
    """
    首次結算（僅限 settle_status == "pending" 的紀錄）。供背景排程
    _run_history_settle_once() 使用，避免自動排程誤觸已結算過的紀錄。
    若需要對「已結算」的紀錄強制重新計算（例如使用者發現結果看起來
    不對，用「重新確認」按鈕重抓資料驗證），請改用
    resettle_history_record_with_candles()。

    回傳 True 代表結算成功並已寫回，False 代表資料不足、找不到該筆
    記錄，或該筆記錄已經不是 pending 狀態。
    """
    with _history_lock:
        data = _read_history()
        target = None
        for r in data.get("records", []):
            if r.get("id") == record_id:
                target = r
                break
        if target is None or target.get("settle_status") != "pending":
            return False

        computed = _compute_settle_result(target, day_candles)
        if computed is None:
            return False

        target.update(computed)
        _write_history(data)
        return True


def resettle_history_record_with_candles(record_id: str, day_candles: List[dict]) -> bool:
    """
    【v19新增】強制重新結算，不限定原本的 settle_status。

    背景：settle_history_record_with_candles() 刻意只處理 pending
    紀錄，是為了讓背景自動排程不會重複觸碰已結算過的資料。但這也
    表示——若某筆「已結算成功」的紀錄其實算錯了（例如當初抓到的
    K線資料不完整、快取到有問題的舊資料等），先前的單筆「重新整理」
    即使重新抓到了正確的完整K線，呼叫舊函式也會因為狀態檢查而直接
    被拒絕，畫面上舊的錯誤結果就會被誤留著，看起來就像「明明有抓到
    分K、卻還是沿用收盤價強制平倉的舊結果」。

    這個函式就是解決這個落差：只要有 record_id 對應的紀錄、且
    day_candles 資料足夠，不論原本是 pending、settled 還是
    stale_unresolvable，一律用新資料重新跑一次
    _compute_settle_result() 並覆寫寫回，同時清掉
    stale_unresolvable 狀態遺留的 settle_attempts 計數，讓這筆紀錄
    回到乾淨的已結算狀態。

    回傳 True 代表重新結算成功並已寫回，False 代表資料不足或找不到
    該筆記錄。
    """
    with _history_lock:
        data = _read_history()
        target = None
        for r in data.get("records", []):
            if r.get("id") == record_id:
                target = r
                break
        if target is None:
            return False

        computed = _compute_settle_result(target, day_candles)
        if computed is None:
            return False

        target.update(computed)
        # 重新結算成功，清掉舊的補結算失敗計數，避免殘留的
        # settle_attempts / stale_unresolvable 痕跡誤導後續判斷
        target.pop("settle_attempts", None)

        _write_history(data)
        return True


def list_history(
    symbol: Optional[str] = None,
    result_filter: Optional[str] = None,
    days: int = 90,
) -> List[dict]:
    """
    取得歷史紀錄清單，供 UI 顯示，依時間新到舊排序。
    result_filter：None（不篩）| "win"（只看賺錢）| "loss"（只看賠錢）
    """
    cutoff_date = (datetime.now() - _timedelta_days(days)).strftime("%Y-%m-%d")
    with _history_lock:
        data = _read_history()
        records = list(data.get("records", []))

    records = [r for r in records if r.get("date", "") >= cutoff_date]
    if symbol:
        records = [r for r in records if r.get("symbol") == symbol]
    if result_filter in ("win", "loss", "breakeven"):
        records = [r for r in records if r.get("result") == result_filter]

    records.sort(key=lambda r: (r.get("date", ""), r.get("time", "")), reverse=True)
    return records


# ═════════════════════════════════════════════════════════════
#  收盤結算用：當天完整1分K持久化快取（v18新增）
# ═════════════════════════════════════════════════════════════
#
# 背景：
#   原本收盤結算是在逐一股票分組時，臨時呼叫 get_intraday_candles()
#   即時抓一次K線，沒有明確的「先盤點今天有哪些股票要結算、統一批次
#   抓好、存成快取」這道步驟。這導致兩個問題：
#     1. 若同一支股票當天有多筆待結算紀錄，理論上只需要抓一次K線，
#        但沒有持久化快取時，一旦執行緒重跑（例如App重啟），
#        又得重新對同一支股票發一次API請求。
#     2. 抓取失敗時（斷網、逾時、API暫時異常）沒有任何記錄或提示，
#        只是靜默 continue 跳過，經驗上會讓人「感覺」結算結果不準確
#        或使用了不完整的資料，卻無從察覺、也無法重試。
#
# 設計：
#   1. settle_intraday_candles.json 存放「日期+股票代碼」為 key 的
#      完整1分K線快照，是結算流程專用的持久化快取，跟即時報價快取
#      是分開的兩回事。
#   2. 收盤結算流程改為兩階段：
#      階段一（batch_fetch_settle_candles）：盤點今天所有待結算紀錄
#      涉及的股票清單，逐一呼叫富果API抓「當天完整1分K」，成功的
#      存入這個快取檔；失敗的記錄下錯誤原因，供之後重試或UI顯示。
#      階段二：逐筆紀錄從快取讀資料回放結算，不再各自臨時發API。
#   3. 快取檔案只留最近5天的資料，每次讀寫前自動清除超過5天的
#      舊項目，避免無限增長佔用手機儲存空間。
#
# 資料結構：
# {
#   "candles": {
#     "2330_2026-09-05": {
#       "fetched_at": "2026-09-05 13:26:10",
#       "candles": [{"time":"09:01","full_time":"...","open":...,...}, ...]
#     }, ...
#   },
#   "fetch_errors": {
#     "2454_2026-09-05": {
#       "last_attempt": "2026-09-05 13:26:12",
#       "error": "網路連線失敗",
#       "attempts": 2
#     }, ...
#   }
# }
#
SETTLE_CANDLES_FILE  = str(_BASE_DIR / "settle_intraday_candles.json")
_settle_candles_lock  = threading.Lock()
SETTLE_CANDLES_MAX_AGE_DAYS = 5  # 快取只保留最近5天，避免佔用手機儲存空間


def _read_settle_candles() -> dict:
    if os.path.exists(SETTLE_CANDLES_FILE):
        try:
            with open(SETTLE_CANDLES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"candles": {}, "fetch_errors": {}}


def _write_settle_candles(data: dict):
    try:
        with open(SETTLE_CANDLES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Settle Candles Cache] 寫入失敗: {e}")


def _purge_stale_settle_candles(data: dict) -> dict:
    """
    移除「日期+股票」快取鍵中，日期超過 SETTLE_CANDLES_MAX_AGE_DAYS
    天前的項目。key 格式為 "{symbol}_{yyyy-MM-dd}"，從尾端切出日期
    字串來判斷，避免依賴額外欄位、快取結構越單純越不容易壞掉。
    """
    cutoff = (datetime.now() - _timedelta_days(SETTLE_CANDLES_MAX_AGE_DAYS)).strftime("%Y-%m-%d")

    def _extract_date(key: str) -> str:
        # key 形如 "2330_2026-09-05"，日期固定在最後10碼
        return key[-10:] if len(key) >= 10 else ""

    for bucket_name in ("candles", "fetch_errors"):
        bucket = data.get(bucket_name, {})
        stale_keys = [k for k in bucket if _extract_date(k) < cutoff]
        for k in stale_keys:
            del bucket[k]
        data[bucket_name] = bucket

    return data


def get_cached_settle_candles(symbol: str, date_str: str) -> Optional[List[dict]]:
    """
    讀取「指定股票+日期」的已快取完整1分K線。找不到快取時回傳 None
    （呼叫端應改用 fetch_and_cache_settle_candles 實際去抓一次）。
    """
    key = f"{symbol}_{date_str}"
    with _settle_candles_lock:
        data = _read_settle_candles()
        entry = data.get("candles", {}).get(key)
        return entry.get("candles") if entry else None


def save_settle_candles(symbol: str, date_str: str, candles: List[dict]) -> None:
    """
    寫入「指定股票+日期」的完整1分K線快取，同時清除該股票在
    fetch_errors 中的失敗記錄（這次已經成功了）。每次寫入前
    順便清理超過5天的舊快取，讓清理動作跟寫入操作綁在一起，
    不需要另外排程一個定時清理的背景執行緒。
    """
    key = f"{symbol}_{date_str}"
    with _settle_candles_lock:
        data = _read_settle_candles()
        data = _purge_stale_settle_candles(data)
        data.setdefault("candles", {})[key] = {
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "candles": candles,
        }
        data.get("fetch_errors", {}).pop(key, None)
        _write_settle_candles(data)


def mark_settle_candles_fetch_error(symbol: str, date_str: str, error_msg: str) -> None:
    """
    記錄一次「抓取當天分K失敗」的錯誤（例如斷網、API逾時），
    讓使用者能在歷史紀錄頁面看到「這筆不是真的沒資料，是抓取失敗」，
    並可透過每筆紀錄的「重新整理」按鈕手動重試，而不是誤以為
    系統已經正確結算、卻是用不完整的資料算出來的。
    """
    key = f"{symbol}_{date_str}"
    with _settle_candles_lock:
        data = _read_settle_candles()
        data = _purge_stale_settle_candles(data)
        errors = data.setdefault("fetch_errors", {})
        prev_attempts = errors.get(key, {}).get("attempts", 0)
        errors[key] = {
            "last_attempt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "error": str(error_msg)[:200],
            "attempts": prev_attempts + 1,
        }
        _write_settle_candles(data)


def get_settle_fetch_error(symbol: str, date_str: str) -> Optional[dict]:
    """查詢「指定股票+日期」上次抓取分K是否有失敗記錄，供UI顯示提示。"""
    key = f"{symbol}_{date_str}"
    with _settle_candles_lock:
        data = _read_settle_candles()
        return data.get("fetch_errors", {}).get(key)


def clear_settle_candles_cache(symbol: str, date_str: str) -> None:
    """
    手動清除「指定股票+日期」的快取（含錯誤記錄），供「重新整理」
    按鈕使用：先清掉舊快取（不管是成功的舊資料還是失敗記錄），
    強制下次結算時重新呼叫API抓取，避免沿用可能不完整的舊資料。
    """
    key = f"{symbol}_{date_str}"
    with _settle_candles_lock:
        data = _read_settle_candles()
        data.get("candles", {}).pop(key, None)
        data.get("fetch_errors", {}).pop(key, None)
        _write_settle_candles(data)


def export_history_json(
    output_path: Optional[str] = None,
    symbol: Optional[str] = None,
    days: int = 365,
) -> Optional[str]:
    """
    將分析歷史完整匯出成 JSON 檔（v16新增），供使用者收集一段時間後
    提供給AI檢討分析品質、尋找可優化之處使用。

    刻意匯出「全部」紀錄（含pending與stale_unresolvable），不像
    list_history()的UI清單只顯示90天內、也不做賺賠篩選——因為這裡的
    目的是給AI做整體分析品質的檢討，篩選掉的資料可能剛好是AI該檢討
    的樣本（例如大量停在pending代表結算機制可能有問題、大量
    stale_unresolvable代表股票代碼或資料源有問題），讓AI自己判斷
    比事先篩選更完整。

    輸出結構包含 summary（整體統計，方便AI快速掌握全貌）跟
    records（完整原始紀錄陣列，含每筆的analysis_reason完整原文）。

    output_path 未指定時，預設輸出到 App 資料目錄下的
    analysis_history_export_{timestamp}.json。

    回傳實際寫入的檔案路徑；寫入失敗回傳 None。
    """
    with _history_lock:
        data = _read_history()
        records = list(data.get("records", []))

    cutoff_date = (datetime.now() - _timedelta_days(days)).strftime("%Y-%m-%d")
    records = [r for r in records if r.get("date", "") >= cutoff_date]
    if symbol:
        records = [r for r in records if r.get("symbol") == symbol]

    records.sort(key=lambda r: (r.get("date", ""), r.get("time", "")))

    settled = [r for r in records if r.get("settle_status") == "settled"]
    summary = {
        "export_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date_range_days": days,
        "symbol_filter": symbol or "全部股票",
        "total_records": len(records),
        "settled_count": len(settled),
        "pending_count": sum(1 for r in records if r.get("settle_status") == "pending"),
        "unresolvable_count": sum(1 for r in records if r.get("settle_status") == "stale_unresolvable"),
        "win_count": sum(1 for r in settled if r.get("result") == "win"),
        "loss_count": sum(1 for r in settled if r.get("result") == "loss"),
        "breakeven_count": sum(1 for r in settled if r.get("result") == "breakeven"),
        "total_pnl_amount": round(sum(r.get("pnl_amount") or 0 for r in settled), 0),
        "win_rate": (
            round(sum(1 for r in settled if r.get("result") == "win") / len(settled), 3)
            if settled else None
        ),
    }

    export_data = {"summary": summary, "records": records}

    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(_BASE_DIR / f"analysis_history_export_{ts}.json")

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)
        return output_path
    except Exception as e:
        print(f"[Analysis History] 匯出失敗: {e}")
        return None


def get_history_stats(symbol: Optional[str] = None, days: int = 30) -> dict:
    """
    統計「已結算」的分析歷史，計算實際命中率。這是 v15 起唯一的
    成效驗證資料源（原本 signal_track 的 30 分鐘單點驗證機制已移除，
    因其容易抓到誤導性的瞬間值，且跟本模組記錄幾乎同一份資料）。

    回傳：{"total": 已結算筆數, "win": 贏, "loss": 輸, "breakeven": 打平,
           "pending": 待結算筆數, "win_rate": 0~1之間的浮點數或None}
    win_rate 分母只計算已結算（settled）的紀錄，pending 中尚未收盤
    結算的不列入計算，避免虛胖或壓低命中率。
    """
    cutoff_date = (datetime.now() - _timedelta_days(days)).strftime("%Y-%m-%d")
    with _history_lock:
        data = _read_history()
        records = list(data.get("records", []))

    records = [r for r in records if r.get("date", "") >= cutoff_date]
    if symbol:
        records = [r for r in records if r.get("symbol") == symbol]

    stats = {
        "total": 0, "win": 0, "loss": 0, "breakeven": 0,
        "pending": 0, "unresolvable": 0, "win_rate": None,
    }
    for r in records:
        status = r.get("settle_status")
        if status == "stale_unresolvable":
            # 補結算多次仍查無該日分K資料（可能資料商未收錄、股票已
            # 下市、或早於分K歷史資料起始日），已放棄自動結算，
            # 跟「still pending」意義不同，獨立分類避免誤導。
            stats["unresolvable"] += 1
            continue
        if status != "settled":
            stats["pending"] += 1
            continue
        stats["total"] += 1
        result = r.get("result")
        if result in ("win", "loss", "breakeven"):
            stats[result] += 1

    if stats["total"] > 0:
        stats["win_rate"] = round(stats["win"] / stats["total"], 3)

    return stats


def delete_history_records(ids: List[str]) -> int:
    """依 id 清單刪除歷史紀錄，回傳實際刪除的筆數。"""
    if not ids:
        return 0
    id_set = set(ids)
    with _history_lock:
        data = _read_history()
        before = len(data.get("records", []))
        data["records"] = [r for r in data.get("records", []) if r.get("id") not in id_set]
        after = len(data["records"])
        _write_history(data)
        return before - after


# ─────────────────────────────────────────────
#  全域單例
# ─────────────────────────────────────────────
_cache = CacheService()

def get_cache() -> CacheService:
    return _cache