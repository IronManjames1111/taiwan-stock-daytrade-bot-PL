# -*- coding: utf-8 -*-
"""
build_analytics.py - 產生「績效分析中心」analytics.html（v25）

讀取 history_records/backtest_*.csv 與 strategy_settings.json，把交易資料壓縮後內嵌進
analytics_template.html，輸出單一靜態網頁 analytics.html（GitHub Pages 直接可用，不需後端）。

用法：  python build_analytics.py
排程：  headless_trader 每日結算後呼叫 build() 即可（失敗不影響交易流程）。
"""
from __future__ import annotations

import csv
import glob
import json
import os
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
XK = {"hit_sl": 0, "hit_tp": 1, "hit_trail": 2, "forced_close": 3}

# v24 → v25 參數變更（頁面「v25 調整」分頁顯示）：[參數, 舊值, 新值, 依據]
CHANGES = [
    ["RISK_MODE（repo Variable）", "relaxed（實盤）", "auto", "實盤紀錄顯示門檻為 1 票（relaxed 覆蓋了設定檔）。v24 期間單一策略 183 筆淨損 −18.6k；≥2 策略共振 79 筆 +5.9k。回放：relaxed −52.2k → auto +4.5k。"],
    ["long_start_hm", "（無）", "10:00", "10:00 前做多 35 筆、勝率 37%、淨損 −18.7k，為最大虧損來源。"],
    ["min_price", "（無）", "20", "股價 <20 元 12 筆、勝率 25%、淨損 −10.8k；成本占比高。"],
    ["breakeven_trigger_r", "1.2", "1.6", "回放：保本太早會被洗出場。1.4 / 1.6 / 2.0 皆優於 1.2（相鄰值表現穩定），選中間值 1.6。"],
    ["min_stop_pct", "0.6", "0.8", "停損落在 1 分 K 雜訊內易被掃。0.8 回放 +45.0k；1.0 反而回落至 +26.0k，故不再加大。"],
    ["max_entries_per_symbol / cooldown", "6 / 10", "6 / 10", "維持：第 4 次以後進場差異不顯著（25 筆 −2.1k），回放改 3 次也無增益。"],
    ["回放總結（17 日）", "−52.2k", "+58.9k", "前半期 +38.9k、後半期 +19.9k。這是事後最佳化結果，實盤請預期明顯縮水，先紙上跑 10 日。"],
]


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _min(hms):
    try:
        h, m = str(hms)[:5].split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def load_trades():
    rows = []
    for f in sorted(glob.glob(os.path.join(BASE, "history_records", "backtest_*.csv"))):
        day = os.path.basename(f)[9:19]
        try:
            with open(f, encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh):
                    if (r.get("status") or "closed") != "closed" or r.get("pnl_amount") in (None, ""):
                        continue
                    t = _min(r.get("entry_time") or r.get("time"))
                    x = _min(r.get("exit_time"))
                    if t is None:
                        continue
                    direction = 0 if (r.get("direction") == "做多" or r.get("signal") == "BUY") else 1
                    rows.append([
                        day, str(r.get("symbol", "")), r.get("name", ""), direction,
                        (r.get("strategy_name") or "未分類").strip(), t, max(0, (x - t) if x is not None else 0),
                        round(_num(r.get("entry_price")), 2), round(_num(r.get("pnl_amount"))),
                        round(_num(r.get("pnl_gross"), _num(r.get("pnl_amount")))), round(_num(r.get("cost_amount"))),
                        XK.get(r.get("exit_reason"), 3),
                    ])
        except OSError:
            continue
    return rows


def build(out="analytics.html"):
    with open(os.path.join(BASE, "strategy_settings.json"), encoding="utf-8") as fh:
        rec = json.load(fh)
    data = {"T": load_trades(), "rec": rec, "chg": CHANGES, "gen": datetime.now().strftime("%Y-%m-%d %H:%M")}
    with open(os.path.join(BASE, "analytics_template.html"), encoding="utf-8") as fh:
        html = fh.read()
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    with open(os.path.join(BASE, out), "w", encoding="utf-8") as fh:
        fh.write(html.replace("/*DATA*/null", payload, 1))
    print(f"✅ 已產生 {out}：{len(data['T'])} 筆交易")


if __name__ == "__main__":
    build()
