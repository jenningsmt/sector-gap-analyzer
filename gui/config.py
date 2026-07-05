"""Persisted settings for the Sector Gap Analyzer GUI.

Stored under %APPDATA%\\SectorGapAnalyzer\\config.json (Windows), deliberately
independent of where the app's own executable/script lives, since a frozen
exe on the Desktop still needs to know where the actual project data
(sector_library DBs, out/ reports, the galaxy dump) lives.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

APP_NAME = "SectorGapAnalyzer"


def _default_workspace_dir() -> str:
    """Per-user writable workspace, independent of wherever the app itself is
    installed -- there's no git checkout to anchor to for an installed app."""
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    return str(base / APP_NAME / "workspace")


def _default_galaxy_dump_path() -> str:
    """Default location a fresh user is told (see README) to save their galaxy
    dump to, so Settings needs no changes out of the box. Anyone keeping their
    dump elsewhere (e.g. for use across multiple projects) can browse to it."""
    return str(Path(_default_workspace_dir()) / "source_data" / "galaxy.json.gz")


DEFAULT_CONFIG: dict[str, Any] = {
    "project_dir": _default_workspace_dir(),
    "galaxy_dump_path": _default_galaxy_dump_path(),
    "sectors": [],
    "max_bracket_width": 25,
    "extend_depth": 5,
    "run_forward": False,
    "max_forward_step": 5,
    "dry_run": True,
    "stages": {
        "extract": True,
        "bracketed_gaps": True,
        "backward_extrap": True,
        "forward_extrap": False,
        "aggregate": True,
    },
}


def config_path() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / ".config"
    return base / APP_NAME / "config.json"


def load_config() -> dict[str, Any]:
    path = config_path()
    config = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                config.update(saved)
                if isinstance(saved.get("stages"), dict):
                    config["stages"] = {**DEFAULT_CONFIG["stages"], **saved["stages"]}
        except (json.JSONDecodeError, OSError):
            pass
    return config


def save_config(config: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
