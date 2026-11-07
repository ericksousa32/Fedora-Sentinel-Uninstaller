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

import argparse
import json
import os
import stat
import sys
import time
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

from fsu import heuristics
from fsu.heuristics import Match, MatchKind, NameIndex
from fsu.inventory import (
    DEFAULT_DESKTOP_DIRS,
    USER_FLATPAK_DESKTOP_DIR,
    AppImageInventory,
    AppImageState,
    DesktopIndex,
    DnfHistory,
    FlatpakInventory,
    RpmInventory,
    is_noise_package,
)
from fsu.models import Candidate, Category, Evidence, ScanReport
from fsu.paths import APPIMAGE_DIRS, SCAN_ROOTS, USER_APPLICATIONS_DIR
from fsu.protected import is_protected
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


def measure(path: Path) -> tuple[int, bool]:
    """Size in bytes of ``path`` without following symlinks or crossing mounts.

    Returns ``(bytes, complete)``; ``complete`` is ``False`` if any part could
    not be read or lived on another file system.
    """
    root_stat = path.lstat()
    if not stat.S_ISDIR(root_stat.st_mode):
        return root_stat.st_size, True
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
                if stat.S_ISDIR(entry_stat.st_mode):
                    if entry_stat.st_dev != root_stat.st_dev:
                        complete = False  # mount point: do not cross it
                        continue
                    stack.append(entry.path)
                else:
                    total += entry_stat.st_size
    return total, complete


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
        with host.open("rb") as handle:
            if PBI_MARKER not in handle.read(4096):
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


class HomeScanner:
    """Lists the first-level items of the scan roots."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.warnings: list[str] = []

    def entries(self) -> Iterator[tuple[Category, Path]]:
        for category, relative_root in SCAN_ROOTS.items():
            root = self.home / relative_root
            if root.is_symlink():
                # Root pointing elsewhere (e.g. a dotfiles manager): out of the
                # safe scope of this stage.
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
                yield category, root / name


class Collector:
    """Orchestrates inventories, the $HOME scan and scoring."""

    def __init__(
        self,
        home: Path | None = None,
        runner: Runner = run_command,
        desktop_dirs: Iterable[Path] | None = None,
        appimage_dirs: Iterable[Path] | None = None,
    ) -> None:
        self.home = (home or Path.home()).absolute()
        self._runner = runner
        if desktop_dirs is None:
            desktop_dirs = (*DEFAULT_DESKTOP_DIRS, self.home / USER_FLATPAK_DESKTOP_DIR)
        self._desktop_dirs = tuple(desktop_dirs)
        if appimage_dirs is None:
            appimage_dirs = tuple(self.home / d for d in APPIMAGE_DIRS)
        self._appimage_dirs = tuple(appimage_dirs)

    # -- inventories ---------------------------------------------------------

    def _rpm_installed(self, warnings: list[str]) -> frozenset[str] | None:
        try:
            return RpmInventory(self._runner).installed_names()
        except CommandError as exc:
            warnings.append(
                f"RPM inventory unavailable ({exc}); ~/.config, ~/.local/share and "
                "~/.cache will not be evaluated."
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
            self.home, self.home / USER_APPLICATIONS_DIR, self._appimage_dirs
        ).scan()
        warnings.extend(state.warnings)
        return state

    # -- scoring -------------------------------------------------------------

    @staticmethod
    def _score_rpm_or_appimage(
        name: str, removed: NameIndex, missing_appimages: NameIndex, evidence: list[Evidence]
    ) -> tuple[float, str | None]:
        """Best score among removed RPMs and gone AppImages (0.20 if neither)."""
        options: list[tuple[float, str, Evidence]] = []

        rpm = removed.match(name)
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
                detail = f"similar to removed package {rpm.name} ({rpm.strength:.2f})" + _via(rpm)
            options.append((score, rpm.name, Evidence("dnf5-history", detail)))

        app = missing_appimages.match(name)
        if app is not None:
            if app.kind is MatchKind.EXACT:
                score = heuristics.SCORE_EXACT_STALE_APPIMAGE
                detail = f"launcher points to a missing AppImage: {app.name}" + _via(app)
            else:
                score = heuristics.partial_removed_score(app.strength)
                detail = f"similar to missing AppImage {app.name} ({app.strength:.2f})" + _via(app)
            options.append((score, app.name, Evidence("appimage", detail)))

        if not options:
            evidence.append(Evidence("heuristic", "no known match"))
            return heuristics.SCORE_NO_MATCH, None
        options.sort(key=lambda o: (-o[0], o[1]))
        evidence.extend(o[2] for o in options)
        return options[0][0], options[0][1]

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
            size, complete = 0, True
            evidence.append(
                Evidence("fsu", "symlink: only the link is considered, never its target")
            )
        else:
            size, complete = measure(path)
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

        installed_exact: set[str] = set(rpm_installed or ()) | set(flatpak_ids or ())
        installed_names: set[str] = installed_exact | desktop_names | appimages.present_names
        for app_id in flatpak_ids or ():
            part = heuristics.useful_id_part(app_id)
            if part:
                installed_names.add(part)
        installed_index = NameIndex(installed_names)
        removed_index = NameIndex(removed)
        missing_index = NameIndex(appimages.missing_names)
        lap("indexes")

        scanner = HomeScanner(self.home)
        candidates: list[Candidate] = []
        protected: list[Path] = []
        regenerated: list[Path] = []
        installed_skipped = 0

        for category, path in scanner.entries():
            if is_protected(path, self.home):
                protected.append(path)
                continue

            evidence: list[Evidence] = []
            matched: str | None = None

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
            else:
                if rpm_installed is None:
                    continue
                if heuristics.owners_matching(path.name, installed_exact):
                    installed_skipped += 1
                    continue
                removed_owners = heuristics.owners_matching(path.name, removed)
                installed_match = installed_index.match(path.name)
                if installed_match is not None and installed_match.kind is MatchKind.EXACT:
                    installed_skipped += 1
                    continue
                if removed_owners:
                    score, matched = heuristics.SCORE_OWNER_REMOVED, removed_owners[0]
                    evidence.append(
                        Evidence(
                            "dnf5-history",
                            "folder owned by removed package(s): " + ", ".join(removed_owners),
                        )
                    )
                else:
                    score, matched = self._score_rpm_or_appimage(
                        path.name, removed_index, missing_index, evidence
                    )
                if installed_match is not None:
                    score = min(score, heuristics.SCORE_CAP_PARTIAL_INSTALLED)
                    evidence.append(
                        Evidence(
                            "installed",
                            f"similar to installed {installed_match.name} "
                            f"({installed_match.strength:.2f})" + _via(installed_match),
                        )
                    )

            candidate = self._candidate(path, category, score, evidence, matched, warnings)
            if candidate is not None:
                candidates.append(candidate)
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
        )


def _via(match: Match) -> str:
    return f" (via '{match.variant}')" if match.variant else ""


# -- manual run (temporary, until stage 4's cli.py) -----------------------------


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"  # pragma: no cover


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fsu.collector",
        description="READ-ONLY scan for application residues in $HOME.",
    )
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--timings", action="store_true", help="print time spent per phase")
    parser.add_argument("--home", type=Path, default=None, help="alternative home (testing)")
    args = parser.parse_args(argv)

    report = Collector(home=args.home).collect()
    if args.json:
        json.dump(report.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    for c in report.candidates:
        reasons = "; ".join(e.detail for e in c.evidence)
        print(f"{c.score:.2f}  {c.action.value:<25} {_human_size(c.size_bytes):>10}  {c.path}")
        print(f"      └─ {reasons}")
    total = sum(c.size_bytes for c in report.candidates)
    print(
        f"\n{len(report.candidates)} candidates ({_human_size(total)}); "
        f"{len(report.protected_skipped)} protected; "
        f"{report.installed_skipped} match something installed; "
        f"{len(report.regenerated_skipped)} recreated by other software."
    )
    for launcher in report.stale_launchers:
        print(f"stale launcher (AppImage is gone): {launcher}")
    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if args.timings:
        for phase, seconds in report.timings.items():
            print(f"timing: {phase:<15} {seconds:7.2f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
