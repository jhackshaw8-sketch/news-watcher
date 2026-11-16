"""Checks the Discord search channel for new messages.
- A message with a link: fetches and reads the FULL article, then writes posts from it.
- A long message with no link (pasted story text): writes posts from that text.
- A short message with no link: treated as a keyword (searches your tracked stories,
  then falls back to the open web).
Add a line like "angle: make it shocked" to any message to steer the posts."""
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
PASTED_TEXT_MIN = 150  # a message this long with no link is treated as pasted story text
UA = "Mozilla/5.0 (compatible; news-watcher/1.0)"

API = "https://discord.com/api/v10"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
HEADERS = {
    "Authorization": f"Bot {BOT_TOKEN}",
    "User-Agent": "DiscordBot (https://github.com, 1.0)",
    "Content-Type": "application/json",
}
URL_RE = re.compile(r"https?://\S+")
ANGLE_RE = re.compile(r"^[ \t]*(?:angle|note)[ \t]*:[ \t]*(.+?)[ \t]*$", re.IGNORECASE | re.MULTILINE)

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

ARTICLE_PROMPT = """Below is the full text of a news story. Read it carefully, then return ONLY JSON:
{{"headline": "a short, catchy, engaging headline for this story (not the original title, your own hook, under 12 words)",
 "recap": "3 to 5 short bullet lines covering the actual details in the story (names, numbers,
 dates, quotes if notable), most important first",
 "posts": [{{"type": "single" or "thread", "tweets": ["..."]}}],
 "longform_post": "ONE alternative post in a longer style, described below"}}

For "posts", write {n} different posts, each a different angle on THIS story (e.g. the headline
fact, a surprising detail, the reaction or what's next). Each post is ONE tweet ("single") or,
when an angle has several distinct parts, a thread ("thread"). "tweets" has exactly 1 item for
"single" and 2 to 4 items for "thread".

{style}

{angle_line}

For "longform_post", write ONE extra post, a different style: a short punchy hook sentence on
its own line, then a blank line, then exactly ONE short paragraph (1 to 2 sentences, the single
most important detail only) in plain conversational language, then a blank line and 1 to 2
relevant hashtags. Keep it under 350 characters total, tight and scannable, not a full recap.

If the story says something is unconfirmed or a rumor, say so ("Rumor:", "Reportedly"). Use
only what the story actually says, never invent details.

Story:
{article}
"""

DIARY_PROMPT = """A user searched for: "{keyword}". Below are numbered stories already
gathered from tracked news sources that match. Return ONLY JSON:
{{"headline": "a short, catchy, engaging headline covering these stories (under 12 words)",
 "recap": "2 to 4 short bullet lines summarizing what these stories say, most important first",
 "posts": [{{"story_id": <number>, "type": "single" or "thread", "tweets": ["..."]}}],
 "longform_post": {{"story_id": <number>, "text": "ONE alternative post in a longer style, described below"}}}}

For "posts", write up to {n} posts, each about a DIFFERENT story from the list (do not repeat
the same story). Each post is ONE tweet ("single") or, when a story has several distinct
parts, a thread ("thread"). "tweets" has exactly 1 item for "single" and 2 to 4 items for
"thread".

{style}

{angle_line}

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
{angle_line}
Then reply in EXACTLY this plain-text format (no JSON, no extra commentary):

TITLE:
(a short, catchy, engaging headline for this news, under 12 words)

RECAP:
- 3 to 5 short bullet lines on what is happening, most important first

POST 1:
(a post in the HOUSE STYLE described below: ONE tweet, or a thread of 2 to 4 tweets numbered
1/3, 2/3, 3/3 when the story has several distinct parts)
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

{style}

Rules: no links inside any post text itself (only after "Link:"). Say clearly when something
is a rumor or unconfirmed ("Rumor:", "Reportedly"). Use only what you actually found and
never invent details or URLs; if you cannot find a real link for a post, write "Link: none"."""


# ---------- Discord ----------
def get_messages(last_id):
    """Reads the NEWEST 50 messages in the channel (so recent pastes are never missed,
    even when the bot's own multi-message replies pile up), then keeps only the ones
    newer than the bookmark."""
    r = requests.get(
        f"{API}/channels/{CHANNEL_ID}/messages",
        headers=HEADERS, params={"limit": 50}, timeout=30,
    )
    r.raise_for_status()
    msgs = sorted(r.json(), key=lambda m: int(m["id"]))
    if last_id:
        msgs = [m for m in msgs if int(m["id"]) > int(last_id)]
    else:
        msgs = msgs[-10:]
    return msgs


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
    # Google is sometimes too busy (503/429): wait and retry instead of failing
    for attempt in range(1, 4):
        r = requests.post(GEMINI_URL, params={"key": GEMINI_KEY}, json=body, timeout=120)
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            print(f"Gemini busy ({r.status_code}); retry {attempt}/2", file=sys.stderr)
            time.sleep(8 * attempt)
            continue
        r.raise_for_status()
        break
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


def angle_line(angle):
    return f'The user wants this angle/emphasis, so follow it: "{angle}"' if angle else ""


def parse_message(content):
    """Splits a message into (main text, angle). An 'angle:' or 'note:' line is an
    instruction for how to write the posts, not part of the story."""
    angle = ""
    m = ANGLE_RE.search(content)
    if m:
        angle = m.group(1).strip()
        content = (content[:m.start()] + content[m.end():]).strip()
    return content, angle


# ---------- Mode A: a link or pasted story text ----------
def fetch_article(url):
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
            r.raise_for_status()
            downloaded = r.text
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        return (text or "")[:ARTICLE_CHAR_LIMIT]
    except Exception as exc:
        print(f"Could not read article {url}: {exc}", file=sys.stderr)
        return ""


def write_from_text(article, link, mid, angle, note=""):
    out = call_gemini_json(ARTICLE_PROMPT.format(
        n=3, article=article, style=STYLE, angle_line=angle_line(angle),
    ))
    for item in out.get("posts", []):
        item["link"] = link
    headline = out.get("headline", "Story breakdown")
    header = f"**{headline}**\n{note}{out.get('recap', '')}" + (f"\n{link}" if link else "")
    post_drafts(out.get("posts", []), mid, header)

    longform = out.get("longform_post", "")
    if longform:
        reply(f"**Alt style (long-form):**\n{longform}" + (f"\n{link}" if link else ""), mid)


def handle_article(url, other_text, mid, angle):
    article = fetch_article(url)
    if article and len(article) >= 200:
        write_from_text(article, url, mid, angle)
    elif len(other_text) >= 80:
        # the site blocks robots (Reddit does), so use the text that came with the link
        write_from_text(
            other_text, url, mid, angle,
            note="_(Couldn't read the full article, so this is based on the text you pasted.)_\n",
        )
    else:
        reply(
            "Couldn't read the full article at that link (it may block automated reading). "
            f"Paste the story text instead, or try a different link.\n{url}",
            mid,
        )


def handle_pasted_text(text, mid, angle):
    write_from_text(text[:ARTICLE_CHAR_LIMIT], "", mid, angle)


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


def handle_web_fallback(keyword, mid, angle):
    note = ""
    prompt = WEB_PROMPT.format(keyword=keyword, style=STYLE, angle_line=angle_line(angle))
    try:
        text = call_gemini_json(prompt, use_search=True)
    except Exception as exc:
        print(f"Web search tool failed ({exc}); asking without live search.", file=sys.stderr)
        text = call_gemini_json(prompt, use_search=False)
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
        f"**{headline}**\n_(Nothing in your tracked stories matched, "
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


def handle_keyword(keyword, mid, angle):
    matches = find_in_diary(keyword)
    if not matches:
        handle_web_fallback(keyword, mid, angle)
        return
    out = call_gemini_json(DIARY_PROMPT.format(
        keyword=keyword, n=min(3, len(matches)), style=STYLE, angle_line=angle_line(angle),
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
    content, angle = parse_message(msg["content"].strip())
    mid = msg["id"]
    if not content:
        reply("Paste a link, the story text, or a keyword. You can add a line like `angle: make it shocked`.", mid)
        return
    url_match = URL_RE.search(content)
    if url_match:
        url = url_match.group(0).rstrip(">).,")
        other_text = URL_RE.sub("", content).strip()
        handle_article(url, other_text, mid, angle)
    elif len(content) >= PASTED_TEXT_MIN:
        handle_pasted_text(content, mid, angle)
    else:
        handle_keyword(content[:100], mid, angle)


def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    last_id = state.get("last_id")
    msgs = get_messages(last_id)
    print(f"Bookmark: {last_id}. New messages since then: {len(msgs)}.")
    if not msgs:
        print("No new messages.")
        return

    from_people = [m for m in msgs if not (m.get("author") or {}).get("bot")]
    human = [m for m in from_people if m.get("content", "").strip()]
    print(f"{len(from_people)} from people, {len(human)} with readable text.")
    if len(from_people) > len(human):
        print(
            "WARNING: some messages from people had NO readable text. That means they were "
            "forwarded messages/images only, or the bot's Message Content Intent is off.",
            file=sys.stderr,
        )
    todo, leftover = human[:MAX_PER_RUN], human[MAX_PER_RUN:]

    for m in todo:
        try:
            handle(m)
            print(f"Answered: {m['content'][:60]}")
        except Exception as exc:
            print(f"Search failed for {m['content'][:60]!r}: {exc}", file=sys.stderr)
            try:
                reply("Sorry, that one failed. Please try again in a few minutes.", m["id"])
            except Exception as exc2:
                print(f"Could not even post the failure notice: {exc2}", file=sys.stderr)

    new_last = int(todo[-1]["id"]) if leftover else int(msgs[-1]["id"])
    STATE_FILE.write_text(json.dumps({"last_id": str(new_last)}))


if __name__ == "__main__":
    main()
