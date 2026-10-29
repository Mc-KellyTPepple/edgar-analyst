"""Run a system over the evaluation questions, grade it, and summarize.

Runs are resumable: finished question ids are skipped, so a free-tier evaluation can
be spread across several days or several workflow runs.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .agent import SYSTEMS
from .grading import cited_accessions, grade
from .llm import BudgetExhausted, CacheMiss


def load_questions(*paths: Path) -> list[dict]:
    """Questions from one or more JSONL files (missing files are skipped)."""
    out = []
    for path in paths:
        if Path(path).exists():
            out += [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    return out


def load_results(path: Path) -> dict[str, dict]:
    p = Path(path)
    if not p.exists():
        return {}
    return {r["id"]: r for r in (json.loads(line) for line in p.read_text().splitlines() if line.strip())}


def drop_false_refusals(path: Path, archive_path: Path, tiers: set[int]) -> int:
    """Move answerable questions that were wrongly declined out of the results file (into an archive) so they are re-run."""
    results = load_results(path)
    drop = {k for k, r in results.items() if r["tier"] in tiers and r.get("abstained") and not r["correct"]}
    if not drop:
        return 0
    archive_path = Path(archive_path)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with open(archive_path, "a", encoding="utf-8") as fh:
        fh.writelines(json.dumps(results[k], sort_keys=True) + "\n" for k in sorted(drop))
    Path(path).write_text("".join(json.dumps(r, sort_keys=True) + "\n" for k, r in results.items() if k not in drop), encoding="utf-8")
    return len(drop)


def drop_tier_results(path: Path, archive_path: Path, tiers: set[int], ids: set[str] | None = None) -> int:
    """Archive and remove the results of whole tiers (or specific question ids) so they are re-run."""
    results = load_results(path)
    drop = {k for k, r in results.items() if r["tier"] in tiers or (ids and k in ids)}
    if not drop:
        return 0
    archive_path = Path(archive_path)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with open(archive_path, "a", encoding="utf-8") as fh:
        fh.writelines(json.dumps(results[k], sort_keys=True) + "\n" for k in sorted(drop))
    Path(path).write_text("".join(json.dumps(r, sort_keys=True) + "\n" for k, r in results.items() if k not in drop), encoding="utf-8")
    return len(drop)


def run_eval(questions: list[dict], system: str, llm, conn, out_path: Path, tiers=None, limit: int | None = None,
             progress=print, **extras) -> dict:
    runner = SYSTEMS[system]
    out_path = Path(out_path)
    done = {k: v for k, v in load_results(out_path).items() if not v.get("error")}
    if out_path.exists():  # failed attempts are retried, so drop them from the file
        out_path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in done.values()), encoding="utf-8")
    todo = [q for q in questions if (tiers is None or q["tier"] in tiers) and q["id"] not in done]
    if limit is not None:
        todo = todo[:limit]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stopped = None
    with open(out_path, "a", encoding="utf-8") as fh:
        for i, q in enumerate(todo, 1):
            t0 = time.monotonic()
            try:
                run = runner(llm, q["question"], conn=conn, **extras)
                g = grade(q, run["text"])
                evidence = {e["accession"] for e in q["evidence"]}
                cited = cited_accessions(run["text"])
                gold_chunks = {e["chunk"] for e in q["evidence"] if e.get("chunk")}
                rec = {
                    "id": q["id"], "prompt_version": run.get("prompt_version", "v2"), "tier": q["tier"], "type": q["type"], "system": system, "correct": g["correct"],
                    "abstained": g.get("abstained", False), "reason": g["reason"], "response": run["text"],
                    "citation_recall": (len(evidence & cited) / len(evidence)) if evidence else None,
                    "citation_precision": (len(evidence & cited) / len(cited)) if evidence and cited else None,
                    "trace": run.get("trace", []), "challenged": run.get("challenged", False),
                    "retrieval_hit": bool(gold_chunks & set(run.get("retrieved", []))) if gold_chunks else None,
                    "model": run["model"], "calls": run["calls"], "tool_calls": run["tool_calls"],
                    "prompt_tokens": run["prompt_tokens"], "completion_tokens": run["completion_tokens"],
                    "seconds": round(time.monotonic() - t0, 2), "error": None,
                }
            except BudgetExhausted as exc:
                stopped = str(exc)
                break
            except CacheMiss as exc:
                stopped = f"cache miss: {exc}"
                break
            except Exception as exc:  # one bad question should not end the run
                rec = {"id": q["id"], "tier": q["tier"], "type": q["type"], "system": system, "correct": False,
                       "abstained": False, "reason": "error", "response": "", "citation_recall": None, "citation_precision": None,
                       "retrieval_hit": None, "model": "",
                       "calls": 0, "tool_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                       "seconds": round(time.monotonic() - t0, 2), "error": f"{type(exc).__name__}: {exc}"[:300]}
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
            fh.flush()
            progress(f"[{i}/{len(todo)}] {q['id']} {'ok' if rec['correct'] else 'wrong'}")
    return {"attempted": len(todo), "stopped": stopped, "remaining": len([q for q in questions if (tiers is None or q["tier"] in tiers)
                                                                           and q["id"] not in {k for k, v in load_results(out_path).items() if not v.get("error")}])}


def summarize_results(results: list[dict]) -> dict:
    def acc(rows):
        return round(100 * sum(r["correct"] for r in rows) / len(rows), 1) if rows else None

    answerable = [r for r in results if r["tier"] in (1, 2, 3)]
    text_rows = [r for r in results if r["tier"] == 4]
    hits = [r["retrieval_hit"] for r in text_rows if r.get("retrieval_hit") is not None]
    precision = [r["citation_precision"] for r in answerable if r.get("citation_precision") is not None]
    refusals = [r for r in results if r["tier"] == 0]
    cited = [r["citation_recall"] for r in answerable if r["citation_recall"] is not None]
    n = len(results)
    return {
        "questions": n,
        "accuracy_by_tier": {str(t): acc([r for r in results if r["tier"] == t]) for t in (1, 2, 3, 4)},
        "retrieval_hit_rate": round(100 * sum(hits) / len(hits), 1) if hits else None,
        "citation_precision": round(100 * sum(precision) / len(precision), 1) if precision else None,
        "accuracy_answerable": acc(answerable),
        "correct_refusal_rate": acc(refusals),
        "false_refusal_rate": round(100 * sum(r["abstained"] for r in answerable) / len(answerable), 1) if answerable else None,
        "citation_recall": round(100 * sum(cited) / len(cited), 1) if cited else None,
        "errors": sum(1 for r in results if r["error"]),
        "mean_tool_calls": round(sum(r["tool_calls"] for r in results) / n, 2) if n else None,
        "mean_llm_calls": round(sum(r["calls"] for r in results) / n, 2) if n else None,
        "mean_tokens": round(sum(r["prompt_tokens"] + r["completion_tokens"] for r in results) / n) if n else None,
        "p95_seconds": sorted(r["seconds"] for r in results)[max(0, int(0.95 * n) - 1)] if n else None,
        "models": sorted({r["model"] for r in results if r["model"]}),
        "by_model": {m: {"n": len(rows), "accuracy": acc(rows)}
                     for m in sorted({r["model"] for r in results if r["model"]})
                     for rows in [[r for r in results if r["model"] == m and r["tier"] > 0]] if rows},
    }


def check_gate(summary: dict, min_accuracy: float, min_refusal: float | None = None) -> list[str]:
    """Return a list of failures (empty means the gate passes)."""
    problems = []
    acc = summary.get("accuracy_answerable")
    if acc is None or acc < min_accuracy:
        problems.append(f"answerable accuracy {acc} is below {min_accuracy}")
    if min_refusal is not None:
        ref = summary.get("correct_refusal_rate")
        if ref is None or ref < min_refusal:
            problems.append(f"correct refusal rate {ref} is below {min_refusal}")
    if summary.get("errors"):
        problems.append(f"{summary['errors']} questions errored")
    return problems


def markdown_table(summaries: dict[str, dict]) -> str:
    cols = ["system", "n", "tier 1", "tier 2", "tier 3", "tier 4 (text)", "answerable (1-3)", "refusals", "false refusals", "citation recall", "citation precision", "retrieval hit",
            "tool calls", "tokens/q", "p95 s"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    pct = lambda v: "-" if v is None else f"{v}%"  # noqa: E731
    for name, s in summaries.items():
        t = s["accuracy_by_tier"]
        lines.append("| " + " | ".join([
            name, str(s["questions"]), pct(t["1"]), pct(t["2"]), pct(t["3"]), pct(t["4"]), pct(s["accuracy_answerable"]),
            pct(s["correct_refusal_rate"]), pct(s["false_refusal_rate"]), pct(s["citation_recall"]), pct(s.get("citation_precision")),
            pct(s.get("retrieval_hit_rate")),
            str(s["mean_tool_calls"]), str(s["mean_tokens"]), str(s["p95_seconds"])]) + " |")
    models = ["", "| system | model | answerable questions | accuracy |", "|---|---|---|---|"]
    for name, sm in summaries.items():
        for m, v in sm.get("by_model", {}).items():
            models.append(f"| {name} | {m} | {v['n']} | {pct(v['accuracy'])} |")
    return "\n".join(lines + (models if len(models) > 3 else []))
