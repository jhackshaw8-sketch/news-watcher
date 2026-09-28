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
{{"headline": "a short, catchy, engaging headline for this story (not the original title, your own hook, under 12 words)",
 "recap": "3 to 5 short bullet lines covering the actual details in the article (names, numbers,
 dates, quotes if notable), most important first",
 "posts": [{{"type": "single" or "thread", "tweets": ["..."]}}],
 "longform_post": "ONE alternative post in a longer style, described below"}}

For "posts", write {n} different short posts/threads, each a different angle on THIS article
(e.g. the headline fact, a surprising detail, the reaction or what's next). For a simple angle
write ONE punchy tweet ("single"). For an angle with several things worth covering, write a
thread of 2 to 4 tweets ("thread"), each starting with its number like "1/3". Rules: each tweet
under 250 characters, no links in the tweet text, plain engaging language, hook the reader in
the first line. ALWAYS end the tweet (or the last tweet of a thread) with 1 to 2 relevant
hashtags that would help people discover the post.

For "longform_post", write ONE extra post, a different style: a short punchy hook sentence on
its own line, then a blank line, then exactly ONE short paragraph (1 to 2 sentences, the single
most important detail only) in plain conversational language, then a blank line and 1 to 2
relevant hashtags. Keep it under 350 characters total, tight and scannable, not a full recap.

If the article says something is unconfirmed or a rumor, say so ("Rumor:", "Reportedly"). Use
only what the article actually says, never invent details.

Article:
{article}
"""

DIARY_PROMPT = """A user searched for: "{keyword}". Below are numbered stories already
gathered from tracked news sources that match. Return ONLY JSON:
{{"headline": "a short, catchy, engaging headline covering these stories (under 12 words)",
 "recap": "2 to 4 short bullet lines summarizing what these stories say, most important first",
 "posts": [{{"story_id": <number>, "type": "single" or "thread", "tweets": ["..."]}}],
 "longform_post": {{"story_id": <number>, "text": "ONE alternative post in a longer style, described below"}}}}

For "posts", write up to {n} short posts/threads, each about a DIFFERENT story from the list
(do not repeat the same story). For a simple story write ONE punchy tweet ("single"). If a
story has several things worth covering, write a thread of 2 to 4 tweets ("thread"), each
starting with its number like "1/3". Rules: each tweet under 250 characters, no links in the
tweet text, plain engaging language. ALWAYS end each tweet (or the last tweet of a thread) with
1 to 2 relevant hashtags that would help people discover the post.

For "longform_post", pick the single best story from the list and write ONE extra post in a
different style: a short punchy hook sentence on its own line, then a blank line, then exactly
ONE short paragraph (1 to 2 sentences, the single most important detail only), then a blank
line and 1 to 2 relevant hashtags. Keep it under 350 characters total, tight and scannable.

If a story is marked unconfirmed, say so ("Rumor:", "Reportedly"). Use only the stories given,
never invent facts.

Stories:
{listing}
"""

WEB_PROMPT = """Search the web for the latest news (from the last few days) about: "{keyword}".
Then reply in EXACTLY this plain-text format (no JSON, no extra commentary):

TITLE:
(a short, catchy, engaging headline for this news, under 12 words)

RECAP:
- 3 to 5 short bullet lines on what is happening, most important first

POST 1:
(one punchy tweet, OR a thread of 2 to 4 tweets, each starting with its number like 1/3,
if the story has several things worth covering. End with 1 to 2 relevant hashtags.)
Link: (the full, real, working article URL you found for this story, starting with https://)

POST 2:
(same idea, a different story or angle)
Link: (the full, real, working article URL for this different story)

POST 3:
(same idea, a different story or angle)
Link: (the full, real, working article URL for this different story)

LONGFORM:
(ONE extra post, a different style, about your favorite story above: a short punchy hook
sentence on its own line, then a blank line, then exactly ONE short paragraph (1 to 2
sentences, the single most important detail only), then a blank line and 1 to 2 relevant
hashtags. Keep the whole post under 350 characters, tight and scannable, not a full recap.)
Link: (the same article URL used above for this story)

Rules: each short tweet under 250 characters, no links inside any post text itself (only after
"Link:"). Say clearly when something is a rumor or unconfirmed ("Rumor:", "Reportedly"). Use
only what you actually found and never invent details or URLs; if you cannot find a real link
for a post, write "Link: none"."""


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
    headline = out.get("headline", "Story breakdown")
    post_drafts(out.get("posts", []), mid, f"**{headline}**\n{out.get('recap', '')}\n{url}")

    longform = out.get("longform_post", "")
    if longform:
        reply(f"**Alt style (long-form):**\n{longform}\n{url}", mid)


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

    title_match = re.search(r"TITLE:\s*\n?(.+)", text)
    headline = title_match.group(1).strip() if title_match else f"Search: {keyword}"
    text = re.sub(r"TITLE:\s*\n?.+\n+", "", text, count=1)

    # split off the LONGFORM section (comes after the numbered POSTs) so it doesn't get
    # treated as just another POST block
    longform_block = ""
    lf_split = re.split(r"\n(?=LONGFORM:)", text)
    text = lf_split[0]
    if len(lf_split) > 1:
        longform_block = lf_split[1]

    parts = [p.strip() for p in re.split(r"\n(?=POST \d)", text) if p.strip()]
    if not parts:
        reply(f"Nothing found for **{keyword}**, in your tracked news or the open web. Try different words.", mid)
        return
    intro = (
        f"**{headline}**\n_(Nothing in your tracked Apple/Gaming/Tech stories matched, "
        f"so this is from an open web search instead.)_{note}\n{parts[0]}"
    )
    reply(intro, mid)
    for p in parts[1:]:
        p = re.sub(r"^POST (\d):", r"**Post \1:**", p)
        p = re.sub(r"^Link:\s*none\s*$", "_(no link found for this one)_", p, flags=re.MULTILINE | re.IGNORECASE)
        p = re.sub(r"^Link:\s*", "", p, flags=re.MULTILINE)
        reply(p, mid)

    if longform_block.strip():
        lf = re.sub(r"^LONGFORM:\s*\n?", "", longform_block.strip())
        lf = re.sub(r"^Link:\s*none\s*$", "_(no link found for this one)_", lf, flags=re.MULTILINE | re.IGNORECASE)
        lf = re.sub(r"^Link:\s*", "", lf, flags=re.MULTILINE)
        reply(f"**Alt style (long-form):**\n{lf}", mid)


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
    headline = out.get("headline", f"Search: {keyword}")
    post_drafts(posts, mid, f"**{headline}** ({len(matches)} tracked stories found)\n{out.get('recap', '')}")

    lf = out.get("longform_post") or {}
    lf_idx, lf_text = lf.get("story_id"), lf.get("text")
    if lf_text and isinstance(lf_idx, int) and 0 <= lf_idx < len(matches):
        reply(f"**Alt style (long-form):**\n{lf_text}\n{matches[lf_idx]['link']}", mid)


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
