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
 "headline": "a short headline for my page (add 1-2 fun relevant emojis if the topic is Cars; no emojis
