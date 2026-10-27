"""Plugin discovery for the fastro CLI.

``fastro.commands`` entry points are Typer sub-apps mounted under the root,
e.g. ``fastro aws deploy``. Plugin packages declare entry points whose
values resolve to a ``typer.Typer`` instance. ``bp.commands`` is still read,
with a warning, until fastro 0.2.

A broken plugin must not break the CLI. Discovery wraps each load in a
broad except and surfaces a warning so the user can still operate the
working subset.
"""

from __future__ import annotations

import sys
import warnings
from importlib.metadata import EntryPoint, entry_points

import typer

COMMANDS_GROUP = "fastro.commands"
DEPRECATED_COMMANDS_GROUP = "bp.commands"


def _safe_load(ep: EntryPoint) -> object | None:
    try:
        loaded: object = ep.load()
    except Exception as exc:
        warnings.warn(
            f"Failed to load plugin {ep.name!r} from {ep.value!r}: {exc!s}",
            RuntimeWarning,
            stacklevel=2,
        )
        return None
    return loaded


def discover_command_plugins() -> dict[str, typer.Typer]:
    """Return ``{name: typer_app}`` for every healthy ``fastro.commands`` and ``bp.commands`` entry point.

    A name registered in both groups is loaded from ``fastro.commands`` only.
    """
    found: dict[str, typer.Typer] = {}
    seen: set[str] = set()
    for group in (COMMANDS_GROUP, DEPRECATED_COMMANDS_GROUP):
        for ep in entry_points(group=group):
            if ep.name in seen:
                continue
            seen.add(ep.name)
            loaded = _safe_load(ep)
            if loaded is None:
                continue
            if not isinstance(loaded, typer.Typer):
                warnings.warn(
                    f"Plugin {ep.name!r} resolved to {type(loaded).__name__}, expected typer.Typer; ignoring.",
                    RuntimeWarning,
                    stacklevel=2,
                )
                continue
            if group == DEPRECATED_COMMANDS_GROUP:
                warnings.warn(
                    f"plugin {ep.name!r} is registered under {DEPRECATED_COMMANDS_GROUP}, which fastro 0.2 stops reading; "
                    f"register it under {COMMANDS_GROUP} instead.",
                    RuntimeWarning,
                    stacklevel=2,
                )
            found[ep.name] = loaded
    return found


def emit_plugin_warnings_to_stderr() -> None:
    """Route plugin-load warnings to stderr so CLI output stays clean."""
    warnings.filterwarnings("default", category=RuntimeWarning, module=__name__)
    if not any(isinstance(h, _StderrWarningHandler) for h in _installed_handlers()):
        warnings.showwarning = _StderrWarningHandler()


class _StderrWarningHandler:
    def __call__(
        self,
        message: Warning | str,
        category: type[Warning],
        filename: str,
        lineno: int,
        file: object = None,
        line: str | None = None,
    ) -> None:
        print(f"warning: {message}", file=sys.stderr)


def _installed_handlers() -> list[object]:
    handler = warnings.showwarning
    return [handler] if handler is not None else []
