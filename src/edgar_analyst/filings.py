"""Turn 10-K HTML into clean, section-tagged text chunks.

Filings are inline-XBRL HTML: large, table-heavy, with a table of contents that repeats
every "Item N." heading. The splitter therefore keeps, for each item, the longest region
between headings (the real section, not the table-of-contents line).
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

ITEMS = {"1": "Business", "1A": "Risk Factors", "7": "MD&A"}
_BLOCK = {"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "ul", "ol"}
_CELL = {"td", "th"}
_ITEM_RE = re.compile(r"^[ \t]*item[ \t]*(\d{1,2}[a-c]?)[ \t]*[\.\:\-–—]?", re.I | re.M)


def html_to_text(raw: bytes | str) -> str:
    soup = BeautifulSoup(raw, "lxml")
    for t in soup(["script", "style"]):
        t.decompose()
    for t in soup.find_all(lambda tag: tag.name == "ix:header" or "display:none" in (tag.get("style") or "").replace(" ", "").lower()):
        t.decompose()
    for t in soup.find_all("br"):
        t.replace_with("\n")
    for t in soup.find_all(list(_BLOCK)):
        t.append("\n")
    for t in soup.find_all(list(_CELL)):
        t.append(" | ")
    text = soup.get_text("")
    text = text.replace("\xa0", " ").replace("​", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"(?:\s*\|\s*)+\n", "\n", text)          # trailing cell separators
    text = re.sub(r"\n[ \t]*\n+", "\n", text)
    return text.strip()


def split_items(text: str, min_chars: int = 1500) -> dict[str, str]:
    """{'1': ..., '1A': ..., '7': ...}; missing items are omitted."""
    marks = [(m.start(), m.group(1).upper()) for m in _ITEM_RE.finditer(text)]
    best: dict[str, str] = {}
    for i, (pos, key) in enumerate(marks):
        if key not in ITEMS:
            continue
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        region = text[pos:end].strip()
        if len(region) >= min_chars and len(region) > len(best.get(key, "")):
            best[key] = region
    return best


def chunk_text(text: str, target: int = 200, max_words: int = 300, min_words: int = 25) -> list[str]:
    units: list[str] = []
    for para in (p.strip() for p in text.split("\n")):
        words = para.split()
        if not words:
            continue
        if len(words) <= max_words:
            units.append(para)
        else:
            units += [" ".join(words[i:i + target]) for i in range(0, len(words), target)]
    chunks, cur, n = [], [], 0
    for u in units:
        w = len(u.split())
        cur.append(u)
        n += w
        if n >= target:
            chunks.append("\n".join(cur))
            tail = cur[-1] if len(cur[-1].split()) <= 60 else None   # one paragraph of overlap
            cur, n = ([tail], len(tail.split())) if tail else ([], 0)
    if cur and n >= min_words and (not chunks or "\n".join(cur) != chunks[-1]):
        chunks.append("\n".join(cur))
    return [c for c in chunks if len(c.split()) >= min_words]


def filing_chunks(html: bytes | str, ticker: str, accession: str, period_end: str) -> tuple[list[dict], dict[str, int]]:
    """Chunk records for one filing, plus {item: chars} for diagnostics."""
    sections = split_items(html_to_text(html))
    out = []
    for item, body in sections.items():
        for n, ch in enumerate(chunk_text(body)):
            out.append({"key": f"{accession}:{item}:{n}", "ticker": ticker, "accession": accession,
                        "period_end": period_end, "item": item, "text": ch})
    return out, {k: len(v) for k, v in sections.items()}
