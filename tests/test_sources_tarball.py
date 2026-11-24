"""Fetching a ``gh:`` source (spec 1.7, 1.10): the tarball of its commit, extracted into the cache only when every
member stays inside the tree, kept per commit, and read back with ``--offline`` without asking GitHub anything."""

import gzip
import hashlib
import io
import json
import os
import stat
import struct
import subprocess
import tarfile
import zlib
from collections.abc import Mapping
from pathlib import Path

import pytest
from packaging.version import Version

from fastro import sources, tarball
from fastro.engine.spec import RepoPath
from fastro.sources import FetchError, Limits, NotFound, SourceError, Tree, _keep, open_gh, parse

TOOL = Version("0.1.0.dev1")
API = "https://api.github.com/repos/acme/bp"
CATALOG = '[blueprint]\nname = "bp"\nversion = "0.21.0"\nschema = 1\nrequires_fastro = "{requires}"\n'


def _git(repo: Path, *arguments: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True).stdout


def _archive(tmp_path: Path, requires: str = ">=0.1.0.dev0") -> tuple[bytes, str]:
    """A blueprint repo's tarball as GitHub builds it, with ``git archive``, and its commit."""
    repo = tmp_path / "upstream"
    (repo / "src").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "blueprint.toml").write_text(CATALOG.format(requires=requires))
    (repo / "src" / "app.py").write_text("print('hi')\n")
    (repo / "run.sh").write_text("#!/bin/sh\n")
    (repo / "run.sh").chmod(0o755)
    (repo / "docs" / "index.md").write_text("# docs\n")
    (repo / ".gitattributes").write_text("docs/ export-ignore\n")
    (repo / "app.py").symlink_to("src/app.py")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "x")
    commit = _git(repo, "rev-parse", "HEAD").decode().strip()
    return _git(repo, "archive", "--format=tar.gz", f"--prefix=bp-{commit}/", "HEAD"), commit


Member = tuple[str, str, str]


def _tarball(commit: str, *members: Member, comment: str | None = None, parents: bool = True) -> bytes:
    """A tarball holding ``members``, each ``(name, kind, data)``: a kind of ``file``, ``dir``, ``symlink`` or
    ``hardlink`` (data is the target), ``fifo`` or ``chr``; the global header's comment is ``commit``. Each
    directory a member sits in under the first member is listed before it, as ``git archive`` lists them."""
    out = io.BytesIO()
    headers = {"comment": comment if comment is not None else commit}
    listed: list[Member] = []
    for name, kind, data in members:
        parts = name.split("/")
        if parents and listed and parts[0] == listed[0][0] and all(part not in ("", ".", "..") for part in parts):
            for depth in range(2, len(parts)):
                parent = "/".join(parts[:depth])
                if all(parent != seen for seen, _, _ in listed):
                    listed.append((parent, "dir", ""))
        listed.append((name, kind, data))
    with tarfile.open(fileobj=out, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers=headers) as archive:
        for name, kind, data in listed:
            info = tarfile.TarInfo(name)
            info.type = {
                "file": tarfile.REGTYPE,
                "dir": tarfile.DIRTYPE,
                "symlink": tarfile.SYMTYPE,
                "hardlink": tarfile.LNKTYPE,
                "fifo": tarfile.FIFOTYPE,
                "chr": tarfile.CHRTYPE,
            }[kind]
            payload = data.encode() if kind == "file" else b""
            if kind in ("symlink", "hardlink"):
                info.linkname = data
            info.size = len(payload)
            info.mode = 0o755 if kind == "dir" else 0o644
            archive.addfile(info, io.BytesIO(payload))
    return out.getvalue()


def _good(commit: str, *extra: Member) -> bytes:
    top = f"bp-{commit}"
    return _tarball(commit, (top, "dir", ""), (f"{top}/blueprint.toml", "file", CATALOG.format(requires=">=0")), *extra)


class Fake:
    """GitHub as a mapping of URL to body; a URL it doesn't hold answers 404. It records every URL asked for."""

    def __init__(self, answers: Mapping[str, bytes]) -> None:
        self.answers = dict(answers)
        self.asked: list[tuple[str, dict[str, str]]] = []
        self.limits: list[int | None] = []

    def __call__(self, url: str, headers: Mapping[str, str], limit: int | None = None) -> bytes:
        self.asked.append((url, dict(headers)))
        self.limits.append(limit)
        if url not in self.answers:
            raise NotFound(f"{url} answered 404 Not Found", 404)
        return self.answers[url]


def _pinned(ref: str, commit: str, tarball: bytes) -> Fake:
    return Fake({f"{API}/commits/{ref}": commit.encode(), f"{API}/tarball/{commit}": tarball})


def _offline(url: str, headers: Mapping[str, str], limit: int | None = None) -> bytes:
    raise AssertionError(f"--offline asked for {url}")


LIMITS = Limits(200 * 10**6, 10**9)


def _entries(cache: Path) -> Path:
    return cache / "gh" / "acme" / "bp"


def test_a_ref_s_tarball_is_fetched_into_the_cache_and_opened_there(tmp_path: Path) -> None:
    tarball, commit = _archive(tmp_path)
    cache = tmp_path / "cache"
    fake = _pinned("v0.21.0", commit, tarball)

    tree = open_gh(parse("gh:acme/bp@v0.21.0"), TOOL, cache, fetch=fake, environ={"GITHUB_TOKEN": "s3cret"})

    root = _entries(cache) / commit / "tree"
    assert tree == Tree(
        root, tuple(RepoPath(name) for name in (".gitattributes", "app.py", "blueprint.toml", "run.sh", "src/app.py")), commit
    )
    assert sorted(os.listdir(root)) == [".gitattributes", "app.py", "blueprint.toml", "run.sh", "src"]
    assert (root / "src" / "app.py").read_text() == "print('hi')\n"
    assert os.readlink(root / "app.py") == "src/app.py"
    assert os.stat(root / "run.sh").st_mode & stat.S_IXUSR
    assert not os.stat(root / "blueprint.toml").st_mode & stat.S_IXUSR
    assert json.loads((_entries(cache) / "refs.json").read_text()) == {"v0.21.0": {"commit": commit, "release": False}}
    assert sorted(os.listdir(_entries(cache))) == [commit, "refs.json"]
    url, headers = fake.asked[-1]
    assert (url, headers["Authorization"]) == (f"{API}/tarball/{commit}", "Bearer s3cret")
    assert all("s3cret" not in path.read_bytes().decode(errors="replace") for path in cache.rglob("*") if path.is_file())


def test_a_commit_already_in_the_cache_isn_t_fetched_again(tmp_path: Path) -> None:
    tarball, commit = _archive(tmp_path)
    cache = tmp_path / "cache"
    first = open_gh(parse("gh:acme/bp@v0.21.0"), TOOL, cache, fetch=_pinned("v0.21.0", commit, tarball), environ={})
    again = Fake({f"{API}/commits/main": commit.encode()})

    assert open_gh(parse("gh:acme/bp@main"), TOOL, cache, fetch=again, environ={}) == first
    assert [url for url, _ in again.asked] == [f"{API}/commits/main"]
    assert json.loads((_entries(cache) / "refs.json").read_text()) == {
        "v0.21.0": {"commit": commit, "release": False},
        "main": {"commit": commit, "release": False},
    }


def test_with_no_ref_the_resolved_release_s_tarball_is_fetched(tmp_path: Path) -> None:
    tarball, commit = _archive(tmp_path)
    fake = _pinned("v0.21.0", commit, tarball)
    fake.answers[f"{API}/releases?per_page=100&page=1"] = json.dumps([{"tag_name": "v0.21.0"}]).encode()
    fake.answers[f"{API}/contents/blueprint.toml?ref=v0.21.0"] = CATALOG.format(requires=">=0").encode()

    assert open_gh(parse("gh:acme/bp"), TOOL, tmp_path / "cache", fetch=fake, environ={}).commit == commit


def test_the_root_is_the_source_s_subdirectory_in_the_tree(tmp_path: Path) -> None:
    commit = "c" * 40
    top = f"bp-{commit}"
    tarball = _tarball(
        commit, (top, "dir", ""), (f"{top}/t/api/blueprint.toml", "file", "x"), (f"{top}/t/api/a.py", "file", "")
    )

    tree = open_gh(parse("gh:acme/bp@v1#t/api"), TOOL, tmp_path, fetch=_pinned("v1", commit, tarball), environ={})

    assert (tree.root, tree.files) == (
        _entries(tmp_path) / commit / "tree" / "t" / "api",
        (RepoPath("a.py"), RepoPath("blueprint.toml")),
    )


@pytest.mark.parametrize(
    ("source", "members", "message"),
    [
        ("gh:acme/bp@v1", [], r"gh:acme/bp@v1 has no blueprint\.toml"),
        ("gh:acme/bp@v1#t", [], r"gh:acme/bp@v1#t isn't a directory in the tree"),
        ("gh:acme/bp@v1#t", [("t", "file", "")], r"gh:acme/bp@v1#t isn't a directory in the tree"),
        ("gh:acme/bp@v1", [("blueprint.toml", "symlink", "x.toml")], "fastro reads blueprint.toml only as a regular file"),
        ("gh:acme/bp@v1", [("blueprint.toml", "dir", "")], "isn't a regular file"),
    ],
)
def test_a_tree_without_a_readable_catalog_at_its_root_is_a_fetch_failure(
    tmp_path: Path, source: str, members: list[Member], message: str
) -> None:
    commit = "c" * 40
    top = f"bp-{commit}"
    tarball = _tarball(commit, (top, "dir", ""), *[(f"{top}/{name}", kind, data) for name, kind, data in members])

    with pytest.raises(FetchError, match=message):
        open_gh(parse(source), TOOL, tmp_path, fetch=_pinned("v1", commit, tarball), environ={})


@pytest.mark.parametrize(
    ("members", "message"),
    [
        ([("/etc/x", "file", "")], r"'/etc/x', which leads outside the tree"),
        ([("TOP/../x", "file", "")], r"'TOP/\.\./x', which leads outside the tree"),
        ([("TOP/./x", "file", "")], r"'TOP/\./x', which leads outside the tree"),
        ([("TOP//x", "file", "")], r"'TOP//x', which has an empty name in it"),
        ([("other/x", "file", "")], r"'other/x', which isn't under the tarball's one top directory"),
        ([("TOP/h", "hardlink", "TOP/blueprint.toml")], r"'TOP/h', which is a hard link"),
        ([("TOP/f", "fifo", "")], r"'TOP/f', which is a FIFO"),
        ([("TOP/c", "chr", "")], r"'TOP/c', which is a device"),
        ([("TOP/l", "symlink", "/etc/passwd")], r"'TOP/l', which is a symlink that leads outside the tree"),
        ([("TOP/d/l", "symlink", "../../x")], r"'TOP/d/l', which is a symlink that leads outside the tree"),
        ([("TOP/l", "symlink", "")], r"'TOP/l', which is a symlink with an empty target"),
        (
            [("TOP/d/a", "symlink", ".."), ("TOP/d/x", "symlink", "a/../..")],
            r"'TOP/d/x', which is a symlink that leads outside",
        ),
        (
            [("TOP/l", "symlink", "s"), ("TOP/s", "dir", ""), ("TOP/l/f", "file", "")],
            r"'TOP/l/f', which isn't in a directory listed before it",
        ),
        ([("TOP/blueprint.toml", "file", "again")], r"'TOP/blueprint\.toml', which is there twice"),
        ([("TOP/blueprint.toml", "dir", "")], r"'TOP/blueprint\.toml/', which is there twice"),
        ([("TOP/x", "file", ""), ("TOP/x", "symlink", "blueprint.toml")], r"'TOP/x', which is there twice"),
        ([("TOP/d", "dir", ""), ("TOP/d", "dir", "")], r"'TOP/d/', which is there twice"),
    ],
)
def test_a_member_that_doesn_t_stay_a_plain_file_inside_the_tree_fails_the_fetch_and_nothing_is_kept(
    tmp_path: Path, members: list[Member], message: str
) -> None:
    commit = "c" * 40
    tarball = _good(
        commit,
        *[(name.replace("TOP", f"bp-{commit}"), kind, data.replace("TOP", f"bp-{commit}")) for name, kind, data in members],
    )
    outside = tmp_path / "x"

    with pytest.raises(FetchError, match=f"gh:acme/bp@{commit}'s tarball has {message.replace('TOP', f'bp-{commit}')}"):
        open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path / "cache", fetch=_pinned("v1", commit, tarball), environ={})

    assert os.listdir(_entries(tmp_path / "cache")) == []
    assert not os.path.lexists(outside)


def test_a_top_directory_that_isn_t_a_directory_fails_the_fetch(tmp_path: Path) -> None:
    commit = "c" * 40
    tarball = _tarball(commit, (f"bp-{commit}", "file", ""))

    with pytest.raises(FetchError, match=f"'bp-{commit}', which isn't under the tarball's one top directory"):
        open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path, fetch=_pinned("v1", commit, tarball), environ={})


@pytest.mark.parametrize("comment", ["d" * 40, ""])
def test_a_tarball_of_another_commit_fails_the_fetch(tmp_path: Path, comment: str) -> None:
    commit = "c" * 40
    tarball = _tarball(commit, (f"bp-{commit}", "dir", ""), comment=comment)

    with pytest.raises(FetchError, match=f"gh:acme/bp@{commit}'s tarball isn't of that commit"):
        open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path, fetch=_pinned("v1", commit, tarball), environ={})

    assert os.listdir(_entries(tmp_path)) == []


@pytest.mark.parametrize(
    "damage", ["not gzip", "cut", "corrupt deflate", "tar cut mid-member", "wrong crc", "bytes after the gzip stream"]
)
def test_a_body_that_isn_t_a_whole_gzipped_tarball_fails_the_fetch(tmp_path: Path, damage: str) -> None:
    tarball, commit = _archive(tmp_path)
    deflate = bytearray(tarball)
    deflate[20:40] = bytes(byte ^ 0xFF for byte in deflate[20:40])
    big = _tarball(commit, (f"bp-{commit}", "dir", ""), (f"bp-{commit}/big", "file", "x" * 10000))
    body = {
        "not gzip": b"not a tarball",
        "cut": tarball[: len(tarball) // 2],
        "corrupt deflate": bytes(deflate),
        "tar cut mid-member": gzip.compress(gzip.decompress(big)[:4096]),
        "wrong crc": tarball[:-8] + bytes(byte ^ 0xFF for byte in tarball[-8:-4]) + tarball[-4:],
        "bytes after the gzip stream": tarball + bytes(512),
    }[damage]

    with pytest.raises(FetchError, match=f"gh:acme/bp@{commit}'s tarball isn't a whole gzipped tar"):
        open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path / "cache", fetch=_pinned("v1", commit, body), environ={})

    assert os.listdir(_entries(tmp_path / "cache")) == []


def test_offline_opens_a_cached_ref_without_asking_github(tmp_path: Path) -> None:
    tarball, commit = _archive(tmp_path)
    cache = tmp_path / "cache"
    fetched = open_gh(parse("gh:acme/bp@v0.21.0"), TOOL, cache, fetch=_pinned("v0.21.0", commit, tarball), environ={})
    index = (_entries(cache) / "refs.json").read_bytes(), os.stat(_entries(cache) / "refs.json").st_ino

    assert open_gh(parse("gh:acme/bp@v0.21.0"), TOOL, cache, offline=True, fetch=_offline, environ={}) == fetched
    assert ((_entries(cache) / "refs.json").read_bytes(), os.stat(_entries(cache) / "refs.json").st_ino) == index


@pytest.mark.parametrize(
    "index",
    [
        None,
        "not json",
        '["v1"]',
        '{"v1": 5}',
        '{"v1": {"commit": 5}}',
        '{"v1": {"release": true}}',
        '{"v1": "' + "e" * 40 + '"}',
        '{"v1": {"commit": "' + "e" * 40 + '", "release": true}}',
    ],
)
def test_offline_with_a_ref_the_cache_doesn_t_hold_is_a_fetch_failure(tmp_path: Path, index: str | None) -> None:
    if index is not None:
        _entries(tmp_path).mkdir(parents=True)
        (_entries(tmp_path) / "refs.json").write_text(index)

    with pytest.raises(
        FetchError, match=r"gh:acme/bp@v1 isn't in the cache, and --offline fetches nothing\. Run it once without --offline"
    ):
        open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path, offline=True, fetch=_offline, environ={})


def _cached(cache: Path, ref: str, requires: str, release: bool = True) -> str:
    """Cache ``ref`` with a catalog that ``requires`` a fastro: as the release a run with no REF picked, by a fastro
    new enough for it, or with ``release`` false, as an explicit ``@REF``."""
    commit = hashlib.sha1(ref.encode()).hexdigest()
    tarball = _tarball(
        commit, (f"bp-{commit}", "dir", ""), (f"bp-{commit}/blueprint.toml", "file", CATALOG.format(requires=requires))
    )
    fake = _pinned(ref, commit, tarball)
    fake.answers[f"{API}/releases?per_page=100&page=1"] = json.dumps([{"tag_name": ref}]).encode()
    fake.answers[f"{API}/contents/blueprint.toml?ref={ref}"] = CATALOG.format(requires=requires).encode()
    source = parse("gh:acme/bp" if release else f"gh:acme/bp@{ref}")
    open_gh(source, Version("99"), cache, fetch=fake, environ={})
    return commit


def test_offline_with_no_ref_opens_the_highest_cached_release_whose_catalog_accepts_this_fastro(tmp_path: Path) -> None:
    _cached(tmp_path, "v0.21.0", ">=0")
    wanted = _cached(tmp_path, "v0.22.0", ">=0")
    _cached(tmp_path, "v0.23.0", ">=9")
    _cached(tmp_path, "v0.24.0rc1", ">=0", release=False)
    _cached(tmp_path, "v0.25.0", ">=0", release=False)
    _cached(tmp_path, "main", ">=0", release=False)
    index = (_entries(tmp_path) / "refs.json").read_bytes(), os.stat(_entries(tmp_path) / "refs.json").st_ino

    tree = open_gh(parse("gh:acme/bp"), TOOL, tmp_path, offline=True, fetch=_offline, environ={})

    assert tree.commit == wanted
    assert ((_entries(tmp_path) / "refs.json").read_bytes(), os.stat(_entries(tmp_path) / "refs.json").st_ino) == index


def test_offline_with_no_cached_release_this_fastro_reads_is_a_fetch_failure(tmp_path: Path) -> None:
    _cached(tmp_path, "v0.23.0", ">=9")
    _cached(tmp_path, "v0.22.0", ">=0", release=False)

    with pytest.raises(
        FetchError, match=r"the cache holds no release of gh:acme/bp whose blueprint\.toml accepts this fastro, 0\.1\.0\.dev1"
    ):
        open_gh(parse("gh:acme/bp"), TOOL, tmp_path, offline=True, fetch=_offline, environ={})


def test_a_member_listed_before_its_directory_fails_the_fetch_and_nothing_is_kept(tmp_path: Path) -> None:
    commit = "c" * 40
    top = f"bp-{commit}"
    body = _tarball(commit, (top, "dir", ""), (f"{top}/d/x", "file", "x"), parents=False)
    message = f"gh:acme/bp@{commit}'s tarball has '{top}/d/x', which isn't in a directory listed before it"

    with pytest.raises(FetchError, match=message):
        open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path, fetch=_pinned("v1", commit, body), environ={})
    assert os.listdir(_entries(tmp_path)) == []


def test_a_commit_another_run_kept_first_is_left_as_it_is(tmp_path: Path) -> None:
    commit = "c" * 40
    (tmp_path / commit / "tree").mkdir(parents=True)
    (tmp_path / commit / "tree" / "theirs").write_text("")

    _keep(_good(commit), "gh:acme/bp@v1", commit, tmp_path, LIMITS)

    assert sorted(os.listdir(tmp_path)) == [commit]
    assert os.listdir(tmp_path / commit / "tree") == ["theirs"]


def test_a_cache_entry_fastro_can_t_put_in_place_is_an_os_error_and_nothing_else_is_kept(tmp_path: Path) -> None:
    commit = "c" * 40
    (tmp_path / commit).write_text("")

    with pytest.raises(OSError):
        _keep(_good(commit), "gh:acme/bp@v1", commit, tmp_path, LIMITS)

    assert os.listdir(tmp_path) == [commit]


def test_a_symlink_to_an_absolute_path_fails_the_fetch_even_when_it_points_inside_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit = "c" * 40
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr("fastro.sources.mkdtemp", lambda prefix, dir: str(work))
    tarball = _good(commit, (f"bp-{commit}/l", "symlink", str(work / "tree" / "blueprint.toml")))

    with pytest.raises(FetchError, match="'bp-c+/l', which is a symlink that leads outside the tree"):
        open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path / "cache", fetch=_pinned("v1", commit, tarball), environ={})


def test_offline_skips_a_cached_release_whose_tree_has_no_catalog(tmp_path: Path) -> None:
    wanted = _cached(tmp_path, "v0.22.0", ">=0")
    gone = _cached(tmp_path, "v0.23.0", ">=0")
    (_entries(tmp_path) / gone / "tree" / "blueprint.toml").unlink()

    assert open_gh(parse("gh:acme/bp"), TOOL, tmp_path, offline=True, fetch=_offline, environ={}).commit == wanted


def test_offline_never_follows_a_recorded_commit_that_isn_t_a_full_sha(tmp_path: Path) -> None:
    (tmp_path / "gh" / "acme" / "evil" / "tree").mkdir(parents=True)
    _entries(tmp_path).mkdir(parents=True)
    (_entries(tmp_path) / "refs.json").write_text('{"v1": {"commit": "../evil", "release": true}}')

    with pytest.raises(FetchError, match="isn't in the cache"):
        open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path, offline=True, fetch=_offline, environ={})


def test_the_root_is_resolved_through_a_symlinked_cache(tmp_path: Path) -> None:
    tarball, commit = _archive(tmp_path)
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to("real")

    tree = open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path / "link", fetch=_pinned("v1", commit, tarball), environ={})

    assert tree.root == _entries(tmp_path / "real") / commit / "tree"


def test_offline_with_no_ref_takes_only_releases_a_run_with_no_ref_picked(tmp_path: Path) -> None:
    wanted = _cached(tmp_path, "v0.22.0", ">=0")
    _cached(tmp_path, "v0.23.0", ">=0", release=False)

    assert open_gh(parse("gh:acme/bp"), TOOL, tmp_path, offline=True, fetch=_offline, environ={}).commit == wanted


def test_fetching_a_picked_release_again_by_its_ref_keeps_it_a_release(tmp_path: Path) -> None:
    wanted = _cached(tmp_path, "v0.22.0", ">=0")
    _cached(tmp_path, "v0.22.0", ">=0", release=False)

    assert json.loads((_entries(tmp_path) / "refs.json").read_text()) == {"v0.22.0": {"commit": wanted, "release": True}}
    assert open_gh(parse("gh:acme/bp"), TOOL, tmp_path, offline=True, fetch=_offline, environ={}).commit == wanted


def test_an_entry_without_a_tree_is_fetched_again(tmp_path: Path) -> None:
    tarball, commit = _archive(tmp_path)
    (_entries(tmp_path / "cache") / commit).mkdir(parents=True)

    tree = open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path / "cache", fetch=_pinned("v1", commit, tarball), environ={})

    assert RepoPath("blueprint.toml") in tree.files


def test_an_index_fastro_can_t_put_in_place_leaves_no_temporary_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tarball, commit = _archive(tmp_path)

    def refused(source: str, target: Path) -> None:
        raise PermissionError(13, "refused")

    monkeypatch.setattr("fastro.sources.os.replace", refused)
    with pytest.raises(PermissionError):
        open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path / "cache", fetch=_pinned("v1", commit, tarball), environ={})

    assert os.listdir(_entries(tmp_path / "cache")) == [commit]


@pytest.mark.parametrize(
    "entry", ['{"commit": "COMMIT"}', '{"commit": "COMMIT", "release": "yes"}', '{"commit": "COMMIT", "release": 1}']
)
def test_an_index_entry_counts_as_a_release_only_when_it_says_true(tmp_path: Path, entry: str) -> None:
    commit = _cached(tmp_path, "v0.22.0", ">=0")
    (_entries(tmp_path) / "refs.json").write_text('{"v0.22.0": ' + entry.replace("COMMIT", commit) + "}")

    with pytest.raises(FetchError, match="the cache holds no release of gh:acme/bp"):
        open_gh(parse("gh:acme/bp"), TOOL, tmp_path, offline=True, fetch=_offline, environ={})


def test_a_picked_release_whose_tag_moved_and_is_fetched_by_its_ref_is_no_longer_a_release(tmp_path: Path) -> None:
    _cached(tmp_path, "v0.22.0", ">=0")
    moved = "b" * 40
    tarball = _tarball(
        moved, (f"bp-{moved}", "dir", ""), (f"bp-{moved}/blueprint.toml", "file", CATALOG.format(requires=">=0"))
    )
    open_gh(parse("gh:acme/bp@v0.22.0"), TOOL, tmp_path, fetch=_pinned("v0.22.0", moved, tarball), environ={})

    assert json.loads((_entries(tmp_path) / "refs.json").read_text())["v0.22.0"] == {"commit": moved, "release": False}


@pytest.mark.parametrize(("requires", "opens"), [(">=0", True), (">=9", False)])
def test_offline_with_no_ref_reads_the_catalog_under_the_source_s_subdirectory(
    tmp_path: Path, requires: str, opens: bool
) -> None:
    commit = "d" * 40
    catalog = CATALOG.format(requires=requires)
    tarball = _tarball(commit, (f"bp-{commit}", "dir", ""), (f"bp-{commit}/t/blueprint.toml", "file", catalog))
    fake = _pinned("v1.0.0", commit, tarball)
    fake.answers[f"{API}/releases?per_page=100&page=1"] = json.dumps([{"tag_name": "v1.0.0"}]).encode()
    fake.answers[f"{API}/contents/t/blueprint.toml?ref=v1.0.0"] = catalog.encode()
    open_gh(parse("gh:acme/bp#t"), Version("99"), tmp_path, fetch=fake, environ={})

    if opens:
        tree = open_gh(parse("gh:acme/bp#t"), TOOL, tmp_path, offline=True, fetch=_offline, environ={})
        assert tree.root == _entries(tmp_path) / commit / "tree" / "t"
    else:
        with pytest.raises(FetchError, match=r"the cache holds no release of gh:acme/bp whose t/blueprint\.toml accepts"):
            open_gh(parse("gh:acme/bp#t"), TOOL, tmp_path, offline=True, fetch=_offline, environ={})


def _sized_body(raw: bytes, size: int) -> bytes:
    """``raw`` gzipped into exactly ``size`` bytes, the gzip header's file name taking up the room."""
    compress = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    deflated = compress.compress(raw) + compress.flush()
    trailer = struct.pack("<II", zlib.crc32(raw), len(raw))
    name = b"x" * (size - 10 - len(deflated) - len(trailer) - 1)
    return b"\x1f\x8b\x08\x08" + bytes(4) + b"\x02\x03" + name + b"\0" + deflated + trailer


def _sized(commit: str, *sizes: int) -> bytes:
    """A tarball of a readable catalog and one file of each of ``sizes`` bytes, with the catalog's own size taken
    off the first, so the members' sizes add up to ``sum(sizes)``."""
    top = f"bp-{commit}"
    catalog = CATALOG.format(requires=">=0")
    payloads = [sizes[0] - len(catalog), *sizes[1:]]
    return _good(commit, *[(f"{top}/f{index}", "file", "x" * size) for index, size in enumerate(payloads)])


@pytest.fixture
def small(monkeypatch: pytest.MonkeyPatch) -> None:
    """The limits scaled down: a megabyte of two bytes, so 200 MB is 400 bytes and 1 GB is 2000, and 4 members."""
    monkeypatch.setattr(sources, "MB", 2)
    monkeypatch.setattr(tarball, "MEMBERS", 4)


def test_the_tarball_is_asked_for_with_the_download_limit_and_nothing_else_is(tmp_path: Path) -> None:
    commit = "c" * 40
    fake = _pinned("v1", commit, _good(commit))

    open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path, fetch=fake, environ={})

    assert fake.limits == [None, 200 * 10**6]


@pytest.mark.usefixtures("small")
@pytest.mark.parametrize(
    ("environ", "size", "kept"),
    [
        ({}, 400, True),
        ({}, 401, False),
        ({"FASTRO_MAX_SOURCE_MB": "300"}, 600, True),
        ({"FASTRO_MAX_SOURCE_MB": "300"}, 601, False),
    ],
)
def test_a_tarball_over_the_download_limit_fails_the_fetch_and_nothing_is_kept(
    tmp_path: Path, environ: dict[str, str], size: int, kept: bool
) -> None:
    commit = "c" * 40
    body = _sized_body(gzip.decompress(_good(commit)), size)
    assert len(body) == size
    fake = _pinned("v1", commit, body)
    limit = environ.get("FASTRO_MAX_SOURCE_MB", "200")

    if kept:
        assert open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path, fetch=fake, environ=environ).commit == commit
    else:
        message = (
            f"gh:acme/bp@{commit}'s tarball is over {limit} MB downloaded, the most fastro fetches \\(1.10\\)\\. "
            "Set FASTRO_MAX_SOURCE_MB to raise it."
        )
        with pytest.raises(FetchError, match=message):
            open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path, fetch=fake, environ=environ)
        assert not _entries(tmp_path).exists() or os.listdir(_entries(tmp_path)) == []
    assert fake.limits[-1] == 2 * int(limit)


@pytest.mark.usefixtures("small")
@pytest.mark.parametrize(
    ("environ", "sizes", "over"),
    [
        ({}, (1200, 800), None),
        ({}, (1200, 801), "1 GB"),
        ({"FASTRO_MAX_SOURCE_MB": "201"}, (2010,), None),
        ({"FASTRO_MAX_SOURCE_MB": "201"}, (2011,), "1005 MB"),
        ({"FASTRO_MAX_SOURCE_MB": "400"}, (2000, 2000), None),
        ({"FASTRO_MAX_SOURCE_MB": "400"}, (2000, 2001), "2 GB"),
    ],
)
def test_a_tarball_over_the_extracted_limit_fails_the_fetch_and_nothing_is_kept(
    tmp_path: Path, environ: dict[str, str], sizes: tuple[int, ...], over: str | None
) -> None:
    commit = "c" * 40
    fake = _pinned("v1", commit, _sized(commit, *sizes))

    if over is None:
        assert open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path, fetch=fake, environ=environ).commit == commit
    else:
        message = (
            f"gh:acme/bp@{commit}'s tarball is over {over} extracted, the most fastro keeps \\(1.10\\)\\. "
            "Set FASTRO_MAX_SOURCE_MB to raise it."
        )
        with pytest.raises(FetchError, match=message):
            open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path, fetch=fake, environ=environ)
        assert os.listdir(_entries(tmp_path)) == []


@pytest.mark.usefixtures("small")
@pytest.mark.parametrize(("files", "environ"), [(2, {}), (3, {}), (3, {"FASTRO_MAX_SOURCE_MB": "1000"})])
def test_a_tarball_with_more_members_than_the_limit_fails_the_fetch_and_nothing_is_kept(
    tmp_path: Path, files: int, environ: dict[str, str]
) -> None:
    commit = "c" * 40
    top = f"bp-{commit}"
    fake = _pinned("v1", commit, _good(commit, *[(f"{top}/f{index}", "file", "") for index in range(files)]))

    if files == 2:
        assert open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path, fetch=fake, environ=environ).commit == commit
    else:
        with pytest.raises(FetchError, match=f"gh:acme/bp@{commit}'s tarball has more than 4 members, the most fastro keeps"):
            open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path, fetch=fake, environ=environ)
        assert os.listdir(_entries(tmp_path)) == []


def test_the_member_limit_is_written_with_a_thousands_separator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tarball, "MEMBERS", 50_000)
    commit = "c" * 40
    top = f"bp-{commit}"
    body = _good(commit, *[(f"{top}/d{index}", "dir", "") for index in range(49_999)])

    with pytest.raises(FetchError, match=f"gh:acme/bp@{commit}'s tarball has more than 50,000 members"):
        open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path, fetch=_pinned("v1", commit, body), environ={})


@pytest.mark.parametrize(
    "value",
    [
        "199",
        "0",
        "abc",
        "2e2",
        " 300",
        "300 ",
        "300\n",
        "-300",
        "+300",
        "300.0",
        "\u0663\u0660\u0660",
        "300MB",
        "0" * 5000,
        "0" * 5000 + "199",
    ],
)
def test_a_max_source_mb_that_isn_t_a_whole_number_of_200_or_more_is_a_usage_error_before_anything_is_asked(
    tmp_path: Path, value: str
) -> None:
    commit = "c" * 40
    open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path, fetch=_pinned("v1", commit, _good(commit)), environ={})
    environ = {"FASTRO_MAX_SOURCE_MB": value}
    message = f"FASTRO_MAX_SOURCE_MB is {value!r}. Set it to a whole number of MB, 200 or more, or unset it."

    for offline in (False, True):
        with pytest.raises(SourceError) as caught:
            open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path, offline=offline, fetch=_offline, environ=environ)
        assert str(caught.value) == message


@pytest.mark.parametrize(
    ("value", "megabytes"),
    [
        ("", 200),
        ("200", 200),
        ("0200", 200),
        ("1000", 1000),
        ("999999999999", 999_999_999_999),
        ("1000000000000", 10**12),
        ("9" * 5000, 10**12),
        ("0" * 5000 + "300", 300),
    ],
)
def test_a_max_source_mb_that_s_empty_or_a_whole_number_of_200_or_more_is_read(
    tmp_path: Path, value: str, megabytes: int
) -> None:
    commit = "c" * 40
    fake = _pinned("v1", commit, _good(commit))

    open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path, fetch=fake, environ={"FASTRO_MAX_SOURCE_MB": value})

    assert fake.limits[-1] == megabytes * 10**6


@pytest.mark.parametrize(
    "value", ["59", "0", "86401", "9" * 5000, "abc", "6e1", " 90", "90 ", "-90", "+90", "90.0", "\u0669\u0660"]
)
def test_a_fetch_timeout_that_isn_t_a_whole_number_from_60_to_86400_is_a_usage_error_before_anything_is_asked(
    tmp_path: Path, value: str
) -> None:
    commit = "c" * 40
    open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path, fetch=_pinned("v1", commit, _good(commit)), environ={})
    environ = {"FASTRO_FETCH_TIMEOUT": value}
    message = f"FASTRO_FETCH_TIMEOUT is {value!r}. Set it to a whole number of seconds, from 60 to 86,400, or unset it."

    for offline in (False, True):
        with pytest.raises(SourceError) as caught:
            open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path, offline=offline, fetch=_offline, environ=environ)
        assert str(caught.value) == message


@pytest.mark.parametrize(("value", "seconds"), [("", 60), ("60", 60), ("0120", 120), ("86400", 86_400)])
def test_a_fetch_timeout_raises_how_long_a_gh_request_waits_for_data(
    tmp_path: Path, value: str, seconds: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    stalls: list[object] = []
    commit = "c" * 40
    answers = _pinned("v1", commit, _good(commit))

    def recorded(url: str, headers: Mapping[str, str], limit: int | None = None, stall: int = 0) -> bytes:
        stalls.append(stall)
        return answers(url, headers, limit)

    monkeypatch.setattr(sources, "http_get", recorded)

    open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path, environ={"FASTRO_FETCH_TIMEOUT": value})

    assert stalls and set(stalls) == {seconds}


def test_with_both_variables_wrong_the_size_limit_is_reported_first(tmp_path: Path) -> None:
    environ = {"FASTRO_MAX_SOURCE_MB": "1", "FASTRO_FETCH_TIMEOUT": "1"}

    with pytest.raises(SourceError, match="^FASTRO_MAX_SOURCE_MB is '1'"):
        open_gh(parse("gh:acme/bp@v1"), TOOL, tmp_path, fetch=_offline, environ=environ)
