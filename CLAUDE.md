# CLAUDE.md

## Purpose
This repository hosts the **AI Civilization Lab** (`projects/ai-civilization-lab/`): AI residents
living in a simulated world, and later an AI teacher that improves their decision rules, with
before/after comparison under identical and held-out conditions.

## Current stage: Phase 0 (development foundation)
- Implemented: deterministic World Engine, mock residents, Budget Guard, SQLite Experiment Store,
  replay verification, CLI, CI. OpenAI adapter exists but is **disabled by default**.
- Not implemented: real LLM experiments, AI teacher / self-improvement loop, large populations.
- Decisions are made by the user and ChatGPT. Claude Code implements and verifies. Do not start
  other autonomous agents (dot, Codex, ...) unless the user asks.

## Commands (run in `projects/ai-civilization-lab/`)
```
uv sync --locked                     # create .venv from uv.lock
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
uv run civlab run --config configs/mock-6x10.toml --db runs/dev.sqlite3
uv run civlab verify --db runs/dev.sqlite3
python ../../tools/check_repo_safety.py --history   # from anywhere in the repo
```
All five checks (ruff check, ruff format --check, mypy, pytest, safety) must pass before a commit.

## Hard rules
- Only the World Engine (`world.py`) changes world state. Model output goes through
  `parse_action` and the engine's feasibility checks; never trust it directly.
- **No paid API calls** unless the user explicitly approves a specific experiment and budget.
  See `.claude/rules/api-cost.md`.
- Never commit secrets, `.env`, or experiment databases. Never print API key values.
- Never push to `main`, merge PRs, change repo visibility, branch protection, or the LICENSE.
- Do not delete user data or `runs/` databases without asking.
- No new external services, MCP servers, or paid tools without approval.
- Changing world rules or the mock policy changes the pinned hash in
  `tests/test_simulation.py`; update it only for an intentional change and log it in
  `docs/decision-log.md`.

## Git
- Work on a feature branch (`claude/<topic>`), commit small verified steps, open a Draft PR.

## Reporting
Follow `.claude/rules/verification.md`: separate *implemented*, *tested*, and *verified in CI*;
cite commands, counts, file paths, SHAs and URLs; list what is unfinished and why.
