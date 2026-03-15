from flask import Flask, jsonify, render_template_string, request
from bot import state, get_prices, get_portfolio_value, PAPER_BALANCE, COIN_SYMBOLS, COINS
from datetime import datetime

app = Flask(__name__)

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>AI Trading Bot — Command Center</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
  :root {
    --bg0:       #060d1a;
    --bg1:       #0a1628;
    --bg2:       #0f1f38;
    --bg3:       #162844;
    --border:    #1e3a5f;
    --accent:    #00a8ff;
    --accent2:   #0066cc;
    --green:     #00e676;
    --red:       #ff1744;
    --yellow:    #ffd600;
    --text:      #c8d8f0;
    --text-dim:  #5a7a9a;
    --text-bright:#e8f4ff;
    --mono:      'IBM Plex Mono', monospace;
    --sans:      'IBM Plex Sans', sans-serif;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  html { font-size:14px; }
  body { background:var(--bg0); color:var(--text); font-family:var(--sans); min-height:100vh; overflow-x:hidden; }

  /* Scanline effect */
  body::before {
    content:''; position:fixed; inset:0; pointer-events:none; z-index:1000;
    background:repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,0.03) 2px, rgba(0,0,0,0.03) 4px);
  }

  /* Header */
  .header {
    background:var(--bg1); border-bottom:1px solid var(--border);
    padding:0 24px; display:flex; align-items:center; justify-content:space-between;
    height:56px; position:sticky; top:0; z-index:100;
  }
  .header-left { display:flex; align-items:center; gap:16px; }
  .logo { font-family:var(--mono); font-size:1.1rem; font-weight:600; color:var(--accent); letter-spacing:2px; }
  .logo span { color:var(--text-dim); font-weight:300; }
  .mode-badge {
    background:rgba(0,168,255,0.1); border:1px solid var(--accent2);
    color:var(--accent); font-family:var(--mono); font-size:0.7rem;
    padding:3px 10px; border-radius:2px; letter-spacing:1px;
  }
  .header-right { display:flex; align-items:center; gap:20px; }
  .status-dot { width:8px; height:8px; border-radius:50%; background:var(--green); box-shadow:0 0 8px var(--green); animation:pulse 2s infinite; }
  .status-dot.paused { background:var(--red); box-shadow:0 0 8px var(--red); }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
  .last-update { font-family:var(--mono); font-size:0.72rem; color:var(--text-dim); }
  .clock { font-family:var(--mono); font-size:0.85rem; color:var(--accent); }

  /* Navigation */
  .nav { background:var(--bg1); border-bottom:1px solid var(--border); display:flex; padding:0 24px; }
  .nav-tab {
    padding:12px 20px; font-size:0.8rem; font-weight:500; color:var(--text-dim);
    cursor:pointer; border-bottom:2px solid transparent; letter-spacing:0.5px;
    transition:all 0.2s; text-transform:uppercase;
  }
  .nav-tab:hover { color:var(--text); }
  .nav-tab.active { color:var(--accent); border-bottom-color:var(--accent); }

  /* Pages */
  .page { display:none; padding:20px 24px; }
  .page.active { display:block; }

  /* Grid */
  .grid { display:grid; gap:16px; }
  .grid-4 { grid-template-columns:repeat(4,1fr); }
  .grid-3 { grid-template-columns:repeat(3,1fr); }
  .grid-2 { grid-template-columns:repeat(2,1fr); }
  .grid-2-1 { grid-template-columns:2fr 1fr; }
  .grid-3-2 { grid-template-columns:3fr 2fr; }
  @media(max-width:1200px){.grid-4{grid-template-columns:repeat(2,1fr)}}
  @media(max-width:768px){.grid-4,.grid-3,.grid-2,.grid-2-1,.grid-3-2{grid-template-columns:1fr}}

  /* Cards */
  .card {
    background:var(--bg1); border:1px solid var(--border); border-radius:4px;
    padding:16px; position:relative; overflow:hidden;
  }
  .card::before {
    content:''; position:absolute; top:0; left:0; right:0; height:2px;
    background:linear-gradient(90deg, var(--accent2), var(--accent), transparent);
  }
  .card-title {
    font-family:var(--mono); font-size:0.68rem; font-weight:500;
    color:var(--text-dim); letter-spacing:1.5px; text-transform:uppercase;
    margin-bottom:10px; display:flex; align-items:center; gap:8px;
  }
  .card-title .dot { width:6px; height:6px; border-radius:50%; background:var(--accent); }

  /* KPI tiles */
  .kpi-value { font-family:var(--mono); font-size:1.8rem; font-weight:600; color:var(--text-bright); line-height:1; }
  .kpi-sub { font-family:var(--mono); font-size:0.72rem; color:var(--text-dim); margin-top:6px; }
  .kpi-change { font-family:var(--mono); font-size:0.85rem; margin-top:4px; }
  .up { color:var(--green); }
  .down { color:var(--red); }
  .neutral { color:var(--text-dim); }

  /* Coin ticker */
  .ticker-row {
    display:flex; align-items:center; justify-content:space-between;
    padding:10px 0; border-bottom:1px solid var(--border);
  }
  .ticker-row:last-child { border-bottom:none; }
  .ticker-symbol { font-family:var(--mono); font-weight:600; font-size:0.9rem; color:var(--text-bright); width:60px; }
  .ticker-name { color:var(--text-dim); font-size:0.8rem; flex:1; }
  .ticker-price { font-family:var(--mono); font-size:0.9rem; color:var(--text-bright); text-align:right; min-width:100px; }
  .ticker-changes { display:flex; gap:12px; min-width:160px; justify-content:flex-end; }
  .ticker-change { font-family:var(--mono); font-size:0.75rem; min-width:60px; text-align:right; }

  /* Chart */
  .chart-wrap { position:relative; height:220px; }

  /* Table */
  .data-table { width:100%; border-collapse:collapse; font-family:var(--mono); font-size:0.78rem; }
  .data-table th {
    text-align:left; padding:8px 12px; color:var(--text-dim);
    font-size:0.68rem; letter-spacing:1px; text-transform:uppercase;
    border-bottom:1px solid var(--border); background:var(--bg2);
  }
  .data-table td { padding:10px 12px; border-bottom:1px solid rgba(30,58,95,0.5); color:var(--text); }
  .data-table tr:hover td { background:rgba(0,168,255,0.04); }
  .data-table .empty { text-align:center; color:var(--text-dim); padding:30px; font-size:0.8rem; }

  /* AI Log */
  .ai-entry {
    padding:12px; border-left:3px solid var(--border);
    margin-bottom:10px; background:var(--bg2); border-radius:0 4px 4px 0;
    transition:border-color 0.2s;
  }
  .ai-entry.buy  { border-left-color:var(--green); }
  .ai-entry.sell { border-left-color:var(--red); }
  .ai-entry.hold { border-left-color:var(--text-dim); }
  .ai-entry-header { display:flex; align-items:center; gap:10px; margin-bottom:6px; flex-wrap:wrap; }
  .ai-action { font-weight:700; font-size:0.8rem; font-family:var(--mono); padding:2px 8px; border-radius:2px; }
  .ai-action.buy  { background:rgba(0,230,118,0.15); color:var(--green); }
  .ai-action.sell { background:rgba(255,23,68,0.15);  color:var(--red); }
  .ai-action.hold { background:rgba(90,122,154,0.2);  color:var(--text-dim); }
  .ai-coin { font-family:var(--mono); font-weight:600; font-size:0.85rem; color:var(--accent); }
  .ai-conf { font-family:var(--mono); font-size:0.75rem; color:var(--text-dim); }
  .ai-time { font-family:var(--mono); font-size:0.7rem; color:var(--text-dim); margin-left:auto; }
  .ai-reasoning { font-size:0.8rem; color:var(--text); line-height:1.5; margin-bottom:4px; }
  .ai-news { font-size:0.75rem; color:var(--text-dim); font-style:italic; }
  .sentiment-badge {
    font-size:0.68rem; font-family:var(--mono); padding:2px 7px;
    border-radius:2px; letter-spacing:0.5px;
  }
  .sentiment-badge.bullish { background:rgba(0,230,118,0.1); color:var(--green); }
  .sentiment-badge.bearish { background:rgba(255,23,68,0.1); color:var(--red); }
  .sentiment-badge.neutral { background:rgba(90,122,154,0.15); color:var(--text-dim); }

  /* News feed */
  .news-item {
    padding:12px 0; border-bottom:1px solid var(--border);
    display:flex; gap:12px; align-items:flex-start;
  }
  .news-item:last-child { border-bottom:none; }
  .news-coin-tag {
    font-family:var(--mono); font-size:0.68rem; font-weight:600;
    padding:2px 8px; border-radius:2px; background:rgba(0,168,255,0.1);
    color:var(--accent); min-width:44px; text-align:center; flex-shrink:0; margin-top:2px;
  }
  .news-content { flex:1; }
  .news-title { font-size:0.82rem; color:var(--text); line-height:1.4; }
  .news-title a { color:var(--text); text-decoration:none; }
  .news-title a:hover { color:var(--accent); }
  .news-meta { font-family:var(--mono); font-size:0.7rem; color:var(--text-dim); margin-top:4px; }

  /* Controls */
  .controls-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px; }
  .btn {
    padding:12px 20px; border:1px solid var(--border); background:var(--bg2);
    color:var(--text); font-family:var(--mono); font-size:0.8rem; letter-spacing:1px;
    cursor:pointer; border-radius:3px; transition:all 0.2s; text-transform:uppercase;
    display:flex; align-items:center; justify-content:center; gap:8px;
  }
  .btn:hover { border-color:var(--accent); color:var(--accent); background:rgba(0,168,255,0.06); }
  .btn.danger:hover { border-color:var(--red); color:var(--red); background:rgba(255,23,68,0.06); }
  .btn.success:hover { border-color:var(--green); color:var(--green); background:rgba(0,230,118,0.06); }
  .btn:active { transform:scale(0.98); }

  .risk-slider-wrap { margin-top:8px; }
  .risk-slider {
    width:100%; -webkit-appearance:none; height:4px;
    background:var(--border); border-radius:2px; outline:none;
  }
  .risk-slider::-webkit-slider-thumb {
    -webkit-appearance:none; width:16px; height:16px;
    border-radius:50%; background:var(--accent); cursor:pointer;
    box-shadow:0 0 6px var(--accent);
  }
  .risk-value { font-family:var(--mono); font-size:1.2rem; color:var(--accent); margin-top:8px; }

  /* Scrollable panels */
  .scroll-panel { max-height:420px; overflow-y:auto; }
  .scroll-panel::-webkit-scrollbar { width:4px; }
  .scroll-panel::-webkit-scrollbar-track { background:var(--bg0); }
  .scroll-panel::-webkit-scrollbar-thumb { background:var(--border); border-radius:2px; }

  /* Toast */
  .toast {
    position:fixed; bottom:24px; right:24px; background:var(--bg2);
    border:1px solid var(--accent); color:var(--accent); font-family:var(--mono);
    font-size:0.8rem; padding:12px 20px; border-radius:4px; z-index:9999;
    transform:translateY(100px); opacity:0; transition:all 0.3s;
  }
  .toast.show { transform:translateY(0); opacity:1; }

  /* Confidence bar */
  .conf-bar-wrap { display:flex; align-items:center; gap:8px; }
  .conf-bar { flex:1; height:4px; background:var(--border); border-radius:2px; overflow:hidden; }
  .conf-bar-fill { height:100%; border-radius:2px; transition:width 0.5s; }
  .conf-label { font-family:var(--mono); font-size:0.72rem; color:var(--text-dim); min-width:36px; text-align:right; }

  /* Section label */
  .section-label {
    font-family:var(--mono); font-size:0.68rem; color:var(--text-dim);
    letter-spacing:2px; text-transform:uppercase;
    padding:12px 0 8px; border-bottom:1px solid var(--border); margin-bottom:16px;
  }

  /* PnL badge in table */
  .pnl-pos { color:var(--green); }
  .pnl-neg { color:var(--red); }

  /* Mini sparkline in ticker */
  .sparkline { display:inline-block; vertical-align:middle; }
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <div class="logo">AI<span>/</span>TRADE <span style="font-size:0.7rem;color:var(--text-dim)">v1.0</span></div>
    <div class="mode-badge">PAPER MODE</div>
  </div>
  <div class="header-right">
    <div class="last-update" id="lastUpdate">Refreshing...</div>
    <div id="statusDot" class="status-dot"></div>
    <div class="clock" id="clock">--:--:--</div>
  </div>
</div>

<div class="nav">
  <div class="nav-tab active" onclick="switchTab('dashboard')">Dashboard</div>
  <div class="nav-tab" onclick="switchTab('trades')">Trades</div>
  <div class="nav-tab" onclick="switchTab('ai-log')">AI Brain</div>
  <div class="nav-tab" onclick="switchTab('news')">News Feed</div>
  <div class="nav-tab" onclick="switchTab('settings')">Settings</div>
</div>

<!-- ═══ DASHBOARD PAGE ═══ -->
<div id="page-dashboard" class="page active">

  <div class="grid grid-4" style="margin-bottom:16px">
    <div class="card">
      <div class="card-title"><span class="dot"></span>Portfolio Value</div>
      <div class="kpi-value" id="portfolioVal">$--</div>
      <div class="kpi-change" id="portfolioRoi">--</div>
      <div class="kpi-sub">Started at $10,000.00</div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Total PnL</div>
      <div class="kpi-value" id="totalPnl">$--</div>
      <div class="kpi-change" id="dailyPnl">Today: --</div>
      <div class="kpi-sub">All realized trades</div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Win Rate</div>
      <div class="kpi-value" id="winRate">--%</div>
      <div class="kpi-change" id="winsLosses">-- W / -- L</div>
      <div class="kpi-sub">Total trades: <span id="totalTrades">0</span></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Open Positions</div>
      <div class="kpi-value" id="openCount">--</div>
      <div class="kpi-change" id="cashBalance">Cash: $--</div>
      <div class="kpi-sub">Last scan: <span id="lastScan">--</span></div>
    </div>
  </div>

  <div class="grid grid-2-1" style="margin-bottom:16px">
    <div class="card">
      <div class="card-title"><span class="dot"></span>Portfolio Performance</div>
      <div class="chart-wrap">
        <canvas id="portfolioChart"></canvas>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Coin Prices</div>
      <div id="tickerList"></div>
    </div>
  </div>

  <div class="grid grid-2">
    <div class="card">
      <div class="card-title"><span class="dot"></span>Open Trades</div>
      <div class="scroll-panel">
        <table class="data-table" id="openTradesTable">
          <thead><tr><th>Coin</th><th>Entry</th><th>Current</th><th>Qty</th><th>Unrealised PnL</th><th>Since</th></tr></thead>
          <tbody id="openTradesTbody"><tr><td colspan="6" class="empty">No open trades</td></tr></tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Latest AI Decisions</div>
      <div class="scroll-panel" id="miniAiLog"></div>
    </div>
  </div>

</div>

<!-- ═══ TRADES PAGE ═══ -->
<div id="page-trades" class="page">
  <div class="card">
    <div class="card-title"><span class="dot"></span>Trade History</div>
    <div class="scroll-panel" style="max-height:600px">
      <table class="data-table">
        <thead><tr><th>Coin</th><th>Entry Price</th><th>Exit Price</th><th>Qty</th><th>Invested</th><th>PnL</th><th>PnL %</th><th>Entry Time</th><th>Exit Time</th></tr></thead>
        <tbody id="tradeHistoryTbody"><tr><td colspan="9" class="empty">No completed trades yet</td></tr></tbody>
      </table>
    </div>
  </div>
</div>

<!-- ═══ AI LOG PAGE ═══ -->
<div id="page-ai-log" class="page">
  <div class="grid grid-2-1">
    <div class="card">
      <div class="card-title"><span class="dot"></span>AI Decision Log</div>
      <div class="scroll-panel" id="fullAiLog" style="max-height:600px"></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Signal Distribution</div>
      <div class="chart-wrap" style="height:200px">
        <canvas id="signalChart"></canvas>
      </div>
      <div style="margin-top:16px">
        <div class="card-title"><span class="dot"></span>Avg Confidence by Action</div>
        <div id="confStats" style="padding-top:8px"></div>
      </div>
    </div>
  </div>
</div>

<!-- ═══ NEWS PAGE ═══ -->
<div id="page-news" class="page">
  <div class="grid grid-2">
    <div class="card">
      <div class="card-title"><span class="dot"></span>Latest News Feed</div>
      <div class="scroll-panel" id="newsPanel" style="max-height:600px"></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>News by Coin</div>
      <div id="newsByCoins"></div>
    </div>
  </div>
</div>

<!-- ═══ SETTINGS PAGE ═══ -->
<div id="page-settings" class="page">
  <div class="grid grid-2">
    <div class="card">
      <div class="card-title"><span class="dot"></span>Bot Controls</div>
      <div class="controls-grid">
        <button class="btn success" onclick="botControl('resume')">▶ Resume Bot</button>
        <button class="btn danger"  onclick="botControl('pause')">⏸ Pause Bot</button>
        <button class="btn danger"  onclick="botControl('forcesell')">⚡ Force Sell All</button>
        <button class="btn"         onclick="fetchAll()">↻ Refresh Now</button>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Risk Per Trade</div>
      <div class="risk-value" id="riskDisplay">10%</div>
      <div class="risk-slider-wrap">
        <input type="range" class="risk-slider" id="riskSlider" min="1" max="25" value="10"
               oninput="updateRisk(this.value)"/>
      </div>
      <div class="kpi-sub" style="margin-top:8px">% of cash balance risked per trade</div>
      <button class="btn" style="margin-top:12px;width:100%" onclick="saveRisk()">Save Risk Setting</button>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Bot Stats</div>
      <div id="botStats" style="font-family:var(--mono);font-size:0.8rem;line-height:2"></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Tracked Coins</div>
      <div style="display:flex;flex-wrap:wrap;gap:8px;padding-top:4px" id="coinBadges"></div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let portfolioChartObj = null;
let signalChartObj    = null;
let autoRefreshTimer  = null;
let currentData       = {};

// ── Clock ──────────────────────────────────────────────
function updateClock() {
  document.getElementById('clock').textContent = new Date().toUTCString().slice(17,25) + ' UTC';
}
setInterval(updateClock, 1000);
updateClock();

// ── Tab switching ──────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('page-' + name).classList.add('active');
}

// ── Toast ──────────────────────────────────────────────
function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3000);
}

// ── Fetch all data ─────────────────────────────────────
async function fetchAll() {
  try {
    const r = await fetch('/api/state');
    currentData = await r.json();
    renderAll(currentData);
    document.getElementById('lastUpdate').textContent = 'Updated ' + new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('lastUpdate').textContent = 'Update failed';
  }
}

// ── Render everything ──────────────────────────────────
function renderAll(d) {
  renderKPIs(d);
  renderTicker(d);
  renderPortfolioChart(d);
  renderOpenTrades(d);
  renderMiniAiLog(d);
  renderFullAiLog(d);
  renderTradeHistory(d);
  renderNews(d);
  renderSignalChart(d);
  renderSettings(d);

  // Status dot
  const dot = document.getElementById('statusDot');
  dot.className = 'status-dot' + (d.paused ? ' paused' : '');
}

function fmt(n, decimals=2) {
  return n === undefined || n === null ? '--' : Number(n).toLocaleString('en-US', {minimumFractionDigits:decimals,maximumFractionDigits:decimals});
}
function pnlClass(n) { return n > 0 ? 'pnl-pos' : n < 0 ? 'pnl-neg' : 'neutral'; }
function pnlSign(n)  { return n > 0 ? '+' : ''; }

// ── KPIs ───────────────────────────────────────────────
function renderKPIs(d) {
  const pv   = d.portfolio_value || 0;
  const roi  = ((pv - 10000) / 10000) * 100;
  const wins = d.wins || 0, losses = d.losses || 0;
  const wr   = (wins + losses > 0) ? (wins / (wins + losses) * 100) : 0;

  document.getElementById('portfolioVal').textContent = '$' + fmt(pv);
  const roiEl = document.getElementById('portfolioRoi');
  roiEl.textContent = (roi >= 0 ? '+' : '') + fmt(roi) + '% ROI';
  roiEl.className = 'kpi-change ' + (roi >= 0 ? 'up' : 'down');

  const pnl = d.total_pnl || 0;
  const pnlEl = document.getElementById('totalPnl');
  pnlEl.textContent = (pnl >= 0 ? '+$' : '-$') + fmt(Math.abs(pnl));
  pnlEl.className = 'kpi-value ' + (pnl >= 0 ? 'up' : 'down');

  const dpnl = d.daily_pnl || 0;
  const dpEl = document.getElementById('dailyPnl');
  dpEl.textContent = 'Today: ' + (dpnl >= 0 ? '+$' : '-$') + fmt(Math.abs(dpnl));
  dpEl.className = 'kpi-change ' + (dpnl >= 0 ? 'up' : 'down');

  document.getElementById('winRate').textContent = fmt(wr, 1) + '%';
  document.getElementById('winsLosses').textContent = wins + ' W / ' + losses + ' L';
  document.getElementById('totalTrades').textContent = wins + losses;

  document.getElementById('openCount').textContent = Object.keys(d.open_trades || {}).length;
  const cashEl = document.getElementById('cashBalance');
  cashEl.textContent = 'Cash: $' + fmt(d.cash_balance || 0);

  const ls = d.last_scan;
  document.getElementById('lastScan').textContent = ls ? ls.slice(11,16) + ' UTC' : 'Not yet';
}

// ── Ticker ─────────────────────────────────────────────
function renderTicker(d) {
  const prices = d.prices || {};
  const html = Object.entries(prices).map(([id, p]) => {
    const c1  = p.change_1h || 0;
    const c24 = p.change_24h || 0;
    const c1c = c1 >= 0 ? 'up' : 'down';
    const c24c= c24>= 0 ? 'up' : 'down';
    const isOpen = d.open_trades && d.open_trades[id];
    const dot = isOpen ? '<span style="color:var(--green);font-size:0.6rem">●</span> ' : '';
    return `<div class="ticker-row">
      <div class="ticker-symbol">${dot}${p.symbol}</div>
      <div class="ticker-name">${p.name}</div>
      <div class="ticker-price">$${p.price > 1 ? fmt(p.price) : p.price.toFixed(8)}</div>
      <div class="ticker-changes">
        <span class="ticker-change ${c1c}">${pnlSign(c1)}${fmt(c1,2)}%<span style="font-size:0.65rem;color:var(--text-dim)"> 1H</span></span>
        <span class="ticker-change ${c24c}">${pnlSign(c24)}${fmt(c24,2)}%<span style="font-size:0.65rem;color:var(--text-dim)"> 24H</span></span>
      </div>
    </div>`;
  }).join('');
  document.getElementById('tickerList').innerHTML = html || '<div style="color:var(--text-dim);padding:20px;text-align:center">Loading prices...</div>';
}

// ── Portfolio Chart ─────────────────────────────────────
function renderPortfolioChart(d) {
  const history = d.portfolio_history || [];
  const labels  = history.map(h => h.time);
  const values  = history.map(h => h.value);

  if (!portfolioChartObj) {
    const ctx = document.getElementById('portfolioChart').getContext('2d');
    portfolioChartObj = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          label: 'Portfolio Value',
          data: values,
          borderColor: '#00a8ff',
          backgroundColor: 'rgba(0,168,255,0.08)',
          borderWidth: 2,
          pointRadius: 0,
          pointHoverRadius: 4,
          fill: true,
          tension: 0.3,
        }, {
          label: 'Start ($10,000)',
          data: labels.map(() => 10000),
          borderColor: 'rgba(90,122,154,0.4)',
          borderWidth: 1,
          borderDash: [4,4],
          pointRadius: 0,
          fill: false,
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { color:'rgba(30,58,95,0.5)' }, ticks: { color:'#5a7a9a', maxTicksLimit:8, font:{family:'IBM Plex Mono',size:10} } },
          y: { grid: { color:'rgba(30,58,95,0.5)' }, ticks: { color:'#5a7a9a', font:{family:'IBM Plex Mono',size:10}, callback: v => '$'+v.toLocaleString() } }
        }
      }
    });
  } else {
    portfolioChartObj.data.labels = labels;
    portfolioChartObj.data.datasets[0].data = values;
    portfolioChartObj.data.datasets[1].data = labels.map(() => 10000);
    portfolioChartObj.update('none');
  }
}

// ── Open Trades Table ───────────────────────────────────
function renderOpenTrades(d) {
  const trades = d.open_trades || {};
  const prices = d.prices || {};
  const tbody  = document.getElementById('openTradesTbody');
  const entries = Object.entries(trades);
  if (!entries.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">No open trades</td></tr>';
    return;
  }
  tbody.innerHTML = entries.map(([id, t]) => {
    const cur = prices[id] ? prices[id].price : t.entry_price;
    const unr = (cur - t.entry_price) * t.qty;
    const pc  = pnlClass(unr);
    const since = t.entry_time ? t.entry_time.slice(11,16) + ' UTC' : '--';
    return `<tr>
      <td><b style="color:var(--accent)">${t.coin}</b></td>
      <td>$${t.entry_price > 1 ? fmt(t.entry_price,4) : t.entry_price.toFixed(8)}</td>
      <td>$${cur > 1 ? fmt(cur,4) : cur.toFixed(8)}</td>
      <td>${t.qty.toFixed(6)}</td>
      <td class="${pc}">${pnlSign(unr)}$${fmt(Math.abs(unr),4)}</td>
      <td style="color:var(--text-dim)">${since}</td>
    </tr>`;
  }).join('');
}

// ── Mini AI Log (dashboard) ─────────────────────────────
function renderMiniAiLog(d) {
  const log  = (d.ai_log || []).slice(0, 8);
  const html = log.map(e => {
    const ac = e.action.toLowerCase();
    const sc = e.sentiment || 'neutral';
    return `<div class="ai-entry ${ac}" style="margin-bottom:8px;padding:10px">
      <div class="ai-entry-header">
        <span class="ai-action ${ac}">${e.action}</span>
        <span class="ai-coin">${e.coin}</span>
        <span class="ai-conf">${e.confidence}%</span>
        <span class="sentiment-badge ${sc}">${sc}</span>
        <span class="ai-time">${(e.time||'').slice(11,16)}</span>
      </div>
      <div class="ai-news" style="font-size:0.72rem">${e.key_news || ''}</div>
    </div>`;
  }).join('') || '<div style="color:var(--text-dim);padding:20px;text-align:center;font-size:0.8rem">Waiting for first scan...</div>';
  document.getElementById('miniAiLog').innerHTML = html;
}

// ── Full AI Log page ────────────────────────────────────
function renderFullAiLog(d) {
  const log  = d.ai_log || [];
  const html = log.map(e => {
    const ac = e.action.toLowerCase();
    const sc = e.sentiment || 'neutral';
    const conf = e.confidence || 0;
    const barColor = conf >= 80 ? 'var(--green)' : conf >= 60 ? 'var(--accent)' : 'var(--yellow)';
    return `<div class="ai-entry ${ac}">
      <div class="ai-entry-header">
        <span class="ai-action ${ac}">${e.action}</span>
        <span class="ai-coin">${e.coin}</span>
        <span class="sentiment-badge ${sc}">${sc}</span>
        <span style="font-family:var(--mono);font-size:0.72rem;color:var(--text-dim)">risk: ${e.risk_level||'--'}</span>
        <span class="ai-time">${(e.time||'').slice(0,16).replace('T',' ')} UTC</span>
      </div>
      <div class="conf-bar-wrap" style="margin-bottom:8px">
        <div class="conf-bar"><div class="conf-bar-fill" style="width:${conf}%;background:${barColor}"></div></div>
        <span class="conf-label">${conf}%</span>
      </div>
      ${e.market_summary ? `<div style="font-size:0.75rem;color:var(--accent);margin-bottom:6px;font-style:italic">${e.market_summary}</div>` : ''}
      <div class="ai-reasoning">${e.reasoning || ''}</div>
      ${e.key_news ? `<div class="ai-news">📰 ${e.key_news}</div>` : ''}
    </div>`;
  }).join('') || '<div style="color:var(--text-dim);padding:40px;text-align:center">No AI decisions yet. Bot will analyze every 15 minutes.</div>';
  document.getElementById('fullAiLog').innerHTML = html;
}

// ── Trade History ───────────────────────────────────────
function renderTradeHistory(d) {
  const trades = d.trade_history || [];
  const tbody  = document.getElementById('tradeHistoryTbody');
  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="empty">No completed trades yet</td></tr>';
    return;
  }
  tbody.innerHTML = trades.map(t => {
    const pc  = pnlClass(t.pnl);
    const ppc = pnlClass(t.pnl_pct);
    return `<tr>
      <td><b style="color:var(--accent)">${t.symbol}</b></td>
      <td>$${t.entry_price > 1 ? fmt(t.entry_price,4) : t.entry_price.toFixed(8)}</td>
      <td>$${t.exit_price > 1 ? fmt(t.exit_price,4) : t.exit_price.toFixed(8)}</td>
      <td>${Number(t.qty).toFixed(6)}</td>
      <td>$${fmt(t.usdt_spent,2)}</td>
      <td class="${pc}">${pnlSign(t.pnl)}$${fmt(Math.abs(t.pnl),4)}</td>
      <td class="${ppc}">${pnlSign(t.pnl_pct)}${fmt(t.pnl_pct,2)}%</td>
      <td style="color:var(--text-dim)">${(t.entry_time||'').slice(0,16).replace('T',' ')}</td>
      <td style="color:var(--text-dim)">${(t.exit_time||'').slice(0,16).replace('T',' ')}</td>
    </tr>`;
  }).join('');
}

// ── News ────────────────────────────────────────────────
function renderNews(d) {
  const news = d.news_feed || [];
  const html = news.map(n => `
    <div class="news-item">
      <div class="news-coin-tag">${n.coin || '--'}</div>
      <div class="news-content">
        <div class="news-title"><a href="${n.url||'#'}" target="_blank">${n.title}</a></div>
        <div class="news-meta">${n.source} ${n.time ? '· ' + n.time.slice(0,10) : ''}</div>
      </div>
    </div>`).join('') || '<div style="color:var(--text-dim);padding:30px;text-align:center">News will appear after first scan</div>';
  document.getElementById('newsPanel').innerHTML = html;

  // By coin
  const coins = ['BTC','ETH','SOL','DOGE','SHIB'];
  const byCoins = coins.map(c => {
    const items = news.filter(n => n.coin === c).slice(0,3);
    if (!items.length) return '';
    return `<div style="margin-bottom:16px">
      <div style="font-family:var(--mono);font-size:0.75rem;color:var(--accent);margin-bottom:8px;letter-spacing:1px">${c}</div>
      ${items.map(n => `<div style="font-size:0.78rem;color:var(--text);margin-bottom:6px;padding-left:8px;border-left:2px solid var(--border)">
        <a href="${n.url||'#'}" target="_blank" style="color:var(--text);text-decoration:none">${n.title}</a>
        <div style="font-size:0.68rem;color:var(--text-dim);margin-top:2px">${n.source}</div>
      </div>`).join('')}
    </div>`;
  }).join('');
  document.getElementById('newsByCoins').innerHTML = byCoins || '<div style="color:var(--text-dim);padding:30px;text-align:center">Loading...</div>';
}

// ── Signal Chart ────────────────────────────────────────
function renderSignalChart(d) {
  const log   = d.ai_log || [];
  const buys  = log.filter(e => e.action === 'BUY').length;
  const sells = log.filter(e => e.action === 'SELL').length;
  const holds = log.filter(e => e.action === 'HOLD').length;

  if (!signalChartObj) {
    const ctx = document.getElementById('signalChart').getContext('2d');
    signalChartObj = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: ['BUY', 'SELL', 'HOLD'],
        datasets: [{ data: [buys, sells, holds], backgroundColor: ['rgba(0,230,118,0.7)', 'rgba(255,23,68,0.7)', 'rgba(90,122,154,0.4)'], borderWidth: 0 }]
      },
      options: {
        responsive: true, maintainAspectRatio: false, cutout: '70%',
        plugins: { legend: { labels: { color: '#5a7a9a', font:{family:'IBM Plex Mono',size:11} } } }
      }
    });
  } else {
    signalChartObj.data.datasets[0].data = [buys, sells, holds];
    signalChartObj.update();
  }

  // Avg confidence stats
  const buyConf  = log.filter(e=>e.action==='BUY').map(e=>e.confidence);
  const sellConf = log.filter(e=>e.action==='SELL').map(e=>e.confidence);
  const avg = arr => arr.length ? (arr.reduce((a,b)=>a+b,0)/arr.length).toFixed(1) : '--';
  document.getElementById('confStats').innerHTML = `
    <div style="font-family:var(--mono);font-size:0.8rem;line-height:2.2">
      <span style="color:var(--green)">BUY</span> avg confidence: <b style="color:var(--text-bright)">${avg(buyConf)}%</b><br>
      <span style="color:var(--red)">SELL</span> avg confidence: <b style="color:var(--text-bright)">${avg(sellConf)}%</b><br>
      Total decisions logged: <b style="color:var(--accent)">${log.length}</b>
    </div>`;
}

// ── Settings ────────────────────────────────────────────
function renderSettings(d) {
  document.getElementById('botStats').innerHTML = `
    Bot running since: <b style="color:var(--accent)">${(d.start_time||'').slice(0,10)}</b><br>
    Last scan: <b style="color:var(--accent)">${(d.last_scan||'Not yet').slice(0,16).replace('T',' ')}</b><br>
    Status: <b style="${d.paused ? 'color:var(--red)' : 'color:var(--green)'}">${d.paused ? 'PAUSED' : 'RUNNING'}</b><br>
    Open trades: <b style="color:var(--accent)">${Object.keys(d.open_trades||{}).length}</b><br>
    Trade history size: <b style="color:var(--accent)">${(d.trade_history||[]).length}</b>
  `;
  const coins = ['BTC','ETH','SOL','DOGE','SHIB'];
  document.getElementById('coinBadges').innerHTML = coins.map(c =>
    `<div style="font-family:var(--mono);font-size:0.75rem;padding:6px 14px;border:1px solid var(--border);border-radius:3px;color:var(--accent)">${c}</div>`
  ).join('');
  const risk = Math.round((d.risk_per_trade||0.1) * 100);
  document.getElementById('riskSlider').value = risk;
  document.getElementById('riskDisplay').textContent = risk + '%';
}

// ── Controls ────────────────────────────────────────────
async function botControl(action) {
  try {
    const r = await fetch('/api/control', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action})});
    const d = await r.json();
    showToast(d.message || 'Done');
    fetchAll();
  } catch(e) { showToast('Error: ' + e.message); }
}

function updateRisk(v) {
  document.getElementById('riskDisplay').textContent = v + '%';
}

async function saveRisk() {
  const v = document.getElementById('riskSlider').value;
  try {
    const r = await fetch('/api/control', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'set_risk', value: parseInt(v)})});
    const d = await r.json();
    showToast(d.message || 'Risk updated');
    fetchAll();
  } catch(e) { showToast('Error'); }
}

// ── Auto refresh every 30s ──────────────────────────────
fetchAll();
autoRefreshTimer = setInterval(fetchAll, 30000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/state")
def api_state():
    prices_data   = get_prices()
    portfolio_val = get_portfolio_value(prices_data)
    total         = state["wins"] + state["losses"]

    return jsonify({
        **state,
        "portfolio_value":  round(portfolio_val, 2),
        "prices":           prices_data,
        "total_trades":     total,
        "win_rate":         round(state["wins"] / total * 100, 1) if total > 0 else 0,
        "roi_pct":          round(((portfolio_val - PAPER_BALANCE) / PAPER_BALANCE) * 100, 2),
        "coin_symbols":     COIN_SYMBOLS,
    })


@app.route("/api/control", methods=["POST"])
def api_control():
    data   = request.get_json()
    action = data.get("action")

    if action == "pause":
        state["paused"] = True
        return jsonify({"message": "Bot paused"})

    elif action == "resume":
        state["paused"] = False
        return jsonify({"message": "Bot resumed"})

    elif action == "forcesell":
        prices_data = get_prices()
        closed = 0
        from bot import paper_sell
        for coin_id in list(state["open_trades"].keys()):
            price = prices_data.get(coin_id, {}).get("price", state["open_trades"][coin_id]["entry_price"])
            pnl   = paper_sell(coin_id, price)
            state["total_pnl"] += pnl
            closed += 1
        return jsonify({"message": f"Closed {closed} trade(s)"})

    elif action == "set_risk":
        val = data.get("value", 10)
        state["risk_per_trade"] = max(1, min(25, val)) / 100
        return jsonify({"message": f"Risk set to {val}%"})

    return jsonify({"message": "Unknown action"}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
