// yopu-pdf egress proxy — a LOCKED, single-purpose fetch relay.
//
// Why this exists: yopu.co IP-bans clients by request volume, and a laptop or a
// small VPS trips it in a handful of requests. Cloudflare's edge egress is a
// large rotating pool the site does not ban, so routing ONLY the same-origin
// yopu.co requests (the tiny HTML doc + the /z data calls) through here keeps
// the tool working. It is not an open proxy:
//   * every request must carry the shared key `k` (constant-time compared),
//   * only yopu.co and its own subdomains may be fetched,
//   * the caller sends the upstream method in x-yp-method and the FULL upstream
//     header set as base64(JSON) in x-yp-headers, so the site's request-signing
//     protocol (nonce in the URL, X-Signature / X-Used-Nonce in the response) is
//     preserved end to end. Every upstream response header is relayed back
//     except hop-by-hop ones, because the sheet engine reads X-Signature.
const ALLOW = new Set(["yopu.co", "www.yopu.co", "scdn.yopu.co", "cdn.yopu.co"]);
const HOP = new Set(["content-encoding", "content-length", "transfer-encoding", "connection", "keep-alive"]);

function timingSafeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let out = 0;
  for (let i = 0; i < a.length; i++) out |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return out === 0;
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname === "/health") return new Response("ok", { status: 200 });
    if (!env.PROXY_KEY || !timingSafeEqual(url.searchParams.get("k") || "", env.PROXY_KEY)) {
      return new Response("forbidden", { status: 403 });
    }
    const target = url.searchParams.get("u");
    if (!target) return new Response("missing u", { status: 400 });
    let t;
    try { t = new URL(target); } catch { return new Response("bad u", { status: 400 }); }
    if (t.protocol !== "https:" || !ALLOW.has(t.hostname)) {
      return new Response("host not allowed", { status: 400 });
    }
    // Rebuild the upstream headers from the caller's declared set.
    const h = new Headers();
    const packed = req.headers.get("x-yp-headers");
    if (packed) {
      try {
        const obj = JSON.parse(atob(packed));
        for (const [k, v] of Object.entries(obj)) {
          const lk = k.toLowerCase();
          if (lk === "host" || lk === "content-length" || lk.startsWith("x-yp-")) continue;
          h.set(k, v);
        }
      } catch (_) { /* fall through to defaults below */ }
    }
    if (!h.has("user-agent")) h.set("user-agent", "Mozilla/5.0");
    if (!h.has("accept-language")) h.set("accept-language", "zh-CN,zh;q=0.9,en;q=0.8");
    const method = req.headers.get("x-yp-method") || "GET";
    const body = (method === "GET" || method === "HEAD") ? undefined : req.body;
    let up;
    try {
      up = await fetch(t.toString(), { method, headers: h, body, redirect: "follow" });
    } catch (e) {
      return new Response("upstream error: " + e, { status: 502 });
    }
    // Relay every upstream response header except hop-by-hop, so X-Signature /
    // X-Used-Nonce (and Set-Cookie) reach the sheet engine intact.
    const rh = new Headers();
    for (const [k, v] of up.headers) {
      if (!HOP.has(k.toLowerCase())) rh.set(k, v);
    }
    rh.set("access-control-allow-origin", "*");
    rh.set("access-control-expose-headers", "*");
    rh.set("x-yp-upstream-status", String(up.status));
    return new Response(up.body, { status: up.status, headers: rh });
  },
};
