"""The template context of spec 6.2, shared by an overlay's ``*.j2`` files and a generator's templates."""

from __future__ import annotations

import codecs
from collections.abc import Callable, Mapping
from typing import Any

from jinja2 import Environment, StrictUndefined

from fastro.engine import conditions
from fastro.engine.blueprint import Blueprint
from fastro.engine.project import slug
from fastro.engine.resolve import Selection
from fastro.engine.spec import OPERATORS, SpecError

ENVIRONMENT = Environment(undefined=StrictUndefined, keep_trailing_newline=True, autoescape=False)


class TemplateError(Exception):
    """A template that isn't UTF-8 or doesn't render. The message starts with the template's name."""


def render(name: str, data: bytes, values: Mapping[str, Any]) -> str:
    """The template ``name``, whose bytes are ``data``, read as UTF-8 past a byte order mark and rendered with
    ``values`` as spec 6.2 says.

    Raises:
        TemplateError: it isn't UTF-8 or doesn't render.
    """
    try:
        text = data.removeprefix(codecs.BOM_UTF8).decode("utf-8")
    except UnicodeDecodeError:
        raise TemplateError(f"{name} isn't UTF-8 text, so it can't be rendered.") from None
    try:
        return ENVIRONMENT.from_string(text).render(values)
    except Exception as error:
        raise TemplateError(f"{name} doesn't render: {error}") from None


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
