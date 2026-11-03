#!/usr/bin/env python3
"""Poll public CS2 news feeds and POST new items to a webhook."""

from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

USER_AGENT = "cs2-news-watcher/1.0 (+https://github.com/actions)"
STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "seen.json"
MAX_SEEN = 500
SUMMARY_MAX = 400
REQUEST_TIMEOUT = 30

# Feeds where every item is forwarded (official game / market signals).
ALWAYS_FORWARD_FEEDS = frozenset({"steam_store", "steamdb_patchnotes"})

# Case-insensitive, word-boundary patterns on title + summary for esports feeds (HLTV, Dexerto).
ESPORTS_MARKET_KEYWORD_PATTERNS: list[str] = [
    # Skins / economy
    r"\bcases?\b",
    r"\bcollections?\b",
    r"\bskins?\b",
    r"\bknives?\b",
    r"\bgloves?\b",
    r"\bstickers?\b",
    r"\bcapsules?\b",
    r"\bsouvenirs?\b",
    r"\barmou?ry\b",
    r"\btrade[-\s]?ups?\b",
    r"\bcontracts?\b",
    r"\boperations?\b",
    r"\bmarkets?\b",
    r"\bdrop\s+pools?\b",
    r"\bValve\b",
    r"\bupdates?\b",
    r"\bpatches?\b",
    r"\bMajor\b",
    # Major pro-scene (not routine transfers / match coverage)
    r"\bdisband(?:ed|s)?\b",
    r"\bshuts?\s+down\b",
    r"\bexits?\s+CS(?:GO|2)?\b",
    r"\bMajor\s+champions?\b",
    r"\bMajor\s+winners?\b",
    r"\bwon?\s+(?:the\s+)?Major\b",
    r"\bqualified\s+for\s+(?:the\s+)?Major\b",
    r"\bMajor\s+qualification\b",
    r"\beliminated\s+from\s+(?:the\s+)?Major\b",
    r"\bMajor\s+elimination\b",
    r"\blegendary\s+player\s+retir",
    r"\b(?:announces?|confirm(?:s|ed)?)\s+retirement\b",
]

_ESPORTS_MARKET_RE = [re.compile(p, re.IGNORECASE) for p in ESPORTS_MARKET_KEYWORD_PATTERNS]

FEEDS: list[dict[str, str]] = [
    {
        "feed": "steam_store",
        "url": "https://store.steampowered.com/feeds/news/app/730/",
        "type": "rss",
    },
    {
        "feed": "steamdb_patchnotes",
        "url": "https://steamdb.info/api/PatchnotesRSS/?appid=730",
        "type": "rss",
    },
    {
        "feed": "hltv",
        "url": "https://www.hltv.org/rss/news",
        "type": "rss",
    },
    {
        "feed": "dexerto_cs",
        "url": "https://www.dexerto.com/csgo/feed/",
        "type": "rss",
    },
]

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return resp.read()


def plain_summary(text: str | None) -> str:
    if not text:
        return ""
    unescaped = html.unescape(text)
    stripped = TAG_RE.sub(" ", unescaped)
    collapsed = WS_RE.sub(" ", stripped).strip()
    if len(collapsed) > SUMMARY_MAX:
        return collapsed[: SUMMARY_MAX - 1].rstrip() + "…"
    return collapsed


def parse_pub_date(raw: str | None) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%a, %d %b %Y %H:%M:%S %z",
    ):
        try:
            dt = datetime.strptime(raw.replace("Z", "+0000"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
    return raw


def local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def child_text(node: ET.Element, name: str) -> str:
    for child in node:
        if local(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def parse_rss(xml_bytes: bytes, feed_name: str) -> list[dict[str, str]]:
    root = ET.fromstring(xml_bytes)
    channel: ET.Element | None = None
    if local(root.tag) == "channel":
        channel = root
    else:
        for el in root.iter():
            if local(el.tag) == "channel":
                channel = el
                break
    if channel is None:
        return []

    items: list[dict[str, str]] = []
    for el in channel:
        if local(el.tag) != "item":
            continue
        title = child_text(el, "title")
        link = child_text(el, "link")
        guid = child_text(el, "guid") or link
        pub = child_text(el, "pubDate") or child_text(el, "date")
        desc = child_text(el, "description") or child_text(el, "summary")
        if not guid and not link:
            continue
        item_id = f"{feed_name}:{guid or link}"
        items.append(
            {
                "id": item_id,
                "feed": feed_name,
                "title": title or "(no title)",
                "link": link or guid,
                "published": parse_pub_date(pub),
                "summary": plain_summary(desc),
            }
        )
    return items


def load_state() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        return {"seeded": False, "seen": []}
    with STATE_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    if "seen" not in data:
        data["seen"] = []
    if "seeded" not in data:
        data["seeded"] = bool(data["seen"])
    return data


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    seen: list[str] = list(state.get("seen", []))
    if len(seen) > MAX_SEEN:
        seen = seen[-MAX_SEEN:]
    state["seen"] = seen
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def mark_github_env_state_changed() -> None:
    env_path = os.environ.get("GITHUB_ENV")
    if env_path:
        with open(env_path, "a", encoding="utf-8") as f:
            f.write("STATE_CHANGED=true\n")


def collect_feed_items() -> tuple[list[dict[str, str]], dict[str, str | int]]:
    all_items: list[dict[str, str]] = []
    stats: dict[str, str | int] = {}
    for spec in FEEDS:
        name = spec["feed"]
        try:
            body = fetch_bytes(spec["url"])
            if spec["type"] == "rss":
                parsed = parse_rss(body, name)
            else:
                parsed = []
            all_items.extend(parsed)
            stats[name] = len(parsed)
            log(f"feed {name}: {len(parsed)} items")
        except (urllib.error.URLError, urllib.error.HTTPError, ET.ParseError, TimeoutError) as exc:
            stats[name] = f"error: {exc}"
            log(f"feed {name}: failed ({exc})")
    return all_items, stats


def item_text(item: dict[str, str]) -> str:
    return f"{item.get('title', '')} {item.get('summary', '')}"


def should_forward(item: dict[str, str]) -> bool:
    if item["feed"] in ALWAYS_FORWARD_FEEDS:
        return True
    text = item_text(item)
    return any(rx.search(text) for rx in _ESPORTS_MARKET_RE)


def partition_new_items(
    items: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    forward: list[dict[str, str]] = []
    dropped: list[dict[str, str]] = []
    for it in items:
        if should_forward(it):
            forward.append(it)
        else:
            dropped.append(it)
    return forward, dropped


def mark_items_seen(state: dict[str, Any], ids: list[str]) -> None:
    seen = state.setdefault("seen", [])
    seen_set = set(seen)
    for item_id in ids:
        if item_id not in seen_set:
            seen.append(item_id)
            seen_set.add(item_id)


def report_filter_stats(items: list[dict[str, str]]) -> int:
    by_feed: dict[str, list[dict[str, str]]] = {}
    for it in items:
        by_feed.setdefault(it["feed"], []).append(it)

    log("Filter report (current feed snapshot, no state changes):")
    for spec in FEEDS:
        name = spec["feed"]
        feed_items = by_feed.get(name, [])
        if name in ALWAYS_FORWARD_FEEDS:
            passed = len(feed_items)
            dropped = 0
            passed_examples = [it["title"] for it in feed_items[:3]]
            dropped_examples: list[str] = []
        else:
            passed_items = [it for it in feed_items if should_forward(it)]
            dropped_items = [it for it in feed_items if not should_forward(it)]
            passed = len(passed_items)
            dropped = len(dropped_items)
            passed_examples = [it["title"] for it in passed_items[:3]]
            dropped_examples = [it["title"] for it in dropped_items[:3]]
        log(f"  {name}: {passed} pass, {dropped} drop (of {len(feed_items)} items)")
        if passed_examples:
            log("    pass examples: " + " | ".join(passed_examples))
        if dropped_examples:
            log("    drop examples: " + " | ".join(dropped_examples))
    return 0


def post_webhook(url: str, auth_header: str, items: list[dict[str, str]]) -> None:
    payload_items = [
        {
            "feed": it["feed"],
            "title": it["title"],
            "link": it["link"],
            "published": it["published"],
            "summary": it["summary"],
        }
        for it in items
    ]
    body = json.dumps({"source": "cs2-news-watcher", "items": payload_items}).encode("utf-8")
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Authorization": auth_header,
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        if resp.status >= 300:
            raise urllib.error.HTTPError(url, resp.status, "unexpected status", resp.headers, None)


def main() -> int:
    if "--filter-stats" in sys.argv:
        fetched, _stats = collect_feed_items()
        return report_filter_stats(fetched)

    state = load_state()
    seen_set = set(state.get("seen", []))

    fetched, _stats = collect_feed_items()
    # Stable order: newest-first per feed is typical in RSS; process in fetch order.
    new_items = [it for it in fetched if it["id"] not in seen_set]

    if not state.get("seeded"):
        log("First run: seeding state with current items (no webhook).")
        mark_items_seen(state, [it["id"] for it in fetched])
        state["seeded"] = True
        save_state(state)
        mark_github_env_state_changed()
        return 0

    if not new_items:
        log("No new items.")
        return 0

    to_forward, filtered_out = partition_new_items(new_items)
    if filtered_out:
        log(f"Filtered out {len(filtered_out)} esports item(s) (marked seen, no POST).")
        mark_items_seen(state, [it["id"] for it in filtered_out])

    if not to_forward:
        log("No new items to forward after filtering.")
        save_state(state)
        mark_github_env_state_changed()
        return 0

    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    webhook_auth = os.environ.get("WEBHOOK_AUTH", "").strip()
    if not webhook_url or not webhook_auth:
        log(
            "WEBHOOK_URL and/or WEBHOOK_AUTH are not set; skipping POST "
            f"({len(to_forward)} forward item(s) will retry next run)."
        )
        if filtered_out:
            save_state(state)
            mark_github_env_state_changed()
        return 0

    try:
        post_webhook(webhook_url, webhook_auth, to_forward)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        log(f"Webhook POST failed ({exc}); not marking forward item(s) as seen.")
        if filtered_out:
            save_state(state)
            mark_github_env_state_changed()
        return 0

    log(f"Webhook POST succeeded for {len(to_forward)} new item(s).")
    mark_items_seen(state, [it["id"] for it in to_forward])
    save_state(state)
    mark_github_env_state_changed()
    return 0


if __name__ == "__main__":
    sys.exit(main())
