"""用 node 實際執行資金圖表的 JS，並用 XML 解析器檢查產生的 SVG（沒有 node 時略過）。"""
import json
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

import dashboard_capital as dc

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")

HARNESS = r"""
const m = require(process.argv[2]);
const out = {};
const mk = (eq) => eq.map((e, i) => ({t: `${String(9 + Math.floor(i * 5 / 60)).padStart(2,'0')}:${String(i * 5 % 60).padStart(2,'0')}:00`,
  equity: e, cash: 1000000 - (i % 3) * 200000, locked: (i % 3) * 200000, realized: 0, unrealized: e - 1000000}));
out.up = m.dayChart({initial: 1000000, curve: mk([1000000, 1000500, 1002000, 1001500, 1004000])});
out.down = m.dayChart({initial: 1000000, curve: mk([1000000, 999000, 997000])});
out.flat = m.dayChart({initial: 1000000, curve: mk([1000000, 1000000, 1000000])});
out.short = m.dayChart({initial: 1000000, curve: mk([1000000])});
out.none = m.dayChart(null);
out.locked = m.lockedChart({initial: 1000000, curve: mk([1000000, 1000500, 1002000, 1001500])});
out.d1 = m.dailyChart([{date: '2026-10-06', pnl: 3200, pnl_pct: 0.32, entries: 5, win_rate: 0.6}]);
out.dmany = m.dailyChart(Array.from({length: 40}, (_, i) => ({date: '2026-09-' + String(i % 28 + 1).padStart(2, '0'), pnl: (i % 5 - 2) * 1500})));
out.dneg = m.dailyChart([{date: '2026-10-05', pnl: -5000}, {date: '2026-10-06', pnl: -2000, provisional: true}]);
out.dempty = m.dailyChart([]);
// DOM 樣板
const els = {};
global.document = undefined;
console.log(JSON.stringify(out));
"""


def run_node(tmp_path):
    js = tmp_path / "cap.js"
    js.write_text(dc.CAPITAL_JS, encoding="utf-8")
    h = tmp_path / "h.js"
    h.write_text(HARNESS, encoding="utf-8")
    r = subprocess.run(["node", str(h), str(js)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_js_syntax_and_svgs_are_well_formed(tmp_path):
    out = run_node(tmp_path)
    for name in ("up", "down", "flat", "locked", "d1", "dmany", "dneg"):
        root = ET.fromstring(out[name])           # 不是合法 XML 會直接拋例外
        assert root.tag.endswith("svg"), name
        assert "NaN" not in out[name] and "Infinity" not in out[name], name
    for name in ("short", "none", "dempty"):
        assert "<svg" not in out[name] and "尚無" in out[name]


def test_up_chart_is_red_and_down_chart_is_green(tmp_path):
    out = run_node(tmp_path)
    assert 'stroke="#ff5470"' in out["up"] and 'stroke="#00d68f"' not in out["up"]      # 台股：賺紅
    assert 'stroke="#00d68f"' in out["down"]                                             # 賠綠
    assert "本金 1,000,000" in out["up"]


def test_dom_render_with_stub(tmp_path):
    js = tmp_path / "cap.js"
    js.write_text(dc.CAPITAL_JS, encoding="utf-8")
    h = tmp_path / "dom.js"
    h.write_text(r"""
const els = {};
for (const id of ['cap-kpis','cap-day-chart','cap-locked-chart','cap-date-tag','cap-daily-kpis','cap-daily-chart'])
  els[id] = {innerHTML: '', textContent: ''};
global.document = {getElementById: id => els[id] || null};
global.window = {EXPORT_DATA: {today_str: '2026-10-06',
  capital: {initial: 1000000, cash: 800000, locked: 200000, realized: 1200, unrealized: -300, equity: 1000900,
            curve: [{t:'09:00:00',equity:1000000,cash:1000000,locked:0,realized:0,unrealized:0},{t:'09:30:00',equity:1000900,cash:800000,locked:200000,realized:1200,unrealized:-300}]},
  capital_history: [{date:'2026-10-05', pnl: 2500, pnl_pct: .25, entries: 4, win_rate: .5}]},
  onHistoryDateChange: async () => {}};
global.EXPORT_DATA = global.window.EXPORT_DATA;
const m = require(process.argv[2]);
console.log(JSON.stringify(els));
""", encoding="utf-8")
    # module.exports 與 init 同時存在：有 document 時 init 會執行
    r = subprocess.run(["node", str(h), str(js)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    els = json.loads(r.stdout)
    assert "1,000,000" in els["cap-kpis"]["innerHTML"] and "+0.09%" in els["cap-kpis"]["innerHTML"]
    assert "<svg" in els["cap-day-chart"]["innerHTML"] and "<svg" in els["cap-locked-chart"]["innerHTML"]
    assert "2026-10-06" in els["cap-daily-chart"]["innerHTML"]        # 今日以「淡色即時」列入每日圖
    assert "2026-10-05" in els["cap-daily-chart"]["innerHTML"]
    assert els["cap-date-tag"]["textContent"] == "2026-10-06（今日）"
