"""API contract tests, with the pipeline stubbed out: no network, no app/data.

The point is the HTTP layer, so `pipeline.config` and `pipeline.score_claim` are replaced
by fixtures. `rank_parties` is left real, so the ranking in the response is produced by the
code that actually ships.

Four of these are regression tests for the bugs listed in server.py's docstring:
  S1  0 or 6 claims must be a Czech 400, not a NameError 500
  S2  the success response must carry `ranking`
  S3  server.py must import on its own, which it does by importing pipeline
  S4  debug must be off
"""

import base64

import pipeline
import pytest
import server

CONFIG = {
    "llm_model": "Qwen-test",
    "parties": ["ano", "pirati", "spd"],
    "tau": 0.0,
    "prompt_version": "v3",
    "k_stance": 5,
    "embed_model": "qwen3-embedding:4b",
    "instr": "Instruct: ...\nQuery: ",
    "llm_params": {"temperature": 0, "seed": 42, "max_tokens": 1024},
}

ROWS = {
    "Praha by měla dostavět metro D.": [
        {"party": "pirati", "quote": "Dostavíme metro D.", "page": 3,
         "score": 1.0, "confidence": 1.0},
        {"party": "ano", "quote": "Metro D prověříme.", "page": None,
         "score": 0.5, "confidence": 0.5},
    ],
}


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Fresh rate-limit and daily-cap state, and no password, for every test."""
    monkeypatch.setattr(server, "APP_PASSWORD", "")
    monkeypatch.setattr(server, "_hits", {})
    monkeypatch.setattr(server, "_day", {"date": None, "baseline": 0})
    monkeypatch.setitem(pipeline.STATS, "judge_calls", 0)
    yield


@pytest.fixture
def client():
    app = server.app
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture
def with_data(monkeypatch):
    monkeypatch.setattr(pipeline, "config", lambda: CONFIG)
    monkeypatch.setattr(pipeline, "score_claim", lambda c: list(ROWS.get(c, [])))


SECRET_PATH = (r"C:\Users\student\OneDrive - Vysoka skola ekonomicka"
               r"\semestral\app\data\app_config.json")


@pytest.fixture
def without_data(monkeypatch):
    def missing():
        raise pipeline.DataMissing(f"{SECRET_PATH} is missing; run the E0 export cell")

    monkeypatch.setattr(pipeline, "config", missing)


# ---------------------------------------------------------------- health

def test_health_reports_the_frozen_config(client, with_data):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["model"] == "Qwen-test"
    assert body["parties"] == ["ano", "pirati", "spd"]
    assert body["tau"] == 0.0
    assert body["prompt_version"] == "v3"


def test_health_stays_200_when_data_is_missing(client, without_data):
    """Render's health check must stay green, and the page falls back to demo mode."""
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.get_json()["ok"] is False


def test_health_never_discloses_a_filesystem_path(client, without_data):
    """/api/health is open on purpose, so its body must say nothing about the server.

    The first version of this endpoint returned the DataMissing reason verbatim, which
    included the absolute path of app_config.json, to anyone who asked without a password.
    """
    r = client.get("/api/health")
    body = r.get_data(as_text=True)
    for leak in (SECRET_PATH, "OneDrive", "app_config.json", "Users", "/opt/render"):
        assert leak not in body, f"{leak!r} must not appear in an unauthenticated response"


def test_analyze_does_not_disclose_a_path_either(client, without_data):
    r = client.post("/api/analyze", json={"claims": ["x"]})
    assert r.status_code == 500
    assert SECRET_PATH not in r.get_data(as_text=True)


# ---------------------------------------------------------------- validation (S1)

@pytest.mark.parametrize("body, because", [
    ({"claims": []}, "alespoň jedno"),
    ({"claims": ["", "   "]}, "alespoň jedno"),
    ({}, "alespoň jedno"),
    ({"claims": ["a"] * 6}, "nejvýše 5"),
    ({"claims": ["x" * 600]}, "delší než 500"),
    ({"claims": "not a list"}, "musí být seznam"),
])
def test_bad_input_is_a_czech_400(client, with_data, body, because):
    r = client.post("/api/analyze", json=body)
    assert r.status_code == 400, f"expected 400, got {r.status_code}"
    assert because in r.get_json()["error"]


def test_six_claims_does_not_crash_the_way_it_used_to(client, with_data):
    """S1: this raised NameError on `queries` and returned an HTML 500."""
    r = client.post("/api/analyze", json={"claims": ["a", "b", "c", "d", "e", "f"]})
    assert r.status_code == 400
    assert r.is_json, "must be JSON, not an HTML error page"


# ---------------------------------------------------------------- success (S2)

def test_analyze_returns_queries_and_ranking(client, with_data):
    """S2: `ranking` must be present, or index.html silently uses its own aggregate()."""
    claim = "Praha by měla dostavět metro D."
    r = client.post("/api/analyze", json={"claims": [claim]})
    assert r.status_code == 200
    body = r.get_json()

    assert [q["claim"] for q in body["queries"]] == [claim]
    assert "ranking" in body, "the ranking is missing: index.html would fall back"

    ranking = body["ranking"]
    assert [row["party"] for row in ranking] == ["pirati", "ano", "spd"]
    assert ranking[0]["score"] == 1.0
    assert ranking[0]["coverage"] == 1
    # spd was never retrieved, so it scores 0 and is not counted as covered
    assert ranking[-1] == {"party": "spd", "score": 0.0, "perClaim": [0.0],
                           "flags": ["none"], "coverage": 0}


def test_results_are_sorted_by_score_descending(client, with_data):
    r = client.post("/api/analyze", json={"claims": ["Praha by měla dostavět metro D."]})
    scores = [x["score"] for x in r.get_json()["queries"][0]["results"]]
    assert scores == sorted(scores, reverse=True)


def test_page_is_null_never_nan(client, with_data):
    """NaN is invalid JSON and breaks the page."""
    r = client.post("/api/analyze", json={"claims": ["Praha by měla dostavět metro D."]})
    assert b"NaN" not in r.data
    pages = [x["page"] for x in r.get_json()["queries"][0]["results"]]
    assert None in pages


def test_scores_are_clamped_to_the_valid_range(client, with_data, monkeypatch):
    monkeypatch.setattr(pipeline, "score_claim", lambda c: [
        {"party": "ano", "quote": "q", "page": 1, "score": 7.5, "confidence": 1.0},
        {"party": "spd", "quote": "q", "page": 1, "score": -9.0, "confidence": 1.0},
    ])
    r = client.post("/api/analyze", json={"claims": ["x"]})
    scores = [x["score"] for x in r.get_json()["queries"][0]["results"]]
    assert scores == [1.0, -1.0]


# ---------------------------------------------------------------- errors

def test_a_czech_pipeline_error_reaches_the_page_verbatim(client, with_data, monkeypatch):
    message = "Model LLM není dostupný (Timeout). Zkuste to znovu později."

    def boom(_):
        raise RuntimeError(message)

    monkeypatch.setattr(pipeline, "score_claim", boom)
    r = client.post("/api/analyze", json={"claims": ["x"]})
    assert r.status_code == 500 and r.get_json()["error"] == message


def test_an_unexpected_error_does_not_leak_its_detail(client, with_data, monkeypatch):
    def boom(_):
        raise ValueError("ValueError: /opt/render/project/src/app/pipeline.py line 12")

    monkeypatch.setattr(pipeline, "score_claim", boom)
    r = client.post("/api/analyze", json={"claims": ["x"]})
    assert r.status_code == 500
    error = r.get_json()["error"]
    assert "pipeline.py" not in error and "ValueError" not in error
    assert "Zkuste" in error


# ---------------------------------------------------------------- limits

def test_the_rate_limit_answers_429_after_the_allowance(client, with_data):
    for n in range(server.RATE_LIMIT):
        assert client.post("/api/analyze", json={"claims": ["x"]}).status_code == 200, n
    r = client.post("/api/analyze", json={"claims": ["x"]})
    assert r.status_code == 429 and "Příliš mnoho" in r.get_json()["error"]


def test_the_rate_limit_is_per_ip(client, with_data):
    for _ in range(server.RATE_LIMIT):
        client.post("/api/analyze", json={"claims": ["x"]},
                    headers={"X-Forwarded-For": "10.0.0.1"})
    blocked = client.post("/api/analyze", json={"claims": ["x"]},
                          headers={"X-Forwarded-For": "10.0.0.1"})
    other = client.post("/api/analyze", json={"claims": ["x"]},
                        headers={"X-Forwarded-For": "10.0.0.2"})
    assert blocked.status_code == 429
    assert other.status_code == 200, "a different address must not be punished"


def test_the_daily_cap_blocks_when_too_few_calls_remain(client, with_data, monkeypatch):
    monkeypatch.setattr(server, "DAILY_JUDGE_CAP", 10)   # a claim needs 5 x 3 = 15
    r = client.post("/api/analyze", json={"claims": ["x"]})
    assert r.status_code == 429 and "Denní limit" in r.get_json()["error"]


def test_the_daily_cap_counts_real_calls_to_the_proxy(client, with_data, monkeypatch):
    monkeypatch.setattr(server, "DAILY_JUDGE_CAP", 20)
    assert client.post("/api/analyze", json={"claims": ["x"]}).status_code == 200
    pipeline.STATS["judge_calls"] = 19          # as if the proxy had been called 19 times
    r = client.post("/api/analyze", json={"claims": ["x"]})
    assert r.status_code == 429


# ---------------------------------------------------------------- password

def test_no_password_means_open_access(client, with_data):
    assert client.get("/").status_code == 200


def test_the_password_protects_the_page(client, with_data, monkeypatch):
    monkeypatch.setattr(server, "APP_PASSWORD", "tajne-heslo")
    r = client.get("/")
    assert r.status_code == 401
    assert "Basic" in r.headers["WWW-Authenticate"]


def test_the_right_password_gets_in(client, with_data, monkeypatch):
    monkeypatch.setattr(server, "APP_PASSWORD", "tajne-heslo")
    token = base64.b64encode("kdokoli:tajne-heslo".encode()).decode()
    r = client.get("/", headers={"Authorization": f"Basic {token}"})
    assert r.status_code == 200


def test_a_wrong_password_is_refused(client, with_data, monkeypatch):
    monkeypatch.setattr(server, "APP_PASSWORD", "tajne-heslo")
    token = base64.b64encode("kdokoli:spatne".encode()).decode()
    r = client.get("/", headers={"Authorization": f"Basic {token}"})
    assert r.status_code == 401


def test_health_stays_open_so_renders_check_can_reach_it(client, with_data, monkeypatch):
    """Render's health check cannot log in, so this endpoint must never be behind the password."""
    monkeypatch.setattr(server, "APP_PASSWORD", "tajne-heslo")
    r = client.get("/api/health")
    assert r.status_code == 200 and r.get_json()["ok"] is True


def test_analyze_is_behind_the_password(client, with_data, monkeypatch):
    monkeypatch.setattr(server, "APP_PASSWORD", "tajne-heslo")
    r = client.post("/api/analyze", json={"claims": ["x"]})
    assert r.status_code == 401


def test_a_non_ascii_password_does_not_crash_the_comparison(client, with_data, monkeypatch):
    """hmac.compare_digest raises TypeError on non-ASCII str, so both sides are encoded."""
    monkeypatch.setattr(server, "APP_PASSWORD", "přílišžluťoučký")
    token = base64.b64encode("u:přílišžluťoučký".encode()).decode()
    assert client.get("/", headers={"Authorization": f"Basic {token}"}).status_code == 200


# ---------------------------------------------------------------- S3 / S4

def test_the_module_imports_and_serves_without_a_notebook(client):
    """S3: TAU, PROMPT_VERSION and rank_parties used to exist only after a cell injected them."""
    assert server.app is not None
    assert client.get("/api/health").status_code == 200


def test_debug_is_off(client):
    """S4: debug=True would expose an interactive console on a public host."""
    assert server.app.debug is False
