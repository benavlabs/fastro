# Track B's catalog and committed wiring at e46bf26

B's `blueprint.toml`, and every file its `[[wiring]]` declares, as committed at FastAPI-boilerplate
commit `e46bf26` ("give taskiq's reload extra to the taskiq feature, and drop the example's em
dash"). It's the first commit with `line_length = 128` in `[wiring_defaults]`, and it comes before
B sets `overlay`, which the engine handles from A5.

`tests/test_engine_render_boilerplate.py` renders the catalog with every feature selected, through
`plan` and `render`, and compares each file byte for byte with the copy here (spec 2.8: B's
committed wiring for `everything` is the golden output).

Every file was copied with `git show` from the commit, never from B's working tree. From the
fastro repo root, with FastAPI-boilerplate checked out beside it:

```sh
git -C ../FastAPI-boilerplate show e46bf26:blueprint.toml > 'tests/fixtures/fastro-boilerplate@e46bf26/blueprint.toml'
git -C ../FastAPI-boilerplate show e46bf26:backend/src/wiring/settings.py > 'tests/fixtures/fastro-boilerplate@e46bf26/backend/src/wiring/settings.py'
git -C ../FastAPI-boilerplate show e46bf26:backend/src/wiring/app.py > 'tests/fixtures/fastro-boilerplate@e46bf26/backend/src/wiring/app.py'
git -C ../FastAPI-boilerplate show e46bf26:backend/src/wiring/hooks.py > 'tests/fixtures/fastro-boilerplate@e46bf26/backend/src/wiring/hooks.py'
git -C ../FastAPI-boilerplate show e46bf26:backend/src/wiring/models.py > 'tests/fixtures/fastro-boilerplate@e46bf26/backend/src/wiring/models.py'
git -C ../FastAPI-boilerplate show e46bf26:backend/src/wiring/email.py > 'tests/fixtures/fastro-boilerplate@e46bf26/backend/src/wiring/email.py'
git -C ../FastAPI-boilerplate show e46bf26:backend/src/wiring/cache.py > 'tests/fixtures/fastro-boilerplate@e46bf26/backend/src/wiring/cache.py'
git -C ../FastAPI-boilerplate show e46bf26:backend/src/wiring/transports.py > 'tests/fixtures/fastro-boilerplate@e46bf26/backend/src/wiring/transports.py'
git -C ../FastAPI-boilerplate show e46bf26:backend/src/wiring/tasks.py > 'tests/fixtures/fastro-boilerplate@e46bf26/backend/src/wiring/tasks.py'
git -C ../FastAPI-boilerplate show e46bf26:backend/src/wiring/tables.py > 'tests/fixtures/fastro-boilerplate@e46bf26/backend/src/wiring/tables.py'
git -C ../FastAPI-boilerplate show e46bf26:backend/src/wiring/admin.py > 'tests/fixtures/fastro-boilerplate@e46bf26/backend/src/wiring/admin.py'
git -C ../FastAPI-boilerplate show e46bf26:backend/scripts/seeders.py > 'tests/fixtures/fastro-boilerplate@e46bf26/backend/scripts/seeders.py'
git -C ../FastAPI-boilerplate show e46bf26:backend/tests/wiring.py > 'tests/fixtures/fastro-boilerplate@e46bf26/backend/tests/wiring.py'
```

The wiring paths are the ones the catalog's `[[wiring]]` lists, in its order. Don't edit these
files to make the comparison pass: a difference is a finding to report.
