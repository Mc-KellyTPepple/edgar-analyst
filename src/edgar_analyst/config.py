"""Project configuration."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = ROOT / "data" / "raw"
DB_PATH = ROOT / "data" / "facts.db"
QUESTIONS_PATH = ROOT / "evals" / "questions.jsonl"
TEXT_DB_PATH = ROOT / "data" / "text.db"
TEXT_QUESTIONS_PATH = ROOT / "evals" / "text_questions.jsonl"
HELDOUT_QUESTIONS_PATH = ROOT / "evals" / "heldout_questions.jsonl"
HELDOUT2_QUESTIONS_PATH = ROOT / "evals" / "heldout2_questions.jsonl"
RESULTS_DIR = ROOT / "evals" / "results"
LLM_CACHE_DIR = ROOT / "evals" / "llm_cache"
LEDGER_PATH = ROOT / "evals" / "groq_ledger.json"

# Start small (10 companies with comparable R&D-heavy business models) and
# widen to ~30 once the pipeline is proven.
DEFAULT_TICKERS = ["NVDA", "AMD", "INTC", "MSFT", "AAPL", "GOOGL", "AMZN", "META", "TSLA", "ORCL"]


def user_agent() -> str:
    """SEC fair-access policy: every request must identify the requester."""
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if "@" not in ua:
        raise RuntimeError(
            "Set SEC_USER_AGENT to something like 'Your Name you@example.com'. "
            "SEC EDGAR requires requests to declare a contact."
        )
    return ua
