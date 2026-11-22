"""Collection core (stage 1): finds residue candidates, READ-ONLY.

:meth:`Collector.collect` works in three steps:

1. Build the inventories: installed RPMs, RPMs removed in DNF5, installed
   Flatpaks, names from ``.desktop`` files and AppImages (present or gone).
2. List the first level of ``~/.config``, ``~/.local/share``, ``~/.cache``
   and ``~/.var/app`` without following symlinks (:class:`HomeScanner`).
3. Drop protected items, items recreated by other software and items that
   exactly match something installed; score the rest with
   :mod:`fsu.heuristics`.

Fail-safe: if the source needed to judge a category is unavailable (e.g.
``rpm`` or ``flatpak`` missing), that whole category is skipped instead of
everything looking "orphaned".

This module never creates, moves, changes or deletes files.
"""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path

from fsu import heuristics, history, inventory
from fsu.heuristics import Match, MatchKind, NameIndex
from fsu.inventory import (
    DEFAULT_DESKTOP_DIRS,
    USER_FLATPAK_DESKTOP_DIR,
    AppImageInventory,
    AppImageState,
    CommandStatus,
    DesktopIndex,
    DnfHistory,
    ExecutableInventory,
    FlatpakInventory,
    RpmInventory,
    check_command,
    is_noise_package,
    parse_desktop_file,
    parse_unit_file,
)
from fsu.models import Action, Candidate, Category, Evidence, ScanReport
from fsu.paths import APPIMAGE_DIRS, scan_roots, user_applications_dir
from fsu.protected import is_protected
from fsu.safeguard import symlink_in_chain
from fsu.system import CommandError, CommandNotFoundError, Runner, run_command

# Plasma Browser Integration (Plasma >= 6.3) writes native-messaging stubs for
# Flatpak browsers into ~/.var/app at every login, even if the browser is not
# installed. Such folders are recreated, so quarantining them is pointless.
PBI_HOST_FILE = "plasma-browser-integration-host"
PBI_MANIFEST_FILE = "org.kde.plasma.browser_integration.json"
PBI_MARKER = b"plasma-browser-integration"
PBI_HINT = (
    "they are recreated at login by Plasma Browser Integration; to stop it, disable "
    "'Plasma Browser Integration Flatpak Integration' in System Settings > "
    "Background Services"
)


@dataclass(frozen=True)
class TreeStats:
    size_bytes: int
    complete: bool  # False if some part was unreadable or on another mount
    # Most recent *write* inside the item (newest modification time of any file
    # or folder). Access times are deliberately not used: any program that
    # sweeps files (indexers, search tools, launchers scanning configs, backups)
    # updates them, as observed on a real system where 142 files of unrelated
    # applications were "accessed" within the same minute.
    last_activity: float


def _activity(info: os.stat_result) -> float:
    return info.st_mtime


def measure(path: Path) -> TreeStats:
    """Size and last activity of ``path``, without following symlinks or crossing mounts."""
    root_stat = path.lstat()
    last = _activity(root_stat)
    if not stat.S_ISDIR(root_stat.st_mode):
        return TreeStats(root_stat.st_size, True, last)
    total, complete = 0, True
    stack = [str(path)]
    while stack:
        directory = stack.pop()
        try:
            iterator = os.scandir(directory)
        except OSError:
            complete = False
            continue
        with iterator:
            for entry in iterator:
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError:
                    complete = False
                    continue
                last = max(last, _activity(entry_stat))
                if stat.S_ISDIR(entry_stat.st_mode):
                    if entry_stat.st_dev != root_stat.st_dev:
                        complete = False  # mount point: do not cross it
                        continue
                    stack.append(entry.path)
                else:
                    total += entry_stat.st_size
    return TreeStats(total, complete, last)


def _read_head(path: Path, size: int) -> bytes:
    """Read the start of a file without updating its access time when possible."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags | getattr(os, "O_NOATIME", 0))
    except PermissionError:  # O_NOATIME needs file ownership
        fd = os.open(path, flags)
    try:
        return os.read(fd, size)
    finally:
        os.close(fd)


def is_generated_stub(path: Path) -> bool:
    """Whether a ``~/.var/app/<id>`` folder only holds Plasma Browser Integration stubs.

    True only if the marker host file exists (and mentions the integrator) and
    every other file is the native-messaging manifest. Any other content - or
    any symlink - means real data and returns False.
    """
    if path.is_symlink() or not path.is_dir():
        return False
    host = path / PBI_HOST_FILE
    try:
        if host.is_symlink() or not host.is_file():
            return False
        if PBI_MARKER not in _read_head(host, 4096):
            return False
    except OSError:
        return False
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        current = Path(dirpath)
        for name in dirnames:
            if (current / name).is_symlink():
                return False
        for name in filenames:
            file = current / name
            if file == host:
                continue
            if name != PBI_MANIFEST_FILE or file.is_symlink():
                return False
    return True


LAUNCHER_SUFFIXES = (".desktop", ".service", ".timer")
# Areas where only high-confidence items (>= 0.70) are ever listed.
STRICT_CATEGORIES = frozenset({Category.HOME, Category.STATE, Category.LAUNCHER})


class HomeScanner:
    """Lists the first-level items of the scan roots."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.warnings: list[str] = []

    @staticmethod
    def _wanted(category: Category, path: Path) -> bool:
        """Pre-filter: in $HOME only real hidden folders; launchers are plain files."""
        if category is Category.HOME:
            return path.name.startswith(".") and path.is_dir() and not path.is_symlink()
        if category is Category.LAUNCHER:
            return (
                path.name.endswith(LAUNCHER_SUFFIXES) and path.is_file() and not path.is_symlink()
            )
        return True

    def entries(self) -> Iterator[tuple[Category, Path]]:
        all_roots = scan_roots(self.home)
        # Folders that contain other scan roots (~/.config, ~/.local, ...) are
        # scanned through those roots, never as items themselves.
        containers = {
            parent for roots in all_roots.values() for root in roots for parent in root.parents
        } | {root for roots in all_roots.values() for root in roots}
        for category, roots in all_roots.items():
            for root in roots:
                if symlink_in_chain(root, self.home):
                    # Root pointing elsewhere (e.g. a dotfiles manager): out of the
                    # safe scope.
                    self.warnings.append(f"{root} is a symlink; root skipped for safety.")
                    continue
                if not root.is_dir():
                    continue
                try:
                    names = sorted(child.name for child in root.iterdir())
                except OSError as exc:
                    self.warnings.append(f"cannot list {root}: {exc}")
                    continue
                for name in names:
                    path = root / name
                    if path not in containers and self._wanted(category, path):
                        yield category, path


class Collector:
    """Orchestrates inventories, the $HOME scan and scoring."""

    def __init__(
        self,
        home: Path | None = None,
        runner: Runner = run_command,
        desktop_dirs: Iterable[Path] | None = None,
        appimage_dirs: Iterable[Path] | None = None,
        executable_dirs: Iterable[Path] | None = None,
        opt_root: Path | None = None,
        record_history: bool = True,
    ) -> None:
        self.home = (home or Path.home()).absolute()
        self._runner = runner
        if desktop_dirs is None:
            desktop_dirs = (*DEFAULT_DESKTOP_DIRS, self.home / USER_FLATPAK_DESKTOP_DIR)
        self._desktop_dirs = tuple(desktop_dirs)
        if appimage_dirs is None:
            appimage_dirs = tuple(self.home / d for d in APPIMAGE_DIRS)
        self._appimage_dirs = tuple(appimage_dirs)
        self._executable_dirs = None if executable_dirs is None else tuple(executable_dirs)
        self._opt_root = opt_root
        self._record_history = record_history

    # -- inventories ---------------------------------------------------------

    def _rpm_installed(self, warnings: list[str]) -> frozenset[str] | None:
        try:
            return RpmInventory(self._runner).installed_names()
        except CommandError as exc:
            warnings.append(
                f"RPM inventory unavailable ({exc}); ~/.config, ~/.local/share, "
                "~/.cache, ~/.local/state and $HOME will not be evaluated."
            )
            return None

    def _removed(self, installed: frozenset[str] | None, warnings: list[str]) -> set[str]:
        history = DnfHistory(self._runner)
        try:
            removed = history.removed_packages()
        except CommandError as exc:
            warnings.append(f"DNF5 history unavailable ({exc}).")
            return set()
        finally:
            warnings.extend(history.warnings)
        installed = installed or frozenset()
        return {r.name for r in removed if r.name not in installed and not is_noise_package(r.name)}

    def _flatpak_installed(self, warnings: list[str]) -> frozenset[str] | None:
        try:
            return FlatpakInventory(self._runner).installed_app_ids()
        except CommandNotFoundError:
            warnings.append("Flatpak not found; ~/.var/app will not be evaluated.")
        except CommandError as exc:
            warnings.append(f"Flatpak inventory unavailable ({exc}); ~/.var/app skipped.")
        return None

    def _appimages(self, warnings: list[str]) -> AppImageState:
        state = AppImageInventory(
            self.home, user_applications_dir(self.home), self._appimage_dirs
        ).scan()
        warnings.extend(state.warnings)
        return state

    # -- scoring -------------------------------------------------------------

    @staticmethod
    def _score_rpm_or_appimage(
        name: str,
        removed: NameIndex,
        missing_appimages: NameIndex,
        evidence: list[Evidence],
        use_variants: bool = True,
        gone: NameIndex | None = None,
        gone_entries: dict[str, history.GoneEntry] | None = None,
    ) -> tuple[float, str | None]:
        """Best score among removed RPMs and gone AppImages (0.20 if neither)."""
        options: list[tuple[float, str, Evidence]] = []

        rpm = removed.match(name, use_variants)
        if rpm is not None:
            if rpm.kind is MatchKind.EXACT:
                score = (
                    heuristics.SCORE_EXACT_REMOVED
                    if rpm.variant is None
                    else heuristics.SCORE_VARIANT_REMOVED
                )
                detail = f"removed package: {rpm.name}" + _via(rpm)
            else:
                score = heuristics.partial_removed_score(rpm.strength)
                detail = similarity_text(rpm.strength, "removed package", rpm.name) + _via(rpm)
            options.append((score, rpm.name, Evidence("dnf5-history", detail)))

        app = missing_appimages.match(name, use_variants)
        if app is not None:
            if app.kind is MatchKind.EXACT:
                score = heuristics.SCORE_EXACT_STALE_APPIMAGE
                detail = f"launcher points to a missing AppImage: {app.name}" + _via(app)
            else:
                score = heuristics.partial_removed_score(app.strength)
                detail = similarity_text(app.strength, "missing AppImage", app.name) + _via(app)
            options.append((score, app.name, Evidence("appimage", detail)))

        seen = gone.match(name, use_variants) if gone is not None else None
        if seen is not None and seen.kind is MatchKind.EXACT and gone_entries:
            entry = gone_entries[seen.name]
            when = time.strftime("%Y-%m-%d", time.localtime(entry.last_seen))
            detail = f"{entry.kind} {entry.path} was present on {when} and is gone" + _via(seen)
            options.append(
                (heuristics.SCORE_SEEN_THEN_GONE, seen.name, Evidence("fsu-history", detail))
            )

        if not options:
            evidence.append(Evidence("heuristic", "no known match"))
            return heuristics.SCORE_NO_MATCH, None
        options.sort(key=lambda o: (-o[0], o[1]))
        evidence.extend(o[2] for o in options)
        return options[0][0], options[0][1]

    def _judge_launcher(
        self, path: Path, search_dirs: tuple[Path, ...], flatpak_ids: frozenset[str] | None
    ) -> tuple[str, float, str | None, list[Evidence]]:
        """Verdict for a launcher/service file: ("installed"|"hidden"|"intentional"|"candidate")."""
        try:
            if path.suffix == ".desktop":
                entry = parse_desktop_file(path)
                if entry.hidden:
                    return "intentional", 0.0, None, []
                line, systemd = entry.exec_line, False
            else:
                line, systemd = parse_unit_file(path).exec_line, path.parent.name == "user"
        except OSError:
            return "hidden", 0.0, None, []
        if not line:
            return "hidden", 0.0, None, []
        check = check_command(line, search_dirs, flatpak_ids, systemd=systemd)
        if check.status is CommandStatus.FOUND:
            return "installed", 0.0, None, []
        if check.status is CommandStatus.UNKNOWN:
            return "hidden", 0.0, None, []
        score = (
            heuristics.SCORE_LAUNCHER_TARGET_MISSING
            if check.status is CommandStatus.MISSING
            else heuristics.SCORE_LAUNCHER_PROGRAM_NOT_FOUND
        )
        return "candidate", score, check.program, [Evidence("launcher", check.detail)]

    def _candidate(
        self, path: Path, category: Category, score: float, evidence: list[Evidence],
        matched: str | None, warnings: list[str],
    ) -> Candidate | None:  # fmt: skip
        try:
            info = path.lstat()
        except OSError as exc:
            warnings.append(f"cannot inspect {path}: {exc}")
            return None
        is_link = stat.S_ISLNK(info.st_mode)
        if is_link:
            size, complete, last_activity = 0, True, info.st_mtime
            evidence.append(
                Evidence("fsu", "symlink: only the link is considered, never its target")
            )
        else:
            stats = measure(path)
            size, complete, last_activity = stats.size_bytes, stats.complete, stats.last_activity
        if not complete:
            warnings.append(f"size of {path} is partial (unreadable content or another mount).")
        if info.st_uid != os.getuid():
            evidence.append(Evidence("fsu", f"owned by another user (uid {info.st_uid})"))
        return Candidate(
            path=path,
            category=category,
            score=score,
            action=heuristics.action_for(score),
            size_bytes=size,
            mtime=info.st_mtime,
            last_activity=last_activity,
            is_symlink=is_link,
            matched_name=matched,
            evidence=tuple(evidence),
        )

    def collect(self) -> ScanReport:
        warnings: list[str] = []
        timings: dict[str, float] = {}
        clock = time.perf_counter()

        def lap(phase: str) -> None:
            nonlocal clock
            now = time.perf_counter()
            timings[phase] = now - clock
            clock = now

        rpm_installed = self._rpm_installed(warnings)
        lap("rpm")
        removed = self._removed(rpm_installed, warnings)
        lap("dnf5_history")
        flatpak_ids = self._flatpak_installed(warnings)
        lap("flatpak")
        desktop = DesktopIndex(self._desktop_dirs)
        desktop_names = desktop.known_names()
        warnings.extend(desktop.warnings)
        lap("desktop_files")
        appimages = self._appimages(warnings)
        lap("appimages")
        dirs = tuple(
            self._executable_dirs
            if self._executable_dirs is not None
            else inventory.default_executable_dirs(self.home)
        )
        executables = ExecutableInventory(
            self.home,
            dirs,
            self._opt_root if self._opt_root is not None else inventory.OPT_ROOT,
            inventory.PIPX_VENV_DIRS,
        ).scan()
        warnings.extend(executables.warnings)
        lap("executables")
        observed = history.observe(
            self._appimage_dirs, user_applications_dir(self.home), executables.names
        )
        past = history.InventoryHistory(history.history_path(self.home)).update(
            observed, record=self._record_history
        )
        warnings.extend(past.warnings)
        gone_names: dict[str, history.GoneEntry] = {}
        for entry in past.gone:
            for name in entry.names:
                gone_names.setdefault(name, entry)
        processes = inventory.running_process_names()
        lap("history")

        installed_exact: set[str] = set(rpm_installed or ()) | set(flatpak_ids or ())
        installed_names: set[str] = installed_exact | desktop_names | appimages.present_names
        for app_id in flatpak_ids or ():
            part = heuristics.useful_id_part(app_id)
            if part:
                installed_names.add(part)
        installed_index = NameIndex(installed_names)
        removed_index = NameIndex(removed)
        missing_index = NameIndex(appimages.missing_names)
        executable_index = NameIndex(executables.names)
        gone_index = NameIndex(gone_names)
        launcher_index = NameIndex(appimages.present_names | desktop_names)
        process_index = NameIndex(processes)
        lap("indexes")

        scanner = HomeScanner(self.home)
        candidates: list[Candidate] = []
        protected: list[Path] = []
        regenerated: list[Path] = []
        installed_skipped = 0
        hidden = 0
        launcher_scores: dict[Path, tuple[float, str | None]] = {}
        timers: list[Path] = []

        for category, path in scanner.entries():
            if is_protected(path, self.home):
                protected.append(path)
                continue

            evidence: list[Evidence] = []
            matched: str | None = None
            strict = category in STRICT_CATEGORIES

            if category is Category.FLATPAK:
                if flatpak_ids is None:
                    continue
                if path.name in flatpak_ids:
                    installed_skipped += 1
                    continue
                if is_generated_stub(path):
                    regenerated.append(path)
                    continue
                score = heuristics.SCORE_FLATPAK_ORPHAN
                matched = path.name
                evidence.append(Evidence("flatpak", f"app {path.name} is not installed"))
            elif category is Category.LAUNCHER:
                if path.suffix == ".timer":
                    timers.append(path)  # judged after the services
                    continue
                verdict, score, matched, evidence = self._judge_launcher(path, dirs, flatpak_ids)
                if verdict == "installed":
                    installed_skipped += 1
                    continue
                if verdict == "intentional":
                    protected.append(path)
                    continue
                if verdict == "hidden":
                    hidden += 1
                    continue
                launcher_scores[path] = (score, matched)
            else:
                if rpm_installed is None:
                    continue
                # In $HOME itself only exact names count: no variants, no similarity.
                variants = category is not Category.HOME
                if heuristics.owners_matching(path.name, installed_exact):
                    installed_skipped += 1
                    continue
                removed_owners = heuristics.owners_matching(path.name, removed)
                installed_match = installed_index.match(path.name, variants)
                if installed_match is not None and installed_match.kind is MatchKind.EXACT:
                    installed_skipped += 1
                    continue
                # A program with the same name exists (e.g. /usr/local/bin/konsave):
                # the application is installed. Its absence proves nothing.
                program = executable_index.match(path.name, variants)
                if program is not None and program.kind is MatchKind.EXACT:
                    installed_skipped += 1
                    continue
                if removed_owners:
                    score, matched = heuristics.SCORE_OWNER_REMOVED, removed_owners[0]
                    evidence.append(
                        Evidence(
                            "dnf5-history",
                            "folder owned by removed package(s): " + _short_list(removed_owners),
                        )
                    )
                else:
                    score, matched = self._score_rpm_or_appimage(
                        path.name,
                        removed_index,
                        missing_index,
                        evidence,
                        variants,
                        gone_index,
                        gone_names,
                    )
                    if not variants and score < heuristics.SCORE_EXACT_STALE_APPIMAGE:
                        score = heuristics.SCORE_NO_MATCH  # no partial matches in $HOME
                if installed_match is not None:
                    score = min(score, heuristics.SCORE_CAP_PARTIAL_INSTALLED)
                    evidence.append(
                        Evidence(
                            "installed",
                            similarity_text(
                                installed_match.strength, "installed app", installed_match.name
                            )
                            + _via(installed_match),
                        )
                    )

            if strict and score < heuristics.THRESHOLD_QUARANTINE:
                hidden += 1
                continue
            candidate = self._candidate(path, category, score, evidence, matched, warnings)
            if candidate is not None:
                if candidate.action is Action.LIST_ONLY:
                    candidate = _with_facts(
                        candidate, executable_index, launcher_index, process_index
                    )
                candidates.append(candidate)

        # A timer of a removed service fires a unit that no longer works.
        for timer in timers:
            try:
                unit = parse_unit_file(timer).unit or timer.with_suffix(".service").name
            except OSError:
                hidden += 1
                continue
            service = timer.parent / unit
            if service in launcher_scores:
                score, matched = launcher_scores[service]
                evidence = [Evidence("launcher", f"timer of {unit}, whose program is gone")]
                candidate = self._candidate(
                    timer, Category.LAUNCHER, score, evidence, matched, warnings
                )
                if candidate is not None:
                    candidates.append(candidate)
            elif service.exists():
                installed_skipped += 1
            else:
                hidden += 1
        lap("scan_and_score")

        if regenerated:
            warnings.append(f"{len(regenerated)} folder(s) in ~/.var/app skipped: {PBI_HINT}.")
        warnings.extend(scanner.warnings)
        candidates.sort(key=lambda c: (-c.score, str(c.path)))
        return ScanReport(
            candidates=tuple(candidates),
            warnings=tuple(warnings),
            protected_skipped=tuple(protected),
            installed_skipped=installed_skipped,
            regenerated_skipped=tuple(regenerated),
            stale_launchers=appimages.stale_launchers,
            timings=timings,
            low_confidence_hidden=hidden,
        )


def similarity_text(strength: float, what: str, name: str) -> str:
    """Unambiguous, self-describing wording for a partial name match.

    A bare "(0.89)" was read by a model as a version number ("Comfy Desktop
    0.89"), and weak similarities were taken as proof; the strength is now
    spelled out.
    """
    if strength >= heuristics.SIMILARITY_STRONG:
        label = "strong"
    elif strength >= heuristics.SIMILARITY_MODERATE:
        label = "moderate"
    else:
        label = "weak"
    return f"{label} name similarity ({strength:.0%}) to {what} '{name}'"


def _with_facts(
    candidate: Candidate, programs: NameIndex, launchers: NameIndex, processes: NameIndex
) -> Candidate:
    """Objective facts for a list-only item, to help the user decide.

    Facts never change the score and are never sent to the AI: a missing
    program proves nothing (apps often bundle their tools or use other names),
    yet models treat such absences as proof. They are for the user only.
    """
    name = candidate.path.name
    found = []
    for label, index in (("program", programs), ("launcher", launchers), ("process", processes)):
        match = index.match(name)
        if match is not None and match.kind is MatchKind.EXACT:
            found.append(f"{label} '{match.name}' exists")
    if candidate.last_activity:
        days = max(0, int((time.time() - candidate.last_activity) / 86400))
        facts = [f"last write {days} day(s) ago"]
    else:
        facts = []
    facts += found or [f"no program, launcher or running process with exactly this name ('{name}')"]
    return replace(candidate, evidence=(*candidate.evidence, Evidence("facts", "; ".join(facts))))


def _short_list(names: list[str], limit: int = 3) -> str:
    shown = ", ".join(names[:limit])
    return f"{shown} (+{len(names) - limit} more)" if len(names) > limit else shown


def _via(match: Match) -> str:
    return f" (via '{match.variant}')" if match.variant else ""
