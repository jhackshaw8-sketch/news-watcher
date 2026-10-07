"""Writes a batch of short, funny, relatable draft tweets in a loose internet-voice
style and posts them to the viral-tweets Discord channel. Some posts are standalone,
some are loosely inspired by recent stories from the other tracked topics, and at
most one "engagement question" post (inviting replies) goes out per day, no matter
how many times this runs. Most posts stay plain text; one random post per batch
gets a real reaction GIF attached for variety."""
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
DIARY_FILE = ROOT / "diary.json"
STATE_FILE = ROOT / "viral_state.json"

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
WEBHOOK = os.environ["DISCORD_WEBHOOK_VIRAL"]
KLIPY_KEY = os.getenv("KLIPY_API_KEY", "")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
HEADLINE_SAMPLE = 6

PROMPT = """You write short, funny, relatable tweets in a loose, chaotic, internet-voice
style, like a popular meme/viral-tweet page: punchy one-liners, exaggerated reactions,
playful trash talk, pop-culture references. Casual grammar, lowercase, and run-on energy
is fine and often funnier than "proper" writing.

HARD RULE: never use slurs, sexual content, or needlessly explicit/vulgar language. Keep
it genuinely funny and relatable through the observation or reaction itself, not through
shock value or crude language. No politics, no punching down at real people.

Write exactly this set of posts:
{spec}

For each "oneliner" or "reaction" post, also give "gif_keyword": one or two plain English
words describing the REACTION/EMOTION the post captures (e.g. "shocked", "done", "crying
laughing", "nervous", "excited", "facepalm") so a matching reaction GIF can be attached.
Leave "gif_keyword" out for "question" posts.

Return ONLY JSON: {{"posts": [{{"type": "oneliner" or "reaction" or "question", "text": "...", "gif_keyword": "..."}}]}}
Each post under 270 characters, no hashtags needed, no links.
"""


def build_spec(include_question, headlines):
    lines = [
        '- 2 to 3 "oneliner" posts: standalone funny/relatable observations about '
        "everyday life, internet culture, or general chaos. Not tied to any specific news."
    ]
    if headlines:
        sample = "; ".join(headlines)
        lines.append(
            f'- 1 to 2 "reaction" posts, each a funny/chaotic reaction to ONE of these current '
            f"topics (pick whichever is funniest, don't force all of them in): {sample}"
        )
    if include_question:
        lines.append(
            '- exactly 1 "question" post: a short, engaging question inviting people to reply '
            "(like asking for picks, opinions, or hot takes on something currently relevant)"
        )
    return "\n".join(lines)


def recent_headlines():
    if not DIARY_FILE.exists():
        return []
    diary = json.loads(DIARY_FILE.read_text())
    cutoff = datetime.now(timezone.utc) - timedelta(hours=36)
    recent = [d["headline"] for d in diary if datetime.fromisoformat(d["date"]) > cutoff]
    random.shuffle(recent)
    return recent[:HEADLINE_SAMPLE]


def fetch_reaction_gif(keyword):
    """Searches Klipy (Discord's own GIF provider, since Tenor's API shut down in
    2026) for a real reaction GIF matching the keyword. Returns a URL, or None."""
    if not KLIPY_KEY or not keyword:
        return None
    try:
        r = requests.get(
            f"https://api.klipy.com/api/v1/{KLIPY_KEY}/gifs/search",
            params={"q": keyword, "customer_id": "viral-tweets-bot", "per_page": 5},
            timeout=15,
        )
        r.raise_for_status()
        results = (r.json().get("data") or {}).get("data") or []
        if not results:
            return None
        file = results[0].get("file", {})
        for size in ("sm", "xs", "hd", "md"):
            url = (file.get(size) or {}).get("gif", {}).get("url")
            if url:
                return url
    except Exception as exc:
        print(f"GIF fetch failed for '{keyword}': {exc}")
    return None


def ask_gemini(spec):
    n = spec.count("\n- ") + 1
    body = {
        "contents": [{"parts": [{"text": PROMPT.format(spec=spec, n=n)}]}],
        "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 2048},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    r = requests.post(url, params={"key": GEMINI_KEY}, json=body, timeout=90)
    r.raise_for_status()
    raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    raw = raw.strip().removeprefix("```json").removesuffix("```").strip()
    return json.loads(raw)


def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    today = datetime.now(timezone.utc).date().isoformat()
    include_question = state.get("last_question_date") != today

    headlines = recent_headlines()
    spec = build_spec(include_question, headlines)
    out = ask_gemini(spec)

    posts = out.get("posts", [])
    # most posts stay plain text like before; only ONE random post per batch
    # (if any are eligible) gets a reaction GIF attached, for variety
    eligible = [i for i, p in enumerate(posts) if p.get("type") in ("oneliner", "reaction")]
    gif_index = random.choice(eligible) if eligible else None

    posted_question = False
    for i, post in enumerate(posts):
        text = (post.get("text") or "").strip()
        if not text:
            continue
        if post.get("type") == "question":
            posted_question = True

        msg = text
        if i == gif_index:
            gif_url = fetch_reaction_gif(post.get("gif_keyword", ""))
            if gif_url:
                msg = f"{text}\n{gif_url}"

        requests.post(WEBHOOK, json={"content": msg[:1990]}, timeout=30).raise_for_status()
        time.sleep(1)

    if posted_question:
        state["last_question_date"] = today
        STATE_FILE.write_text(json.dumps(state))

    print(f"Posted {len(out.get('posts', []))} drafts. Question included: {posted_question}")


if __name__ == "__main__":
    main()
