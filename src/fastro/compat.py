"""``bp``, the name the boilerplate's CLI went by, kept for fastro 0.1.x."""

from __future__ import annotations

import shlex
import sys
from collections.abc import Sequence

from .cli import app
from .start import check_cwd

RENAMED: dict[tuple[str, str], tuple[str, str]] = {
    ("deploy", "generate"): ("make", "deploy"),
    ("env", "gen-secret"): ("env", "secret"),
}


def translate(argv: Sequence[str]) -> list[str]:
    """``argv`` as ``fastro`` takes it, with the commands that were renamed spelled the new way."""
    renamed = RENAMED.get((argv[0], argv[1])) if len(argv) >= 2 else None
    if renamed is None:
        return list(argv)
    return [*renamed, *argv[2:]]


def bp_main() -> None:
    """Check the current directory as ``fastro`` does, say that ``bp`` is now ``fastro``, then run ``fastro`` with the
    same arguments."""
    check_cwd()
    argv = translate(sys.argv[1:])
    print(f"bp is now fastro. Next time, run: {shlex.join(['fastro', *argv])}", file=sys.stderr)
    app(args=argv, prog_name="fastro")
