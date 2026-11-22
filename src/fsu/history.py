"""FSU's own inventory history: evidence for software installed *outside* package managers.

RPM and Flatpak keep a history of removals; an AppImage, a program in
``~/.local/bin`` or a tool in ``/opt`` disappears without leaving any record.
Each scan therefore records what exists right now (AppImage files, programs run
by the user's launchers, programs outside the system directories) and, on later
scans, reports names that **existed before and are gone now**: concrete,
local, verifiable evidence of removal.

The history lives in FSU's own state folder (``$XDG_STATE_HOME/fsu``), never
leaves the machine and only holds paths and names. Recording can be disabled
(``fsu scan --no-history``); evidence then comes only from what was recorded
before.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fsu.inventory import (
    AppImageInventory,
    appimage_base_name,
    parse_desktop_file,
)
from fsu.paths import xdg_home

HISTORY_VERSION = 1
# Programs below these directories belong to the system (RPM) and are already
# covered by the DNF5 history.
SYSTEM_PREFIXES: tuple[Path, ...] = (
    Path("/usr/bin"), Path("/usr/sbin"), Path("/bin"), Path("/sbin"), Path("/usr/libexec"),
    Path("/usr/lib"), Path("/usr/lib64"), Path("/var/lib/flatpak"),
)  # fmt: skip
# A missing path on these prefixes may simply be unmounted.
REMOVABLE_PREFIXES: tuple[Path, ...] = (Path("/run/media"), Path("/media"), Path("/mnt"))


def history_path(home: Path) -> Path:
    return xdg_home("XDG_STATE_HOME", home) / "fsu" / "inventory.json"


@dataclass(frozen=True)
class Observation:
    """Something installed outside package managers, seen on disk."""

    kind: str  # "appimage" | "launcher-target" | "program"
    path: str
    names: frozenset[str]


@dataclass(frozen=True)
class GoneEntry:
    kind: str
    path: str
    names: frozenset[str]
    last_seen: float


@dataclass
class HistoryResult:
    gone: list[GoneEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    recorded: bool = False


def _is_system(path: Path) -> bool:
    return any(path == p or p in path.parents for p in SYSTEM_PREFIXES)


def observe(
    appimage_dirs: Iterable[Path],
    applications_dir: Path,
    executables: Mapping[str, str],
) -> dict[str, Observation]:
    """What exists now, keyed by ``kind:path``."""
    found: dict[str, Observation] = {}

    def add(kind: str, path: Path, names: Iterable[str]) -> None:
        clean = frozenset(n for n in names if n)
        if clean:
            key = f"{kind}:{path}"
            previous = found.get(key)
            found[key] = Observation(
                kind, str(path), clean | (previous.names if previous else frozenset())
            )

    for directory in appimage_dirs:
        try:
            files = sorted(directory.iterdir()) if directory.is_dir() else []
        except OSError:
            files = []
        for file in files:
            if file.name.lower().endswith(".appimage") and file.is_file():
                add("appimage", file, [appimage_base_name(file.name)])

    if applications_dir.is_dir():
        for desktop_file in sorted(applications_dir.glob("*.desktop")):
            try:
                entry = parse_desktop_file(desktop_file)
            except OSError:
                continue
            target = Path(entry.exec_target).expanduser() if entry.exec_target else None
            if target is None or not target.is_absolute() or _is_system(target):
                continue
            if target.exists():
                add("launcher-target", target, AppImageInventory.entry_names(entry))

    for name, where in executables.items():
        path = Path(where)
        if not _is_system(path):
            add("program", path, [name])
    return found


class InventoryHistory:
    """Persistent record of observations, with "seen then gone" detection."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self, warnings: list[str]) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"inventory history unreadable ({exc}); starting a new one")
            return {}
        if not isinstance(data, dict) or data.get("version") != HISTORY_VERSION:
            warnings.append("inventory history has an unknown format; starting a new one")
            return {}
        items = data.get("items", {})
        return (
            {k: v for k, v in items.items() if isinstance(v, dict)}
            if isinstance(items, dict)
            else {}
        )

    def _save(self, items: Mapping[str, Mapping[str, Any]]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump({"version": HISTORY_VERSION, "items": items}, handle, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(self.path)

    def update(
        self,
        current: Mapping[str, Observation],
        now: float | None = None,
        record: bool = True,
    ) -> HistoryResult:
        """Compare with the stored history, report what is gone, store the new state."""
        now = time.time() if now is None else now
        result = HistoryResult()
        items = self._load(result.warnings)
        present_names = {n for obs in current.values() for n in obs.names}

        for key, item in items.items():
            if key in current:
                continue
            path = Path(str(item.get("path", "")))
            names = frozenset(str(n) for n in item.get("names", []) if n)
            if not path.is_absolute() or not names:
                continue
            if any(path == p or p in path.parents for p in REMOVABLE_PREFIXES):
                continue  # possibly just unmounted
            if os.path.lexists(path):
                continue  # still there (e.g. temporarily not observed)
            if names & present_names:
                continue  # same application present elsewhere (e.g. updated AppImage)
            result.gone.append(
                GoneEntry(
                    str(item.get("kind", "?")),
                    str(path),
                    names,
                    float(item.get("last_seen", 0)),
                )
            )

        if record:
            for key, obs in current.items():
                previous = items.get(key, {})
                items[key] = {
                    "kind": obs.kind,
                    "path": obs.path,
                    "names": sorted(obs.names),
                    "first_seen": previous.get("first_seen", now),
                    "last_seen": now,
                }
            try:
                self._save(items)
                result.recorded = True
            except OSError as exc:
                result.warnings.append(f"cannot save inventory history: {exc}")
        return result
