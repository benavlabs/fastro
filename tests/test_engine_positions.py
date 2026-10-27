"""The line every key, table and array element of a TOML document starts on."""

import tomllib
from typing import Any

import pytest

from fastro.engine.positions import KeyPath, key_lines

MARKED = '''\
# every integer below is the number of the line it sits on
title = 2
"quoted\\u0020key" = 3
'literal.key' = 4
dotted.inner.leaf = 5
spaced . out = 6

[ features . accounts ]
summary = """
not_a_key = 99
[not_a_table]
"""
requires = 13
paths = [
  15,
  # a comment, then a trailing comma
  17,
]
env = { A = 19, "B.C" = 19, nested = { deep = 19 } }
dates = [1979-05-27 07:32:00Z, 20]
literal = 'a # inside a literal'
basic = "a \\" # inside a basic string"
trick = """ends with quotes"""""
literals = \'\'\'
x = 99
\'\'\'
after = 27

[[features.taskiq.when]]
if = "accounts"
order = 31

[[features.taskiq.when]]
order = 34
contributes = [{ ref = "a:b", order = 35 }, { ref = "c:d", order = 35 },
  { order = 36 }]

[[fruits]]
name = 39

[[fruits.varieties]]
weight = 42

[[fruits.varieties]]
weight = 45

[[fruits]]
name = 48

[[fruits.varieties]]
weight = 51

[options]
"taskiq.broker" = 54

[later]
child.value = 57

[implicit.parent.child]
value = 60

[implicit.parent]
own = 63
'''


def _integers(value: Any, path: KeyPath = ()) -> list[tuple[KeyPath, int]]:
    if isinstance(value, dict):
        return [found for key, item in value.items() for found in _integers(item, (*path, key))]
    if isinstance(value, list):
        return [found for index, item in enumerate(value) for found in _integers(item, (*path, index))]
    return [(path, value)] if isinstance(value, int) else []


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_every_key_and_element_maps_to_the_line_it_starts_on(newline: str) -> None:
    text = MARKED.replace("\n", newline)
    expected = _integers(tomllib.loads(text))
    lines = key_lines(text)

    assert len(expected) == 27
    assert [(path, lines.get(path)) for path, _ in expected] == expected


@pytest.mark.parametrize(
    ("path", "line"),
    [
        (("features",), 8),
        (("features", "accounts"), 8),
        (("features", "accounts", "summary"), 9),
        (("features", "accounts", "paths"), 14),
        (("features", "accounts", "env"), 19),
        (("features", "accounts", "env", "nested"), 19),
        (("features", "accounts", "literals"), 24),
        (("features", "taskiq", "when"), 29),
        (("features", "taskiq", "when", 0), 29),
        (("features", "taskiq", "when", 0, "if"), 30),
        (("features", "taskiq", "when", 1), 33),
        (("features", "taskiq", "when", 1, "contributes", 2), 36),
        (("fruits", 1, "varieties", 0), 50),
        (("implicit",), 59),
        (("implicit", "parent"), 62),
        (("implicit", "parent", "child"), 59),
    ],
)
def test_tables_and_arrays_of_tables_map_to_their_header(path: KeyPath, line: int) -> None:
    assert key_lines(MARKED)[path] == line


def test_text_inside_strings_and_comments_is_never_taken_for_a_key() -> None:
    lines = key_lines(MARKED)

    assert [path for path in lines if "not_a_key" in path or "not_a_table" in path or "x" in path] == []


@pytest.mark.parametrize(
    ("text", "lines"),
    [
        ("a = 1", {("a",): 1}),
        ("a = 1 # last", {("a",): 1}),
        ("a = [\n  1] # last", {("a",): 1, ("a", 0): 2}),
        ('a = "x\\" = 1"\nb = 2', {("a",): 1, ("b",): 2}),
        ("a = 'x\\'\nb = 2", {("a",): 1, ("b",): 2}),
        ("a = [\n  1 # one, two\n  , 2]", {("a",): 1, ("a", 0): 2, ("a", 1): 3}),
    ],
)
def test_strings_comments_and_the_end_of_text_end_where_toml_says(text: str, lines: dict[KeyPath, int]) -> None:
    assert key_lines(text) == lines


@pytest.mark.parametrize("text", ["a = [1,", 'a = "x', "a = { b = 1", "[a"])
def test_text_that_isnt_toml_is_refused_rather_than_scanned(text: str) -> None:
    with pytest.raises(tomllib.TOMLDecodeError):
        key_lines(text)
