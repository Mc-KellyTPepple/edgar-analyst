# EDGAR Analyst

A research agent that answers questions about public company filings (SEC 10-Ks),
**and an evaluation suite that grades its answers automatically**.

> **Status: week 1 of 6, evaluation harness added.** The data pipeline has run on live SEC data for 10 companies,
> producing 164 evaluation questions (`evals/questions.jsonl`). Eight generated answers
> were spot-checked against the companies' reported figures and all matched. The agent,
> the results and the deployment are still to come, and no accuracy numbers are claimed yet.

## The idea

Most agent demos can't say how often they are right. Here the ground truth comes
from the SEC's structured XBRL data, so about two thirds of the evaluation questions
are generated with a known, machine-checkable answer, and every answer points to the
filing it came from.

Question tiers (`evals/questions.jsonl`):

| Tier | Type | Example |
|---|---|---|
| 1 | Lookup | "What was X's net income for the fiscal year ended …? (USD millions)" |
| 2 | Multi-step | Year-over-year growth, R&D as a percentage of revenue, margins |
| 3 | Comparison | "Which had the higher R&D intensity, X or Y?" / "Did X's margin increase or decrease between …?" |
| 0 | Abstain | Future periods and companies outside the corpus: the right answer is "I can't answer that" |

The same seed always produces the same questions. `edgar_analyst/grading.py` scores
free-text answers: dollar amounts within 0.5%, percentages within 0.15 points,
choices by ticker or company name, and refusals on unanswerable questions.

## What exists today

- `sec_client.py`: SEC EDGAR client that follows the fair-access policy (declared contact,
  under 10 requests per second, retries, on-disk cache).
- `facts.py`: annual figures from XBRL "company facts" (10-K only, 52/53-week years,
  latest restatement wins, tag fallbacks across years).
- `db.py`: SQLite facts store, plus `run_readonly_query`, the hardened SQL entry point the
  agent will use: read-only connection, SELECT-only authorizer, one statement, row cap, time limit.
- `questions.py` and `grading.py`: the evaluation set and its grader.
- `llm.py`, `agent.py`, `evalrun.py`: the Groq client, the two answer systems, and the resumable runner, grader and report.
- 60 unit tests on synthetic, SEC-shaped data (including attempted SQL writes, runaway queries, rate limits, quota fallback and cache replay, with a fake Groq transport).

Known gaps: the corpus is 10 companies for now, and companies that don't tag R&D expense the
standard way (for example Amazon) have no R&D-based questions.

## Running on Groq's free tier

The model calls use [Groq](https://console.groq.com) and the code is built around its free-tier limits
(requests and tokens per minute, requests per day, per model):

- **Rate limiter**: a sliding 60-second window over requests and tokens per model, at 90% of the limit,
  and `retry-after` is honoured on 429s.
- **Fallback chain**: when one model's daily quota is gone the next takes over
  (default `openai/gpt-oss-120b`, then `openai/gpt-oss-20b`; models the account cannot use are skipped; override with `--models` or `EDGAR_MODELS`).
- **Cache and replay**: every response is stored under `evals/llm_cache/` keyed by the exact request, so
  re-running costs nothing, and `--mode replay` forbids network calls entirely (deterministic CI).
- **Daily ledger** (`evals/groq_ledger.json`): request counts per model and day, committed with the results.
- **Resumable runs**: `--max-requests N` caps a run; finished questions are skipped, so the full
  set can be completed over a few days.
- **Small prompts**: the model reads dollar figures from a `value_musd` column and does arithmetic with a
  calculator tool, since tokens per minute is the binding limit and models are unreliable at 12-digit maths.

Two systems are compared: `closed_book` (the model answers from memory) and `facts_agent`
(SQL over the facts DB plus a calculator, citing accession numbers). Per question the run records
correctness, refusal behaviour, citation recall, tool calls, tokens, latency and which model answered.

The limits in `llm.py` come from a third-party summary; check `console.groq.com/settings/limits` and adjust `DEFAULT_LIMITS`.

```bash
export GROQ_API_KEY=...                                   # never commit this
PYTHONPATH=src python -m edgar_analyst run-eval --system closed_book --max-requests 200
PYTHONPATH=src python -m edgar_analyst run-eval --system facts_agent --max-requests 400
PYTHONPATH=src python -m edgar_analyst report --gate-system facts_agent --min-accuracy 60
```

Or run the **Evaluate on Groq** workflow in GitHub Actions (needs the `GROQ_API_KEY` secret).

## Run it

```bash
python -m unittest discover -s tests -t . -v        # tests, no network needed
export SEC_USER_AGENT="Your Name you@example.com"    # the SEC requires a declared contact
make ingest db questions                             # download data, build the DB, write the questions
```

Or run the **Ingest SEC data** workflow in GitHub Actions (set the `SEC_CONTACT`
repository variable to your email first).

## Roadmap

- [x] Week 1: data pipeline (verified on live SEC data), facts DB, question generator and grader
- [ ] Week 1-2: filing text sections and hybrid retrieval (vector plus keyword, reranking)
- [x] Groq client and evaluation harness (facts-only agent: SQL plus calculator), awaiting first run
- [ ] Week 2: add the third tool (text search over filing sections) to the agent
- [ ] Week 3: full evaluation runs: plain RAG vs RAG plus tools vs full agent; quality gate in CI
- [ ] Week 4: tracing and cost dashboard; Docker; AWS deployment with Terraform
- [ ] Week 5-6: load test, write-up with failure cases, demo video

## Results

To be filled in after the evaluation runs. This section will hold the measured comparison
(accuracy by tier, citation correctness, refusal rate, cost per question, p95 latency).

*Not investment advice. Figures come from public SEC filings and may contain errors.*
