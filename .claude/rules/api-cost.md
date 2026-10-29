# Paid API rules

- Phase 0 budget for paid APIs is **$0**. Any later budget (e.g. the planned $3 cap) applies only
  after the user approves that specific experiment.
- A paid call needs all of: config `[openai] allow_paid_api = true`, env
  `CIVLAB_ALLOW_PAID_API=yes`, `OPENAI_API_KEY` in the environment, `[budget] max_paid_calls > 0`
  with token and cost limits, and `[pricing]` copied by a human from the provider's official
  pricing page (with `source` and `checked_at`). Do not set any of these without approval.
- Never hard-code model names or prices. Verify them in official docs at the time of use and
  mark anything unverified as unverified.
- Costs from `civlab` are **estimates** (tokens x configured price). Actual billed cost is not
  available from the API response; report it as unknown unless the user supplies billing data.
- Tests and CI must never need a real key: use `MockAdapter`, `ScriptedAdapter`, or the fake
  HTTP transport in `tests/test_openai_adapter.py`. Tests block network access.
- To stop a run immediately: create the file passed as `--stop-file`, or set
  `CIVLAB_KILL_SWITCH=1`.
