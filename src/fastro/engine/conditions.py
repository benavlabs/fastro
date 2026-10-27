"""The condition language behind every ``if = "…"`` (spec 1.3)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from fastro.engine.spec import CHOICE, NAME, OPERATORS, SpecError

TOKEN = re.compile(r"\s*(?:([()])|([^\s()]+))")
ATOM = re.compile(rf"({NAME.pattern})(?:\.({NAME.pattern})=({CHOICE.pattern}))?")
MAX_DEPTH = 100
QUOTED = 60


@dataclass(frozen=True)
class Feature:
    """True when the feature is selected."""

    name: str


@dataclass(frozen=True)
class Option:
    """True when the feature is selected and its option equals ``value``."""

    feature: str
    option: str
    value: str


@dataclass(frozen=True)
class Not:
    operand: Condition


@dataclass(frozen=True)
class And:
    operands: tuple[Condition, ...]


@dataclass(frozen=True)
class Or:
    operands: tuple[Condition, ...]


Condition = Feature | Option | Not | And | Or


class _Parser:
    def __init__(self, text: str) -> None:
        self.tokens = [(match.start(match.lastindex or 0) + 1, match[0].strip()) for match in TOKEN.finditer(text)]
        self.index = 0
        self.depth = 0
        self.quoted = repr(text) if len(text) <= QUOTED else f"{text[:QUOTED]!r}..."

    def peek(self) -> str | None:
        return self.tokens[self.index][1] if self.index < len(self.tokens) else None

    def refuse(self, expected: str) -> SpecError:
        if self.index < len(self.tokens):
            column, token = self.tokens[self.index]
            return SpecError(f"{self.quoted} isn't a condition: expected {expected} at column {column}, found {token!r}.")
        return SpecError(f"{self.quoted} isn't a condition: expected {expected} at the end.")

    def condition(self) -> Condition:
        result = self.either()
        if self.peek() is not None:
            raise self.refuse("'and', 'or' or the end")
        return result

    def either(self) -> Condition:
        operands = [self.both()]
        while self.peek() == "or":
            self.index += 1
            operands.append(self.both())
        return operands[0] if len(operands) == 1 else Or(tuple(operands))

    def both(self) -> Condition:
        operands = [self.negation()]
        while self.peek() == "and":
            self.index += 1
            operands.append(self.negation())
        return operands[0] if len(operands) == 1 else And(tuple(operands))

    def negation(self) -> Condition:
        token = self.peek()
        if token in ("not", "("):
            self.depth += 1
            if self.depth > MAX_DEPTH:
                column = self.tokens[self.index][0]
                raise SpecError(f"{self.quoted} isn't a condition: it nests deeper than {MAX_DEPTH} levels at column {column}.")
            self.index += 1
            inner = Not(self.negation()) if token == "not" else self.group()
            self.depth -= 1
            return inner
        if token is None or token == ")" or token in OPERATORS:
            raise self.refuse("a feature name, 'not' or '('")
        return self.atom(token)

    def group(self) -> Condition:
        inner = self.either()
        if self.peek() != ")":
            raise self.refuse("')'")
        self.index += 1
        return inner

    def atom(self, token: str) -> Condition:
        match = ATOM.fullmatch(token)
        if match is None or match[1] in OPERATORS:
            column = self.tokens[self.index][0]
            raise SpecError(
                f"{self.quoted} isn't a condition: {token!r} at column {column} isn't a feature name or 'feature.option=value'."
            )
        self.index += 1
        feature, option, value = match.groups()
        return Feature(feature) if option is None else Option(feature, option, value)


def parse(text: str) -> Condition:
    """``text`` as a condition tree, or a :class:`SpecError` that says where it breaks the grammar."""
    return _Parser(text).condition()


def atoms(condition: Condition) -> list[Feature | Option]:
    """Every feature and option the condition mentions, left to right, repeats included."""
    if isinstance(condition, Feature | Option):
        return [condition]
    if isinstance(condition, Not):
        return atoms(condition.operand)
    return [atom for operand in condition.operands for atom in atoms(operand)]
