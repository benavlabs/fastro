"""The line every key, table and array element of a TOML document starts on, which ``tomllib`` doesn't keep."""

from __future__ import annotations

import bisect
import re
import tomllib

KeyPath = tuple[str | int, ...]

BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
SCALAR_END = re.compile(r"[,\]}#\r\n]")


def key_lines(text: str) -> dict[KeyPath, int]:
    """The line each key path of ``text`` starts on, or ``tomllib``'s error if ``text`` isn't TOML.

    Array elements are keyed by their index, and so are the tables of an array of tables.
    """
    tomllib.loads(text)
    return _Scanner(text).document()


class _Scanner:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.newlines = [index for index, char in enumerate(text) if char == "\n"]
        self.lines: dict[KeyPath, int] = {}
        self.table_arrays: dict[KeyPath, int] = {}

    def line(self) -> int:
        return bisect.bisect_left(self.newlines, self.pos) + 1

    def at(self, chars: str) -> bool:
        return self.text.startswith(chars, self.pos)

    def record(self, path: KeyPath, line: int) -> None:
        for end in range(1, len(path)):
            self.lines.setdefault(path[:end], line)
        self.lines[path] = line

    def blank(self) -> None:
        while self.pos < len(self.text):
            if self.text[self.pos] in " \t\r\n":
                self.pos += 1
            elif self.at("#"):
                end = self.text.find("\n", self.pos)
                self.pos = len(self.text) if end == -1 else end
            else:
                return

    def document(self) -> dict[KeyPath, int]:
        table: KeyPath = ()
        self.blank()
        while self.pos < len(self.text):
            line = self.line()
            if self.at("[["):
                self.pos += 2
                keys = self.key()
                self.pos += 2
                parent = self.resolve(keys[:-1])
                array = (*parent, keys[-1])
                index = self.table_arrays.get(array, 0)
                self.table_arrays[array] = index + 1
                table = (*array, index)
                self.record(table, line)
            elif self.at("["):
                self.pos += 1
                table = self.resolve(self.key())
                self.pos += 1
                self.record(table, line)
            else:
                self.pair(table)
            self.blank()
        return self.lines

    def resolve(self, keys: tuple[str, ...]) -> KeyPath:
        path: KeyPath = ()
        for key in keys:
            path = (*path, key)
            if path in self.table_arrays:
                path = (*path, self.table_arrays[path] - 1)
        return path

    def pair(self, table: KeyPath) -> None:
        line = self.line()
        path = (*table, *self.key())
        self.pos += 1
        self.record(path, line)
        self.blank()
        self.value(path)

    def key(self) -> tuple[str, ...]:
        keys = []
        while True:
            self.blank()
            if self.at('"') or self.at("'"):
                start = self.pos
                self.string()
                keys.append(tomllib.loads(f"k = {self.text[start : self.pos]}")["k"])
            else:
                match = BARE_KEY.match(self.text, self.pos)
                assert match is not None
                keys.append(match[0])
                self.pos = match.end()
            self.blank()
            if not self.at("."):
                return tuple(keys)
            self.pos += 1

    def value(self, path: KeyPath) -> None:
        if self.at("["):
            self.pos += 1
            index = 0
            self.blank()
            while not self.at("]"):
                self.record((*path, index), self.line())
                self.value((*path, index))
                index += 1
                self.blank()
                if self.at(","):
                    self.pos += 1
                    self.blank()
            self.pos += 1
        elif self.at("{"):
            self.pos += 1
            self.blank()
            while not self.at("}"):
                self.pair(path)
                self.blank()
                if self.at(","):
                    self.pos += 1
                    self.blank()
            self.pos += 1
        elif self.at('"') or self.at("'"):
            self.string()
        else:
            match = SCALAR_END.search(self.text, self.pos)
            self.pos = len(self.text) if match is None else match.start()

    def string(self) -> None:
        quote = self.text[self.pos]
        triple = quote * 3
        multiline = self.at(triple)
        self.pos += 3 if multiline else 1
        while not self.at(triple if multiline else quote):
            self.pos += 2 if quote == '"' and self.at("\\") else 1
        self.pos += 3
        if not multiline:
            self.pos -= 2
            return
        for _ in range(2):
            if self.at(quote):
                self.pos += 1
