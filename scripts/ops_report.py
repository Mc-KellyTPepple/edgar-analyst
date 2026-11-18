"""Cost, latency and tool-use report from the stored evaluation records (no network, no model calls).

    python scripts/ops_report.py            # writes evals/results/OPS.md and evals/results/ops.json
"""
from __future__ import annotations

import collections
import json
import statistics
from pathlib import Path

RES = Path(__file__).resolve().parents[1] / "evals" / "results"
SETS = {"tuned set": ["closed_book", "facts_agent", "full_agent"], "held-out 1": ["facts_agent_heldout", "full_agent_heldout"], "held-out 2": ["facts_agent_heldout2", "full_agent_heldout2"]}


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))] if xs else None


def load(name):
    path = RES / f"{name}.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


def main() -> None:
    out, md = {}, ["| set | system | questions | tokens/q | prompt | completion | tool calls/q | median s | p95 s | answered by 120b | errors |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for label, names in SETS.items():
        for name in names:
            rows = load(name)
            if not rows:
                continue
            toks = [(r.get("prompt_tokens") or 0) + (r.get("completion_tokens") or 0) for r in rows]
            secs = [r["seconds"] for r in rows if r.get("seconds") is not None]
            tools = collections.Counter(r.get("tool_calls") or 0 for r in rows)
            big = sum(1 for r in rows if "120b" in (r.get("model") or ""))
            rec = {"questions": len(rows), "tokens_per_question": round(statistics.mean(toks)),
                   "prompt_tokens": round(statistics.mean(r.get("prompt_tokens") or 0 for r in rows)),
                   "completion_tokens": round(statistics.mean(r.get("completion_tokens") or 0 for r in rows)),
                   "tool_calls_per_question": round(statistics.mean(r.get("tool_calls") or 0 for r in rows), 2),
                   "tool_call_histogram": dict(sorted(tools.items())),
                   "median_seconds": statistics.median(secs), "p95_seconds": pct(secs, 0.95),
                   "share_answered_by_120b": round(big / len(rows), 3), "errors": sum(1 for r in rows if r.get("error"))}
            out[f"{label}/{name}"] = rec
            md.append(f"| {label} | {name} | {rec['questions']} | {rec['tokens_per_question']} | {rec['prompt_tokens']} | {rec['completion_tokens']} | {rec['tool_calls_per_question']} | {rec['median_seconds']} | {rec['p95_seconds']} | {rec['share_answered_by_120b']:.0%} | {rec['errors']} |")
    (RES / "ops.json").write_text(json.dumps(out, indent=1))
    (RES / "OPS.md").write_text("# Cost and latency (from stored evaluation records)\n\nTokens are what the Groq API reported per question. Seconds include rate-limit waits and fallbacks, so p95 reflects the daily-quota regime, not a production deployment.\n\n" + "\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
