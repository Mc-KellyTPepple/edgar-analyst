"""Command line: ingest SEC data, build the facts database, generate questions."""
from __future__ import annotations

import argparse
import json
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
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
