# Volební kalkulačka Praha 2026

Czech voting advice tool: compares free-text claims with the programmes of 10 parties
(dense retrieval + LLM judge) and ranks the parties, with verbatim quotes.
Research notebook: `notebooks/`. Web app: `app/`. Hosting: Render (free web service).

> Skeleton (Phase 1). Sections marked TODO are filled in later phases.

## Local setup
```bash
git clone <repo-url> && cd voting-advice-praha-2026
python3.13 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then put your LiteLLM key into .env
```

## Phase 1 probe (temporary)
`python scripts/probe.py` makes one embedding call and one chat call and prints `OK` or `FAIL` for each.

## Run the app locally
```bash
python app/server.py        # http://127.0.0.1:5000
```
Flask's own server, for development only. With `APP_PASSWORD` empty in `.env` there is no
password; set it and the browser asks for one (any user name, that password).

Until the notebook's `E0` cell has been run and `app/data` filled in, `/api/health` answers
`{"ok": false}` and the page runs in **demo mode** with fictional parties. That is deliberate:
a missing export should not produce a broken page, and Render's health check must stay green.

On Windows you cannot run the production command locally — `gunicorn` needs `fcntl` and is
POSIX-only, even though it installs. Test with `python app/server.py` here and rely on
Render for the gunicorn path.

### What the two endpoints answer
| Route | Password? | Notes |
|---|---|---|
| `GET /` | yes | serves `app/index.html` |
| `GET /api/health` | **no** | Render's health check cannot log in. Returns only model, parties, `tau`, `prompt_version` — never a path or any detail about the server |
| `POST /api/analyze` | yes | 1–5 claims, ≤ 500 characters joined; 400 / 429 / 500 with `{"error": "<Czech>"}` |

### Abuse limits
- 3 analyses per IP per minute, and a cap of 2000 judge prompts per UTC day.
- Both counters live in this process's memory, so a restart resets them. Render restarts a
  free service whenever it likes, so treat the daily cap as a brake on accidents, not a
  guarantee against a determined abuser.
- The client address comes from `X-Forwarded-For`, which is only trustworthy because Render's
  proxy overwrites it. Do not run this open to the internet without such a proxy.

### Bugs fixed here (Failure Journal)
| # | Was | Effect |
|---|---|---|
| S1 | the validation branch returned `queries` before it was assigned | 0 or 6 claims raised `NameError` → HTML 500 instead of a Czech 400 |
| S2 | the success path returned no `ranking` | `index.html` fell back to its own `aggregate()`: no contradiction rule, different coverage. **Users were not seeing the evaluated ranking rule.** Fixing it restores the evaluated method; it is not a change of method |
| S3 | `TAU`, `PROMPT_VERSION`, `rank_parties` only existed after a notebook cell injected them | the file could not run or be tested on its own; it now imports `pipeline` |
| S4 | `debug=True` | an interactive console on a public host |

One more, found while testing this phase: `/api/health` first returned the `DataMissing`
reason verbatim, which included the absolute path of `app_config.json` — to anyone, with no
password, since that endpoint is open on purpose. It now returns a generic Czech sentence and
logs the detail. `tests/test_api.py::test_health_never_discloses_a_filesystem_path` guards it.

## Tests
```bash
python -m pytest tests/ -q -rs      # -rs prints why anything was skipped
```
No test touches the network. `tests/conftest.py` puts `app/` on `sys.path`, which is how
Render runs the app too (`gunicorn --chdir app server:app`), so the import is the same one
production uses.

| File | What it covers | Needs `app/data`? |
|---|---|---|
| `tests/test_pipeline.py` | `parse`, `passage_score`, `claim_score`, `rank_parties` | no |
| `tests/test_parity.py` | `score_claim` against the notebook's own answers | **yes** |

`test_parity.py` skips with a message naming the missing files until the notebook's `E0`
cell has been run and its output copied in. It is the test that proves this code reproduces
the evaluated method, and it works like an answer key:

- The caches are keyed by a hash of the configuration — `sha1(embed_model | instr | claim)`
  for a query, and `sha1(json([llm_model, prompt_version, lang, claim, passage_id, llm_params]))`
  for a judge answer.
- The fixtures are stored under the keys **the notebook computed**; the test looks them up
  under the keys **this code computes**.
- So a wrong model, prompt version, instruction prefix or parameter set gives a different
  key, the lookup misses, the code reaches for the network, and a fake client fails the test.
  A wrong retrieval order, `tau` or scoring rule keeps the keys but changes the rows, which
  the row-by-row comparison catches instead.

The fake client raises a `BaseException`, not an `Exception`, on purpose: `ask_msgs` retries
on `except Exception`, so an ordinary error would be swallowed, slept over three times and
then misreported as a proxy outage.

`tests/test_api.py` (health, the analyze contract, 400 on 6 claims) arrives with the server
in Phase 4 — it cannot run before there is an app to import.

## Refresh data from the notebook
The app never holds a copy of the method: the notebook writes the config the app reads, so the
two cannot drift apart. The last cell of `notebooks/Communal_Party_Election_analysis_v2_fixed.ipynb`
(`E0 | Export for the web app`) does the whole export. It makes no API call and changes no method.

1. Open the notebook in Colab and run it down to `D1` (the app adapter), so `score_claim`,
   the query cache and the judge cache all exist in the session.
2. Run the `E0` cell. It writes into `<PROJECT_DIR>/export/`:

   | File | What it is |
   |---|---|
   | `app/data/passages.csv` | the 696 non-noise passages (`passage_id, party, page, text`), in `df_clust` order |
   | `app/data/passage_emb.npy` | `E_qwen`, float32, **same row order** as `passages.csv` |
   | `app/data/app_config.json` | model, params, prompt text, `k_stance`, `tau`, corpus version, SHA-256 of the two files above |
   | `tests/fixtures/parity_expected.json` | 3 claims with their judge answers and the expected `score_claim` rows |
   | `tests/fixtures/parity_query_vectors.npz` | the query vectors those 3 claims used |

3. Copy `export/app/data` over `app/data`, and `export/tests/fixtures` over `tests/fixtures`.
4. Commit on a branch and open a pull request. `app/data` is read-only at run time.

Why the export is generated and not written by hand: `app_config.json` takes the judge from
`APP_MODEL`, which cell `C3` sets to the smoke-test winner. `P["llm_model"]` in cell 3 is only a
starting value and is **not** the model that was evaluated, so retyping it would silently ship the
wrong judge. The same applies to `tau`, which `C6` freezes to a file.

If `E0` stops with `need 3 fully cached claims`, the judge cache only holds `D1`'s own test claim.
Run `score_claim` once per extra claim in a scratch cell (about 50 judge calls each), then re-run `E0`.

## Deploy to Render: TODO (Phase 5)
## Free-tier limits: TODO (Phase 6)

## Team rules
- Work on a branch, open a pull request, one reviewer approves, then merge. `main` deploys automatically.
- Never commit `.env` or any key.
- The notebook has one owner at a time (M3) and is committed with outputs cleared.
