"""The definitions every catalog file shares: names, paths, refs and versions (spec section 1)."""

from __future__ import annotations

import keyword
import re
from dataclasses import dataclass

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

NAME = re.compile(r"[a-z][a-z0-9_]*")
BLUEPRINT_NAME = re.compile(r"[a-z][a-z0-9-]*")
LABEL = re.compile(r"[a-z][a-z0-9_-]*")
OPERATORS = ("and", "or", "not")
RESERVED = {
    "app": "the project's own code",
    "core": "the files no feature owns",
    **dict.fromkeys(OPERATORS, "the condition language"),
}
CHOICE = re.compile(r"[^\s()=]+")
GLOB = re.compile(r"[*?\[\]]")
CHECK_CODE = re.compile(r"[A-Z]+[0-9]+")
SOURCE = re.compile(
    r"/.*|\.\.?(/.*)?"
    r"|gh:[A-Za-z0-9._-]+/[A-Za-z0-9._-]+(@[^@\s]+)?"
    r"|git\+(ssh|https)://\S+"
    r"|[a-z][a-z0-9_-]*"
)


class SpecError(ValueError):
    """A value that breaks one of the rules every catalog file shares."""


def option_name(text: str) -> str:
    """``text``, if it's a valid option name: lowercase letters, digits and underscores, starting with a letter."""
    if not NAME.fullmatch(text):
        raise SpecError(f"{text!r} isn't a valid name: use lowercase letters, digits and underscores, starting with a letter.")
    return text


def feature_name(text: str) -> str:
    """``text``, if it's a valid feature name: an option name other than ``app`` and ``core``."""
    option_name(text)
    if text in RESERVED:
        raise SpecError(f"{text!r} is reserved for {RESERVED[text]}.")
    return text


def choice(text: str) -> str:
    """``text``, if it's a valid option choice: one or more characters a condition's option atom can hold."""
    if not CHOICE.fullmatch(text):
        raise SpecError(f"{text!r} isn't a valid choice: use no whitespace, '(', ')' or '=', so a condition can name it.")
    return text


def label(text: str, kind: str) -> str:
    """``text``, if it's a valid preset or verify set name: lowercase letters, digits, ``-`` and ``_``, from a letter."""
    if not LABEL.fullmatch(text):
        raise SpecError(
            f"{text!r} isn't a valid {kind} name: use lowercase letters, digits, '-' and '_', starting with a letter."
        )
    return text


def blueprint_name(text: str) -> str:
    """``text``, if it's a valid blueprint name: lowercase letters, digits and hyphens, starting with a letter."""
    if not BLUEPRINT_NAME.fullmatch(text):
        raise SpecError(
            f"{text!r} isn't a valid blueprint name: use lowercase letters, digits and hyphens, starting with a letter."
        )
    return text


@dataclass(frozen=True)
class RepoPath:
    """A path relative to the repo root: one file, or with a trailing ``/``, the whole subtree."""

    text: str

    @property
    def subtree(self) -> bool:
        return self.text.endswith("/")


def repo_path(text: str) -> RepoPath:
    """``text`` as a :class:`RepoPath`, if it's POSIX, relative, and names files literally."""

    def refuse(why: str) -> SpecError:
        return SpecError(f"{text!r} isn't a repo path: it's {why}. Write it relative to the repo root, with '/'.")

    if not text:
        raise refuse("empty")
    if text.startswith("/"):
        raise refuse("absolute")
    if "\\" in text:
        raise refuse("written with a backslash")
    if GLOB.search(text):
        raise refuse("a glob, and globs aren't supported")
    segments = text.removesuffix("/").split("/")
    if ".." in segments:
        raise refuse("climbing out with '..'")
    if "." in segments:
        raise refuse("using '.'")
    if "" in segments:
        raise refuse("got an empty segment")
    return RepoPath(text)


def identifier(text: str) -> bool:
    """Whether ``text`` is a Python name that isn't a keyword."""
    return text.isidentifier() and not keyword.iskeyword(text)


@dataclass(frozen=True)
class Ref:
    """``MODULE:ATTR [as ALIAS]``: a name defined at the top level of a module under the Python root."""

    module: str
    attr: str
    alias: str | None = None

    @property
    def name(self) -> str:
        """The local name a rendered import binds: the alias, or else the attribute."""
        return self.alias or self.attr

    @classmethod
    def parse(cls, text: str) -> Ref:
        target, has_alias, alias = text.partition(" as ")
        module, _, attr = target.partition(":")
        target_ok = all(identifier(part) for part in module.split(".")) and identifier(attr)
        if not target_ok or (has_alias and not identifier(alias)):
            raise SpecError(f"{text!r} isn't a ref: expected 'package.module:name' or 'package.module:name as alias'.")
        return cls(module, attr, alias if has_alias else None)

    def __str__(self) -> str:
        return f"{self.module}:{self.attr}" + (f" as {self.alias}" if self.alias else "")


def version(text: str) -> Version:
    """``text`` as a PEP 440 version."""
    try:
        return Version(text)
    except InvalidVersion:
        raise SpecError(f"{text!r} isn't a PEP 440 version, such as '0.21.0'.") from None


def specifier(text: str) -> SpecifierSet:
    """``text`` as a PEP 440 version specifier."""
    try:
        return SpecifierSet(text)
    except InvalidSpecifier:
        raise SpecError(f"{text!r} isn't a PEP 440 version specifier, such as '>=0.20,<0.22'.") from None


def requirement(text: str) -> Requirement:
    """``text`` as a PEP 508 requirement."""
    try:
        return Requirement(text)
    except InvalidRequirement:
        raise SpecError(f"{text!r} isn't a PEP 508 requirement, such as 'fastapi[standard]>=0.115.8'.") from None


def allows(spec: SpecifierSet, candidate: Version) -> bool:
    """Whether ``candidate`` is in ``spec``, taking a prerelease only when ``spec`` names one."""
    return spec.contains(candidate, prereleases=bool(spec.prereleases))


def tool_allowed(requires_fastro: SpecifierSet, tool: Version) -> bool:
    """Whether a catalog's ``requires_fastro`` lets this tool read it, development builds included."""
    return requires_fastro.contains(tool, prereleases=True)


def check_code(text: str) -> str:
    """``text``, if it's a check code such as ``FR013``: capitals, then digits."""
    if not CHECK_CODE.fullmatch(text):
        raise SpecError(f"{text!r} isn't a check code, such as 'FR013'.")
    return text


def source(text: str) -> str:
    """``text``, if it reads as a source (spec 1.7). Nothing is fetched, so this checks only how it's written."""
    where, hash_, subdir = text.partition("#")
    if not SOURCE.fullmatch(where) or (hash_ and not _subdir(subdir)):
        raise SpecError(
            f"{text!r} isn't a source: write a path (absolute, ./ or ../), gh:OWNER/REPO[@REF], git+ssh://… or "
            "git+https://…, or a registry name, then an optional #SUBDIR."
        )
    return text


def _subdir(text: str) -> bool:
    try:
        repo_path(text)
    except SpecError:
        return False
    return True
