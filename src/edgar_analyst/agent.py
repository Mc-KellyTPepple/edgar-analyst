"""Answer systems under evaluation.

* closed_book: the model answers from memory, no data access (the baseline).
* facts_agent: the model plans, queries the facts database with read-only SQL and does
  arithmetic with a calculator, then cites the filings it used.

Prompts are kept short on purpose: on Groq's free tier, tokens per minute is the
binding limit.
"""
from __future__ import annotations

import ast
import json
import operator
import re

from .llm import LLMError
from .db import QueryError, run_readonly_query
from .facts import METRICS

METRIC_NAMES = ", ".join(METRICS)

_RULES = f"""You answer questions about US public companies from a read-only SQLite database of annual (10-K) figures{{extra}}.
View: financials(ticker, name, metric, period_start, period_end, value, value_musd, accession).
value_musd is US dollars in millions; period_end is the fiscal year end date; metric is one of: {METRIC_NAMES}.
Rules:
- Never guess a figure. Read each figure with lookup_fact (it returns the accession) or query_facts, and do all arithmetic with calculate.
- Fiscal years differ by company, so match periods by period_end date.
{{more}}- If the data cannot answer (future period, company or metric not present), say you cannot answer.
- Finish with exactly two lines:
FINAL ANSWER: <answer in the unit the question asks for>
SOURCES: <the accession numbers (like 0001045810-24-000029) returned by the tools for the figures you used, comma separated>"""

FACTS_SYSTEM = _RULES.format(extra="", more="")
FULL_SYSTEM = _RULES.format(
    extra=" and a search index over the text of those 10-Ks (Item 1 Business, Item 1A Risk Factors, Item 7 MD&A)",
    more="- For facts only stated in the filing text (employees, risks, strategy), use search_filings with ticker and year, "
         "and answer only from the passages returned.\n")

CLOSED_BOOK_SYSTEM = """Answer the question about a US public company from memory only. You have no tools.
If you do not know or the information does not exist, say you cannot answer.
Finish with one line: FINAL ANSWER: <answer in the unit the question asks for>"""

TOOL_QUERY = {"type": "function", "function": {
    "name": "query_facts",
    "description": "Run one read-only SQL SELECT on the financials view. Returns up to 30 rows. Include the accession column.",
    "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}}}
TOOL_LOOKUP = {"type": "function", "function": {
    "name": "lookup_fact",
    "description": "Get one reported figure with its accession number. period_end is a date (2024-12-31) or just a year (2024).",
    "parameters": {"type": "object", "properties": {"ticker": {"type": "string"}, "metric": {"type": "string"},
                                                    "period_end": {"type": "string"}}, "required": ["ticker", "metric", "period_end"]}}}
TOOL_CALC = {"type": "function", "function": {
    "name": "calculate",
    "description": "Evaluate an arithmetic expression, e.g. (2400-1800)/1800*100. Supports + - * / ** % and round(), abs().",
    "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}}}
TOOL_SEARCH = {"type": "function", "function": {
    "name": "search_filings",
    "description": "Search the text of 10-K filings. Returns the best passages with their accession numbers.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "ticker": {"type": "string"},
                                                    "year": {"type": "string", "description": "fiscal year end year, e.g. 2024"}},
                   "required": ["query"]}}}
FACT_TOOLS = [TOOL_LOOKUP, TOOL_QUERY, TOOL_CALC]
FULL_TOOLS = FACT_TOOLS + [TOOL_SEARCH]
TOOLS = FACT_TOOLS  # kept for callers that import it

_ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}")

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod}


def safe_calculate(expression: str) -> float:
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 10:
                raise ValueError("exponent too large")
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            return -ev(node.operand) if isinstance(node.op, ast.USub) else ev(node.operand)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("round", "abs") and not node.keywords:
            return {"round": round, "abs": abs}[node.func.id](*[ev(a) for a in node.args])
        raise ValueError("unsupported expression")
    if len(expression) > 200:
        raise ValueError("expression too long")
    return ev(ast.parse(expression.strip(), mode="eval"))


def _format_rows(result: dict, limit: int = 30) -> str:
    cols, rows = result["columns"], result["rows"][:limit]
    lines = [" | ".join(cols)]
    for r in rows:
        lines.append(" | ".join("" if v is None else (f"{v:.6g}" if isinstance(v, float) and abs(v) < 1e6 else str(v)) for v in r))
    note = f"\n({len(result['rows'])} rows)" if len(result["rows"]) <= limit and not result["truncated"] else f"\n(showing first {limit}; refine the query)"
    return "\n".join(lines) + note


def _lookup(conn, ticker: str, metric: str, period_end: str) -> str:
    ticker, metric, period_end = str(ticker).upper(), str(metric).lower(), str(period_end)
    if metric not in METRICS:
        return f"error: metric must be one of {METRIC_NAMES}"
    pat = period_end + "%" if len(period_end) <= 4 else period_end
    rows = conn.execute("SELECT ticker, metric, period_end, value_musd, accession FROM financials "
                        "WHERE ticker=? AND metric=? AND period_end LIKE ? ORDER BY period_end", (ticker, metric, pat)).fetchall()
    if rows:
        return "ticker | metric | period_end | value_musd | accession\n" + "\n".join(" | ".join(str(v) for v in r) for r in rows[:6])
    have = [r[0] for r in conn.execute("SELECT period_end FROM financials WHERE ticker=? AND metric=? ORDER BY period_end", (ticker, metric))]
    return f"no {metric} figure for {ticker} at {period_end}. Available period_end dates: {', '.join(have) or 'none (ticker or metric not in the data)'}"


def _format_passages(hits: list[dict]) -> str:
    if not hits:
        return "no passages found"
    return "\n\n".join(f"[{h['ticker']} fiscal year ended {h['period_end']}, Item {h['item']}, accession {h['accession']}]\n{h['text'][:900]}"
                        for h in hits)


def run_tool(name: str, args: dict, conn, search=None, retrieved: list | None = None) -> str:
    try:
        if name == "query_facts":
            return _format_rows(run_readonly_query(conn, str(args.get("sql", "")), max_rows=200, timeout_s=5.0))
        if name == "lookup_fact":
            return _lookup(conn, args.get("ticker", ""), args.get("metric", ""), args.get("period_end", ""))
        if name == "calculate":
            return repr(safe_calculate(str(args.get("expression", ""))))
        if name == "search_filings" and search is not None:
            hits = search(str(args.get("query", "")), ticker=args.get("ticker") or None, year=args.get("year") or None)
            if retrieved is not None:
                retrieved.extend(h["key"] for h in hits)
            return _format_passages(hits)
        return f"error: unknown tool {name}"
    except (QueryError, ValueError, ZeroDivisionError, SyntaxError, TypeError) as exc:
        return f"error: {exc}"


def _flatten(llm, system: str, question: str, messages: list, max_tokens: int = 600) -> dict:
    """Ask for the final answer from a plain-text digest of the tool results, with no tool machinery in the request.

    Models sometimes keep emitting tool calls when the history contains tool calls but no tools are offered
    (Groq rejects those requests with `tool_use_failed`); a flat transcript avoids that.
    """
    notes = [m["content"][:1500] for m in messages if m.get("role") == "tool"][-6:]
    prompt = (f"{question}\n\nData retrieved so far:\n" + "\n---\n".join(notes)
              + "\n\nAnswer now using only this data. End with the FINAL ANSWER and SOURCES lines "
                "(accession numbers exactly as shown above), or say you cannot answer.")
    return llm.chat([{"role": "system", "content": system}, {"role": "user", "content": prompt}], max_tokens=max_tokens)


def _chat(llm, messages: list, tools, max_tokens: int, fallback=None) -> dict:
    try:
        if tools is None and fallback is not None:
            return fallback()
        return llm.chat(messages, tools=tools, max_tokens=max_tokens)
    except LLMError as exc:
        if "tool_use_failed" not in str(exc) or fallback is None:
            raise
        return fallback()   # malformed tool call: finish from the digest instead of failing the question


def run_closed_book(llm, question: str, **_) -> dict:
    resp = llm.chat([{"role": "system", "content": CLOSED_BOOK_SYSTEM}, {"role": "user", "content": question}], max_tokens=400)
    return {"text": resp["message"].get("content") or "", "steps": 1, "tool_calls": 0, "model": resp["model"], "calls": 1,
            "prompt_tokens": resp["usage"]["prompt_tokens"], "completion_tokens": resp["usage"]["completion_tokens"]}


def _agent(llm, question: str, conn, system: str, tools: list, search=None, max_steps: int = 6) -> dict:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": question}]
    tool_calls = prompt = completion = calls = 0
    model = ""
    retrieved: list[str] = []
    seen: set[str] = set()
    text = ""
    steps = max_steps
    for step in range(max_steps):
        last = step == max_steps - 1
        resp = _chat(llm, messages, None if last else tools, 600,
                     fallback=lambda: _flatten(llm, system, question, messages))
        calls += 1
        model = resp["model"]
        prompt += resp["usage"]["prompt_tokens"]
        completion += resp["usage"]["completion_tokens"]
        msg = resp["message"]
        messages.append(msg)
        if not msg.get("tool_calls"):
            text, steps = msg.get("content") or "", step + 1
            break
        for tc in msg["tool_calls"]:
            tool_calls += 1
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
                out = run_tool(tc["function"]["name"], args, conn, search=search, retrieved=retrieved)
            except json.JSONDecodeError:
                out = "error: tool arguments were not valid JSON"
            seen.update(_ACCESSION.findall(out))
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": out[:3500]})
        if step == max_steps - 2:
            messages.append({"role": "user", "content": "Out of steps. Give your best FINAL ANSWER and SOURCES now, or say you cannot answer."})
    return {"text": text, "steps": steps, "tool_calls": tool_calls, "model": model, "calls": calls,
            "prompt_tokens": prompt, "completion_tokens": completion, "retrieved": retrieved, "seen_accessions": sorted(seen)}


def run_facts_agent(llm, question: str, conn, max_steps: int = 6, **_) -> dict:
    return _agent(llm, question, conn, FACTS_SYSTEM, FACT_TOOLS, max_steps=max_steps)


def run_full_agent(llm, question: str, conn, search=None, max_steps: int = 6, **_) -> dict:
    tools = FULL_TOOLS if search is not None else FACT_TOOLS
    return _agent(llm, question, conn, FULL_SYSTEM, tools, search=search, max_steps=max_steps)


SYSTEMS = {"closed_book": run_closed_book, "facts_agent": run_facts_agent, "full_agent": run_full_agent}
