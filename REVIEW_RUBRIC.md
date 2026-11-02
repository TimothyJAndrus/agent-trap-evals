# Review rubric (used by the AI reviewer and by humans)

Cite the rule number for every finding, and quote the exact diff line that shows it.

- **R1 · Public safety.** Trap files, packs, intake, and fixtures must not contain real home paths (`/Users/…`, `/home/<name>`), real-looking emails outside `example.com/org/net`, tokens or key shapes, or real names, IDs, or hostnames. Fixtures use invented values.
- **R2 · Secrets and CI trust.** Workflows must not use `pull_request_target`, must not echo or print secrets, must not widen `permissions:` beyond what the job needs, and must not run untrusted PR code with secrets in scope.
- **R3 · Scoring integrity.** A change to `flag`, `claim`, `unless_line`, `rubric`, or a rule regex must not make a trap easier to pass by accident: no catch-all patterns (`.`, `.*`, empty alternation), no removed `must: true`, no lowered `pass_threshold` without a stated reason. Trace rules must name the measuring command, not the file being measured.
- **R4 · Tests prove failure, not just success.** A new trap or rule ships with evidence it can fail: the scripted `fooled` (and for rubrics `guess`/`shallow`) agent must fail it in `tests/selftest.py`.
- **R5 · Baseline honesty.** `baselines/ci.json` changes only together with a results run that justifies it, never to make a failing PR pass.

If none of these is violated, approve. Style, naming, and wording are out of scope.
