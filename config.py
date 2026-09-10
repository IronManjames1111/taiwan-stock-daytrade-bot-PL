import json
import os
from pathlib import Path

# ── 取得持久化儲存目錄 ──────────────────────────────────────
_storage_data = os.getenv("FLET_APP_STORAGE_DATA")
if _storage_data:
    _BASE_DIR = Path(_storage_data)
else:
    # 本地開發時 fallback 到腳本所在目錄
    _BASE_DIR = Path(__file__).parent

CONFIG_FILE = str(_BASE_DIR / "config.json")

DEFAULT_CONFIG = {
    "fugle_api_key":    "",
    "gemini_api_key":   "",
    "watchlist":        ["2330", "2317", "2454"],
    # ── ws_symbols 預設為空列表 ────────────────────────────────────────
    # 由使用者手動開啟 WS 開關（或 app.py 按上限自動取前幾檔）。
    "ws_symbols":       [],
    "refresh_interval": 10,
    # ── API 節流設定（v8 新增）────────────────────────────────────────
    # 全域每分鐘最多可送出的富果 API 請求數（含主頁面輪詢＋詳細頁載入）。
    # 建議設為你方案額度的 80~90%，保留安全緩衝。
    # 例如方案上限 60/分鐘，建議設 50。
    "api_rate_limit":   50,
    # ── 分析歷史結算設定（v14 新增）───────────────────────────────
    # 每筆「分析歷史」紀錄在此時間之後，若整天都沒觸及停利/停損，
    # 會用最後一根K棒（收盤價）強制平倉計算損益，對應當沖「當日
    # 沖銷、不留倉」的實際紀律。預設收盤前5分鐘（13:30收盤）。
    "history_settle_time": "13:25",
    "gemini_model":     "gemini-2.0-flash",
    "ai_auto_interval": 0,   # 0=手動, 其他=分鐘數
    # ── WS 模式設定 ───────────────────────────────────────────────────
    # "full"        = trades + books（五檔委買委賣），最多 2 檔股票
    # "trades_only" = 只訂 trades（無五檔資料），最多 5 檔股票
    "ws_mode":          "full",
    # 日K與AI歷史天數設定
    "daily_history_display_days": 240,
    "ai_daily_days": 90,
    # ── 風險模式設定（v5 新增）────────────────────────────────────────
    # "aggressive"  = 高風險高暴利：停損大、目標遠、積極進場
    # "conservative"= 低風險低獲利：停損近、目標保守、謹慎進場
    # "auto"        = 自動判斷：AI 依日線支撐/壓力位自動決定建議點
    "risk_mode":        "auto",
    # ── 券商手續費折扣設定（v6 新增）──────────────────────────────────
    # 手續費費率：0.1425%（雙向，買進與賣出各收一次）
    # 折扣範圍：0.1 ~ 1.0，例如 6 折 = 0.6，無折扣 = 1.0
    # 證交稅：賣出時收 0.3%（ETF 為 0.1%，當沖減半 = 0.15%）
    # 當沖證交稅折半（2024 年底前有效）：0.15%
    "broker_discount":      0.28,   # 手續費折扣（如 6 折 = 0.6，2.8 折 = 0.28）
    "trade_shares":         1000,   # 預設交易張數（1 張 = 1000 股）
    "is_day_trade_tax":     True,   # 是否適用當沖證交稅減半（0.15%）
    "force_direction": None, #強制買賣按鈕
    # ── AI 分析精簡模式（v11 新增）────────────────────────────────────
    # False = 完整模式：40根K棒+完整日K+所有指標，AI 回應 800 tokens（手動分析預設）
    # True  = 精簡模式：20根K棒+無日K逐行，AI 回應 300 tokens（速度快，自動分析用）
    # 設定頁可強制全部走完整模式；不設定時自動/手動分開（自動=精簡，手動=完整）
    "concise_mode": False,

    # ── 純策略模式設定（新增） ───────────────────────────────────────
    "strategy_mode": False,        # 是否啟用純策略模式
    "strategy_direction": "long",  # "long" = 做多, "short" = 做空
    "strategy_buy_cond": "any",    # 買入組合邏輯："any" (OR) / "all" (AND)
    "strategy_sell_cond": "any",   # 賣出組合邏輯："any" (OR) / "all" (AND)
    "strategies_buy": {            # 買入啟用哪些指標
        "ma_crossover": True,
        "kd": False,
        "macd": False,
        "rsi": False,
        "bollinger": False,
        "obv": False
    },
    "strategies_sell": {           # 賣出啟用哪些指標
        "ma_crossover": True,
        "kd": False,
        "macd": False,
        "rsi": False,
        "bollinger": False,
        "obv": False
    },
    "strategy_params": {           # 技術指標共享參數設定
        "ma_crossover": {
            "fast_period": 5,
            "slow_period": 20
        },
        "kd": {
            "k_period": 9,
            "d_period": 3,
            "overbought": 80,
            "oversold": 20
        },
        "macd": {
            "fast_period": 12,
            "slow_period": 26,
            "signal_period": 9
        },
        "rsi": {
            "period": 14,
            "overbought": 70,
            "oversold": 30
        },
        "bollinger": {
            "period": 20,
            "std_dev": 2.0
        },
        "obv": {
            "ma_period": 10
        }
    },

    # ── 自選股群組（新增）────────────────────────────────────────────
    # 群組結構：{ "群組名稱": ["2330", "2317", ...], ... }
    # active_group: 目前顯示 the 群組名稱，None 表示使用全域 watchlist（不分組）
    "groups": {},
    "active_group": None,
}

def _migrate_config(cfg: dict) -> dict:
    """
    自動遷移舊版 config 格式到新版，避免 KeyError。
    v2 → v3：strategy_condition + strategies → strategies_buy/sell/params + direction
    """
    # ── 遷移舊版策略設定 ─────────────────────────────────────────
    if "strategies" in cfg and "strategies_buy" not in cfg:
        old_st = cfg.get("strategies", {})
        cfg["strategies_buy"] = {
            k: old_st.get(k, {}).get("enabled", k == "ma_crossover")
            for k in ["ma_crossover", "kd", "macd", "rsi", "bollinger", "obv"]
        }
        cfg["strategies_sell"] = {
            k: old_st.get(k, {}).get("enabled", k == "ma_crossover")
            for k in ["ma_crossover", "kd", "macd", "rsi", "bollinger", "obv"]
        }
        cfg["strategy_params"] = {
            "ma_crossover": {
                "fast_period": old_st.get("ma_crossover", {}).get("fast_period", 5),
                "slow_period": old_st.get("ma_crossover", {}).get("slow_period", 20),
            },
            "kd": {
                "k_period":   old_st.get("kd", {}).get("k_period", 9),
                "d_period":   old_st.get("kd", {}).get("d_period", 3),
                "overbought": old_st.get("kd", {}).get("overbought", 80),
                "oversold":   old_st.get("kd", {}).get("oversold", 20),
            },
            "macd": {
                "fast_period":   old_st.get("macd", {}).get("fast_period", 12),
                "slow_period":   old_st.get("macd", {}).get("slow_period", 26),
                "signal_period": old_st.get("macd", {}).get("signal_period", 9),
            },
            "rsi": {
                "period":     old_st.get("rsi", {}).get("period", 14),
                "overbought": old_st.get("rsi", {}).get("overbought", 70),
                "oversold":   old_st.get("rsi", {}).get("oversold", 30),
            },
            "bollinger": {
                "period":  old_st.get("bollinger", {}).get("period", 20),
                "std_dev": old_st.get("bollinger", {}).get("std_dev", 2.0),
            },
            "obv": {
                "ma_period": old_st.get("obv", {}).get("ma_period", 10),
            },
        }
        # 移除舊版鍵
        cfg.pop("strategies", None)
        cfg.pop("strategy_condition", None)

    # ── 補充新增的頂層鍵（如沒有 strategy_direction 等）──────────────
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v
        elif isinstance(v, dict) and isinstance(cfg.get(k), dict):
            # 深度補充：確保巢狀 dict 的子鍵也補齊
            for sk, sv in v.items():
                if sk not in cfg[k]:
                    cfg[k][sk] = sv
    return cfg


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                cfg = _migrate_config(cfg)
                return cfg
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(config: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)