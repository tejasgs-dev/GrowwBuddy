# Deployment Plan — Render

Facts-only. No investment advice.

How to run GrowwBuddy on [Render](https://render.com) as one web service, **free for as long
as possible**. This completes task 8.6 (single container image) and closes the "Hosting
target" open decision in [implementation-plan.md](implementation-plan.md).

- **Default:** Render's **free** web service, no persistent disk. Chats reset whenever the
  service restarts, and the service sleeps when idle (see [Known limitations](#known-limitations)).
- **Upgrade path:** paid **Starter** instance with a persistent disk, so chats survive
  restarts and the service never sleeps (see [Upgrade path](#upgrade-path-starter--disk)).

Files: [`Dockerfile`](../Dockerfile), [`.dockerignore`](../.dockerignore),
[`render.yaml`](../render.yaml).

---

## Target setup (free tier)

| Item | Choice | Why |
|---|---|---|
| Service | One Render **Web Service**, `runtime: docker`, `plan: free` | The image needs Node (UI build) and Python; a Dockerfile handles both. |
| Embedding runtime | **ONNX Runtime** (`GROWWBUDDY_EMBED_RUNTIME=onnx`), same `intfloat/e5-small-v2` model | The torch runtime needs ~570 MB after start-up, over the free tier's 512 MB. ONNX needs ~260 MB and gives the same vectors (see [Measurements](#measurements)). The image has no torch. |
| Instances / workers | 1 / 1 | Per-thread locks, rate limits and the Groq budget guard live in the process (EC-THR-08). The free tier can't scale anyway. |
| Storage | None: the container's own filesystem, lost on every restart, deploy and spin-down | The free tier has no persistent disks. |
| Index | Pulled from the latest `index-*` GitHub release at every start (`GROWWBUDDY_INDEX_REPO`), then every 60 minutes | A fresh filesystem starts empty; the pull fills it in ~2 s. The daily "Refresh data" workflow keeps publishing as before. |
| Region | `singapore` | Closest Render region to users in India. Can't be changed after creation. |
| UI | Served by FastAPI from `frontend/dist` (task 5.10) | Same origin: no CORS, no second service. |
| Deploys | `autoDeployTrigger: checksPass` on `main` | A red CI run never ships. |

### Render free tier facts (checked 14 Nov 2026)

| Fact | Source |
|---|---|
| 0.1 CPU, 512 MB RAM (plan ID `free`) | [compute-plans](https://render.com/docs/compute-plans) |
| Spins down after 15 minutes with no inbound traffic; waking takes about a minute | [free](https://render.com/docs/free) |
| 750 free instance hours per **workspace** per month; spun-down time doesn't count; when they run out, all free services are suspended until the next month | [free](https://render.com/docs/free) |
| Filesystem is ephemeral: lost on redeploy, restart or spin-down | [free](https://render.com/docs/free) |
| No persistent disks, no Shell (SSH or dashboard), no one-off jobs, no scaling | [free](https://render.com/docs/free), [disks](https://render.com/docs/disks) |
| "Render might restart a Free web service at any time"; unusually high public traffic can get it suspended | [free](https://render.com/docs/free) |
| Rollbacks work, but only to the two most recent previous deploys | [free](https://render.com/docs/free) |
| Bandwidth and build minutes count against included amounts; without a payment method, going over suspends the service or disables builds | [free](https://render.com/docs/free) |
| Render sets `PORT` (default `10000`); bind `0.0.0.0` | [environment-variables](https://render.com/docs/environment-variables), [web-services](https://render.com/docs/web-services) |
| Blueprint: `runtime: docker`, `plan: free`, `region: singapore`, `healthCheckPath`, `branch` are valid; `autoDeployTrigger` takes `commit` (default), `checksPass` or `off` (the older `autoDeploy` is deprecated) | [blueprint-spec](https://render.com/docs/blueprint-spec), [regions](https://render.com/docs/regions) |
| HTTP health check: 2xx/3xx is healthy, 5 s timeout; 60 s of failures restarts the instance | [health-checks](https://render.com/docs/health-checks) |

Not stated in the docs: whether health checks run on free instances (the Blueprint accepts
`healthCheckPath` either way); how Render's proxy treats a client-sent `X-Forwarded-For`;
plan and disk prices (the [pricing page](https://render.com/pricing) has them, not the docs).
One service awake all month is at most 744 hours, so it fits in 750 hours **only if it is the
only free service in the workspace**.

---

## Measurements

Measured 14 Nov 2026 on Windows 11, Python 3.12, by running the real server (`uvicorn
growwbuddy.main:app`, 1 worker) against a copy of `data/growwbuddy.db` and reading the
process working set (≈ Linux RSS). Docker isn't installed on that machine, so **the image
itself was not built**; the ONNX runs used a fresh venv with only `pip install -e backend`
(no torch, sentence-transformers or transformers installed), which is what the image installs.

### Memory and start-up

| | torch (sentence-transformers) | ONNX Runtime, 1 thread |
|---|---|---|
| Server after start-up (model loaded, `/healthz` ready) | **572–576 MB** | **255–264 MB** |
| Server peak during start-up | 576 MB | 293–295 MB |
| Peak of a full retrieval run (225 queries + re-embedding all 60 chunks) | 686 MB | 351 MB |
| Model load (`HF_HUB_OFFLINE=1`) | 8.0 s | 0.8–0.9 s |
| Model load with Hub checks on | 76–223 s | — |
| Cold boot on an empty database, ready with the pulled index | — | 6.8 s |
| Search, per query | 16–18 ms | 7 ms |
| Installed packages | — | 214 MB |

The torch runtime is over 512 MB before any request, so it can't run on the free tier. ONNX
leaves ~150–250 MB of headroom. Linux memory use can differ a little (allocator, ONNX
Runtime's CPU arena), so **re-check on Render** after the first deploy (dashboard → Metrics).
The 0.1 CPU will make the start-up and each search several times slower than above.

The Hub-check row is why the image bakes the model in and sets `HF_HUB_OFFLINE=1`.

### Retrieval accuracy (same index, real model, filtered by scheme)

| Check | torch | ONNX |
|---|---|---|
| Smoke set (`ingest/gates.py`, eval.md §4.2) | 7/7 in the top 3 | 7/7 in the top 3 |
| `retrieval_queries.jsonl` (180): top-1 / top-3 / top-5 | 87.2% / 95.6% / 98.3% | 87.2% / 95.6% / 98.3% |
| `retrieval_queries_heldout.jsonl` (45): top-1 / top-3 / top-5 | 75.6% / 91.1% / 97.8% | 75.6% / 91.1% / 97.8% |
| Queries with the identical top-5 order | — | 225 / 225 |
| Vector difference, 225 queries and 60 chunks | — | min cosine 0.9999999, max abs diff 2.8e-07 |
| Stored index vectors vs ONNX re-embedding | — | min cosine 0.9999999 |

The two runtimes are interchangeable: an index built with torch (the daily workflow) works
with ONNX (the server) under the same `GROWWBUDDY_EMBED_MODEL`. Both are a little below the
Phase 2 table (88.3% top-1 on the 180 set), which predates the 13 Nov expense-ratio re-ingest;
that gap is not from the runtime.

### How to repeat

- ONNX vs torch vectors: `GROWWBUDDY_RUN_MODEL_TESTS=1 python -m pytest tests/test_embed_onnx.py`
- Accuracy with either runtime: `GROWWBUDDY_EMBED_RUNTIME=onnx python -m growwbuddy.retrieval.evaluate`
  (and the same with `torch`, and with `eval/retrieval_queries_heldout.jsonl`)
- Memory in the container: `docker stats` while the container from D1 runs

---

## Phase D1 — Container image

| ID | Task | Status |
|---|---|---|
| D1.1 | `Dockerfile`: Node stage builds `frontend/dist`; Python stage installs `backend` **without** the `[torch]` extra, bakes `model.onnx` + `tokenizer.json` into `HF_HOME=/opt/hf`, sets `HF_HUB_OFFLINE=1`, `GROWWBUDDY_EMBED_RUNTIME=onnx`, `GROWWBUDDY_EMBED_THREADS=1` | Done |
| D1.2 | `.dockerignore`: no `.env`, `data/`, `.venv`, `node_modules`, eval data or tests in the build context | Done |
| D1.3 | ONNX embedder (`retrieval/embed.py`, `OnnxE5Embedder`) and settings `GROWWBUDDY_EMBED_RUNTIME` (`torch` default) and `GROWWBUDDY_EMBED_THREADS` | Done; unit tests in `tests/test_embed_onnx.py` |
| D1.4 | Dependencies: `sentence-transformers` moved to a `[torch]` extra (included in `[dev]`); `onnxruntime`, `tokenizers`, `huggingface-hub` added to the core; the refresh workflow installs `backend[torch]` | Done |
| D1.5 | Keep the install editable with the source tree at `/app` | Done. `config.py`, `llm/prompts.py` and `ingest/manifest.py` find `data/`, `prompts/` and `corpus/` relative to their own files. |
| D1.6 | Measure memory with the model loaded | Done natively (above). **Open:** repeat inside the built container. |
| D1.7 | CI job that runs `docker build` (no push) | Open. With no local Docker, this or the first Render build is the first real build of the image. |

The torch runtime stays the default everywhere else (local `make ingest`, CI, the daily
refresh), so nothing outside the container changes.

### Exit check

- [ ] `docker build .` succeeds from a clean clone (CI job or first Render build)
- [ ] The container serves the UI at `/`; `/healthz` returns `ready: true` after the index pull
- [ ] Memory inside the container stays under ~400 MB after a few questions
- [ ] The image contains no `.env` and no `data/` contents (`docker run --rm <image> ls -a /app /app/data`)
- [x] Retrieval with ONNX matches torch (smoke 7/7; 225/225 identical rankings)

---

## Phase D2 — Render service (free)

| ID | Task | Notes |
|---|---|---|
| D2.1 | Make sure an `index-*` release exists | `index-2026-11-14` exists (Latest). A cold boot pulled it in ~2 s in testing. |
| D2.2 | Render dashboard → New → **Blueprint** → this repo | Creates the service from `render.yaml`. |
| D2.3 | Enter the secrets when asked | `GROQ_API_KEY`. `GROWWBUDDY_INDEX_TOKEN`: the repo is public so it isn't required, but a read-only fine-grained token (Contents: read) is **recommended**: unauthenticated GitHub API calls are rate-limited per IP, and Render's outbound IPs are shared. Leave it empty to skip. |
| D2.4 | Check the service URL | If Render assigns something other than `growwbuddy.onrender.com`, update `GROWWBUDDY_FRONTEND_ORIGIN` in `render.yaml`. |
| D2.5 | Turn on "Auto-Deploy: after CI checks pass" | Already `autoDeployTrigger: checksPass` in `render.yaml`. |
| D2.6 | Optional: an external uptime check on `/healthz` | Sends an alert on an outage. Pinging more often than every 15 minutes also keeps the service awake (no cold starts), which uses up to ~744 of the workspace's 750 free hours a month; only do this if it's the only free service in the workspace. |

### Environment variables

| Variable | Where | Value |
|---|---|---|
| `GROQ_API_KEY` | Dashboard (secret) | Groq key |
| `GROWWBUDDY_INDEX_TOKEN` | Dashboard (secret, optional) | Read-only token |
| `GROQ_MODEL`, `GROWWBUDDY_REASONING_EFFORT` | `render.yaml` | `openai/gpt-oss-120b`, `low` |
| `GROWWBUDDY_INDEX_REPO`, `GROWWBUDDY_INDEX_POLL_MINUTES` | `render.yaml` | `tejasgs-dev/GrowwBuddy`, `60` |
| `GROWWBUDDY_FRONTEND_ORIGIN` | `render.yaml` | The service URL |
| `GROWWBUDDY_EMBED_RUNTIME`, `GROWWBUDDY_EMBED_THREADS`, `GROWWBUDDY_EMBED_MODEL`, `HF_HOME`, `HF_HUB_OFFLINE` | `Dockerfile` | `onnx`, `1`, `intfloat/e5-small-v2`, `/opt/hf`, `1` |
| `PORT` | Render | `10000` |
| Everything else | `config.py` defaults | Groq free-tier limits, per-client limits, 3-day staleness |

### Exit check

- [ ] First deploy: the log shows "embedding model loaded", then "imported index bundle index-…"; `/healthz` reports `ready: true`
- [ ] Memory on the Metrics tab stays under ~400 MB after a few questions
- [ ] The UI loads over HTTPS; disclaimer, welcome message and three example questions show
- [ ] A factual, an advisory and a PII message each get the expected reply type
- [ ] After 15+ idle minutes, the next visit wakes the service and works
- [ ] Logs contain no message text, no raw paths and no secrets

---

## Operations (free)

**Daily data.** Unchanged from Phase 7: the workflow publishes at about 10:00 IST; the
service picks it up within 60 minutes while awake, and at every wake-up.

**Rollback.**

| What went wrong | Do this |
|---|---|
| Bad code deploy | Dashboard → Events → **Rollback** (free tier: only the two most recent previous deploys), or revert the commit on `main`. |
| Bad index data | There is no Shell on the free tier, so `refresh --tag` can't be run. On GitHub, mark the bad `index-*` release as a **pre-release** (the app ignores pre-releases and drafts), then **restart** the service: the new start pulls the previous release. Undo by un-marking it. |

**Backups.** None needed: the index can always be pulled again, and chats don't persist.

**Monitoring.** `/healthz` (`ready: false` = no index: check the log for a failed pull); the
staleness marker and the "Daily data refresh failed" issue from Phase 7; Render's deploy
notifications; the optional uptime check (D2.6).

---

## Known limitations

| Limitation | Effect for users | Notes |
|---|---|---|
| **Chats reset on every restart.** No persistent disk on the free tier; restarts happen on each deploy, each spin-down, and whenever Render chooses. | Old chats disappear. Opening one shows "That chat no longer exists"; a message sent into one is put back in the box to resend in a new chat. | Upgrade path fixes this. Thread retention (30 days) is moot. |
| **Sleeps when idle.** After 15 minutes with no traffic the service spins down. | The first visit after that waits about a minute (Render's wake-up), plus the app start (seconds). | An external ping keeps it awake (D2.6), within the 750-hour limit only if it's the workspace's only free service. |
| **Rate limits and the Groq budget guard reset on restart.** They are kept in memory. | A user's daily limit starts over after a restart. | Groq's own limits still apply: past them, users get the busy reply. No cost risk on the Groq free tier. |
| **0.1 CPU.** | Slower start-up and searches than measured locally; the Groq call still dominates the answer time. | Starter has 0.5 CPU. |
| **Per-IP limits trust `X-Forwarded-For`.** With `--forwarded-allow-ips="*"` a client can send its own header and get a fresh per-IP allowance. | — | Best-effort, like per-client limits (a client can change its `client_id`). Groq's limits and the budget guard are the real cap. |
| **Possible suspension.** Free services are suspended when the workspace runs out of free hours, bandwidth or build minutes (with no payment method), or on unusually high traffic. | The site is down until the next month or until resolved. | Keep it the only free service in the workspace. |
| **No Shell.** | — | Index rollback goes through GitHub release flags (above). |

---

## Upgrade path: Starter + disk

When chats should survive restarts, or cold starts are a problem. Starter is `0.5c-512mb`
(0.5 CPU, 512 MB); the ONNX runtime fits it with the same headroom as on the free tier.

Changes in `render.yaml`:

```yaml
    plan: starter
    disk:
      name: growwbuddy-data
      mountPath: /app/data
      sizeGB: 1
```

and add `GROWWBUDDY_THREAD_RETENTION_DAYS: "30"` back to the env vars (it matters again).

What changes:

- Chats, rate-limit state on disk (none today) and the index survive restarts and deploys.
- No spin-down; no 750-hour limit.
- Render **Shell** works, so index rollback is `cd /app/backend && python -m growwbuddy.refresh --tag index-YYYY-MM-DD` (`--unpin` to undo).
- A service with a disk gets **no zero-downtime deploys** (the old instance stops before the new one starts: a few seconds down) and **can't scale past one instance**, which this app needs anyway ([disks](https://render.com/docs/disks)).
- Render snapshots the disk every 24 hours and keeps each snapshot for 7 days. A disk can grow but not shrink.
- If the disk is root-owned and the container ever drops root, check it can still write `/app/data`.
- Cost: the Starter plan plus the disk per GB-month; see [render.com/pricing](https://render.com/pricing).

If memory ever needs more room (another model, more data), the next plan is `1c-2g`
(1 CPU, 2 GB, legacy name `standard`). Torch would fit there, but there's no reason to switch back.

---

## Order of work

1. ~~D1.1–D1.5~~ (done), D1.6 native measurement (done).
2. D1.7: CI image build, or let the first Render build be the test.
3. D2.2–D2.5: create the service from the Blueprint; run the D2 exit check; re-check memory on the Metrics tab.
4. Record the service URL and the Render memory figure here; update the README (task 8.1) with the URL, a short "Deploying" section pointing here, and the free-tier limitations above.
