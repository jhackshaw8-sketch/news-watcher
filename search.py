"""Checks the Discord search channel for new messages.
- If the message contains a link (e.g. you copied one of the bot's own posts),
  it fetches and reads the FULL article and writes an engaging post/thread from it.
- Otherwise it treats the message as a keyword: first checks the diary already
  gathered by watcher.py (Apple/Gaming/Tech), and if nothing matches, falls back
  to asking Gemini to search the open web."""
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
import trafilatura

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "search_state.json"
DIARY_FILE = ROOT / "diary.json"

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID = os.environ["SEARCH_CHANNEL_ID"]
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
MAX_PER_RUN = 5
MAX_MATCHES = 8
ARTICLE_CHAR_LIMIT = 8000
UA = "Mozilla/5.0 (compatible; news-watcher/1.0)"

API = "https://discord.com/api/v10"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
HEADERS = {
    "Authorization": f"Bot {BOT_TOKEN}",
    "User-Agent": "DiscordBot (https://github.com, 1.0)",
    "Content-Type": "application/json",
}
URL_RE = re.compile(r"https?://\S+")

ARTICLE_PROMPT = """Below is the full text of a news article. Read it carefully, then return ONLY JSON:
{{"recap": "3 to 5 short bullet lines covering the actual details in the article (names, numbers,
 dates, quotes if notable), most important first",
 "posts": [{{"type": "single" or "thread", "tweets": ["..."]}}]}}

Write {n} different posts/threads, each a different angle on THIS article (e.g. the headline
fact, a surprising detail, the reaction or what's next). For a simple angle write ONE punchy
tweet ("single"). For an angle with several things worth covering, write a thread of 2 to 4
tweets ("thread"), each starting with its number like "1/3".
Rules: each tweet under 250 characters, no links in the tweet text, plain engaging language,
hook the reader in the first line, at most one hashtag per post. If the article says something
is unconfirmed or a rumor, say so ("Rumor:", "Reportedly"). Use only what the article actually
says, never invent details.

Article:
{article}
"""

DIARY_PROMPT = """A user searched for: "{keyword}". Below are numbered stories already
gathered from tracked news sources that match. Return ONLY JSON:
{{"recap": "2 to 4 short bullet lines summarizing what these stories say, most important first",
 "posts": [{{"story_id": <number>, "type": "single" or "thread", "tweets": ["..."]}}]}}

Write up to {n} posts, each about a DIFFERENT story from the list (do not repeat the same
story). For a simple story write ONE punchy tweet ("single"). If a story has several things
worth covering, write a thread of 2 to 4 tweets ("thread"), each starting with its number
like "1/3". Rules: each tweet under 250 characters, no links in the tweet text, plain
engaging language, at most one hashtag per post. If a story is marked unconfirmed, say so
("Rumor:", "Reportedly"). Use only the stories given, never invent facts.

Stories:
{listing}
"""

WEB_PROMPT = """Search the web for the latest news (from the last few days) about: "{keyword}".
Then reply in EXACTLY this plain-text format (no JSON, no extra commentary):

RECAP:
- 3 to 5 short bullet lines on what is happening, most important first

POST 1:
(one punchy tweet, OR a thread of 2 to 4 tweets, each starting with its number like 1/3,
if the story has several things worth covering)
Link: (the full, real, working article URL you found for this story, starting with https://)

POST 2:
(same idea, a different story or angle)
Link: (the full, real, working article URL for this different story)

POST 3:
(same idea, a different story or angle)
Link: (the full, real, working article URL for this different story)

Rules: each tweet under 250 characters, no links inside the tweet text itself (only after
"Link:"), plain engaging language, at most one hashtag per post. Say clearly when something
is a rumor or unconfirmed ("Rumor:", "Reportedly"). Use only what you actually found and
never invent details or URLs; if you cannot find a real link for a post, write "Link: none"."""


# ---------- Discord ----------
def get_messages(last_id):
    params = {"limit": 20} if last_id else {"limit": 10}
    if last_id:
        params["after"] = last_id
    r = requests.get(f"{API}/channels/{CHANNEL_ID}/messages", headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return sorted(r.json(), key=lambda m: int(m["id"]))


def reply(text, message_id):
    body = {"content": text[:1990], "message_reference": {"message_id": message_id}}
    r = requests.post(f"{API}/channels/{CHANNEL_ID}/messages", headers=HEADERS, json=body, timeout=30)
    r.raise_for_status()
    time.sleep(1)


def call_gemini_json(prompt, use_search=False):
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if use_search:
        body["tools"] = [{"google_search": {}}]
    else:
        body["generationConfig"] = {"responseMimeType": "application/json", "maxOutputTokens": 4096}
    r = requests.post(GEMINI_URL, params={"key": GEMINI_KEY}, json=body, timeout=120)
    r.raise_for_status()
    cand = r.json()["candidates"][0]
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", [])).strip()
    if not use_search:
        text = text.removeprefix("```json").removesuffix("```").strip()
        return json.loads(text)
    return text


def post_drafts(items, mid, header):
    reply(header, mid)
    for i, item in enumerate(items, 1):
        tweets = item.get("tweets") or []
        if not tweets:
            continue
        label = "Thread" if item.get("type") == "thread" and len(tweets) > 1 else "Post"
        link = item.get("link", "")
        msg = f"**{label} {i}:**\n" + "\n\n".join(tweets) + (f"\n{link}" if link else "")
        reply(msg, mid)


# ---------- Mode A: a link was pasted, read the full article ----------
def fetch_article(url):
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
        downloaded = r.text
    text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
    return (text or "")[:ARTICLE_CHAR_LIMIT]


def handle_article(url, mid):
    article = fetch_article(url)
    if not article or len(article) < 200:
        reply(
            f"Couldn't read the full article at that link (it may block automated reading). "
            f"Try pasting just the headline as a keyword instead, or a different link.\n{url}",
            mid,
        )
        return
    out = call_gemini_json(ARTICLE_PROMPT.format(n=3, article=article))
    for item in out.get("posts", []):
        item["link"] = url
    post_drafts(out.get("posts", []), mid, f"**From article:** {url}\n{out.get('recap', '')}")


# ---------- Mode B: plain keyword ----------
def find_in_diary(keyword):
    if not DIARY_FILE.exists():
        return []
    diary = json.loads(DIARY_FILE.read_text())
    kw = keyword.lower()
    matches = [
        d for d in diary
        if kw in d.get("headline", "").lower()
        or kw in d.get("summary", "").lower()
        or kw in d.get("topic", "").lower()
        or kw in d.get("category", "").lower()
    ]
    matches.sort(key=lambda d: d["score"], reverse=True)
    return matches[:MAX_MATCHES]


def handle_web_fallback(keyword, mid):
    note = ""
    try:
        text = call_gemini_json(WEB_PROMPT.format(keyword=keyword), use_search=True)
    except Exception as exc:
        print(f"Web search tool failed ({exc}); asking without live search.", file=sys.stderr)
        text = call_gemini_json(WEB_PROMPT.format(keyword=keyword), use_search=False)
        note = "\n_(Live web search was unavailable, so this may be out of date and have no link.)_"

    parts = [p.strip() for p in re.split(r"\n(?=POST \d)", text) if p.strip()]
    if not parts:
        reply(f"Nothing found for **{keyword}**, in your tracked news or the open web. Try different words.", mid)
        return
    intro = (
        f"**Search: {keyword}**\n_(Nothing in your tracked Apple/Gaming/Tech stories matched, "
        f"so this is from an open web search instead.)_{note}\n{parts[0]}"
    )
    reply(intro, mid)
    for p in parts[1:]:
        p = re.sub(r"^POST (\d):", r"**Post \1:**", p)
        p = re.sub(r"^Link:\s*none\s*$", "_(no link found for this one)_", p, flags=re.MULTILINE | re.IGNORECASE)
        p = re.sub(r"^Link:\s*", "", p, flags=re.MULTILINE)
        reply(p, mid)


def handle_keyword(keyword, mid):
    matches = find_in_diary(keyword)
    if not matches:
        handle_web_fallback(keyword, mid)
        return
    out = call_gemini_json(DIARY_PROMPT.format(
        keyword=keyword, n=min(3, len(matches)),
        listing="\n".join(
            f"{i}. [{m['topic']}, {m['score']}/10, {m['verified']}] {m['headline']}: {m['summary']}"
            for i, m in enumerate(matches)
        ),
    ))
    posts = []
    for post in out.get("posts", []):
        idx = post.get("story_id")
        if isinstance(idx, int) and 0 <= idx < len(matches):
            post["link"] = matches[idx]["link"]
            posts.append(post)
    post_drafts(posts, mid, f"**Search: {keyword}** ({len(matches)} tracked stories found)\n{out.get('recap', '')}")


# ---------- Dispatch ----------
def handle(msg):
    content = msg["content"].strip()
    mid = msg["id"]
    url_match = URL_RE.search(content)
    if url_match:
        handle_article(url_match.group(0).rstrip(">).,"), mid)
    else:
        handle_keyword(content[:100], mid)


def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    last_id = state.get("last_id")
    msgs = get_messages(last_id)
    if not msgs:
        print("No new messages.")
        return

    human = [m for m in msgs if not (m.get("author") or {}).get("bot") and m.get("content", "").strip()]
    todo, leftover = human[:MAX_PER_RUN], human[MAX_PER_RUN:]

    for m in todo:
        try:
            handle(m)
            print(f"Answered: {m['content'][:60]}")
        except Exception as exc:
            print(f"Search failed for {m['content'][:60]!r}: {exc}", file=sys.stderr)
            try:
                reply("Sorry, that one failed. Please try again in a few minutes.", m["id"])
            except Exception:
                pass

    new_last = int(todo[-1]["id"]) if leftover else int(msgs[-1]["id"])
    STATE_FILE.write_text(json.dumps({"last_id": str(new_last)}))


if __name__ == "__main__":
    main()
