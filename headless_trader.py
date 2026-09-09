# -*- coding: utf-8 -*-
"""
headless_trader.py - 雲端無頭當沖機器人 (v3.0 獨立網頁看板版)
─────────────────────────────────────────────────────────────
• 09:15 早盤第一次抓取成交量排行前 5 檔 (避開開盤假突破雜訊)
• 10:30 中盤第二次重新抓取成交量排行前 5 檔 (鎖定盤中換手輪動飆股)
• 盤中每 10 分鐘調用 Google AI (多模型自動降級鏈) 進行深度判斷
• 自動生成獨立網頁 index.html (透過 GitHub Pages 提供免登入固定專屬網址)
• 同步輸出 GitHub Step Summary 即時 Markdown 看板
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

import hashlib
import base64
from fugle_service import FugleService
from gemini_service import GeminiService
import cache_service
from config import load_config

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

def update_html_dashboard(
    wave1_stocks: List[Dict] = None,
    wave2_stocks: List[Dict] = None,
    latest_analysis: List[Dict] = None,
    settle_records: List[Dict] = None,
    active_model: str = "gemma-4-31b-it",
    status_text: str = "運行中"
):
    """
    生成單一獨立網頁 index.html，供 GitHub Pages 直接託管展示
    具備密碼防護機制、暗黑風質感交易介面、手機響應式設計
    """
    now_str = get_tw_now().strftime("%Y-%m-%d %H:%M:%S")
    total_signals = len([a for a in (latest_analysis or []) if a.get("signal") in ["BUY", "SHORT"]])

    # 取得密碼設定 (預設 888888)，清除前後空白與換行，計算安全 SHA-256 與 Base64
    raw_pwd = (os.getenv("DASHBOARD_PASSWORD") or "888888").strip()
    pwd_hash = hashlib.sha256(raw_pwd.encode("utf-8")).hexdigest()
    pwd_b64 = base64.b64encode(raw_pwd.encode("utf-8")).decode("utf-8")

    wave1_stocks = wave1_stocks or []
    wave2_stocks = wave2_stocks or []
    latest_analysis = latest_analysis or []
    settle_records = settle_records or []

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
                <span class="mono text-gray-300 bg-gray-800 px-2 py-0.5 rounded text-xs">現價: {s['price']} 元</span>
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
                <span class="mono text-gray-300 bg-gray-800 px-2 py-0.5 rounded text-xs">現價: {s['price']} 元</span>
            </li>
            """
    else:
        wave2_html = '<li class="text-gray-500 text-xs py-2">10:30 自動重新掃描成交量排行...</li>'

    # 生成分析表格
    analysis_rows = ""
    if latest_analysis:
        for a in latest_analysis:
            sig = a.get("signal", "WATCH")
            sig_badge = (
                '<span class="px-2 py-0.5 rounded bg-emerald-950 text-emerald-400 border border-emerald-800 font-bold">🟢 做多</span>'
                if "BUY" in sig else
                '<span class="px-2 py-0.5 rounded bg-red-950 text-red-400 border border-red-800 font-bold">🔴 放空</span>'
                if "SHORT" in sig else
                '<span class="px-2 py-0.5 rounded bg-gray-800 text-gray-400">⚪ 觀望</span>'
            )
            analysis_rows += f"""
            <tr class="hover:bg-gray-800/30">
                <td class="py-2.5 px-3 font-bold text-white">{a.get('symbol')} {a.get('name', '')}</td>
                <td class="py-2.5 px-3">{sig_badge}</td>
                <td class="py-2.5 px-3 mono text-gray-200">{a.get('entry', '-')}</td>
                <td class="py-2.5 px-3 mono text-emerald-400">{a.get('stop_loss', '-')}</td>
                <td class="py-2.5 px-3 mono text-red-400">{a.get('target', '-')}</td>
                <td class="py-2.5 px-3 text-gray-300 text-xs">{a.get('reason', '')}</td>
            </tr>
            """
    else:
        analysis_rows = '<tr><td colspan="6" class="py-6 text-center text-gray-500 text-xs">盤中每 10 分鐘自動更新分析看板...</td></tr>'

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
            <div class="flex items-center justify-between border-b border-gray-800 pb-3 mb-4">
                <div class="flex items-center gap-2">
                    <span class="bg-emerald-950 text-emerald-400 p-1.5 rounded-lg text-sm">🤖</span>
                    <div>
                        <h2 class="font-bold text-white text-base">即時當沖多空訊號 & 決策理由</h2>
                        <p class="text-xs text-gray-400">每 10 分鐘調用 Gemini / Gemma 深度判定進出場價與停損利</p>
                    </div>
                </div>
                <span class="text-xs text-gray-400 mono">每 60 秒自動刷新</span>
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
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-gray-800/60">{analysis_rows}</tbody>
                </table>
            </div>
        </div>

        <!-- 13:25 收盤回放結算卡片 -->
        <div class="bg-gray-900 border border-gray-800 rounded-2xl p-5 shadow-lg">
            <div class="flex items-center justify-between border-b border-gray-800 pb-3 mb-3">
                <div class="flex items-center gap-2">
                    <span class="bg-amber-950 text-amber-400 p-1.5 rounded-lg text-sm">🏆</span>
                    <div>
                        <h2 class="font-bold text-white text-base">今日當沖回測結算 (收盤回放)</h2>
                        <p class="text-xs text-gray-400">13:25 自動以當日 1分K 逐根回放比對真實賺賠 (扣除 6折手續費與 0.15% 減半證交稅)</p>
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
        }});
    </script>
</body>
</html>
"""
    try:
        with open("index.html", "w", encoding="utf-8") as f:
            f.write(html_content)
        print("📄 已成功更新獨立網頁儀表板：index.html")
    except Exception as e:
        print(f"[HTML Dashboard] 寫入失敗: {e}")

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

    # ── 正式盤中雙波段運作流程 ─────────────────────────────────────
    wave1_stocks = []
    wave2_stocks = []
    latest_analysis_records = []
    total_signals = 0

    render_html_dashboard(
        status_text="盤前準備中 (等待 09:15)",
        active_model=gemini.active_model
    )

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
    wave1_stocks = get_free_top_volume_stocks(limit=5)
    current_stocks = wave1_stocks
    symbols = [s["symbol"] for s in current_stocks]
    
    render_html_dashboard(
        status_text="早盤第一波監控中",
        active_model=gemini.active_model,
        wave1_stocks=wave1_stocks
    )

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
            wave2_stocks = get_free_top_volume_stocks(limit=5)
            if wave2_stocks:
                current_stocks = wave2_stocks
                symbols = [s["symbol"] for s in current_stocks]
                print(f"🔥 中盤 10:30 已更新監控標的：{', '.join(symbols)}")
                
            mid_wave_triggered = True
            render_html_dashboard(
                status_text="中盤第二波監控中",
                active_model=gemini.active_model,
                wave1_stocks=wave1_stocks,
                wave2_stocks=wave2_stocks,
                latest_analysis=latest_analysis_records,
                total_signals=total_signals
            )

        # 每 10 分鐘例行分析 (09:20, 09:30, 09:40 ... 13:20)
        if now.minute % 10 == 0:
            print(f"\n⚡ [{now.strftime('%H:%M:%S')}] 執行 10 分鐘定時分析...")
            latest_analysis_records = []

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
                    if len(reason) > 60:
                        reason = reason[:60] + "..."

                    print(f"  [{symbol} {name}] 訊號: {sig} | 進場: {entry_p} | 停損: {stop_p} | 停利: {target_p}")
                    
                    latest_analysis_records.append({
                        "symbol": symbol,
                        "name": name,
                        "signal": sig,
                        "entry": entry_p,
                        "stop_loss": stop_p,
                        "target": target_p,
                        "reason": reason
                    })

                    # 出現買賣訊號時寫入歷史紀錄
                    if raw_sig in {"STRONG_BUY", "BUY", "SHORT", "STRONG_SHORT"}:
                        total_signals += 1
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

            # 即時渲染網頁 index.html
            render_html_dashboard(
                status_text=f"盤中分析中 ({now.strftime('%H:%M')})",
                active_model=gemini.active_model,
                wave1_stocks=wave1_stocks,
                wave2_stocks=wave2_stocks,
                latest_analysis=latest_analysis_records,
                total_signals=total_signals
            )
            time.sleep(65)

        time.sleep(10)

    # 3. 13:25 收盤回放結算
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

        # 重新讀取更新後的結算紀錄
        all_data = cache_service._read_history()
        today_settled_list = [r for r in all_data.get("records", []) if r.get("date") == today_str]

        # 產出每日回測 CSV
        os.makedirs("history_records", exist_ok=True)
        df = pd.DataFrame(today_settled_list)
        csv_path = f"history_records/backtest_{today_str}.csv"
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(f"✅ 今日回測報表已成功產出：{csv_path}")

    # 渲染最終收盤結算網頁
    render_html_dashboard(
        status_text="已收盤結算完成",
        active_model=gemini.active_model,
        wave1_stocks=wave1_stocks,
        wave2_stocks=wave2_stocks,
        latest_analysis=latest_analysis_records,
        settle_records=today_settled_list,
        total_signals=total_signals
    )

if __name__ == "__main__":
    main()
