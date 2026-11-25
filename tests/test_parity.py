"""Parity test: does this code give the notebook's answers?

The notebook's E0 cell exports, for 3 fixed claims, the judge answers and query vectors it
used and the rows `score_claim` produced. This test puts those answers into the pipeline's
caches and asks for the same 3 claims back.

Why this catches drift. The caches are keyed by a hash of the configuration:

    query key = sha1(embed_model | instr | claim)
    judge key = sha1(json([llm_model, prompt_version, lang, claim, passage_id, llm_params]))

The fixtures are stored under the keys **the notebook computed**. This code looks them up
under the keys **it computes itself**. So a wrong model, prompt version, instruction prefix
or parameter set produces a different key, the lookup misses, the pipeline reaches for the
network, and the fake client fails the test. A wrong retrieval order, tau or scoring rule
keeps the keys intact but changes the rows, which the comparison catches instead.

No network is involved, and the test skips cleanly until the notebook has been exported.
"""

import json
from pathlib import Path

import numpy as np
import pipeline
import pytest

FIX_DIR = Path(__file__).resolve().parent / "fixtures"
EXPECTED = FIX_DIR / "parity_expected.json"
VECTORS = FIX_DIR / "parity_query_vectors.npz"
DATA = Path(pipeline.DATA_DIR)

_missing = [
    str(p.relative_to(p.parents[2]))
    for p in (EXPECTED, VECTORS, DATA / "app_config.json",
              DATA / "passages.csv", DATA / "passage_emb.npy")
    if not p.exists()
]
pytestmark = pytest.mark.skipif(
    bool(_missing),
    reason="run the notebook's E0 export cell first; missing: " + ", ".join(_missing),
)


class NetworkWasCalled(BaseException):
    """Raised if the pipeline tries to reach the proxy.

    Deliberately a BaseException and not an Exception: `ask_msgs` retries on
    `except Exception`, so an ordinary error would be swallowed, slept over three times and
    reported as a network outage. This propagates immediately and names the real problem.
    """


@pytest.fixture
def no_network(monkeypatch):
    def boom(*a, **kw):
        raise NetworkWasCalled(
            "the pipeline asked for the network, so a cache key did not match the "
            "notebook's: check llm_model, prompt_version, llm_params, embed_model, instr"
        )

    monkeypatch.setattr(pipeline, "client", boom)
    return boom


@pytest.fixture
def fixtures():
    return json.loads(EXPECTED.read_text(encoding="utf-8"))


@pytest.fixture
def warm_caches(fixtures):
    """Load the notebook's answers under the notebook's own keys, then restore."""
    q_before = dict(pipeline.QUERY_CACHE)
    j_before = dict(pipeline.JUDGE_CACHE)
    with np.load(VECTORS) as vecs:
        for entry in fixtures["claims"]:
            # stored under the notebook's key on purpose: see the module docstring
            pipeline.QUERY_CACHE[entry["query_cache_key"]] = vecs[entry["query_cache_key"]]
            for rec in entry["judge_answers"]:
                pipeline.JUDGE_CACHE[rec["key"]] = rec
    yield
    pipeline.QUERY_CACHE.clear()
    pipeline.QUERY_CACHE.update(q_before)
    pipeline.JUDGE_CACHE.clear()
    pipeline.JUDGE_CACHE.update(j_before)


def test_the_export_carries_three_claims(fixtures):
    assert len(fixtures["claims"]) == 3


def test_config_on_disk_matches_the_config_the_fixtures_were_made_with(fixtures):
    """A readable diagnostic. The cache keys are the real guard, but this says why."""
    live, frozen = pipeline.config(), fixtures["config"]
    for field in ("embed_model", "llm_model", "llm_params", "prompt_version",
                  "instr", "k_stance", "tau", "corpus_version"):
        assert live[field] == frozen[field], (
            f"{field} drifted: app/data has {live[field]!r}, "
            f"the fixtures were exported with {frozen[field]!r}. Re-export both together."
        )


def test_prompt_text_on_disk_matches_the_fixtures(fixtures):
    assert pipeline.config()["prompt_cs"] == fixtures["config"]["prompt_cs"]


def test_corpus_matches_the_exported_checksums(fixtures):
    """passages.csv and passage_emb.npy must be the pair the config was written for."""
    import hashlib

    cfg = pipeline.config()
    for name, field in (("passages.csv", "sha256_passages_csv"),
                        ("passage_emb.npy", "sha256_passage_emb_npy")):
        actual = hashlib.sha256((DATA / name).read_bytes()).hexdigest()
        assert actual == cfg[field], (
            f"{name} does not match the checksum in app_config.json. "
            "The data and the config come from different exports; re-export both."
        )


@pytest.mark.parametrize("i", [0, 1, 2])
def test_score_claim_reproduces_the_notebook(i, fixtures, warm_caches, no_network):
    entry = fixtures["claims"][i]
    rows = pipeline.score_claim(entry["claim"])
    expected = entry["expected_rows"]

    assert len(rows) == len(expected), (
        f"{len(rows)} rows against the notebook's {len(expected)} for "
        f"{entry['claim']!r}: retrieval, tau or the scoring rule has changed"
    )
    for n, (got, want) in enumerate(zip(rows, expected)):
        assert got["party"] == want["party"], f"row {n}: party order differs"
        assert got["page"] == want["page"], f"row {n}: page differs"
        assert got["quote"] == want["quote"], f"row {n}: quote differs"
        assert got["confidence"] == want["confidence"], f"row {n}: confidence differs"
        assert got["score"] == pytest.approx(want["score"], abs=1e-9), (
            f"row {n} ({got['party']}): score {got['score']} vs the "
            f"notebook's {want['score']}"
        )


def test_ranking_reproduces_the_notebook(fixtures, warm_caches, no_network):
    """rank_parties over all 3 claims, so formula (3) is exercised end to end."""
    queries = [
        {"results": pipeline.score_claim(e["claim"])} for e in fixtures["claims"]
    ]
    expected = [
        {"results": e["expected_rows"]} for e in fixtures["claims"]
    ]
    assert pipeline.rank_parties(queries) == pipeline.rank_parties(expected)


def test_the_fake_client_really_does_fail(no_network):
    """Guards the guard: if this passed silently, every test above would be worthless."""
    with pytest.raises(NetworkWasCalled):
        pipeline.client()
