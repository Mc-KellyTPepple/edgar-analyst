"""Command line: ingest SEC data, build the facts database, generate questions."""
from __future__ import annotations

import argparse
import json
import os
import sys

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
    qs = generate(conn, seed=args.seed)
    write_jsonl(qs, config.QUESTIONS_PATH)
    print(summarize(qs))
    print(f"wrote {config.QUESTIONS_PATH}")


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
    tiers = {int(t) for t in args.tiers.split(",")} if args.tiers else None
    out = config.RESULTS_DIR / f"{args.system}.jsonl"
    summary = run_eval(load_questions(config.QUESTIONS_PATH), args.system, llm, conn, out, tiers=tiers, limit=args.limit)
    u = llm.usage
    print(f"{args.system}: attempted {summary['attempted']}, remaining {summary['remaining']}; "
          f"{u.requests} network requests, {u.cache_hits} cache hits, {u.retries} retries, "
          f"{u.prompt_tokens + u.completion_tokens} tokens")
    if summary["stopped"]:
        print(f"stopped early: {summary['stopped']} (re-run later; finished questions are skipped)")


def cmd_report(args) -> None:
    from .agent import SYSTEMS
    from .evalrun import check_gate, load_results, markdown_table, summarize_results

    summaries = {}
    for name in SYSTEMS:
        rows = list(load_results(config.RESULTS_DIR / f"{name}.jsonl").values())
        if rows:
            summaries[name] = summarize_results(rows)
    if not summaries:
        sys.exit("no results yet: run `run-eval` first")
    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (config.RESULTS_DIR / "summary.json").write_text(json.dumps(summaries, indent=2, sort_keys=True))
    table = markdown_table(summaries)
    (config.RESULTS_DIR / "RESULTS.md").write_text(table + "\n")
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
    c.set_defaults(fn=cmd_gen_questions)
    from .agent import SYSTEMS
    d = sub.add_parser("run-eval", help="run a system over the questions with Groq (resumable)")
    d.add_argument("--system", choices=sorted(SYSTEMS), default="facts_agent")
    d.add_argument("--models", help="comma-separated Groq models, first is preferred (or EDGAR_MODELS)")
    d.add_argument("--max-requests", type=int, default=None, help="network requests allowed in this run")
    d.add_argument("--tiers", help="e.g. 1,2 (default: all)")
    d.add_argument("--limit", type=int, default=None, help="questions to attempt in this run")
    d.add_argument("--mode", choices=["live", "replay"], default="live", help="replay never touches the network")
    d.set_defaults(fn=cmd_run_eval)
    e = sub.add_parser("report", help="summarize results; optionally enforce a quality gate")
    e.add_argument("--gate-system")
    e.add_argument("--min-accuracy", type=float, default=60.0)
    e.add_argument("--min-refusal", type=float, default=None)
    e.set_defaults(fn=cmd_report)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
