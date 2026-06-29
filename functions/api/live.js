// Cloudflare Pages Function: returns LIVE account + positions from Alpaca.
// Mapped to the route /api/live. Keys live in Cloudflare env vars (server-side,
// via context.env) and are never sent to the browser. The dashboard overlays
// this on top of the committed docs/data.json snapshot.

const BASE = "https://paper-api.alpaca.markets/v2";

function num(x) {
  const n = parseFloat(x);
  return Number.isFinite(n) ? n : 0;
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
  const { env } = context;
  const key = env.ALPACA_API_KEY;
  const secret = env.ALPACA_API_SECRET;
  if (!key || !secret) {
    return json({ error: "Missing ALPACA_API_KEY / ALPACA_API_SECRET env vars" }, 500);
  }
  const headers = { "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret };

  try {
    const [acctR, posR, clockR] = await Promise.all([
      fetch(`${BASE}/account`, { headers }),
      fetch(`${BASE}/positions`, { headers }),
      fetch(`${BASE}/clock`, { headers }),
    ]);

    if (!acctR.ok) {
      return json({ error: `Alpaca account ${acctR.status}` }, 502);
    }

    const a = await acctR.json();
    const positionsRaw = posR.ok ? await posR.json() : [];
    const clock = clockR.ok ? await clockR.json() : { is_open: false };

    const equity = num(a.equity);
    const lastEquity = num(a.last_equity);

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
      },
      positions: (positionsRaw || []).map((p) => ({
        symbol: p.symbol,
        qty: num(p.qty),
        avg_entry: num(p.avg_entry_price),
        current_price: num(p.current_price),
        market_value: num(p.market_value),
        unrealized_pl: num(p.unrealized_pl),
        unrealized_plpc: num(p.unrealized_plpc) * 100,
      })),
    });
  } catch (e) {
    return json({ error: String(e) }, 502);
  }
}
