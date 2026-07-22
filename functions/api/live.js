// Cloudflare Pages Function: returns LIVE account + positions from Alpaca.
// Mapped to the route /api/live?account=<slug>. Keys live in Cloudflare env
// vars (server-side, via context.env) and are never sent to the browser. The
// dashboard overlays this on top of the committed docs/wheel*.json snapshot.
//
// account=default (or omitted) reads ALPACA_API_KEY/SECRET; any other slug
// reads ALPACA_<SLUG>_API_KEY/SECRET, matching the Python side's convention
// in bot/wheel/config.py so both halves of the pipeline agree on naming.

const BASE = "https://paper-api.alpaca.markets/v2";
const DATA_BASE = "https://data.alpaca.markets/v2/stocks";
// OCC option symbol, e.g. "F260710P00013500" -> AAPL, 2026-07-10, put, 13.50
const OCC = /^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/;

function num(x) {
  const n = parseFloat(x);
  return Number.isFinite(n) ? n : 0;
}

function parseOcc(symbol) {
  const m = OCC.exec(symbol);
  if (!m) return null;
  const [, root, yy, mm, dd, cp, strike] = m;
  return {
    underlying: root,
    expiration: `20${yy}-${mm}-${dd}`,
    type: cp === "C" ? "call" : "put",
    strike: parseInt(strike, 10) / 1000,
  };
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: {
      "content-type": "application/json",
      // Cache briefly at the edge so we don't hammer Alpaca on every refresh.
      "cache-control": "s-maxage=20, stale-while-revalidate=40",
    },
  });
}

export async function onRequestGet(context) {
  const { env, request } = context;
  const account = (new URL(request.url).searchParams.get("account") || "default").toLowerCase();
  const [keyVar, secretVar] = account === "default"
    ? ["ALPACA_API_KEY", "ALPACA_API_SECRET"]
    : [`ALPACA_${account.toUpperCase()}_API_KEY`, `ALPACA_${account.toUpperCase()}_API_SECRET`];
  const key = env[keyVar];
  const secret = env[secretVar];
  if (!key || !secret) {
    return json({ error: `Missing ${keyVar} / ${secretVar} env vars` }, 500);
  }
  const headers = { "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret };

  try {
    const [acctR, posR, clockR, ordR, histR] = await Promise.all([
      fetch(`${BASE}/account`, { headers }),
      fetch(`${BASE}/positions`, { headers }),
      fetch(`${BASE}/clock`, { headers }),
      fetch(`${BASE}/orders?status=open`, { headers }),
      fetch(`${BASE}/account/portfolio/history?period=1A&timeframe=1D`, { headers }),
    ]);

    if (!acctR.ok) {
      return json({ error: `Alpaca account ${acctR.status}` }, 502);
    }

    const a = await acctR.json();
    const positionsRaw = posR.ok ? await posR.json() : [];
    const clock = clockR.ok ? await clockR.json() : { is_open: false };
    const ordersRaw = ordR.ok ? await ordR.json() : [];

    const equity = num(a.equity);
    const lastEquity = num(a.last_equity);

    const stockPositions = [];
    const optionPositions = [];
    for (const p of positionsRaw || []) {
      const ac = p.asset_class;
      if (ac === "us_option") {
        const occ = parseOcc(p.symbol);
        if (!occ) continue;
        optionPositions.push({
          underlying: occ.underlying,
          type: occ.type,
          strike: occ.strike,
          expiration: occ.expiration,
          qty: num(p.qty),
          market_value: num(p.market_value),
          unrealized_pl: num(p.unrealized_pl),
        });
      } else {
        stockPositions.push({
          symbol: p.symbol,
          qty: num(p.qty),
          avg_entry: num(p.avg_entry_price),
          current_price: num(p.current_price),
          market_value: num(p.market_value),
          unrealized_pl: num(p.unrealized_pl),
          unrealized_plpc: num(p.unrealized_plpc) * 100,
        });
      }
    }

    // Exposure = stock notional + cash reserved against open short puts
    // (filled positions and resting sell-to-open orders both count, matching
    // the Python bot's PositionsView.exposure() + pending-put logic). Must
    // multiply by contract qty -- positions are no longer always 1 contract
    // now that the bot sizes up to the per-stock cap.
    let exposure = stockPositions.reduce((sum, p) => sum + p.qty * p.current_price, 0);
    exposure += optionPositions
      .filter((o) => o.type === "put")
      .reduce((sum, o) => sum + o.strike * 100 * Math.abs(o.qty), 0);
    for (const o of ordersRaw || []) {
      const occ = parseOcc(o.symbol);
      if (occ && occ.type === "put" && o.side === "sell") {
        exposure += occ.strike * 100 * Math.abs(num(o.qty) || 1);
      }
    }

    // Benchmark vs SPY: this account's total return since it was funded,
    // against SPY's price return over that same window. Best-effort -- the
    // dashboard just hides the section if any of this fails.
    let benchmark = null;
    try {
      const hist = histR.ok ? await histR.json() : null;
      const baseValue = hist ? num(hist.base_value) : 0;
      const sinceDate = hist?.base_value_asof;
      if (baseValue && sinceDate) {
        const [barsR, snapR] = await Promise.all([
          fetch(
            `${DATA_BASE}/SPY/bars?timeframe=1Day&feed=iex&adjustment=raw` +
              `&start=${sinceDate}&end=${new Date().toISOString().slice(0, 10)}&limit=1000`,
            { headers },
          ),
          fetch(`${DATA_BASE}/SPY/snapshot?feed=iex`, { headers }),
        ]);
        const bars = barsR.ok ? (await barsR.json()).bars || [] : [];
        const snap = snapR.ok ? await snapR.json() : null;
        const spyStart = bars.length ? num(bars[0].c) : 0;
        const spyNow = num(snap?.latestTrade?.p) || num(snap?.dailyBar?.c) ||
          (bars.length ? num(bars[bars.length - 1].c) : 0);
        if (spyStart && spyNow) {
          benchmark = {
            since: sinceDate,
            account_return_pct: ((equity - baseValue) / baseValue) * 100,
            spy_return_pct: ((spyNow - spyStart) / spyStart) * 100,
          };
        }
      }
    } catch (_) { /* benchmark is optional overlay, never fail the main response for it */ }

    return json({
      generated_at: new Date().toISOString(),
      live: true,
      market_open: !!clock.is_open,
      account: {
        equity,
        last_equity: lastEquity,
        cash: num(a.cash),
        buying_power: num(a.buying_power),
        portfolio_value: num(a.portfolio_value),
        day_pl: equity - lastEquity,
        day_pl_pct: lastEquity ? ((equity - lastEquity) / lastEquity) * 100 : 0,
        exposure_pct: equity ? (exposure / equity) * 100 : 0,
      },
      positions: stockPositions,
      option_positions: optionPositions,
      benchmark,
    });
  } catch (e) {
    return json({ error: String(e) }, 502);
  }
}
