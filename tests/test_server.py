import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from edgar_analyst.llm import LLMError
from edgar_analyst.server import App, Limits, make_handler


def fake_runner(llm, question, conn=None, search=None, **_):
    if "crash" in question:
        raise ConnectionError("secret internal detail")
    if "boom" in question:
        raise LLMError("every model in the chain is out of quota for today")
    return {"text": "Reasoning.\nFINAL ANSWER: 42\nSOURCES: 0000000001-24-000001", "model": "fake", "tool_calls": 2}


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


class LimitsTests(unittest.TestCase):
    def test_per_ip_hourly_limit_and_expiry(self):
        clk = Clock()
        lim = Limits(per_ip_per_hour=2, daily_cap=100, clock=clk)
        self.assertIsNone(lim.allow("a"))
        self.assertIsNone(lim.allow("a"))
        self.assertIn("Rate limit", lim.allow("a"))
        self.assertIsNone(lim.allow("b"))          # other visitors unaffected
        clk.t += 3601
        self.assertIsNone(lim.allow("a"))          # window slid

    def test_daily_cap_and_reset(self):
        clk = Clock()
        lim = Limits(per_ip_per_hour=100, daily_cap=2, clock=clk)
        lim.allow("a"); lim.allow("b")
        self.assertIn("daily", lim.allow("c"))
        clk.t += 86400
        self.assertIsNone(lim.allow("c"))


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = App(None, None, None, Limits(per_ip_per_hour=3, daily_cap=50), max_len=100, runner=fake_runner)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def call(self, path, body=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, data=data, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_health_and_page(self):
        code, body = self.call("/health")
        self.assertEqual((code, json.loads(body)["ok"]), (200, True))
        code, body = self.call("/")
        self.assertEqual(code, 200)
        self.assertIn(b"EDGAR Analyst", body)
        self.assertEqual(self.call("/nope")[0], 404)

    def test_ask_success_shape(self):
        code, body = self.call("/ask", {"question": "What is 6*7?"}, {"X-Forwarded-For": "9.9.9.1"})
        j = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(j["final_answer"], "42")
        self.assertEqual(j["sources"], ["0000000001-24-000001"])
        self.assertEqual(j["model"], "fake")

    def test_bad_inputs(self):
        h = {"X-Forwarded-For": "9.9.9.2"}
        self.assertEqual(self.call("/ask", raw=b"not json", headers=h)[0], 400)
        self.assertEqual(self.call("/ask", {"question": 5}, h)[0], 400)
        self.assertEqual(self.call("/ask", {"question": ""}, h)[0], 400)
        self.assertEqual(self.call("/ask", {"question": "x" * 101}, h)[0], 400)
        self.assertEqual(self.call("/ask", raw=b"x" * 5000, headers=h)[0], 413)

    def test_provider_quota_error_is_friendly_503(self):
        code, body = self.call("/ask", {"question": "boom"}, {"X-Forwarded-For": "9.9.9.3"})
        self.assertEqual(code, 503)
        self.assertIn("out of free quota", json.loads(body)["error"])
        self.assertNotIn("chain", json.loads(body)["error"])   # no internals leaked

    def test_unexpected_errors_do_not_leak(self):
        code, body = self.call("/ask", {"question": "crash"}, {"X-Forwarded-For": "9.9.9.5"})
        self.assertEqual(code, 502)
        self.assertNotIn(b"secret", body)

    def test_rate_limit_per_forwarded_ip(self):
        h = {"X-Forwarded-For": "9.9.9.4, 10.0.0.1"}
        codes = [self.call("/ask", {"question": "q"}, h)[0] for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])


if __name__ == "__main__":
    unittest.main()
