"""A repo's tracked files, and a plan's changes made in a directory: copies, writes and deletes, and nothing else."""

from __future__ import annotations

import base64
import contextlib
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from fastro.engine.files import file_kind, present
from fastro.engine.plan import Plan
from fastro.engine.spec import RepoPath
from fastro.tools import require

EXPORT_IGNORE = "export-ignore"
EXPORT_SUBST = "export-subst"
EXTRAHEADER = "http.https://github.com/.extraheader"
CHUNK = 1 << 16
LOW_SPEED = 1000
STALL = 60
CONNECT = 30
POLL = 0.05


class RepositoryError(Exception):
    """A directory whose tracked files were asked for isn't a git repository."""


class GitError(Exception):
    """A git command fastro ran failed. The message is what git said."""


class TooBig(Exception):
    """A fetch whose objects passed the most fastro lets one take on disk."""


class Stalled(Exception):
    """An SSH fetch whose objects stopped growing for longer than fastro waits."""


class ApplyError(Exception):
    """A change apply refuses to make: outside the target, through a symlink, not one this step may change, at a
    directory or anything else that isn't a regular file, or under a file where a directory should be."""


@dataclass(frozen=True)
class Change:
    """One change to a directory: ``copy`` a source file (the one at ``source``, or at ``path`` when ``source`` is
    empty), ``write`` ``text`` as UTF-8 with its newlines as given, or ``delete`` a file."""

    kind: str
    path: str
    text: str = ""
    source: str = ""


def tracked_files(repo: Path, untracked: bool = False, exported: bool = True) -> tuple[RepoPath, ...]:
    """The files git tracks in ``repo``, sorted by path in code point order, leaving out one deleted from the working
    tree and, while ``exported`` holds, one ``export-ignore`` marks, as ``git archive`` does (spec 1.7, 2.13). With
    ``untracked``, the files git doesn't track or ignore are listed too, as ``--include-untracked`` asks (1.7).
    ``repo`` may be a directory inside a work tree: the paths are relative to it, and the work tree's own
    ``.gitattributes`` decide.

    One fastro can't check, in a directory it isn't allowed to search, is kept, so whoever reads it says why it can't.

    Raises:
        MissingToolError: ``git`` isn't on ``PATH``.
        RepositoryError: ``repo`` isn't a git repository, or isn't in its work tree.
    """
    git = require("git")
    others = ["--others", "--exclude-standard"] if untracked else []
    listed = subprocess.run([git, "-C", str(repo), "ls-files", "-z", "--cached", *others], capture_output=True, text=True)
    if listed.returncode != 0:
        raise RepositoryError(f"{repo} isn't a git repository: {listed.stderr.strip()}")
    top, prefix = _place(repo)
    listed_names = {name.rstrip("/") for name in listed.stdout.split("\0") if name}
    names = sorted(name for name in listed_names if _in_working_tree(repo / name))
    if not exported:
        return tuple(RepoPath(name) for name in names)
    directories = sorted({directory for name in names for directory in _above(prefix + name)})
    ignored = _set(Path(top), [*(prefix + name for name in names), *directories], EXPORT_IGNORE)
    return tuple(RepoPath(name) for name in names if ignored.isdisjoint([prefix + name, *_above(prefix + name)]))


def export_ignored(top: Path, name: str) -> bool:
    """Whether ``export-ignore`` marks ``name``, or a directory above it checked as ``dir/``, by the
    ``.gitattributes`` files under ``top`` alone (spec 1.7), as ``git archive`` would leave it out.

    Raises:
        MissingToolError: ``git`` isn't on ``PATH``.
    """
    return bool(_set(top, [name, *_above(name)], EXPORT_IGNORE))


def _above(name: str) -> list[str]:
    """Each directory ``name`` sits in, written as ``dir/``, which is how an ``export-ignore`` directory pattern
    matches it."""
    parts = name.split("/")[:-1]
    return [f"{'/'.join(parts[: count + 1])}/" for count in range(len(parts))]


def _place(repo: Path) -> tuple[Path, str]:
    """The top of the work tree ``repo`` sits in, and ``repo``'s path inside it as git writes a prefix (``a/b/``).

    Raises:
        RepositoryError: git says ``repo`` isn't in a work tree, quoting it.
    """
    git = require("git")
    where = subprocess.run(
        [git, "-C", str(repo), "rev-parse", "--show-toplevel", "--show-prefix"], capture_output=True, text=True
    )
    if where.returncode != 0:
        raise RepositoryError(f"{repo} isn't in a git work tree: {where.stderr.strip()}")
    top, prefix = where.stdout.split("\n")[:2]
    return Path(top), prefix


def head_commit(repo: Path) -> str | None:
    """The full commit ``HEAD`` names in the work tree ``repo`` sits in, or ``None`` before its first commit.

    Raises:
        MissingToolError: ``git`` isn't on ``PATH``.
        RepositoryError: git says ``repo`` isn't in a work tree.
    """
    _place(repo)
    git = require("git")
    head = subprocess.run([git, "-C", str(repo), "rev-parse", "--verify", "-q", "HEAD"], capture_output=True, text=True)
    return head.stdout.strip() if head.returncode == 0 else None


def export_subst(repo: Path, files: Iterable[RepoPath]) -> tuple[RepoPath, ...]:
    """Each of ``files`` that git's ``export-subst`` attribute is set on itself, by the ``.gitattributes`` files of
    the work tree ``repo`` sits in (spec 1.7), in the order given.

    Raises:
        MissingToolError: ``git`` isn't on ``PATH``.
        RepositoryError: git says ``repo`` isn't in a work tree.
    """
    given = [path.text for path in files]
    top, prefix = _place(repo)
    marked = _set(top, [prefix + name for name in given], EXPORT_SUBST)
    return tuple(RepoPath(name) for name in given if prefix + name in marked)


def _set(repo: Path, names: Sequence[str], attribute: str) -> set[str]:
    """Which of ``names`` ``attribute`` is set on, by the working tree's ``.gitattributes`` files alone, matched case
    by case as on Linux: never ``core.attributesFile``, the system's attributes, ``$GIT_DIR/info/attributes``, a git
    template's, or a repo the environment's git variables name (spec 1.7)."""
    if not names:
        return set()
    git = require("git")
    env = _environment()
    with tempfile.TemporaryDirectory(prefix="fastro-attributes-") as empty:
        subprocess.run([git, "init", "-q", "--bare", "--template=", empty], capture_output=True, check=True, env=env)
        checked = subprocess.run(
            [
                git,
                f"--git-dir={empty}",
                f"--work-tree={repo}",
                "-c",
                f"core.attributesFile={os.devnull}",
                "-c",
                "core.ignoreCase=false",
                "check-attr",
                "-z",
                "--stdin",
                attribute,
            ],
            input="\0".join(names) + "\0",
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
    fields = checked.stdout.split("\0")
    return {fields[index] for index in range(0, len(fields) - 2, 3) if fields[index + 2] == "set"}


def _environment(remote: bool = False, token: str | None = None, cwd: Path | None = None, stall: int = STALL) -> dict[str, str]:
    """This process's environment for a git that reads no repo the environment's git variables name and no
    system attributes (spec 1.7). With ``remote``, git never prompts, through a terminal, an askpass program or
    SSH, accepts no new host key, traces nothing, whatever its config says, and, given ``token``, sends it to
    ``https://github.com/`` as a header, set through git's environment config so it's in no argv and no config
    file. Its transfer aborts below ``LOW_SPEED`` bytes a second for ``stall`` seconds, and SSH gives up connecting
    after ``CONNECT`` seconds (spec 1.7). ``cwd`` is where git runs, and no repo is looked for above it, or in it
    unless it's one.

    Raises:
        MissingToolError: ``git`` isn't on ``PATH``.
    """
    git = require("git")
    local = subprocess.run([git, "rev-parse", "--local-env-vars"], capture_output=True, text=True, check=True)
    unset = {*local.stdout.split(), "GIT_ATTR_SOURCE"}
    env = {name: value for name, value in os.environ.items() if name not in unset} | {"GIT_ATTR_NOSYSTEM": "1"}
    if not remote:
        return env
    quiet = {
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "GIT_CURL_VERBOSE",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_CEILING_DIRECTORIES",
        "GIT_HTTP_LOW_SPEED_LIMIT",
        "GIT_HTTP_LOW_SPEED_TIME",
    }
    env = {name: value for name, value in env.items() if name not in quiet and not name.startswith("GIT_TRACE")}
    env |= {
        "GIT_TRACE2": "0",
        "GIT_TRACE2_EVENT": "0",
        "GIT_TRACE2_PERF": "0",
        "GIT_ASKPASS": "",
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
    }
    if cwd is not None:
        env["GIT_CEILING_DIRECTORIES"] = os.path.realpath(cwd.parent)
    ssh = os.environ.get("GIT_SSH_COMMAND", "")
    if not ssh:
        configured = subprocess.run(
            [git, "config", "--get", "core.sshCommand"],
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
        )
        ssh = configured.stdout.strip()
    if not ssh:
        ssh = shlex.quote(os.environ["GIT_SSH"]) if os.environ.get("GIT_SSH") else "ssh"
    env["GIT_SSH_COMMAND"] = f"{ssh} -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout={CONNECT}"
    settings = {"http.lowSpeedLimit": str(LOW_SPEED), "http.lowSpeedTime": str(stall)}
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        settings = {EXTRAHEADER: f"AUTHORIZATION: basic {basic}", **settings}
    env["GIT_CONFIG_COUNT"] = str(len(settings))
    for index, (key, value) in enumerate(settings.items()):
        env |= {f"GIT_CONFIG_KEY_{index}": key, f"GIT_CONFIG_VALUE_{index}": value}
    return env


def _git(arguments: Sequence[str], env: Mapping[str, str], cwd: Path | None = None) -> str:
    """What git prints on stdout for ``arguments``, run with ``env`` in ``cwd`` and nothing on stdin.

    Raises:
        GitError: git exits with an error.
    """
    git = require("git")
    done = subprocess.run([git, *arguments], capture_output=True, text=True, env=dict(env), stdin=subprocess.DEVNULL, cwd=cwd)
    if done.returncode != 0:
        raise GitError(done.stderr.strip() or f"git exited {done.returncode}")
    return done.stdout


def remote_refs(url: str, token: str | None, stall: int = STALL) -> dict[str, str]:
    """Each ref the remote at ``url`` lists, by its full name, with the object it names, listed from an empty
    directory so no repo's own config steers it. An annotated tag's commit is under the tag's name plus ``^{}``,
    as ``git ls-remote`` writes it.

    An SSH remote that hasn't listed them within ``stall`` seconds is stopped, with what it started (spec 1.7).

    Raises:
        MissingToolError: ``git`` isn't on ``PATH``.
        GitError: git can't list the remote's refs, or an SSH remote didn't in time.
    """
    git = require("git")
    with tempfile.TemporaryDirectory(prefix="fastro-git-") as empty:
        env = _environment(True, token, Path(empty), stall)
        with subprocess.Popen(
            [git, "ls-remote", "--", url],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=empty,
            start_new_session=True,
        ) as process:
            try:
                listed, said = process.communicate(timeout=stall if url.startswith("ssh://") else None)
            except subprocess.TimeoutExpired:
                _stop(process)
                raise GitError(f"it hadn't finished after {stall} seconds.") from None
            except BaseException:
                _stop(process)
                raise
    if process.returncode != 0:
        raise GitError(said.decode("utf-8", "replace").strip() or f"git exited {process.returncode}")
    lines = listed.decode("utf-8", "replace").splitlines()
    return {name: sha for sha, _, name in (line.partition("\t") for line in lines) if name}


def fetch(url: str, ref: str, token: str | None, repo: Path, most: int, stall: int = STALL) -> str:
    """Fetch ``ref`` from ``url`` alone, one commit deep, into a new bare repo at ``repo``, and give its commit.
    Its objects may take no more than ``most`` bytes on disk, checked while git fetches, stopping it and what it
    started when they don't, and once it's done. Over SSH, it's stopped too when they haven't passed their largest
    total for ``stall`` seconds, counted from when git starts (spec 1.7).

    Raises:
        MissingToolError: ``git`` isn't on ``PATH``.
        GitError: git can't fetch it.
        TooBig: its objects take more than ``most`` bytes.
        Stalled: an SSH fetch's objects stopped growing for ``stall`` seconds.
    """
    env = _environment(True, token, repo.parent, stall)
    _git(["init", "-q", "--bare", "--template=", str(repo)], env, repo.parent)
    git = require("git")
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            [
                git,
                f"--git-dir={repo}",
                "-c",
                "maintenance.auto=false",
                "-c",
                "gc.auto=0",
                "fetch",
                "-q",
                "--depth",
                "1",
                "--",
                url,
                ref,
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=errors,
            start_new_session=True,
        )
        watching, largest, grew = url.startswith("ssh://"), 0, time.monotonic()
        try:
            while process.poll() is None:
                size = _size(repo / "objects")
                if size > most:
                    raise TooBig
                if size > largest:
                    largest, grew = size, time.monotonic()
                elif watching and time.monotonic() - grew > stall:
                    raise Stalled
                time.sleep(POLL)
        except BaseException:
            _stop(process)
            raise
        if process.returncode != 0:
            raise GitError(_said(errors, process.returncode))
    if _size(repo / "objects") > most:
        raise TooBig
    return _git([f"--git-dir={repo}", "rev-parse", "--verify", "-q", "FETCH_HEAD^{commit}"], env, repo.parent).strip()


def _stop(process: subprocess.Popen[bytes]) -> None:
    """Kill ``process`` and every process it started in its session, and wait for it."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def _said(errors: IO[bytes], code: int) -> str:
    errors.seek(0)
    return errors.read().decode("utf-8", "replace").strip() or f"git exited {code}"


def _size(directory: Path) -> int:
    total = 0
    for top, _, files in os.walk(directory):
        for name in files:
            with contextlib.suppress(FileNotFoundError):
                total += os.lstat(os.path.join(top, name)).st_size
    return total


@contextlib.contextmanager
def archive(repo: Path, commit: str) -> Iterator[Iterator[bytes]]:
    """``git archive --format=tar.gz --prefix=archive/`` of ``commit`` in the bare repo ``repo``, its bytes as git
    writes them, with attributes from the commit alone and none of the user's or the system's git config, so the
    tree is what ``git archive`` on GitHub writes (spec 1.7). git and what it started are stopped when the caller
    raises.

    Raises:
        GitError: git exits with an error, raised as its last bytes are read, before anything after them.
    """
    env = _environment() | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    git = require("git")
    attributes = f"core.attributesFile={os.devnull}"
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            [git, f"--git-dir={repo}", "-c", attributes, "archive", "--format=tar.gz", "--prefix=archive/", commit],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=errors,
            cwd=repo.parent,
            start_new_session=True,
        )
        assert process.stdout is not None
        output = process.stdout

        def pieces() -> Iterator[bytes]:
            yield from iter(lambda: output.read(CHUNK), b"")
            if process.wait() != 0:
                raise GitError(_said(errors, process.returncode))

        try:
            yield pieces()
        except BaseException:
            _stop(process)
            raise
        finally:
            output.close()
            process.wait()


def _in_working_tree(path: Path) -> bool:
    try:
        return present(path)
    except OSError:
        return True


def changes(plan: Plan, rendered: Mapping[str, str]) -> list[Change]:
    """The changes ``plan`` makes: a copy for each ``copy`` op, a write of ``rendered`` text for each ``render`` op,
    and a delete for each ``delete`` op, in the plan's order. A ``skip`` changes nothing."""
    found = []
    for op in plan.ops:
        if op.op == "copy":
            found.append(Change("copy", op.path))
        elif op.op == "render":
            found.append(Change("write", op.path, rendered[op.path]))
        elif op.op == "delete":
            found.append(Change("delete", op.path))
    return found


def check(
    found: Iterable[Change], source: Path, target: Path, only: Collection[str] | None = None
) -> list[tuple[Change, Path, Path | None]]:
    """Each change with where it lands in ``target`` and, for a copy, the file it copies from ``source``, once every
    one is known to be allowed. Nothing is changed.

    A path must stay inside ``target`` once resolved, and no part of it there, nor of a copied file in ``source``,
    may be a symlink, and a copied file must be a regular file there. In ``target``, no part of the way to it may be a
    file, and what's already at it must be a regular file. With ``only``, each change's path must be one of them.

    Raises:
        ApplyError: a change breaks one of those rules.
    """
    checked = []
    for change in found:
        if only is not None and change.path not in only:
            raise ApplyError(f"{change.path!r} isn't a file this step may change, so nothing was changed.")
        destination = _inside(target, change.path, "the target")
        _regular_or_absent(target, destination, change.path)
        origin = None
        if change.kind == "copy":
            origin = _inside(source, change.source or change.path, "the source")
            _regular_source(origin, change.source or change.path)
        checked.append((change, destination, origin))
    return checked


def apply(found: Iterable[Change], source: Path, target: Path, only: Collection[str] | None = None) -> None:
    """Make each change in ``target``, copying from ``source``, once :func:`check` has allowed every one.

    Raises:
        ApplyError: a change isn't allowed. Nothing has been changed.
    """
    for change, destination, origin in check(found, source, target, only):
        if change.kind == "delete":
            if file_kind(destination) is not None:
                destination.unlink()
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if origin is not None:
            shutil.copyfile(origin, destination)
            shutil.copymode(origin, destination)
        else:
            destination.write_bytes(change.text.encode("utf-8"))


def _regular_source(origin: Path, path: str) -> None:
    """Refuse a copy whose file in the source is a directory or anything else that isn't a regular file."""
    kind = file_kind(origin)
    if kind == "directory":
        raise ApplyError(f"{path!r} is a directory in the source, so nothing was changed.")
    if kind != "file":
        raise ApplyError(f"{path!r} isn't a regular file in the source, so nothing was changed.")


def _regular_or_absent(target: Path, destination: Path, path: str) -> None:
    """Refuse a change whose way in ``target`` passes through a file, or whose path holds anything but a regular
    file."""
    base = target.resolve()
    current = base
    for part in destination.relative_to(base).parts[:-1]:
        current = current / part
        if file_kind(current) not in (None, "directory"):
            shown = current.relative_to(base).as_posix()
            raise ApplyError(f"{shown!r} is a file in the target where a directory should be, so nothing was changed.")
    kind = file_kind(destination)
    if kind == "directory":
        raise ApplyError(f"{path!r} is a directory in the target, so nothing was changed.")
    if kind not in (None, "file"):
        raise ApplyError(f"{path!r} isn't a regular file in the target, so nothing was changed.")


def _inside(root: Path, path: str, where: str) -> Path:
    """``root / path``, once it's known to stay inside ``root`` with no symlink on the way."""
    base = root.resolve()
    joined = Path(os.path.normpath(base / path))
    if not joined.is_relative_to(base) or joined == base:
        raise ApplyError(f"{path!r} resolves outside {where}, so nothing was changed.")
    current = base
    for part in joined.relative_to(base).parts:
        current = current / part
        if file_kind(current, follow=False) == "symlink":
            shown = current.relative_to(base).as_posix()
            raise ApplyError(f"{shown!r} is a symlink in {where}, and apply doesn't follow one, so nothing was changed.")
    return joined
