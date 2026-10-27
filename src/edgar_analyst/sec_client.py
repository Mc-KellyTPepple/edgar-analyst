"""Polite, cached client for SEC EDGAR.

The SEC asks for a declared User-Agent and at most 10 requests per second.
We stay under that (about 8/s), retry on 429/5xx, and cache every response
on disk so re-running ingestion costs nothing.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import requests

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{doc}"


class SecClient:
    def __init__(self, user_agent: str, cache_dir: Path, min_interval: float = 0.125, max_retries: int = 5):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._last = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".bin")

    def get_bytes(self, url: str) -> bytes:
        path = self._cache_path(url)
        if path.exists():
            return path.read_bytes()
        for attempt in range(self.max_retries):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            resp = self.session.get(url, timeout=60)
            if resp.status_code == 200:
                path.write_bytes(resp.content)
                return resp.content
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
        raise RuntimeError(f"giving up on {url} after {self.max_retries} attempts")

    def get_json(self, url: str):
        return json.loads(self.get_bytes(url))

    # --- endpoints -------------------------------------------------------
    def tickers(self) -> dict[str, tuple[int, str]]:
        """ticker -> (cik, company name)."""
        raw = self.get_json(TICKERS_URL)
        return {row["ticker"].upper(): (int(row["cik_str"]), row["title"]) for row in raw.values()}

    def company_facts(self, cik: int) -> dict:
        return self.get_json(FACTS_URL.format(cik=cik))

    def submissions(self, cik: int) -> dict:
        return self.get_json(SUBMISSIONS_URL.format(cik=cik))

    def filing_document(self, cik: int, accession: str, doc: str) -> bytes:
        url = ARCHIVE_URL.format(cik=cik, acc_nodash=accession.replace("-", ""), doc=doc)
        return self.get_bytes(url)
