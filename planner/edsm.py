"""EDSM validation and caching."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from planner_strategic.models import Candidate, JobProgress

logger = logging.getLogger(__name__)


@dataclass
class EDSMClient:
    base_url: str = "https://www.edsm.net/api-v1/system"
    requests_per_sec: float = 1.0
    timeout_sec: float = 10.0
    max_retries: int = 2
    backoff_base_sec: float = 0.5

    _last_request_at: Optional[float] = None

    def exists_system(self, system_name: str) -> bool:
        response = self._fetch(system_name)
        if isinstance(response, dict):
            if response.get("name") or response.get("id"):
                return True
        return False

    def _fetch(self, system_name: str) -> dict:
        params = urllib.parse.urlencode({"systemName": system_name, "showId": 1})
        url = f"{self.base_url}?{params}"
        attempt = 0
        while True:
            try:
                self._throttle()
                with urllib.request.urlopen(url, timeout=self.timeout_sec) as handle:
                    payload = handle.read().decode("utf-8")
                return json.loads(payload) if payload else {}
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    self._backoff(attempt)
                    attempt += 1
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt < self.max_retries:
                    self._backoff(attempt)
                    attempt += 1
                    continue
                raise

    def _throttle(self) -> None:
        if self.requests_per_sec <= 0:
            return
        min_interval = 1.0 / self.requests_per_sec
        now = time.monotonic()
        if self._last_request_at is not None:
            elapsed = now - self._last_request_at
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _backoff(self, attempt: int) -> None:
        time.sleep(self.backoff_base_sec * (2**attempt))


def filter_undiscovered(
    candidates: list[Candidate],
    working_db_path: str,
    client: EDSMClient,
    progress_cb: Optional[callable] = None,
    cache_max_age_days: int = 7,
    min_keep: int = 10,
    max_checks: int = 200,
    invalid_systems: Optional[set[str]] = None,
) -> list[Candidate]:
    conn = sqlite3.connect(working_db_path)
    try:
        kept: list[Candidate] = []
        invalid_set = invalid_systems or set()
        capped = candidates[: max_checks if max_checks > 0 else len(candidates)]
        total = len(capped)
        checks = 0
        for idx, candidate in enumerate(capped, start=1):
            # Skip if marked as invalid (non-existent in game)
            if candidate.system_name in invalid_set:
                continue
            exists = _cached_exists(conn, candidate.system_name, cache_max_age_days)
            if exists is None:
                try:
                    exists = client.exists_system(candidate.system_name)
                except Exception:
                    logger.warning("EDSM validation failed for %s", candidate.system_name)
                    exists = False
                _write_cache(conn, candidate.system_name, exists)
            if not exists:
                kept.append(candidate)
            checks += 1
            _emit(
                progress_cb,
                JobProgress(
                    phase="validate",
                    current=idx,
                    total=total,
                    message=f"Checked {candidate.system_name}",
                ),
            )
            if min_keep > 0 and len(kept) >= min_keep:
                break
        _emit(
            progress_cb,
            JobProgress(
                phase="validate",
                current=checks,
                total=total,
                message=f"Validation complete: {checks} checked, {len(kept)} kept",
            ),
        )
        conn.commit()
        return kept
    finally:
        conn.close()


def _cached_exists(conn: sqlite3.Connection, system_name: str, max_age_days: int) -> Optional[bool]:
    row = conn.execute(
        "SELECT \"exists\", checked_at FROM edsm_cache WHERE system_name = ?",
        (system_name,),
    ).fetchone()
    if not row:
        return None
    exists, checked_at = row
    try:
        checked_time = datetime.fromisoformat(checked_at)
    except ValueError:
        return None
    if datetime.now(timezone.utc) - checked_time > timedelta(days=max_age_days):
        return None
    return bool(exists)


def _write_cache(conn: sqlite3.Connection, system_name: str, exists: bool) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO edsm_cache(system_name, "exists", checked_at)
        VALUES (?, ?, ?)
        """,
        (system_name, int(exists), datetime.now(timezone.utc).isoformat()),
    )


def _emit(progress_cb: Optional[callable], progress: JobProgress) -> None:
    if progress_cb:
        progress_cb(progress)
