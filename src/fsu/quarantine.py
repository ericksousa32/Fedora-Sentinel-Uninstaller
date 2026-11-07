"""Quarantine (stage 2): reversible moves, restore, purge and crash recovery.

Layout::

    ~/.local/share/fsu/
    ├── log.jsonl                  # append-only record of every operation
    └── quarantine/<id>/
        ├── manifest.json          # where the item came from and its state
        └── payload                # the item itself, moved with rename()

Moves are a single ``os.rename`` on the same file system (checked by
:mod:`fsu.safeguard`), so they are atomic and keep permissions, timestamps,
extended attributes, SELinux labels and symlinks exactly as they were.

The manifest is written *before* the move (state ``moving``) and updated
after it (state ``quarantined``); :meth:`QuarantineStore.recover` settles
entries left in between by a crash.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shutil
import stat
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fsu import __version__
from fsu.models import Candidate
from fsu.paths import fsu_data_dir, quarantine_dir
from fsu.protected import matching_root
from fsu.safeguard import (
    MOUNTINFO,
    PROC_ROOT,
    Block,
    Protection,
    SnapshotProvider,
    check_in_use,
    decide_protection,
    ensure_not_root,
    preflight,
    revalidate,
    symlink_in_chain,
)
from fsu.system import CommandError

STATE_MOVING = "moving"
STATE_QUARANTINED = "quarantined"
MANIFEST = "manifest.json"
PAYLOAD = "payload"
_ENTRY_ID = re.compile(r"^\d{8}T\d{6}-[0-9a-f]{6}$")


class QuarantineError(RuntimeError):
    """An operation on the quarantine could not be completed safely."""


@dataclass
class Manifest:
    id: str
    original_path: str
    category: str
    score: float
    action: str
    matched_name: str | None
    evidence: list[dict[str, str]]
    size_bytes: int
    is_symlink: bool
    uid: int
    gid: int
    mode: int
    mtime: float
    quarantined_at: float
    state: str
    snapshot: dict[str, Any] | None = None
    fsu_version: str = __version__

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Manifest:
        try:
            return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})
        except TypeError as exc:
            raise QuarantineError(f"invalid manifest: {exc}") from exc


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
    _fsync_dir(path.parent)


class QuarantineStore:
    """Reversible storage for quarantined items under ``~/.local/share/fsu``."""

    def __init__(self, home: Path, clock: Callable[[], float] = time.time) -> None:
        self.home = home
        self.root = quarantine_dir(home)
        self.log_path = fsu_data_dir(home) / "log.jsonl"
        self._clock = clock

    # -- helpers -----------------------------------------------------------------

    def _ensure_dirs(self) -> None:
        for directory in (fsu_data_dir(self.home), self.root):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise QuarantineError(f"{directory} is not a real directory")

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Exclusive lock for operations that change the quarantine.

        Two FSU processes moving, restoring or purging at the same time could
        race on the same items; the second one fails instead of waiting.
        """
        self._ensure_dirs()
        path = fsu_data_dir(self.home) / ".lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise QuarantineError("another FSU operation is in progress") from exc
            yield
        finally:
            os.close(fd)  # closing the descriptor releases the lock

    def _log(self, event: str, **details: Any) -> None:
        self._ensure_dirs()
        record = {"time": self._clock(), "event": event, **details}
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _entry_dir(self, entry_id: str) -> Path:
        if not _ENTRY_ID.match(entry_id):
            raise QuarantineError(f"invalid quarantine id: {entry_id!r}")
        entry = self.root / entry_id
        if entry.is_symlink() or not entry.is_dir():
            raise QuarantineError(f"quarantine entry {entry_id} not found")
        return entry

    def _new_id(self) -> str:
        stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(self._clock()))
        return f"{stamp}-{secrets.token_hex(3)}"

    def load(self, entry_id: str) -> Manifest:
        entry = self._entry_dir(entry_id)
        try:
            data = json.loads((entry / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise QuarantineError(f"cannot read manifest of {entry_id}: {exc}") from exc
        if not isinstance(data, dict):
            raise QuarantineError(f"invalid manifest of {entry_id}")
        return Manifest.from_dict(data)

    def _validated_original(self, manifest: Manifest) -> Path:
        """The manifest is user-writable data: never trust its path blindly."""
        original = Path(manifest.original_path)
        if not original.is_absolute() or ".." in original.parts:
            raise QuarantineError(f"unsafe original path in manifest: {original}")
        found = matching_root(original, self.home)
        if found is None or found[0].value != manifest.category:
            raise QuarantineError(f"original path is outside the scan roots: {original}")
        if symlink_in_chain(found[1], self.home):
            raise QuarantineError(f"{found[1]} (or a folder above it) is a symlink")
        try:
            expected = original.parent.resolve(strict=True)
            if found[1] == self.home or self.home in found[1].parents:
                wanted = self.home.resolve(strict=True) / found[1].relative_to(self.home)
            else:
                wanted = found[1].parent.resolve(strict=True) / found[1].name
        except OSError as exc:
            raise QuarantineError(f"cannot resolve {original.parent}: {exc}") from exc
        if expected != wanted:
            raise QuarantineError(f"{original.parent} does not resolve to its scan root")
        return original

    # -- operations --------------------------------------------------------------

    def entries(self) -> list[Manifest]:
        if not self.root.is_dir():
            return []
        manifests = []
        for entry in sorted(self.root.iterdir()):
            if _ENTRY_ID.match(entry.name):
                try:
                    manifests.append(self.load(entry.name))
                except QuarantineError:
                    continue
        return manifests

    def quarantine(self, candidate: Candidate, snapshot: dict[str, Any] | None = None) -> Manifest:
        """Move one item into the quarantine. Callers must run the safeguard checks first."""
        source = candidate.path
        info = source.lstat()
        self._ensure_dirs()
        entry_id = self._new_id()
        entry = self.root / entry_id
        entry.mkdir(mode=0o700)
        manifest = Manifest(
            id=entry_id,
            original_path=str(source),
            category=candidate.category.value,
            score=candidate.score,
            action=candidate.action.value,
            matched_name=candidate.matched_name,
            evidence=[{"source": e.source, "detail": e.detail} for e in candidate.evidence],
            size_bytes=candidate.size_bytes,
            is_symlink=stat.S_ISLNK(info.st_mode),
            uid=info.st_uid,
            gid=info.st_gid,
            mode=stat.S_IMODE(info.st_mode),
            mtime=info.st_mtime,
            quarantined_at=self._clock(),
            state=STATE_MOVING,
            snapshot=snapshot,
        )
        _write_json_atomic(entry / MANIFEST, asdict(manifest))
        try:
            source.rename(entry / PAYLOAD)
        except OSError as exc:
            shutil.rmtree(entry, ignore_errors=True)
            raise QuarantineError(f"cannot move {source}: {exc}") from exc
        _fsync_dir(source.parent)
        manifest.state = STATE_QUARANTINED
        _write_json_atomic(entry / MANIFEST, asdict(manifest))
        self._log("quarantine", id=entry_id, path=str(source), snapshot=snapshot)
        return manifest

    def restore(self, entry_id: str) -> Path:
        """Move an item back to where it came from. Never overwrites anything."""
        entry = self._entry_dir(entry_id)
        manifest = self.load(entry_id)
        if manifest.state != STATE_QUARANTINED:
            raise QuarantineError(f"{entry_id} is in state {manifest.state!r}; run recover first")
        original = self._validated_original(manifest)
        payload = entry / PAYLOAD
        if not os.path.lexists(payload):
            raise QuarantineError(f"payload of {entry_id} is missing")
        if os.path.lexists(original):
            raise QuarantineError(f"{original} already exists; refusing to overwrite it")
        try:
            payload.rename(original)
        except OSError as exc:
            raise QuarantineError(f"cannot restore {original}: {exc}") from exc
        _fsync_dir(original.parent)
        self._remove_entry(entry)
        self._log("restore", id=entry_id, path=str(original))
        return original

    def _remove_entry(self, entry: Path) -> None:
        """Delete an entry directory safely (no symlink following, read-only dirs ok)."""
        if entry.parent.resolve() != self.root.resolve() or entry.is_symlink():
            raise QuarantineError(f"refusing to delete {entry}")
        if not shutil.rmtree.avoids_symlink_attacks:
            raise QuarantineError("this platform's rmtree is not safe against symlink attacks")
        for directory, _dirs, _files in os.walk(entry, followlinks=False):
            mode = os.lstat(directory).st_mode
            if stat.S_ISDIR(mode) and (mode & 0o700) != 0o700:
                Path(directory).chmod(mode | 0o700)
        shutil.rmtree(entry)

    def purge(self, entry_id: str) -> None:
        """Permanently delete one quarantined item."""
        entry = self._entry_dir(entry_id)
        manifest = self.load(entry_id)
        self._remove_entry(entry)
        self._log("purge", id=entry_id, path=manifest.original_path)

    def purge_older_than(self, seconds: float) -> list[str]:
        limit = self._clock() - seconds
        purged = []
        for manifest in self.entries():
            if manifest.state == STATE_QUARANTINED and manifest.quarantined_at < limit:
                self.purge(manifest.id)
                purged.append(manifest.id)
        return purged

    def recover(self) -> list[str]:
        """Settle entries interrupted between the two manifest writes."""
        messages: list[str] = []
        if not self.root.is_dir():
            return messages
        for entry in sorted(self.root.iterdir()):
            if not _ENTRY_ID.match(entry.name):
                continue
            try:
                manifest = self.load(entry.name)
            except QuarantineError as exc:
                messages.append(f"{entry.name}: {exc} (left untouched)")
                continue
            if manifest.state != STATE_MOVING:
                continue
            payload = entry / PAYLOAD
            original = Path(manifest.original_path)
            if os.path.lexists(payload):
                manifest.state = STATE_QUARANTINED
                _write_json_atomic(entry / MANIFEST, asdict(manifest))
                messages.append(f"{entry.name}: move had completed; marked as quarantined")
                self._log("recover-complete", id=entry.name)
            elif os.path.lexists(original):
                self._remove_entry(entry)
                messages.append(f"{entry.name}: move never happened; entry removed")
                self._log("recover-rollback", id=entry.name)
            else:
                messages.append(f"{entry.name}: item not found anywhere (left untouched)")
        return messages


# -- batch execution ----------------------------------------------------------------


@dataclass
class ItemOutcome:
    path: Path
    status: str  # "quarantined" | "would-quarantine" | "blocked" | "failed"
    blocks: list[Block] = field(default_factory=list)
    protection: Protection = Protection.NONE
    entry_id: str | None = None


@dataclass
class BatchResult:
    outcomes: list[ItemOutcome]
    snapshot: dict[str, Any] | None
    snapshot_status: str


def execute(
    candidates: Iterable[Candidate],
    home: Path,
    *,
    store: QuarantineStore,
    snapshots: SnapshotProvider,
    selected: Iterable[Path] = (),
    allow_without_snapshot: bool = False,
    dry_run: bool = True,
    proc_root: Path = PROC_ROOT,
    mountinfo: Path = MOUNTINFO,
) -> BatchResult:
    """Plan and (unless ``dry_run``) quarantine a batch of candidates.

    One Snapper snapshot is created for the whole batch, only if at least one
    allowed item needs it. Each item is re-checked right before it is moved.
    """
    ensure_not_root()
    items = list(candidates)
    by_path = {c.path: c for c in items}
    chosen = {Path(p) for p in selected}
    status = snapshots.detect()
    outcomes: list[ItemOutcome] = []

    for candidate in items:
        protection, blocks = decide_protection(
            candidate,
            snapshot_available=status.available,
            explicitly_selected=candidate.path in chosen,
            allow_without_snapshot=allow_without_snapshot,
        )
        blocks += preflight(candidate, home, store.root, proc_root, mountinfo)
        outcome = ItemOutcome(candidate.path, "blocked" if blocks else "pending", blocks)
        outcome.protection = protection
        outcomes.append(outcome)

    allowed = [o for o in outcomes if o.status == "pending"]
    if dry_run:
        for outcome in allowed:
            outcome.status = "would-quarantine"
        return BatchResult(outcomes, None, status.reason)

    snapshot: dict[str, Any] | None = None
    if any(o.protection is Protection.SNAPSHOT for o in allowed):
        try:
            config, number = snapshots.create(f"fsu: before quarantining {len(allowed)} item(s)")
            snapshot = {"config": config, "number": number}
        except CommandError as exc:
            for outcome in allowed:
                if outcome.protection is Protection.SNAPSHOT:
                    outcome.status = "blocked"
                    outcome.blocks.append(Block("snapshot-failed", str(exc)))

    for outcome in outcomes:
        if outcome.status != "pending":
            continue
        candidate = by_path[outcome.path]
        # Last-moment re-check: the item may have changed or been opened since planning.
        late = revalidate(candidate, home) or check_in_use(
            candidate.path, candidate.matched_name, proc_root
        )
        if late:
            outcome.status, outcome.blocks = "blocked", late
            continue
        try:
            manifest = store.quarantine(
                candidate, snapshot if outcome.protection is Protection.SNAPSHOT else None
            )
        except (QuarantineError, OSError) as exc:
            outcome.status = "failed"
            outcome.blocks.append(Block("move-failed", str(exc)))
            continue
        outcome.status, outcome.entry_id = "quarantined", manifest.id
    return BatchResult(outcomes, snapshot, status.reason)
