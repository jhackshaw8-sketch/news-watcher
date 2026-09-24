"""Reads the diary, asks Gemini for a recap and tweet drafts per topic,
and posts them to the digest Discord channel."""
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

PROMPT = """You write the daily recap for a news page about: {topic}.
Below are today's stories (score = importance 1-10, verified = how solid the sourcing is).
Return ONLY JSON: {{"recap": "3 to 6 short bullet lines, each starting with '- ', most important first",
 "tweets": ["draft 1", "draft 2"]}}

Tweet rules: each under 270 characters, punchy and engaging, plain language, at most one hashtag,
no links, and never invent facts beyond the stories below. If a story is unconfirmed or a rumor,
say so clearly (for example "Rumor:" or "Reportedly") instead of stating it as fact.

Stories:
"""


def ask_gemini(topic, stories):
    text = PROMPT.format(topic=topic) + "\n".join(
        f"- [{s['score']}/10, {s['verified']}, {s['source']}] {s['headline']}: {s['summary']}"
        for s in stories
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
        tweets = "\n".join(f"{i}) {t}" for i, t in enumerate(out.get("tweets", []), 1))
        top = stories[0]
        msg = (
            f"**{topic} - Daily Digest** ({len(stories)} stories)\n{out.get('recap', '')}\n\n"
            f"**Tweet drafts:**\n{tweets}\n\n"
            f"Top story link: {top['link']}"
        )
        requests.post(WEBHOOK, json={"content": msg[:1990]}, timeout=30).raise_for_status()
        print(f"Posted digest for {topic}.")
        time.sleep(1)


if __name__ == "__main__":
    main()
