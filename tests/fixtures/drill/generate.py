"""Write the old removal drill's wiring for each of its sets into this directory, as its builders return it.

Takes the path of a copy of track B's ``tools/removal_drill.py`` and calls only its ``_wiring_*``, ``_seeders`` and
``_tests_wiring`` builders, never ``build()``. Each set gets ``<set>/`` with the files ``build()`` would write.
``sets.json`` records each set's closed selection, and ``requires.json`` the drill's ``REQUIRES``.
"""

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
FILES = (
    ("backend/src/wiring/settings.py", "_wiring_settings"),
    ("backend/src/wiring/app.py", "_wiring_app"),
    ("backend/src/wiring/hooks.py", "_wiring_hooks"),
    ("backend/src/wiring/email.py", "_wiring_email"),
    ("backend/src/wiring/tasks.py", "_wiring_tasks"),
    ("backend/src/wiring/tables.py", "_wiring_tables"),
    ("backend/src/wiring/transports.py", "_wiring_transports"),
    ("backend/src/wiring/cache.py", "_wiring_cache"),
    ("backend/src/wiring/models.py", "_wiring_models"),
    ("backend/src/wiring/admin.py", "_wiring_admin"),
    ("backend/scripts/seeders.py", "_seeders"),
    ("backend/tests/wiring.py", "_tests_wiring"),
)


def _drill(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("removal_drill", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(drill_path: str) -> None:
    """Write every set's wiring, ``sets.json`` and ``requires.json``, from the drill at ``drill_path``."""
    drill = _drill(Path(drill_path))
    sets = {}
    for name in drill.PRESETS:
        chosen = drill._selected(name)
        sets[name] = sorted(chosen)
        for path, builder in FILES:
            if path.endswith("/admin.py") and "admin" not in chosen:
                continue
            target = HERE / name / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(getattr(drill, builder)(chosen))
    (HERE / "sets.json").write_text(json.dumps(sets, indent=2) + "\n")
    requires = {name: sorted(drill.REQUIRES.get(name, ())) for name in drill.FEATURES}
    (HERE / "requires.json").write_text(json.dumps(requires, indent=2) + "\n")


if __name__ == "__main__":
    main(sys.argv[1])
