# -*- coding: utf-8 -*-
"""
market_calendar.py - 台股交易日 / 交易時段判斷（只用標準函式庫）

用途
────
1. headless_trader.py 判斷「今天是不是交易日」，避免國定假日走到測試模式、
   每 5 分鐘覆蓋 index.html 並 commit。
2. GitHub Actions 在 pip install 之前先跑 `python market_calendar.py gate`，
   非交易時段直接結束，不浪費安裝套件與 checkout 之後的時間。

休市日來源（依序）
────────────────
1. market_holidays.json（本機手動維護，最高優先）
     {"closed": ["2026-10-09", ...], "open": ["2026-02-14", ...]}
   closed = 強制休市；open = 強制開市（例如補班日不開市則不必填；颱風假等臨時休市請加入 closed）。
2. 證交所 OpenAPI holidaySchedule（連線失敗就略過，並視為交易日）。
   僅採用「市場無交易 / 休市 / 放假」類的項目，排除「最後交易日 / 開始交易日」。

⚠️ 假日資料請以證交所公告為準；自動抓取失敗時會 fail-open（當作交易日），
   這時若遇到颱風假等臨時休市，請手動寫入 market_holidays.json。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import urllib.request
from typing import Optional, Set, Tuple

TW_TZ = _dt.timezone(_dt.timedelta(hours=8))  # 台灣無日光節約時間，固定 UTC+8 即可
HOLIDAY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_holidays.json")
TWSE_HOLIDAY_URL = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"

# 可由環境變數調整（單位：HH:MM）
SESSION_OPEN = os.getenv("SESSION_OPEN", "08:50")
SESSION_CLOSE = os.getenv("SESSION_CLOSE", "13:45")  # 含 13:25 結算 + 補跑緩衝

_online_cache: Optional[Set[str]] = None


def now_tw() -> _dt.datetime:
    return _dt.datetime.now(TW_TZ)


def _roc_to_iso(s: str) -> Optional[str]:
    """'1150101' -> '2026-01-01'；格式不符回傳 None。"""
    s = (s or "").strip()
    if not s.isdigit() or len(s) not in (6, 7):
        return None
    y, m, d = int(s[:-4]) + 1911, int(s[-4:-2]), int(s[-2:])
    try:
        return _dt.date(y, m, d).isoformat()
    except ValueError:
        return None


def parse_twse_holidays(rows) -> Set[str]:
    """把 TWSE holidaySchedule 回傳（list[dict]）轉成休市日集合（ISO 日期字串）。"""
    out: Set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        iso = _roc_to_iso(str(row.get("Date", "")))
        if not iso:
            continue
        name = str(row.get("Name", ""))
        desc = str(row.get("Description", ""))
        text = name + desc
        if "開始交易" in name or "最後交易" in name:
            continue
        if any(k in text for k in ("無交易", "休市", "放假", "國定假日", "補假")):
            out.add(iso)
    return out


def _fetch_online_holidays(timeout: float = 8.0) -> Set[str]:
    global _online_cache
    if _online_cache is not None:
        return _online_cache
    try:
        req = urllib.request.Request(TWSE_HOLIDAY_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
        _online_cache = parse_twse_holidays(rows)
    except Exception as e:  # 網路 / 格式問題一律 fail-open
        print(f"⚠️ [交易日曆] 無法取得證交所休市日（{e}），改視為交易日。", file=sys.stderr)
        _online_cache = set()
    return _online_cache


def _load_local(path: str = HOLIDAY_FILE) -> Tuple[Set[str], Set[str]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("closed", [])), set(data.get("open", []))
    except (OSError, ValueError, TypeError, AttributeError):
        return set(), set()


def is_trading_day(d: _dt.date, online: bool = True, holiday_file: str = HOLIDAY_FILE) -> Tuple[bool, str]:
    """回傳 (是否交易日, 原因)。"""
    iso = d.isoformat()
    closed, forced_open = _load_local(holiday_file)
    if iso in forced_open:
        return True, "market_holidays.json 指定開市"
    if iso in closed:
        return False, "market_holidays.json 指定休市"
    if d.weekday() >= 5:
        return False, "週末"
    if online and iso in _fetch_online_holidays():
        return False, "證交所公告休市日"
    return True, "交易日"


def in_session(now: Optional[_dt.datetime] = None) -> bool:
    now = now or now_tw()
    return SESSION_OPEN <= now.strftime("%H:%M") <= SESSION_CLOSE


def should_run(now: Optional[_dt.datetime] = None, online: bool = True) -> Tuple[bool, str]:
    now = now or now_tw()
    ok, why = is_trading_day(now.date(), online=online)
    if not ok:
        return False, f"{now.date()} 非交易日（{why}）"
    if not in_session(now):
        return False, f"現在 {now.strftime('%H:%M')} 不在交易時段 {SESSION_OPEN}~{SESSION_CLOSE}"
    return True, "交易日且在交易時段"


def _cli_gate() -> int:
    """給 GitHub Actions 使用：把 run=true/false 寫進 $GITHUB_OUTPUT。"""
    force = os.getenv("FORCE_RUN", "").strip().lower() in ("1", "true", "yes")
    ok, why = (True, "手動強制執行（force=true）") if force else should_run()
    print(("▶️ 執行：" if ok else "⏭️ 略過：") + why)
    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"run={'true' if ok else 'false'}\n")
            f.write(f"reason={why}\n")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "gate":
        sys.exit(_cli_gate())
    ok, why = should_run()
    print(ok, why)
