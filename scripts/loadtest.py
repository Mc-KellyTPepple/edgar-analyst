"""Load test of the HTTP service layer: real server, real sockets, model replaced by a stub that sleeps.

It measures the service (limits, queueing, request handling), NOT the model, and never touches the live deployment or Groq.

    PYTHONPATH=src python scripts/loadtest.py
"""
from __future__ import annotations

import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from edgar_analyst.server import App, Limits, Server, make_handler  # noqa: E402


def start(delay: float, limits: Limits):
    def runner(llm, question, conn=None, search=None):
        time.sleep(delay)
        return {"text": "FINAL ANSWER: 42\nSOURCES: 0000000000-00-000001", "model": "stub", "tool_calls": 1}
    app = App(None, None, None, limits, runner=runner)
    srv = Server(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def post(port: int, ip: str):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/ask", data=json.dumps({"question": "test question"}).encode(),
                                 headers={"Content-Type": "application/json", "X-Forwarded-For": ip})
    t = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    return code, time.monotonic() - t


def burst(port: int, jobs: list[str], workers: int):
    results, lock, q = [], threading.Lock(), list(jobs)

    def work():
        while True:
            with lock:
                if not q:
                    return
                ip = q.pop()
            r = post(port, ip)
            with lock:
                results.append(r)
    t0 = time.monotonic()
    ts = [threading.Thread(target=work) for _ in range(workers)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return results, time.monotonic() - t0


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * (len(xs) - 1)))]


def line(name, results, wall):
    codes = {}
    for c, _ in results:
        codes[c] = codes.get(c, 0) + 1
    lat = [s for _, s in results]
    return f"| {name} | {len(results)} | {codes} | {len(results) / wall:.1f} | {statistics.median(lat) * 1000:.0f} | {pct(lat, 0.95) * 1000:.0f} |"


def main() -> None:
    rows = ["| scenario | requests | status codes | requests/s | median ms | p95 ms |", "|---|---|---|---|---|---|"]
    # A: service overhead, stub answers instantly, 200 distinct visitors, 20 at once
    s = start(0.0, Limits(per_ip_per_hour=10, daily_cap=10_000))
    r, w = burst(s.server_address[1], [f"10.0.{i // 250}.{i % 250}" for i in range(200)], 20)
    rows.append(line("A: 200 visitors, 20 at once, instant model", r, w)); s.shutdown()
    # B: one visitor hammering, limit is 10 per hour
    s = start(0.0, Limits(per_ip_per_hour=10, daily_cap=10_000))
    r, w = burst(s.server_address[1], ["9.9.9.9"] * 60, 10)
    rows.append(line("B: one visitor sends 60 requests", r, w)); s.shutdown()
    # C: daily cap of 25 shared by everyone
    s = start(0.0, Limits(per_ip_per_hour=10, daily_cap=25))
    r, w = burst(s.server_address[1], [f"8.8.{i // 250}.{i % 250}" for i in range(60)], 10)
    rows.append(line("C: 60 visitors, daily cap 25", r, w)); s.shutdown()
    # D: model takes 0.5 s; runs are serialised because the SQLite connection and Groq limiter are shared
    s = start(0.5, Limits(per_ip_per_hour=10, daily_cap=10_000))
    r, w = burst(s.server_address[1], [f"7.7.7.{i}" for i in range(12)], 12)
    rows.append(line("D: 12 simultaneous visitors, 0.5 s model", r, w)); s.shutdown()
    print("\n".join(rows))
    Path(__file__).resolve().parents[1].joinpath("evals/results/LOADTEST.md").write_text(
        "# Load test of the service layer\n\nReal HTTP server on localhost with the model replaced by a stub. It measures limits and queueing, not model speed, and was never run against the live service or Groq.\n\n"
        + "\n".join(rows) + "\n\nExpected: A all 200 OK; B 10 OK and 50 rate-limited (429); C 25 OK and 35 refused (429); D about 12 x 0.5 s = 6 s wall time because one question runs at a time, so the last visitor waits for everyone ahead.\n")


if __name__ == "__main__":
    main()
