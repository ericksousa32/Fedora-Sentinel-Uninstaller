"""Locations used by FSU.

The XDG base directories are honoured when set to absolute paths
(``XDG_CONFIG_HOME``, ``XDG_DATA_HOME``, ``XDG_CACHE_HOME``,
``XDG_STATE_HOME``); otherwise the defaults below ``$HOME`` are used, as the
XDG Base Directory specification requires.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from fsu.models import Category

_XDG_DEFAULTS: dict[str, Path] = {
    "XDG_CONFIG_HOME": Path(".config"),
    "XDG_DATA_HOME": Path(".local/share"),
    "XDG_CACHE_HOME": Path(".cache"),
    "XDG_STATE_HOME": Path(".local/state"),
}

# Directories, inside the config/data homes, holding launchers and services
# whose target program can be checked (category LAUNCHER, files only).
LAUNCHER_SUBDIRS_CONFIG: tuple[Path, ...] = (Path("autostart"), Path("systemd/user"))
LAUNCHER_SUBDIRS_DATA: tuple[Path, ...] = (Path("applications"), Path("dbus-1/services"))

# AppImage locations (Gear Lever uses ~/AppImages), relative to $HOME.
APPIMAGE_DIRS: tuple[Path, ...] = (Path("AppImages"), Path("Applications"), Path(".local/bin"))


def xdg_home(variable: str, home: Path, environ: Mapping[str, str] | None = None) -> Path:
    """Value of an ``XDG_*_HOME`` variable, or its default below ``home``.

    Relative values are invalid per the specification and are ignored.
    """
    environ = os.environ if environ is None else environ
    value = environ.get(variable, "")
    if value and Path(value).is_absolute():
        return Path(value)
    return home / _XDG_DEFAULTS[variable]


def config_home(home: Path) -> Path:
    return xdg_home("XDG_CONFIG_HOME", home)


def data_home(home: Path) -> Path:
    return xdg_home("XDG_DATA_HOME", home)


def scan_roots(home: Path) -> dict[Category, tuple[Path, ...]]:
    """Folders whose direct children FSU evaluates, per category."""
    config, data = config_home(home), data_home(home)
    return {
        Category.CONFIG: (config,),
        Category.DATA: (data,),
        Category.CACHE: (xdg_home("XDG_CACHE_HOME", home),),
        Category.STATE: (xdg_home("XDG_STATE_HOME", home),),
        Category.FLATPAK: (home / ".var/app",),
        Category.LAUNCHER: (
            *(config / d for d in LAUNCHER_SUBDIRS_CONFIG),
            *(data / d for d in LAUNCHER_SUBDIRS_DATA),
        ),
        # Last on purpose: the most generic parent.
        Category.HOME: (home,),
    }


def fsu_data_dir(home: Path) -> Path:
    """FSU's own folder (quarantine, log), inside the data home."""
    return data_home(home) / "fsu"


def quarantine_dir(home: Path) -> Path:
    return fsu_data_dir(home) / "quarantine"


def user_applications_dir(home: Path) -> Path:
    return data_home(home) / "applications"


ROOT_LABELS: dict[Category, str] = {
    Category.CONFIG: "~/.config",
    Category.DATA: "~/.local/share",
    Category.CACHE: "~/.cache",
    Category.STATE: "~/.local/state",
    Category.FLATPAK: "~/.var/app",
    Category.HOME: "~",
    Category.LAUNCHER: "launcher directory",
}
