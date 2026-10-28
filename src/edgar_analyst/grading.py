"""Automatic grading of free-text answers against the generated questions.

Agents are asked to finish with a line `FINAL ANSWER: <answer>`. If it is
missing, the last number (or option) in the text is used.
"""
from __future__ import annotations

import re

_FINAL = re.compile(r"final answer\s*[:\-]\s*(.+)", re.I | re.S)
_NUM = re.compile(
    r"(?P<sign>[-−]?)\s*\$?\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<suf>%|percent\b|pp\b|trillion\b|billion\b|million\b|thousand\b|tn\b|bn\b|mm\b|m\b|b\b|k\b)?",
    re.I,
)
_ABSTAIN = re.compile(
    r"\b(cannot|can't|can not|unable|not available|no data|not (?:yet )?reported|not in (?:the|my) (?:corpus|data|dataset|database)|"
    r"don't have|do not have|doesn't exist|does not exist|no information|not covered|insufficient)\b",
    re.I,
)
_SCALE_TO_MILLIONS = {"trillion": 1e6, "tn": 1e6, "billion": 1e3, "bn": 1e3, "b": 1e3,
                      "million": 1.0, "mm": 1.0, "m": 1.0, "thousand": 1e-3, "k": 1e-3}


def extract_final(response: str) -> str:
    matches = _FINAL.findall(response)
    return matches[-1].strip().splitlines()[0] if matches else response.strip()


def _numbers(text: str) -> list[tuple[float, str]]:
    out = []
    for m in _NUM.finditer(text):
        value = float(m.group("num").replace(",", ""))
        if m.group("sign"):
            value = -value
        out.append((value, (m.group("suf") or "").lower()))
    return out


def is_abstain(text: str) -> bool:
    return bool(_ABSTAIN.search(text))


def _pick_number(text: str):
    nums = _numbers(text)
    return nums[-1] if nums else None


def cited_accessions(response: str) -> set[str]:
    """Accession numbers listed on the 'SOURCES:' line."""
    m = re.findall(r"sources?\s*[:\-]\s*(.+)", response, re.I)
    return set(re.findall(r"\d{10}-\d{2}-\d{6}", m[-1])) if m else set()


def grade(question: dict, response: str) -> dict:
    ans = question["answer"]
    kind = ans["kind"]
    final = extract_final(response)
    gave_number = bool(re.search(r"final answer\s*[:\-]\s*[-−$]?\s*\d", response, re.I))

    if kind == "abstain":
        ok = is_abstain(response) and not gave_number
        return {"correct": ok, "abstained": is_abstain(response), "reason": "abstained" if ok else "should have declined to answer"}

    if is_abstain(final) and not _numbers(final):
        return {"correct": False, "abstained": True, "reason": "abstained on an answerable question"}

    if kind == "usd_millions":
        picked = _pick_number(final)
        if not picked:
            return {"correct": False, "reason": "no number found"}
        value, suf = picked
        got = value * _SCALE_TO_MILLIONS.get(suf, 1.0)
        want = ans["value"]
        ok = abs(got - want) <= abs(want) * ans["tolerance_rel"]
        return {"correct": ok, "reason": f"got {got:,.2f}M, expected {want:,.2f}M"}

    if kind == "percent":
        picked = _pick_number(final)
        if not picked:
            return {"correct": False, "reason": "no number found"}
        got, want = picked[0], ans["value"]
        ok = abs(got - want) <= ans["tolerance_abs"]
        return {"correct": ok, "reason": f"got {got}, expected {want}"}

    if kind == "count":
        picked = _pick_number(final)
        if not picked:
            return {"correct": False, "reason": "no number found"}
        value, suf = picked
        got = value * {"k": 1e3, "thousand": 1e3, "million": 1e6, "m": 1e6, "mm": 1e6}.get(suf, 1.0)
        want = ans["value"]
        ok = abs(got - want) <= abs(want) * ans["tolerance_rel"]
        return {"correct": ok, "reason": f"got {got:,.0f}, expected {want:,.0f}"}

    if kind == "choice":
        hits = []
        for opt in ans["options"]:
            words = [opt] + ans.get("aliases", {}).get(opt, [])
            if any(re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", final, re.I) for w in words):
                hits.append(opt)
        if len(hits) != 1:
            return {"correct": False, "reason": f"ambiguous or no choice found ({hits})"}
        return {"correct": hits[0] == ans["value"], "reason": f"chose {hits[0]}, expected {ans['value']}"}

    raise ValueError(f"unknown answer kind {kind!r}")
