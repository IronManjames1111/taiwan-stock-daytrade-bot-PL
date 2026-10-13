# -*- coding: utf-8 -*-
"""
replay_backtest.py - 用「當天存下來的 1 分 K」離線重放策略，比較新舊參數（v24）

資料來源：結算時自動存的 history_records/candles_日期.json.gz（v24 起每天都會存）。
用法：
    python replay_backtest.py                       # 重放 history_records 內所有 candles_*.json.gz，v23 舊參數 vs v24 新參數
    python replay_backtest.py --date 2026-10-13     # 只看某天
    python replay_backtest.py --grid                # 對 v24 掃一輪 停利倍數 / 最小停損% / 保本 組合，依淨損益排序
    python replay_backtest.py --files a.json.gz b.json.gz

限制（請知悉）：
  · 每檔獨立、一次一個部位，不模擬資金池與持倉上限；用來比較「參數好壞」而非精確複製實盤。
  · 進場價 = 訊號當根收盤價，沒有滑價；同根同時觸及停損停利判停損（與實盤一致）。
  · 「v23 舊參數」是近似：訊號視窗 1 根、無趨勢/波動過濾、停損 = ATR×1.25（至少 0.3%×1.25）、停利 = ATR×1.8，無出場管理。
    量能判斷為 v24 的「近 2 根均量」版本，與 v23 的單根量略有差異。
  · 樣本太少（例如只有幾天）時，勝率與損益的差異可能純屬運氣，請累積 10 個交易日以上再做結論。
"""
from __future__ import annotations

import argparse
import glob
import gzip
import itertools
import json
import statistics
from math import ceil, floor
from typing import Dict, List

import indicator_strategies as I
import trade_engine as te

DISCOUNT, DAY_TAX = 0.28, True
FIRST_ENTRY_HM, LAST_ENTRY_HM, SETTLE_HM = "09:15", "13:00", "13:25"

LEGACY_OVERRIDES = {
    "signal_lookback": 1, "min_efficiency": 0.0, "min_atr_pct": 0.0, "volume_floor": 0.0,
    "solo_strategies": list(I.STRATEGY_NAMES), "stop_atr": 1.25, "target_atr": 1.8,
    "breakeven_trigger_r": 0.0, "trail_gap_r": 0.0, "cooldown_minutes": 0, "cooldown_after_stop_only": False,
    "max_stops_per_symbol": 0, "max_entries_per_symbol": 5, "daily_loss_limit_pct": 0.0, "min_net_rr": 1.0,
}


def legacy_levels(side: str, entry: float, atr: float, cfg: Dict):
    risk = max(float(atr or 0), entry * 0.003)
    sd, td = risk * cfg["stop_atr"], risk * cfg["target_atr"]
    snap = I._snap
    if side == "BUY":
        return snap(entry - sd, "floor"), snap(entry + td, "ceil")
    return snap(entry + sd, "ceil"), snap(entry - td, "floor")


def load_days(files: List[str]) -> Dict[str, Dict[str, List[Dict]]]:
    days = {}
    for f in files:
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        day = data["date"]
        out = {}
        for sym, rows in data["candles"].items():
            out[sym] = [{"date": f"{day}T{r[0]}:00.000+08:00", "open": r[1], "high": r[2], "low": r[3],
                         "close": r[4], "volume": r[5]} for r in rows]
        days[day] = out
    return days


def simulate_day(bars_by_sym: Dict[str, List[Dict]], cfg: Dict, legacy: bool = False) -> List[Dict]:
    trades: List[Dict] = []
    initial = 1_000_000.0
    for sym, bars in bars_by_sym.items():
        bars = te.normalize_candles(bars)
        open_pos = None
        for i in range(len(bars)):
            hm = te.bar_hm(bars[i])
            # 先處理出場（用到目前為止的 K，與實盤每輪先掃出場一致）
            if open_pos:
                r = te.replay_position(open_pos, bars[:i + 1], SETTLE_HM)
                if r["hit"]:
                    h = r["hit"]
                    closed = te.close_position(open_pos, h["price"], h["time"], h["reason"], DISCOUNT, DAY_TAX, "replay",
                                               te.excursion_extra(open_pos, bars[:i + 1], SETTLE_HM))
                    trades[trades.index(open_pos)] = closed
                    open_pos = None
                elif hm >= SETTLE_HM:
                    last = te.last_close_until(bars[:i + 1], SETTLE_HM)
                    closed = te.close_position(open_pos, last[0], f"{SETTLE_HM}:00", "forced_close", DISCOUNT, DAY_TAX,
                                               "replay", te.excursion_extra(open_pos, bars[:i + 1], SETTLE_HM))
                    trades[trades.index(open_pos)] = closed
                    open_pos = None
            if open_pos or not hm or hm < FIRST_ENTRY_HM or hm > LAST_ENTRY_HM or i + 1 < cfg["min_bars"]:
                continue
            completed = bars[:i + 1]          # 這根已收完
            now_hms = f"{hm}:59"
            res = I.evaluate(completed, cfg)
            if res["signal"] not in ("BUY", "SHORT"):
                continue
            ok, _ = te.can_enter(sym, [t for t in trades if t["symbol"] == sym], now_hms, cfg,
                                 signal_bar_hm=res.get("signal_bar_hm"))
            if not ok:
                continue
            breached, _ = te.daily_loss_breached(initial, trades, {}, cfg.get("daily_loss_limit_pct", 0), DISCOUNT, DAY_TAX)
            if breached:
                continue
            side, entry = res["signal"], float(res["price"])
            if side == "SHORT" and not cfg["allow_short"]:
                continue
            if legacy:
                stop, target = legacy_levels(side, entry, res["atr"], cfg)
                plan = {"risk_dist": abs(entry - stop)}
            else:
                plan = I.position_plan(side, entry, res["atr"], cfg, completed)
                stop, target = plan["stop"], plan["target"]
            shares, _ = te.calc_shares(side, entry, stop, cfg, DISCOUNT, DAY_TAX)
            if shares <= 0:
                continue
            ok, _ = te.check_cost_gate(te.evaluate_net_rr(side, entry, stop, target, shares, DISCOUNT, DAY_TAX), cfg)
            if not ok:
                continue
            open_pos = {"symbol": sym, "signal": side, "entry_price": entry, "stop_loss": stop, "take_profit": target,
                        "shares": shares, "entry_time": now_hms, "entry_bar_hm": hm, "status": "open",
                        "strategy_name": res.get("strategy_name", ""), "risk_dist": plan["risk_dist"],
                        "be_trigger_r": 0 if legacy else cfg["breakeven_trigger_r"],
                        "trail_gap_r": 0 if legacy else cfg["trail_gap_r"],
                        "lock_dist": te.breakeven_lock_dist(side, entry, DISCOUNT, DAY_TAX)}
            trades.append(open_pos)
        if open_pos:       # 資料不足 13:25 時，以最後一根收盤平倉
            last = te.last_close_until(bars, "13:30")
            closed = te.close_position(open_pos, last[0], f"{last[1]}:00", "forced_close", DISCOUNT, DAY_TAX, "replay")
            trades[trades.index(open_pos)] = closed
    return [t for t in trades if t.get("status") == "closed"]


def summarize(trades: List[Dict]) -> Dict:
    n = len(trades)
    if not n:
        return {"n": 0, "win": 0.0, "net": 0.0, "gross": 0.0, "cost": 0.0, "avg_net": 0.0, "stop_pct": 0.0}
    wins = sum(1 for t in trades if t["pnl_amount"] > 0)
    return {"n": n, "win": wins / n, "net": sum(t["pnl_amount"] for t in trades), "gross": sum(t["pnl_gross"] for t in trades),
            "cost": sum(t["cost_amount"] for t in trades), "avg_net": sum(t["pnl_amount"] for t in trades) / n,
            "stop_pct": statistics.median(abs(t["stop_loss"] - t["entry_price"]) / t["entry_price"] * 100 for t in trades)}


def run(days: Dict, cfg: Dict, legacy: bool = False) -> List[Dict]:
    out = []
    for day, bars in sorted(days.items()):
        for t in simulate_day(bars, cfg, legacy):
            out.append({**t, "day": day})
    return out


def fmt(label: str, s: Dict) -> str:
    return (f"{label:<26} 筆數 {s['n']:>4}｜勝率 {s['win']*100:5.1f}%｜毛損益 {s['gross']:>9,.0f}｜成本 {s['cost']:>8,.0f}｜"
            f"淨損益 {s['net']:>9,.0f}｜每筆 {s['avg_net']:>7,.0f}｜停損中位 {s['stop_pct']:.2f}%")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--files", nargs="*")
    ap.add_argument("--date")
    ap.add_argument("--grid", action="store_true")
    a = ap.parse_args()
    files = a.files or sorted(glob.glob("history_records/candles_*.json.gz"))
    if a.date:
        files = [f for f in files if a.date in f]
    if not files:
        print("找不到 candles_*.json.gz。v24 起每天 13:25 結算時會自動儲存；請先累積資料。")
        return
    days = load_days(files)
    print(f"載入 {len(days)} 個交易日：{', '.join(sorted(days))}")
    new_cfg = I.load_strategy_settings()
    legacy_cfg = I.normalize_settings({**new_cfg, **LEGACY_OVERRIDES})
    old = run(days, legacy_cfg, legacy=True)
    new = run(days, new_cfg)
    print(fmt("v23 舊參數（近似）", summarize(old)))
    print(fmt("v24 新參數", summarize(new)))
    for label, tr in (("v24", new),):
        by = {}
        for t in tr:
            by.setdefault(t["exit_reason"], []).append(t)
        print("\n出場原因（v24）：")
        for k, v in sorted(by.items()):
            print(f"  {k:<13} {len(v):>3} 筆｜平均淨損益 {sum(x['pnl_amount'] for x in v)/len(v):>7,.0f}")
        mfe = [t.get("mfe_pct", 0) for t in tr if t["exit_reason"] == "hit_sl"]
        if mfe:
            print(f"  停損單在被洗出場前，平均曾經有利 {sum(mfe)/len(mfe):.2f}%（若遠大於保本啟動點，代表可再調低 breakeven_trigger_r）")
    if a.grid:
        rows = []
        for rr, stop, be in itertools.product((1.5, 2.0, 2.5, 3.0), (0.4, 0.6, 0.8, 1.0), (0.0, 1.0)):
            cfg = I.normalize_settings({**new_cfg, "target_rr": rr, "min_stop_pct": stop, "breakeven_trigger_r": be})
            rows.append((summarize(run(days, cfg)), rr, stop, be))
        rows.sort(key=lambda r: -r[0]["net"])
        print("\n參數掃描（依淨損益排序，前 8）：")
        for s, rr, stop, be in rows[:8]:
            print(fmt(f"rr={rr} 最小停損={stop}% 保本={'開' if be else '關'}", s))
        print("⚠️ 掃描結果在樣本少時容易過度擬合；請以多日、且前後期都表現穩定的組合為準。")


if __name__ == "__main__":
    main()
