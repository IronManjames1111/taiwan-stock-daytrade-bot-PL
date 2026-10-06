# -*- coding: utf-8 -*-
"""
dashboard_capital.py - 儀表板「資金變化」卡片的 HTML 與 JavaScript（純字串，不是 f-string）

獨立成檔的原因：headless_trader.render_html_dashboard 是一個超大的 f-string，
裡面每個 JS 大括號都要寫成 {{ }}，很容易出錯。這裡的內容原封不動插入頁面，
不需要跳脫，也能單獨用 node 做語法與繪圖測試。

圖表全部用內嵌 SVG 繪製（不依賴任何外部套件 / CDN）。
配色採台股慣例：賺錢＝紅、賠錢＝綠。
"""

CAPITAL_CARD_HTML = """
        <!-- 💰 資金變化卡片 -->
        <div class="panel border rounded-2xl p-5" id="capital-card">
            <div class="flex items-center justify-between border-b border-white/5 pb-3 mb-3">
                <div>
                    <h2 class="font-bold text-white text-base">💰 資金變化</h2>
                    <p class="text-[11px] text-gray-500 mt-0.5">每天開盤給固定本金；進場先扣除佔用資金，出場後「本金＋淨損益」回補</p>
                </div>
                <span id="cap-date-tag" class="text-[11px] text-[#f5b942] font-semibold whitespace-nowrap"></span>
            </div>
            <div id="cap-kpis" class="grid grid-cols-2 md:grid-cols-3 gap-2 mb-3"></div>
            <div class="text-[11px] text-gray-500 mb-1">當日資金（權益）曲線　<span class="text-gray-600">含未實現損益，已扣預估手續費與證交稅</span></div>
            <div id="cap-day-chart"></div>
            <div class="text-[11px] text-gray-500 mt-3 mb-1">當日資金佔用（佔本金比例）</div>
            <div id="cap-locked-chart"></div>
            <div class="border-t border-white/5 mt-5 pt-4">
                <h3 class="font-bold text-white text-sm">每日資金變化</h3>
                <p class="text-[11px] text-gray-500 mt-0.5 mb-3">長條＝當日淨損益（每天本金重置），折線＝累計損益；淡色為今日尚未結算的即時數字</p>
                <div id="cap-daily-kpis" class="grid grid-cols-2 md:grid-cols-4 gap-2 mb-3"></div>
                <div id="cap-daily-chart"></div>
            </div>
        </div>
"""

CAPITAL_JS = r"""
(function () {
  'use strict';
  var RED = '#ff5470', GREEN = '#00d68f', GRAY = '#9ca3af', GOLD = '#f5b942', GRID = 'rgba(255,255,255,0.07)';
  function $(id) { return document.getElementById(id); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); }
  function fmt(n) { return Math.round(Number(n) || 0).toLocaleString('en-US'); }
  function sgn(n) { n = Math.round(Number(n) || 0); return (n > 0 ? '+' : n < 0 ? '-' : '') + Math.abs(n).toLocaleString('en-US'); }
  function col(n) { return n > 0 ? RED : n < 0 ? GREEN : GRAY; }
  function minutes(t) { var p = String(t).split(':'); return (+p[0]) * 60 + (+p[1] || 0) + ((+p[2] || 0) / 60); }

  function niceStep(raw) {
    var exp = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / exp;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * exp;
  }
  function ticks(min, max, n) {
    if (!(max > min)) { max = min + 1; }
    var step = niceStep((max - min) / n), out = [];
    for (var v = Math.ceil(min / step) * step; v <= max + step * 1e-6; v += step) out.push(v);
    return out;
  }
  function svg(w, h, inner) {
    return '<svg viewBox="0 0 ' + w + ' ' + h + '" width="100%" role="img" style="display:block;max-width:100%">' + inner + '</svg>';
  }
  function empty(msg) { return '<div class="text-center text-gray-500 text-xs py-6">' + esc(msg) + '</div>'; }
  function kpi(label, value, color, sub) {
    return '<div class="panel-raised border rounded-lg p-2.5"><div class="text-[11px] text-gray-500">' + esc(label) +
      '</div><div class="text-base font-bold mono mt-0.5" style="color:' + (color || '#e5e7eb') + '">' + esc(value) +
      '</div>' + (sub ? '<div class="text-[10px] text-gray-500 mono">' + esc(sub) + '</div>' : '') + '</div>';
  }

  /* ── 當日權益曲線 ─────────────────────────────── */
  function dayChart(cap) {
    var curve = (cap && cap.curve) || [];
    if (curve.length < 2) return empty('尚無足夠的資金曲線資料（盤中開始分析後會逐輪累積）');
    var W = 640, H = 270, L = 88, R = 14, T = 16, B = 32, pw = W - L - R, ph = H - T - B;
    var base = Number(cap.initial) || curve[0].equity;
    var vals = curve.map(function (p) { return Number(p.equity); }).concat([base]);
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    var pad = Math.max((hi - lo) * 0.15, base * 0.0005);
    lo -= pad; hi += pad;
    var X0 = 9 * 60, X1 = 13.5 * 60;
    var last = minutes(curve[curve.length - 1].t); if (last > X1) X1 = last;
    function x(t) { return L + (Math.max(X0, minutes(t)) - X0) / (X1 - X0) * pw; }
    function y(v) { return T + (hi - v) / (hi - lo) * ph; }
    var finalEq = Number(curve[curve.length - 1].equity), c = finalEq >= base ? RED : GREEN;
    var g = '';
    ticks(lo, hi, 4).forEach(function (v) {
      g += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y(v) + '" y2="' + y(v) + '" stroke="' + GRID + '"/>' +
           '<text x="' + (L - 6) + '" y="' + (y(v) + 3) + '" text-anchor="end" font-size="15" fill="#6b7280">' + fmt(v) + '</text>';
    });
    for (var h = 9; h <= 13; h++) {
      var xx = L + (h * 60 - X0) / (X1 - X0) * pw;
      g += '<text x="' + xx + '" y="' + (H - 10) + '" text-anchor="middle" font-size="15" fill="#6b7280">' + (h < 10 ? '0' : '') + h + ':00</text>';
    }
    g += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y(base) + '" y2="' + y(base) + '" stroke="#9ca3af" stroke-dasharray="4 4"/>' +
         '<text x="' + (W - R) + '" y="' + (y(base) - 4) + '" text-anchor="end" font-size="15" fill="#9ca3af">本金 ' + fmt(base) + '</text>';
    var pts = curve.map(function (p) { return x(p.t).toFixed(1) + ',' + y(Number(p.equity)).toFixed(1); });
    var area = 'M' + x(curve[0].t).toFixed(1) + ',' + y(base).toFixed(1) + ' L' + pts.join(' L') +
               ' L' + x(curve[curve.length - 1].t).toFixed(1) + ',' + y(base).toFixed(1) + ' Z';
    g += '<path d="' + area + '" fill="' + c + '" fill-opacity="0.12"/>' +
         '<polyline points="' + pts.join(' ') + '" fill="none" stroke="' + c + '" stroke-width="2" stroke-linejoin="round"/>';
    curve.forEach(function (p) {
      var d = Number(p.equity) - base;
      g += '<circle cx="' + x(p.t).toFixed(1) + '" cy="' + y(Number(p.equity)).toFixed(1) + '" r="5" fill="transparent"><title>' +
           esc(String(p.t).slice(0, 5)) + '  權益 ' + fmt(p.equity) + '（' + sgn(d) + '）  可用 ' + fmt(p.cash) + '  佔用 ' + fmt(p.locked) +
           '</title></circle>';
    });
    return svg(W, H, g);
  }

  /* ── 當日資金佔用（階梯面積）────────────────────── */
  function lockedChart(cap) {
    var curve = (cap && cap.curve) || [];
    if (curve.length < 2) return empty('—');
    var W = 640, H = 100, L = 88, R = 14, T = 8, B = 8, pw = W - L - R, ph = H - T - B;
    var base = Number(cap.initial) || 1;
    var X0 = 9 * 60, X1 = 13.5 * 60, last = minutes(curve[curve.length - 1].t); if (last > X1) X1 = last;
    function x(t) { return L + (Math.max(X0, minutes(t)) - X0) / (X1 - X0) * pw; }
    function y(r) { return T + (1 - Math.min(r, 1)) * ph; }
    var d = 'M' + x(curve[0].t).toFixed(1) + ',' + y(0).toFixed(1), prevR = 0, maxR = 0;
    curve.forEach(function (p) {
      var r = (Number(p.locked) || 0) / base; maxR = Math.max(maxR, r);
      d += ' L' + x(p.t).toFixed(1) + ',' + y(prevR).toFixed(1) + ' L' + x(p.t).toFixed(1) + ',' + y(r).toFixed(1); prevR = r;
    });
    d += ' L' + x(curve[curve.length - 1].t).toFixed(1) + ',' + y(0).toFixed(1) + ' Z';
    var g = '';
    [0, 0.5, 1].forEach(function (r) {
      g += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y(r) + '" y2="' + y(r) + '" stroke="' + GRID + '"/>' +
           '<text x="' + (L - 6) + '" y="' + (y(r) + 3) + '" text-anchor="end" font-size="15" fill="#6b7280">' + Math.round(r * 100) + '%</text>';
    });
    g += '<path d="' + d + '" fill="#8db3ff" fill-opacity="0.35" stroke="#8db3ff" stroke-width="1.2"/>' +
         '<text x="' + (W - R) + '" y="' + (T + 14) + '" text-anchor="end" font-size="15" fill="#8db3ff">最高佔用 ' + Math.round(maxR * 100) + '%</text>';
    return svg(W, H, g);
  }

  /* ── 每日資金變化（長條＝當日損益、折線＝累計）──────── */
  function dailyChart(days) {
    if (!days.length) return empty('尚無歷史資金資料（收盤結算後開始累積）');
    var W = 640, H = 270, L = 76, R = 14, T = 16, B = 34, pw = W - L - R, ph = H - T - B;
    var cum = 0, rows = days.map(function (d) { cum += Number(d.pnl) || 0; return {d: d, cum: cum}; });
    var all = [0]; rows.forEach(function (r) { all.push(Number(r.d.pnl) || 0, r.cum); });
    var lo = Math.min.apply(null, all), hi = Math.max.apply(null, all), pad = Math.max((hi - lo) * 0.12, 500);
    lo -= pad; hi += pad;
    function y(v) { return T + (hi - v) / (hi - lo) * ph; }
    var slot = pw / rows.length, bw = Math.min(36, slot * 0.6);
    function cx(i) { return L + slot * (i + 0.5); }
    var g = '';
    ticks(lo, hi, 4).forEach(function (v) {
      g += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y(v) + '" y2="' + y(v) + '" stroke="' + (v === 0 ? '#9ca3af' : GRID) + '"/>' +
           '<text x="' + (L - 6) + '" y="' + (y(v) + 3) + '" text-anchor="end" font-size="15" fill="#6b7280">' + sgn(v) + '</text>';
    });
    var every = Math.ceil(rows.length / 10);
    rows.forEach(function (r, i) {
      var p = Number(r.d.pnl) || 0, top = Math.min(y(p), y(0)), hgt = Math.max(Math.abs(y(p) - y(0)), 1);
      var tip = esc(r.d.date) + '  損益 ' + sgn(p) + (r.d.pnl_pct != null ? '（' + Number(r.d.pnl_pct).toFixed(2) + '%）' : '') +
        '  累計 ' + sgn(r.cum) + (r.d.entries != null ? '  進場 ' + r.d.entries + ' 筆' : '') +
        (r.d.win_rate != null ? '  勝率 ' + Math.round(r.d.win_rate * 100) + '%' : '') + (r.d.provisional ? '  （今日尚未結算）' : '');
      g += '<rect x="' + (cx(i) - bw / 2).toFixed(1) + '" y="' + top.toFixed(1) + '" width="' + bw.toFixed(1) + '" height="' + hgt.toFixed(1) +
           '" fill="' + col(p) + '" fill-opacity="' + (r.d.provisional ? 0.35 : 0.85) + '" rx="2"' +
           (r.d.provisional ? ' stroke="' + col(p) + '" stroke-dasharray="3 2"' : '') + '><title>' + tip + '</title></rect>';
      if (i % every === 0 || i === rows.length - 1) {
        g += '<text x="' + cx(i).toFixed(1) + '" y="' + (H - 10) + '" text-anchor="middle" font-size="15" fill="#6b7280">' + esc(String(r.d.date).slice(5)) + '</text>';
      }
    });
    var line = rows.map(function (r, i) { return cx(i).toFixed(1) + ',' + y(r.cum).toFixed(1); });
    g += '<polyline points="' + line.join(' ') + '" fill="none" stroke="' + GOLD + '" stroke-width="2" stroke-linejoin="round"/>';
    rows.forEach(function (r, i) {
      g += '<circle cx="' + cx(i).toFixed(1) + '" cy="' + y(r.cum).toFixed(1) + '" r="3" fill="' + GOLD + '"><title>' + esc(r.d.date) + ' 累計 ' + sgn(r.cum) + '</title></circle>';
    });
    return svg(W, H, g);
  }

  /* ── 對外：渲染 ─────────────────────────────── */
  function renderCapital(cap, label) {
    var tag = $('cap-date-tag'); if (tag) tag.textContent = label || '';
    var k = $('cap-kpis'), a = $('cap-day-chart'), b = $('cap-locked-chart');
    if (!k || !a || !b) return;
    if (!cap || cap.initial == null) {
      k.innerHTML = ''; a.innerHTML = empty('這一天沒有資金資料（資金池功能啟用前的日期，或今日尚未開始分析）'); b.innerHTML = '';
      return;
    }
    var ret = cap.initial ? (cap.equity - cap.initial) / cap.initial * 100 : 0, d = cap.equity - cap.initial;
    k.innerHTML =
      kpi('當日本金', fmt(cap.initial)) +
      kpi('可用資金', fmt(cap.cash), '#8db3ff', '佔用 ' + fmt(cap.locked)) +
      kpi('權益（總資金）', fmt(cap.equity), col(d), sgn(d) + '（' + (ret >= 0 ? '+' : '') + ret.toFixed(2) + '%）') +
      kpi('已實現損益', sgn(cap.realized), col(cap.realized), '已扣手續費與證交稅') +
      kpi('未實現損益', sgn(cap.unrealized), col(cap.unrealized), '持倉試算') +
      kpi('資金運用', (cap.initial ? Math.round(cap.locked / cap.initial * 100) : 0) + '%', null, '目前佔用比例');
    a.innerHTML = dayChart(cap);
    b.innerHTML = lockedChart(cap);
  }

  function renderDaily(history, todayCap, todayStr) {
    var days = ((history || []).filter(function (d) { return d && d.date; })).slice();
    if (todayCap && todayCap.initial != null && todayStr && !days.some(function (d) { return d.date === todayStr; })) {
      days.push({date: todayStr, pnl: todayCap.equity - todayCap.initial,
                 pnl_pct: todayCap.initial ? (todayCap.equity - todayCap.initial) / todayCap.initial * 100 : 0, provisional: true});
    }
    days.sort(function (p, q) { return p.date < q.date ? -1 : 1; });
    var kp = $('cap-daily-kpis'), ch = $('cap-daily-chart'); if (!kp || !ch) return;
    var tot = 0, winD = 0, worst = 0, best = 0;
    days.forEach(function (d) { var p = Number(d.pnl) || 0; tot += p; if (p > 0) winD++; worst = Math.min(worst, p); best = Math.max(best, p); });
    kp.innerHTML = days.length ?
      kpi('累計損益', sgn(tot), col(tot), days.length + ' 個交易日') +
      kpi('賺錢天數', winD + ' / ' + days.length, null, Math.round(winD / days.length * 100) + '%') +
      kpi('平均每日', sgn(tot / days.length), col(tot)) +
      kpi('單日最大虧損', sgn(worst), col(worst), '最佳 ' + sgn(best)) : '';
    ch.innerHTML = dailyChart(days);
  }

  if (typeof window !== 'undefined') { window.renderCapital = renderCapital; window.renderDailyCapital = renderDaily; }

  function init() {
    var ED = (typeof window !== 'undefined' && window.EXPORT_DATA) || (typeof EXPORT_DATA !== 'undefined' ? EXPORT_DATA : null);
    if (!ED) return;
    renderCapital(ED.capital, ED.today_str ? ED.today_str + '（今日）' : '');
    renderDaily(ED.capital_history, ED.capital, ED.today_str);
    /* 切換「查看日期」時，同步切換當日資金圖 */
    if (typeof window.onHistoryDateChange === 'function') {
      var orig = window.onHistoryDateChange;
      window.onHistoryDateChange = async function (value) {
        try { await orig(value); } finally {
          if (value === '__today__') { renderCapital(ED.capital, ED.today_str + '（今日）'); return; }
          try {
            var r = await fetch('history_records/analysis_' + value + '.json');
            var snap = r.ok ? await r.json() : null;
            renderCapital(snap && snap.capital, value);
          } catch (e) { renderCapital(null, value); }
        }
      };
    }
  }
  if (typeof document !== 'undefined') { init(); }
  if (typeof module !== 'undefined') { module.exports = {dayChart: dayChart, lockedChart: lockedChart, dailyChart: dailyChart, renderCapital: renderCapital, renderDaily: renderDaily}; }
})();
"""
