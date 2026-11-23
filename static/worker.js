/**
 * Price proxy for the Investment Tracker (Cloudflare Worker).
 *
 * Why it exists: Yahoo Finance does not allow web pages to call it directly,
 * so the tracker page asks this Worker, and the Worker asks Yahoo.
 * It only forwards the few price addresses listed below, nothing else.
 *
 * It never sees your holdings: only a symbol such as "RELIANCE.NS".
 */

// Optional: put your site's address here so other websites cannot use your proxy.
// Example: ['https://yourname.github.io']   (no slash at the end)
// Leave empty to allow any site.
const ALLOWED_SITES = [];

const ALLOWED_TARGETS = {
  'query1.finance.yahoo.com': [/^\/v8\/finance\/chart\/[^/]+$/, /^\/v1\/finance\/search$/],
  'api.mfapi.in': [/^\/mf\/[\w/]+$/],
  'api.coingecko.com': [/^\/api\/v3\/(simple\/price|search|coins\/[^/]+\/market_chart)$/],
};

const json = (body, status, headers) =>
  new Response(JSON.stringify(body), { status, headers: { ...headers, 'Content-Type': 'application/json' } });

export default {
  async fetch(request, env, ctx) {
    const origin = request.headers.get('Origin') || '';
    const siteOk = ALLOWED_SITES.length === 0 || ALLOWED_SITES.includes(origin);
    const cors = {
      'Access-Control-Allow-Origin': ALLOWED_SITES.length === 0 ? '*' : siteOk ? origin : 'null',
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Vary': 'Origin',
    };
    if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers: cors });
    if (request.method !== 'GET') return json({ error: 'only GET is supported' }, 405, cors);
    if (!siteOk) return json({ error: 'this site is not allowed to use the proxy' }, 403, cors);

    const asked = new URL(request.url).searchParams.get('url');
    if (!asked) return json({ ok: true, about: 'Investment Tracker price proxy. Use ?url=<price address>' }, 200, cors);
    let target;
    try { target = new URL(asked); } catch { return json({ error: 'invalid url' }, 400, cors); }
    const rules = ALLOWED_TARGETS[target.hostname];
    if (target.protocol !== 'https:' || target.port || !rules || !rules.some((r) => r.test(target.pathname))) {
      return json({ error: 'this address is not on the allowed list' }, 403, cors);
    }

    // Reuse an answer for 5 minutes so the price sources are not asked too often.
    const cacheKey = new Request(target.toString());
    let cache = null, hit = null;
    try { cache = caches.default; hit = await cache.match(cacheKey); } catch {}
    if (hit) {
      const res = new Response(hit.body, hit);
      for (const [k, v] of Object.entries(cors)) res.headers.set(k, v);
      return res;
    }

    let upstream;
    try {
      upstream = await fetch(target.toString(), {
        headers: { 'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json' },
      });
    } catch {
      return json({ error: 'could not reach the price source' }, 502, cors);
    }
    const res = new Response(upstream.body, {
      status: upstream.status,
      headers: {
        'Content-Type': upstream.headers.get('Content-Type') || 'application/json',
        'Cache-Control': 'public, max-age=300',
        ...cors,
      },
    });
    if (upstream.ok && cache) {
      try { ctx.waitUntil(cache.put(cacheKey, res.clone())); } catch {}
    }
    return res;
  },
};
