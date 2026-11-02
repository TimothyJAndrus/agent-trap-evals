#!/usr/bin/env python3
"""
Google Form responses (CSV export of the linked Sheet) -> one trapgen intake YAML per incident.

  python intake/sheet_to_intake.py responses.csv --client acme [--out intake/acme]

Columns are matched by the key phrase in each question (see intake/FORM.md), so lightly reworded
questions still map. Every file gets a `review:` list: unsanitized-looking content (emails, IPs,
hostnames, URLs, key shapes), thin answers to the three questions that make a trap (Q3 check,
Q4 truth, Q6 measurement), and rows without the sanitization consent. Fix those before
`trapgen.py draft`. trapgen's own safety gate still runs on the drafted trap.
"""
from __future__ import annotations
import argparse, csv, re, sys
from pathlib import Path

import yaml

# intake field -> key phrase in the form question
FIELDS = {
    "title": "what went wrong",
    "incident": "what happened",
    "shallow_check": "made it look fine",
    "deep_truth": "actually true",
    "found_out": "how did you find out",
    "measurement": "shown the truth earlier",
    "action_task": "task was being done",
    "false_claim": "written, announced, or done",
    "direction": "which way was it wrong",
    "cost": "how much did it cost",
    "ai_involved": "ai tool involved",
    "evidence_location": "evidence live",
    "consent": "i removed names",
    "contact": "your email",
}
CORE = ["shallow_check", "deep_truth", "measurement"]  # thin answers here make a weak trap
LEAKS = {
    "email address": r"[\w.+-]+@(?!example\.(com|org|net)\b)[\w-]+\.[\w.]+",
    "IP address": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    "hostname/URL": r"\b(?:https?://)?(?![\w-]*example\b)[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|io|local|internal|corp|cloud)\b",
    "secret-looking token": r"\b(?:ghp_|sk-|AKIA|xox[bp]-)[A-Za-z0-9_-]{8,}",
    "home path": r"/(?:Users|home)/\w+",
}
ARTIFACT_HINTS = [("release note", "RELEASE_NOTES.md"), ("status", "STATUS.md"), ("ticket", "RESOLUTION.md"),
                  ("incident", "RESOLUTION.md"), ("sign", "SIGNOFF.md"), ("checklist", "CHECKLIST.md"),
                  ("announce", "ANNOUNCEMENT.md"), ("log", "ops_log.md")]


def slug(s: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:48].rstrip("-") or "incident"
    out, i = base, 2
    while out in taken:
        out, i = f"{base}-{i}", i + 1
    taken.add(out)
    return out


def map_columns(header) -> dict[str, str]:
    cols = {}
    for field, phrase in FIELDS.items():
        hit = next((h for h in header if phrase in h.lower()), None)
        if hit: cols[field] = hit
    return cols


def convert(row: dict, cols: dict, taken: set[str]) -> tuple[dict, list[str]]:
    v = {f: (row.get(c) or "").strip() for f, c in cols.items()}
    review = []
    if not v.get("consent"):
        review.append("submitter did NOT confirm sanitization (Q13): read every field before use")
    for f in CORE:
        if len(v.get(f, "").split()) < 8:
            review.append(f"thin answer for {f} ({len(v.get(f, '').split())} words): follow up with the submitter")
    for f, txt in v.items():
        if f == "contact": continue
        for kind, rx in LEAKS.items():
            for m in re.finditer(rx, txt, re.I):
                review.append(f"possible {kind} in {f}: {m.group(0)!r} (replace with a generic stand-in)")
    task = v.get("action_task", "").lower()
    artifact = next((a for k, a in ARTIFACT_HINTS if k in task), "RESOLUTION.md")
    direction = "over-claim" if "broken, but it was fine" in v.get("direction", "").lower() else "under-claim"
    measurement = v.get("measurement", "")
    if v.get("evidence_location"):
        measurement += f"\n(Evidence lives in: {v['evidence_location']})"
    intake = {
        "id": slug(v.get("title", ""), taken),
        "title": v.get("title", ""),
        "incident": v.get("incident", "") + (f"\n\nHow it was found: {v['found_out']}" if v.get("found_out") else ""),
        "shallow_check": v.get("shallow_check", ""),
        "deep_truth": v.get("deep_truth", ""),
        "false_claim": v.get("false_claim", ""),
        "direction": direction,
        "measurement": measurement.strip(),
        "action_task": v.get("action_task", ""),
        "artifact": artifact,
        "source": {"cost": v.get("cost", ""), "ai_involved": v.get("ai_involved", ""), "contact": v.get("contact", "")},
    }
    return intake, review


class Literal(str): pass
yaml.add_representer(Literal, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv"); ap.add_argument("--client", required=True); ap.add_argument("--out")
    args = ap.parse_args()
    out = Path(args.out or Path(__file__).parent / args.client); out.mkdir(parents=True, exist_ok=True)
    with open(args.csv, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = map_columns(reader.fieldnames or [])
        missing = [f for f in ["title", "incident", "shallow_check", "deep_truth", "measurement", "action_task", "false_claim"] if f not in cols]
        if missing:
            sys.exit(f"CSV is missing questions for: {missing}. Check the key phrases in intake/FORM.md.")
        taken = {p.stem for p in out.glob("*.yaml")}
        n_ok = n_rev = 0
        for row in reader:
            if not any((row.get(c) or "").strip() for c in cols.values()): continue
            intake, review = convert(row, cols, taken)
            doc = {k: (Literal(v) if isinstance(v, str) and "\n" in v else v) for k, v in intake.items()}
            if review: doc["review"] = review
            path = out / f"{intake['id']}.yaml"
            header = "# Generated by sheet_to_intake.py. Resolve every `review:` item, delete the list, then run trapgen.py draft.\n"
            path.write_text(header + yaml.dump(doc, sort_keys=False, width=100, allow_unicode=True))
            n_rev += bool(review); n_ok += not review
            print(f"{'REVIEW' if review else 'ready '} {path}" + "".join(f"\n         - {r}" for r in review))
    print(f"\n{n_ok + n_rev} intake file(s) → {out} ({n_rev} need review)")


if __name__ == "__main__":
    main()
