"""Command line: ingest SEC data, build the facts database, generate questions."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import config
from .db import build_db, open_readonly
from .facts import extract_10k_filings, extract_annual_facts
from .questions import generate, summarize, write_jsonl
from .sec_client import SecClient


def cmd_ingest(args) -> None:
    client = SecClient(config.user_agent(), config.RAW_DIR / "cache")
    tickers = client.tickers()
    chosen = []
    for t in args.tickers:
        if t.upper() not in tickers:
            sys.exit(f"Unknown ticker: {t}")
        cik, name = tickers[t.upper()]
        chosen.append({"ticker": t.upper(), "cik": cik, "name": name})
        (config.RAW_DIR / "facts").mkdir(parents=True, exist_ok=True)
        (config.RAW_DIR / "submissions").mkdir(parents=True, exist_ok=True)
        (config.RAW_DIR / "facts" / f"{cik}.json").write_text(json.dumps(client.company_facts(cik)))
        (config.RAW_DIR / "submissions" / f"{cik}.json").write_text(json.dumps(client.submissions(cik)))
        print(f"fetched {t.upper()} ({name})")
    (config.RAW_DIR / "companies.json").write_text(json.dumps(chosen, indent=2))


def cmd_build_db(args) -> None:
    companies = json.loads((config.RAW_DIR / "companies.json").read_text())
    facts, filings, rows = {}, {}, []
    for c in companies:
        cf = json.loads((config.RAW_DIR / "facts" / f"{c['cik']}.json").read_text())
        sub = json.loads((config.RAW_DIR / "submissions" / f"{c['cik']}.json").read_text())
        facts[c["cik"]] = extract_annual_facts(cf)
        filings[c["cik"]] = extract_10k_filings(sub)
        rows.append((c["cik"], c["ticker"], c["name"]))
        print(f"{c['ticker']}: {len(facts[c['cik']])} annual facts, {len(filings[c['cik']])} 10-K filings")
    build_db(config.DB_PATH, rows, facts, filings)
    print(f"wrote {config.DB_PATH}")


def cmd_gen_questions(args) -> None:
    conn = open_readonly(config.DB_PATH)
    sizes = [int(x) for x in args.sizes.split(",")] if args.sizes else None
    kw = dict(zip(("n_t1", "n_t2", "n_t3", "n_abstain"), sizes)) if sizes else {}
    qs = generate(conn, seed=args.seed, **kw)
    if args.exclude:
        from .evalrun import load_questions

        seen = {q["id"] for q in load_questions(*[Path(x) for x in args.exclude])}
        qs = [q for q in qs if q["id"] not in seen]
    out = Path(args.out) if args.out else config.QUESTIONS_PATH
    write_jsonl(qs, out)
    print(summarize(qs))
    print(f"wrote {out}")


def cmd_serve(args) -> None:
    from .llm import LLM
    from .server import App, Limits, serve
    from .textdb import TextIndex, try_embedder, try_reranker

    llm = LLM(os.environ.get("GROQ_API_KEY"), models=_models(args.models), cache_dir=config.LLM_CACHE_DIR,
              ledger_path=Path(os.environ.get("LEDGER_PATH", "/tmp/groq_ledger.json")),
              max_requests=None)
    conn = open_readonly(config.DB_PATH)
    index = TextIndex(config.TEXT_DB_PATH, None if args.no_dense else try_embedder(), None if args.no_rerank else try_reranker())
    print(f"retrieval mode: {index.mode}, rerank: {'yes' if index.reranker else 'no'}", flush=True)
    search = lambda q, ticker=None, year=None: index.search(q, k=4, ticker=ticker, year=year)  # noqa: E731
    serve(App(llm, conn, search, Limits(args.per_ip, args.daily_cap), mode=index.mode + ("+rerank" if index.reranker else "")))


def _suffix(qset: str) -> str:
    return "" if qset == "main" else f"_{qset}"


def _set_path(qset: str):
    return {"heldout": config.HELDOUT_QUESTIONS_PATH, "heldout2": config.HELDOUT2_QUESTIONS_PATH}[qset]


def cmd_ingest_text(args) -> None:
    from .filings import filing_chunks
    from .textdb import build_text_db, try_embedder

    client = SecClient(config.user_agent(), config.RAW_DIR / "cache")
    companies = json.loads((config.RAW_DIR / "companies.json").read_text())
    chunks, problems = [], []
    for c in companies:
        sub = json.loads((config.RAW_DIR / "submissions" / f"{c['cik']}.json").read_text())
        filings = sorted(extract_10k_filings(sub), key=lambda f: f["filed"], reverse=True)[: args.per_company]
        for f in filings:
            html = client.filing_document(c["cik"], f["accession"], f["primary_doc"])
            ch, sizes = filing_chunks(html, c["ticker"], f["accession"], f["report_date"])
            chunks += ch
            print(f"{c['ticker']} {f['report_date']}: {len(html) // 1024} KB html, sections {sizes}, {len(ch)} chunks")
            if not {"1", "1A", "7"} <= set(sizes):
                problems.append(f"{c['ticker']} {f['report_date']} missing {sorted({'1', '1A', '7'} - set(sizes))}")
    embedder = None if args.no_dense else try_embedder()
    build_text_db(config.TEXT_DB_PATH, chunks, embedder)
    print(f"wrote {config.TEXT_DB_PATH}: {len(chunks)} chunks, dense={'yes' if embedder else 'no'}")
    if problems:
        print("sections not found (parser may need work):\n  " + "\n  ".join(problems))


def cmd_gen_text_questions(args) -> None:
    import sqlite3

    from .questions import write_jsonl
    from .textq import generate_text_questions

    companies = json.loads((config.RAW_DIR / "companies.json").read_text())
    names = {c["ticker"]: c["name"] for c in companies}
    conn = sqlite3.connect(f"file:{config.TEXT_DB_PATH}?mode=ro", uri=True)
    qs = generate_text_questions(conn, names)
    write_jsonl(qs, config.TEXT_QUESTIONS_PATH)
    print(f"wrote {len(qs)} text questions to {config.TEXT_QUESTIONS_PATH}")
    from .textq import unmatched_examples

    matched = {q["evidence"][0]["accession"] for q in qs}
    for label, examples in unmatched_examples(conn, matched).items():
        print(f"no employee-count match for {label}; candidate sentences: {examples}")


def _models(arg: str | None) -> list[str] | None:
    raw = arg or os.environ.get("EDGAR_MODELS", "")
    return [m.strip() for m in raw.split(",") if m.strip()] or None


def cmd_run_eval(args) -> None:
    from .evalrun import load_questions, run_eval
    from .llm import LLM, LLMError

    try:
        llm = LLM(os.environ.get("GROQ_API_KEY"), models=_models(args.models), cache_dir=config.LLM_CACHE_DIR,
                  mode=args.mode, ledger_path=config.LEDGER_PATH, max_requests=args.max_requests)
    except LLMError as exc:
        sys.exit(str(exc))
    conn = open_readonly(config.DB_PATH)
    extras = {}
    if args.system == "full_agent":
        from .textdb import TextIndex, try_embedder, try_reranker

        index = TextIndex(config.TEXT_DB_PATH, None if args.no_dense else try_embedder(),
                          None if args.no_rerank else try_reranker())
        print(f"retrieval mode: {index.mode}, rerank: {'yes' if index.reranker else 'no'}")
        extras["search"] = lambda q, ticker=None, year=None: index.search(q, k=4, ticker=ticker, year=year)
    tiers = {int(t) for t in args.tiers.split(",")} if args.tiers else None
    out = config.RESULTS_DIR / f"{args.system}{_suffix(args.set)}.jsonl"
    if args.redo_tiers or args.redo_ids:
        from .evalrun import drop_tier_results

        redo = {int(t) for t in (args.redo_tiers or "").split(",") if t}
        ids = {i for i in (args.redo_ids or "").split(",") if i}
        n = drop_tier_results(out, config.RESULTS_DIR / "archive" / f"{args.system}_redo_prev.jsonl", redo, ids)
        print(f"re-running tiers {sorted(redo)} ids {sorted(ids)}: {n} earlier answers archived")
    if args.redo_refusals:
        from .evalrun import drop_false_refusals

        # the facts agent has no way to read filing text, so declining tier 4 is correct for it
        redo_tiers = {1, 2, 3} if args.system == "facts_agent" else {1, 2, 3, 4}
        n = drop_false_refusals(out, config.RESULTS_DIR / "archive" / f"{args.system}_false_refusals_v2.jsonl", redo_tiers)
        print(f"re-running {n} wrongly declined questions (old answers archived)")
    questions = (load_questions(_set_path(args.set)) if args.set != "main"
                 else load_questions(config.QUESTIONS_PATH, config.TEXT_QUESTIONS_PATH))
    summary = run_eval(questions, args.system, llm, conn, out,
                       tiers=tiers, limit=args.limit, **extras)
    u = llm.usage
    print(f"{args.system}: attempted {summary['attempted']}, remaining {summary['remaining']}; "
          f"{u.requests} network requests, {u.cache_hits} cache hits, {u.retries} retries, "
          f"{u.prompt_tokens + u.completion_tokens} tokens")
    if summary["stopped"]:
        print(f"stopped early: {summary['stopped']} (re-run later; finished questions are skipped)")


def cmd_pending(args) -> None:
    """Print how many (system, question) pairs are still unanswered, so a scheduled run can skip itself when finished."""
    from .evalrun import load_questions, load_results

    total = 0
    for qset in ("main", "heldout", "heldout2"):
        paths = (config.QUESTIONS_PATH, config.TEXT_QUESTIONS_PATH) if qset == "main" else (_set_path(qset),)
        qids = {q["id"] for q in load_questions(*paths)}
        for system in args.systems:
            done = {k for k, v in load_results(config.RESULTS_DIR / f"{system}{_suffix(qset)}.jsonl").items() if not v.get("error")}
            left = len(qids - done)
            if qids:
                print(f"{system} [{qset}]: {left} of {len(qids)} questions left", file=sys.stderr)
            total += left
    print(total)


def cmd_report(args) -> None:
    from .agent import SYSTEMS
    from .evalrun import check_gate, load_results, markdown_table, summarize_results

    summaries = {}
    suffix = _suffix(args.set)
    for name in SYSTEMS:
        rows = list(load_results(config.RESULTS_DIR / f"{name}{suffix}.jsonl").values())
        if rows:
            summaries[name] = summarize_results(rows)
    if not summaries:
        sys.exit("no results yet: run `run-eval` first")
    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (config.RESULTS_DIR / f"summary{suffix}.json").write_text(json.dumps(summaries, indent=2, sort_keys=True))
    table = markdown_table(summaries)
    (config.RESULTS_DIR / f"RESULTS{suffix}.md").write_text(table + "\n")
    print(table)
    if args.gate_system:
        problems = check_gate(summaries.get(args.gate_system, {}), args.min_accuracy, args.min_refusal)
        for pr in problems:
            print(f"GATE FAILED: {pr}")
        if problems:
            sys.exit(1)
        print("gate passed")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="edgar_analyst")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ingest", help="download company facts and filing lists from SEC EDGAR")
    a.add_argument("--tickers", nargs="+", default=config.DEFAULT_TICKERS)
    a.set_defaults(fn=cmd_ingest)
    b = sub.add_parser("build-db", help="build data/facts.db from downloaded data")
    b.set_defaults(fn=cmd_build_db)
    c = sub.add_parser("gen-questions", help="generate evals/questions.jsonl")
    c.add_argument("--seed", type=int, default=7)
    c.add_argument("--out", help="output file (default evals/questions.jsonl)")
    c.add_argument("--exclude", nargs="+", help="drop questions whose id is already in these files (for a held-out set)")
    c.add_argument("--sizes", help="tier sizes n_t1,n_t2,n_t3,n_abstain")
    c.set_defaults(fn=cmd_gen_questions)
    from .agent import SYSTEMS
    f = sub.add_parser("ingest-text", help="download 10-K documents and build data/text.db (BM25 + embeddings)")
    f.add_argument("--per-company", type=int, default=3, help="most recent 10-Ks per company")
    f.add_argument("--no-dense", action="store_true")
    f.set_defaults(fn=cmd_ingest_text)
    g = sub.add_parser("gen-text-questions", help="generate evals/text_questions.jsonl from the text database")
    g.set_defaults(fn=cmd_gen_text_questions)
    d = sub.add_parser("run-eval", help="run a system over the questions with Groq (resumable)")
    d.add_argument("--system", choices=sorted(SYSTEMS), default="facts_agent")
    d.add_argument("--models", help="comma-separated Groq models, first is preferred (or EDGAR_MODELS)")
    d.add_argument("--max-requests", type=int, default=None, help="network requests allowed in this run")
    d.add_argument("--tiers", help="e.g. 1,2 (default: all)")
    d.add_argument("--limit", type=int, default=None, help="questions to attempt in this run")
    d.add_argument("--set", choices=["main", "heldout", "heldout2"], default="main", help="question set; heldout sets were not used for tuning when first run")
    d.add_argument("--redo-tiers", help="archive and re-run these tiers, e.g. 3")
    d.add_argument("--redo-ids", help="archive and re-run these question ids (comma separated)")
    d.add_argument("--redo-refusals", action="store_true", help="re-run only answerable questions that were wrongly declined")
    d.add_argument("--no-rerank", action="store_true", help="full_agent: skip the cross-encoder reranker")
    d.add_argument("--no-dense", action="store_true", help="full_agent: BM25 only, skip local embeddings")
    d.add_argument("--mode", choices=["live", "replay"], default="live", help="replay never touches the network")
    d.set_defaults(fn=cmd_run_eval)
    v = sub.add_parser("serve", help="HTTP API and one-page UI (set GROQ_API_KEY)")
    v.add_argument("--models")
    v.add_argument("--per-ip", type=int, default=10, help="questions per visitor per hour")
    v.add_argument("--daily-cap", type=int, default=150, help="questions per day for the whole service")
    v.add_argument("--no-dense", action="store_true")
    v.add_argument("--no-rerank", action="store_true")
    v.set_defaults(fn=cmd_serve)
    h = sub.add_parser("pending", help="count unanswered (system, question) pairs")
    h.add_argument("--systems", nargs="+", default=["full_agent", "facts_agent"])
    h.set_defaults(fn=cmd_pending)
    e = sub.add_parser("report", help="summarize results; optionally enforce a quality gate")
    e.add_argument("--set", choices=["main", "heldout", "heldout2"], default="main")
    e.add_argument("--gate-system")
    e.add_argument("--min-accuracy", type=float, default=60.0)
    e.add_argument("--min-refusal", type=float, default=None)
    e.set_defaults(fn=cmd_report)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
