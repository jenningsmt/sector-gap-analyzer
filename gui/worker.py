"""Background job runner for the GUI: runs one pipeline job at a time in a
thread, redirects its stdout into a queue for the Tk main loop to poll, and
supports cooperative cancellation via a shared threading.Event.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import traceback
import uuid
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
        # poll worker.confirm_queue the same way for pending confirmation
        # requests (see request_confirmation/answer_confirmation below)
        # worker.cancel() to request cooperative cancellation
    """

    def __init__(self) -> None:
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.confirm_queue: "queue.Queue[dict]" = queue.Queue()
        self.cancel_event = threading.Event()
        self._confirm_events: dict[str, threading.Event] = {}
        self._confirm_answers: dict[str, bool] = {}
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
            self._confirm_events.clear()
            self._confirm_answers.clear()
            self._thread = threading.Thread(
                target=self._run, args=(fn, args, kwargs), daemon=True
            )
            self._thread.start()
            return True

    def cancel(self) -> None:
        self.cancel_event.set()
        # Unblock anything currently waiting on a confirmation dialog so
        # cancel always takes effect immediately, even mid-prompt.
        for event in list(self._confirm_events.values()):
            event.set()

    def request_confirmation(self, payload: dict) -> bool:
        """Called from the WORKER thread (e.g. from within pipeline code) to
        pause and ask the user a yes/no question before continuing -- e.g.
        "this sector looks huge, proceed anyway?". Blocks the worker thread
        (never the GUI) until the main thread calls answer_confirmation()
        for this same request, or the job is cancelled. Returns True to
        proceed, False to decline/abort."""
        req_id = str(uuid.uuid4())
        event = threading.Event()
        self._confirm_events[req_id] = event
        self.confirm_queue.put({"id": req_id, **payload})
        event.wait()
        answer = self._confirm_answers.pop(req_id, False)
        self._confirm_events.pop(req_id, None)
        return answer and not self.cancel_event.is_set()

    def answer_confirmation(self, req_id: str, proceed: bool) -> None:
        """Called from the GUI (main) thread once the user has responded to
        a confirmation request drained from confirm_queue."""
        self._confirm_answers[req_id] = proceed
        event = self._confirm_events.get(req_id)
        if event is not None:
            event.set()

    def _run(self, fn: Callable[..., Any], args: tuple, kwargs: dict) -> None:
        writer = _QueueWriter(self.log_queue)
        returncode: Any = 0
        error: Optional[str] = None
        try:
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                returncode = fn(
                    *args,
                    cancel_event=self.cancel_event,
                    confirm_cb=self.request_confirmation,
                    **kwargs,
                )
        except SystemExit as exc:
            returncode = exc.code
        except Exception:
            error = traceback.format_exc()
        finally:
            writer.flush()
            if error:
                self.log_queue.put(error)
            self.log_queue.put(f"{DONE_SENTINEL}:{returncode}")
