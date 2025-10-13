# Repository Guidelines

## Project Structure & Module Organization
The core Python package lives in `fragenda/`; extend modules inside rather than creating ad-hoc scripts. Data inputs stay in `input/` and downstream artifacts in `output/`. Reusable HTML or template assets belong in `templates/`. Notebooks used for exploration reside in `notebooks/`—sync key insights back into the package. Tests go in `tests/`, mirroring the package layout. The `splitpages.sh` utility handles PDF splitting; keep shell helpers in the repository root.

## Build, Test, and Development Commands
Run `poetry install` to sync dependencies. Use `poetry shell` for an activated environment or prefix commands with `poetry run`. Execute the automated suite via `poetry run pytest`. Open exploratory notebooks with `poetry run jupyter lab`. Use `bash splitpages.sh path/to/file.pdf` to fan out PDF inputs into named pages.

## Coding Style & Naming Conventions
Target Python 3.10 and follow PEP 8 with 4-space indentation. Prefer dataclasses or TypedDicts for structured records, and annotate functions with types. Module names stay lowercase with underscores (for example, `schedule_parser.py`), while public classes use CapWords and non-exported helpers start with `_`. Keep notebooks numbered (`notebook-v#.ipynb`) and avoid camelCase files.

## Testing Guidelines
Write pytest suites that mirror the package (`tests/test_schedule_parser.py`). Favor fixtures for sample timetable data placed under `tests/data/`. Test new parsing logic against representative rows from `input/`. Aim for meaningful coverage of date parsing, merging, and export routines; add regression tests for bug fixes. Run `poetry run pytest -k ...` when iterating locally and ensure the full suite passes before pushing.

## Commit & Pull Request Guidelines
Follow the existing bracketed prefix pattern: `[add]`, `[fix]`, `[refactor]`, `[docs]`, etc., followed by a concise summary (for example, `[fix] handle empty agenda pages`). Squash noisy commits locally. Pull requests should describe the change set, reference related issues, list manual verification steps, and attach before/after artifacts (export samples or screenshots) when applicable.

## Data & Notebook Practices
Do not commit large raw exports to Git; store transient files in `output/` and add `output/` artifacts to `.gitignore` if needed. Keep notebooks clean—restart kernels before saving and move reusable code into the package so it can be tested. Sanitize data before sharing and remove student-identifying details from sample inputs.
