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

## Run the app locally: TODO (Phase 4)
## Tests: TODO (Phase 3)

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
