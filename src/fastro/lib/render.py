"""Jinja-based template rendering.

``Renderer`` loads the templates in a directory and renders one with a
context, refusing any name the context doesn't define.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined


class Renderer:
    """Render Jinja templates from one directory."""

    def __init__(self, templates_root: Path) -> None:
        if not templates_root.is_dir():
            raise FileNotFoundError(f"Templates directory does not exist: {templates_root}")
        self.templates_root = templates_root
        self.env = Environment(
            loader=FileSystemLoader(str(templates_root)),
            keep_trailing_newline=True,
            undefined=StrictUndefined,
            autoescape=False,
        )

    def render(self, template_path: str, context: dict[str, Any]) -> str:
        """Render ``template_path`` (relative to ``templates_root``) with ``context``."""
        template = self.env.get_template(template_path)
        return template.render(**context)
