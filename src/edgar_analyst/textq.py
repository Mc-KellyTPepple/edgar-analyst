"""Evaluation questions whose answer is stated only in filing text (tier 4).

The ground truth is the figure the filing itself states, found by pattern from the
chunk text, so no model is involved in producing the answer key. Each question keeps the
supporting quote and the stable key of the chunk that contains it, which lets the
evaluation measure retrieval separately from answering.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3

_EMP = re.compile(
    r"\b(?:had|employed|employs)\s+(?:a total of |approximately |about |over |more than |roughly )*"
    r"(\d{1,3}(?:,\d{3})+|\d{3,7})\s+(?:full[- ]time(?: equivalent)?,? |regular |permanent )?(?:employees|people|team members)",
    re.I,
)


def employee_facts(text: str):
    m = _EMP.search(text)
    if not m:
        return None
    value = int(m.group(1).replace(",", ""))
    if not 100 <= value <= 3_000_000:
        return None
    quote = text[max(0, m.start() - 120): m.end() + 80].replace("\n", " ").strip()
    return value, quote


def generate_text_questions(text_conn: sqlite3.Connection, names: dict[str, str]) -> list[dict]:
    """One employee-count question per filing that states it. `names` maps ticker to company name."""
    rows = text_conn.execute(
        "SELECT ticker, accession, period_end, key, text FROM chunks WHERE item IN ('1', 'all') ORDER BY ticker, period_end, id").fetchall()
    seen, qs = set(), []
    for ticker, accession, period_end, key, text in rows:
        if accession in seen:
            continue
        hit = employee_facts(text)
        if not hit:
            continue
        seen.add(accession)
        value, quote = hit
        qid = "t4-" + hashlib.sha1(f"{accession}:employees".encode()).hexdigest()[:10]
        qs.append({
            "id": qid, "tier": 4, "type": "text_employees", "company": ticker,
            "question": (f"According to {names.get(ticker, ticker)} ({ticker})'s annual report (10-K) for the fiscal year ended "
                         f"{period_end}, how many employees did the company report? Give a whole number."),
            "answer": {"kind": "count", "value": value, "tolerance_rel": 0.01},
            "evidence": [{"accession": accession, "chunk": key, "quote": quote, "period_end": period_end, "ticker": ticker}],
        })
    return sorted(qs, key=lambda q: q["id"])
