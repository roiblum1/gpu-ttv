---
name: r1-status
description: Report Release 1 progress against AGENT.md §20.12 (definition of done), §17's numerical checks and §20.10 Discovery, with honest evidence labels. Use when asked where R1 stands, at a checkpoint, or before claiming R1 is done.
---

Read the current text of AGENT.md §20.10, §20.12 and §17 "Numerical checks" (`grep -n '^### 20.1[02]\|^### Numerical checks' AGENT.md`), not from memory. Then build one table, one row per item.

For each item:
- Find what covers it in `tests/`, `docs/`, `config/`, `deploy/`, `grafana/` or `offline/`. For a test, run it (`uv run pytest -q <path>::<name>`) instead of trusting its name.
- **Status:** `done`, `partial`, `missing` or `blocked`. For `blocked`, name the blocker: access, user review, or an open input (I1–I10).
- **Evidence:** `documented`, `fixture-tested`, `lab-tested` (e.g. the sandbox server-scan) or `production-verified`. Only runs against real production sources count as production-verified.

Rules:
- No evidence means not done. A file existing, or a test named after the requirement, is not a pass.
- The one-day dry-run review and the 10-asset spot check (§20.12) are done only if the user confirmed them in this conversation or a dated record exists in `docs/`.
- While `docs/discovery.md` is incomplete, list each open §20.10 Discovery item too.

Output the table, then the three most useful next actions. Read-only: don't edit files unless asked.
