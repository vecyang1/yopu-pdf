#!/usr/bin/env python3
"""Dump a yopu.co (有谱么) sheet page to PDF.

The site renders the sheet client-side and its own "打印曲谱" button is
login-gated, so `@media print` on a logged-out page shows only the
placeholder "请点击右下方菜单打印曲谱". The sheet itself is public and fully
rendered on screen, so this tool prints *that* instead: it overrides the
site's print rules, unlocks the app-shell (which viewport-locks height and
clips overflow), and lets Chrome paginate the real sheet as vector text.

No login, no screenshots -- the output has selectable text.

yopu.co also IP-bans by request volume and, once tripped, answers a blocked IP
with a bare `404`/empty body (a lie -- the sheet is public). This module:

* recognises that block and fails fast with the right diagnosis (see
  ``EgressBlocked`` / ``SheetNotRendered``) instead of hanging for a minute;
* keeps the app-host footprint tiny -- only the document and two ``/z`` data
  calls are same-origin; the CDN loads direct, media and analytics are dropped
  (see ``net.py`` and ``install_router``);
* can route just those same-origin requests through a healthy egress (a proxy
  or the ``proxy-worker`` Cloudflare relay) configured in
  ``~/.config/yopu-pdf/egress``, trying direct first and falling back only when
  blocked.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import net

__version__ = "1.3.0"


class EgressBlocked(Exception):
    """yopu.co refused the request itself -- the IP (or the egress exit IP) is
    rate-limited/blocked. Carries the remedy, since a bare '404' from this site
    means 'banned', not 'no such sheet'."""

    def __init__(self, url: str, status, egress_kind: str | None):
        self.url = url
        self.status = status
        self.egress_kind = egress_kind
        super().__init__(self._message())

    def _message(self) -> str:
        code = f"HTTP {self.status}" if self.status else "a connection error"
        if self.egress_kind:
            return (f"yopu.co returned {code} even via the '{self.egress_kind}' egress -- "
                    f"its exit IP looks flagged too. Wait a few minutes, or switch egress "
                    f"(a different proxy/node, or another 'worker' deployment).")
        return (f"yopu.co returned {code} for this sheet, but the sheet is public and loads "
                f"from other networks -- your IP is being rate-limited/blocked by yopu.co "
                f"(its ban shows up as an empty 404). Wait a few minutes, or route through a "
                f"healthy egress: put one line in {net.config_path()} -- e.g.\n"
                f"    worker https://<your-worker>.workers.dev/?k=YOUR_KEY\n"
                f"  or a proxy your browser already uses, e.g.\n"
                f"    socks5://127.0.0.1:7897\n"
                f"  or pass it once with --egress / --proxy.")


class SheetNotRendered(Exception):
    """The page (/view) loaded 200 but the sheet data call (/z -> /api/sheet)
    returned 404, so nothing rendered.

    Measured 2026-09-03: yopu.co serves the public /view shell from anywhere, but
    its sheet API is refused (empty 404) from every PROXY IP class tested -- 11
    datacenter/VPN exits (Surfshark WireGuard, vless, three VPSes) AND two genuine
    residential exits from a shared residential-proxy pool,
    each /view=200 /z=404 from first contact. So NO egress this tool can drive
    (ssh:<vps>, a Worker, a browser proxy -- datacenter or residential-pool) fetches
    the sheet: yopu.co appears to block known proxy ranges on /api/sheet. The only
    vantage ever observed to return /z=200 is the user's own un-flagged connection.
    (Unverified whether yopu also changed the endpoint server-side; no clean IP was
    available to test.)"""

    def __init__(self, url: str, egress_kind: str | None):
        self.url = url
        self.egress_kind = egress_kind
        via = f" via the '{egress_kind}' egress" if egress_kind else ""
        super().__init__(
            f"loaded the /view page{via} but the sheet API (/api/sheet) returned an empty 404, so "
            f"it never rendered. yopu.co refuses /api/sheet from proxy IPs of every class tested "
            f"(datacenter, VPN, and shared residential-proxy pools). Run this from your own "
            f"un-flagged connection -- your home network, or a phone hotspot for a fresh mobile IP -- "
            f"or wait for your home IP's rate-limit block to clear (hours). A commercial VPN/proxy or "
            f"the 'ssh:'/'worker' egress will NOT work for the sheet.")

VIEW_RE = re.compile(r"^[A-Za-z0-9_-]{5,32}$")
PAGE_IMG_RE = re.compile(r"^\d+\.png$")
PX_PER_MM = 96 / 25.4
PAPER = {"a4": (210.0, 297.0), "letter": (215.9, 279.4)}

# Chrome's printToPDF accepts 0.1 - 2.0.
SCALE_MIN, SCALE_MAX = 0.1, 2.0

PRINT_CSS = """
@page {{ size: {paper}; margin: {margin}mm 0; }}
@media print {{
  /* 1. drop the app chrome */
  header.no-print, .dt-top-navigation, .side, .player-panel, .progress-bar,
  .notif, #yp-toast, .print-sheet, .loader {{ display: none !important; }}

  html, body {{ background: #fff !important; }}

  /* 2. unlock the app-shell: it pins every ancestor to viewport height and
        clips overflow, which otherwise truncates the PDF to one page */
  html, body, .layout, .main, .sheet-container,
  .xhe-sheet, .xhe-body, .nier-sheet, .nier-body {{
    overflow: visible !important;
    height: auto !important;
    max-height: none !important;
    min-height: 0 !important;
  }}

  /* 3. the sheet lives inside .layout, which the site marks .no-print */
  .layout.no-print {{
    display: block !important;
    width: 100% !important; min-width: 0 !important; max-width: none !important;
  }}
  .main, .sheet-container {{
    width: 100% !important; max-width: none !important; margin: 0 !important;
    box-shadow: none !important; border: none !important;
  }}

  /* 4. the renderer bakes an explicit px width at load time; clear it so the
        inline chord/lyric flow re-wraps to the paper width */
  .xhe-sheet, .xhe-body, .sheet-header,
  .nier-sheet, .nier-body {{ width: auto !important; max-width: none !important; }}

  /* 5. never split a chord off the syllable it sits above */
  xhe-chord-anchor, nier-chord-anchor, xhe-headline {{ break-inside: avoid; }}
}}
"""

_UNSAFE = set('/\\:*?"<>|')


def safe_filename(name: str, fallback: str) -> str:
    """Filesystem-safe name. Explicit unsafe set + codepoint check, so the
    rule stays readable (a regex range here silently eats spaces/hyphens)."""
    out = []
    for ch in name:
        cp = ord(ch)
        out.append(" " if (ch in _UNSAFE or cp < 0x20 or cp == 0x7F) else ch)
    cleaned = " ".join("".join(out).split()).strip(" .")
    return (cleaned[:120] or fallback)


def output_path(title: str, slug: str, out_dir: str, used: set[Path]) -> Path:
    """Pick the output file, keeping same-titled sheets distinct.

    Two arrangements of one song share a page title. Without the suffix the
    second overwrites the first and the run still prints OK for both, so the
    loss is silent -- which is why this is pinned by a test.
    """
    stem = safe_filename(title, slug)
    path = Path(out_dir) / f"{stem}.pdf"
    if path in used:
        path = Path(out_dir) / f"{stem} [{slug}].pdf"
    return path


def normalize_url(raw: str) -> str:
    raw = raw.strip()
    if VIEW_RE.match(raw):
        return f"https://yopu.co/view/{raw}"
    if raw.startswith("//"):
        raw = "https:" + raw
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw


def trim_blank_tail(path: Path) -> int:
    """Drop trailing pages that carry neither text nor images.

    Chrome can emit one because a page break lands just past the content: the
    prediction says 3 pages, atomic line boxes make it 4. Returns final count.
    """
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(path))
    pages = list(reader.pages)

    def is_blank(pg) -> bool:
        try:
            if (pg.extract_text() or "").strip():
                return False
        except Exception:
            return False  # unreadable -> assume content, never delete blindly
        res = pg.get("/Resources")
        try:
            return "/XObject" not in (res or {})
        except Exception:
            return False

    n = len(pages)
    while n > 1 and is_blank(pages[n - 1]):
        n -= 1
    if n == len(pages):
        return n

    writer = PdfWriter()
    for pg in pages[:n]:
        writer.add_page(pg)
    with open(path, "wb") as fh:
        writer.write(fh)
    return n


def images_dir(pdf: Path) -> Path:
    """Folder that will hold the exploded pages, beside the PDF.

    `-o` may name a file with no .pdf suffix, and stripping a suffix that is
    not there would point the folder at the PDF itself -- so only strip .pdf.
    """
    if pdf.suffix.lower() == ".pdf":
        return pdf.with_suffix("")
    return pdf.with_name(pdf.name + " pages")


def page_image_name(index: int, total: int) -> str:
    """`1.png` for a 3-page sheet, `01.png` for a 12-page one, so the pages
    stay in reading order under a plain string sort, not just in Finder."""
    return f"{index:0{len(str(total))}d}.png"


def stale_page_images(folder: Path) -> list[Path]:
    """Pages left by an earlier, longer export of the same sheet.

    Without this a 3-page run into a folder that held 5 leaves pages 4-5
    sitting there looking exactly like real ones. Matches only our own
    `<digits>.png` output, so anything else the user keeps here survives.
    """
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and PAGE_IMG_RE.match(p.name))


def collect_into_folder(pdf: Path, dpi: int) -> tuple[Path, Path, int]:
    """Explode `pdf` into numbered PNGs and move the PDF in alongside them.

    One sheet becomes one folder, so it is a single thing to open, move, or
    delete. Returns (folder, pdf_in_its_new_home, page_count).
    """
    folder = images_dir(pdf)
    folder.mkdir(parents=True, exist_ok=True)
    for old in stale_page_images(folder):
        old.unlink()

    try:
        subprocess.run(
            ["pdftoppm", "-png", "-r", str(dpi), str(pdf), str(folder / "page")],
            check=True, capture_output=True,
        )
    except FileNotFoundError:
        raise SystemExit(
            "pdftoppm not found -- it ships with poppler.\n"
            "Install it with:  brew install poppler"
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", "replace").strip() or f"exit {exc.returncode}"
        raise RuntimeError(f"pdftoppm failed: {detail}")

    # pdftoppm zero-pads its own counter consistently, so a string sort is
    # already page order; renumber anyway so the names match `p`'s convention.
    produced = sorted(folder.glob("page-*.png"))
    if not produced:
        raise RuntimeError("pdftoppm wrote no pages")
    for i, src in enumerate(produced, start=1):
        src.rename(folder / page_image_name(i, len(produced)))

    # Last, so a failure above leaves the PDF where the ✗ path can still name it.
    final = folder / pdf.name
    pdf.replace(final)
    return folder, final, len(produced)


def render(page, out: Path, args, scale: float, paper_w: float, paper_h: float) -> int:
    """Render at `scale` and return the real page count (blank tail removed)."""
    page.pdf(
        path=str(out),
        width=f"{paper_w}mm",
        height=f"{paper_h}mm",
        print_background=True,
        prefer_css_page_size=True,
        scale=max(SCALE_MIN, min(SCALE_MAX, scale)),
    )
    return trim_blank_tail(out)


def launch(pw, headed: bool, proxy: dict | None = None):
    """Prefer the installed Google Chrome so no 150MB browser download is
    needed; fall back to Playwright's own chromium if present.

    ``proxy`` is Playwright's launch proxy dict (browser-proxy egress mode); it
    routes *all* traffic, so it is used only for real socks5/http proxies, never
    for the Worker relay (which is per-request, in the route handler)."""
    kw = {"headless": not headed}
    if proxy:
        kw["proxy"] = proxy
    try:
        return pw.chromium.launch(channel="chrome", **kw)
    except Exception:
        try:
            return pw.chromium.launch(**kw)
        except Exception as exc:
            raise SystemExit(
                "No usable Chrome/Chromium found.\n"
                "Install Google Chrome, or run:  ./.venv/bin/playwright install chromium\n"
                f"underlying error: {exc}"
            )


SHEET_READY = ("() => { const s = document.querySelector('.sheet-container');"
               "        return s && s.innerText.trim().length > 20; }")


def install_router(page, relay, egress_kind: str | None):
    """Route the page's traffic to keep the yopu.co footprint tiny.

    * ``media`` (audio samples) and analytics/ad hosts are aborted -- never
      needed for a print, and they are the bulk of the request volume that
      trips the ban.
    * same-origin app-host requests are relayed through the egress when one is
      given (Worker mode); the CDN and everything else go direct.

    An upstream error from the relay is fulfilled faithfully (status + body), so
    the sheet engine sees a real Response rather than a network abort it cannot
    handle.
    """
    def handle(route):
        req = route.request
        host = urlparse(req.url).hostname or ""
        if req.resource_type == "media" or net.is_tracker(req.url):
            return route.abort()
        if relay is not None and net.is_app_host(host):
            try:
                status, headers, body = relay(req.url, req.method, dict(req.headers), req.post_data)
            except Exception as exc:
                if os.environ.get("YOPU_PDF_DEBUG"):
                    print(f"[relay] {req.method} {req.url[:96]} -> EXC {exc}", file=sys.stderr)
                return route.abort()
            if os.environ.get("YOPU_PDF_DEBUG"):
                print(f"[relay] {req.method} {req.url[:96]} -> {status} ({len(body)}B)", file=sys.stderr)
            return route.fulfill(status=status, headers=headers, body=body)
        return route.continue_()

    page.route("**/*", handle)


def dump(page, url: str, args, used: set[Path], relay=None, egress_kind: str | None = None) -> tuple[Path, int, float]:
    from playwright.sync_api import Error as PWError, TimeoutError as PWTimeout

    paper_w_mm, paper_h_mm = PAPER[args.paper.lower()]
    install_router(page, relay, egress_kind)

    # domcontentloaded, not networkidle: this page beacons to analytics forever,
    # so networkidle never settles here (and hangs for the full timeout). A 4xx
    # main document makes Chrome raise ERR_HTTP_RESPONSE_CODE_FAILURE -- on this
    # site that is the *ban*, not a missing sheet, so classify it as such.
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
    except PWError as exc:
        s = str(exc)
        if "ERR_HTTP_RESPONSE_CODE_FAILURE" in s or "ERR_ABORTED" in s or "ERR_FAILED" in s:
            raise EgressBlocked(url, None, egress_kind)
        raise
    if resp is not None and resp.status >= 400:
        raise EgressBlocked(url, resp.status, egress_kind)

    # The sheet is client-rendered; wait for actual content, not just load.
    # A timeout here means the shell loaded but the /z data never rendered the
    # sheet -- the 'soft' block -- so fail fast with that diagnosis, not a raw
    # Playwright timeout the user cannot act on.
    try:
        page.wait_for_function(SHEET_READY, timeout=args.render_timeout * 1000)
    except PWTimeout:
        raise SheetNotRendered(url, egress_kind)

    title = (page.title() or "").strip()
    css = PRINT_CSS.format(paper=args.paper.upper(), margin=args.margin)
    page.add_style_tag(content=css)
    page.emulate_media(media="print")
    page.wait_for_timeout(args.settle)

    content_px = page.evaluate(
        "() => { const d = document.documentElement;"
        "        const s = document.querySelector('.sheet-container');"
        "        const b = s ? s.getBoundingClientRect().bottom + window.scrollY : 0;"
        "        return Math.ceil(Math.max(d.scrollHeight, b)); }"
    )
    page_px = (paper_h_mm - 2 * args.margin) * PX_PER_MM

    slug = url.rstrip("/").rsplit("/", 1)[-1]
    out = Path(args.out) if args.out else output_path(title, slug, args.dir, used)
    used.add(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    if args.scale is not None:
        pages = render(page, out, args, args.scale, paper_w_mm, paper_h_mm)
        return out, pages, args.scale

    pages = render(page, out, args, 1.0, paper_w_mm, paper_h_mm)
    if args.no_fit or pages <= 1:
        return out, pages, 1.0

    # Shrink slightly to reclaim an orphan page -- but only adopt a scale that
    # is *verified* to produce fewer pages. Predicting from content height is
    # wrong: line boxes are atomic, so each page wastes part of a line.
    tmp = out.with_name(out.stem + ".fit.pdf")
    try:
        for target in range(max(1, pages - 2), pages):
            guess = min(1.0, target * page_px / content_px * 0.97)
            if guess < args.min_scale or guess >= 1.0:
                continue
            if render(page, tmp, args, guess, paper_w_mm, paper_h_mm) <= target:
                tmp.replace(out)
                return out, target, guess
    finally:
        if tmp.exists():
            tmp.unlink()

    return out, pages, 1.0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Dump yopu.co (有谱么) sheets to PDF with selectable text.",
        epilog="examples:\n"
               "  yopu-pdf https://yopu.co/view/aXYaaOXZ\n"
               "  yopu-pdf https://yopu.co/view/aXYaaOXZ --p   # + a folder of numbered PNGs\n"
               "  yopu-pdf aXYaaOXZ rpQ4rdoP -d ~/Desktop\n"
               "  yopu-pdf aXYaaOXZ --no-fit        # never shrink to save a page",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("urls", nargs="+", metavar="URL", help="yopu.co/view/<id> URL, or just the <id>")
    ap.add_argument("-o", "--out", help="output file (single URL only)")
    ap.add_argument("-d", "--dir", default=".", help="output directory (default: .)")
    ap.add_argument("--paper", default="A4", choices=["A4", "a4", "Letter", "letter"])
    ap.add_argument("--margin", type=float, default=12.0, help="top/bottom margin in mm (default: 12)")
    ap.add_argument("--scale", type=float, help="force a print scale (0.1-2.0)")
    ap.add_argument("--no-fit", action="store_true", help="do not shrink to avoid an orphan page")
    ap.add_argument("--min-scale", type=float, default=0.8,
                    help="smallest auto-shrink allowed (default: 0.8)")
    ap.add_argument("-p", "--p", "--images", dest="images", action="store_true",
                    help="also explode the PDF into a folder of numbered PNGs (like `p`)")
    ap.add_argument("--dpi", type=int, default=200, help="PNG resolution for --p (default: 200)")
    ap.add_argument("--timeout", type=int, default=60, help="per-page navigation timeout, seconds")
    ap.add_argument("--render-timeout", type=int, default=35,
                    help="seconds to wait for the sheet to render before declaring a soft block (default: 35)")
    ap.add_argument("--settle", type=int, default=1200, help="ms to settle after print CSS")
    ap.add_argument("--egress", "--proxy", dest="egress", metavar="SPEC",
                    help="route the yopu.co requests through a healthy egress when your IP is blocked. "
                         "SPEC is 'worker https://<worker>/?k=KEY', or a proxy URL "
                         "(socks5://host:port, http://host:port). Overrides YOPU_PDF_EGRESS and "
                         f"the config at {net.config_path()}.")
    ap.add_argument("--no-egress", action="store_true",
                    help="ignore any configured egress and go direct")
    ap.add_argument("--headed", action="store_true", help="show the browser (debugging)")
    ap.add_argument("--version", action="version", version=f"yopu-pdf {__version__}")
    args = ap.parse_args()

    if args.out and len(args.urls) > 1:
        ap.error("--out takes a single URL; use --dir for several")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed. Run ./setup.sh (or: pip install playwright)",
              file=sys.stderr)
        return 2

    # Egress policy:
    #   --egress SPEC -> force that egress for every request (no direct attempt).
    #   --no-egress   -> direct only, never fall back.
    #   otherwise     -> try DIRECT first and, only when yopu.co blocks us, retry
    #                    that sheet via the egress configured in env/config. This
    #                    keeps the normal (un-banned) path fast and puts no load
    #                    on the proxy/Worker until it is actually needed.
    forced_spec = args.egress.strip() if (args.egress and args.egress.strip()) else None
    fallback_spec = None
    if not args.no_egress and not forced_spec:
        fallback_spec = net.resolve_egress(None)  # env or config file, not the CLI flag

    def setup_egress(spec):
        """spec -> (kind, browser_proxy_dict_or_None, relay_or_None)."""
        kind, value = net.parse_egress(spec)
        if kind == "worker":
            return kind, None, net.make_worker_relay(value)
        if kind == "ssh":
            return kind, None, net.make_ssh_relay(value)
        return kind, {"server": value}, None  # browser-proxy

    primary_kind = primary_proxy = primary_relay = None
    if forced_spec:
        primary_kind, primary_proxy, primary_relay = setup_egress(forced_spec)
        print(f"↪ egress forced: {primary_kind}", file=sys.stderr)

    failed = []
    used: set[Path] = set()
    with sync_playwright() as pw:
        browser = launch(pw, args.headed, proxy=primary_proxy)
        fb_browser = [None]  # a proxied browser, launched lazily only if a browser-proxy fallback fires

        def render_once(br, relay, kind, target):
            page = br.new_page(
                viewport={"width": 794, "height": 1123},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            )
            page.on("dialog", lambda d: d.dismiss())
            try:
                return dump(page, target, args, used, relay=relay, egress_kind=kind)
            finally:
                page.close()

        try:
            for raw in args.urls:
                url = normalize_url(raw)
                try:
                    out, pages, scale = render_once(browser, primary_relay, primary_kind, url)
                except (EgressBlocked, SheetNotRendered) as exc:
                    if not fallback_spec:
                        failed.append(raw)
                        print(f"✗ {url}\n  {exc}", file=sys.stderr)
                        continue
                    fk, fp, fr = setup_egress(fallback_spec)
                    print(f"  ↪ direct blocked by yopu.co; retrying via the configured {fk} egress…",
                          file=sys.stderr)
                    try:
                        if fp:  # a browser-proxy fallback needs its own proxied browser
                            if fb_browser[0] is None:
                                fb_browser[0] = launch(pw, args.headed, proxy=fp)
                            out, pages, scale = render_once(fb_browser[0], None, fk, url)
                        else:
                            out, pages, scale = render_once(browser, fr, fk, url)
                    except Exception as exc2:
                        failed.append(raw)
                        print(f"✗ {url}\n  {exc2}", file=sys.stderr)
                        continue
                except Exception as exc:
                    failed.append(raw)
                    print(f"✗ {url}\n  {type(exc).__name__}: {exc}", file=sys.stderr)
                    continue

                note = "" if scale >= 0.999 else f", shrunk to {scale:.2f} to save a page"
                if not args.images:
                    print(f"✓ {out}  ({pages} page{'s' if pages != 1 else ''}{note})")
                    continue

                # The PDF is already on disk, so an image failure must report
                # where it actually is rather than read as "no sheet was saved".
                try:
                    folder, out, n = collect_into_folder(out, args.dpi)
                except RuntimeError as exc:
                    failed.append(raw)
                    print(f"✓ {out}  ({pages} page{'s' if pages != 1 else ''}{note})")
                    print(f"  ! images for {out.name}: {exc}", file=sys.stderr)
                    continue

                names = [page_image_name(i, n) for i in range(1, n + 1)]
                shown = " ".join(names) if n <= 6 else f"{names[0]} … {names[-1]}"
                print(f"✓ {folder}/  ({n} page{'s' if n != 1 else ''}{note})")
                print(f"    {out.name}  +  {shown}   @ {args.dpi} dpi")
        finally:
            browser.close()
            if fb_browser[0] is not None:
                fb_browser[0].close()

    if failed:
        print(f"\n{len(failed)} of {len(args.urls)} failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
