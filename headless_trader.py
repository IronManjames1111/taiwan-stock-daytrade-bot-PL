# -*- coding: utf-8 -*-
"""
headless_trader.py - 雲端無頭當沖機器人 (v22 成本控管 / 逐根回放 / 穩定性修正版)

v23 重點（詳見「更新說明_v23.md」）：
  • 每日固定本金資金池（預設 100 萬）：進場扣除佔用資金、出場「本金＋淨損益」回補；
    同輪多檔訊號由程式依票數與淨賺賠比分配資金，不足則縮減張數或略過
  • 儀表板新增「當日資金曲線 / 資金佔用 / 每日資金變化」圖表，資金摘要存於 history_records/capital_history.json
  • 同檔每日進場次數與出場冷卻預設不設限；新增五組獨立影子策略並行記錄每日績效

v22 重點（詳見「優化說明_v22.md」）：
  • 結算清單改以 strategy_trades 為單一來源（修復收盤後仍顯示「尚未結算」）
  • 出場改為掃描「進場後的每一根 K 棒」，不再只看最後一根
  • 先存狀態、再渲染；渲染失敗只警告；訊號只用已收完的 K 棒
  • 進場前做成本檢查（淨賺賠比）、固定風險部位、每檔每日次數與冷卻
  • RISK_MODE 真正影響門檻；策略票數改為「獨立家族」計票
  • 交易日曆（國定假日不跑）、Yahoo 重試與沿用上次標的、K 線並行抓取
  • 可選 loop 模式：單一 job 內每 60 秒一輪（RUN_MODE=loop）

以下為原 v4.0 說明（單輪執行 + 狀態持久化版）：
─────────────────────────────────────────────────────────────
• 09:05 早盤第一次抓取成交量排行前 5 檔，並開始盤中 AI 分析（v20 調整，原為 09:15）
• 10:30 中盤第二次重新抓取成交量排行前 5 檔 (鎖定盤中換手輪動飆股)
• 盤中每 60 秒以本地技術策略判斷；13:00 後停止新進場，持倉仍監控
• 自動生成獨立網頁 index.html (透過 GitHub Pages 提供免登入固定專屬網址)
• 同步輸出 GitHub Step Summary 即時 Markdown 看板
• 13:25 收盤自動回放當日 1分K 結算盈虧（已扣手續費與證交稅），產出 CSV 報表保存至 GitHub

v4.0 架構變更說明：
────────────────
舊版本用單一個 GitHub Actions job、從 09:15 內部 while 迴圈一路等到 13:25 才結束，
中間每輪會呼叫 render_html_dashboard() 更新本地 index.html，
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
import csv
import html as _html_mod
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
from concurrent.futures import ThreadPoolExecutor

import hashlib
import base64
from fugle_service import FugleService
from tw_market_rules import calc_limit_prices as _calc_limit_prices, check_at_limit as _check_at_limit
from tw_market_rules import extract_symbol_rules, check_entry_allowed
from indicator_strategies import (DEFAULT_SETTINGS, STRATEGY_NAMES, evaluate as evaluate_strategies,
                                  load_strategy_settings, normalize_settings, position_levels,
                                  apply_risk_mode, RISK_MODE_PRESETS,
                                  experiment_settings)
import trade_engine as te
import market_calendar
from dashboard_capital import CAPITAL_CARD_HTML, CAPITAL_JS
from config import load_config

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

TW_TZ = pytz.timezone("Asia/Taipei")


class _StrategyDisplay:
    """Compatibility label for legacy dashboard/settlement helpers; no AI calls."""
    active_model = "純技術指標（本地策略）"


def new_strategy_experiment_state(date_str: str, strategy_settings: Dict = None) -> Dict:
    """建立五組互相獨立的影子交易帳本，依使用者選定的訊號模組組合執行。"""
    mode_configs = experiment_settings(strategy_settings)
    return {
        "date": date_str,
        "modes": {
            key: {
                "name": spec["name"], "indicators": spec["indicators"], "settings": spec["settings"],
                "open_positions": [], "trades": [], "analysis_log": [],
                "summary": None,
            }
            for key, spec in mode_configs.items()
        },
    }


def ensure_strategy_experiment_state(state: Dict, date_str: str, strategy_settings: Dict = None) -> Dict:
    """補齊舊版狀態並套用最新五組設定；保留當日已有交易、持倉與日誌。"""
    experiments = state.get("strategy_experiments")
    if not isinstance(experiments, dict) or experiments.get("date") != date_str:
        experiments = new_strategy_experiment_state(date_str, strategy_settings)
        state["strategy_experiments"] = experiments
        return experiments
    if not isinstance(experiments.get("modes"), dict):
        experiments["modes"] = {}
    defaults = new_strategy_experiment_state(date_str, strategy_settings)["modes"]
    modes = experiments["modes"]
    legacy_modes = {key: value for key, value in modes.items() if key not in defaults}
    if legacy_modes:
        if not isinstance(experiments.get("legacy_modes"), dict):
            experiments["legacy_modes"] = {}
        experiments["legacy_modes"].update(legacy_modes)
    experiments["modes"] = {key: modes.get(key, default) for key, default in defaults.items()}
    for key, default in defaults.items():
        mode = experiments["modes"][key]
        if not isinstance(mode, dict):
            mode = default
            experiments["modes"][key] = mode
        for field in ("open_positions", "trades", "analysis_log"):
            if not isinstance(mode.get(field), list):
                mode[field] = []
        # 取最新設定供後續輪次使用；每筆分析/交易另存設定快照以保留可追溯性。
        mode.update(name=default["name"], indicators=default["indicators"], settings=default["settings"])
    return experiments

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

def _esc(value) -> str:
    """輸出到 HTML 前一律跳脫（股票名稱來自外部爬蟲，策略理由含 <、& 等符號，不能直接塞進頁面）。"""
    return _html_mod.escape(str(value), quote=True)


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
    open_positions: List[Dict] = None,
    strategy_trades: List[Dict] = None,
    strategy_settings: Dict = None,
    strategy_experiments: Dict = None,
    settled: bool = False,
    capital: Dict = None,
    capital_history: List[Dict] = None,
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
    open_positions = open_positions or []
    strategy_trades = strategy_trades or []
    strategy_experiments = strategy_experiments or {}
    strategy_settings = normalize_settings(strategy_settings or load_strategy_settings())
    strategy_labels_html = "".join(
        f'<label class="flex items-center gap-2 rounded-lg bg-black/20 p-2 text-xs"><input type="checkbox" id="strategy-enabled-{key}" class="accent-blue-400">{label}</label>'
        for key, label in STRATEGY_NAMES.items()
    )
    def _experiment_indicator_labels(combo):
        return "".join(
            f'<label class="flex items-start gap-2 rounded bg-slate-900/70 p-2 text-[11px]"><input type="checkbox" id="experiment-config-indicator-{combo["id"]}-{key}" class="accent-blue-400 mt-0.5" {"checked" if key in combo["indicators"] else ""}><span>{_esc(label)}</span></label>'
            for key, label in STRATEGY_NAMES.items()
        )

    experiment_config_html = "".join(
        '<article class="rounded-xl border border-white/10 bg-black/20 p-3">'
        f'<label class="block text-xs text-gray-400">策略名稱<input id="experiment-config-name-{combo["id"]}" type="text" maxlength="48" value="{_esc(combo["name"])}" class="setting-input w-full mt-1 rounded bg-slate-900 border border-slate-700 p-2 text-white"></label>'
        '<p class="text-[11px] text-gray-500 mt-2">勾選項目必須全部同方向成立（至少 2 項）</p>'
        f'<div class="grid grid-cols-1 sm:grid-cols-2 gap-1 mt-2">{_experiment_indicator_labels(combo)}</div>'
        '</article>'
        for combo in strategy_settings["experiment_strategies"]
    )
    strategy_stats = {}
    for trade in strategy_trades:
        key = trade.get("strategy_name", trade.get("strategy", "未分類"))
        stat = strategy_stats.setdefault(key, {"entries": 0, "closed": 0, "open": 0, "wins": 0, "pnl": 0.0, "floating_pnl": 0.0})
        stat["entries"] += 1
        if trade.get("status") == "closed":
            stat["closed"] += 1
            stat["wins"] += int(float(trade.get("pnl_amount", 0) or 0) > 0)
            stat["pnl"] += float(trade.get("pnl_amount", 0) or 0)
        elif trade.get("status") == "open":
            stat["open"] += 1
            quote = live_quotes.get(str(trade.get("symbol")), {})
            try:
                stat["floating_pnl"] += te.gross_pnl(trade.get("signal", "BUY"), float(trade["entry_price"]),
                                                    float(quote["price"]), int(trade.get("shares", 0)))
            except (KeyError, TypeError, ValueError):
                pass
    position_rows = []
    for pos in open_positions:
        symbol = str(pos.get("symbol", ""))
        quote = live_quotes.get(symbol, {})
        current_price = quote.get("price")
        floating_pnl = None
        progress = None
        try:
            entry_price = float(pos["entry_price"])
            target_price = float(pos["take_profit"])
            if current_price is not None:
                current_price = float(current_price)
                floating_pnl = te.gross_pnl(pos.get("signal", "BUY"), entry_price, current_price, int(pos.get("shares", 0)))
                distance = target_price - entry_price
                progress = (current_price - entry_price) / distance * 100 if distance else None
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            pass
        pnl_class = "text-[#ff5470]" if floating_pnl is not None and floating_pnl > 0 else "text-[#00d68f]" if floating_pnl is not None and floating_pnl < 0 else "text-gray-400"
        pnl_text = f'{floating_pnl:+,.0f}' if floating_pnl is not None else "-"
        progress_text = f'{progress:.0f}%' if progress is not None else "-"
        position_rows.append(
            f'<tr><td class="py-2 px-3 whitespace-nowrap">{_esc(symbol)} {_esc(pos.get("name", ""))}</td>'
            f'<td>{_esc(pos.get("direction"))}</td><td>{_esc(pos.get("strategy_name"))}</td>'
            f'<td class="mono">{_esc(pos.get("entry_time", "-"))}</td><td class="mono">{pos.get("entry_price", "-")}</td>'
            f'<td class="mono">{current_price if current_price is not None else "-"}</td>'
            f'<td class="mono {pnl_class}">{pnl_text}<span class="block text-[10px] text-gray-500">未扣費稅</span></td>'
            f'<td class="mono text-[#00d68f]">{pos.get("stop_loss", "-")}</td><td class="mono text-[#ff5470]">{pos.get("take_profit", "-")}</td>'
            f'<td class="mono">{progress_text}</td></tr>'
        )
    positions_html = "".join(position_rows) or '<tr><td colspan="10" class="py-4 text-center text-gray-500">目前沒有持倉</td></tr>'
    def _win_rate_text(stat):
        # 已平倉筆數為 0（例如剛進場尚未出場）時不可相除，改顯示 "-"
        closed = stat["closed"]
        return f"{stat['wins'] / closed * 100:.1f}%" if closed > 0 else "-"
    strategy_html = "".join(
        f'<tr><td>{_esc(name)}</td><td>{stat["entries"]}</td><td>{stat["closed"]}</td><td>{stat["open"]}</td><td>{_win_rate_text(stat)}</td><td>{stat["pnl"]:+,.0f}</td><td>{stat["floating_pnl"]:+,.0f}</td></tr>'
        for name, stat in sorted(strategy_stats.items(), key=lambda item: (item[1]["entries"], item[1]["pnl"]), reverse=True)
    ) or '<tr><td colspan="7" class="py-4 text-center text-gray-500">尚無策略交易</td></tr>'
    experiment_rows = []
    for mode_key, mode in (strategy_experiments.get("modes") or {}).items():
        summary = te.summarize_trades(mode.get("trades") or [])
        win_rate = f"{summary['win_rate'] * 100:.1f}%" if summary["win_rate"] is not None else "-"
        experiment_rows.append(
            f'<tr><td class="py-2 px-3">{_esc(mode.get("name") or mode_key)}</td>'
            f'<td>{summary["entries"]}</td><td>{summary["closed"]}</td><td>{win_rate}</td>'
            f'<td class="mono">{summary["net_pnl"]:+,.0f}</td>'
            f'<td>{len(mode.get("open_positions") or [])}</td></tr>'
        )
    experiment_stats_html = "".join(experiment_rows) or '<tr><td colspan="6" class="py-2 text-center text-gray-500">尚無平行策略紀錄</td></tr>'

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
        "open_positions": open_positions,
        "strategy_trades": strategy_trades,
        "strategy_settings": strategy_settings,
        "strategy_experiments": strategy_experiments,
        "capital": capital,
        "capital_history": capital_history or [],
    }
    # ensure_ascii=False 保留中文可讀；再用 json.dumps 序列化成字串安全地塞進 <script> 的 JS 常數
    export_json_str = json.dumps(export_payload, ensure_ascii=False, indent=2)
    # </script> 若原封不動出現在字串內會提前結束 script 標籤，需要跳脫
    export_json_js_safe = export_json_str.replace("</", "<\\/")

    # 計算損益卡片文字
    pnl_text = "今日無交易" if settled else "尚未結算"
    pnl_class = "text-gray-400"
    if settle_records:
        # 【bug修復】cache_service._compute_settle_result() 回傳的欄位是
        # "pnl_amount"，從來沒有 "net_profit" 這個 key。原本這裡誤用
        # r.get("net_profit", 0) 讀取，每次都拿不到值、靜默 fallback 成 0，
        # 導致「結算損益」KPI 卡片不論實際賺賠多少，永遠顯示 $0。
        net_total = round(sum(float(r.get("pnl_amount", 0) or 0) for r in settle_records))
        pnl_text = f"+${net_total:,}" if net_total > 0 else f"-${abs(net_total):,}" if net_total < 0 else "$0"
        pnl_class = "text-[#ff5470]" if net_total > 0 else "text-[#00d68f]" if net_total < 0 else "text-gray-300"

    # 生成波段一列表
    wave1_html = ""
    if wave1_stocks:
        for idx, s in enumerate(wave1_stocks, 1):
            wave1_html += f"""
            <li class="panel-raised border rounded-lg p-2.5 flex items-center justify-between gap-2">
                <span class="font-bold text-white text-sm whitespace-nowrap"><span class="text-[#8db3ff] mr-1.5">#{idx}</span>{_esc(s['symbol'])} {_esc(s['name'])}</span>
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
                <span class="font-bold text-white text-sm whitespace-nowrap"><span class="text-[#c4a6ff] mr-1.5">#{idx}</span>{_esc(s['symbol'])} {_esc(s['name'])}</span>
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
            symbol = _esc(a.get("symbol", ""))
            name = _esc(a.get("name", ""))
            entry = _esc(a.get("entry", "-"))
            stop_loss = _esc(a.get("stop_loss", "-"))
            target = _esc(a.get("target", "-"))
            reason = _esc(a.get("reason", ""))

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
        analysis_rows = '<tr><td colspan="7" class="py-6 text-center text-gray-500 text-xs">盤中依排程自動更新技術策略看板...</td></tr>'
        analysis_cards = '<div class="text-center text-gray-500 text-xs py-6">盤中依排程自動更新技術策略看板...</div>'

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
            net_p = round(float(r.get("pnl_amount", 0) or 0))
            net_str = f"+${net_p:,}" if net_p > 0 else f"-${abs(net_p):,}" if net_p < 0 else "$0"
            net_color = "text-[#ff5470]" if net_p > 0 else "text-[#00d68f]" if net_p < 0 else "text-gray-300"
            symbol = _esc(r.get("symbol", ""))
            stock_name = _esc(r.get("name", ""))
            symbol_label = f'{symbol} <span class="text-gray-400 font-normal">{stock_name}</span>' if stock_name else symbol
            stock_name_label = f'<span class="text-gray-400 font-normal text-xs">{stock_name}</span>' if stock_name else ""
            signal = _esc(r.get("signal", ""))
            strategy = _esc(r.get("strategy") or r.get("strategy_name") or "-")
            entry_price = _esc(r.get("entry_price", "-"))
            exit_price = _esc(r.get("exit_price", "-"))
            # 【bug修復】exit_reason 原本是 hit_sl/hit_tp/forced_close 這種
            # 給程式看的英文代碼，直接顯示在畫面上使用者看不懂，這裡轉成中文。
            exit_reason = format_exit_reason(r.get("exit_reason", "-"))

            settle_rows += f"""
            <tr class="hover:bg-white/[0.02]">
                <td class="py-2 px-3 font-bold text-white whitespace-nowrap">{symbol_label}</td>
                <td class="py-2 px-3 font-semibold whitespace-nowrap">{signal}</td>
                <td class="py-2 px-3 text-[#8db3ff]">{strategy}</td>
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
                    <span class="font-bold text-white text-sm">{symbol}　{stock_name_label} <span class="text-gray-400 font-normal text-xs">{signal}</span></span>
                    {res_badge}
                </div>
                <div class="data-row"><span class="dlabel">進場 → 出場</span><span class="dvalue mono">{entry_price} → {exit_price}</span></div>
                <div class="data-row"><span class="dlabel">進場策略</span><span class="dvalue">{strategy}</span></div>
                <div class="data-row"><span class="dlabel">淨損益</span><span class="dvalue mono {net_color} font-bold">{net_str}</span></div>
                <div class="data-row"><span class="dlabel">出場原因</span><span class="dvalue text-xs">{exit_reason}</span></div>
            </div>
            """
    else:
        _empty_msg = ("今日已收盤結算：沒有任何已平倉的交易" if settled
                      else f"尚未達到收盤結算時間 ({HISTORY_SETTLE_TIME})")
        settle_rows = f'<tr><td colspan="8" class="py-4 text-center text-gray-500 text-xs">{_empty_msg}</td></tr>'
        settle_cards = f'<div class="text-center text-gray-500 text-xs py-4">{_empty_msg}</div>'

    html_content = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
    <title>台股當沖技術策略終端</title>
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
                <h2 class="text-xl font-bold text-white tracking-wide">台股當沖技術策略終端</h2>
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
                    <h1 class="text-lg md:text-xl font-bold text-white tracking-tight">台股技術策略當沖雲端終端</h1>
                    <span class="bg-[#06231b] text-[#00d68f] text-[11px] px-2 py-0.5 rounded-full border border-[#00d68f]/25 font-semibold whitespace-nowrap">雲端全自動</span>
                </div>
                <p class="text-xs text-gray-500 mt-1">{ANALYSIS_START_TIME} / {MID_WAVE_TRIGGER_TIME} 成交量選股　·　多策略逐輪判斷（{ANALYSIS_STOP_TIME} 後不再進場）　·　{HISTORY_SETTLE_TIME} 結算</p>
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
                <div class="text-[11px] text-gray-500">判斷方式</div>
                <div class="text-sm font-bold text-[#8db3ff] mono mt-1 truncate">{active_model}</div>
            </div>
            <div class="panel border rounded-xl p-4">
                <div class="text-[11px] text-gray-500">監控標的</div>
                <div class="text-xl font-bold text-white mono mt-1">{len(wave2_stocks or wave1_stocks)} 檔</div>
            </div>
            <div class="panel border rounded-xl p-4">
                <div class="text-[11px] text-gray-500">今日進場</div>
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

        <section class="panel border rounded-2xl p-5">
            <div class="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-2 mb-3"><div><h2 class="font-bold text-white text-base">目前持倉監控</h2><p class="text-[11px] text-gray-500 mt-1">策略每輪（預設每分鐘）以分 K 高低價檢查停利／停損；浮動損益為未扣費稅估值。</p></div><span class="text-[11px] text-gray-500">正式策略模擬持倉 · <b id="open-position-count">{len(open_positions)}</b> 檔</span></div>
            <div class="analysis-table-wrap overflow-x-auto"><table class="w-full text-left text-xs"><thead><tr class="text-gray-500 border-b border-white/5"><th class="py-2 px-3">標的</th><th>方向</th><th>進場策略</th><th>進場時間</th><th>進場價</th><th>最新價</th><th>浮動損益</th><th>停損</th><th>停利</th><th>目標進度</th></tr></thead><tbody id="open-position-tbody">{positions_html}</tbody></table></div>
        </section>

        <section class="panel border rounded-2xl p-5">
            <h2 class="font-bold text-white text-base mb-1">正式策略績效</h2><p class="text-[11px] text-gray-500 mb-3">已平倉損益已扣手續費與稅；未實現損益尚未扣費稅。</p>
            <div class="analysis-table-wrap overflow-x-auto"><table class="w-full text-left text-xs"><thead><tr class="text-gray-500 border-b border-white/5"><th class="py-2 px-3">策略組合</th><th>進場</th><th>已平倉</th><th>持倉</th><th>勝率</th><th>已實現淨損益</th><th>未實現損益</th></tr></thead><tbody id="strategy-stat-tbody">{strategy_html}</tbody></table></div>
        </section>

        <section class="panel border rounded-2xl p-5">
            <div class="flex flex-col md:flex-row md:items-end md:justify-between gap-3 mb-3"><div><h2 class="font-bold text-white text-base mb-1">五組策略每日比較（影子交易）</h2><p class="text-xs text-gray-500">五組各自獨立持倉與本金；組內勾選的指標模組須全數同方向成立。完整訊號與成交明細會保存在每日 JSON 日誌。</p></div><div class="flex flex-wrap items-end gap-2"><label class="text-xs text-gray-400">切換策略<select id="experiment-strategy-select" class="setting-input block min-w-64 mt-1 rounded bg-slate-900 border border-slate-700 p-2 text-white"><option>載入策略中…</option></select></label><button onclick="downloadStrategyExperimentLogs()" class="px-3 py-2 rounded-lg bg-slate-700 text-white text-xs">下載這天的策略日誌 JSON</button></div></div>
            <div id="experiment-selected-meta" class="text-xs text-gray-400 mb-3">選擇策略查看組合條件、績效與明細。</div>
            <div id="experiment-selected-metrics" class="grid grid-cols-2 md:grid-cols-5 gap-2 mb-4"></div>
            <div class="grid grid-cols-1 xl:grid-cols-2 gap-4">
                <div><h3 class="text-xs text-gray-400 mb-2">五組績效總覽（點列可切換）</h3><div class="analysis-table-wrap overflow-x-auto"><table class="w-full text-left text-xs"><thead><tr class="text-gray-500 border-b border-white/5"><th class="py-2 px-3">策略組合</th><th>指標條件</th><th>進場</th><th>平倉</th><th>勝率</th><th>已實現淨損益</th><th>未實現毛損益</th><th>持倉</th></tr></thead><tbody id="strategy-experiment-tbody"><tr><td colspan="8" class="py-2 text-center text-gray-500">尚無平行策略紀錄</td></tr></tbody></table></div></div>
                <div><h3 class="text-xs text-gray-400 mb-2">該策略目前持倉（影子）</h3><div class="analysis-table-wrap overflow-x-auto"><table class="w-full text-left text-xs"><thead><tr class="text-gray-500 border-b border-white/5"><th class="py-2 px-2">標的</th><th>方向</th><th>進場</th><th>現價</th><th>停損／停利</th><th>浮動損益</th></tr></thead><tbody id="experiment-open-tbody"><tr><td colspan="6" class="py-2 text-center text-gray-500">尚無持倉</td></tr></tbody></table></div></div>
                <div><h3 class="text-xs text-gray-400 mb-2">該策略已進場紀錄</h3><div class="analysis-table-wrap overflow-x-auto max-h-72"><table class="w-full text-left text-xs"><thead><tr class="text-gray-500 border-b border-white/5"><th class="py-2 px-2">標的</th><th>方向</th><th>進場／出場</th><th>結果</th><th>淨損益</th></tr></thead><tbody id="experiment-trades-tbody"><tr><td colspan="5" class="py-2 text-center text-gray-500">尚無交易</td></tr></tbody></table></div></div>
                <div><h3 class="text-xs text-gray-400 mb-2">最近訊號日誌</h3><div class="analysis-table-wrap overflow-x-auto max-h-72"><table class="w-full text-left text-xs"><thead><tr class="text-gray-500 border-b border-white/5"><th class="py-2 px-2">時間</th><th>標的</th><th>訊號</th><th>結果／條件</th></tr></thead><tbody id="experiment-log-tbody"><tr><td colspan="4" class="py-2 text-center text-gray-500">尚無分析日誌</td></tr></tbody></table></div></div>
            </div>
        </section>

        <details class="panel border rounded-2xl p-5">
            <summary class="font-bold text-white cursor-pointer">策略與風控設定</summary>
            <p class="text-xs text-gray-400 mt-3">每組策略可重新命名並勾選訊號模組；組內所有勾選項必須同方向、同時成立才會建立影子交易。此靜態頁面不能直接修改 GitHub，按下載後請將 strategy_settings.json 放回 repo 根目錄並提交，下一輪 Actions 才會套用。瀏覽器會暫存本機草稿。</p>
            <h3 class="font-bold text-white text-sm mt-4 mb-2">正式策略模組（主帳本）</h3><div class="grid grid-cols-2 md:grid-cols-4 gap-2">{strategy_labels_html}</div>
            <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mt-4 text-xs">
                <label>最少同向策略家族數<input id="setting-min-votes" type="number" min="2" max="8" step="1" class="setting-input w-full mt-1 rounded bg-slate-900 border border-slate-700 p-2"></label>
                <label>領先反向票數<input id="setting-min-vote-margin" type="number" min="1" max="8" step="1" class="setting-input w-full mt-1 rounded bg-slate-900 border border-slate-700 p-2"></label>
                <label>量能倍數<input id="setting-volume-multiple" type="number" min="0.5" max="5" step="0.1" class="setting-input w-full mt-1 rounded bg-slate-900 border border-slate-700 p-2"></label>
                <label>最低K線數<input id="setting-min-bars" type="number" min="14" max="120" step="1" class="setting-input w-full mt-1 rounded bg-slate-900 border border-slate-700 p-2"></label>
                <label>停損 ATR 倍數<input id="setting-stop-atr" type="number" min="0.5" max="5" step="0.05" class="setting-input w-full mt-1 rounded bg-slate-900 border border-slate-700 p-2"></label>
                <label>停利 ATR 倍數<input id="setting-target-atr" type="number" min="0.5" max="10" step="0.05" class="setting-input w-full mt-1 rounded bg-slate-900 border border-slate-700 p-2"></label>
                <label>最大持倉檔數<input id="setting-max-open-positions" type="number" min="1" max="20" step="1" class="setting-input w-full mt-1 rounded bg-slate-900 border border-slate-700 p-2"></label>
            </div>
            <h3 class="font-bold text-white text-sm mt-5 mb-2">五組影子策略的指標搭配</h3><div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">{experiment_config_html}</div>
            <div class="flex flex-wrap gap-2 mt-4"><button onclick="downloadStrategySettings()" class="px-4 py-2 rounded-lg bg-blue-600 text-white text-xs">下載 strategy_settings.json</button><button onclick="restoreActiveSettings()" class="px-4 py-2 rounded-lg bg-slate-700 text-white text-xs">還原目前線上設定</button><span id="settings-status" class="text-xs text-gray-400 self-center"></span></div>
        </details>

        <!-- 最新技術策略分析結果 -->
        <div class="panel border rounded-2xl p-5">
            <div class="flex flex-col md:flex-row md:items-center md:justify-between border-b border-white/5 pb-3 mb-4 gap-2">
                <div>
                    <h2 class="font-bold text-white text-base">即時多空訊號</h2>
                    <p class="text-xs text-gray-500 mt-0.5">8 種可設定策略並行判斷；預設至少 2 個獨立策略家族同向確認，並通過量能與淨賺賠比條件</p>
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
                            <th class="py-2.5 px-3">策略依據</th>
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
                            <th class="py-2 px-3">進場策略</th>
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
{CAPITAL_CARD_HTML}
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
        const ACTIVE_STRATEGY_SETTINGS = EXPORT_DATA.strategy_settings || {{}};

        function readStrategySettingsForm() {{
            const number = id => Number(document.getElementById(id).value);
            const enabled = {{}};
            Object.keys(ACTIVE_STRATEGY_SETTINGS.enabled_strategies || {{}}).forEach(key => {{ enabled[key] = document.getElementById(`strategy-enabled-${{key}}`).checked; }});
            const experiment_strategies = (ACTIVE_STRATEGY_SETTINGS.experiment_strategies || []).map(combo => ({{
                id: combo.id,
                name: document.getElementById(`experiment-config-name-${{combo.id}}`).value.trim(),
                indicators: Object.keys(ACTIVE_STRATEGY_SETTINGS.enabled_strategies || {{}}).filter(key => document.getElementById(`experiment-config-indicator-${{combo.id}}-${{key}}`).checked)
            }}));
            return {{ ...ACTIVE_STRATEGY_SETTINGS, enabled_strategies: enabled, experiment_strategies, min_votes: number('setting-min-votes'), min_vote_margin: number('setting-min-vote-margin'), volume_multiple: number('setting-volume-multiple'), min_bars: number('setting-min-bars'), stop_atr: number('setting-stop-atr'), target_atr: number('setting-target-atr'), max_open_positions: number('setting-max-open-positions') }};
        }}
        function setStrategySettings(settings) {{
            Object.entries(settings.enabled_strategies || {{}}).forEach(([key, value]) => {{ const el = document.getElementById(`strategy-enabled-${{key}}`); if (el) el.checked = !!value; }});
            [['min-votes','min_votes'],['min-vote-margin','min_vote_margin'],['volume-multiple','volume_multiple'],['min-bars','min_bars'],['stop-atr','stop_atr'],['target-atr','target_atr'],['max-open-positions','max_open_positions']].forEach(([id,key]) => {{ const el=document.getElementById(`setting-${{id}}`); if(el && settings[key] !== undefined) el.value=settings[key]; }});
            (settings.experiment_strategies || []).forEach(combo => {{
                const name = document.getElementById(`experiment-config-name-${{combo.id}}`);
                if (name) name.value = combo.name || '';
                Object.keys(settings.enabled_strategies || {{}}).forEach(key => {{ const el=document.getElementById(`experiment-config-indicator-${{combo.id}}-${{key}}`); if(el) el.checked=(combo.indicators || []).includes(key); }});
            }});
        }}
        function saveSettingsDraft() {{ try {{ localStorage.setItem('daytrade_strategy_settings_draft', JSON.stringify(readStrategySettingsForm())); document.getElementById('settings-status').textContent='本機草稿已儲存'; }} catch(e) {{}} }}
        function restoreActiveSettings() {{ setStrategySettings(ACTIVE_STRATEGY_SETTINGS); localStorage.removeItem('daytrade_strategy_settings_draft'); document.getElementById('settings-status').textContent='已還原本次頁面內的線上設定'; }}
        function downloadStrategySettings() {{ const data=readStrategySettingsForm(); const invalid=data.experiment_strategies.find(combo => combo.indicators.length < 2); if(invalid) {{ document.getElementById('settings-status').textContent=`${{invalid.name || invalid.id}} 至少要勾選 2 個指標模組`; return; }} const blob=new Blob([JSON.stringify(data,null,2)+'\\n'],{{type:'application/json'}}); const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='strategy_settings.json'; a.click(); URL.revokeObjectURL(a.href); saveSettingsDraft(); }}

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
            csv += `技術策略當沖雲端看板匯出報表\\n`;
            csv += `產生時間,${{csvCell(EXPORT_DATA.generated_at)}}\\n`;
            csv += `目前狀態,${{csvCell(EXPORT_DATA.status_text)}}\\n`;
            csv += `分析模式,${{csvCell(EXPORT_DATA.active_model)}}\\n`;
            csv += `累計訊號數,${{csvCell(EXPORT_DATA.total_signals)}}\\n\\n`;
            csv += csvSection("【波段一 {ANALYSIS_START_TIME} 選股】", EXPORT_DATA.wave1_stocks);
            csv += csvSection("【波段二 {MID_WAVE_TRIGGER_TIME} 選股】", EXPORT_DATA.wave2_stocks);
            csv += csvSection("【技術策略即時訊號 (每檔股票最新狀態)】", EXPORT_DATA.latest_analysis);
            csv += csvSection("【技術策略分析歷程】", EXPORT_DATA.analysis_log);
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
            setStrategySettings(ACTIVE_STRATEGY_SETTINGS);
            try {{ const draft=localStorage.getItem('daytrade_strategy_settings_draft'); if(draft) setStrategySettings(JSON.parse(draft)); }} catch(e) {{}}
            document.querySelectorAll('[id^="strategy-enabled-"], [id^="setting-"], [id^="experiment-config-"]').forEach(el => el.addEventListener('change', saveSettingsDraft));
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
            renderOpenPositionTable(EXPORT_DATA.open_positions || [], EXPORT_DATA.live_quotes || {{}});
            renderStrategyStats(EXPORT_DATA.strategy_trades || [], EXPORT_DATA.live_quotes || {{}});
            renderStrategyExperimentStats(EXPORT_DATA.strategy_experiments || {{}});
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
                renderOpenPositionTable(EXPORT_DATA.open_positions || [], EXPORT_DATA.live_quotes || {{}});
                renderStrategyStats(EXPORT_DATA.strategy_trades || [], EXPORT_DATA.live_quotes || {{}});
                renderStrategyExperimentStats(EXPORT_DATA.strategy_experiments || {{}});
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
                renderOpenPositionTable([], {{}});
                renderStrategyStats(snapshot.strategy_trades || [], snapshot.live_quotes || {{}});
                renderStrategyExperimentStats(snapshot.strategy_experiments || {{}});
                setSignalFilter(localStorage.getItem("daytrade_signal_filter") || "all");
            }} catch (e) {{
                errorMsg.classList.remove("hidden");
                document.getElementById("analysis-tbody").innerHTML =
                    '<tr><td colspan="7" class="py-6 text-center text-gray-500 text-xs">此日期尚無分析資料</td></tr>';
                document.getElementById("settle-tbody").innerHTML =
                    '<tr><td colspan="8" class="py-4 text-center text-gray-500 text-xs">此日期尚無結算資料</td></tr>';
            }} finally {{
                loadingMsg.classList.add("hidden");
            }}
        }}

        function escapeHtml(str) {{
            const div = document.createElement("div");
            div.textContent = str ?? "";
            return div.innerHTML;
        }}

        const STRATEGY_INDICATOR_LABELS = {{
            vwap_momentum: "VWAP 動能突破", ema_pullback: "EMA 趨勢回檔", rsi_reversal: "RSI 布林反轉",
            macd_volume: "MACD 量能確認", orb_breakout: "開盤區間突破", ema_momentum: "EMA 快慢線動能",
            stochastic_trend: "KD 順勢交叉", range_breakout: "區間高低突破"
        }};
        let CURRENT_STRATEGY_EXPERIMENTS = {{}};

        function renderOpenPositionTable(positions, quotes) {{
            const tbody = document.getElementById("open-position-tbody");
            if (!tbody) return;
            const count = document.getElementById("open-position-count");
            if (count) count.textContent = (positions || []).length;
            const rows = (positions || []).map(pos => {{
                const quote = (quotes || {{}})[pos.symbol] || {{}};
                const current = Number(quote.price);
                const entry = Number(pos.entry_price);
                const target = Number(pos.take_profit);
                const shares = Number(pos.shares || 0);
                const side = (pos.signal || "BUY").toUpperCase();
                const hasQuote = Number.isFinite(current) && current > 0;
                const pnl = hasQuote ? (current - entry) * (side === "SHORT" ? -1 : 1) * shares : null;
                const progress = hasQuote && Number.isFinite(target) && target !== entry ? (current - entry) / (target - entry) * 100 : null;
                const pnlClass = pnl > 0 ? "text-[#ff5470]" : pnl < 0 ? "text-[#00d68f]" : "text-gray-400";
                return `<tr><td class="py-2 px-3 whitespace-nowrap">${{escapeHtml(pos.symbol)}} ${{escapeHtml(pos.name || "")}}</td><td>${{escapeHtml(pos.direction || (side === "SHORT" ? "放空" : "做多"))}}</td><td>${{escapeHtml(pos.strategy_name || "-")}}</td><td class="mono">${{escapeHtml(pos.entry_time || "-")}}</td><td class="mono">${{Number.isFinite(entry) ? entry.toFixed(2) : "-"}}</td><td class="mono">${{hasQuote ? current.toFixed(2) : "-"}}</td><td class="mono ${{pnlClass}}">${{pnl === null ? "-" : (pnl > 0 ? "+" : "") + Math.round(pnl).toLocaleString()}}<span class="block text-[10px] text-gray-500">未扣費稅</span></td><td class="mono text-[#00d68f]">${{escapeHtml(pos.stop_loss ?? "-")}}</td><td class="mono text-[#ff5470]">${{escapeHtml(pos.take_profit ?? "-")}}</td><td class="mono">${{progress === null ? "-" : `${{Math.round(progress)}}%`}}</td></tr>`;
            }}).join("");
            tbody.innerHTML = rows || '<tr><td colspan="10" class="py-4 text-center text-gray-500">目前沒有持倉</td></tr>';
        }}

        function renderStrategyStats(trades, liveQuotes) {{
            const stats = {{}};
            (trades || []).forEach(t => {{
                const name = t.strategy_name || t.strategy || "未分類";
                const s = stats[name] || (stats[name] = {{ entries: 0, closed: 0, open: 0, wins: 0, pnl: 0, floating: 0 }});
                s.entries++;
                if (t.status === "closed") {{ s.closed++; s.wins += Number((t.pnl_amount || 0) > 0); s.pnl += Number(t.pnl_amount || 0); }}
                else if (t.status === "open") {{
                    s.open++;
                    const price = Number((liveQuotes || {{}})[t.symbol]?.price);
                    const entry = Number(t.entry_price);
                    if (Number.isFinite(price) && Number.isFinite(entry)) s.floating += (price - entry) * ((t.signal || "BUY").toUpperCase() === "SHORT" ? -1 : 1) * Number(t.shares || 0);
                }}
            }});
            const rows = Object.entries(stats).sort((a,b) => b[1].entries-a[1].entries || b[1].pnl-a[1].pnl).map(([name,s]) =>
                `<tr><td>${{escapeHtml(name)}}</td><td>${{s.entries}}</td><td>${{s.closed}}</td><td>${{s.open}}</td><td>${{s.closed ? (s.wins/s.closed*100).toFixed(1)+'%' : '—'}}</td><td class="mono">${{s.pnl > 0 ? '+' : ''}}${{Math.round(s.pnl).toLocaleString()}}</td><td class="mono">${{s.floating > 0 ? '+' : ''}}${{Math.round(s.floating).toLocaleString()}}</td></tr>`
            ).join("");
            document.getElementById("strategy-stat-tbody").innerHTML = rows || '<tr><td colspan="7" class="py-4 text-center text-gray-500">尚無策略交易</td></tr>';
        }}

        function renderStrategyExperimentStats(experiments) {{
            const tbody = document.getElementById("strategy-experiment-tbody");
            const select = document.getElementById("experiment-strategy-select");
            if (!tbody || !select) return;
            CURRENT_STRATEGY_EXPERIMENTS = experiments || {{}};
            const modes = experiments?.modes || {{}};
            const entries = Object.entries(modes);
            let preferred = select.value;
            try {{ preferred = preferred || localStorage.getItem("daytrade_selected_experiment"); }} catch (e) {{}}
            select.innerHTML = entries.map(([key, mode]) => `<option value="${{escapeHtml(key)}}">${{escapeHtml(mode.name || key)}}</option>`).join("") || '<option value="">尚無策略資料</option>';
            select.value = entries.some(([key]) => key === preferred) ? preferred : (entries[0]?.[0] || "");
            select.onchange = () => {{
                try {{ localStorage.setItem("daytrade_selected_experiment", select.value); }} catch (e) {{}}
                tbody.querySelectorAll("[data-experiment-select]").forEach(row => row.classList.toggle("bg-white/5", row.dataset.experimentSelect === select.value));
                renderStrategyExperimentDetail(select.value);
            }};
            tbody.onclick = event => {{
                const row = event.target.closest("[data-experiment-select]");
                if (!row) return;
                select.value = row.dataset.experimentSelect;
                select.dispatchEvent(new Event("change"));
            }};
            const rows = entries.map(([key, mode]) => {{
                const trades = mode.trades || [];
                const closed = trades.filter(t => t.status === "closed");
                const wins = closed.filter(t => Number(t.pnl_amount || 0) > 0).length;
                const pnl = closed.reduce((sum, t) => sum + Number(t.pnl_amount || 0), 0);
                const floating = (mode.open_positions || []).reduce((sum, pos) => {{
                    const price = Number(CURRENT_LIVE_QUOTES?.[pos.symbol]?.price), entry = Number(pos.entry_price);
                    if (!Number.isFinite(price) || !Number.isFinite(entry)) return sum;
                    return sum + (price-entry) * ((pos.signal || "BUY").toUpperCase() === "SHORT" ? -1 : 1) * Number(pos.shares || 0);
                }}, 0);
                const winRate = closed.length ? `${{(wins / closed.length * 100).toFixed(1)}}%` : "-";
                const pnlClass = pnl > 0 ? "text-[#ff5470]" : (pnl < 0 ? "text-[#00d68f]" : "text-gray-300");
                const floatingClass = floating > 0 ? "text-[#ff5470]" : (floating < 0 ? "text-[#00d68f]" : "text-gray-300");
                const indicators = mode.indicators || Object.keys(mode.settings?.enabled_strategies || {{}}).filter(k => mode.settings.enabled_strategies[k]);
                const indicatorNames = indicators.map(k => STRATEGY_INDICATOR_LABELS[k] || k).join(" + ") || "未設定（不會進場）";
                return `<tr data-experiment-select="${{escapeHtml(key)}}" class="cursor-pointer hover:bg-white/5 ${{select.value === key ? "bg-white/5" : ""}}"><td class="py-2 px-3 whitespace-nowrap">${{escapeHtml(mode.name || key)}}</td><td class="max-w-64">${{escapeHtml(indicatorNames)}}</td><td>${{trades.length}}</td><td>${{closed.length}}</td><td>${{winRate}}</td><td class="mono ${{pnlClass}}">${{pnl > 0 ? "+" : ""}}${{Math.round(pnl).toLocaleString()}}</td><td class="mono ${{floatingClass}}">${{floating > 0 ? "+" : ""}}${{Math.round(floating).toLocaleString()}}</td><td>${{(mode.open_positions || []).length}}</td></tr>`;
            }}).join("");
            tbody.innerHTML = rows || '<tr><td colspan="8" class="py-2 text-center text-gray-500">尚無平行策略紀錄</td></tr>';
            renderStrategyExperimentDetail(select.value);
        }}

        function downloadStrategyExperimentLogs() {{
            const data = CURRENT_STRATEGY_EXPERIMENTS || {{}};
            const date = data.date || EXPORT_DATA.today_str || "unknown-date";
            triggerDownload(JSON.stringify(data, null, 2) + "\\n", `strategy_experiments_${{date}}.json`, "application/json;charset=utf-8");
        }}

        function renderStrategyExperimentDetail(modeKey) {{
            const mode = CURRENT_STRATEGY_EXPERIMENTS?.modes?.[modeKey];
            const meta = document.getElementById("experiment-selected-meta");
            const metrics = document.getElementById("experiment-selected-metrics");
            const openBody = document.getElementById("experiment-open-tbody");
            const tradesBody = document.getElementById("experiment-trades-tbody");
            const logBody = document.getElementById("experiment-log-tbody");
            if (!meta || !metrics || !openBody || !tradesBody || !logBody) return;
            if (!mode) {{
                meta.textContent = "尚無策略資料。策略設定套用後，盤中開始累積影子分析。";
                metrics.innerHTML = "";
                openBody.innerHTML = '<tr><td colspan="6" class="py-2 text-center text-gray-500">尚無持倉</td></tr>';
                tradesBody.innerHTML = '<tr><td colspan="5" class="py-2 text-center text-gray-500">尚無交易</td></tr>';
                logBody.innerHTML = '<tr><td colspan="4" class="py-2 text-center text-gray-500">尚無分析日誌</td></tr>';
                return;
            }}
            const trades = mode.trades || [];
            const closed = trades.filter(t => t.status === "closed");
            const wins = closed.filter(t => Number(t.pnl_amount || 0) > 0).length;
            const netPnl = closed.reduce((sum, t) => sum + Number(t.pnl_amount || 0), 0);
            const openPositions = mode.open_positions || [];
            const quoteMap = CURRENT_LIVE_QUOTES || {{}};
            const floating = openPositions.reduce((sum, pos) => {{
                const price = Number(quoteMap[pos.symbol]?.price), entry = Number(pos.entry_price);
                if (!Number.isFinite(price) || !Number.isFinite(entry)) return sum;
                return sum + (price-entry) * ((pos.signal || "BUY").toUpperCase() === "SHORT" ? -1 : 1) * Number(pos.shares || 0);
            }}, 0);
            const indicators = mode.indicators || Object.keys(mode.settings?.enabled_strategies || {{}}).filter(k => mode.settings.enabled_strategies[k]);
            const indicatorNames = indicators.map(k => STRATEGY_INDICATOR_LABELS[k] || k).join(" + ") || "未設定（至少勾選兩項）";
            meta.innerHTML = `<span class="text-white font-semibold">${{escapeHtml(mode.name || modeKey)}}</span><span class="text-gray-500"> · 組合條件：${{escapeHtml(indicatorNames)}}（全數同方向成立）</span>`;
            const metricCards = [
                ["進場筆數", trades.length], ["已平倉", closed.length],
                ["勝率", closed.length ? `${{(wins/closed.length*100).toFixed(1)}}%` : "—"],
                ["已實現淨損益", `${{netPnl > 0 ? "+" : ""}}${{Math.round(netPnl).toLocaleString()}}`],
                ["持倉／浮動毛損益", `${{openPositions.length}} 檔 · ${{floating > 0 ? "+" : ""}}${{Math.round(floating).toLocaleString()}}`]
            ];
            metrics.innerHTML = metricCards.map(([label, value]) => `<div class="rounded-lg border border-white/10 bg-black/20 p-2"><div class="text-[10px] text-gray-500">${{escapeHtml(label)}}</div><div class="text-sm font-semibold text-white mt-1">${{escapeHtml(value)}}</div></div>`).join("");
            const openRows = openPositions.map(pos => {{
                const quote = quoteMap[pos.symbol] || {{}};
                const price = Number(quote.price), entry = Number(pos.entry_price);
                const hasPrice = Number.isFinite(price);
                const pnl = hasPrice ? (price-entry) * ((pos.signal || "BUY").toUpperCase() === "SHORT" ? -1 : 1) * Number(pos.shares || 0) : null;
                const pnlClass = pnl > 0 ? "text-[#ff5470]" : pnl < 0 ? "text-[#00d68f]" : "text-gray-400";
                return `<tr><td class="py-2 px-2 whitespace-nowrap">${{escapeHtml(pos.symbol)}} ${{escapeHtml(pos.name || "")}}</td><td>${{escapeHtml(pos.direction || "-")}}</td><td class="mono">${{escapeHtml(pos.entry_price ?? "-")}}</td><td class="mono">${{hasPrice ? price.toFixed(2) : "-"}}</td><td class="mono"><span class="text-[#00d68f]">${{escapeHtml(pos.stop_loss ?? "-")}}</span> / <span class="text-[#ff5470]">${{escapeHtml(pos.take_profit ?? "-")}}</span></td><td class="mono ${{pnlClass}}">${{pnl === null ? "-" : (pnl > 0 ? "+" : "") + Math.round(pnl).toLocaleString()}}</td></tr>`;
            }}).join("");
            openBody.innerHTML = openRows || '<tr><td colspan="6" class="py-2 text-center text-gray-500">尚無持倉</td></tr>';
            const tradeRows = trades.slice().reverse().slice(0, 25).map(t => {{
                const status = t.status === "open" ? "持倉中" : (t.result === "win" ? "獲利" : t.result === "loss" ? "虧損" : "已平倉");
                const pnl = Number(t.pnl_amount || 0);
                const pnlClass = pnl > 0 ? "text-[#ff5470]" : pnl < 0 ? "text-[#00d68f]" : "text-gray-400";
                return `<tr><td class="py-2 px-2 whitespace-nowrap">${{escapeHtml(t.symbol)}} ${{escapeHtml(t.name || "")}}</td><td>${{escapeHtml(t.direction || "-")}}</td><td class="mono">${{escapeHtml(t.entry_time || "-")}}${{t.exit_time ? ` / ${{escapeHtml(t.exit_time)}}` : ""}}</td><td>${{status}}</td><td class="mono ${{pnlClass}}">${{t.status === "closed" ? (pnl > 0 ? "+" : "") + Math.round(pnl).toLocaleString() : "-"}}</td></tr>`;
            }}).join("");
            tradesBody.innerHTML = tradeRows || '<tr><td colspan="5" class="py-2 text-center text-gray-500">尚無交易</td></tr>';
            const logs = (mode.analysis_log || []).slice(-12).reverse().map(log => {{
                const signal = log.signal === "BUY" ? "做多" : log.signal === "SHORT" ? "放空" : "觀望";
                const outcome = log.entered === true ? "已進場" : log.entry_candidate ? `未進場：${{log.entry_block_reason || "資金/持倉限制"}}` : (log.reason || "未觸發全部條件");
                return `<tr><td class="py-2 px-2 mono whitespace-nowrap">${{escapeHtml(log.time || "-")}}</td><td class="whitespace-nowrap">${{escapeHtml(log.symbol || "")}} ${{escapeHtml(log.name || "")}}</td><td>${{signal}}</td><td class="text-gray-400 min-w-48">${{escapeHtml(outcome)}}</td></tr>`;
            }}).join("");
            logBody.innerHTML = logs || '<tr><td colspan="4" class="py-2 text-center text-gray-500">尚無分析日誌</td></tr>';
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
                ? '<tr><td colspan="7" class="py-6 text-center text-gray-500 text-xs">盤中依排程自動更新技術策略看板...</td></tr>'
                : '<tr><td colspan="7" class="py-6 text-center text-gray-500 text-xs">此日期尚無分析資料</td></tr>';
            const emptyCardHtml = isToday
                ? '<div class="text-center text-gray-500 text-xs py-6">盤中依排程自動更新技術策略看板...</div>'
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
                tbody.innerHTML = `<tr><td colspan="8" class="py-4 text-center text-gray-500 text-xs">${{emptyMsg}}</td></tr>`;
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
                const stockName = escapeHtml(r.name || "");
                const symbolLabel = stockName ? `${{symbol}} <span class="text-gray-400 font-normal">${{stockName}}</span>` : symbol;
                const direction = escapeHtml(r.direction || "-");
                const strategy = escapeHtml(r.strategy || r.strategy_name || "-");
                const entryPrice = escapeHtml(r.entry_price ?? "-");
                const exitPrice = escapeHtml(r.exit_price ?? "-");
                // 【bug修復】同上，exit_reason 原本直接顯示 hit_sl 這種英文代碼，改為中文。
                const exitReason = escapeHtml(EXIT_REASON_LABELS[r.exit_reason] || r.exit_reason || "-");

                rowsHtml += `
                <tr class="hover:bg-white/[0.02]">
                    <td class="py-2 px-3 font-bold text-white whitespace-nowrap">${{symbolLabel}}</td>
                    <td class="py-2 px-3 whitespace-nowrap">${{direction}}</td>
                    <td class="py-2 px-3 text-[#8db3ff]">${{strategy}}</td>
                    <td class="py-2 px-3 mono whitespace-nowrap">${{entryPrice}}</td>
                    <td class="py-2 px-3 mono whitespace-nowrap">${{exitPrice}}</td>
                    <td class="py-2 px-3">${{badge}}</td>
                    <td class="py-2 px-3 mono font-semibold ${{resultClass}} whitespace-nowrap">${{pnlDisplay}}</td>
                    <td class="py-2 px-3 text-gray-400 text-xs">${{exitReason}}</td>
                </tr>`;

                cardsHtml += `
                <div class="data-card">
                    <div class="flex items-center justify-between mb-2">
                        <span class="font-bold text-white text-sm">${{symbolLabel}}　<span class="text-gray-400 font-normal text-xs">${{direction}}</span></span>
                        ${{badge}}
                    </div>
                    <div class="data-row"><span class="dlabel">進場 → 出場</span><span class="dvalue mono">${{entryPrice}} → ${{exitPrice}}</span></div>
                    <div class="data-row"><span class="dlabel">進場策略</span><span class="dvalue">${{strategy}}</span></div>
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
<script>{CAPITAL_JS}</script>
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
        "last_analysis_minute_bucket": None,
        "open_positions": [],
        "strategy_trades": [],
        "strategy_experiments": new_strategy_experiment_state(today_str),
        "settled_today": False,  # 今日是否已完成 13:25 收盤結算，避免收盤後的非盤中測試模式覆蓋掉正式看板
        "symbol_rules": {},      # 個股交易限制快取（可否當沖、處置/注意股、漲跌停價），每檔每日只查一次
        "capital": None,         # 每日資金池：{initial, cash, locked, realized, unrealized, equity, curve:[...]}
        "wave1_fail_count": 0,   # 選股（Yahoo 爬蟲）連續失敗次數，用來決定何時改沿用上次標的
        "wave2_fail_count": 0,
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
                        allow_fetch=False,  # /intraday/candles 只回「今天」的K線，不能拿來結算昨天
                        render=False,
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
        if not isinstance(state.get("symbol_rules"), dict):
            state["symbol_rules"] = {}
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
    return te.append_log_compact(log, new_record)

def save_daily_history_snapshot(
    date_str: str,
    analysis_records: List[Dict],
    settle_records: List[Dict],
    analysis_log: List[Dict] = None,
    live_quotes: Dict = None,
    strategy_trades: List[Dict] = None,
    capital: Dict = None,
    strategy_experiments: Dict = None,
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
        "strategy_trades": strategy_trades or [],
        "strategy_experiments": strategy_experiments or {},
        "capital": capital,
        "saved_at": get_tw_now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    snapshot_path = f"history_records/analysis_{date_str}.json"
    try:
        with open(snapshot_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        print(f"✅ 已保存當日分析快照：{snapshot_path}")
    except Exception as e:
        print(f"⚠️ 寫入 {snapshot_path} 失敗: {e}")

    experiment_path = f"history_records/strategy_experiments_{date_str}.json"
    try:
        with open(experiment_path, "w", encoding="utf-8") as f:
            json.dump(strategy_experiments or {}, f, ensure_ascii=False, indent=2)
        print(f"✅ 已保存五組策略比較日誌：{experiment_path}")
    except Exception as e:
        print(f"⚠️ 寫入 {experiment_path} 失敗: {e}")

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

def _http_get_retry(url: str, headers: Dict, attempts: int = 3, timeout: int = 10):
    """GET 加簡單重試（1s、2s 退避）；Yahoo 偶發 5xx / 逾時時不要整輪選股就作廢。"""
    last_err = None
    for i in range(attempts):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(i + 1)
    raise last_err


def load_fallback_stocks(today_str: str, limit: int = 8) -> List[Dict]:
    """Yahoo 排行榜連續失敗時，沿用最近一個交易日快照裡的標的（價格僅供顯示，進場判斷一律用即時K線）。"""
    try:
        files = sorted(f for f in os.listdir("history_records")
                       if re.match(r"^analysis_\d{4}-\d{2}-\d{2}\.json$", f))
    except FileNotFoundError:
        return []
    for fname in reversed(files):
        if fname[len("analysis_"):-len(".json")] >= today_str:
            continue
        try:
            with open(os.path.join("history_records", fname), "r", encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, ValueError):
            continue
        quotes = snap.get("live_quotes") or {}
        out = []
        for rec in snap.get("analysis_records") or []:
            sym = str(rec.get("symbol", ""))
            if not sym or any(o["symbol"] == sym for o in out):
                continue
            out.append({"symbol": sym, "name": rec.get("name", sym),
                        "price": (quotes.get(sym) or {}).get("price") or rec.get("entry") or 0,
                        "volume": 0, "rank": str(len(out) + 1), "stale": True})
            if len(out) >= limit:
                break
        if out:
            print(f"ℹ️ 沿用 {fname} 的標的：{[o['symbol'] for o in out]}")
            return out
    return []


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
            try:
                resp = _http_get_retry(url, headers)
            except requests.RequestException as page_err:
                if candidates:
                    # 後面幾頁失敗不要丟掉已經抓到的候選池，用現有的繼續
                    print(f"[Yahoo排行] 第 {page} 頁請求失敗（{page_err}），改用已取得的 {len(candidates)} 檔候選")
                    break
                raise

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

    def _quote(sym):
        try:
            return fugle.get_intraday_quote(sym) or {}
        except Exception as e:  # 以例外物件回傳，交給下方統一處理
            return e

    with ThreadPoolExecutor(max_workers=4) as pool:
        quotes = list(pool.map(lambda st: _quote(st["symbol"]), stocks))

    for s, quote in zip(stocks, quotes):
        symbol = s["symbol"]
        if isinstance(quote, Exception):
            # 查詢失敗（例如 API 額度用盡、逾時）時保守起見不排除
            print(f"   ⚠️ [排除漲停股] {symbol} 查詢即時報價失敗，保留原判斷: {quote}")
            kept.append(s)
            continue
        prev_close = quote.get("previousClose")
        current_price = s.get("price")
        limits = _calc_limit_prices(prev_close) if prev_close else None
        if limits:
            limit_up, limit_down = limits
            if _check_at_limit(current_price, limit_up, limit_down) == "up":
                excluded.append(s)
                print(f"   🚫 [排除漲停股] {symbol} {s.get('name', '')} 現價 {current_price} 已達漲停 {limit_up}，不納入當沖標的")
                continue
        kept.append(s)

    if excluded:
        print(f"   ℹ️ [排除漲停股] 本輪共排除 {len(excluded)} 檔已漲停股票，剩餘 {len(kept)} 檔可用（目標 {limit} 檔）")

    return kept[:limit]

# ═════════════════════════════════════════════════════════════════
#  持久化 / 渲染（v22：先存檔、後渲染；渲染失敗只警告、不中斷）
# ═════════════════════════════════════════════════════════════════
CAPITAL_HISTORY_FILE = os.path.join("history_records", "capital_history.json")


def load_capital_history() -> List[Dict]:
    """每日資金摘要（每個交易日一筆），供「每日資金變化」圖使用。檔案不存在或損壞時回傳空清單。"""
    try:
        with open(CAPITAL_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        days = data.get("days", []) if isinstance(data, dict) else data
        return [d for d in days if isinstance(d, dict) and d.get("date")]
    except (OSError, ValueError):
        return []


def save_capital_history(summary: Dict) -> None:
    try:
        os.makedirs("history_records", exist_ok=True)
        days = te.upsert_day_summary(load_capital_history(), summary)
        with open(CAPITAL_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump({"days": days}, f, ensure_ascii=False, indent=2)
        print(f"✅ 每日資金摘要已更新：{CAPITAL_HISTORY_FILE}（共 {len(days)} 天）")
    except OSError as e:
        print(f"⚠️ 寫入每日資金摘要失敗: {e}")


def get_daily_capital(settings: Optional[Dict] = None) -> float:
    """每日本金：環境變數 DAILY_CAPITAL（repo variable）優先，其次 strategy_settings.json 的 daily_capital。"""
    raw = os.getenv("DAILY_CAPITAL")
    if raw not in (None, ""):
        try:
            return normalize_settings({"daily_capital": float(raw)})["daily_capital"]
        except (TypeError, ValueError):
            print(f"⚠️ DAILY_CAPITAL「{raw}」無法解析，改用 strategy_settings.json 的設定")
    return float((settings or load_strategy_settings())["daily_capital"])


def update_capital(state: Dict, settings: Optional[Dict], now_hms: str, cfg: Dict, live_quotes: Dict) -> Dict:
    """
    由 strategy_trades 推算最新資金狀態並加入資金曲線。
    本金在「當天第一次呼叫」時定下並存入 state，之後即使改了設定也不會讓當天曲線錯亂。
    """
    cap = state.get("capital") or {}
    initial = float(cap.get("initial") or get_daily_capital(settings))
    snap = te.capital_snapshot(initial, state.get("strategy_trades", []), live_quotes,
                               float(cfg.get("broker_discount", 1.0)), bool(cfg.get("is_day_trade_tax", True)))
    state["capital"] = te.append_capital_point(cap, now_hms, snap)
    return state["capital"]


def safe_render(**kwargs) -> bool:
    """渲染 index.html。任何例外都只印警告，絕不讓一次渲染失敗毀掉整輪結果。"""
    try:
        render_html_dashboard(**kwargs)
        return True
    except Exception as e:
        import traceback
        print(f"⚠️ 儀表板渲染失敗（已略過，狀態已先行存檔，不影響交易紀錄）: {e}")
        traceback.print_exc()
        update_github_summary(f"⚠️ 儀表板渲染失敗：`{e}`（交易狀態已保存）")
        return False


def persist(state: Dict, **render_kwargs) -> None:
    """統一的收尾：① 先存狀態 ② 再渲染（失敗不中斷）。"""
    save_dashboard_state(state)
    render_kwargs.setdefault("active_model", _StrategyDisplay.active_model)
    render_kwargs.setdefault("wave1_stocks", state.get("wave1_stocks"))
    render_kwargs.setdefault("wave2_stocks", state.get("wave2_stocks"))
    render_kwargs.setdefault("latest_analysis", state.get("latest_analysis_records"))
    render_kwargs.setdefault("analysis_log", state.get("analysis_log"))
    render_kwargs.setdefault("live_quotes", state.get("live_quotes"))
    render_kwargs.setdefault("total_signals", state.get("total_signals"))
    render_kwargs.setdefault("open_positions", state.get("open_positions"))
    render_kwargs.setdefault("strategy_trades", state.get("strategy_trades"))
    render_kwargs.setdefault("strategy_experiments", state.get("strategy_experiments"))
    render_kwargs.setdefault("capital", state.get("capital"))
    render_kwargs.setdefault("capital_history", load_capital_history())
    safe_render(**render_kwargs)


TRADE_CSV_COLUMNS = ["id", "symbol", "name", "direction", "strategy_name", "shares", "entry_time", "entry_price",
                     "stop_loss", "take_profit", "exit_time", "exit_price", "exit_reason", "pnl_gross",
                     "cost_amount", "pnl_amount", "result"]


def write_trades_csv(path: str, trades: List[Dict]) -> None:
    """單一份交易明細 CSV（取代舊的 backtest_ / strategy_trades_ 兩份互相覆蓋的檔案）。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    rows, cols = [], list(TRADE_CSV_COLUMNS)
    for t in trades:
        row = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v) for k, v in t.items()
               if k not in ("strategy_votes",)}
        rows.append(row)
        cols.extend(k for k in row if k not in cols)
    try:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"✅ 交易明細已輸出：{path}（{len(rows)} 筆）")
    except OSError as e:
        print(f"⚠️ 寫入 {path} 失敗: {e}")


def fetch_candles_parallel(fugle, symbols: List[str], workers: int = 4) -> Dict[str, Optional[List[Dict]]]:
    """並行抓取多檔 1 分 K（Fugle 客戶端內建全域節流，執行緒安全）。失敗的標的回傳 None。"""
    def one(sym):
        try:
            raw = fugle.get_intraday_candles(sym, force_refresh=True)
            if raw and "error" not in raw:
                return sym, raw.get("data", []) or []
            print(f"  [{sym}] 取得K線失敗: {(raw or {}).get('error', '無回應')}")
        except Exception as e:
            print(f"  [{sym}] 取得K線異常: {e}")
        return sym, None
    if not symbols:
        return {}
    with ThreadPoolExecutor(max_workers=min(workers, len(symbols))) as pool:
        return dict(pool.map(one, symbols))


# ═════════════════════════════════════════════════════════════════
#  收盤結算（v22：單一資料來源 strategy_trades；逐根回放；強平價即時重抓）
# ═════════════════════════════════════════════════════════════════
def run_settlement(state: Dict, today_str: str, gemini, fugle, wave1_stocks: List[Dict], wave2_stocks: List[Dict],
                   latest_analysis_records: List[Dict], analysis_log: List[Dict], live_quotes: Dict,
                   total_signals: int, cfg: Optional[Dict] = None,
                   allow_fetch: bool = True, render: bool = True,
                   strategy_settings: Optional[Dict] = None):
    """
    13:25 收盤結算。

    v22 修正（根因：結算清單曾經取自舊 AI 時代的 analysis_history.json，新策略流程沒人寫入，
    導致只要收盤時沒有未平倉部位，清單就是空的，畫面顯示「尚未結算」且損益卡永遠停在「尚未結算」）：
      1. 結算清單 = strategy_trades 中所有已平倉交易（停損 / 停利 / 強平全部都算）。
      2. 仍持倉的部位：先重抓最新 1 分 K，從進場後逐根回放確認是否其實已觸價，
         沒有才以 13:25 前最後一根 K 收盤價強平（不再使用上一輪可能已過期的 live_quotes）。
      3. 順序：輸出 CSV / 歷史快照 → 存狀態 → 最後才渲染（渲染失敗不影響前兩者）。
    allow_fetch=False：跨日搶救結算用（/intraday/candles 只能查「今天」，不可拿來結算昨天）。
    """
    cfg = cfg or {}
    discount = float(cfg.get("broker_discount", 1.0))
    day_tax = bool(cfg.get("is_day_trade_tax", True))
    strategy_trades = state.setdefault("strategy_trades", [])
    open_positions = state.setdefault("open_positions", [])
    experiments = ensure_strategy_experiment_state(state, today_str, strategy_settings or load_strategy_settings())
    legacy_modes = experiments.get("legacy_modes")
    if not isinstance(legacy_modes, dict):
        legacy_modes = {}
    all_experiment_modes = list(experiments["modes"].values()) + list(legacy_modes.values())
    print(f"\n🎯 開始收盤結算：未平倉 {len(open_positions)} 筆、今日進場 {len(strategy_trades)} 筆")

    # ① 抓最新K線：持倉標的 + 全部監控標的（順便把參考價更新為收盤價，供歷史損益試算）
    symbols = list(dict.fromkeys(
        [p["symbol"] for p in open_positions] +
        [p["symbol"] for mode in all_experiment_modes for p in mode.get("open_positions", [])] +
        [str(s.get("symbol")) for s in (wave2_stocks or []) + (wave1_stocks or [])]))
    candles_map = fetch_candles_parallel(fugle, symbols) if allow_fetch else {}
    for sym, raw in candles_map.items():
        bars = te.normalize_candles(raw)
        if bars:
            live_quotes[sym] = {"price": bars[-1]["close"], "updated_at": "13:30:00"}

    # ② 未平倉部位：回放 → 強平
    for pos in list(open_positions):
        sym = pos["symbol"]
        bars = te.normalize_candles(candles_map.get(sym))
        closed = None
        if bars:
            hit = te.scan_exit(pos, bars, last_hm=HISTORY_SETTLE_TIME)
            if hit:
                closed = te.close_position(pos, hit["price"], hit["time"], hit["reason"], discount, day_tax, "candles")
            else:
                last = te.last_close_until(bars, HISTORY_SETTLE_TIME)
                if last:
                    closed = te.close_position(pos, last[0], f"{HISTORY_SETTLE_TIME}:00", "forced_close",
                                               discount, day_tax, f"bar_{last[1]}_close")
        if closed is None:
            price = float((live_quotes.get(sym) or {}).get("price") or pos["entry_price"])
            print(f"  ⚠️ [{sym}] 無法取得最新K線，強平價沿用最近一次報價 {price}")
            closed = te.close_position(pos, price, f"{HISTORY_SETTLE_TIME}:00", "forced_close",
                                       discount, day_tax, "stale_quote")
        for i in range(len(strategy_trades) - 1, -1, -1):
            if strategy_trades[i].get("id") == pos.get("id"):
                strategy_trades[i] = closed
                break
        else:
            strategy_trades.append(closed)
        print(f"  ⏱ [{sym}] {closed['exit_reason']} @ {closed['exit_price']} 淨損益 {closed['pnl_amount']:+,.0f}")
    state["open_positions"] = []

    # 五組影子帳本各自依收盤前 K 線平倉，績效不混入正式策略交易。
    for mode in all_experiment_modes:
        trades = mode.setdefault("trades", [])
        for pos in list(mode.get("open_positions") or []):
            bars = te.normalize_candles(candles_map.get(pos["symbol"]))
            closed = None
            if bars:
                hit = te.scan_exit(pos, bars, last_hm=HISTORY_SETTLE_TIME)
                if hit:
                    closed = te.close_position(pos, hit["price"], hit["time"], hit["reason"], discount, day_tax, "candles")
                else:
                    last = te.last_close_until(bars, HISTORY_SETTLE_TIME)
                    if last:
                        closed = te.close_position(pos, last[0], f"{HISTORY_SETTLE_TIME}:00", "forced_close",
                                                   discount, day_tax, f"bar_{last[1]}_close")
            if closed is None:
                price = float((live_quotes.get(pos["symbol"]) or {}).get("price") or pos["entry_price"])
                closed = te.close_position(pos, price, f"{HISTORY_SETTLE_TIME}:00", "forced_close",
                                           discount, day_tax, "stale_quote")
            for i in range(len(trades) - 1, -1, -1):
                if trades[i].get("id") == pos.get("id"):
                    trades[i] = closed
                    break
            else:
                trades.append(closed)
        mode["open_positions"] = []
        mode["summary"] = te.summarize_trades(trades)

    # ③ 結算清單 = 所有已平倉交易；資金：全部平倉後「本金＋當日淨損益」回到資金池
    settled_list = te.build_settle_records(strategy_trades)
    cap = update_capital(state, None, f"{HISTORY_SETTLE_TIME}:00", cfg, live_quotes)
    print(f"💰 資金結算：本金 {cap['initial']:,.0f} → 收盤 {cap['equity']:,.0f}"
          f"（{cap['equity'] - cap['initial']:+,.0f}）")
    summary = te.summarize_trades(strategy_trades)
    wr = f"{summary['win_rate'] * 100:.1f}%" if summary["win_rate"] is not None else "-"
    print(f"📊 今日 {summary['entries']} 筆進場 / {summary['closed']} 筆平倉 / 勝率 {wr} / "
          f"成本 {summary['cost']:,.0f} / 淨損益 {summary['net_pnl']:+,.0f}")
    update_github_summary(f"### 📊 {today_str} 收盤結算\n進場 {summary['entries']} 筆、勝率 {wr}、"
                          f"成本 {summary['cost']:,.0f}、**淨損益 {summary['net_pnl']:+,.0f}**")

    # ④ 輸出（CSV＋歷史快照）→ 存狀態 → 渲染
    write_trades_csv(f"history_records/backtest_{today_str}.csv", [t for t in strategy_trades if t.get("status") == "closed"])
    save_daily_history_snapshot(today_str, latest_analysis_records, settled_list, analysis_log, live_quotes,
                                strategy_trades, capital=cap, strategy_experiments=experiments)
    save_capital_history(te.capital_day_summary(today_str, cap, strategy_trades))

    state["settled_today"] = True
    state["live_quotes"] = live_quotes
    save_dashboard_state(state)

    if render:
        safe_render(
            status_text="已收盤結算完成", active_model=gemini.active_model,
            wave1_stocks=wave1_stocks, wave2_stocks=wave2_stocks, latest_analysis=latest_analysis_records,
            analysis_log=analysis_log, live_quotes=live_quotes, settle_records=settled_list,
            total_signals=total_signals, open_positions=[], strategy_trades=strategy_trades, settled=True,
            capital=cap, capital_history=load_capital_history(), strategy_experiments=experiments,
        )
    print("✅ 本輪次（收盤結算）執行完畢。")


# ═════════════════════════════════════════════════════════════════
#  進場前的所有檢查
# ═════════════════════════════════════════════════════════════════
def get_symbol_rules(state: Dict, fugle, symbol: str) -> Dict:
    """個股交易限制（每檔每日只查一次，存在 state 供下一輪沿用）。查詢失敗回傳空 dict＝未知＝放行。"""
    cache = state.setdefault("symbol_rules", {})
    if symbol in cache:
        return cache[symbol]
    try:
        ticker = fugle.get_intraday_ticker(symbol)
        quote = None
        if isinstance(ticker, dict) and "error" not in ticker:
            if not (ticker.get("previousClose") or ticker.get("referencePrice")):
                quote = fugle.get_intraday_quote(symbol)
            rules = extract_symbol_rules(ticker, quote)
            cache[symbol] = rules
            return rules
    except Exception as e:
        print(f"  [{symbol}] 取得個股交易限制失敗（視為未知、放行）: {e}")
    return {}


def try_open_position(state: Dict, fugle, symbol: str, name: str, res: Dict, completed: List[Dict],
                      now: datetime.datetime, settings: Dict, discount: float, day_tax: bool,
                      today_str: str, experiment_id: str = "", rule_state: Dict = None) -> (Optional[Dict], str, bool):
    """
    依序檢查：時間 → 持倉 → 放空許可 → 每日次數/冷卻 → 個股限制 → 部位大小 → 成本淨賺賠比。
    回傳 (部位 or None, 未進場原因, 是否值得在畫面上提示)。
    """
    sig = res["signal"]
    open_positions = state["open_positions"]
    hm = now.strftime("%H:%M")
    if hm >= ANALYSIS_STOP_TIME:
        return None, f"{ANALYSIS_STOP_TIME} 後不再進場", False
    if any(p.get("symbol") == symbol for p in open_positions):
        return None, "已有持倉", False
    if sig == "SHORT" and not settings["allow_short"]:
        return None, "設定禁止放空", True
    trade_ledger = state.get("strategy_trades", state.get("trades", []))
    ok, why = te.can_enter(symbol, trade_ledger, now.strftime("%H:%M:%S"), settings)
    if not ok:
        return None, why, True

    entry_p = float(res.get("price") or completed[-1]["close"])
    rules = get_symbol_rules(rule_state if rule_state is not None else state, fugle, symbol)
    ok, why = check_entry_allowed(sig, entry_p, rules, settings["skip_attention"])
    if not ok:
        return None, why, True

    stop_p, target_p = position_levels(sig, entry_p, float(res.get("atr") or 0), settings)
    shares, why = te.calc_shares(sig, entry_p, stop_p, settings, discount, day_tax)
    if shares <= 0:
        return None, why, True
    metrics = te.evaluate_net_rr(sig, entry_p, stop_p, target_p, shares, discount, day_tax)
    ok, why = te.check_cost_gate(metrics, settings)
    if not ok:
        return None, why, True

    trade_id = f"{today_str}_{experiment_id}_{symbol}_{now.strftime('%H%M%S')}_{len(trade_ledger) + 1}" if experiment_id else f"{today_str}_{symbol}_{now.strftime('%H%M%S')}"
    strategy_label = (res.get("strategy_name") if experiment_id else " + ".join(res.get("strategy_matches") or [])) or "多策略共識"
    position = {
        "id": trade_id, "symbol": symbol, "name": name, "signal": sig,
        "direction": "做多" if sig == "BUY" else "放空",
        "strategy": res.get("strategy", ""), "strategy_name": strategy_label,
        "strategy_votes": res.get("strategy_votes", []), "entry_price": round(entry_p, 2),
        "stop_loss": stop_p, "take_profit": target_p, "shares": shares,
        "entry_time": now.strftime("%H:%M:%S"), "entry_bar_hm": te.bar_hm(completed[-1]),
        "status": "open", "reason": f"{res.get('strategy_name', '多策略共識')}：{res.get('reason', '')}",
        "expected_net_win": metrics["net_win"], "expected_net_loss": metrics["net_loss"],
        "expected_cost": metrics["cost"], "net_rr": metrics["net_rr"],
    }
    return position, "", False


# ═════════════════════════════════════════════════════════════════
#  單輪執行
# ═════════════════════════════════════════════════════════════════
def _parse_bucket(last_bucket: Optional[str]) -> Optional[datetime.datetime]:
    if not last_bucket:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return TW_TZ.localize(datetime.datetime.strptime(last_bucket, fmt))
        except ValueError:
            continue
    print(f"⚠️ 解析上次分析時間戳記「{last_bucket}」失敗，視為尚未分析過")
    return None


def build_context() -> Dict:
    """讀取設定、建立 Fugle 連線。迴圈模式只需建立一次。"""
    cfg = load_config()
    fugle_api_key = os.getenv("FUGLE_API_KEY") or cfg.get("fugle_api_key", "")
    if not fugle_api_key:
        print("❌ 錯誤：未設定 FUGLE_API_KEY 環境變數！")
        sys.exit(1)

    valid_modes = set(RISK_MODE_PRESETS)
    risk_mode = (os.getenv("RISK_MODE") or cfg.get("risk_mode") or "auto").strip().lower()
    if risk_mode not in valid_modes:
        print(f"⚠️ 未知的 RISK_MODE「{risk_mode}」，已回退為 auto")
        risk_mode = "auto"

    raw = os.getenv("BROKER_DISCOUNT")
    try:
        discount = float(raw) if raw not in (None, "") else float(cfg.get("broker_discount", 1.0))
    except (TypeError, ValueError):
        print(f"⚠️ BROKER_DISCOUNT「{raw}」無法解析，改用預設值")
        discount = float(cfg.get("broker_discount", 1.0))
    discount = max(0.1, min(1.0, discount))
    cfg["broker_discount"] = discount
    raw_tax = os.getenv("IS_DAY_TRADE_TAX")
    if raw_tax not in (None, ""):
        cfg["is_day_trade_tax"] = raw_tax.strip().lower() in ("1", "true", "yes", "on")
    else:
        cfg["is_day_trade_tax"] = bool(cfg.get("is_day_trade_tax", True))

    return {
        "cfg": cfg, "fugle": FugleService(api_key=fugle_api_key), "gemini": _StrategyDisplay(),
        "risk_mode": risk_mode, "discount": discount, "day_tax": cfg["is_day_trade_tax"],
        "force": os.getenv("FORCE_RUN", "").strip().lower() in ("1", "true", "yes", "on"),
    }


def run_once(ctx: Dict) -> str:
    """
    執行一輪。回傳狀態字串供迴圈模式判斷：
      "ok" 正常跑完一輪 / "idle" 目前不需要做事 / "done" 今日已結算（可結束）
    """
    cfg, fugle, gemini = ctx["cfg"], ctx["fugle"], ctx["gemini"]
    discount, day_tax = ctx["discount"], ctx["day_tax"]
    settings = apply_risk_mode(load_strategy_settings(), ctx["risk_mode"])

    now = get_tw_now()
    hm = now.strftime("%H:%M")
    today_str = now.strftime("%Y-%m-%d")
    trading_day, day_why = market_calendar.is_trading_day(now.date())
    print("=" * 65)
    print(f"🕒 {now.strftime('%Y-%m-%d %H:%M:%S')}（{day_why}）｜風險模式 {ctx['risk_mode']}｜"
          f"最少 {settings['min_votes']} 個獨立家族同向｜手續費折扣 {discount:.2f}")

    is_market_session = ("08:50" <= hm <= "13:30") and trading_day

    # ── 非盤中 ────────────────────────────────────────────────
    if not is_market_session:
        if not trading_day and not ctx["force"]:
            print(f"ℹ️ 今日非交易日（{day_why}），不執行、不覆蓋 index.html。")
            return "idle"
        existing = load_dashboard_state(today_str, gemini, fugle, cfg)
        if existing.get("settled_today"):
            print(f"ℹ️ 今日 ({today_str}) 已完成收盤結算，直接結束。")
            return "done"
        # 補跑：13:25~13:30 視窗沒被觸發到時，盤後任何一輪都補做結算
        if trading_day and existing.get("wave1_stocks") and hm >= HISTORY_SETTLE_TIME:
            print(f"⚠️ 今日盤中流程已跑過但尚未結算，現在 {hm} 已過收盤，立即補跑結算。")
            run_settlement(existing, today_str, gemini, fugle, existing.get("wave1_stocks", []),
                           existing.get("wave2_stocks", []), existing.get("latest_analysis_records", []),
                           existing.get("analysis_log", []), existing.get("live_quotes", {}),
                           existing.get("total_signals", 0), cfg, strategy_settings=settings)
            return "done"
        if not ctx["force"]:
            print("ℹ️ 目前不在交易時段，略過（不覆蓋 index.html）。手動測試請勾選 force_run。")
            return "idle"

        print("\n⚠️ force_run：進入【連線與即時看板測試模式】...")
        test_stocks = get_free_top_volume_stocks(limit=3)
        test_analysis = []
        for s in test_stocks:
            print(f"   📌 {s['symbol']} {s['name']} (參考價: {s['price']} 元, 成交量: {s.get('volume', 0):,} 張)")
        if test_stocks:
            test_analysis.append({"symbol": test_stocks[0]["symbol"], "name": test_stocks[0]["name"],
                                  "signal": "WATCH", "entry": test_stocks[0]["price"], "stop_loss": "-",
                                  "target": "-", "reason": "純技術指標策略已載入"})
        safe_render(status_text="非盤中連線測試（僅測3檔，平日盤中將完整執行8檔選股）",
                    active_model=gemini.active_model, wave1_stocks=test_stocks, latest_analysis=test_analysis,
                    analysis_log=existing.get("analysis_log", []), live_quotes=existing.get("live_quotes", {}))
        print("🎉 測試完成，index.html 已更新。")
        return "idle"

    # ── 盤中 ──────────────────────────────────────────────────
    state = load_dashboard_state(today_str, gemini, fugle, cfg)
    experiments = ensure_strategy_experiment_state(state, today_str, settings)
    if state.get("settled_today"):
        print(f"ℹ️ 今日 ({today_str}) 已完成收盤結算，不重複結算。")
        return "done"

    wave1_stocks, wave2_stocks = state["wave1_stocks"], state["wave2_stocks"]
    latest_analysis_records, analysis_log = state["latest_analysis_records"], state["analysis_log"]
    live_quotes = state["live_quotes"]
    current_stocks = wave2_stocks if wave2_stocks else wave1_stocks

    if hm < ANALYSIS_START_TIME:
        print(f"尚未到 {ANALYSIS_START_TIME} 開盤選股時間。")
        persist(state, status_text=f"盤前準備中 (等待 {ANALYSIS_START_TIME})")
        return "ok"

    # 收盤結算優先於任何選股（避免前面輪次缺失時，13:25 之後的第一輪還跑去重新選股）
    if hm >= HISTORY_SETTLE_TIME:
        run_settlement(state, today_str, gemini, fugle, wave1_stocks, wave2_stocks, latest_analysis_records,
                       analysis_log, live_quotes, state["total_signals"], cfg, strategy_settings=settings)
        return "done"

    # 第一波選股（13:00 後才補選已無意義，不再選）
    if not wave1_stocks and hm < ANALYSIS_STOP_TIME:
        print(f"\n⏰ 【第一波段：早盤動能成交量排行選股】")
        candidates = get_free_top_volume_stocks(limit=13)
        if candidates:
            wave1_stocks = filter_out_limit_up_stocks(candidates, fugle, limit=8)
        else:
            state["wave1_fail_count"] = int(state.get("wave1_fail_count", 0)) + 1
            if state["wave1_fail_count"] >= 3:
                wave1_stocks = load_fallback_stocks(today_str, 8)
            if not wave1_stocks:
                print(f"⚠️ 選股失敗（連續 {state['wave1_fail_count']} 次），下一輪重試。")
                persist(state, status_text=f"選股失敗，下一輪重試（第 {state['wave1_fail_count']} 次）")
                return "ok"
        state["wave1_stocks"] = wave1_stocks
        for s in wave1_stocks:
            print(f"   📌 {s['symbol']} {s['name']} (現價: {s['price']} 元, 成交量: {s.get('volume', 0):,} 張)"
                  f"{' [沿用上次標的]' if s.get('stale') else ''}")
        persist(state, status_text="早盤第一波監控中" + ("（沿用上次標的）" if wave1_stocks and wave1_stocks[0].get("stale") else ""),
                wave1_stocks=wave1_stocks)
        print("✅ 本輪次（選股）執行完畢。")
        return "ok"

    # 第二波重挑
    if MID_WAVE_TRIGGER_TIME <= hm < ANALYSIS_STOP_TIME and wave1_stocks and not state["mid_wave_triggered"]:
        print(f"\n⏰ 【第二波段：中盤換手與輪動股票重挑】")
        candidates = get_free_top_volume_stocks(limit=13)
        wave2_stocks = filter_out_limit_up_stocks(candidates, fugle, limit=8) if candidates else []
        if not wave2_stocks:
            state["wave2_fail_count"] = int(state.get("wave2_fail_count", 0)) + 1
            if state["wave2_fail_count"] < 3:
                print(f"⚠️ 中盤選股失敗（第 {state['wave2_fail_count']} 次），下一輪重試。")
                persist(state, status_text="中盤選股失敗，下一輪重試")
                return "ok"
            print("⚠️ 中盤選股連續失敗，沿用第一波標的。")
        state["wave2_stocks"] = wave2_stocks
        state["mid_wave_triggered"] = True
        persist(state, status_text="中盤第二波監控中", wave2_stocks=wave2_stocks)
        print("✅ 本輪次（中盤重挑）執行完畢。")
        return "ok"

    # 13:00 後且無持倉：不再分析
    legacy_modes = experiments.get("legacy_modes") if isinstance(experiments.get("legacy_modes"), dict) else {}
    has_experiment_positions = any(mode.get("open_positions") for mode in list(experiments["modes"].values()) + list(legacy_modes.values()))
    if hm >= ANALYSIS_STOP_TIME and not state.get("open_positions") and not has_experiment_positions:
        print(f"已過 {ANALYSIS_STOP_TIME}，停止新進場，等待 {HISTORY_SETTLE_TIME} 收盤結算。")
        persist(state, status_text=f"已停止新進場 (等待 {HISTORY_SETTLE_TIME} 收盤結算)")
        return "ok"

    # 同一分鐘內重複觸發的保護（60 秒）
    last_dt = _parse_bucket(state.get("last_analysis_minute_bucket"))
    if last_dt is not None and (now - last_dt).total_seconds() < 60:
        print("距上次分析不滿 60 秒，本輪僅同步看板。")
        persist(state, status_text=f"盤中監控中 ({hm})")
        return "ok"

    # ── 分析 + 進出場 ───────────────────────────────────────────
    print(f"\n⚡ [{now.strftime('%H:%M:%S')}] 執行技術策略分析...")
    open_positions = state.setdefault("open_positions", [])
    strategy_trades = state.setdefault("strategy_trades", [])
    seen = {str(s.get("symbol")) for s in current_stocks}
    monitored = list(current_stocks) + [{"symbol": p["symbol"], "name": p.get("name", p["symbol"])}
                                        for p in open_positions if str(p.get("symbol")) not in seen]
    seen.update(str(p.get("symbol")) for p in open_positions)
    for mode in experiments["modes"].values():
        monitored.extend({"symbol": p["symbol"], "name": p.get("name", p["symbol"])}
                         for p in mode.get("open_positions", []) if str(p.get("symbol")) not in seen)
        seen.update(str(p.get("symbol")) for p in mode.get("open_positions", []))
    for mode in legacy_modes.values():
        monitored.extend({"symbol": p["symbol"], "name": p.get("name", p["symbol"])}
                         for p in mode.get("open_positions", []) if str(p.get("symbol")) not in seen)
        seen.update(str(p.get("symbol")) for p in mode.get("open_positions", []))
    candles_map = fetch_candles_parallel(fugle, [str(s["symbol"]) for s in monitored])

    pending = []  # 本輪所有分析結果；進場候選先收集，掃完全部標的後依「可用資金」統一分配
    experiment_pending = {key: [] for key in experiments["modes"]}
    for s_info in monitored:
        symbol, name = str(s_info["symbol"]), s_info.get("name", s_info["symbol"])
        try:
            bars = te.normalize_candles(candles_map.get(symbol))
            if len(bars) < 5:
                continue
            live_quotes[symbol] = {"price": bars[-1]["close"], "updated_at": now.strftime("%H:%M:%S")}

            # ① 先處理出場：掃描「進場後的每一根 K 棒」（含進行中的這根，其高低點是已發生的真實成交）
            #    出場即釋放資金（本金＋淨損益回到資金池），本輪後面的進場候選就能用到
            for pos in list(open_positions):
                if pos.get("symbol") != symbol:
                    continue
                hit = te.scan_exit(pos, bars, last_hm=HISTORY_SETTLE_TIME)
                if not hit:
                    continue
                closed = te.close_position(pos, hit["price"], hit["time"], hit["reason"], discount, day_tax, "candles")
                open_positions.remove(pos)
                for i in range(len(strategy_trades) - 1, -1, -1):
                    if strategy_trades[i].get("id") == pos.get("id"):
                        strategy_trades[i] = closed
                        break
                print(f"   {'✅' if hit['reason'] == 'hit_tp' else '🛑'} [持倉出場] {symbol} {hit['reason']} "
                      f"@ {closed['exit_price']}（{hit['time'][:5]}）淨損益 {closed['pnl_amount']:+,.0f}，"
                      f"回補資金 {closed.get('position_value') or te.position_value(closed['entry_price'], closed['shares']):,.0f}")

            # ② 訊號只用「已收完」的 K 棒（進行中的分K會重繪）
            completed = te.drop_incomplete_bar(bars, hm)

            # 舊版影子持倉只繼續監控出場，不再建立舊版新倉；換版當天的歷史帳本保留到收盤。
            for legacy_mode in legacy_modes.values():
                legacy_trades = legacy_mode.setdefault("trades", [])
                legacy_positions = legacy_mode.setdefault("open_positions", [])
                for pos in list(legacy_positions):
                    if pos.get("symbol") != symbol:
                        continue
                    hit = te.scan_exit(pos, bars, last_hm=HISTORY_SETTLE_TIME)
                    if not hit:
                        continue
                    closed = te.close_position(pos, hit["price"], hit["time"], hit["reason"], discount, day_tax, "candles")
                    legacy_positions.remove(pos)
                    for trade_index in range(len(legacy_trades) - 1, -1, -1):
                        if legacy_trades[trade_index].get("id") == pos.get("id"):
                            legacy_trades[trade_index] = closed
                            break
                    legacy_mode["summary"] = te.summarize_trades(legacy_trades)

            # 五種策略組合平行跑影子交易：每組有獨立持倉、交易帳本與設定，互不影響正式帳本。
            for mode_key, mode in experiments["modes"].items():
                mode_settings = normalize_settings(mode.get("settings"))
                mode_trades = mode.setdefault("trades", [])
                mode_positions = mode.setdefault("open_positions", [])
                for pos in list(mode_positions):
                    if pos.get("symbol") != symbol:
                        continue
                    hit = te.scan_exit(pos, bars, last_hm=HISTORY_SETTLE_TIME)
                    if not hit:
                        continue
                    closed = te.close_position(pos, hit["price"], hit["time"], hit["reason"],
                                               discount, day_tax, "candles")
                    mode_positions.remove(pos)
                    for trade_index in range(len(mode_trades) - 1, -1, -1):
                        if mode_trades[trade_index].get("id") == pos.get("id"):
                            mode_trades[trade_index] = closed
                            break

                mode_res = evaluate_strategies(completed, mode_settings)
                mode_signal = mode_res.get("signal", "WATCH")
                mode_entry = mode_res.get("price") or (completed[-1]["close"] if completed else bars[-1]["close"])
                mode_votes_by_side = mode_res.get("strategy_votes_by_side") or {"BUY": [], "SHORT": []}
                matched_votes = mode_res.get("strategy_votes") or []
                if mode_signal in {"BUY", "SHORT"}:
                    mode_res["strategy"] = mode_key
                    mode_res["strategy_name"] = mode.get("name", mode_key)
                    mode_res["reason"] = "；".join(v.get("reason", "") for v in matched_votes)
                required_indicators = list(mode.get("indicators") or [])
                risk_snapshot = {key: mode_settings.get(key) for key in (
                    "volume_multiple", "stop_atr", "target_atr", "min_net_rr", "min_target_cost_multiple",
                    "sizing_mode", "risk_per_trade", "position_amount", "fixed_shares", "max_position_value",
                )}
                fired_by_side = {
                    side: [v.get("key") for v in mode_votes_by_side.get(side, [])]
                    for side in ("BUY", "SHORT")
                }
                mode_reason = mode_res.get("reason", "")
                if mode_signal == "WATCH":
                    fired_labels = []
                    for side, side_label in (("BUY", "多方"), ("SHORT", "空方")):
                        votes = mode_votes_by_side.get(side, [])
                        if votes:
                            detail = "、".join(v.get("name", v.get("key", "")) for v in votes)
                            fired_labels.append(f"{side_label}觸發 {detail}")
                    mode_reason = ("；".join(fired_labels) + "；" if fired_labels else "") + (
                        f"需至少勾選兩項，且所選模組全數同方向成立：{ ' + '.join(STRATEGY_NAMES.get(key, key) for key in required_indicators) or '尚未設定' }"
                    )
                mode_record = {
                    "time": now.strftime("%H:%M:%S"), "symbol": symbol, "name": name,
                    "signal": mode_signal, "price": mode_entry,
                    "reason": mode_reason,
                    "strategy_votes": matched_votes,
                    "strategy_votes_by_side": mode_votes_by_side,
                    "required_indicators": required_indicators,
                    "fired_indicators_by_side": fired_by_side,
                    "settings_snapshot": risk_snapshot,
                    "vote_counts": mode_res.get("vote_counts", {}),
                }
                mode_candidate = None
                if mode_signal in {"BUY", "SHORT"}:
                    position, why, _show = try_open_position(
                        mode, fugle, symbol, name, mode_res, completed, now, mode_settings,
                        discount, day_tax, today_str, experiment_id=mode_key, rule_state=state,
                    )
                    if position:
                        position["experiment_mode"] = mode_key
                        position["experiment_name"] = mode.get("name")
                        position["experiment_config"] = {
                            "name": mode.get("name"), "indicators": required_indicators,
                            "settings": risk_snapshot,
                        }
                        mode_candidate = {"position": position,
                                          "votes": int((mode_res.get("vote_counts") or {}).get(mode_signal, 0))}
                        mode_record["entry_candidate"] = True
                    else:
                        mode_record["entry_candidate"] = True
                        mode_record["entry_block_reason"] = why
                mode.setdefault("analysis_log", []).append(mode_record)
                experiment_pending[mode_key].append({"record": mode_record, "candidate": mode_candidate})

            res = evaluate_strategies(completed, settings)
            sig = res["signal"]
            entry_p = res.get("price") or (completed[-1]["close"] if completed else bars[-1]["close"])
            stop_p, target_p = "-", "-"
            reason = f"{res.get('strategy_name', '多策略共識')}：{res.get('reason', '')}"
            cand = None
            if sig in {"BUY", "SHORT"}:
                stop_p, target_p = position_levels(sig, float(entry_p), float(res.get("atr") or 0), settings)
                position, why, show = try_open_position(state, fugle, symbol, name, res, completed, now,
                                                        settings, discount, day_tax, today_str)
                if position:
                    cand = {"position": position, "votes": int((res.get("vote_counts") or {}).get(sig, 0))}
                elif show:
                    reason += f"　⛔未進場：{why}"
                    print(f"   ⛔ [{symbol}] {sig} 未進場：{why}")

            print(f"  [{symbol} {name}] 訊號: {sig} | 進場: {entry_p} | 停損: {stop_p} | 停利: {target_p}")
            record = {"symbol": symbol, "name": name, "signal": sig, "entry": entry_p, "stop_loss": stop_p,
                      "target": target_p, "reason": reason, "strategy": res.get("strategy_name", "多策略共識"),
                      "updated_at": now.strftime("%H:%M:%S")}
            pending.append({"record": record, "cand": cand})
        except Exception as ex:
            import traceback
            print(f"  [{symbol}] 分析異常: {ex}")
            traceback.print_exc()

    # ③ 資金分配：同一輪若有多檔訊號，依「獨立票數 → 淨賺賠比」排序，資金用完為止；
    #    資金不足一張的縮減張數，買不起就略過（原因會寫在該檔的分析理由裡）
    initial_cap = float((state.get("capital") or {}).get("initial") or get_daily_capital(settings))
    snap = te.capital_snapshot(initial_cap, strategy_trades, live_quotes, discount, day_tax)
    cands = [p["cand"] for p in pending if p["cand"]]
    if cands:
        print(f"   💰 可用資金 {snap['cash']:,.0f} / 本金 {snap['initial']:,.0f}，本輪進場候選 {len(cands)} 檔")
        allocs = {id(a["candidate"]): a for a in te.allocate_candidates(
            cands, snap["cash"], len(open_positions), settings["max_open_positions"], discount, day_tax, settings)}
        for p in pending:
            if not p["cand"]:
                continue
            a = allocs[id(p["cand"])]
            pos = a["position"]
            if pos:
                open_positions.append(pos)
                strategy_trades.append(pos.copy())
                state["total_signals"] = int(state.get("total_signals", 0)) + 1
                p["record"]["reason"] += f"　💰 進場 {pos['shares']:,} 股，佔用資金 {pos['position_value']:,.0f} 元"
                print(f"   👉 [進場] {pos['symbol']} {pos['signal']} {pos['shares']}股 @ {pos['entry_price']} "
                      f"SL {pos['stop_loss']} / TP {pos['take_profit']}｜佔用 {pos['position_value']:,.0f}｜"
                      f"預期淨賺 {pos['expected_net_win']:+,.0f} / 淨賠 -{pos['expected_net_loss']:,.0f}"
                      f"（淨賺賠比 {pos['net_rr']}）{' [資金不足已縮減張數]' if pos.get('downsized') else ''}")
            else:
                p["record"]["reason"] += f"　⛔未進場：{a['why']}"
                print(f"   ⛔ [{p['record']['symbol']}] 未進場：{a['why']}")

    # 各影子組合獨立分配相同的每日本金與持倉上限，不會互相搶正式策略資金。
    for mode_key, mode in experiments["modes"].items():
        mode_candidates = [item["candidate"] for item in experiment_pending[mode_key] if item["candidate"]]
        mode_trades = mode.setdefault("trades", [])
        mode_positions = mode.setdefault("open_positions", [])
        if mode_candidates:
            mode_settings = normalize_settings(mode.get("settings"))
            mode_initial = get_daily_capital(mode_settings)
            mode_cash = te.capital_snapshot(mode_initial, mode_trades, live_quotes, discount, day_tax)["cash"]
            allocations = {id(item["candidate"]): item for item in te.allocate_candidates(
                mode_candidates, mode_cash, len(mode_positions), mode_settings["max_open_positions"],
                discount, day_tax, mode_settings)}
            for item in experiment_pending[mode_key]:
                candidate = item["candidate"]
                if not candidate:
                    continue
                allocation = allocations[id(candidate)]
                position = allocation["position"]
                if position:
                    mode_positions.append(position)
                    mode_trades.append(position.copy())
                    item["record"]["entered"] = True
                    item["record"]["trade_id"] = position["id"]
                else:
                    item["record"]["entered"] = False
                    item["record"]["entry_block_reason"] = allocation["why"]
        mode["summary"] = te.summarize_trades(mode_trades)

    for p in pending:
        latest_analysis_records = upsert_analysis_record(latest_analysis_records, p["record"])
        analysis_log = append_analysis_log(analysis_log, p["record"])

    update_capital(state, settings, now.strftime("%H:%M:%S"), cfg, live_quotes)

    state.update(wave1_stocks=wave1_stocks, wave2_stocks=wave2_stocks, latest_analysis_records=latest_analysis_records,
                 analysis_log=analysis_log, live_quotes=live_quotes, open_positions=open_positions,
                 strategy_trades=strategy_trades, last_analysis_minute_bucket=now.strftime("%Y-%m-%d %H:%M:%S"))
    persist(state, status_text=f"盤中分析中 ({hm})")
    print("✅ 本輪次（技術指標分析）執行完畢。")
    return "ok"


# ═════════════════════════════════════════════════════════════════
#  進入點：single（預設，一次觸發跑一輪）/ loop（單一 job 內每 60 秒一輪）
# ═════════════════════════════════════════════════════════════════
def _commit_push():
    """loop 模式在 job 內定期推送，讓網站維持近即時更新。"""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "commit_push.sh")
    if os.getenv("GITHUB_ACTIONS") and os.path.exists(script):
        import subprocess
        try:
            subprocess.run(["bash", script], check=False, timeout=180)
        except Exception as e:
            print(f"⚠️ 中途推送失敗（不影響交易流程）: {e}")


def run_loop(ctx: Dict):
    interval = int(os.getenv("LOOP_INTERVAL_SEC", "60"))
    end_hm = os.getenv("LOOP_END_TIME", "13:32")
    commit_every = int(os.getenv("COMMIT_EVERY_MIN", "5"))
    last_commit = time.monotonic()
    print(f"🔁 loop 模式：每 {interval} 秒一輪，{end_hm} 結束，每 {commit_every} 分鐘推送一次")
    while get_tw_now().strftime("%H:%M") <= end_hm:
        t0 = time.monotonic()
        try:
            status = run_once(ctx)
        except SystemExit:
            raise
        except Exception as e:  # 單輪失敗不可讓整個迴圈死掉
            import traceback
            print(f"⚠️ 本輪發生未預期錯誤，60 秒後重試: {e}")
            traceback.print_exc()
            status = "ok"
        if status == "done":
            break
        if status == "idle" and not market_calendar.is_trading_day(get_tw_now().date())[0] and not ctx["force"]:
            break
        if commit_every > 0 and time.monotonic() - last_commit >= commit_every * 60:
            _commit_push()
            last_commit = time.monotonic()
        time.sleep(max(1.0, interval - (time.monotonic() - t0)))
    print("🏁 loop 結束")


def main():
    print("=" * 65)
    print("🚀 [GitHub Actions] 雲端當沖技術策略系統啟動（v22）")
    print("=" * 65)
    ctx = build_context()
    mode = (os.getenv("RUN_MODE") or "single").strip().lower()
    if "--loop" in sys.argv:
        mode = "loop"
    if mode == "loop":
        run_loop(ctx)
    else:
        run_once(ctx)


if __name__ == "__main__":
    main()
