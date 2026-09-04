#!/usr/bin/env python3
"""Egress routing for yopu-pdf.

yopu.co IP-bans clients by request *volume*: a laptop or a small VPS trips it in
a handful of app-host requests, and a blocked IP then gets a bare ``404`` with an
empty body on ``/view/<id>`` -- a lie, since the sheet is public and returns 200
from any un-flagged vantage. Recovery is uneven and slow: the CACHED ``/view``
document starts returning 200 again within ~tens of minutes, but the DYNAMIC
``/api/sheet`` endpoint stays banned for HOURS on a heavily-hit IP. So a doc 200
does not mean the IP is usable -- the sheet call is the real test. Each render is
~3 app-host requests; budget one render per IP per long cooldown.

Two facts make a narrow fix possible:

* Only the **app host** (``yopu.co`` and its ``scdn`` sibling) enforces the ban.
  The static CDN ``cdn.yopu.co`` (all the JS/CSS/images) is never banned.
* A single render touches the app host only ~3 times (the tiny HTML document and
  two ``/z`` data calls). The audio samples under ``yopu.co/sound/`` are
  same-origin ``media`` but load lazily on play, not during a print render.

So we route **only the same-origin app-host requests** through a healthy egress,
leave the CDN and everything else direct, and drop ``media`` + analytics so the
footprint (and thus the ban pressure) stays tiny.

The sheet call ``/api/sheet?code=<id>`` (hidden behind ``/z/<obfuscated>``) needs
the session cookie the document mints, and that cookie is bound to the exit IP —
so a usable egress must send *every* app-host request from ONE IP. That is why
``ssh:<host>`` (a per-request ``curl`` relay with a shared remote cookie jar) is
the primary mode, a single-IP proxy is the alternative, and the Cloudflare
``worker`` relay does not work for sheets (CF varies its egress IP per request).

This module is the pure, testable half: egress resolution, request
classification, the Worker and SSH relays, and the ``/z`` decoder. The Playwright
wiring lives in ``yopu_pdf.py``.
"""
from __future__ import annotations

import base64

def safe_b64decode(data: bytes) -> bytes:
    clean = b"".join(data.split())
    pad = len(clean) % 4
    if pad:
        clean += b"=" * (4 - pad)
    return base64.b64decode(clean)
import json
import os
import shlex
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlparse


class EgressUnavailable(Exception):
    """An egress lane cannot be set up (missing dep, unresolved proxy). The caller
    treats it like a lane that failed and moves to the next one in the chain."""


# The banned host and its same-origin sibling. cdn.yopu.co is deliberately NOT
# here: it is a separate, un-banned CDN and must stay direct (proxying ~1MB of
# JS would be slow and pointless).
APP_HOSTS = frozenset({"yopu.co", "www.yopu.co", "scdn.yopu.co"})

# Analytics / ad / error-reporting hosts the page beacons to. They never carry
# sheet content, they are what makes ``networkidle`` never settle, and dropping
# them cuts requests. Matched as substrings of the full URL.
TRACKER_SUBSTRINGS = (
    "google-analytics.com", "googletagmanager.com", "www.googletagmanager",
    "doubleclick.net", "hm.baidu.com", "sentry.io", "ynuf.aliapp.org",
    "tdum.alibaba.com", "cf.aliyun.com", "recaptcha", "gstatic.com",
    "bilibili.com", "upload.qiniup.com", "uplog.qbox.me", "hooks.slack.com",
)

# A browser-shaped UA for the OUTER request to the Worker: workers.dev sits
# behind Cloudflare's own bot protection, which 1010-blocks the default
# ``Python-urllib`` agent before the request ever reaches our code.
OUTER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

# Response headers that must NOT be replayed to Chrome: hop-by-hop ones, and the
# length/encoding fields (the body is already decoded by the time we relay it, so
# a stale content-length/encoding would corrupt it). Everything else IS relayed
# -- crucially X-Signature / X-Used-Nonce / Set-Cookie, which the sheet engine
# reads back from its /z responses.
_RESP_SKIP = frozenset({
    "content-encoding", "content-length", "transfer-encoding", "connection",
    "keep-alive", "x-yp-upstream-status",
    "access-control-allow-origin", "access-control-expose-headers",
})

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "yopu-pdf"


def config_path() -> Path:
    """Where the egress spec is read from. ``YOPU_PDF_CONFIG_DIR`` overrides the
    directory (used by the tests to stay hermetic)."""
    base = os.environ.get("YOPU_PDF_CONFIG_DIR")
    return (Path(base) if base else DEFAULT_CONFIG_DIR) / "egress"


def _read_config_file() -> str | None:
    """First non-comment, non-blank line of the egress config, or None.

    The predicate is "the file names an egress", not merely "the file exists":
    a blank or all-comment file reads as *unconfigured*, not as a broken egress.
    """
    try:
        text = config_path().read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


def resolve_egress(cli_value: str | None) -> str | None:
    """The egress spec, by precedence: CLI flag, ``YOPU_PDF_EGRESS`` env, config
    file. ``None`` means "go direct". Empty/whitespace at any layer is skipped so
    ``--egress ''`` does not shadow the config."""
    for src in (cli_value, os.environ.get("YOPU_PDF_EGRESS"), _read_config_file()):
        if src and src.strip():
            return src.strip()
    return None


def _split_lanes(spec: str) -> list[str]:
    """A spec string -> ordered lane list. Commas separate lanes, so one line can
    say ``ssh:vps,proxy:vn``. Empty pieces are dropped."""
    return [p.strip() for p in spec.split(",") if p.strip()]


def _read_config_lanes() -> list[str]:
    """Every non-comment, non-blank line of the config, each split on commas, in
    file order -- the ordered fallback chain."""
    try:
        text = config_path().read_text(encoding="utf-8")
    except OSError:
        return []
    lanes: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            lanes.extend(_split_lanes(line))
    return lanes


def resolve_egress_chain(cli_value: str | None) -> list[str]:
    """The ordered fallback chain of egress lanes tried AFTER a direct attempt,
    by precedence: CLI flag, ``YOPU_PDF_EGRESS`` env, then the config file. The
    first source that yields any lane wins (they do not stack), so ``--egress``
    fully overrides the config. Each source may list several lanes (comma-
    separated, or one per config line): ``ssh:my-vps,proxy:vn`` means try the VPS
    first, then the residential lane."""
    for src in (cli_value, os.environ.get("YOPU_PDF_EGRESS")):
        if src and src.strip():
            return _split_lanes(src)
    return _read_config_lanes()


def parse_egress(spec: str) -> tuple[str, str]:
    """Classify an egress spec into ``(kind, value)``.

    * ``ssh:<host>`` -> ``("ssh", host)`` — a per-request relay that runs ``curl``
      on that SSH host. This is the sturdiest mode for yopu.co: it proxies only
      the app-host requests (tiny footprint, so it does not re-trip the ban) AND
      every request exits the *same* IP, which the site's per-sheet session
      cookie is bound to. A whole-browser proxy fails the second half (routing
      the ~1MB of CDN assets through one IP re-trips the volume ban), and the
      ``worker`` relay fails the first (Cloudflare varies its egress IP between
      requests, so the cookie minted on the document call is rejected on the
      ``/api/sheet`` call).
    * ``worker <url>`` / ``worker:<url>`` -> ``("worker", url)`` — the CF
      fetch-proxy base already carrying ``?k=KEY``. Works only for endpoints that
      are not IP-bound (it kept failing on yopu.co's cookie-bound sheet API).
    * ``socks5://…`` / ``http://…`` -> ``("browser-proxy", url)`` for Playwright's
      launch ``proxy=`` (all traffic, one IP).
    * anything else -> ``("browser-proxy", spec)`` unchanged.

    The ``worker``/``ssh`` prefixes are required because a Worker base is itself
    an ``https://`` URL and a bare host is ambiguous with a proxy address.
    """
    s = spec.strip()
    low = s.lower()
    if low.startswith("vps:"):
        # vps:<host> -- curl_cffi (browser TLS) through an `ssh -D` tunnel that
        # exits <host>'s IP. Free lane that fetches the sheet; the plain `ssh:`
        # curl relay does NOT (its non-browser fingerprint gets 404).
        return ("vps", s[len("vps:"):].strip())
    if low.startswith("ssh:"):
        return ("ssh", s[len("ssh:"):].strip())
    if low.startswith("proxy:"):
        # proxy:<geo> -- residential lane via curl_cffi impersonation; the ONLY
        # egress that fetches the sheet. <geo> resolves creds through the local
        # resolver so they never touch argv. Empty geo => resolver's default.
        return ("residential", s[len("proxy:"):].strip())
    if low.startswith("imp:"):
        # imp:<proxy-url> -- same relay, but a literal proxy URL you pass yourself.
        return ("impersonate", s[len("imp:"):].strip())
    if low.startswith("worker "):
        return ("worker", s[len("worker "):].strip())
    if low.startswith("worker:"):
        return ("worker", s[len("worker:"):].strip())
    return ("browser-proxy", s)


def is_app_host(host: str | None) -> bool:
    return (host or "").lower() in APP_HOSTS


def is_tracker(url: str) -> bool:
    return any(sub in url for sub in TRACKER_SUBSTRINGS)


def build_worker_url(worker_base: str, target_url: str) -> str:
    """Append the (url-encoded) target to a Worker base that already holds ``k``.

    Keyed on ``?`` presence so a base written either as ``.../?k=KEY`` or
    ``...?k=KEY`` both get ``&u=…``; a base with no query at all gets ``?u=…``
    (the Worker will then 403 for the missing key, which is the honest result).
    """
    sep = "&" if "?" in worker_base else "?"
    return f"{worker_base}{sep}u={quote(target_url, safe='')}"


def pack_headers(headers: dict) -> str:
    """base64(JSON) of the upstream request headers, for the ``x-yp-headers``
    envelope the Worker unpacks. Full fidelity matters: the site's data API is
    sensitive to the exact same-origin header set."""
    return base64.b64encode(json.dumps(dict(headers)).encode("utf-8")).decode("ascii")


def filter_response_headers(headers) -> dict:
    """Drop only the headers that must not be replayed (see ``_RESP_SKIP``); keep
    everything else so signatures, nonces and cookies survive the relay."""
    return {k: v for k, v in dict(headers).items() if k.lower() not in _RESP_SKIP}


def make_worker_relay(worker_base: str, *, timeout: int = 45, opener=None):
    """Return ``relay(url, method, headers, post_data) -> (status, headers, body)``
    that fetches one same-origin request through the CF Worker.

    Upstream 4xx/5xx are relayed **faithfully** as fulfilled responses -- never
    turned into a network abort -- because the sheet engine expects a real
    Response (even a 404) from its /z calls and throws on "Failed to fetch".
    """
    import urllib.error
    import urllib.request

    _open = opener or urllib.request.urlopen

    def relay(url: str, method: str, headers: dict, post_data):
        full = build_worker_url(worker_base, url)
        data = None
        outer_method = "GET"
        if method not in ("GET", "HEAD") and post_data is not None:
            data = post_data.encode("utf-8") if isinstance(post_data, str) else post_data
            outer_method = "POST"
        req = urllib.request.Request(full, data=data, method=outer_method)
        req.add_header("User-Agent", OUTER_UA)
        req.add_header("x-yp-method", method)
        req.add_header("x-yp-headers", pack_headers(headers))
        try:
            resp = _open(req, timeout=timeout)
            raw_headers = resp.headers
            body = resp.read()
            status = int(raw_headers.get("x-yp-upstream-status", getattr(resp, "status", 200)))
        except urllib.error.HTTPError as exc:              # relay upstream error faithfully
            raw_headers = exc.headers
            body = exc.read()
            status = int(raw_headers.get("x-yp-upstream-status", exc.code))
        return status, filter_response_headers(raw_headers), body

    return relay


# Request headers the SSH relay forwards to the upstream curl -- a strict
# ALLOWLIST, not a droplist. Measured: a minimal set (UA + Referer + Accept)
# returns the sheet 200, while forwarding Chrome's full set (Origin, the
# Sec-Fetch-*/Sec-CH-* family, Priority, ...) makes /api/sheet answer 404 --
# the site treats the extra fetch-metadata as a non-page request and refuses it.
# Cookie is deliberately excluded: the relay keeps the session in a remote curl
# jar shared across calls (authoritative), because Chrome does not persist a
# cookie delivered via route.fulfill(). The jar is what carries the document's
# session cookie to the cookie-bound /api/sheet call from the same IP -- exactly
# the working manual `curl -c jar -b jar` chain.
_REQ_ALLOW = frozenset({"user-agent", "referer", "accept", "accept-language", "content-type"})


def parse_http_dump(raw: bytes) -> tuple[int, dict, bytes]:
    """Split a ``curl -D -`` dump (headers + body, possibly several redirect
    header blocks) into ``(status, headers, body)``, taking the LAST header
    block so a followed redirect reports its final response."""
    parts = raw.split(b"\r\n\r\n")
    hdr_idx = 0
    for i, p in enumerate(parts):
        if p[:5] == b"HTTP/":
            hdr_idx = i
    head = parts[hdr_idx]
    body = b"\r\n\r\n".join(parts[hdr_idx + 1:])
    lines = head.split(b"\r\n")
    status = int(lines[0].split()[1]) if lines and lines[0][:5] == b"HTTP/" else 0
    headers = {}
    for line in lines[1:]:
        if b":" in line:
            k, v = line.split(b":", 1)
            headers[k.decode("latin1").strip()] = v.decode("latin1").strip()
    return status, headers, body


def make_ssh_relay(host: str, *, timeout: int = 40, runner=None):
    """Return ``relay(url, method, headers, post_data) -> (status, headers, body)``
    that fetches one same-origin request by running ``curl`` on ``host`` over a
    reused SSH ControlMaster.

    Every request exits ``host``'s single IP, so yopu.co's per-sheet session
    cookie (minted on the document call, checked on ``/api/sheet``) stays valid
    -- the property the Worker relay cannot hold. Upstream 4xx/5xx are relayed
    faithfully as ``(status, headers, body)``; the caller fulfills, never aborts.
    """
    # ControlMaster socket must be short (<104 bytes) and unique per host/pid.
    safe = "".join(c if c.isalnum() else "_" for c in host)[:24]
    ctl = f"/tmp/ypcm_{safe}_{os.getpid()}"
    jar = f"/tmp/ypjar_{safe}_{os.getpid()}"  # remote cookie jar, shared across calls
    _run = runner or _ssh_exec
    started = {"ok": runner is not None}  # an injected runner (tests) needs no real master

    def _ensure_master():
        if started["ok"]:
            return
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             "-o", "ServerAliveInterval=15", "-M", "-S", ctl, "-f", "-N", host],
            check=False, capture_output=True, timeout=timeout,
        )
        started["ok"] = True

    def relay(url: str, method: str, headers: dict, post_data):
        _ensure_master()
        args = ["curl", "-s", "--compressed", "-D", "-", "--max-time", str(timeout - 5),
                "-c", jar, "-b", jar, "-X", method, url]
        for k, v in headers.items():
            if k.lower() in _REQ_ALLOW:
                if k.lower() == "user-agent":
                    v = v.replace("HeadlessChrome", "Chrome")
                args += ["-H", f"{k}: {v}"]
        stdin = None
        if method not in ("GET", "HEAD") and post_data is not None:
            args += ["--data-binary", "@-"]
            stdin = post_data.encode("utf-8") if isinstance(post_data, str) else post_data
        remote = " ".join(shlex.quote(a) for a in args) + " | base64"
        raw = _run(ctl, host, remote, stdin, timeout)
        status, resp_headers, body = parse_http_dump(safe_b64decode(raw))
        return status, filter_response_headers(resp_headers), body

    return relay


def _ssh_exec(ctl: str, host: str, remote_cmd: str, stdin: bytes | None, timeout: int) -> bytes:
    proc = subprocess.run(
        ["ssh", "-S", ctl, host, remote_cmd],
        input=stdin, capture_output=True, timeout=timeout + 10,
    )
    if proc.returncode != 0 and not proc.stdout:
        raise RuntimeError(f"ssh relay to {host} failed: "
                           f"{proc.stderr.decode('utf-8', 'replace')[:160]}")
    return proc.stdout


# curl_cffi impersonate relay -- the only relay that fetches yopu.co's sheet ---
# Headers to forward from the browser's own request. NOT user-agent (impersonate
# sets the whole browser header order/casing that the JA3/JA4 check keys on) and
# NOT cookie (the curl_cffi Session carries its own jar across doc -> /z).
_IMP_REQ_ALLOW = frozenset({"referer", "accept", "accept-language", "content-type"})


def resolve_residential(geo: str | None = None, *, resolver=None) -> str:
    """Return a residential proxy URL (with creds) from the local resolver, so the
    credential never has to pass through argv. Default resolver is the
    ultra-low-cost-scraper skill's ``proxy_resolver.py``; override with
    ``$YOPU_PDF_PROXY_RESOLVER`` (a command; ``--geo <cc> --format url`` is appended)."""
    cmd = (resolver or os.environ.get("YOPU_PDF_PROXY_RESOLVER")
           or f"python3 {os.path.expanduser('~')}/.agents/skills/ultra-low-cost-scraper/scripts/proxy_resolver.py")
    args = shlex.split(cmd) + ["--format", "url"] + (["--geo", geo] if geo else [])
    out = subprocess.run(args, capture_output=True, text=True, timeout=30)
    url = (out.stdout or "").strip()
    if not url:
        raise EgressUnavailable(
            "could not resolve a residential proxy URL. Set $YOPU_PDF_PROXY_RESOLVER "
            "to a command that prints 'http://user:pass@host:port', or configure the "
            f"ultra-low-cost-scraper skill.\n  resolver said: {(out.stderr or '').strip()[:160]}")
    return url


def make_impersonate_relay(proxy_url: str | None, *, impersonate: str = "chrome",
                           timeout: int = 40, session=None):
    """Return ``relay(url, method, headers, post_data) -> (status, headers, body)``
    that fetches one same-origin request with ``curl_cffi`` using a real browser
    TLS/JA3 fingerprint, through ``proxy_url``.

    This is the ONLY relay that fetches yopu.co's sheet. Measured 2026-09-04: the
    ``/api/sheet`` (``/z``) call is refused (empty 404) for any non-browser TLS
    fingerprint -- plain ``curl``, and therefore the ``ssh:``/``worker`` relays,
    all 404 even from a healthy home IP. ``curl_cffi``'s ``impersonate`` passes the
    check; paired with a residential ``proxy_url`` (the un-flagged IP the sheet API
    also requires) it returns 200. A single kept-alive Session pins one exit IP
    across the doc and ``/z`` calls, so the session cookie holds even on a
    rotating proxy port, and the Session's own jar carries that cookie -- the
    browser's Cookie header is neither needed nor forwarded."""
    try:
        from curl_cffi import requests as _cffi  # optional dep; only this relay needs it
    except ImportError as e:
        raise EgressUnavailable(
            "the residential/impersonate egress needs curl_cffi. Install it:\n"
            "    ./.venv/bin/pip install curl_cffi") from e
    sess = session if session is not None else _cffi.Session(impersonate=impersonate)
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    lock = threading.Lock()  # one Session, serialize the ~3 concurrent app-host calls
    from curl_cffi import CurlError  # transient handshake failures on rotating exits

    def relay(url: str, method: str, headers: dict, post_data):
        fwd = {k: v for k, v in headers.items() if k.lower() in _IMP_REQ_ALLOW}
        last = None
        for attempt in range(3):  # a residential exit can die mid-handshake; retry
            try:
                with lock:  # curl_cffi Session is not safe for concurrent requests
                    r = sess.request(method, url, headers=fwd, data=post_data,
                                     proxies=proxies, timeout=timeout)
                return r.status_code, filter_response_headers(dict(r.headers)), r.content
            except CurlError as e:
                last = e
                time.sleep(0.6 * (attempt + 1))
        raise last

    return relay


def open_ssh_socks(host: str, *, timeout: int = 15):
    """Open ``ssh -D <port> -N <host>`` -- a local SOCKS5 proxy whose traffic exits
    *host*'s IP -- and return ``(socks_url, proc)``. The caller must
    ``proc.terminate()`` when done. Pairs with ``make_impersonate_relay`` to give
    a free VPS lane: curl_cffi's browser TLS fingerprint (which the sheet API
    requires) exiting a host you already rent, at zero marginal cost."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    proc = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
         "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
         "-N", "-D", f"127.0.0.1:{port}", host],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read().decode("utf-8", "replace")[:160] if proc.stderr else ""
            raise EgressUnavailable(f"ssh -D to {host} exited: {err}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return f"socks5h://127.0.0.1:{port}", proc
        except OSError:
            time.sleep(0.3)
    proc.terminate()
    raise EgressUnavailable(f"ssh -D to {host} did not come up within {timeout}s")


# --- /z path de-obfuscation -------------------------------------------------
# The site hides its data endpoints (/api/, /i/, /auth/, /promotion/, /ping/,
# /ping-user/) behind /z/<rt(path)>, where rt = utf8 bytes -> XOR 0x5C ->
# length-seeded Fisher-Yates shuffle -> base64url(_Z_ALPHABET). The transform is
# deterministic and reversible. Constants come from the bundle literal
# H="ə\vĀ": J=H[0], W=H[1], K=H[2]^2. Fetching the decoded RAW path is a fallback
# for exits where the obfuscated /z route is refused but the raw route is served.
#
# This is tied to the current bundle; if the site changes the codec, decode fails
# and the caller falls back to fetching the /z path verbatim. Never guess.
_Z_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_Z_J, _Z_W, _Z_K = 601, 11, 65536


def _z_mod(t: int, n: int) -> int:
    return ((t % n) + n) % n


def _z_b64_decode(s: str) -> list[int]:
    vals = [_Z_ALPHABET.index(c) for c in s]
    out: list[int] = []
    for i in range(0, len(vals), 4):
        ch = vals[i:i + 4]
        if len(ch) >= 2:
            out.append(((ch[0] << 2) | (ch[1] >> 4)) & 0xFF)
        if len(ch) >= 3:
            out.append(((ch[1] << 4) | (ch[2] >> 2)) & 0xFF)
        if len(ch) >= 4:
            out.append(((ch[2] << 6) | ch[3]) & 0xFF)
    return out


def _z_unshuffle(arr: list[int]) -> list[int]:
    n = len(arr)
    state = _z_mod(n, _Z_K)
    swaps = []
    for i in range(n - 1, 0, -1):
        state = _z_mod(_Z_J * state + _Z_W, _Z_K)
        idx = int((state / _Z_K) * (i + 1))
        swaps.append((i, idx))
    for i, idx in reversed(swaps):
        arr[i], arr[idx] = arr[idx], arr[i]
    return arr


def decode_z_path(z_tail: str) -> str | None:
    """Decode a ``/z/<tail>`` tail back to its real path (e.g.
    ``/api/sheet?code=…``), or ``None`` if it does not decode to a known
    endpoint (so the caller keeps the original)."""
    try:
        arr = _z_unshuffle(_z_b64_decode(z_tail))
        path = bytes(b ^ 0x5C for b in arr).decode("latin1")
    except Exception:
        return None
    return path if path.startswith(("/api/", "/i/", "/auth/", "/promotion/", "/ping/")) else None
