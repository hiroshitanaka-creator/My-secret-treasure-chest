# Verification and reporting

- "Implemented", "tested locally", "passed in GitHub CI" and "works in a real experiment" are
  different facts. Say exactly which one you have evidence for.
- Every claim of success cites its evidence: the command, the pass/fail counts, the file path,
  the commit SHA, and for CI the run URL and conclusion.
- Local test success is not CI success. If CI was not observed, say "CI not confirmed" and why.
- Values not actually obtained (actual API cost, a model's behaviour, a version you did not
  check) are reported as unknown, not filled in.
- Unfinished items are listed with cause and next step; never drop them from the report.
- For independent review, use the `independent-verifier` subagent: it re-runs checks and reads
  diffs instead of trusting the implementer's summary.
