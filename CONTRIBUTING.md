# Contributing

## Ground rules

- **Read-only toward every source.** server-scan, managers and clusters are read, never written. Each client has an allow-list guard; extend it only with a test proving what is still refused.
- **Unknown is never zero.** Missing telemetry, stale sources, unpriced models and time before first observation stay visible as UNKNOWN or as a lower bound (`≥ X`).
- **Label evidence honestly:** `documented`, `fixture-tested`, `lab-tested`, `production-verified`. Fixtures and labs are never production evidence.
- **Rules live in one place.** Classification is the pure function in `src/ttv/classify.py` and `gpu_rows.py`, described row by row in `docs/adr/0002-classification-rules.md`. Change both together.

## Workflow

1. Branch from `main`.
2. Make the change, and update the docs it makes wrong in the same change: `docs/discovery.md`, `docs/adr/`, the config examples, `.env.example`.
3. Run the gate with Postgres (see README "Development"). It must pass. CI runs it again on every push and pull request.
4. Commit with [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`, `test:`, `ci:`, `chore:`, and `feat!:` for breaking changes.
5. Open a pull request against `main`.

## Code style

- Python 3.12 floor; ruff (line length 100) and ty (not mypy: suppress with `# ty: ignore[rule]`).
- Every function and class has a Google-style docstring (`Args:` / `Returns:` / `Raises:`) whose summary is three lines or fewer.
- At most three consecutive comment lines. Reasoning belongs in an ADR or in `docs/discovery.md`, with a one-line pointer in code.
- Never put credentials in code, fixtures, logs or examples. Fixtures are sanitised captures or obviously synthetic values.

## Lab work

`deploy/lab/` is for lab clusters only. Real endpoints of personal labs and sandboxes go in the gitignored `CLAUDE.local.md`, never in tracked files.
