"""``{python_root}/.env.example`` processed by its ``# @fastro`` markers for a selection (spec 1.9, 6.1)."""

from __future__ import annotations

import re

from fastro.engine import conditions
from fastro.engine.blueprint import Blueprint
from fastro.engine.resolve import Selection
from fastro.engine.spec import SpecError

BOM = "\ufeff"
RULE = re.compile(r"# =+")
TITLE = re.compile(r"#.*")
IF = re.compile(r"# @fastro if=(.*)")
OPTION = re.compile(r"# @fastro option=(\S+)")
ANY_MARKER = re.compile(r"# @fastro\b.*")
ASSIGNED = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=")
IF_PLACE = "'# @fastro if=' sits on its own line directly below a section header, a block of '# ===' lines and a title."


class EnvFileError(ValueError):
    """A marker in ``.env.example`` that breaks spec 6.1, at the line the message starts with."""

    def __init__(self, number: int, message: str) -> None:
        super().__init__(f"line {number}: {message}")


def lines(text: str) -> list[str]:
    """``text`` split into lines at ``\\n`` only, so a form feed or another Unicode line break stays in its line (1.9)."""
    return text.split("\n")


def process(text: str, catalog: Blueprint, selection: Selection) -> str:
    """``text``, an ``.env.example`` decoded as UTF-8 with any byte order mark it started with still at its start,
    as a project with ``selection`` gets it (1.9, 6.1).

    Each section whose ``# @fastro if=`` condition the selection doesn't hold is dropped, up to the next header.
    Each line under a ``# @fastro option=`` marker for an option the selection has and one of its ``env`` VARs is
    rewritten from that VAR's template. Every marker line is removed. A byte order mark at the start is kept.

    Raises:
        EnvFileError: an ``if`` marker isn't directly below a header, or its condition doesn't parse or names a
            feature, option or choice the catalog doesn't have; or a ``# @fastro`` line is neither marker.
    """
    bom = BOM if text.startswith(BOM) else ""
    given = lines(text.removeprefix(bom))
    headers = _headers(given)
    dropped: set[int] = set()
    for index, line in enumerate(given):
        line = line.rstrip()
        if (found := IF.fullmatch(line)) is not None:
            start = index - 3
            if start not in headers:
                raise EnvFileError(index + 1, IF_PLACE)
            if not selection.holds(_condition(catalog, index + 1, found.group(1))):
                end = next((header for header in headers if header > start), len(given) - (given[-1] == ""))
                dropped.update(range(start, end))
        elif OPTION.fullmatch(line) is None and ANY_MARKER.fullmatch(line) is not None:
            message = f"{line!r} isn't a marker fastro knows: use '# @fastro if=' or '# @fastro option='."
            raise EnvFileError(index + 1, message)
    out: list[str] = []
    for index, line in enumerate(given):
        if index in dropped:
            continue
        if (found := OPTION.fullmatch(line.rstrip())) is not None:
            if index + 1 < len(given):
                given[index + 1] = _rewritten(catalog, selection, found.group(1), given[index + 1])
        elif IF.fullmatch(line.rstrip()) is None:
            out.append(line)
    return bom + "\n".join(out)


def _headers(given: list[str]) -> list[int]:
    """The index of each header's first line: a ``# ===`` rule, a title, then another rule."""
    return [
        index
        for index in range(len(given) - 2)
        if RULE.fullmatch(given[index].rstrip())
        and TITLE.fullmatch(given[index + 1].rstrip())
        and not RULE.fullmatch(given[index + 1].rstrip())
        and not ANY_MARKER.fullmatch(given[index + 1].rstrip())
        and RULE.fullmatch(given[index + 2].rstrip())
    ]


def _condition(catalog: Blueprint, number: int, text: str) -> conditions.Condition:
    """``text`` as a condition whose every name the catalog has (1.3)."""
    try:
        condition = conditions.parse(text)
    except SpecError as error:
        raise EnvFileError(number, str(error)) from None
    features = {feature.name: feature for feature in catalog.features}
    for atom in conditions.atoms(condition):
        name = atom.feature if isinstance(atom, conditions.Option) else atom.name
        if name not in features:
            raise EnvFileError(number, f"{name!r} isn't a feature of this catalog.")
        if isinstance(atom, conditions.Option):
            options = features[name].options
            if atom.option not in options:
                raise EnvFileError(number, f"'{name}.{atom.option}' isn't an option of {name}.")
            if atom.value not in options[atom.option].choices:
                raise EnvFileError(number, f"{atom.value!r} isn't a choice of {name}.{atom.option}.")
    return condition


def _rewritten(catalog: Blueprint, selection: Selection, key: str, line: str) -> str:
    """``line`` rewritten from the ``env`` template of option ``key`` for its VAR, or as it was when ``key`` isn't an
    option the selection has or ``line`` doesn't assign one of its VARs."""
    feature, _, option = key.partition(".")
    value = selection.options.get((feature, option))
    assigned = ASSIGNED.match(line)
    if value is None or assigned is None:
        return line
    owner = next(candidate for candidate in catalog.features if candidate.name == feature)
    template = owner.options[option].env.get(assigned.group(1))
    return line if template is None else f"{assigned.group(1)}={template.replace('{value}', value)}"
