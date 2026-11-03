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
    state = load_state()
    seen_set = set(state.get("seen", []))

    fetched, _stats = collect_feed_items()
    # Stable order: newest-first per feed is typical in RSS; process in fetch order.
    new_items = [it for it in fetched if it["id"] not in seen_set]

    if not state.get("seeded"):
        log("First run: seeding state with current items (no webhook).")
        for it in fetched:
            if it["id"] not in seen_set:
                state.setdefault("seen", []).append(it["id"])
                seen_set.add(it["id"])
        state["seeded"] = True
        save_state(state)
        mark_github_env_state_changed()
        return 0

    if not new_items:
        log("No new items.")
        return 0

    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    webhook_auth = os.environ.get("WEBHOOK_AUTH", "").strip()
    if not webhook_url or not webhook_auth:
        log(
            "WEBHOOK_URL and/or WEBHOOK_AUTH are not set; skipping POST "
            f"({len(new_items)} new item(s) will retry next run)."
        )
        return 0

    try:
        post_webhook(webhook_url, webhook_auth, new_items)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        log(f"Webhook POST failed ({exc}); not marking items as seen.")
        return 0

    log(f"Webhook POST succeeded for {len(new_items)} new item(s).")
    for it in new_items:
        state.setdefault("seen", []).append(it["id"])
    save_state(state)
    mark_github_env_state_changed()
    return 0


if __name__ == "__main__":
    sys.exit(main())
