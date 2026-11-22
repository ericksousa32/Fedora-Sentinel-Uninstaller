from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import FakeRunner
from fsu.models import Candidate
from fsu.quarantine import (
    MANIFEST,
    PAYLOAD,
    STATE_MOVING,
    QuarantineError,
    QuarantineStore,
    execute,
)
from fsu.safeguard import RootRefusedError, SnapshotProvider, mount_point_of
from fsu.system import CommandError
from helpers import candidate, make, running


@pytest.fixture
def store(home: Path) -> QuarantineStore:
    return QuarantineStore(home, clock=lambda: 1_800_000_000.0)


@pytest.fixture
def regular_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """execute() refuses root; CI's Fedora container runs tests as root."""
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.delenv("SUDO_UID", raising=False)
    monkeypatch.delenv("PKEXEC_UID", raising=False)


def _tree(root: Path) -> dict[str, tuple[int, float, bytes | str]]:
    out: dict[str, tuple[int, float, bytes | str]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            info = path.lstat()
            if path.is_symlink():
                content: bytes | str = str(path.readlink())
            elif path.is_file():
                content = path.read_bytes()
            else:
                content = b""
            out[str(path.relative_to(root))] = (info.st_mode, info.st_mtime, content)
    return out


# -- store ------------------------------------------------------------------------------


def test_quarantine_and_restore_roundtrip(home: Path, store: QuarantineStore) -> None:
    item = home / ".config/hexchat"
    make(item / "servlist.conf", 100)
    make(item / "sub/deep.txt", 5)
    (item / "sub/deep.txt").chmod(0o600)
    (item / "link").symlink_to("servlist.conf")
    before = _tree(item)

    manifest = store.quarantine(candidate(item, home, 0.85, "hexchat"))

    assert not os.path.lexists(item)
    payload = store.root / manifest.id / PAYLOAD
    assert _tree(payload) == before  # modes, mtimes, contents and links preserved
    saved = json.loads((store.root / manifest.id / MANIFEST).read_text())
    assert saved["state"] == "quarantined"
    assert saved["original_path"] == str(item)
    assert (store.root.stat().st_mode & 0o777) == 0o700
    assert [m.id for m in store.entries()] == [manifest.id]

    assert store.restore(manifest.id) == item
    assert _tree(item) == before
    assert store.entries() == []
    events = [json.loads(line)["event"] for line in store.log_path.read_text().splitlines()]
    assert events == ["quarantine", "restore"]


def test_symlink_item_moves_the_link_only(
    home: Path, store: QuarantineStore, tmp_path: Path
) -> None:
    target = tmp_path / "dotfiles/hexchat"
    make(target / "a")
    link = home / ".config/hexchat"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)

    manifest = store.quarantine(candidate(link, home, 0.85))
    payload = store.root / manifest.id / PAYLOAD
    assert payload.is_symlink()
    assert payload.readlink() == target
    assert (target / "a").exists()  # target untouched
    store.restore(manifest.id)
    assert link.is_symlink()


def test_restore_never_overwrites(home: Path, store: QuarantineStore) -> None:
    item = home / ".config/hexchat"
    make(item / "a")
    manifest = store.quarantine(candidate(item, home, 0.85))
    make(item / "new")  # the app recreated its folder meanwhile
    with pytest.raises(QuarantineError, match="already exists"):
        store.restore(manifest.id)
    assert (store.root / manifest.id / PAYLOAD / "a").exists()


@pytest.mark.parametrize(
    "tampered",
    ["/etc/hexchat", "{home}/.ssh", "{home}/.config/../.ssh", "{home}/.config/a/b", "relative"],
)
def test_restore_rejects_tampered_manifest(
    home: Path, store: QuarantineStore, tampered: str
) -> None:
    item = home / ".config/hexchat"
    make(item / "a")
    manifest = store.quarantine(candidate(item, home, 0.85))
    path = store.root / manifest.id / MANIFEST
    data = json.loads(path.read_text())
    data["original_path"] = tampered.format(home=home)
    path.write_text(json.dumps(data))
    with pytest.raises(QuarantineError):
        store.restore(manifest.id)


@pytest.mark.parametrize("bad_id", ["../x", "20260101T000000-abc", "", "/etc"])
def test_invalid_ids_are_rejected(store: QuarantineStore, bad_id: str) -> None:
    with pytest.raises(QuarantineError, match="invalid quarantine id"):
        store.restore(bad_id)
    with pytest.raises(QuarantineError, match="invalid quarantine id"):
        store.purge(bad_id)


def test_purge_handles_read_only_directories(home: Path, store: QuarantineStore) -> None:
    item = home / ".config/hexchat"
    make(item / "ro/a")
    (item / "ro").chmod(0o500)
    manifest = store.quarantine(candidate(item, home, 0.85))
    store.purge(manifest.id)
    assert not (store.root / manifest.id).exists()
    assert store.entries() == []


def test_purge_does_not_follow_symlinks(home: Path, store: QuarantineStore, tmp_path: Path) -> None:
    outside = tmp_path / "precious"
    make(outside / "keep.txt")
    item = home / ".config/hexchat"
    make(item / "a")
    (item / "escape").symlink_to(outside)
    manifest = store.quarantine(candidate(item, home, 0.85))
    store.purge(manifest.id)
    assert (outside / "keep.txt").exists()


def test_purge_older_than(home: Path) -> None:
    now = [1_000.0]
    store = QuarantineStore(home, clock=lambda: now[0])
    make(home / ".config/old/a")
    make(home / ".config/new/a")
    old = store.quarantine(candidate(home / ".config/old", home, 0.85))
    now[0] = 1_000.0 + 40 * 86400
    store.quarantine(candidate(home / ".config/new", home, 0.85))
    assert store.purge_older_than(30 * 86400) == [old.id]
    assert [m.original_path for m in store.entries()] == [str(home / ".config/new")]


def test_failed_move_leaves_no_entry(home: Path, store: QuarantineStore) -> None:
    item = home / ".config/hexchat"
    make(item / "a")
    cand = candidate(item, home, 0.85)
    (item / "a").unlink()
    item.rmdir()
    with pytest.raises((QuarantineError, OSError)):
        store.quarantine(cand)
    assert store.entries() == []


def _interrupted(store: QuarantineStore, home: Path, moved: bool) -> str:
    """Simulate a crash between the move and the second manifest write."""
    item = home / ".config/hexchat"
    make(item / "a")
    manifest = store.quarantine(candidate(item, home, 0.85))
    entry = store.root / manifest.id
    data = json.loads((entry / MANIFEST).read_text())
    data["state"] = STATE_MOVING
    (entry / MANIFEST).write_text(json.dumps(data))
    if not moved:
        (entry / PAYLOAD).rename(item)
    return manifest.id


def test_recover_completed_move(home: Path, store: QuarantineStore) -> None:
    entry_id = _interrupted(store, home, moved=True)
    assert "marked as quarantined" in store.recover()[0]
    assert store.load(entry_id).state == "quarantined"


def test_recover_move_that_never_happened(home: Path, store: QuarantineStore) -> None:
    entry_id = _interrupted(store, home, moved=False)
    assert "entry removed" in store.recover()[0]
    assert not (store.root / entry_id).exists()
    assert (home / ".config/hexchat/a").exists()


def test_restore_refuses_unrecovered_entry(home: Path, store: QuarantineStore) -> None:
    entry_id = _interrupted(store, home, moved=True)
    with pytest.raises(QuarantineError, match="recover"):
        store.restore(entry_id)


def test_recover_leaves_corrupt_manifest_untouched(home: Path, store: QuarantineStore) -> None:
    make(home / ".config/hexchat/a")
    manifest = store.quarantine(candidate(home / ".config/hexchat", home, 0.85))
    (store.root / manifest.id / MANIFEST).write_text("{not json")
    assert "left untouched" in store.recover()[0]
    assert (store.root / manifest.id / PAYLOAD).exists()


# -- execute ----------------------------------------------------------------------------


def _provider(home: Path, available: bool, create_ok: bool = True) -> SnapshotProvider:
    if not available:
        return SnapshotProvider(home, FakeRunner({}))
    configs = json.dumps([{"config": "home", "subvolume": str(mount_point_of(home))}])
    create: str | BaseException = "7\n" if create_ok else CommandError(["snapper"], "create failed")
    return SnapshotProvider(
        home,
        FakeRunner(
            {
                ("snapper", "--jsonout", "list-configs"): configs,
                ("snapper", "-c", "home", "list"): "",
                ("snapper", "-c", "home", "create"): create,
            }
        ),
    )


@pytest.fixture
def batch(home: Path) -> list[Path]:
    paths = [home / ".config/high", home / ".config/medium", home / ".config/low"]
    for path in paths:
        make(path / "a")
    return paths


def _cands(home: Path, paths: list[Path]) -> list[Candidate]:
    scores = [0.85, 0.57, 0.20]
    return [candidate(p, home, s) for p, s in zip(paths, scores, strict=True)]


@pytest.mark.usefixtures("regular_user")
def test_execute_dry_run_changes_nothing(
    home: Path, store: QuarantineStore, batch: list[Path]
) -> None:
    result = execute(_cands(home, batch), home, store=store, snapshots=_provider(home, False))
    assert [o.status for o in result.outcomes] == ["would-quarantine", "blocked", "blocked"]
    assert all(p.exists() for p in batch)
    assert not store.root.exists()


@pytest.mark.usefixtures("regular_user")
def test_execute_without_snapper(home: Path, store: QuarantineStore, batch: list[Path]) -> None:
    result = execute(
        _cands(home, batch),
        home,
        store=store,
        snapshots=_provider(home, False),
        selected=[batch[2]],
        dry_run=False,
    )
    statuses = {o.path.name: (o.status, [b.code for b in o.blocks]) for o in result.outcomes}
    assert statuses == {
        "high": ("quarantined", []),
        "medium": ("blocked", ["snapshot-required"]),
        "low": ("blocked", ["snapshot-required"]),  # selected, but never without snapshot
    }
    assert result.snapshot is None
    assert not batch[0].exists()
    assert batch[1].exists()
    assert batch[2].exists()


@pytest.mark.usefixtures("regular_user")
def test_execute_medium_with_explicit_confirmation(
    home: Path, store: QuarantineStore, batch: list[Path]
) -> None:
    result = execute(
        _cands(home, batch)[:2],
        home,
        store=store,
        snapshots=_provider(home, False),
        allow_without_snapshot=True,
        dry_run=False,
    )
    assert [o.status for o in result.outcomes] == ["quarantined", "quarantined"]


@pytest.mark.usefixtures("regular_user")
def test_execute_with_snapper_creates_one_snapshot(
    home: Path, store: QuarantineStore, batch: list[Path]
) -> None:
    provider = _provider(home, True)
    result = execute(
        _cands(home, batch),
        home,
        store=store,
        snapshots=provider,
        selected=[batch[2]],
        dry_run=False,
    )
    assert [o.status for o in result.outcomes] == ["quarantined"] * 3
    assert result.snapshot == {"config": "home", "number": 7}
    runner = provider._runner
    assert isinstance(runner, FakeRunner)
    assert sum(1 for c in runner.calls if "create" in c) == 1
    by_name = {Path(m.original_path).name: m.snapshot for m in store.entries()}
    assert by_name == {"high": None, "medium": result.snapshot, "low": result.snapshot}


@pytest.mark.usefixtures("regular_user")
def test_execute_snapshot_failure_blocks_only_protected_items(
    home: Path, store: QuarantineStore, batch: list[Path]
) -> None:
    result = execute(
        _cands(home, batch)[:2],
        home,
        store=store,
        snapshots=_provider(home, True, create_ok=False),
        dry_run=False,
    )
    assert [o.status for o in result.outcomes] == ["quarantined", "blocked"]
    assert result.outcomes[1].blocks[-1].code == "snapshot-failed"


@pytest.mark.usefixtures("regular_user")
def test_execute_skips_items_in_use(home: Path, store: QuarantineStore, batch: list[Path]) -> None:
    with running(["sleep", "30"], cwd=batch[0]):
        result = execute(
            _cands(home, batch)[:1],
            home,
            store=store,
            snapshots=_provider(home, False),
            dry_run=False,
        )
    assert result.outcomes[0].status == "blocked"
    assert result.outcomes[0].blocks[0].code == "in-use"
    assert batch[0].exists()


def test_execute_refuses_root(
    home: Path, store: QuarantineStore, batch: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(RootRefusedError):
        execute(_cands(home, batch), home, store=store, snapshots=_provider(home, False))


def test_lock_is_exclusive(home: Path) -> None:
    first, second = QuarantineStore(home), QuarantineStore(home)
    with first.lock(), pytest.raises(QuarantineError, match="in progress"), second.lock():
        pass
    with second.lock():  # released after the first block
        pass
