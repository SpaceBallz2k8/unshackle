"""
webui/jobs.py

Runs unshackle dl via CliRunner in a thread pool.
Uses the real unshackle CLI entry point so all internals (CDM, vaults, etc.)
work exactly as they do on the command line.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import aiosqlite

log = logging.getLogger("webui.jobs")

DB_PATH = os.environ.get("DATABASE_URL", "/data/unshackle.db")

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="unshackle-dl")
_queues: dict[str, "JobQueue"] = {}
_running: dict[str, bool] = {}
_cancel_flags: dict[str, bool] = {}


# ── Simple in-process event bus ────────────────────────────────────────────────

class JobQueue:
    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subscribers: list[asyncio.Queue] = []
        self._buffer: list[dict] = []
        import threading
        self._lock = threading.Lock()

    def set_loop(self, loop):
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.append(q)
            for evt in self._buffer:
                q.put_nowait(evt)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def _emit(self, evt: dict):
        with self._lock:
            if evt.get("type") in ("log", "error", "status"):
                self._buffer.append(evt)
            subs = list(self._subscribers)
        if self._loop and not self._loop.is_closed():
            for q in subs:
                try:
                    self._loop.call_soon_threadsafe(q.put_nowait, evt)
                except RuntimeError:
                    pass

    def log(self, msg: str):
        self._emit({"type": "log", "message": msg})

    def error(self, msg: str):
        self._emit({"type": "error", "message": msg})

    def status(self, s: str):
        self._emit({"type": "status", "message": s})

    def progress(self, task_id: str, description: str, completed: float, total: float, speed: str = ""):
        self._emit({
            "type": "progress",
            "message": description,
            "task_id": task_id,
            "completed": completed,
            "total": total,
            "speed": speed,
        })


def get_queue(job_id: str) -> Optional[JobQueue]:
    return _queues.get(job_id)


# ── Database helpers ───────────────────────────────────────────────────────────

async def _db_set(job_id: str, **fields):
    sets = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [job_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE jobs SET {sets}, updated_at = datetime('now') WHERE id = ?", vals
        )
        await db.commit()


async def _db_append_log(job_id: str, line: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE jobs SET log = log || ?, updated_at = datetime('now') WHERE id = ?",
            (line + "\n", job_id),
        )
        await db.commit()


# ── Public API ─────────────────────────────────────────────────────────────────

async def create_job(service: str, content_id: str, title: str = "", extra_args: Optional[list] = None) -> str:
    job_id = str(uuid.uuid4())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO jobs (id, service, content_id, title, status) VALUES (?, ?, ?, ?, 'queued')",
            (job_id, service, content_id, title or content_id),
        )
        await db.commit()
    asyncio.create_task(_run_job(job_id, service, content_id, extra_args or []))
    return job_id


def cancel_job(job_id: str):
    _cancel_flags[job_id] = True


async def _run_job(job_id: str, service: str, content_id: str, extra_args: list):
    loop = asyncio.get_event_loop()
    q = JobQueue()
    q.set_loop(loop)
    _queues[job_id] = q

    await _db_set(job_id, status="running")
    q.status("running")

    # Persist log lines to DB as they arrive
    log_sub = q.subscribe()
    persist_done = asyncio.Event()

    async def _persist():
        while True:
            try:
                evt = await asyncio.wait_for(log_sub.get(), timeout=2.0)
                if evt["type"] in ("log", "error"):
                    await _db_append_log(job_id, evt["message"])
                if evt["type"] == "status" and evt["message"] not in ("queued", "running"):
                    break
            except asyncio.TimeoutError:
                if not _running.get(job_id):
                    break
        persist_done.set()

    persist_task = asyncio.create_task(_persist())

    def _thread():
        try:
            _do_download(job_id, service, content_id, extra_args, q)
        except Exception as e:
            q.error(f"Fatal: {e}")
            q.status("failed")
        finally:
            _running.pop(job_id, None)

    _running[job_id] = True
    loop.run_in_executor(_executor, _thread)

    # Wait for completion signal
    status_sub = q.subscribe()
    final = "failed"
    while True:
        try:
            evt = await asyncio.wait_for(status_sub.get(), timeout=600.0)
            if evt["type"] == "status" and evt["message"] not in ("queued", "running"):
                final = evt["message"]
                break
        except asyncio.TimeoutError:
            if not _running.get(job_id):
                break

    await persist_done
    await _db_set(job_id, status=final)
    q.unsubscribe(status_sub)
    q.unsubscribe(log_sub)
    _queues.pop(job_id, None)


def _do_download(job_id: str, service: str, content_id: str, extra_args: list, q: JobQueue):
    """
    Run `unshackle dl SERVICE CONTENT_ID [extra_args]` via CliRunner.
    We capture stdout/stderr and stream lines to the JobQueue in real time.
    """
    import io
    import threading
    from click.testing import CliRunner
    from unshackle.core.__main__ import main

    q.log(f"$ unshackle dl {service} {content_id} {' '.join(extra_args)}")

    # Use a pipe to get streaming output
    r_fd, w_fd = None, None
    try:
        import os as _os
        r_fd, w_fd = _os.pipe()
        w_file = _os.fdopen(w_fd, "w", buffering=1)
        r_file = _os.fdopen(r_fd, "r", buffering=1)

        output_lines = []
        read_done = threading.Event()

        def _reader():
            for line in r_file:
                line = line.rstrip()
                if line:
                    output_lines.append(line)
                    q.log(line)
            read_done.set()

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        runner = CliRunner(mix_stderr=True)
        args = ["dl", service, content_id] + extra_args

        result = runner.invoke(
            main,
            args,
            catch_exceptions=True,
        )

        w_file.close()
        read_done.wait(timeout=5)

        # Also emit any output that came through CliRunner's capture
        if result.output:
            for line in result.output.splitlines():
                if line.strip() and line not in output_lines:
                    q.log(line)

        if result.exception and not isinstance(result.exception, SystemExit):
            import traceback as tb
            q.error(f"Exception: {result.exception}")
            q.error(tb.format_exc())

        if result.exit_code == 0:
            q.status("completed")
        else:
            q.status("failed")

    except Exception as e:
        q.error(f"Runner error: {e}")
        q.status("failed")
    finally:
        try:
            if w_fd and not w_file.closed:
                w_file.close()
        except Exception:
            pass
