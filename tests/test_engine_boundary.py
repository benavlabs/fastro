"""``fastro.engine`` imports nothing from the CLI's libraries or the rest of fastro, and only the runner starts a subprocess."""

import ast
import re
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1] / "src" / "fastro" / "engine"

CLI_LIBRARIES = {"typer", "click", "rich", "jinja2", "questionary", "prompt_toolkit", "httpx", "httpx2"}


def _module_name(path: Path) -> list[str]:
    parts = ["fastro", *path.relative_to(ENGINE.parent).with_suffix("").parts]
    return parts[:-1] if parts[-1] == "__init__" else parts


def _imports(path: Path) -> list[tuple[int, str]]:
    """Every module ``path`` imports, as an absolute dotted name, with its line."""
    package = _module_name(path) if path.name == "__init__.py" else _module_name(path)[:-1]
    found = []
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join([*base, *(node.module or "").split(".")]).strip(".")
            found += [(node.lineno, module if alias.name == "*" else f"{module}.{alias.name}") for alias in node.names]
    return found


def _violations() -> list[str]:
    modules = sorted(ENGINE.rglob("*.py"))
    assert modules, f"no modules under {ENGINE}"
    found = []
    for path in modules:
        for line, name in _imports(path):
            top = name.split(".")[0]
            outside_engine = top == "fastro" and name != "fastro.engine" and not name.startswith("fastro.engine.")
            if top in CLI_LIBRARIES or outside_engine:
                found.append(f"{path.relative_to(ENGINE.parent)}:{line} imports {name}")
    return found


def test_the_engine_imports_nothing_from_the_cli_or_the_rest_of_fastro() -> None:
    assert _violations() == []


PACKAGE = ENGINE.parent
RUNS_PROJECT_CODE = PACKAGE / "runner.py"
RUNS_GIT_ONLY = PACKAGE / "apply.py"
PROCESS_MODULES = {"subprocess", "multiprocessing", "pty", "asyncio.subprocess"}
OS_STARTERS = re.compile(r"^(system|popen|exec\w*|spawn\w*|posix_spawn\w*|fork\w*)$")
ASYNCIO_STARTERS = re.compile(r"^create_subprocess_\w+$")


def _process_module(name: str) -> bool:
    return any(name == module or name.startswith(f"{module}.") for module in PROCESS_MODULES)


def _starts_a_process(tree: ast.AST) -> list[tuple[int, str]]:
    """Each place in ``tree`` that can start a process: a process module imported, by statement or by name, or one of
    ``os``'s or ``asyncio``'s process starters imported or used."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names if _process_module(alias.name)]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = [alias.name for alias in node.names]
            if _process_module(module):
                found.append((node.lineno, module))
            elif module == "os":
                found += [(node.lineno, f"os.{name}") for name in names if OS_STARTERS.match(name)]
            elif module == "asyncio":
                found += [
                    (node.lineno, f"asyncio.{name}") for name in names if ASYNCIO_STARTERS.match(name) or name == "subprocess"
                ]
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            starter = OS_STARTERS if node.value.id == "os" else ASYNCIO_STARTERS if node.value.id == "asyncio" else None
            if starter is not None and starter.match(node.attr):
                found.append((node.lineno, f"{node.value.id}.{node.attr}"))
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            loader = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if loader in ("__import__", "import_module") and _process_module(str(node.args[0].value)):
                found.append((node.lineno, str(node.args[0].value)))
    return found


def _runs_only_git(tree: ast.AST) -> list[int]:
    """The line of each ``subprocess`` call in ``tree`` whose argv doesn't start with the name ``git``."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            if node.func.value.id != "subprocess":
                continue
            argv = node.args[0] if node.args else None
            first = argv.elts[0] if isinstance(argv, ast.List) and argv.elts else None
            if not (isinstance(first, ast.Name) and first.id == "git"):
                found.append(node.lineno)
    return found


def test_only_the_runner_starts_a_process() -> None:
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path in (RUNS_PROJECT_CODE, RUNS_GIT_ONLY):
            continue
        tree = ast.parse(path.read_text(), str(path))
        found += [f"{path.relative_to(PACKAGE)}:{line} {name}" for line, name in _starts_a_process(tree)]
    assert found == []


def test_apply_starts_only_git() -> None:
    assert _runs_only_git(ast.parse(RUNS_GIT_ONLY.read_text())) == []


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess",
        "import os.path, subprocess as sp",
        "from subprocess import run",
        "import multiprocessing",
        "import pty",
        "import asyncio.subprocess",
        "from asyncio import create_subprocess_exec",
        "from asyncio import subprocess",
        "asyncio.create_subprocess_shell('x')",
        "os.system('x')",
        "os.popen('x')",
        "os.execv('x', [])",
        "os.spawnl(0, 'x')",
        "os.posix_spawn('x', [], {})",
        "os.fork()",
        "from os import popen",
        "from os import execvp",
        "__import__('subprocess')",
        "importlib.import_module('subprocess')",
    ],
)
def test_each_way_to_start_a_process_is_caught(source: str) -> None:
    assert _starts_a_process(ast.parse(source)) != []


@pytest.mark.parametrize(
    "source", ["import os", "os.path.join('a')", "from os import environ", "import asyncio", "os.getcwd()"]
)
def test_what_starts_no_process_isn_t_caught(source: str) -> None:
    assert _starts_a_process(ast.parse(source)) == []


@pytest.mark.parametrize("source", ["subprocess.run(['sh'])", "subprocess.run(argv)", "subprocess.Popen([uv, 'run'])"])
def test_a_subprocess_that_isn_t_git_is_caught_in_apply(source: str) -> None:
    assert _runs_only_git(ast.parse(source)) != []
