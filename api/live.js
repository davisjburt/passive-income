// Vercel serverless function: returns LIVE account + positions from Alpaca.
// Keys live in Vercel env vars (server-side) and are never sent to the browser.
// The dashboard overlays this on top of the committed docs/data.json snapshot.

const BASE = "https://paper-api.alpaca.markets/v2";

function num(x) {
  const n = parseFloat(x);
  return Number.isFinite(n) ? n : 0;
}

export default async function handler(req, res) {
  const key = process.env.ALPACA_API_KEY;
  const secret = process.env.ALPACA_API_SECRET;
  if (!key || !secret) {
    res.status(500).json({ error: "Missing ALPACA_API_KEY / ALPACA_API_SECRET env vars" });
    return;
  }
  const headers = { "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret };

  try {
    const [acctR, posR, clockR] = await Promise.all([
      fetch(`${BASE}/account`, { headers }),
      fetch(`${BASE}/positions`, { headers }),
      fetch(`${BASE}/clock`, { headers }),
    ]);

    if (!acctR.ok) {
      res.status(502).json({ error: `Alpaca account ${acctR.status}` });
      return;
    }

    const a = await acctR.json();
    const positionsRaw = posR.ok ? await posR.json() : [];
    const clock = clockR.ok ? await clockR.json() : { is_open: false };

    const equity = num(a.equity);
    const lastEquity = num(a.last_equity);

    const payload = {
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
    };

    // Cache briefly at the edge so we don't hammer Alpaca on every refresh.
    res.setHeader("Cache-Control", "s-maxage=20, stale-while-revalidate=40");
    res.status(200).json(payload);
  } catch (e) {
    res.status(502).json({ error: String(e) });
  }
}
