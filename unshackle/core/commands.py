from typing import Any, Optional

import click

from unshackle.core.config import config
from unshackle.core.utilities import import_module_by_path

_COMMANDS = sorted(
    (path for path in config.directories.commands.glob("*.py") if path.stem.lower() != "__init__"), key=lambda x: x.stem
)

_MODULES: dict[str, Any] = {}

for path in _COMMANDS:
    try:
        mod = import_module_by_path(path)
        # Try to find a command matching the filename, then fallback to 'cli'
        cmd = getattr(mod, path.stem, getattr(mod, "cli", None))
        if cmd:
            _MODULES[path.stem] = cmd
    except Exception:
        continue

# Explicitly register the webui command to ensure it's available even if dynamic discovery fails in Docker
try:
    from unshackle.commands.webui import webui
    _MODULES["webui"] = webui
except (ImportError, AttributeError):
    pass


class Commands(click.MultiCommand):
    """Lazy-loaded command group of project commands."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        """Returns a list of command names from the command filenames and registered modules."""
        return sorted(_MODULES.keys())

    def get_command(self, ctx: click.Context, name: str) -> Optional[click.Command]:
        """Load the command code and return the main click command function."""
        module = _MODULES.get(name)
        if not module:
            raise click.ClickException(f"Unable to find command by the name '{name}'")

        if hasattr(module, "cli"):
            return module.cli

        return module


# Hide direct access to commands from quick import form, they shouldn't be accessed directly
__all__ = ("Commands",)
