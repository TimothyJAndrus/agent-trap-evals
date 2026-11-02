#!/usr/bin/env python3
"""
Offline self-test for the trap harness (what CI runs on every push). No LLM, no network, no cost.

  1. compile every module
  2. scorer self-test: the scripted "fooled" agent must fail every trap row (and pass only the
     honest control rows); the scripted "caught" agent must pass every row
  2b. rubric/trace scoring: only "caught" (right answer + the check that proves it) passes;
     "guess" (right answer, no check) and "shallow" (read the obvious file only) must fail
  2c. regression gate: compare.py blocks pass-rate drops, score drops, VOID rows, deleted tasks
  3. trapgen: every draft in packs/drafts/ passes all 7 validation gates
  4. starter pack: every digest flag still matches its recorded paraphrases (trapgen digest check)
  5. public safety: no home paths or personal identifiers in the public trap files

Exit code 0 only if everything holds.
"""
from __future__ import annotations
import glob, json, os, py_compile, re, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
PY = sys.executable
fails: list[str] = []


def step(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)
    if not ok: fails.append(name)


def run(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=HERE, capture_output=True, text=True, env={**os.environ, **(env or {})}, timeout=900)


# 1 compile
for f in ["run_evals.py", "compare.py", "review_pr.py", "scoreboard.py", "trapgen.py", "trap_evals_mcp.py",
          "agents/scripted_agent.py", "intake/sheet_to_intake.py"]:
    try:
        py_compile.compile(str(HERE / f), doraise=True); step(f"compile {f}", True)
    except py_compile.PyCompileError as e:
        step(f"compile {f}", False, str(e)[:200])

# 2 scorer self-test with scripted agents
tmp = tempfile.mkdtemp(prefix="selftest-")
for mode in ["fooled", "caught"]:
    before = set(glob.glob(str(HERE / "results" / "*.jsonl")))
    run([PY, "run_evals.py", "--tasks", "traps.yaml", "--tag", "layer-embedded", "--agent-name", f"scripted-{mode}",
         "--agent-cmd", f"python3 {{evals_dir}}/agents/scripted_agent.py --mode {mode} --prompt {{prompt}} --cwd {{workdir}}"],
        env={"TMPDIR": tmp})
    new = sorted(set(glob.glob(str(HERE / "results" / "*.jsonl"))) - before)
    if not new:
        step(f"scripted-{mode} run produced results", False); continue
    rows = [json.loads(l) for l in open(new[-1]) if l.strip()]
    for p in new:  # CI artifacts only; never commit self-test runs
        os.remove(p); Path(p).with_suffix(".md").unlink(missing_ok=True)
    traps = [r for r in rows if not (r.get("trap") or "").startswith("control-")]
    ctrls = [r for r in rows if (r.get("trap") or "").startswith("control-")]
    if mode == "fooled":
        bad = [r["id"] for r in traps if r["pass"]]
        step("scripted-fooled fails every trap row", not bad and len(traps) > 0, f"{len(traps)} trap rows" + (f"; wrongly passed: {bad[:5]}" if bad else ""))
    else:
        bad = [r["id"] for r in rows if not r["pass"]]
        step("scripted-caught passes every row", not bad and len(rows) > 0, f"{len(rows)} rows" + (f"; wrongly failed: {bad[:5]}" if bad else ""))
    step(f"scripted-{mode}: honest control rows pass", all(r["pass"] for r in ctrls) and len(ctrls) > 0, f"{len(ctrls)} control rows")

# 2b rubric + trace scoring: right answer without receipts must not pass
expect = {"fooled": (False, False), "caught": (True, True), "guess": (False, False), "shallow": (False, False)}
for mode, (want_pass, want_verified) in expect.items():
    before = set(glob.glob(str(HERE / "results" / "*.jsonl")))
    run([PY, "run_evals.py", "--tasks", "traps.yaml", "rubrics.yaml", "--tag", "rubric", "--no-judge",
         "--agent-name", f"scripted-{mode}",
         "--agent-cmd", f"python3 {{evals_dir}}/agents/scripted_agent.py --mode {mode} --prompt {{prompt}} --cwd {{workdir}} --trace {{trace_file}}"],
        env={"TMPDIR": tmp})
    new = sorted(set(glob.glob(str(HERE / "results" / "*.jsonl"))) - before)
    rows = [json.loads(l) for p_ in new for l in open(p_) if l.strip()]
    for p_ in new: os.remove(p_); Path(p_).with_suffix(".md").unlink(missing_ok=True)
    verified = [next(c["ok"] for c in r["rubric"]["criteria"] if c["id"] == "verified-deep-layer") for r in rows]
    bad = [r["id"] for r, v in zip(rows, verified) if r["pass"] != want_pass or bool(v) != want_verified]
    step(f"rubric scripted-{mode}: pass={want_pass}, verified={want_verified} on every row",
         len(rows) == 3 and not bad, f"{len(rows)} rows" + (f"; wrong: {bad}" if bad else ""))

# 2c regression gate (compare.py): same run passes; a drop, a VOID, or a deleted task blocks
def _rows(spec):  # spec: list of (id, pass, score, void)
    f = Path(tmp) / f"cmp-{abs(hash(str(spec)))}.jsonl"
    f.write_text("".join(json.dumps({"id": i, "pass": ok, "score": sc, "void": v, "agent": "t", "model": "m"}) + "\n"
                         for i, ok, sc, v in spec)); return f
good = _rows([("a", True, 1.0, None), ("a", True, 1.0, None), ("b", True, 0.9, None)])
base = Path(tmp) / "base.json"
run([PY, "compare.py", str(good), "--write-baseline", str(base)])
cases = {"identical run passes": (good, 0),
         "pass-rate drop blocks": (_rows([("a", False, 0.4, None), ("a", False, 0.4, None), ("b", True, 0.9, None)]), 1),
         "score drop blocks": (_rows([("a", True, 1.0, None), ("a", True, 1.0, None), ("b", True, 0.6, None)]), 1),
         "VOID row blocks": (_rows([("a", True, 1.0, None), ("a", True, 1.0, "answered by x"), ("b", True, 0.9, None)]), 1),
         "deleted task blocks": (_rows([("a", True, 1.0, None)]), 1)}
for name, (f, want) in cases.items():
    rc = run([PY, "compare.py", str(f), "--baseline", str(base)]).returncode
    step(f"compare: {name}", rc == want, f"rc={rc}")

# 3 trapgen gates on every draft
for d in sorted(glob.glob(str(HERE / "packs" / "drafts" / "*.yaml"))):
    r = run([PY, "trapgen.py", "validate", d])
    errs = [l.strip() for l in r.stdout.splitlines() if "✗" in l]
    step(f"trapgen gates: {Path(d).stem}", r.returncode == 0, "; ".join(errs)[:300])

# 4 starter pack digest flags vs recorded paraphrases (build into a temp copy; don't touch the repo)
import shutil
pack_tmp = Path(tmp) / "starter.yaml"; shutil.copy(HERE / "packs" / "starter.yaml", pack_tmp)
r = run([PY, "trapgen.py", "digest", str(pack_tmp)])
step("starter pack: flags match all paraphrases", r.returncode == 0, (r.stdout + r.stderr).strip().splitlines()[-1][:300] if (r.stdout + r.stderr).strip() else "")

# 5 public safety
deny = re.compile(r"/Users/|/home/\w|timothy|andrus|\bTim\b|aitecxo|\bWGU\b", re.I)
public = [HERE / "traps.yaml", HERE / "rubrics.yaml", HERE / "trapgen_prompt.md", *map(Path, glob.glob(str(HERE / "packs" / "**" / "*.yaml"), recursive=True)),
          *map(Path, glob.glob(str(HERE / "intake" / "**" / "*.yaml"), recursive=True))]
hits = [f"{p.relative_to(HERE)}:{i}" for p in public for i, l in enumerate(p.read_text().splitlines(), 1)
        if deny.search(l) and "Never use paths under" not in l]
step("public trap files contain no personal identifiers", not hits, ", ".join(hits[:5]))

print(f"\n{'ALL PASSED' if not fails else f'{len(fails)} FAILED: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
