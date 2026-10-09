# Contributing to Modus

Thanks for your interest in Modus. This is a small, maintainer-run project;
issues and pull requests are welcome, though responses are best-effort.

By contributing you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).

## Development setup

Requirements: Python 3.10+ (3.12 recommended; CI uses 3.12), and Docker if you
want to try the Compose stack. The SDK alone declares `>=3.9`, but the pinned
orchestrator dependencies need 3.10 or newer.

```bash
git clone https://github.com/babbguy/Modus.git
cd Modus
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r orchestrator/requirements.txt -r requirements-dev.txt
pip install -e ./sdk
```

Run the orchestrator locally (development mode only; stub auth is unsafe
anywhere else):

```bash
export MODUS_ENVIRONMENT=development MODUS_AUTH_MODE=stub
export MODUS_DATABASE_URL=sqlite+aiosqlite:///modus.db
export MODUS_MASTER_API_KEY=mds_master_$(python -c "import secrets; print(secrets.token_urlsafe(32))")
python -m uvicorn orchestrator.main:app --reload --port 8080
```

## Tests and linting

```bash
python -m pytest -q                              # full suite; takes several minutes
python -m pytest tests/test_policies_api.py -q   # a single file
ruff check .                                     # lint (config in ruff.toml)
```

Please run both before opening a pull request, and add or update tests for any
behaviour you change. Tests use an in-memory SQLite database and need no
external services.

Pull requests into `develop` and `main` must also pass the release gate
(`Release gate (sqlite)` and `Release gate (postgres)`), which runs the real
Docker image with real SDK traffic and a browser; run it locally with
`python e2e/run_gate.py --db sqlite` (see [e2e/README.md](e2e/README.md)).

## Project conventions

- **The SDK stays standard-library only.** Do not add third-party imports to
  `sdk/modus/`. Optional integrations must be imported lazily and degrade
  gracefully when absent.
- **Money is never a float.** Use `Decimal` in Python and `NUMERIC(18,8)` in the
  database. Timestamps are UTC, ISO-8601.
- **Validate at boundaries and do not fail silently.** Log or raise; do not
  swallow errors.
- **Database changes need an Alembic migration** under `migrations/versions/`.
  The test suite runs on SQLite, which builds tables straight from the models,
  so it cannot catch a missing migration. Generate one with
  `alembic revision --autogenerate`, then apply it (`alembic upgrade heads`) to
  an empty PostgreSQL database and run `python scripts/check_migrations.py`
  with `MODUS_DATABASE_URL` pointing at it; the CI job "PostgreSQL migrations"
  does the same and also checks that `alembic downgrade base` works. The
  history starts at the single baseline `migrations/versions/0001_baseline_schema.py`.
- **Keep optional integrations optional.** Nothing should make outbound calls
  unless the operator has enabled it.
- Add the SPDX header (`Copyright 2026 babbguy` and
  `SPDX-License-Identifier: Apache-2.0`) to new source files.

## Branches and releases

The project follows git flow:

| Branch | Purpose |
|--------|---------|
| `main` | Released code only. Every commit on `main` is a tagged release (`vX.Y.Z`). |
| `develop` | Integration branch and the repository's default branch. |
| `feat/<topic>`, `fix/<topic>` | Work branches, created from `develop` and merged back into it by pull request. |
| `release/X.Y.Z` | Created from `develop` to prepare a release (version bump, changelog), merged into `main` by pull request, tagged, then merged back into `develop`. |
| `hotfix/X.Y.Z` | Created from `main` for an urgent fix to a release, merged into `main` (tagged) and into `develop`. |

`main` and `develop` are protected: changes land only through pull requests whose
CI checks pass, and direct or force pushes are refused.

## Pull request process

1. Open an issue first for anything larger than a small fix, so we can agree on
   the approach.
2. Branch from `develop` (`feat/<topic>` or `fix/<topic>`) and open the pull
   request against `develop`.
3. Keep commits focused, with clear messages in the imperative mood
   (for example `fix: reject negative token counts at ingest`).
4. Make sure `pytest` and `ruff check .` pass.
5. Open a pull request using the template and describe what changed and how you
   tested it.

## Reporting security issues

Please do not file public issues for vulnerabilities. See
[SECURITY.md](SECURITY.md).
