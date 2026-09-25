"""Checks the Discord search channel for new keywords. For each one it asks Gemini to
search Google and write a recap plus 3 post/thread drafts, then replies in the channel."""
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "search_state.json"

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID = os.environ["SEARCH_CHANNEL_ID"]
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
MAX_PER_RUN = 5       # keywords handled per run
MAX_SOURCES = 5

API = "https://discord.com/api/v10"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
UA = "Mozilla/5.0 (compatible; news-watcher/1.0)"
HEADERS = {
    "Authorization": f"Bot {BOT_TOKEN}",
    "User-Agent": "DiscordBot (https://github.com, 1.0)",
    "Content-Type": "application/json",
}

PROMPT = """Search the web for the latest news (from the last few days) about: "{keyword}".
Then reply in EXACTLY this plain-text format (no JSON, no extra commentary):

RECAP:
- 3 to 5 short bullet lines on what is happening, most important first

POST 1:
(one punchy tweet, OR a thread of 2 to 4 tweets, each starting with its number like 1/3,
if the story has several things worth covering)
Source: (site name)

POST 2:
(same idea, a different story or angle)
Source: (site name)

POST 3:
(same idea, a different story or angle)
Source: (site name)

Rules: each tweet under 250 characters, no links, plain engaging language, at most one hashtag
per post. Say clearly when something is a rumor or unconfirmed ("Rumor:", "Reportedly").
Use only what you actually found and never invent details."""


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


# ---------- Gemini ----------
def resolve_link(url):
    """Follow Google's redirect so the link points at the real article."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=10, allow_redirects=True, stream=True)
        final = r.url
        r.close()
        return url if "google.com" in urlparse(final).netloc else final
    except Exception:
        return url


def ask_gemini(keyword, use_search=True):
    body = {"contents": [{"parts": [{"text": PROMPT.format(keyword=keyword)}]}]}
    if use_search:
        body["tools"] = [{"google_search": {}}]
    r = requests.post(GEMINI_URL, params={"key": GEMINI_KEY}, json=body, timeout=120)
    r.raise_for_status()
    cand = r.json()["candidates"][0]
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", [])).strip()
    chunks = (cand.get("groundingMetadata") or {}).get("groundingChunks") or []
    sources = []
    for c in chunks[:MAX_SOURCES]:
        web = c.get("web")
        if web and web.get("uri"):
            sources.append((web.get("title", "source"), resolve_link(web["uri"])))
    return text, sources


# ---------- One keyword ----------
def handle(msg):
    keyword = msg["content"].strip()[:100]
    mid = msg["id"]

    note = ""
    try:
        text, sources = ask_gemini(keyword, use_search=True)
    except Exception as exc:  # Google Search tool may be unavailable on the free tier
        print(f"Search tool failed ({exc}); asking without live search.", file=sys.stderr)
        text, sources = ask_gemini(keyword, use_search=False)
        note = "\n_(Live web search was unavailable, so this may be out of date. Double-check before posting.)_"

    parts = [p.strip() for p in re.split(r"\n(?=POST \d)", text) if p.strip()]
    if not parts:
        reply(f"No results for **{keyword}**. Try different words.", mid)
        return
    reply(f"**Search: {keyword}**{note}\n{parts[0]}", mid)

    used_links = set()
    for p in parts[1:]:
        p = re.sub(r"^POST (\d):", r"**Post \1:**", p)
        # try to match this post's "Source: X" line to one of the real links we found
        m = re.search(r"Source:\s*(.+)", p)
        link = ""
        if m and sources:
            wanted = m.group(1).strip().lower()
            for title, url in sources:
                if wanted in title.lower() or title.lower() in wanted:
                    link = url
                    used_links.add(url)
                    break
        if link:
            p = f"{p}\n{link}"
        reply(p, mid)

    leftover = [(t, u) for t, u in sources if u not in used_links]
    if leftover:
        reply("**More sources:**\n" + "\n".join(f"- {t}: <{u}>" for t, u in leftover), mid)


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
            print(f"Answered: {m['content'][:50]}")
        except Exception as exc:
            print(f"Search failed for {m['content'][:50]!r}: {exc}", file=sys.stderr)
            try:
                reply("Sorry, that search failed. Please try again in a few minutes.", m["id"])
            except Exception:
                pass

    new_last = int(todo[-1]["id"]) if leftover else int(msgs[-1]["id"])
    STATE_FILE.write_text(json.dumps({"last_id": str(new_last)}))


if __name__ == "__main__":
    main()
