import json
import tempfile
import unittest
from pathlib import Path

from edgar_analyst.agent import run_closed_book, run_facts_agent, run_tool, safe_calculate
from edgar_analyst.db import open_readonly
from edgar_analyst.evalrun import check_gate, load_results, markdown_table, run_eval, summarize_results
from edgar_analyst.llm import BudgetExhausted

from .test_facts_db import make_db


def reply(content=None, calls=None):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [{"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
                             for i, (n, a) in enumerate(calls)]
    return {"message": msg, "usage": {"prompt_tokens": 100, "completion_tokens": 10}, "model": "fake", "cached": False}


class ScriptedLLM:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def chat(self, messages, tools=None, **kw):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class CalculatorTests(unittest.TestCase):
    def test_arithmetic(self):
        self.assertAlmostEqual(safe_calculate("(2400-1800)/1800*100"), 33.3333, places=3)
        self.assertEqual(safe_calculate("round(2/3, 2)"), 0.67)
        self.assertEqual(safe_calculate("-5 + abs(-2)"), -3)

    def test_rejects_code(self):
        for bad in ["__import__('os').system('ls')", "open('x')", "[1,2]", "1 if 1 else 2", "x+1", "2**1000", "'a'*5"]:
            with self.assertRaises((ValueError, SyntaxError), msg=bad):
                safe_calculate(bad)

    def test_tool_errors_become_text(self):
        self.assertTrue(run_tool("calculate", {"expression": "1/0"}, None).startswith("error"))
        self.assertTrue(run_tool("nope", {}, None).startswith("error"))


class AgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.conn = open_readonly(make_db(Path(cls.tmp.name)))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def test_sql_tool_uses_value_musd_and_rejects_writes(self):
        out = run_tool("query_facts", {"sql": "SELECT ticker, period_end, value_musd FROM financials WHERE metric='revenue' ORDER BY period_end LIMIT 2"}, self.conn)
        self.assertIn("value_musd", out)
        self.assertIn("1005", out)
        self.assertTrue(run_tool("query_facts", {"sql": "DELETE FROM companies"}, self.conn).startswith("error"))

    def test_financial_ratio_picks_nearest_fiscal_year(self):
        # synthetic data: ALPH net income / revenue = 120/1000 (FY2020 restated revenue 1005), BETA fiscal year ends late January
        out = run_tool("financial_ratio", {"ticker": "ALPH", "ratio": "net_margin", "as_of": "2021-01-15"}, self.conn)
        self.assertIn("fiscal year ended 2020-12-31", out)
        self.assertIn("accessions: 000", out)
        beta = run_tool("financial_ratio", {"ticker": "beta", "ratio": "net_margin", "as_of": "2021-01-15"}, self.conn)
        self.assertIn("fiscal year ended 2021-01-31", beta)
        self.assertIn("%", beta)

    def test_financial_ratio_generic_pair_and_errors(self):
        ok = run_tool("financial_ratio", {"ticker": "ALPH", "ratio": "rd_expense/operating_income", "as_of": "2024-12-31"}, self.conn)
        self.assertIn("rd_expense 340 / operating_income 720", ok)
        far = run_tool("financial_ratio", {"ticker": "ALPH", "ratio": "net_margin", "as_of": "2030-01-01"}, self.conn)
        self.assertIn("Available period_end dates", far)
        self.assertTrue(run_tool("financial_ratio", {"ticker": "ALPH", "ratio": "bogus", "as_of": "2024-12-31"}, self.conn).startswith("error"))
        self.assertTrue(run_tool("financial_ratio", {"ticker": "ALPH", "ratio": "net_margin", "as_of": "last year"}, self.conn).startswith("error"))
        self.assertIn("not in the data", run_tool("financial_ratio", {"ticker": "ZZZ", "ratio": "net_margin", "as_of": "2024-12-31"}, self.conn))

    def test_empty_final_reply_is_recovered_from_digest(self):
        llm = ScriptedLLM(reply(calls=[("calculate", {"expression": "2+2"})]), reply(""), reply("FINAL ANSWER: 4"))
        r = run_facts_agent(llm, "q?", self.conn)
        self.assertIn("FINAL ANSWER: 4", r["text"])
        self.assertEqual(r["calls"], 3)

    def test_agent_tool_loop(self):
        llm = ScriptedLLM(
            reply(calls=[("query_facts", {"sql": "SELECT value_musd FROM financials LIMIT 1"})]),
            reply(calls=[("calculate", {"expression": "2+2"})]),
            reply("FINAL ANSWER: 4\nSOURCES: 0000000001-20-000000"),
        )
        r = run_facts_agent(llm, "q?", self.conn)
        self.assertEqual((r["steps"], r["tool_calls"], r["calls"]), (3, 2, 3))
        self.assertEqual(r["prompt_tokens"], 300)
        self.assertEqual([t["tool"] for t in r["trace"]], ["query_facts", "calculate"])
        self.assertEqual(r["trace"][1]["out"], "4")
        tool_msgs = [m for m in llm.calls[2]["messages"] if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 2)

    def test_agent_forced_to_finish(self):
        loop = [reply(calls=[("calculate", {"expression": "1+1"})]) for _ in range(5)] + [reply("FINAL ANSWER: 2")]
        llm = ScriptedLLM(*loop)
        r = run_facts_agent(llm, "q?", self.conn, max_steps=6)
        self.assertEqual(r["steps"], 6)
        self.assertIsNone(llm.calls[-1]["tools"])  # last step offers no tools, so it must answer
        self.assertIn("FINAL ANSWER", r["text"])

    def test_bad_tool_arguments_do_not_crash(self):
        msg = reply(calls=[("calculate", {})])
        msg["message"]["tool_calls"][0]["function"]["arguments"] = "{not json"
        llm = ScriptedLLM(msg, reply("FINAL ANSWER: 1"))
        r = run_facts_agent(llm, "q?", self.conn)
        self.assertEqual(r["tool_calls"], 1)

    def test_malformed_tool_call_finishes_from_a_flat_digest(self):
        from edgar_analyst.llm import LLMError
        bad = LLMError('Groq returned 400: {"code": "tool_use_failed"}')
        llm = ScriptedLLM(reply(calls=[("calculate", {"expression": "2+2"})]), bad, bad, reply("FINAL ANSWER: 4"))
        r = run_facts_agent(llm, "q?", self.conn)
        self.assertIn("FINAL ANSWER", r["text"])
        flat = llm.calls[-1]["messages"]
        self.assertTrue(all(m["role"] in ("system", "user") for m in flat))   # no tool-call history
        self.assertIn("4", flat[1]["content"])

    def test_parse_failure_and_repeat_failure_are_both_recovered(self):
        from edgar_analyst.llm import LLMError
        llm = ScriptedLLM(reply(calls=[("calculate", {"expression": "1+1"})]),
                          LLMError("Groq returned 400: Parsing failed. The model generated output that could not be parsed"),
                          LLMError("Groq returned 400: tool_use_failed"), reply("FINAL ANSWER: 2"))
        r = run_facts_agent(llm, "q?", self.conn)
        self.assertIn("FINAL ANSWER", r["text"])
        self.assertEqual(r["calls"], 2)

    def test_unrelated_errors_still_raise(self):
        from edgar_analyst.llm import LLMError
        llm = ScriptedLLM(LLMError("Groq returned 401: invalid key"))
        with self.assertRaises(LLMError):
            run_facts_agent(llm, "q?", self.conn)

    def test_malformed_tool_call_is_resampled_first(self):
        from edgar_analyst.llm import LLMError
        llm = ScriptedLLM(LLMError('Groq returned 400: {"code": "tool_use_failed"}'), reply("FINAL ANSWER: 7"))
        r = run_facts_agent(llm, "q?", self.conn)
        self.assertEqual(r["text"], "FINAL ANSWER: 7")
        self.assertIsNotNone(llm.calls[1]["tools"])   # same step again with tools, not the flat digest

    def test_ungrounded_refusal_is_challenged_once(self):
        llm = ScriptedLLM(reply("I cannot answer."),
                          reply(calls=[("lookup_fact", {"ticker": "ALPH", "metric": "revenue", "period_end": "2024"})]),
                          reply("FINAL ANSWER: 2400"))
        r = run_facts_agent(llm, "q?", self.conn)
        self.assertEqual(r["text"], "FINAL ANSWER: 2400")
        self.assertTrue(r["challenged"])
        self.assertIn("No tool has reported missing data", llm.calls[1]["messages"][-1]["content"])

    def test_grounded_refusal_is_accepted(self):
        llm = ScriptedLLM(reply(calls=[("lookup_fact", {"ticker": "ZZZ", "metric": "revenue", "period_end": "2024"})]),
                          reply("I cannot answer: not in the data."))
        r = run_facts_agent(llm, "q?", self.conn)
        self.assertEqual(r["calls"], 2)
        self.assertFalse(r["challenged"])

    def test_refusal_challenged_at_most_once(self):
        llm = ScriptedLLM(reply("I cannot answer."), reply("I cannot answer."))
        r = run_facts_agent(llm, "q?", self.conn)
        self.assertEqual(r["text"], "I cannot answer.")
        self.assertEqual(r["calls"], 2)

    def test_last_step_uses_flat_digest(self):
        loop = [reply(calls=[("calculate", {"expression": "1+1"})]) for _ in range(5)] + [reply("FINAL ANSWER: 2")]
        llm = ScriptedLLM(*loop)
        run_facts_agent(llm, "q?", self.conn, max_steps=6)
        self.assertIsNone(llm.calls[-1]["tools"])
        self.assertTrue(all(m["role"] in ("system", "user") for m in llm.calls[-1]["messages"]))

    def test_closed_book_has_no_tools(self):
        llm = ScriptedLLM(reply("FINAL ANSWER: 5"))
        r = run_closed_book(llm, "q?")
        self.assertIsNone(llm.calls[0]["tools"])
        self.assertEqual(r["tool_calls"], 0)


QS = [
    {"id": "a", "tier": 1, "type": "lookup", "question": "Alpha net income FY2024 (USD millions)?",
     "answer": {"kind": "usd_millions", "tolerance_rel": 0.005, "value": 600.0}, "evidence": [{"accession": "0000000001-24-000004"}]},
    {"id": "b", "tier": 1, "type": "lookup", "question": "Alpha revenue FY2024?",
     "answer": {"kind": "usd_millions", "tolerance_rel": 0.005, "value": 2400.0}, "evidence": [{"accession": "0000000001-24-000004"}]},
    {"id": "c", "tier": 0, "type": "abstain", "question": "Alpha revenue FY2031?",
     "answer": {"kind": "abstain"}, "evidence": []},
]


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "results" / "closed_book.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_grading_and_resume(self):
        llm = ScriptedLLM(reply("FINAL ANSWER: $600 million"), BudgetExhausted("quota"))
        s1 = run_eval(QS, "closed_book", llm, None, self.out, progress=lambda *_: None)
        self.assertEqual(s1["stopped"], "quota")
        self.assertEqual(s1["remaining"], 2)
        self.assertEqual(list(load_results(self.out)), ["a"])
        llm2 = ScriptedLLM(reply("FINAL ANSWER: 2400 million"), reply("I cannot answer that."))
        s2 = run_eval(QS, "closed_book", llm2, None, self.out, progress=lambda *_: None)
        self.assertEqual((s2["attempted"], s2["remaining"]), (2, 0))   # 'a' was not re-run
        res = load_results(self.out)
        self.assertTrue(all(r["correct"] for r in res.values()), res)
        self.assertTrue(res["c"]["abstained"])

    def test_errors_are_recorded_not_fatal(self):
        llm = ScriptedLLM(RuntimeError("boom"), reply("FINAL ANSWER: 2400"), reply("I cannot answer that."))
        run_eval(QS, "closed_book", llm, None, self.out, progress=lambda *_: None)
        res = load_results(self.out)
        self.assertEqual(res["a"]["reason"], "error")
        self.assertIn("boom", res["a"]["error"])
        self.assertTrue(res["b"]["correct"])

    def test_failed_questions_are_retried(self):
        run_eval(QS[:1], "closed_book", ScriptedLLM(RuntimeError("boom")), None, self.out, progress=lambda *_: None)
        s = run_eval(QS[:1], "closed_book", ScriptedLLM(reply("FINAL ANSWER: 600")), None, self.out, progress=lambda *_: None)
        res = load_results(self.out)
        self.assertEqual((s["attempted"], s["remaining"], res["a"]["correct"]), (1, 0, True))
        self.assertEqual(len(self.out.read_text().splitlines()), 1)

    def test_false_refusals_are_archived_for_rerun(self):
        from edgar_analyst.evalrun import drop_false_refusals
        llm = ScriptedLLM(reply("I cannot answer."), reply("FINAL ANSWER: 2400"), reply("I cannot answer."))
        run_eval(QS, "closed_book", llm, None, self.out, progress=lambda *_: None)
        arch = self.out.parent / "archive" / "x.jsonl"
        self.assertEqual(drop_false_refusals(self.out, arch, {1, 2, 3}), 1)
        self.assertEqual(sorted(load_results(self.out)), ["b", "c"])         # 'a' dropped; correct abstain 'c' kept
        self.assertEqual(len(arch.read_text().splitlines()), 1)
        s = run_eval(QS, "closed_book", ScriptedLLM(reply("FINAL ANSWER: 600")), None, self.out, progress=lambda *_: None)
        self.assertEqual((s["attempted"], s["remaining"]), (1, 0))
        self.assertEqual(drop_false_refusals(self.out, arch, {1, 2, 3}), 0)

    def test_redo_tiers_archives_and_reruns(self):
        from edgar_analyst.evalrun import drop_tier_results
        run_eval(QS, "closed_book", ScriptedLLM(reply("FINAL ANSWER: 600"), reply("FINAL ANSWER: 2400"), reply("I cannot answer.")),
                 None, self.out, progress=lambda *_: None)
        arch = self.out.parent / "archive" / "prev.jsonl"
        self.assertEqual(drop_tier_results(self.out, arch, {1}), 2)
        self.assertEqual(list(load_results(self.out)), ["c"])
        self.assertEqual(len(arch.read_text().splitlines()), 2)

    def test_redo_by_id(self):
        from edgar_analyst.evalrun import drop_tier_results
        run_eval(QS, "closed_book", ScriptedLLM(reply("FINAL ANSWER: 600"), reply("FINAL ANSWER: 2400"), reply("I cannot answer.")),
                 None, self.out, progress=lambda *_: None)
        self.assertEqual(drop_tier_results(self.out, self.out.parent / "a.jsonl", set(), {"b"}), 1)
        self.assertEqual(sorted(load_results(self.out)), ["a", "c"])

    def test_calculator_error_is_not_a_grounded_refusal(self):
        llm = ScriptedLLM(reply(calls=[("calculate", {"expression": "(1/??)"})]), reply("I cannot answer."),
                          reply(calls=[("lookup_fact", {"ticker": "ALPH", "metric": "revenue", "period_end": "2024"})]), reply("FINAL ANSWER: 2400"))
        r = run_facts_agent(llm, "q?", self.conn)
        self.assertTrue(r["challenged"])
        self.assertEqual(r["text"], "FINAL ANSWER: 2400")

    def test_tier_filter_and_limit(self):
        llm = ScriptedLLM(reply("FINAL ANSWER: 1"))
        s = run_eval(QS, "closed_book", llm, None, self.out, tiers={1}, limit=1, progress=lambda *_: None)
        self.assertEqual(s["attempted"], 1)
        self.assertEqual(s["remaining"], 1)

    def test_citation_recall(self):
        llm = ScriptedLLM(reply("FINAL ANSWER: 600\nSOURCES: 0000000001-24-000004"))
        run_eval(QS[:1], "closed_book", llm, None, self.out, progress=lambda *_: None)
        self.assertEqual(load_results(self.out)["a"]["citation_recall"], 1.0)


def row(tier, correct, abstained=False, error=None, recall=None, tools=0):
    return {"id": f"{tier}{correct}{abstained}{tools}", "tier": tier, "correct": correct, "abstained": abstained,
            "error": error, "citation_recall": recall, "tool_calls": tools, "calls": 2, "prompt_tokens": 90,
            "completion_tokens": 10, "seconds": 1.5, "model": "fake"}


class ReportTests(unittest.TestCase):
    def test_summary_gate_and_table(self):
        rows = [row(1, True, recall=1.0), row(1, True, recall=1.0), row(2, False, True), row(3, True, recall=0.5),
                row(0, True, True), row(0, False)]
        s = summarize_results(rows)
        self.assertEqual(s["accuracy_by_tier"], {"1": 100.0, "2": 0.0, "3": 100.0, "4": None})
        self.assertEqual(s["accuracy_answerable"], 75.0)
        self.assertEqual(s["correct_refusal_rate"], 50.0)
        self.assertEqual(s["false_refusal_rate"], 25.0)
        self.assertEqual(s["citation_recall"], 83.3)
        self.assertEqual(s["mean_tokens"], 100)
        self.assertEqual(check_gate(s, 70.0), [])
        self.assertTrue(check_gate(s, 80.0))
        self.assertTrue(check_gate(s, 70.0, min_refusal=90.0))
        self.assertTrue(check_gate({}, 50.0))
        self.assertIn("| closed_book |", markdown_table({"closed_book": s}))
        self.assertIn("| closed_book | fake | 4 | 75.0% |", markdown_table({"closed_book": s}))


if __name__ == "__main__":
    unittest.main()
