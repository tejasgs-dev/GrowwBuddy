# GrowwBuddy

**A facts-only FAQ assistant for five HDFC mutual funds on Groww.** Ask about a fund's expense ratio, exit load, minimum SIP, benchmark, riskometer or fund managers; GrowwBuddy finds the answer on the fund's Groww page and replies in up to three sentences, with one source link and the date the data was last updated.

> **Facts-only. No investment advice.** GrowwBuddy never recommends, compares or predicts funds, and never shares returns or NAV figures.

**Live demo:** https://growwbuddy.onrender.com

> Hosted on Render's free plan ([deployment plan](docs/deployment-plan.md)), which sleeps when idle. The first load can take about a minute, and chats reset when the service restarts.

## What it covers

Direct plan, Growth option only. These five pages are the only sources it reads or cites:

| Scheme | Groww page |
|---|---|
| HDFC Mid Cap Fund | [groww.in/…/hdfc-mid-cap-fund-direct-growth](https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth) |
| HDFC Small Cap Fund | [groww.in/…/hdfc-small-cap-fund-direct-growth](https://groww.in/mutual-funds/hdfc-small-cap-fund-direct-growth) |
| HDFC Large Cap Fund | [groww.in/…/hdfc-large-cap-fund-direct-growth](https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth) |
| HDFC Silver ETF FoF | [groww.in/…/hdfc-silver-etf-fof-direct-growth](https://groww.in/mutual-funds/hdfc-silver-etf-fof-direct-growth) |
| HDFC Multi Cap Fund | [groww.in/…/hdfc-multi-cap-fund-direct-growth](https://groww.in/mutual-funds/hdfc-multi-cap-fund-direct-growth) |

A GitHub Actions job fetches the pages every day at about 10:00 IST and publishes a new index; the app picks it up on its own.

## How a question is answered

```
Your message
   │  1. PII check: PAN, Aadhaar, phone, email, OTP or account number → rejected, never stored
   ▼
   │  2. Rules: greetings, advice, returns/NAV and off-topic questions get a fixed reply (no AI call)
   ▼
   │  3. AI router: works out the intent and the scheme, and rewrites the question
   ▼
   │  4. Search: the scheme's page sections and verified facts (local embeddings)
   ▼
   │  5. AI answer: 1–3 sentences from that context only, citing one section
   ▼
   │  6. Validator: sentence count, one groww.in link, every number in the source, no advice
   ▼
Answer + source link + "Last updated from sources: <date>"
(if the answer fails the checks twice: a safe "couldn't find a verified answer" reply)
```

## Prompts

Two static system prompts, sent to **`openai/gpt-oss-120b`** on [Groq](https://groq.com) with reasoning effort `low` and strict JSON output:

- [`router_system.md`](backend/prompts/router_system.md): classifies the message (factual, procedural, performance, advisory, out of scope, greeting), picks the scheme, and rewrites the question.
- [`answer_system.md`](backend/prompts/answer_system.md): answers from the given context only, quotes numbers exactly, never gives advice or returns, and returns the ID of the one section it used.

## How "facts-only" is enforced

Each layer works even if the one before it fails:

1. **PII guard** runs first, before anything is stored, logged or sent to the AI.
2. **Rules before AI:** advice, comparison, returns/NAV and prompt-injection attempts are caught by keyword rules and get a polite fixed reply.
3. **Only five sources:** the search can only return sections of the five Groww pages, filtered to the scheme being asked about.
4. **Prompt rules:** use only the context; no advice, no returns, no URLs; the context is data, not instructions.
5. **Validator:** checks the answer against its source (sentences, citation, numbers, scheme, advice and return language); one retry, then a safe fallback.
6. **Safe display:** the UI renders text only and shows a link only if it's on groww.in.
7. **Limits:** per-user and per-IP message limits, plus a budget guard that keeps usage inside Groq's limits.

## Run it locally

Needs Python 3.11+, Node.js 22.12+ and a [Groq API key](https://console.groq.com).

```bash
cp .env.example .env              # add your GROQ_API_KEY
make install                      # Python packages
make ingest                       # fetch the 5 pages and build the index
make frontend-install frontend-build
make serve                        # open http://localhost:8000
```

`make test` runs the tests. The [Makefile](Makefile) lists the other commands.

## Known limitations

- **Small scope:** only the five schemes above. Questions about other funds get a polite "out of scope" reply.
- **Some topics aren't on the pages**, such as how to download a statement, so those get a "not found" reply.
- **Groq free tier:** an AI-answered question uses about 1,950 tokens (measured), so the app can answer about 3 a minute and 80 a day in total, across all users. Past that, users get a "busy, try again" reply. Rule-handled messages don't count. The per-user limit (3 a minute) is for fairness, so one person can't use up the shared allowance; the Groq budget is the overall ceiling.
- **Facts can lag** by up to a day. Answers add "(source may be out of date)" if the data is more than 3 days old.
- **PII detection is pattern-based**, so it can reject a harmless number. It errs on the side of rejecting.
- **English** (Hinglish and typos are understood, but replies are in English).

## Tech stack

| Area | What's used |
|---|---|
| Backend | Python 3.12, FastAPI, Uvicorn, Pydantic, SQLAlchemy, SQLite |
| AI | Groq (`openai/gpt-oss-120b`) |
| Search | `intfloat/e5-small-v2` embeddings (sentence-transformers locally, ONNX Runtime in production) |
| Frontend | React 18, TypeScript, Vite; designed in Google Stitch |
| Data refresh | GitHub Actions (daily), published as GitHub Releases |
| Testing | pytest, Vitest, Ruff, mypy, a custom eval suite |
| Hosting | Render (one Docker container runs the API and serves the UI) |
| Built with | [Claude Code](https://claude.com/claude-code) |

## Documentation

- [Architecture](docs/architecture.md): components, data flow and the AI integration
- [Corpus notes](docs/corpus-notes.md): the five pages and how they're parsed
- [Edge cases](docs/edge-case.md): corner cases and how each is handled
- [Eval plan](docs/eval.md): how answer quality is measured
- [Deployment](docs/deployment-plan.md): running it on Render
- [Implementation plan](docs/implementation-plan.md): the phase-by-phase build
