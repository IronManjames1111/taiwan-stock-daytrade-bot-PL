# -*- coding: utf-8 -*-
"""
headless_trader.py - 雲端無頭當沖機器人 (v4.0 單輪執行 + 狀態持久化版)
─────────────────────────────────────────────────────────────
• 09:05 早盤第一次抓取成交量排行前 5 檔，並開始盤中 AI 分析（v20 調整，原為 09:15）
• 10:30 中盤第二次重新抓取成交量排行前 5 檔 (鎖定盤中換手輪動飆股)
• 盤中每 10 分鐘調用 Google AI (多模型自動降級鏈) 進行深度判斷，13:00 後截止（v20 新增）
• 自動生成獨立網頁 index.html (透過 GitHub Pages 提供免登入固定專屬網址)
• 同步輸出 GitHub Step Summary 即時 Markdown 看板
• 13:25 收盤自動回放當日 1分K 結算盈虧（已扣手續費與證交稅），產出 CSV 報表保存至 GitHub

v4.0 架構變更說明：
────────────────
舊版本用單一個 GitHub Actions job、從 09:15 內部 while 迴圈一路等到 13:25 才結束，
中間雖然每 10 分鐘會呼叫 render_html_dashboard() 更新本地 index.html，
但 git commit / push 只在整個 script 執行完畢後才跑一次 —— 導致：
  1) 使用者在收盤前完全看不到網站上的即時進度（只有結算後才看得到）
  2) latest_analysis_records 每輪都被整個清空重建，畫面上只顯示「最新一輪」的少數幾檔，
     不是當日所有分析紀錄的累積結果

新版本改為「單輪執行、執行完立即結束」，並將 wave1/wave2 股票池、累積分析紀錄、
是否已觸發過中盤重挑等狀態存入 dashboard_state.json，寫回 repo 供下一次 workflow
觸發時讀取接續使用。搭配 GitHub Actions 端的高頻率 cron（每 5 分鐘一次）與每輪
執行完立即 commit/push，讓網站可以做到近乎即時更新。
"""

import os
import re
import sys
import time
import json
import datetime
import pytz
import requests
import pandas as pd
from bs4 import BeautifulSoup
from typing import List, Dict, Optional

import hashlib
import base64
from fugle_service import FugleService
from gemini_service import GeminiService, _calc_limit_prices, _check_at_limit
import cache_service
from config import load_config

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

TW_TZ = pytz.timezone("Asia/Taipei")

# ── 盤中時間節點設定（v20 調整）──────────────────────────────────
# 集中放在這裡管理，避免同一個時間點散落在程式各處、改一處漏改
# 另一處（例如原本 09:15 這個字串同時出現在好幾個判斷式與說明文字裡）。
#
# ANALYSIS_START_TIME：開始選股＋進行AI分析的時間點。
#   原本是 09:15，考量開盤 5~10 分鐘的價格容易有開盤跳空/假突破雜訊，
#   才特意延後啟動；現在提早到 09:05，讓策略能更早掌握當天動能股，
#   但相對地開盤初期的訊號雜訊可能略增，可視實際回測結果再微調。
# ANALYSIS_STOP_TIME：盤中最後一次「丟給 AI 分析」的時間點。
#   13:00 之後不再呼叫 AI 進行判斷（避免尾盤時間不足以完成一趟
#   當沖來回、也節省 API 額度），但收盤結算（回放1分K比對損益，
#   不呼叫AI）仍照常在 HISTORY_SETTLE_TIME 執行。
# HISTORY_SETTLE_TIME：收盤回放結算時間點，維持 13:25 不變。
ANALYSIS_START_TIME  = "09:05"
ANALYSIS_STOP_TIME   = "13:00"
HISTORY_SETTLE_TIME  = "13:25"
MID_WAVE_TRIGGER_TIME = "10:30"

def get_tw_now() -> datetime.datetime:
    return datetime.datetime.now(TW_TZ)

def update_github_summary(content: str, append: bool = True):
    """將即時看板內容寫入 GitHub Actions 網頁 Summary"""
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        mode = "a" if append else "w"
        try:
            with open(summary_path, mode, encoding="utf-8") as f:
                f.write(content + "\n\n")
        except Exception as e:
            print(f"[Summary] 寫入失敗: {e}")

def render_html_dashboard(
    wave1_stocks: List[Dict] = None,
    wave2_stocks: List[Dict] = None,
    latest_analysis: List[Dict] = None,
    analysis_log: List[Dict] = None,
    live_quotes: Dict = None,
    settle_records: List[Dict] = None,
    active_model: str = "gemma-4-31b-it",
    status_text: str = "運行中",
    total_signals: int = None,
    **kwargs
):
    """
    生成單一獨立網頁 index.html，供 GitHub Pages 直接託管展示
    具備密碼防護機制、暗黑風質感交易介面、手機響應式設計

    latest_analysis：每檔股票「目前最新狀態」的清單（同一檔股票只有一筆），供總覽表格顯示。
    analysis_log：當天「每一輪分析」的完整歷程（同一檔股票可能有多筆，依時間序列），
    供「展開查看歷史分析」功能依 symbol 分組後顯示，修復先前中間分析輪次被覆蓋遺失的問題。
    live_quotes：{symbol: {"price": float, "updated_at": "HH:MM:SS"}}，每檔監控股票
    「最近一次分析當下」抓到的參考價。網站是純靜態的 GitHub Pages，前端沒有管道能直接
    呼叫需要金鑰的 Fugle API 取得即時報價，所以改由後端每輪分析時順便記錄下來，供前端
    在「展開查看歷史分析」清單裡，對有 BUY/SHORT 訊號的紀錄計算「以最近一次報價試算」
    的損益，不需要使用者手動操作，更新頻率跟現有排程（盤中每 5~10 分鐘）同步。
    """
    now_str = get_tw_now().strftime("%Y-%m-%d %H:%M:%S")
    today_str = get_tw_now().strftime("%Y-%m-%d")
    if total_signals is None:
        # .upper()：相容新舊資料，理由同下方 for 迴圈內的 sig 正規化說明
        total_signals = len([a for a in (latest_analysis or []) if a.get("signal", "").upper() in ["BUY", "SHORT"]])

    # 取得密碼設定 (預設 888888)，清除前後空白與換行，計算安全 SHA-256 與 Base64
    raw_pwd = (os.getenv("DASHBOARD_PASSWORD") or "888888").strip()
    pwd_hash = hashlib.sha256(raw_pwd.encode("utf-8")).hexdigest()
    pwd_b64 = base64.b64encode(raw_pwd.encode("utf-8")).decode("utf-8")

    wave1_stocks = wave1_stocks or []
    wave2_stocks = wave2_stocks or []
    latest_analysis = latest_analysis or []
    analysis_log = analysis_log or []
    live_quotes = live_quotes or {}
    settle_records = settle_records or []

    # ── 下載功能：把本次看板的完整原始資料打包成 JSON，供頁面右上角下載按鈕使用 ──
    # today_str 一併放入 payload：供前端日期切換選單判斷「目前選的是不是今天」，
    # 以及切回今日時可以直接從這份記憶體資料還原畫面，不需要重新 fetch。
    # analysis_log 同樣放入 payload：供「展開查看歷史分析」功能依 symbol 篩選、
    # 按時間序列呈現當天每一輪的完整分析紀錄（不是只有最新一筆）。
    # live_quotes 同樣放入 payload：供前端計算歷史分析紀錄中 BUY/SHORT 訊號的即時損益。
    export_payload = {
        "generated_at": now_str,
        "today_str": today_str,
        "status_text": status_text,
        "active_model": active_model,
        "total_signals": total_signals,
        "wave1_stocks": wave1_stocks,
        "wave2_stocks": wave2_stocks,
        "latest_analysis": latest_analysis,
        "analysis_log": analysis_log,
        "live_quotes": live_quotes,
        "settle_records": settle_records,
    }
    # ensure_ascii=False 保留中文可讀；再用 json.dumps 序列化成字串安全地塞進 <script> 的 JS 常數
    export_json_str = json.dumps(export_payload, ensure_ascii=False, indent=2)
    # </script> 若原封不動出現在字串內會提前結束 script 標籤，需要跳脫
    export_json_js_safe = export_json_str.replace("</", "<\\/")

    # 計算損益卡片文字
    pnl_text = "尚未結算"
    pnl_class = "text-gray-400"
    if settle_records:
        # 【bug修復】cache_service._compute_settle_result() 回傳的欄位是
        # "pnl_amount"，從來沒有 "net_profit" 這個 key。原本這裡誤用
        # r.get("net_profit", 0) 讀取，每次都拿不到值、靜默 fallback 成 0，
        # 導致「結算損益」KPI 卡片不論實際賺賠多少，永遠顯示 $0。
        net_total = sum(r.get("pnl_amount", 0) for r in settle_records)
        pnl_text = f"+${net_total:,}" if net_total > 0 else f"-${abs(net_total):,}" if net_total < 0 else "$0"
        pnl_class = "text-[#ff5470]" if net_total > 0 else "text-[#00d68f]" if net_total < 0 else "text-gray-300"

    # 生成波段一列表
    wave1_html = ""
    if wave1_stocks:
        for idx, s in enumerate(wave1_stocks, 1):
            wave1_html += f"""
            <li class="panel-raised border rounded-lg p-2.5 flex items-center justify-between gap-2">
                <span class="font-bold text-white text-sm whitespace-nowrap"><span class="text-[#8db3ff] mr-1.5">#{idx}</span>{s['symbol']} {s['name']}</span>
                <span class="mono text-gray-400 text-[11px] text-right whitespace-nowrap">{s['price']} 元 · {s.get('volume', 0):,} 張</span>
            </li>
            """
    else:
        wave1_html = f'<li class="text-gray-500 text-xs py-2">等待開盤 {ANALYSIS_START_TIME} 抓取中...</li>'

    # 生成波段二列表
    wave2_html = ""
    if wave2_stocks:
        for idx, s in enumerate(wave2_stocks, 1):
            wave2_html += f"""
            <li class="panel-raised border rounded-lg p-2.5 flex items-center justify-between gap-2">
                <span class="font-bold text-white text-sm whitespace-nowrap"><span class="text-[#c4a6ff] mr-1.5">#{idx}</span>{s['symbol']} {s['name']}</span>
                <span class="mono text-gray-400 text-[11px] text-right whitespace-nowrap">{s['price']} 元 · {s.get('volume', 0):,} 張</span>
            </li>
            """
    else:
        wave2_html = f'<li class="text-gray-500 text-xs py-2">{MID_WAVE_TRIGGER_TIME} 自動重新掃描成交量排行...</li>'

    # 生成分析表格（依訊號分類貼上 data-filter-group 屬性，供前端做多/做空/觀望篩選使用）
    # 配色依台股慣例「紅漲綠跌」：做多(偏多/漲) 用紅、放空(偏空/跌) 用綠，跟一般西式股市剛好相反
    analysis_rows = ""      # 桌面版表格列
    analysis_cards = ""     # 手機版直式資訊卡（避免長文字被表格固定欄寬硬擠導致換行跑版）
    if latest_analysis:
        for a in latest_analysis:
            # .upper() 是防禦性寫法：修復前寫入的舊資料 (dashboard_state.json /
            # history_records/*.json) signal 欄位可能還是小寫 ("buy"/"short"/"watch")，
            # 加上 .upper() 讓新舊資料都能被正確分類，不用等舊資料被覆蓋掉才會顯示正常。
            sig = a.get("signal", "WATCH").upper()
            if "BUY" in sig:
                filter_group = "long"
                sig_badge = '<span class="sig-badge sig-long">🔺 做多</span>'
            elif "SHORT" in sig:
                filter_group = "short"
                sig_badge = '<span class="sig-badge sig-short">🔻 放空</span>'
            else:
                filter_group = "watch"
                sig_badge = '<span class="sig-badge sig-watch">— 觀望</span>'

            updated_at = a.get("updated_at", "")
            symbol = a.get("symbol", "")
            name = a.get("name", "")
            entry = a.get("entry", "-")
            stop_loss = a.get("stop_loss", "-")
            target = a.get("target", "-")
            reason = a.get("reason", "")

            # 統計這檔股票今天總共被分析過幾輪（來自 analysis_log 完整歷程），
            # 只有 >1 筆時才顯示「展開歷史」按鈕，避免只分析過一次的股票也顯示無意義的按鈕
            log_count = sum(1 for lg in analysis_log if lg.get("symbol") == symbol)
            history_btn = (
                f'<button type="button" class="history-toggle-btn" data-symbol="{symbol}" '
                f'onclick="toggleHistoryLog(this, \'{symbol}\')">📜 歷史 {log_count} 筆</button>'
                if log_count > 1 else ""
            )

            analysis_rows += f"""
            <tr class="hover:bg-white/[0.02] analysis-row" data-filter-group="{filter_group}" data-symbol="{symbol}">
                <td class="py-2.5 px-3 font-bold text-white whitespace-nowrap">{symbol} {name}</td>
                <td class="py-2.5 px-3">{sig_badge}</td>
                <td class="py-2.5 px-3 mono text-gray-200 whitespace-nowrap">{entry}</td>
                <td class="py-2.5 px-3 mono text-[#00d68f] whitespace-nowrap">{stop_loss}</td>
                <td class="py-2.5 px-3 mono text-[#ff5470] whitespace-nowrap">{target}</td>
                <td class="py-2.5 px-3 text-gray-300 text-xs">{reason}</td>
                <td class="py-2.5 px-3 text-gray-500 text-[11px] mono whitespace-nowrap">{updated_at}{history_btn}</td>
            </tr>
            <tr class="history-log-row hidden" data-symbol-log="{symbol}">
                <td colspan="7" class="px-3 pb-3"><div class="history-log-container"></div></td>
            </tr>
            """

            analysis_cards += f"""
            <div class="data-card analysis-row" data-filter-group="{filter_group}" data-symbol="{symbol}">
                <div class="flex items-center justify-between mb-2">
                    <span class="font-bold text-white text-sm">{symbol} {name}</span>
                    {sig_badge}
                </div>
                <div class="data-row"><span class="dlabel">建議進場</span><span class="dvalue mono">{entry}</span></div>
                <div class="data-row"><span class="dlabel">建議停損</span><span class="dvalue mono text-[#00d68f]">{stop_loss}</span></div>
                <div class="data-row"><span class="dlabel">建議停利</span><span class="dvalue mono text-[#ff5470]">{target}</span></div>
                <div class="data-row"><span class="dlabel">更新時間</span><span class="dvalue mono text-gray-500">{updated_at}</span></div>
                <div class="mt-2 pt-2 border-t border-white/5 text-xs text-gray-300 leading-relaxed">{reason}</div>
                {f'<div class="mt-2 pt-2 border-t border-white/5">{history_btn}<div class="history-log-container" data-symbol-log-card="{symbol}"></div></div>' if history_btn else ''}
            </div>
            """
    else:
        analysis_rows = '<tr><td colspan="7" class="py-6 text-center text-gray-500 text-xs">盤中每 10 分鐘自動更新分析看板...</td></tr>'
        analysis_cards = '<div class="text-center text-gray-500 text-xs py-6">盤中每 10 分鐘自動更新分析看板...</div>'

    # 生成結算表格
    settle_rows = ""       # 桌面版表格列
    settle_cards = ""      # 手機版直式資訊卡
    if settle_records:
        for r in settle_records:
            res = r.get("result")
            res_badge = (
                '<span class="sig-badge sig-long">✅ 獲利</span>'
                if res == "win" else
                '<span class="sig-badge sig-short">❌ 虧損</span>'
                if res == "loss" else
                '<span class="sig-badge sig-watch">— 打平</span>'
            )
            # 【bug修復】同上，這裡也是誤用不存在的 "net_profit" key，
            # 導致每筆結算卡片「淨損益」都顯示 $0，即使 result 徽章（win/loss）
            # 本身是對的——因為 result 欄位名稱沒打錯，只有金額欄位打錯。
            net_p = r.get("pnl_amount", 0)
            net_str = f"+${net_p:,}" if net_p > 0 else f"-${abs(net_p):,}" if net_p < 0 else "$0"
            net_color = "text-[#ff5470]" if net_p > 0 else "text-[#00d68f]" if net_p < 0 else "text-gray-300"
            symbol = r.get("symbol", "")
            signal = r.get("signal", "")
            entry_price = r.get("entry_price", "-")
            exit_price = r.get("exit_price", "-")
            # 【bug修復】exit_reason 原本是 hit_sl/hit_tp/forced_close 這種
            # 給程式看的英文代碼，直接顯示在畫面上使用者看不懂，這裡轉成中文。
            exit_reason = format_exit_reason(r.get("exit_reason", "-"))

            settle_rows += f"""
            <tr class="hover:bg-white/[0.02]">
                <td class="py-2 px-3 font-bold text-white whitespace-nowrap">{symbol}</td>
                <td class="py-2 px-3 font-semibold whitespace-nowrap">{signal}</td>
                <td class="py-2 px-3 mono whitespace-nowrap">{entry_price}</td>
                <td class="py-2 px-3 mono whitespace-nowrap">{exit_price}</td>
                <td class="py-2 px-3">{res_badge}</td>
                <td class="py-2 px-3 mono {net_color} font-bold whitespace-nowrap">{net_str}</td>
                <td class="py-2 px-3 text-gray-400 text-xs">{exit_reason}</td>
            </tr>
            """

            settle_cards += f"""
            <div class="data-card">
                <div class="flex items-center justify-between mb-2">
                    <span class="font-bold text-white text-sm">{symbol}　<span class="text-gray-400 font-normal text-xs">{signal}</span></span>
                    {res_badge}
                </div>
                <div class="data-row"><span class="dlabel">進場 → 出場</span><span class="dvalue mono">{entry_price} → {exit_price}</span></div>
                <div class="data-row"><span class="dlabel">淨損益</span><span class="dvalue mono {net_color} font-bold">{net_str}</span></div>
                <div class="data-row"><span class="dlabel">出場原因</span><span class="dvalue text-xs">{exit_reason}</span></div>
            </div>
            """
    else:
        settle_rows = f'<tr><td colspan="7" class="py-4 text-center text-gray-500 text-xs">尚未達到收盤結算時間 ({HISTORY_SETTLE_TIME})</td></tr>'
        settle_cards = f'<div class="text-center text-gray-500 text-xs py-4">尚未達到收盤結算時間 ({HISTORY_SETTLE_TIME})</div>'

    html_content = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
    <title>台股當沖 AI 終端</title>
    <meta http-equiv="refresh" content="60">
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Noto+Sans+TC:wght@400;500;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg: #0a0e14;
            --surface: #10161f;
            --surface-raised: #161d29;
            --line: #202834;
            --long: #ff5470;
            --long-dim: #2a1017;
            --short: #00d68f;
            --short-dim: #06231b;
            --watch: #8b93a1;
            --watch-dim: #171c24;
            --accent: #4d8dff;
        }}
        body {{ font-family: 'Noto Sans TC', -apple-system, sans-serif; background-color: var(--bg) !important; color: #dde3ea; }}
        .mono {{ font-family: 'JetBrains Mono', ui-monospace, monospace; }}
        .shake {{ animation: shake 0.4s cubic-bezier(.36,.07,.19,.97) both; }}
        @keyframes shake {{
            10%, 90% {{ transform: translate3d(-1px, 0, 0); }}
            20%, 80% {{ transform: translate3d(2px, 0, 0); }}
            30%, 50%, 70% {{ transform: translate3d(-4px, 0, 0); }}
            40%, 60% {{ transform: translate3d(4px, 0, 0); }}
        }}
        @keyframes pulse-dot {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.3; }} }}
        .live-dot {{ animation: pulse-dot 1.6s ease-in-out infinite; }}

        /* ── 終端機式視覺覆寫：取代原本清一色的灰卡片樣板 ────────────── */
        .panel {{ background: var(--surface) !important; border-color: var(--line) !important; }}
        .panel-raised {{ background: var(--surface-raised) !important; border-color: var(--line) !important; }}

        /* 訊號徽章：white-space nowrap 是修正「觀望」等文字在窄螢幕斷行跑版的關鍵 */
        .sig-badge {{
            display: inline-flex; align-items: center; gap: 4px;
            padding: 3px 10px; border-radius: 5px; font-size: 12px; font-weight: 700;
            white-space: nowrap;
        }}
        .sig-long {{ background: var(--long-dim); color: var(--long); border: 1px solid rgba(255,84,112,.35); }}
        .sig-short {{ background: var(--short-dim); color: var(--short); border: 1px solid rgba(0,214,143,.35); }}
        .sig-watch {{ background: var(--watch-dim); color: var(--watch); border: 1px solid rgba(139,147,161,.3); }}

        /* 訊號篩選按鈕：預設(未選取)樣式，JS 會依目前選取狀態動態切換 active 樣式 */
        .filter-btn {{ background-color: var(--surface-raised); border-color: var(--line); color: #94a0b0; white-space: nowrap; }}
        .filter-btn.active-all {{ background-color: rgba(77,141,255,.12); border-color: var(--accent); color: #a9c6ff; }}
        .filter-btn.active-long {{ background-color: var(--long-dim); border-color: var(--long); color: var(--long); }}
        .filter-btn.active-short {{ background-color: var(--short-dim); border-color: var(--short); color: var(--short); }}
        .filter-btn.active-watch {{ background-color: var(--watch-dim); border-color: var(--watch); color: var(--watch); }}

        /* 桌面顯示表格，手機改顯示直式資訊卡：這是解決手機排版跑版的核心結構調整，
           而不是硬把長文字塞進固定表格欄寬 */
        .analysis-table-wrap {{ display: none; }}
        .analysis-cards-wrap {{ display: block; }}
        @media (min-width: 768px) {{
            .analysis-table-wrap {{ display: block; }}
            .analysis-cards-wrap {{ display: none; }}
        }}

        .data-card {{ background: var(--surface-raised); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; }}
        .data-card + .data-card {{ margin-top: 8px; }}
        .data-row {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; padding: 3px 0; font-size: 12.5px; }}
        .data-row .dlabel {{ color: #6b7685; white-space: nowrap; flex-shrink: 0; }}
        .data-row .dvalue {{ color: #dde3ea; text-align: right; word-break: break-word; }}

        /* 「展開查看歷史分析」按鈕與展開內容：修復先前同一檔股票只留最後一筆分析結果、
           中間所有分析輪次都被覆蓋看不到的問題。按鈕預設低調（小字+底線），展開後轉為
           強調色，讓使用者清楚知道目前是展開狀態。 */
        .history-toggle-btn {{
            display: inline-block; margin-left: 8px; font-size: 11px; color: var(--accent);
            text-decoration: underline; text-underline-offset: 2px; cursor: pointer; background: none; border: none; padding: 0;
            white-space: nowrap;
        }}
        .history-toggle-btn.active {{ color: #a9c6ff; font-weight: 700; }}
        .history-log-row.hidden {{ display: none; }}
        .history-log-container {{
            background: var(--surface); border: 1px solid var(--line); border-radius: 8px;
            padding: 8px 10px; max-height: 260px; overflow-y: auto;
        }}
        .history-log-container:not(.open):empty {{ display: none; }}
        .history-log-entry {{ padding: 6px 2px; border-bottom: 1px dashed var(--line); }}
        .history-log-entry:last-child {{ border-bottom: none; }}

        /* 「以最近報價試算」的損益區塊：顯示在每筆 BUY/SHORT 歷史分析紀錄下方。
           台股慣例漲(賺)用紅色、跌(賠)用綠色，跟 sig-long(做多/紅) sig-short(放空/綠)
           兩種既有配色的意涵一致 —— 做多賺錢是紅、放空賺錢也是紅，賠錢則反過來是綠，
           這裡直接沿用 calcLivePnl() 算出的 isProfit 顏色，語意保持一致不會反直覺。 */
        .live-pnl-box {{
            display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
            margin-top: 4px; padding-top: 4px; border-top: 1px dotted var(--line);
            font-size: 11px;
        }}
    </style>
</head>
<body class="min-h-screen p-3 md:p-6 flex flex-col justify-between">

    <!-- 🔐 密碼保護鎖定遮罩 -->
    <div id="lock-screen" class="fixed inset-0 z-50 bg-[#0d1117] flex items-center justify-center p-4">
        <div id="lock-card" class="bg-gray-900 border border-gray-800 rounded-3xl p-8 max-w-sm w-full shadow-2xl text-center space-y-6">
            <div class="inline-flex p-4 rounded-2xl bg-indigo-950/60 border border-indigo-800/50 text-indigo-400 text-3xl">
                🔒
            </div>
            <div>
                <h2 class="text-xl font-bold text-white tracking-wide">台股當沖 AI 終端</h2>
                <p class="text-xs text-gray-400 mt-1">此頁面受密碼保護，請輸入存取密碼</p>
            </div>
            <div class="space-y-4">
                <div class="relative">
                    <input type="password" id="pwd-input" placeholder="請輸入查看密碼" autofocus
                        onkeydown="if(event.key==='Enter') handleUnlock();"
                        class="w-full bg-gray-950 border border-gray-700 rounded-xl px-4 py-3 text-sm text-white focus:outline-none focus:border-indigo-500 mono tracking-widest text-center" />
                </div>
                <div id="error-msg" class="text-xs text-rose-400 hidden font-medium">密碼錯誤，請重新輸入</div>
                
                <div class="flex items-center justify-between text-xs text-gray-400 px-1">
                    <label class="flex items-center gap-1.5 cursor-pointer select-none">
                        <input type="checkbox" id="remember-me" checked class="rounded bg-gray-800 border-gray-700 text-indigo-500 focus:ring-0" />
                        <span>記住此裝置 (免再輸入)</span>
                    </label>
                </div>

                <button type="button" onclick="handleUnlock()"
                    class="w-full bg-gradient-to-r from-indigo-600 to-blue-600 hover:from-indigo-500 hover:to-blue-500 text-white font-bold py-3 rounded-xl text-sm shadow-lg shadow-indigo-500/20 transition duration-200 cursor-pointer">
                    解鎖進入看板 ➔
                </button>
            </div>
        </div>
    </div>

    <!-- 📊 主看板內容 (解鎖後顯示) -->
    <div id="main-content" class="max-w-5xl mx-auto w-full space-y-5 hidden">
        
        <!-- Header -->
        <header class="panel border rounded-2xl p-5 flex flex-col md:flex-row md:items-center md:justify-between gap-4">
            <div>
                <div class="flex items-center gap-2">
                    <span class="inline-block w-2.5 h-2.5 rounded-full bg-[#00d68f] live-dot"></span>
                    <h1 class="text-lg md:text-xl font-bold text-white tracking-tight">台股 AI 當沖雲端終端</h1>
                    <span class="bg-[#06231b] text-[#00d68f] text-[11px] px-2 py-0.5 rounded-full border border-[#00d68f]/25 font-semibold whitespace-nowrap">雲端全自動</span>
                </div>
                <p class="text-xs text-gray-500 mt-1">{ANALYSIS_START_TIME} / {MID_WAVE_TRIGGER_TIME} 雙波段選股　·　每 10 分鐘 AI 分析（{ANALYSIS_STOP_TIME} 截止）　·　{HISTORY_SETTLE_TIME} 回放結算</p>
            </div>
            <div class="flex flex-wrap items-center gap-2 text-xs">
                <div class="panel-raised rounded-lg px-3 py-2 border">
                    <span class="text-gray-500">狀態</span>
                    <span class="text-[#00d68f] font-bold ml-1.5">{status_text}</span>
                </div>
                <div class="panel-raised rounded-lg px-3 py-2 border">
                    <span class="text-gray-500">更新</span>
                    <span class="text-white mono ml-1.5">{now_str}</span>
                </div>
                <button type="button" onclick="downloadJSON()"
                    class="bg-[#0f1c33] hover:bg-[#152544] border border-[#4d8dff]/30 text-[#8db3ff] rounded-lg px-3 py-2 font-semibold transition cursor-pointer whitespace-nowrap">
                    ⬇ JSON
                </button>
                <button type="button" onclick="downloadCSV()"
                    class="bg-[#06231b] hover:bg-[#0a2e24] border border-[#00d68f]/30 text-[#5ce8b8] rounded-lg px-3 py-2 font-semibold transition cursor-pointer whitespace-nowrap">
                    ⬇ CSV
                </button>
            </div>
        </header>

        <!-- 📅 歷史日期切換：預設顯示今日即時資料，切換後改為唯讀顯示該日的完整分析與結算紀錄 -->
        <div class="panel border rounded-2xl p-4 flex flex-col md:flex-row md:items-center gap-3">
            <div class="flex items-center gap-2 text-xs text-gray-500 shrink-0">
                <span>查看日期</span>
            </div>
            <select id="history-date-select" onchange="onHistoryDateChange(this.value)"
                class="panel-raised border rounded-lg px-3 py-1.5 text-xs text-white focus:outline-none focus:border-[#4d8dff] mono">
                <option value="__today__">今日即時（{today_str}）</option>
            </select>
            <span id="history-loading-msg" class="hidden text-xs text-gray-500">載入中...</span>
            <span id="history-error-msg" class="hidden text-xs text-[#ff5470]">該日期尚無資料或載入失敗</span>
        </div>

        <!-- KPI 數據卡片 -->
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
            <div class="panel border rounded-xl p-4">
                <div class="text-[11px] text-gray-500">AI 模型</div>
                <div class="text-sm font-bold text-[#8db3ff] mono mt-1 truncate">{active_model}</div>
            </div>
            <div class="panel border rounded-xl p-4">
                <div class="text-[11px] text-gray-500">監控標的</div>
                <div class="text-xl font-bold text-white mono mt-1">{len(wave2_stocks or wave1_stocks)} 檔</div>
            </div>
            <div class="panel border rounded-xl p-4">
                <div class="text-[11px] text-gray-500">今日訊號</div>
                <div class="text-xl font-bold text-[#f5b942] mono mt-1">{total_signals} 筆</div>
            </div>
            <div class="panel border rounded-xl p-4">
                <div class="text-[11px] text-gray-500">結算損益</div>
                <div class="text-xl font-bold {pnl_class} mono mt-1">{pnl_text}</div>
            </div>
        </div>

        <!-- 雙波段選股板塊 -->
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div class="panel border rounded-2xl p-5">
                <div class="flex items-center justify-between border-b border-white/5 pb-3 mb-3">
                    <div>
                        <h2 class="font-bold text-white text-sm">早盤成交量排行</h2>
                        <p class="text-[11px] text-gray-500 mt-0.5">{ANALYSIS_START_TIME} 觸發（早盤動能成交量排行）</p>
                    </div>
                    <span class="text-[11px] bg-[#0f1c33] text-[#8db3ff] border border-[#4d8dff]/25 px-2 py-0.5 rounded-md whitespace-nowrap">波段一</span>
                </div>
                <ul class="space-y-2 text-sm">{wave1_html}</ul>
            </div>

            <div class="panel border rounded-2xl p-5">
                <div class="flex items-center justify-between border-b border-white/5 pb-3 mb-3">
                    <div>
                        <h2 class="font-bold text-white text-sm">中盤換手重挑</h2>
                        <p class="text-[11px] text-gray-500 mt-0.5">{MID_WAVE_TRIGGER_TIME} 觸發（鎖定盤中輪動主升股）</p>
                    </div>
                    <span class="text-[11px] bg-[#1f1433] text-[#c4a6ff] border border-[#a78bfa]/25 px-2 py-0.5 rounded-md whitespace-nowrap">波段二</span>
                </div>
                <ul class="space-y-2 text-sm">{wave2_html}</ul>
            </div>
        </div>

        <!-- 最新 10 分鐘分析結果 -->
        <div class="panel border rounded-2xl p-5">
            <div class="flex flex-col md:flex-row md:items-center md:justify-between border-b border-white/5 pb-3 mb-4 gap-2">
                <div>
                    <h2 class="font-bold text-white text-base">即時多空訊號</h2>
                    <p class="text-xs text-gray-500 mt-0.5">每 10 分鐘 AI 判定進出場價與停損停利，累積顯示當日所有分析紀錄</p>
                </div>
                <span class="text-[11px] text-gray-500 mono whitespace-nowrap">每 60 秒自動刷新</span>
            </div>

            <!-- 🔎 訊號篩選按鈕：做多 / 做空 / 觀望 / 全部 -->
            <div class="flex flex-wrap items-center gap-2 mb-4">
                <button type="button" onclick="setSignalFilter('all')" id="filter-btn-all"
                    class="filter-btn px-3 py-1.5 rounded-lg text-xs font-semibold border transition cursor-pointer">
                    全部 <span id="count-all" class="mono"></span>
                </button>
                <button type="button" onclick="setSignalFilter('long')" id="filter-btn-long"
                    class="filter-btn px-3 py-1.5 rounded-lg text-xs font-semibold border transition cursor-pointer">
                    🔺 做多 <span id="count-long" class="mono"></span>
                </button>
                <button type="button" onclick="setSignalFilter('short')" id="filter-btn-short"
                    class="filter-btn px-3 py-1.5 rounded-lg text-xs font-semibold border transition cursor-pointer">
                    🔻 做空 <span id="count-short" class="mono"></span>
                </button>
                <button type="button" onclick="setSignalFilter('watch')" id="filter-btn-watch"
                    class="filter-btn px-3 py-1.5 rounded-lg text-xs font-semibold border transition cursor-pointer">
                    — 觀望 <span id="count-watch" class="mono"></span>
                </button>
            </div>

            <!-- 桌面版：表格 -->
            <div class="analysis-table-wrap overflow-x-auto">
                <table class="w-full text-left text-xs md:text-sm">
                    <thead>
                        <tr class="text-gray-500 border-b border-white/5 text-[11px]">
                            <th class="py-2.5 px-3">標的</th>
                            <th class="py-2.5 px-3">訊號</th>
                            <th class="py-2.5 px-3">進場</th>
                            <th class="py-2.5 px-3">停損</th>
                            <th class="py-2.5 px-3">停利</th>
                            <th class="py-2.5 px-3">AI 決策依據</th>
                            <th class="py-2.5 px-3">更新時間</th>
                        </tr>
                    </thead>
                    <tbody id="analysis-tbody" class="divide-y divide-white/5">{analysis_rows}</tbody>
                </table>
            </div>

            <!-- 手機版：直式資訊卡（避免長文字被表格固定欄寬硬擠導致換行跑版） -->
            <div id="analysis-cards" class="analysis-cards-wrap">{analysis_cards}</div>

            <p id="filter-empty-msg" class="hidden text-center text-gray-500 text-xs py-6">此篩選條件下目前沒有符合的標的</p>
        </div>

        <!-- 收盤回放結算卡片 -->
        <div class="panel border rounded-2xl p-5">
            <div class="flex items-center justify-between border-b border-white/5 pb-3 mb-3">
                <div>
                    <h2 class="font-bold text-white text-base">今日回測結算</h2>
                    <p class="text-[11px] text-gray-500 mt-0.5">{HISTORY_SETTLE_TIME} 以當日 1 分K 逐根回放比對實際賺賠（已扣手續費與證交稅）</p>
                </div>
                <span class="text-[11px] text-[#f5b942] font-semibold whitespace-nowrap">{HISTORY_SETTLE_TIME} 結算</span>
            </div>

            <!-- 桌面版：表格 -->
            <div class="analysis-table-wrap overflow-x-auto">
                <table class="w-full text-left text-xs md:text-sm">
                    <thead>
                        <tr class="text-gray-500 border-b border-white/5 text-[11px]">
                            <th class="py-2 px-3">代號</th>
                            <th class="py-2 px-3">方向</th>
                            <th class="py-2 px-3">進場價</th>
                            <th class="py-2 px-3">出場價</th>
                            <th class="py-2 px-3">結果</th>
                            <th class="py-2 px-3">淨損益</th>
                            <th class="py-2 px-3">出場原因</th>
                        </tr>
                    </thead>
                    <tbody id="settle-tbody" class="divide-y divide-white/5">{settle_rows}</tbody>
                </table>
            </div>

            <!-- 手機版：直式資訊卡 -->
            <div id="settle-cards" class="analysis-cards-wrap">{settle_cards}</div>
        </div>

        <!-- Footer -->
        <footer class="text-center text-xs text-gray-600 py-3">
            本儀表板由 GitHub Actions 全自動維護 · 密碼防護機制已啟用
        </footer>
    </div>

    <!-- 🔐 密碼驗證核心邏輯 (Base64 即時比對 + SHA-256 備援 + LocalStorage 記住裝置) -->
    <script>
        const PWD_HASH = "{pwd_hash}";
        const PWD_B64 = "{pwd_b64}";

        // 本次看板的完整原始資料，供右上角「下載 JSON / 下載 CSV」按鈕使用
        const EXPORT_DATA = {export_json_js_safe};

        function triggerDownload(content, filename, mimeType) {{
            const blob = new Blob([content], {{ type: mimeType }});
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        }}

        function downloadJSON() {{
            const ts = EXPORT_DATA.generated_at.replace(/[: ]/g, "-");
            const content = JSON.stringify(EXPORT_DATA, null, 2);
            triggerDownload(content, `daytrade_${{ts}}.json`, "application/json;charset=utf-8");
        }}

        // 將單一儲存格值轉為安全的 CSV 欄位 (處理逗號、雙引號、換行)
        function csvCell(val) {{
            if (val === null || val === undefined) return "";
            const str = String(val);
            if (/[",\\n]/.test(str)) {{
                return '"' + str.replace(/"/g, '""') + '"';
            }}
            return str;
        }}

        function csvSection(title, rows) {{
            if (!rows || rows.length === 0) {{
                return `${{title}}\\n(無資料)\\n\\n`;
            }}
            const headers = Object.keys(rows[0]);
            const lines = [headers.join(",")];
            for (const row of rows) {{
                lines.push(headers.map(h => csvCell(row[h])).join(","));
            }}
            return `${{title}}\\n${{lines.join("\\n")}}\\n\\n`;
        }}

        function downloadCSV() {{
            const ts = EXPORT_DATA.generated_at.replace(/[: ]/g, "-");
            let csv = "\\uFEFF"; // UTF-8 BOM，確保 Excel 開啟中文不亂碼
            csv += `AI 當沖雲端即時看板匯出報表\\n`;
            csv += `產生時間,${{csvCell(EXPORT_DATA.generated_at)}}\\n`;
            csv += `目前狀態,${{csvCell(EXPORT_DATA.status_text)}}\\n`;
            csv += `AI 模型,${{csvCell(EXPORT_DATA.active_model)}}\\n`;
            csv += `累計訊號數,${{csvCell(EXPORT_DATA.total_signals)}}\\n\\n`;
            csv += csvSection("【波段一 {ANALYSIS_START_TIME} 選股】", EXPORT_DATA.wave1_stocks);
            csv += csvSection("【波段二 {MID_WAVE_TRIGGER_TIME} 選股】", EXPORT_DATA.wave2_stocks);
            csv += csvSection("【AI 即時分析訊號 (每檔股票最新狀態)】", EXPORT_DATA.latest_analysis);
            csv += csvSection("【AI 完整分析歷程 (每一輪分析，不覆蓋)】", EXPORT_DATA.analysis_log);
            csv += csvSection("【收盤結算紀錄】", EXPORT_DATA.settle_records);
            triggerDownload(csv, `daytrade_${{ts}}.csv`, "text/csv;charset=utf-8");
        }}

        async function sha256(str) {{
            try {{
                if (window.crypto && crypto.subtle) {{
                    const buffer = new TextEncoder().encode(str);
                    const hashBuffer = await crypto.subtle.digest("SHA-256", buffer);
                    return Array.from(new Uint8Array(hashBuffer)).map(b => b.toString(16).padStart(2, "0")).join("");
                }}
            }} catch (e) {{}}
            return null;
        }}

        function toB64(str) {{
            try {{
                return btoa(unescape(encodeURIComponent(str)));
            }} catch (e) {{
                return "";
            }}
        }}

        async function handleUnlock(e) {{
            if (e && e.preventDefault) e.preventDefault();
            const input = (document.getElementById("pwd-input").value || "").trim();
            const errorMsg = document.getElementById("error-msg");
            const lockCard = document.getElementById("lock-card");

            let matched = false;
            // 優先比對 Base64 (同步且零依賴，100% 在任何瀏覽器與行動裝置中皆能運作)
            if (toB64(input) === PWD_B64) {{
                matched = true;
            }} else {{
                // 備援比對 SHA-256
                const hash = await sha256(input);
                if (hash && hash === PWD_HASH) {{
                    matched = true;
                }}
            }}

            if (matched) {{
                if (document.getElementById("remember-me").checked) {{
                    localStorage.setItem("daytrade_auth_token", PWD_B64);
                }}
                unlockUI();
            }} else {{
                errorMsg.classList.remove("hidden");
                lockCard.classList.remove("shake");
                void lockCard.offsetWidth;
                lockCard.classList.add("shake");
            }}
        }}

        function unlockUI() {{
            document.getElementById("lock-screen").classList.add("hidden");
            document.getElementById("main-content").classList.remove("hidden");
        }}

        function handleLock() {{
            localStorage.removeItem("daytrade_auth_token");
            location.reload();
        }}

        window.addEventListener("DOMContentLoaded", () => {{
            const savedToken = localStorage.getItem("daytrade_auth_token");
            if (savedToken === PWD_B64 || savedToken === PWD_HASH) {{
                unlockUI();
            }}
            // 今日的分析表格本身是後端 Python 產生時就直接寫入靜態 HTML 的（非透過
            // renderAnalysisTable 動態產生），所以這裡要單獨把 CURRENT_LOG_BY_SYMBOL 跟
            // CURRENT_LIVE_QUOTES 初始化好，「展開歷史」按鈕在使用者尚未切換過日期前
            // 也才能正確查到資料、算出即時損益。
            CURRENT_LOG_BY_SYMBOL = buildLogBySymbol(EXPORT_DATA.analysis_log || []);
            CURRENT_LIVE_QUOTES = EXPORT_DATA.live_quotes || {{}};
            initSignalFilter();
            initHistoryDateSelect();
        }});

        // ── 歷史日期切換 ─────────────────────────────────────────────
        // GitHub Pages 是純靜態網站，前端無法列出資料夾內容，
        // 因此透過 history_records/index.json 這份索引檔取得「有哪些日期可查」，
        // 選擇日期後改讀取 history_records/analysis_YYYY-MM-DD.json 動態重新渲染表格。
        // 今日資料則直接使用 EXPORT_DATA（頁面產生當下就內嵌好的資料），不需要額外 fetch。
        const TODAY_STR = EXPORT_DATA.today_str;

        async function initHistoryDateSelect() {{
            const select = document.getElementById("history-date-select");
            try {{
                const resp = await fetch("history_records/index.json", {{ cache: "no-store" }});
                if (!resp.ok) throw new Error("index.json 不存在");
                const data = await resp.json();
                const dates = (data.dates || []).filter(d => d !== TODAY_STR);

                dates.forEach(d => {{
                    const opt = document.createElement("option");
                    opt.value = d;
                    opt.textContent = d;
                    select.appendChild(opt);
                }});
            }} catch (e) {{
                // 索引檔還不存在是正常情況 (代表尚未有任何一天收盤結算過)，靜默處理即可，
                // 下拉選單維持只有「今日即時」一個選項
                console.log("尚無歷史日期索引可載入 (可能是第一個交易日，尚未收盤結算過)");
            }}
        }}

        async function onHistoryDateChange(value) {{
            const loadingMsg = document.getElementById("history-loading-msg");
            const errorMsg = document.getElementById("history-error-msg");
            errorMsg.classList.add("hidden");

            if (value === "__today__") {{
                // 切回今日：直接用頁面產生當下就內嵌好的 EXPORT_DATA 還原，不需要重新 fetch
                renderAnalysisTable(EXPORT_DATA.latest_analysis, true, EXPORT_DATA.analysis_log || [], EXPORT_DATA.live_quotes || {{}});
                renderSettleTable(EXPORT_DATA.settle_records);
                setSignalFilter(localStorage.getItem("daytrade_signal_filter") || "all");
                return;
            }}

            loadingMsg.classList.remove("hidden");
            try {{
                const resp = await fetch(`history_records/analysis_${{value}}.json`, {{ cache: "no-store" }});
                if (!resp.ok) throw new Error("該日期無資料");
                const snapshot = await resp.json();

                renderAnalysisTable(snapshot.analysis_records || [], false, snapshot.analysis_log || [], snapshot.live_quotes || {{}});
                renderSettleTable(snapshot.settle_records || []);
                setSignalFilter(localStorage.getItem("daytrade_signal_filter") || "all");
            }} catch (e) {{
                errorMsg.classList.remove("hidden");
                document.getElementById("analysis-tbody").innerHTML =
                    '<tr><td colspan="7" class="py-6 text-center text-gray-500 text-xs">此日期尚無分析資料</td></tr>';
                document.getElementById("settle-tbody").innerHTML =
                    '<tr><td colspan="7" class="py-4 text-center text-gray-500 text-xs">此日期尚無結算資料</td></tr>';
            }} finally {{
                loadingMsg.classList.add("hidden");
            }}
        }}

        function escapeHtml(str) {{
            const div = document.createElement("div");
            div.textContent = str ?? "";
            return div.innerHTML;
        }}

        // 依訊號分類重新產生分析表格/卡片的 HTML，邏輯對應 Python 端 render_html_dashboard()
        // 的組裝方式，確保今日即時畫面與歷史查詢畫面呈現一致。同時更新桌面表格與手機卡片
        // 兩種畫面，因為 CSS 是用 media query 切換顯示/隱藏，兩者都要有內容。
        //
        // logRecords：當天（或所選歷史日期）「每一輪分析」的完整歷程，同一檔股票可能有多筆。
        // 用來在畫面上提供「展開查看歷史分析」功能，修復先前 latest_analysis 只保留最後一筆、
        // 中間分析輪次全部遺失看不到的問題。渲染時暫存到 CURRENT_LOG_BY_SYMBOL，供展開按鈕查詢。
        //
        // CURRENT_LIVE_QUOTES：{{symbol: {{price, updated_at}}}}，當天（或所選歷史日期）
        // 每檔股票最近一次的參考價，用來在展開歷史時對 BUY/SHORT 訊號試算損益。切換到
        // 歷史日期時會換成那個日期快照裡的 live_quotes（也就是當天收盤價），而不是今天
        // 的即時報價，避免用「今天的股價」誤算「過去某一天」的損益。
        let CURRENT_LOG_BY_SYMBOL = {{}};
        let CURRENT_LIVE_QUOTES = {{}};

        function buildLogBySymbol(logRecords) {{
            const map = {{}};
            (logRecords || []).forEach(lg => {{
                const sym = lg.symbol || "";
                if (!map[sym]) map[sym] = [];
                map[sym].push(lg);
            }});
            return map;
        }}

        function renderAnalysisTable(records, isToday, logRecords, liveQuotes) {{
            const tbody = document.getElementById("analysis-tbody");
            const cardsWrap = document.getElementById("analysis-cards");
            const emptyRowHtml = isToday
                ? '<tr><td colspan="7" class="py-6 text-center text-gray-500 text-xs">盤中每 10 分鐘自動更新分析看板...</td></tr>'
                : '<tr><td colspan="7" class="py-6 text-center text-gray-500 text-xs">此日期尚無分析資料</td></tr>';
            const emptyCardHtml = isToday
                ? '<div class="text-center text-gray-500 text-xs py-6">盤中每 10 分鐘自動更新分析看板...</div>'
                : '<div class="text-center text-gray-500 text-xs py-6">此日期尚無分析資料</div>';

            CURRENT_LOG_BY_SYMBOL = buildLogBySymbol(logRecords);
            CURRENT_LIVE_QUOTES = liveQuotes || {{}};

            if (!records || records.length === 0) {{
                tbody.innerHTML = emptyRowHtml;
                cardsWrap.innerHTML = emptyCardHtml;
                return;
            }}

            let rowsHtml = "";
            let cardsHtml = "";
            records.forEach(a => {{
                // .toUpperCase()：修復前寫入的舊資料 signal 欄位可能是小寫，
                // 統一轉大寫比對，新舊資料都能正確分類。
                const sig = (a.signal || "WATCH").toUpperCase();
                let filterGroup, badge;
                if (sig.includes("BUY")) {{
                    filterGroup = "long";
                    badge = '<span class="sig-badge sig-long">🔺 做多</span>';
                }} else if (sig.includes("SHORT")) {{
                    filterGroup = "short";
                    badge = '<span class="sig-badge sig-short">🔻 放空</span>';
                }} else {{
                    filterGroup = "watch";
                    badge = '<span class="sig-badge sig-watch">— 觀望</span>';
                }}

                const symbol = escapeHtml(a.symbol);
                const name = escapeHtml(a.name || "");
                const entry = escapeHtml(a.entry ?? "-");
                const stopLoss = escapeHtml(a.stop_loss ?? "-");
                const target = escapeHtml(a.target ?? "-");
                const reason = escapeHtml(a.reason || "");
                const updatedAt = escapeHtml(a.updated_at || "");

                const logCount = (CURRENT_LOG_BY_SYMBOL[a.symbol] || []).length;
                const historyBtn = logCount > 1
                    ? `<button type="button" class="history-toggle-btn" onclick="toggleHistoryLog(this, '${{symbol}}')">📜 歷史 ${{logCount}} 筆</button>`
                    : "";

                rowsHtml += `
                <tr class="hover:bg-white/[0.02] analysis-row" data-filter-group="${{filterGroup}}" data-symbol="${{symbol}}">
                    <td class="py-2.5 px-3 font-bold text-white whitespace-nowrap">${{symbol}} ${{name}}</td>
                    <td class="py-2.5 px-3">${{badge}}</td>
                    <td class="py-2.5 px-3 mono text-gray-200 whitespace-nowrap">${{entry}}</td>
                    <td class="py-2.5 px-3 mono text-[#00d68f] whitespace-nowrap">${{stopLoss}}</td>
                    <td class="py-2.5 px-3 mono text-[#ff5470] whitespace-nowrap">${{target}}</td>
                    <td class="py-2.5 px-3 text-gray-300 text-xs">${{reason}}</td>
                    <td class="py-2.5 px-3 text-gray-500 text-[11px] mono whitespace-nowrap">${{updatedAt}}${{historyBtn}}</td>
                </tr>
                <tr class="history-log-row hidden" data-symbol-log="${{symbol}}">
                    <td colspan="7" class="px-3 pb-3"><div class="history-log-container"></div></td>
                </tr>`;

                cardsHtml += `
                <div class="data-card analysis-row" data-filter-group="${{filterGroup}}" data-symbol="${{symbol}}">
                    <div class="flex items-center justify-between mb-2">
                        <span class="font-bold text-white text-sm">${{symbol}} ${{name}}</span>
                        ${{badge}}
                    </div>
                    <div class="data-row"><span class="dlabel">建議進場</span><span class="dvalue mono">${{entry}}</span></div>
                    <div class="data-row"><span class="dlabel">建議停損</span><span class="dvalue mono text-[#00d68f]">${{stopLoss}}</span></div>
                    <div class="data-row"><span class="dlabel">建議停利</span><span class="dvalue mono text-[#ff5470]">${{target}}</span></div>
                    <div class="data-row"><span class="dlabel">更新時間</span><span class="dvalue mono text-gray-500">${{updatedAt}}</span></div>
                    <div class="mt-2 pt-2 border-t border-white/5 text-xs text-gray-300 leading-relaxed">${{reason}}</div>
                    ${{historyBtn ? `<div class="mt-2 pt-2 border-t border-white/5">${{historyBtn}}<div class="history-log-container" data-symbol-log-card="${{symbol}}"></div></div>` : ""}}
                </div>`;
            }});
            tbody.innerHTML = rowsHtml;
            cardsWrap.innerHTML = cardsHtml;
        }}

        // 用最近一次抓到的參考價，試算某筆 BUY/SHORT 歷史分析紀錄目前的損益。
        // entry 欄位可能是數字，也可能是「-」或其他非數字字串（例如觀望紀錄，
        // 或是 AI 沒給出明確進場價時），這裡一律防呆，算不出來就回傳 null，
        // 呼叫端看到 null 就不顯示損益區塊，不會硬擠出一個誤導的數字。
        function calcLivePnl(logEntry) {{
            // .toUpperCase()：與上方 filterGroup 判斷同理，相容修復前寫入的小寫舊資料。
            const sig = (logEntry.signal || "WATCH").toUpperCase();
            const isLong = sig.includes("BUY");
            const isShort = sig.includes("SHORT");
            if (!isLong && !isShort) return null; // 觀望沒有進場動作，不算損益

            const entryPrice = parseFloat(logEntry.entry);
            if (!isFinite(entryPrice) || entryPrice <= 0) return null;

            const quote = CURRENT_LIVE_QUOTES[logEntry.symbol];
            if (!quote || !isFinite(quote.price)) return null;

            const currentPrice = quote.price;
            const diff = isLong ? (currentPrice - entryPrice) : (entryPrice - currentPrice);
            const pct = (diff / entryPrice) * 100;
            return {{
                currentPrice,
                diff,
                pct,
                asOf: quote.updated_at || "",
                isProfit: diff > 0,
                isFlat: diff === 0,
            }};
        }}

        // 產生「展開歷史」清單的內容：把某檔股票今天所有輪次的分析結果，
        // 依時間序列由舊到新條列出來，讓使用者能看到訊號/進場價如何隨盤勢變化。
        // 若該筆是 BUY/SHORT 訊號且拿得到參考價，額外附上「以最近報價試算」的損益。
        function renderHistoryLogEntries(symbol) {{
            const entries = CURRENT_LOG_BY_SYMBOL[symbol] || [];
            if (entries.length === 0) {{
                return '<div class="text-gray-500 text-xs py-2">尚無歷史分析紀錄</div>';
            }}
            return entries.map(lg => {{
                // .toUpperCase()：與上方同理，相容修復前寫入的小寫舊資料，
                // 這是「展開歷史分析」清單本體，先前訊號被誤判成觀望就是這裡的比對失敗。
                const sig = (lg.signal || "WATCH").toUpperCase();
                const badgeClass = sig.includes("BUY") ? "sig-long" : sig.includes("SHORT") ? "sig-short" : "sig-watch";
                const badgeText = sig.includes("BUY") ? "🔺 做多" : sig.includes("SHORT") ? "🔻 放空" : "— 觀望";

                const pnl = calcLivePnl(lg);
                let pnlHtml = "";
                if (pnl) {{
                    const pnlClass = pnl.isFlat ? "text-gray-400" : (pnl.isProfit ? "text-[#ff5470]" : "text-[#00d68f]");
                    const sign = pnl.diff > 0 ? "+" : "";
                    pnlHtml = `
                    <div class="live-pnl-box ${{pnlClass}}">
                        <span class="mono">現價 ${{escapeHtml(pnl.currentPrice)}}</span>
                        <span class="mono">${{sign}}${{pnl.diff.toFixed(2)}} (${{sign}}${{pnl.pct.toFixed(2)}}%)</span>
                        <span class="text-gray-500 text-[10px]">以 ${{escapeHtml(pnl.asOf)}} 報價試算</span>
                    </div>`;
                }}

                return `
                <div class="history-log-entry">
                    <div class="flex items-center justify-between gap-2">
                        <span class="mono text-gray-500 text-[11px] whitespace-nowrap">${{escapeHtml(lg.updated_at || "")}}</span>
                        <span class="sig-badge ${{badgeClass}} text-[10px]">${{badgeText}}</span>
                        <span class="mono text-gray-300 text-[11px] whitespace-nowrap">進場 ${{escapeHtml(lg.entry ?? "-")}}</span>
                    </div>
                    <div class="text-gray-400 text-[11px] mt-1 leading-relaxed">${{escapeHtml(lg.reason || "")}}</div>
                    ${{pnlHtml}}
                </div>`;
            }}).join("");
        }}

        // 點擊「📜 歷史 N 筆」按鈕：切換展開/收合該檔股票的完整分析歷程。
        // 桌面表格用隱藏列 (history-log-row)，手機卡片用卡片內的容器 (history-log-container)，
        // 兩處都要同步處理，因為兩者透過 CSS media query 切換顯示，使用者可能用任一種畫面操作。
        function toggleHistoryLog(btnEl, symbol) {{
            const isCard = btnEl.closest(".data-card") !== null;
            if (isCard) {{
                const container = btnEl.parentElement.querySelector(`[data-symbol-log-card="${{symbol}}"]`);
                if (!container) return;
                const isOpen = container.classList.toggle("open");
                container.innerHTML = isOpen ? renderHistoryLogEntries(symbol) : "";
                btnEl.classList.toggle("active", isOpen);
            }} else {{
                const logRow = document.querySelector(`tr.history-log-row[data-symbol-log="${{symbol}}"]`);
                if (!logRow) return;
                const container = logRow.querySelector(".history-log-container");
                const isOpen = logRow.classList.toggle("hidden") === false;
                container.innerHTML = isOpen ? renderHistoryLogEntries(symbol) : "";
                btnEl.classList.toggle("active", isOpen);
            }}
        }}

        // 依結算結果重新產生結算表格/卡片的 HTML，欄位對應 cache_service 產生的歷史紀錄格式。
        // 配色依台股慣例「紅漲綠跌」：獲利用紅、虧損用綠，跟一般西式股市配色相反。
        function renderSettleTable(records) {{
            const tbody = document.getElementById("settle-tbody");
            const cardsWrap = document.getElementById("settle-cards");
            if (!records || records.length === 0) {{
                const emptyMsg = '此日期尚無結算資料';
                tbody.innerHTML = `<tr><td colspan="7" class="py-4 text-center text-gray-500 text-xs">${{emptyMsg}}</td></tr>`;
                cardsWrap.innerHTML = `<div class="text-center text-gray-500 text-xs py-4">${{emptyMsg}}</div>`;
                return;
            }}

            let rowsHtml = "";
            let cardsHtml = "";
            // 出場原因代碼 → 中文對照，與 Python 端 EXIT_REASON_LABELS 保持一致。
            const EXIT_REASON_LABELS = {{
                "hit_tp": "✅ 觸及停利",
                "hit_sl": "🛑 觸及停損",
                "forced_close": "⏱ 收盤強制平倉",
            }};
            records.forEach(r => {{
                const isWin = (r.pnl_amount ?? 0) > 0;
                const isLoss = (r.pnl_amount ?? 0) < 0;
                const resultClass = isWin ? "text-[#ff5470]" : (isLoss ? "text-[#00d68f]" : "text-gray-400");
                const badgeClass = isWin ? "sig-long" : (isLoss ? "sig-short" : "sig-watch");
                // 【bug修復】r.result 存的是 "win"/"loss"/"breakeven" 英文值，原本
                // `r.result || (...)` 只要 r.result 有值就會直接顯示英文單字，
                // 後面判斷 isWin/isLoss 的中文分支永遠是 dead code。改成明確查表轉中文。
                const RESULT_LABELS = {{ win: "獲利", loss: "虧損", breakeven: "持平" }};
                const resultText = RESULT_LABELS[r.result] || (isWin ? "獲利" : (isLoss ? "虧損" : "持平"));
                const pnlDisplay = (r.pnl_amount !== null && r.pnl_amount !== undefined)
                    ? `${{r.pnl_amount > 0 ? "+" : ""}}${{r.pnl_amount}}` : "-";
                const badge = `<span class="sig-badge ${{badgeClass}}">${{escapeHtml(resultText)}}</span>`;

                const symbol = escapeHtml(r.symbol);
                const direction = escapeHtml(r.direction || "-");
                const entryPrice = escapeHtml(r.entry_price ?? "-");
                const exitPrice = escapeHtml(r.exit_price ?? "-");
                // 【bug修復】同上，exit_reason 原本直接顯示 hit_sl 這種英文代碼，改為中文。
                const exitReason = escapeHtml(EXIT_REASON_LABELS[r.exit_reason] || r.exit_reason || "-");

                rowsHtml += `
                <tr class="hover:bg-white/[0.02]">
                    <td class="py-2 px-3 font-bold text-white whitespace-nowrap">${{symbol}}</td>
                    <td class="py-2 px-3 whitespace-nowrap">${{direction}}</td>
                    <td class="py-2 px-3 mono whitespace-nowrap">${{entryPrice}}</td>
                    <td class="py-2 px-3 mono whitespace-nowrap">${{exitPrice}}</td>
                    <td class="py-2 px-3">${{badge}}</td>
                    <td class="py-2 px-3 mono font-semibold ${{resultClass}} whitespace-nowrap">${{pnlDisplay}}</td>
                    <td class="py-2 px-3 text-gray-400 text-xs">${{exitReason}}</td>
                </tr>`;

                cardsHtml += `
                <div class="data-card">
                    <div class="flex items-center justify-between mb-2">
                        <span class="font-bold text-white text-sm">${{symbol}}　<span class="text-gray-400 font-normal text-xs">${{direction}}</span></span>
                        ${{badge}}
                    </div>
                    <div class="data-row"><span class="dlabel">進場 → 出場</span><span class="dvalue mono">${{entryPrice}} → ${{exitPrice}}</span></div>
                    <div class="data-row"><span class="dlabel">淨損益</span><span class="dvalue mono font-bold ${{resultClass}}">${{pnlDisplay}}</span></div>
                    <div class="data-row"><span class="dlabel">出場原因</span><span class="dvalue text-xs">${{exitReason}}</span></div>
                </div>`;
            }});
            tbody.innerHTML = rowsHtml;
            cardsWrap.innerHTML = cardsHtml;
        }}

        // ── 訊號篩選：做多 / 做空 / 觀望 / 全部 ─────────────────────────
        // 篩選狀態保存在 localStorage，重新整理頁面（每 60 秒自動刷新）後仍會記住上次的選擇
        function initSignalFilter() {{
            const saved = localStorage.getItem("daytrade_signal_filter") || "all";
            setSignalFilter(saved);
        }}

        function setSignalFilter(group) {{
            localStorage.setItem("daytrade_signal_filter", group);

            // 桌面表格與手機卡片都有各自一份 .analysis-row，兩者需要同步套用篩選狀態，
            // 否則手機版切換篩選按鈕會沒有反應（這是先前版本的疏漏，這次一併修正）
            const rows = document.querySelectorAll(".analysis-row");
            const counts = {{ all: 0, long: 0, short: 0, watch: 0 }};
            let visibleCount = 0;

            rows.forEach(row => {{
                const rowGroup = row.getAttribute("data-filter-group");
                if (rowGroup && counts.hasOwnProperty(rowGroup)) {{
                    counts[rowGroup]++;
                    counts.all++;
                }}
                const shouldShow = (group === "all") || (rowGroup === group);
                row.style.display = shouldShow ? "" : "none";
                if (shouldShow) visibleCount++;
            }});

            // counts 統計了表格版+卡片版兩份重複的列，這裡除以 2 還原成實際筆數
            ["all", "long", "short", "watch"].forEach(g => {{
                const el = document.getElementById(`count-${{g}}`);
                if (el) el.textContent = `(${{Math.floor(counts[g] / 2)}})`;
            }});

            // 更新按鈕選取樣式
            ["all", "long", "short", "watch"].forEach(g => {{
                const btn = document.getElementById(`filter-btn-${{g}}`);
                if (!btn) return;
                btn.classList.remove("active-all", "active-long", "active-short", "active-watch");
                if (g === group) btn.classList.add(`active-${{g}}`);
            }});

            // 空狀態提示：若表格原本就沒有任何資料列（尚未產生分析），不顯示「無符合資料」訊息
            const emptyMsg = document.getElementById("filter-empty-msg");
            if (emptyMsg) {{
                emptyMsg.classList.toggle("hidden", !(rows.length > 0 && visibleCount === 0));
            }}
        }}
    </script>
</body>
</html>
"""
    try:
        with open("index.html", "w", encoding="utf-8") as f:
            f.write(html_content)
    except Exception as e:
        print(f"[HTML Dashboard] 寫入失敗: {e}")
        return

    try:
        print("📄 已成功更新獨立網頁儀表板：index.html")
    except Exception:
        print("[HTML Dashboard] 已成功更新獨立網頁儀表板：index.html")

# 保留別名相容性
update_html_dashboard = render_html_dashboard

STATE_FILE = "dashboard_state.json"

# 收盤回放結算的「出場原因」代碼 → 中文顯示文字對照表。
# 來源：cache_service._compute_settle_result()，該函式只會產生
# "hit_tp"（觸及停利）/ "hit_sl"（觸及停損）/ "forced_close"（當天都沒
# 觸及、用收盤價強制平倉）這三種值。原本畫面直接把這串英文代碼原封
# 不動塞進「出場原因」欄位，跟頁面其他地方（訊號徽章、AI理由文字）
# 都是中文的風格不一致，一般使用者也看不懂 hit_sl 是什麼意思。
EXIT_REASON_LABELS = {
    "hit_tp": "✅ 觸及停利",
    "hit_sl": "🛑 觸及停損",
    "forced_close": "⏱ 收盤強制平倉",
}


def format_exit_reason(code: str) -> str:
    """把 exit_reason 代碼轉成中文顯示文字；未知值原樣顯示，避免吃掉除錯線索。"""
    if not code or code == "-":
        return "-"
    return EXIT_REASON_LABELS.get(code, code)



def load_dashboard_state(today_str: str, gemini=None, fugle=None, cfg: Optional[Dict] = None) -> Dict:
    """
    讀取上一輪次留下的看板狀態。若狀態檔不存在，或存的是「不同日期」的舊資料
    (例如今天是新的交易日，但檔案還留著昨天收盤的紀錄)，則回傳全新的空白狀態，
    避免不同交易日的資料互相混雜。

    【v21 修正：跨日搶救性結算】────────────────────────────────────
    背景：原本偵測到「狀態檔案日期 ≠ 今天」時，會直接捨棄舊狀態、回傳全新
    空白狀態。但如果前一天因為任何原因（cron 排程延遲、API 逾時、13:25~
    13:30 的結算視窗剛好沒被觸發到）沒有成功跑完 13:25 收盤結算，
    settled_today 會一直停在 False，這份累積了一整天的分析紀錄
    （wave1_stocks、latest_analysis_records、analysis_log 等）就會在
    「今天第一次執行、偵測到跨日」的當下被直接丟棄，從來沒有機會寫進
    history_records/analysis_YYYY-MM-DD.json，導致那一天的歷史永久消失、
    查看日期下拉選單裡也就看不到那天。

    修法：偵測到跨日時，若舊狀態顯示「有跑過盤中流程但尚未結算完成」
    （wave1_stocks 非空且 settled_today 為 False），且呼叫端有提供
    gemini / fugle 兩個依賴，就先用舊狀態補跑一次收盤結算，把那一天的
    快照存進 history_records/，搶救性地留下歷史紀錄，再回傳全新的今日
    狀態。gemini / fugle 任一為 None 時（呼叫端沒有提供，或初始化失敗）
    則略過搶救、比照舊行為直接重置，不會因此拋錯。
    """
    default_state = {
        "date": today_str,
        "wave1_stocks": [],
        "wave2_stocks": [],
        "mid_wave_triggered": False,
        "latest_analysis_records": [],  # 累積型：同一檔股票用 symbol 當 key 覆蓋更新，只代表「目前最新狀態」
        "analysis_log": [],  # 完整歷程型：每一輪分析都 append 一筆，不覆蓋，供回溯當天每檔股票的完整分析歷程
        "live_quotes": {},  # 每檔監控股票「最近一次分析當下」的參考價，key 是 symbol，
                             # 值為 {"price": float, "updated_at": "HH:MM:SS"}。用途是讓網頁在
                             # 「展開歷史分析」清單裡，對有 BUY/SHORT 訊號的紀錄即時算出損益，
                             # 不需要前端另外連線報價來源（GitHub Pages 是純靜態網站，前端沒有
                             # 管道可以直接呼叫需要金鑰的 Fugle API）。
        "total_signals": 0,
        "last_analysis_minute_bucket": None,  # 記錄上次執行過分析的時間戳記 (YYYY-MM-DD HH:MM)，用於判斷距今是否已滿 10 分鐘
        "settled_today": False,  # 今日是否已完成 13:25 收盤結算，避免收盤後的非盤中測試模式覆蓋掉正式看板
    }
    if not os.path.exists(STATE_FILE):
        print(f"ℹ️ {STATE_FILE} 不存在，視為今日第一次執行，建立全新狀態。")
        return default_state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        if not raw.strip():
            print(f"⚠️ {STATE_FILE} 是空檔案（可能是上一輪寫入中斷），改用全新狀態。")
            return default_state
        state = json.loads(raw)
        if state.get("date") != today_str:
            stale_date = state.get("date")
            # 跨日了：先看看昨天的狀態是不是「有跑過盤中流程，但還沒結算完」，
            # 若是，搶救性地補跑一次結算，把那一天的資料存進歷史快照，
            # 避免直接重置導致那天的分析紀錄整個消失、之後查看日期永遠找不到。
            if (stale_date and state.get("wave1_stocks") and not state.get("settled_today")
                    and gemini is not None and fugle is not None):
                print(f"⚠️ 偵測到狀態檔案為前一交易日 ({stale_date}) 的資料，且尚未完成收盤結算"
                      f"（可能昨天 13:25~13:30 的結算視窗剛好沒有排程準時觸發成功）。"
                      f"為避免 {stale_date} 的分析紀錄整個遺失，先搶救性地補跑一次收盤結算...")
                try:
                    # 幫舊狀態補齊可能缺少的欄位，避免 run_settlement 內部存取欄位時 KeyError
                    for key, default_val in default_state.items():
                        if key not in state:
                            state[key] = default_val
                    run_settlement(
                        state, stale_date, gemini, fugle,
                        state.get("wave1_stocks", []),
                        state.get("wave2_stocks", []),
                        state.get("latest_analysis_records", []),
                        state.get("analysis_log", []),
                        state.get("live_quotes", {}),
                        state.get("total_signals", 0),
                        cfg,
                    )
                    print(f"✅ {stale_date} 的搶救性收盤結算已完成並存入歷史快照。")
                except Exception as e:
                    print(f"⚠️ {stale_date} 的搶救性收盤結算失敗，該日資料可能無法補救: {e}")
                finally:
                    # run_settlement() 內部會把傳入的 state（帶著 stale_date 舊日期）
                    # 寫回 STATE_FILE。不論搶救結算成功與否，這裡都要立刻把 STATE_FILE
                    # 覆蓋回「今天」的全新狀態，避免檔案系統上殘留昨天的日期，
                    # 導致下一輪執行又誤判一次跨日、甚至反覆嘗試搶救。
                    save_dashboard_state(default_state)
            print(f"ℹ️ 偵測到狀態檔案為前一交易日 ({stale_date}) 的資料，重置為今日 ({today_str}) 全新狀態。")
            return default_state
        # 補齊欄位：若讀到的是舊版 state（缺少新增欄位），用預設值補上，避免 KeyError
        for key, default_val in default_state.items():
            if key not in state:
                state[key] = default_val
        # 基本合理性檢查：latest_analysis_records / analysis_log 應該是 list，live_quotes
        # 應該是 dict，若型別跑掉（代表檔案可能在一次失敗的 git rebase/merge 中被寫壞），
        # 寧可用空狀態重跑，也不要帶著壞資料繼續污染。
        if not isinstance(state.get("latest_analysis_records"), list):
            print(f"⚠️ {STATE_FILE} 內 latest_analysis_records 型別異常，判定檔案已損毀，改用全新狀態。")
            return default_state
        if not isinstance(state.get("analysis_log"), list):
            print(f"⚠️ {STATE_FILE} 內 analysis_log 型別異常，判定檔案已損毀，改用全新狀態。")
            return default_state
        if not isinstance(state.get("live_quotes"), dict):
            print(f"⚠️ {STATE_FILE} 內 live_quotes 型別異常，判定檔案已損毀，改用全新狀態。")
            return default_state
        print(f"✅ 成功讀取上一輪狀態：累積分析 {len(state.get('latest_analysis_records', []))} 檔、"
              f"完整分析歷程 {len(state.get('analysis_log', []))} 筆、"
              f"參考股價 {len(state.get('live_quotes', {}))} 檔、"
              f"已記錄訊號 {state.get('total_signals', 0)} 筆、上次分析時間戳記={state.get('last_analysis_minute_bucket')}")
        return state
    except Exception as e:
        print(f"⚠️ 讀取 {STATE_FILE} 失敗，改用全新狀態: {e}")
        return default_state

def save_dashboard_state(state: Dict):
    """
    寫入 dashboard_state.json。採用「先寫暫存檔、成功後再原子性覆蓋」的方式，
    避免寫到一半被中斷（例如 runner 被砍掉）導致檔案內容殘缺、下一輪讀到半殘 JSON。
    """
    tmp_path = STATE_FILE + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, STATE_FILE)
    except Exception as e:
        print(f"⚠️ 寫入 {STATE_FILE} 失敗: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

def upsert_analysis_record(records: List[Dict], new_record: Dict) -> List[Dict]:
    """
    將本次分析結果併入累積清單：同一檔股票(symbol)存在就覆蓋更新為最新結果，
    不存在就新增一筆，藉此讓網站的「即時總覽」顯示每一檔股票目前最新狀態，
    而不是每一輪的舊資料一直往下疊。

    注意：這個函式只維護「最新狀態」快照，不是完整歷程。完整的每一輪分析
    紀錄由 append_analysis_log() 另外累積保存，兩者並存、互不取代。
    """
    updated = False
    for i, r in enumerate(records):
        if r.get("symbol") == new_record.get("symbol"):
            records[i] = new_record
            updated = True
            break
    if not updated:
        records.append(new_record)
    return records

def append_analysis_log(log: List[Dict], new_record: Dict) -> List[Dict]:
    """
    將本次分析結果「附加」進完整歷程清單，同一檔股票被重複分析多次時，
    每一輪都各自保留一筆（不覆蓋），讓使用者能回溯當天某檔股票每 10 分鐘
    的分析變化，而不是只看到收盤前最後一次的結果。

    這是為了修復先前的問題：upsert_analysis_record() 的覆蓋式更新，
    導致同一檔股票中間所有分析輪次都被悄悄蓋掉、收盤快照也只存到
    「最後一筆」，看起來就像「明明分析了一整天、卻只保存了一筆」。
    """
    log.append(new_record)
    return log

def save_daily_history_snapshot(
    date_str: str,
    analysis_records: List[Dict],
    settle_records: List[Dict],
    analysis_log: List[Dict] = None,
    live_quotes: Dict = None,
):
    """
    收盤結算時呼叫：將當天的完整分析紀錄 (含觀望) 與結算損益，
    寫成 history_records/analysis_YYYY-MM-DD.json，並更新
    history_records/index.json 這份「有哪些日期可查」的索引檔。

    GitHub Pages 是純靜態網站，前端 JavaScript 沒辦法直接列出
    history_records/ 資料夾底下有哪些檔案，所以需要額外維護
    這份 index.json，供 index.html 的日期下拉選單讀取。

    analysis_records：每檔股票的「最新狀態」快照（供總覽表格顯示）。
    analysis_log：當天每一輪分析的完整歷程（不覆蓋），供「展開查看歷史分析」
    功能依 symbol 分組、按時間序列呈現，修復先前只保存最後一筆的問題。
    live_quotes：{symbol: {"price", "updated_at"}}，收盤時最後更新的參考價
    （run_settlement 會把它更新成當天最後一根分K的收盤價），供之後查詢這一天的
    歷史紀錄時，也能用「收盤價」試算 BUY/SHORT 訊號的損益。
    """
    os.makedirs("history_records", exist_ok=True)

    snapshot = {
        "date": date_str,
        "analysis_records": analysis_records,
        "analysis_log": analysis_log or [],
        "live_quotes": live_quotes or {},
        "settle_records": settle_records,
        "saved_at": get_tw_now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    snapshot_path = f"history_records/analysis_{date_str}.json"
    try:
        with open(snapshot_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        print(f"✅ 已保存當日分析快照：{snapshot_path}")
    except Exception as e:
        print(f"⚠️ 寫入 {snapshot_path} 失敗: {e}")

    # 更新日期索引檔（v21 修正：改為「掃描資料夾實際檔案」重建索引，
    # 不再只靠讀取舊 index.json 內容 append）─────────────────────────
    # 背景：先前的寫法是「讀取既有 index.json → 把今天加進去 → 寫回」，
    # 這個「讀改寫」模式在雲端環境隱藏了一個風險：如果任何一次執行
    # checkout 下來的 index.json 因為 git 時序問題（例如同一天內高頻率
    # 執行、rebase 重試、或跨日交界時的 race condition）而是舊版、空白
    # 或缺漏，那次執行就會誤判成「只有今天」，把過去累積好幾週的日期
    # 直接覆蓋消失——即使 git push 本身完全成功，資料還是會不見，
    # 因為問題發生在寫入內容的當下，不是發生在 push 失敗。
    #
    # 修正做法：與其信任一份容易失真的獨立索引檔，不如每次都直接掃描
    # history_records/ 資料夾裡實際存在哪些 analysis_YYYY-MM-DD.json
    # 檔案，用檔名反推出日期清單來重建 index.json。只要那些日期的快照
    # 檔案本身還在 repo 裡（它們不會被覆蓋，每天各自獨立一個檔名），
    # 索引檔就一定能正確反映所有歷史日期，不會再因為索引檔本身的讀寫
    # 時序問題而遺失過去的資料。
    index_path = "history_records/index.json"
    dates = _scan_history_snapshot_dates()
    if date_str not in dates:
        dates.append(date_str)
    dates.sort(reverse=True)

    try:
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump({"dates": dates}, f, ensure_ascii=False, indent=2)
        print(f"✅ 已更新歷史日期索引：{index_path} (共 {len(dates)} 天，依資料夾實際檔案重建)")
    except Exception as e:
        print(f"⚠️ 寫入 {index_path} 失敗: {e}")


def _scan_history_snapshot_dates() -> List[str]:
    """
    掃描 history_records/ 資料夾裡實際存在的 analysis_YYYY-MM-DD.json
    檔案，回傳其中的日期字串清單（未排序）。用來重建 index.json，
    避免直接信任舊索引檔內容導致歷史日期不小心被覆蓋遺失（詳見
    save_daily_history_snapshot() 內的說明）。檔名格式不符的檔案
    會被略過，不會讓整個掃描中斷。
    """
    pattern = re.compile(r"^analysis_(\d{4}-\d{2}-\d{2})\.json$")
    dates: List[str] = []
    try:
        for fname in os.listdir("history_records"):
            m = pattern.match(fname)
            if m:
                dates.append(m.group(1))
    except FileNotFoundError:
        pass
    return dates

def get_free_top_volume_stocks(limit: int = 8, min_price: float = 10.0, min_pool_size: int = 25) -> List[Dict]:
    """
    自 Yahoo 奇摩股市抓取即時成交量排行榜。
    特點：
    1. 動態定位代號 (.TW / .TWO)，防止因名次圖示或排版微調造成欄位偏移
    2. 正確解析真實成交價 (price) 與成交量 (volume，單位：張)
    3. 自動排除 00 開頭 ETF、特別股及 6 碼權證衍生品
    4. 支援 min_price 門檻 (預設 10.0 元，兼顧流動性並避免過度排除如 14 元熱門股)
    5. 自行依真實成交量由大到小降冪排序，確保精準取得前 limit 檔熱門標的

    v4.1 修正說明：
    ────────────
    舊版只抓榜單第一頁（通常僅 10~20 筆原始資料），扣掉其中常見的 00 開頭 ETF
    （如 0050、00919 等，成交量經常擠進榜單前段）與少數權證後，篩選完往往只
    剩 3 檔左右可用，遠低於預期的 limit 檔數。
    新版改為「先擴大抓取候選池（自動翻頁直到候選池 >= min_pool_size 或無更多資料），
    篩選完再依成交量排序取前 limit 檔」，確保篩選後仍有足夠檔數可選。
    """
    base_url = "https://tw.stock.yahoo.com/rank/volume"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
    candidates = []
    max_pages = 6  # 保護機制：最多翻 6 頁 (約 120~180 筆原始資料)，避免候選池目標設太高時無限翻頁

    try:
        for page in range(1, max_pages + 1):
            # Yahoo 股市排行頁面以 ?page=N 分頁，第 1 頁可省略參數
            url = base_url if page == 1 else f"{base_url}?page={page}"
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")
            rows = soup.find_all("li", class_=lambda c: c and "List(n)" in c)

            if not rows:
                # 這一頁已經沒有資料列，代表榜單到底了，不用再往後翻頁
                print(f"[Yahoo排行] 第 {page} 頁無資料，停止翻頁 (榜單已到底)")
                break

            page_found = 0
            for row_idx, r in enumerate(rows):
                texts = [t.strip() for t in r.stripped_strings]

                # 動態尋找包含 .TW 或 .TWO 的代號欄位索引
                sym_idx = next((i for i, t in enumerate(texts) if t.endswith(".TW") or t.endswith(".TWO")), -1)
                if sym_idx == -1:
                    continue

                symbol_raw = texts[sym_idx]
                symbol = symbol_raw.split(".")[0]

                # 避免同一檔股票因翻頁重疊等因素被重複加入候選池
                if any(c["symbol"] == symbol for c in candidates):
                    continue

                name = texts[sym_idx - 1] if sym_idx > 0 else symbol
                rank = texts[sym_idx - 2] if sym_idx >= 2 else str(row_idx + 1)

                # 排除 ETF 等 00 開頭商品
                if symbol.startswith("00"):
                    continue

                # 排除權證 (台股權證為6碼) 與非數字商品，保留 4~5 碼普通股票
                if not symbol.isdigit() or len(symbol) > 5:
                    continue

                try:
                    # 價格位於代號後方一位 (sym_idx + 1)
                    price = float(texts[sym_idx + 1].replace(",", ""))
                    # 成交量位於 sym_idx + 7 (亦常為倒數第二欄)
                    volume_str = texts[sym_idx + 7] if len(texts) > sym_idx + 7 else texts[-2]
                    volume = int(volume_str.replace(",", ""))
                except (ValueError, IndexError):
                    continue

                if price < min_price:
                    continue

                candidates.append({
                    "symbol": symbol,
                    "name": name,
                    "price": price,
                    "volume": volume,
                    "rank": rank,
                })
                page_found += 1

            print(f"[Yahoo排行] 第 {page} 頁篩選後新增 {page_found} 檔，候選池累計 {len(candidates)} 檔")

            # 候選池已經夠大，不用再翻下一頁
            if len(candidates) >= min_pool_size:
                break

        # 明確依真實成交量 (volume) 重新排序，不單純盲目依賴網頁預設順序
        candidates.sort(key=lambda item: item["volume"], reverse=True)
        result = candidates[:limit]

        if len(result) < limit:
            # 候選池篩選後仍不足 limit 檔，印出警告方便從 log 判斷原因
            # (常見原因：當天大量個股跌破 min_price 門檻、或 Yahoo 頁面結構有變動導致解析失敗)
            print(f"⚠️ [Yahoo排行] 篩選後僅取得 {len(result)} 檔，未達目標 {limit} 檔 (候選池總數: {len(candidates)})")

        return result

    except requests.RequestException as e:
        print(f"[Yahoo排行] 網路請求錯誤: {e}")
    except Exception as e:
        print(f"[Yahoo排行] 資料解析錯誤: {e}")

    return []


def filter_out_limit_up_stocks(stocks: List[Dict], fugle, limit: int) -> List[Dict]:
    """
    從選股結果中排除「已經漲停」的股票，並依候選池排名遞補下一名補齊，
    確保最終回傳的檔數仍盡量湊滿 limit。

    背景：get_free_top_volume_stocks() 資料源是 Yahoo 成交量排行榜，這個
    榜單頁面本身不提供漲跌停價格，所以沒辦法在爬蟲階段就直接判斷。
    這裡改用已經在 main() 中建立好的 FugleService 物件，對選出的候選股票
    逐一查詢即時報價（含昨收價 previousClose），用 gemini_service 裡
    既有、已經在 AI 分析階段使用的 _calc_limit_prices()/_check_at_limit()
    算出漲停價並比對，兩邊判斷標準保持一致，不會出現「選股階段判斷跟
    AI 分析階段判斷用不同公式，結果對不起來」的情況。

    為何要在選股階段就排除，而不是只靠 AI 分析階段的觀望標記：
    漲停股當沖本來就難以成交（委買單大量堆積、賣盤稀少），選進來只會
    白白佔用一個分析名額、耗用一次 Gemini API 額度，最後也只能得到
    強制觀望的結果。選股階段先濾掉，把名額留給真正有機會成交的股票。

    只對「最終入選的候選股票」逐一查詢，而非整個候選池，藉此控制
    Fugle API 呼叫次數（原本 stocks 已經是排序、篩選過、恰好 limit 檔
    或不足 limit 檔的最終結果，只有在有股票被排除時才會用到候選池
    之外的遞補資料，但目前呼叫端沒有把完整候選池傳進來，遞補只能
    在「這批 stocks 本身」範圍內進行——若排除後仍不足 limit，屬於
    正常情況，get_free_top_volume_stocks() 本來就可能因候選池不足
    而回傳少於 limit 檔，不強求一定要湊滿）。
    """
    if not stocks:
        return stocks

    kept = []
    excluded = []
    for s in stocks:
        symbol = s["symbol"]
        try:
            quote = fugle.get_intraday_quote(symbol) or {}
            prev_close = quote.get("previousClose")
            current_price = s.get("price")
            limits = _calc_limit_prices(prev_close) if prev_close else None
            if limits:
                limit_up, limit_down = limits
                at_limit = _check_at_limit(current_price, limit_up, limit_down)
                if at_limit == "up":
                    excluded.append(s)
                    print(f"   🚫 [排除漲停股] {symbol} {s.get('name', '')} 現價 {current_price} 已達漲停 {limit_up}，不納入當沖標的")
                    continue
            kept.append(s)
        except Exception as e:
            # 查詢失敗（例如 API 額度用盡、逾時）時保守起見不排除，
            # 避免因為查詢異常就誤刪原本正常的候選股票。
            print(f"   ⚠️ [排除漲停股] {symbol} 查詢即時報價失敗，保留原判斷: {e}")
            kept.append(s)

    if excluded:
        print(f"   ℹ️ [排除漲停股] 本輪共排除 {len(excluded)} 檔已漲停股票，剩餘 {len(kept)} 檔可用（目標 {limit} 檔）")

    return kept[:limit]

def run_settlement(state: Dict, today_str: str, gemini, fugle, wave1_stocks: List[Dict], wave2_stocks: List[Dict],
                    latest_analysis_records: List[Dict], analysis_log: List[Dict], live_quotes: Dict,
                    total_signals: int, cfg: Optional[Dict] = None):
    """
    執行收盤回放結算：把當天所有 pending 的下單訊號跟分K比對算出損益，
    存成 CSV 報表，並把當日完整分析紀錄/歷程存成歷史快照，最後把
    index.html 換成「已收盤結算完成」的正式畫面。

    cfg：main() 讀到的使用者設定（load_config() 結果），內含
    broker_discount（手續費折扣）與 is_day_trade_tax（當沖證交稅
    減半），會傳入 cache_service 讓結算損益扣除實際交易成本、
    真正變成「淨損益」而不是價差毛額。

    這段邏輯獨立抽成函式，是因為結算判斷式 `hm >= "13:25"` 原本只有在
    is_market_session（08:50~13:30）範圍內才會被檢查到，一旦 13:25~13:30
    這個 5 分鐘視窗剛好沒有任何一次排程準時觸發成功（GitHub Actions 排隊
    延遲、API 逾時等），收盤結算就會被永久錯過，settled_today 永遠是
    False，之後每一輪都會被判定為「非盤中時段」，被測試模式的畫面覆蓋掉。
    抽成獨立函式後，main() 除了在盤中視窗內呼叫一次，也能在盤後任何
    時間點（只要偵測到今天尚未結算過）補跑這個函式，修復「明明已經收盤
    卻一直顯示測試資料」的問題。
    """
    pending_records = cache_service.get_pending_history_for_date(today_str)
    print(f"\n🎯 開始收盤分K回放結算，今日待結算筆數: {len(pending_records)}")

    today_settled_list = []
    if pending_records:
        for rec in pending_records:
            sym = rec["symbol"]
            candles_raw = fugle.get_intraday_candles(sym, force_refresh=True)
            day_candles = candles_raw.get("data", []) if candles_raw else []
            if day_candles:
                cache_service.settle_history_record_with_candles(rec["id"], day_candles, cfg)
                # 結算時順便把這檔股票的參考價更新成「當天最後一根分K的收盤價」，
                # 也就是真正的收盤價，讓收盤後看「展開歷史分析」時算出來的損益
                # 是以收盤價計算，而不是停留在盤中最後一次分析時的價格。
                live_quotes[sym] = {
                    "price": day_candles[-1]["close"],
                    "updated_at": "13:30:00",
                }

        all_data = cache_service._read_history()
        today_settled_list = [r for r in all_data.get("records", []) if r.get("date") == today_str]

        os.makedirs("history_records", exist_ok=True)
        df = pd.DataFrame(today_settled_list)
        csv_path = f"history_records/backtest_{today_str}.csv"
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(f"✅ 今日回測報表已成功產出：{csv_path}")
    else:
        # 沒有待結算資料，也可能代表今天已經結算過了；仍讀取既有結算清單顯示在網站上
        all_data = cache_service._read_history()
        today_settled_list = [r for r in all_data.get("records", []) if r.get("date") == today_str]

    # 將當日累積的盤中分析紀錄 (latest_analysis_records，含觀望在內)、完整分析歷程
    # (analysis_log，同一檔股票每一輪都保留、不覆蓋) 與收盤參考價 (live_quotes)
    # 一併保存成 history_records/analysis_YYYY-MM-DD.json，供網頁日後切換日期時
    # 查看完整分析過程與收盤損益，而不是只能看到 backtest CSV 裡「有實際下單訊號」
    # 的部分，也不會只剩最後一筆。
    save_daily_history_snapshot(today_str, latest_analysis_records, today_settled_list, analysis_log, live_quotes)

    # 標記今日已完成收盤結算：往後收盤後若 cron 仍持續觸發，main() 開頭的
    # 非盤中測試模式會讀到這個旗標，直接跳過、不再覆蓋這份正式的收盤結算頁面。
    state["settled_today"] = True
    state["live_quotes"] = live_quotes

    render_html_dashboard(
        status_text="已收盤結算完成",
        active_model=gemini.active_model,
        wave1_stocks=wave1_stocks,
        wave2_stocks=wave2_stocks,
        latest_analysis=latest_analysis_records,
        analysis_log=analysis_log,
        live_quotes=live_quotes,
        settle_records=today_settled_list,
        total_signals=total_signals
    )
    save_dashboard_state(state)
    print("✅ 本輪次（收盤結算）執行完畢。")



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

    # ── 風險模式設定（可用 RISK_MODE 環境變數覆蓋，預設 auto）──────────
    # 合法值：aggressive / conservative / auto / relaxed（實驗性寬鬆模式）
    VALID_RISK_MODES = {"aggressive", "conservative", "auto", "relaxed"}
    RISK_MODE = (os.getenv("RISK_MODE") or cfg.get("risk_mode") or "auto").strip().lower()
    if RISK_MODE not in VALID_RISK_MODES:
        print(f"⚠️ 未知的 RISK_MODE 設定值「{RISK_MODE}」，已自動回退為 auto")
        RISK_MODE = "auto"
    if RISK_MODE == "relaxed":
        print("🧪 目前使用【實驗性寬鬆模式】：訊號門檻降低，訊號數量會明顯變多，僅建議測試用途。")
    else:
        print(f"⚙️ 目前使用風險模式：{RISK_MODE}")

    # ── 手續費折扣 / 當沖證交稅設定（v20 新增，可用環境變數覆蓋）───────
    # config.json 已被 .gitignore 排除、不會推上雲端，雲端這裡永遠只會
    # 讀到 DEFAULT_CONFIG 裡的預設折扣值。為了讓使用者不用碰版控檔案
    # 也能調整自己實際的手續費折數，比照 RISK_MODE 的做法，開放用
    # GitHub Actions 的 Repo Variables（Settings > Secrets and
    # variables > Actions > Variables）設定 BROKER_DISCOUNT /
    # IS_DAY_TRADE_TAX 來覆蓋，沒有設定時才退回 config.py 的預設值。
    # 折扣範圍強制夾在 0.1~1.0 之間，避免打錯數字（例如打成 60 而不是
    # 0.6）導致手續費暴增或變成負數。
    try:
        broker_discount_raw = os.getenv("BROKER_DISCOUNT")
        broker_discount = float(broker_discount_raw) if broker_discount_raw not in (None, "") else float(cfg.get("broker_discount", 1.0))
    except (TypeError, ValueError):
        print(f"⚠️ BROKER_DISCOUNT 設定值「{broker_discount_raw}」無法解析為數字，已改用預設值。")
        broker_discount = float(cfg.get("broker_discount", 1.0))
    broker_discount = max(0.1, min(1.0, broker_discount))
    cfg["broker_discount"] = broker_discount

    is_day_trade_tax_raw = os.getenv("IS_DAY_TRADE_TAX")
    if is_day_trade_tax_raw is not None and is_day_trade_tax_raw != "":
        is_day_trade_tax = is_day_trade_tax_raw.strip().lower() in ("1", "true", "yes", "on")
    else:
        is_day_trade_tax = bool(cfg.get("is_day_trade_tax", True))
    cfg["is_day_trade_tax"] = is_day_trade_tax

    tax_rate_display = "0.15%（當沖減半）" if is_day_trade_tax else "0.3%（一般稅率）"
    print(f"💰 手續費折扣：{broker_discount:.2f}（單向費率 0.1425% × {broker_discount:.2f}）　"
          f"證交稅率：{tax_rate_display}")

    now = get_tw_now()
    hm = now.strftime("%H:%M")
    is_weekend = now.weekday() >= 5
    today_str = now.strftime("%Y-%m-%d")

    print(f"🕒 當前台灣時間: {now.strftime('%Y-%m-%d %H:%M:%S')} (星期{now.weekday()+1})")

    # 模式判斷：若非盤中時間 (如晚上手動測試或週末)，執行快速測試模式
    is_market_session = ("08:50" <= hm <= "13:30") and not is_weekend
    if not is_market_session:
        # 在進入測試模式、覆蓋 index.html 之前，先檢查今天是否已經完成過 13:25 收盤結算。
        # 若已結算過，代表今天的正式流程已跑完，之後 cron 若仍持續每 5 分鐘觸發（收盤後、
        # 隔天開盤前皆然），絕對不能再讓測試模式把正式的收盤結算頁面覆蓋掉。
        existing_state = load_dashboard_state(today_str, gemini, fugle, cfg)
        if existing_state.get("settled_today"):
            print(f"\nℹ️ 今日 ({today_str}) 已完成 13:25 收盤結算，非盤中時段不再執行測試模式、"
                  f"也不覆蓋 index.html，直接結束本輪。")
            return

        # ── 收盤結算補跑機制 ──────────────────────────────────────────
        # 修復說明：原本收盤結算 (hm >= "13:25") 只有在 is_market_session
        # (08:50~13:30) 範圍內才會被檢查，也就是說只有 13:25、13:30 這兩次
        # 排程（每 5 分鐘觸發一次）有機會執行到。只要這個 5 分鐘視窗剛好因為
        # GitHub Actions 排隊延遲、API 逾時等原因沒有任何一次成功跑完整段
        # 結算流程，settled_today 就永遠不會被設成 True，之後每一輪都會被
        # 判定為「非盤中時段」而走向這裡，用測試資料覆蓋掉本應顯示的正式
        # 收盤結算頁面——這正是「已經收盤卻一直看到測試資料」的根本原因。
        #
        # 修法：只要偵測到「今天已經有正式盤中流程跑過 (wave1_stocks 非空，
        # 代表不是還沒開盤的凌晨/盤前時段) 但尚未結算」，且現在時間已經在
        # 收盤時間之後 (>= 13:25)，不管是不是週末判斷出的非盤中時段、
        # 也不管現在到底幾點，都在這裡直接補跑一次收盤結算，而不是放著
        # 讓測試模式覆蓋畫面、一路等到隔天才恢復正常。
        if existing_state.get("wave1_stocks") and hm >= HISTORY_SETTLE_TIME:
            print(f"\n⚠️ 偵測到今日 ({today_str}) 已執行過盤中流程，但尚未完成收盤結算"
                  f"（可能是 {HISTORY_SETTLE_TIME}~13:30 的結算視窗剛好沒有排程準時觸發成功）。"
                  f"現在時間 {hm} 已過收盤，立即補跑一次收盤結算，避免頁面繼續顯示測試資料。")
            run_settlement(
                existing_state, today_str, gemini, fugle,
                existing_state.get("wave1_stocks", []),
                existing_state.get("wave2_stocks", []),
                existing_state.get("latest_analysis_records", []),
                existing_state.get("analysis_log", []),
                existing_state.get("live_quotes", {}),
                existing_state.get("total_signals", 0),
                cfg,
            )
            return

        print("\n⚠️ 目前非台股盤中交易時間 (09:00~13:30)，進入【連線與即時看板測試模式】...")
        test_stocks = get_free_top_volume_stocks(limit=3)
        print("🔍 測試 Yahoo 成交量排行抓取：")
        for s in test_stocks:
            print(f"   📌 {s['symbol']} {s['name']} (參考價: {s['price']} 元, 成交量: {s.get('volume', 0):,} 張)")
        
        ai_reply = "尚未測試"
        test_analysis = []
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
                test_analysis.append({
                    "symbol": test_sym,
                    "name": test_stocks[0]["name"],
                    "signal": "WATCH",
                    "entry": test_stocks[0]["price"],
                    "stop_loss": "-",
                    "target": "-",
                    "reason": f"測試連線成功: {ai_reply}"
                })
            except Exception as e:
                print(f"   ❌ Gemini 連線異常: {e}")

        # 渲染出初始 index.html。這裡額外把 existing_state 內既有的 analysis_log 一併帶入，
        # 避免非盤中測試模式重新整理畫面時，把白天盤中已經累積的「展開查看歷史分析」
        # 按鈕暫時性地清空不見（latest_analysis 維持原本邏輯不變，僅補上 analysis_log）。
        render_html_dashboard(
            status_text="非盤中連線測試（僅測3檔，平日盤中將完整執行8檔選股）",
            active_model=gemini.active_model,
            wave1_stocks=test_stocks,
            latest_analysis=test_analysis,
            analysis_log=existing_state.get("analysis_log", []),
            live_quotes=existing_state.get("live_quotes", {})
        )

        print("\n🎉 GitHub Actions 測試驗證全數通過！專屬網頁 index.html 已更新。")
        return

    # ── 正式盤中運作流程（v4.0：單輪執行模式）──────────────────────
    # 讀取上一輪次留下的狀態（同一交易日內累積），這是讓分析紀錄能夠「累加」
    # 而不是每次觸發都從零開始、只顯示最新幾筆的關鍵。
    state = load_dashboard_state(today_str, gemini, fugle, cfg)
    wave1_stocks = state["wave1_stocks"]
    wave2_stocks = state["wave2_stocks"]
    mid_wave_triggered = state["mid_wave_triggered"]
    latest_analysis_records = state["latest_analysis_records"]
    analysis_log = state["analysis_log"]
    live_quotes = state["live_quotes"]
    total_signals = state["total_signals"]
    last_bucket = state["last_analysis_minute_bucket"]

    current_stocks = wave2_stocks if wave2_stocks else wave1_stocks

    # 盤前 (08:50~09:04)：只更新「準備中」狀態，不抓股也不分析
    if hm < ANALYSIS_START_TIME:
        print(f"[{now.strftime('%H:%M:%S')}] 尚未到 {ANALYSIS_START_TIME} 開盤選股時間，僅更新盤前準備狀態。")
        render_html_dashboard(
            status_text=f"盤前準備中 (等待 {ANALYSIS_START_TIME})",
            active_model=gemini.active_model,
            wave1_stocks=wave1_stocks,
            wave2_stocks=wave2_stocks,
            latest_analysis=latest_analysis_records,
            analysis_log=analysis_log,
            live_quotes=live_quotes,
            total_signals=total_signals
        )
        save_dashboard_state(state)
        return

    # ANALYSIS_START_TIME (09:05) 首次觸發：第一波段選股 (只在 wave1_stocks 還是空的時候做一次)
    if hm >= ANALYSIS_START_TIME and not wave1_stocks:
        print(f"\n⏰ 達到 {ANALYSIS_START_TIME}，開始執行【第一波段：早盤動能成交量排行選股】...")
        # 多抓幾檔候選 (limit+5)，排除漲停股後仍有機會湊滿 limit 檔，
        # 避免「候選8檔剛好有2檔漲停」導致最終監控標的縮水成6檔。
        wave1_candidates = get_free_top_volume_stocks(limit=13)
        wave1_stocks = filter_out_limit_up_stocks(wave1_candidates, fugle, limit=8)
        current_stocks = wave1_stocks
        print(f"🔥 早盤 09:15 已鎖定標的：")
        for s in wave1_stocks:
            print(f"   📌 {s['symbol']} {s['name']} (現價: {s['price']} 元, 成交量: {s.get('volume', 0):,} 張)")

        state["wave1_stocks"] = wave1_stocks
        render_html_dashboard(
            status_text="早盤第一波監控中",
            active_model=gemini.active_model,
            wave1_stocks=wave1_stocks,
            latest_analysis=latest_analysis_records,
            analysis_log=analysis_log,
            live_quotes=live_quotes,
            total_signals=total_signals
        )
        save_dashboard_state(state)
        # 選股完當輪就結束，讓 workflow 立即 commit/push，下一次 5 分鐘後的觸發再繼續分析
        print("✅ 本輪次（選股）執行完畢。")
        return

    # 10:30 觸發：第二波段重挑股票 (只做一次)
    if hm >= MID_WAVE_TRIGGER_TIME and not mid_wave_triggered:
        print(f"\n⏰ 達到 {MID_WAVE_TRIGGER_TIME}，開始執行【第二波段：中盤換手與輪動股票重挑】...")
        # 同上：多抓候選再過濾漲停，避免湊不滿 8 檔
        wave2_candidates = get_free_top_volume_stocks(limit=13)
        wave2_stocks = filter_out_limit_up_stocks(wave2_candidates, fugle, limit=8)
        if wave2_stocks:
            current_stocks = wave2_stocks
            print(f"🔥 中盤 10:30 已更新監控標的：")
            for s in wave2_stocks:
                print(f"   📌 {s['symbol']} {s['name']} (現價: {s['price']} 元, 成交量: {s.get('volume', 0):,} 張)")

        mid_wave_triggered = True
        state["wave2_stocks"] = wave2_stocks
        state["mid_wave_triggered"] = True
        render_html_dashboard(
            status_text="中盤第二波監控中",
            active_model=gemini.active_model,
            wave1_stocks=wave1_stocks,
            wave2_stocks=wave2_stocks,
            latest_analysis=latest_analysis_records,
            analysis_log=analysis_log,
            live_quotes=live_quotes,
            total_signals=total_signals
        )
        save_dashboard_state(state)
        print("✅ 本輪次（中盤重挑）執行完畢。")
        return

    # 13:25 (或之後)：收盤回放結算 (只做一次；用 settled_today 判斷本日是否已結算過)
    if hm >= HISTORY_SETTLE_TIME:
        run_settlement(state, today_str, gemini, fugle, wave1_stocks, wave2_stocks,
                        latest_analysis_records, analysis_log, live_quotes, total_signals, cfg)
        return

    # 13:00 (或之後，但還沒到 13:25 收盤結算)：AI 分析截止，不再丟給 AI 判斷。
    # 當沖需要留時間完成「進場→出場」的來回，尾盤時間太短即使 AI 判斷出訊號
    # 也很難真正走完一趟當沖，因此 13:00 後只單純更新看板顯示目前狀態、
    # 等待 13:25 的收盤回放結算，不再消耗 AI 額度做新的盤中判斷。
    if hm >= ANALYSIS_STOP_TIME:
        print(f"[{now.strftime('%H:%M:%S')}] 已過 {ANALYSIS_STOP_TIME}，AI 盤中分析截止，等待 {HISTORY_SETTLE_TIME} 收盤結算。")
        render_html_dashboard(
            status_text=f"AI 分析已截止 (等待 {HISTORY_SETTLE_TIME} 收盤結算)",
            active_model=gemini.active_model,
            wave1_stocks=wave1_stocks,
            wave2_stocks=wave2_stocks,
            latest_analysis=latest_analysis_records,
            analysis_log=analysis_log,
            live_quotes=live_quotes,
            total_signals=total_signals
        )
        save_dashboard_state(state)
        return

    # ANALYSIS_START_TIME ~ ANALYSIS_STOP_TIME 盤中：每 10 分鐘執行一次分析
    # (v20 調整後目標約為 09:05, 09:15, 09:25 ... 12:55，13:00 起不再進行新的 AI 分析，
    #  實際觸發時間仍取決於 GitHub Actions 排程間隔與 queue latency)
    # v4.1 修正說明：
    # ────────────
    # 舊版用 `now.minute % 10 == 0` 判斷「是否剛好命中整 10 分鐘」，前提是 GitHub Actions
    # 每次都能準時在整 10 分鐘那一刻開始執行。但實際上 workflow_dispatch 從被 cron-job.org
    # 呼叫、到 runner 排隊分配、再到 python 腳本真正開始跑，中間常有數十秒到數分鐘不等的
    # queue latency；只要延遲跨過了那一分鐘，現在時間就不再是 10 的倍數，導致 current_bucket
    # 直接變成 None、本輪整個跳過分析——而且因為沒有補跑機制，這個 10 分鐘窗口就永久錯過了。
    # 這是先前「一整天只分析到一次」的根本原因。
    #
    # 新版改用「距離上次分析是否已經過了至少 10 分鐘」的時間差來判斷，不再要求分鐘數剛好
    # 對上整數，只要間隔滿足就觸發，對排隊延遲有完整容錯空間。
    last_bucket_dt = None
    if last_bucket:
        try:
            last_bucket_dt = TW_TZ.localize(datetime.datetime.strptime(last_bucket, "%Y-%m-%d %H:%M"))
        except Exception as e:
            print(f"⚠️ 解析上次分析時間戳記「{last_bucket}」失敗，視為尚未分析過: {e}")
            last_bucket_dt = None

    ANALYSIS_INTERVAL_SECONDS = 10 * 60
    if last_bucket_dt is None:
        should_analyze = True
        seconds_since_last = None
    else:
        seconds_since_last = (now - last_bucket_dt).total_seconds()
        should_analyze = seconds_since_last >= ANALYSIS_INTERVAL_SECONDS

    current_bucket = now.strftime("%Y-%m-%d %H:%M") if should_analyze else last_bucket

    if not should_analyze:
        remain = ANALYSIS_INTERVAL_SECONDS - seconds_since_last if seconds_since_last is not None else None
        remain_msg = f"，距下次分析還需約 {int(remain // 60)} 分 {int(remain % 60)} 秒" if remain is not None else ""
        print(f"[{now.strftime('%H:%M:%S')}] 距上次分析 ({last_bucket}) 尚未滿 10 分鐘{remain_msg}，本輪次僅同步目前看板狀態。")
        render_html_dashboard(
            status_text=f"盤中監控中 ({now.strftime('%H:%M')})",
            active_model=gemini.active_model,
            wave1_stocks=wave1_stocks,
            wave2_stocks=wave2_stocks,
            latest_analysis=latest_analysis_records,
            analysis_log=analysis_log,
            live_quotes=live_quotes,
            total_signals=total_signals
        )
        save_dashboard_state(state)
        return

    print(f"\n⚡ [{now.strftime('%H:%M:%S')}] 執行 10 分鐘定時分析...")
    for s_info in current_stocks:
        symbol = s_info["symbol"]
        name = s_info["name"]
        try:
            candles_raw = fugle.get_intraday_candles(symbol, force_refresh=True)
            candles = candles_raw.get("data", []) if candles_raw else []
            if not candles or len(candles) < 5:
                continue

            # 記錄這一輪抓到的最新分K收盤價，做為「即時損益」計算的參考價。
            # 用同一輪已經抓好的 candles，不用額外呼叫 API：candles[-1]["close"]
            # 就是目前最新的成交價。不論這一輪分析結果是不是有訊號都會更新，
            # 讓「展開歷史分析」清單裡，即使某檔股票的訊號後來轉為觀望，
            # 先前留下的 BUY/SHORT 歷史紀錄一樣能對到最新的參考價計算損益。
            live_quotes[symbol] = {
                "price": candles[-1]["close"],
                "updated_at": now.strftime("%H:%M:%S"),
            }

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
                risk_mode=RISK_MODE,
                concise=True
            )

            # gemini_service.analyze_with_signal() 回傳的 res["signal"] 是小寫粗分類
            # ("buy" / "short" / "watch")，但畫面渲染（Python 端的 render_html_dashboard
            # 與前端 JS）長期以來都是用「"BUY" in sig」/「sig.includes("BUY")」這種
            # 區分大小寫的字串比對來判斷做多/做空/觀望分類。因為 Python 的 `in` 與 JS 的
            # `includes` 都區分大小寫，"BUY" in "buy" 恆為 False，導致所有寫入
            # analysis_log / latest_analysis_records 的訊號都被誤判成「觀望」，
            # 徽章顯示錯誤、做多/做空篩選按鈕也篩不到——即使 total_signals 計數器
            # (下面用的是 raw_sig，本身是大寫) 有正確 +1，畫面上的分類卻對不起來，
            # 造成「今日訊號有算到，但股票的歷史分析紀錄卻看不到該訊號」的假象。
            # 修法：在這裡就把 sig 正規化成大寫，讓後續所有比對統一用大寫，
            # 一次修正所有下游（Python 渲染、JS 渲染、篩選、統計）。
            sig = res.get("signal", "WATCH").upper()
            raw_sig = res.get("raw_signal", sig).upper()

            # ── 一致性校驗：防止 signal 與 raw_signal 各自代表不同判斷 ──
            # 【bug修復】曾發生單一情況：res["raw_signal"] 是 "BUY"，但
            # res["signal"] 卻是 "watch"，兩者理論上該同步（見
            # gemini_service._SIGNAL_MAP 賦值邏輯），一旦不同步，畫面會用
            # signal 判斷徽章分類（顯示觀望），但歷史結算卻用 raw_signal
            # 記錄成 BUY 並真的觸發交易紀錄——形成「歷史紀錄裡股票卡片
            # 顯示觀望，但收盤結算卻多出一筆該股票的買賣紀錄」的矛盾。
            # 這裡以 raw_signal（訊號強度分類，直接對應 BUY/SHORT 是否會
            # 觸發交易紀錄）為準，反推 signal 的粗分類，確保兩者永遠一致，
            # 不管上游解析發生什麼未預期狀況，下游顯示都不會自相矛盾。
            _RAW_TO_COARSE = {
                "STRONG_BUY": "BUY", "BUY": "BUY",
                "SHORT": "SHORT", "STRONG_SHORT": "SHORT",
            }
            _expected_sig = _RAW_TO_COARSE.get(raw_sig, "WATCH")
            if sig != _expected_sig:
                print(
                    f"  ⚠️ [{symbol}] 偵測到 signal({sig}) 與 raw_signal({raw_sig}) "
                    f"不一致，以 raw_signal 為準修正為 {_expected_sig}"
                )
                sig = _expected_sig
            entry_p = res.get("entry")
            if isinstance(entry_p, str):
                try: entry_p = float(entry_p.split()[0].replace("元",""))
                except: entry_p = candles[-1]["close"]

            stop_p = res.get("stop_loss", "-")
            target_p = res.get("target", "-")
            reason = (res.get("reason") or res.get("full_text", "")).replace("\n", " ").strip()

            print(f"  [{symbol} {name}] 訊號: {sig} | 進場: {entry_p} | 停損: {stop_p} | 停利: {target_p}")

            analysis_record = {
                "symbol": symbol,
                "name": name,
                "signal": sig,
                "entry": entry_p,
                "stop_loss": stop_p,
                "target": target_p,
                "reason": reason,
                "updated_at": now.strftime("%H:%M:%S")
            }

            # 用 upsert 併入「即時總覽」清單：同一檔股票覆蓋更新為最新狀態，
            # 讓即時看板顯示的是「當日所有被分析過的股票目前最新結果」
            latest_analysis_records = upsert_analysis_record(latest_analysis_records, analysis_record)

            # 同時 append 進「完整歷程」清單：同一檔股票每一輪都各自保留一筆，不覆蓋。
            # 這是修復先前問題的關鍵——之前只有 upsert 這份會覆蓋掉中間所有分析輪次，
            # 收盤快照也只存到覆蓋後的最後一筆。現在完整歷程獨立保存，收盤與盤中
            # 查詢都能回溯每檔股票今天每一次分析的變化。
            analysis_log = append_analysis_log(analysis_log, analysis_record)

            # 出現買賣訊號時寫入歷史紀錄
            if raw_sig in {"STRONG_BUY", "BUY", "SHORT", "STRONG_SHORT"}:
                total_signals += 1
                rec_id = cache_service.add_history_record(
                    symbol=symbol,
                    model=gemini.active_model,
                    risk_mode=RISK_MODE,
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

    state["wave1_stocks"] = wave1_stocks
    state["wave2_stocks"] = wave2_stocks
    state["mid_wave_triggered"] = mid_wave_triggered
    state["latest_analysis_records"] = latest_analysis_records
    state["analysis_log"] = analysis_log
    state["live_quotes"] = live_quotes
    state["total_signals"] = total_signals
    state["last_analysis_minute_bucket"] = current_bucket

    render_html_dashboard(
        status_text=f"盤中分析中 ({now.strftime('%H:%M')})",
        active_model=gemini.active_model,
        wave1_stocks=wave1_stocks,
        wave2_stocks=wave2_stocks,
        latest_analysis=latest_analysis_records,
        analysis_log=analysis_log,
        live_quotes=live_quotes,
        total_signals=total_signals
    )
    save_dashboard_state(state)
    print("✅ 本輪次（10 分鐘分析）執行完畢。")

if __name__ == "__main__":
    main()
