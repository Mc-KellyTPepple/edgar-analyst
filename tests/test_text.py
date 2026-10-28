import re
import tempfile
import unittest
from pathlib import Path

import numpy as np

from edgar_analyst.agent import run_full_agent, run_tool
from edgar_analyst.filings import chunk_text, filing_chunks, html_to_text, split_items
from edgar_analyst.grading import grade
from edgar_analyst.textdb import TextIndex, _fts_query, build_text_db
from edgar_analyst.textq import employee_facts, generate_text_questions

from .test_agent_eval import ScriptedLLM, reply

FILLER = " ".join(f"word{i}" for i in range(400))


def make_html(employees="approximately 29,600 employees", risk="supply chain disruption could harm our manufacturing"):
    toc = "".join(f"<p>Item {n}. {t}</p>" for n, t in [("1", "Business"), ("1A", "Risk Factors"), ("7", "MD&amp;A")])
    return (
        "<html><body><div style='display:none'><ix:header>HIDDEN-XBRL</ix:header></div>"
        f"<p>Table of Contents</p>{toc}"
        "<p>Item 1. Business</p>"
        f"<p>Alpha Corp designs widgets for data centers. As of December 31, 2024, we had {employees} worldwide. {FILLER}</p>"
        f"<p>Item 1A. Risk Factors</p><p>Risks: {risk}. {FILLER}</p><p>{FILLER}</p>"
        f"<p>Item 7. Management's Discussion and Analysis</p><p>Revenue grew because of demand. {FILLER}</p><p>{FILLER}</p>"
        "<table><tr><td>Revenue</td><td>1,000</td></tr></table></body></html>"
    )


class ParseTests(unittest.TestCase):
    def test_html_to_text_drops_hidden_and_keeps_blocks(self):
        t = html_to_text(make_html())
        self.assertNotIn("HIDDEN-XBRL", t)
        self.assertIn("\nItem 1. Business\n", t)
        self.assertIn("Revenue | 1,000", t)

    def test_inline_tags_do_not_split_numbers(self):
        t = html_to_text("<p>we had <span><ix:nonfraction>29,600</ix:nonfraction></span> employees</p>")
        self.assertIn("we had 29,600 employees", t)

    def test_split_items_skips_table_of_contents(self):
        items = split_items(html_to_text(make_html()))
        self.assertEqual(set(items), {"1", "1A", "7"})
        self.assertIn("designs widgets", items["1"])
        self.assertGreater(len(items["1"]), 1500)

    def test_running_page_headers_do_not_truncate_sections(self):
        body = "<p>Item 1. Business</p>" + "".join(f"<p>{FILLER}</p><p>PART I</p><p>Item 1</p>" for _ in range(3))
        body += "<p>Item 1A. Risk Factors</p>" + f"<p>{FILLER} {FILLER}</p>"
        items = split_items(html_to_text(f"<html><body>{body}</body></html>"))
        self.assertGreater(len(items["1"]), 3 * len(FILLER))
        self.assertNotIn("Risk Factors", items["1"])

    def test_unrecognised_layout_falls_back_to_whole_text(self):
        html = f"<html><body><p>Our business overview. we had 1,500 employees. {FILLER}</p></body></html>"
        chunks, sizes = filing_chunks(html, "ZZZ", "0000000009-25-000001", "2024-12-31")
        self.assertEqual(sizes, {"all": len(html_to_text(html))})
        self.assertTrue(chunks and all(c["item"] == "all" for c in chunks))

    def test_chunking(self):
        text = "\n".join(" ".join(f"w{p}x{i}" for i in range(90)) for p in range(8))
        chunks = chunk_text(text)
        self.assertTrue(all(25 <= len(c.split()) <= 400 for c in chunks))
        self.assertTrue(len(chunks) >= 3)
        self.assertEqual(chunk_text("tiny fragment"), [])

    def test_filing_chunks_keys_are_stable(self):
        a, sizes = filing_chunks(make_html(), "ALPH", "0000000001-25-000001", "2024-12-31")
        b, _ = filing_chunks(make_html(), "ALPH", "0000000001-25-000001", "2024-12-31")
        self.assertEqual([c["key"] for c in a], [c["key"] for c in b])
        self.assertEqual(set(sizes), {"1", "1A", "7"})
        self.assertTrue(a[0]["key"].startswith("0000000001-25-000001:"))


class FakeEmbedder:
    """Bag-of-words hashing embedder with a hand-made synonym so dense search can rescue BM25 misses."""
    name = "fake-hash"
    SYN = {"staff": "employees", "headcount": "employees"}

    def _vec(self, text):
        v = np.zeros(64, dtype=np.float32)
        for w in re.findall(r"[a-z]+", text.lower()):
            v[hash(self.SYN.get(w, w)) % 64] += 1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed(self, texts):
        return np.vstack([self._vec(t) for t in texts])

    def embed_query(self, text):
        return self._vec(text)


def corpus():
    mk = lambda key, tk, pe, item, text: {"key": key, "ticker": tk, "accession": key.split(":")[0], "period_end": pe, "item": item, "text": text}  # noqa: E731
    return [
        mk("0000000001-25-000001:1:0", "ALPH", "2024-12-31", "1", "As of December 31, 2024 we had 29,600 employees worldwide in research and sales."),
        mk("0000000001-24-000001:1:0", "ALPH", "2023-12-31", "1", "As of December 31, 2023 we had 27,000 employees worldwide in research and sales."),
        mk("0000000002-25-000001:1:0", "BETA", "2025-01-26", "1", "We had 11,000 employees. Our factories make batteries."),
        mk("0000000001-25-000001:1A:0", "ALPH", "2024-12-31", "1A", "Supply chain disruption could harm manufacturing and delay shipments of widgets."),
    ]


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "text.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_bm25_search_and_filters(self):
        build_text_db(self.path, corpus())
        idx = TextIndex(self.path)
        self.assertEqual(idx.mode, "bm25")
        top = idx.search("how many employees", k=3, ticker="alph", year="2023")
        self.assertEqual([h["key"] for h in top], ["0000000001-24-000001:1:0"])
        self.assertEqual(idx.search("batteries factories")[0]["ticker"], "BETA")
        self.assertEqual(idx.search("", k=3), [])
        self.assertEqual(idx.search("zzzzqqq"), [])

    def test_query_sanitising(self):
        self.assertEqual(_fts_query('what is "AND" NEAR(xy) *'), '"near" OR "xy"')
        self.assertIsNone(_fts_query("the of a"))

    def test_hybrid_rescues_vocabulary_mismatch(self):
        build_text_db(self.path, corpus(), FakeEmbedder())
        idx = TextIndex(self.path, FakeEmbedder())
        self.assertEqual(idx.mode, "hybrid")
        # "staff headcount" shares no word with the passage, so BM25 finds nothing; the dense side does
        bm = TextIndex(self.path).search("staff headcount", k=2, ticker="BETA")
        self.assertEqual(bm, [])
        hy = idx.search("staff headcount", k=2, ticker="BETA")
        self.assertEqual(hy[0]["key"], "0000000002-25-000001:1:0")

    def test_embedder_mismatch_falls_back_to_bm25(self):
        build_text_db(self.path, corpus(), FakeEmbedder())
        other = FakeEmbedder()
        other.name = "different-model"
        self.assertEqual(TextIndex(self.path, other).mode, "bm25")


class TextQuestionTests(unittest.TestCase):
    def test_employee_pattern(self):
        self.assertEqual(employee_facts("As of December 31, 2023, we had 140,473 employees worldwide")[0], 140473)
        self.assertEqual(employee_facts("the Company had approximately 164,000 full-time equivalent employees")[0], 164000)
        self.assertEqual(employee_facts("we employed about 1,200 people")[0], 1200)
        self.assertEqual(employee_facts("we employed approximately 1,556,000 full-time and part-time employees")[0], 1556000)
        self.assertIsNone(employee_facts("we had 12 employees"))
        self.assertIsNone(employee_facts("our employees are talented"))

    def test_generation_and_grading(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            build_text_db(path, corpus())
            import sqlite3
            conn = sqlite3.connect(path)
            qs = generate_text_questions(conn, {"ALPH": "Alpha Corp", "BETA": "Beta Inc"})
            self.assertEqual(len(qs), 3)
            q = next(q for q in qs if q["evidence"][0]["period_end"] == "2023-12-31")
            self.assertEqual(q["answer"]["value"], 27000)
            self.assertEqual(q["evidence"][0]["chunk"], "0000000001-24-000001:1:0")
            self.assertTrue(grade(q, "FINAL ANSWER: 27,000")["correct"])
            self.assertTrue(grade(q, "FINAL ANSWER: 27 thousand")["correct"])
            self.assertFalse(grade(q, "FINAL ANSWER: 29,600")["correct"])
            self.assertFalse(grade(q, "FINAL ANSWER: I cannot answer")["correct"])
            self.assertEqual([x["id"] for x in qs], [x["id"] for x in generate_text_questions(conn, {})])


class FullAgentTests(unittest.TestCase):
    def test_search_tool_tracks_retrieved_and_formats(self):
        hits = [{"key": "k1", "ticker": "ALPH", "period_end": "2024-12-31", "item": "1", "accession": "0000000001-25-000001", "text": "we had 29,600 employees"}]
        retrieved = []
        out = run_tool("search_filings", {"query": "employees", "ticker": "ALPH", "year": "2024"}, None,
                       search=lambda q, ticker=None, year=None: hits, retrieved=retrieved)
        self.assertIn("accession 0000000001-25-000001", out)
        self.assertEqual(retrieved, ["k1"])
        self.assertTrue(run_tool("search_filings", {"query": "x"}, None, search=None).startswith("error"))

    def test_full_agent_uses_search_and_collects_citations(self):
        hits = [{"key": "k1", "ticker": "ALPH", "period_end": "2024-12-31", "item": "1", "accession": "0000000001-25-000001", "text": "29,600 employees"}]
        llm = ScriptedLLM(reply(calls=[("search_filings", {"query": "employees"})]), reply("FINAL ANSWER: 29,600\nSOURCES: 0000000001-25-000001"))
        r = run_full_agent(llm, "q?", None, search=lambda q, ticker=None, year=None: hits)
        self.assertEqual(r["retrieved"], ["k1"])
        self.assertEqual(r["seen_accessions"], ["0000000001-25-000001"])
        names = [t["function"]["name"] for t in llm.calls[0]["tools"]]
        self.assertIn("search_filings", names)
        self.assertIn("lookup_fact", names)

    def test_without_index_search_tool_is_not_offered(self):
        llm = ScriptedLLM(reply("FINAL ANSWER: 1"))
        run_full_agent(llm, "q?", None)
        self.assertNotIn("search_filings", [t["function"]["name"] for t in llm.calls[0]["tools"]])


if __name__ == "__main__":
    unittest.main()
