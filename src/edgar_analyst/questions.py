"""Generate an evaluation set whose answers are known from the data.

Every answer is computed from the facts table, so grading is automatic and
nothing is hand-labelled. Tiers:

  1  lookup          one figure for one company and year
  2  multi-step      growth rates and ratios (two figures plus arithmetic)
  3  comparison      across companies or across years (several figures)
  0  abstain         questions the corpus cannot answer; the agent should say so

The same seed always produces the same file.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import sqlite3
from datetime import date
from itertools import combinations

from .facts import METRICS

LOOKUP_METRICS = ["revenue", "net_income", "operating_income", "rd_expense", "operating_cash_flow", "total_assets"]
GROWTH_METRICS = ["revenue", "rd_expense", "operating_income", "net_income"]
RATIOS = {
    "rd_intensity": ("research and development expense as a percentage of revenue", "rd_expense", "revenue"),
    "operating_margin": ("operating margin (operating income as a percentage of revenue)", "operating_income", "revenue"),
    "net_margin": ("net profit margin (net income as a percentage of revenue)", "net_income", "revenue"),
}
# Companies that are deliberately NOT in the corpus (for the abstain tier).
OUT_OF_CORPUS = ["Snowflake Inc.", "Palantir Technologies Inc.", "Spotify Technology S.A.", "Shopify Inc."]

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
NUM_TOL_REL = 0.005   # 0.5% on dollar amounts
PCT_TOL_ABS = 0.15    # 0.15 percentage points


def fmt_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{MONTHS[d.month - 1]} {d.day}, {d.year}"


_SUFFIXES = {"inc", "corp", "corporation", "co", "company", "ltd", "plc", "holdings", "group", "limited", "sa", "nv"}


def name_aliases(name: str, other: str = "") -> list[str]:
    """Ways a person might write a company's name: full SEC name, without the corporate suffix,
    and the first word (skipped if the other company in the question shares it)."""
    clean = name.replace(".com", "").replace(",", " ").replace(".", " ")
    words = [w for w in clean.split() if w]
    while len(words) > 1 and words[-1].lower() in _SUFFIXES:
        words.pop()
    out = [name, " ".join(words)]
    first = words[0] if words else ""
    other_first = (other.replace(".com", "").replace(",", " ").split() or [""])[0].lower()
    if len(first) >= 4 and first.lower() != other_first:
        out.append(first)
    seen, uniq = set(), []
    for a in out:
        if a and a.lower() not in seen:
            seen.add(a.lower())
            uniq.append(a)
    return uniq


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _qid(tier: int, *key) -> str:
    return f"t{tier}-" + hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:10]


class Corpus:
    """In-memory view of the facts table."""

    def __init__(self, conn: sqlite3.Connection):
        self.companies: dict[int, tuple[str, str]] = {}
        for r in conn.execute("SELECT cik, ticker, name FROM companies ORDER BY ticker"):
            self.companies[r[0]] = (r[1], r[2])
        self.facts: dict[int, dict[str, dict[str, tuple[float, str]]]] = {}
        for r in conn.execute("SELECT cik, metric, period_end, value, accession FROM facts"):
            self.facts.setdefault(r[0], {}).setdefault(r[1], {})[r[2]] = (r[3], r[4])

    def label(self, cik: int) -> str:
        t, n = self.companies[cik]
        return f"{n} ({t})"

    def ticker(self, cik: int) -> str:
        return self.companies[cik][0]

    def get(self, cik: int, metric: str, end: str):
        return self.facts.get(cik, {}).get(metric, {}).get(end)

    def periods(self, cik: int, metric: str) -> list[str]:
        return sorted(self.facts.get(cik, {}).get(metric, {}))

    def evidence(self, cik: int, metric: str, end: str) -> dict:
        v, acc = self.facts[cik][metric][end]
        return {"ticker": self.ticker(cik), "metric": metric, "period_end": end, "accession": acc, "value": v}

    def ratio(self, cik: int, num: str, den: str, end: str):
        a, b = self.get(cik, num, end), self.get(cik, den, end)
        if not a or not b or b[0] <= 0:
            return None
        return a[0] / b[0] * 100.0

    def consecutive(self, cik: int, metric: str):
        ps = self.periods(cik, metric)
        return [(a, b) for a, b in zip(ps, ps[1:]) if 350 <= _days(a, b) <= 380]


def _round_robin(cands: list[dict], n: int, rng: random.Random, group) -> list[dict]:
    """Pick n items spread evenly across groups, deterministically."""
    cands = sorted(cands, key=lambda q: q["id"])
    rng.shuffle(cands)
    groups: dict = {}
    for q in cands:
        groups.setdefault(group(q), []).append(q)
    out: list[dict] = []
    keys = sorted(groups)
    while len(out) < n and any(groups.values()):
        for k in keys:
            if groups[k] and len(out) < n:
                out.append(groups[k].pop())
    return out


def _tier1(c: Corpus) -> list[dict]:
    out = []
    for cik in c.companies:
        for metric in LOOKUP_METRICS:
            for end in c.periods(cik, metric)[-4:]:
                v, _ = c.get(cik, metric, end)
                label = METRICS[metric][0].lower()
                out.append({
                    "id": _qid(1, c.ticker(cik), metric, end), "tier": 1, "type": "lookup",
                    "question": (f"What was {c.label(cik)}'s {label} for the fiscal year ended {fmt_date(end)}? "
                                 "Give the answer in US dollars, in millions."),
                    "answer": {"kind": "usd_millions", "value": v / 1e6, "tolerance_rel": NUM_TOL_REL},
                    "evidence": [c.evidence(cik, metric, end)], "company": c.ticker(cik),
                })
    return out


def _tier2(c: Corpus) -> list[dict]:
    out = []
    for cik in c.companies:
        for metric in GROWTH_METRICS:
            for prev, cur in c.consecutive(cik, metric)[-3:]:
                a, b = c.get(cik, metric, prev), c.get(cik, metric, cur)
                if a[0] <= 0 or b[0] <= 0:
                    continue
                g = (b[0] / a[0] - 1) * 100
                out.append({
                    "id": _qid(2, "growth", c.ticker(cik), metric, cur), "tier": 2, "type": "growth",
                    "question": (f"By what percentage did {c.label(cik)}'s {METRICS[metric][0].lower()} change between the fiscal year "
                                 f"ended {fmt_date(prev)} and the fiscal year ended {fmt_date(cur)}? "
                                 "Give the answer as a percentage rounded to one decimal place (negative if it fell)."),
                    "answer": {"kind": "percent", "value": round(g, 2), "tolerance_abs": PCT_TOL_ABS},
                    "evidence": [c.evidence(cik, metric, prev), c.evidence(cik, metric, cur)], "company": c.ticker(cik),
                })
        for rname, (desc, num, den) in RATIOS.items():
            for end in c.periods(cik, den)[-4:]:
                r = c.ratio(cik, num, den, end)
                if r is None:
                    continue
                out.append({
                    "id": _qid(2, rname, c.ticker(cik), end), "tier": 2, "type": "ratio",
                    "question": (f"What was {c.label(cik)}'s {desc} for the fiscal year ended {fmt_date(end)}? "
                                 "Give the answer as a percentage rounded to one decimal place."),
                    "answer": {"kind": "percent", "value": round(r, 2), "tolerance_abs": PCT_TOL_ABS},
                    "evidence": [c.evidence(cik, num, end), c.evidence(cik, den, end)], "company": c.ticker(cik),
                })
    return out


def _tier3(c: Corpus) -> list[dict]:
    out = []
    ciks = sorted(c.companies)
    # cross-company: who had the higher ratio in aligned fiscal years?
    for rname, (desc, num, den) in RATIOS.items():
        for a, b in combinations(ciks, 2):
            for end_a in c.periods(a, den)[-3:]:
                end_b = next((e for e in c.periods(b, den) if abs(_days(end_a, e)) <= 45), None)
                if end_b is None:
                    continue
                ra, rb = c.ratio(a, num, den, end_a), c.ratio(b, num, den, end_b)
                if ra is None or rb is None or abs(ra - rb) < 1.0:
                    continue
                win = a if ra > rb else b
                ta, tb = c.ticker(a), c.ticker(b)
                out.append({
                    "id": _qid(3, "compare", rname, ta, tb, end_a), "tier": 3, "type": "compare",
                    "question": (f"Comparing each company's fiscal year ending closest to {fmt_date(end_a)}, which had the higher {desc}: "
                                 f"{c.label(a)} or {c.label(b)}? Answer with the company's ticker."),
                    "answer": {"kind": "choice", "value": c.ticker(win), "options": [ta, tb],
                               "aliases": {ta: name_aliases(c.companies[a][1], c.companies[b][1]),
                                           tb: name_aliases(c.companies[b][1], c.companies[a][1])}},
                    "evidence": [c.evidence(a, num, end_a), c.evidence(a, den, end_a), c.evidence(b, num, end_b), c.evidence(b, den, end_b)],
                    "company": ta,
                })
    # cross-year: did a ratio go up or down over several years?
    for cik in ciks:
        for rname, (desc, num, den) in RATIOS.items():
            ps = [p for p in c.periods(cik, den) if c.ratio(cik, num, den, p) is not None]
            if len(ps) < 3:
                continue
            first, last = ps[-3], ps[-1]
            r1, r2 = c.ratio(cik, num, den, first), c.ratio(cik, num, den, last)
            if abs(r2 - r1) < 0.5:
                continue
            out.append({
                "id": _qid(3, "trend", rname, c.ticker(cik), last), "tier": 3, "type": "trend",
                "question": (f"Did {c.label(cik)}'s {desc} increase or decrease between the fiscal year ended {fmt_date(first)} "
                             f"and the fiscal year ended {fmt_date(last)}? Answer 'increased' or 'decreased'."),
                "answer": {"kind": "choice", "value": "increased" if r2 > r1 else "decreased", "options": ["increased", "decreased"],
                           "aliases": {"increased": ["increase", "rose", "grew", "higher", "up"],
                                       "decreased": ["decrease", "fell", "declined", "dropped", "lower", "down"]}},
                "evidence": [c.evidence(cik, num, first), c.evidence(cik, den, first), c.evidence(cik, num, last), c.evidence(cik, den, last)],
                "company": c.ticker(cik),
            })
    return out


def _abstain(c: Corpus, rng: random.Random, n: int) -> list[dict]:
    out = []
    latest = max((p for cik in c.companies for p in c.periods(cik, "revenue")), default=None)
    if latest:
        future_year = date.fromisoformat(latest).year + 2
        for cik in sorted(c.companies):
            out.append({
                "id": _qid(0, "future", c.ticker(cik), future_year), "tier": 0, "type": "abstain_future",
                "question": (f"What was {c.label(cik)}'s revenue for its fiscal year {future_year}? "
                             "Give the answer in US dollars, in millions."),
                "answer": {"kind": "abstain", "reason": "period not yet reported"}, "evidence": [], "company": c.ticker(cik),
            })
    for name in OUT_OF_CORPUS:
        out.append({
            "id": _qid(0, "outside", name), "tier": 0, "type": "abstain_outside_corpus",
            "question": f"What was {name}'s revenue in its most recent fiscal year? Give the answer in US dollars, in millions.",
            "answer": {"kind": "abstain", "reason": "company is not in the corpus"}, "evidence": [], "company": name,
        })
    return _round_robin(out, n, rng, lambda q: q["type"])


def generate(conn: sqlite3.Connection, seed: int = 7, n_t1: int = 50, n_t2: int = 60, n_t3: int = 40, n_abstain: int = 15) -> list[dict]:
    rng = random.Random(seed)
    c = Corpus(conn)
    qs = []
    qs += _round_robin(_tier1(c), n_t1, rng, lambda q: q["company"])
    qs += _round_robin(_tier2(c), n_t2, rng, lambda q: (q["company"], q["type"]))
    qs += _round_robin(_tier3(c), n_t3, rng, lambda q: (q["type"], q["company"]))
    qs += _abstain(c, rng, n_abstain)
    return sorted(qs, key=lambda q: (q["tier"], q["id"]))


def write_jsonl(questions: list[dict], path) -> None:
    from pathlib import Path

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for q in questions:
            fh.write(json.dumps(q, sort_keys=True) + "\n")


def summarize(questions: list[dict]) -> str:
    by_tier: dict[int, int] = {}
    for q in questions:
        by_tier[q["tier"]] = by_tier.get(q["tier"], 0) + 1
    companies = {q["company"] for q in questions if q["tier"] > 0}
    parts = ", ".join(f"tier {t}: {n}" for t, n in sorted(by_tier.items()))
    return f"{len(questions)} questions ({parts}) across {len(companies)} companies"
