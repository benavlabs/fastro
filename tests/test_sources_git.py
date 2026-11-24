"""Fetching a ``git+`` source (spec 1.7, 1.10): its REF alone, one commit deep, archived with ``git archive`` and kept
as a ``gh:`` tarball is, with no ``.git``, the token sent only through git's environment, and SSH falling back to
HTTPS for GitHub only when the SSH remote can't be listed. Remotes are local bare repos reached through
``url.<path>.insteadOf`` in a test's own global git config."""

import base64
import hashlib
import os
import re
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from packaging.version import Version

from fastro import apply, sources
from fastro.engine.spec import RepoPath
from fastro.sources import FetchError, SourceError, Tree, open_gh, open_git, parse
from fastro.tools import MissingToolError

TOOL = Version("0.1.0.dev1")
CATALOG = '[blueprint]\nname = "bp"\nversion = "0.21.0"\nschema = 1\nrequires_fastro = "{requires}"\n'
HTTPS = "https://example.test/acme/bp"
TOKEN = "s3cret-token"


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True, text=True).stdout


def _commit(work: Path, files: Mapping[str, str], message: str = "x") -> str:
    for name, text in files.items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(text)
    _git(work, "add", "-A")
    _git(work, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", message)
    return _git(work, "rev-parse", "HEAD").strip()


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A global git config of the test's own, read instead of the user's, holding the ``insteadOf`` rules."""
    path = tmp_path / "gitconfig"
    path.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return path


def _reach(config: Path, url: str, path: Path) -> None:
    with config.open("a") as out:
        out.write(f'[url "file://{path}"]\n\tinsteadOf = {url}\n')


@pytest.fixture
def remote(tmp_path: Path, config: Path) -> tuple[Path, Path]:
    """A work tree whose commits are pushed to a bare repo that ``HTTPS`` reaches, and the bare repo."""
    work, bare = tmp_path / "work", tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "-b", "main", str(work))
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    _git(bare, "config", "uploadpack.allowAnySHA1InWant", "true")
    _git(work, "remote", "add", "origin", str(bare))
    _reach(config, HTTPS, bare)
    return work, bare


def _push(work: Path) -> None:
    _git(work, "push", "-q", "--force", "--tags", "origin", "HEAD:refs/heads/main")


def _source(ref: str | None = None, subdir: str | None = None) -> str:
    return f"git+{HTTPS}" + (f"@{ref}" if ref else "") + (f"#{subdir}" if subdir else "")


def _files(tree: Tree) -> list[str]:
    return [path.text for path in tree.files]


def _entries(cache: Path, location: str = f"git+{HTTPS}") -> Path:
    return cache / "git" / hashlib.sha256(location.encode()).hexdigest()


def test_a_branch_is_fetched_alone_archived_and_kept_with_no_git(remote: tuple[Path, Path], tmp_path: Path) -> None:
    work, _ = remote
    _commit(work, {"blueprint.toml": CATALOG.format(requires=">=0"), "src/app.py": "x\n", "old.txt": "old\n"})
    commit = _commit(
        work,
        {"src/app.py": "y\n", "docs/index.md": "# d\n", ".gitattributes": "docs/ export-ignore\n"},
    )
    _push(work)
    cache = tmp_path / "cache"

    tree = open_git(parse(_source("main")), TOOL, cache, environ={})

    assert (tree.commit, tree.root) == (commit, (_entries(cache) / commit / "tree").resolve())
    assert _files(tree) == [".gitattributes", "blueprint.toml", "old.txt", "src/app.py"]
    assert (tree.root / "src" / "app.py").read_text() == "y\n"
    assert sorted(os.listdir(_entries(cache))) == [commit, "refs.json"]
    assert not any(name == ".git" for _, names, _ in os.walk(_entries(cache)) for name in names)


def _no_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(*arguments: object) -> str:
        raise AssertionError("fetched again")

    monkeypatch.setattr(apply, "fetch", refused)


def test_an_annotated_tag_resolves_to_its_commit_and_is_found_kept_by_it(
    remote: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, _ = remote
    commit = _commit(work, {"blueprint.toml": CATALOG.format(requires=">=0")})
    _git(work, "-c", "user.email=a@b", "-c", "user.name=a", "tag", "-a", "-m", "v", "v1.0")
    _commit(work, {"later.txt": "x\n"})
    _push(work)

    assert open_git(parse(_source("v1.0")), TOOL, tmp_path / "cache", environ={}).commit == commit
    _no_fetch(monkeypatch)
    assert open_git(parse(_source("v1.0")), TOOL, tmp_path / "cache", environ={}).commit == commit


def test_a_full_commit_that_isn_t_a_tip_is_fetched_by_itself(remote: tuple[Path, Path], tmp_path: Path) -> None:
    work, _ = remote
    old = _commit(work, {"blueprint.toml": CATALOG.format(requires=">=0"), "a.txt": "1\n"})
    _commit(work, {"a.txt": "2\n"})
    _push(work)

    tree = open_git(parse(_source(old)), TOOL, tmp_path / "cache", environ={})

    assert (tree.commit, (tree.root / "a.txt").read_text()) == (old, "1\n")


def test_a_ref_reads_as_git_reads_one_a_tag_before_a_branch(remote: tuple[Path, Path], tmp_path: Path) -> None:
    work, _ = remote
    tagged = _commit(work, {"blueprint.toml": CATALOG.format(requires=">=0")})
    _git(work, "tag", "same")
    branched = _commit(work, {"b.txt": "b\n"})
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/same", "--tags")
    _push(work)
    assert tagged != branched

    assert open_git(parse(_source("same")), TOOL, tmp_path / "cache", environ={}).commit == tagged


def test_a_full_commit_the_remote_doesn_t_have_fails_with_what_git_said(remote: tuple[Path, Path], tmp_path: Path) -> None:
    work, _ = remote
    _commit(work, {"blueprint.toml": CATALOG.format(requires=">=0")})
    _push(work)

    with pytest.raises(
        FetchError, match=rf"(?s)git can't fetch {'d' * 40} from {HTTPS}: .*(not our ref|remote error|couldn't find)"
    ):
        open_git(parse(_source("d" * 40)), TOOL, tmp_path / "cache", environ={})


def test_a_full_commit_that_names_a_tag_object_isn_t_taken_for_its_commit(remote: tuple[Path, Path], tmp_path: Path) -> None:
    work, _ = remote
    commit = _commit(work, {"blueprint.toml": CATALOG.format(requires=">=0")})
    _git(work, "-c", "user.email=a@b", "-c", "user.name=a", "tag", "-a", "-m", "v", "v1.0")
    _push(work)
    tag = _git(work, "rev-parse", "v1.0").strip()
    assert tag != commit

    with pytest.raises(FetchError, match=f"answered '{tag}' with commit {commit}, which isn't that commit"):
        open_git(parse(_source(tag)), TOOL, tmp_path / "cache", environ={})


@pytest.mark.parametrize("ref", ["heads/main", "tags/v1", "refs/heads/main"])
def test_a_ref_is_a_branch_or_a_tag_by_its_name_never_a_path_under_refs(
    ref: str, remote: tuple[Path, Path], tmp_path: Path
) -> None:
    work, _ = remote
    _commit(work, {"blueprint.toml": CATALOG.format(requires=">=0")})
    _git(work, "tag", "v1")
    _push(work)

    with pytest.raises(FetchError, match=f"has no ref '{ref}'"):
        open_git(parse(_source(ref)), TOOL, tmp_path / "cache", environ={})


def test_a_ref_the_remote_doesn_t_have_is_a_fetch_failure(remote: tuple[Path, Path], tmp_path: Path) -> None:
    work, _ = remote
    _commit(work, {"blueprint.toml": CATALOG.format(requires=">=0")})
    _push(work)

    with pytest.raises(FetchError, match=rf"git\+{HTTPS} has no ref 'nope'\. Name a branch, a tag or a full commit"):
        open_git(parse(_source("nope")), TOOL, tmp_path / "cache", environ={})


def test_a_commit_already_kept_isn_t_fetched_again(
    remote: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, _ = remote
    commit = _commit(work, {"blueprint.toml": CATALOG.format(requires=">=0")})
    _push(work)
    first = open_git(parse(_source("main")), TOOL, tmp_path / "cache", environ={})

    def refused(*arguments: object) -> str:
        raise AssertionError("fetched again")

    monkeypatch.setattr(apply, "fetch", refused)

    assert open_git(parse(_source("main")), TOOL, tmp_path / "cache", environ={}) == first
    assert first.commit == commit


def _tag(work: Path, tag: str, files: Mapping[str, str]) -> str:
    commit = _commit(work, files, tag)
    _git(work, "tag", tag)
    return commit


def test_with_no_ref_the_highest_version_tag_whose_catalog_this_fastro_reads_is_taken(
    remote: tuple[Path, Path], tmp_path: Path
) -> None:
    work, _ = remote
    _tag(work, "v0.1.0", {"README.md": "no catalog yet\n"})
    picked = _tag(work, "v0.1.5", {"blueprint.toml": CATALOG.format(requires=">=0")})
    _tag(work, "v0.2.0", {"blueprint.toml": CATALOG.format(requires=">=9")})
    _tag(work, "v0.3.0", {"blueprint.toml": CATALOG.format(requires=">=0"), ".gitattributes": "blueprint.toml export-ignore\n"})
    _tag(work, "v0.4.0rc1", {".gitattributes": "", "blueprint.toml": CATALOG.format(requires=">=0")})
    _tag(work, "latest", {"x.txt": "x\n"})
    _push(work)
    cache = tmp_path / "cache"

    tree = open_git(parse(_source()), TOOL, cache, environ={})

    assert tree.commit == picked
    assert '"v0.1.5"' in (_entries(cache) / "refs.json").read_text()


def test_the_walk_finds_an_annotated_tag_s_commit_kept_without_fetching_it_again(
    remote: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, _ = remote
    commit = _commit(work, {"blueprint.toml": CATALOG.format(requires=">=0")})
    _git(work, "-c", "user.email=a@b", "-c", "user.name=a", "tag", "-a", "-m", "v", "v1.0.0")
    _push(work)
    open_git(parse(_source()), TOOL, tmp_path / "cache", environ={})
    _no_fetch(monkeypatch)

    assert open_git(parse(_source()), TOOL, tmp_path / "cache", environ={}).commit == commit


def test_offline_with_no_ref_never_takes_a_commit_fetched_by_its_ref(remote: tuple[Path, Path], tmp_path: Path) -> None:
    work, _ = remote
    _tag(work, "v1.0.0", {"blueprint.toml": CATALOG.format(requires=">=0")})
    _push(work)
    open_git(parse(_source("v1.0.0")), TOOL, tmp_path / "cache", environ={})

    with pytest.raises(FetchError, match="the cache holds no release of"):
        open_git(parse(_source()), TOOL, tmp_path / "cache", offline=True, environ={})


@pytest.mark.usefixtures("small")
def test_a_tag_over_a_limit_fails_the_walk_rather_than_being_skipped(remote: tuple[Path, Path], tmp_path: Path) -> None:
    work, _ = remote
    _tag(work, "v1.0.0", {"blueprint.toml": CATALOG.format(requires=">=0")})
    _tag(work, "v2.0.0", {"big.txt": "x" * 30_000})
    _push(work)

    with pytest.raises(FetchError, match="'s archive is over 1 GB extracted"):
        open_git(parse(_source()), TOOL, tmp_path / "cache", environ={})


def test_with_no_ref_and_no_tag_this_fastro_reads_the_walk_is_a_fetch_failure(
    remote: tuple[Path, Path], tmp_path: Path
) -> None:
    work, _ = remote
    _tag(work, "v1.0.0", {"blueprint.toml": CATALOG.format(requires=">=9")})
    _push(work)

    with pytest.raises(FetchError, match=r"has no tag whose blueprint\.toml accepts this fastro, 0\.1\.0\.dev1"):
        open_git(parse(_source()), TOOL, tmp_path / "cache", environ={})


def test_offline_opens_what_a_run_kept_without_asking_the_remote(
    remote: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, _ = remote
    _tag(work, "v1.0.0", {"blueprint.toml": CATALOG.format(requires=">=0")})
    _push(work)
    fetched = open_git(parse(_source()), TOOL, tmp_path / "cache", environ={})

    def unreachable(*arguments: object) -> dict[str, str]:
        raise AssertionError("asked the remote")

    monkeypatch.setattr(apply, "remote_refs", unreachable)

    assert open_git(parse(_source()), TOOL, tmp_path / "cache", offline=True, environ={}) == fetched
    assert open_git(parse(_source("v1.0.0")), TOOL, tmp_path / "cache", offline=True, environ={}) == fetched


def test_a_max_source_mb_that_doesn_t_read_is_a_usage_error_before_the_remote_is_asked(
    tmp_path: Path, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable(*arguments: object) -> dict[str, str]:
        raise AssertionError("asked the remote")

    monkeypatch.setattr(apply, "remote_refs", unreachable)

    for offline in (False, True):
        with pytest.raises(SourceError, match="FASTRO_MAX_SOURCE_MB is '12'"):
            open_git(parse(_source()), TOOL, tmp_path, offline=offline, environ={"FASTRO_MAX_SOURCE_MB": "12"})


@pytest.fixture
def small(monkeypatch: pytest.MonkeyPatch) -> None:
    """A megabyte of 20 bytes, so the download limit is 4,000 bytes and the extracted one 20,000."""
    monkeypatch.setattr(sources, "MB", 20)


@pytest.mark.usefixtures("small")
def test_a_fetch_whose_objects_take_more_than_the_download_limit_fails_and_nothing_is_kept(
    remote: tuple[Path, Path], tmp_path: Path
) -> None:
    work, _ = remote
    _commit(work, {"blueprint.toml": CATALOG.format(requires=">=0"), "noise.bin": os.urandom(6000).hex()})
    _push(work)
    cache = tmp_path / "cache"

    with pytest.raises(
        FetchError, match=rf"^git\+{HTTPS}@main's fetch takes more than 200 MB on disk, the most fastro fetches \(1\.10\)\."
    ):
        open_git(parse(_source("main")), TOOL, cache, environ={})

    assert os.listdir(_entries(cache)) == []


@pytest.mark.usefixtures("small")
def test_a_fetch_is_stopped_once_its_objects_pass_the_limit_while_git_is_still_fetching(
    remote: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, _ = remote
    _commit(work, {"blueprint.toml": CATALOG.format(requires=">=0")})
    _push(work)
    real = subprocess.Popen
    started: list[Any] = []
    child = tmp_path / "child"

    def slow(command: list[str], **keywords: Any) -> Any:
        if "fetch" in command:
            command = ["sh", "-c", 'sleep 30 & echo $! > "$1"; wait', "sh", str(child)]
        started.append(real(command, **keywords))
        return started[-1]

    sizes = iter([0, 10**9])
    monkeypatch.setattr(subprocess, "Popen", slow)
    monkeypatch.setattr(apply, "_size", lambda directory: next(sizes))
    cache = tmp_path / "cache"

    with pytest.raises(FetchError, match="@main's fetch takes more than 200 MB on disk"):
        open_git(parse(_source("main")), TOOL, cache, environ={})

    assert os.listdir(_entries(cache)) == []
    assert started[-1].returncode == -signal.SIGKILL
    pid = int(child.read_text())
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        raise AssertionError(f"git's child {pid} outlived it")


@pytest.mark.usefixtures("small")
def test_a_fetch_whose_objects_pass_the_limit_only_once_git_is_done_still_fails(
    remote: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, _ = remote
    _commit(work, {"blueprint.toml": CATALOG.format(requires=">=0")})
    _push(work)
    real = subprocess.Popen
    started: list[Any] = []

    def recorded(command: list[str], **keywords: Any) -> Any:
        started.append(real(command, **keywords))
        return started[-1]

    monkeypatch.setattr(subprocess, "Popen", recorded)
    monkeypatch.setattr(apply, "_size", lambda directory: 10**9 if started[-1].poll() is not None else 0)

    with pytest.raises(FetchError, match="@main's fetch takes more than 200 MB on disk"):
        open_git(parse(_source("main")), TOOL, tmp_path / "cache", environ={})


@pytest.mark.usefixtures("small")
def test_only_the_commit_is_fetched_so_history_doesn_t_count_against_the_limit(
    remote: tuple[Path, Path], tmp_path: Path
) -> None:
    work, _ = remote
    _commit(work, {"blueprint.toml": CATALOG.format(requires=">=0"), "noise.bin": os.urandom(6000).hex()})
    (work / "noise.bin").unlink()
    commit = _commit(work, {})
    _push(work)

    assert open_git(parse(_source("main")), TOOL, tmp_path / "cache", environ={}).commit == commit


@pytest.mark.usefixtures("small")
def test_an_archive_over_the_extracted_limit_fails_and_nothing_is_kept(remote: tuple[Path, Path], tmp_path: Path) -> None:
    work, _ = remote
    _commit(work, {"blueprint.toml": CATALOG.format(requires=">=0"), "big.txt": "x" * 30_000})
    _push(work)
    cache = tmp_path / "cache"

    with pytest.raises(FetchError, match=r"'s archive is over 1 GB extracted, the most fastro keeps \(1\.10\)"):
        open_git(parse(_source("main")), TOOL, cache, environ={})

    assert os.listdir(_entries(cache)) == []


SSH = "git+ssh://git@github.com/acme/bp"


@pytest.fixture
def github(tmp_path: Path, config: Path, remote: tuple[Path, Path]) -> Path:
    """The remote reached as ``https://github.com/acme/bp``, with ``ssh://git@github.com/acme/bp`` leading nowhere."""
    work, bare = remote
    _commit(work, {"blueprint.toml": CATALOG.format(requires=">=0")})
    _push(work)
    _reach(config, "https://github.com/acme/bp", bare)
    _reach(config, "ssh://git@github.com/acme/bp", tmp_path / "nowhere.git")
    return bare


def test_an_ssh_remote_github_can_t_list_falls_back_to_https_with_the_token_and_says_so(github: Path, tmp_path: Path) -> None:
    said: list[str] = []

    tree = open_git(parse(f"{SSH}@main"), TOOL, tmp_path / "cache", environ={"GITHUB_TOKEN": TOKEN}, say=said.append)

    assert tree.files[0] == RepoPath("blueprint.toml")
    assert said == [
        f"fastro can't list the refs of {SSH} over SSH, so it's fetching https://github.com/acme/bp with GITHUB_TOKEN instead."
    ]
    assert os.path.isdir(_entries(tmp_path / "cache", SSH))


def test_without_a_token_an_ssh_remote_that_can_t_be_listed_fails_and_names_github_token(github: Path, tmp_path: Path) -> None:
    with pytest.raises(
        FetchError, match=rf"(?s)git can't list the refs of {re.escape(SSH)}: .*Set GITHUB_TOKEN to fetch it over HTTPS"
    ):
        open_git(parse(f"{SSH}@main"), TOOL, tmp_path / "cache", environ={}, say=print)


def test_when_https_fails_too_the_failure_says_what_each_answered(tmp_path: Path, config: Path) -> None:
    _reach(config, "ssh://git@github.com/acme/bp", tmp_path / "nowhere.git")
    _reach(config, "https://github.com/acme/bp", tmp_path / "nowhere-either.git")

    with pytest.raises(
        FetchError, match=rf"(?s)of {re.escape(SSH)} over SSH \(.+\) or of https://github\.com/acme/bp over HTTPS \(.+\)\."
    ):
        open_git(parse(f"{SSH}@main"), TOOL, tmp_path / "cache", environ={"GITHUB_TOKEN": TOKEN}, say=lambda text: None)


def test_an_ssh_remote_that_answers_never_falls_back_even_for_a_ref_it_doesn_t_have(
    tmp_path: Path, config: Path, remote: tuple[Path, Path]
) -> None:
    work, bare = remote
    _commit(work, {"blueprint.toml": CATALOG.format(requires=">=0")})
    _push(work)
    _reach(config, "ssh://git@github.com/acme/bp", bare)
    said: list[str] = []

    with pytest.raises(FetchError, match="has no ref 'nope'"):
        open_git(parse(f"{SSH}@nope"), TOOL, tmp_path / "cache", environ={"GITHUB_TOKEN": TOKEN}, say=said.append)

    assert open_git(parse(f"{SSH}@main"), TOOL, tmp_path / "cache", environ={"GITHUB_TOKEN": TOKEN}, say=said.append)
    assert said == []


@pytest.mark.parametrize("source", ["git+ssh://git@gitlab.com/acme/bp", "git+ssh://git@github.com/acme/bp/extra"])
def test_only_a_github_owner_and_repo_falls_back(source: str, tmp_path: Path, config: Path) -> None:
    _reach(config, source.removeprefix("git+"), tmp_path / "nowhere.git")

    said: list[str] = []

    with pytest.raises(FetchError, match="git can't list the refs of") as caught:
        open_git(parse(f"{source}@main"), TOOL, tmp_path, environ={"GITHUB_TOKEN": TOKEN}, say=said.append)

    assert ("GITHUB_TOKEN" in str(caught.value), said) == (False, [])


def test_the_token_reaches_git_only_through_its_environment_and_nothing_kept_holds_it(
    github: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    run, popen = subprocess.run, subprocess.Popen

    def recorded_run(command: list[str], **keywords: Any) -> Any:
        calls.append((list(command), dict(keywords.get("env") or {})))
        return run(command, **keywords)

    def recorded_popen(command: list[str], **keywords: Any) -> Any:
        calls.append((list(command), dict(keywords.get("env") or {})))
        if "fetch" in command:
            stdins.append(keywords.get("stdin"))
        return popen(command, **keywords)

    stdins: list[Any] = []
    real_archive = apply.archive
    scanned: list[Path] = []

    def scanning(repo: Path, commit: str) -> Any:
        for top, _, names in os.walk(repo):
            for name in names:
                scanned.append(Path(top, name))
                data = Path(top, name).read_bytes()
                assert TOKEN.encode() not in data and encoded.encode() not in data, Path(top, name)
        return real_archive(repo, commit)

    monkeypatch.setattr(subprocess, "run", recorded_run)
    monkeypatch.setattr(subprocess, "Popen", recorded_popen)
    monkeypatch.setattr(apply, "archive", scanning)
    encoded = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    cache = tmp_path / "cache"

    open_git(parse(f"{SSH}@main"), TOOL, cache, environ={"GITHUB_TOKEN": TOKEN}, say=lambda text: None)

    fetches = [command for command, _ in calls if "fetch" in command]
    assert fetches and all({"maintenance.auto=false", "gc.auto=0"} <= set(command) for command in fetches)
    assert all(stdin is subprocess.DEVNULL for stdin in stdins)
    ssh_listing = [env for command, env in calls if "ls-remote" in command and command[-1].startswith("ssh://")]
    assert ssh_listing and not [env for env in ssh_listing if "http.https://github.com/.extraheader" in _settings(env)]
    remote_calls = [env for command, env in calls if command[-1].startswith("https://") or "fetch" in command]
    assert remote_calls and all(
        _settings(env)["http.https://github.com/.extraheader"] == f"AUTHORIZATION: basic {encoded}" for env in remote_calls
    )
    assert not any(TOKEN in part or encoded in part for command, _ in calls for part in command)
    assert any(path.name == "config" for path in scanned)
    for top, _, names in os.walk(cache):
        for name in names:
            data = Path(top, name).read_bytes()
            assert TOKEN.encode() not in data and encoded.encode() not in data, Path(top, name)


def test_the_archive_reads_attributes_from_the_commit_alone_never_a_user_s_attributes_file(
    remote: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, _ = remote
    _commit(work, {"blueprint.toml": CATALOG.format(requires=">=0"), "keep.txt": "k\n"})
    _push(work)
    xdg = tmp_path / "xdg"
    (xdg / "git").mkdir(parents=True)
    (xdg / "git" / "attributes").write_text("keep.txt export-ignore\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    assert "keep.txt" in _files(open_git(parse(_source("main")), TOOL, tmp_path / "cache", environ={}))


def _settings(env: Mapping[str, str]) -> dict[str, str]:
    """The git config ``env`` sets through ``GIT_CONFIG_COUNT``, ``GIT_CONFIG_KEY_n`` and ``GIT_CONFIG_VALUE_n``."""
    return {env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"] for index in range(int(env["GIT_CONFIG_COUNT"]))}


@pytest.mark.parametrize(
    ("variables", "global_ssh", "local_ssh", "ssh"),
    [
        ({}, None, None, "ssh"),
        ({"GIT_SSH_COMMAND": "ssh -i key"}, "ssh -p 1", None, "ssh -i key"),
        ({"GIT_SSH": "/opt/my ssh"}, "ssh -p 1", None, "ssh -p 1"),
        ({"GIT_SSH": "/opt/my ssh"}, None, None, "'/opt/my ssh'"),
        ({}, "ssh -p 2222", None, "ssh -p 2222"),
        ({}, None, "ssh -p 9", "ssh"),
    ],
)
def test_git_never_prompts_and_ssh_accepts_no_new_host_key_whatever_ssh_the_user_runs(
    variables: dict[str, str],
    global_ssh: str | None,
    local_ssh: str | None,
    ssh: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "gitconfig"
    config.write_text(f"[core]\n\tsshCommand = {global_ssh}\n" if global_ssh else "")
    for name in ("GIT_SSH_COMMAND", "GIT_SSH"):
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_ASKPASS": "/bin/echo",
        "SSH_ASKPASS": "/bin/echo",
        "GIT_TRACE": "1",
        "GIT_TRACE_CURL": "1",
        "GIT_CURL_VERBOSE": "1",
        "GIT_HTTP_LOW_SPEED_LIMIT": "1",
        "GIT_HTTP_LOW_SPEED_TIME": "99999",
        **variables,
    }.items():
        monkeypatch.setenv(name, value)
    here = tmp_path / "here"
    _git(tmp_path, "init", "-q", str(here))
    if local_ssh:
        _git(here, "config", "core.sshCommand", local_ssh)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(here)

    env = apply._environment(True, TOKEN, empty)

    assert env["GIT_SSH_COMMAND"] == f"{ssh} -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=30"
    assert env["GCM_INTERACTIVE"] == "never"
    assert (env["GIT_TERMINAL_PROMPT"], env["GIT_ASKPASS"]) == ("0", "")
    assert not {"GIT_SSH", "SSH_ASKPASS", "GIT_CURL_VERBOSE", "GIT_HTTP_LOW_SPEED_LIMIT", "GIT_HTTP_LOW_SPEED_TIME"} & set(env)
    assert {name: value for name, value in env.items() if name.startswith("GIT_TRACE")} == {
        "GIT_TRACE2": "0",
        "GIT_TRACE2_EVENT": "0",
        "GIT_TRACE2_PERF": "0",
    }
    assert _settings(env) == {
        "http.https://github.com/.extraheader": "AUTHORIZATION: basic "
        + base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode(),
        "http.lowSpeedLimit": "1000",
        "http.lowSpeedTime": "60",
    }
    assert _settings(apply._environment(True, None, empty, 95)) == {"http.lowSpeedLimit": "1000", "http.lowSpeedTime": "95"}
    assert {"GIT_ASKPASS", "GIT_TRACE"} <= set(apply._environment()) and "GIT_TERMINAL_PROMPT" not in apply._environment()


def test_a_repo_above_the_cache_or_the_temporary_directory_never_names_the_ssh_git_runs(
    remote: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, _ = remote
    _commit(work, {"blueprint.toml": CATALOG.format(requires=">=0")})
    _push(work)
    home = tmp_path / "home"
    _git(tmp_path, "init", "-q", str(home))
    _git(home, "config", "core.sshCommand", "evil-ssh")
    (home / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(home / "tmp"))
    run, popen = subprocess.run, subprocess.Popen
    commands: list[str] = []

    def recorded_run(command: list[str], **keywords: Any) -> Any:
        commands.append((keywords.get("env") or {}).get("GIT_SSH_COMMAND", ""))
        return run(command, **keywords)

    def recorded_popen(command: list[str], **keywords: Any) -> Any:
        commands.append((keywords.get("env") or {}).get("GIT_SSH_COMMAND", ""))
        return popen(command, **keywords)

    monkeypatch.setattr(subprocess, "run", recorded_run)
    monkeypatch.setattr(subprocess, "Popen", recorded_popen)

    open_git(parse(_source("main")), TOOL, home / ".cache" / "fastro", environ={})

    assert commands and not [command for command in commands if "evil" in command]


def test_trace2_a_config_turns_on_never_records_the_token(github: Path, tmp_path: Path, config: Path) -> None:
    trace = tmp_path / "trace"
    with config.open("a") as out:
        out.write(f"[trace2]\n\teventTarget = {trace}\n\tconfigParams = http.*\n\tenvVars = GIT_CONFIG_VALUE_0\n")
    encoded = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()

    open_git(parse(f"{SSH}@main"), TOOL, tmp_path / "cache", environ={"GITHUB_TOKEN": TOKEN}, say=lambda text: None)

    assert not trace.exists() or encoded not in trace.read_text()


def test_a_repo_in_the_current_directory_is_never_read_while_a_source_is_listed_or_fetched(
    remote: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, _ = remote
    commit = _commit(work, {"blueprint.toml": CATALOG.format(requires=">=0")})
    _push(work)
    here = tmp_path / "here"
    _git(tmp_path, "init", "-q", str(here))
    (here / ".git" / "config").write_text("[core\nnot config\n")
    monkeypatch.chdir(here)

    assert open_git(parse(_source("main")), TOOL, tmp_path / "cache", environ={}).commit == commit


def test_a_git_source_and_a_gh_tarball_of_the_same_commit_give_the_same_tree_whatever_the_user_s_git_config(
    remote: tuple[Path, Path], tmp_path: Path, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, _ = remote
    commit = _commit(
        work,
        {
            "blueprint.toml": CATALOG.format(requires=">=0"),
            "src/app.py": "x\n",
            "text.txt": "a\nb\n",
            "docs/a.md": "a\n",
            ".gitattributes": "docs/ export-ignore\n*.txt filter=up text\n",
        },
    )
    (work / "run.sh").write_text("#!/bin/sh\n")
    (work / "run.sh").chmod(0o755)
    commit = _commit(work, {})
    _push(work)
    body = subprocess.run(
        ["git", "-C", str(work), "archive", "--format=tar.gz", f"--prefix=acme-bp-{commit[:7]}/", commit],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    ).stdout
    answers = {
        f"https://api.github.com/repos/acme/bp/commits/{commit}": commit.encode(),
        f"https://api.github.com/repos/acme/bp/tarball/{commit}": body,
    }

    def fetch(url: str, headers: Mapping[str, str], limit: int | None = None) -> bytes:
        return answers[url]

    with config.open("a") as out:
        out.write("[core]\n\tautocrlf = true\n")
    system = tmp_path / "system"
    system.write_text('[filter "up"]\n\tsmudge = tr a-z A-Z\n')
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system))
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM")
    from_git = open_git(parse(_source(commit)), TOOL, tmp_path / "git", environ={})
    from_gh = open_gh(parse(f"gh:acme/bp@{commit}"), TOOL, tmp_path / "gh", fetch=fetch, environ={})

    assert from_git.files == from_gh.files
    assert (from_git.root / "text.txt").read_bytes() == b"a\nb\n"
    for path in from_git.files:
        assert (from_git.root / path.text).read_bytes() == (from_gh.root / path.text).read_bytes()
        assert os.access(from_git.root / path.text, os.X_OK) == os.access(from_gh.root / path.text, os.X_OK)


def test_without_git_a_git_source_raises_missing_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(MissingToolError):
        open_git(parse(_source("main")), TOOL, tmp_path, environ={})


def test_archive_stops_git_when_its_caller_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: Path) -> None:
    real = subprocess.Popen
    started: list[Any] = []

    def endless(command: list[str], **keywords: Any) -> Any:
        if "archive" not in command:
            return real(command, **keywords)
        started.append(real(["yes"], **keywords))
        return started[-1]

    monkeypatch.setattr(subprocess, "Popen", endless)

    with pytest.raises(RuntimeError, match="stop"), apply.archive(tmp_path, "c" * 40) as pieces:
        next(pieces)
        raise RuntimeError("stop")

    assert started[-1].returncode == -signal.SIGKILL


def test_an_archive_git_fails_to_finish_is_a_fetch_failure_and_nothing_is_kept(
    remote: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, _ = remote
    commit = _commit(work, {"blueprint.toml": CATALOG.format(requires=">=0")})
    _push(work)
    good = tmp_path / "good.tar.gz"
    good.write_bytes(_git_bytes(work, "archive", "--format=tar.gz", "--prefix=archive/", commit))
    real = subprocess.Popen

    def failing(command: list[str], **keywords: Any) -> Any:
        if "archive" in command:
            command = ["sh", "-c", 'cat "$1"; echo broken >&2; exit 3', "sh", str(good)]
        return real(command, **keywords)

    monkeypatch.setattr(subprocess, "Popen", failing)
    cache = tmp_path / "cache"

    with pytest.raises(FetchError, match=f"git can't archive {commit} of git\\+{re.escape(HTTPS)}: broken"):
        open_git(parse(_source("main")), TOOL, cache, environ={})

    assert os.listdir(_entries(cache)) == []


def test_a_fetch_timeout_that_doesn_t_read_is_a_usage_error_before_the_remote_is_asked(
    tmp_path: Path, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable(*arguments: object) -> dict[str, str]:
        raise AssertionError("asked the remote")

    monkeypatch.setattr(apply, "remote_refs", unreachable)

    for offline in (False, True):
        with pytest.raises(SourceError, match="FASTRO_FETCH_TIMEOUT is '30'"):
            open_git(parse(_source()), TOOL, tmp_path, offline=offline, environ={"FASTRO_FETCH_TIMEOUT": "30"})


def test_a_fetch_timeout_reaches_both_the_listing_and_the_fetch(
    remote: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, _ = remote
    _commit(work, {"blueprint.toml": CATALOG.format(requires=">=0")})
    _push(work)
    run, popen = subprocess.run, subprocess.Popen
    seen: list[str] = []

    def recorded_run(command: list[str], **keywords: Any) -> Any:
        if "ls-remote" in command:
            seen.append(_settings(keywords["env"])["http.lowSpeedTime"])
        return run(command, **keywords)

    def recorded_popen(command: list[str], **keywords: Any) -> Any:
        if "fetch" in command:
            seen.append(_settings(keywords["env"])["http.lowSpeedTime"])
        return popen(command, **keywords)

    monkeypatch.setattr(subprocess, "run", recorded_run)
    monkeypatch.setattr(subprocess, "Popen", recorded_popen)

    open_git(parse(_source("main")), TOOL, tmp_path / "cache", environ={"FASTRO_FETCH_TIMEOUT": "75"})

    assert seen == ["75", "75"]


def test_with_both_variables_wrong_the_size_limit_is_reported_first(tmp_path: Path, config: Path) -> None:
    environ = {"FASTRO_MAX_SOURCE_MB": "1", "FASTRO_FETCH_TIMEOUT": "1"}

    with pytest.raises(SourceError, match="^FASTRO_MAX_SOURCE_MB is '1'"):
        open_git(parse(_source()), TOOL, tmp_path, environ=environ)


def test_a_fetch_timeout_reaches_the_https_listing_and_fetch_after_a_fallback(
    github: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, popen = subprocess.run, subprocess.Popen
    seen: list[tuple[str, str]] = []

    def recorded_run(command: list[str], **keywords: Any) -> Any:
        if "ls-remote" in command:
            seen.append((command[-1], _settings(keywords["env"])["http.lowSpeedTime"]))
        return run(command, **keywords)

    def recorded_popen(command: list[str], **keywords: Any) -> Any:
        if "fetch" in command:
            seen.append((command[-2], _settings(keywords["env"])["http.lowSpeedTime"]))
        return popen(command, **keywords)

    monkeypatch.setattr(subprocess, "run", recorded_run)
    monkeypatch.setattr(subprocess, "Popen", recorded_popen)
    environ = {"GITHUB_TOKEN": TOKEN, "FASTRO_FETCH_TIMEOUT": "75"}

    open_git(parse(f"{SSH}@main"), TOOL, tmp_path / "cache", environ=environ, say=lambda text: None)

    assert seen == [
        ("ssh://git@github.com/acme/bp", "75"),
        ("https://github.com/acme/bp", "75"),
        ("https://github.com/acme/bp", "75"),
    ]


def _git_bytes(repo: Path, *arguments: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True).stdout
