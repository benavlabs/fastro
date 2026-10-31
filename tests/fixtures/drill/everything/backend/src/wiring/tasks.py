"""The modules whose background tasks the worker registers."""

TASK_MODULES: tuple[str, ...] = ('src.infrastructure.taskiq.email', 'src.modules.api_keys.tasks')
