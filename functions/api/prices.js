// Cloudflare Pages Function: live price + short intraday trend per symbol.
// Mapped to /api/prices?symbols=T,PFE,F,... Keys live server-side in
// context.env, same pattern as /api/live. Uses Alpaca's free IEX feed.

const DATA_BASE = "https://data.alpaca.markets/v2/stocks";

function num(x) {
  const n = parseFloat(x);
  return Number.isFinite(n) ? n : 0;
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: {
      "content-type": "application/json",
      // Cache briefly at the edge — dashboard polls this every 60s.
      "cache-control": "s-maxage=30, stale-while-revalidate=60",
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

  const url = new URL(context.request.url);
  const symbolsParam = (url.searchParams.get("symbols") || "").trim();
  if (!symbolsParam) {
    return json({ error: "missing ?symbols=A,B,C" }, 400);
  }
  const symbols = [...new Set(symbolsParam.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean))];
  if (!symbols.length) {
    return json({ error: "no valid symbols" }, 400);
  }

  const headers = { "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret };
  const symQuery = symbols.join(",");

  // Last 4 hours of 5-min bars gives ~48 points — enough for a short trend line
  // without needing exchange-calendar logic for "market open today".
  const end = new Date();
  const start = new Date(end.getTime() - 4 * 60 * 60 * 1000);
  const barsUrl =
    `${DATA_BASE}/bars?symbols=${symQuery}&timeframe=5Min&feed=iex` +
    `&start=${start.toISOString()}&end=${end.toISOString()}&limit=1000&adjustment=raw`;
  const snapUrl = `${DATA_BASE}/snapshots?symbols=${symQuery}&feed=iex`;

  try {
    const [barsR, snapR] = await Promise.all([fetch(barsUrl, { headers }), fetch(snapUrl, { headers })]);
    if (!barsR.ok && !snapR.ok) {
      return json({ error: `Alpaca data ${barsR.status}/${snapR.status}` }, 502);
    }
    const barsData = barsR.ok ? await barsR.json() : { bars: {} };
    const snapData = snapR.ok ? await snapR.json() : {};

    const out = {};
    for (const sym of symbols) {
      const snap = snapData[sym] || {};
      const price = num(snap.latestTrade?.p) || num(snap.dailyBar?.c) || 0;
      const prevClose = num(snap.prevDailyBar?.c);
      const changePct = prevClose ? ((price - prevClose) / prevClose) * 100 : 0;
      const bars = (barsData.bars?.[sym] || []).map((b) => num(b.c));
      out[sym] = { price, change_pct: changePct, trend: bars };
    }

    return json({ generated_at: new Date().toISOString(), prices: out });
  } catch (e) {
    return json({ error: String(e) }, 502);
  }
}
