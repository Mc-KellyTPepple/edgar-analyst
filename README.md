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
| 4 | Text only | "How many employees did X report in its 10-K for fiscal year …?" (the answer exists only in the filing text; the key is the figure the filing states, found by pattern, with the supporting quote and chunk kept) |
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
- `filings.py`, `textdb.py`, `textq.py`: 10-K HTML to Item 1 / 1A / 7 chunks (whole-text fallback for non-standard layouts), hybrid
  search (SQLite FTS5 BM25 + local `bge-small` embeddings via fastembed, fused with reciprocal rank fusion), and tier 4 questions
  generated from the filing text itself.
- `llm.py`, `agent.py`, `evalrun.py`: the Groq client, the two answer systems, and the resumable runner, grader and report.
- 80 unit tests on synthetic, SEC-shaped data (including attempted SQL writes, runaway queries, rate limits, quota fallback and cache replay, with a fake Groq transport).

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

## Deploy for free

No paid plan is needed. The service (`python -m edgar_analyst serve`, standard library HTTP) runs in the `Dockerfile` on a free
[Hugging Face Space](https://huggingface.co/new-space) (Docker SDK, CPU, 16 GB RAM, enough for the local search models) and uses your free Groq key.
It protects that quota: 10 questions per visitor per hour, 150 per day for the whole service, a 500-character question limit, and cached
answers cost nothing. When the quota is gone it says so instead of failing.

1. Run the **Build deploy data** workflow once (needs the `SEC_CONTACT` variable). It commits `deploy/data/facts.db` and `text.db` (about 20 MB).
2. Create a Space (Docker, blank) and add a secret `GROQ_API_KEY` under Settings, Variables and secrets.
3. Upload the files to the Space: in the Space, Files, Add file, Upload files, and drag in the contents of the Space bundle
   (`Dockerfile`, `pyproject.toml`, `README.md` taken from `deploy/hf/README.md`, `src/`, `deploy/data/`). The web upload handles the
   20 MB database through Git LFS automatically; a plain `git push` of it is rejected unless you run `git lfs track "*.db"` first.
The Space builds the image (a few minutes, mostly model downloads) and serves the UI at its URL; `POST /ask` with `{"question": "..."}` is the API.
Free Spaces sleep after about 48 hours without visitors and wake on the next request.
The Docker image and server were not built or run on a real host from the development sandbox (no network there); the server code is covered by tests.

## Roadmap

- [x] Week 1: data pipeline (verified on live SEC data), facts DB, question generator and grader
- [x] Week 1-2: filing text sections and hybrid retrieval (BM25 plus local embeddings, rank fusion); reranking still to do
- [x] Groq client and evaluation harness (facts-only agent: SQL plus calculator), awaiting first run
- [x] Week 2: third tool (text search over filing sections) added to the agent (`full_agent`)
- [x] Week 3: full evaluation runs (closed book vs facts agent vs full agent) and a quality gate in CI (on stored results)
- [ ] Next: a clean single-version 120b-only rerun of all tiers, then reranking and broader text questions
- [x] Week 4 (part): Docker image and HTTP service built for free hosting (Hugging Face Space); deployment steps below
- [ ] Week 4: tracing and cost dashboard
- [ ] Week 5-6: load test, write-up with failure cases, demo video

## Results

Full run of all 187 questions per system (164 structured over 10 companies, plus 23 text-only questions), `temperature` 0,
graded automatically. Raw responses are in `evals/results/`; the first-run results with the old prompts are archived in
`evals/results/archive/`.

| system | tier 1 lookup | tier 2 multi-step | tier 3 comparison | tier 4 filing text | answerable (1-3) | correct refusals (tier 0) | false refusals | citation recall | citation precision | retrieval hit |
|---|---|---|---|---|---|---|---|---|---|---|
| closed book (model memory) | 10.0% | 8.3% | 40.0% | - | 17.3% | 71.4% | 37.3% | n/a | n/a | n/a |
| facts agent (ratio + lookup + SQL + calculator) | 100% | 100% | 100% | 0.0% | 100% | 100% | 0% | 100% | 100% | n/a |
| full agent (+ filing search + reranker) | 100% | 100% | 100% | 87.0% | 100% | 100% | 0% | 99.8% | 100% | 100% |

What the numbers say:

- **Data access is the difference.** Memory alone gets about 1 in 6 figure questions right and invents an answer for 29% of the
  unanswerable ones. With the facts tools every lookup and multi-step calculation is right and every unanswerable question is declined.
- **Citations are fixed.** Recall was 48% before the `lookup_fact` tool returned accession numbers to the model; now 94-96% with
  no invented sources (precision 100%).
- **The facts agent scores 0% on tier 4 by design.** It has no way to read filing text, and it declines every one of those questions,
  which is the right behaviour for it and a control for the full agent.
- **Filing search works.** The full agent answers 82.6% of the text-only questions, and the gold passage is among the
  retrieved results 91.3% of the time (the misses are refusals or rounding, e.g. 1,500,000 for Amazon's 1,525,000).
- **A prompt fix removed most premature refusals.** The first full run scored the full agent 82.0% on tiers 1-3 because it answered
  "I cannot answer" on 18% of answerable questions, often without a single lookup. Re-running only those 41 questions with a
  prompt that requires a lookup before any refusal (v3) raised it to 92.7% and cut false refusals to 3.3%. The re-run answers are tagged
  `prompt_version: v3` in the results; the original answers are in `evals/results/archive/*_false_refusals_v2.jsonl`. Only the
  previously declined questions were re-run, so the rest of each run used the v2 prompt.
- **Comparison questions (tier 3) went from 85.0% / 72.5% to 95.0% / 95.0%** (facts / full agent). Process: read every failing answer
  and its tool trace, group the causes, fix each at the right layer, re-run only the affected questions.
  Causes found: (1) wrong fiscal year chosen when companies' years end on different dates, (2) several lookups followed by a refusal or an
  empty reply, (3) arithmetic spread over many model steps, (4) a hidden API problem: the model passed `year` as an integer, the tool schema
  said string, and Groq rejected the whole request as `tool_use_failed`. Fixes: a `financial_ratio(ticker, ratio, as_of)` tool that picks each
  company's nearest fiscal year, divides, and returns the accessions; recovery from empty replies; a permissive schema; a clearer
  out-of-steps instruction; tool-call traces stored with every answer so failures can be diagnosed.
- **Filing text: 82.6% to 87.0%** (20 of 23) after adding a local cross-encoder reranker to the search, a grader fix (it was reading an accession
  number as the answer), and a prompt telling the model to use the figure matching the question's exact wording without rounding. The right passage is now
  retrieved for every question (100%, was 91%). The 3 remaining misses are the model rounding or choosing a wrong number from a retrieved passage
  (Amazon 1,540,000 for 1,576,000 and for 1,556,000; Google 190,000 for 183,323), so the next lever is answer extraction, not search.
- Tiers 1-3 are now 100% on the tuned set after several fix rounds, which is why the held-out results below matter more.

**Held-out check.** The 187 questions above were used to find and fix failures, so their scores are tuned-on numbers. To measure
generalisation, a second set was generated with a different seed and every question that also appears in the main set removed.
Held-out set 1 (66 answerable questions: 16 lookups, 29 ratios/growth, 21 comparisons; no refusal questions) was run once on the then-current agents:

| system | lookup | multi-step | comparison | all 66 |
|---|---|---|---|---|
| facts agent | 100% | 86.2% | 100% | 93.9% (62/66) |
| full agent | 100% | 100% | 100% | 100% (66/66) |

So the tuned-set scores overstate generalisation a little for the facts agent. Its 4 misses: two requests rejected by the API after the model
asked for data the final digest step did not have, one ratio computed by hand from a half-remembered revenue figure with an invented source,
and one growth question answered with a year. Fixes: a `financial_growth` tool, lookups that accept the nearest fiscal year and an optional
period, clearer tool selection. Because set 1 was then used for fixing, it no longer counts as held-out. A fresh set 2 (seed 12, 89 questions, excluding all earlier ones)
was run once on the fixed agents (prompt v6, no errors):

| system | lookup (35) | multi-step (33) | comparison (21) | all 89 | citation recall | citation precision |
|---|---|---|---|---|---|---|
| facts agent | 100% | 100% | 100% | **100% (89/89)** | 100% | 100% |
| full agent | 100% | 100% | 100% | **100% (89/89)** | 98.3% | 100% |

The fixes generalised: the facts agent went from 93.9% on set 1 to 100% on a set it had never seen, and the full agent stayed at 100%.
Limits: 89 questions still means the true rate could be a few points lower (100% on 89 is consistent with roughly 96-100%); both held-out sets use the same
question templates and the same 10 companies as the tuned set, so this shows the agents handle new instances of these question types, not new
kinds of question; they contain no refusal or filing-text questions; and about a third of the full agent's answers came from the smaller `gpt-oss-20b`
model. Refusals and filing-text questions are only covered by the tuned set (100% refusals, 82.6% filing text).

Caveats: single run, no confidence intervals; every question now has an answer (no errors). The last 3 that Groq kept rejecting were completed by resampling malformed tool calls and raising the step budget from 6 to 8, so a few answers used a slightly different setup than the rest. Questions about figures come from the same database the agent queries, so tiers 1-3
show correct tool use, not the ability to read filings; tier 4 is the step toward that but covers one question type (employee counts),
and broader text questions would need a model-based grader. Answers come from `gpt-oss-120b`, with `gpt-oss-20b` taking over when the
daily quota ran out. Tiers 1-2 and the text questions come from earlier prompt versions (`prompt_version` in each record: v2, v3, v4);
only tier 3 and the previously refused questions were re-run with the newest one, so a clean single-version, 120b-only run could differ. The report has a per-model table. Retrieval is not reranked, and Intel's 10-K is indexed as unsectioned text.

The CI workflow replays these stored results through a quality gate (facts agent: at least 85% answerable accuracy and 95% correct
refusals), so a change that breaks the recorded numbers fails the build. It does not call the API.

*Not investment advice. Figures come from public SEC filings and may contain errors.*
