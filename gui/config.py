"""Persisted settings for the ED Sector Surveyor GUI.

Stored under %APPDATA%\\EDSectorSurveyor\\config.json (Windows), deliberately
independent of where the app's own executable/script lives, since a frozen
exe on the Desktop still needs to know where the actual project data
(sector_library DBs, out/ reports, the galaxy dump) lives.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

APP_NAME = "EDSectorSurveyor"

# Pre-rebrand folder names, most recent first (the app was "Sector Surveyor"
# through v1.3.0, and originally "Sector Gap Analyzer" through v1.2.0).
# load_config() migrates the first settings file it finds among these
# forward one time, so existing installs don't silently lose their
# configuration on upgrade, however many rebrands back they're coming from.
_OLD_APP_NAMES = ["SectorSurveyor", "SectorGapAnalyzer"]

# Displayed in the window title. Keep in sync with installer.iss's
# MyAppVersion and version_info.txt's filevers/prodvers/FileVersion/
# ProductVersion when cutting a new release.
APP_VERSION = "1.4.0"


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
    "mode": "gap",  # "gap" (sector-prefix, existing) or "spatial" (radius-around-a-system)
    "sectors": [],
    "spatial_center_system": "",
    "spatial_radius_ly": 20,
    "spatial_sector_override": "",
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
        "bio_opportunity": False,
    },
}


def _appdata_base() -> Path:
    appdata = os.environ.get("APPDATA")
    return Path(appdata) if appdata else Path.home() / ".config"


def config_path() -> Path:
    return _appdata_base() / APP_NAME / "config.json"


def _old_config_paths() -> list[Path]:
    base = _appdata_base()
    return [base / name / "config.json" for name in _OLD_APP_NAMES]


def _migrate_old_config_if_needed(path: Path) -> None:
    """One-time forward-copy of a pre-rebrand settings file (from the most
    recent prior app name that has one), so upgrading to ED Sector Surveyor
    doesn't silently reset settings. The old file is left in place untouched
    -- this only ever copies."""
    if path.exists():
        return
    for old_path in _old_config_paths():
        if not old_path.exists():
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(old_path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass
        return


def load_config() -> dict[str, Any]:
    path = config_path()
    _migrate_old_config_if_needed(path)
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
