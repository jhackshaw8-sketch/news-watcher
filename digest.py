"""Reads the diary, asks Gemini to pick 3 stand-out stories per topic and write
either a single tweet or a short thread for each (its choice, based on how
much there is to say), then posts them to the digest Discord channel, each
ending with that story's own link so Discord shows its image."""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
DIARY_FILE = ROOT / "diary.json"

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
WEBHOOK = os.environ["DISCORD_WEBHOOK_DIGEST"]
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
HOURS = int(os.getenv("DIGEST_HOURS", "24"))
MAX_STORIES = 40
POSTS_PER_TOPIC = 3
MAX_THREAD_TWEETS = 4

PROMPT = """You write daily X (Twitter) posts for a page about: {topic}.
Below is a numbered list of today's stories (score = importance 1-10, verified = how solid the sourcing is).

Pick the {n} stand-out stories (favor a mix of different stories, not near-duplicates).
For EACH story, decide:
- If it's a simple, single-fact update -> write ONE punchy tweet.
- If it has multiple things worth covering (what happened, why it matters, reactions,
  what's next, background) -> write a THREAD of 2 to {max_thread} short tweets that build
  on each other, most important fact first. Start each thread tweet with its number,
  like "1/3", "2/3".

Return ONLY JSON:
{{"posts": [{{"story_id": <number>, "type": "single" or "thread", "tweets": ["...", "..."]}}]}}
("tweets" has exactly 1 item for "single", 2 to {max_thread} items for "thread")

Tweet rules: each tweet under 250 characters (a link is added after the last one, leave room),
no links inside the tweet text itself, plain engaging language, at most one hashtag total per post.
If a story is unconfirmed or a rumor, say so clearly (e.g. "Rumor:" or "Reportedly") instead of
stating it as fact. Never invent facts beyond the story given.

Stories:
"""


def ask_gemini(topic, stories):
    listing = "\n".join(
        f"{i}. [{s['score']}/10, {s['verified']}, {s['source']}] {s['headline']}: {s['summary']}"
        for i, s in enumerate(stories)
    )
    text = PROMPT.format(
        topic=topic, n=min(POSTS_PER_TOPIC, len(stories)), max_thread=MAX_THREAD_TWEETS
    ) + listing
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


def main():
    diary = json.loads(DIARY_FILE.read_text()) if DIARY_FILE.exists() else []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS)
    recent = [d for d in diary if datetime.fromisoformat(d["date"]) > cutoff]

    by_topic = {}
    for d in recent:
        by_topic.setdefault(d["topic"], []).append(d)

    if not by_topic:
        print("Quiet day: nothing in the diary.")
        return

    for topic, stories in by_topic.items():
        stories.sort(key=lambda s: s["score"], reverse=True)
        stories = stories[:MAX_STORIES]
        out = ask_gemini(topic, stories)

        header = f"**{topic} - Daily Digest** ({len(stories)} stories today)"
        requests.post(WEBHOOK, json={"content": header}, timeout=30).raise_for_status()
        time.sleep(1)

        posted = 0
        for i, post in enumerate(out.get("posts", []), 1):
            idx = post.get("story_id")
            if not (isinstance(idx, int) and 0 <= idx < len(stories)):
                continue
            tweets = post.get("tweets") or []
            if not tweets:
                continue
            link = stories[idx]["link"]
            label = "Thread" if post.get("type") == "thread" and len(tweets) > 1 else "Post"
            body_text = "\n\n".join(tweets)
            msg = f"**{label} {i}:**\n{body_text}\n{link}"
            requests.post(WEBHOOK, json={"content": msg[:1990]}, timeout=30).raise_for_status()
            posted += 1
            time.sleep(1)

        print(f"Posted {posted} drafts for {topic}.")


if __name__ == "__main__":
    main()
