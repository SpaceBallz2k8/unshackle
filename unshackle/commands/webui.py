"""
commands/webui.py — `unshackle webui` command.

Discovered automatically by Commands via config.directories.commands glob.
"""
from __future__ import annotations

import click


@click.command(name="webui", short_help="Start the Unshackle Web UI server")
@click.option("--host", default="0.0.0.0", show_default=True, help="Host to bind to")
@click.option("--port", default=8080, show_default=True, help="Port to listen on")
@click.option("--reload", is_flag=True, default=False, help="Auto-reload on code changes (dev only)")
@click.pass_context
def webui(ctx: click.Context, host: str, port: int, reload: bool):
    """
    Start the Unshackle Web UI.

    \b
    Opens a browser-based interface for searching, browsing,
    and downloading content from all configured services.

    \b
    Access at: http://<host>:<port>
    """
    import uvicorn
    click.echo(f"Starting Unshackle Web UI at http://{host}:{port}")
    uvicorn.run(
        "unshackle.webui.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


# Commands class looks for either module.stem or "cli" attribute
cli = webui
