---
name: gate
description: Run the full local check (ruff check, ruff format --check, ty check, pytest) and report every failing step. Use before calling any code change done.
---

Run the gate from the repo root:

```bash
bash .claude/skills/gate/gate.sh $ARGUMENTS
```

`--no-tests` skips pytest (use only for doc- or config-only changes).

- Exit 2 means the gate could not run (no `pyproject.toml` yet, or uv missing). Say so; don't substitute pip, a different Python, or a partial set of checks.
- If `ruff format --check` fails, run `uv run ruff format .` and re-run the gate. Never hand-fix formatting.
- Report the summary table and the first lines of each failure. Don't call the work done while any step fails, and don't silence a check (`# noqa`, `# ty: ignore[...]`, skipping a test) just to get it green without saying why.
