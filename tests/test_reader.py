"""Offline tests: the HTTP layer is replaced by a fake, nothing reaches Reddit."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import reddit_reader as rr  # noqa: E402


def listing(*titles):
    return {"kind": "Listing", "data": {"children": [
        {"kind": "t3", "data": {"subreddit": "FashionReps", "title": t, "score": 10, "num_comments": 2,
                                "created_utc": 0, "permalink": "/r/FashionReps/comments/abc123/x/",
                                "selftext": "body", "link_flair_text": "QC"}} for t in titles]}}


class FakeHTTP:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, req, timeout=30):
        self.calls.append((req.get_method(), req.full_url))
        for key, resp in self.routes.items():
            if key in req.full_url:
                code, body = resp
                return code, {}, json.dumps(body).encode()
        return 404, {}, b"{}"


class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        rr.DATA = Path(self.tmp.name)
        rr.MIN_INTERVAL = 0
        os.environ.update(REDDIT_CLIENT_ID="cid", REDDIT_CLIENT_SECRET="sec",
                          REDDIT_USERNAME="bot", REDDIT_PASSWORD="pw")
        self.orig = rr._http

    def tearDown(self):
        rr._http = self.orig
        self.tmp.cleanup()

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = rr.main(list(argv))
        return code, buf.getvalue()

    def test_not_configured(self):
        del os.environ["REDDIT_CLIENT_ID"]
        code, out = self.run_cli("status")
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("STATUS: NOT_CONFIGURED"))
        self.assertEqual(json.loads((rr.DATA / "status.json").read_text())["status"], "NOT_CONFIGURED")

    def test_feed_ok_and_only_gets_api(self):
        fake = FakeHTTP({"access_token": (200, {"access_token": "T", "expires_in": 3600}),
                         "/r/FashionReps+kakobuy/new": (200, listing("W2C hoodie", "QC jacket"))})
        rr._http = fake
        code, out = self.run_cli("feed", "FashionReps", "r/kakobuy")
        self.assertEqual(code, 0, out)
        self.assertIn("STATUS: OK 2 post(s)", out)
        self.assertIn("[QC] W2C hoodie", out)
        api_calls = [c for c in fake.calls if "oauth.reddit.com" in c[1]]
        self.assertTrue(api_calls and all(m == "GET" for m, _ in api_calls))
        self.assertEqual(oct((rr.DATA / "token.json").stat().st_mode & 0o777), "0o600")

    def test_token_refused(self):
        rr._http = FakeHTTP({"access_token": (403, {})})
        code, out = self.run_cli("feed")
        self.assertTrue(out.startswith("STATUS: NOT_APPROVED"))

    def test_bad_password(self):
        rr._http = FakeHTTP({"access_token": (400, {"error": "invalid_grant"})})
        code, out = self.run_cli("status")
        self.assertTrue(out.startswith("STATUS: AUTH_FAILED"))

    def test_rate_limited(self):
        rr._http = FakeHTTP({"access_token": (200, {"access_token": "T", "expires_in": 3600}),
                             "/search": (429, {})})
        code, out = self.run_cli("search", "arcteryx sizing", "--sub", "FashionReps")
        self.assertTrue(out.startswith("STATUS: RATE_LIMITED"))

    def test_thread_and_ids(self):
        post = listing("Sizing help")["data"]["children"]
        comments = {"data": {"children": [{"kind": "t1", "data": {"author": "a", "score": 5, "body": "size up",
                    "replies": {"data": {"children": [{"kind": "t1", "data": {"author": "b", "score": 1,
                                                                               "body": "agree", "replies": ""}}]}}}}]}}
        rr._http = FakeHTTP({"access_token": (200, {"access_token": "T", "expires_in": 3600}),
                             "/comments/abc123": (200, [{"data": {"children": post}}, comments])})
        code, out = self.run_cli("thread", "https://www.reddit.com/r/FashionReps/comments/abc123/sizing/")
        self.assertEqual(code, 0, out)
        self.assertIn("u/a (5 pts): size up", out)
        self.assertIn("  - u/b", out)
        with self.assertRaises(rr.ReaderError):
            rr.post_id("not a url")

    def test_invalid_subreddit(self):
        code, out = self.run_cli("feed", "../etc")
        self.assertTrue(out.startswith("STATUS: ERROR invalid subreddit"))

    def test_no_write_endpoints_in_code(self):
        src = Path(rr.__file__).read_text()
        for verb in ("/api/submit", "/api/comment", "/api/vote", "/api/compose", "/api/subscribe", "/api/del"):
            self.assertNotIn(verb, src)


if __name__ == "__main__":
    unittest.main()
