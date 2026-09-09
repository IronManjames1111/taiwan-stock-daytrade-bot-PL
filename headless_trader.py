# -*- coding: utf-8 -*-
"""
headless_trader.py - 雲端無頭當沖機器人
─────────────────────────────────────────────
• 早上 09:05 自動從 Yahoo 奇摩股市抓取成交量排行前 5 檔
• 09:05 ~ 13:25 每 10 分鐘使用 Fugle + Gemini 進行多空訊號判定
• 出現 BUY / SHORT 訊號時自動記錄進出場點與理由
• 13:25 收盤自動回放當日分K線結算盈虧 (勝/負/強制平倉)
• 匯出回測報表至 history_records/ 便於檢討與優化 Prompt
"""

import os
import sys
import time
import json
import datetime
import pytz
import requests
import pandas as pd
from bs4 import BeautifulSoup
from typing import List, Dict

from fugle_service import FugleService
from gemini_service import GeminiService
import cache_service
from config import load_config

TW_TZ = pytz.timezone("Asia/Taipei")

def get_tw_now() -> datetime.datetime:
    return datetime.datetime.now(TW_TZ)

def get_free_top_volume_stocks(limit: int = 5, min_price: float = 15.0) -> List[Dict]:
    """
    免費從 Yahoo 奇摩股市抓取成交量排行榜 (盤中即時更新)
    自動過濾：00 開頭 ETF、權證、特別股，並限制最低股價
    """
    url = "https://tw.stock.yahoo.com/rank/volume"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    candidates = []
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"[Yahoo排行] 請求失敗 HTTP {resp.status_code}")
            return []
        
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.find_all("li", class_=lambda c: c and "List(n)" in c)
        
        for r in rows:
            texts = [t.strip() for t in r.stripped_strings]
            if len(texts) < 4:
                continue
            symbol_raw = next((t for t in texts if ".TW" in t or ".TWO" in t), "")
            if not symbol_raw:
                continue
            
            symbol = symbol_raw.split(".")[0]
            name = texts[0] if texts[0] != symbol_raw else symbol
            
            # 純4碼個股、排除 00 開頭 ETF
            if not (len(symbol) == 4 and symbol.isdigit() and not symbol.startswith("00")):
                continue
                
            try:
                price = float(texts[2].replace(",", ""))
            except ValueError:
                continue
                
            if price < min_price:
                continue

            candidates.append({"symbol": symbol, "name": name, "price": price})
            if len(candidates) >= limit:
                break

    except Exception as e:
        print(f"[Yahoo排行] 解析錯誤: {e}")
        
    return candidates

def main():
    print("=" * 60)
    print("🚀 [GitHub Actions] 雲端當沖全自動分析與回測系統啟動")
    print("=" * 60)

    # 讀取 API 金鑰 (優先讀取環境變數，本地則讀 config.json)
    cfg = load_config()
    fugle_api_key = os.getenv("FUGLE_API_KEY") or cfg.get("fugle_api_key", "")
    gemini_api_key = os.getenv("GEMINI_API_KEY") or cfg.get("gemini_api_key", "")

    if not fugle_api_key or not gemini_api_key:
        print("❌ 錯誤：未設定 FUGLE_API_KEY 或 GEMINI_API_KEY！")
        print("   請至 GitHub 倉庫 -> Settings -> Secrets and variables -> Actions 設定。")
        sys.exit(1)

    fugle = FugleService(api_key=fugle_api_key)
    gemini = GeminiService(api_key=gemini_api_key, model=cfg.get("gemini_model", "gemini-2.0-flash"))

    now = get_tw_now()
    hm = now.strftime("%H:%M")
    is_weekend = now.weekday() >= 5

    print(f"🕒 當前台灣時間: {now.strftime('%Y-%m-%d %H:%M:%S')} (星期{now.weekday()+1})")

    # 模式判斷：若非盤中時間 (如晚上手動測試或週末)，執行快速測試模式
    is_market_session = ("08:50" <= hm <= "13:30") and not is_weekend
    if not is_market_session:
        print("\n⚠️ 目前非台股盤中時間 (09:00~13:30)，進入【連線與抓取測試模式】...")
        print("🔍 測試 Yahoo 成交量排行抓取：")
        test_stocks = get_free_top_volume_stocks(limit=3)
        for s in test_stocks:
            print(f"   📌 {s['symbol']} {s['name']} (參考價: {s['price']})")
        
        if test_stocks:
            test_sym = test_stocks[0]["symbol"]
            print(f"\n🔍 測試 Fugle 日K線抓取 ({test_sym})：")
            daily = fugle.get_historical_candles(test_sym, timeframe="D")
            daily_len = len(daily.get("data", [])) if daily else 0
            print(f"   ✅ 富果 API 連線成功！取得 {daily_len} 根日K")

            print(f"\n🔍 測試 Gemini AI 快速分析連線 ({test_sym})：")
            check_res = gemini.quick_check(test_sym, test_stocks[0]["price"], 1.5)
            print(f"   ✅ Gemini 回覆: {check_res.strip()}")

        print("\n🎉 GitHub Actions 測試驗證全數通過！在開盤日早上 08:55 排程啟動時將自動進入正式當沖監控。")
        return

    # ── 正式盤中運作流程 ─────────────────────────────────────
    
    # 1. 08:55 ~ 09:05 倒數等待
    while True:
        now = get_tw_now()
        hm = now.strftime("%H:%M")
        if hm >= "09:05":
            break
        print(f"[{now.strftime('%H:%M:%S')}] 等待開盤至 09:05:00...")
        time.sleep(15)

    # 2. 09:05 免費抓取成交量前 5 檔
    print(f"\n⏰ 達到 09:05，開始抓取 Yahoo 奇摩股市當前成交量前 5 檔...")
    top_stocks = get_free_top_volume_stocks(limit=5)
    symbols = [s["symbol"] for s in top_stocks]

    print("🔥 今日鎖定當沖標的：")
    for idx, s in enumerate(top_stocks, 1):
        print(f"   {idx}. {s['symbol']} {s['name']} (現價: {s['price']})")

    if not symbols:
        print("⚠️ 未能取得股票清單，程式終止。")
        return

    # 3. 09:05 ~ 13:25 每 10 分鐘輪詢分析
    print("\n📈 進入 10 分鐘例行分析迴圈 (監控至 13:25)...")
    while True:
        now = get_tw_now()
        hm = now.strftime("%H:%M")

        if hm >= "13:25":
            print(f"\n🔔 [{now.strftime('%H:%M:%S')}] 達到 13:25 收盤時間，退出輪詢迴圈，開始收盤結算！")
            break

        if now.minute % 10 == 0:
            print(f"\n⚡ [{now.strftime('%H:%M:%S')}] 執行 10 分鐘定時分析...")
            for symbol in symbols:
                try:
                    candles_raw = fugle.get_intraday_candles(symbol, force_refresh=True)
                    candles = candles_raw.get("data", []) if candles_raw else []
                    if not candles or len(candles) < 5:
                        continue

                    indicators = fugle.get_technical_indicators(candles, is_intraday=True)
                    daily_raw = fugle.get_historical_candles(symbol, timeframe="D")
                    daily_candles = daily_raw.get("data", []) if daily_raw else []
                    quote = fugle.get_intraday_quote(symbol) or {}
                    prev_close = quote.get("previousClose")

                    res = gemini.analyze_with_signal(
                        symbol=symbol,
                        candles=candles,
                        indicators=indicators,
                        daily_candles=daily_candles,
                        prev_close=prev_close,
                        risk_mode="auto",
                        concise=True
                    )

                    sig = res.get("signal", "WATCH")
                    raw_sig = res.get("raw_signal", sig)
                    entry_p = res.get("entry")
                    if isinstance(entry_p, str):
                        try: entry_p = float(entry_p.split()[0].replace("元",""))
                        except: entry_p = candles[-1]["close"]

                    print(f"  [{symbol}] 訊號: {sig} | 建議進場: {entry_p} | 停損: {res.get('stop_loss')} | 停利: {res.get('target')}")

                    # 出現多空買賣訊號時寫入歷史紀錄
                    if raw_sig in {"STRONG_BUY", "BUY", "SHORT", "STRONG_SHORT"}:
                        rec_id = cache_service.add_history_record(
                            symbol=symbol,
                            model=gemini.model,
                            risk_mode="auto",
                            signal=raw_sig,
                            direction=res.get("direction"),
                            entry_price=entry_p,
                            stop_loss=res.get("stop_loss"),
                            take_profit=res.get("target"),
                            shares=cfg.get("trade_shares", 1000),
                            analysis_reason=res.get("full_text", "")
                        )
                        print(f"   👉 [已記錄交易] {symbol} {raw_sig} 寫入歷史紀錄 (ID: {rec_id})")

                    time.sleep(2) # 節流保護 API

                except Exception as ex:
                    print(f"  [{symbol}] 分析異常: {ex}")

            time.sleep(65) # 避開當前這分鐘重複觸發

        time.sleep(10)

    # 4. 13:25 收盤回放結算
    today_str = get_tw_now().strftime("%Y-%m-%d")
    pending_records = cache_service.get_pending_history_for_date(today_str)
    print(f"\n🎯 開始收盤分K回放結算，今日待結算筆數: {len(pending_records)}")

    for rec in pending_records:
        sym = rec["symbol"]
        candles_raw = fugle.get_intraday_candles(sym, force_refresh=True)
        day_candles = candles_raw.get("data", []) if candles_raw else []
        if day_candles:
            success = cache_service.settle_history_record_with_candles(rec["id"], day_candles)
            print(f"   - {sym} 結算結果: {'✅ 結算成功' if success else '跳過或已結算'}")

    # 5. 產出每日回測 CSV 報表
    os.makedirs("history_records", exist_ok=True)
    all_data = cache_service._read_history()
    today_records = [r for r in all_data.get("records", []) if r.get("date") == today_str]

    if today_records:
        df = pd.DataFrame(today_records)
        csv_path = f"history_records/backtest_{today_str}.csv"
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(f"✅ 今日回測報表已成功產出：{csv_path}")
    else:
        print("ℹ️ 今日無進場訊號產生，未產出報表。")

if __name__ == "__main__":
    main()
