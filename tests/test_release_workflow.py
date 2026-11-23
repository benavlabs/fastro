"""The release workflow builds and dry-runs a publish on every push with no credentials, and releases only from a
manual run on the default branch for the package version, untagged yet: it publishes what it built through trusted
publishing, then tags that commit and makes a GitHub release of the same files, a pre-release for a pre or development
version so it's never the latest (A13a)."""

import os
import re
import stat
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
DISPATCH = "github.event_name == 'workflow_dispatch'"
CHECK = "Check the release can be made"
RELEASE = "Tag the built commit and release the built files"
PINS = {
    "actions/checkout": ("3d3c42e5aac5ba805825da76410c181273ba90b1", "v7.0.1"),
    "actions/download-artifact": ("9000827ccba6bdab643e8b6fd33ac0654aef8333", "v8.0.2"),
    "actions/upload-artifact": ("cf430e030ddbb5b0abf93d22962f4752f3646cd9", "v7.0.2"),
    "astral-sh/setup-uv": ("07a047e608db84b3d8c06cd9a7c7dcc9c355c18e", "v10.3.2"),
}
ACTIONS = {
    "build": ["actions/checkout", "astral-sh/setup-uv", "actions/upload-artifact"],
    "publish": ["actions/download-artifact", "astral-sh/setup-uv"],
    "release": ["actions/download-artifact"],
}
DIST = {"name": "dist", "path": "dist/"}
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def _workflow() -> dict[Any, Any]:
    workflow: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text())
    return workflow


def _runs(job: str) -> list[str]:
    return [step["run"] for step in _workflow()["jobs"][job]["steps"] if "run" in step]


def _step(job: str, name: str) -> dict[str, Any]:
    step: dict[str, Any] = next(step for step in _workflow()["jobs"][job]["steps"] if step.get("name") == name)
    return step


def test_it_runs_on_every_branch_push_and_by_hand_with_a_version_never_on_a_tag() -> None:
    assert _workflow()[True] == {
        "push": {"branches": ["**"]},
        "workflow_dispatch": {
            "inputs": {
                "version": {
                    "description": "The version to release, as [project].version has it",
                    "required": True,
                    "type": "string",
                }
            }
        },
    }


def test_every_push_builds_dry_runs_the_publish_without_credentials_and_keeps_what_it_built() -> None:
    job = _workflow()["jobs"]["build"]

    assert "if" not in job and "permissions" not in job and "environment" not in job
    assert _runs("build")[-2:] == ["uv build", "uv publish --dry-run --trusted-publishing never dist/*"]
    assert job["steps"][-1] == {"uses": f"actions/upload-artifact@{PINS['actions/upload-artifact'][0]}", "with": DIST}


def test_the_release_check_runs_only_by_hand_before_the_build() -> None:
    steps = [step.get("name", step.get("run")) for step in _workflow()["jobs"]["build"]["steps"]]

    assert _step("build", CHECK)["if"] == DISPATCH
    assert steps.index(CHECK) < steps.index("uv build")
    assert _step("build", CHECK)["env"] == {
        "VERSION": "${{ inputs.version }}",
        "DEFAULT_BRANCH": "${{ github.event.repository.default_branch }}",
    }


def test_only_a_manual_run_publishes_what_the_build_job_built_through_trusted_publishing() -> None:
    job = _workflow()["jobs"]["publish"]

    assert (job["needs"], job["if"], job["environment"]) == ("build", DISPATCH, "pypi")
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert job["steps"][0] == {"uses": f"actions/download-artifact@{PINS['actions/download-artifact'][0]}", "with": DIST}
    assert _runs("publish") == ["uv publish --trusted-publishing always dist/*"]


def test_only_a_successful_publish_is_tagged_and_released_with_the_built_files() -> None:
    job = _workflow()["jobs"]["release"]

    assert (job["needs"], "if" in job, "environment" in job) == ("publish", False, False)
    assert job["permissions"] == {"contents": "write"}
    assert job["steps"][0] == {"uses": f"actions/download-artifact@{PINS['actions/download-artifact'][0]}", "with": DIST}
    assert _step("release", RELEASE)["env"] == {"GH_TOKEN": "${{ github.token }}", "VERSION": "${{ inputs.version }}"}


@pytest.mark.parametrize("job", ["build", "publish", "release"])
def test_each_job_pins_each_action_by_commit_with_its_release_tag_beside_it(job: str) -> None:
    steps = _workflow()["jobs"][job]["steps"]
    after = WORKFLOW.read_text().splitlines()
    after = after[after.index(f"  {job}:") + 1 :]
    within = next((n for n, line in enumerate(after) if re.match(r"  \S", line)), len(after))
    lines = [line.strip() for line in after[:within]]

    assert [step["uses"] for step in steps if "uses" in step] == [f"{name}@{PINS[name][0]}" for name in ACTIONS[job]]
    for name in ACTIONS[job]:
        sha, tag = PINS[name]
        pinned = rf"- uses: {re.escape(name)}@{sha}\s+#\s*{re.escape(tag)}"
        assert any(re.fullmatch(pinned, line) for line in lines)


def test_the_workflow_reads_no_secret_gets_a_read_only_token_otherwise_and_never_expands_into_a_script() -> None:
    assert "secrets." not in WORKFLOW.read_text()
    assert _workflow()["permissions"] == {"contents": "read"}
    assert all("${{" not in run for job in _workflow()["jobs"] for run in _runs(job))


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True)


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A clone of a repo whose origin has the tag ``v9.9.9``, holding this pyproject.toml plus another table with a
    ``version`` of its own."""
    origin, clone = tmp_path / "origin.git", tmp_path / "clone"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    pyproject = (ROOT / "pyproject.toml").read_text()
    (clone / "pyproject.toml").write_text(pyproject + '\n[tool.other]\nversion = "9.9.9"\nname = "x"\n')
    _git(clone, "add", "pyproject.toml")
    _git(clone, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "x")
    _git(clone, "tag", "v9.9.9")
    _git(clone, "push", "-q", "origin", "HEAD:refs/heads/main", "v9.9.9")
    _git(clone, "tag", "-d", "v9.9.9")
    return clone


@pytest.mark.parametrize(
    ("version", "ref", "origin", "message"),
    [
        (VERSION, "refs/heads/main", True, None),
        ("9.9.9", "refs/heads/main", True, f"9.9.9 doesn't match the package version, {VERSION}."),
        (f"v{VERSION}", "refs/heads/main", True, f"v{VERSION} doesn't match the package version, {VERSION}."),
        (VERSION, "refs/heads/feature", True, "A release runs only on main, not refs/heads/feature."),
        (VERSION, "refs/tags/v1", True, "A release runs only on main, not refs/tags/v1."),
        (VERSION, "refs/heads/main", False, "Can't tell whether tag v"),
    ],
)
def test_the_release_check_passes_only_on_the_default_branch_for_the_package_version_untagged_yet(
    clone: Path, version: str, ref: str, origin: bool, message: str | None
) -> None:
    if not origin:
        _git(clone, "remote", "set-url", "origin", str(clone.parent / "gone.git"))

    result = _check(clone, version, ref)

    assert (result.returncode == 0) is (message is None), result.stdout + result.stderr
    if message is not None:
        assert message in result.stdout + result.stderr


def test_the_release_check_refuses_a_version_whose_tag_origin_already_has(clone: Path) -> None:
    text = (clone / "pyproject.toml").read_text().replace(f'version = "{VERSION}"', 'version = "9.9.9"', 1)
    (clone / "pyproject.toml").write_text(text)

    result = _check(clone, "9.9.9", "refs/heads/main")

    assert result.returncode != 0
    assert "Tag v9.9.9 already exists, so 9.9.9 has been released. Bump [project].version first." in result.stdout


def test_the_release_check_refuses_when_the_default_branch_comes_through_empty(clone: Path) -> None:
    result = _check(clone, VERSION, "refs/heads/main", default="")

    assert result.returncode != 0
    assert "A release runs only on , not refs/heads/main." in result.stdout


def _check(clone: Path, version: str, ref: str, default: str = "main") -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "VERSION": version, "DEFAULT_BRANCH": default, "GITHUB_REF": ref}
    return subprocess.run(
        ["bash", "-e", "-c", _step("build", CHECK)["run"]], cwd=clone, env=env, capture_output=True, text=True
    )


def _release(tmp_path: Path, version: str) -> list[str]:
    """The arguments the release step hands ``gh`` for ``version``, from a stub ``gh`` on ``PATH``."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    gh = bin_ / "gh"
    gh.write_text('#!/bin/sh\nfor argument in "$@"; do printf "%s\\n" "$argument"; done > "$OUT"\n')
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "dist").mkdir()
    for name in ("fastro-1.2.3-py3-none-any.whl", "fastro-1.2.3.tar.gz"):
        (tmp_path / "dist" / name).write_text("")
    env = {
        **os.environ,
        "PATH": f"{bin_}:{os.environ['PATH']}",
        "OUT": str(tmp_path / "argv"),
        "VERSION": version,
        "GITHUB_SHA": "c" * 40,
        "GITHUB_REPOSITORY": "acme/fastro",
    }
    subprocess.run(["bash", "-e", "-c", _step("release", RELEASE)["run"]], cwd=tmp_path, env=env, check=True)
    return (tmp_path / "argv").read_text().splitlines()


def test_the_release_step_tags_the_built_commit_and_releases_the_built_files(tmp_path: Path) -> None:
    assert _release(tmp_path, "1.2.3") == [
        "release",
        "create",
        "v1.2.3",
        "--repo",
        "acme/fastro",
        "--target",
        "c" * 40,
        "--title",
        "v1.2.3",
        "--generate-notes",
        "dist/fastro-1.2.3-py3-none-any.whl",
        "dist/fastro-1.2.3.tar.gz",
    ]


@pytest.mark.parametrize(
    "version",
    [
        "1.2.3",
        "0.1.0.dev1",
        "0.1.0a1",
        "0.1.0b2",
        "0.1.0rc1",
        "0.1.0c1",
        "0.1.0-alpha.1",
        "0.1.0.beta2",
        "0.1.0pre1",
        "0.1.0preview1",
        "0.1.0RC1",
        "1.0.post1",
        "1.0.post1.dev0",
        "1.0-r1",
        "1.0rev2",
        "1!2.0",
        "1!2.0a1",
        "1.0+abc.dev",
        "1.0a1+local",
        "1.0.DEV3",
        "v1.0",
        "V1.0a1",
    ],
)
def test_a_pre_or_development_release_is_marked_a_prerelease(tmp_path: Path, version: str) -> None:
    argv = _release(tmp_path, version)

    assert ("--prerelease" in argv) is Version(version).is_prerelease
    assert argv[2] == f"v{version}"
