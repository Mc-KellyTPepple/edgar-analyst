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
from .grading import is_abstain
from .questions import RATIOS

PROMPT_VERSION = "v6"   # v2: first full run; v3: no refusal before the data was checked; v4: financial_ratio tool; v5: refusals must be grounded in a tool result
METRIC_NAMES = ", ".join(METRICS)

_RULES = f"""You answer questions about US public companies from a read-only SQLite database of annual (10-K) figures{{extra}}.
View: financials(ticker, name, metric, period_start, period_end, value, value_musd, accession).
value_musd is US dollars in millions; period_end is the fiscal year end date; metric is one of: {METRIC_NAMES}.
Rules:
- Never guess a figure. Read each figure with lookup_fact (it returns the accession) or query_facts, and do all arithmetic with calculate.
- For growth or change between two years use financial_growth. For margins, R&D intensity or any "X as a percentage of Y", call financial_ratio(ticker, ratio, as_of) once per company and date: it picks the
  fiscal year ending closest to as_of (fiscal years differ by company), does the division, and returns the accessions. Compare the returned numbers.
{{more}}- Never say you cannot answer before you have used the tools. Look the figures up first, and decline only if the tools show the data is missing (no row, a period that has not been reported, a company or metric not in the data).
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
                                                    "period_end": {"type": "string"}}, "required": ["ticker", "metric"]}}}
TOOL_RATIO = {"type": "function", "function": {
    "name": "financial_ratio",
    "description": ("Ratio of two reported figures for the fiscal year ending closest to as_of (within 60 days). ratio is one of "
                    + ", ".join(RATIOS) + ", or 'numerator/denominator' using metric names, e.g. capex/revenue. Returns the value in percent and accessions."),
    "parameters": {"type": "object", "properties": {"ticker": {"type": "string"}, "ratio": {"type": "string"},
                                                    "as_of": {"type": "string", "description": "a date like 2024-06-30"}},
                   "required": ["ticker", "ratio", "as_of"]}}}
TOOL_GROWTH = {"type": "function", "function": {
    "name": "financial_growth",
    "description": "Percentage change of one metric between the fiscal years ending closest to from_date and to_date. Returns the percent and accessions.",
    "parameters": {"type": "object", "properties": {"ticker": {"type": "string"}, "metric": {"type": "string"},
                                                    "from_date": {"type": "string"}, "to_date": {"type": "string"}},
                   "required": ["ticker", "metric", "from_date", "to_date"]}}}
TOOL_CALC = {"type": "function", "function": {
    "name": "calculate",
    "description": "Evaluate an arithmetic expression, e.g. (2400-1800)/1800*100. Supports + - * / ** % and round(), abs().",
    "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}}}
TOOL_SEARCH = {"type": "function", "function": {
    "name": "search_filings",
    "description": "Search the text of 10-K filings. Returns the best passages with their accession numbers.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "ticker": {"type": "string"},
                                                    "year": {"type": ["string", "integer"], "description": "fiscal year end year, e.g. 2024"}},
                   "required": ["query"]}}}
FACT_TOOLS = [TOOL_RATIO, TOOL_GROWTH, TOOL_LOOKUP, TOOL_QUERY, TOOL_CALC]
FULL_TOOLS = FACT_TOOLS + [TOOL_SEARCH]
TOOLS = FACT_TOOLS  # kept for callers that import it

# a tool result that really says the data is absent; a refusal is only accepted after one of these (a calculator syntax error is not one)
_NO_DATA = re.compile(r"\bno [a-z_ ]*(?:figure|fiscal year|passages)|not in the data|available period_end|\(0 rows\)", re.I)
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


def _lookup(conn, ticker: str, metric: str, period_end: str = "") -> str:
    ticker, metric, period_end = str(ticker).upper(), str(metric).lower(), str(period_end or "")
    if metric not in METRICS:
        return f"error: metric must be one of {METRIC_NAMES}"
    cols = "SELECT ticker, metric, period_end, value_musd, accession FROM financials "
    head = "ticker | metric | period_end | value_musd | accession\n"
    fmt = lambda rows: head + "\n".join(" | ".join(str(v) for v in r) for r in rows)  # noqa: E731
    have = [r[0] for r in conn.execute("SELECT period_end FROM financials WHERE ticker=? AND metric=? ORDER BY period_end", (ticker, metric))]
    if not period_end:    # no period given: show the most recent years
        rows = conn.execute(cols + "WHERE ticker=? AND metric=? ORDER BY period_end DESC LIMIT 8", (ticker, metric)).fetchall()
        return fmt(rows) if rows else f"no {metric} figure for {ticker} (ticker or metric not in the data)"
    pat = period_end + "%" if len(period_end) <= 4 else period_end
    rows = conn.execute(cols + "WHERE ticker=? AND metric=? AND period_end LIKE ? ORDER BY period_end", (ticker, metric, pat)).fetchall()
    if not rows and len(period_end) > 4 and have:
        try:   # fiscal years end on different dates (52/53-week years): accept the nearest one within 45 days
            near = min(have, key=lambda e: _days_between(e, period_end))
            if _days_between(near, period_end) <= 45:
                rows = conn.execute(cols + "WHERE ticker=? AND metric=? AND period_end=?", (ticker, metric, near)).fetchall()
        except ValueError:
            pass
    if rows:
        return fmt(rows[:6])
    return f"no {metric} figure for {ticker} at {period_end}. Available period_end dates: {', '.join(have) or 'none (ticker or metric not in the data)'}"


def _days_between(a: str, b: str) -> int:
    from datetime import date
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def _ratio(conn, ticker: str, ratio: str, as_of: str, window_days: int = 60) -> str:
    ticker, ratio = str(ticker).upper(), str(ratio).strip().lower()
    if ratio in RATIOS:
        num, den = RATIOS[ratio][1], RATIOS[ratio][2]
    elif "/" in ratio:
        num, den = (x.strip() for x in ratio.split("/", 1))
    else:
        return f"error: ratio must be one of {', '.join(RATIOS)} or numerator/denominator metrics"
    if num not in METRICS or den not in METRICS:
        return f"error: metrics must be among {METRIC_NAMES}"
    try:
        _days_between(as_of, as_of)
    except ValueError:
        return "error: as_of must be a date like 2024-06-30"
    ends = [r[0] for r in conn.execute(
        "SELECT a.period_end FROM financials a JOIN financials b ON a.ticker=b.ticker AND a.period_end=b.period_end "
        "WHERE a.ticker=? AND a.metric=? AND b.metric=? ORDER BY a.period_end", (ticker, num, den))]
    if not ends:
        return f"no {num} and {den} figures for {ticker} in the same fiscal year (ticker or metrics not in the data)"
    end = min(ends, key=lambda e: _days_between(e, as_of))
    if _days_between(end, as_of) > window_days:
        return f"no fiscal year for {ticker} ends within {window_days} days of {as_of}. Available period_end dates: {', '.join(ends)}"
    get = lambda m: conn.execute("SELECT value_musd, accession FROM financials WHERE ticker=? AND metric=? AND period_end=?", (ticker, m, end)).fetchone()  # noqa: E731
    (nv, na), (dv, da) = get(num), get(den)
    if not dv:
        return f"error: {den} is zero for {ticker} {end}"
    return (f"{ticker} {ratio} for the fiscal year ended {end}: {100 * nv / dv:.2f}% "
            f"({num} {nv:g} / {den} {dv:g}, USD millions). accessions: {na}, {da}")


def _growth(conn, ticker: str, metric: str, from_date: str, to_date: str, window_days: int = 60) -> str:
    ticker, metric = str(ticker).upper(), str(metric).lower()
    if metric not in METRICS:
        return f"error: metric must be one of {METRIC_NAMES}"
    ends = [r[0] for r in conn.execute("SELECT period_end FROM financials WHERE ticker=? AND metric=? ORDER BY period_end", (ticker, metric))]
    if not ends:
        return f"no {metric} figure for {ticker} (ticker or metric not in the data)"
    picked = []
    for d in (from_date, to_date):
        try:
            e = min(ends, key=lambda x: _days_between(x, str(d)))
            if _days_between(e, str(d)) > window_days:
                return f"no fiscal year for {ticker} ends within {window_days} days of {d}. Available period_end dates: {', '.join(ends)}"
        except ValueError:
            return "error: dates must look like 2024-06-30"
        picked.append(e)
    get = lambda e: conn.execute("SELECT value_musd, accession FROM financials WHERE ticker=? AND metric=? AND period_end=?", (ticker, metric, e)).fetchone()  # noqa: E731
    (v1, a1), (v2, a2) = get(picked[0]), get(picked[1])
    if not v1:
        return f"error: {metric} is zero for {ticker} {picked[0]}"
    return (f"{ticker} {metric} grew {100 * (v2 / v1 - 1):.2f}% from fiscal year ended {picked[0]} ({v1:g}) to fiscal year ended "
            f"{picked[1]} ({v2:g}), USD millions. accessions: {a1}, {a2}")


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
        if name == "financial_ratio":
            return _ratio(conn, args.get("ticker", ""), args.get("ratio", ""), args.get("as_of", ""))
        if name == "financial_growth":
            return _growth(conn, args.get("ticker", ""), args.get("metric", ""), args.get("from_date", ""), args.get("to_date", ""))
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


def _flatten(llm, system: str, question: str, messages: list, max_tokens: int = 600, temperature: float = 0.0) -> dict:
    """Ask for the final answer from a plain-text digest of the tool results, with no tool machinery in the request.

    Models sometimes keep emitting tool calls when the history contains tool calls but no tools are offered
    (Groq rejects those requests with `tool_use_failed`); a flat transcript avoids that.
    """
    notes = [m["content"][:1500] for m in messages if m.get("role") == "tool"][-6:]
    prompt = (f"{question}\n\nData retrieved so far:\n" + "\n---\n".join(notes)
              + "\n\nAnswer now using only this data. End with the FINAL ANSWER and SOURCES lines "
                "(accession numbers exactly as shown above). Say you cannot answer only if a needed figure is missing from this data.")
    return llm.chat([{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                    max_tokens=max_tokens, temperature=temperature)


_RECOVERABLE = ("tool_use_failed", "Parsing failed", "could not be parsed", "json_validate_failed")


def _is_recoverable(exc: Exception) -> bool:
    return any(t in str(exc) for t in _RECOVERABLE)


def _chat(llm, messages: list, tools, max_tokens: int, fallback=None) -> dict:
    """`fallback(temperature)` finishes from a flat digest; used when no tools are offered or the model's output is malformed."""
    try:
        if tools is None and fallback is not None:
            return fallback(0.0)
        return llm.chat(messages, tools=tools, max_tokens=max_tokens)
    except LLMError as exc:
        if not _is_recoverable(exc) or fallback is None:
            raise
    if tools is not None:
        # resample the same step first: on the very first step there is no tool data to build a digest from
        try:
            return llm.chat(messages, tools=tools, max_tokens=max_tokens, temperature=0.4)
        except LLMError as exc:
            if not _is_recoverable(exc):
                raise
    try:
        return fallback(0.0)
    except LLMError as exc:
        if not _is_recoverable(exc):
            raise
        return fallback(0.4)   # the same prompt failed deterministically; a little sampling noise gets past it


def run_closed_book(llm, question: str, **_) -> dict:
    resp = llm.chat([{"role": "system", "content": CLOSED_BOOK_SYSTEM}, {"role": "user", "content": question}], max_tokens=400)
    return {"text": resp["message"].get("content") or "", "steps": 1, "tool_calls": 0, "model": resp["model"], "calls": 1,
            "prompt_tokens": resp["usage"]["prompt_tokens"], "completion_tokens": resp["usage"]["completion_tokens"]}


def _agent(llm, question: str, conn, system: str, tools: list, search=None, max_steps: int = 8) -> dict:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": question}]
    tool_calls = prompt = completion = calls = 0
    model = ""
    retrieved: list[str] = []
    trace: list[dict] = []
    challenged = False
    seen: set[str] = set()
    text = ""
    steps = max_steps
    for step in range(max_steps):
        last = step == max_steps - 1
        resp = _chat(llm, messages, None if last else tools, 900,
                     fallback=lambda t: _flatten(llm, system, question, messages, temperature=t))
        calls += 1
        model = resp["model"]
        prompt += resp["usage"]["prompt_tokens"]
        completion += resp["usage"]["completion_tokens"]
        msg = resp["message"]
        messages.append(msg)
        if (not msg.get("tool_calls") and not challenged and not last and is_abstain(msg.get("content") or "")
                and not any(_NO_DATA.search(t["out"]) for t in trace)):
            # grounded abstention: declining is only legitimate once a tool has reported that the data is absent
            challenged = True
            messages.append({"role": "user", "content": "No tool has reported missing data yet. Retrieve the figures you need with the tools "
                                                        "first, then answer; decline only if a tool shows the data is absent."})
            continue
        if not msg.get("tool_calls"):
            text, steps = msg.get("content") or "", step + 1
            if not text.strip() and tool_calls:
                # the reply was empty (reasoning used up the token budget); finish from a digest instead of returning nothing
                retry = _flatten(llm, system, question, messages)
                calls += 1
                prompt += retry["usage"]["prompt_tokens"]
                completion += retry["usage"]["completion_tokens"]
                text, model = retry["message"].get("content") or "", retry["model"]
            break
        for tc in msg["tool_calls"]:
            tool_calls += 1
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
                out = run_tool(tc["function"]["name"], args, conn, search=search, retrieved=retrieved)
            except json.JSONDecodeError:
                out = "error: tool arguments were not valid JSON"
            seen.update(_ACCESSION.findall(out))
            trace.append({"tool": tc["function"]["name"], "args": tc["function"].get("arguments", "")[:200], "out": out[:240]})
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": out[:3500]})
        if step == max_steps - 2:
            messages.append({"role": "user", "content": "Out of tool steps. Using the figures already returned above, give your FINAL ANSWER and SOURCES now. "
                                                "Say you cannot answer only if a needed figure was never returned by a tool."})
    return {"prompt_version": PROMPT_VERSION, "text": text, "steps": steps, "tool_calls": tool_calls, "model": model, "calls": calls,
            "prompt_tokens": prompt, "completion_tokens": completion, "retrieved": retrieved, "seen_accessions": sorted(seen),
            "trace": trace[:10], "challenged": challenged}


def run_facts_agent(llm, question: str, conn, max_steps: int = 8, **_) -> dict:
    return _agent(llm, question, conn, FACTS_SYSTEM, FACT_TOOLS, max_steps=max_steps)


def run_full_agent(llm, question: str, conn, search=None, max_steps: int = 8, **_) -> dict:
    tools = FULL_TOOLS if search is not None else FACT_TOOLS
    return _agent(llm, question, conn, FULL_SYSTEM, tools, search=search, max_steps=max_steps)


SYSTEMS = {"closed_book": run_closed_book, "facts_agent": run_facts_agent, "full_agent": run_full_agent}
