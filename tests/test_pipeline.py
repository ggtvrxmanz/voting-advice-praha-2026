"""Unit tests for the pure parts of the pipeline: parser, scoring, ranking.

No network and no app/data: every function here is tested with values passed in, so these
run on a fresh clone before the notebook has exported anything. The parity test in
test_parity.py is the one that proves the whole chain matches the notebook.

What each group is really checking:
  parse          bad model output can never reach the scoring stage
  passage_score  the tau filter and the "NO POSITION" rule
  claim_score    the contradiction rule, which index.html's own aggregate() lacks
  rank_parties   formula (3), its tie-break order, and what counts as coverage
"""

import pipeline
import pytest

PASSAGE = "Zahájíme výstavbu 5 000 obecních bytů na městských pozemcích."


def answer(**kw):
    """A judge answer as parse() would return it, overridable per test."""
    base = {"relevant": True, "stance": 1.0, "confidence": 1.0,
            "quote": PASSAGE, "quote_ok": True}
    return base | kw


# --------------------------------------------------------------- parse (C2)


def test_parse_accepts_a_valid_answer():
    raw = '{"relevant": true, "stance": 1, "confidence": 1, "quote": "%s"}' % PASSAGE
    d, err = pipeline.parse(raw, PASSAGE)
    assert err == ""
    assert (d["relevant"], d["stance"], d["confidence"]) == (True, 1.0, 1.0)
    assert d["quote_ok"] is True


def test_parse_strips_think_block_and_takes_the_last_object():
    raw = ('<think>{"relevant": false}</think>'
           '{"relevant": false, "stance": null, "confidence": null, "quote": ""}')
    d, err = pipeline.parse(raw, PASSAGE)
    assert err == "" and d["relevant"] is False and d["stance"] is None


def test_parse_rejects_a_quote_that_is_not_verbatim():
    raw = '{"relevant": true, "stance": 1, "confidence": 1, "quote": "postavíme byty"}'
    d, err = pipeline.parse(raw, PASSAGE)
    assert err == "" and d["quote_ok"] is False  # parsed, but will be dropped by scoring


def test_parse_accepts_a_quote_differing_only_in_whitespace():
    raw = '{"relevant": true, "stance": 1, "confidence": 1, "quote": "Zahájíme   výstavbu\\n5 000 obecních bytů"}'
    d, err = pipeline.parse(raw, PASSAGE)
    assert err == "" and d["quote_ok"] is True


@pytest.mark.parametrize("raw, because", [
    ("no json here at all", "no JSON object"),
    ('{"relevant": true, "stance": }', "bad JSON"),
    ('{"relevant": "yes"}', "bad 'relevant'"),
    ('{"relevant": true, "stance": null, "confidence": 1, "quote": "x"}', "no numeric stance"),
    ('{"relevant": true, "stance": 0.7, "confidence": 1, "quote": "x"}', "not on the scale"),
    ('{"relevant": true, "stance": 1, "confidence": 0.6, "quote": "x"}', "confidence not on the scale"),
    ('{"relevant": false, "stance": 1, "confidence": null, "quote": ""}', "not relevant but stance given"),
])
def test_parse_rejects_malformed_output(raw, because):
    d, err = pipeline.parse(raw, PASSAGE)
    assert d is None, f"should have been rejected: {raw}"
    assert because in err


def test_norm_collapses_whitespace():
    assert pipeline.norm("  a\n\tb   c  ") == "a b c"


def test_make_prompt_substitutes_both_placeholders():
    out = pipeline.make_prompt("teze", "uryvek", template="A<<QUERY>>B<<PASSAGE>>C")
    assert out == "AtezeBuryvekC"


# --------------------------------------------------------- passage_score (C6)


def test_passage_score_multiplies_confidence_by_stance():
    assert pipeline.passage_score(answer(stance=1.0, confidence=0.5), 0.0) == 0.5
    assert pipeline.passage_score(answer(stance=-1.0, confidence=0.75), 0.0) == -0.75


@pytest.mark.parametrize("rec", [
    answer(relevant=False),
    answer(quote_ok=False),
    answer(relevant=None),            # a net_error record must never score
    {"relevant": None, "net_error": True},
])
def test_passage_score_is_none_for_anything_not_usable(rec):
    assert pipeline.passage_score(rec, 0.0) is None


def test_passage_score_applies_the_tau_filter():
    rec = answer(confidence=0.5, stance=1.0)
    assert pipeline.passage_score(rec, 0.5) == 0.5   # exactly tau is kept
    assert pipeline.passage_score(rec, 0.75) is None  # below tau is dropped


def test_passage_score_ignores_confidence_when_asked():
    assert pipeline.passage_score(answer(confidence=0.25, stance=-1.0), 0.0,
                                  use_conf=False) == -1.0


# ----------------------------------------------------------- claim_score (C6)


def test_claim_score_reports_no_position_for_no_passages():
    assert pipeline.claim_score([]) == (0.0, "none")


def test_claim_score_takes_the_strongest_by_absolute_value():
    assert pipeline.claim_score([0.5, 1.0, -0.25]) == (1.0, "ok")
    assert pipeline.claim_score([-1.0, 0.5]) == (-1.0, "ok")


def test_claim_score_calls_an_opposite_sign_tie_a_contradiction():
    assert pipeline.claim_score([1.0, -1.0]) == (0.0, "contradiction")
    assert pipeline.claim_score([0.5, -0.5]) == (0.0, "contradiction")


def test_claim_score_only_looks_at_signs_among_the_strongest():
    # the -0.5 is weaker than the 1.0, so this is not a contradiction
    assert pipeline.claim_score([1.0, -0.5]) == (1.0, "ok")
    # here both extremes tie at 1.0, so it is
    assert pipeline.claim_score([1.0, -1.0, 0.5]) == (0.0, "contradiction")


def test_claim_score_treats_a_zero_score_as_a_position_not_as_absence():
    # Worth pinning: a kept passage scoring 0.0 is flagged "ok", so rank_parties counts it
    # as covered. The page text defines coverage as "S != 0", which disagrees. This test
    # records the notebook's behaviour, which is what ships; it does not endorse it.
    assert pipeline.claim_score([0.0]) == (0.0, "ok")


# ---------------------------------------------------------- rank_parties (D1)


def query(**per_party_score):
    """One analysed claim, as score_claim would return it."""
    return {"results": [{"party": p, "score": s} for p, s in per_party_score.items()]}


def test_rank_parties_averages_over_claims_and_sorts_by_score():
    ranking = pipeline.rank_parties(
        [query(a=1.0, b=0.0), query(a=0.0, b=0.0)], parties=["a", "b"]
    )
    assert [r["party"] for r in ranking] == ["a", "b"]
    assert ranking[0]["score"] == 0.5      # (1.0 + 0.0) / 2
    assert ranking[0]["perClaim"] == [1.0, 0.0]


def test_rank_parties_counts_a_contradiction_as_covered():
    ranking = pipeline.rank_parties([query2()], parties=["a"])
    assert ranking[0]["flags"] == ["contradiction"]
    assert ranking[0]["score"] == 0.0
    assert ranking[0]["coverage"] == 1     # documented, even though it scores 0


def query2():
    return {"results": [{"party": "a", "score": 1.0}, {"party": "a", "score": -1.0}]}


def test_rank_parties_does_not_count_a_silent_party_as_covered():
    ranking = pipeline.rank_parties([query(a=1.0)], parties=["a", "b"])
    silent = next(r for r in ranking if r["party"] == "b")
    assert silent["flags"] == ["none"] and silent["coverage"] == 0


def test_rank_parties_breaks_ties_by_coverage_then_party_name():
    # c and b both average 0.0, but b has a documented position and c has none,
    # so b must come first; a and d tie completely and fall back to the name.
    ranking = pipeline.rank_parties(
        [{"results": [{"party": "b", "score": 0.0}]}], parties=["d", "c", "b", "a"]
    )
    assert [r["party"] for r in ranking] == ["b", "a", "c", "d"]
