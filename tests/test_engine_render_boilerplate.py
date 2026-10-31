"""A3.7: track B's catalog at e46bf26, with every feature selected, renders B's committed wiring byte for byte (2.8).

The catalog and the wiring it declares are copied in ``fixtures/fastro-boilerplate@e46bf26/`` (see its README).
"""

from pathlib import Path

import pytest
from packaging.version import Version

from fastro.engine import blueprint
from fastro.engine.plan import plan
from fastro.engine.render import render
from fastro.engine.resolve import select

BOILERPLATE = Path(__file__).parent / "fixtures" / "fastro-boilerplate@e46bf26"
CATALOG = blueprint.load("blueprint.toml", (BOILERPLATE / "blueprint.toml").read_text(), Version("0.1.0"))
EVERYTHING = render(plan(CATALOG, select(CATALOG, [feature.name for feature in CATALOG.features]), ()))
WIRING = [file.path.text for file in CATALOG.wiring]


def test_every_feature_renders_every_wiring_file_the_catalog_declares() -> None:
    assert sorted(EVERYTHING) == sorted(WIRING)


def test_the_fixture_holds_every_wiring_file_and_nothing_else() -> None:
    copied = sorted(str(path.relative_to(BOILERPLATE)) for path in BOILERPLATE.rglob("*") if path.is_file())

    assert copied == sorted([*WIRING, "README.md", "blueprint.toml"])


@pytest.mark.parametrize("path", WIRING)
def test_each_wiring_file_renders_as_b_committed_it(path: str) -> None:
    assert EVERYTHING[path] == (BOILERPLATE / path).read_text()
