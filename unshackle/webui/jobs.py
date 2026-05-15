"""
webui/jobs.py

Manages download jobs. Calls unshackle's dl command internals directly
in a thread pool — no subprocess, no PTY, no output parsing.

Each job gets its own JobQueue (the event bus) and WebConsole (Rich shim).
"""
from __future__ import annotations

import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import aiosqlite

from unshackle.webui.console import JobQueue, WebConsole, WebProgress

DB_PATH_ENV = "DATABASE_URL"
import os
DB_PATH = os.environ.get(DB_PATH_ENV, "/data/unshackle.db")

# Thread pool for running blocking dl work
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="unshackle-dl")

# In-memory map of job_id -> JobQueue (for live SSE streaming)
_queues: dict[str, JobQueue] = {}


# ── Database helpers ───────────────────────────────────────────────────────────

async def _db_set(job_id: str, **fields):
    sets = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [job_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE jobs SET {sets}, updated_at = datetime('now') WHERE id = ?", vals)
        await db.commit()


async def _db_append_log(job_id: str, line: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE jobs SET log = log || ?, updated_at = datetime('now') WHERE id = ?",
            (line + "\n", job_id),
        )
        await db.commit()


# ── Public API ─────────────────────────────────────────────────────────────────

def get_queue(job_id: str) -> Optional[JobQueue]:
    return _queues.get(job_id)


async def create_job(
    service: str,
    content_id: str,
    title: str = "",
    extra_args: Optional[list] = None,
) -> str:
    job_id = str(uuid.uuid4())

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO jobs (id, service, content_id, title, status) VALUES (?, ?, ?, ?, 'queued')",
            (job_id, service, content_id, title or content_id),
        )
        await db.commit()

    asyncio.create_task(_run_job(job_id, service, content_id, extra_args or []))
    return job_id


async def _run_job(job_id: str, service: str, content_id: str, extra_args: list):
    loop = asyncio.get_event_loop()

    # Create the event bus for this job
    q = JobQueue()
    q.set_loop(loop)
    _queues[job_id] = q

    await _db_set(job_id, status="running")
    q.emit_status("running")

    # Drain the queue and write to DB + SSE
    async def _drain():
        while True:
            try:
                evt = await asyncio.wait_for(q.subscribe().__class__(), timeout=0.1)
            except Exception:
                break

    # Subscribe to the queue so we can persist log lines to DB
    log_q = q.subscribe()

    async def _persist_loop():
        """Write log/error events to DB as they arrive."""
        while True:
            try:
                evt = await asyncio.wait_for(log_q.get(), timeout=1.0)
                if evt.type in ("log", "error"):
                    await _db_append_log(job_id, evt.message)
                elif evt.type == "status" and evt.message not in ("queued", "running"):
                    break
            except asyncio.TimeoutError:
                # Check if job thread is done
                if not _running.get(job_id):
                    break

    persist_task = asyncio.create_task(_persist_loop())

    # Run the blocking dl work in a thread
    def _run_in_thread():
        try:
            _do_download(job_id, service, content_id, extra_args, q, loop)
        except Exception as e:
            q.emit_error(f"Fatal error: {e}")
            q.emit_status("failed")
        finally:
            _running.pop(job_id, None)

    _running[job_id] = True
    loop.run_in_executor(_executor, _run_in_thread)

    # Wait for the status to resolve
    status_q = q.subscribe()
    final_status = "failed"
    while True:
        try:
            evt = await asyncio.wait_for(status_q.get(), timeout=300.0)
            if evt.type == "status" and evt.message not in ("queued", "running"):
                final_status = evt.message
                break
        except asyncio.TimeoutError:
            if not _running.get(job_id):
                break

    await persist_task
    await _db_set(job_id, status=final_status)
    q.unsubscribe(status_q)
    q.unsubscribe(log_q)
    _queues.pop(job_id, None)


_running: dict[str, bool] = {}
_cancel_flags: dict[str, bool] = {}


def cancel_job(job_id: str):
    """Signal a running job to stop (best-effort)."""
    _cancel_flags[job_id] = True


def _do_download(
    job_id: str,
    service_name: str,
    content_id: str,
    extra_args: list,
    q: JobQueue,
    loop: asyncio.AbstractEventLoop,
):
    """
    The actual download logic — runs in a thread.
    Imports unshackle internals and invokes them directly.
    """
    import click
    from unshackle.config import Config
    from unshackle.constants import context_settings
    from unshackle.utilities import get_binary_path

    q.emit_log(f"Starting: {service_name} {content_id}")

    # Build a minimal Click context that mirrors what `unshackle dl` provides
    # We use invoke_without_command to get the root ctx with config loaded
    try:
        from unshackle import __main__ as _main
        from unshackle.commands.dl import dl as DlClass
    except ImportError as e:
        q.emit_error(f"Failed to import unshackle internals: {e}")
        q.emit_status("failed")
        return

    # Build args list: unshackle dl SERVICE CONTENT_ID [extra_args]
    # We monkey-patch the console on the context so all output goes to our queue
    args = [service_name, content_id] + extra_args

    try:
        # Create a fake context that the dl command expects
        # The cleanest way: use Click's standalone_mode=False to get the object back
        web_console = WebConsole(q)

        # Invoke via Click in standalone mode so exceptions propagate cleanly
        from click.testing import CliRunner
        runner = CliRunner(mix_stderr=False)

        # We need to patch the console BEFORE the dl command instantiates
        # Patch at the module level — unshackle uses a module-level `console`
        import unshackle.utilities as _utils
        import unshackle.commands.dl as _dl_mod

        original_console = getattr(_dl_mod, 'console', None)
        _dl_mod.console = web_console

        # Also patch rich.progress.Progress so download bars come to us
        import unshackle.commands.dl as _dl_mod2
        original_progress_cls = None
        try:
            import rich.progress as _rp
            original_progress_cls = _rp.Progress
            # Create a factory that returns WebProgress bound to our queue
            class _WebProgressFactory:
                def __new__(cls, *a, **kw):
                    return WebProgress(q)
            _rp.Progress = _WebProgressFactory
        except Exception:
            pass

        result = runner.invoke(
            _main.cli,
            ["dl"] + args,
            catch_exceptions=False,
        )

        # Restore patches
        if original_console is not None:
            _dl_mod.console = original_console
        if original_progress_cls is not None:
            _rp.Progress = original_progress_cls

        if result.output:
            for line in result.output.splitlines():
                if line.strip():
                    q.emit_log(line)

        if result.exit_code == 0:
            q.emit_status("completed")
        else:
            if result.exception:
                q.emit_error(str(result.exception))
            q.emit_status("failed")

    except Exception as e:
        q.emit_error(f"Error: {e}")
        q.emit_status("failed")
