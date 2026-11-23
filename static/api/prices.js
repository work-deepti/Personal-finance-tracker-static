/**
 * Price service for the Investment Tracker, as a Vercel Function.
 *
 * Why it exists: Yahoo Finance does not allow web pages to call it directly,
 * so the tracker page asks this function, and the function asks Yahoo.
 * It only forwards the few price addresses listed below, nothing else,
 * and it only ever sees ticker symbols such as "RELIANCE.NS", never your holdings.
 *
 * The page finds it by itself at /api/prices. There is nothing to configure.
 */
const ALLOWED_TARGETS = {
  'query1.finance.yahoo.com': [/^\/v8\/finance\/chart\/[^/]+$/, /^\/v1\/finance\/search$/],
  'api.mfapi.in': [/^\/mf\/[\w/]+$/],
  'api.coingecko.com': [/^\/api\/v3\/(simple\/price|search|coins\/[^/]+\/market_chart)$/],
};

const json = (body, status) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' } });

export default {
  async fetch(request) {
    if (request.method !== 'GET' && request.method !== 'HEAD') return json({ error: 'only GET is supported' }, 405);
    const asked = new URL(request.url, 'https://localhost').searchParams.get('url');
    if (!asked) return json({ ok: true, about: 'Investment Tracker price proxy. Use ?url=<price address>' }, 200);
    let target;
    try { target = new URL(asked); } catch { return json({ error: 'invalid url' }, 400); }
    const rules = ALLOWED_TARGETS[target.hostname];
    if (target.protocol !== 'https:' || target.port || !rules || !rules.some((r) => r.test(target.pathname))) {
      return json({ error: 'this address is not on the allowed list' }, 403);
    }
    let upstream;
    try {
      upstream = await fetch(target.toString(), {
        headers: { 'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json' },
        signal: AbortSignal.timeout(8000),
      });
    } catch {
      return json({ error: 'could not reach the price source' }, 502);
    }
    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        'Content-Type': upstream.headers.get('Content-Type') || 'application/json',
        // Good answers are reused for 5 minutes so the price sources are not asked too often.
        'Cache-Control': upstream.ok ? 'public, s-maxage=300, max-age=60' : 'no-store',
      },
    });
  },
};
