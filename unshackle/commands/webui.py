"""
commands/webui.py

Adds `unshackle webui` command to the CLI.
Run with: uv run unshackle webui
"""
from __future__ import annotations

import click


@click.command(short_help="Start the Unshackle Web UI server")
@click.option("--host", default="0.0.0.0", show_default=True, help="Host to bind to")
@click.option("--port", default=8080, show_default=True, help="Port to listen on")
@click.option("--reload", is_flag=True, default=False, help="Auto-reload on code changes (dev mode)")
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
    try:
        import uvicorn
    except ImportError:
        click.secho("Error: The 'uvicorn' package is required for the Web UI.", fg="red", err=True)
        click.echo("Please add it to your project dependencies (e.g., run 'uv add uvicorn').", err=True)
        ctx.exit(1)

    click.echo(f"Starting Unshackle Web UI on http://{host}:{port}")
    click.echo("Press CTRL+C to stop.")

    uvicorn.run(
        "unshackle.webui.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )
