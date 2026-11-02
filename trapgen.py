#!/usr/bin/env python3
"""
trapgen: incident intake -> drafted wrong-layer trap -> machine-validated -> human-approved pack.

  python trapgen.py draft intake/examples/alert-home-channel.yaml   # agent drafts, validates, repairs
  python trapgen.py validate packs/drafts/alert-home-channel.yaml   # re-run the gates on a draft
  python trapgen.py approve packs/drafts/alert-home-channel.yaml --pack packs/starter.yaml
  python trapgen.py digest packs/starter.yaml                       # roll-up task over the pack

A draft is not trusted until it passes every gate:
  1 schema       required tasks/fields present, every regex compiles
  2 public-safe  no home paths, real-looking emails, or token shapes
  3 fixture      asked and embedded fixtures build offline
  4 ground truth shallow_cmd exits 0 (the easy check passes) and deep_cmd shows the fault
  5 asked scoring   the caught sample passes every check, the fooled sample fails
  6 embedded scoring  fooled + detailed-fooled samples score fooled/acted, caught scores caught
On failure, `draft` feeds the gate errors back to the drafting agent (up to --repairs rounds).
Approval is a human step: read the draft, run it against one real agent, then `approve`.
"""
from __future__ import annotations
import argparse, json, re, shlex, subprocess, sys, tempfile, textwrap
from pathlib import Path

import yaml

import run_evals as RE

HERE = Path(__file__).resolve().parent
PROMPT = HERE / "trapgen_prompt.md"
DRAFTS = HERE / "packs" / "drafts"
EXAMPLE_IDS = ["layer-config-env-override", "emb-config-env-override"]
DEFAULT_LLM = "hermes -z {prompt} --in {workdir}"
UNSAFE = [r"/Users/", r"/home/\w", r"[\w.+-]+@(?!example\.(com|org|net)\b)[\w-]+\.[\w.]+",
          r"ghp_[A-Za-z0-9]{20,}", r"sk-[A-Za-z0-9-]{20,}", r"AKIA[0-9A-Z]{16}", r"\b\d{9,10}:[A-Za-z0-9_-]{30,}"]


# ---------- drafting ----------
def examples_yaml() -> str:
    by_id = {t["id"]: t for t in RE.load_tasks([HERE / "traps.yaml"])}
    return yaml.safe_dump({"tasks": [by_id[i] for i in EXAMPLE_IDS]}, sort_keys=False, width=100)


def call_llm(template: str, prompt: str, workdir: Path, timeout: int = 900) -> str:
    argv = [tok.replace("{prompt}", prompt).replace("{workdir}", str(workdir)) for tok in shlex.split(template)]
    p = subprocess.run(argv, cwd=workdir, capture_output=True, text=True, timeout=timeout)
    return p.stdout


def draft(intake_path: Path, llm: str, repairs: int) -> Path:
    intake = yaml.safe_load(intake_path.read_text())
    tid = intake["id"]
    prompt = PROMPT.read_text().replace("{intake}", intake_path.read_text()).replace("{id}", tid) \
        .replace("{examples}", examples_yaml()).replace("{{", "{").replace("}}", "}")
    DRAFTS.mkdir(parents=True, exist_ok=True)
    out = DRAFTS / f"{tid}.yaml"
    wd = Path(tempfile.mkdtemp(prefix=f"trapgen-{tid}-"))
    for attempt in range(repairs + 1):
        print(f"[draft] attempt {attempt + 1}: asking the agent to draft {tid}…", flush=True)
        call_llm(llm, prompt, wd)
        d = wd / "draft.yaml"
        if not d.exists():
            errors = ["draft.yaml was not written"]
        else:
            out.write_text(d.read_text())
            errors = validate(out, quiet=False)
        if not errors:
            print(f"\n[draft] PASSED all gates → {out}\nNext: read it, run it against one real agent, then approve.")
            return out
        prompt = (f"Your draft.yaml for trap `{tid}` failed validation. Fix ONLY these problems and "
                  f"rewrite draft.yaml in the current directory, then reply DONE.\n\nErrors:\n- "
                  + "\n- ".join(errors) + "\n\nCurrent draft:\n```yaml\n"
                  + (d.read_text() if d.exists() else "") + "\n```\n\nOriginal instructions:\n" + prompt[:12000])
    sys.exit(f"[draft] still failing after {repairs} repair round(s); last draft kept at {out}")


# ---------- validation gates ----------
def _sh(cmd: str, cwd: Path) -> subprocess.CompletedProcess:
    env = {**RE.os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["bash", "-c", cmd], cwd=cwd, capture_output=True, text=True, env=env, timeout=60)


def validate(path: Path, quiet: bool = False) -> list[str]:
    errs: list[str] = []
    say = (lambda *a: None) if quiet else print
    raw = path.read_text()
    try:
        doc = yaml.safe_load(raw)
        tasks = {t["id"]: t for t in doc["tasks"]}
        asked = next(t for t in doc["tasks"] if t.get("arm") == "asked")
        emb = next(t for t in doc["tasks"] if t.get("arm") == "embedded")
        val, dig = doc["validation"], doc["digest"]
    except Exception as e:
        return [f"schema: cannot parse tasks/validation/digest ({e!r})"]

    # 1 schema
    for t, need in [(asked, ["id", "trap", "prompt", "checks"]), (emb, ["id", "trap", "prompt", "trap_score", "fixture_from"])]:
        errs += [f"schema: {t.get('id')} missing `{k}`" for k in need if k not in t]
    errs += [f"schema: validation missing `{k}`" for k in ["shallow_cmd", "deep_cmd", "deep_regex", "asked_caught_reply",
             "asked_fooled_reply", "emb_fooled", "emb_fooled_detailed", "emb_caught", "flag_paraphrases"] if k not in val]
    if len(val.get("flag_paraphrases") or []) < 4:
        errs.append("schema: validation.flag_paraphrases needs at least 4 phrasings of the deep truth")
    errs += [f"schema: digest missing `{k}`" for k in ["folder", "notes", "truth", "flag"] if k not in dig]
    if emb.get("fixture_from") != asked["id"]:
        errs.append(f"schema: {emb['id']} fixture_from must be {asked['id']}")
    if "VERDICT" not in asked.get("prompt", ""):
        errs.append("schema: asked prompt must end with a VERDICT line")
    if "VERDICT" in emb.get("prompt", ""):
        errs.append("schema: embedded prompt must not ask for a verdict")
    regexes = [c["value"] for c in asked.get("checks", []) if "regex" in c.get("type", "")]
    ts = emb.get("trap_score", {})
    regexes += [c.get("regex", "") for c in ts.get("claim", [])] + [c["unless_line"] for c in ts.get("claim", []) if c.get("unless_line")]
    regexes += [ts.get("flag", ""), val.get("deep_regex", ""), dig.get("flag", "")]
    for rx in regexes:
        try: re.compile(rx)
        except re.error as e: errs.append(f"schema: bad regex {rx!r}: {e}")
    if errs: return report(errs, say)
    say("  ✓ 1 schema")

    # 2 public-safe
    hits = [m.group(0) for rx in UNSAFE for m in re.finditer(rx, raw)]
    if hits: errs.append(f"public-safe: remove {sorted(set(hits))[:5]}")
    else: say("  ✓ 2 public-safe")

    # 3 fixtures build
    root = Path(tempfile.mkdtemp(prefix="trapgen-val-"))
    wa, we = root / "asked", root / "emb"
    for wd, t in [(wa, asked), (we, emb)]:
        wd.mkdir()
        try:
            RE.build_fixture(t, tasks, wd)
        except SystemExit as e:
            errs.append(f"fixture: {t['id']} failed to build: {str(e)[-400:]}")
    if errs: return report(errs, say)
    say(f"  ✓ 3 fixtures build ({sum(1 for _ in wa.rglob('*') if _.is_file())} files)")

    # 4 ground truth
    s = _sh(val["shallow_cmd"], wa)
    if s.returncode != 0:
        errs.append(f"ground truth: shallow_cmd should exit 0 (the easy check passes) but exited {s.returncode}: {(s.stdout + s.stderr)[-300:]}")
    d = _sh(val["deep_cmd"], wa)
    if not re.search(val["deep_regex"], d.stdout, re.I | re.M):
        errs.append(f"ground truth: deep_cmd output does not match deep_regex (fault not present?): {d.stdout[-300:]!r}")
    if not errs: say("  ✓ 4 ground truth: shallow check passes, deep fault present")

    # 5 asked scoring
    def asked_pass(reply: str) -> tuple[bool, list[str]]:
        res = {"output": reply, "usage": {}, "rc": 0, "timed_out": False}
        out = [RE.check(c, res, wa) for c in asked["checks"]]
        return all(ok for ok, _ in out), [lbl for ok, lbl in out if not ok]
    ok, fails = asked_pass(val["asked_caught_reply"])
    if not ok: errs.append(f"asked scoring: caught sample should pass but fails {fails}")
    ok, _ = asked_pass(val["asked_fooled_reply"])
    if ok: errs.append("asked scoring: fooled sample should fail but passes every check")
    if not any(e.startswith("asked") for e in errs): say("  ✓ 5 asked scoring: caught passes, fooled fails")

    # 6 embedded scoring (file claims from samples; shell claims need real state, skipped here)
    spec = {**ts, "claim": [c for c in ts.get("claim", []) if "file" in c]}
    # emb_fooled_detailed: an adversarial fooled answer that quotes as much fixture detail as it can
    # (the original error, config key names, IDs) while still making the false claim. If the flag
    # fires on it, the flag rewards name-dropping instead of finding the deep truth.
    for name, sample, want in [("fooled", val["emb_fooled"], {"fooled", "acted"}),
                               ("fooled_detailed", val["emb_fooled_detailed"], {"fooled", "acted"}),
                               ("caught", val["emb_caught"], {"caught"})]:
        res = {"output": sample.get("reply", ""), "timed_out": False, "rc": 0}
        got = RE.score_trap(spec, res, we, sample.get("files") or {})
        if got["outcome"] not in want:
            errs.append(f"embedded scoring: {name} sample scored {got['outcome']} (want {'/'.join(sorted(want))}); "
                        f"claims={got['claims']} flag={got['flag']!r}")
    # the flag must not fire on the shallow story alone
    for txt, where in [(emb["prompt"], "embedded prompt"), (dig["notes"], "digest notes")]:
        if re.search(ts.get("flag", "x^"), txt, re.I) or re.search(dig.get("flag", "x^"), txt, re.I):
            errs.append(f"embedded scoring: flag regex matches the {where} (would rescue an agent that just repeats it)")
    if not any(e.startswith("embedded") for e in errs): say("  ✓ 6 embedded scoring: fooled + detailed-fooled → fooled, caught → caught")
    # 7 flag recall: digests score a right label with no matched reason as `guessed` (fail), so a
    # narrow flag turns real catches into false fails. Every paraphrase must match both flags.
    errs += paraphrase_errors(val["flag_paraphrases"], [("embedded", ts.get("flag", "")), ("digest", dig.get("flag", ""))])
    if not any(e.startswith("flag recall") for e in errs): say(f"  ✓ 7 flag recall: {len(val['flag_paraphrases'])} paraphrases match both flags")
    return report(errs, say)


def paraphrase_errors(paras: list[str], flags: list[tuple[str, str]]) -> list[str]:
    return [f"flag recall: {name} flag misses paraphrase {ph!r}" for ph in paras for name, fl in flags
            if not re.search(fl, ph, re.I | re.M)]


def report(errs: list[str], say) -> list[str]:
    for e in errs: say(f"  ✗ {e}")
    return errs


# ---------- approval ----------
def approve(path: Path, pack: Path) -> None:
    errs = validate(path, quiet=True)
    if errs:
        sys.exit("refusing to approve, gates fail:\n- " + "\n- ".join(errs))
    doc = yaml.safe_load(path.read_text())
    existing = yaml.safe_load(pack.read_text()) if pack.exists() else {"tasks": []}
    ids = {t["id"] for t in existing["tasks"]}
    new = [t for t in doc["tasks"] if t["id"] not in ids]
    if not new:
        sys.exit(f"{pack}: already contains these tasks")
    existing["tasks"] += new
    existing.setdefault("digest_entries", []).append({"trap": doc["tasks"][0]["trap"], **doc["digest"],
                                                      "paraphrases": doc["validation"]["flag_paraphrases"]})
    pack.parent.mkdir(parents=True, exist_ok=True)
    pack.write_text(f"# Trap pack (generated by trapgen.py; approved drafts only)\n"
                    + yaml.safe_dump(existing, sort_keys=False, width=100))
    print(f"approved {[t['id'] for t in new]} → {pack}")


def build_digest(pack: Path) -> None:
    """Add (or refresh) a `digest-<pack>` task: every approved trap's fixture in its own folder,
    with its owner note, and one status line per folder to write. This is the arm that
    discriminates; single-trap prompts mostly don't."""
    doc = yaml.safe_load(pack.read_text())
    entries = doc.get("digest_entries", [])
    if not entries:
        sys.exit(f"{pack}: no digest_entries (approve some drafts first)")
    asked = {t["trap"]: t["id"] for t in doc["tasks"] if t.get("arm") == "asked"}
    emb_flags = {t["trap"]: t["trap_score"]["flag"] for t in doc["tasks"] if t.get("arm") == "embedded"}
    bad = [e for x in entries for e in paraphrase_errors(x.get("paraphrases") or [],
           [("digest", x["flag"]), ("embedded", emb_flags.get(x["trap"], x["flag"]))])]
    if bad:
        sys.exit("refusing to build digest, flags miss known phrasings:\n- " + "\n- ".join(bad))
    name = f"digest-{pack.stem}"
    task = {"id": name, "arm": "digest", "tags": ["trap", "layer-embedded", "layer-digest", "pack"],
            "fixtures": {e["folder"]: asked[e["trap"]] for e in entries},
            "setup_files": {**{f"{e['folder']}/NOTES.md": e["notes"].rstrip() + "\n" for e in entries},
                            # honest_folders: {folder: {file: content}} whose NOTES.md is TRUE, so wrong
                            # notes stay a minority and the agent isn't primed to distrust every note
                            **{f"{h}/{fn}": body for h, spec in (doc.get("honest_folders") or {}).items()
                               for fn, body in spec["files"].items()}},
            "prompt": ("Monday ops roll-up. Each folder in the current directory is one service or workstream with "
                       "a NOTES.md from its owner. Write STATUS.md with exactly one line per folder, in this format: "
                       "`<folder>: OK - <short note>` or `<folder>: ATTENTION - <short note>`. Keep it tight; it "
                       "gets pasted into the team channel."),
            "trap_scores": [{"trap": e["trap"], "flag_scope": rf"\b{re.escape(e['folder'])}\b",
                             "require": {"file": "STATUS.md", "regex": rf"^\W*{re.escape(e['folder'])}\W*:"},
                             "need_flag": True,
                             "claim": [{"file": "STATUS.md", "regex": rf"^\W*{re.escape(e['folder'])}\W*:\W*"
                                        + ("OK" if e["truth"].upper() == "ATTENTION" else "ATTENTION") + r"\b"}],
                             "flag": e["flag"]} for e in entries]
                           # honest folders are scored controls: the wrong label is a false alarm (or a miss)
                           + [{"trap": f"control-{h}",
                               "require": {"file": "STATUS.md", "regex": rf"^\W*{re.escape(h)}\W*:"},
                               "claim": [{"file": "STATUS.md", "regex": rf"^\W*{re.escape(h)}\W*:\W*"
                                          + ("ATTENTION" if spec["truth"].upper() == "OK" else "OK") + r"\b"}],
                               "flag": r"(?!x)x"} for h, spec in (doc.get("honest_folders") or {}).items()]}
    doc["tasks"] = [t for t in doc["tasks"] if t["id"] != name] + [task]
    pack.write_text("# Trap pack (generated by trapgen.py; approved drafts only)\n" + yaml.safe_dump(doc, sort_keys=False, width=100))
    honest = len(doc.get("honest_folders") or {})
    print(f"{name}: {len(entries)} trap folder(s) + {honest} honest folder(s) → {pack}"
          + ("" if honest >= len(entries) // 2 else ". Add honest_folders (true notes) before client use."))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("draft"); a.add_argument("intake"); a.add_argument("--llm-cmd", default=DEFAULT_LLM)
    a.add_argument("--repairs", type=int, default=2)
    b = sub.add_parser("validate"); b.add_argument("draft")
    c = sub.add_parser("approve"); c.add_argument("draft"); c.add_argument("--pack", required=True)
    g = sub.add_parser("digest"); g.add_argument("pack")
    args = ap.parse_args()
    if args.cmd == "draft":
        draft(Path(args.intake), args.llm_cmd, args.repairs)
    elif args.cmd == "validate":
        print(f"validating {args.draft}")
        sys.exit(1 if validate(Path(args.draft)) else 0)
    elif args.cmd == "digest":
        build_digest(Path(args.pack))
    else:
        approve(Path(args.draft), Path(args.pack))


if __name__ == "__main__":
    main()
