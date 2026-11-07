"""Sources of truth about what is (or was) installed on the system.

* :class:`RpmInventory` - RPM packages installed now (``rpm -qa``).
* :class:`DnfHistory` - packages removed according to the DNF5 history
  (``dnf5 history list/info --json``). It neither parses log files nor reads
  DNF4's ``/var/lib/dnf``.
* :class:`FlatpakInventory` - installed Flatpak apps (user and system).
* :class:`DesktopIndex` - names taken from the ``.desktop`` files of installed
  applications (avoids false positives such as ``~/.config/Code``).
* :class:`AppImageInventory` - AppImages present in the usual folders and user
  launchers that point to AppImages (present or gone).
* :class:`ExecutableInventory` - programs found in $PATH, /opt and pipx
  environments (positive evidence only).

All classes are read-only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fsu.heuristics import useful_id_part
from fsu.models import RemovedPackage
from fsu.system import CommandError, Runner, run_command

# DNF5 actions meaning the package left the system. "Replaced" and
# "Obsoleted" are excluded: the name usually stays installed in another
# version or under the package that replaces it.
_REMOVE_ACTIONS = frozenset({"remove", "removed"})

# Packages that practically never create their own folders in $HOME.
# Ignoring them reduces noise (removing an app also removes its libraries).
_NOISE_SUFFIXES = (
    "-devel", "-libs", "-doc", "-docs", "-debuginfo", "-debugsource", "-static",
    "-headers", "-common", "-data", "-filesystem", "-fonts", "-langpacks",
)  # fmt: skip
_NOISE_PREFIXES = (
    "python3-", "perl-", "golang-", "rust-", "texlive-", "ghc-", "ocaml-",
    "lib", "kf5-", "kf6-", "qt5-", "qt6-", "glibc-", "langpacks-",
)  # fmt: skip
# Exceptions to the "lib" prefix that are real applications.
_NOISE_PREFIX_EXCEPTIONS = ("libre",)  # libreoffice, librewolf

_FLATPAK_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z_][A-Za-z0-9_-]*)+$")


def nevra_name(nevra: str) -> str:
    """Extract the name from a NEVRA: ``name-[epoch:]version-release.arch``."""
    without_arch = nevra.rsplit(".", 1)[0] if "." in nevra else nevra
    parts = without_arch.rsplit("-", 2)
    return parts[0] if len(parts) == 3 else without_arch


def is_noise_package(name: str) -> bool:
    """Library/support packages that do not leave residues of their own in $HOME."""
    lowered = name.lower()
    if lowered.endswith(_NOISE_SUFFIXES):
        return True
    if lowered.startswith(_NOISE_PREFIX_EXCEPTIONS):
        return False
    return lowered.startswith(_NOISE_PREFIXES)


class RpmInventory:
    """RPM packages currently installed."""

    def __init__(self, runner: Runner = run_command) -> None:
        self._runner = runner

    def installed_names(self) -> frozenset[str]:
        output = self._runner(["rpm", "-qa", "--queryformat", "%{NAME}\\n"])
        names = frozenset(line.strip() for line in output.splitlines() if line.strip())
        if not names:
            # A Fedora system with no RPM installed is impossible: treat it as a
            # failure so that nothing is judged against an empty inventory.
            raise CommandError(["rpm", "-qa"], "empty inventory")
        return names


class DnfHistory:
    """Reads the DNF5 transaction history (JSON format)."""

    def __init__(self, runner: Runner = run_command, fallback_limit: int = 150) -> None:
        self._runner = runner
        self._fallback_limit = fallback_limit
        self.warnings: list[str] = []

    def _json(self, args: Sequence[str]) -> list[dict[str, Any]]:
        output = self._runner(args)
        try:
            data = json.loads(output) if output.strip() else []
        except json.JSONDecodeError as exc:
            raise CommandError(args, f"invalid JSON: {exc.msg}") from exc
        if not isinstance(data, list):
            raise CommandError(args, "unexpected JSON: expected an array")
        return [item for item in data if isinstance(item, dict)]

    def transaction_ids(self) -> list[int]:
        """IDs of every recorded transaction (descending order)."""
        transactions = self._json(["dnf5", "history", "list", "--json"])
        ids = {t["id"] for t in transactions if isinstance(t.get("id"), int)}
        return sorted(ids, reverse=True)

    def _details(self, ids: list[int]) -> list[dict[str, Any]]:
        """Details (with packages) of the transactions.

        Tries a single call with a range (``min..max``), confirmed to work on
        dnf5 5.4.6. If the DNF5 version rejects it, falls back to one call per
        transaction for the most recent ones, limited by ``fallback_limit``.

        Performance note (measured on Fedora 45, dnf5 5.4.6, ~400 transactions):
        this call takes ~20 s and returns ~5.8 MB of JSON. The time is spent
        inside dnf5 itself (``--cacheonly`` makes no difference, so repository
        metadata is not involved); FSU's own processing takes well under a
        second. Reading DNF5's internal SQLite database would be faster but
        depends on an undocumented schema, so the public CLI is kept.
        """
        if not ids:
            return []
        spec = f"{min(ids)}..{max(ids)}"
        try:
            return self._json(["dnf5", "history", "info", spec, "--json"])
        except CommandError as exc:
            self.warnings.append(
                f"dnf5 history info with a range failed ({exc}); querying the "
                f"{min(len(ids), self._fallback_limit)} most recent transactions one by one."
            )
        details: list[dict[str, Any]] = []
        for tid in ids[: self._fallback_limit]:
            details.extend(self._json(["dnf5", "history", "info", str(tid), "--json"]))
        return details

    def removed_packages(self) -> list[RemovedPackage]:
        """Removed packages, keeping the most recent removal of each name.

        Packages reinstalled later are *not* filtered here: the collector does
        that by cross-checking :class:`RpmInventory`, the source of the current
        state.
        """
        latest: dict[str, RemovedPackage] = {}
        for transaction in self._details(self.transaction_ids()):
            tid = transaction.get("id")
            end_time = transaction.get("end_time") or transaction.get("start_time") or 0
            packages = transaction.get("packages")
            if not isinstance(tid, int) or not isinstance(packages, list):
                continue
            for package in packages:
                if not isinstance(package, dict):
                    continue
                action = str(package.get("action", "")).strip().lower()
                nevra = package.get("nevra")
                if action not in _REMOVE_ACTIONS or not isinstance(nevra, str):
                    continue
                name = nevra_name(nevra)
                record = RemovedPackage(
                    name=name,
                    transaction_id=tid,
                    removed_at=int(end_time),
                    reason=str(package.get("reason", "")),
                )
                current = latest.get(name)
                if current is None or record.removed_at >= current.removed_at:
                    latest[name] = record
        return sorted(latest.values(), key=lambda r: (-r.removed_at, r.name))


class FlatpakInventory:
    """Installed Flatpak applications (user and system installations)."""

    def __init__(self, runner: Runner = run_command) -> None:
        self._runner = runner

    def installed_app_ids(self) -> frozenset[str]:
        output = self._runner(["flatpak", "list", "--app", "--columns=application"])
        # Drop headers (possibly localized, e.g. "ID de aplicativo") by
        # accepting only lines shaped like a Flatpak ID.
        return frozenset(
            line.strip() for line in output.splitlines() if _FLATPAK_ID.match(line.strip())
        )


# -- .desktop parsing -----------------------------------------------------------

_EXEC_WRAPPERS = frozenset({"env", "sh", "bash", "pkexec", "sudo", "gio", "xdg-open"})


@dataclass
class DesktopEntry:
    """The few ``[Desktop Entry]`` keys FSU cares about."""

    path: Path
    name: str = ""
    startup_wm_class: str = ""
    icon: str = ""
    exec_target: str = ""  # first real program of Exec (wrappers skipped)
    is_flatpak_launcher: bool = False
    is_appimage: bool = False


def _exec_target(value: str) -> tuple[str, bool]:
    """First real program in an ``Exec`` value and whether it is ``flatpak run``."""
    try:
        tokens = shlex.split(value)
    except ValueError:
        tokens = value.split()
    for token in tokens:
        if "=" in token and not token.startswith("/"):
            continue  # environment variables (FOO=bar)
        program = Path(token).name
        if program == "flatpak":
            return "", True
        if program in _EXEC_WRAPPERS or program.startswith("-"):
            continue
        return ("" if token.startswith("%") else token), False
    return "", False


def parse_desktop_file(path: Path) -> DesktopEntry:
    """Parse the main ``[Desktop Entry]`` group of a ``.desktop`` file."""
    entry = DesktopEntry(path=path)
    in_entry = False
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_entry = line == "[Desktop Entry]"
            continue
        if not in_entry or "=" not in line or line.startswith("#"):
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key == "Name":
            entry.name = value
        elif key == "StartupWMClass":
            entry.startup_wm_class = value
        elif key == "Icon":
            entry.icon = value
        elif key == "Exec":
            entry.exec_target, entry.is_flatpak_launcher = _exec_target(value)
        elif key.startswith("X-AppImage-"):
            entry.is_appimage = True
    if entry.exec_target.lower().endswith(".appimage"):
        entry.is_appimage = True
    return entry


def _id_names(stem: str) -> set[str]:
    names = {stem}
    part = useful_id_part(stem)
    if part:
        names.add(part)
    return names


class DesktopIndex:
    """Names associated with installed applications, from ``.desktop`` files."""

    def __init__(self, directories: Iterable[Path]) -> None:
        self._directories = tuple(directories)
        self.warnings: list[str] = []

    @staticmethod
    def names_for(entry: DesktopEntry) -> set[str]:
        names = _id_names(entry.path.stem)
        if entry.startup_wm_class:
            names.add(entry.startup_wm_class)
        if entry.exec_target:
            names.add(Path(entry.exec_target).name)
        return {n for n in names if n}

    def known_names(self) -> frozenset[str]:
        names: set[str] = set()
        for directory in self._directories:
            if not directory.is_dir():
                continue
            try:
                files = sorted(directory.glob("*.desktop"))
            except OSError as exc:
                self.warnings.append(f"cannot list {directory}: {exc}")
                continue
            for desktop_file in files:
                try:
                    names |= self.names_for(parse_desktop_file(desktop_file))
                except OSError as exc:
                    self.warnings.append(f"cannot read {desktop_file}: {exc}")
        return frozenset(names)


# Default locations of .desktop files of installed applications. The user's
# ~/.local/share/applications is handled by AppImageInventory instead: it may
# contain stale launchers of removed applications.
DEFAULT_DESKTOP_DIRS: tuple[Path, ...] = (
    Path("/usr/share/applications"),
    Path("/usr/local/share/applications"),
    Path("/var/lib/flatpak/exports/share/applications"),
)
USER_FLATPAK_DESKTOP_DIR = Path(".local/share/flatpak/exports/share/applications")


# -- AppImages ------------------------------------------------------------------

# "photocraft-0.18.0-linux-x86_64" -> "photocraft"; "LM-Studio-0.3.5" -> "LM-Studio"
_VERSION_TAIL = re.compile(r"^(.+?)[-_. ]v?\d")
# Locations that may simply be unmounted: a missing file there proves nothing.
_REMOVABLE_PREFIXES = (Path("/run/media"), Path("/media"), Path("/mnt"))


def appimage_base_name(file_name: str) -> str:
    """AppImage file name without extension and version suffix."""
    stem = file_name[: -len(".appimage")] if file_name.lower().endswith(".appimage") else file_name
    match = _VERSION_TAIL.match(stem)
    return match.group(1) if match else stem


@dataclass(frozen=True)
class AppImageState:
    present_names: frozenset[str]  # AppImages that exist (in use)
    missing_names: frozenset[str]  # launchers whose AppImage is gone
    stale_launchers: tuple[Path, ...]
    warnings: tuple[str, ...] = field(default=())


class AppImageInventory:
    """AppImages in the usual folders plus user launchers pointing to AppImages.

    * A launcher whose AppImage exists, or an AppImage file in one of the
      folders, makes its names *present* (protected).
    * A launcher whose AppImage is gone - and the path is inside $HOME, not on
      removable media - makes its names *missing* (evidence of removal).
    """

    def __init__(self, home: Path, applications_dir: Path, appimage_dirs: Iterable[Path]) -> None:
        self._home = home
        self._applications_dir = applications_dir
        self._appimage_dirs = tuple(appimage_dirs)

    @staticmethod
    def _entry_names(entry: DesktopEntry) -> set[str]:
        names = DesktopIndex.names_for(entry)
        if entry.exec_target:
            names.add(appimage_base_name(Path(entry.exec_target).name))
        if entry.name:
            names.add(entry.name)
        if entry.startup_wm_class:
            names |= _id_names(entry.startup_wm_class)
        # Icon= is often the application ID (e.g. ai.storyteller.photocraft);
        # icon file paths carry no such information.
        if entry.icon and "/" not in entry.icon:
            names |= _id_names(entry.icon)
        return {n for n in names if n}

    def _is_judgeable_missing(self, target: Path) -> bool:
        if not target.is_absolute():
            return False
        if any(target.is_relative_to(prefix) for prefix in _REMOVABLE_PREFIXES):
            return False
        return target.is_relative_to(self._home)

    def scan(self) -> AppImageState:
        present: set[str] = set()
        missing: set[str] = set()
        stale: list[Path] = []
        warnings: list[str] = []

        for directory in self._appimage_dirs:
            if not directory.is_dir():
                continue
            try:
                for file in directory.iterdir():
                    if file.name.lower().endswith(".appimage"):
                        present.add(appimage_base_name(file.name))
            except OSError as exc:
                warnings.append(f"cannot list {directory}: {exc}")

        if self._applications_dir.is_dir():
            for desktop_file in sorted(self._applications_dir.glob("*.desktop")):
                try:
                    entry = parse_desktop_file(desktop_file)
                except OSError as exc:
                    warnings.append(f"cannot read {desktop_file}: {exc}")
                    continue
                if not entry.is_appimage or not entry.exec_target:
                    continue
                target = Path(entry.exec_target).expanduser()
                names = self._entry_names(entry)
                if target.exists():
                    present |= names
                elif self._is_judgeable_missing(target):
                    missing |= names
                    stale.append(desktop_file)

        # A name that is present wins over the same name in a stale launcher.
        return AppImageState(
            present_names=frozenset(present),
            missing_names=frozenset(missing - present),
            stale_launchers=tuple(stale),
            warnings=tuple(warnings),
        )


# -- executables ----------------------------------------------------------------

# pipx keeps one virtual environment per installed application.
PIPX_VENV_DIRS: tuple[Path, ...] = (
    Path("/opt/pipx/venvs"),
    Path(".local/share/pipx/venvs"),  # relative: resolved against $HOME
    Path(".local/pipx/venvs"),
)
OPT_ROOT = Path("/opt")
# Directory names under /opt/<vendor>/ that say nothing about an application.
_GENERIC_OPT_DIRS = frozenset(
    {
        "bin", "sbin", "lib", "lib64", "libexec", "share", "shared", "etc", "include",
        "var", "log", "logs", "doc", "docs", "data", "resources", "usr", "venvs", "locales",
    }
)  # fmt: skip


def default_executable_dirs(home: Path) -> list[Path]:
    """Directories searched for installed programs: absolute $PATH entries.

    Relative entries (such as ``.``) are ignored: they depend on the current
    directory and are a classic way to shadow real programs.
    """
    entries = os.environ.get("PATH", "").split(os.pathsep)
    dirs: list[Path] = []
    for entry in entries:
        if entry and Path(entry).is_absolute() and Path(entry) not in dirs:
            dirs.append(Path(entry))
    return dirs


@dataclass(frozen=True)
class ExecutableState:
    names: dict[str, str]  # program name -> where it was found
    warnings: tuple[str, ...] = ()


class ExecutableInventory:
    """Programs that exist on this system, by name.

    Used only as *positive* evidence: a program with the item's name proves
    the application is installed. Not finding one proves nothing (AppImages
    and Flatpaks often bundle the tools they use), so absence is never used.
    """

    def __init__(
        self,
        home: Path,
        executable_dirs: Iterable[Path],
        opt_root: Path | None = OPT_ROOT,
        pipx_dirs: Iterable[Path] = PIPX_VENV_DIRS,
    ) -> None:
        self._home = home
        self._dirs = tuple(executable_dirs)
        self._opt_root = opt_root
        self._pipx_dirs = tuple(d if d.is_absolute() else home / d for d in pipx_dirs)

    def scan(self) -> ExecutableState:
        names: dict[str, str] = {}
        warnings: list[str] = []

        def add(name: str, where: Path) -> None:
            names.setdefault(name, str(where))

        for directory in self._dirs:
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            if entry.is_file() and os.access(entry.path, os.X_OK):
                                add(entry.name, Path(entry.path))
                        except OSError:
                            continue
            except OSError:
                continue  # missing PATH entries are normal
        if self._opt_root is not None and self._opt_root.is_dir():
            try:
                for vendor in sorted(self._opt_root.iterdir()):
                    if not vendor.is_dir():
                        continue
                    add(vendor.name, vendor)
                    try:
                        children = sorted(vendor.iterdir())
                    except OSError:
                        continue
                    for sub in children:
                        if sub.is_dir() and sub.name.lower() not in _GENERIC_OPT_DIRS:
                            add(sub.name, sub)  # /opt/google/chrome -> "chrome"
            except OSError as exc:
                warnings.append(f"cannot list {self._opt_root}: {exc}")
        for venvs in self._pipx_dirs:
            if venvs.is_dir():
                try:
                    for venv in venvs.iterdir():
                        add(venv.name, venv)
                except OSError as exc:
                    warnings.append(f"cannot list {venvs}: {exc}")
        return ExecutableState(names=names, warnings=tuple(warnings))
