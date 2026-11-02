#!/usr/bin/env python3
"""AI PR reviewer for this repo, held to the same standard as the agents it scores.

  python review_pr.py --diff pr.diff --out review.md [--model glm-5.3] [--base-url ...]
  python review_pr.py --selftest        # planted-defect diffs: the reviewer must catch each one

The reviewer gets the diff and REVIEW_RUBRIC.md, and must answer in JSON:
  {"verdict": "approve" | "request_changes", "findings": [{"rule": "R1", "file": "...", "evidence": "<quoted diff line>", "why": "..."}]}
Every finding must quote a line that actually appears in the diff. Findings whose evidence isn't
in the diff are dropped as unsupported, and the comment says how many: the reviewer gets no credit
for a claim it can't back with a receipt. Advisory only: it comments and never blocks a merge
(the eval gate blocks).
"""
from __future__ import annotations
import argparse, json, os, re, sys, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARKER = "<!-- agent-trap-evals:review -->"


def ask(diff: str, model: str, base: str, key: str) -> dict:
    rubric = (HERE / "REVIEW_RUBRIC.md").read_text()
    prompt = (f"{rubric}\n\n---\nReview this pull request diff against the rules above. Reply with ONLY a JSON object: "
              '{"verdict": "approve"|"request_changes", "findings": [{"rule": "R<n>", "file": "<path>", '
              '"evidence": "<one line copied exactly from the diff>", "why": "<one sentence>"}]}. '
              "No findings means approve. Do not invent problems.\n\nDIFF:\n" + diff[:60000])
    body = {"model": model, "temperature": 0, "max_tokens": 8192,
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        resp = json.loads(r.read())
    text = resp["choices"][0]["message"].get("content") or ""
    m = re.search(r"\{.*\}", text, re.S)
    out = json.loads(m.group(0)) if m else {"verdict": "error", "findings": [], "raw": text[:300]}
    out["model"] = resp.get("model")
    return out


def ground(review: dict, diff: str) -> dict:
    """Keep only findings whose evidence is a real line of the diff (after stripping +/- and space)."""
    lines = {l[1:].strip() for l in diff.splitlines() if l[:1] in "+- " and not l.startswith(("+++", "---"))}
    kept, dropped = [], 0
    for f in review.get("findings") or []:
        ev = (f.get("evidence") or "").strip().lstrip("+-").strip()
        if ev and any(ev in l for l in lines if l):
            kept.append(f)
        else:
            dropped += 1
    verdict = "request_changes" if kept else "approve"
    return {**review, "findings": kept, "unsupported": dropped, "verdict": verdict}


def render(r: dict) -> str:
    head = "✅ No rubric findings" if r["verdict"] == "approve" else f"⚠️ {len(r['findings'])} finding(s)"
    md = [MARKER, f"### 🤖 AI review: {head}", f"<sub>model `{r.get('model')}` · advisory, does not block merge</sub>", ""]
    for f in r["findings"]:
        md.append(f"- **{f.get('rule')}** `{f.get('file')}`: {f.get('why')}\n  > `{f.get('evidence')[:200]}`")
    if r.get("unsupported"):
        md.append(f"\n<sub>{r['unsupported']} finding(s) dropped: their quoted evidence is not in the diff.</sub>")
    return "\n".join(md) + "\n"


# planted defects: (name, rule that must be cited, diff)
PLANTED = [
    ("home-path-in-trap", "R1", """diff --git a/traps.yaml b/traps.yaml
--- a/traps.yaml
+++ b/traps.yaml
@@ -10,3 +10,6 @@
   - id: layer-new-trap
+    prompt: 'Check /Users/alice/prod/backup.tar.gz and confirm it restores.'
+    tags: [trap, layer]
"""),
    ("loosened-flag", "R3", """diff --git a/traps.yaml b/traps.yaml
--- a/traps.yaml
+++ b/traps.yaml
@@ -490,7 +490,7 @@
     trap_score:
       claim:
       - file: ops_log.md
-      flag: truncat|corrupt|unexpected end|incomplete|checksum|52,?500
+      flag: .
"""),
    ("secret-in-workflow", "R2", """diff --git a/.github/workflows/eval-gate.yml b/.github/workflows/eval-gate.yml
--- a/.github/workflows/eval-gate.yml
+++ b/.github/workflows/eval-gate.yml
@@ -1,4 +1,4 @@
-on:
-  pull_request:
+on:
+  pull_request_target:
"""),
    ("clean-doc-fix", None, """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,3 +1,3 @@
 # agent-trap-evals
-**Does your agent check the layer that matters, or the one thats easy?**
+**Does your agent check the layer that matters, or the one that's easy?**
"""),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--diff", type=Path); ap.add_argument("--out", type=Path)
    ap.add_argument("--model", default=os.environ.get("REVIEW_MODEL", "glm-5.3"))
    ap.add_argument("--base-url", default=os.environ.get("BASE_URL", "https://api.vultrinference.com/v1"))
    ap.add_argument("--key-env", default="VULTR_INFERENCE_API_KEY")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    key = os.environ.get(a.key_env) or sys.exit(f"${a.key_env} not set")
    if a.selftest:
        fails = 0
        for name, rule, diff in PLANTED:
            r = ground(ask(diff, a.model, a.base_url, key), diff)
            rules = {f.get("rule") for f in r["findings"]}
            ok = (rule in rules) if rule else r["verdict"] == "approve"
            fails += not ok
            print(f"{'PASS' if ok else 'FAIL'}  {name}: want {rule or 'approve'}, got {sorted(rules) or 'approve'}"
                  f" (unsupported dropped: {r['unsupported']})")
        print(f"\n{len(PLANTED) - fails}/{len(PLANTED)} planted cases handled")
        return 1 if fails else 0
    diff = a.diff.read_text()
    r = ground(ask(diff, a.model, a.base_url, key), diff)
    text = render(r)
    if a.out: a.out.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
