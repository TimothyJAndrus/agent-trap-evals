#!/usr/bin/env python3
"""Compare an eval run against a committed baseline; write a PR comment; exit 1 on regression.

  python compare.py results/<run>.jsonl --baseline baselines/ci.json --out comment.md
  python compare.py results/<run>.jsonl --write-baseline baselines/ci.json   # refresh (on main)

The baseline is a small, path-free summary: per task id, pass rate, mean rubric score and runs.
A run regresses when ANY of these hold:
  - a row was VOID (answered by a different model than requested: silent fallback)
  - a task's pass rate fell by >= --max-pass-drop (default 0.5) vs the baseline
  - a rubric task's mean score fell by more than --max-score-drop (default 0.15)
  - a baseline task is missing from the run (a deleted test is a regression until the baseline says so)
New tasks are reported, not gated.
"""
from __future__ import annotations
import argparse, json, sys
from collections import defaultdict
from pathlib import Path

MARKER = "<!-- agent-trap-evals:ci -->"


def summarize(path: Path) -> tuple[dict, list, dict]:
    agg, voids, meta = defaultdict(lambda: {"passes": 0, "runs": 0, "scores": []}), [], {}
    for line in path.read_text().splitlines():
        if not line.strip(): continue
        r = json.loads(line)
        meta = {"agent": r.get("agent"), "model": r.get("model")} if r.get("model") else meta or {"agent": r.get("agent")}
        if r.get("void"):
            voids.append(f"{r['id']}: {r['void']}"); continue
        a = agg[r["id"]]; a["runs"] += 1; a["passes"] += bool(r["pass"])
        if r.get("score") is not None: a["scores"].append(r["score"])
    out = {}
    for tid, a in sorted(agg.items()):
        out[tid] = {"pass_rate": round(a["passes"] / a["runs"], 3), "runs": a["runs"],
                    "score": round(sum(a["scores"]) / len(a["scores"]), 3) if a["scores"] else None}
    return out, voids, meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl", type=Path)
    ap.add_argument("--baseline", type=Path); ap.add_argument("--write-baseline", type=Path)
    ap.add_argument("--out", type=Path); ap.add_argument("--max-pass-drop", type=float, default=0.5)
    ap.add_argument("--max-score-drop", type=float, default=0.15)
    a = ap.parse_args()
    cur, voids, meta = summarize(a.jsonl)
    if a.write_baseline:
        a.write_baseline.parent.mkdir(parents=True, exist_ok=True)
        a.write_baseline.write_text(json.dumps({"meta": meta, "tasks": cur}, indent=2) + "\n")
        print(f"baseline written: {a.write_baseline} ({len(cur)} tasks)"); return 0
    base = json.loads(a.baseline.read_text())["tasks"] if a.baseline and a.baseline.exists() else {}
    problems, lines = [], []
    for tid in sorted(set(base) | set(cur)):
        b, c = base.get(tid), cur.get(tid)
        fmt = lambda x: "—" if x is None else f"{x['pass_rate']:.0%} ({x['runs']})" + (f" · {x['score']:.2f}" if x.get("score") is not None else "")
        note = ""
        if b and not c:
            note = "**missing**"; problems.append(f"{tid} missing from run")
        elif c and not b:
            note = "new"
        elif b and c:
            if b["pass_rate"] - c["pass_rate"] >= a.max_pass_drop:
                note = "**regressed**"; problems.append(f"{tid} pass rate {b['pass_rate']:.0%} → {c['pass_rate']:.0%}")
            elif b.get("score") is not None and c.get("score") is not None and b["score"] - c["score"] > a.max_score_drop:
                note = "**score dropped**"; problems.append(f"{tid} score {b['score']:.2f} → {c['score']:.2f}")
            elif c["pass_rate"] > b["pass_rate"] or (c.get("score") or 0) > (b.get("score") or 0) + 0.05:
                note = "improved"
        lines.append(f"| `{tid}` | {fmt(b)} | {fmt(c)} | {note} |")
    problems += [f"VOID {v}" for v in voids]
    ok = not problems
    md = [MARKER, f"### {'✅' if ok else '❌'} Agent eval gate: {'no regressions' if ok else f'{len(problems)} problem(s)'}",
          f"Agent `{meta.get('agent')}` · model `{meta.get('model')}` · vs `{a.baseline}`", "",
          "| task | baseline (pass · score) | this PR | |", "|---|---|---|---|", *lines]
    if problems:
        md += ["", "**Blocking:**", *[f"- {p}" for p in problems]]
    md += ["", "<sub>Rubric score = weighted criteria incl. trace rules (did the agent run the check that proves its claim). "
           "VOID = answered by a different model than requested. Refresh the baseline on main with `compare.py --write-baseline`.</sub>"]
    text = "\n".join(md) + "\n"
    if a.out: a.out.write_text(text)
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
