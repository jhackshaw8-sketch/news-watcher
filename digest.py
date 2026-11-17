"""Reads the diary, asks Gemini to pick 3 stand-out stories per topic, write a
catchy headline for each, and write either a single tweet or a short thread in the
page's house style, then posts them to the digest Discord channel, each ending
with that story's own link so Discord shows its image. Each topic is handled on its
own, so one topic failing never blocks the others."""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from gemini_client import generate

ROOT = Path(__file__).parent
DIARY_FILE = ROOT / "diary.json"

WEBHOOK = os.environ["DISCORD_WEBHOOK_DIGEST"]
HOURS = int(os.getenv("DIGEST_HOURS", "24"))
MAX_STORIES = 40
POSTS_PER_TOPIC = 3
MAX_THREAD_TWEETS = 4

STYLE = """HOUSE STYLE for every post (match it closely; it is the voice of a popular news page on X):
- Start with 1 or 2 attention emojis that fit the story: 🚨 for breaking news, rumors and big reveals; a topic emoji (🎮 🎬 🚗 📱 🏆) and/or 🔥 for hype.
- Then ONE ALL-CAPS hook sentence that states the news, ending in ! (example: FABLE IS SHOWING OFF ITS COMBAT!).
- Then 1 to 2 plain sentences with the key facts (names, numbers, dates). Use "reportedly" or "could be" for anything unconfirmed.
- Then a short line aimed at fans: either a question that invites replies (UNITED FANS, WOULD YOU TAKE HIM?) or an excited reaction in caps (WE JUST WANT THE GAME!).
- Finish with 1 emoji, then 2 to 3 relevant hashtags.
- Keep each post under 270 characters. No links inside the post text.
- EXCEPTION: for stories about deaths, crimes, injuries or tragedies, stay sober: a single 🚨, no jokes, no hype, no fan question, just the facts and 1 to 2 hashtags.

THREADS (only when a story has several distinct parts): 2 to 4 tweets, numbered 1/4, 2/4 and so on. Tweet 1 is the hook in the house style above. Middle tweets each carry one detail. If the story is unconfirmed, one tweet must say so plainly. The LAST tweet asks fans a question, then 1 emoji and 2 to 3 hashtags. Each tweet stays under 270 characters.

Examples of the voice:
🚨 POCHETTINO COULD BE HEADING TO MANCHESTER UNITED! Mauricio Pochettino is reportedly ready to accept an offer to become United's next head coach. UNITED FANS, WOULD YOU TAKE HIM? OR IS IT TIME TO LOOK ELSEWHERE? 👀 #MUFC #ManchesterUnited #Pochettino
🎮🔥 FABLE IS SHOWING OFF ITS COMBAT! Xbox just dropped four minutes of fresh gameplay, giving us a closer look at the upcoming RPG. Albion is looking GOOD! 👀 #Fable #Xbox
🔥 BETHESDA TELLS ELDER SCROLLS FANS TO CALM DOWN! No major Skyrim anniversary reveal, and Elder Scrolls VI news is still a long way off. WE JUST WANT THE GAME! 😤 #ElderScrollsVI #Skyrim"""

PROMPT = """You write daily X (Twitter) posts for a page about: {topic}.
Below is a numbered list of today's stories (score = importance 1-10, verified = how solid the sourcing is).

Pick the {n} stand-out stories (favor a mix of different stories, not near-duplicates).
For EACH story:
1. Write a short, catchy, engaging headline (your own hook, not the original title, under 12 words). It is only a label for the Discord message, not part of the tweet.
2. Decide: ONE tweet ("single") for a simple update, or a thread ("thread") of 2 to {max_thread} tweets when the story has several distinct parts.

{style}

Return ONLY JSON:
{{"posts": [{{"story_id": <number>, "headline": "...", "type": "single" or "thread", "tweets": ["...", "..."]}}]}}
("tweets" has exactly 1 item for "single", 2 to {max_thread} items for "thread")

If a story is unconfirmed or a rumor, say so clearly ("reportedly", "could be") instead of stating it as fact. Never invent facts beyond the story given.

Stories:
"""


def ask_gemini(topic, stories):
    listing = "\n".join(
        f"{i}. [{s['score']}/10, {s['verified']}, {s['source']}] {s['headline']}: {s['summary']}"
        for i, s in enumerate(stories)
    )
    text = PROMPT.format(
        topic=topic, n=min(POSTS_PER_TOPIC, len(stories)),
        max_thread=MAX_THREAD_TWEETS, style=STYLE,
    ) + listing
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 8192},
    }
    data = generate(body)  # tries backup models if the main one is overloaded
    raw = data["candidates"][0]["content"]["parts"][0]["text"]
    raw = raw.strip().removeprefix("```json").removesuffix("```").strip()
    return json.loads(raw)


def post_topic(topic, stories):
    stories.sort(key=lambda s: s["score"], reverse=True)
    stories = stories[:MAX_STORIES]
    out = ask_gemini(topic, stories)  # asked first, so a failure never leaves a lonely header

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
        # this real article link is what makes Discord show a picture; repeating it on
        # every tweet of a thread gives each one its own matching picture
        link = stories[idx]["link"]
        label = "Thread" if post.get("type") == "thread" and len(tweets) > 1 else "Post"
        headline = post.get("headline", "").strip()
        title_line = f"**{label} {i}: {headline}**" if headline else f"**{label} {i}:**"
        for j, tweet in enumerate(tweets):
            prefix = f"{title_line}\n" if j == 0 else ""
            msg = f"{prefix}{tweet}\n{link}"
            requests.post(WEBHOOK, json={"content": msg[:1990]}, timeout=30).raise_for_status()
            time.sleep(1)
        posted += 1
    print(f"Posted {posted} drafts for {topic}.")


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

    failed = []
    for topic, stories in by_topic.items():
        try:
            post_topic(topic, stories)
        except Exception as exc:  # one topic failing must not block the others
            print(f"{topic} failed: {exc}", file=sys.stderr)
            failed.append(topic)

    if failed:
        print(f"Digest incomplete. Missing: {', '.join(failed)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
