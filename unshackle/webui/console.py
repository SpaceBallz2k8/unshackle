"""
webui/console.py

A drop-in replacement for rich.console.Console that, instead of printing to
stdout, emits every log line and progress update to an asyncio Queue.

The dl command and service code call self.log.info() / self.console.log() etc.
We intercept all of that here so the web UI gets live structured events with
zero subprocess/PTY hackery.
"""
from __future__ import annotations

import asyncio
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
)
from rich.text import Text

# ANSI stripper – progress callbacks give us rich markup or ANSI
_ANSI = re.compile(r"\x1b\[[0-9;]*[mGKHFJKST]?|\x1b\].*?(?:\x07|\x1b\\)")


def _strip(s: str) -> str:
    return _ANSI.sub("", s).strip()


@dataclass
class WebEvent:
    """A single event pushed to the browser."""
    type: str          # "log" | "progress" | "status" | "error"
    message: str = ""
    task_id: Optional[str] = None
    completed: float = 0
    total: float = 0
    speed: str = ""
    elapsed: str = ""
    extra: dict = field(default_factory=dict)


class JobQueue:
    """
    Per-job event bus. The FastAPI SSE endpoint subscribes to this.
    Thread-safe: dl runs in a thread pool, FastAPI is async.
    """

    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subscribers: list[asyncio.Queue] = []
        self._lock = threading.Lock()
        self._log_buffer: list[WebEvent] = []  # replay for late subscribers

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.append(q)
            # Replay buffered events so late-joining clients see full history
            for evt in self._log_buffer:
                q.put_nowait(evt)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def emit(self, event: WebEvent):
        """Called from the dl thread — thread-safe push to all async queues."""
        with self._lock:
            if event.type in ("log", "error", "status"):
                self._log_buffer.append(event)
            subs = list(self._subscribers)

        if self._loop and not self._loop.is_closed():
            for q in subs:
                try:
                    self._loop.call_soon_threadsafe(q.put_nowait, event)
                except RuntimeError:
                    pass

    def emit_log(self, message: str):
        self.emit(WebEvent(type="log", message=message))

    def emit_error(self, message: str):
        self.emit(WebEvent(type="error", message=message))

    def emit_status(self, status: str):
        self.emit(WebEvent(type="status", message=status))

    def emit_progress(
        self,
        task_id: str,
        description: str,
        completed: float,
        total: float,
        speed: str = "",
        elapsed: str = "",
    ):
        self.emit(WebEvent(
            type="progress",
            message=description,
            task_id=task_id,
            completed=completed,
            total=total,
            speed=speed,
            elapsed=elapsed,
        ))


class WebConsole(Console):
    """
    Subclass of rich.Console that intercepts print/log calls and forwards
    them to a JobQueue instead of stdout.

    Rich's Progress widget is also replaced – see WebProgress below.
    """

    def __init__(self, job_queue: JobQueue, **kwargs):
        # Force no real output — we're the only consumer
        super().__init__(quiet=True, **kwargs)
        self._job_queue = job_queue

    # ── Intercept the two main output methods ─────────────────────────────────

    def print(self, *args, **kwargs):
        """Called by service code and dl internals for regular messages."""
        parts = []
        for a in args:
            if isinstance(a, str):
                parts.append(_strip(a))
            elif isinstance(a, Text):
                parts.append(_strip(a.plain))
            else:
                parts.append(_strip(str(a)))
        msg = " ".join(parts)
        if msg:
            self._job_queue.emit_log(msg)

    def log(self, *args, **kwargs):
        self.print(*args, **kwargs)

    def rule(self, title="", **kwargs):
        """Section dividers like ─── Title ─── become plain log lines."""
        if title:
            msg = _strip(str(title)) if not isinstance(title, str) else title.strip()
            if msg:
                self._job_queue.emit_log(f"── {msg} ──")


class WebProgress:
    """
    Drop-in for rich.progress.Progress.
    Tracks active download tasks and emits progress events to the JobQueue.
    """

    def __init__(self, job_queue: JobQueue):
        self._queue = job_queue
        self._tasks: dict[TaskID, dict] = {}
        self._next_id = 0

    # Context manager no-ops (Progress is used as `with Progress(...) as p:`)
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def add_task(
        self,
        description: str,
        total: Optional[float] = 100,
        **kwargs,
    ) -> TaskID:
        tid = TaskID(self._next_id)
        self._next_id += 1
        self._tasks[tid] = {
            "description": _strip(str(description)),
            "completed": 0,
            "total": total or 100,
        }
        self._queue.emit_progress(str(tid), _strip(str(description)), 0, total or 100)
        return tid

    def update(
        self,
        task_id: TaskID,
        *,
        advance: float = 0,
        completed: Optional[float] = None,
        description: Optional[str] = None,
        total: Optional[float] = None,
        **kwargs,
    ):
        if task_id not in self._tasks:
            return
        t = self._tasks[task_id]
        if description is not None:
            t["description"] = _strip(str(description))
        if total is not None:
            t["total"] = total
        if completed is not None:
            t["completed"] = completed
        else:
            t["completed"] = min(t["completed"] + advance, t["total"])

        self._queue.emit_progress(
            str(task_id),
            t["description"],
            t["completed"],
            t["total"],
        )

    def stop(self):
        pass

    def start(self):
        pass
