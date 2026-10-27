#!/usr/bin/env python3
"""hermes-reddit-reader: a personal, read-only Reddit reader for one user's AI assistant.

It can only READ: every API call is a GET to https://oauth.reddit.com, and no code path exists to
post, comment, vote, message, subscribe or moderate. Python 3.9+ standard library only.

Commands
  status                                  check credentials and API access (first line: STATUS: …)
  feed  [SUB ...] [--sort new|hot|top] [--time week] [--limit 10]
  search QUERY [--sub SUB] [--sort relevance|new|top] [--time year] [--limit 10]
  thread URL_OR_ID [--comments 30] [--depth 3]
  wiki SUB PAGE                           e.g. wiki FashionReps trusted
  --json on any command prints raw-ish JSON instead of text.

Configuration (environment)
  REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET  from the approved "script" app (reddit.com/prefs/apps)
  REDDIT_USERNAME                         the dedicated account that owns the app
  REDDIT_PASSWORD                         optional; otherwise read from the macOS Keychain item
                                          service "hermes-reddit", account = REDDIT_USERNAME
  REDDIT_SUBREDDITS                       default subreddits for `feed` (comma-separated)
  REDDIT_READER_DATA                      data dir (default: ./data next to this file)
  REDDIT_CONTACT                          contact shown in the User-Agent (default: /u/<username>)

Output contract: the first line is always `STATUS: <code> …` with code in
OK, NOT_CONFIGURED, AUTH_FAILED, NOT_APPROVED, RATE_LIMITED, NOT_FOUND, NETWORK_DOWN, ERROR.
The same status is written to <data>/status.json for health checks. Exit code 0 only on OK.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "0.1.0"
API = "https://oauth.reddit.com"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
SCOPES = "read identity"
DEFAULT_SUBS = "FashionReps,Repsneakers,kakobuy,FashionRepsHub"
CACHE_TTL = 15 * 60          # seconds a GET response is reused
MIN_INTERVAL = 1.0           # seconds between two API calls (Reddit allows ~100 QPM per client)
HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("REDDIT_READER_DATA") or HERE / "data")


class ReaderError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ---------------- status ----------------
def write_status(code, detail):
    DATA.mkdir(parents=True, exist_ok=True)
    payload = {"status": code, "detail": detail, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "version": VERSION}
    tmp = DATA / "status.json.tmp"
    tmp.write_text(json.dumps(payload, indent=1))
    os.replace(tmp, DATA / "status.json")


# ---------------- config ----------------
def config():
    cid, secret = os.environ.get("REDDIT_CLIENT_ID", ""), os.environ.get("REDDIT_CLIENT_SECRET", "")
    user = os.environ.get("REDDIT_USERNAME", "")
    missing = [n for n, v in (("REDDIT_CLIENT_ID", cid), ("REDDIT_CLIENT_SECRET", secret),
                              ("REDDIT_USERNAME", user)) if not v]
    if missing:
        raise ReaderError("NOT_CONFIGURED", "missing " + ", ".join(missing))
    pwd = os.environ.get("REDDIT_PASSWORD") or keychain_password(user)
    if not pwd:
        raise ReaderError("NOT_CONFIGURED", "no password: set REDDIT_PASSWORD or add Keychain item "
                          "service 'hermes-reddit' account '%s'" % user)
    contact = os.environ.get("REDDIT_CONTACT") or "/u/" + user
    ua = "macos:hermes-reddit-reader:v%s (personal read-only; by %s)" % (VERSION, contact)
    return {"id": cid, "secret": secret, "user": user, "password": pwd, "ua": ua}


def keychain_password(user):
    try:
        out = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", "hermes-reddit",
                              "-a", user, "-w"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------------- HTTP ----------------
def _http(req, timeout=30):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read() or b""
    except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
        raise ReaderError("NETWORK_DOWN", "cannot reach Reddit: %s" % getattr(e, "reason", e))


def get_token(cfg):
    """Password grant for a script app; cached in <data>/token.json (0600) until expiry."""
    tok_file = DATA / "token.json"
    if tok_file.exists():
        try:
            t = json.loads(tok_file.read_text())
            if t.get("expires_at", 0) > time.time() + 60 and t.get("client_id") == cfg["id"]:
                return t["access_token"]
        except (ValueError, KeyError):
            pass
    body = urllib.parse.urlencode({"grant_type": "password", "username": cfg["user"],
                                   "password": cfg["password"], "scope": SCOPES}).encode()
    auth = base64.b64encode(("%s:%s" % (cfg["id"], cfg["secret"])).encode()).decode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={
        "Authorization": "Basic " + auth, "User-Agent": cfg["ua"],
        "Content-Type": "application/x-www-form-urlencoded"})
    code, _, raw = _http(req)
    try:
        data = json.loads(raw.decode() or "{}")
    except ValueError:
        data = {}
    if code == 401:
        raise ReaderError("AUTH_FAILED", "client id/secret rejected (HTTP 401)")
    if code == 403:
        raise ReaderError("NOT_APPROVED", "token request refused (HTTP 403): API access not approved yet?")
    if code != 200 or "access_token" not in data:
        err = data.get("error") or ("HTTP %s" % code)
        if err == "invalid_grant":
            raise ReaderError("AUTH_FAILED", "username/password rejected (invalid_grant; 2FA accounts can't use the password grant)")
        raise ReaderError("AUTH_FAILED" if code < 500 else "ERROR", "token request failed: %s" % err)
    DATA.mkdir(parents=True, exist_ok=True)
    t = {"access_token": data["access_token"], "scope": data.get("scope"), "client_id": cfg["id"],
         "expires_at": time.time() + int(data.get("expires_in", 3600))}
    fd = os.open(str(tok_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(t, fh)
    return t["access_token"]


_last_call = [0.0]


def api_get(cfg, path, params=None, use_cache=True):
    """The ONLY way this tool talks to the API: a GET on oauth.reddit.com."""
    params = dict(params or {}, raw_json=1)
    url = API + path + "?" + urllib.parse.urlencode(params)
    if not url.startswith(API + "/"):
        raise ReaderError("ERROR", "refusing non-API URL")
    cache = DATA / "cache" / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".json")
    if use_cache and cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL:
        return json.loads(cache.read_text())
    wait = MIN_INTERVAL - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    for attempt in (1, 2):
        token = get_token(cfg)
        req = urllib.request.Request(url, method="GET", headers={
            "Authorization": "bearer " + token, "User-Agent": cfg["ua"]})
        code, headers, raw = _http(req)
        _last_call[0] = time.time()
        if code == 401 and attempt == 1:          # expired/revoked token: refresh once
            (DATA / "token.json").unlink(missing_ok=True)
            continue
        break
    remaining = headers.get("x-ratelimit-remaining") or headers.get("X-Ratelimit-Remaining")
    if remaining is not None:
        try:
            if float(remaining) < 5:
                reset = float(headers.get("x-ratelimit-reset") or headers.get("X-Ratelimit-Reset") or 60)
                time.sleep(min(reset, 120))
        except ValueError:
            pass
    if code == 429:
        raise ReaderError("RATE_LIMITED", "HTTP 429 from Reddit; retry later")
    if code == 403:
        raise ReaderError("NOT_APPROVED", "HTTP 403 on %s (private/quarantined sub, or API access not approved)" % path)
    if code == 404:
        raise ReaderError("NOT_FOUND", "HTTP 404 on %s" % path)
    if code != 200:
        raise ReaderError("ERROR", "HTTP %s on %s" % (code, path))
    data = json.loads(raw.decode())
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data))
    return data


# ---------------- formatting ----------------
def clean_sub(s):
    s = s.strip().removeprefix("r/").removeprefix("/r/")
    if not re.fullmatch(r"[A-Za-z0-9_]{2,21}", s):
        raise ReaderError("ERROR", "invalid subreddit name %r" % s)
    return s


def age(ts):
    d = max(0, time.time() - float(ts))
    return "%dm" % (d // 60) if d < 3600 else "%dh" % (d // 3600) if d < 86400 else "%dd" % (d // 86400)


def short(text, n):
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def post_line(p):
    flair = " [%s]" % p["link_flair_text"] if p.get("link_flair_text") else ""
    lines = ["- r/%s%s %s (%s pts, %s comments, %s ago) https://www.reddit.com%s" % (
        p.get("subreddit"), flair, short(p.get("title"), 160), p.get("score"), p.get("num_comments"),
        age(p.get("created_utc", time.time())), p.get("permalink"))]
    if p.get("selftext"):
        lines.append("  " + short(p["selftext"], 400))
    elif p.get("url") and "reddit.com" not in p.get("url", ""):
        lines.append("  link: " + p["url"])
    return "\n".join(lines)


def listing_posts(data):
    return [c["data"] for c in data.get("data", {}).get("children", []) if c.get("kind") == "t3"]


def walk_comments(children, depth, max_depth, out, limit):
    for c in children:
        if len(out) >= limit or c.get("kind") != "t1":
            continue
        d = c["data"]
        out.append("%s- u/%s (%s pts): %s" % ("  " * depth, d.get("author"), d.get("score"), short(d.get("body"), 500)))
        replies = d.get("replies")
        if depth + 1 < max_depth and isinstance(replies, dict):
            walk_comments(replies.get("data", {}).get("children", []), depth + 1, max_depth, out, limit)


def post_id(ref):
    m = re.search(r"/comments/([a-z0-9]+)", ref)
    if m:
        return m.group(1)
    ref = ref.removeprefix("t3_")
    if re.fullmatch(r"[a-z0-9]{4,10}", ref):
        return ref
    raise ReaderError("ERROR", "not a Reddit post URL or id: %r" % ref)


# ---------------- commands ----------------
def cmd_status(cfg, a):
    me = api_get(cfg, "/api/v1/me", use_cache=False)
    return "account u/%s, scopes %s" % (me.get("name"), SCOPES), me


def cmd_feed(cfg, a):
    subs = [clean_sub(s) for s in (a.subs or os.environ.get("REDDIT_SUBREDDITS", DEFAULT_SUBS).split(","))]
    params = {"limit": a.limit}
    if a.sort == "top":
        params["t"] = a.time
    data = api_get(cfg, "/r/%s/%s" % ("+".join(subs), a.sort), params)
    posts = listing_posts(data)
    text = "\n".join(post_line(p) for p in posts) or "(no posts)"
    return "%d post(s) from r/%s, sort=%s" % (len(posts), "+".join(subs), a.sort), (posts if a.json else text)


def cmd_search(cfg, a):
    params = {"q": a.query, "sort": a.sort, "t": a.time, "limit": a.limit, "type": "link"}
    if a.sub:
        params["restrict_sr"] = "on"
        path = "/r/%s/search" % clean_sub(a.sub)
    else:
        path = "/search"
    posts = listing_posts(api_get(cfg, path, params))
    text = "\n".join(post_line(p) for p in posts) or "(no results)"
    return "%d result(s) for %r%s" % (len(posts), a.query, " in r/" + a.sub if a.sub else ""), (posts if a.json else text)


def cmd_thread(cfg, a):
    pid = post_id(a.ref)
    data = api_get(cfg, "/comments/%s" % pid, {"limit": a.comments, "depth": a.depth, "sort": "top"})
    post = data[0]["data"]["children"][0]["data"]
    out = []
    walk_comments(data[1]["data"]["children"], 0, a.depth, out, a.comments)
    if a.json:
        return "thread %s" % pid, data
    head = post_line(post)
    if post.get("selftext"):
        head = head.split("\n")[0] + "\n  " + short(post["selftext"], 3000)
    return "thread %s, %d comment(s) shown" % (pid, len(out)), head + "\n\nComments (top first):\n" + ("\n".join(out) or "(none)")


def cmd_wiki(cfg, a):
    sub, page = clean_sub(a.sub), a.page.strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_/\-]{1,100}", page):
        raise ReaderError("ERROR", "invalid wiki page %r" % page)
    data = api_get(cfg, "/r/%s/wiki/%s" % (sub, page))
    md = data.get("data", {}).get("content_md", "")
    return "wiki r/%s/%s, %d chars" % (sub, page, len(md)), (data if a.json else md)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true")
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("status")
    f = s.add_parser("feed"); f.add_argument("subs", nargs="*")
    f.add_argument("--sort", default="new", choices=["new", "hot", "top"])
    f.add_argument("--time", default="week", choices=["hour", "day", "week", "month", "year", "all"])
    f.add_argument("--limit", type=int, default=10)
    q = s.add_parser("search"); q.add_argument("query"); q.add_argument("--sub")
    q.add_argument("--sort", default="relevance", choices=["relevance", "new", "top", "comments"])
    q.add_argument("--time", default="year", choices=["hour", "day", "week", "month", "year", "all"])
    q.add_argument("--limit", type=int, default=10)
    t = s.add_parser("thread"); t.add_argument("ref")
    t.add_argument("--comments", type=int, default=30); t.add_argument("--depth", type=int, default=3)
    w = s.add_parser("wiki"); w.add_argument("sub"); w.add_argument("page")
    a = p.parse_args(argv)
    if hasattr(a, "limit"):
        a.limit = max(1, min(a.limit, 100))
    handlers = {"status": cmd_status, "feed": cmd_feed, "search": cmd_search, "thread": cmd_thread, "wiki": cmd_wiki}
    try:
        cfg = config()
        summary, body = handlers[a.cmd](cfg, a)
    except ReaderError as e:
        write_status(e.code, str(e))
        print("STATUS: %s %s" % (e.code, e))
        return 1
    except Exception as e:  # never crash without a STATUS line
        write_status("ERROR", "%s: %s" % (type(e).__name__, e))
        print("STATUS: ERROR %s: %s" % (type(e).__name__, e))
        return 1
    write_status("OK", summary)
    print("STATUS: OK %s" % summary)
    if body is not None and (a.cmd != "status" or a.json):
        print(json.dumps(body, indent=1, ensure_ascii=False) if not isinstance(body, str) else body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
