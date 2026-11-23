"""The base snapshot in ``.fastro/base/files/``, and the ``[files]`` entries of the lock that go with it (spec 4)."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path

from fastro.apply import Change
from fastro.engine.files import breaks_the_rule, present
from fastro.engine.lock import LOCK_FILE, Lock, LockedFile
from fastro.engine.spec import RepoPath

BASE = ".fastro/base/files"
SUFFIX = ".base"
ATTRIBUTES = ".fastro/.gitattributes"
NO_CONVERSION = "* -text\n"
WIRING = "wiring"
RESTORE = "fastro writes both together, so one was changed by hand. Restore them from git."


class SnapshotError(Exception):
    """A snapshot file that's missing or doesn't match its ``[files]`` entry, or one asked for that has none."""


class UnreadableSnapshot(Exception):
    """A snapshot file spec 1.10 rules out: a usage error."""


def base(path: str) -> str:
    """Where ``path``'s snapshot sits in a project: ``.fastro/base/files/<path>.base``."""
    return f"{BASE}/{path}{SUFFIX}"


def recorded(changes: Sequence[Change], origins: Mapping[str, str], source: Path) -> dict[RepoPath, LockedFile]:
    """The ``[files]`` entry for each of ``changes`` whose path has one of ``origins``, a copy or a write, with the
    sha256 of the bytes it puts there: the file a copy reads from ``source``, or the UTF-8 of the text it writes."""
    return {
        RepoPath(change.path): LockedFile(hashlib.sha256(_bytes(change, source)).hexdigest(), origins[change.path])
        for change in changes
        if change.path in origins
    }


def snapshot(changes: Sequence[Change], origins: Mapping[str, str]) -> list[Change]:
    """The changes that write the snapshot: each of ``changes`` whose path has an origin other than ``wiring``, made
    again at its :func:`base` path, then ``.fastro/.gitattributes`` holding ``* -text``, so git never converts a
    snapshot's line endings."""
    mirrored = [
        Change(change.kind, base(change.path), change.text, change.source or (change.path if change.kind == "copy" else ""))
        for change in changes
        if origins.get(change.path, WIRING) != WIRING
    ]
    return [*mirrored, Change("write", ATTRIBUTES, NO_CONVERSION)]


def read(root: Path, lock: Lock, path: str) -> bytes:
    """The bytes the snapshot in the project at ``root`` holds for ``path``, checked against ``lock``.

    Raises:
        SnapshotError: ``path`` has no ``[files]`` entry, it's a wiring file, or its snapshot file is missing or its
            sha256 isn't the entry's (spec 4: exit 1).
        UnreadableSnapshot: the snapshot file breaks spec 1.10's rule.
        OSError: the system won't let fastro resolve or open it (spec 8: exit 3).
    """
    entry = lock.files.get(RepoPath(path))
    if entry is None:
        raise SnapshotError(f"{path} isn't in {LOCK_FILE}'s [files], so it has no snapshot.")
    if entry.origin == WIRING:
        raise SnapshotError(f"{path} is a wiring file, which has no snapshot: fastro renders it again instead.")
    file = base(path)
    if not present(root / file):
        raise SnapshotError(f"{file} is missing, and {LOCK_FILE} records {path}. {RESTORE}")
    if (reason := breaks_the_rule(root, file)) is not None:
        raise UnreadableSnapshot(f"{root / file} {reason}. fastro reads a snapshot only as a regular file inside the project.")
    data = (root / file).read_bytes()
    if hashlib.sha256(data).hexdigest() != entry.sha256:
        raise SnapshotError(f"{file} doesn't match its sha256 in {LOCK_FILE}. {RESTORE}")
    return data


def _bytes(change: Change, source: Path) -> bytes:
    if change.kind == "copy":
        return (source / (change.source or change.path)).read_bytes()
    return change.text.encode("utf-8")
