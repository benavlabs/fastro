"""Plugin discovery for the fastro CLI.

``bp.commands`` entry points are Typer sub-apps mounted under the root,
e.g. ``fastro aws deploy``. Plugin packages declare entry points whose
values resolve to a ``typer.Typer`` instance.

A broken plugin must not break the CLI. Discovery wraps each load in a
broad except and surfaces a warning so the user can still operate the
working subset.
"""

from __future__ import annotations

import sys
import warnings
from importlib.metadata import EntryPoint, entry_points

import typer

COMMANDS_GROUP = "bp.commands"


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
    """Return ``{name: typer_app}`` for every healthy ``bp.commands`` entry point."""
    found: dict[str, typer.Typer] = {}
    for ep in entry_points(group=COMMANDS_GROUP):
        loaded = _safe_load(ep)
        if loaded is None:
            continue
        if isinstance(loaded, typer.Typer):
            found[ep.name] = loaded
        else:
            warnings.warn(
                f"Plugin {ep.name!r} resolved to {type(loaded).__name__}, expected typer.Typer; ignoring.",
                RuntimeWarning,
                stacklevel=2,
            )
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
