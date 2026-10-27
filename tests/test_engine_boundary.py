"""``fastro.engine`` imports nothing from the CLI's libraries and nothing from the rest of fastro."""

import ast
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / "src" / "fastro" / "engine"

CLI_LIBRARIES = {"typer", "click", "rich", "jinja2", "questionary", "prompt_toolkit", "httpx"}


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
