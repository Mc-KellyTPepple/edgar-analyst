# EDGAR Analyst

A research agent that answers questions about public company filings (SEC 10-Ks),
**and an evaluation suite that grades its answers automatically**.

> **Status: week 1 of 6.** Data pipeline, facts database and the evaluation
> question generator are built and tested. The agent, the results and the
> deployment are still to come, and no accuracy numbers are claimed yet.

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
- 30 unit tests on synthetic, SEC-shaped data (including attempted SQL writes and runaway queries).

## Run it

```bash
python -m unittest discover -s tests -t . -v        # tests, no network needed
export SEC_USER_AGENT="Your Name you@example.com"    # the SEC requires a declared contact
make ingest db questions                             # download data, build the DB, write the questions
```

Or run the **Ingest SEC data** workflow in GitHub Actions (set the `SEC_CONTACT`
repository variable to your email first).

## Roadmap

- [x] Week 1: data pipeline, facts DB, question generator and grader
- [ ] Week 1-2: filing text sections and hybrid retrieval (vector plus keyword, reranking)
- [ ] Week 2: agent with three tools (text search, read-only SQL over facts, calculator)
- [ ] Week 3: full evaluation runs: plain RAG vs RAG plus tools vs full agent; quality gate in CI
- [ ] Week 4: tracing and cost dashboard; Docker; AWS deployment with Terraform
- [ ] Week 5-6: load test, write-up with failure cases, demo video

## Results

To be filled in after the evaluation runs. This section will hold the measured comparison
(accuracy by tier, citation correctness, refusal rate, cost per question, p95 latency).

*Not investment advice. Figures come from public SEC filings and may contain errors.*
