# yopu-pdf egress proxy (Cloudflare Worker)

A tiny, **locked, single-purpose** fetch relay that lets `yopu-pdf` keep working
when your own IP is rate-limited/blocked by yopu.co.

## Why

yopu.co bans clients by request volume: a laptop or a small VPS trips it in a
handful of app-host requests, after which `/view/<id>` returns a bare `404` with
an empty body even though the sheet is public. Cloudflare's edge egress is a
large pool the site does not ban, so routing **only** the same-origin `yopu.co`
requests (the tiny HTML document and two `/z` data calls) through a Worker keeps
the tool working. The heavy `cdn.yopu.co` assets and everything else stay direct.

## Not an open proxy

- Every request must carry the shared key `k` (constant-time compared).
- Only `yopu.co` / `www.yopu.co` / `scdn.yopu.co` / `cdn.yopu.co` may be fetched.
- The caller's real request is carried across faithfully: the upstream method in
  `x-yp-method` and the full upstream header set as base64(JSON) in
  `x-yp-headers`, and **every** upstream response header is relayed back except
  hop-by-hop ones — the sheet engine reads `X-Signature` / `X-Used-Nonce` and the
  session cookie back off its `/z` responses, so dropping them breaks rendering.

## Deploy

```bash
npx wrangler secret put PROXY_KEY   # paste a long random string (see below)
npx wrangler deploy                 # prints https://<name>.<subdomain>.workers.dev
```

Generate a key with `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`
and keep a copy in `proxy.key` (gitignored). Then point the tool at it — one line
in `~/.config/yopu-pdf/egress`:

```text
worker https://<name>.<subdomain>.workers.dev/?k=<that key>
```

## Endpoints

- `GET /health` → `ok` (no key needed).
- `GET /?k=<key>&u=<url-encoded target>` → relays the target. `403` on a wrong
  key, `400` on a disallowed host, `x-yp-upstream-status` carries the real code.

## Caveats

- **It cannot render yopu.co sheets.** The sheet call `/api/sheet?code=<id>` needs
  the session cookie the document mints, and that cookie is bound to the exit IP.
  Cloudflare varies its edge egress IP between requests, so the cookie minted on
  the document call is rejected (404) on the `/api/sheet` call. For yopu.co use an
  `ssh:<host>` egress or a single-IP proxy instead — both send every request from
  one IP. This Worker is kept for endpoints that are not cookie/IP-bound.
- `wrangler` auths against your Cloudflare account; the deployed Worker lives
  there. Delete it with `npx wrangler delete` if you no longer want it.
