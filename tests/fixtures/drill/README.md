# The old removal drill's wiring

What track B's removal drill (`tools/removal_drill.py`) generated for each of its nine sets, at
FastAPI-boilerplate commit `48e8107`. `tests/test_engine_render_drill.py` renders B's catalog from
the same commit for each set and compares the files by AST (spec 2.8: smaller selections are
compared by AST, not bytes).

- `<set>/` holds the files the drill's `build()` writes for that set, as its builders return them,
  before its ruff pass.
- `sets.json` holds each set's closed selection, as the drill's `_selected` gives it.
- `requires.json` holds each of the drill's features and what it requires (its `REQUIRES`).
- `blueprint.toml` is B's catalog at the same commit.
- `generate.py` calls only the drill's `_wiring_*`, `_seeders` and `_tests_wiring` builders. It
  never calls `build()`, which copies the repository and runs subprocesses.

The set directories aren't linted or type-checked (`pyproject.toml` excludes them): they're the
drill's output, kept as it wrote it.

To regenerate, from the fastro repo root, with FastAPI-boilerplate checked out beside it:

```sh
git -C ../FastAPI-boilerplate show 48e8107:tools/removal_drill.py > /tmp/removal_drill.py
git -C ../FastAPI-boilerplate show 48e8107:blueprint.toml > tests/fixtures/drill/blueprint.toml
.venv/bin/python tests/fixtures/drill/generate.py /tmp/removal_drill.py
```

Don't edit these files to make the comparison pass. A difference between the drill and the
catalog is a finding to report.
