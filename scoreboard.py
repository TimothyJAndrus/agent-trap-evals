#!/usr/bin/env python3
"""Trap scoreboard: catch rate per trap, per agent, per arm (asked vs embedded).

  python scoreboard.py results/A.jsonl results/B.jsonl [--out SCOREBOARD.md]

Rows come from run_evals.py JSONL. Only rows with a `trap` field are counted; a row's agent
label comes from --agent-name at run time. Catch = task passed (asked: right verdict + deep
evidence; embedded: outcome caught | warned | abstained).
"""
import argparse, json
from collections import defaultdict
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("jsonl", nargs="+"); ap.add_argument("--out")
args = ap.parse_args()

cell = defaultdict(lambda: [0, 0])          # (trap, agent, arm) -> [passes, runs]
outcomes = defaultdict(list)                # (trap, agent) -> embedded outcomes
traps, cols = [], []
for f in args.jsonl:
    for line in Path(f).read_text().splitlines():
        if not line.strip(): continue
        r = json.loads(line)
        if not r.get("trap"): continue
        if r.get("void"): continue  # answered by a fallback model, not the agent under test
        agent = r.get("agent") or r.get("version", "?")
        k = (r["trap"], agent, r["arm"])
        cell[k][0] += bool(r["pass"]); cell[k][1] += 1
        if r["arm"] == "embedded": outcomes[(r["trap"], agent)].append(r.get("outcome") or "?")
        if r["trap"] not in traps: traps.append(r["trap"])
        if (agent, r["arm"]) not in cols: cols.append((agent, r["arm"]))
cols.sort(key=lambda c: (c[0], c[1] != "asked"))

def fmt(p, n): return "—" if n == 0 else (f"{p}/{n}" if n > 1 else ("✅" if p else "❌"))

md = ["| trap | " + " | ".join(f"{a} · {arm}" for a, arm in cols) + " | embedded outcomes |",
      "|---|" + "---|" * len(cols) + "---|"]
tot = defaultdict(lambda: [0, 0])
for t in traps:
    cells = []
    for a, arm in cols:
        p, n = cell[(t, a, arm)]; tot[(a, arm)][0] += p; tot[(a, arm)][1] += n
        cells.append(fmt(p, n))
    oc = "; ".join(f"{a}: {', '.join(outcomes[(t, a)])}" for a in dict.fromkeys(c[0] for c in cols) if outcomes[(t, a)])
    md.append(f"| {t} | " + " | ".join(cells) + f" | {oc} |")
md.append("| **catch rate** | " + " | ".join(
    f"**{p}/{n} ({100 * p // n}%)**" if n else "—" for p, n in (tot[c] for c in cols)) + " | |")
text = "\n".join(md) + "\n"
print(text)
if args.out: Path(args.out).write_text(text)
