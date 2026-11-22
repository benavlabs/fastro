"""The release workflow builds and dry-runs a publish on every push, and publishes through trusted publishing only for a
``v*`` tag that matches the package version, with no token in the repo (A13a)."""

import os
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
TAG = "startsWith(github.ref, 'refs/tags/v')"
CHECK = "Check the tag matches the package version"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def _workflow() -> dict[Any, Any]:
    workflow: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text())
    return workflow


def _runs(job: str) -> list[str]:
    return [step["run"] for step in _workflow()["jobs"][job]["steps"] if "run" in step]


def _step(job: str, name: str) -> dict[str, Any]:
    step: dict[str, Any] = next(step for step in _workflow()["jobs"][job]["steps"] if step.get("name") == name)
    return step


def test_it_runs_on_every_branch_push_and_on_v_tags() -> None:
    assert _workflow()[True] == {"push": {"branches": ["**"], "tags": ["v*"]}}


def test_every_push_builds_dry_runs_the_publish_without_credentials_and_keeps_what_it_built() -> None:
    job = _workflow()["jobs"]["build"]

    assert "if" not in job
    assert _runs("build")[-2:] == ["uv build", "uv publish --dry-run --trusted-publishing never dist/*"]
    assert job["steps"][-1] == {"uses": "actions/upload-artifact@v7.0.2", "with": {"name": "dist", "path": "dist/"}}


def test_only_a_v_tag_publishes_what_the_build_job_built_through_trusted_publishing() -> None:
    job = _workflow()["jobs"]["publish"]

    assert (job["needs"], job["if"], job["environment"]) == ("build", TAG, "pypi")
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert job["steps"][0] == {"uses": "actions/download-artifact@v8.0.2", "with": {"name": "dist", "path": "dist/"}}
    assert _runs("publish") == ["uv publish --trusted-publishing always dist/*"]


def test_the_workflow_reads_no_secret_and_gets_a_read_only_token_otherwise() -> None:
    assert "secrets." not in WORKFLOW.read_text()
    assert _workflow()["permissions"] == {"contents": "read"}


def test_the_version_check_runs_only_on_a_tag() -> None:
    assert _step("build", CHECK)["if"] == TAG


@pytest.mark.parametrize(("tag", "passes"), [(f"v{VERSION}", True), ("v9.9.9", False), (VERSION, False)])
def test_the_version_check_passes_only_for_v_and_the_package_version(tag: str, passes: bool, tmp_path: Path) -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    (tmp_path / "pyproject.toml").write_text(pyproject + '\n[tool.other]\nversion = "9.9.9"\nname = "x"\n')

    result = subprocess.run(
        ["bash", "-c", _step("build", CHECK)["run"]],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_REF_NAME": tag},
        capture_output=True,
        text=True,
    )

    assert (result.returncode == 0) is passes
    if not passes:
        assert f"{tag} doesn't match the package version, {VERSION}." in result.stdout + result.stderr
