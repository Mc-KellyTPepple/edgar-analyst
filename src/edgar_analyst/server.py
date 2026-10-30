"""Small HTTP service around the full agent (standard library only).

  GET  /          a one-page UI
  GET  /health    liveness plus index/limit status
  POST /ask       {"question": "..."} -> {"answer", "final_answer", "sources", "model", "tool_calls", "seconds"}

It runs on a free host with a free Groq key, so it protects the quota: per-IP rate limit, a daily cap on
model calls for the whole service, a question length limit, and the on-disk response cache (repeat
questions cost nothing).
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .agent import run_full_agent
from .grading import cited_accessions, extract_final
from .llm import LLMError

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>EDGAR Analyst</title>
<style>body{font:16px/1.5 system-ui;max-width:42rem;margin:2rem auto;padding:0 1rem}textarea{width:100%;height:5rem;font:inherit}
button{font:inherit;padding:.4rem 1rem;margin-top:.5rem}pre{white-space:pre-wrap;background:#f4f4f4;padding:1rem;border-radius:6px}
@media(prefers-color-scheme:dark){body{background:#111;color:#eee}pre{background:#222}}small{opacity:.7}</style>
<h1>EDGAR Analyst</h1>
<p>Ask about the 10-K filings of NVDA, AMD, INTC, MSFT, AAPL, GOOGL, AMZN, META, TSLA, ORCL. Answers come from SEC data and cite filings.</p>
<textarea id=q placeholder="What was NVIDIA's net profit margin for the fiscal year ended January 28, 2024?"></textarea><br>
<button id=b>Ask</button> <small id=s></small><pre id=o hidden></pre>
<p><small>Free demo on a free Groq key: it may be slow or say it is busy. Not investment advice.</small></p>
<script>b.onclick=async()=>{o.hidden=false;o.textContent='Working... (can take up to a minute)';s.textContent='';
try{const r=await fetch('/ask',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({question:q.value})});
const j=await r.json();o.textContent=r.ok?(j.final_answer+'\\n\\nSources: '+(j.sources.join(', ')||'none')+'\\n\\n'+j.answer):(j.error||'error');
s.textContent=r.ok?j.model+', '+j.seconds+'s':''}catch(e){o.textContent='Request failed: '+e}}</script>"""


class Limits:
    def __init__(self, per_ip_per_hour=10, daily_cap=150, clock=time.time):
        self.per_ip, self.daily_cap, self.clock = per_ip_per_hour, daily_cap, clock
        self.hits: dict[str, deque] = defaultdict(deque)
        self.day, self.count = "", 0
        self.lock = threading.Lock()

    def allow(self, ip: str) -> str | None:
        """None if allowed, otherwise the reason to refuse."""
        with self.lock:
            now = self.clock()
            today = time.strftime("%Y-%m-%d", time.gmtime(now))
            if today != self.day:
                self.day, self.count = today, 0
            if self.count >= self.daily_cap:
                return "The daily free quota for this demo is used up. Please try again tomorrow."
            q = self.hits[ip]
            while q and now - q[0] > 3600:
                q.popleft()
            if len(q) >= self.per_ip:
                return f"Rate limit: at most {self.per_ip} questions per hour per visitor."
            q.append(now)
            self.count += 1
            return None


class App:
    def __init__(self, llm, conn, search, limits: Limits | None = None, max_len: int = 500, runner=run_full_agent):
        self.llm, self.conn, self.search = llm, conn, search
        self.limits, self.max_len, self.runner = limits or Limits(), max_len, runner
        self.lock = threading.Lock()   # one agent run at a time: sqlite connection and the Groq rate limiter are shared

    def ask(self, question: str, ip: str) -> tuple[int, dict]:
        question = (question or "").strip()
        if not question:
            return 400, {"error": "Send a JSON body like {\"question\": \"...\"}."}
        if len(question) > self.max_len:
            return 400, {"error": f"Question too long (max {self.max_len} characters)."}
        refused = self.limits.allow(ip)
        if refused:
            return 429, {"error": refused}
        t0 = time.monotonic()
        try:
            with self.lock:
                run = self.runner(self.llm, question, conn=self.conn, search=self.search)
        except LLMError as exc:
            msg = "The model provider is busy or out of free quota right now. Try again later." if "quota" in str(exc).lower() or "budget" in str(exc).lower() else "The model provider returned an error. Try again."
            return 503, {"error": msg}
        text = run["text"] or ""
        return 200, {"answer": text, "final_answer": extract_final(text), "sources": sorted(cited_accessions(text)),
                     "model": run["model"], "tool_calls": run["tool_calls"], "seconds": round(time.monotonic() - t0, 1)}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: dict) -> None:
            self._send(code, json.dumps(obj).encode())

        def do_GET(self):  # noqa: N802
            if self.path == "/health":
                self._json(200, {"ok": True, "retrieval": getattr(app.search, "mode", "n/a")})
            elif self.path in ("/", "/index.html"):
                self._send(200, PAGE.encode(), "text/html")
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path != "/ask":
                return self._json(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 4096:
                    return self._json(413, {"error": "request too large"})
                data = json.loads(self.rfile.read(n) or b"{}")
                question = data.get("question") if isinstance(data, dict) else None
                if question is not None and not isinstance(question, str):
                    raise ValueError
            except (ValueError, json.JSONDecodeError):
                return self._json(400, {"error": "Send a JSON body like {\"question\": \"...\"}."})
            # behind a proxy (Hugging Face, Render) the client address is the first X-Forwarded-For entry
            fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            code, obj = app.ask(question, fwd or self.client_address[0])
            self._json(code, obj)

        def log_message(self, *a):  # keep logs free of question text
            pass

    return Handler


def serve(app: App, host: str = "0.0.0.0", port: int | None = None) -> None:
    port = port or int(os.environ.get("PORT", "7860"))
    srv = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"listening on {host}:{port}", flush=True)
    srv.serve_forever()
