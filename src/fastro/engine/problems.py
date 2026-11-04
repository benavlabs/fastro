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


def key_name(path: KeyPath) -> str:
    """``path`` written the way TOML would: dotted, quoted where it must be, with array indexes in brackets."""
    name = ""
    for part in path:
        if isinstance(part, int):
            name += f"[{part}]"
        else:
            name += ("." if name else "") + (part if BARE_KEY.fullmatch(part) else json.dumps(part, ensure_ascii=False))
    return name


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

    def problem(self, path: KeyPath, message: str, fix: str = "", code: str = "") -> Problem:
        """A problem at ``path``; ``code`` is the lint code it's reported under when it isn't BP001 (spec 2.13)."""
        return Problem(self.file, self.line(path), key_name(path), message, fix, code)


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
