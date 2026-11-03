# CS2 News Watcher

A small GitHub Actions job that polls free public feeds for Counter-Strike 2 news (patches, builds, roster moves, and related esports headlines) and notifies your assistant when **new** items appear.

On each run the workflow:

1. Fetches configured RSS feeds.
2. Compares item IDs against `state/seen.json` in this repo.
3. **First run only:** records everything currently in the feeds as already seen and does **not** call the webhook (avoids a flood of old headlines).
4. **Later runs:** classifies new items, POSTs one JSON payload for items that pass the filter, and updates `state/seen.json`. Filtered-out esports headlines are still marked seen so they are not reconsidered. If the webhook POST fails, only **forward** items stay unseen for retry.

### Skin-market filter

| Feed | Rule |
|------|------|
| `steam_store`, `steamdb_patchnotes` | **Always forward** (official updates and every patch note). |
| `hltv`, `dexerto_cs` | Forward only when the title or summary matches a keyword pattern (word boundaries, case-insensitive). |

Edit the lists at the top of `scripts/watch_cs2_news.py`:

- `ALWAYS_FORWARD_FEEDS` — feeds that bypass keyword filtering.
- `ESPORTS_MARKET_KEYWORD_PATTERNS` — regex patterns for esports feeds, including skin/economy terms (`case`, `collection`, `skin`, `knife`, `glove`, `sticker`, `capsule`, `souvenir`, `armory`, `trade-up`, `contract`, `operation`, `market`, `drop pool`, `Valve`, `update`, `patch`, `Major`) and major pro-scene events (`disband`, org shuts down / exits CS, Major wins/qualification/elimination, high-profile retirement wording).

Routine roster moves, match previews/results, and opinion pieces are dropped unless they also hit a market keyword.

Preview filtering against live feeds (no state or webhook):

```bash
python3 scripts/watch_cs2_news.py --filter-stats
```

Scheduled GitHub Actions runs are **disabled**; the watcher now runs on another machine. The workflow here is kept for optional manual runs only.

## Feeds

| Feed key | Source |
|----------|--------|
| `steam_store` | [Steam news RSS for app 730](https://store.steampowered.com/feeds/news/app/730/) |
| `steamdb_patchnotes` | [SteamDB patch/build RSS](https://steamdb.info/api/PatchnotesRSS/?appid=730) |
| `hltv` | [HLTV news RSS](https://www.hltv.org/rss/news) |
| `dexerto_cs` | [Dexerto CS RSS](https://www.dexerto.com/csgo/feed/) |

Feeds considered but not used:

- **Steam `ISteamNews` API** — works, but mixes many third-party/community posts (often non-English); official Steam store RSS is enough for patch/market-relevant announcements.
- **blog.counter-strike.net** — RSS is stale (last posts from 2023).
- **SteamDB patch notes HTML** — returns 403 from automation; the SteamDB **PatchnotesRSS API** works and is used instead.
- **PC Gamer CS2 tag RSS** — 404.

## Webhook payload

```json
{
  "source": "cs2-news-watcher",
  "items": [
    {
      "feed": "steam_store",
      "title": "...",
      "link": "...",
      "published": "2026-11-03T01:28:55+00:00",
      "summary": "Plain-text excerpt"
    }
  ]
}
```

## Repository secrets

In GitHub: **Settings → Secrets and variables → Actions → New repository secret**

1. **`WEBHOOK_URL`** — full URL to POST the JSON payload to.
2. **`WEBHOOK_AUTH`** — the complete value for the `Authorization` request header (for example `Bearer your-token`). The workflow sends `Authorization: <WEBHOOK_AUTH>` exactly as stored.

If either secret is missing, the watcher logs a message and exits without failing the job. Items that would be forwarded stay unseen for retry; filtered-out items are still marked seen.

## Manual run

**Actions → CS2 News Watcher → Run workflow → Run workflow**


## Local development

```bash
python scripts/watch_cs2_news.py
```

Use a temporary state file by pointing at a copy of `state/seen.json`, or delete `seeded` to simulate a first run.

Test the webhook with a local listener:

```bash
python -m http.server 8765  # or a small script that prints POST bodies
export WEBHOOK_URL=http://127.0.0.1:8765/
export WEBHOOK_AUTH="Bearer test"
# Seed state first, then add a fake unseen id or wait for real news
python scripts/watch_cs2_news.py
```
