# -*- coding: utf-8 -*-
"""
headless_trader.py - 雲端無頭當沖機器人 (v4.0 單輪執行 + 狀態持久化版)
─────────────────────────────────────────────────────────────
• 09:15 早盤第一次抓取成交量排行前 5 檔 (避開開盤假突破雜訊)
• 10:30 中盤第二次重新抓取成交量排行前 5 檔 (鎖定盤中換手輪動飆股)
• 盤中每 10 分鐘調用 Google AI (多模型自動降級鏈) 進行深度判斷
• 自動生成獨立網頁 index.html (透過 GitHub Pages 提供免登入固定專屬網址)
• 同步輸出 GitHub Step Summary 即時 Markdown 看板
• 13:25 收盤自動回放當日 1分K 結算盈虧，產出 CSV 報表保存至 GitHub

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
from gemini_service import GeminiService
import cache_service
from config import load_config

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

TW_TZ = pytz.timezone("Asia/Taipei")

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
    settle_records: List[Dict] = None,
    active_model: str = "gemma-4-31b-it",
    status_text: str = "運行中",
    total_signals: int = None,
    **kwargs
):
    """
    生成單一獨立網頁 index.html，供 GitHub Pages 直接託管展示
    具備密碼防護機制、暗黑風質感交易介面、手機響應式設計
    """
    now_str = get_tw_now().strftime("%Y-%m-%d %H:%M:%S")
    today_str = get_tw_now().strftime("%Y-%m-%d")
    if total_signals is None:
        total_signals = len([a for a in (latest_analysis or []) if a.get("signal") in ["BUY", "SHORT"]])

    # 取得密碼設定 (預設 888888)，清除前後空白與換行，計算安全 SHA-256 與 Base64
    raw_pwd = (os.getenv("DASHBOARD_PASSWORD") or "888888").strip()
    pwd_hash = hashlib.sha256(raw_pwd.encode("utf-8")).hexdigest()
    pwd_b64 = base64.b64encode(raw_pwd.encode("utf-8")).decode("utf-8")

    wave1_stocks = wave1_stocks or []
    wave2_stocks = wave2_stocks or []
    latest_analysis = latest_analysis or []
    settle_records = settle_records or []

    # ── 下載功能：把本次看板的完整原始資料打包成 JSON，供頁面右上角下載按鈕使用 ──
    # today_str 一併放入 payload：供前端日期切換選單判斷「目前選的是不是今天」，
    # 以及切回今日時可以直接從這份記憶體資料還原畫面，不需要重新 fetch。
    export_payload = {
        "generated_at": now_str,
        "today_str": today_str,
        "status_text": status_text,
        "active_model": active_model,
        "total_signals": total_signals,
        "wave1_stocks": wave1_stocks,
        "wave2_stocks": wave2_stocks,
        "latest_analysis": latest_analysis,
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
        net_total = sum(r.get("net_profit", 0) for r in settle_records)
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
        wave1_html = '<li class="text-gray-500 text-xs py-2">等待開盤 09:15 抓取中...</li>'

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
        wave2_html = '<li class="text-gray-500 text-xs py-2">10:30 自動重新掃描成交量排行...</li>'

    # 生成分析表格（依訊號分類貼上 data-filter-group 屬性，供前端做多/做空/觀望篩選使用）
    # 配色依台股慣例「紅漲綠跌」：做多(偏多/漲) 用紅、放空(偏空/跌) 用綠，跟一般西式股市剛好相反
    analysis_rows = ""      # 桌面版表格列
    analysis_cards = ""     # 手機版直式資訊卡（避免長文字被表格固定欄寬硬擠導致換行跑版）
    if latest_analysis:
        for a in latest_analysis:
            sig = a.get("signal", "WATCH")
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

            analysis_rows += f"""
            <tr class="hover:bg-white/[0.02] analysis-row" data-filter-group="{filter_group}">
                <td class="py-2.5 px-3 font-bold text-white whitespace-nowrap">{symbol} {name}</td>
                <td class="py-2.5 px-3">{sig_badge}</td>
                <td class="py-2.5 px-3 mono text-gray-200 whitespace-nowrap">{entry}</td>
                <td class="py-2.5 px-3 mono text-[#00d68f] whitespace-nowrap">{stop_loss}</td>
                <td class="py-2.5 px-3 mono text-[#ff5470] whitespace-nowrap">{target}</td>
                <td class="py-2.5 px-3 text-gray-300 text-xs">{reason}</td>
                <td class="py-2.5 px-3 text-gray-500 text-[11px] mono whitespace-nowrap">{updated_at}</td>
            </tr>
            """

            analysis_cards += f"""
            <div class="data-card analysis-row" data-filter-group="{filter_group}">
                <div class="flex items-center justify-between mb-2">
                    <span class="font-bold text-white text-sm">{symbol} {name}</span>
                    {sig_badge}
                </div>
                <div class="data-row"><span class="dlabel">建議進場</span><span class="dvalue mono">{entry}</span></div>
                <div class="data-row"><span class="dlabel">建議停損</span><span class="dvalue mono text-[#00d68f]">{stop_loss}</span></div>
                <div class="data-row"><span class="dlabel">建議停利</span><span class="dvalue mono text-[#ff5470]">{target}</span></div>
                <div class="data-row"><span class="dlabel">更新時間</span><span class="dvalue mono text-gray-500">{updated_at}</span></div>
                <div class="mt-2 pt-2 border-t border-white/5 text-xs text-gray-300 leading-relaxed">{reason}</div>
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
            net_p = r.get("net_profit", 0)
            net_str = f"+${net_p:,}" if net_p > 0 else f"-${abs(net_p):,}" if net_p < 0 else "$0"
            net_color = "text-[#ff5470]" if net_p > 0 else "text-[#00d68f]" if net_p < 0 else "text-gray-300"
            symbol = r.get("symbol", "")
            signal = r.get("signal", "")
            entry_price = r.get("entry_price", "-")
            exit_price = r.get("exit_price", "-")
            exit_reason = r.get("exit_reason", "-")

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
        settle_rows = '<tr><td colspan="7" class="py-4 text-center text-gray-500 text-xs">尚未達到收盤結算時間 (13:25)</td></tr>'
        settle_cards = '<div class="text-center text-gray-500 text-xs py-4">尚未達到收盤結算時間 (13:25)</div>'

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
                <p class="text-xs text-gray-500 mt-1">09:15 / 10:30 雙波段選股　·　每 10 分鐘 AI 分析　·　13:25 回放結算</p>
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
                        <p class="text-[11px] text-gray-500 mt-0.5">09:15 觸發（ORB-15 區間成型）</p>
                    </div>
                    <span class="text-[11px] bg-[#0f1c33] text-[#8db3ff] border border-[#4d8dff]/25 px-2 py-0.5 rounded-md whitespace-nowrap">波段一</span>
                </div>
                <ul class="space-y-2 text-sm">{wave1_html}</ul>
            </div>

            <div class="panel border rounded-2xl p-5">
                <div class="flex items-center justify-between border-b border-white/5 pb-3 mb-3">
                    <div>
                        <h2 class="font-bold text-white text-sm">中盤換手重挑</h2>
                        <p class="text-[11px] text-gray-500 mt-0.5">10:30 觸發（鎖定盤中輪動主升股）</p>
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

        <!-- 13:25 收盤回放結算卡片 -->
        <div class="panel border rounded-2xl p-5">
            <div class="flex items-center justify-between border-b border-white/5 pb-3 mb-3">
                <div>
                    <h2 class="font-bold text-white text-base">今日回測結算</h2>
                    <p class="text-[11px] text-gray-500 mt-0.5">13:25 以當日 1 分K 逐根回放比對實際賺賠（已扣手續費與證交稅）</p>
                </div>
                <span class="text-[11px] text-[#f5b942] font-semibold whitespace-nowrap">13:25 結算</span>
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
            csv += csvSection("【波段一 09:15 選股】", EXPORT_DATA.wave1_stocks);
            csv += csvSection("【波段二 10:30 選股】", EXPORT_DATA.wave2_stocks);
            csv += csvSection("【AI 即時分析訊號】", EXPORT_DATA.latest_analysis);
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
                renderAnalysisTable(EXPORT_DATA.latest_analysis, true);
                renderSettleTable(EXPORT_DATA.settle_records);
                setSignalFilter(localStorage.getItem("daytrade_signal_filter") || "all");
                return;
            }}

            loadingMsg.classList.remove("hidden");
            try {{
                const resp = await fetch(`history_records/analysis_${{value}}.json`, {{ cache: "no-store" }});
                if (!resp.ok) throw new Error("該日期無資料");
                const snapshot = await resp.json();

                renderAnalysisTable(snapshot.analysis_records || [], false);
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
        function renderAnalysisTable(records, isToday) {{
            const tbody = document.getElementById("analysis-tbody");
            const cardsWrap = document.getElementById("analysis-cards");
            const emptyRowHtml = isToday
                ? '<tr><td colspan="7" class="py-6 text-center text-gray-500 text-xs">盤中每 10 分鐘自動更新分析看板...</td></tr>'
                : '<tr><td colspan="7" class="py-6 text-center text-gray-500 text-xs">此日期尚無分析資料</td></tr>';
            const emptyCardHtml = isToday
                ? '<div class="text-center text-gray-500 text-xs py-6">盤中每 10 分鐘自動更新分析看板...</div>'
                : '<div class="text-center text-gray-500 text-xs py-6">此日期尚無分析資料</div>';

            if (!records || records.length === 0) {{
                tbody.innerHTML = emptyRowHtml;
                cardsWrap.innerHTML = emptyCardHtml;
                return;
            }}

            let rowsHtml = "";
            let cardsHtml = "";
            records.forEach(a => {{
                const sig = a.signal || "WATCH";
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

                rowsHtml += `
                <tr class="hover:bg-white/[0.02] analysis-row" data-filter-group="${{filterGroup}}">
                    <td class="py-2.5 px-3 font-bold text-white whitespace-nowrap">${{symbol}} ${{name}}</td>
                    <td class="py-2.5 px-3">${{badge}}</td>
                    <td class="py-2.5 px-3 mono text-gray-200 whitespace-nowrap">${{entry}}</td>
                    <td class="py-2.5 px-3 mono text-[#00d68f] whitespace-nowrap">${{stopLoss}}</td>
                    <td class="py-2.5 px-3 mono text-[#ff5470] whitespace-nowrap">${{target}}</td>
                    <td class="py-2.5 px-3 text-gray-300 text-xs">${{reason}}</td>
                    <td class="py-2.5 px-3 text-gray-500 text-[11px] mono whitespace-nowrap">${{updatedAt}}</td>
                </tr>`;

                cardsHtml += `
                <div class="data-card analysis-row" data-filter-group="${{filterGroup}}">
                    <div class="flex items-center justify-between mb-2">
                        <span class="font-bold text-white text-sm">${{symbol}} ${{name}}</span>
                        ${{badge}}
                    </div>
                    <div class="data-row"><span class="dlabel">建議進場</span><span class="dvalue mono">${{entry}}</span></div>
                    <div class="data-row"><span class="dlabel">建議停損</span><span class="dvalue mono text-[#00d68f]">${{stopLoss}}</span></div>
                    <div class="data-row"><span class="dlabel">建議停利</span><span class="dvalue mono text-[#ff5470]">${{target}}</span></div>
                    <div class="data-row"><span class="dlabel">更新時間</span><span class="dvalue mono text-gray-500">${{updatedAt}}</span></div>
                    <div class="mt-2 pt-2 border-t border-white/5 text-xs text-gray-300 leading-relaxed">${{reason}}</div>
                </div>`;
            }});
            tbody.innerHTML = rowsHtml;
            cardsWrap.innerHTML = cardsHtml;
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
            records.forEach(r => {{
                const isWin = (r.pnl_amount ?? 0) > 0;
                const isLoss = (r.pnl_amount ?? 0) < 0;
                const resultClass = isWin ? "text-[#ff5470]" : (isLoss ? "text-[#00d68f]" : "text-gray-400");
                const badgeClass = isWin ? "sig-long" : (isLoss ? "sig-short" : "sig-watch");
                const resultText = r.result || (isWin ? "獲利" : (isLoss ? "虧損" : "持平"));
                const pnlDisplay = (r.pnl_amount !== null && r.pnl_amount !== undefined)
                    ? `${{r.pnl_amount > 0 ? "+" : ""}}${{r.pnl_amount}}` : "-";
                const badge = `<span class="sig-badge ${{badgeClass}}">${{escapeHtml(resultText)}}</span>`;

                const symbol = escapeHtml(r.symbol);
                const direction = escapeHtml(r.direction || "-");
                const entryPrice = escapeHtml(r.entry_price ?? "-");
                const exitPrice = escapeHtml(r.exit_price ?? "-");
                const exitReason = escapeHtml(r.exit_reason || "-");

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

def load_dashboard_state(today_str: str) -> Dict:
    """
    讀取上一輪次留下的看板狀態。若狀態檔不存在，或存的是「不同日期」的舊資料
    (例如今天是新的交易日，但檔案還留著昨天收盤的紀錄)，則回傳全新的空白狀態，
    避免不同交易日的資料互相混雜。
    """
    default_state = {
        "date": today_str,
        "wave1_stocks": [],
        "wave2_stocks": [],
        "mid_wave_triggered": False,
        "latest_analysis_records": [],  # 累積型：同一檔股票用 symbol 當 key 覆蓋更新，不同股票會並存
        "total_signals": 0,
        "last_analysis_minute_bucket": None,  # 記錄上次執行過 10 分鐘分析的時間戳記，避免同一個 5 分鐘窗被重複觸發兩次
    }
    if not os.path.exists(STATE_FILE):
        return default_state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        if state.get("date") != today_str:
            print(f"ℹ️ 偵測到狀態檔案為前一交易日 ({state.get('date')}) 的資料，重置為今日 ({today_str}) 全新狀態。")
            return default_state
        return state
    except Exception as e:
        print(f"⚠️ 讀取 {STATE_FILE} 失敗，改用全新狀態: {e}")
        return default_state

def save_dashboard_state(state: Dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 寫入 {STATE_FILE} 失敗: {e}")

def upsert_analysis_record(records: List[Dict], new_record: Dict) -> List[Dict]:
    """
    將本次分析結果併入累積清單：同一檔股票(symbol)存在就覆蓋更新為最新結果，
    不存在就新增一筆，藉此讓網站顯示「當日所有被分析過的股票」而不是只有最新一輪的幾檔。
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

def save_daily_history_snapshot(date_str: str, analysis_records: List[Dict], settle_records: List[Dict]):
    """
    收盤結算時呼叫：將當天的完整分析紀錄 (含觀望) 與結算損益，
    寫成 history_records/analysis_YYYY-MM-DD.json，並更新
    history_records/index.json 這份「有哪些日期可查」的索引檔。

    GitHub Pages 是純靜態網站，前端 JavaScript 沒辦法直接列出
    history_records/ 資料夾底下有哪些檔案，所以需要額外維護
    這份 index.json，供 index.html 的日期下拉選單讀取。
    """
    os.makedirs("history_records", exist_ok=True)

    snapshot = {
        "date": date_str,
        "analysis_records": analysis_records,
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

    # 更新日期索引檔：讀取既有索引，把今天加進去 (若已存在則不重複加入)，
    # 並依日期新到舊排序，方便前端下拉選單直接照順序顯示。
    index_path = "history_records/index.json"
    dates = []
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                dates = json.load(f).get("dates", [])
        except Exception as e:
            print(f"⚠️ 讀取既有 {index_path} 失敗，將重新建立: {e}")
            dates = []

    if date_str not in dates:
        dates.append(date_str)
    dates.sort(reverse=True)

    try:
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump({"dates": dates}, f, ensure_ascii=False, indent=2)
        print(f"✅ 已更新歷史日期索引：{index_path} (共 {len(dates)} 天)")
    except Exception as e:
        print(f"⚠️ 寫入 {index_path} 失敗: {e}")

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

        # 渲染出初始 index.html
        render_html_dashboard(
            status_text="非盤中連線測試（僅測3檔，平日盤中將完整執行8檔選股）",
            active_model=gemini.active_model,
            wave1_stocks=test_stocks,
            latest_analysis=test_analysis
        )

        print("\n🎉 GitHub Actions 測試驗證全數通過！專屬網頁 index.html 已更新。")
        return

    # ── 正式盤中運作流程（v4.0：單輪執行模式）──────────────────────
    # 讀取上一輪次留下的狀態（同一交易日內累積），這是讓分析紀錄能夠「累加」
    # 而不是每次觸發都從零開始、只顯示最新幾筆的關鍵。
    state = load_dashboard_state(today_str)
    wave1_stocks = state["wave1_stocks"]
    wave2_stocks = state["wave2_stocks"]
    mid_wave_triggered = state["mid_wave_triggered"]
    latest_analysis_records = state["latest_analysis_records"]
    total_signals = state["total_signals"]
    last_bucket = state["last_analysis_minute_bucket"]

    current_stocks = wave2_stocks if wave2_stocks else wave1_stocks

    # 盤前 (08:50~09:14)：只更新「準備中」狀態，不抓股也不分析
    if hm < "09:15":
        print(f"[{now.strftime('%H:%M:%S')}] 尚未到 09:15 開盤選股時間，僅更新盤前準備狀態。")
        render_html_dashboard(
            status_text="盤前準備中 (等待 09:15)",
            active_model=gemini.active_model,
            wave1_stocks=wave1_stocks,
            wave2_stocks=wave2_stocks,
            latest_analysis=latest_analysis_records,
            total_signals=total_signals
        )
        save_dashboard_state(state)
        return

    # 09:15 首次觸發：第一波段選股 (只在 wave1_stocks 還是空的時候做一次)
    if hm >= "09:15" and not wave1_stocks:
        print(f"\n⏰ 達到 09:15，開始執行【第一波段：早盤動能成交量排行選股】...")
        wave1_stocks = get_free_top_volume_stocks(limit=8)
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
            total_signals=total_signals
        )
        save_dashboard_state(state)
        # 選股完當輪就結束，讓 workflow 立即 commit/push，下一次 5 分鐘後的觸發再繼續分析
        print("✅ 本輪次（選股）執行完畢。")
        return

    # 10:30 觸發：第二波段重挑股票 (只做一次)
    if hm >= "10:30" and not mid_wave_triggered:
        print(f"\n⏰ 達到 10:30，開始執行【第二波段：中盤換手與輪動股票重挑】...")
        wave2_stocks = get_free_top_volume_stocks(limit=8)
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
            total_signals=total_signals
        )
        save_dashboard_state(state)
        print("✅ 本輪次（中盤重挑）執行完畢。")
        return

    # 13:25 (或之後)：收盤回放結算 (只做一次；用 settle_records 是否已存在判斷本日是否已結算過)
    if hm >= "13:25":
        pending_records = cache_service.get_pending_history_for_date(today_str)
        print(f"\n🎯 開始收盤分K回放結算，今日待結算筆數: {len(pending_records)}")

        today_settled_list = []
        if pending_records:
            for rec in pending_records:
                sym = rec["symbol"]
                candles_raw = fugle.get_intraday_candles(sym, force_refresh=True)
                day_candles = candles_raw.get("data", []) if candles_raw else []
                if day_candles:
                    cache_service.settle_history_record_with_candles(rec["id"], day_candles)

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

        # 將當日累積的盤中分析紀錄 (latest_analysis_records，含觀望在內) 一併保存成
        # history_records/analysis_YYYY-MM-DD.json，供網頁日後切換日期時查看完整分析過程，
        # 而不是只能看到 backtest CSV 裡「有實際下單訊號」的部分。
        save_daily_history_snapshot(today_str, latest_analysis_records, today_settled_list)

        render_html_dashboard(
            status_text="已收盤結算完成",
            active_model=gemini.active_model,
            wave1_stocks=wave1_stocks,
            wave2_stocks=wave2_stocks,
            latest_analysis=latest_analysis_records,
            settle_records=today_settled_list,
            total_signals=total_signals
        )
        save_dashboard_state(state)
        print("✅ 本輪次（收盤結算）執行完畢。")
        return

    # 09:15 ~ 13:25 盤中：每 10 分鐘執行一次分析 (09:20, 09:30 ... 13:20)
    # 用 last_analysis_minute_bucket 記錄「上一次已經跑過分析的整 10 分鐘時間戳記」，
    # 避免同一個 10 分鐘區間內，因為 cron 每 5 分鐘觸發一次而被重複執行兩次。
    current_bucket = now.strftime("%Y-%m-%d %H:%M") if now.minute % 10 == 0 else None
    should_analyze = current_bucket is not None and current_bucket != last_bucket

    if not should_analyze:
        print(f"[{now.strftime('%H:%M:%S')}] 尚未到下一個 10 分鐘分析時間點，本輪次僅同步目前看板狀態。")
        render_html_dashboard(
            status_text=f"盤中監控中 ({now.strftime('%H:%M')})",
            active_model=gemini.active_model,
            wave1_stocks=wave1_stocks,
            wave2_stocks=wave2_stocks,
            latest_analysis=latest_analysis_records,
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

            sig = res.get("signal", "WATCH")
            raw_sig = res.get("raw_signal", sig)
            entry_p = res.get("entry")
            if isinstance(entry_p, str):
                try: entry_p = float(entry_p.split()[0].replace("元",""))
                except: entry_p = candles[-1]["close"]

            stop_p = res.get("stop_loss", "-")
            target_p = res.get("target", "-")
            reason = (res.get("reason") or res.get("full_text", "")).replace("\n", " ").strip()
            if len(reason) > 60:
                reason = reason[:60] + "..."

            print(f"  [{symbol} {name}] 訊號: {sig} | 進場: {entry_p} | 停損: {stop_p} | 停利: {target_p}")

            # 用 upsert 併入累積清單：同一檔股票覆蓋更新，不同股票並存，
            # 讓網站顯示的是「當日所有被分析過的股票」而不是只有這一輪的幾檔
            latest_analysis_records = upsert_analysis_record(latest_analysis_records, {
                "symbol": symbol,
                "name": name,
                "signal": sig,
                "entry": entry_p,
                "stop_loss": stop_p,
                "target": target_p,
                "reason": reason,
                "updated_at": now.strftime("%H:%M:%S")
            })

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
    state["total_signals"] = total_signals
    state["last_analysis_minute_bucket"] = current_bucket

    render_html_dashboard(
        status_text=f"盤中分析中 ({now.strftime('%H:%M')})",
        active_model=gemini.active_model,
        wave1_stocks=wave1_stocks,
        wave2_stocks=wave2_stocks,
        latest_analysis=latest_analysis_records,
        total_signals=total_signals
    )
    save_dashboard_state(state)
    print("✅ 本輪次（10 分鐘分析）執行完畢。")

if __name__ == "__main__":
    main()
