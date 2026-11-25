"""Retrieval + LLM judge for the Praha 2026 voting advice tool.

This is the notebook's method, moved into a module the web app can import. Nothing here
decides anything: the model, params, prompt text, K and tau all come from
``app/data/app_config.json``, which the notebook's ``E0`` cell writes. That is the single
source of truth, so the app and the research notebook cannot drift apart.

Which notebook cell each part comes from:
  B2  per_party            top-K per party, stable sort
  B3  dense_scores, INSTR  query prefix, L2 norm, cosine against E
  C2  make_prompt, norm, parse, SCALE, CONF
  C6  passage_score, claim_score
  D1  ask_msgs, judge_claim, score_claim, rank_parties

No pandas: ``csv`` and ``numpy`` are enough and leave more of Render's 512 MB for the
embedding matrix. Data is loaded on first use, not at import time, so the pure functions
can be tested without any data present and a cold start pays only for what it uses.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

DATA_DIR = Path(__file__).resolve().parent / "data"
log = logging.getLogger(__name__)

# Anchored scales from C2. They belong to prompt v3 and must match the prompt text that
# app_config.json carries; the parity test is what proves they still agree with it.
SCALE = (-1.0, -0.5, 0.0, 0.5, 1.0)
CONF = (0.25, 0.5, 0.75, 1.0)
LANG = "cs"
WORKERS = 10  # parallel judge calls; lower it if the proxy answers 429
FIX_CS = (
    "Tvoje odpověď nebyla platná ({err}). Odpověz znovu pouze jedním objektem JSON "
    "podle pokynů; citace musí být doslovně zkopírovaná z úryvku."
)

# ---------------------------------------------------------------- lazy state

_config = None
_corpus = None
_client = None
_warmed = False
_lock = threading.Lock()

QUERY_CACHE: dict[str, np.ndarray] = {}  # sha1 -> query vector
JUDGE_CACHE: dict[str, dict] = {}  # sha1 -> judge answer
LAST_ERROR = {"msg": ""}  # last LLM error, shown by /api/health and the page

# Counters the server uses for its daily cap. judge_calls counts prompts actually sent to
# the proxy, including a re-ask and each failed retry, so the cap limits real usage rather
# than requests. In-process only: a restart resets them, which Render may do at any time.
STATS = {"judge_calls": 0, "cache_hits": 0, "embed_calls": 0}


class DataMissing(RuntimeError):
    """app/data has not been exported from the notebook yet."""


def config() -> dict:
    """The frozen config written by the notebook's E0 cell."""
    global _config
    if _config is None:
        path = DATA_DIR / "app_config.json"
        if not path.exists():
            raise DataMissing(f"{path} is missing; run the notebook's E0 export cell")
        _config = json.loads(path.read_text(encoding="utf-8"))
    return _config


def corpus() -> dict:
    """Passage table and embedding matrix, in the same row order."""
    global _corpus
    if _corpus is None:
        csv_path, emb_path = DATA_DIR / "passages.csv", DATA_DIR / "passage_emb.npy"
        for p in (csv_path, emb_path):
            if not p.exists():
                raise DataMissing(f"{p} is missing; run the notebook's E0 export cell")
        ids, parties, pages, texts = [], [], [], []
        with open(csv_path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                ids.append(row["passage_id"])
                parties.append(row["party"])
                # empty means the source had no page (TXT/JSON), matching pd.isna upstream
                pages.append(int(row["page"]) if row["page"] not in ("", None) else None)
                texts.append(row["text"])
        emb = np.load(emb_path)
        if emb.shape[0] != len(ids):
            raise DataMissing(f"{emb.shape[0]} vectors but {len(ids)} passages: re-export")
        _corpus = {
            "passage_id": ids,
            "party": np.array(parties),
            "page": dict(zip(ids, pages)),
            "text": dict(zip(ids, texts)),
            "E": emb,
        }
    return _corpus


def client():
    """OpenAI-compatible client for the VŠE LiteLLM proxy. Built on first use."""
    global _client
    if _client is None:
        from openai import OpenAI  # imported here so tests need no network stack

        key = os.environ.get("LITELLM_API_KEY", "")
        if not key:
            raise RuntimeError("LITELLM_API_KEY is not set")
        base = os.environ.get("LITELLM_BASE_URL", "https://litellm.vse.cz/v1")
        _client = OpenAI(base_url=base, api_key=key, timeout=180)
    return _client


def warm_start(force: bool = False) -> dict:
    """Fill the caches from the files the notebook exported, if they are present.

    Render's filesystem is ephemeral and a free service spins down after 15 idle minutes, so
    both caches are empty again on every restart, redeploy and spin-down. Without this, the
    first analysis of a session pays the spin-up *and* a full set of live judge calls — the
    worst possible moment being a demo.

    Two properties worth knowing:

    * **A stale file cannot give a wrong answer.** Every key is a hash of the configuration,
      so if the model, prompt version, parameters or instruction prefix have changed, the
      keys simply will not match and the app calls the model as usual. The file becomes
      useless, never misleading.
    * **A corrupt file cannot take the app down.** The app is perfectly usable with cold
      caches, so a damaged file is logged and skipped rather than raised.

    Existing entries always win, so a test that has injected its own answers is never
    overwritten. Returns what it loaded, for the caller to log.
    """
    global _warmed
    with _lock:
        if _warmed and not force:
            return {"queries": 0, "judgements": 0, "skipped": "already warmed"}
        _warmed = True

    out = {"queries": 0, "judgements": 0}

    qpath = DATA_DIR / "query_cache.json"
    if qpath.exists():
        try:
            raw = json.loads(qpath.read_text(encoding="utf-8"))
            for key, vec in raw.items():
                # cast back to float32: the notebook's vectors are float32, and JSON wrote
                # their exact value, so this round trip changes nothing
                QUERY_CACHE.setdefault(key, np.asarray(vec, dtype=np.float32))
            out["queries"] = len(raw)
        except (OSError, ValueError, TypeError) as e:
            log.warning("warm start: ignoring %s: %s", qpath.name, e)

    jpath = DATA_DIR / "app_stance_cache.jsonl"
    if jpath.exists():
        try:
            with open(jpath, encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError as e:
                        log.warning("warm start: %s line %d: %s", jpath.name, n, e)
                        continue
                    if isinstance(rec, dict) and rec.get("key"):
                        JUDGE_CACHE.setdefault(rec["key"], rec)
                        out["judgements"] += 1
        except OSError as e:
            log.warning("warm start: ignoring %s: %s", jpath.name, e)

    log.info("warm start: %d query vectors, %d judge answers",
             out["queries"], out["judgements"])
    return out


# ---------------------------------------------------------------- B3: retrieval


def l2norm(a: np.ndarray) -> np.ndarray:
    """Row-wise unit length, so a dot product is a cosine."""
    return a / np.maximum(np.linalg.norm(a, axis=-1, keepdims=True), 1e-12)


def query_key(claim: str) -> str:
    """B3's cache key. Same string as the notebook, so a cached vector is reused."""
    c = config()
    return hashlib.sha1(
        f"{c['embed_model']}|{c['instr']}|{claim}".encode("utf-8")
    ).hexdigest()


def embed_query(claim: str) -> np.ndarray:
    key = query_key(claim)
    if key not in QUERY_CACHE:
        c = config()
        STATS["embed_calls"] += 1
        r = client().embeddings.create(
            model=c["embed_model"], input=[c["instr"] + claim]
        )
        QUERY_CACHE[key] = l2norm(
            np.array(r.data[0].embedding, dtype=np.float32)
        )
    return QUERY_CACHE[key]


def dense_scores(claim: str) -> np.ndarray:
    return corpus()["E"] @ embed_query(claim)


# ---------------------------------------------------------------- B2: top-K per party


def per_party(scores: np.ndarray, k: int) -> list[dict]:
    """Top-k per party, never a global top-k: a long programme would crowd out short ones.

    Stable sort and sorted party order, so the row order matches the notebook exactly.
    """
    cor = corpus()
    party, ids = cor["party"], cor["passage_id"]
    rows = []
    for name in sorted(set(party.tolist())):
        idx = np.where(party == name)[0]
        top = idx[np.argsort(-scores[idx], kind="stable")[:k]]
        rows += [
            {
                "party": name,
                "passage_id": ids[i],
                "rank_in_party": r + 1,
                "score": float(scores[i]),
            }
            for r, i in enumerate(top)
        ]
    return rows


# ---------------------------------------------------------------- C2: prompt and parser


def make_prompt(claim: str, passage: str, template: str | None = None) -> str:
    text = config()["prompt_cs"] if template is None else template
    return text.replace("<<QUERY>>", claim).replace("<<PASSAGE>>", passage)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def parse(raw: str, passage: str) -> tuple[dict | None, str]:
    """Strict parser. Returns (answer, "") or (None, reason). Never raises on bad output."""
    txt = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S)
    objs = re.findall(r"\{[^{}]*\}", txt)
    if not objs:
        return None, "no JSON object"
    try:
        d = json.loads(objs[-1])
    except json.JSONDecodeError as e:
        return None, f"bad JSON: {e}"
    if not isinstance(d.get("relevant"), bool):
        return None, f"bad 'relevant': {d}"

    st = d.get("stance")
    if d["relevant"]:
        try:
            st = float(st)
        except (TypeError, ValueError):
            return None, f"relevant but no numeric stance: {d}"
        if st not in SCALE:
            return None, f"stance not on the scale: {d}"
    elif st not in (None, "null", ""):
        return None, f"not relevant but stance given: {d}"
    else:
        st = None

    c = d.get("confidence")  # same rule as stance: on the scale when relevant, else null
    if d["relevant"]:
        try:
            c = float(c)
        except (TypeError, ValueError):
            return None, f"relevant but no numeric confidence: {d}"
        if c not in CONF:
            return None, f"confidence not on the scale: {d}"
    else:
        c = None

    d["confidence"], d["stance"] = c, st
    d["quote"] = str(d.get("quote") or "")
    d["quote_ok"] = (not d["relevant"]) or (
        len(d["quote"]) > 0 and norm(d["quote"]) in norm(passage)
    )
    return d, ""


# ---------------------------------------------------------------- C6: scoring


def passage_score(r: dict, tau: float = 0.0, use_conf: bool = True):
    """None = NO POSITION. Otherwise s = c * stance (c = 1 without confidence)."""
    if r.get("relevant") is not True or not r.get("quote_ok"):
        return None
    c = r["confidence"] if use_conf else 1.0
    if c is None or c < tau:
        return None
    return c * r["stance"]


def claim_score(scores: list[float]) -> tuple[float, str]:
    """One party's kept scores for one claim -> (score, flag). Opposite-sign ties give 0."""
    if not scores:
        return 0.0, "none"  # no kept passage: NO POSITION
    m = max(abs(s) for s in scores)
    signs = {np.sign(s) for s in scores if abs(s) == m}  # signs among the strongest
    if 1 in signs and -1 in signs:
        return 0.0, "contradiction"
    return max(scores, key=abs), "ok"


# ---------------------------------------------------------------- D1: judge


def judge_key(claim: str, pid: str) -> str:
    """D1's cache key. Any change to model, prompt version or params changes it."""
    c = config()
    return hashlib.sha1(
        json.dumps(
            [c["llm_model"], c["prompt_version"], LANG, claim, pid, c["llm_params"]],
            sort_keys=True,
        ).encode()
    ).hexdigest()


def ask_msgs(msgs: list[dict]) -> str | None:
    """3 tries, then None. The error is recorded, never hidden."""
    c = config()
    for t in range(3):
        try:
            STATS["judge_calls"] += 1
            r = client().chat.completions.create(
                model=c["llm_model"], messages=msgs, **c["llm_params"]
            )
            return r.choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001 - any failure is retried then reported
            LAST_ERROR["msg"] = f"{type(e).__name__}: {str(e)[:200]}"
            time.sleep(5 * (t + 1))
    return None


def judge_claim(claim: str, pid: str) -> dict:
    """One passage judged. Cached; one re-ask with the reason, exactly as C4/D1."""
    key = judge_key(claim, pid)
    if key in JUDGE_CACHE:
        STATS["cache_hits"] += 1
        return JUDGE_CACHE[key]

    passage = corpus()["text"][pid]
    msgs = [{"role": "user", "content": make_prompt(claim, passage)}]
    d, err = None, ""
    for _ in (1, 2):
        raw = ask_msgs(msgs)
        if raw is None:
            return {"relevant": None, "net_error": True}  # not cached
        d, err = parse(raw, passage)
        if d is not None and d["quote_ok"]:
            break
        msgs += [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": FIX_CS.format(err=err or "quote not verbatim")},
        ]

    rec = {
        "key": key,
        "claim": claim,
        "passage_id": pid,
        "relevant": None if d is None else d["relevant"],
        "stance": None if d is None else d["stance"],
        "confidence": None if d is None else d["confidence"],
        "quote": "" if d is None else d["quote"],
        "quote_ok": bool(d) and d["quote_ok"],
    }
    with _lock:
        JUDGE_CACHE[key] = rec
    return rec


def score_claim(claim: str) -> list[dict]:
    """All parties' kept passages for one claim, in per_party order."""
    claim = claim.strip()
    c = config()
    hits = per_party(dense_scores(claim), k=c["k_stance"])
    with ThreadPoolExecutor(WORKERS) as pool:
        recs = list(pool.map(lambda h: judge_claim(claim, h["passage_id"]), hits))

    failed = sum(bool(r.get("net_error")) for r in recs)
    if failed == len(recs):  # nothing reached the model: say so in Czech, in the page
        raise RuntimeError(
            "Model LLM není dostupný (" + LAST_ERROR["msg"] + "). Zkuste to znovu později."
        )

    page = corpus()["page"]
    rows = []
    for h, r in zip(hits, recs):
        s = passage_score(r, c["tau"])
        if s is None:
            continue  # NO POSITION: not sent, counts as 0
        rows.append(
            {
                "party": h["party"],
                "quote": r["quote"],
                "page": page[h["passage_id"]],
                "score": round(s, 3),
                "confidence": r["confidence"],
            }
        )
    return rows


def rank_parties(queries: list[dict], parties: list[str] | None = None) -> list[dict]:
    """Formula (3) with the neutral tie rule, server side.

    index.html must use this instead of its own aggregate(), which has no contradiction
    rule. Coverage counts documented claims, contradictions included.
    """
    if parties is None:
        parties = config()["parties"]
    out = []
    for p in parties:
        per, flags = [], []
        for q in queries:
            s, flag = claim_score([r["score"] for r in q["results"] if r["party"] == p])
            per.append(s)
            flags.append(flag)
        out.append(
            {
                "party": p,
                "score": sum(per) / len(per),
                "perClaim": per,
                "flags": flags,
                "coverage": sum(f != "none" for f in flags),
            }
        )
    return sorted(out, key=lambda r: (-r["score"], -r["coverage"], r["party"]))
