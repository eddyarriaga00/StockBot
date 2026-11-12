// StockBot front end: vanilla JS talking to the local API.
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

const state = {
  symbol: localGet("symbol") || "AAPL",
  range: "1d",
  chartType: "area",
  side: "buy",
  quote: null,
  watchlist: [],
  quotes: {},
  account: null,
  positions: [],
  settings: null,
  bot: null,
  view: "trade",
};

// ---------- helpers ----------
function localGet(k) { try { return localStorage.getItem("sb:" + k); } catch { return null; } }
function localSet(k, v) { try { localStorage.setItem("sb:" + k, v); } catch {} }

// On the public GitHub Pages site there is no local server. If the StockBot computer is online and you log in
// with its passcode, the site sends every call to it through its secure tunnel (full control). Otherwise the
// site reads the snapshot the bot publishes (docs/data/) and every control that would change something is off.
let SNAPSHOT = !/^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname) || new URLSearchParams(location.search).has("viewonly");
const REMOTE = { base: null, token: localGet("remote-token") };
const SNAPSHOT_RANGES = ["1d", "5d", "1mo", "1y", "5y"];
let core = null;
const symFiles = {};

async function snapshotApi(path, opts) {
  if ((opts.method || "GET") !== "GET") throw new Error("This is the view-only website. Trading and bot controls live in the StockBot app on its home computer.");
  if (!core) core = await fetch("data/core.json", { cache: "no-store" }).then((r) => r.json());
  const url = new URL(path, location.origin);
  const [route, arg] = url.pathname.replace(/^\/api\//, "").split(/\/(?=[^/]+$)/);
  const q = (k) => url.searchParams.get(k);
  const sym = (s) => (symFiles[s] ??= fetch(`data/sym/${encodeURIComponent(s)}.json`, { cache: "no-store" })
    .then((r) => { if (!r.ok) throw new Error(`${s} isn't on the published watchlist.`); return r.json(); }));
  const fixed = { account: core.account, equity: core.equity, watchlist: core.watchlist, clock: core.clock, bot: core.bot,
    tournament: core.tournament, settings: core.settings, performance: core.performance || null, track: core.track || null, activity: core.activity || [], strategies: core.strategies, "bot/backtest": core.backtest };
  const full = arg ? `${route}/${arg}` : route;
  if (full in fixed) return fixed[full];
  if (full === "tournament/curves") return core.tournament_curves;
  if (full === "orders") return core.orders[q("status") || "all"] || [];
  if (full === "quotes") return Object.fromEntries((q("symbols") || "").split(",").filter((s) => core.quotes[s]).map((s) => [s, core.quotes[s]]));
  if (full === "search") {
    const t = (q("q") || "").toLowerCase();
    return core.symbols.filter((x) => x.symbol.toLowerCase().startsWith(t) || x.name.toLowerCase().includes(t)).slice(0, 8);
  }
  if (route === "quote") return core.quotes[arg] || (await sym(arg)).quote;
  if (route === "news") return (await sym(arg)).news || [];
  if (route === "history") {
    const h = (await sym(arg)).history[q("range")];
    if (!h) throw new Error("That chart range isn't published.");
    return { gmtoffset: h.gmtoffset, candles: h.c.map(([time, open, high, low, close, volume]) => ({ time, open, high, low, close, volume })) };
  }
  throw new Error("Not available on the view-only website.");
}

async function api(path, opts = {}) {
  if (SNAPSHOT) return snapshotApi(path, opts);
  const headers = { "Content-Type": "application/json" };
  if (REMOTE.base) headers.Authorization = "Bearer " + REMOTE.token;
  let res;
  try {
    res = await fetch((REMOTE.base || "") + path, { ...opts, headers, body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined });
  } catch (e) {
    throw new Error(REMOTE.base ? "Can't reach the StockBot computer right now." : e.message);
  }
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && REMOTE.base) {
    localSet("remote-token", "");
    toast("Your login expired. Please enter the passcode again.", "bad");
    setTimeout(() => location.reload(), 1500);
  }
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

// ---------- website remote control ----------
async function connectRemote() {
  const base = core?.remote_url;
  const reachable = base && await fetch(base + "/api/remote/ping", { signal: AbortSignal.timeout(6000) }).then((r) => r.ok).catch(() => false);
  if (!reachable) {
    $("#remote-status").textContent = "The StockBot computer is offline right now, so this shows the latest published results.";
    return false;
  }
  if (REMOTE.token) {
    const ok = await fetch(base + "/api/remote/whoami", { headers: { Authorization: "Bearer " + REMOTE.token }, signal: AbortSignal.timeout(6000) })
      .then((r) => r.ok).catch(() => false);
    if (ok) {
      REMOTE.base = base;
      SNAPSHOT = false;
      document.body.classList.add("remote");
      return true;
    }
  }
  $("#remote-status").textContent = "The StockBot computer is online. Enter its passcode to trade and control the bot from here.";
  $("#remote-login").classList.remove("hidden");
  $("#remote-login").onsubmit = async (e) => {
    e.preventDefault();
    const btn = $("#remote-login button");
    btn.disabled = true;
    try {
      const r = await fetch(base + "/api/remote/login", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode: $("#remote-passcode").value }) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || "Login failed.");
      localSet("remote-token", d.token);
      location.reload();
    } catch (err) { toast(err.message, "bad"); btn.disabled = false; }
  };
  return false;
}

async function logoutRemote() {
  await api("/api/remote/logout", { method: "POST" }).catch(() => {});
  localSet("remote-token", "");
  location.reload();
}
const money = (v, d = 2) => v == null || isNaN(v) ? "—" :
  (v < 0 ? "-" : "") + "$" + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const pct = (v) => v == null || isNaN(v) ? "—" : (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
const signed = (v) => (v >= 0 ? "+" : "") + money(v);
const cls = (v) => (v >= 0 ? "up" : "down");
const shares = (v) => v == null ? "—" : (+v).toLocaleString(undefined, { maximumFractionDigits: 6 });
const big = (v) => v == null ? "—" : v >= 1e9 ? (v / 1e9).toFixed(2) + "B" : v >= 1e6 ? (v / 1e6).toFixed(2) + "M" : v >= 1e3 ? (v / 1e3).toFixed(1) + "K" : String(v);
const when = (t) => t ? new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "—";
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const TYPE_NAMES = { market: "Market", limit: "Limit", stop: "Stop", stop_limit: "Stop limit", trailing_stop: "Trailing stop" };

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

// ---------- charts ----------
const chartOpts = (el) => ({
  autoSize: true,
  layout: { background: { color: "transparent" }, textColor: "#8a94a3", fontFamily: "inherit" },
  grid: { vertLines: { visible: false }, horzLines: { color: "#1b212b" } },
  rightPriceScale: { borderVisible: false },
  timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false, lockVisibleTimeRangeOnResize: true },
  crosshair: { mode: 1 },
  handleScroll: true,
});
const priceChart = LightweightCharts.createChart($("#chart"), chartOpts());
let mainSeries = null, volSeries = null;

async function loadChart() {
  const sym = state.symbol, rng = state.range;
  let h;
  try { h = await api(`/api/history/${sym}?range=${rng}`); } catch (e) { return; }
  if (sym !== state.symbol || rng !== state.range) return;
  if (mainSeries) priceChart.removeSeries(mainSeries);
  if (volSeries) priceChart.removeSeries(volSeries);
  const intraday = ["1d", "5d", "1mo"].includes(rng);
  const shift = intraday ? h.gmtoffset : 0; // show exchange time on intraday charts
  const c = h.candles.map((x) => ({ ...x, time: x.time + shift }));
  const first = c[0]?.open ?? 0, last = c.at(-1)?.close ?? 0;
  const ref = rng === "1d" && state.quote ? state.quote.prev_close : first;
  const up = last >= ref;
  const color = up ? "#22c38e" : "#f2555a";
  if (state.chartType === "candle") {
    mainSeries = priceChart.addCandlestickSeries({ upColor: "#22c38e", downColor: "#f2555a", borderVisible: false, wickUpColor: "#22c38e", wickDownColor: "#f2555a" });
    mainSeries.setData(c.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
  } else {
    mainSeries = priceChart.addAreaSeries({ lineColor: color, topColor: color + "55", bottomColor: color + "00", lineWidth: 2, priceLineVisible: false });
    mainSeries.setData(c.map(({ time, close }) => ({ time, value: close })));
    if (rng === "1d" && ref) mainSeries.createPriceLine({ price: ref, color: "#5a6372", lineStyle: 2, lineWidth: 1, axisLabelVisible: false, title: "Prev close" });
  }
  volSeries = priceChart.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
  priceChart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  volSeries.setData(c.map((x) => ({ time: x.time, value: x.volume, color: x.close >= x.open ? "#22c38e33" : "#f2555a33" })));
  addTradeMarkers(sym, c, shift);
  priceChart.timeScale().fitContent();
  requestAnimationFrame(() => priceChart.timeScale().fitContent());
}

// Arrows on the chart where the account bought (▲) or sold (▼) this stock.
let fillsCache = { time: 0, orders: [] };
async function addTradeMarkers(sym, candles, shift) {
  if (!candles.length) return;
  if (Date.now() - fillsCache.time > 30000) {
    fillsCache = { time: Date.now(), orders: await api("/api/orders?status=closed").catch(() => []) };
  }
  const series = mainSeries;
  const markers = [];
  for (const o of fillsCache.orders) {
    if (o.symbol !== sym || o.status !== "filled" || !o.filled_at) continue;
    const t = o.filled_at + shift;
    if (t < candles[0].time) continue;
    let bar = candles[0].time;
    for (const x of candles) { if (x.time <= t) bar = x.time; else break; }
    const buy = o.side === "buy";
    markers.push({ time: bar, position: buy ? "belowBar" : "aboveBar", color: buy ? "#22c38e" : "#f2555a",
      shape: buy ? "arrowUp" : "arrowDown", text: `${buy ? "Buy" : "Sell"} ${(+o.filled_qty).toLocaleString(undefined, { maximumFractionDigits: 2 })}${o.source === "bot" ? " · bot" : ""}` });
  }
  markers.sort((a, b) => a.time - b.time);
  if (series === mainSeries) series.setMarkers(markers);
}

// ---------- quote panel ----------
async function selectSymbol(sym) {
  state.symbol = sym.toUpperCase();
  localSet("symbol", state.symbol);
  showView("trade");
  $("#q-symbol").textContent = state.symbol;
  renderAlerts();
  $("#news").textContent = "Loading…";
  renderWatchlist();
  await refreshQuote();
  loadChart();
  loadNews();
  renderTicket();
}

async function refreshQuote() {
  try {
    const q = await api(`/api/quote/${state.symbol}`);
    if (q.symbol !== state.symbol) return;
    state.quote = q;
    state.quotes[q.symbol] = q;
  } catch (e) {
    toast(`Couldn't load ${state.symbol}: ${e.message}`, "bad");
    return;
  }
  const q = state.quote;
  $("#q-symbol").textContent = q.symbol;
  $("#q-name").textContent = q.name;
  $("#q-exchange").textContent = q.exchange || "";
  $("#q-price").textContent = money(q.price);
  $("#q-change").innerHTML = `<span class="${cls(q.change)}">${signed(q.change)} (${pct(q.change_pct)})</span> <span class="muted small">Today</span>`;
  const extName = q.session === "pre" ? "Pre-market" : "After hours";
  $("#q-ext").innerHTML = q.ext_price && !q.market_open
    ? `<span class="muted">${extName}</span> <b>${money(q.ext_price)}</b> <span class="${cls(q.ext_change)}">${signed(q.ext_change)} (${pct(q.ext_change_pct)})</span>` : "";
  $("#q-session").textContent = SNAPSHOT ? "Price as of the last website update" : q.market_open ? "Market open · live"
    : q.session === "pre" ? "Pre-market · regular trading opens 9:30 AM ET" : q.session === "post" ? "After hours · regular trading has closed" : "Market closed · last price";
  const stats = [
    ["Open", money(q.open)], ["High", money(q.high)], ["Low", money(q.low)], ["Prev close", money(q.prev_close)],
    ["Bid", money(q.bid)], ["Ask", money(q.ask)], ["Spread", `${money(q.ask - q.bid)} (${((q.ask - q.bid) / q.price * 100).toFixed(2)}%)`], ["Volume", big(q.volume)],
    ["52-wk range", q.week52_low ? `${money(q.week52_low)} – ${money(q.week52_high)}` : "—"],
  ];
  $("#q-stats").innerHTML = stats.map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("");
  $("#wl-toggle").textContent = state.watchlist.includes(q.symbol) ? "−" : "＋";
  renderMyPosition();
  updateEstimate();
}

async function loadNews() {
  const sym = state.symbol;
  const items = await api(`/api/news/${sym}`).catch(() => []);
  if (sym !== state.symbol) return;
  $("#news").innerHTML = items.length
    ? items.map((n) => `<a href="${esc(n.link)}" target="_blank" rel="noopener">${esc(n.title)}<span>${esc(n.publisher)} · ${when(n.time)}</span></a>`).join("")
    : "No recent headlines.";
}

function renderMyPosition() {
  const p = state.positions.find((x) => x.symbol === state.symbol);
  if (!p) { $("#my-position").innerHTML = `<span class="muted">You don't own ${esc(state.symbol)}.</span>`; return; }
  $("#my-position").innerHTML = `<div class="kpis">
    <div><span>Shares</span><b>${shares(p.qty)}</b></div>
    <div><span>Market value</span><b>${money(p.market_value)}</b></div>
    <div><span>Avg cost</span><b>${money(p.avg_price)}</b></div>
    <div><span>Today</span><b class="${cls(p.day_pl)}">${signed(p.day_pl)}</b></div>
    <div><span>Total return</span><b class="${cls(p.unrealized_pl)}">${signed(p.unrealized_pl)} (${pct(p.unrealized_pl_pct)})</b></div>
  </div>`;
}

// ---------- price alerts ----------
async function refreshAlerts() {
  if (SNAPSHOT) return;
  const list = await api("/api/alerts").catch(() => null);
  if (!list) return;
  const seen = new Set(JSON.parse(localGet("alerts-seen") || "[]"));
  const fresh = list.filter((a) => a.fired && !seen.has(a.id));
  if (state.alerts) fresh.forEach((a) => toast(`🔔 ${a.symbol} is ${a.direction} ${money(a.price)} (now ${money(a.fired_price)})`, "ok"));
  localSet("alerts-seen", JSON.stringify(list.filter((a) => a.fired).map((a) => a.id)));
  state.alerts = list;
  renderAlerts();
}

function renderAlerts() {
  const list = state.alerts || [];
  $("#alert-add").textContent = `Alert me on ${state.symbol}`;
  $("#alerts-list").innerHTML = list.map((a) => `<div class="alert-row ${a.fired ? "fired" : ""}">
    <button class="link" data-alert-sym="${esc(a.symbol)}"><b>${esc(a.symbol)}</b></button>
    <span>${a.direction === "above" ? "rises to" : "falls to"} <b>${money(a.price)}</b></span>
    <span class="muted small">${a.fired ? `Reached ${when(a.fired)} at ${money(a.fired_price)}` : `Waiting · set at ${money(a.price_then)}`}</span>
    <button class="ghost small" data-alert-del="${a.id}" title="Remove this alert">Remove</button></div>`).join("");
}

$("#alert-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const price = parseFloat($("#alert-price").value);
  if (!(price > 0)) return toast("Enter the price to be alerted at.", "bad");
  try {
    const a = await api("/api/alerts", { method: "POST", body: { symbol: state.symbol, price } });
    $("#alert-price").value = "";
    toast(`Alert set: ${a.symbol} ${a.direction === "above" ? "rises to" : "falls to"} ${money(a.price)}`, "ok");
    refreshAlerts();
  } catch (err) { toast(err.message, "bad"); }
});
$("#alerts-list").addEventListener("click", async (e) => {
  const del = e.target.closest("[data-alert-del]"), sym = e.target.closest("[data-alert-sym]");
  if (sym) return selectSymbol(sym.dataset.alertSym);
  if (!del) return;
  try { await api(`/api/alerts/${del.dataset.alertDel}`, { method: "DELETE" }); refreshAlerts(); } catch (err) { toast(err.message, "bad"); }
});

// ---------- watchlist ----------
async function refreshWatchlist() {
  if (!state.watchlist.length) state.watchlist = await api("/api/watchlist");
  const q = await api(`/api/quotes?symbols=${state.watchlist.join(",")}`).catch(() => ({}));
  Object.assign(state.quotes, q);
  renderWatchlist();
}

function sparkline(vals, up) {
  if (!vals || vals.length < 2) return "";
  const min = Math.min(...vals), max = Math.max(...vals), w = 60, h = 24;
  const pts = vals.map((v, i) => `${(i / (vals.length - 1)) * w},${h - ((v - min) / (max - min || 1)) * h}`).join(" ");
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><polyline fill="none" stroke="${up ? "#22c38e" : "#f2555a"}" stroke-width="1.5" points="${pts}"/></svg>`;
}

function renderWatchlist() {
  $("#watchlist").innerHTML = state.watchlist.map((s) => {
    const q = state.quotes[s];
    if (!q) return `<div class="wl-row" data-s="${s}"><div><b>${s}</b></div><div></div><div class="px muted">…</div></div>`;
    return `<div class="wl-row ${s === state.symbol ? "sel" : ""}" data-s="${s}">
      <div><b>${s}</b><span class="nm">${esc(q.name)}</span></div>
      <div>${sparkline(q.spark, q.change >= 0)}</div>
      <div class="px">${money(q.price)}<span class="chg ${cls(q.change)}">${pct(q.change_pct)}</span></div>
    </div>`;
  }).join("");
}

$("#watchlist").addEventListener("click", (e) => {
  const row = e.target.closest(".wl-row");
  if (row) selectSymbol(row.dataset.s);
});

$("#wl-toggle").addEventListener("click", async () => {
  const s = state.symbol;
  const list = state.watchlist.includes(s) ? state.watchlist.filter((x) => x !== s) : [...state.watchlist, s];
  state.watchlist = await api("/api/watchlist", { method: "POST", body: list });
  $("#wl-toggle").textContent = state.watchlist.includes(s) ? "−" : "＋";
  toast(state.watchlist.includes(s) ? `${s} added to watchlist` : `${s} removed from watchlist`);
  refreshWatchlist();
});

// ---------- search ----------
let searchTimer, searchSel = -1;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  if (!q) return $("#search-results").classList.add("hidden");
  searchTimer = setTimeout(async () => {
    const r = await api(`/api/search?q=${encodeURIComponent(q)}`).catch(() => []);
    searchSel = -1;
    $("#search-results").innerHTML = r.length
      ? r.map((x) => `<div data-s="${esc(x.symbol)}"><b>${esc(x.symbol)}</b><span class="muted">${esc(x.name)}</span></div>`).join("")
      : `<div class="muted">No matches</div>`;
    $("#search-results").classList.remove("hidden");
  }, 250);
});
$("#search").addEventListener("keydown", (e) => {
  const items = $$("#search-results div[data-s]");
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    searchSel = Math.max(0, Math.min(items.length - 1, searchSel + (e.key === "ArrowDown" ? 1 : -1)));
    items.forEach((el, i) => el.classList.toggle("sel", i === searchSel));
  } else if (e.key === "Enter") {
    const pick = items[searchSel]?.dataset.s || items[0]?.dataset.s || e.target.value.trim();
    if (pick) chooseSearch(pick);
  } else if (e.key === "Escape") $("#search-results").classList.add("hidden");
});
$("#search-results").addEventListener("mousedown", (e) => {
  const el = e.target.closest("div[data-s]");
  if (el) chooseSearch(el.dataset.s);
});
$("#search").addEventListener("blur", () => setTimeout(() => $("#search-results").classList.add("hidden"), 150));
function chooseSearch(s) {
  $("#search").value = "";
  $("#search-results").classList.add("hidden");
  selectSymbol(s);
}

// ---------- chart controls ----------
$("#ranges").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  $$("#ranges button").forEach((x) => x.classList.toggle("active", x === b));
  state.range = b.dataset.r;
  loadChart();
});
$("#chart-type").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  $$("#chart-type button").forEach((x) => x.classList.toggle("active", x === b));
  state.chartType = b.dataset.t;
  loadChart();
});

// ---------- order ticket ----------
const ticket = () => ({
  type: $("#o-type").value, unit: $("#o-unit").value, qty: parseFloat($("#o-qty").value),
  limit: parseFloat($("#o-limit").value), stop: parseFloat($("#o-stop").value), trail: parseFloat($("#o-trail").value),
  tif: $("#o-tif").value, ext: $("#o-ext").checked, bracket: $("#o-bracket").checked, tp: parseFloat($("#o-tp").value), sl: parseFloat($("#o-sl").value),
});

$("#side").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  state.side = b.dataset.side;
  $$("#side button").forEach((x) => x.classList.toggle("active", x === b));
  renderTicket();
});
["#o-type", "#o-unit", "#o-bracket", "#o-ext"].forEach((s) => $(s).addEventListener("change", renderTicket));
$$("#ticket-form input, #ticket-form select").forEach((el) => el.addEventListener("input", updateEstimate));

function renderTicket() {
  const t = ticket(), buy = state.side === "buy";
  const isMkt = t.type === "market";
  if (!isMkt && t.unit === "dollars") $("#o-unit").value = "shares";
  $("#row-unit").classList.toggle("hidden", !isMkt);
  $("#row-unit").firstChild.textContent = buy ? "Buy in " : "Sell in ";
  $("#qty-label").textContent = isMkt && $("#o-unit").value === "dollars" ? "Amount ($)" : "Shares";
  $("#o-qty").step = isMkt ? "any" : "1";
  $("#row-limit").classList.toggle("hidden", !["limit", "stop_limit"].includes(t.type));
  $("#row-stop").classList.toggle("hidden", !["stop", "stop_limit"].includes(t.type));
  $("#row-trail").classList.toggle("hidden", t.type !== "trailing_stop");
  $("#row-ext").classList.toggle("hidden", t.type !== "limit");
  const ext = t.type === "limit" && t.ext;
  $("#row-bracket").classList.toggle("hidden", !buy || ext);
  $("#bracket-fields").classList.toggle("hidden", !(buy && t.bracket) || ext);
  $("#review").textContent = "Review order";
  $("#review").classList.toggle("sell-mode", !buy);
  $("#submit").classList.toggle("sell-mode", !buy);
  showReview(false);
  updateEstimate();
}

function estPrice(t) {
  const q = state.quote; if (!q) return null;
  const buy = state.side === "buy";
  if (t.type === "limit" || t.type === "stop_limit") return t.limit || null;
  if (t.type === "stop") return t.stop || null;
  return buy ? q.ask : q.bid;
}

function updateEstimate() {
  const t = ticket(), q = state.quote, buy = state.side === "buy";
  if (!q) return;
  const px = estPrice(t);
  const useExt = !q.market_open && q.ext_price && t.type === "limit" && t.ext;
  const [bid, ask] = useExt ? [q.ext_bid, q.ext_ask] : [q.bid, q.ask];
  $("#bidask").innerHTML = `<div><span>Bid</span><b class="down">${money(bid)}</b></div><div><span>Ask</span><b class="up">${money(ask)}</b></div>
    <div><span>Spread</span><b>${money(ask - bid)}</b></div>` + (useExt ? `<small>${q.session === "pre" ? "Pre-market" : "After-hours"} quote</small>` : "");
  $("#est-px-label").textContent = { market: buy ? "Ask price" : "Bid price", limit: "Limit price", stop: "Stop price", stop_limit: "Limit price", trailing_stop: "Current price" }[t.type];
  $("#est-px").textContent = money(t.type === "trailing_stop" ? q.price : px);
  const dollars = t.type === "market" && t.unit === "dollars";
  let total = null, sh = null;
  if (dollars && t.qty) { total = t.qty; sh = px ? t.qty / px : null; }
  else if (t.qty) { sh = t.qty; total = (t.type === "trailing_stop" ? q.price : px) * t.qty; }
  $("#est-label").textContent = dollars ? "Estimated shares" : buy ? "Estimated cost" : "Estimated credit";
  $("#est-total").textContent = dollars ? (sh ? shares(sh.toFixed(6)) : "0") : money(total || 0);
  const pos = state.positions.find((p) => p.symbol === state.symbol);
  $("#avail").textContent = buy
    ? `${money(state.account?.buying_power)} buying power available`
    : `${shares(pos?.qty || 0)} shares available`;
}

function buildOrder() {
  const t = ticket();
  const o = { symbol: state.symbol, side: state.side, type: t.type, time_in_force: t.tif };
  if (!t.qty || t.qty <= 0) throw new Error("Enter a quantity.");
  if (t.type === "market" && t.unit === "dollars") o.notional = t.qty; else o.qty = t.qty;
  if (["limit", "stop_limit"].includes(t.type)) { if (!t.limit) throw new Error("Enter a limit price."); o.limit_price = t.limit; }
  if (["stop", "stop_limit"].includes(t.type)) { if (!t.stop) throw new Error("Enter a stop price."); o.stop_price = t.stop; }
  if (t.type === "trailing_stop") { if (!t.trail) throw new Error("Enter a trail percent."); o.trail_percent = t.trail; }
  if (t.type === "limit" && t.ext) o.extended_hours = true;
  if (state.side === "buy" && t.bracket && !o.extended_hours) {
    if (t.tp) o.take_profit = t.tp;
    if (t.sl) o.stop_loss = t.sl;
  }
  return o;
}

function showReview(on) {
  $("#ticket-form").classList.toggle("hidden", on);
  $("#ticket-review").classList.toggle("hidden", !on);
}

$("#review").addEventListener("click", () => {
  let o;
  try { o = buildOrder(); } catch (e) { return toast(e.message, "bad"); }
  const q = state.quote, t = ticket();
  const px = estPrice(t) || q.price;
  const lines = [
    ["Symbol", `${o.symbol} · ${esc(q.name)}`],
    ["Action", o.side === "buy" ? "Buy" : "Sell"],
    ["Order type", TYPE_NAMES[o.type]],
    [o.notional ? "Amount" : "Shares", o.notional ? money(o.notional) : shares(o.qty)],
  ];
  if (o.limit_price) lines.push(["Limit price", money(o.limit_price)]);
  if (o.stop_price) lines.push(["Stop price", money(o.stop_price)]);
  if (o.trail_percent) lines.push(["Trail", o.trail_percent + "%"]);
  if (o.take_profit) lines.push(["Take profit", money(o.take_profit)]);
  if (o.stop_loss) lines.push(["Stop loss", money(o.stop_loss)]);
  lines.push(["Time in force", o.time_in_force === "gtc" ? "Good till canceled" : "Good for day"]);
  if (o.extended_hours) lines.push(["Hours", "4 AM to 8 PM ET"]);
  lines.push([o.side === "buy" ? "Est. cost" : "Est. credit", o.notional ? money(o.notional) : money(px * o.qty)]);
  $("#rv-title").textContent = `${o.side === "buy" ? "Buy" : "Sell"} ${o.symbol}`;
  $("#rv-lines").innerHTML = lines.map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("");
  const extNow = o.extended_hours && (q.session === "pre" || q.session === "post");
  $("#rv-note").textContent = q.market_open
    ? (o.type === "market" ? "Market orders fill at the best available price, which can differ from the estimate." : "Your order will fill only if the price condition is met.")
    : extNow ? "Extended hours: the order can fill now, but only at your limit price or better."
    : o.extended_hours ? "Trading is closed right now. Your order will start working when pre-market opens at 4:00 AM ET."
    : "The market is closed. Your order will be queued and sent at the next market open.";
  $("#rv-warn").innerHTML = orderWarnings(o, q).map((w) => `<p class="warn-note">${esc(w)}</p>`).join("");
  $("#submit").textContent = `Submit ${o.side === "buy" ? "buy" : "sell"}`;
  showReview(true);
});
// The cautions a good broker shows before you submit.
function orderWarnings(o, q) {
  const w = [], buy = o.side === "buy";
  const qty = o.qty || (o.notional ? o.notional / q.ask : 0);
  const vol = q.est_volume || q.volume;
  if (vol && qty / vol >= 0.0005 && ["market", "stop", "trailing_stop"].includes(o.type))
    w.push(`This order is ${(qty / vol * 100).toFixed(2)}% of a typical day's trading in ${o.symbol}, so expect a worse price than the quote. A limit order avoids that.`);
  if (o.type === "limit" && (buy ? o.limit_price >= q.ask : o.limit_price <= q.bid) && q.market_open)
    w.push(`Your limit is ${buy ? "at or above the ask" : "at or below the bid"}, so this will fill right away at about ${money(buy ? q.ask : q.bid)}.`);
  if (o.type === "limit" && Math.abs(o.limit_price / q.price - 1) > 0.2)
    w.push(`Your limit price is ${Math.abs((o.limit_price / q.price - 1) * 100).toFixed(0)}% away from the current price. Check for a typo.`);
  if (o.type === "stop_limit" && (buy ? o.limit_price < o.stop_price : o.limit_price > o.stop_price))
    w.push(`The limit is ${buy ? "below" : "above"} the stop, so the order may trigger and then not fill.`);
  if ((q.ask - q.bid) / q.price > 0.005 && o.type === "market")
    w.push(`${o.symbol} has a wide spread (${((q.ask - q.bid) / q.price * 100).toFixed(2)}%). A market order pays that gap.`);
  if (o.extended_hours) w.push("Outside regular hours fewer people trade: prices jump more and the gap between bid and ask is wider.");
  if (buy && state.account && (o.notional || qty * q.ask) > state.account.equity * 0.25)
    w.push(`This would put more than 25% of the account in one stock.`);
  return w;
}

$("#quick").addEventListener("click", (e) => {
  const b = e.target.closest("[data-pct]"); if (!b || !state.quote) return;
  const f = +b.dataset.pct / 100, t = ticket(), q = state.quote, buy = state.side === "buy";
  const whole = t.type !== "market";
  let v;
  if (buy) {
    const cash = (state.account?.buying_power || 0) * f * (f === 1 ? 0.995 : 1);  // leave room for the price to tick up
    const px = estPrice(t) || q.ask;
    v = t.type === "market" && t.unit === "dollars" ? Math.floor(cash * 100) / 100 : whole ? Math.floor(cash / px) : Math.floor(cash / px * 1e4) / 1e4;
  } else {
    const have = state.positions.find((p) => p.symbol === state.symbol)?.qty || 0;
    v = t.type === "market" && t.unit === "dollars" ? Math.floor(have * f * q.bid * 100) / 100 : whole ? Math.floor(have * f) : f === 1 ? have : Math.floor(have * f * 1e4) / 1e4;
  }
  $("#o-qty").value = v > 0 ? v : "";
  updateEstimate();
});

$("#edit").addEventListener("click", () => showReview(false));
$("#submit").addEventListener("click", async () => {
  let o;
  try { o = buildOrder(); } catch (e) { return toast(e.message, "bad"); }
  $("#submit").disabled = true;
  try {
    const r = await api("/api/orders", { method: "POST", body: o });
    knownDone.add(r.id);
    const desc = `${r.side === "buy" ? "Bought" : "Sold"} ${shares(r.filled_qty)} ${r.symbol} at ${money(r.filled_avg_price)}`;
    toast(r.status === "filled" ? desc : `${TYPE_NAMES[r.type]} ${r.side} order for ${r.symbol} placed (${r.status === "accepted" ? "queued" : "open"}).`, "ok");
    $("#o-qty").value = "";
    showReview(false);
    refreshAccount();
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    $("#submit").disabled = false;
  }
});

// ---------- account / portfolio ----------
async function refreshAccount() {
  try {
    const r = await api("/api/account");
    state.account = r.account; state.positions = r.positions;
  } catch (e) { return; }
  renderMyPosition();
  updateEstimate();
  if (state.view === "portfolio") renderPortfolio();
}

const equityChart = LightweightCharts.createChart($("#equity-chart"), chartOpts());
const equitySeries = equityChart.addAreaSeries({ lineColor: "#4c8dff", topColor: "#4c8dff44", bottomColor: "#4c8dff00", lineWidth: 2, priceLineVisible: false });

async function renderPortfolio() {
  const a = state.account; if (!a) return;
  $("#p-equity").textContent = money(a.equity);
  $("#p-day").innerHTML = `<span class="${cls(a.day_pl)}">${signed(a.day_pl)} (${pct(a.day_pl_pct)})</span> <span class="muted small">Today</span>`;
  $("#p-kpis").innerHTML = [
    ["Total return", `<span class="${cls(a.total_pl)}">${signed(a.total_pl)} (${pct(a.total_pl_pct)})</span>`],
    ["Invested", money(a.long_market_value)], ["Cash", money(a.cash)], ["Buying power", money(a.buying_power)],
  ].map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("");

  const ps = state.positions;
  $("#positions-table").innerHTML = ps.length ? `<tr><th>Symbol</th><th class="r">Shares</th><th class="r">Price</th><th class="r">Avg cost</th>
    <th class="r">Market value</th><th class="r">Today</th><th class="r">Total return</th><th></th></tr>` +
    ps.map((p) => `<tr class="click" data-s="${p.symbol}"><td><b>${p.symbol}</b> ${state.bot?.owned?.[p.symbol] ? '<span class="tag bot">bot</span>' : ""}</td>
      <td class="r">${shares(p.qty)}</td><td class="r">${money(p.price)}</td><td class="r">${money(p.avg_price)}</td>
      <td class="r">${money(p.market_value)}</td><td class="r ${cls(p.day_pl)}">${signed(p.day_pl)}</td>
      <td class="r ${cls(p.unrealized_pl)}">${signed(p.unrealized_pl)} (${pct(p.unrealized_pl_pct)})</td>
      <td class="r"><button class="ghost small" data-close="${p.symbol}">Sell all</button></td></tr>`).join("")
    : `<tr><td class="empty">No positions yet. Search for a stock and buy it, or turn on the bot.</td></tr>`;

  const [open, hist, eq, perf, acts] = await Promise.all([
    api("/api/orders?status=open").catch(() => []), api("/api/orders?status=closed").catch(() => []), api("/api/equity").catch(() => []),
    api("/api/performance").catch(() => null), api("/api/activity").catch(() => []),
  ]);
  renderPerformance(perf);
  $("#activity-card").classList.toggle("hidden", !acts.length);
  $("#activity-table").innerHTML = `<tr><th>Date</th><th>Symbol</th><th>What happened</th><th class="r">Cash</th></tr>` +
    acts.map((x) => `<tr><td>${new Date(x.time * 1000).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" })}</td>
      <td><b>${esc(x.symbol)}</b></td><td>${esc(x.note)}</td><td class="r ${x.amount ? "up" : ""}">${x.amount ? "+" + money(x.amount) : "—"}</td></tr>`).join("");
  const orderRow = (o, canCancel) => `<tr><td>${when(o.created_at)}</td><td><b>${o.symbol}</b></td>
    <td>${o.side === "buy" ? "Buy" : "Sell"} · ${TYPE_NAMES[o.type] || o.type}${o.source === "bot" ? ' <span class="tag bot">bot</span>' : ""}${o.extended_hours ? ' <span class="tag" title="Can trade pre-market and after hours">ext</span>' : ""}</td>
    <td class="r">${o.notional ? money(o.notional) : shares(o.qty)}</td>
    <td class="r">${o.limit_price ? "L " + money(o.limit_price) : ""} ${o.stop_price ? "S " + money(o.stop_price) : ""} ${o.trail_percent ? o.trail_percent + "%" : ""}</td>
    <td class="r">${o.filled_avg_price ? shares(o.filled_qty) + " @ " + money(o.filled_avg_price) : "—"}</td>
    <td><span class="tag ${o.status}">${o.status === "accepted" ? "queued" : o.status}</span>${o.reject_reason ? ` <span class="muted small">${esc(o.reject_reason)}</span>` : ""}</td>
    <td class="r nowrap">${canCancel ? `${o.notional ? "" : `<button class="ghost small" data-edit="${o.id}">Edit</button> `}<button class="ghost small" data-cancel="${o.id}">Cancel</button>` : ""}</td></tr>`;
  state.openOrders = open;
  const head = `<tr><th>Placed</th><th>Symbol</th><th>Order</th><th class="r">Qty</th><th class="r">Prices</th><th class="r">Filled</th><th>Status</th><th></th></tr>`;
  $("#open-orders").innerHTML = open.length ? head + open.map((o) => orderRow(o, true)).join("") : `<tr><td class="empty">No open orders.</td></tr>`;
  $("#order-history").innerHTML = hist.length ? head + hist.map((o) => orderRow(o, false)).join("") : `<tr><td class="empty">No orders yet.</td></tr>`;
  const pts = eq.map((x) => ({ time: Math.floor(x.time), value: x.equity }));
  equitySeries.setData(pts.filter((p, i) => i === 0 || p.time > pts[i - 1].time));
  equityChart.timeScale().fitContent();
}

function renderPerformance(p) {
  if (!p) { $("#perf-table").innerHTML = `<tr><td class="empty">Performance isn't available right now.</td></tr>`; $("#perf-stats").innerHTML = ""; return; }
  const day = (t) => new Date(t * 1000).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
  $("#perf-since").textContent = p.since ? `Since ${day(p.since)}` : "";
  const cell = (v, pts) => v == null ? '<td class="r muted">—</td>' : `<td class="r ${cls(v)}">${pts ? (v >= 0 ? "+" : "") + v.toFixed(2) + " pts" : pct(v)}</td>`;
  $("#perf-table").innerHTML = `<tr><th>Period</th><th class="r">This account</th><th class="r">S&amp;P 500 (SPY)</th><th class="r">Difference</th></tr>` +
    p.periods.map((r) => `<tr><td>${esc(r.label)}</td>${cell(r.you)}${cell(r.spy)}${cell(r.diff, true)}</tr>`).join("");
  const t = p.trades, d = (x) => x ? `<span class="${cls(x[1])}">${pct(x[1])}</span> <span class="muted small">${esc(x[0].slice(5))}</span>` : "—";
  const tr = (x) => x ? `<span class="${cls(x.pl)}">${signed(x.pl)}</span> <span class="muted small">${esc(x.symbol)}</span>` : "—";
  $("#perf-stats").innerHTML = [
    ["Worst drop from a high", pct(p.max_drawdown)],
    ["Now vs. highest value", p.from_high > -0.005 ? "At the high" : pct(p.from_high)],
    ["Up days", p.days ? `${p.up_days} of ${p.days}` : "—"],
    ["Best day", d(p.best_day)], ["Worst day", d(p.worst_day)],
    ["Sharpe ratio", p.sharpe == null ? `<span class="muted" title="Needs 20 days of history">Needs 20 days</span>` : p.sharpe.toFixed(2)],
    ["Profit on sold shares", t.count ? `<span class="${cls(t.realized)}">${signed(t.realized)}</span>` : "—"],
    ["Winning sales", t.count ? `${t.wins} of ${t.count} (${t.win_rate.toFixed(0)}%)` : "—"],
    ["Best sale", tr(t.best)], ["Worst sale", tr(t.worst)],
    ["Dividends received", money(p.dividends)], ["Orders filled", String(t.fills)],
  ].map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("");
}

$("#view-portfolio").addEventListener("click", async (e) => {
  const c = e.target.closest("[data-cancel]"), cl = e.target.closest("[data-close]"), row = e.target.closest("tr[data-s]");
  const ed = e.target.closest("[data-edit]");
  if (ed) return openEditOrder((state.openOrders || []).find((o) => o.id === ed.dataset.edit));
  try {
    if (c) { await api(`/api/orders/${c.dataset.cancel}`, { method: "DELETE" }); toast("Order canceled", "ok"); return refreshAccount(); }
    if (cl) {
      if (!confirm(`Sell your entire ${cl.dataset.close} position at market?`)) return;
      await api(`/api/positions/${cl.dataset.close}/close`, { method: "POST" }); toast(`Sell order sent for ${cl.dataset.close}`, "ok"); return refreshAccount();
    }
    if (row) selectSymbol(row.dataset.s);
  } catch (err) { toast(err.message, "bad"); }
});
// ---------- edit an open order ----------
let editing = null;
function openEditOrder(o) {
  if (!o) return;
  editing = o;
  $("#eo-title").textContent = `Edit ${o.side} order · ${o.symbol}`;
  $("#eo-sub").textContent = `${TYPE_NAMES[o.type] || o.type} order placed ${when(o.created_at)}. Changes take effect right away.`;
  $("#eo-qty").value = o.qty; $("#eo-qty").step = o.type === "market" ? "any" : "1";
  $("#eo-limit").value = o.limit_price ?? ""; $("#eo-stop").value = o.stop_price ?? ""; $("#eo-trail").value = o.trail_percent ?? "";
  $("#eo-row-limit").classList.toggle("hidden", !["limit", "stop_limit"].includes(o.type));
  $("#eo-row-stop").classList.toggle("hidden", !["stop", "stop_limit"].includes(o.type));
  $("#eo-row-trail").classList.toggle("hidden", o.type !== "trailing_stop");
  $("#edit-order").showModal();
}
$("#edit-order").addEventListener("close", async () => {
  const o = editing; editing = null;
  if (!o || $("#edit-order").returnValue !== "save") return;
  const body = { qty: $("#eo-qty").value };
  if (["limit", "stop_limit"].includes(o.type)) body.limit_price = $("#eo-limit").value;
  if (["stop", "stop_limit"].includes(o.type)) body.stop_price = $("#eo-stop").value;
  if (o.type === "trailing_stop") body.trail_percent = $("#eo-trail").value;
  try {
    const r = await api(`/api/orders/${o.id}`, { method: "PATCH", body });
    if (r.status === "filled") knownDone.add(r.id);
    toast(r.status === "filled" ? `Order updated and filled at ${money(r.filled_avg_price)}` : "Order updated", "ok");
  } catch (err) { toast(err.message, "bad"); }
  refreshAccount();
});

// ---------- notices when orders finish ----------
const knownDone = new Set();
let fillsPrimed = false;
async function watchFills() {
  if (SNAPSHOT) return;
  const done = await api("/api/orders?status=closed").catch(() => null);
  if (!done) return;
  const fresh = done.filter((o) => !knownDone.has(o.id));
  done.forEach((o) => knownDone.add(o.id));
  if (!fillsPrimed) { fillsPrimed = true; return; }
  fresh.slice(0, 6).reverse().forEach((o) => {
    const who = o.source === "bot" ? "Bot " : "";
    if (o.status === "filled") toast(`${who}${o.side === "buy" ? (who ? "bought" : "Bought") : (who ? "sold" : "Sold")} ${shares(o.filled_qty)} ${o.symbol} at ${money(o.filled_avg_price)}`, "ok");
    else if (o.status === "expired") toast(`Your ${o.symbol} ${o.side} order expired without filling.`, "");
    else if (o.status === "rejected") toast(`${o.symbol} ${o.side} order rejected: ${o.reject_reason || "no reason given"}`, "bad");
  });
  if (fresh.length) refreshAccount();
}

$("#cancel-all").addEventListener("click", async () => {
  if (!confirm("Cancel all open orders?")) return;
  await api("/api/orders", { method: "DELETE" }); toast("All open orders canceled", "ok"); refreshAccount();
});

// ---------- bot ----------
const PALETTE = { momentum_max: "#ff5c5c", momentum_weekly: "#00d1d1", nasdaq_2x: "#c8e64c", trend_momentum: "#22c38e", concentrated: "#f5b041", dual_momentum: "#4c8dff", sector_rotation: "#b07cff", dip_buyer: "#ff7eb6", high_52w: "#ffd166", core_blend: "#ffffff", spy_hold: "#8a94a3" };
const lineChart = (el) => LightweightCharts.createChart(el, { ...chartOpts(), timeScale: { borderVisible: false, timeVisible: false, lockVisibleTimeRangeOnResize: true } });
const btChart = lineChart($("#bt-chart"));
const tChart = lineChart($("#t-chart"));
const btLines = {}, tLines = {};
let strategyNames = {};

const RISK_FIELDS = [
  ["max_position_pct", "Max % in any one stock", 0.5], ["budget_pct", "% of account bot manages", 1],
  ["cash_reserve_pct", "Cash reserve %", 0.5], ["stop_atr", "Trailing stop (× avg daily range, 0 = off)", 0.5],
  ["daily_loss_limit_pct", "Halt after daily loss %", 0.5],
];

function setLines(chart, store, curves) {
  for (const [k, pts] of Object.entries(curves)) {
    if (!store[k]) store[k] = chart.addLineSeries({ color: PALETTE[k] || "#ccc", lineWidth: k === "spy_hold" ? 1 : 2, priceLineVisible: false, lastValueVisible: false });
    const clean = pts.filter((p, i) => i === 0 || p.time > pts[i - 1].time);
    store[k].setData(clean);
  }
  chart.timeScale().fitContent();
}
const legend = (keys) => keys.map((k) => `<span><i style="background:${PALETTE[k]}"></i>${esc(strategyNames[k] || k)}</span>`).join("");

async function refreshBot() {
  state.bot = await api("/api/bot").catch(() => state.bot);
  if (state.view === "bot") renderBot();
}

function renderBot() {
  const b = state.bot; if (!b) return;
  const c = b.config;
  $("#bot-enabled").checked = c.enabled;
  $("#bot-enabled-label").textContent = c.enabled ? "On" : "Off";
  $$("#bot-mode button").forEach((x) => x.classList.toggle("active", x.dataset.m === c.mode));
  if (Object.keys(strategyNames).length && !$("#bot-strategy").options.length) {
    $("#bot-strategy").innerHTML = `<option value="auto">Follow the champion (auto)</option>` +
      Object.entries(strategyNames).map(([k, n]) => `<option value="${k}">${esc(n)}</option>`).join("");
  }
  $("#bot-strategy").value = c.strategy || "auto";
  $("#bot-summary").innerHTML = `${c.enabled ? "Running" : "Stopped"} · ${c.mode === "auto" ? "places orders automatically" : "asks before every order"} ·
    following <b>${esc(b.strategy_name)}</b>${c.strategy === "auto" ? " (current champion)" : ""} ·
    last rebalance ${b.last_rebalance ? when(b.last_rebalance) : "never"}${b.halted_today ? ' · <b class="down">halted today (loss limit)</b>' : ""}`;

  $("#bot-next").innerHTML = b.next_action ? `<b>Next:</b> ${esc(b.next_action)}` : "";
  $("#bot-targets").innerHTML = (b.targets || []).length ? `<span class="muted">Target portfolio:</span> ` +
    b.targets.map((t) => `<span class="chip" title="${esc(t.why)}">${esc(t.symbol)} ${t.weight.toFixed(0)}%</span>`).join("") : "";
  const h = b.health;
  if (h) {
    const ago = Math.max(0, Math.round(h.now - h.last_loop));
    const up = h.uptime > 86400 ? (h.uptime / 86400).toFixed(1) + " days" : h.uptime > 3600 ? (h.uptime / 3600).toFixed(1) + " h" : Math.round(h.uptime / 60) + " min";
    $("#bot-health").innerHTML = [
      h.loop_ok ? `<span class="up">● Running</span> (checked ${ago}s ago)` : `<span class="down">● Not checking prices</span> (last ${ago}s ago)`,
      `up ${up}`,
      h.awake === false ? '<span class="down">Mac may sleep</span>' : h.awake ? "keeping Mac awake" : "",
    ].filter(Boolean).join(" · ");
  }

  $("#pending-card").classList.toggle("hidden", !b.pending.length);
  $("#pending").innerHTML = b.pending.map((p) => `<div class="pending-row"><div><b>${p.side.toUpperCase()} ${p.symbol}</b>
    ${p.notional ? money(p.notional) : shares(p.qty) + " sh"} <span class="muted">· ${esc(p.reason)}</span></div>
    <div><button class="primary" data-approve="${p.id}">Approve</button><button class="ghost" data-reject="${p.id}">Skip</button></div></div>`).join("");

  if (!$("#risk-form").dataset.ready) {
    $("#risk-form").innerHTML = RISK_FIELDS.map(([k, label, step]) => `<label>${label}<input name="${k}" type="number" step="${step}" value="${c[k]}"></label>`).join("");
    $("#risk-form").dataset.ready = 1;
  }

  const s = b.last_scan;
  if (s) {
    const r = s.regime;
    $("#regime").innerHTML = `Market regime: <b class="${r.risk_on ? "up" : "down"}">${r.risk_on ? "Risk-on" : "Risk-off"}</b> (SPY ${money(r.spy)} vs 200-day ${money(r.spy_sma200)}) · scanned ${when(s.time)}`;
    $("#strategy-picks").innerHTML = Object.entries(s.strategies || {}).map(([k, x]) => `<div><b style="color:${PALETTE[k]}">${esc(x.name)}</b>
      ${Object.entries(x.weights).sort((a, b) => b[1] - a[1]).map(([sym, w]) => `${sym} ${(w * 100).toFixed(0)}%`).join(" · ") || "Cash"}</div>`).join("");
    $("#scan-table").innerHTML = `<tr><th>Symbol</th><th>Trend signal</th><th class="r">Price</th><th class="r">6-mo</th><th class="r">RSI</th><th class="r">Score</th><th>Why</th></tr>` +
      s.rows.map((x) => `<tr class="click" data-s="${x.symbol}"><td><b>${x.symbol}</b></td><td><span class="tag ${x.action.split(" ")[0]}">${x.action}</span></td>
        <td class="r">${money(x.price)}</td><td class="r ${cls(x.momentum)}">${pct(x.momentum * 100)}</td><td class="r">${x.rsi.toFixed(0)}</td>
        <td class="r">${x.score.toFixed(2)}</td><td class="muted small">${x.reasons.map(esc).join(" · ")}</td></tr>`).join("");
  } else {
    $("#regime").textContent = "No scan yet. Click Scan now.";
  }

  $("#bot-log").innerHTML = b.log.length ? b.log.map((l) => `<div class="${l.kind}"><span class="muted">${when(l.time)}</span><span>${esc(l.msg)}</span></div>`).join("") : '<div class="muted">Nothing yet.</div>';
}

// ---------- track record ----------
async function refreshTrack() {
  const t = await api("/api/track").catch(() => null);
  if (!t) return;
  state.track = t;
  const day = (x) => new Date(x * 1000).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
  const badge = $("#day-badge");
  badge.textContent = `Day ${t.days}`;
  badge.classList.remove("hidden");
  $("#track-since").textContent = `Running since ${day(t.started)}`;
  const span = t.days < 14 ? `${t.days} day${t.days === 1 ? "" : "s"}` : t.days < 70 ? `${Math.floor(t.days / 7)} weeks` : `${(t.days / 30.4).toFixed(1)} months`;
  const num = (v) => v == null ? "—" : `<span class="${cls(v)}">${pct(v)}</span>`;
  $("#track-kpis").innerHTML = [
    ["Running for", t.days < 14 ? span : `${span} <span class="muted small">day ${t.days}</span>`],
    ["Trading days", `${t.trading_days}`],
    ["StockBot was running", `${t.covered_days} of ${t.trading_days} days${t.missed_days.length ? ` <span class="down small">${t.missed_days.length} missed</span>` : ""}`],
    ["Since the start", num(t.ret)], ["S&P 500, same time", num(t.spy_ret)],
    ["Worst drop", pct(t.max_drawdown)],
    ["Up weeks", t.weeks_done ? `${t.weeks_up} of ${t.weeks_done}` : "First week"],
    ["Rebalances", String(t.rebalances)], ["Trades", String(t.trades)],
  ].map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("");
  const frac = Math.min(1, t.trading_days / t.live_days_needed);
  $("#track-bar").style.width = (frac * 100).toFixed(1) + "%";
  $("#track-bar-label").textContent = `${t.trading_days} of ${t.live_days_needed} trading days. Before that, results are mostly luck.`;
  $("#track-milestones").innerHTML = t.milestones.map((m) => `<div class="${m.done ? "done" : ""}">
    <i>${m.done ? "✓" : ""}</i><div><b>${esc(m.name)}</b><span class="muted small">${esc(m.detail)}</span></div></div>`).join("");
  $("#track-note").textContent = `${t.milestones_done} of ${t.milestones.length} met. ${t.note}`;
}
$("#day-badge").addEventListener("click", () => { showView("bot"); $("#track-card").scrollIntoView({ behavior: "smooth", block: "start" }); });

async function refreshTournament() {
  let t;
  try { t = await api("/api/tournament"); } catch (e) { $("#t-champion").textContent = "Tournament data not ready yet: " + e.message; return; }
  t.rows.forEach((r) => (strategyNames[r.key] = r.name));
  const champ = t.rows.find((r) => r.champion);
  $("#t-champion").innerHTML = `<span class="champ">🏆 Champion: ${esc(champ?.name)}</span> · ${esc(t.reason)}`;
  const rows = [...t.rows].sort((a, b) => b.live_return - a.live_return || (b.bt_checked?.sharpe ?? 0) - (a.bt_checked?.sharpe ?? 0));
  $("#t-table").innerHTML = `<tr><th>Strategy</th><th class="r">Live return</th><th class="r">vs SPY</th><th class="r">Today</th><th class="r">Worst drop</th>
    <th class="r">Trades</th><th class="r">Days live</th><th class="r">Backtest held-out /yr</th><th class="r" title="10-year backtest re-run without the 6 stocks that rose the most, so it can't cheat with hindsight">Honest backtest /yr</th><th class="r">Honest worst drop</th><th class="r" title="The weakest Sharpe ratio across three tests: without the biggest winners, on random halves of the stock list, and over 30 years. Above 1 is good.">Honest Sharpe</th><th>Holding now</th></tr>` +
    rows.map((r) => `<tr><td><b style="color:${PALETTE[r.key]}">${esc(r.name)}</b>${r.champion ? ' <span class="champ">🏆</span>' : ""}<span class="about">${esc(r.about)}</span></td>
      <td class="r ${cls(r.live_return)}">${pct(r.live_return)}</td><td class="r ${cls(r.vs_spy)}">${r.started ? (r.vs_spy >= 0 ? "+" : "") + r.vs_spy.toFixed(2) + " pts" : "—"}</td>
      <td class="r ${cls(r.day_pl_pct)}">${pct(r.day_pl_pct)}</td><td class="r">${r.live_max_drawdown.toFixed(1)}%</td>
      <td class="r">${r.trades}</td><td class="r">${r.trading_days}/${t.live_days_needed}</td>
      <td class="r ${r.beats_spy_oos ? "up" : ""}">${pct(r.bt_oos.cagr)}</td><td class="r">${r.bt_checked ? pct(r.bt_checked.cagr) : "—"}</td><td class="r">${r.bt_checked ? r.bt_checked.max_drawdown.toFixed(1) + "%" : "—"}</td><td class="r">${r.honest_sharpe != null ? r.honest_sharpe.toFixed(2) : r.bt_checked ? r.bt_checked.sharpe.toFixed(2) : "—"}</td>
      <td class="muted small">${r.holdings.sort((a, b) => b.weight - a.weight).slice(0, 6).map((h) => `${h.symbol} ${h.weight.toFixed(0)}%`).join(", ") || "Waiting for market open"}</td></tr>`).join("");
  renderCrashTest(t);
  const curves = await api("/api/tournament/curves").catch(() => ({}));
  setLines(tChart, tLines, curves);
  $("#t-legend").innerHTML = legend(Object.keys(curves));
  if (!$("#bot-strategy").options.length) renderBot();
}

function renderCrashTest(t) {
  const wins = t.stress_windows || [];
  $("#crash-card").classList.toggle("hidden", !wins.length);
  if (!wins.length) return;
  const rows = [...t.rows].filter((r) => r.bt_stress).sort((a, b) => (b.honest_sharpe ?? 0) - (a.honest_sharpe ?? 0));
  const cell = (w) => !w ? '<td class="r muted" title="This strategy\'s funds did not exist yet">n/a</td>' : `<td class="r ${cls(w.ret)}" title="Worst drop inside this period: ${w.drop.toFixed(1)}%">${pct(w.ret)}</td>`;
  $("#crash-table").innerHTML = `<tr><th>Strategy</th>${wins.map((w) => `<th class="r">${esc(w)}</th>`).join("")}
    <th class="r">Worst drop since ${new Date((t.stress_start || 0) * 1000).getFullYear()}</th><th class="r">Per year since then</th>
    <th class="r" title="How often the strategy beat SPY when it was only allowed a random 60% of the stock list (${t.subsets} tries)">Beat SPY on random halves</th></tr>` +
    rows.map((r) => `<tr><td><b style="color:${PALETTE[r.key]}">${esc(r.name)}</b>${r.champion ? ' <span class="champ">🏆</span>' : ""}</td>
      ${r.bt_stress.windows.map(cell).join("")}<td class="r">${r.bt_stress.full.max_drawdown.toFixed(1)}%</td><td class="r">${pct(r.bt_stress.full.cagr)}</td>
      <td class="r">${r.bt_robust && !["spy_hold"].includes(r.key) ? r.bt_robust.beat_spy_pct.toFixed(0) + "%" : "—"}</td></tr>`).join("");
}

async function loadBacktest(force) {
  $("#bt-caveat").textContent = "Running 10-year backtest of every strategy…";
  try {
    const bt = force ? await api("/api/bot/backtest", { method: "POST" }) : await api("/api/bot/backtest");
    const curves = {};
    for (const [k, v] of Object.entries(bt.strategies)) {
      strategyNames[k] = v.name;
      curves[k] = v.curve.map((x) => ({ time: x.time, value: x.value * 10000 }));
    }
    setLines(btChart, btLines, curves);
    $("#bt-legend").innerHTML = legend(Object.keys(curves));
    const d = (t) => new Date(t * 1000).toLocaleDateString([], { month: "short", year: "numeric" });
    $("#bt-caveat").textContent = `${d(bt.start)} to ${d(bt.end)}. Held-out test period starts ${d(bt.split)}: strategies were never tuned on it. "Honest" numbers in the tournament table re-run every strategy without ${(bt.hindsight_winners || []).join(", ") || "the biggest winners"}. ${bt.caveat}`;
    if (force) refreshTournament();
  } catch (e) { $("#bt-caveat").textContent = "Backtest failed: " + e.message; }
}

$("#bot-enabled").addEventListener("change", async (e) => {
  state.bot = await api("/api/bot/enable", { method: "POST", body: { enabled: e.target.checked } });
  toast(e.target.checked ? "Bot is on. It trades during market hours." : "Bot is off.", "ok");
  renderBot();
});
$("#bot-mode").addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  await api("/api/settings", { method: "POST", body: { bot: { mode: b.dataset.m } } });
  refreshBot();
});
$("#bot-strategy").addEventListener("change", async (e) => {
  await api("/api/settings", { method: "POST", body: { bot: { strategy: e.target.value } } });
  toast(e.target.value === "auto" ? "Your account now follows the tournament champion." : `Your account now follows ${strategyNames[e.target.value]}.`, "ok");
  refreshBot();
});
$("#bot-run").addEventListener("click", async () => {
  $("#bot-run").disabled = true; $("#bot-run").textContent = "Working…";
  try { state.bot = await api("/api/bot/rebalance", { method: "POST" }); toast("Rebalance done. See Bot activity.", "ok"); renderBot(); refreshAccount(); }
  catch (e) { toast(e.message, "bad"); }
  finally { $("#bot-run").disabled = false; $("#bot-run").textContent = "Rebalance now"; }
});
$("#bot-kill").addEventListener("click", async () => {
  state.bot = await api("/api/bot/kill", { method: "POST" });
  toast("Kill switch: bot stopped and its open orders canceled.", "bad");
  renderBot();
});
$("#scan-run").addEventListener("click", async () => {
  $("#scan-run").textContent = "Scanning…";
  await api("/api/bot/scan", { method: "POST" }).catch((e) => toast(e.message, "bad"));
  $("#scan-run").textContent = "Scan now";
  refreshBot();
});
$("#bt-run").addEventListener("click", () => loadBacktest(true));
$("#report-run").addEventListener("click", async () => {
  try { const r = await api("/api/tournament/report", { method: "POST" }); toast(`Report card saved to ${r.path}`, "ok"); }
  catch (e) { toast(e.message, "bad"); }
});
$("#risk-save").addEventListener("click", async () => {
  const f = new FormData($("#risk-form")), bot = {};
  RISK_FIELDS.forEach(([k]) => (bot[k] = parseFloat(f.get(k))));
  await api("/api/settings", { method: "POST", body: { bot } });
  toast("Risk limits saved", "ok");
  refreshBot();
});
$("#view-bot").addEventListener("click", async (e) => {
  const a = e.target.closest("[data-approve]"), r = e.target.closest("[data-reject]"), row = e.target.closest("tr[data-s]");
  try {
    if (a) { state.bot = await api(`/api/bot/approve/${a.dataset.approve}`, { method: "POST" }); toast("Order sent", "ok"); renderBot(); refreshAccount(); }
    else if (r) { state.bot = await api(`/api/bot/reject/${r.dataset.reject}`, { method: "POST" }); renderBot(); }
    else if (row) selectSymbol(row.dataset.s);
  } catch (err) { toast(err.message, "bad"); }
});

// ---------- settings ----------
async function refreshSettings() {
  state.settings = await api("/api/settings");
  const s = state.settings;
  $$("#brokers button").forEach((b) => b.classList.toggle("active", b.dataset.b === s.broker));
  $("#k-pk").placeholder = s.alpaca_paper_key || "PK…";
  $("#k-ps").placeholder = s.alpaca_paper_secret ? "saved" : "";
  $("#k-lk").placeholder = s.alpaca_live_key || "AK…";
  $("#k-ls").placeholder = s.alpaca_live_secret ? "saved" : "";
  refreshDiscord(s);
  const badge = $("#broker-badge");
  badge.textContent = { sim: "SIMULATOR", alpaca_paper: "ALPACA PAPER", alpaca_live: "LIVE · REAL MONEY" }[s.broker];
  badge.classList.toggle("live", s.broker === "alpaca_live");
}
$("#brokers").addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  const body = { broker: b.dataset.b };
  if (b.dataset.b === "alpaca_live") {
    const typed = prompt("This uses REAL money. Type REAL MONEY to confirm.");
    if (typed !== "REAL MONEY") return;
    body.confirm_live = typed;
  }
  try {
    await api("/api/settings", { method: "POST", body });
    toast(`Now trading through ${b.querySelector("b").textContent}.`, "ok");
    await refreshSettings();
    refreshAccount(); refreshBot();
  } catch (err) { toast(err.message, "bad"); }
});
$("#keys-save").addEventListener("click", async () => {
  const body = { alpaca_paper_key: $("#k-pk").value, alpaca_paper_secret: $("#k-ps").value, alpaca_live_key: $("#k-lk").value, alpaca_live_secret: $("#k-ls").value };
  await api("/api/settings", { method: "POST", body });
  ["#k-pk", "#k-ps", "#k-lk", "#k-ls"].forEach((s) => ($(s).value = ""));
  toast("Keys saved. Now pick Alpaca Paper above.", "ok");
  refreshSettings();
});
// ---------- Discord ----------
function refreshDiscord(s) {
  if (!s.discord) return;
  const on = !!s.discord_webhook;
  $("#dc-url").placeholder = on ? `Connected (${s.discord_webhook})` : "https://discord.com/api/webhooks/…";
  const box = $("#dc-channels");
  if (box && !box.contains(document.activeElement)) {
    box.innerHTML = (s.discord_channels || []).map((c) => `<label class="dc-ch"><span><b>#${c.name}</b><small class="muted">${c.about}</small></span>
      <input data-dc-ch="${c.kind}" autocomplete="off" placeholder="${c.set ? "Connected. Paste a new link to replace, or type off to remove" : "https://discord.com/api/webhooks/…"}"></label>`).join("");
  }
  $("#dc-status").textContent = on ? (s.discord_error ? "Last send failed: " + s.discord_error : "Connected. Messages go to your Discord channel.") : "Not connected.";
  $$("[data-dc]").forEach((c) => (c.checked = !!s.discord[c.dataset.dc]));
  ["#dc-test", "#dc-summary", "#dc-clear"].forEach((id) => ($(id).disabled = !on));
}
$("#dc-save").addEventListener("click", async () => {
  const url = $("#dc-url").value.trim();
  if (!url) return toast("Paste the webhook link first.", "bad");
  try {
    await api("/api/discord/test", { method: "POST", body: { discord_webhook: url } });
    await api("/api/settings", { method: "POST", body: { discord_webhook: url } });
    $("#dc-url").value = "";
    toast("Discord connected. Check your channel for a hello message.", "ok");
    refreshSettings();
  } catch (e) { toast(e.message, "bad"); }
});
$$("[data-dc]").forEach((c) => c.addEventListener("change", async () => {
  try { await api("/api/settings", { method: "POST", body: { discord: { [c.dataset.dc]: c.checked } } }); }
  catch (e) { toast(e.message, "bad"); }
}));
$("#dc-ch-save").addEventListener("click", async () => {
  const hooks = {};
  $$("[data-dc-ch]").forEach((i) => { const v = i.value.trim(); if (v) hooks[i.dataset.dcCh] = v.toLowerCase() === "off" ? "" : v; });
  if (!Object.keys(hooks).length) return toast("Paste at least one webhook link first.", "bad");
  try {
    await api("/api/settings", { method: "POST", body: { discord_webhooks: hooks } });
    await api("/api/discord/refresh", { method: "POST" });
    $$("[data-dc-ch]").forEach((i) => (i.value = ""));
    toast("Channels saved.", "ok"); refreshSettings();
  } catch (e) { toast(e.message, "bad"); }
});
$("#dc-refresh").addEventListener("click", async () => {
  try { await api("/api/discord/refresh", { method: "POST" }); toast("Dashboard updated.", "ok"); }
  catch (e) { toast(e.message, "bad"); }
});
$("#dc-test").addEventListener("click", async () => {
  try { await api("/api/discord/test", { method: "POST", body: {} }); toast("Test message sent.", "ok"); }
  catch (e) { toast(e.message, "bad"); }
});
$("#dc-summary").addEventListener("click", async () => {
  try { await api("/api/discord/summary", { method: "POST" }); toast("Summary sent.", "ok"); }
  catch (e) { toast(e.message, "bad"); }
});
$("#dc-clear").addEventListener("click", async () => {
  if (!confirm("Stop sending to Discord?")) return;
  await api("/api/settings", { method: "POST", body: { discord_clear: true } });
  toast("Discord disconnected.", "ok"); refreshSettings();
});
$("#reset").addEventListener("click", async () => {
  if (!confirm("Erase all simulator trades and start fresh?")) return;
  try {
    await api("/api/sim/reset", { method: "POST", body: { starting_cash: parseFloat($("#reset-cash").value) } });
    toast("Simulator reset", "ok"); refreshAccount(); refreshBot();
  } catch (e) { toast(e.message, "bad"); }
});

// ---------- website control settings (on the Mac only) ----------
let passcodeShown = false;
async function refreshRemoteCard() {
  if (REMOTE.base || SNAPSHOT) return;
  const c = await api("/api/remote/config").catch(() => null);
  if (!c) return;
  $("#remote-enabled").checked = c.enabled;
  const status = !c.cloudflared_installed ? '<span class="down">Tunnel tool missing (brew install cloudflared)</span>'
    : !c.enabled ? '<span class="muted">Off</span>' : c.url ? '<span class="up">Online</span>' : '<span class="muted">Connecting…</span>';
  $("#remote-info").innerHTML = [["Status", status], ["Passcode", passcodeShown ? `<code>${esc(c.passcode)}</code>` : "••••-••••-••••"],
    ["Signed-in devices", c.devices]].map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("");
  $("#remote-reveal").textContent = passcodeShown ? "Hide passcode" : "Show passcode";
}
$("#remote-reveal").addEventListener("click", () => { passcodeShown = !passcodeShown; refreshRemoteCard(); });
$("#remote-enabled").addEventListener("change", async (e) => {
  await api("/api/remote/config", { method: "POST", body: { enabled: e.target.checked } });
  toast(e.target.checked ? "Website control on." : "Website control off. The website is view-only.", "ok");
  refreshRemoteCard();
});
$("#remote-new").addEventListener("click", async () => {
  if (!confirm("Make a new passcode? Every device that's logged in will be signed out.")) return;
  await api("/api/remote/config", { method: "POST", body: { new_passcode: true } });
  passcodeShown = true; toast("New passcode created.", "ok"); refreshRemoteCard();
});
$("#remote-revoke").addEventListener("click", async () => {
  if (!confirm("Sign out every device using the website?")) return;
  await api("/api/remote/config", { method: "POST", body: { revoke_all: true } });
  toast("All devices signed out.", "ok"); refreshRemoteCard();
});
$("#remote-logout").addEventListener("click", logoutRemote);

// ---------- navigation ----------
function showView(v) {
  state.view = v;
  $$("nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  $$(".view").forEach((el) => el.classList.toggle("hidden", el.id !== "view-" + v));
  if (v === "portfolio") renderPortfolio();
  if (v === "bot") {
    renderBot(); refreshTournament(); refreshTrack();
    if (!Object.keys(btLines).length) loadBacktest(false);
    if (!state.bot?.last_scan) api("/api/bot/scan", { method: "POST" }).then(refreshBot).catch(() => {});
  }
  if (v === "settings") { refreshSettings(); refreshRemoteCard(); }
}
$("nav").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) showView(b.dataset.view); });

async function refreshClock() {
  const c = await api("/api/clock").catch(() => null);
  if (!c) return;
  const pill = $("#market-pill");
  if (SNAPSHOT) {
    const age = (Date.now() / 1000 - core.updated) / 60;
    pill.textContent = `Updated ${age < 60 ? Math.max(1, Math.round(age)) + " min ago" : when(core.updated)}`;
    $("#snapshot-updated").textContent = when(core.updated);
    return;
  }
  pill.classList.toggle("open", c.is_open);
  const t = (x) => new Date(x * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  pill.textContent = c.is_open ? `Market open · closes ${t(c.session_end)}` : "Market closed";
}

// ---------- boot ----------
(async function boot() {
  if (SNAPSHOT) {
    core = await fetch("data/core.json", { cache: "no-store" }).then((r) => r.json()).catch(() => null);
    await connectRemote();
  }
  if (SNAPSHOT) {
    document.body.classList.add("snapshot");
    $$("#ranges button").forEach((b) => { if (!SNAPSHOT_RANGES.includes(b.dataset.r)) b.remove(); });
    // Pick up newly published results without a reload.
    setInterval(() => { core = null; for (const k in symFiles) delete symFiles[k]; fillsCache.time = 0; }, 5 * 60000);
  }
  await Promise.all([refreshSettings(), refreshAccount(), refreshBot(), refreshClock()]);
  await refreshWatchlist();
  selectSymbol(state.symbol);
  setInterval(refreshQuote, 5000);
  setInterval(refreshWatchlist, 10000);
  setInterval(refreshAccount, 10000);
  refreshAlerts(); setInterval(refreshAlerts, 15000);
  watchFills(); setInterval(watchFills, 8000);
  refreshTrack(); setInterval(refreshTrack, 5 * 60000);
  setInterval(refreshBot, 15000);
  setInterval(() => { if (state.view === "bot") refreshTournament(); }, 60000);
  setInterval(refreshClock, 60000);
  setInterval(() => { if (state.range === "1d" && state.quote?.market_open) loadChart(); }, 60000);
})();
