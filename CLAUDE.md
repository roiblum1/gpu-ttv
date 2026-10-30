# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

GPU Time-to-Value platform: tracks GPU servers from intake to productive use and charges idle GPU-hours to the stage owner (DC → Redbull → POPAI → Serving). The binding brief is `AGENT.md` (~1,200 lines; read the sections a task touches rather than working from memory). Precedence: **Section 0 > Section 20 > Sections 1–19**. Where Sections 1–19 go beyond Release 1, they are the contract for R2/R3.

- Current phase: **R1, Phase 0 Discovery (§20.10)**. Produce `docs/discovery.md`; no product code until Discovery is done. If a finding contradicts Section 0, stop and report it instead of redesigning.
- This folder is the repo root. §20.9 calls it `gpu-ttv/`; build that layout (`src/ttv/`, `migrations/`, `config/`, `grafana/`, `deploy/`, `offline/`) here, not in a subfolder.
- The target is air-gapped: no runtime downloads, no external fonts/CDNs in Grafana, digest-pinned images.

## Rules that are easy to break

- **Read-only everywhere** outside our own Postgres: no writes to server-scan, UCS/OpenManage/Intersight, or clusters. A hook refuses mutating `oc`/`kubectl`/`helm` commands; installs are the user's to run. The one exception is a lab cluster the user approved: its API URL is listed in the gitignored `.claude/lab-clusters.local`, and the command must name a kubeconfig, by literal path, whose servers are all on that list. Synthetic service probes stay disabled unless a service is authorised in config.
- The engine runs `--dry-run` (classify and log, no writes) against production for at least a day, and the user reviews it, before ledger writes are enabled (§20.6).
- **Unknown is never zero.** Stale sources, missing telemetry, unpriced GPU models and time before observation stay visible as UNKNOWN or a lower bound (`≥ X`). A partial listing never closes intervals.
- **Label evidence honestly:** `documented`, `fixture-tested`, `lab-tested`, `production-verified`. Fixtures and the sandbox below are never production evidence.
- `ocp*` is a **prefix/glob** rule (`^ocp.*` as a regex), not the regex `ocp*`. Use the same pattern server-scan's collectors use.
- Never hard-code Run:ai CRD names or serving-engine metric names; discover them (`oc api-resources --api-group=run.ai`) and keep versioned mappings.
- `src/ttv/classify.py` is pure (no I/O). §20.2 is first-match-wins, and per-asset GPU buckets must sum to `E`.
- Identity: `asset_id` is `<vendor>:<serial_normalized>` using server-scan's normalisation. Never key on hostname, IP, profile name or node name alone; ambiguous joins go to `identity_conflicts`, never a silent merge.
- Serving traffic: one counter of record per logical deployment. Never add gateway, frontend and worker counters together, and never average per-instance percentiles. No prompt content or request/user IDs in metric labels.
- Ledger intervals hold physical GPU counts only; money is computed at query time from a selected pricing snapshot. Next-action annotations never move totals.
- Keep the three money views separate (energy, AWS-equivalent, internal cost) and never sum them. AWS-equivalent value is a comparison, not our cost or a loss.
- Don't rebuild server-scan's collectors; the only new inventory code is the profile-less collector.

## server-scan

- Code: `../server-scan` (github.com/team-redbull/server-scan). AGENT.md's references are pinned to commit `defe172`, which the local checkout may lack: run `git -C ../server-scan fetch` first, then read files with `git -C ../server-scan show defe172:<path>`. Never edit, commit to, or switch branches in that repo.
- Sandbox instance (read-only GETs only; demo data, not production): its URL is in the gitignored `CLAUDE.local.md` (it has no auth, so it is never committed). API under `/api/v1/`.
- Never call `GET /api/v1/servers/available`: despite being a GET, it runs a live recheck that writes through server-scan's ingest pipeline.

## Stack and checks

Python 3.12 floor (`requires-python >=3.12`, ruff `target-version = "py312"`, ty `python-version = "3.12"`), uv, FastAPI, httpx async, pydantic v2 + pydantic-settings, psycopg 3, structlog, prometheus-client, plain SQL migrations. Mirror server-scan's `pyproject.toml` tool config where it fits.

Before calling work done, run `/gate`, which runs:

```bash
uv run ruff check . && uv run ruff format --check . && uv run ty check src tests
uv run pytest -q
```

- The type checker is **ty, not mypy**: suppress with `# ty: ignore[rule-name]`. A coded `# type: ignore[...]` silently suppresses nothing.
- If `ruff format --check` fails, run `uv run ruff format .`; don't hand-fix formatting.
- The §17 numerical checks and §20.12 scenarios must be automated tests with those exact numbers (24 → 16 overdue GPU-hours, 12 GPU-hours partial idle, linear pricing, unknown ≠ zero). Pricing tests use obviously synthetic rates.

## Code and docs

- Every function and class gets a Google-style docstring (`Args:` / `Returns:` / `Raises:` / `Yields:`, types in parentheses, summary of 3 lines or fewer).
- At most 3 consecutive comment lines. Reasoning goes in `docs/adr/` (decisions) or `docs/discovery.md` (verified source facts), with a one-line pointer in code. Delete comments that restate the code.
- Each change updates the docs it makes wrong, in the same change: `docs/discovery.md`, `docs/adr/`, `docs/executive-question-map.md`, `config/panel-contracts.yaml`, `.env.example`.
- `.env.example` is committed; `.env` is gitignored. Never put credentials in fixtures, logs or exported config; fixtures are sanitised captures.

## Git

Only run `git init`, commit or push when asked. Commit subjects follow Conventional Commits (`feat:`, `fix:`, `feat!:`).
