# fastro

`fastro` is the command-line tool for Fastro projects. It generates
deployment artifacts, helps prepare the runtime environment, and serves as
the host for plugin commands.

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
├── plugins.py             entry-point loader for bp.commands
├── commands/              in-tree command sub-apps
│   ├── deploy.py          fastro deploy generate <mode>
│   └── env.py             fastro env gen-secret / fastro env validate
├── _legacy_deploy/        compose and nginx templates for local/prod/nginx
└── lib/                   shared helpers (project discovery, prompts, render)
```

## Plugin extension point

The `bp.commands` entry-point group mounts third-party Typer sub-apps under
`fastro <name>` (e.g. `fastro aws deploy`).

See `src/fastro/plugins.py` for the discovery contract.
