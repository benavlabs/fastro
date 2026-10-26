# fastro

`fastro` is the command-line tool for Fastro projects. It generates
deployment artifacts, helps prepare the runtime environment, and serves as
the host for plugin commands and feature generators.

## Install

From a checkout of this repo:

```bash
uv tool install .
fastro --help
```

To work on `fastro` itself:

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/fastro --help
```

## What's here

```
src/fastro/
├── cli.py                 root Typer app + plugin discovery
├── runner.py              runs a project's scripts in its own interpreter
├── plugins.py             entry-point loaders for bp.commands and bp.features
├── commands/              in-tree command sub-apps
│   ├── deploy.py          fastro deploy generate <mode>
│   └── env.py             fastro env gen-secret / fastro env validate
├── features/              feature framework (manifest, plan, installer)
│   └── _builtins/         in-tree features
│       └── deploy/        compose/Dockerfile templates for local/prod/nginx
└── lib/                   shared helpers (project discovery, prompts, render)
```

## Plugin extension points

Two kinds of plugins, kept deliberately separate:

- `bp.commands` entry-point group: third-party Typer sub-apps mounted under
  `fastro <name>` (e.g. `fastro aws deploy`).
- `bp.features` entry-point group: code generators with a manifest that
  `bp feature` can list, install, and remove.

See `src/fastro/plugins.py` for the discovery contracts.
