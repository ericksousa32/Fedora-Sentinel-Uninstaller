"""Fixed paths used by FSU, relative to $HOME."""

from __future__ import annotations

from pathlib import Path

from fsu.models import Category

# The only folders FSU scans (first level of each one only).
SCAN_ROOTS: dict[Category, Path] = {
    Category.CONFIG: Path(".config"),
    Category.DATA: Path(".local/share"),
    Category.CACHE: Path(".cache"),
    Category.FLATPAK: Path(".var/app"),
}

# FSU's own working directory (quarantine, state). It lives inside
# ~/.local/share and is therefore explicitly protected.
FSU_DATA_DIR = Path(".local/share/fsu")
QUARANTINE_DIR = FSU_DATA_DIR / "quarantine"

# User launchers and the usual AppImage locations (Gear Lever uses ~/AppImages).
USER_APPLICATIONS_DIR = Path(".local/share/applications")
APPIMAGE_DIRS: tuple[Path, ...] = (Path("AppImages"), Path("Applications"), Path(".local/bin"))
