import os
import gc
import sqlite3
import tempfile
import unittest
import warnings
from contextlib import contextmanager
from unittest.mock import patch

from app.config import resolve_sector_db_path
from planner_strategic.job import run_strategic_plan
from planner_strategic.models import JobParams
from contextlib import closing

warnings.filterwarnings("ignore", category=ResourceWarning)


def _close_tracked_connections(tracked: list[sqlite3.Connection]) -> None:
    seen: set[int] = set()
    for conn in reversed(tracked):
        conn_id = id(conn)
        if conn_id in seen:
            continue
        seen.add(conn_id)
        try:
            if conn.in_transaction:
                conn.rollback()
        except Exception:
            pass
        try:
            sqlite3.Connection.close(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass


def _run_strategic_plan_closed(*args, **kwargs):
    tracked: list[sqlite3.Connection] = []
    original_connect = sqlite3.connect

    def _tracked_connect(*c_args, **c_kwargs):
        conn = original_connect(*c_args, **c_kwargs)
        tracked.append(conn)
        return conn

    try:
        with patch("sqlite3.connect", side_effect=_tracked_connect):
            return run_strategic_plan(*args, **kwargs)
    finally:
        _close_tracked_connections(tracked)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            gc.collect()


class FakeEDSMClient:
    def __init__(self, reject_first: int) -> None:
        self._reject_first = reject_first
        self._count = 0

    def exists_system(self, system_name: str) -> bool:
        self._count += 1
        return self._count <= self._reject_first


def _create_sector_db(path: str) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            CREATE TABLE systems(
                name TEXT PRIMARY KEY,
                x REAL NOT NULL,
                y REAL NOT NULL,
                z REAL NOT NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO systems(name, x, y, z) VALUES (?, ?, ?, ?)",
            [
                ("Eotchorts FG-X d1-318", 0.0, 0.0, 0.0),
                ("Eotchorts FG-X d1-1", 0.0, 0.0, 0.0),
                ("Eotchorts FG-X d1-2", 1.0, 0.0, 0.0),
                ("Eotchorts FG-X d1-4", 2.0, 0.0, 0.0),
                ("Eotchorts FG-X d1-5", 3.0, 0.0, 0.0),
                ("Eotchorts FG-X d1-320", 10.0, 0.0, 0.0),
                ("Eotchorts FG-X d1-321", 15.0, 5.0, 0.0),
                ("Eotchorts FG-X d1-322", 20.0, 0.0, 10.0),
                ("Eotchorts FG-X d1-323", 30.0, 0.0, 0.0),
                ("Eotchorts FG-X d1-324", 40.0, 0.0, 0.0),
                ("Eotchorts FG-X d1-325", 45.0, 0.0, 0.0),
                ("Eotchorts FG-X d1-326", 48.0, 0.0, 0.0),
                ("Vega AA-A h0", 0.0, 1.0, 0.0),
                ("Vega AA-A h1", 1.0, 1.0, 1.0),
                ("Vega AA-B h2", 2.0, 2.0, 2.0),
                ("Sol", 0.0, 0.0, 0.0),
                ("Sol AB-1", 5.0, 0.0, 0.0),
                ("Sol AB-2", 10.0, 0.0, 0.0),
                ("Sol AB-3", 15.0, 0.0, 0.0),
                ("Sol AB-4", 20.0, 0.0, 0.0),
                ("Sol AB-5", 25.0, 0.0, 0.0),
            ],
        )
        conn.commit()


@contextmanager
def _temp_env(**kwargs):
    previous = {key: os.environ.get(key) for key in kwargs}
    try:
        for key, value in kwargs.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class StrategicPlannerTests(unittest.TestCase):
    def test_gap_mode_filters_existing_and_persists(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            library_dir = f"{temp_dir}/sector_library"
            working_db = f"{temp_dir}/working.sqlite"
            os.makedirs(library_dir, exist_ok=True)

            progress_phases: list[str] = []

            def progress_cb(progress) -> None:
                progress_phases.append(progress.phase)

            client = FakeEDSMClient(reject_first=0)
            params = JobParams(mode="gap", sector="Eotchorts")
            with _temp_env(MFI_SECTOR_LIBRARY_DIR=library_dir, MFI_SECTOR_DB=None):
                sector_db = resolve_sector_db_path("Eotchorts")
                _create_sector_db(sector_db)
                results = _run_strategic_plan_closed(
                    params,
                    working_db_path=working_db,
                    progress_cb=progress_cb,
                    edsm_client=client,
                )

            self.assertGreaterEqual(len(results), 25)
            self.assertEqual(len(results), len({c.system_name for c in results}))
            self.assertTrue(all(candidate.system_name for candidate in results))
            self.assertTrue(any("Eotchorts FG-X d1-3" == c.system_name for c in results))
            self.assertFalse(any("AA-A" in c.system_name and "h" in c.system_name for c in results))

            with closing(sqlite3.connect(working_db)) as conn:
                cache = conn.execute("SELECT system_name FROM edsm_cache").fetchall()
                self.assertGreaterEqual(len(cache), 25)

                kept = conn.execute("SELECT system_name FROM strategic_candidates").fetchall()
                self.assertEqual(sorted(row[0] for row in kept), sorted(c.system_name for c in results))

                working_count = conn.execute("SELECT COUNT(*) FROM working_systems").fetchone()[0]
                self.assertGreater(working_count, 0)

            self._assert_phase_order(progress_phases)
            repeat_results = self._run_gap_again(reject_first=0)
            self.assertEqual([c.system_name for c in results], [c.system_name for c in repeat_results])

    def test_spatial_mode_filters_existing_and_persists(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            library_dir = f"{temp_dir}/sector_library"
            working_db = f"{temp_dir}/working.sqlite"
            os.makedirs(library_dir, exist_ok=True)

            progress_phases: list[str] = []

            def progress_cb(progress) -> None:
                progress_phases.append(progress.phase)

            client = FakeEDSMClient(reject_first=0)
            params = JobParams(mode="spatial", center_system="Eotchorts FG-X d1-318", radius_ly=50.0)
            with _temp_env(MFI_SECTOR_LIBRARY_DIR=library_dir, MFI_SECTOR_DB=None):
                sector_db = resolve_sector_db_path("Eotchorts")
                _create_sector_db(sector_db)
                results = _run_strategic_plan_closed(
                    params,
                    working_db_path=working_db,
                    progress_cb=progress_cb,
                    edsm_client=client,
                )

            self.assertGreaterEqual(len(results), 25)
            self.assertTrue(all(candidate.system_name for candidate in results))

            with closing(sqlite3.connect(working_db)) as conn:
                cache = conn.execute("SELECT system_name FROM edsm_cache").fetchall()
                self.assertGreaterEqual(len(cache), 25)

                kept = conn.execute("SELECT system_name FROM strategic_candidates").fetchall()
                self.assertGreaterEqual(len(kept), 25)

                working_count = conn.execute("SELECT COUNT(*) FROM working_systems").fetchone()[0]
                self.assertGreater(working_count, 0)

            self._assert_phase_order(progress_phases)

    def _assert_phase_order(self, phases: list[str]) -> None:
        required = ["extract", "generate", "validate", "persist", "done"]
        indices = [phases.index(phase) for phase in required]
        self.assertEqual(indices, sorted(indices))

    def _run_gap_again(self, reject_first: int) -> list:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            library_dir = f"{temp_dir}/sector_library"
            working_db = f"{temp_dir}/working.sqlite"
            os.makedirs(library_dir, exist_ok=True)
            client = FakeEDSMClient(reject_first=reject_first)
            params = JobParams(mode="gap", sector="Eotchorts")
            with _temp_env(MFI_SECTOR_LIBRARY_DIR=library_dir, MFI_SECTOR_DB=None):
                sector_db = resolve_sector_db_path("Eotchorts")
                _create_sector_db(sector_db)
                return _run_strategic_plan_closed(
                    params,
                    working_db_path=working_db,
                    edsm_client=client,
                )

    def test_spatial_center_not_found(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            library_dir = f"{temp_dir}/sector_library"
            working_db = f"{temp_dir}/working.sqlite"
            os.makedirs(library_dir, exist_ok=True)
            client = FakeEDSMClient(reject_first=0)
            params = JobParams(mode="spatial", center_system="Missing", radius_ly=20.0)
            with _temp_env(MFI_SECTOR_LIBRARY_DIR=library_dir):
                sector_db = resolve_sector_db_path("Eotchorts")
                _create_sector_db(sector_db)
                os.environ["MFI_SECTOR_DB"] = sector_db
                with self.assertRaises(ValueError):
                    _run_strategic_plan_closed(
                        params,
                        working_db_path=working_db,
                        edsm_client=client,
                    )

    def test_missing_sector_db_error(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            library_dir = f"{temp_dir}/sector_library"
            working_db = f"{temp_dir}/working.sqlite"
            os.makedirs(library_dir, exist_ok=True)
            params = JobParams(mode="gap", sector="Eotchorts")
            with _temp_env(MFI_SECTOR_LIBRARY_DIR=library_dir, MFI_SECTOR_DB=None):
                expected_path = resolve_sector_db_path("Eotchorts")
            expected_message = f"Sector DB not found: {expected_path}. Run extractor to create it."
            with _temp_env(MFI_SECTOR_LIBRARY_DIR=library_dir, MFI_SECTOR_DB=None):
                with self.assertRaises(ValueError) as context:
                    _run_strategic_plan_closed(
                        params,
                        working_db_path=working_db,
                    )
            self.assertEqual(str(context.exception), expected_message)


if __name__ == "__main__":
    unittest.main()

