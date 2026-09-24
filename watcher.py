"""Watches RSS feeds for each topic in topics.json, has Gemini score new items,
posts the important ones to that topic's Discord channel, and keeps a diary
of the day's stories for the daily digest."""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).parent
TOPICS = json.loads((ROOT / "topics.json").read_text())
STATE_FILE = ROOT / "seen.json"
DIARY_FILE = ROOT / "diary.json"

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")  # check AI Studio for current free models
MIN_SCORE = int(os.getenv("MIN_SCORE", "7"))         # stories at or above this ping you
DIARY_MIN_SCORE = int(os.getenv("DIARY_MIN_SCORE", "5"))  # stories at or above this go in the diary
MAX_ITEMS = 25
UA = "Mozilla/5.0 (compatible; news-watcher/1.0)"

PROMPT = """You are a news filter for a news page about: {focus}.
For each numbered item below, return ONLY a JSON list with one object per item:
{{"id": <number>, "score": 1-10, "category": "launch|update|rumor|business|legal|other",
 "summary": "one sentence", "verified": "official|reliable_reporter|unconfirmed",
 "headline": "a short headline for my page"}}

Scoring: 8-10 confirmed launches, major announcements, big business or legal news.
5-7 notable but routine. 1-4 opinion, deals, tips, fan chatter.
Mark leaks, unnamed sources and fan posts as "unconfirmed".

Items:
"""


def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def collect_new(topic, seen_ids):
    """Returns new items, mixed fairly so no single site fills the whole batch."""
    per_feed = []
    for feed in topic["feeds"]:
        parsed = feedparser.parse(feed["url"], agent=UA)
        if parsed.bozo and not parsed.entries:
            print(f"Could not read {feed['name']}", file=sys.stderr)
            continue
        group = []
        for e in parsed.entries:
            uid = e.get("id") or e.get("link")
            if uid and uid not in seen_ids:
                group.append({
                    "uid": uid,
                    "source": feed["name"],
                    "title": e.get("title", ""),
                    "summary": (e.get("summary", "") or "")[:400],
                    "link": e.get("link", ""),
                })
        per_feed.append(group)
    # take one from each site in turn
    return [it for row in zip_longest(*per_feed) for it in row if it]


def ask_gemini(topic, items):
    text = PROMPT.format(focus=topic["focus"]) + "\n".join(
        f"{i}. [{it['source']}] {it['title']} - {it['summary']}" for i, it in enumerate(items)
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    r = requests.post(url, params={"key": GEMINI_KEY}, json=body, timeout=90)
    r.raise_for_status()
    raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    raw = raw.strip().removeprefix("```json").removesuffix("```").strip()
    return json.loads(raw)


def post_discord(webhook, item, result):
    msg = (
        f"**[{result['score']}/10 | {result['category']}] {result['headline']}**\n"
        f"{result['summary']}\n"
        f"_{result['verified']} | {item['source']}_\n{item['link']}"
    )
    requests.post(webhook, json={"content": msg[:1990]}, timeout=30).raise_for_status()
    time.sleep(1)


def main():
    state = load_json(STATE_FILE, {"seeded": [], "ids": []})
    diary = load_json(DIARY_FILE, [])
    now = datetime.now(timezone.utc)
    failed = False

    for topic in TOPICS:
        webhook = os.getenv(topic["webhook_env"])
        if not webhook:
            print(f"Skipping {topic['name']}: secret {topic['webhook_env']} not set.")
            continue
        try:
            items = collect_new(topic, set(state["ids"]))

            # First time we see a topic: remember what exists, don't flood Discord.
            if topic["name"] not in state["seeded"]:
                state["ids"] += [i["uid"] for i in items]
                state["seeded"].append(topic["name"])
                print(f"{topic['name']}: first run, marked {len(items)} items as seen.")
                continue

            batch = items[:MAX_ITEMS]
            new_diary = []
            if batch:
                for res in ask_gemini(topic, batch):
                    idx = res.get("id")
                    if not (isinstance(idx, int) and 0 <= idx < len(batch)):
                        continue
                    score = res.get("score", 0)
                    if score >= DIARY_MIN_SCORE:
                        new_diary.append({
                            "date": now.isoformat(),
                            "topic": topic["name"],
                            "score": score,
                            "category": res.get("category", "other"),
                            "headline": res.get("headline", batch[idx]["title"]),
                            "summary": res.get("summary", ""),
                            "verified": res.get("verified", "unconfirmed"),
                            "source": batch[idx]["source"],
                            "link": batch[idx]["link"],
                        })
                    if score >= MIN_SCORE:
                        post_discord(webhook, batch[idx], res)
                state["ids"] += [i["uid"] for i in batch]
                diary += new_diary
            print(f"{topic['name']}: checked {len(batch)} new items, diary +{len(new_diary)}.")
        except Exception as exc:  # one broken topic shouldn't stop the others
            print(f"{topic['name']} failed: {exc}", file=sys.stderr)
            failed = True

    cutoff = now - timedelta(days=3)
    diary = [d for d in diary if datetime.fromisoformat(d["date"]) > cutoff]
    state["ids"] = state["ids"][-5000:]
    STATE_FILE.write_text(json.dumps(state))
    DIARY_FILE.write_text(json.dumps(diary, indent=1))
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
