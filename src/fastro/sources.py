"""Where a blueprint comes from (spec 1.7): reading a source, the default one, the named blueprints, the cache, and
the files a ``PATH`` source contributes."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp, mkstemp
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

from packaging.version import InvalidVersion, Version

from fastro import apply, tarball
from fastro.apply import RepositoryError, export_ignored, head_commit, tracked_files
from fastro.engine import spec
from fastro.engine.blueprint import readable
from fastro.engine.files import file_kind, present, unreadable_config
from fastro.engine.spec import RepoPath, SpecError

NAMED = {"fastapi-boilerplate": "gh:benavlabs/FastAPI-boilerplate"}
UNCONFIRMED = ("fastroai",)
DEFAULT = "fastapi-boilerplate"
DEFAULT_VARIABLE = "FASTRO_BLUEPRINT"
CACHE_VARIABLE = "FASTRO_CACHE_DIR"
CATALOG = "blueprint.toml"
INDEX = "refs.json"
GIT_PREFIXES = ("git+ssh://", "git+https://")
API = "https://api.github.com"
API_VERSION = "2022-11-28"
TOKEN_VARIABLE = "GITHUB_TOKEN"
TIMEOUT = 30
COMMIT = re.compile(r"[0-9a-f]{40}")
JSON = "application/vnd.github+json"
RAW = "application/vnd.github.raw+json"
SHA = "application/vnd.github.sha"
PAGE = 100
RATE_LIMITED = (403, 429)
SIZE_VARIABLE = "FASTRO_MAX_SOURCE_MB"
MB = 10**6
DEFAULT_MB = 200
MOST_MB_DIGITS = 12
CONTENTS = 10**6
GITHUB_SSH = re.compile(r"git\+ssh://git@github\.com/([^/]+/[^/]+)")
CHUNK = 1 << 16


class SourceError(Exception):
    """A source that doesn't read, or a ``PATH`` source that isn't a blueprint directory fastro may read: a usage
    error (spec 8)."""


class FetchError(Exception):
    """A source that can't be fetched, or has no release this fastro can read: an environment failure (spec 8).
    ``status`` is the HTTP status the server answered with, if it answered."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NotFound(FetchError):
    """A URL the server answered 404 for."""


class Fetch(Protocol):
    """How fastro asks for a URL: the body it answers with, sending ``headers``, read to at most one byte past
    ``limit`` when there is one."""

    def __call__(self, url: str, headers: Mapping[str, str], limit: int | None = None) -> bytes: ...


@dataclass(frozen=True)
class Limits:
    """The most a ``gh:`` tarball or a ``git+`` fetch may be, in bytes: as downloaded or fetched, and its files'
    sizes added up (spec 1.10)."""

    download: int
    extracted: int


def limits(environ: Mapping[str, str]) -> Limits:
    """200 MB downloaded and 1 GB extracted, or with ``FASTRO_MAX_SOURCE_MB`` set, that many MB and five times it
    (spec 1.10).

    Raises:
        SourceError: ``FASTRO_MAX_SOURCE_MB`` is set to anything but empty or a whole number of 200 or more.
    """
    value = environ.get(SIZE_VARIABLE, "")
    whole = re.fullmatch("[0-9]+", value) is not None
    digits = value.lstrip("0")
    megabytes = DEFAULT_MB
    if whole:
        megabytes = int(digits or "0") if len(digits) <= MOST_MB_DIGITS else 10**MOST_MB_DIGITS
    if value and (not whole or megabytes < DEFAULT_MB):
        raise SourceError(f"{SIZE_VARIABLE} is {value!r}. Set it to a whole number of MB, {DEFAULT_MB} or more, or unset it.")
    return Limits(megabytes * MB, 5 * megabytes * MB)


def _size(count: int) -> str:
    gigabyte = 1000 * MB
    return f"{count // gigabyte} GB" if count % gigabyte == 0 else f"{count // MB} MB"


@dataclass(frozen=True)
class Resolved:
    """A ``gh:`` or ``git+`` source pinned: the ref it resolved to, and that ref's full commit."""

    ref: str
    commit: str


@dataclass(frozen=True)
class Source:
    """A source as written (spec 1.7): ``kind`` is ``path``, ``gh`` or ``git``; ``location`` is the path, the
    ``OWNER/REPO`` or the git URL as written, ``git+`` included; ``ref`` and ``subdir`` are what follows ``@`` and
    ``#``, when given."""

    kind: str
    location: str
    ref: str | None
    subdir: str | None


@dataclass(frozen=True)
class Tree:
    """A source opened: its root, the files it contributes (relative to the root, sorted), and its commit when it's
    a git work tree."""

    root: Path
    files: tuple[RepoPath, ...]
    commit: str | None


def default_source(environ: Mapping[str, str], blueprint: str | None, given: str | None) -> str:
    """The source ``new`` and ``init`` use (spec 1.7): ``--from``'s, else ``--blueprint``'s, else the one
    ``FASTRO_BLUEPRINT`` holds when it isn't empty, else ``fastapi-boilerplate``."""
    return given or blueprint or environ.get(DEFAULT_VARIABLE) or DEFAULT


def parse(text: str) -> Source:
    """``text`` as a source: a named blueprint stands for its source (spec 1.7).

    Raises:
        SourceError: ``text`` names no named blueprint and doesn't read as a source.
    """
    name, hash_, subdir_given = text.partition("#")
    if name in UNCONFIRMED:
        raise SourceError(f"{name!r} is a named blueprint whose repo isn't confirmed yet (1.7). Pass its source with --from.")
    written = f"{NAMED[name]}{hash_}{subdir_given}" if name in NAMED else text
    try:
        spec.source(written)
    except SpecError as error:
        raise SourceError(str(error).replace(repr(written), repr(text), 1)) from None
    where, _, subdir = written.partition("#")
    if where.startswith("gh:"):
        location, _, ref = where.removeprefix("gh:").partition("@")
        if any(part in (".", "..") for part in location.split("/")):
            raise SourceError(f"{text!r} isn't a source: a gh: source names OWNER/REPO, and neither is . or ..")
        kind = "gh"
    elif where.startswith(GIT_PREFIXES):
        scheme, _, rest = where.partition("://")
        authority, slash, path = rest.partition("/")
        path, at, ref = path.rpartition("@") if "@" in path else (path, "", "")
        if at and not ref:
            raise SourceError(f"{text!r} isn't a source: a ref follows @. Name one, or leave out the @.")
        location, kind = f"{scheme}://{authority}{slash}{path}", "git"
    elif where.startswith(("/", ".")):
        location, ref, kind = where, "", "path"
    else:
        raise SourceError(f"{text!r} isn't a named blueprint: use one of {', '.join(NAMED)}, or a source (1.7).")
    return Source(kind, location, ref or None, subdir or None)


def cache_dir(environ: Mapping[str, str], platform: str, home: Path) -> Path:
    """Where fetched sources are cached: ``FASTRO_CACHE_DIR``, else the platform's cache directory plus ``fastro``."""
    if given := environ.get(CACHE_VARIABLE):
        return Path(given)
    if platform == "darwin":
        return home / "Library" / "Caches" / "fastro"
    if platform == "win32":
        return Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "fastro"
    xdg = environ.get("XDG_CACHE_HOME", "")
    return (Path(xdg) if os.path.isabs(xdg) else home / ".cache") / "fastro"


def open_path(source: Source, include_untracked: bool = False) -> Tree:
    """The ``PATH`` ``source`` opened: its root is the directory that holds ``blueprint.toml`` (spec 1.7). In a git
    work tree it contributes its tracked files as the working tree holds them, with untracked ones git doesn't ignore
    when ``include_untracked`` asks, and records ``HEAD``'s commit; anywhere else it contributes every file.

    Raises:
        SourceError: the path doesn't exist or isn't a directory, ``#SUBDIR`` leads outside it, there's no
            ``blueprint.toml`` or 1.10 rules it out; git says it isn't in a work tree though a ``.git`` sits there or
            above, its work tree has no commit yet, or it doesn't contribute its ``blueprint.toml``.
        MissingToolError: it's in a git work tree, and ``git`` isn't on ``PATH``.
        OSError: the system won't let fastro look (spec 8: exit 3).
    """
    path = Path(source.location)
    if not path.exists():
        raise SourceError(f"{path} doesn't exist. Pass a blueprint repo's directory.")
    if not path.is_dir():
        raise SourceError(f"{path} isn't a directory. Pass a blueprint repo's directory.")
    root = (path / source.subdir).resolve() if source.subdir else path.resolve()
    if not root.is_relative_to(path.resolve()):
        raise SourceError(f"{path}#{source.subdir} leads outside {path}. Name a directory inside it.")
    if not root.is_dir():
        raise SourceError(f"{path}#{source.subdir} isn't a directory. Name a directory inside {path}.")
    catalog = root / CATALOG
    if not present(catalog):
        raise SourceError(f"{root} has no {CATALOG}. Pass a blueprint repo's directory.")
    if (reason := unreadable_config(root, CATALOG)) is not None:
        raise SourceError(reason)
    if not _in_work_tree(root):
        return Tree(root, _every_file(root), None)
    try:
        commit = head_commit(root)
        if commit is None:
            raise SourceError(f"{root} is in a git work tree that has no commit yet. Commit it, then run this again.")
        files = tracked_files(root, untracked=include_untracked)
    except RepositoryError as error:
        raise SourceError(str(error)) from None
    if RepoPath(CATALOG) not in files:
        fix = "Track it." if include_untracked else "Track it, or pass --include-untracked for one git doesn't track or ignore."
        raise SourceError(
            f"{catalog} isn't a file the source contributes: git doesn't track it, git ignores it, or export-ignore marks "
            f"it (1.7). {fix}"
        )
    return Tree(root, files, commit)


def _in_work_tree(root: Path) -> bool:
    """Whether ``root`` sits in a git work tree: whether it or a directory above it holds ``.git``."""
    return any(present(directory / ".git") for directory in (root, *root.parents))


def _raise(error: OSError) -> None:
    raise error


def _every_file(root: Path) -> tuple[RepoPath, ...]:
    """Every file under ``root`` and every symlink, which the plan refuses to copy (1.10), sorted by path."""
    found = []
    for directory, subdirectories, files in os.walk(root, onerror=_raise):
        base = Path(directory).relative_to(root)
        links = [name for name in subdirectories if (Path(directory) / name).is_symlink()]
        found += [(base / name).as_posix() for name in (*files, *links)]
    return tuple(RepoPath(name) for name in sorted(found))


def open_gh(
    source: Source,
    tool: Version,
    cache: Path,
    offline: bool = False,
    fetch: Fetch = lambda url, headers, limit=None: http_get(url, headers, limit),
    environ: Mapping[str, str] = os.environ,
) -> Tree:
    """The ``gh:`` ``source`` opened from the tree of its commit in ``cache``: resolved through GitHub (``resolve_gh``)
    and its tarball fetched when the cache doesn't hold that commit yet, or with ``offline``, only from the cache, where
    no ref takes the highest release that a run with no ref picked and cached, and whose catalog lets ``tool`` read it
    (spec 1.7). Its root is the directory that holds ``blueprint.toml``, and it contributes every file there.

    Raises:
        SourceError: ``FASTRO_MAX_SOURCE_MB`` doesn't read (``limits``), checked first, even with ``offline``.
        FetchError: GitHub can't be reached or answers with an error, the tarball isn't a whole gzipped tar of that
            commit, is over a limit, or has a member that doesn't stay a plain file, directory or symlink inside the
            tree (1.10), the tree has no ``blueprint.toml`` fastro may read where the source says, or ``offline``
            finds nothing in the cache.
        MissingToolError: ``git`` isn't on ``PATH`` when a release's ``.gitattributes`` needs checking.
        OSError: the system won't let fastro write the cache or look in it (spec 8: exit 3).
    """
    most = limits(environ)
    entries = cache / "gh" / source.location
    if offline:
        resolved = _cached(source, tool, entries)
    else:
        resolved = resolve_gh(source, tool, fetch, environ)
        if not _kept(entries, resolved.commit):
            label = f"gh:{source.location}@{resolved.commit}"
            url = f"{API}/repos/{source.location}/tarball/{resolved.commit}"
            body = fetch(url, _headers(environ, JSON), most.download)
            if len(body) > most.download:
                raise FetchError(
                    f"{label}'s tarball is over {_size(most.download)} downloaded, the most fastro fetches (1.10). "
                    f"Set {SIZE_VARIABLE} to raise it."
                )
            _keep(body, label, resolved.commit, entries, most)
        _remember(entries, resolved, release=source.ref is None)
    return _opened(source, resolved, entries / resolved.commit / "tree")


def _cached(source: Source, tool: Version, entries: Path) -> Resolved:
    index = _index(entries)
    catalog = f"{source.subdir}/{CATALOG}" if source.subdir else CATALOG
    if source.ref is not None:
        if source.ref not in index:
            raise FetchError(
                f"{_name(source)}@{source.ref} isn't in the cache, and --offline fetches nothing. Run it once "
                "without --offline."
            )
        return Resolved(source.ref, index[source.ref].commit)
    for tag in newest_first(ref for ref, entry in index.items() if entry.release):
        resolved = Resolved(tag, index[tag].commit)
        try:
            root = _root(source, resolved, entries / resolved.commit / "tree")
        except FetchError:
            continue
        if _readable((root / CATALOG).read_bytes(), tool):
            return resolved
    raise FetchError(
        f"the cache holds no release of {_name(source)} whose {catalog} accepts this fastro, {tool}, and --offline "
        "fetches nothing. Run it once without --offline."
    )


@dataclass(frozen=True)
class _Entry:
    commit: str
    release: bool


def _index(entries: Path) -> dict[str, _Entry]:
    """The refs ``entries`` records, each with the commit it resolved to last and whether a run with no REF picked
    it as a release, leaving out one whose commit isn't in the cache. An index that doesn't read records nothing."""
    try:
        data = json.loads((entries / INDEX).read_bytes())
    except (FileNotFoundError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    index = {}
    for ref, entry in data.items():
        commit = entry.get("commit") if isinstance(entry, dict) else None
        if isinstance(commit, str) and COMMIT.fullmatch(commit) and _kept(entries, commit):
            index[ref] = _Entry(commit, entry.get("release") is True)
    return index


def _kept(entries: Path, commit: str) -> bool:
    return (entries / commit / "tree").is_dir()


def _remember(entries: Path, resolved: Resolved, release: bool) -> None:
    index = _index(entries)
    before = index.get(resolved.ref)
    index[resolved.ref] = _Entry(resolved.commit, release or before == _Entry(resolved.commit, True))
    handle, name = mkstemp(prefix=".index-", dir=entries)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump({ref: asdict(entry) for ref, entry in index.items()}, out, ensure_ascii=False, indent=2)
        os.replace(name, entries / INDEX)
    except BaseException:
        os.unlink(name)
        raise


def _opened(source: Source, resolved: Resolved, tree: Path) -> Tree:
    root = _root(source, resolved, tree)
    return Tree(root, _every_file(root), resolved.commit)


def _root(source: Source, resolved: Resolved, tree: Path) -> Path:
    """The directory in ``tree`` that holds the source's ``blueprint.toml``, which fastro may read (1.10)."""
    label = f"{_name(source)}@{resolved.ref}" + (f"#{source.subdir}" if source.subdir else "")
    tree = tree.resolve()
    root = (tree / source.subdir).resolve() if source.subdir else tree
    if file_kind(root) != "directory":
        raise FetchError(f"{label} isn't a directory in the tree of {resolved.commit}. Name one that is.")
    if not present(root / CATALOG):
        raise FetchError(f"{label} has no {CATALOG} at {root}. Name a ref or #SUBDIR that holds one.")
    if (reason := unreadable_config(root, CATALOG)) is not None:
        raise FetchError(reason)
    return root


def _name(source: Source) -> str:
    return f"gh:{source.location}" if source.kind == "gh" else source.location


def _keep(body: bytes | Iterable[bytes], label: str, commit: str, entries: Path, most: Limits, what: str = "tarball") -> None:
    """Extract the tarball ``body`` into ``entries / commit / "tree"``, which appears whole or not at all, read as
    ``git archive`` writes it and within ``most`` (``tarball.unpack``, spec 1.10)."""
    entries.mkdir(parents=True, exist_ok=True)
    work = Path(mkdtemp(prefix=".fetch-", dir=entries))
    over = f"is over {_size(most.extracted)} extracted, the most fastro keeps (1.10). Set {SIZE_VARIABLE} to raise it."
    try:
        try:
            tarball.unpack(body, commit, work / "tree", most.extracted, over)
        except tarball.Refused as refused:
            raise FetchError(f"{label}'s {what} {refused}") from None
        try:
            work.rename(entries / commit)
        except OSError:
            if not _kept(entries, commit):
                raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def open_git(
    source: Source,
    tool: Version,
    cache: Path,
    offline: bool = False,
    environ: Mapping[str, str] = os.environ,
    say: Callable[[str], object] = lambda text: print(text, file=sys.stderr),
) -> Tree:
    """The ``git+`` ``source`` opened from the tree of its commit in ``cache``: its REF, or with no REF the tag with
    the highest version whose catalog lets ``tool`` read it, fetched alone and archived (``apply.fetch``,
    ``apply.archive``) when the cache doesn't hold that commit yet, or with ``offline``, only from the cache (spec
    1.7). A ``git+ssh://git@github.com/OWNER/REPO`` remote fastro can't list over SSH is fetched over HTTPS with
    ``GITHUB_TOKEN`` when it's set, and ``say`` reports it.

    Raises:
        SourceError: ``FASTRO_MAX_SOURCE_MB`` doesn't read (``limits``), checked first, even with ``offline``.
        FetchError: git can't list the remote or fetch the REF, the remote has no such REF or no tag this fastro can
            read, the fetch or its archive is over a limit or isn't one fastro keeps (1.10), the tree has no
            ``blueprint.toml`` fastro may read where the source says, or ``offline`` finds nothing in the cache.
        MissingToolError: ``git`` isn't on ``PATH``.
        OSError: the system won't let fastro write the cache or look in it (spec 8: exit 3).
    """
    most = limits(environ)
    entries = cache / "git" / hashlib.sha256(source.location.encode()).hexdigest()
    if offline:
        resolved = _cached(source, tool, entries)
    else:
        remote = _remote(source, environ.get(TOKEN_VARIABLE) or None, say)
        resolved = _pinned(source, remote, entries, most) if source.ref else _newest(source, tool, remote, entries, most)
        _remember(entries, resolved, release=source.ref is None)
    return _opened(source, resolved, entries / resolved.commit / "tree")


@dataclass(frozen=True)
class _Remote:
    """A ``git+`` source's remote as fastro reached it: the URL, the token sent with it, the refs it listed, and
    what SSH answered when fastro fell back to HTTPS."""

    url: str
    token: str | None
    refs: Mapping[str, str]
    ssh: str | None = None


def _remote(source: Source, token: str | None, say: Callable[[str], object]) -> _Remote:
    url = source.location.removeprefix("git+")
    sent = token if url.startswith("https://") else None
    try:
        return _Remote(url, sent, apply.remote_refs(url, sent))
    except apply.GitError as error:
        github = GITHUB_SSH.fullmatch(source.location)
        if github is None or not token:
            hint = " Set GITHUB_TOKEN to fetch it over HTTPS instead." if github else ""
            raise FetchError(f"git can't list the refs of {source.location}: {error}{hint}") from None
        ssh = str(error)
    https = f"https://github.com/{github.group(1)}"
    say(f"fastro can't list the refs of {source.location} over SSH, so it's fetching {https} with GITHUB_TOKEN instead.")
    try:
        return _Remote(https, token, apply.remote_refs(https, token), ssh)
    except apply.GitError as error:
        raise FetchError(
            f"git can't list the refs of {source.location} over SSH ({ssh}) or of {https} over HTTPS ({error})."
        ) from None


def _pinned(source: Source, remote: _Remote, entries: Path, most: Limits) -> Resolved:
    """The source's REF and its commit: a tag, else a branch, the remote lists by that name, or else REF itself when
    it's a full commit (spec 1.7), fetched unless it's kept."""
    ref = source.ref
    assert ref is not None
    name = next((name for name in (f"refs/tags/{ref}", f"refs/heads/{ref}") if name in remote.refs), None)
    if name is None and not COMMIT.fullmatch(ref):
        raise FetchError(f"{source.location} has no ref {ref!r}. Name a branch, a tag or a full commit it has.")
    commit = ref if name is None else remote.refs.get(f"{name}^{{}}", remote.refs[name])
    if not _kept(entries, commit):
        commit = _fetched(source, remote, name or ref, ref, entries, most)
    if name is None and commit != ref:
        raise FetchError(f"{source.location} answered {ref!r} with commit {commit}, which isn't that commit.")
    return Resolved(ref, commit)


def _newest(source: Source, tool: Version, remote: _Remote, entries: Path, most: Limits) -> Resolved:
    """The tag with the highest version (``newest_first``) whose tree has a ``blueprint.toml`` (under ``#SUBDIR``)
    that lets ``tool`` read it, each tag's tree fetched unless it's kept (spec 1.7)."""
    tags = {
        name.removeprefix("refs/tags/"): sha
        for name, sha in remote.refs.items()
        if name.startswith("refs/tags/") and not name.endswith("^{}")
    }
    for tag in newest_first(tags):
        commit = remote.refs.get(f"refs/tags/{tag}^{{}}", tags[tag])
        if not _kept(entries, commit):
            commit = _fetched(source, remote, f"refs/tags/{tag}", tag, entries, most)
        root = entries / commit / "tree" / (source.subdir or "")
        if file_kind(root) != "directory" or not present(root / CATALOG):
            continue
        if _readable(_root(source, Resolved(tag, commit), entries / commit / "tree").joinpath(CATALOG).read_bytes(), tool):
            return Resolved(tag, commit)
    catalog = f"{source.subdir}/{CATALOG}" if source.subdir else CATALOG
    raise FetchError(
        f"{source.location} has no tag whose {catalog} accepts this fastro, {tool}. Name a ref with @, or install a "
        "fastro its tags accept."
    )


def _fetched(source: Source, remote: _Remote, ref: str, shown: str, entries: Path, most: Limits) -> str:
    """Fetch ``ref`` alone into a temporary repo in ``entries``, keep its commit's archive there, and give the
    commit; messages name the ref as ``shown``. The temporary repo is removed whatever happens."""
    entries.mkdir(parents=True, exist_ok=True)
    work = Path(mkdtemp(prefix=".fetch-", dir=entries))
    ssh = f" SSH answered: {remote.ssh}" if remote.ssh else ""
    try:
        try:
            commit = apply.fetch(remote.url, ref, remote.token, work / "repo", most.download)
        except apply.TooBig:
            raise FetchError(
                f"{source.location}@{shown}'s fetch takes more than {_size(most.download)} on disk, the most fastro "
                f"fetches (1.10). Set {SIZE_VARIABLE} to raise it."
            ) from None
        except apply.GitError as error:
            raise FetchError(f"git can't fetch {shown} from {remote.url}: {error}{ssh}") from None
        if not _kept(entries, commit):
            label = f"{source.location}@{commit}"
            try:
                with apply.archive(work / "repo", commit) as chunks:
                    _keep(chunks, label, commit, entries, most, "archive")
            except apply.GitError as error:
                raise FetchError(f"git can't archive {commit} of {source.location}: {error}") from None
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return commit


def resolve_gh(
    source: Source,
    tool: Version,
    fetch: Fetch = lambda url, headers, limit=None: http_get(url, headers, limit),
    environ: Mapping[str, str] = os.environ,
) -> Resolved:
    """The ``gh:`` ``source`` pinned through GitHub's API (spec 1.7): its ref's full commit, or with no ref, the
    release with the highest version (``newest_first``) whose ``blueprint.toml`` (under ``#SUBDIR``) lets ``tool``
    read it. A release without one is skipped, and so is one whose ``blueprint.toml`` ``export-ignore`` marks there
    (``_export_ignored``). ``GITHUB_TOKEN`` goes in a header when it's set.

    Raises:
        FetchError: GitHub can't be reached, answers with an error or something that isn't what was asked for, or
            has no release this fastro can read.
        MissingToolError: ``git`` isn't on ``PATH`` when a release's ``.gitattributes`` needs checking.
    """
    if source.ref is not None:
        return Resolved(source.ref, _commit(source, source.ref, fetch, environ))
    catalog = f"{source.subdir}/{CATALOG}" if source.subdir else CATALOG
    for tag in newest_first(_release_tags(source, fetch, environ)):
        try:
            text = _contents(source, catalog, tag, fetch, environ)
        except NotFound:
            continue
        if _readable(text, tool) and not _export_ignored(source, tag, catalog, fetch, environ):
            return Resolved(tag, _commit(source, tag, fetch, environ))
    raise FetchError(
        f"gh:{source.location} has no release whose {catalog} accepts this fastro, {tool}. Name a ref with @, or "
        "install a fastro its releases accept."
    )


def _export_ignored(source: Source, tag: str, catalog: str, fetch: Fetch, environ: Mapping[str, str]) -> bool:
    """Whether ``export-ignore`` marks ``catalog`` at ``tag``, by the ``.gitattributes`` files there in the
    directories on its path, the only ones that can (spec 1.7)."""
    parts = catalog.split("/")[:-1]
    paths = ["/".join([*parts[:count], ".gitattributes"]) for count in range(len(parts) + 1)]
    with TemporaryDirectory(prefix="fastro-attributes-") as top:
        found = False
        for path in paths:
            try:
                data = _contents(source, path, tag, fetch, environ)
            except NotFound:
                continue
            Path(top, path).parent.mkdir(parents=True, exist_ok=True)
            Path(top, path).write_bytes(data)
            found = True
        return found and export_ignored(Path(top), catalog)


def _contents(source: Source, path: str, tag: str, fetch: Fetch, environ: Mapping[str, str]) -> bytes:
    """The file at ``path`` in ``tag``, through GitHub's contents API, as long as it's within 1 MB (spec 1.10).

    Raises:
        NotFound: the tag has no file at ``path``.
        FetchError: GitHub can't be reached or answers with another error, or the file is over 1 MB.
    """
    url = f"{API}/repos/{source.location}/contents/{quote(path)}?ref={quote(tag, safe='')}"
    body = fetch(url, _headers(environ, RAW), CONTENTS)
    if len(body) > CONTENTS:
        raise FetchError(
            f"gh:{source.location}'s {path} at {tag} is over 1 MB, the most fastro reads through GitHub's API (1.10)."
        )
    return body


def newest_first(tags: Iterable[str]) -> list[str]:
    """The release ``tags`` by version, highest first, leaving out one whose name doesn't read as a PEP 440 version
    or whose version is a pre-release or a development release; two with the same version go by name, the higher in
    code point order first (spec 1.7)."""
    versions: dict[str, Version] = {}
    for tag in tags:
        try:
            version = Version(tag)
        except InvalidVersion:
            continue
        if not version.is_prerelease:
            versions[tag] = version
    return sorted(versions, key=lambda tag: (versions[tag], tag), reverse=True)


def _release_tags(source: Source, fetch: Fetch, environ: Mapping[str, str]) -> list[str]:
    """Every tag of a release of ``source`` that isn't marked draft or pre-release, from every page of the list."""
    tags, page = [], 1
    while True:
        url = f"{API}/repos/{source.location}/releases?per_page={PAGE}&page={page}"
        try:
            releases = _json(fetch(url, _headers(environ, JSON)), source)
        except NotFound:
            raise FetchError(
                f"GitHub shows no repo gh:{source.location}. If it's private, set GITHUB_TOKEN to a token that can read it.",
                404,
            ) from None
        for release in releases:
            if isinstance(release, dict) and not release.get("draft") and not release.get("prerelease"):
                tag = release.get("tag_name")
                if isinstance(tag, str):
                    tags.append(tag)
        if len(releases) < PAGE:
            return tags
        page += 1


def _readable(data: bytes, tool: Version) -> bool:
    try:
        return readable(data.decode("utf-8"), tool)
    except UnicodeDecodeError:
        return False


def http_get(url: str, headers: Mapping[str, str], limit: int | None = None) -> bytes:
    """The body ``url`` answers with, sending ``headers``, read to at most one byte past ``limit`` when there is one.
    A redirect to another host drops ``Authorization``, so a token reaches only the host it was meant for.

    Raises:
        NotFound: the server answered 404.
        FetchError: the server answered another error, or can't be reached.
    """
    opener = urllib.request.build_opener(_SameHostAuthorization)
    try:
        with opener.open(urllib.request.Request(url, headers=dict(headers)), timeout=TIMEOUT) as response:
            body = response.read() if limit is None else _bounded(response, limit + 1)
    except urllib.error.HTTPError as error:
        kind = NotFound if error.code == 404 else FetchError
        hint = " If that's GitHub's rate limit, set GITHUB_TOKEN, or wait." if error.code in RATE_LIMITED else ""
        raise kind(f"{url} answered {error.code} {error.reason}.{hint}", error.code) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
        reason = getattr(error, "reason", None) or type(error).__name__
        raise FetchError(f"fastro can't reach {url}: {reason}. Check the network.") from None
    return body


def _bounded(response: Any, most: int) -> bytes:
    """``response``'s body, read in pieces up to ``most`` bytes.

    Raises:
        http.client.IncompleteRead: the body ended before ``most`` bytes and before its ``Content-Length``.
    """
    parts: list[bytes] = []
    count = 0
    while part := response.read(min(CHUNK, most - count)):
        parts.append(part)
        count += len(part)
    if count < most and response.length:
        raise http.client.IncompleteRead(b"".join(parts), response.length)
    return b"".join(parts)


class _SameHostAuthorization(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> urllib.request.Request | None:
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and urlsplit(newurl)[:2] != urlsplit(req.full_url)[:2]:
            redirected.remove_header("Authorization")
        return redirected


def _commit(source: Source, ref: str, fetch: Fetch, environ: Mapping[str, str]) -> str:
    url = f"{API}/repos/{source.location}/commits/{quote(ref, safe='')}"
    try:
        answer = fetch(url, _headers(environ, SHA)).decode("utf-8", "replace").strip()
    except NotFound:
        raise FetchError(
            f"GitHub shows no ref {ref!r} in gh:{source.location}. If the repo is private, set GITHUB_TOKEN to a token "
            "that can read it.",
            404,
        ) from None
    if not COMMIT.fullmatch(answer):
        raise FetchError(f"gh:{source.location} didn't answer with a full commit for {ref!r}.")
    return answer


def _json(body: bytes, source: Source) -> list[Any]:
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if not isinstance(data, list):
        raise FetchError(f"gh:{source.location} didn't answer with a list of releases.")
    return data


def _headers(environ: Mapping[str, str], accept: str) -> dict[str, str]:
    headers = {"Accept": accept, "User-Agent": "fastro", "X-GitHub-Api-Version": API_VERSION}
    if token := environ.get(TOKEN_VARIABLE):
        headers["Authorization"] = f"Bearer {token}"
    return headers
