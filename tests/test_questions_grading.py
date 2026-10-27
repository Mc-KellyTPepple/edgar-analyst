import json
import tempfile
import unittest
from pathlib import Path

from edgar_analyst.db import open_readonly
from edgar_analyst.grading import grade
from edgar_analyst.questions import generate, summarize, write_jsonl

from .helpers import ALPHA, ALPHA_ENDS
from .test_facts_db import make_db


class GenerateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.conn = open_readonly(make_db(Path(cls.tmp.name)))
        cls.qs = generate(cls.conn, seed=7, n_t1=10, n_t2=12, n_t3=8, n_abstain=6)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def test_counts_and_tiers(self):
        tiers = {t: sum(1 for q in self.qs if q["tier"] == t) for t in (0, 1, 2, 3)}
        self.assertEqual(tiers, {0: 6, 1: 10, 2: 12, 3: 8})

    def test_deterministic(self):
        again = generate(self.conn, seed=7, n_t1=10, n_t2=12, n_t3=8, n_abstain=6)
        self.assertEqual([q["id"] for q in self.qs], [q["id"] for q in again])
        other = generate(self.conn, seed=8, n_t1=10, n_t2=12, n_t3=8, n_abstain=6)
        self.assertNotEqual([q["id"] for q in self.qs], [q["id"] for q in other])

    def test_ids_unique(self):
        ids = [q["id"] for q in self.qs]
        self.assertEqual(len(ids), len(set(ids)))

    def test_answers_match_source_data(self):
        for q in self.qs:
            if q["type"] == "lookup":
                ev = q["evidence"][0]
                self.assertAlmostEqual(q["answer"]["value"], ev["value"] / 1e6)
            if q["type"] == "growth":
                a, b = q["evidence"]
                self.assertAlmostEqual(q["answer"]["value"], round((b["value"] / a["value"] - 1) * 100, 2))
            if q["type"] == "ratio":
                n, d = q["evidence"]
                self.assertAlmostEqual(q["answer"]["value"], round(n["value"] / d["value"] * 100, 2))

    def test_every_answerable_question_has_citations(self):
        for q in self.qs:
            if q["tier"] > 0:
                self.assertTrue(q["evidence"])
                self.assertTrue(all(e["accession"] for e in q["evidence"]))

    def test_known_values(self):
        full = generate(self.conn, seed=7, n_t1=999, n_t2=999, n_t3=999, n_abstain=99)
        growth = next(q for q in full if q["type"] == "growth" and q["company"] == "ALPH" and q["evidence"][1]["metric"] == "revenue"
                      and q["evidence"][1]["period_end"] == "2024-12-31")
        self.assertAlmostEqual(growth["answer"]["value"], 33.33)  # 1800 -> 2400
        ratio = next(q for q in full if q["type"] == "ratio" and q["company"] == "ALPH" and "net profit margin" in q["question"]
                     and "December 31, 2024" in q["question"])
        self.assertAlmostEqual(ratio["answer"]["value"], 25.0)    # 600 / 2400
        compare = next(q for q in full if q["type"] == "compare" and "R&D" not in q["question"]
                       and "research and development" in q["question"] and "December 31, 2024" in q["question"])
        self.assertEqual(compare["answer"]["value"], "ALPH")      # 340/2400=14.2% vs 560/5600=10%
        trend = next(q for q in full if q["type"] == "trend" and q["company"] == "ALPH" and "operating margin" in q["question"])
        self.assertEqual(trend["answer"]["value"], "increased")

    def test_abstain_questions_cover_future_and_outside(self):
        types = {q["type"] for q in self.qs if q["tier"] == 0}
        self.assertEqual(types, {"abstain_future", "abstain_outside_corpus"})

    def test_write_jsonl_roundtrip(self):
        path = Path(self.tmp.name) / "missing_dir" / "q.jsonl"  # parent folder is created on demand
        write_jsonl(self.qs, path)
        loaded = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(loaded, json.loads(json.dumps(self.qs)))
        self.assertIn("questions", summarize(self.qs))


def q_usd(v=60922.0):
    return {"answer": {"kind": "usd_millions", "value": v, "tolerance_rel": 0.005}}


def q_pct(v=25.0):
    return {"answer": {"kind": "percent", "value": v, "tolerance_abs": 0.15}}


class GradingTests(unittest.TestCase):
    def test_usd_scales(self):
        for resp in ["FINAL ANSWER: $60,922 million", "FINAL ANSWER: 60922", "FINAL ANSWER: $60.9 billion",
                     "Revenue was big.\nFINAL ANSWER: 60,922.0M", "FINAL ANSWER: $0.0609 trillion"]:
            with self.subTest(resp=resp):
                self.assertTrue(grade(q_usd(), resp)["correct"], resp)

    def test_usd_wrong(self):
        for resp in ["FINAL ANSWER: $61,500 million", "FINAL ANSWER: 60.9 million", "FINAL ANSWER: $6 billion"]:
            with self.subTest(resp=resp):
                self.assertFalse(grade(q_usd(), resp)["correct"], resp)

    def test_percent(self):
        self.assertTrue(grade(q_pct(25.0), "FINAL ANSWER: 25.1%")["correct"])
        self.assertTrue(grade(q_pct(-12.5), "FINAL ANSWER: -12.4 percent")["correct"])
        self.assertTrue(grade(q_pct(-12.5), "It fell. FINAL ANSWER: −12.5%")["correct"])
        self.assertFalse(grade(q_pct(25.0), "FINAL ANSWER: 26%")["correct"])
        self.assertFalse(grade(q_pct(25.0), "FINAL ANSWER: -25%")["correct"])

    def test_falls_back_to_last_number_without_marker(self):
        self.assertTrue(grade(q_pct(25.0), "In 2024 the margin was 25.0%")["correct"])

    def test_choice_tickers_and_names(self):
        q = {"answer": {"kind": "choice", "value": "NVDA", "options": ["NVDA", "AMD"],
                        "aliases": {"NVDA": ["NVIDIA CORP"], "AMD": ["Advanced Micro Devices, Inc"]}}}
        self.assertTrue(grade(q, "FINAL ANSWER: NVDA")["correct"])
        self.assertTrue(grade(q, "FINAL ANSWER: NVIDIA CORP")["correct"])
        self.assertFalse(grade(q, "FINAL ANSWER: AMD")["correct"])
        self.assertFalse(grade(q, "FINAL ANSWER: NVDA or AMD")["correct"])  # ambiguous

    def test_name_aliases(self):
        from edgar_analyst.questions import name_aliases
        self.assertEqual(name_aliases("NVIDIA CORP"), ["NVIDIA CORP", "NVIDIA"])
        self.assertEqual(name_aliases("Advanced Micro Devices, Inc."), ["Advanced Micro Devices, Inc.", "Advanced Micro Devices", "Advanced"])
        self.assertEqual(name_aliases("Amazon.com, Inc."), ["Amazon.com, Inc.", "Amazon"])
        self.assertNotIn("Alpha", name_aliases("Alpha Beta Corp", other="Alpha Gamma Inc"))  # shared first word dropped
        self.assertIn("Alpha Beta", name_aliases("Alpha Beta Corp", other="Alpha Gamma Inc"))

    def test_choice_accepts_short_name(self):
        from edgar_analyst.questions import name_aliases
        q = {"answer": {"kind": "choice", "value": "NVDA", "options": ["NVDA", "AMD"],
                        "aliases": {"NVDA": name_aliases("NVIDIA CORP", "Advanced Micro Devices, Inc."),
                                    "AMD": name_aliases("Advanced Micro Devices, Inc.", "NVIDIA CORP")}}}
        self.assertTrue(grade(q, "FINAL ANSWER: NVIDIA")["correct"])
        self.assertFalse(grade(q, "FINAL ANSWER: Advanced Micro Devices")["correct"])  # AMD is the wrong company here

    def test_choice_trend_words(self):
        q = {"answer": {"kind": "choice", "value": "increased", "options": ["increased", "decreased"],
                        "aliases": {"increased": ["increase", "rose"], "decreased": ["decrease", "fell"]}}}
        self.assertTrue(grade(q, "FINAL ANSWER: increased")["correct"])
        self.assertTrue(grade(q, "FINAL ANSWER: It rose")["correct"])
        self.assertFalse(grade(q, "FINAL ANSWER: decreased")["correct"])

    def test_abstain(self):
        q = {"answer": {"kind": "abstain", "reason": "x"}}
        self.assertTrue(grade(q, "I cannot answer this: that fiscal year has not been reported yet.")["correct"])
        self.assertFalse(grade(q, "FINAL ANSWER: $5,000 million")["correct"])
        self.assertFalse(grade(q, "Revenue was about $5 billion.")["correct"])

    def test_abstaining_on_answerable_question_is_wrong(self):
        r = grade(q_usd(), "I cannot find that data.")
        self.assertFalse(r["correct"])


if __name__ == "__main__":
    unittest.main()
