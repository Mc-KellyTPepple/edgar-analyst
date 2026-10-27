import sqlite3
import tempfile
import unittest
from pathlib import Path

from edgar_analyst.db import QueryError, build_db, open_readonly, run_readonly_query
from edgar_analyst.facts import extract_10k_filings, extract_annual_facts

from .helpers import ALPHA, ALPHA_ENDS, BETA, BETA_ENDS, make_companyfacts


def make_db(tmp: Path) -> Path:
    fa = extract_annual_facts(make_companyfacts(1, "Alpha Corp", ALPHA_ENDS, ALPHA))
    fb = extract_annual_facts(make_companyfacts(2, "Beta Inc", BETA_ENDS, BETA))
    path = tmp / "facts.db"
    build_db(path, [(1, "ALPH", "Alpha Corp"), (2, "BETA", "Beta Inc")], {1: fa, 2: fb})
    return path


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.facts = extract_annual_facts(make_companyfacts(1, "Alpha Corp", ALPHA_ENDS, ALPHA))

    def test_one_value_per_metric_and_period(self):
        keys = [(f.metric, f.period_end) for f in self.facts]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(self.facts), 6 * 5)

    def test_ignores_quarterly_and_10q(self):
        rev_2024 = [f for f in self.facts if f.metric == "revenue" and f.period_end == "2024-12-31"]
        self.assertEqual(len(rev_2024), 1)
        self.assertEqual(rev_2024[0].value, 2400 * 1e6)

    def test_latest_restatement_wins(self):
        rev_2020 = next(f for f in self.facts if f.metric == "revenue" and f.period_end == "2020-12-31")
        self.assertEqual(rev_2020.value, 1005 * 1e6)
        self.assertEqual(rev_2020.accession, "restated")

    def test_balance_sheet_items_are_instants(self):
        ta = next(f for f in self.facts if f.metric == "total_assets")
        self.assertIsNone(ta.period_start)

    def test_extract_10k_filings(self):
        sub = {"filings": {"recent": {
            "form": ["10-K", "10-Q", "8-K", "10-K"],
            "accessionNumber": ["a-1", "a-2", "a-3", "a-4"],
            "filingDate": ["2025-02-20", "2025-05-01", "2025-06-01", "2024-02-21"],
            "reportDate": ["2024-12-31", "2025-03-31", "", "2023-12-31"],
            "primaryDocument": ["x.htm", "y.htm", "z.htm", "w.htm"]}}}
        out = extract_10k_filings(sub)
        self.assertEqual([f["accession"] for f in out], ["a-1", "a-4"])


class ReadonlyQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = open_readonly(make_db(Path(self.tmp.name)))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_select_works(self):
        res = run_readonly_query(self.conn, "SELECT ticker, value FROM financials WHERE metric='revenue' AND period_end='2024-12-31'")
        self.assertEqual(res["rows"], [["ALPH", 2400 * 1e6]])
        self.assertEqual(res["columns"], ["ticker", "value"])

    def test_cte_works(self):
        res = run_readonly_query(self.conn, "WITH r AS (SELECT value v FROM financials WHERE ticker='ALPH' AND metric='revenue') SELECT COUNT(*) FROM r")
        self.assertEqual(res["rows"], [[5]])

    def test_writes_and_schema_changes_rejected(self):
        for sql in ["DELETE FROM facts", "DROP TABLE facts", "INSERT INTO companies VALUES (9,'X','x')",
                    "UPDATE facts SET value=0", "CREATE TABLE t(x)", "ATTACH DATABASE ':memory:' AS m",
                    "PRAGMA writable_schema=ON", "VACUUM"]:
            with self.subTest(sql=sql), self.assertRaises(QueryError):
                run_readonly_query(self.conn, sql)

    def test_multiple_statements_rejected(self):
        with self.assertRaises(QueryError):
            run_readonly_query(self.conn, "SELECT 1; DROP TABLE facts")

    def test_data_unchanged_after_attacks(self):
        for sql in ["DELETE FROM facts", "DROP TABLE facts"]:
            with self.assertRaises(QueryError):
                run_readonly_query(self.conn, sql)
        self.assertEqual(run_readonly_query(self.conn, "SELECT COUNT(*) FROM facts")["rows"][0][0], 60)

    def test_row_cap_and_truncation(self):
        res = run_readonly_query(self.conn, "SELECT * FROM facts", max_rows=10)
        self.assertEqual(len(res["rows"]), 10)
        self.assertTrue(res["truncated"])

    def test_runaway_query_times_out(self):
        slow = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT COUNT(*) FROM c"
        with self.assertRaises(QueryError):
            run_readonly_query(self.conn, slow, timeout_s=0.3)


if __name__ == "__main__":
    unittest.main()
