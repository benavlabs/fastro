"""Which files fastro may read (spec 1.10): a path read only when it resolves to a regular file inside the repo."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path


def utf8(path: str) -> bool:
    """Whether ``path`` is UTF-8: it holds none of the lone surrogates Python reads a byte UTF-8 can't decode as."""
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def shown(path: str) -> str:
    """``path`` as a message names it (spec 1.7): each byte UTF-8 can't decode written ``\\xNN``, as Python's
    ``backslashreplace`` writes it."""
    return path.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")


def unreadable(root: Path, path: str) -> str | None:
    """Why fastro may not read ``path`` in ``root``, or ``None`` when it resolves to a regular file inside ``root``
    (spec 1.10). Where its symlinks finally lead is what counts, wherever they pass on the way."""
    try:
        return breaks_the_rule(root, path)
    except OSError as error:
        return f"can't be resolved: {error.strerror}"


def file_kind(path: Path, follow: bool = True) -> str | None:
    """What's at ``path``: ``"file"``, ``"directory"``, ``"symlink"`` (only when ``follow`` is false), ``"other"``,
    or ``None`` when nothing is, which with ``follow`` includes a symlink that leads to nothing or can't be followed.

    Raises:
        OSError: the system won't let fastro look (spec 1.10, 8), on every Python version.
    """
    try:
        mode = os.stat(path).st_mode if follow else os.lstat(path).st_mode
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            return None
        raise
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    return "other"


def present(path: Path) -> bool:
    """Whether anything is at ``path``, a symlink to nothing included.

    Raises:
        OSError: the system won't let fastro look (spec 1.10).
    """
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def breaks_the_rule(root: Path, path: str) -> str | None:
    """How ``path`` in ``root`` breaks spec 1.10's rule, or ``None`` when it doesn't.

    Raises:
        OSError: the system won't let fastro resolve it, other than a symlink loop.
    """
    base = root.resolve()
    try:
        target = (base / path).resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        return "leads to nothing"
    except RuntimeError:
        return "has symlinks that can't be followed"
    except OSError as error:
        if error.errno != errno.ELOOP:
            raise
        return "has symlinks that can't be followed"
    if not target.is_relative_to(base):
        return "leads outside the repo"
    if file_kind(target) != "file":
        return "isn't a regular file"
    return None


def unreadable_config(root: Path, name: str) -> str | None:
    """The usage error for ``root / name`` when there's anything at that path, a symlink to nothing included, that
    fastro may not read (spec 1.10); ``None`` when there's nothing there or it may be read.

    Raises:
        OSError: the system won't let fastro look at the path or resolve it, which is a file it won't let fastro
            open (spec 1.10, 8).
    """
    reason = breaks_the_rule(root, name) if present(root / name) else None
    return f"{root / name} {reason}. fastro reads {name} only as a regular file inside the repo." if reason else None
