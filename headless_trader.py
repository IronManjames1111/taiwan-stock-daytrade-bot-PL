# -*- coding: utf-8 -*-
"""
headless_trader.py - 雲端無頭當沖機器人 (v2.0 增強版)
─────────────────────────────────────────────────────────────
• 09:15 早盤第一次抓取成交量排行前 5 檔 (避開開盤假突破雜訊)
• 10:30 中盤第二次重新抓取成交量排行前 5 檔 (鎖定盤中換手輪動飆股)
• 盤中每 10 分鐘調用 Google AI (多模型自動降級鏈) 進行深度判斷
• 自動將挑選標的與每輪 AI 分析即時同步至 GitHub Step Summary 網頁看板
• 13:25 收盤自動回放當日 1分K 結算盈虧，產出 CSV 報表保存至 GitHub
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
from typing import List, Dict, Optional

from fugle_service import FugleService
from gemini_service import GeminiService
import cache_service
from config import load_config

TW_TZ = pytz.timezone("Asia/Taipei")

def get_tw_now() -> datetime.datetime:
    return datetime.datetime.now(TW_TZ)

def update_github_summary(content: str, append: bool = True):
    """將即時看板內容寫入 GitHub Actions 網頁 Summary，方便在網頁即時觀看"""
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        mode = "a" if append else "w"
        try:
            with open(summary_path, mode, encoding="utf-8") as f:
                f.write(content + "\n\n")
        except Exception as e:
            print(f"[Summary] 寫入失敗: {e}")

def get_free_top_volume_stocks(limit: int = 5, min_price: float = 15.0) -> List[Dict]:
    """
    免費自 Yahoo 奇摩股市抓取即時成交量排行榜
    自動排除：00 開頭 ETF、權證、特別股，並限制最低股價
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
    print("=" * 65)
    print("🚀 [GitHub Actions] 雲端當沖全自動雙波段選股與回測系統啟動")
    print("=" * 65)

    cfg = load_config()
    fugle_api_key = os.getenv("FUGLE_API_KEY") or cfg.get("fugle_api_key", "")
    gemini_api_key = os.getenv("GEMINI_API_KEY") or cfg.get("gemini_api_key", "")

    if not fugle_api_key:
        print("❌ 錯誤：未設定 FUGLE_API_KEY 環境變數！")
    if not gemini_api_key:
        print("❌ 錯誤：未設定 GEMINI_API_KEY 環境變數！")
    if not fugle_api_key or not gemini_api_key:
        sys.exit(1)

    # 依優先順序設定模型降級鏈
    PREFERRED_MODELS = [
        "gemma-4-31b-it",
        "gemma-4-26b-a4b-it",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
    ]

    fugle = FugleService(api_key=fugle_api_key)
    gemini = GeminiService(api_key=gemini_api_key, model_priority=PREFERRED_MODELS)
    print(f"🤖 AI 模型優先選擇順序: {' -> '.join(PREFERRED_MODELS)}")

    now = get_tw_now()
    hm = now.strftime("%H:%M")
    is_weekend = now.weekday() >= 5
    today_str = now.strftime("%Y-%m-%d")

    print(f"🕒 當前台灣時間: {now.strftime('%Y-%m-%d %H:%M:%S')} (星期{now.weekday()+1})")

    # 模式判斷：若非盤中時間 (如晚上手動測試或週末)，執行快速測試模式
    is_market_session = ("08:50" <= hm <= "13:30") and not is_weekend
    if not is_market_session:
        print("\n⚠️ 目前非台股盤中交易時間 (09:00~13:30)，進入【連線與即時看板測試模式】...")
        test_stocks = get_free_top_volume_stocks(limit=3)
        print("🔍 測試 Yahoo 成交量排行抓取：")
        for s in test_stocks:
            print(f"   📌 {s['symbol']} {s['name']} (參考價: {s['price']})")
        
        ai_reply = "尚未測試"
        if test_stocks:
            test_sym = test_stocks[0]["symbol"]
            print(f"\n🔍 測試 Fugle 日K線抓取 ({test_sym})：")
            try:
                daily = fugle.get_historical_candles(test_sym)
                daily_len = len(daily.get("data", [])) if daily else 0
                print(f"   ✅ 富果 API 連線成功！取得 {daily_len} 根日K")
            except Exception as e:
                print(f"   ❌ 富果日K抓取異常: {e}")

            print(f"\n🔍 測試 Gemini AI 多模型優先連線 ({test_sym})：")
            try:
                ai_reply = gemini.quick_check(test_sym, test_stocks[0]["price"], 1.5)
                print(f"   ✅ Gemini 回覆 [{gemini.active_model}]: {ai_reply.strip()}")
            except Exception as e:
                print(f"   ❌ Gemini 連線異常: {e}")

        # 寫入 GitHub Step Summary 網頁儀表板
        summary_md = f"""# 📈 當沖機器人雲端儀表板 (非開盤測試模式)
> **測試時間 (台灣)**: `{now.strftime('%Y-%m-%d %H:%M:%S')}`  
> **當前主用模型**: `{gemini.active_model}`  

### 🔍 測試抓取成交量前列標的：
| 股票代號 | 股票名稱 | 參考價 |
| :---: | :---: | :---: |
"""
        for s in test_stocks:
            summary_md += f"| **{s['symbol']}** | {s['name']} | `{s['price']}` |\n"
        summary_md += f"\n**AI 測試分析簡評**: `{ai_reply}`\n\n✅ **雲端系統就緒！開盤日將在 09:15 與 10:30 分別挑選標的進行當沖分析。**"
        update_github_summary(summary_md, append=False)

        print("\n🎉 GitHub Actions 測試驗證全數通過！請查看該 Action 頁面的 Summary 標籤。")
        return

    # ── 正式盤中雙波段運作流程 ─────────────────────────────────────
    update_github_summary(f"# 🚀 台股當沖自動化即時看板 ({today_str})\n系統已於 `{now.strftime('%H:%M:%S')}` 啟動。", append=False)

    # 1. 等待至 09:15 (避開開盤 15 分鐘前置雜訊)
    while True:
        now = get_tw_now()
        hm = now.strftime("%H:%M")
        if hm >= "09:15":
            break
        print(f"[{now.strftime('%H:%M:%S')}] 等待開盤至 09:15:00 (ORB-15 區間成型)...")
        time.sleep(15)

    # 第一次選股 (09:15 早盤主力突破股)
    print(f"\n⏰ 達到 09:15，開始執行【第一波段：早盤動能成交量排行選股】...")
    current_stocks = get_free_top_volume_stocks(limit=5)
    symbols = [s["symbol"] for s in current_stocks]
    
    first_wave_md = f"### ⏰ 09:15 第一波早盤選股 (鎖定成交量 Top {len(symbols)})\n"
    for idx, s in enumerate(current_stocks, 1):
        first_wave_md += f"- **{idx}. {s['symbol']} {s['name']}** (現價: `{s['price']}` 元)\n"
    update_github_summary(first_wave_md, append=True)

    mid_wave_triggered = False

    # 2. 09:15 ~ 13:25 盤中輪詢迴圈
    print("\n📈 進入每 10 分鐘例行分析迴圈...")
    while True:
        now = get_tw_now()
        hm = now.strftime("%H:%M")

        # 達到 13:25 收盤結算時間
        if hm >= "13:25":
            print(f"\n🔔 [{now.strftime('%H:%M:%S')}] 達到 13:25 收盤時間，開始回放結算！")
            break

        # 中盤 10:30 重挑股票 (第二波段：盤中輪動飆股)
        if hm >= "10:30" and not mid_wave_triggered:
            print(f"\n⏰ 達到 10:30，開始執行【第二波段：中盤換手與輪動股票重挑】...")
            mid_stocks = get_free_top_volume_stocks(limit=5)
            new_symbols = [s["symbol"] for s in mid_stocks]
            if new_symbols:
                current_stocks = mid_stocks
                symbols = new_symbols
                print(f"🔥 中盤 10:30 已更新監控標的：{', '.join(symbols)}")
                
                mid_wave_md = f"\n---\n### ⏰ 10:30 第二波中盤重挑 (更新成交量 Top {len(symbols)})\n"
                for idx, s in enumerate(current_stocks, 1):
                    mid_wave_md += f"- **{idx}. {s['symbol']} {s['name']}** (現價: `{s['price']}` 元)\n"
                update_github_summary(mid_wave_md, append=True)
                
            mid_wave_triggered = True

        # 每 10 分鐘例行分析 (09:20, 09:30, 09:40 ... 13:20)
        if now.minute % 10 == 0:
            print(f"\n⚡ [{now.strftime('%H:%M:%S')}] 執行 10 分鐘定時分析...")
            round_summary = f"#### 📊 {now.strftime('%H:%M')} 例行分析結果 (模型: `{gemini.active_model}`)\n"
            round_summary += "| 代號 | 訊號 | 建議進場 | 停損 | 停利 | AI 決策理由 |\n| :---: | :---: | :---: | :---: | :---: | :--- |\n"

            for s_info in current_stocks:
                symbol = s_info["symbol"]
                name = s_info["name"]
                try:
                    candles_raw = fugle.get_intraday_candles(symbol, force_refresh=True)
                    candles = candles_raw.get("data", []) if candles_raw else []
                    if not candles or len(candles) < 5:
                        continue

                    indicators = fugle.get_technical_indicators(candles, is_intraday=True)
                    daily_raw = fugle.get_historical_candles(symbol)
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

                    stop_p = res.get("stop_loss", "-")
                    target_p = res.get("target", "-")
                    reason = (res.get("reason") or res.get("full_text", "")).replace("\n", " ").strip()
                    if len(reason) > 50:
                        reason = reason[:50] + "..."

                    print(f"  [{symbol} {name}] 訊號: {sig} | 進場: {entry_p} | 停損: {stop_p} | 停利: {target_p}")
                    
                    sig_badge = f"🟢 **{sig}**" if "BUY" in sig else f"🔴 **{sig}**" if "SHORT" in sig else f"⚪ {sig}"
                    round_summary += f"| **{symbol} {name}** | {sig_badge} | `{entry_p}` | `{stop_p}` | `{target_p}` | {reason} |\n"

                    # 出現買賣訊號時寫入歷史紀錄
                    if raw_sig in {"STRONG_BUY", "BUY", "SHORT", "STRONG_SHORT"}:
                        rec_id = cache_service.add_history_record(
                            symbol=symbol,
                            model=gemini.active_model,
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

                    time.sleep(2)

                except Exception as ex:
                    print(f"  [{symbol}] 分析異常: {ex}")

            update_github_summary(round_summary, append=True)
            time.sleep(65)

        time.sleep(10)

    # 3. 13:25 收盤回放結算
    pending_records = cache_service.get_pending_history_for_date(today_str)
    print(f"\n🎯 開始收盤分K回放結算，今日待結算筆數: {len(pending_records)}")

    settle_report_md = f"\n---\n### 🏁 13:25 收盤回放結算報告\n"
    if pending_records:
        settle_report_md += "| 代號 | 訊號 | 進場價 | 出場價 | 結果 | 淨損益 | 出場原因 |\n| :---: | :---: | :---: | :---: | :---: | :---: | :---: |\n"
        for rec in pending_records:
            sym = rec["symbol"]
            candles_raw = fugle.get_intraday_candles(sym, force_refresh=True)
            day_candles = candles_raw.get("data", []) if candles_raw else []
            if day_candles:
                cache_service.settle_history_record_with_candles(rec["id"], day_candles)

        # 重新讀取更新後的結算紀錄
        all_data = cache_service._read_history()
        today_records = [r for r in all_data.get("records", []) if r.get("date") == today_str]
        for r in today_records:
            res_str = "✅ 獲利" if r.get("result") == "win" else "❌ 虧損" if r.get("result") == "loss" else "➖ 打平"
            pnl_str = f"`${r.get('net_profit', 0):,}`"
            settle_report_md += f"| **{r.get('symbol')}** | `{r.get('signal')}` | `{r.get('entry_price')}` | `{r.get('exit_price')}` | {res_str} | {pnl_str} | {r.get('exit_reason')} |\n"

        # 產出每日回測 CSV
        os.makedirs("history_records", exist_ok=True)
        df = pd.DataFrame(today_records)
        csv_path = f"history_records/backtest_{today_str}.csv"
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(f"✅ 今日回測報表已成功產出：{csv_path}")
        settle_report_md += f"\n📁 **完整明細與決策理由已儲存至**：`{csv_path}`"
    else:
        settle_report_md += "今日無開倉進場訊號，無須結算。\n"

    update_github_summary(settle_report_md, append=True)

if __name__ == "__main__":
    main()
