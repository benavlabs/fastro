"""The names a module binds at module level, as FR014 reads them, with the import each comes from (spec 2.13)."""

import ast

from fastro.engine.bindings import STAR, bindings


def _bound(code: str) -> set[tuple[str, object]]:
    return set(bindings(ast.parse(code)))


def test_definitions_assignments_and_imports_bind_names_with_where_imports_come_from() -> None:
    code = (
        "def f(): pass\nasync def g(): pass\nclass C: pass\na = b = 1\n(c, [d, *e]) = x\nh: int\ni += 1\nj.k = 1\n"
        "import os.path\nimport sys as s\nfrom ..m import n, o as p\n"
    )

    assert _bound(code) == {
        *((name, None) for name in "fgCabcdehi"),
        ("os", ("import", "os.path")),
        ("s", ("import", "sys")),
        ("n", ("from", 2, "m", "n")),
        ("p", ("from", 2, "m", "o")),
    }


def test_blocks_at_module_level_count_to_any_depth_and_nothing_inside_a_def_does() -> None:
    code = (
        "if x:\n    a = 1\nelif y:\n    b = 1\nelse:\n    with z:\n        c = 1\n"
        "try:\n    d = 1\nexcept E:\n    e = 1\nelse:\n    f = 1\nfinally:\n    g = 1\n"
        "try:\n    h = 1\nexcept* E:\n    i = 1\nasync def k():\n    inner = 1\nfor loop in x:\n    j = 1\n"
    )

    assert {name for name, _ in _bound(code)} == set("abcdefghik")


def test_a_star_import_gives_star() -> None:
    assert _bound("from m import *\n") == {(STAR, ("from", 0, "m", "*"))}


def test_async_with_counts() -> None:
    assert {name for name, _ in bindings(ast.parse("async with x:\n    a = 1\n", feature_version=(3, 11)))} == {"a"}
