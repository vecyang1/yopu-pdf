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
on its own within ~tens of minutes to a few hours; a full render worked again the
next morning without any intervention.

**The tool fails fast with that diagnosis** instead of hanging, so you know to wait
or switch networks rather than debug the tool.

### Why a proxy does not fix it (measured 2026-09-03/04)

The sheet itself comes from `/api/sheet` (hidden behind `/z/<obfuscated>`). That call
needs **two things at once**, and every workaround fails one of them:

1. **A real browser client fingerprint.** Plain `curl` gets `404` on `/z` *even from
   a healthy home IP that renders fine in the browser* — yopu.co checks the TLS/client
   fingerprint. So any `curl`-based relay (an `ssh:<host>` relay, the CF Worker) cannot
   fetch the sheet, on any IP.
2. **A clean, un-flagged IP.** A real browser routed through a proxy also gets `404` on
   `/z` — tested across 13 exits: 11 datacenter/VPN, **and two genuine residential**
   exits from a shared residential-proxy pool. yopu.co risk-scores
   and blocks known proxy ranges on the sheet API (its page loads Alibaba anti-fraud +
   reCAPTCHA).

Only **a real browser from your own un-flagged connection** satisfies both — which is
exactly how this tool renders (Playwright Chrome, direct). So:

- **If your IP is blocked, the fix is a clean connection, not a proxy:** run it from a
  different network, or a **phone hotspot** (a fresh mobile IP). Or just wait for your
  home IP's block to clear.
- **A commercial VPN / residential-proxy pool will not work** for the sheet — the whole
  pool is flagged.

### The `--egress` fallback (exists, but cannot beat the sheet gate)

The tool still supports `--egress "<spec>"` (alias `--proxy`; `--no-egress` forces
direct), and a one-line `~/.config/yopu-pdf/egress` for a default. It is **direct-first
with auto-fallback**, and the mechanism is correct — but for yopu.co's sheet it cannot
help (per the two requirements above). It remains useful only for un-authenticated
reads. `proxy-worker/` is a small, locked Cloudflare Worker relay kept for those; its
key stays out of git.


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

Hermetic — no network, no browser. 60 tests across `test_yopu_pdf.py`
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
