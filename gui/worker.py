"""Background job runner for the GUI: runs one pipeline job at a time in a
thread, redirects its stdout into a queue for the Tk main loop to poll, and
supports cooperative cancellation via a shared threading.Event.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import traceback
from typing import Any, Callable, Optional

DONE_SENTINEL = "__job_done__"


class _QueueWriter:
    """File-like object that splits writes on newlines and pushes complete
    lines onto a queue.Queue for the GUI to drain."""

    def __init__(self, log_queue: "queue.Queue[str]") -> None:
        self._queue = log_queue
        self._buffer = ""

    def write(self, text: str) -> int:
        if not text:
            return 0
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._queue.put(line)
        return len(text)

    def flush(self) -> None:
        if self._buffer:
            self._queue.put(self._buffer)
            self._buffer = ""


class Worker:
    """Runs a single callable in a background thread, one job at a time.

    Usage:
        worker = Worker()
        worker.start(some_fn, arg1, arg2, kwarg=value)
        # poll worker.log_queue on the Tk main loop via root.after(...)
        # worker.cancel() to request cooperative cancellation
    """

    def __init__(self) -> None:
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.cancel_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
        """Start fn(*args, **kwargs) in a background thread. Returns False
        (and does nothing) if a job is already running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self.cancel_event = threading.Event()
            self._thread = threading.Thread(
                target=self._run, args=(fn, args, kwargs), daemon=True
            )
            self._thread.start()
            return True

    def cancel(self) -> None:
        self.cancel_event.set()

    def _run(self, fn: Callable[..., Any], args: tuple, kwargs: dict) -> None:
        writer = _QueueWriter(self.log_queue)
        returncode: Any = 0
        error: Optional[str] = None
        try:
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                returncode = fn(*args, cancel_event=self.cancel_event, **kwargs)
        except SystemExit as exc:
            returncode = exc.code
        except Exception:
            error = traceback.format_exc()
        finally:
            writer.flush()
            if error:
                self.log_queue.put(error)
            self.log_queue.put(f"{DONE_SENTINEL}:{returncode}")
