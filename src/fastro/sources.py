"""Where a blueprint comes from (spec 1.7): reading a source, the default one, the named blueprints, the cache, and
the files a ``PATH`` source contributes."""

from __future__ import annotations

import http.client
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from packaging.version import Version

from fastro.apply import RepositoryError, head_commit, tracked_files
from fastro.engine import spec
from fastro.engine.blueprint import readable
from fastro.engine.files import present, unreadable_config
from fastro.engine.spec import RepoPath, SpecError

NAMED = {"fastapi-boilerplate": "gh:benavlabs/FastAPI-boilerplate"}
UNCONFIRMED = ("fastroai",)
DEFAULT = "fastapi-boilerplate"
DEFAULT_VARIABLE = "FASTRO_BLUEPRINT"
CACHE_VARIABLE = "FASTRO_CACHE_DIR"
CATALOG = "blueprint.toml"
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


Fetch = Callable[[str, Mapping[str, str]], bytes]


@dataclass(frozen=True)
class Resolved:
    """A ``gh:`` source pinned: the ref it resolved to, and that ref's full commit."""

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


def resolve_gh(
    source: Source,
    tool: Version,
    fetch: Fetch = lambda url, headers: http_get(url, headers),
    environ: Mapping[str, str] = os.environ,
) -> Resolved:
    """The ``gh:`` ``source`` pinned through GitHub's API (spec 1.7): its ref's full commit, or with no ref, the
    newest release, drafts and prereleases left out, whose ``blueprint.toml`` (under ``#SUBDIR``) lets ``tool`` read
    it. A release without one is skipped. ``GITHUB_TOKEN`` goes in a header when it's set.

    Raises:
        FetchError: GitHub can't be reached, answers with an error or something that isn't what was asked for, or
            has no release this fastro can read.
    """
    repo = f"{API}/repos/{source.location}"
    if source.ref is not None:
        return Resolved(source.ref, _commit(repo, source.ref, fetch, environ))
    catalog = f"{source.subdir}/{CATALOG}" if source.subdir else CATALOG
    for page in count(1):
        releases = _json(fetch(f"{repo}/releases?per_page={PAGE}&page={page}", _headers(environ, JSON)), repo)
        for release in releases:
            tag = release.get("tag_name") if isinstance(release, dict) else None
            if not tag or not isinstance(tag, str) or release.get("draft") or release.get("prerelease"):
                continue
            try:
                text = fetch(f"{repo}/contents/{catalog}?ref={quote(tag, safe='')}", _headers(environ, RAW))
            except NotFound:
                continue
            if _readable(text, tool):
                return Resolved(tag, _commit(repo, tag, fetch, environ))
        if len(releases) < PAGE:
            break
    raise FetchError(
        f"gh:{source.location} has no release whose {catalog} accepts this fastro, {tool}. Name a ref with @, or "
        "install a fastro its releases accept."
    )


def _readable(data: bytes, tool: Version) -> bool:
    try:
        return readable(data.decode("utf-8"), tool)
    except UnicodeDecodeError:
        return False


def http_get(url: str, headers: Mapping[str, str]) -> bytes:
    """The body ``url`` answers with, sending ``headers``. A redirect to another host drops ``Authorization``, so a
    token reaches only the host it was meant for.

    Raises:
        NotFound: the server answered 404.
        FetchError: the server answered another error, or can't be reached.
    """
    opener = urllib.request.build_opener(_SameHostAuthorization)
    try:
        with opener.open(urllib.request.Request(url, headers=dict(headers)), timeout=TIMEOUT) as response:
            body: bytes = response.read()
    except urllib.error.HTTPError as error:
        kind = NotFound if error.code == 404 else FetchError
        hint = " If that's GitHub's rate limit, set GITHUB_TOKEN, or wait." if error.code in RATE_LIMITED else ""
        raise kind(f"{url} answered {error.code} {error.reason}.{hint}", error.code) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
        reason = getattr(error, "reason", None) or type(error).__name__
        raise FetchError(f"fastro can't reach {url}: {reason}. Check the network.") from None
    return body


class _SameHostAuthorization(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> urllib.request.Request | None:
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and urlsplit(newurl)[:2] != urlsplit(req.full_url)[:2]:
            redirected.remove_header("Authorization")
        return redirected


def _commit(repo: str, ref: str, fetch: Fetch, environ: Mapping[str, str]) -> str:
    answer = fetch(f"{repo}/commits/{quote(ref, safe='')}", _headers(environ, SHA)).decode("utf-8", "replace").strip()
    if not COMMIT.fullmatch(answer):
        raise FetchError(f"{repo} didn't answer with a full commit for {ref!r}.")
    return answer


def _json(body: bytes, repo: str) -> list[Any]:
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if not isinstance(data, list):
        raise FetchError(f"{repo} didn't answer with a list of releases.")
    return data


def _headers(environ: Mapping[str, str], accept: str) -> dict[str, str]:
    headers = {"Accept": accept, "User-Agent": "fastro", "X-GitHub-Api-Version": API_VERSION}
    if token := environ.get(TOKEN_VARIABLE):
        headers["Authorization"] = f"Bearer {token}"
    return headers
