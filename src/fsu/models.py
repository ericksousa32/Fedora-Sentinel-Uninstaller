"""Immutable data models shared by the FSU modules."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class Category(enum.Enum):
    """Where a residual item lives inside $HOME."""

    CONFIG = "config"  # ~/.config
    DATA = "data"  # ~/.local/share
    CACHE = "cache"  # ~/.cache
    FLATPAK = "flatpak"  # ~/.var/app
    STATE = "state"  # ~/.local/state
    HOME = "home"  # hidden folders directly in $HOME (~/.nuget, ~/.dotnet, ...)
    LAUNCHER = "launcher"  # autostart / user systemd / D-Bus / .desktop files


class Action(enum.Enum):
    """What FSU is allowed to do with a candidate, based on its score.

    The bands are applied by :func:`fsu.heuristics.action_for`.
    """

    QUARANTINE = "quarantine"  # score >= 0.70: direct quarantine
    QUARANTINE_WITH_SNAPSHOT = "quarantine_with_snapshot"  # 0.40 <= score < 0.70
    LIST_ONLY = "list_only"  # score < 0.40: never moved automatically


@dataclass(frozen=True)
class Evidence:
    """A human-readable justification for a candidate's score."""

    source: str  # e.g. "flatpak", "dnf5-history", "appimage", "installed", "fsu"
    detail: str


@dataclass(frozen=True)
class RemovedPackage:
    """RPM package removed according to the DNF5 history."""

    name: str
    transaction_id: int
    removed_at: int  # UNIX time of the end of the transaction
    reason: str = ""  # install reason recorded by DNF5 (User, Dependency, ...)


@dataclass(frozen=True)
class Candidate:
    """An item in $HOME that may be left over by an uninstalled application."""

    path: Path
    category: Category
    score: float
    action: Action
    size_bytes: int
    mtime: float
    is_symlink: bool
    matched_name: str | None = None
    evidence: tuple[Evidence, ...] = ()
    # Most recent read/write seen inside the item (0 = unknown).
    last_activity: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "category": self.category.value,
            "score": round(self.score, 3),
            "action": self.action.value,
            "size_bytes": self.size_bytes,
            "mtime": self.mtime,
            "last_activity": self.last_activity,
            "is_symlink": self.is_symlink,
            "matched_name": self.matched_name,
            "evidence": [{"source": e.source, "detail": e.detail} for e in self.evidence],
        }


@dataclass(frozen=True)
class ScanReport:
    """Result of a read-only scan."""

    candidates: tuple[Candidate, ...]
    warnings: tuple[str, ...] = ()
    protected_skipped: tuple[Path, ...] = ()
    installed_skipped: int = 0
    # Items that another component recreates (e.g. Plasma Browser Integration
    # stubs in ~/.var/app); quarantining them would be pointless.
    regenerated_skipped: tuple[Path, ...] = ()
    # User launchers (.desktop) whose AppImage no longer exists.
    stale_launchers: tuple[Path, ...] = ()
    timings: dict[str, float] = field(default_factory=dict)
    # Items in the strict areas (home root, state, launchers) that did not reach
    # 0.70: never listed, only counted.
    low_confidence_hidden: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": [c.to_dict() for c in self.candidates],
            "warnings": list(self.warnings),
            "protected_skipped": [str(p) for p in self.protected_skipped],
            "installed_skipped": self.installed_skipped,
            "regenerated_skipped": [str(p) for p in self.regenerated_skipped],
            "stale_launchers": [str(p) for p in self.stale_launchers],
            "timings": {k: round(v, 3) for k, v in self.timings.items()},
            "low_confidence_hidden": self.low_confidence_hidden,
        }
