# yopu-pdf

Dump a [yopu.co](https://yopu.co) (有谱么) sheet page to PDF — **selectable vector text**, not a screenshot.

```bash
./setup.sh                                  # once
yp https://yopu.co/view/aXYaaOXZ            # alias: saves into ~/Downloads
yp https://yopu.co/view/aXYaaOXZ --p        # + a folder of numbered page PNGs
./yopu-pdf https://yopu.co/view/aXYaaOXZ    # -> 再见青春 - 汪峰 吉他和弦谱.pdf
./yopu-pdf aXYaaOXZ rpQ4rdoP -d ~/Desktop   # bare IDs work too; batch is fine
```

The `yp` alias (zsh function + `noglob`, defined in `~/.zshrc`, documented in the
`alias` skill) is the everyday entry point: `yp <url-or-id>` saves into `~/Downloads`.

## `--p` — one sheet, one folder

`--p` (also `-p` / `--images`) explodes the PDF into numbered PNGs — the same
shape the existing `p` alias produces, so pages flip in reading order — and
moves the PDF in with them. One sheet becomes **one item** in `~/Downloads`, so
it is a single thing to open, move, or throw away:

```text
~/Downloads/香水有毒 - 胡杨林 吉他弹唱谱/
    香水有毒 - 胡杨林 吉他弹唱谱.pdf
    1.png  2.png  3.png  4.png  5.png
```

Without `--p` nothing changes: a bare `.pdf` lands in the output directory.

- 200 dpi by default (`--dpi 300` for print-grade); A4 at 200 dpi is 1653×2339.
- Names are padded only when the sheet needs it — `1.png` for a 3-page sheet,
  `01.png` for a 12-page one, so a plain string sort is still page order.
- Re-exporting a sheet that got shorter deletes the leftover trailing pages,
  which would otherwise sit there looking exactly like real ones. Only our own
  `<digits>.png` files are touched; anything else you keep in that folder stays.
- Needs `pdftoppm` (`brew install poppler`). Its absence is reported with that
  command. On a conversion failure the PDF stays put and the ✓ names it where it
  actually is — the sheet *was* fetched, so that must not read as a failed run.
- Re-running `--p` over a folder made by an older version absorbs the PDF that
  was sitting beside it, so the layout self-corrects.

## Why this exists

The site *has* a 打印曲谱 button, but it is **login-gated**. Logged out, the page's
own `@media print` renders only the placeholder `请点击右下方菜单打印曲谱` — so a plain
Cmd+P, or any naive headless `--print-to-pdf`, produces a one-line PDF.

The sheet itself is public and fully rendered on screen. This tool prints *that*:

1. Overrides the site's print rules — `div.layout` (which holds the real sheet) is
   marked `.no-print`, and `.print-sheet` holds only the gated placeholder.
2. Unlocks the app-shell. Every ancestor is pinned to viewport height with
   `overflow:hidden`, which otherwise **truncates the PDF to one page**.
3. Clears the width the renderer bakes in at load time, so the inline chord/lyric
   flow re-wraps to the paper width instead of being shrink-to-fit.
4. Sets the viewport to A4 at 96dpi so the browser layout matches the paper 1:1.

No login, no account, no screenshots.

## Fitting

By default, if a sheet spills a line or two onto an extra page, it is shrunk to
reclaim it — but only at a scale that is **verified by re-rendering** to actually
produce fewer pages. (Predicting from content height is wrong: line boxes are
atomic, so every page wastes part of a line.) Trailing blank pages are removed.

- `--no-fit` — never shrink; natural pagination
- `--scale 0.85` — force a scale (0.1–2.0)
- `--min-scale 0.7` — allow more aggressive shrinking (default 0.8)
- `--paper Letter`, `--margin 15` — paper and top/bottom margin (mm)

## When yopu.co blocks your IP

yopu.co rate-limits by IP. Once tripped, `/view/<id>` returns a **bare `404` with an
empty body** — even though the sheet is public and loads fine elsewhere. So a `404`
here means *"your IP is blocked"*, not *"no such sheet"*. The `/view` shell recovers
on its own within ~tens of minutes to a few hours.

**The tool fails fast with that diagnosis** instead of hanging, and it can route
around the block with an automatic **fallback chain**.

### The one thing the sheet API actually checks: a browser TLS fingerprint

The sheet comes from `/api/sheet` (behind `/z/<obfuscated>`). Measured 2026-09-04:
that call requires a **real-browser TLS/JA3 fingerprint**. A plain-`curl` client
gets an empty `404` *even from a healthy home IP that renders fine in a browser* —
so the naive `ssh:<host>` curl relay and the CF Worker cannot fetch the sheet. The
**exit IP does not matter**: [`curl_cffi`](https://github.com/lexiforest/curl_cffi)
with `impersonate="chrome"` returns `200` from a home IP, a datacenter VPS, **and**
a residential proxy alike. (Direct mode already works because it drives a real
Chrome via Playwright.)

So when your home IP is rate-limited, route the small handful of app-host requests
(the doc + two `/z` calls) through a lane that carries a browser fingerprint. The
heavy `cdn.yopu.co` assets always load direct.

### Egress lanes and the fallback chain

Configure a chain in `~/.config/yopu-pdf/egress` (one lane per line, or comma-
separated; tried **in order after a direct attempt**). Override per-run with
`--egress "A,B"`, or force direct with `--no-egress`.

```text
vps:my-vps      # curl_cffi through an `ssh -D` tunnel exiting a host you rent  (free)
proxy:vn        # curl_cffi through a residential pool, geo vn  (~$1/GB backstop)
```

- **`vps:<host>`** — opens `ssh -D` to a host you can `ssh` to and sends the
  app-host requests through it with a browser fingerprint. Free, and it fetches the
  sheet. (This is the lane the old plain `ssh:` relay *should* have been.)
- **`proxy:<geo>`** — curl_cffi through a residential proxy pool. Credentials are
  resolved by an external command so they never touch argv: set
  `$YOPU_PDF_PROXY_RESOLVER` to a command that prints `http://user:pass@host:port`
  (given `--geo <cc> --format url`).
- **`imp:<proxy-url>`** — curl_cffi through any SOCKS/HTTP proxy URL you supply.
- `ssh:<host>` (plain curl) and `worker <url>` remain for non-sheet reads; they do
  **not** fetch the sheet (no browser fingerprint).

A single kept-alive session pins one exit IP across the doc and `/z` calls, so the
session cookie holds even on a rotating proxy port. `curl_cffi` is an optional
dependency (only the `vps:`/`proxy:`/`imp:` lanes use it): `pip install curl_cffi`.


## Notes

- Uses the Google Chrome already installed on this Mac via Playwright's
  `channel="chrome"`, so setup does **not** download a 150MB browser.
  If Chrome is absent, `setup.sh` installs Playwright's chromium instead.
- Output is named from the page title. Two different arrangements of one song
  share a title, so within a run the second gets a ` [id]` suffix rather than
  silently overwriting the first.
- Verified against 5 sheets (1–3 pages each): full content, no blank pages,
  no chord split from its syllable across a page break.

## Tests

```bash
./.venv/bin/python -m unittest discover -p "test_*.py"
```

Hermetic — no network, no browser. 67 tests across `test_yopu_pdf.py`
(PDF/pagination/routing) and `test_net.py` (egress), all pinning failures that
are *silent* rather than loud:

- same-titled sheets overwriting each other (5 URLs once produced 3 files while
  reporting OK for all 5)
- filename sanitising that eats the spaces and hyphens it is meant to keep
- `--p` with an extensionless `-o`, where stripping a suffix that is not there
  aims the image folder at the PDF itself
- page names that stop sorting into page order past 9 pages
- a re-export leaving a longer run's trailing pages behind / a stray PDF beside
  the folder instead of inside it
- `--dpi` never reaching `pdftoppm` — the fake records the whole argv, because a
  fake that *accepts* `-r` and drops it reads as covered
- the router proxying the CDN by mistake (it must stay direct), letting audio
  through, or turning a relayed upstream 404 into a hard abort
- an upstream 4xx raised instead of relayed; a signature/cookie header dropped on
  the way back; the `/z` de-obfuscation, pinned to two real captures

Every behavioural fix was mutation-checked: revert it and the suite goes red.
