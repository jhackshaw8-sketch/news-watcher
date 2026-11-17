"""Shared helper for calling Gemini.

Tries a list of models in order. If a model is overloaded (503), over its limit, or
not available to your key, it moves on to the next one instead of failing. A model
that fails during a run is skipped for the rest of that run, so one bad stretch
doesn't slow down every topic. To change the order, edit DEFAULT_MODELS below
(or set a GEMINI_MODELS variable, comma-separated)."""
import os
import sys
import time

import requests

KEY = os.environ["GEMINI_API_KEY"]
DEFAULT_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash"]
MODELS = [m.strip() for m in os.getenv("GEMINI_MODELS", ",".join(DEFAULT_MODELS)).split(",") if m.strip()]

TRANSIENT = {429, 500, 502, 503, 504}  # worth one retry on the same model
OUR_FAULT = {400, 401}                 # a bad request or key: no other model will fix it

_skip = set()      # models that already failed during this run
_last_error = None


def generate(body, timeout=90):
    """Sends `body` to Gemini and returns the parsed JSON response.
    Raises the last error only if every model fails."""
    global _last_error
    for model in MODELS:
        if model in _skip:
            continue
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in (1, 2):
            try:
                r = requests.post(url, params={"key": KEY}, json=body, timeout=timeout)
            except requests.RequestException as exc:  # network hiccup
                _last_error = exc
                print(f"Gemini {model}: network problem (attempt {attempt}/2)", file=sys.stderr)
                time.sleep(3)
                continue
            if r.status_code == 200:
                return r.json()
            _last_error = requests.HTTPError(f"{r.status_code} from {model}: {r.text[:200]}", response=r)
            print(f"Gemini {model}: {r.status_code} (attempt {attempt}/2)", file=sys.stderr)
            if r.status_code in OUR_FAULT:
                raise _last_error
            if r.status_code in TRANSIENT and attempt == 1:
                time.sleep(5)
                continue
            break
        _skip.add(model)
        print(f"Switching away from {model} for the rest of this run.", file=sys.stderr)
    raise _last_error or RuntimeError("No Gemini model is available right now.")
