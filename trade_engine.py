# -*- coding: utf-8 -*-
"""
trade_engine.py - 當沖模擬交易的「純函式」核心（不碰網路、不碰檔案，方便單元測試）

集中放這幾件事：
  1. 成本計算（手續費＋證交稅）與「扣成本後」的淨賺賠比檢查
  2. 部位大小（固定風險 / 固定金額 / 固定股數）
  3. 逐根 K 棒回放判斷停損停利（修正：不再只看最新一根）
  4. 重複進場限制（每檔每日上限＋冷卻時間）
  5. 平倉紀錄、結算清單、統計
  6. 分析歷程壓縮（連續重複的 WATCH 只留一筆）
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

FEE_RATE_BASE = 0.001425   # 手續費率（單向，未打折）
MIN_FEE_PER_SIDE = 20.0    # 單邊最低手續費
LOT = 1000                 # 1 張 = 1000 股


# ───────────────────────── 成本 ─────────────────────────

def tax_rate_for(is_day_trade_tax: bool) -> float:
    return 0.0015 if is_day_trade_tax else 0.003


def gross_pnl(side: str, entry: float, exit_price: float, shares: int) -> float:
    return (exit_price - entry) * shares * (1 if side == "BUY" else -1)


def round_trip_cost(side: str, entry: float, exit_price: float, shares: int,
                    broker_discount: float = 1.0, is_day_trade_tax: bool = True) -> float:
    """來回手續費＋證交稅（賣出那一邊才收稅：做多=出場價、放空=進場價）。"""
    discount = max(0.1, min(1.0, float(broker_discount or 1.0)))
    fee_rate = FEE_RATE_BASE * discount
    fee_in = max(entry * shares * fee_rate, MIN_FEE_PER_SIDE) if entry > 0 else 0.0
    fee_out = max(exit_price * shares * fee_rate, MIN_FEE_PER_SIDE) if exit_price > 0 else 0.0
    sell_value = (exit_price if side == "BUY" else entry) * shares
    return fee_in + fee_out + sell_value * tax_rate_for(is_day_trade_tax)


def net_pnl(side: str, entry: float, exit_price: float, shares: int,
            broker_discount: float = 1.0, is_day_trade_tax: bool = True) -> float:
    return gross_pnl(side, entry, exit_price, shares) - round_trip_cost(
        side, entry, exit_price, shares, broker_discount, is_day_trade_tax)


def evaluate_net_rr(side: str, entry: float, stop: float, target: float, shares: int,
                    broker_discount: float = 1.0, is_day_trade_tax: bool = True) -> Dict:
    """停利 / 停損各自扣成本後的淨損益、淨賺賠比、損益兩平勝率。"""
    net_win = net_pnl(side, entry, target, shares, broker_discount, is_day_trade_tax)
    net_loss = -net_pnl(side, entry, stop, shares, broker_discount, is_day_trade_tax)  # 轉成正的虧損額
    cost_target = round_trip_cost(side, entry, target, shares, broker_discount, is_day_trade_tax)
    target_gross = abs(gross_pnl(side, entry, target, shares))
    net_rr = (net_win / net_loss) if net_loss > 0 and net_win > 0 else 0.0
    breakeven = (net_loss / (net_win + net_loss)) if (net_win > 0 and net_loss > 0) else 1.0
    return {
        "net_win": round(net_win, 0), "net_loss": round(net_loss, 0),
        "net_rr": round(net_rr, 2), "breakeven_winrate": round(breakeven, 3),
        "cost": round(cost_target, 0), "target_gross": round(target_gross, 0),
        "target_cost_multiple": round(target_gross / cost_target, 2) if cost_target > 0 else 0.0,
    }


def check_cost_gate(metrics: Dict, settings: Dict) -> Tuple[bool, str]:
    """進場前的成本檢查：停利扣成本後必須仍值得做。"""
    if metrics["net_win"] <= 0:
        return False, f"停利扣成本後無獲利（成本約 {metrics['cost']:.0f} 元）"
    if metrics["target_cost_multiple"] < settings["min_target_cost_multiple"]:
        return False, (f"停利空間僅為成本的 {metrics['target_cost_multiple']:.1f} 倍"
                       f"（需 ≥ {settings['min_target_cost_multiple']}）")
    if metrics["net_rr"] < settings["min_net_rr"]:
        return False, (f"扣成本後淨賺賠比 {metrics['net_rr']:.2f} < {settings['min_net_rr']}"
                       f"（損益兩平勝率約 {metrics['breakeven_winrate']*100:.0f}%）")
    return True, ""


# ───────────────────────── 部位大小 ─────────────────────────

def calc_shares(side: str, entry: float, stop: float, settings: Dict,
                broker_discount: float = 1.0, is_day_trade_tax: bool = True) -> Tuple[int, str]:
    """
    回傳 (股數, 原因)。股數為 0 代表這筆不該做，原因會說明。
      fixed_risk   ：每筆最大虧損約 risk_per_trade 元（含成本），股價高波動大自動減量
      fixed_amount ：每筆投入約 position_amount 元
      fixed_shares ：固定 fixed_shares 股（舊行為）
    皆受 max_position_value 上限約束；最小單位為 1 張。
    """
    if entry <= 0:
        return 0, "進場價無效"
    mode = settings.get("sizing_mode", "fixed_risk")
    if mode == "fixed_shares":
        shares = int(settings.get("fixed_shares", LOT))
        return (shares, "") if shares > 0 else (0, "fixed_shares 設定為 0")
    lot_value = entry * LOT
    if mode == "fixed_amount":
        lots = math.floor(settings["position_amount"] / lot_value)
        why = f"每張約 {lot_value:,.0f} 元，超過單筆金額 {settings['position_amount']:,.0f}"
    else:
        per_lot_loss = abs(entry - stop) * LOT + round_trip_cost(
            side, entry, stop, LOT, broker_discount, is_day_trade_tax)
        lots = math.floor(settings["risk_per_trade"] / per_lot_loss) if per_lot_loss > 0 else 0
        why = f"每張停損風險約 {per_lot_loss:,.0f} 元，超過單筆風險上限 {settings['risk_per_trade']:,.0f}"
    cap = math.floor(settings["max_position_value"] / lot_value)
    lots = min(lots, cap)
    if lots < 1:
        return 0, why if cap >= 1 else f"每張約 {lot_value:,.0f} 元，超過部位上限 {settings['max_position_value']:,.0f}"
    return int(lots) * LOT, ""


# ───────────────────────── K 棒處理 ─────────────────────────

def bar_hm(bar: Dict) -> Optional[str]:
    """取 K 棒起始時間的 'HH:MM'（Fugle 的 date 形如 2026-10-06T09:05:00.000+08:00）。"""
    t = str(bar.get("date") or bar.get("time") or "")
    if "T" in t:
        t = t.split("T", 1)[1]
    t = t.strip()
    return t[:5] if len(t) >= 5 and t[2] == ":" else None


def normalize_candles(raw: Optional[List[Dict]]) -> List[Dict]:
    """去除無法解析時間的 K 棒並依時間由舊到新排序（API 可能回傳倒序）。"""
    bars = [b for b in (raw or []) if isinstance(b, dict) and bar_hm(b)]
    return sorted(bars, key=lambda b: str(b.get("date") or b.get("time")))


def drop_incomplete_bar(candles: List[Dict], now_hm: str) -> List[Dict]:
    """進行中的分 K 會「重繪」，訊號只能用已收完的 K 棒；起始分鐘 >= 現在分鐘者視為未收完。"""
    if candles and (bar_hm(candles[-1]) or "") >= now_hm:
        return candles[:-1]
    return candles


def scan_exit(pos: Dict, candles: List[Dict], last_hm: Optional[str] = None) -> Optional[Dict]:
    """
    依序掃描「進場那根 K 之後」的所有 K 棒，回傳第一個觸及的出場，或 None。
    - 同一根同時觸及停損與停利 → 保守判停損
    - 跳空越過停損 → 以該根開盤價（更差）成交；停利以掛單價成交
    - last_hm：只掃描到此分鐘（含），用於 13:25 強平
    """
    long_side = pos.get("signal") == "BUY"
    sl, tp = float(pos["stop_loss"]), float(pos["take_profit"])
    start = pos.get("entry_bar_hm") or str(pos.get("entry_time", ""))[:5]
    for bar in candles:
        hm = bar_hm(bar)
        if not hm or hm <= start:
            continue
        if last_hm and hm > last_hm:
            break
        try:
            high, low = float(bar["high"]), float(bar["low"])
        except (KeyError, TypeError, ValueError):
            continue
        open_ = float(bar["open"]) if bar.get("open") not in (None, "") else None
        hit_sl = low <= sl if long_side else high >= sl
        hit_tp = high >= tp if long_side else low <= tp
        if hit_sl:
            price = sl
            if open_ is not None:
                price = min(sl, open_) if long_side else max(sl, open_)
            return {"reason": "hit_sl", "price": price, "time": f"{hm}:00"}
        if hit_tp:
            return {"reason": "hit_tp", "price": tp, "time": f"{hm}:00"}
    return None


def last_close_until(candles: List[Dict], last_hm: str) -> Optional[Tuple[float, str]]:
    """last_hm（含）之前最後一根 K 的收盤價與時間，供強平使用。"""
    chosen = None
    for bar in candles:
        hm = bar_hm(bar)
        if hm and hm <= last_hm and bar.get("close") is not None:
            chosen = (float(bar["close"]), hm)
    return chosen


def close_position(pos: Dict, exit_price: float, exit_time: str, reason: str,
                   broker_discount: float, is_day_trade_tax: bool, price_source: str = "") -> Dict:
    side = pos.get("signal")
    shares = int(pos.get("shares", LOT))
    entry = float(pos["entry_price"])
    gross = gross_pnl(side, entry, exit_price, shares)
    cost = round_trip_cost(side, entry, exit_price, shares, broker_discount, is_day_trade_tax)
    net = gross - cost
    closed = {**pos, "status": "closed", "exit_price": round(float(exit_price), 2), "exit_time": exit_time,
              "exit_reason": reason, "pnl_gross": round(gross, 0), "cost_amount": round(cost, 0),
              "pnl_amount": round(net, 0), "result": "win" if net > 0 else "loss" if net < 0 else "breakeven"}
    if price_source:
        closed["price_source"] = price_source
    return closed


# ───────────────────────── 重複進場限制 ─────────────────────────

def _minutes(hms: str) -> Optional[int]:
    try:
        h, m = str(hms).split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, TypeError):
        return None


def can_enter(symbol: str, trades: List[Dict], now_hm: str, settings: Dict) -> Tuple[bool, str]:
    """
    重複進場限制（v23 放寬）：
      • 每檔每日進場次數上限 max_entries_per_symbol（預設 5）
      • 冷卻 cooldown_minutes（預設 5 分鐘）；cooldown_after_stop_only=True 時只有「停損出場」後才冷卻，
        停利出場後趨勢仍在可以立刻再進。
    """
    mine = [t for t in trades if t.get("symbol") == symbol]
    if len(mine) >= settings["max_entries_per_symbol"]:
        return False, f"今日已進場 {len(mine)} 次，達每檔上限 {settings['max_entries_per_symbol']}"
    now_m = _minutes(now_hm)
    cooldown = settings["cooldown_minutes"]
    stop_only = settings.get("cooldown_after_stop_only", True)
    if cooldown <= 0 or now_m is None:
        return True, ""
    for t in mine:
        if t.get("status") != "closed" or not t.get("exit_time"):
            continue
        if stop_only and t.get("exit_reason") != "hit_sl":
            continue
        ex = _minutes(t["exit_time"])
        if ex is not None and 0 <= now_m - ex < cooldown:
            return False, f"{t['exit_time'][:5]} 停損出場，冷卻 {cooldown} 分鐘內不再進場"
    return True, ""


# ───────────────────────── 每日資金池 ─────────────────────────

def position_value(entry: float, shares: int) -> float:
    """部位佔用資金（做多=買進金額；放空以賣出金額估算，實際融券保證金成數另計，這裡是簡化模型）。"""
    return float(entry) * int(shares)


def capital_snapshot(initial: float, trades: List[Dict], live_quotes: Optional[Dict] = None,
                     broker_discount: float = 1.0, is_day_trade_tax: bool = True) -> Dict:
    """
    由交易紀錄「推算」當下資金狀態（不另外維護一個會對不上的餘額）：
      realized   = 已平倉淨損益合計（已扣手續費、證交稅）
      locked     = 未平倉部位佔用資金
      cash       = 本金 + realized - locked        ← 進場時扣款、出場時（本金 + 淨損益）回補
      unrealized = 未平倉部位以最新價試算的損益（已扣預估來回成本，與平倉後結果一致）
      equity     = cash + locked + unrealized
    """
    live_quotes = live_quotes or {}
    realized = locked = unrealized = 0.0
    for t in trades:
        if t.get("status") == "closed":
            realized += float(t.get("pnl_amount", 0) or 0)
        elif t.get("status") == "open":
            shares = int(t.get("shares", LOT))
            entry = float(t["entry_price"])
            locked += position_value(entry, shares)
            px = (live_quotes.get(t.get("symbol")) or {}).get("price")
            if px:
                px = float(px)
                unrealized += (gross_pnl(t["signal"], entry, px, shares)
                               - round_trip_cost(t["signal"], entry, px, shares, broker_discount, is_day_trade_tax))
    cash = initial + realized - locked
    return {"initial": round(initial, 0), "cash": round(cash, 0), "locked": round(locked, 0),
            "realized": round(realized, 0), "unrealized": round(unrealized, 0),
            "equity": round(initial + realized + unrealized, 0)}


def append_capital_point(cap: Dict, t_hms: str, snap: Dict) -> Dict:
    """把一個時間點的資金狀態加進資金曲線（同一時間點覆蓋；曲線起點固定為開盤本金）。"""
    cap = dict(cap or {})
    curve = list(cap.get("curve") or [])
    if not curve:
        curve.append({"t": "09:00:00", "equity": snap["initial"], "cash": snap["initial"], "locked": 0,
                      "realized": 0, "unrealized": 0})
    point = {"t": t_hms, **{k: snap[k] for k in ("equity", "cash", "locked", "realized", "unrealized")}}
    if curve[-1]["t"] == t_hms:
        curve[-1] = point
    else:
        curve.append(point)
    cap.update(snap)
    cap["curve"] = curve
    return cap


def allocate_candidates(candidates: List[Dict], cash: float, open_count: int, max_open: int,
                        broker_discount: float, is_day_trade_tax: bool, settings: Dict) -> List[Dict]:
    """
    同一輪有多個可進場訊號時，由程式決定先買誰、買多少。
      排序：獨立票數多者優先 → 扣成本後淨賺賠比高者優先。
      資金不足一整張時縮減張數；一張都買不起就略過並註明原因。
    candidates 每項：{"position": {...}, "votes": int}；回傳同順序、附上 "funded"(bool) 與 "why"。
    """
    out = []
    for c in sorted(candidates, key=lambda c: (-c.get("votes", 0), -float(c["position"].get("net_rr", 0)))):
        pos = dict(c["position"])
        entry, shares = float(pos["entry_price"]), int(pos["shares"])
        res = {"candidate": c, "position": None, "why": ""}
        if open_count >= max_open:
            res["why"] = f"持倉已達上限 {max_open} 檔"
        elif position_value(entry, shares) <= cash:
            res["position"] = pos
        else:
            lots = int(cash // (entry * LOT))
            if lots < 1:
                res["why"] = (f"資金不足：可用 {cash:,.0f} 元，買一張需 {entry * LOT:,.0f} 元")
            else:
                shares = lots * LOT
                m = evaluate_net_rr(pos["signal"], entry, float(pos["stop_loss"]), float(pos["take_profit"]),
                                    shares, broker_discount, is_day_trade_tax)
                ok, why = check_cost_gate(m, settings)
                if not ok:
                    res["why"] = f"資金只夠 {lots} 張，縮減後{why}"
                else:
                    pos.update(shares=shares, expected_net_win=m["net_win"], expected_net_loss=m["net_loss"],
                               expected_cost=m["cost"], net_rr=m["net_rr"], downsized=True)
                    res["position"] = pos
        if res["position"]:
            res["position"]["position_value"] = round(position_value(entry, res["position"]["shares"]), 0)
            cash -= position_value(entry, res["position"]["shares"])
            open_count += 1
        out.append(res)
    return out


def capital_day_summary(date_str: str, cap: Dict, trades: List[Dict]) -> Dict:
    """寫進 capital_history.json 的單日摘要。"""
    s = summarize_trades(trades)
    curve = (cap or {}).get("curve") or []
    initial = float((cap or {}).get("initial", 0) or 0)
    pnl = float(s["net_pnl"])
    return {"date": date_str, "initial": round(initial, 0), "final": round(initial + pnl, 0),
            "pnl": round(pnl, 0), "pnl_pct": round(pnl / initial * 100, 3) if initial else 0.0,
            "entries": s["entries"], "closed": s["closed"], "wins": s["wins"],
            "win_rate": round(s["win_rate"], 4) if s["win_rate"] is not None else None,
            "cost": round(s["cost"], 0),
            "max_locked": round(max([float(p.get("locked", 0)) for p in curve] or [0]), 0),
            "low_equity": round(min([float(p.get("equity", initial)) for p in curve] or [initial]), 0)}


def upsert_day_summary(days: List[Dict], summary: Dict) -> List[Dict]:
    days = [d for d in (days or []) if isinstance(d, dict) and d.get("date") != summary["date"]]
    days.append(summary)
    return sorted(days, key=lambda d: d["date"])


# ───────────────────────── 結算 / 統計 ─────────────────────────

def trade_to_settle_record(t: Dict) -> Dict:
    """把一筆已平倉交易轉成儀表板「結算清單」用的格式（單一資料來源：strategy_trades）。"""
    return {
        "symbol": t.get("symbol"), "name": t.get("name", ""), "signal": t.get("signal"),
        "direction": t.get("direction", ""), "strategy": t.get("strategy_name") or t.get("strategy", ""),
        "shares": t.get("shares"), "position_value": t.get("position_value"), "entry_time": t.get("entry_time"), "exit_time": t.get("exit_time"),
        "entry_price": t.get("entry_price"), "exit_price": t.get("exit_price"),
        "stop_loss": t.get("stop_loss"), "take_profit": t.get("take_profit"),
        "result": t.get("result"), "pnl_amount": t.get("pnl_amount", 0),
        "pnl_gross": t.get("pnl_gross"), "cost_amount": t.get("cost_amount"),
        "exit_reason": t.get("exit_reason"), "settle_status": "settled",
    }


def build_settle_records(trades: List[Dict]) -> List[Dict]:
    return [trade_to_settle_record(t) for t in trades if t.get("status") == "closed"]


def summarize_trades(trades: List[Dict]) -> Dict:
    closed = [t for t in trades if t.get("status") == "closed"]
    wins = sum(1 for t in closed if float(t.get("pnl_amount", 0) or 0) > 0)
    return {
        "entries": len(trades), "closed": len(closed), "wins": wins,
        "win_rate": (wins / len(closed)) if closed else None,
        "net_pnl": sum(float(t.get("pnl_amount", 0) or 0) for t in closed),
        "cost": sum(float(t.get("cost_amount", 0) or 0) for t in closed),
    }


# ───────────────────────── 分析歷程壓縮 ─────────────────────────

def append_log_compact(log: List[Dict], rec: Dict) -> List[Dict]:
    """同一檔連續且理由相同的 WATCH 只保留一筆（更新時間與重複次數），避免 state / index.html 膨脹。"""
    if rec.get("signal") == "WATCH":
        for i in range(len(log) - 1, -1, -1):
            prev = log[i]
            if prev.get("symbol") != rec.get("symbol"):
                continue
            if prev.get("signal") == "WATCH" and prev.get("reason") == rec.get("reason"):
                prev.setdefault("first_seen", prev.get("updated_at"))
                prev["updated_at"] = rec.get("updated_at")
                prev["entry"] = rec.get("entry")
                prev["repeat"] = int(prev.get("repeat", 1)) + 1
                return log
            break
    log.append(rec)
    return log
