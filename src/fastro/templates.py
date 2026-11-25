"""The template context of spec 6.2, shared by an overlay's ``*.j2`` files and a generator's templates."""

from __future__ import annotations

import codecs
import sys
from collections.abc import Callable, Mapping
from typing import Any

from jinja2 import StrictUndefined
from jinja2.exceptions import SecurityError
from jinja2.runtime import Context
from jinja2.sandbox import ImmutableSandboxedEnvironment

from fastro.engine import conditions
from fastro.engine.blueprint import Blueprint
from fastro.engine.project import slug
from fastro.engine.resolve import Selection
from fastro.engine.spec import OPERATORS, SpecError

MOST_BYTES = 10**7
MOST_ITEMS = 10**7
MOST_DIGITS = 4300
DIGITS_BOUND: int = 10**MOST_DIGITS
DIGITS_BITS = DIGITS_BOUND.bit_length()
STRING = f"{{op!r}} would give a string over {MOST_BYTES // 10**6} MB, the most fastro builds in a template (6.2)."
ITEMS = f"{{op!r}} would give a list or tuple of more than {MOST_ITEMS:,} items, the most fastro builds in a template (6.2)."
LONG = f"{{op!r}} would give an integer of more than {MOST_DIGITS:,} digits, the most fastro writes out (6.2)."
OUTPUT = f"its output is over {MOST_BYTES // 10**6} MB, the most fastro writes from one template (6.2)."
UNENCODABLE = "its output holds a character UTF-8 can't encode, so it can't be written."


class _Bounded(ImmutableSandboxedEnvironment):
    """The sandbox of spec 6.2, with ``*``, ``+`` and ``**`` checked before they compute."""

    intercepted_binops = frozenset({"*", "+", "**"})

    def call_binop(self, context: Context, operator: str, left: Any, right: Any) -> Any:
        _bounded(operator, left, right)
        return super().call_binop(context, operator, left, right)


def _bounded(operator: str, left: object, right: object) -> None:
    """Refuse an operation whose result would pass 6.2's limits: 10 MB of a string's UTF-8 or 10^7 items of a list or
    tuple for ``*`` and ``+``, and more than 4,300 digits for an integer ``*`` or ``**``, decided exactly and without
    building a value past the limit."""
    if operator == "+":
        for kind in (str, list, tuple):
            if isinstance(left, kind) and isinstance(right, kind):
                _sized(operator, left, _size(left) + _size(right))
        return
    if not (isinstance(left, int) and isinstance(right, int)):
        if operator == "*":
            count, sized = (right, left) if isinstance(right, int) else (left, right)
            if isinstance(count, int) and isinstance(sized, (str, list, tuple)):
                _sized(operator, sized, _size(sized) * count)
        return
    if operator == "*" and left and right and _at_least(abs(left), abs(right)):
        raise SecurityError(LONG.format(op=operator))
    if operator == "**" and abs(left) >= 2 and _power_too_long(abs(left), right):
        raise SecurityError(LONG.format(op=operator))


def _size(value: str | list[Any] | tuple[Any, ...]) -> int:
    return len(value.encode("utf-8", "surrogatepass")) if isinstance(value, str) else len(value)


def _sized(operator: str, kind: object, size: int) -> None:
    if isinstance(kind, str) and size > MOST_BYTES:
        raise SecurityError(STRING.format(op=operator))
    if not isinstance(kind, str) and size > MOST_ITEMS:
        raise SecurityError(ITEMS.format(op=operator))


def _at_least(one: int, other: int) -> bool:
    """Whether ``one`` times ``other`` is ``10^4300`` or more, without computing a product past it."""
    bits = one.bit_length() + other.bit_length()
    if bits < DIGITS_BITS:
        return False
    if bits - 2 >= DIGITS_BITS:
        return True
    return one >= -(-DIGITS_BOUND // other)


def _power_too_long(base: int, exponent: int) -> bool:
    """Whether ``base`` to the ``exponent`` is ``10^4300`` or more, without computing a power past it."""
    bits = base.bit_length()
    if (bits - 1) * exponent >= DIGITS_BITS:
        return True
    if bits * exponent < DIGITS_BITS:
        return False
    power, count, most = 1, 0, (DIGITS_BOUND - 1) // base
    while count < exponent and power <= most:
        power *= base
        count += 1
    return count < exponent


ENVIRONMENT = _Bounded(undefined=StrictUndefined, keep_trailing_newline=True, autoescape=False)


class TemplateError(Exception):
    """A template that isn't UTF-8 or doesn't render. The message starts with the template's name."""


def render(name: str, data: bytes, values: Mapping[str, Any]) -> str:
    """The template ``name``, whose bytes are ``data``, read as UTF-8 past a byte order mark and rendered with
    ``values`` in Jinja's sandbox, as spec 6.2 says, so a template reaches only its context, changes none of it, and
    runs no code. Its output may come to 10 MB of UTF-8, counted as it's produced, and Python's integer digit limit
    is 4,300 while it renders, whatever the interpreter's own setting, which is restored afterwards.

    Raises:
        TemplateError: it isn't UTF-8 or doesn't render.
    """
    try:
        text = data.removeprefix(codecs.BOM_UTF8).decode("utf-8")
    except UnicodeDecodeError:
        raise TemplateError(f"{name} isn't UTF-8 text, so it can't be rendered.") from None
    pieces = []
    size = 0
    before = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(MOST_DIGITS)
    try:
        for piece in ENVIRONMENT.from_string(text).generate(values):
            try:
                size += len(piece.encode("utf-8"))
            except UnicodeEncodeError:
                raise TemplateError(f"{name} doesn't render: {UNENCODABLE}") from None
            if size > MOST_BYTES:
                raise TemplateError(f"{name} doesn't render: {OUTPUT}")
            pieces.append(piece)
    except TemplateError:
        raise
    except Exception as error:
        raise TemplateError(f"{name} doesn't render: {error}") from None
    finally:
        sys.set_int_max_str_digits(before)
    return "".join(pieces)


def context(
    catalog: Blueprint, selection: Selection, name: str, version: str, params: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The template context of spec 6.2 for ``catalog``'s ``selection``, a project named ``name``, fastro
    ``version`` and the generator ``params``."""
    services: set[str] = set()
    for feature in catalog.features:
        if feature.name not in selection.features:
            continue
        services.update(feature.services)
        for option in feature.options.values():
            services.update(option.services.get(selection.options[(feature.name, option.name)], ()))
        for when in feature.when:
            if selection.holds(when.condition):
                services.update(when.services)
    return {
        "project": {"name": name, "slug": slug(name)},
        "blueprint": {"name": catalog.info.name, "version": str(catalog.info.version)},
        "fastro": {"version": version},
        "features": list(selection.features),
        "options": {f"{feature}.{option}": value for (feature, option), value in selection.options.items()},
        "services": sorted(services),
        "params": dict(params or {}),
        "has": _has(catalog, selection),
    }


def _has(catalog: Blueprint, selection: Selection) -> Callable[[object], bool]:
    features = {feature.name: feature for feature in catalog.features}

    def has(text: object) -> bool:
        match = conditions.ATOM.fullmatch(text) if isinstance(text, str) else None
        if match is None or match[1] in OPERATORS:
            raise SpecError(f"has({text!r}): has takes one feature name or 'feature.option=value'.")
        name, option, value = match.groups()
        if name not in features:
            raise SpecError(f"has({text!r}): {name!r} isn't a feature of this blueprint.")
        if option is None:
            return selection.holds(conditions.Feature(name))
        options = features[name].options
        if option not in options:
            raise SpecError(f"has({text!r}): '{name}.{option}' isn't an option of {name}.")
        if value not in options[option].choices:
            raise SpecError(f"has({text!r}): {value!r} isn't a choice of {name}.{option}.")
        return selection.holds(conditions.Option(name, option, value))

    return has
