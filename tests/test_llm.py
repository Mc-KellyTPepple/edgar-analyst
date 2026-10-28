import json
import tempfile
import unittest
from pathlib import Path

from edgar_analyst.llm import LLM, BudgetExhausted, CacheMiss, Ledger, LLMError, ModelLimits, RateLimiter


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def ok(text="hi", tokens=20, tool_calls=None):
    msg = {"role": "assistant", "content": text}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return Resp(200, {"choices": [{"message": msg}], "usage": {"prompt_tokens": tokens - 5, "completion_tokens": 5, "total_tokens": tokens}})


class FakeNet:
    """Scripted transport: pops one response per call and records the request bodies."""

    def __init__(self, *responses):
        self.responses, self.bodies = list(responses), []

    def __call__(self, url, headers, body, timeout):
        self.bodies.append(json.loads(json.dumps(body)))  # snapshot: the client may mutate its dict
        return self.responses.pop(0)


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


MSG = [{"role": "user", "content": "hello"}]


def make(net, tmp=None, **kw):
    clk = Clock()
    llm = LLM("key", post=net, clock=clk.now, sleep=clk.sleep, today=lambda: "2026-01-01",
              cache_dir=Path(tmp) / "cache" if tmp else None, ledger_path=Path(tmp) / "ledger.json" if tmp else None, **kw)
    return llm, clk


class RateLimiterTests(unittest.TestCase):
    def test_waits_for_request_slot(self):
        clk = Clock()
        rl = RateLimiter(rpm=10, tpm=10**9, clock=clk.now, sleep=clk.sleep, margin=1.0)
        for _ in range(10):
            rl.acquire(1)
        self.assertEqual(clk.slept, [])
        rl.acquire(1)  # eleventh inside the window must wait for the first to age out
        self.assertGreaterEqual(clk.t, 60)

    def test_waits_for_token_budget(self):
        clk = Clock()
        rl = RateLimiter(rpm=100, tpm=1000, clock=clk.now, sleep=clk.sleep, margin=1.0)
        rl.acquire(800)
        rl.acquire(300)
        self.assertGreaterEqual(clk.t, 60)

    def test_settle_frees_overestimated_tokens(self):
        clk = Clock()
        rl = RateLimiter(rpm=100, tpm=1000, clock=clk.now, sleep=clk.sleep, margin=1.0)
        e = rl.acquire(900)
        rl.settle(e, 100)
        rl.acquire(800)
        self.assertEqual(clk.slept, [])

    def test_oversized_request_does_not_hang(self):
        clk = Clock()
        rl = RateLimiter(rpm=10, tpm=1000, clock=clk.now, sleep=clk.sleep, margin=1.0)
        rl.acquire(50_000)


class ChatTests(unittest.TestCase):
    def test_success_and_cache_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            net = FakeNet(ok("first"))
            llm, _ = make(net, tmp)
            a = llm.chat(MSG)
            b = llm.chat(MSG)
            self.assertEqual((a["message"]["content"], a["cached"]), ("first", False))
            self.assertEqual((b["message"]["content"], b["cached"]), ("first", True))
            self.assertEqual(len(net.bodies), 1)
            self.assertEqual(llm.usage.requests, 1)
            self.assertEqual(llm.usage.cache_hits, 1)

    def test_replay_mode_never_hits_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm, _ = make(FakeNet(ok("recorded")), tmp)
            llm.chat(MSG)
            net2 = FakeNet()
            clk = Clock()
            replay = LLM(None, mode="replay", cache_dir=Path(tmp) / "cache", post=net2, clock=clk.now, sleep=clk.sleep)
            self.assertEqual(replay.chat(MSG)["message"]["content"], "recorded")
            with self.assertRaises(CacheMiss):
                replay.chat([{"role": "user", "content": "other"}])
            self.assertEqual(net2.bodies, [])

    def test_live_mode_requires_key(self):
        with self.assertRaises(LLMError):
            LLM(None)

    def test_retry_after_on_per_minute_429(self):
        rate = Resp(429, {"error": {"message": "Rate limit reached on tokens per minute (TPM)"}}, {"retry-after": "7"})
        llm, clk = make(FakeNet(rate, ok("done")))
        out = llm.chat(MSG)
        self.assertEqual(out["message"]["content"], "done")
        self.assertIn(7.0, clk.slept)
        self.assertEqual(llm.usage.retries, 1)

    def test_daily_limit_falls_back_to_next_model(self):
        daily = Resp(429, {"error": {"message": "Rate limit reached on requests per day (RPD): Limit 1000"}}, {"retry-after": "3600"})
        net = FakeNet(daily, ok("from fallback"))
        llm, _ = make(net, models=["m1", "m2"])
        out = llm.chat(MSG)
        self.assertEqual((out["model"], out["message"]["content"]), ("m2", "from fallback"))
        self.assertEqual([b["model"] for b in net.bodies], ["m1", "m2"])
        self.assertTrue(llm.ledger.exhausted("m1"))
        # next call skips the exhausted model without spending a request on it
        net.responses.append(ok("again"))
        llm.chat([{"role": "user", "content": "second"}])
        self.assertEqual(net.bodies[-1]["model"], "m2")

    def test_all_models_exhausted(self):
        daily = lambda: Resp(429, {"error": {"message": "tokens per day (TPD)"}})  # noqa: E731
        llm, _ = make(FakeNet(daily(), daily()), models=["m1", "m2"])
        with self.assertRaises(BudgetExhausted):
            llm.chat(MSG)

    def test_run_budget(self):
        llm, _ = make(FakeNet(ok(), ok()), max_requests=1)
        llm.chat(MSG)
        with self.assertRaises(BudgetExhausted):
            llm.chat([{"role": "user", "content": "again"}])

    def test_ledger_persists_and_enforces_daily_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            net = FakeNet(ok(), ok())
            lim = {"m1": ModelLimits(30, 10_000, 2)}
            llm, _ = make(net, tmp, models=["m1"], limits=lim)
            llm.chat(MSG)
            # a new process on the same day sees the saved count; 1 of 2 used and the 95% cap is 1.9
            llm2, _ = make(net, tmp, models=["m1"], limits=lim)
            self.assertEqual(llm2.ledger.used("m1"), 1)
            llm2.chat([{"role": "user", "content": "b"}])
            llm3, _ = make(net, tmp, models=["m1"], limits=lim)
            with self.assertRaises(BudgetExhausted):
                llm3.chat([{"role": "user", "content": "c"}])

    def test_ledger_resets_on_new_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "l.json"
            Ledger(path, today=lambda: "2026-01-01").add("m1", 5)
            self.assertEqual(Ledger(path, today=lambda: "2026-01-02").used("m1"), 0)

    def test_server_error_retried_then_success(self):
        llm, clk = make(FakeNet(Resp(503, {"error": "busy"}), ok("fine")))
        self.assertEqual(llm.chat(MSG)["message"]["content"], "fine")
        self.assertTrue(clk.slept)

    def test_reasoning_effort_dropped_when_rejected(self):
        bad = Resp(400, {"error": {"message": "reasoning_effort is not supported with this model"}})
        net = FakeNet(bad, ok("fine"))
        llm, _ = make(net, models=["openai/gpt-oss-120b"])
        llm.chat(MSG)
        self.assertIn("reasoning_effort", net.bodies[0])
        self.assertNotIn("reasoning_effort", net.bodies[1])

    def test_unavailable_model_is_skipped(self):
        gone = Resp(404, {"error": {"message": "The model `m1` does not exist or you do not have access to it.", "code": "model_not_found"}})
        net = FakeNet(gone, ok("a"), ok("b"))
        llm, _ = make(net, models=["m1", "m2"])
        self.assertEqual(llm.chat(MSG)["model"], "m2")
        llm.chat([{"role": "user", "content": "x"}])
        self.assertEqual([b["model"] for b in net.bodies], ["m1", "m2", "m2"])  # m1 not retried

    def test_other_client_errors_raise(self):
        llm, _ = make(FakeNet(Resp(401, {"error": {"message": "invalid key"}})))
        with self.assertRaises(LLMError):
            llm.chat(MSG)

    def test_tools_are_sent_and_returned(self):
        call = [{"id": "c1", "type": "function", "function": {"name": "calculate", "arguments": "{\"expression\": \"1+1\"}"}}]
        net = FakeNet(ok(None, tool_calls=call))
        llm, _ = make(net)
        out = llm.chat(MSG, tools=[{"type": "function", "function": {"name": "calculate"}}])
        self.assertEqual(out["message"]["tool_calls"][0]["id"], "c1")
        self.assertEqual(net.bodies[0]["tool_choice"], "auto")


if __name__ == "__main__":
    unittest.main()
