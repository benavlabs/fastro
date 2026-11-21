"""Problems with a catalog file, each at the line of the key it's about."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from fastro.engine.positions import BARE_KEY, KeyPath, key_lines

TOML_POSITION = re.compile(r" \(at (?:line (\d+), column \d+|end of document)\)$")
TOML_FIX = "Fix the TOML on that line, then run this again."


@dataclass(frozen=True, order=True)
class Problem:
    """One thing wrong with a catalog file: where it is, which key, what's wrong and how to fix it."""

    file: str
    line: int
    key: str
    message: str
    fix: str = ""
    code: str = field(default="", compare=False)

    def __str__(self) -> str:
        where = f"{self.file}:{self.line}: {self.key}: " if self.key else f"{self.file}:{self.line}: "
        return where + self.message + (f"\n  {self.fix}" if self.fix else "")


class CatalogError(Exception):
    """Every problem found in a catalog, in file and line order."""

    def __init__(self, problems: Iterable[Problem]) -> None:
        self.problems = tuple(sorted(problems))
        super().__init__("\n".join(map(str, self.problems)))


def key_name(path: KeyPath, data: object = None) -> str:
    """``path`` named as spec 1.11 says: dotted, quoted where TOML must quote it, with a position in a list of values
    in brackets, and an entry of one of 1.11's arrays of tables in ``data``, the parsed file, by what identifies it."""
    name = ""
    value = data
    for index, part in enumerate(path):
        if isinstance(part, int):
            name += _entry(path[:index], value, part)
            value = value[part] if isinstance(value, list) and 0 <= part < len(value) else None
        else:
            name += ("." if name else "") + (part if BARE_KEY.fullmatch(part) else json.dumps(part, ensure_ascii=False))
            value = value.get(part) if isinstance(value, dict) else None
    return name


def _identifier(within: KeyPath) -> str | None:
    """The key that identifies an entry of the array at ``within``, when it's one of 1.11's arrays of tables."""
    if within in (("wiring",), ("modules",), ("plugin",), ("starter",), ("verify", "checks"), ("feature", "when")):
        return "path" if within == ("wiring",) else "if" if within == ("feature", "when") else "name"
    if len(within) == 3 and within[0] == "wiring" and isinstance(within[1], int) and within[2] == "slots":
        return "name"
    if len(within) == 3 and within[0] == "features" and within[2] == "when":
        return "if"
    return None


def _entry(within: KeyPath, items: object, index: int) -> str:
    """How ``items[index]``, the array at ``within``, is named: by its identifying key when it's one of 1.11's arrays
    of tables and the key is a string no earlier entry has, by its place counting from 1 after ``#`` when it's such
    an entry otherwise, and by its position in brackets when it's a list of values."""
    key = _identifier(within)
    if key is None or not isinstance(items, list) or not 0 <= index < len(items) or not isinstance(items[index], dict):
        return f"[{index}]"
    found = items[index].get(key)
    raw = within[-1] == "slots" and items[index].get("kind") == "raw"
    earlier = [item.get(key) for item in items[:index] if isinstance(item, dict)]
    if raw or not isinstance(found, str) or found in earlier:
        return f"[#{index + 1}]"
    return f"[{json.dumps(found, ensure_ascii=False)}]"


@dataclass(frozen=True)
class Document:
    """A parsed catalog file, with the line each of its keys starts on."""

    file: str
    data: dict[str, Any]
    lines: dict[KeyPath, int]

    def line(self, path: KeyPath) -> int:
        """The line ``path`` starts on, or else that of the nearest enclosing key, or else 1."""
        for end in range(len(path), 0, -1):
            if path[:end] in self.lines:
                return self.lines[path[:end]]
        return 1

    def key(self, path: KeyPath) -> str:
        """How a message names the key at ``path`` in this file (spec 1.11)."""
        return key_name(path, self.data)

    def table(self, path: KeyPath) -> str:
        """How a message names the table at ``path``: in brackets, as a header, unless it's an entry of an array."""
        name = self.key(path)
        return name if path and isinstance(path[-1], int) else f"[{name}]"

    def problem(self, path: KeyPath, message: str, fix: str = "", code: str = "") -> Problem:
        """A problem at ``path``; ``code`` is the lint code it's reported under when it isn't BP001 (spec 2.13)."""
        return Problem(self.file, self.line(path), self.key(path), message, fix, code)


def read(file: str, text: str) -> Document:
    """``text`` parsed as the TOML file ``file``, or a :class:`CatalogError` at the line that breaks it."""
    try:
        return Document(file, tomllib.loads(text), key_lines(text))
    except tomllib.TOMLDecodeError as error:
        match = TOML_POSITION.search(str(error))
        message = str(error)[: match.start()] if match else str(error)
        line = int(match[1]) if match and match[1] else _last_line(text)
        raise CatalogError([Problem(file, line, "", f"isn't valid TOML: {message}.", TOML_FIX)]) from None
    except RecursionError:
        raise CatalogError([Problem(file, 1, "", "isn't valid TOML: it nests too deeply.", TOML_FIX)]) from None


def _last_line(text: str) -> int:
    lines = text.splitlines()
    return max((number for number, line in enumerate(lines, 1) if line.strip()), default=1)
