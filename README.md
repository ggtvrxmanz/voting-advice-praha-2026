# Volební kalkulačka Praha 2026

Czech voting advice tool. It compares free-text claims with the programmes of 10 parties
(dense retrieval + LLM judge), ranks the parties, and shows verbatim quotes as evidence.

**The research was done in Google Colab.** The notebook is the original work: corpus building,
retrieval, the LLM judge and the evaluation all live there. This repository holds the web app
(`app/`), its tests, and a snapshot of the notebook (`notebooks/`).

| | Link |
|---|---|
| Research notebook (Colab, original) | <COLAB_LINK> |
| Web app (Render, free web service) | <https://voting-advice-praha-2026.onrender.com> |

> **Status: live.** <https://voting-advice-praha-2026.onrender.com> is behind a shared
> password. The data is exported from the notebook, the parity test passes,
> and the app answers from the real model.

**How to connect:**

- username: anything (it may stay empty)
- password: `Praha`

Verified on the deployed service:

```
GET /api/health
{"ok": true, "model": "Qwen3.8-27B", "parties": [... 10 ...],
 "prompt_version": "v3", "tau": 0}

GET /
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Basic realm="Volebni kalkulacka Praha 2026"
```

## Run the notebook in Colab
The notebook reads and writes everything (programmes, corpus versions, embeddings, LLM caches,
evaluation files) in one shared Google Drive folder. Colab can only mount **your own** My Drive,
so a folder that was only shared with you is invisible until you add a shortcut to it.

1. **Add a shortcut to the project folder.** In Google Drive, open *Shared with me*, right-click
   `TA2 - Communal Party Election analysis` → *Organize* → *Add shortcut* → *My Drive*.
   The config cell finds the folder by the name hint `TA2` (up to three levels deep in My Drive),
   so the shortcut may sit in a subfolder, but its name must still contain `TA2`.
   The folder must contain `Programy` (the party programmes) and `data/`.
2. **Open the notebook** from the Colab link above. Without edit rights, use
   *File → Save a copy in Drive* and work in the copy.
3. **Add the API key as a Colab secret.** Key icon in the left panel → *Add new secret* →
   name `LITELLM_API_KEY`, value = your VŠE LiteLLM key → switch on *Notebook access*.
   The key is never typed into a cell.
4. **Run cells 1–3** (installs, Drive mount, paths and config). Allow the Drive access when asked.
   If the folder is not found, the cell says so; use *Runtime → Disconnect and delete runtime*
   and run it again (a new shortcut is only seen after a fresh mount).
5. **Then either** *Runtime → Run all* (rebuilds the corpus from the programmes),
   **or** click the cell under the heading **"CAN JUST RUN FROM HERE"** and use
   *Runtime → Run cell and below*. That starts from the frozen corpus and is much faster.

Every LLM and embedding answer is cached on Drive, keyed by model, prompt version, claim,
passage and parameters, so re-running the notebook only calls the model for new pairs.

## What the two endpoints answer
| Route | Password? | Notes |
|---|---|---|
| `GET /` | yes | serves `app/index.html` |
| `GET /api/health` | **no** | Render's health check cannot log in. Returns only model, parties, `tau`, `prompt_version`, never a path or any detail about the server |
| `POST /api/analyze` | yes | 1–5 claims, ≤ 500 characters joined; 400 / 429 / 500 with `{"error": "<Czech>"}` |

## Abuse limits
Two limits, each of which tells the user **which** one they hit and when to come back. Both
answer `429` with a Czech `error` the page renders verbatim, plus a `Retry-After` header.

| Limit | Scope | Value | What the user sees |
|---|---|---|---|
| Rate | **per user** (per IP) | `RATE_LIMIT` = 3 analyses per 60 s | `Dosažen limit rychlosti: 3 analýzy za 60 sekund. Zkuste to znovu za 47 s.` |
| Daily | **shared by everyone** | `DAILY_JUDGE_CAP` = 10000 prompts per UTC day | `Dosažen denní limit dotazů na model. Zbývá 12 z 200 tvrzení na dnešek, požadováno 3. Limit se obnoví po půlnoci UTC.` |

The scopes differ on purpose. The rate limit is a dict keyed by client address, so each user
gets their own window. The daily cap reads one process-wide counter with no per-user
breakdown, so it is **one shared pot**.

**What 10000 buys.** One claim costs `k_stance × parties` = 5 × 10 = 50 prompts, and a passage
may need one re-ask, so a claim costs at most 100. That gives **100 uncached claims a day in
the worst case** and 200 if nothing is re-asked. The 200 is the figure the message quotes,
since it divides the remaining budget by 50. Recompute it if `k_stance` or the number of
parties changes.

Cached claims cost nothing, because the counter only increments on a prompt actually sent. The
18 claims in the committed cache are therefore effectively unlimited, which is what makes a
demo safe.

- **One user can empty the shared pot.** At 3 analyses a minute, 5 claims each, 50 prompts a
  claim, that is 750 prompts a minute: the whole 10000 in about 13 minutes, all within the
  rate limit. A per-user daily allowance was considered and rejected as not worth the
  complexity for a service behind a shared password.
- Both counters live in this process's memory, so a restart resets them. Treat the daily cap
  as a brake on accidents, not a guarantee against a determined abuser.
- The daily check reserves the full cost up front and ignores the cache, so near exhaustion a
  request that would be served entirely from cache is still refused. The alternative is
  admitting a request and running out of budget halfway through it.
- Retries count. A failed prompt is retried three times, so a flaky proxy burns the budget
  faster than the arithmetic above suggests.
- The client address comes from `X-Forwarded-For`, which is only trustworthy because Render's
  proxy overwrites it.

## Bugs fixed during the migration (Failure Journal)
| # | Was | Effect |
|---|---|---|
| S1 | the validation branch returned `queries` before it was assigned | 0 or 6 claims raised `NameError` → HTML 500 instead of a Czech 400 |
| S2 | the success path returned no `ranking` | `index.html` fell back to its own `aggregate()`: no contradiction rule, different coverage. **Users were not seeing the evaluated ranking rule.** Fixing it restores the evaluated method; it is not a change of method |
| S3 | `TAU`, `PROMPT_VERSION`, `rank_parties` only existed after a notebook cell injected them | the file could not run or be tested on its own; it now imports `pipeline` |
| S4 | `debug=True` | an interactive console on a public host |

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
| `tests/test_api.py` | health, the analyze contract, 400 / 429 / 500, the password | no |
| `tests/test_warm_start.py` | loading the committed caches, and failing safe | no |
| `tests/test_parity.py` | `score_claim` against the notebook's own answers | **yes** |

Current state: **79 passed, 0 skipped.** The parity test is what makes the claim
"this code is the evaluated method" evidenced rather than asserted.

It works like an answer key:

- The caches are keyed by a hash of the configuration: `sha1(embed_model | instr | claim)`
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

`test_parity.py` skips with a message naming the missing files if the notebook's `E0` output
has not been copied in.

## Refresh data from the notebook
The app never holds its own copy of the method: the notebook writes the config the app reads,
so the two cannot drift apart. The `E0 | Export for the web app` cell does the whole export.
It makes no API call and changes no method.

1. Open the notebook in Colab (see [Run the notebook in Colab](#run-the-notebook-in-colab)) and
   run it down to `D1` (the app adapter), so `score_claim`, the query cache and the judge cache
   all exist in the session.
2. Run the `E0` cell. It writes into `<PROJECT_DIR>/export/`:

   | File | What it is |
   |---|---|
   | `app/data/passages.csv` | the 696 non-noise passages (`passage_id, party, page, text`), in `df_clust` order |
   | `app/data/passage_emb.npy` | `E_qwen`, float32, **same row order** as `passages.csv` |
   | `app/data/app_config.json` | model, params, prompt text, `k_stance`, `tau`, corpus version, SHA-256 of the two files above |
   | `app/data/app_stance_cache.jsonl` | every cached judge answer, for the warm start |
   | `app/data/query_cache.json` | their query vectors, for the warm start |
   | `tests/fixtures/parity_expected.json` | 3 claims with their judge answers and the expected `score_claim` rows |
   | `tests/fixtures/parity_query_vectors.npz` | the query vectors those 3 claims used |

3. Copy `export/app/data` over `app/data`, and `export/tests/fixtures` over `tests/fixtures`.
4. Commit and push. `main` deploys automatically. `app/data` is read-only at run time.

Why the export is generated and not written by hand: `app_config.json` takes the judge from
`APP_MODEL`, which cell `C3` sets to the smoke-test winner. `P["llm_model"]` in cell 3 is only a
starting value and is **not** the model that was evaluated, so retyping it would silently ship the
wrong judge. The same applies to `tau`, which `C6` freezes to a file.

If `E0` stops with `need 3 fully cached claims`, the judge cache only holds `D1`'s own test claim.
Run `score_claim` once per extra claim in a scratch cell (about 50 judge calls each), then re-run `E0`.

## Deploy to Render
One free web service in Frankfurt, auto-deploying from `main`. The settings live in
[`render.yaml`](render.yaml) as the documented truth; read it before changing anything in
the dashboard. The running service was created through the dashboard, so Render does **not**
apply that file to it; the file is there to be reviewed and to recreate the service exactly.

| Setting | Value |
|---|---|
| Build command | `pip install -r requirements.txt` |
| Start command | `gunicorn --chdir app server:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 600` |
| Health check path | `/api/health` |
| Region / plan | Frankfurt / Free |
| Branch | `main`, auto-deploy on commit |

Why the start command reads like that: **one worker** because the query cache, the judge
cache and the rate-limit counters all live in one process's memory and 512 MB will not hold
a second copy of the embedding matrix; **threads** because each request waits on the proxy
rather than the CPU; **`--timeout 600`** because gunicorn's default of 30 s would kill a
50-call analysis long before it finished.

### How it was set up, and the order to repeat it in
Already done for the live service. Follow the same order if you ever rebuild it, because
steps 2 and 3 must not be swapped.

1. **Merge into `main`.** Auto-deploy fires, but while the start command is still the Phase 1
   probe nothing is exposed.
2. **Set `APP_PASSWORD`** in Render → Environment. The URL stays protected until the teacher
   or VŠE IT agrees to public use. Do this *before* step 3, or there is a window in which the
   app is live, open, and spending the university's LiteLLM key.
3. **Change the start command and health check path** in Render → Settings. This is the step
   that makes the app live.
4. **Export the data only once step 3 is done.** The Phase 1 start command was
   `python -m http.server --directory app/data`, a plain static file server with no password.
   Committing `app/data` while that was running would have published the corpus and the full
   judge prompt to anyone with the URL.
5. **Verify**: `/api/health` answers 200 with `ok: true`, the root URL challenges for the
   password, and a wrong password is refused.

`LITELLM_API_KEY` and `APP_PASSWORD` are set in the dashboard only, never in Git. In
`render.yaml` they carry `sync: false`, which tells Render to ask for the value instead of
reading one from the file.

### If `app/data` is ever missing
`app/data` is committed, so this should not happen. If those files are removed or an
export is half-copied, the app does not break: `/api/health` answers `200` with
`{"ok": false}`, the health check still passes, the deploy still succeeds, and the page falls
back to demo mode with fictional parties instead of showing an error. Only `/api/analyze`
stops working, with a Czech 500. The detailed reason, including the path of the missing file,
goes to the Render log only, never to the open `/api/health` response.

## Free-tier limits
Checked against `render.com/docs/free` and `render.com/docs/python-version` on
**2026-11-25**. Re-check before relying on any of it; Render changes these.

| Limit | Value |
|---|---|
| Compute | 0.1 CPU, 512 MB RAM, one instance, no scaling |
| Spin-down | after **15 minutes** with no inbound traffic |
| Spin-up | **about one minute**; the browser shows a loading page |
| Instance hours | **750 per workspace per calendar month**, not per service. Spun-down time does not count. Reset monthly, no roll-over |
| Out of hours | Render **suspends every free web service in the workspace** until the next month |
| Filesystem | ephemeral: lost on redeploy, restart **and spin-down**. No persistent disk on free |
| Restarts | Render **may restart a free service at any time** |
| Postgres | one per workspace, 1 GB, **expires 30 days after creation**; 14-day grace, then deleted. No backups |
| Key Value | one per workspace, **never persisted to disk**; data lost on any restart |
| Not available | shell access, one-off jobs, private network traffic, more than one instance. Outbound ports 25 / 465 / 587 blocked. Rollback only to the two most recent deploys |

### The limit that actually threatens this app
> Render may suspend a free web service that initiates an uncommonly high volume of traffic
> over the public internet, and the docs name **external API calls** as an example.

Every analysed claim makes one embedding call plus `k_stance × parties` judge calls: up to
51 outbound requests per claim, so ~255 for five claims. No numeric threshold is published.
This is the main reason the daily judge cap exists: it protects the university's key, and it
keeps the service from being suspended.

The cap of 10000 prompts a day is a deliberate choice, not a derived one, because there is no
published threshold to derive it from. It accepts some risk in exchange for not cutting users
off mid-demo. A suspended free service is only restored by moving to a paid plan, so if the
service is ever suspended for outbound traffic, lowering the cap is the first thing to try.

### Bandwidth and build minutes
The docs give **no numbers** for either on free; see the dashboard's *Included Usage* page.
What happens on overage:

- **Bandwidth**: Render bills a supplementary amount. With no payment method on file it
  instead **suspends all free services for the rest of the month**.
- **Build minutes**: billed likewise; with no payment method or at the spend limit it
  **disables new builds for the whole workspace** for the rest of the month. Running services
  keep running on their existing build.

So without a card on file, an overage is not a bill: it is an outage.

### Python version
The default for services created on or after 2026-11-03 is **3.14.10**. We pin **3.13**, the
same minor version Colab uses, so a result in the notebook means something on the server:

- `.python-version` with `3.13`. Render takes the latest matching patch; the build log shows
  `Using Python version 3.13.18 via /opt/render/project/src/.python-version`.
- `PYTHON_VERSION` as an env var would also work but **must** be fully qualified, e.g.
  `3.13.5`. We do not use it: the file is in Git and reviewable, the variable is not.

### Cold starts, and why we do not ping
The first request after a quiet spell pays the ~1 minute spin-up, and the ephemeral
filesystem means the in-memory caches are **empty again** after every spin-down, restart
and deploy.

We do not run a keep-alive pinger. Render's free-tier docs say nothing about pinging either
way, but it burns the 750 shared instance hours to keep an idle service awake, it is plainly an
attempt to defeat a published limit, and the outbound-traffic rule shows Render does suspend
services it considers greedy. The warm start below handles the cache instead.

### Warm start after a spin-down
`E0` exports every cached judge answer to `app/data/app_stance_cache.jsonl` and their query
vectors to `app/data/query_cache.json`, both committed. `pipeline.warm_start()` loads them
when `server.py` is imported, once per gunicorn worker. A claim that was run in the notebook
then answers from cache on the deployed app, with no model call at all.

- **A stale file cannot give a wrong answer.** Every key is a hash of the configuration, so
  if the model, prompt version, parameters or instruction prefix change, the entries are never
  found and the app calls the model as usual. `test_a_stale_file_is_useless_but_never_wrong`
  pins this.
- **A corrupt file cannot take the app down.** A damaged file is logged and skipped; one bad
  line in the `.jsonl` does not lose the others.

Entries already in memory always win, so a live answer is never overwritten by a committed file.
The files hold judge answers and query vectors only, no key. They grow with every claim run in
the notebook; if the repository gets too large, re-export from a trimmed cache.

### Measurements
| What | Value |
|---|---|
| `/api/health`, warm | ~0.35 s |
| Deploy time, clone + `pip install` + boot | ~57 s |
| Proxy latency from Frankfurt | 0.4 s embedding, 0.9 s judge call |

Still open:

| What | Where to look |
|---|---|
| Peak memory against the 512 MB ceiling | Render dashboard → Metrics |
| Cold-start time, first request after spin-down | time a request after 15+ idle minutes |
| Warm latency per claim | `/api/analyze` with one claim, cache cold then warm |
| Instance hours used per month | dashboard → Included Usage |

`passage_emb.npy` is 696 × 2560 float32 ≈ **6.8 MB**, so the matrix is not the memory risk;
the Python process and its dependencies dominate. A claim with a cold cache makes 51 calls at
~0.9 s each, ten at a time, so roughly 5–10 s plus any spin-up. A cached claim should return in
well under a second.
