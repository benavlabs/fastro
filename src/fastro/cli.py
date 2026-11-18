"""Root Typer application and entry point.

``app`` holds the built-in commands and the third-party plugins. ``for_directory`` adds the commands the blueprint
declares (spec 2.9) for the repo a directory is in, and is what the ``fastro`` and ``bp`` console scripts run.
"""

from __future__ import annotations

import copy
import functools
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import typer
from typer.core import TyperCommand

from . import declared as _declared
from . import plugins as _plugins
from . import runner as _runner
from .commands import blueprint as _blueprint_cmd
from .commands import env as _env_cmd
from .commands import make as _make_cmd
from .commands import sync as _sync_cmd
from .engine.blueprint import Command
from .tools import MissingToolError

HELP = "Start and maintain FastAPI projects."
GROUPS: dict[str, tuple[typer.Typer, str]] = {
    "make": (_make_cmd.app, "Generate files the project owns, such as the deploy compose files."),
    "env": (_env_cmd.app, "Inspect and prepare the runtime environment."),
    "blueprint": (_blueprint_cmd.app, "Check and verify a blueprint repo."),
}
COMMANDS: dict[str, Callable[..., None]] = {"sync": _sync_cmd.sync}
ORDER = ("make", "env", "sync", "blueprint")
BUILT_IN_ANYWAY = {("env", "secret"), ("env", "validate")}
REPORTS_THE_CATALOG = {"sync", "blueprint", "make"}
TAKES_EVERY_WORD = {"make"}
NO_COMMANDS = "this repo's declared commands aren't available, so there's no validator to run."
NOT_DECLARED = "this selection has no env validate command, so there's no validator to run."
UV_INSTALL_URL = "https://docs.astral.sh/uv/getting-started/installation/"


def _root() -> None:
    """Start and maintain FastAPI projects."""


def _application(groups: Mapping[str, typer.Typer]) -> typer.Typer:
    built = typer.Typer(name="fastro", help=HELP, no_args_is_help=True, pretty_exceptions_show_locals=False)
    built.callback()(_root)
    for name in ORDER:
        if name in COMMANDS:
            built.command(name=name)(COMMANDS[name])
        else:
            built.add_typer(groups[name], name=name, help=GROUPS[name][1])
    return built


@functools.cache
def _command_plugins() -> dict[str, typer.Typer]:
    return _plugins.discover_command_plugins()


def _mount_command_plugins(into: typer.Typer | None = None) -> None:
    """Mount external Typer sub-apps registered under ``fastro.commands`` (or, deprecated, ``bp.commands``).

    A plugin that fails to load warns rather than stopping the CLI, so the warning
    is routed to stderr first: on stdout it would land in whatever the caller is
    piping command output into.
    """
    target = app if into is None else into
    _plugins.emit_plugin_warnings_to_stderr()

    taken = {group.name for group in target.registered_groups} | {command.name for command in target.registered_commands}
    for name, sub_app in _command_plugins().items():
        if name in taken:
            typer.secho(
                f"warning: plugin command '{name}' shadows a built-in or declared command; ignoring.",
                fg=typer.colors.YELLOW,
                err=True,
            )
            continue
        target.add_typer(sub_app, name=name)


app = _application({name: group for name, (group, _) in GROUPS.items()})
_mount_command_plugins()


def for_directory(start: Path, argv: Sequence[str]) -> typer.Typer:
    """The CLI for ``start``: the built-ins, plus the commands the blueprint declares when ``start`` is in a blueprint
    repo, found by looking for ``fastro.toml`` from ``start`` upward.

    When the repo's declared commands can't be added, it warns on stderr, unless ``argv`` runs a command that reports
    the problem itself.
    """
    try:
        root = _declared.find(start)
        found = _declared.load(root) if root is not None else None
    except _declared.Unavailable as error:
        if not argv or argv[0] not in REPORTS_THE_CATALOG:
            _warn(str(error))
        return _built({}, NO_COMMANDS)
    if found is None:
        return _built({}, _env_cmd.OUTSIDE)
    commands = {words: (command.help, _run(command, found)) for words, command in _kept(found.commands).items()}
    return _built(commands, NOT_DECLARED)


def _built(commands: Mapping[tuple[str, ...], tuple[str, Callable[[typer.Context], None]]], reason: str) -> typer.Typer:
    """The built-ins with ``commands`` added at their words, and a built-in ``env validate`` saying ``reason`` unless
    ``commands`` has one."""
    groups = {
        name: _copy(group, {words[1] for words in commands if words[:1] == (name,)}) for name, (group, _) in GROUPS.items()
    }
    if ("env", "validate") not in commands:
        groups["env"].registered_commands = [info for info in groups["env"].registered_commands if info.name != "validate"]
        groups["env"].command("validate")(_env_cmd.validate_command(reason))
    built = _application(groups)
    tree: dict[tuple[str, ...], typer.Typer] = {(): built, **{(name,): group for name, group in groups.items()}}
    for words, (help_text, run) in commands.items():
        for depth in range(1, len(words)):
            if words[:depth] not in tree:
                tree[words[:depth]] = typer.Typer(no_args_is_help=True)
                tree[words[: depth - 1]].add_typer(tree[words[:depth]], name=words[depth - 1])
        tree[words[:-1]].command(name=words[-1], help=help_text, cls=_Verbatim)(run)
    _mount_command_plugins(built)
    return built


class _Verbatim(TyperCommand):
    """A command that hands its callback every argument after its words, ``--`` and ``--help`` included."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        ctx.args = list(args)
        return []


def _warn(message: str) -> None:
    typer.secho(f"warning: {message}", fg=typer.colors.YELLOW, err=True)


def _kept(commands: Mapping[tuple[str, ...], Command]) -> dict[tuple[str, ...], Command]:
    """The declared commands that neither shadow a built-in nor have commands under them, warning about the rest."""
    kept = {}
    for words in sorted(commands):
        shown = f"fastro {' '.join(words)}"
        if words not in BUILT_IN_ANYWAY and _shadows(words):
            _warn(f"blueprint.toml declares '{shown}', which is built in; ignoring it.")
        elif any(other[: len(words)] == words for other in commands if other != words):
            _warn(f"blueprint.toml declares '{shown}' and commands under it; ignoring '{shown}'.")
        else:
            kept[words] = commands[words]
    return kept


def _shadows(words: tuple[str, ...]) -> bool:
    if words[0] in COMMANDS or words[0] in TAKES_EVERY_WORD:
        return True
    if words[0] not in GROUPS:
        return False
    return len(words) == 1 or words[1] in {info.name for info in GROUPS[words[0]][0].registered_commands}


def _copy(group: typer.Typer, without: set[str]) -> typer.Typer:
    copied = copy.copy(group)
    copied.registered_commands = [info for info in group.registered_commands if info.name not in without]
    copied.registered_groups = list(group.registered_groups)
    return copied


def _run(command: Command, found: _declared.Declared) -> Callable[[typer.Context], None]:
    def run(context: typer.Context) -> None:
        try:
            code = _runner.run_declared(command, found.root, found.python_root, context.args)
        except MissingToolError as missing:
            typer.echo(
                f"error: fastro {' '.join(command.words)} runs in the project through {missing.tool}, which isn't on "
                f"PATH. Install it: {UV_INSTALL_URL}",
                err=True,
            )
            raise typer.Exit(3) from None
        raise typer.Exit(code)

    return run


if __name__ == "__main__":  # pragma: no cover
    app()
