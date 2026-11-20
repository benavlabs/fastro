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
OPTION_PLACE = "'# @fastro option=' sits on its own line directly above a line that starts with VAR=."


class EnvFileError(ValueError):
    """A marker in ``.env.example`` that breaks spec 6.1, at the line ``number`` the message starts with, or an option
    ``env`` VAR without exactly one marked line, at its second marker when it has more than one."""

    def __init__(self, number: int | None, message: str) -> None:
        super().__init__(message if number is None else f"line {number}: {message}")
        self.number = number
        self.reason = message


def lines(text: str) -> list[str]:
    """``text`` split into lines at ``\\n`` only, so a form feed or another Unicode line break stays in its line (1.9)."""
    return text.split("\n")


def process(text: str, catalog: Blueprint, selection: Selection) -> str:
    """``text``, an ``.env.example`` decoded as UTF-8 with any byte order mark it started with still at its start,
    as a project with ``selection`` gets it (1.9, 6.1).

    Each section whose ``# @fastro if=`` condition the selection doesn't hold is dropped, up to the next header.
    Each line under a ``# @fastro option=`` marker for an option the selection has is rewritten from that VAR's
    template, keeping a trailing ``\\r``. Every marker line is removed. A byte order mark at the start is kept.

    Raises:
        EnvFileError: the first of :func:`problems`.
    """
    if found := problems(text, catalog):
        raise found[0]
    bom = BOM if text.startswith(BOM) else ""
    given = lines(text.removeprefix(bom))
    headers = _headers(given)
    dropped: set[int] = set()
    for index, line in enumerate(given):
        if (found_if := IF.fullmatch(line.rstrip())) is not None:
            start = index - 3
            if not selection.holds(_condition(catalog, index + 1, found_if.group(1))):
                end = next((header for header in headers if header > start), len(given) - (given[-1] == ""))
                dropped.update(range(start, end))
    out: list[str] = []
    for index, line in enumerate(given):
        if index in dropped:
            continue
        if (found_option := OPTION.fullmatch(line.rstrip())) is not None:
            given[index + 1] = _rewritten(catalog, selection, found_option.group(1), given[index + 1])
        elif IF.fullmatch(line.rstrip()) is None:
            out.append(line)
    return bom + "\n".join(out)


def problems(text: str, catalog: Blueprint) -> list[EnvFileError]:
    """Everything in ``text`` that 6.1 refuses whatever the selection: :func:`refused_lines`, then :func:`unmarked`."""
    return [*refused_lines(text, catalog), *unmarked(text, catalog)]


def refused_lines(text: str, catalog: Blueprint) -> list[EnvFileError]:
    """Each marker line of ``text`` that 6.1 refuses, in file order: one that's neither marker, an ``if`` marker that
    isn't directly below a header or whose condition doesn't parse or names a feature, option or choice the catalog
    doesn't have, and an ``option`` marker naming an option the catalog doesn't have, not directly above a ``VAR=``
    line, or above a VAR its option's ``env`` doesn't have."""
    given = lines(text.removeprefix(BOM))
    headers = set(_headers(given))
    options = {f"{feature.name}.{name}": option for feature in catalog.features for name, option in feature.options.items()}
    found = []
    for index, line in enumerate(given):
        line = line.rstrip()
        number = index + 1
        try:
            if (found_if := IF.fullmatch(line)) is not None:
                if index - 3 not in headers:
                    raise EnvFileError(number, IF_PLACE)
                _condition(catalog, number, found_if.group(1))
            elif (found_option := OPTION.fullmatch(line)) is not None:
                key = found_option.group(1)
                if key not in options:
                    raise EnvFileError(number, f"{key!r} isn't an option of this catalog.")
                assigned = ASSIGNED.match(given[index + 1]) if index + 1 < len(given) else None
                if assigned is None:
                    raise EnvFileError(number, OPTION_PLACE)
                if assigned.group(1) not in options[key].env:
                    raise EnvFileError(number, f"{assigned.group(1)} isn't in the env table of {key}.")
            elif ANY_MARKER.fullmatch(line) is not None:
                raise EnvFileError(number, f"{line!r} isn't a marker fastro knows: use '# @fastro if=' or '# @fastro option='.")
        except EnvFileError as error:
            found.append(error)
    return found


def unmarked(text: str, catalog: Blueprint) -> list[EnvFileError]:
    """Each option ``env`` VAR of the catalog, selected or not, without exactly one marked line in ``text`` (6.1)."""
    counted = marked(text.removeprefix(BOM))
    found = []
    for feature in catalog.features:
        for name, option in feature.options.items():
            key = f"{feature.name}.{name}"
            marker = f"'# @fastro option={key}'"
            for var in option.env:
                numbers = counted.get((key, var), [])
                if not numbers:
                    found.append(EnvFileError(None, f"{var} has no {marker} marker directly above a {var}= line."))
                elif len(numbers) > 1:
                    listed = ", ".join(map(str, numbers[:-1])) + f" and {numbers[-1]}"
                    message = f"{var} has {len(numbers)} {marker} markers, on lines {listed}, and needs one."
                    found.append(EnvFileError(numbers[1], message))
    return found


def marked(text: str) -> dict[tuple[str, str], list[int]]:
    """Each ``(feature.option, VAR)`` an ``option`` marker line in ``text`` names, with the ``VAR=`` line under it, and
    the markers' line numbers (6.1, 2.13)."""
    given = lines(text)
    found: dict[tuple[str, str], list[int]] = {}
    for number, (line, below) in enumerate(zip(given, given[1:], strict=False), start=1):
        marker, assigned = OPTION.fullmatch(line.rstrip()), ASSIGNED.match(below)
        if marker is not None and assigned is not None:
            found.setdefault((marker.group(1), assigned.group(1)), []).append(number)
    return found


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
    """``line``, which assigns a VAR of option ``key``'s ``env``, rewritten from its template keeping a trailing
    ``\\r``, or as it was when ``key`` isn't an option the selection has."""
    feature, _, option = key.partition(".")
    value = selection.options.get((feature, option))
    assigned = ASSIGNED.match(line)
    if value is None or assigned is None:
        return line
    var = assigned.group(1)
    owner = next(candidate for candidate in catalog.features if candidate.name == feature)
    ending = "\r" if line.endswith("\r") else ""
    return f"{var}={owner.options[option].env[var].replace('{value}', value)}{ending}"
