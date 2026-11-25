"""Flask app: serves index.html and the two API endpoints.

Run locally:   python app/server.py          (Flask's own server, development only)
On Render:     gunicorn --chdir app server:app --bind 0.0.0.0:$PORT \
                   --workers 1 --threads 8 --timeout 600

One worker because the caches and the rate-limit counters live in this process's memory and
Render's free instance has 512 MB; threads because every request spends its time waiting on
the proxy, not on the CPU.

The LiteLLM key stays here and never reaches the browser.

Four bugs in the team's earlier version are fixed here, and the Failure Journal entries are:

  S1  the validation branch returned `queries` before it was assigned, so 0 or 6 claims
      raised NameError and the page got an HTML 500 instead of a Czech 400.
  S2  the success path returned no `ranking`; it had ended up in the dead error branch. So
      index.html fell back to its own aggregate(), which has no contradiction rule and
      counts coverage differently. Users were not seeing the evaluated ranking rule. This
      is a bug fix, not a change of method.
  S3  TAU, PROMPT_VERSION and rank_parties existed only after a notebook cell injected
      them, so the file could not run or be tested on its own. It now imports pipeline.
  S4  debug=True, which must never run on a public host.
"""

from __future__ import annotations

import hmac
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

import pipeline

HERE = Path(__file__).resolve().parent

MAX_CHARS, MAX_CLAIMS = 500, 5          # same limits the page enforces in the browser
RATE_LIMIT, RATE_WINDOW = 3, 60         # analyses per IP per minute
DAILY_JUDGE_CAP = 2000                  # prompts sent to the proxy per UTC day
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")   # empty = no password (local dev)
REALM = 'Basic realm="Volebni kalkulacka Praha 2026", charset="UTF-8"'

app = Flask(__name__)
_lock = threading.Lock()
_hits: dict[str, deque] = {}                        # ip -> timestamps of recent analyses
_day = {"date": None, "baseline": 0}                # for the daily judge-call cap

# Load the committed caches at import, which is once per gunicorn worker. Render's filesystem
# is ephemeral and a free service spins down after 15 idle minutes, so without this the first
# analysis after a quiet spell pays the spin-up and a full set of live judge calls. A no-op
# when the files are absent, so the app still starts before the notebook has been exported.
pipeline.warm_start()


# ------------------------------------------------------------------ password

@app.before_request
def require_password():
    """Shared password for the whole site, until the teacher agrees to public use.

    /api/health stays open: Render's health check cannot log in, and it reveals only the
    party names, tau and the model, which are in the report anyway.
    """
    if not APP_PASSWORD or request.path == "/api/health":
        return None
    auth = request.authorization
    # compare_digest on bytes, so a non-ASCII password cannot raise, and the comparison
    # takes the same time whatever the guess: no character-by-character leak.
    if auth and auth.password and hmac.compare_digest(
        auth.password.encode("utf-8"), APP_PASSWORD.encode("utf-8")
    ):
        return None
    return Response("Tato stránka je chráněna heslem.", 401, {"WWW-Authenticate": REALM})


# ------------------------------------------------------------------ limits

def client_ip() -> str:
    """Render puts the real address first in X-Forwarded-For.

    Only trustworthy because the app is always behind Render's proxy, which overwrites the
    header. Running this open to the internet without such a proxy would let anyone forge it.
    """
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() or (request.remote_addr or "unknown")


def too_many_requests(ip: str) -> bool:
    now = time.time()
    with _lock:
        dq = _hits.setdefault(ip, deque())
        while dq and now - dq[0] > RATE_WINDOW:
            dq.popleft()
        if len(dq) >= RATE_LIMIT:
            return True
        dq.append(now)
        return False


def judge_calls_left() -> int:
    """Remaining prompts for today. Resets on the UTC date, and on any restart."""
    today = datetime.now(timezone.utc).date().isoformat()
    with _lock:
        if _day["date"] != today:
            _day["date"] = today
            _day["baseline"] = pipeline.STATS["judge_calls"]
        used = pipeline.STATS["judge_calls"] - _day["baseline"]
        return max(0, DAILY_JUDGE_CAP - used)


def bad_request(message: str, status: int = 400):
    """The page shows `error` verbatim, so every message here is Czech."""
    return jsonify(error=message), status


# ------------------------------------------------------------------ routes

@app.get("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.get("/api/health")
def health():
    """Never calls the model. Always answers 200 so Render's health check stays green.

    `ok: false` means the exported data is missing, and the page then runs in demo mode
    with fictional parties instead of showing an error. That is the better failure.
    """
    try:
        c = pipeline.config()
    except pipeline.DataMissing as e:
        # The reason names a filesystem path, and this endpoint is deliberately open, so
        # the detail goes to the log and the caller gets a generic Czech sentence.
        app.logger.warning("health: data missing: %s", e)
        return jsonify(ok=False, error="Data aplikace nejsou nahraná.")
    return jsonify(
        ok=True,
        model=c["llm_model"],
        parties=c["parties"],
        tau=c["tau"],
        prompt_version=c["prompt_version"],
    )


@app.post("/api/analyze")
def analyze():
    payload = request.get_json(silent=True) or {}
    raw = payload.get("claims", [])
    if not isinstance(raw, list):
        return bad_request("Pole „claims“ musí být seznam tvrzení.")
    claims = [str(c).strip() for c in raw if str(c).strip()]

    # S1: validate BEFORE anything is computed, and answer 400 with a Czech message.
    if not claims:
        return bad_request("Zadejte alespoň jedno tvrzení.")
    if len(claims) > MAX_CLAIMS:
        return bad_request(f"Najednou lze poslat nejvýše {MAX_CLAIMS} tvrzení.")
    if len(" / ".join(claims)) > MAX_CHARS:
        return bad_request(f"Tvrzení jsou dohromady delší než {MAX_CHARS} znaků.")

    if too_many_requests(client_ip()):
        return bad_request(
            f"Příliš mnoho dotazů. Zkuste to znovu za {RATE_WINDOW} sekund.", 429)

    try:
        cfg = pipeline.config()
        need = len(claims) * cfg["k_stance"] * len(cfg["parties"])
    except pipeline.DataMissing as e:
        app.logger.warning("analyze: data missing: %s", e)
        return bad_request("Data aplikace nejsou nahraná.", 500)
    if judge_calls_left() < need:
        return bad_request(
            "Denní limit dotazů na model je vyčerpán. Zkuste to prosím zítra.", 429)

    try:
        queries = []
        for claim in claims:
            rows = pipeline.score_claim(claim)
            for r in rows:
                r["score"] = max(-1.0, min(1.0, float(r.get("score", 0))))  # never trust the range
            rows.sort(key=lambda r: r["score"], reverse=True)
            queries.append({"claim": claim, "results": rows})
        # S2: the ranking belongs in the success response. Without it index.html falls back
        # to its own aggregate(), which has no contradiction rule.
        return jsonify(queries=queries, ranking=pipeline.rank_parties(queries))
    except pipeline.DataMissing as e:
        app.logger.warning("analyze: data missing: %s", e)
        return bad_request("Data aplikace nejsou nahraná.", 500)
    except RuntimeError as e:
        return bad_request(str(e), 500)       # already Czech, raised by pipeline.score_claim
    except Exception:                         # noqa: BLE001 - never leak a stack trace
        app.logger.exception("analyze failed")
        return bad_request("Při zpracování došlo k chybě. Zkuste to prosím znovu.", 500)


if __name__ == "__main__":
    # S4: debug=False. Flask's own server is for local development only; Render uses gunicorn.
    app.run(host="127.0.0.1", port=5000, debug=False)
