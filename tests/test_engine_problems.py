"""Problems name the file, the line and the key, and a catalog error carries all of them."""

import re

import pytest

from fastro.engine.positions import KeyPath
from fastro.engine.problems import CatalogError, Problem, key_name, read

BLUEPRINT = """\
schema = 1

[features.taskiq]
requires = ["accounts"]

[[features.taskiq.when]]
if = "accounts"
paths = ["a.py", "b.py"]

[options]
"taskiq.broker" = "redis"
"""


@pytest.mark.parametrize(
    ("path", "name"),
    [
        (("schema",), "schema"),
        (("features", "taskiq", "when", 0, "if"), "features.taskiq.when[0].if"),
        (("features", "taskiq", "when", 0, "paths", 1), "features.taskiq.when[0].paths[1]"),
        (("options", "taskiq.broker"), 'options."taskiq.broker"'),
        (("env", 'say "hi"'), 'env."say \\"hi\\""'),
        (("my-key", "with space"), 'my-key."with space"'),
        ((), ""),
    ],
)
def test_a_key_is_named_as_toml_would_write_it(path: KeyPath, name: str) -> None:
    assert key_name(path) == name


@pytest.mark.parametrize(
    ("path", "line", "name"),
    [
        (("schema",), 1, "schema"),
        (("features", "taskiq"), 3, "features.taskiq"),
        (("features", "taskiq", "requires", 0), 4, "features.taskiq.requires[0]"),
        (("features", "taskiq", "when", 0, "if"), 7, 'features.taskiq.when["accounts"].if'),
        (("features", "taskiq", "when", 0, "paths", 1), 8, 'features.taskiq.when["accounts"].paths[1]'),
        (("features", "taskiq", "summary"), 3, "features.taskiq.summary"),
        (("features", "taskiq", "when", 0, "contributes"), 6, 'features.taskiq.when["accounts"].contributes'),
        (("options", "taskiq.broker"), 11, 'options."taskiq.broker"'),
        (("presets",), 1, "presets"),
    ],
)
def test_a_problem_sits_on_its_keys_line_or_else_the_nearest_enclosing_one(path: KeyPath, line: int, name: str) -> None:
    problem = read("blueprint.toml", BLUEPRINT).problem(path, "is wrong.")

    assert problem == Problem("blueprint.toml", line, name, "is wrong.")


WIRING = """\
[[wiring]]
path = "backend/src/wiring/app.py"
[[wiring.slots]]
kind = "raw"
[[wiring.slots]]
name = "ROUTERS"
[[wiring.slots]]
name = "ROUTERS"
[[wiring.slots]]
name = 3
[[wiring]]
path = "backend/src/wiring/app.py"
[[wiring]]
doc = "no path"
[features.a]
contributes = { routers = [{ ref = "x:y" }], when = [{ ref = "x:y", if = "b" }], slots = [{ name = "q" }] }
[[features.a.when]]
if = "b"
[[feature.when]]
if = "c"
[[modules]]
name = "billing"
[[other]]
slots = [{ name = "q" }]
[[verify.checks]]
name = "smoke \\"q\\""
"""


@pytest.mark.parametrize(
    ("path", "name"),
    [
        (("wiring", 0, "path"), 'wiring["backend/src/wiring/app.py"].path'),
        (("wiring", 0, "slots", 0, "code"), 'wiring["backend/src/wiring/app.py"].slots[#1].code'),
        (("wiring", 0, "slots", 1), 'wiring["backend/src/wiring/app.py"].slots["ROUTERS"]'),
        (("wiring", 0, "slots", 2), 'wiring["backend/src/wiring/app.py"].slots[#3]'),
        (("wiring", 0, "slots", 3), 'wiring["backend/src/wiring/app.py"].slots[#4]'),
        (("wiring", 0, "slots", 9), 'wiring["backend/src/wiring/app.py"].slots[9]'),
        (("wiring", 1, "path"), "wiring[#2].path"),
        (("wiring", 2), "wiring[#3]"),
        (("features", "a", "contributes", "routers", 0, "ref"), "features.a.contributes.routers[0].ref"),
        (("features", "a", "when", 0, "if"), 'features.a.when["b"].if'),
        (("features", "a", "contributes", "when", 0, "if"), "features.a.contributes.when[0].if"),
        (("features", "a", "contributes", "slots", 0, "name"), "features.a.contributes.slots[0].name"),
        (("feature", "when", 0), 'feature.when["c"]'),
        (("modules", 0, "name"), 'modules["billing"].name'),
        (("other", 0, "slots", 0), "other[0].slots[0]"),
        (("verify", "checks", 0, "run"), 'verify.checks["smoke \\"q\\""].run'),
        (("presets", 0), "presets[0]"),
    ],
)
def test_an_entry_of_an_array_of_tables_is_named_by_what_identifies_it(path: KeyPath, name: str) -> None:
    assert read("blueprint.toml", WIRING).key(path) == name


def test_a_table_is_named_in_brackets_and_an_entry_of_an_array_without() -> None:
    document = read("blueprint.toml", WIRING)

    assert (document.table(("features", "a")), document.table(("wiring", 0))) == (
        "[features.a]",
        'wiring["backend/src/wiring/app.py"]',
    )


def test_read_returns_the_parsed_document() -> None:
    document = read("blueprint.toml", BLUEPRINT)

    assert document.file == "blueprint.toml"
    assert document.data["features"]["taskiq"]["when"][0]["paths"] == ["a.py", "b.py"]


def test_a_problem_reads_as_file_line_key_message_and_then_its_fix() -> None:
    problem = Problem("blueprint.toml", 4, "features.taskiq.require", "isn't a key here.", "Did you mean 'requires'?")

    assert str(problem) == "blueprint.toml:4: features.taskiq.require: isn't a key here.\n  Did you mean 'requires'?"
    assert str(Problem("blueprint.toml", 1, "", "is empty.")) == "blueprint.toml:1: is empty."


def test_a_catalog_error_holds_its_problems_in_file_and_line_order() -> None:
    late = Problem("blueprint.toml", 9, "b", "is wrong.")
    early = Problem("blueprint.toml", 2, "a", "is wrong.")
    other = Problem("alpha.toml", 5, "c", "is wrong.")

    error = CatalogError([late, early, other])

    assert error.problems == (other, early, late)
    assert str(error) == "\n".join(map(str, [other, early, late]))


@pytest.mark.parametrize(
    ("text", "line", "message"),
    [
        ("schema = 1\n[a]\nx = 1\n[a]\ny = 2\n", 4, "Cannot declare ('a',) twice"),
        ("schema = 1\n\nb c = 2\nd = 3\n", 3, "Expected '=' after a key in a key/value pair"),
        ("schema = 1\nname = ", 2, "Invalid value"),
        ("schema = 1\nname = 'x\n\n", 2, 'Expected "\'"'),
    ],
)
def test_text_that_isnt_toml_is_one_problem_at_the_line_tomllib_names(text: str, line: int, message: str) -> None:
    with pytest.raises(CatalogError) as caught:
        read("blueprint.toml", text)

    assert caught.value.problems == (
        Problem("blueprint.toml", line, "", f"isn't valid TOML: {message}.", "Fix the TOML on that line, then run this again."),
    )


def test_toml_nested_too_deeply_for_the_parser_is_a_problem_not_a_crash() -> None:
    with pytest.raises(CatalogError, match=re.escape("blueprint.toml:1: isn't valid TOML: it nests too deeply.")):
        read("blueprint.toml", "a = " + "[" * 100_000 + "]" * 100_000)
