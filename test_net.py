#!/usr/bin/env python3
"""Hermetic tests for the egress layer -- no network, no browser.

Covers the things that would fail silently: the CDN being proxied by mistake
(it must stay direct), an upstream 4xx being raised instead of relayed (the
sheet engine then sees "Failed to fetch" and never renders), and a signature
header being dropped on the way back.
"""
import base64
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import net


class TestClassification(unittest.TestCase):
    def test_app_host_is_the_banned_host_not_the_cdn(self):
        self.assertTrue(net.is_app_host("yopu.co"))
        self.assertTrue(net.is_app_host("scdn.yopu.co"))
        # The whole design rests on this: the CDN is never banned and must load
        # direct. Proxying it would be slow and pointless.
        self.assertFalse(net.is_app_host("cdn.yopu.co"))

    def test_app_host_is_case_insensitive(self):
        self.assertTrue(net.is_app_host("YOPU.CO"))

    def test_app_host_none_is_false(self):
        self.assertFalse(net.is_app_host(None))

    def test_trackers_matched_and_content_hosts_left_alone(self):
        self.assertTrue(net.is_tracker("https://www.google-analytics.com/g/collect"))
        self.assertTrue(net.is_tracker("https://o1.ingest.sentry.io/x"))
        self.assertFalse(net.is_tracker("https://cdn.yopu.co/js/view.es.js"))
        self.assertFalse(net.is_tracker("https://yopu.co/z/abc"))


class TestEgressResolution(unittest.TestCase):
    def setUp(self):
        # Hermetic: no ambient egress env, and a config dir that starts empty.
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.env = mock.patch.dict(os.environ,
                                   {"YOPU_PDF_CONFIG_DIR": str(self.tmp)}, clear=False)
        self.env.start()
        os.environ.pop("YOPU_PDF_EGRESS", None)
        self.addCleanup(self.env.stop)

    def test_none_when_nothing_configured(self):
        self.assertIsNone(net.resolve_egress(None))

    def test_cli_wins(self):
        os.environ["YOPU_PDF_EGRESS"] = "socks5://127.0.0.1:1"
        self.assertEqual(net.resolve_egress("worker https://w/?k=K"), "worker https://w/?k=K")

    def test_env_over_config(self):
        (self.tmp / "egress").write_text("socks5://from-config:1\n", encoding="utf-8")
        os.environ["YOPU_PDF_EGRESS"] = "socks5://from-env:2"
        self.assertEqual(net.resolve_egress(None), "socks5://from-env:2")

    def test_blank_cli_does_not_shadow_config(self):
        (self.tmp / "egress").write_text("socks5://from-config:1\n", encoding="utf-8")
        self.assertEqual(net.resolve_egress("   "), "socks5://from-config:1")

    def test_config_ignores_comments_and_blanks(self):
        (self.tmp / "egress").write_text("# a comment\n\n  \nworker https://w/?k=K\n",
                                         encoding="utf-8")
        self.assertEqual(net.resolve_egress(None), "worker https://w/?k=K")

    def test_comment_only_config_reads_as_unconfigured_not_broken(self):
        # Absent-is-not-a-broken-egress: a file with no real line means "direct".
        (self.tmp / "egress").write_text("# nothing here\n", encoding="utf-8")
        self.assertIsNone(net.resolve_egress(None))


class TestParseEgress(unittest.TestCase):
    def test_worker_space_form(self):
        self.assertEqual(net.parse_egress("worker https://w.dev/?k=K"),
                         ("worker", "https://w.dev/?k=K"))

    def test_worker_colon_form(self):
        self.assertEqual(net.parse_egress("worker:https://w.dev/?k=K"),
                         ("worker", "https://w.dev/?k=K"))

    def test_socks_and_http_are_browser_proxies(self):
        self.assertEqual(net.parse_egress("socks5://127.0.0.1:7897"),
                         ("browser-proxy", "socks5://127.0.0.1:7897"))
        self.assertEqual(net.parse_egress("http://127.0.0.1:8080"),
                         ("browser-proxy", "http://127.0.0.1:8080"))

    def test_a_bare_worker_https_url_without_prefix_is_treated_as_a_proxy(self):
        # This is why the 'worker' prefix is required: a Worker base is itself an
        # https URL and is indistinguishable from an http proxy without it.
        self.assertEqual(net.parse_egress("https://w.dev/?k=K")[0], "browser-proxy")


class TestWorkerUrl(unittest.TestCase):
    def test_appends_with_ampersand_when_key_present(self):
        u = net.build_worker_url("https://w.dev/?k=SECRET", "https://yopu.co/z/abc?x=1")
        self.assertTrue(u.startswith("https://w.dev/?k=SECRET&u="))
        self.assertIn("https%3A%2F%2Fyopu.co%2Fz%2Fabc%3Fx%3D1", u)

    def test_uses_question_mark_when_base_has_no_query(self):
        u = net.build_worker_url("https://w.dev/", "https://yopu.co/view/x")
        self.assertIn("/?u=", u)


class TestHeaderPlumbing(unittest.TestCase):
    def test_pack_headers_round_trips(self):
        h = {"user-agent": "UA", "cookie": "c=1", "referer": "https://yopu.co/"}
        self.assertEqual(json.loads(base64.b64decode(net.pack_headers(h))), h)

    def test_filter_keeps_signature_and_cookie_drops_length(self):
        got = net.filter_response_headers({
            "Content-Type": "text/plain", "Content-Length": "70981",
            "Content-Encoding": "gzip", "X-Signature": "sig123",
            "X-Used-Nonce": "v3-abc", "Set-Cookie": "c=2",
            "x-yp-upstream-status": "200",
        })
        self.assertEqual(got.get("X-Signature"), "sig123")     # engine reads this
        self.assertEqual(got.get("X-Used-Nonce"), "v3-abc")
        self.assertEqual(got.get("Set-Cookie"), "c=2")
        self.assertEqual(got.get("Content-Type"), "text/plain")
        self.assertNotIn("Content-Length", got)                # would corrupt the body
        self.assertNotIn("Content-Encoding", got)
        self.assertNotIn("x-yp-upstream-status", got)


class _FakeResp:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self._body = body

    def read(self):
        return self._body


class TestWorkerRelay(unittest.TestCase):
    """The relay is a fake here; we assert what it *sends* and that it relays
    upstream errors faithfully instead of aborting."""

    def _capture(self, resp=None, error=None):
        sent = {}

        def opener(req, timeout=None):
            sent["url"] = req.full_url
            sent["method"] = req.get_method()
            # urllib title-cases header keys
            sent["headers"] = {k.lower(): v for k, v in req.header_items()}
            sent["body"] = req.data
            if error:
                raise error
            return resp

        return opener, sent

    def test_success_reports_upstream_status_and_forwards_envelope(self):
        resp = _FakeResp(200, {"x-yp-upstream-status": "200", "content-type": "text/plain",
                               "content-length": "5"}, b"hello")
        opener, sent = self._capture(resp=resp)
        relay = net.make_worker_relay("https://w.dev/?k=K", opener=opener)
        status, headers, body = relay("https://yopu.co/z/abc", "GET",
                                      {"user-agent": "ChromeUA", "cookie": "c=1"}, None)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"hello")
        self.assertNotIn("content-length", {k.lower() for k in headers})
        # outer request carried the browser UA (passes CF's bot check) + the envelope
        self.assertEqual(sent["headers"]["user-agent"], net.OUTER_UA)
        self.assertEqual(sent["headers"]["x-yp-method"], "GET")
        packed = json.loads(base64.b64decode(sent["headers"]["x-yp-headers"]))
        self.assertEqual(packed["cookie"], "c=1")
        self.assertIn("u=https%3A%2F%2Fyopu.co%2Fz%2Fabc", sent["url"])

    def test_upstream_404_is_relayed_not_raised(self):
        # The regression that stopped the sheet rendering: a 404 must come back as
        # a real (404, body) response, never as an abort/raise.
        err = urllib.error.HTTPError(
            "https://w.dev", 404,
            "Not Found", {"x-yp-upstream-status": "404", "content-type": "text/plain"},
            io.BytesIO(b""))
        opener, _ = self._capture(error=err)
        relay = net.make_worker_relay("https://w.dev/?k=K", opener=opener)
        status, headers, body = relay("https://yopu.co/z/missing", "GET", {}, None)
        self.assertEqual(status, 404)
        self.assertEqual(body, b"")

    def test_post_body_is_forwarded(self):
        resp = _FakeResp(200, {"x-yp-upstream-status": "200"}, b"ok")
        opener, sent = self._capture(resp=resp)
        relay = net.make_worker_relay("https://w.dev/?k=K", opener=opener)
        relay("https://yopu.co/api/x", "POST", {"content-type": "application/json"}, '{"a":1}')
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(sent["body"], b'{"a":1}')
        self.assertEqual(sent["headers"]["x-yp-method"], "POST")


if __name__ == "__main__":
    unittest.main()


class TestParseHttpDump(unittest.TestCase):
    def test_simple(self):
        raw = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nX-Signature: sig\r\n\r\nHELLO"
        st, hd, body = net.parse_http_dump(raw)
        self.assertEqual(st, 200)
        self.assertEqual(hd["Content-Type"], "text/plain")
        self.assertEqual(hd["X-Signature"], "sig")
        self.assertEqual(body, b"HELLO")

    def test_takes_last_block_after_redirect(self):
        raw = (b"HTTP/1.1 302 Found\r\nLocation: /x\r\n\r\n"
               b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"a\":1}")
        st, hd, body = net.parse_http_dump(raw)
        self.assertEqual(st, 200)
        self.assertEqual(body, b'{"a":1}')


class TestSshRelay(unittest.TestCase):
    def _relay_with(self, dump_bytes):
        seen = {}
        def runner(ctl, host, remote_cmd, stdin, timeout):
            seen["cmd"] = remote_cmd
            seen["stdin"] = stdin
            import base64 as b64
            return b64.b64encode(dump_bytes)
        relay = net.make_ssh_relay("test-vps", runner=runner)
        return relay, seen

    def test_uses_a_shared_cookie_jar_and_drops_browser_cookie(self):
        relay, seen = self._relay_with(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nDATA")
        st, hd, body = relay("https://yopu.co/z/abc", "GET",
                             {"User-Agent": "UA", "Cookie": "stale=1", "Referer": "https://yopu.co/"}, None)
        self.assertEqual((st, body), (200, b"DATA"))
        cmd = seen["cmd"]
        self.assertIn("-c", cmd); self.assertIn("ypjar_test_vps", cmd)   # jar present
        self.assertIn("-b", cmd)
        self.assertNotIn("Cookie:", cmd)                                # browser cookie dropped
        self.assertIn("Referer: https://yopu.co/", cmd)                 # other headers kept
        self.assertIn("https://yopu.co/z/abc", cmd)

    def test_upstream_404_relayed_faithfully(self):
        relay, _ = self._relay_with(b"HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\n\r\n")
        st, hd, body = relay("https://yopu.co/z/missing", "GET", {}, None)
        self.assertEqual(st, 404)
        self.assertEqual(body, b"")

    def test_post_body_sent_on_stdin(self):
        relay, seen = self._relay_with(b"HTTP/1.1 200 OK\r\n\r\nok")
        relay("https://yopu.co/api/x", "POST", {"Content-Type": "application/json"}, '{"a":1}')
        self.assertIn("--data-binary", seen["cmd"])
        self.assertEqual(seen["stdin"], b'{"a":1}')


class TestDecodeZPath(unittest.TestCase):
    """The /z de-obfuscation, pinned to the two real captures. If the site
    rotates its codec these vectors change and this test is the tripwire."""
    def test_decodes_the_sheet_endpoint(self):
        self.assertEqual(
            net.decode_z_path("azhjbQRhPS8vcxE9OQR6M3M0LjI5OTk_ORYsJj9oNWEo"),
            "/api/sheet?code=MXJz47aX&screen=1")

    def test_decodes_the_filled_queries_endpoint(self):
        self.assertEqual(
            net.decode_z_path("Lj05KSlhOTBzczE5Li0oNXMyPy8vOjU9OyhxLDA9KTQoOS41NTkvLjI1Yzg"),
            "/api/search/filled-queries?instrument=guitar")

    def test_non_endpoint_returns_none(self):
        self.assertIsNone(net.decode_z_path("!!!not-valid"))
