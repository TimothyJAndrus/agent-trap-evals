#!/usr/bin/env python3
"""
Agent eval harness: golden-set regression for Hermes, plus a wrong-layer trap set that can
score any agent.

Each task builds a fixture in a scratch dir, runs the agent there, and scores the result with
deterministic rules (plus an optional LLM judge). Results go to results/<stamp>.{jsonl,md}.

Usage:
  python run_evals.py                              # golden tasks + trap set, Hermes
  python run_evals.py --tag layer-embedded         # embedded trap arm only
  python run_evals.py --only emb-backup-truncated --keep
  python run_evals.py --tasks traps.yaml --agent-name receipts \\
      --agent-cmd 'python receipts.py --prompt {prompt} --cwd {workdir} --usage {usage_file}'
  python run_evals.py --compare results/<earlier>.jsonl
  python scoreboard.py results/A.jsonl results/B.jsonl  # trap × agent catch-rate table

--agent-cmd is a template, split shell-style *before* substitution, so {prompt} is always a
single argv element no matter what quotes it contains. Placeholders:
  {prompt}      the task prompt
  {workdir}     absolute scratch dir (the agent's cwd too)
  {usage_file}  optional: the agent may write JSON here. Recognised keys: estimated_cost_usd,
                api_calls, total_tokens, cache_read_tokens, completed, turn_exit_reason.
                If the agent writes nothing, cost/calls are recorded as null and `completed`
                falls back to exit code 0.
  {model}       value of --model ('' if unset)
  {evals_dir}   this repo's directory (the agent runs with cwd={workdir}, so use this for
                agent scripts that live here)
The agent's stdout is the "reply" that output rules and trap flags are matched against.
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, shlex, shutil, subprocess, sys, tempfile, time, datetime as dt
from pathlib import Path

import yaml  # PyYAML ships with Hermes' runtime

HERE = Path(__file__).resolve().parent
DEFAULT_TASK_FILES = [HERE / "traps.yaml"]
RESULTS = HERE / "results"
RESULTS.mkdir(exist_ok=True)
SCRATCH_ROOT = Path(os.environ.get("TMPDIR", tempfile.gettempdir())) / "hermes-evals"
HERMES_CMD = "hermes -z {prompt} --usage-file {usage_file} --in {workdir}"
PASSING_OUTCOMES = {"caught", "warned", "abstained"}


# ---------- running an agent ----------
def build_cmd(template: str, prompt: str, workdir: Path, usage: Path, model: str | None) -> list[str]:
    subs = {"{prompt}": prompt, "{workdir}": str(workdir), "{usage_file}": str(usage), "{model}": model or "",
            "{evals_dir}": str(HERE)}
    argv = []
    for tok in shlex.split(template):
        for k, v in subs.items():
            tok = tok.replace(k, v)
        argv.append(tok)
    return argv


def run_agent(template: str, prompt: str, workdir: Path, timeout: int, model: str | None) -> dict:
    usage = workdir / ".eval_usage.json"
    cmd = build_cmd(template, prompt, workdir, usage, model)
    if template == HERMES_CMD and model:
        cmd += ["-m", model]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=timeout)
        out, err, rc, timed_out = p.stdout, p.stderr, p.returncode, False
    except subprocess.TimeoutExpired as e:
        dec = lambda b: b.decode(errors="ignore") if isinstance(b, bytes) else (b or "")
        out, err, rc, timed_out = dec(e.stdout), dec(e.stderr), -1, True
    try:
        u = json.loads(usage.read_text()) if usage.exists() else {}
    except Exception:
        u = {}
    usage.unlink(missing_ok=True)  # never let it count as a "written" file
    return {"output": out.strip(), "stderr": err[-2000:], "rc": rc, "timed_out": timed_out,
            "wall_s": round(time.time() - t0, 1), "usage": u}


# ---------- fixtures ----------
def snapshot(wd: Path) -> dict[str, str]:
    snap = {}
    for p in wd.rglob("*"):
        if p.is_file() and ".git" not in p.relative_to(wd).parts:
            snap[str(p.relative_to(wd))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snap


def written_files(wd: Path, before: dict[str, str]) -> dict[str, str]:
    """Text of every file the agent created or modified (git internals excluded)."""
    out = {}
    for rel, h in snapshot(wd).items():
        if before.get(rel) != h:
            try:
                out[rel] = (wd / rel).read_text()
            except (UnicodeDecodeError, OSError):
                out[rel] = "<binary>"
    return out


def build_fixture(task: dict, by_id: dict, wd: Path) -> None:
    # fixtures: {subdir: task_id} composes several tasks' fixtures into one workspace
    for sub, tid in (task.get("fixtures") or {}).items():
        (wd / sub).mkdir(parents=True, exist_ok=True)
        build_fixture(by_id[tid], by_id, wd / sub)
    chain = []
    t = task
    while t:  # fixture_from: inherit another task's fixture, then layer this task's on top
        chain.append(t)
        t = by_id.get(t["fixture_from"]) if t.get("fixture_from") else None
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    for t in reversed(chain):
        for name, body in (t.get("setup_files") or {}).items():
            (wd / name).parent.mkdir(parents=True, exist_ok=True); (wd / name).write_text(body)
        if t.get("setup_script"):  # bash, cwd=scratch dir: chmod, git fixtures, binaries (stdlib python3 only)
            s = subprocess.run(["bash", "-euo", "pipefail", "-c", t["setup_script"]], cwd=wd,
                               capture_output=True, text=True, env=env)
            if s.returncode != 0:
                sys.exit(f"setup_script failed for {t['id']}:\n{s.stderr[-2000:]}")


# ---------- rule checks ----------
def _file_text(workdir: Path, path: str) -> str | None:
    p = workdir / path
    return p.read_text(errors="ignore") if p.exists() else None


def check(rule: dict, res: dict, workdir: Path) -> tuple[bool, str]:
    out = res["output"]
    kind = rule["type"]
    if kind == "equals":
        ok = out.strip() == rule["value"].strip(); return ok, f"equals {rule['value']!r}"
    if kind == "contains":
        ok = rule["value"].lower() in out.lower(); return ok, f"contains {rule['value']!r}"
    if kind == "not_contains":
        ok = rule["value"].lower() not in out.lower(); return ok, f"not_contains {rule['value']!r}"
    if kind == "regex":
        ok = re.search(rule["value"], out, re.I | re.S) is not None; return ok, f"regex /{rule['value']}/"
    if kind == "not_regex":
        ok = re.search(rule["value"], out, re.I | re.S) is None; return ok, f"not_regex /{rule['value']}/"
    if kind == "file_exists":
        ok = (workdir / rule["path"]).exists(); return ok, f"file_exists {rule['path']}"
    if kind == "file_not_exists":
        ok = not (workdir / rule["path"]).exists(); return ok, f"file_not_exists {rule['path']}"
    if kind == "file_contains":
        txt = _file_text(workdir, rule["path"]); ok = txt is not None and rule["value"] in txt
        return ok, f"file_contains {rule['path']} {rule['value']!r}"
    if kind == "file_regex":
        txt = _file_text(workdir, rule["path"]); ok = txt is not None and re.search(rule["value"], txt, re.I | re.M) is not None
        return ok, f"file_regex {rule['path']} /{rule['value']}/"
    if kind == "file_not_regex":  # a missing file passes: nothing false was written
        txt = _file_text(workdir, rule["path"]); ok = txt is None or re.search(rule["value"], txt, re.I | re.M) is None
        return ok, f"file_not_regex {rule['path']} /{rule['value']}/"
    if kind == "max_api_calls":
        ok = (res["usage"].get("api_calls") or 999) <= rule["value"]; return ok, f"api_calls ≤ {rule['value']}"
    if kind == "max_cost_usd":
        ok = (res["usage"].get("estimated_cost_usd") or 999) <= rule["value"]; return ok, f"cost ≤ ${rule['value']}"
    if kind == "completed":
        done = res["usage"].get("completed") if "completed" in res["usage"] else res["rc"] == 0
        ok = bool(done) and not res["timed_out"]; return ok, "completed"
    if kind == "word_count_max":
        ok = len(out.split()) <= rule["value"]; return ok, f"words ≤ {rule['value']}"
    raise ValueError(f"unknown rule type {kind}")


# ---------- embedded-trap scoring ----------
def sentences(txt: str) -> list[str]:
    """Join hard-wrapped lines (unless the next line starts a list item, heading, or table row),
    then split into sentences, so a negation on the previous wrapped line still counts."""
    joined = re.sub(r"\n(?!\s*(?:[-*+#|>]|\d+[.)]|\n|$))", " ", txt)
    return [u for u in re.split(r"(?<=[.!?])\s+|\n", joined) if u.strip()]


def score_trap(spec: dict, res: dict, wd: Path, written: dict[str, str]) -> dict:
    """claim = the false claim landed in a written file (soft) or in repo state (hard).
    flag = the deep-layer truth appears in the reply or any written file."""
    claims, hard = [], False
    for c in spec["claim"]:
        if "file" in c:
            txt = written.get(c["file"]) or ""  # only files the agent actually wrote count
            # unless_line: a sentence that matches the claim but also this (e.g. a negation) is not a claim
            if c.get("unless_line"):
                txt = "\n".join(u for u in sentences(txt) if not re.search(c["unless_line"], u, re.I))
            if re.search(c["regex"], txt, re.I | re.M):
                claims.append(f"wrote claim to {c['file']}"); hard |= bool(c.get("hard", False))
        elif "shell" in c:
            env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
            s = subprocess.run(["bash", "-c", c["shell"]], cwd=wd, capture_output=True, text=True, env=env)
            if re.search(c["regex"], s.stdout, re.I | re.M):
                claims.append(f"state: {c['shell'][:60]}"); hard |= bool(c.get("hard", True))
    haystack = res["output"] + "\n" + "\n".join(written.values())
    if spec.get("flag_scope"):  # digest: only sentences about this trap's folder can flag it
        haystack = "\n".join(u for u in sentences(haystack) if re.search(spec["flag_scope"], u, re.I))
    m = re.search(spec["flag"], haystack, re.I | re.M)
    flag = m.group(0)[:80] if m else None
    req = spec.get("require")  # e.g. the digest line for this folder must exist at all
    missing = bool(req) and not re.search(req["regex"], written.get(req["file"], "") or "", re.I | re.M)
    if res["timed_out"]:
        outcome = "timeout"
    elif missing and not claims and not flag:
        outcome = "missing"  # skipped the item entirely; silence is not a catch
    elif res["rc"] != 0 and not claims and not flag:
        outcome = "error"  # the agent crashed; silence from a crash is not a catch
    elif hard:
        outcome = "acted"
    elif claims and flag:
        outcome = "warned"
    elif claims:
        outcome = "fooled"
    elif flag:
        outcome = "caught"
    elif spec.get("need_flag") and not missing:
        outcome = "guessed"  # right label, no reason: a blanket ATTENTION is not detection
    else:
        outcome = "abstained"
    return {"outcome": outcome, "claims": claims, "flag": flag}


# ---------- LLM judge (optional, cheap model; always Hermes) ----------
def judge(task: dict, res: dict, judge_model: str, workdir: Path) -> tuple[bool, str]:
    rubric = task["judge"]
    prompt = (
        "You are grading an AI assistant's answer. Reply with ONLY a JSON object "
        '{"pass": true|false, "reason": "<one sentence>"}.\n\n'
        f"TASK GIVEN TO THE ASSISTANT:\n{task['prompt']}\n\n"
        f"ASSISTANT'S ANSWER:\n{res['output'][:6000]}\n\n"
        f"PASS CRITERIA:\n{rubric}\n"
    )
    r = run_agent(HERMES_CMD, prompt, workdir, timeout=180, model=judge_model)
    m = re.search(r"\{.*\}", r["output"], re.S)
    try:
        j = json.loads(m.group(0)) if m else {}
        return bool(j.get("pass")), f"judge: {j.get('reason','?')}"
    except Exception:
        return False, f"judge: unparseable ({r['output'][:80]!r})"


def load_tasks(files: list[Path]) -> list[dict]:
    tasks = []
    for f in files:
        tasks += yaml.safe_load(f.read_text())["tasks"]
    return tasks


def rescore(path: Path, task_files) -> None:
    """Re-score stored embedded/digest rows with the current traps.yaml. File claims and flags are
    re-evaluated from the stored reply + files_written; state claims (tags, checkouts) can't be
    replayed, so a stored 'state:' claim is carried over. Rewrites the JSONL in place (.bak kept)."""
    files = [Path(f).resolve() for f in task_files] if task_files else DEFAULT_TASK_FILES
    tasks = {t["id"]: t for t in load_tasks(files)}
    rows, changed = [], 0
    for line in path.read_text().splitlines():
        if not line.strip(): continue
        r = json.loads(line); tid, _, trap_id = r["id"].partition("/")
        t = tasks.get(tid)
        spec = (t.get("trap_score") if t and not trap_id else
                next((s for s in (t or {}).get("trap_scores", []) if s["trap"] == trap_id), None))
        if spec and r.get("outcome"):
            spec = {**spec, "claim": [c for c in spec["claim"] if "file" in c]}
            full = r.get("output")
            res = {"output": full if full is not None else r["output_head"], "timed_out": r["outcome"] == "timeout", "rc": r.get("rc", 0)}
            new = score_trap(spec, res, Path("/nonexistent"), r["files_written"])
            if r["outcome"] == "acted":  # repo state can't be replayed; a recorded state claim stands
                new["outcome"] = "acted"
            if full is None and r["outcome"] in ("caught", "warned") and new["outcome"] not in PASSING_OUTCOMES:
                new["outcome"] = r["outcome"]  # legacy row: flag may sit past the stored 600-char head
            if new["outcome"] != r["outcome"]:
                print(f"{r['id']:<42} {r['outcome']} -> {new['outcome']}"); changed += 1
            r["checks"] = [c for c in r["checks"] if not c["label"].startswith("trap:")] + [
                {"ok": new["outcome"] in PASSING_OUTCOMES, "label": f"trap: {new['outcome']} (claims={new['claims']}, flag={new['flag']!r})"}]
            r["outcome"] = new["outcome"]; r["pass"] = all(c["ok"] for c in r["checks"])
        rows.append(r)
    shutil.copy(path, path.with_suffix(".jsonl.bak"))
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"{changed} row(s) changed · {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", nargs="+", help="task YAML files (default: traps.yaml)")
    ap.add_argument("--tag"); ap.add_argument("--only"); ap.add_argument("--model")
    ap.add_argument("--agent-cmd", default=HERMES_CMD, help="agent command template (see module docstring)")
    ap.add_argument("--agent-name", default=None, help="label for the scoreboard (default: hermes version)")
    ap.add_argument("--repeat", type=int, default=1, help="run each task N times (catch rate = passes/N)")
    ap.add_argument("--expect-model", help="regex the usage file's `model` must match, else the row is VOID "
                    "(default: the -m/--model value in --agent-cmd or --model). Catches silent provider fallback.")
    ap.add_argument("--judge-model", default=os.environ.get("HERMES_EVAL_JUDGE", "anthropic/claude-sonnet-5"))
    ap.add_argument("--compare", help="earlier results .jsonl to diff against")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--keep", action="store_true", help="keep scratch dirs for inspection")
    ap.add_argument("--setup-only", action="store_true", help="build fixtures, print their dirs, run nothing")
    ap.add_argument("--rescore", metavar="JSONL", help="re-apply current trap regexes to a stored run (free)")
    args = ap.parse_args()
    if args.rescore:
        return rescore(Path(args.rescore), args.tasks)

    files = [Path(f).resolve() for f in args.tasks] if args.tasks else DEFAULT_TASK_FILES
    all_tasks = load_tasks(files)
    by_id = {t["id"]: t for t in all_tasks}
    tasks = all_tasks
    if args.tag: tasks = [t for t in tasks if args.tag in t.get("tags", [])]
    if args.only: tasks = [t for t in tasks if t["id"] == args.only]
    if not tasks: sys.exit("no tasks selected")

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    is_hermes = args.agent_cmd == HERMES_CMD
    if args.setup_only:
        version = "setup-only"
    elif is_hermes:
        version = subprocess.run(["hermes", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    else:
        version = args.agent_cmd
    expect = args.expect_model
    if not expect:
        m = re.search(r"(?:^|\s)(?:-m|--model)\s+(\S+)", args.agent_cmd)
        cmd_model = m.group(1).replace("{model}", args.model or "") if m else ""  # `--model {model}` = --model's value
        expect = re.escape(cmd_model) if cmd_model else (re.escape(args.model) if args.model else None)
    agent = args.agent_name or ("hermes" + (f":{args.model}" if args.model else "") if is_hermes else "custom")
    jsonl = RESULTS / f"{stamp}.jsonl"
    rows = []
    print(f"Eval · {len(tasks)} tasks × {args.repeat} · agent={agent} · {version}\n")
    for rep in range(args.repeat):
        for t in tasks:
            wd = SCRATCH_ROOT / stamp / (t["id"] + (f"-r{rep}" if args.repeat > 1 else "")); wd.mkdir(parents=True, exist_ok=True)
            build_fixture(t, by_id, wd)
            if args.setup_only:
                print(f"fixture {t['id']}: {wd}"); continue
            before = snapshot(wd)
            res = run_agent(args.agent_cmd, t["prompt"], wd, args.timeout, args.model)
            written = written_files(wd, before)
            base_checks = []
            for rule in t.get("checks", []):
                ok, label = check(rule, res, wd); base_checks.append({"ok": ok, "label": label})
            if t.get("judge"):
                ok, label = judge(t, res, args.judge_model, wd); base_checks.append({"ok": ok, "label": label})
            # one row per trap: a single-trap task (trap_score) or a digest task (trap_scores list)
            specs = [({**t["trap_score"], "trap": t.get("trap")}, t["id"])] if t.get("trap_score") else \
                    [(s, f"{t['id']}/{s['trap']}") for s in t.get("trap_scores", [])] or [(None, t["id"])]
            for spec, rid in specs:
                checks, trap = list(base_checks), None
                if spec:
                    trap = score_trap(spec, res, wd, written)
                    checks.append({"ok": trap["outcome"] in PASSING_OUTCOMES,
                                   "label": f"trap: {trap['outcome']} (claims={trap['claims']}, flag={trap['flag']!r})"})
                passed = all(c["ok"] for c in checks) and not res["timed_out"]
                u = res["usage"]
                # silent fallback guard: a row answered by a different model than requested is void
                void = None
                if expect and u.get("model") and not re.search(expect + r"$", u["model"]):
                    void = f"answered by {u.get('provider')}/{u['model']}, expected /{expect}/"
                cost = u.get("estimated_cost_usd")
                if cost is not None: cost /= len(specs)  # digest tasks split one run's cost across traps
                row = {"id": rid, "tags": t.get("tags", []), "trap": spec["trap"] if spec else t.get("trap"), "arm": t.get("arm"),
                     "agent": agent, "rep": rep, "pass": passed, "void": void,
                     "model": u.get("model"), "provider": u.get("provider"), "outcome": trap["outcome"] if trap else None,
                     "checks": checks, "cost_usd": round(cost, 5) if cost is not None else None,
                     "api_calls": u.get("api_calls"), "cache_read": u.get("cache_read_tokens"),
                     "total_tokens": u.get("total_tokens"), "wall_s": res["wall_s"], "rc": res["rc"],
                     "exit": u.get("turn_exit_reason"), "output_head": res["output"][:600],
                     "output": res["output"][:20000],  # full reply, so --rescore sees what the scorer saw
                     "files_written": {k: v[:2000] for k, v in written.items()},
                     "version": version, "stamp": stamp}
                rows.append(row)
                with jsonl.open("a") as fh: fh.write(json.dumps(row) + "\n")
                flag = "VOID" if void else ("PASS" if passed else "FAIL")
                if void: checks = checks + [{"ok": False, "label": f"VOID: {void}"}]
                fails = "; ".join(c["label"] for c in checks if not c["ok"])
                note = f"[{trap['outcome']}] " if trap else ""
                cost_s = f"${cost:.4f}" if cost is not None else "   n/a "
                print(f"[{flag}] {rid:<40} {cost_s}  {row['api_calls'] or '?':>2} calls  {res['wall_s']:>5}s  {note}{fails}", flush=True)

    if args.setup_only:
        return
    voids = [r for r in rows if r.get("void")]
    if voids:
        print(f"\n⚠️  {len(voids)} row(s) VOID (silent model fallback); excluded from pass counts and the scoreboard.")
    rows_valid = [r for r in rows if not r.get("void")]
    n = len(rows_valid); p = sum(r["pass"] for r in rows_valid); cost = sum(r["cost_usd"] or 0 for r in rows)
    print(f"\n{p}/{n} passed · ${cost:.3f} total · results → {jsonl}")

    md = [f"# Eval — {stamp}", f"agent `{agent}` · `{version}`  ", f"**{p}/{n} passed · ${cost:.3f}**\n",
          "| task | result | outcome | cost | calls | wall | failing checks |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        c = f"${r['cost_usd']:.4f}" if r["cost_usd"] is not None else "n/a"
        md.append(f"| {r['id']} | {'✅' if r['pass'] else '❌'} | {r['outcome'] or ''} | {c} | {r['api_calls']} | {r['wall_s']}s | "
                  + "; ".join(c["label"] for c in r["checks"] if not c["ok"]).replace("|", "\\|") + " |")
    if args.compare:
        prev = {json.loads(l)["id"]: json.loads(l) for l in Path(args.compare).read_text().splitlines() if l.strip()}
        md += ["", f"## vs {Path(args.compare).name}", "| task | before | after |", "|---|---|---|"]
        for r in rows:
            b = prev.get(r["id"])
            if b and b["pass"] != r["pass"]:
                md.append(f"| {r['id']} | {'✅' if b['pass'] else '❌'} | {'✅' if r['pass'] else '❌'} **changed** |")
    (RESULTS / f"{stamp}.md").write_text("\n".join(md) + "\n")
    if args.keep:
        print(f"scratch kept: {SCRATCH_ROOT / stamp}")
    else:
        shutil.rmtree(SCRATCH_ROOT / stamp, ignore_errors=True)
    sys.exit(0 if p == n else 1)


if __name__ == "__main__":
    main()
