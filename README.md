# hermes-reddit-reader

A small, **read-only** Reddit reader used by one person's private AI assistant (Hermes Agent) on his own Mac.
It reads public posts, comments and wiki pages in a handful of subreddits so the assistant can summarise
sizing advice, seller feedback and quality-check discussions for the owner's own clothing purchases.

## What it does, and what it can't do
- **Reads only.** Every API call goes through one function that sends a `GET` to `https://oauth.reddit.com`.
  There is no code path that posts, comments, votes, sends messages, subscribes or moderates
  (a test checks that no write endpoint appears in the source).
- **Scopes requested:** `read identity`.
- **Personal and non-commercial.** One user, one dedicated Reddit account that is used only by this app.
  No redistribution of Reddit data, no AI model training, no resale.
- **Light on the API:** at most one request per second, responses cached for 15 minutes, back-off when
  `X-Ratelimit-Remaining` gets low, stops on HTTP 429. Typical use is a few hundred requests per day at most.
- **Descriptive User-Agent:** `macos:hermes-reddit-reader:v<version> (personal read-only; by /u/<account>)`.
- **Minimal local data:** short-lived response cache and an OAuth token file (0600) in `data/`
  (git-ignored). The assistant keeps short summaries with links, not copies of threads.

Intended subreddits: r/FashionReps, r/Repsneakers, r/kakobuy, r/FashionRepsHub.

## Usage
```bash
cp .env.example .env && chmod 600 .env    # fill in the script app's client id/secret and the username
security add-generic-password -s hermes-reddit -a <username> -w   # password in the macOS Keychain

./run.sh status                                   # STATUS: OK account u/…, scopes read identity
./run.sh feed --sort top --time week --limit 10   # default subreddits from REDDIT_SUBREDDITS
./run.sh search "jacket sizing" --sub FashionReps --sort top --time year
./run.sh thread https://www.reddit.com/r/FashionReps/comments/<id>/<slug>/ --comments 30
./run.sh wiki FashionReps trusted
```
The first output line is always `STATUS: <code> …` (`OK`, `NOT_CONFIGURED`, `AUTH_FAILED`, `NOT_APPROVED`,
`RATE_LIMITED`, `NOT_FOUND`, `NETWORK_DOWN`, `ERROR`), mirrored in `data/status.json` for health checks.

Requirements: Python 3.9+, standard library only. Tests (offline, no network): `python3 -m unittest tests/test_reader.py`.

## Compliance
Built to follow Reddit's [Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy)
and [Data API Terms](https://redditinc.com/policies/data-api-terms). It is used only after Reddit approves API access for the account.

License: MIT.
