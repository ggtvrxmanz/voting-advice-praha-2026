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
## Refresh data from the notebook: TODO (Phase 2)
## Deploy to Render: TODO (Phase 5)
## Free-tier limits: TODO (Phase 6)

## Team rules
- Work on a branch, open a pull request, one reviewer approves, then merge. `main` deploys automatically.
- Never commit `.env` or any key.
- The notebook has one owner at a time (M3) and is committed with outputs cleared.
