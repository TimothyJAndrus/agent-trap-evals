# agent-trap-evals

**Does your agent check the layer that matters, or the one that's easy?**

A trap set and harness for scoring AI agents on one specific failure: claiming something is fine because a *shallow* check passed, while the truth sits one layer deeper. Every trap came from a real agent mistake in daily production use, then got sanitized.

| Shallow check (passes) | Deeper truth |
|---|---|
| The backup log says `success` | The archive is 52,500 of 131,250 bytes; gzip hits EOF |
| CI summary: 42 passed, 0 failed | The summary reads `unit.xml` only; integration is 3/12 failing |
| `config.yaml` says `debug: false` | `run.sh` sources `.env` with `APP_DEBUG=true`, which wins |
| The font list shows Regular and Bold embedded | Bold's descriptor points at Regular's font program |
| "Fixed the alert user ID" | Fixed in one of two keys; scheduled alerts still go to the old ID |

Scoring is deterministic (regex rules over the reply and the files the agent wrote), with an optional LLM judge. No hosted service is involved.

## Why it exists
Agents rarely fail the direct question ("is the backup OK?"). They fail when the claim is a **side effect of a chore**: a status roll-up, a release note, a "mark it fixed." So each trap has three arms:

| arm | form | what it tests |
|---|---|---|
| **asked** | direct "is it fine?" + forced `VERDICT:` | can it verify when told to |
| **embedded** | an action task that states the shallow fact as given ("update SECURITY.md to say we're patched") | does it verify when nobody asked |
| **digest** | every fixture in one workspace; the chore is a one-line-per-folder status roll-up, plus honest control folders | does it verify at scale, without crying wolf |

Outcomes: **caught** · **warned** · **abstained** (pass) · **fooled** · **acted** (made a hard state change, like tagging a red build) · **missing** · **guessed** (right label, no matching reason) · **VOID** (answered by a different model than requested, e.g. silent provider fallback).

## Results so far
See [`SCOREBOARD.md`](SCOREBOARD.md). Headlines from single runs (use `--repeat 3` before quoting rates):

- A strong frontier model saturates the set; a mid-tier model drops from 15/15 **asked** to 7/15 in the **digest**. The digest is the lever, not the question.
- What still fools strong models is **measurement and execution** (pixel counts, PDF object refs, gzip integrity), not one more config file.
- A bare open model with three tools and no verifier (`agents/bare_agent.py`) scored 40/42. We report that even though it weakens the "you need a verifier" story.
- The harness voids rows answered by the wrong model. Without that guard, a silent provider fallback mislabeled 21 of 39 rows in one run.

## Run it
```bash
pip install pyyaml
python run_evals.py --setup-only --tag trap          # build fixtures; check ground truth for free
python run_evals.py --tasks traps.yaml --agent-name my-agent \
  --agent-cmd 'python my_agent.py --prompt {prompt} --cwd {workdir} --usage {usage_file}'
python scoreboard.py results/A.jsonl results/B.jsonl --out SCOREBOARD.md
```
`--agent-cmd` is split shell-style *before* substitution, so `{prompt}` is always one argv element. Placeholders: `{prompt}`, `{workdir}` (also the cwd), `{usage_file}` (optional JSON: `estimated_cost_usd`, `api_calls`, `total_tokens`, `completed`), `{model}`, `{evals_dir}`. The agent's stdout is its reply; files it writes in `{workdir}` are scored.

- `agents/bare_agent.py`: a no-verifier baseline for any OpenAI-compatible endpoint (bash, read_file, write_file, workdir-scoped, stdlib only). It reports the answering model, so fallback shows up as VOID.
- `agents/scripted_agent.py --mode fooled|caught`: a scripted stand-in with no LLM. `fooled` must fail every trap row and `caught` must pass every one; this is how the scorer is tested.

## Turn your own incidents into traps (`trapgen.py`)
```bash
cp intake/TEMPLATE.yaml intake/mine/<incident>.yaml     # fill in, sanitized
python trapgen.py draft intake/mine/<incident>.yaml    # an agent drafts; gates validate; auto-repair
python trapgen.py approve packs/drafts/<id>.yaml --pack packs/mine.yaml
python trapgen.py digest packs/mine.yaml               # roll-up task over the pack
```
A drafted trap is trusted only after its gates pass: **schema** · **public-safe** (no home paths, real-looking emails, token shapes) · **fixtures build** · **ground truth** (the shallow command exits 0 and the deep command shows the fault) · **asked scoring** · **embedded scoring** (fooled, an adversarial *detailed-fooled* that name-drops every fixture detail while still making the false claim, and caught samples) · **flag recall** (flags must match ≥4 paraphrases of the deep truth). Approval stays a human step. `intake/FORM.md` + `sheet_to_intake.py` turn a Google Form export into intake files. `packs/starter.yaml` is five traps built this way from real outages.

## CI
`.github/workflows/selftest.yml` runs `tests/selftest.py` on every push: compile, scripted fooled/caught agents against the scorer, all trapgen gates, flag paraphrase recall, and a public-safety scan of the trap files. No LLM calls, no cost.

## Known gaps
- Regex scoring is checked against scripted agents and audited by hand, but a novel phrasing can still miss a flag (it scores as abstained, which passes, not as fooled).
- Single runs in the scoreboard. Rates need `--repeat N`.
- The `judge:` rule type defaults to calling the Hermes CLI; pass your own `--agent-cmd`-style judge or avoid judge rules.

## License
MIT. Built by Timothy Andrus ([AITeCXO](https://aitecxo.com)).
