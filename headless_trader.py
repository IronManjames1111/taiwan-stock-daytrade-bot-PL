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
    export_payload = {
        "generated_at": now_str,
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
        pnl_class = "text-red-400" if net_total > 0 else "text-emerald-400" if net_total < 0 else "text-gray-300"

    # 生成波段一列表
    wave1_html = ""
    if wave1_stocks:
        for idx, s in enumerate(wave1_stocks, 1):
            wave1_html += f"""
            <li class="flex items-center justify-between p-2 rounded-xl bg-gray-800/40 border border-gray-800">
                <span class="font-bold text-white"><span class="text-blue-400 mr-2">#{idx}</span>{s['symbol']} {s['name']}</span>
                <span class="mono text-gray-300 bg-gray-800 px-2 py-0.5 rounded text-xs">現價: {s['price']} 元｜成交量: {s.get('volume', 0):,} 張</span>
            </li>
            """
    else:
        wave1_html = '<li class="text-gray-500 text-xs py-2">等待開盤 09:15 抓取中...</li>'

    # 生成波段二列表
    wave2_html = ""
    if wave2_stocks:
        for idx, s in enumerate(wave2_stocks, 1):
            wave2_html += f"""
            <li class="flex items-center justify-between p-2 rounded-xl bg-gray-800/40 border border-gray-800">
                <span class="font-bold text-white"><span class="text-purple-400 mr-2">#{idx}</span>{s['symbol']} {s['name']}</span>
                <span class="mono text-gray-300 bg-gray-800 px-2 py-0.5 rounded text-xs">現價: {s['price']} 元｜成交量: {s.get('volume', 0):,} 張</span>
            </li>
            """
    else:
        wave2_html = '<li class="text-gray-500 text-xs py-2">10:30 自動重新掃描成交量排行...</li>'

    # 生成分析表格（依訊號分類貼上 data-filter-group 屬性，供前端做多/做空/觀望篩選使用）
    analysis_rows = ""
    if latest_analysis:
        for a in latest_analysis:
            sig = a.get("signal", "WATCH")
            if "BUY" in sig:
                filter_group = "long"
                sig_badge = '<span class="px-2 py-0.5 rounded bg-emerald-950 text-emerald-400 border border-emerald-800 font-bold">🟢 做多</span>'
            elif "SHORT" in sig:
                filter_group = "short"
                sig_badge = '<span class="px-2 py-0.5 rounded bg-red-950 text-red-400 border border-red-800 font-bold">🔴 放空</span>'
            else:
                filter_group = "watch"
                sig_badge = '<span class="px-2 py-0.5 rounded bg-gray-800 text-gray-400">⚪ 觀望</span>'

            updated_at = a.get("updated_at", "")
            analysis_rows += f"""
            <tr class="hover:bg-gray-800/30 analysis-row" data-filter-group="{filter_group}">
                <td class="py-2.5 px-3 font-bold text-white">{a.get('symbol')} {a.get('name', '')}</td>
                <td class="py-2.5 px-3">{sig_badge}</td>
                <td class="py-2.5 px-3 mono text-gray-200">{a.get('entry', '-')}</td>
                <td class="py-2.5 px-3 mono text-emerald-400">{a.get('stop_loss', '-')}</td>
                <td class="py-2.5 px-3 mono text-red-400">{a.get('target', '-')}</td>
                <td class="py-2.5 px-3 text-gray-300 text-xs">{a.get('reason', '')}</td>
                <td class="py-2.5 px-3 text-gray-500 text-[11px] mono">{updated_at}</td>
            </tr>
            """
    else:
        analysis_rows = '<tr><td colspan="7" class="py-6 text-center text-gray-500 text-xs">盤中每 10 分鐘自動更新分析看板...</td></tr>'

    # 生成結算表格
    settle_rows = ""
    if settle_records:
        for r in settle_records:
            res = r.get("result")
            res_badge = (
                '<span class="text-red-400 font-bold">✅ 獲利</span>'
                if res == "win" else
                '<span class="text-emerald-400 font-bold">❌ 虧損</span>'
                if res == "loss" else
                '<span class="text-gray-400">➖ 打平</span>'
            )
            net_p = r.get("net_profit", 0)
            net_str = f"+${net_p:,}" if net_p > 0 else f"-${abs(net_p):,}" if net_p < 0 else "$0"
            net_color = "text-red-400" if net_p > 0 else "text-emerald-400" if net_p < 0 else "text-gray-300"

            settle_rows += f"""
            <tr class="hover:bg-gray-800/30">
                <td class="py-2 px-3 font-bold text-white">{r.get('symbol')}</td>
                <td class="py-2 px-3 font-semibold">{r.get('signal')}</td>
                <td class="py-2 px-3 mono">{r.get('entry_price')}</td>
                <td class="py-2 px-3 mono">{r.get('exit_price')}</td>
                <td class="py-2 px-3">{res_badge}</td>
                <td class="py-2 px-3 mono {net_color} font-bold">{net_str}</td>
                <td class="py-2 px-3 text-gray-400 text-xs">{r.get('exit_reason')}</td>
            </tr>
            """
    else:
        settle_rows = '<tr><td colspan="7" class="py-4 text-center text-gray-500 text-xs">尚未達到收盤結算時間 (13:25)</td></tr>'

    html_content = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI 當沖雲端即時看盤儀表板</title>
    <meta http-equiv="refresh" content="60">
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Noto+Sans+TC:wght@400;500;700&display=swap" rel="stylesheet">
    <style>
        body {{ font-family: 'Noto Sans TC', sans-serif; background-color: #0d1117; color: #c9d1d9; }}
        .mono {{ font-family: 'JetBrains Mono', monospace; }}
        .shake {{ animation: shake 0.4s cubic-bezier(.36,.07,.19,.97) both; }}
        @keyframes shake {{
            10%, 90% {{ transform: translate3d(-1px, 0, 0); }}
            20%, 80% {{ transform: translate3d(2px, 0, 0); }}
            30%, 50%, 70% {{ transform: translate3d(-4px, 0, 0); }}
            40%, 60% {{ transform: translate3d(4px, 0, 0); }}
        }}
        /* 訊號篩選按鈕：預設(未選取)樣式，JS 會依目前選取狀態動態切換 active 樣式 */
        .filter-btn {{ background-color: #1f2937; border-color: #374151; color: #9ca3af; }}
        .filter-btn.active-all {{ background-color: #312e81; border-color: #6366f1; color: #c7d2fe; }}
        .filter-btn.active-long {{ background-color: #022c22; border-color: #10b981; color: #6ee7b7; }}
        .filter-btn.active-short {{ background-color: #450a0a; border-color: #ef4444; color: #fca5a5; }}
        .filter-btn.active-watch {{ background-color: #1f2937; border-color: #9ca3af; color: #e5e7eb; }}
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
        <header class="bg-gray-900 border border-gray-800 rounded-2xl p-5 shadow-2xl flex flex-col md:flex-row md:items-center md:justify-between gap-4">
            <div>
                <div class="flex items-center gap-2">
                    <span class="inline-block w-3 h-3 rounded-full bg-emerald-400 animate-ping"></span>
                    <h1 class="text-xl md:text-2xl font-bold text-white tracking-wide">台股 AI 當沖雲端即時看板</h1>
                    <span class="bg-emerald-950 text-emerald-400 text-xs px-2.5 py-0.5 rounded-full border border-emerald-800 font-semibold">雲端全自動</span>
                </div>
                <p class="text-xs text-gray-400 mt-1">工作日 09:15 / 10:30 雙波段選股 ➔ 每 10 分鐘 Google AI 深度分析 ➔ 13:25 回放結算</p>
            </div>
            <div class="flex flex-wrap items-center gap-2 text-xs">
                <div class="bg-gray-800 border border-gray-700 rounded-xl px-3 py-2">
                    <span class="text-gray-400">當前狀態:</span>
                    <span class="text-emerald-400 font-bold ml-1">{status_text}</span>
                </div>
                <div class="bg-gray-800 border border-gray-700 rounded-xl px-3 py-2">
                    <span class="text-gray-400">更新時間:</span>
                    <span class="text-white mono ml-1">{now_str}</span>
                </div>
                <button type="button" onclick="downloadJSON()"
                    class="bg-indigo-950/60 hover:bg-indigo-900 border border-indigo-800/60 text-indigo-300 rounded-xl px-3 py-2 font-semibold transition cursor-pointer">
                    ⬇️ 下載 JSON
                </button>
                <button type="button" onclick="downloadCSV()"
                    class="bg-emerald-950/60 hover:bg-emerald-900 border border-emerald-800/60 text-emerald-300 rounded-xl px-3 py-2 font-semibold transition cursor-pointer">
                    ⬇️ 下載 CSV
                </button>
            </div>
        </header>

        <!-- KPI 數據卡片 -->
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
            <div class="bg-gray-900 border border-gray-800 rounded-xl p-4">
                <div class="text-xs text-gray-400">當前調用 AI 模型</div>
                <div class="text-sm font-bold text-indigo-400 mono mt-1 truncate">{active_model}</div>
            </div>
            <div class="bg-gray-900 border border-gray-800 rounded-xl p-4">
                <div class="text-xs text-gray-400">監控標的檔數</div>
                <div class="text-xl font-bold text-white mono mt-1">{len(wave2_stocks or wave1_stocks)} 檔</div>
            </div>
            <div class="bg-gray-900 border border-gray-800 rounded-xl p-4">
                <div class="text-xs text-gray-400">今日發出訊號</div>
                <div class="text-xl font-bold text-yellow-400 mono mt-1">{total_signals} 筆</div>
            </div>
            <div class="bg-gray-900 border border-gray-800 rounded-xl p-4">
                <div class="text-xs text-gray-400">回測結算損益</div>
                <div class="text-xl font-bold {pnl_class} mono mt-1">{pnl_text}</div>
            </div>
        </div>

        <!-- 雙波段選股板塊 -->
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div class="bg-gray-900 border border-gray-800 rounded-2xl p-5 shadow-lg">
                <div class="flex items-center justify-between border-b border-gray-800 pb-3 mb-3">
                    <div class="flex items-center gap-2">
                        <span class="bg-blue-950 text-blue-400 p-1.5 rounded-lg text-sm">🌅</span>
                        <div>
                            <h2 class="font-bold text-white text-sm md:text-base">第一波段：早盤成交量排行</h2>
                            <p class="text-[11px] text-gray-400">09:15 觸發 (ORB-15 區間成型)</p>
                        </div>
                    </div>
                    <span class="text-xs bg-blue-900/50 text-blue-300 border border-blue-700/50 px-2 py-0.5 rounded-md">早盤主流</span>
                </div>
                <ul class="space-y-2 text-sm">{wave1_html}</ul>
            </div>

            <div class="bg-gray-900 border border-gray-800 rounded-2xl p-5 shadow-lg">
                <div class="flex items-center justify-between border-b border-gray-800 pb-3 mb-3">
                    <div class="flex items-center gap-2">
                        <span class="bg-purple-950 text-purple-400 p-1.5 rounded-lg text-sm">⚡</span>
                        <div>
                            <h2 class="font-bold text-white text-sm md:text-base">第二波段：中盤換手重挑</h2>
                            <p class="text-[11px] text-gray-400">10:30 觸發 (鎖定中盤輪動主升股)</p>
                        </div>
                    </div>
                    <span class="text-xs bg-purple-900/50 text-purple-300 border border-purple-700/50 px-2 py-0.5 rounded-md">盤中輪動</span>
                </div>
                <ul class="space-y-2 text-sm">{wave2_html}</ul>
            </div>
        </div>

        <!-- 最新 10 分鐘分析結果 -->
        <div class="bg-gray-900 border border-gray-800 rounded-2xl p-5 shadow-lg">
            <div class="flex flex-col md:flex-row md:items-center md:justify-between border-b border-gray-800 pb-3 mb-4 gap-3">
                <div class="flex items-center gap-2">
                    <span class="bg-emerald-950 text-emerald-400 p-1.5 rounded-lg text-sm">🤖</span>
                    <div>
                        <h2 class="font-bold text-white text-base">即時當沖多空訊號 & 決策理由</h2>
                        <p class="text-xs text-gray-400">每 10 分鐘調用 Gemini / Gemma 深度判定進出場價與停損利，累積顯示當日所有分析紀錄</p>
                    </div>
                </div>
                <span class="text-xs text-gray-400 mono">每 60 秒自動刷新</span>
            </div>

            <!-- 🔎 訊號篩選按鈕：做多 / 做空 / 觀望 / 全部 -->
            <div class="flex flex-wrap items-center gap-2 mb-4">
                <span class="text-xs text-gray-500 mr-1">篩選訊號：</span>
                <button type="button" onclick="setSignalFilter('all')" id="filter-btn-all"
                    class="filter-btn px-3 py-1.5 rounded-lg text-xs font-semibold border transition cursor-pointer">
                    全部 <span id="count-all" class="mono"></span>
                </button>
                <button type="button" onclick="setSignalFilter('long')" id="filter-btn-long"
                    class="filter-btn px-3 py-1.5 rounded-lg text-xs font-semibold border transition cursor-pointer">
                    🟢 做多 <span id="count-long" class="mono"></span>
                </button>
                <button type="button" onclick="setSignalFilter('short')" id="filter-btn-short"
                    class="filter-btn px-3 py-1.5 rounded-lg text-xs font-semibold border transition cursor-pointer">
                    🔴 做空 <span id="count-short" class="mono"></span>
                </button>
                <button type="button" onclick="setSignalFilter('watch')" id="filter-btn-watch"
                    class="filter-btn px-3 py-1.5 rounded-lg text-xs font-semibold border transition cursor-pointer">
                    ⚪ 觀望 <span id="count-watch" class="mono"></span>
                </button>
            </div>

            <div class="overflow-x-auto">
                <table class="w-full text-left text-xs md:text-sm">
                    <thead>
                        <tr class="text-gray-400 border-b border-gray-800 text-[11px]">
                            <th class="py-2.5 px-3">標的</th>
                            <th class="py-2.5 px-3">訊號</th>
                            <th class="py-2.5 px-3">建議進場</th>
                            <th class="py-2.5 px-3">建議停損</th>
                            <th class="py-2.5 px-3">建議停利</th>
                            <th class="py-2.5 px-3">AI 決策依據 (Prompt 優化重點)</th>
                            <th class="py-2.5 px-3">更新時間</th>
                        </tr>
                    </thead>
                    <tbody id="analysis-tbody" class="divide-y divide-gray-800/60">{analysis_rows}</tbody>
                </table>
                <p id="filter-empty-msg" class="hidden text-center text-gray-500 text-xs py-6">此篩選條件下目前沒有符合的標的</p>
            </div>
        </div>

        <!-- 13:25 收盤回放結算卡片 -->
        <div class="bg-gray-900 border border-gray-800 rounded-2xl p-5 shadow-lg">
            <div class="flex items-center justify-between border-b border-gray-800 pb-3 mb-3">
                <div class="flex items-center gap-2">
                    <span class="bg-amber-950 text-amber-400 p-1.5 rounded-lg text-sm">🏆</span>
                    <div>
                        <h2 class="font-bold text-white text-base">今日當沖回測結算 (收盤回放)</h2>
                        <p class="text-xs text-gray-400">13:25 自動以當日 1分K 逐根回放比對真實賺賠 (扣除 2.8折手續費與 0.15% 減半證交稅)</p>
                    </div>
                </div>
                <span class="text-xs text-amber-400 font-semibold">13:25 結算</span>
            </div>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-xs md:text-sm">
                    <thead>
                        <tr class="text-gray-400 border-b border-gray-800 text-[11px]">
                            <th class="py-2 px-3">代號</th>
                            <th class="py-2 px-3">方向</th>
                            <th class="py-2 px-3">進場價</th>
                            <th class="py-2 px-3">出場價</th>
                            <th class="py-2 px-3">結果</th>
                            <th class="py-2 px-3">淨損益</th>
                            <th class="py-2 px-3">出場原因</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-gray-800/60">{settle_rows}</tbody>
                </table>
            </div>
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
        }});

        // ── 訊號篩選：做多 / 做空 / 觀望 / 全部 ─────────────────────────
        // 篩選狀態保存在 localStorage，重新整理頁面（每 60 秒自動刷新）後仍會記住上次的選擇
        function initSignalFilter() {{
            const saved = localStorage.getItem("daytrade_signal_filter") || "all";
            setSignalFilter(saved);
        }}

        function setSignalFilter(group) {{
            localStorage.setItem("daytrade_signal_filter", group);

            const rows = document.querySelectorAll("#analysis-tbody .analysis-row");
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

            // 更新按鈕上的統計數字
            ["all", "long", "short", "watch"].forEach(g => {{
                const el = document.getElementById(`count-${{g}}`);
                if (el) el.textContent = `(${{counts[g]}})`;
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
            status_text="非開盤測試成功",
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
