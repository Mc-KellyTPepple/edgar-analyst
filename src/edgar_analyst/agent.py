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

from .db import QueryError, run_readonly_query
from .facts import METRICS

METRIC_NAMES = ", ".join(METRICS)

FACTS_SYSTEM = f"""You answer questions about US public companies from a read-only SQLite database of annual (10-K) figures.
View: financials(ticker, name, metric, period_start, period_end, value, value_musd, accession).
value is US dollars; value_musd is US dollars in millions; period_end is the fiscal year end date.
metric is one of: {METRIC_NAMES}.
Rules:
- Never guess a figure. Read it with query_facts and do all arithmetic with calculate.
- Fiscal years differ by company, so match periods by period_end date.
- If the database cannot answer (future period, company or metric not present), say you cannot answer.
- Finish with exactly two lines:
FINAL ANSWER: <answer in the unit the question asks for>
SOURCES: <accession numbers you used, comma separated>"""

CLOSED_BOOK_SYSTEM = """Answer the question about a US public company from memory only. You have no tools.
If you do not know or the information does not exist, say you cannot answer.
Finish with one line: FINAL ANSWER: <answer in the unit the question asks for>"""

TOOLS = [
    {"type": "function", "function": {
        "name": "query_facts",
        "description": "Run one read-only SQL SELECT on the financials view. Returns up to 30 rows.",
        "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}}},
    {"type": "function", "function": {
        "name": "calculate",
        "description": "Evaluate an arithmetic expression, e.g. (2400-1800)/1800*100. Supports + - * / ** % and round(), abs().",
        "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}}},
]

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


def run_tool(name: str, args: dict, conn) -> str:
    try:
        if name == "query_facts":
            return _format_rows(run_readonly_query(conn, str(args.get("sql", "")), max_rows=200, timeout_s=5.0))
        if name == "calculate":
            return repr(safe_calculate(str(args.get("expression", ""))))
        return f"error: unknown tool {name}"
    except (QueryError, ValueError, ZeroDivisionError, SyntaxError) as exc:
        return f"error: {exc}"


def run_closed_book(llm, question: str, **_) -> dict:
    resp = llm.chat([{"role": "system", "content": CLOSED_BOOK_SYSTEM}, {"role": "user", "content": question}], max_tokens=400)
    return {"text": resp["message"].get("content") or "", "steps": 1, "tool_calls": 0, "model": resp["model"], "calls": 1,
            "prompt_tokens": resp["usage"]["prompt_tokens"], "completion_tokens": resp["usage"]["completion_tokens"]}


def run_facts_agent(llm, question: str, conn, max_steps: int = 6, **_) -> dict:
    messages = [{"role": "system", "content": FACTS_SYSTEM}, {"role": "user", "content": question}]
    tool_calls = prompt = completion = calls = 0
    model = ""
    for step in range(max_steps):
        last = step == max_steps - 1
        resp = llm.chat(messages, tools=None if last else TOOLS, max_tokens=600)
        calls += 1
        model = resp["model"]
        prompt += resp["usage"]["prompt_tokens"]
        completion += resp["usage"]["completion_tokens"]
        msg = resp["message"]
        messages.append(msg)
        if not msg.get("tool_calls"):
            return {"text": msg.get("content") or "", "steps": step + 1, "tool_calls": tool_calls, "model": model,
                    "calls": calls, "prompt_tokens": prompt, "completion_tokens": completion}
        for tc in msg["tool_calls"]:
            tool_calls += 1
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
                out = run_tool(tc["function"]["name"], args, conn)
            except json.JSONDecodeError:
                out = "error: tool arguments were not valid JSON"
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": out[:3000]})
        if step == max_steps - 2:
            messages.append({"role": "user", "content": "Out of steps. Give your best FINAL ANSWER and SOURCES now, or say you cannot answer."})
    return {"text": "", "steps": max_steps, "tool_calls": tool_calls, "model": model, "calls": calls,
            "prompt_tokens": prompt, "completion_tokens": completion}


SYSTEMS = {"closed_book": run_closed_book, "facts_agent": run_facts_agent}
