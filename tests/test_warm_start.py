"""Tests for the warm start: loading the committed caches at boot.

The point of these files is that the first analysis after a spin-down should not pay for a
full set of live judge calls. The point of these tests is that they can never make the app
*wrong* or *dead*: a stale file is ignored, a corrupt file is skipped, and an absent file is
simply a cold start.
"""

import json

import numpy as np
import pipeline
import pytest

CONFIG = {
    "embed_model": "qwen3-embedding:4b",
    "instr": "Instruct: ...\nQuery: ",
    "llm_model": "Qwen-test",
    "prompt_version": "v3",
    "llm_params": {"temperature": 0, "seed": 42, "max_tokens": 1024},
    "prompt_cs": "Teze: <<QUERY>>\nÚryvek: <<PASSAGE>>",
}


class NetworkWasCalled(BaseException):
    """BaseException so pipeline's `except Exception` retry cannot swallow it."""


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Point pipeline at an empty temp data dir and reset its caches."""
    monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "_warmed", False)
    monkeypatch.setattr(pipeline, "config", lambda: CONFIG)
    pipeline.QUERY_CACHE.clear()
    pipeline.JUDGE_CACHE.clear()
    yield tmp_path
    pipeline.QUERY_CACHE.clear()
    pipeline.JUDGE_CACHE.clear()


def write_queries(tmp_path, mapping):
    (tmp_path / "query_cache.json").write_text(
        json.dumps({k: [float(x) for x in v] for k, v in mapping.items()}),
        encoding="utf-8")


def write_judgements(tmp_path, records):
    (tmp_path / "app_stance_cache.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8")


def answer(key, **kw):
    base = {"key": key, "claim": "c", "passage_id": "p1", "relevant": True,
            "stance": 1.0, "confidence": 1.0, "quote": "citace", "quote_ok": True}
    return base | kw


# ---------------------------------------------------------------- loading

def test_no_files_is_a_cold_start_not_an_error(isolated):
    out = pipeline.warm_start()
    assert out == {"queries": 0, "judgements": 0}
    assert not pipeline.QUERY_CACHE and not pipeline.JUDGE_CACHE


def test_it_loads_both_files(isolated):
    write_queries(isolated, {"qkey": np.arange(4, dtype=np.float32)})
    write_judgements(isolated, [answer("a"), answer("b")])

    out = pipeline.warm_start()
    assert out == {"queries": 1, "judgements": 2}
    assert set(pipeline.JUDGE_CACHE) == {"a", "b"}
    assert np.array_equal(pipeline.QUERY_CACHE["qkey"], np.arange(4, dtype=np.float32))


def test_query_vectors_come_back_as_float32(isolated):
    """dense_scores does a float32 matmul; a float64 vector would change the dtype."""
    write_queries(isolated, {"q": np.array([0.1, -0.2, 0.3], dtype=np.float32)})
    pipeline.warm_start()
    vec = pipeline.QUERY_CACHE["q"]
    assert vec.dtype == np.float32
    # 0.1 is not exact in binary, so this only passes if the round trip is lossless
    assert vec[0] == np.float32(0.1)


def test_it_is_idempotent(isolated):
    write_judgements(isolated, [answer("a")])
    assert pipeline.warm_start()["judgements"] == 1
    again = pipeline.warm_start()
    assert again["judgements"] == 0 and "skipped" in again


def test_force_reloads(isolated):
    write_judgements(isolated, [answer("a")])
    pipeline.warm_start()
    assert pipeline.warm_start(force=True)["judgements"] == 1


def test_entries_already_in_the_cache_win(isolated):
    """A test or an earlier live call must not be overwritten by a committed file."""
    pipeline.JUDGE_CACHE["a"] = answer("a", quote="live")
    write_judgements(isolated, [answer("a", quote="from the file")])
    pipeline.warm_start()
    assert pipeline.JUDGE_CACHE["a"]["quote"] == "live"


# ---------------------------------------------------------------- robustness

def test_a_corrupt_query_file_is_skipped_not_raised(isolated):
    (isolated / "query_cache.json").write_text("{not json", encoding="utf-8")
    write_judgements(isolated, [answer("a")])
    out = pipeline.warm_start()
    assert out["queries"] == 0           # skipped
    assert out["judgements"] == 1        # the other file still loaded


def test_one_bad_line_does_not_lose_the_good_ones(isolated):
    path = isolated / "app_stance_cache.jsonl"
    path.write_text(
        json.dumps(answer("a")) + "\n"
        + "{broken\n"
        + "\n"                                    # blank lines are fine
        + json.dumps(answer("b")) + "\n",
        encoding="utf-8")
    out = pipeline.warm_start()
    assert out["judgements"] == 2
    assert set(pipeline.JUDGE_CACHE) == {"a", "b"}


def test_records_without_a_key_are_ignored(isolated):
    write_judgements(isolated, [{"claim": "c", "quote": "no key here"}, answer("a")])
    pipeline.warm_start()
    assert set(pipeline.JUDGE_CACHE) == {"a"}


# ---------------------------------------------------------------- the actual point

def test_a_warm_answer_means_no_network_call(isolated, monkeypatch):
    """The whole reason these files exist."""
    key = pipeline.judge_key("Praha by měla dostavět metro D.", "p1")
    write_judgements(isolated, [answer(key, quote="doslovná citace")])
    pipeline.warm_start()

    def boom(*a, **kw):
        raise NetworkWasCalled("judge_claim reached for the proxy")

    monkeypatch.setattr(pipeline, "client", boom)
    rec = pipeline.judge_claim("Praha by měla dostavět metro D.", "p1")
    assert rec["quote"] == "doslovná citace"


def test_a_stale_file_is_useless_but_never_wrong(isolated, monkeypatch):
    """A key from a different configuration must miss, so the app asks the model instead."""
    stale = pipeline.judge_key("claim", "p1")
    write_judgements(isolated, [answer(stale)])
    pipeline.warm_start()

    # now pretend the judge model changed, as it would after a re-evaluation
    monkeypatch.setattr(pipeline, "config", lambda: {**CONFIG, "llm_model": "Qwen-other"})
    assert pipeline.judge_key("claim", "p1") != stale

    def boom(*a, **kw):
        raise NetworkWasCalled("expected: the stale entry must not be used")

    monkeypatch.setattr(pipeline, "client", boom)
    monkeypatch.setattr(pipeline, "corpus", lambda: {"text": {"p1": "úryvek"}})
    with pytest.raises(NetworkWasCalled):
        pipeline.judge_claim("claim", "p1")
