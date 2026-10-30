"""Hybrid retrieval over filing text: BM25 (SQLite FTS5) + dense embeddings, fused with
reciprocal rank fusion. Dense search is optional: without an embedder (or embeddings in the
database) it degrades to BM25 alone, and `TextIndex.mode` says which one is active.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE chunks(id INTEGER PRIMARY KEY, key TEXT UNIQUE, ticker TEXT, accession TEXT, period_end TEXT, item TEXT, text TEXT);
CREATE VIRTUAL TABLE chunks_fts USING fts5(text, content='chunks', content_rowid='id', tokenize='porter unicode61');
CREATE TABLE embeddings(key TEXT PRIMARY KEY, vec BLOB);
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
"""
_STOP = set("a an and are as at be by did do does for from how in is it of on or the to was were what which who with many much".split())
EMBED_MODEL = "BAAI/bge-small-en-v1.5"


class FastEmbedder:
    """Local ONNX embeddings through the `fastembed` package (no API, no GPU)."""

    def __init__(self, model: str = EMBED_MODEL):
        from fastembed import TextEmbedding  # imported lazily: optional dependency

        self.name = model
        self.model = TextEmbedding(model_name=model)

    @staticmethod
    def _norm(a: np.ndarray) -> np.ndarray:
        a = np.asarray(a, dtype=np.float32)
        return a / np.maximum(np.linalg.norm(a, axis=-1, keepdims=True), 1e-9)

    def embed(self, texts: list[str]) -> np.ndarray:
        return self._norm(np.array(list(self.model.embed(texts, batch_size=32))))

    def embed_query(self, text: str) -> np.ndarray:
        return self._norm(np.array(list(self.model.query_embed(text)))[0])


RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"


class FastReranker:
    """Local cross-encoder: scores (query, passage) pairs jointly, which is more precise than comparing embeddings."""

    def __init__(self, model: str = RERANK_MODEL):
        from fastembed.rerank.cross_encoder import TextCrossEncoder  # optional dependency

        self.model = TextCrossEncoder(model_name=model)

    def score(self, query: str, texts: list[str]) -> list[float]:
        return [float(x) for x in self.model.rerank(query, texts)]


def try_reranker():
    try:
        return FastReranker()
    except Exception as exc:
        print(f"reranker unavailable ({type(exc).__name__}: {exc}); using fused ranking only")
        return None


def try_embedder():
    try:
        return FastEmbedder()
    except Exception as exc:  # missing package, offline, model download failed
        print(f"dense retrieval unavailable ({type(exc).__name__}: {exc}); using BM25 only")
        return None


def build_text_db(path: Path, chunks: list[dict], embedder=None, batch: int = 64) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT OR IGNORE INTO chunks(key,ticker,accession,period_end,item,text) VALUES (?,?,?,?,?,?)",
                         [(c["key"], c["ticker"], c["accession"], c["period_end"], c["item"], c["text"]) for c in chunks])
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        if embedder is not None:
            conn.execute("INSERT INTO meta VALUES ('embedder', ?)", (getattr(embedder, "name", "custom"),))
            rows = conn.execute("SELECT key, text FROM chunks ORDER BY id").fetchall()
            for i in range(0, len(rows), batch):
                part = rows[i:i + batch]
                vecs = embedder.embed([t for _, t in part])
                conn.executemany("INSERT INTO embeddings VALUES (?,?)",
                                 [(k, np.asarray(v, dtype=np.float32).tobytes()) for (k, _), v in zip(part, vecs)])
        conn.commit()
    finally:
        conn.close()


def _fts_query(q: str) -> str | None:
    toks, seen = [], set()
    for t in re.findall(r"[A-Za-z0-9]+", q.lower()):
        if t not in _STOP and len(t) > 1 and t not in seen:
            seen.add(t)
            toks.append(t)
    return " OR ".join(f'"{t}"' for t in toks[:24]) or None


class TextIndex:
    def __init__(self, path: Path, embedder=None, reranker=None, rerank_pool: int = 16):
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        self.embedder = embedder
        self.reranker, self.rerank_pool = reranker, rerank_pool
        self.keys: list[str] = []
        self.matrix = None
        stored = self.conn.execute("SELECT v FROM meta WHERE k='embedder'").fetchone()
        if embedder is not None and stored and stored[0] == getattr(embedder, "name", "custom"):
            rows = self.conn.execute("SELECT c.key, e.vec FROM chunks c JOIN embeddings e ON e.key=c.key ORDER BY c.id").fetchall()
            if rows:
                self.keys = [r[0] for r in rows]
                self.matrix = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
        self.mode = "hybrid" if self.matrix is not None else "bm25"
        self._meta = {r[0]: r[1:] for r in self.conn.execute("SELECT key, ticker, period_end FROM chunks")}

    def _allowed(self, key: str, ticker: str | None, year: str | None) -> bool:
        t, pe = self._meta[key]
        return (ticker is None or t == ticker) and (year is None or (pe or "").startswith(year))

    def _bm25(self, query: str, ticker, year, n: int) -> list[str]:
        fq = _fts_query(query)
        if not fq:
            return []
        sql = ("SELECT c.key FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid WHERE chunks_fts MATCH ?"
               + (" AND c.ticker = ?" if ticker else "") + (" AND c.period_end LIKE ?" if year else "")
               + " ORDER BY bm25(chunks_fts) LIMIT ?")
        args = [fq] + ([ticker] if ticker else []) + ([f"{year}%"] if year else []) + [n]
        return [r[0] for r in self.conn.execute(sql, args)]

    def _dense(self, query: str, ticker, year, n: int) -> list[str]:
        if self.matrix is None:
            return []
        scores = self.matrix @ np.asarray(self.embedder.embed_query(query), dtype=np.float32)
        order = np.argsort(-scores)
        out = []
        for i in order:
            if self._allowed(self.keys[i], ticker, year):
                out.append(self.keys[i])
                if len(out) >= n:
                    break
        return out

    def search(self, query: str, k: int = 4, ticker: str | None = None, year: str | None = None, pool: int = 30) -> list[dict]:
        ticker = ticker.upper() if ticker else None
        year = str(year) if year else None
        ranked: dict[str, float] = {}
        for ranking in (self._bm25(query, ticker, year, pool), self._dense(query, ticker, year, pool)):
            for rank, key in enumerate(ranking):
                ranked[key] = ranked.get(key, 0.0) + 1.0 / (60 + rank)
        top = sorted(ranked, key=lambda k_: -ranked[k_])[: max(k, self.rerank_pool) if self.reranker else k]
        if self.reranker and len(top) > 1:
            texts = [self.conn.execute("SELECT text FROM chunks WHERE key=?", (key,)).fetchone()[0] for key in top]
            scores = self.reranker.score(query, texts)
            top = [key for _, key in sorted(zip(scores, top), key=lambda x: -x[0])][:k]
            ranked = {key: ranked[key] for key in top}
        top = top[:k]
        out = []
        for key in top:
            r = self.conn.execute("SELECT key, ticker, accession, period_end, item, text FROM chunks WHERE key=?", (key,)).fetchone()
            out.append({"key": r[0], "ticker": r[1], "accession": r[2], "period_end": r[3], "item": r[4], "text": r[5],
                        "score": round(ranked[key], 5)})
        return out
