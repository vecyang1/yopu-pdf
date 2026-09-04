#!/usr/bin/env python3
"""Hermetic unit tests -- no network, no browser.

Covers the two things that failed silently during development: same-titled
sheets overwriting each other, and filename sanitising that eats the spaces
and hyphens it was supposed to keep.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yopu_pdf import (
    collect_into_folder,
    images_dir,
    normalize_url,
    output_path,
    page_image_name,
    safe_filename,
    stale_page_images,
)


class TestSafeFilename(unittest.TestCase):
    def test_keeps_spaces_and_hyphens(self):
        # A regex class like [ -/] looks like "space, hyphen, slash" and is
        # actually the range 0x20-0x2F, which silently eats both.
        self.assertEqual(
            safe_filename("再见青春 - 汪峰 吉他和弦谱", "x"),
            "再见青春 - 汪峰 吉他和弦谱",
        )

    def test_strips_path_separators(self):
        got = safe_filename("AC/DC: Back\\In Black", "x")
        for ch in '/\\:':
            self.assertNotIn(ch, got)

    def test_strips_control_characters(self):
        self.assertNotIn("\n", safe_filename("a\nb", "x"))
        self.assertNotIn("\x7f", safe_filename("a\x7fb", "x"))

    def test_falls_back_when_title_is_empty_or_all_unsafe(self):
        self.assertEqual(safe_filename("", "slug123"), "slug123")
        self.assertEqual(safe_filename("///", "slug123"), "slug123")

    def test_does_not_end_in_dot_or_space(self):
        self.assertFalse(safe_filename("name. ", "x").endswith((".", " ")))


class TestOutputPath(unittest.TestCase):
    def test_distinct_titles_keep_plain_names(self):
        used: set[Path] = set()
        a = output_path("Song A", "aaa", "/out", used); used.add(a)
        b = output_path("Song B", "bbb", "/out", used); used.add(b)
        self.assertEqual(a, Path("/out/Song A.pdf"))
        self.assertEqual(b, Path("/out/Song B.pdf"))

    def test_same_title_does_not_overwrite(self):
        """The regression: 5 URLs once produced 3 files, reporting OK for all 5."""
        used: set[Path] = set()
        first = output_path("吹吹山顶的风", "MXJzEkxX", "/out", used); used.add(first)
        second = output_path("吹吹山顶的风", "rpQ4rdoP", "/out", used); used.add(second)
        self.assertNotEqual(first, second)
        self.assertEqual(second, Path("/out/吹吹山顶的风 [rpQ4rdoP].pdf"))

    def test_n_distinct_slugs_yield_n_distinct_paths(self):
        used: set[Path] = set()
        slugs = ["s1", "s2", "s3", "s4", "s5"]
        paths = []
        for s in slugs:
            p = output_path("Same Title", s, "/out", used)
            used.add(p)
            paths.append(p)
        self.assertEqual(len(set(paths)), len(slugs), f"graded {len(slugs)} slugs")


class TestNormalizeUrl(unittest.TestCase):
    def test_bare_id(self):
        self.assertEqual(normalize_url("aXYaaOXZ"), "https://yopu.co/view/aXYaaOXZ")

    def test_full_url_untouched(self):
        u = "https://yopu.co/view/aXYaaOXZ"
        self.assertEqual(normalize_url(u), u)

    def test_scheme_added(self):
        self.assertEqual(normalize_url("yopu.co/view/aXYaaOXZ"),
                         "https://yopu.co/view/aXYaaOXZ")

    def test_id_with_hyphen_and_underscore(self):
        self.assertEqual(normalize_url("a-b_c12"), "https://yopu.co/view/a-b_c12")


class TestImagesDir(unittest.TestCase):
    def test_strips_the_pdf_suffix(self):
        self.assertEqual(images_dir(Path("/out/香水有毒 - 胡杨林 吉他弹唱谱.pdf")),
                         Path("/out/香水有毒 - 胡杨林 吉他弹唱谱"))

    def test_keeps_a_dot_inside_the_title(self):
        self.assertEqual(images_dir(Path("/out/Song vol.2.pdf")), Path("/out/Song vol.2"))

    def test_extensionless_out_does_not_collide_with_the_pdf(self):
        """`-o mysheet` writes the PDF to `mysheet`; stripping a suffix that is
        not there would aim the folder at that same path and mkdir would fail."""
        pdf = Path("/out/mysheet")
        self.assertNotEqual(images_dir(pdf), pdf)


class TestPageImageName(unittest.TestCase):
    def test_single_digit_matches_the_p_convention(self):
        self.assertEqual([page_image_name(i, 3) for i in (1, 2, 3)],
                         ["1.png", "2.png", "3.png"])

    def test_pads_so_a_plain_string_sort_is_page_order(self):
        names = [page_image_name(i, 12) for i in range(1, 13)]
        self.assertEqual(names[0], "01.png")
        self.assertEqual(sorted(names), names, "12 pages graded")


class TestStalePageImages(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_missing_folder_is_empty_not_an_error(self):
        self.assertEqual(stale_page_images(self.tmp / "nope"), [])

    def test_finds_previous_pages(self):
        for n in ("1.png", "2.png", "03.png"):
            (self.tmp / n).write_bytes(b"")
        self.assertEqual({p.name for p in stale_page_images(self.tmp)},
                         {"1.png", "2.png", "03.png"})

    def test_leaves_anything_that_is_not_our_output(self):
        """A shorter re-export must drop old trailing pages -- but the folder
        is in the user's Downloads, so only our own <digits>.png may go."""
        # No "1.PNG" here: macOS is case-insensitive, so it and "1.png" are one
        # file and the fixture would be asserting something the FS cannot hold.
        keep = ["notes.md", "cover.png", "page-1.png", "1.jpg"]
        for n in keep:
            (self.tmp / n).write_bytes(b"")
        (self.tmp / "1.png").write_bytes(b"")
        self.assertEqual([p.name for p in stale_page_images(self.tmp)], ["1.png"])
        for n in keep:
            self.assertTrue((self.tmp / n).exists())


class TestCollectIntoFolder(unittest.TestCase):
    """`--p` gathers one sheet into one folder: pages *and* the PDF."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.calls = []

    def fake_pdftoppm(self, pages):
        """Stands in for pdftoppm. Records the whole argv -- a fake that accepts
        `-r` and drops it would make the dpi look covered while never testing
        it -- and writes the zero-padded names the real tool writes."""
        def run(argv, **kwargs):
            self.calls.append(list(argv))
            prefix = Path(argv[-1])
            width = len(str(pages))
            for i in range(1, pages + 1):
                Path(f"{prefix}-{i:0{width}d}.png").write_bytes(b"png")
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return run

    def make_pdf(self, name="我如此爱你 - 汪峰 吉他和弦谱.pdf"):
        pdf = self.tmp / name
        pdf.write_bytes(b"%PDF-1.4")
        return pdf

    def test_pdf_ends_up_inside_the_folder(self):
        pdf = self.make_pdf()
        with mock.patch("yopu_pdf.subprocess.run", self.fake_pdftoppm(3)):
            folder, final, n = collect_into_folder(pdf, 200)
        self.assertEqual(n, 3)
        self.assertEqual(folder, self.tmp / "我如此爱你 - 汪峰 吉他和弦谱")
        self.assertEqual(final, folder / pdf.name)
        self.assertTrue(final.is_file())
        self.assertFalse(pdf.exists(), "no stray PDF may be left beside the folder")
        self.assertEqual({p.name for p in folder.iterdir()},
                         {"1.png", "2.png", "3.png", pdf.name})

    def test_folder_is_the_only_thing_added_to_the_output_dir(self):
        """The point of the change: one sheet is one item in ~/Downloads."""
        pdf = self.make_pdf()
        with mock.patch("yopu_pdf.subprocess.run", self.fake_pdftoppm(2)):
            folder, _, _ = collect_into_folder(pdf, 200)
        self.assertEqual([p.name for p in self.tmp.iterdir()], [folder.name])

    def test_dpi_actually_reaches_pdftoppm(self):
        pdf = self.make_pdf()
        with mock.patch("yopu_pdf.subprocess.run", self.fake_pdftoppm(1)):
            collect_into_folder(pdf, 300)
        argv = self.calls[0]
        self.assertIn("-r", argv)
        self.assertEqual(argv[argv.index("-r") + 1], "300")

    def test_rerun_replaces_the_pdf_and_drops_the_extra_pages(self):
        pdf = self.make_pdf()
        with mock.patch("yopu_pdf.subprocess.run", self.fake_pdftoppm(5)):
            folder, _, _ = collect_into_folder(pdf, 200)
        (folder / "notes.md").write_text("mine", encoding="utf-8")

        self.make_pdf()  # a fresh download of the same sheet, now shorter
        with mock.patch("yopu_pdf.subprocess.run", self.fake_pdftoppm(2)):
            folder, final, n = collect_into_folder(pdf, 200)
        self.assertEqual(n, 2)
        self.assertTrue(final.is_file())
        self.assertEqual({p.name for p in folder.iterdir()},
                         {"1.png", "2.png", "notes.md", pdf.name})


class _FakeReq:
    def __init__(self, url, resource_type="document", method="GET", headers=None, post_data=None):
        self.url = url
        self.resource_type = resource_type
        self.method = method
        self.headers = headers or {}
        self.post_data = post_data


class _FakeRoute:
    def __init__(self, req):
        self.request = req
        self.action = None
        self.fulfilled = None

    def abort(self):
        self.action = "abort"

    def continue_(self):
        self.action = "continue"

    def fulfill(self, status=None, headers=None, body=None):
        self.action = "fulfill"
        self.fulfilled = (status, headers, body)


class _FakePage:
    """Captures the route handler install_router registers."""
    def __init__(self):
        self.handler = None

    def route(self, pattern, handler):
        self.handler = handler


class TestInstallRouter(unittest.TestCase):
    """The routing decision is the half that can silently misroute: proxy the
    CDN by mistake (slow, defeats the point), let audio through (bloats the
    footprint that trips the ban), or turn a proxied 404 into a hard abort."""

    def _route(self, req, relay=None):
        page = _FakePage()
        from yopu_pdf import install_router
        install_router(page, relay, "worker" if relay else None)
        r = _FakeRoute(req)
        page.handler(r)
        return r

    def test_audio_is_aborted(self):
        r = self._route(_FakeReq("https://yopu.co/sound/guitar-sound-1.mp3", "media"))
        self.assertEqual(r.action, "abort")

    def test_tracker_is_aborted(self):
        r = self._route(_FakeReq("https://www.google-analytics.com/g/collect", "xhr"))
        self.assertEqual(r.action, "abort")

    def test_cdn_goes_direct_even_with_a_relay(self):
        # The whole design: cdn.yopu.co is never banned and must NOT be proxied.
        relay = lambda *a, **k: (_ for _ in ()).throw(AssertionError("CDN must not be relayed"))
        r = self._route(_FakeReq("https://cdn.yopu.co/js/view.es.js", "script"), relay=relay)
        self.assertEqual(r.action, "continue")

    def test_app_host_is_relayed_when_a_relay_is_given(self):
        calls = []
        def relay(url, method, headers, post_data):
            calls.append(url)
            return 200, {"content-type": "text/plain"}, b"data"
        r = self._route(_FakeReq("https://yopu.co/z/abc", "fetch"), relay=relay)
        self.assertEqual(r.action, "fulfill")
        self.assertEqual(r.fulfilled, (200, {"content-type": "text/plain"}, b"data"))
        self.assertEqual(calls, ["https://yopu.co/z/abc"])

    def test_app_host_goes_direct_when_no_relay(self):
        r = self._route(_FakeReq("https://yopu.co/view/x", "document"), relay=None)
        self.assertEqual(r.action, "continue")

    def test_a_relayed_upstream_404_is_still_a_fulfill_not_an_abort(self):
        # Faithful relay: the sheet engine must see a real 404 Response, never a
        # network abort it cannot handle.
        r = self._route(_FakeReq("https://yopu.co/z/missing", "fetch"),
                        relay=lambda *a, **k: (404, {"content-type": "text/plain"}, b""))
        self.assertEqual(r.action, "fulfill")
        self.assertEqual(r.fulfilled[0], 404)

    def test_a_relay_exception_aborts_rather_than_crashing(self):
        r = self._route(_FakeReq("https://yopu.co/z/x", "fetch"),
                        relay=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertEqual(r.action, "abort")


if __name__ == "__main__":
    unittest.main()
