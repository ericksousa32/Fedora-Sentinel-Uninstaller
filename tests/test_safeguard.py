from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from conftest import FakeRunner
from fsu import safeguard
from fsu.safeguard import (
    Protection,
    RootRefusedError,
    SnapshotProvider,
    check_in_use,
    check_mounts,
    check_ownership,
    check_same_filesystem,
    decide_protection,
    ensure_not_root,
    mount_point_of,
    preflight,
    revalidate,
)
from fsu.system import CommandError
from helpers import candidate, make, renamed_sleep, running

IS_ROOT = os.geteuid() == 0


# -- ensure_not_root ----------------------------------------------------------------


def test_root_is_refused() -> None:
    with pytest.raises(RootRefusedError):
        ensure_not_root(euid=0, environ={})


@pytest.mark.parametrize("variable", ["SUDO_UID", "PKEXEC_UID"])
def test_privilege_escalation_is_refused(variable: str) -> None:
    with pytest.raises(RootRefusedError, match=variable):
        ensure_not_root(euid=1000, environ={variable: "1000"})


def test_regular_user_is_accepted() -> None:
    ensure_not_root(euid=1000, environ={"HOME": "/home/u"})


# -- revalidate ---------------------------------------------------------------------


def test_revalidate_ok(home: Path) -> None:
    make(home / ".config/hexchat/a")
    assert revalidate(candidate(home / ".config/hexchat", home, 0.85), home) == []


def test_revalidate_missing(home: Path) -> None:
    blocks = revalidate(candidate(home / ".config/gone", home, 0.85), home)
    assert [b.code for b in blocks] == ["missing"]


def test_revalidate_protected(home: Path) -> None:
    make(home / ".config/dconf/user")
    blocks = revalidate(candidate(home / ".config/dconf", home, 0.85), home)
    assert [b.code for b in blocks] == ["protected"]


def test_revalidate_symlinked_root(home: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    make(elsewhere / "hexchat/a")
    (home / ".config").symlink_to(elsewhere)
    blocks = revalidate(candidate(home / ".config/hexchat", home, 0.85), home)
    assert [b.code for b in blocks] == ["symlinked-root"]


def test_revalidate_type_changed(home: Path) -> None:
    make(home / ".config/hexchat/a")
    item = candidate(home / ".config/hexchat", home, 0.85)
    (home / ".config/hexchat/a").unlink()
    (home / ".config/hexchat").rmdir()
    (home / ".config/hexchat").symlink_to(home)
    assert [b.code for b in revalidate(item, home)] == ["changed"]


def test_revalidate_home_behind_a_symlink(tmp_path: Path) -> None:
    """Fedora Atomic: /home -> /var/home. Resolving must still match."""
    real = tmp_path / "var/home/u"
    make(real / ".config/hexchat/a")
    (tmp_path / "home").symlink_to(tmp_path / "var/home")
    home = tmp_path / "home/u"
    assert revalidate(candidate(home / ".config/hexchat", home, 0.85), home) == []


# -- ownership / mounts / file systems ----------------------------------------------


def test_ownership_ok(home: Path) -> None:
    make(home / ".config/hexchat/sub/a")
    assert check_ownership(home / ".config/hexchat") == []


@pytest.mark.skipif(not IS_ROOT, reason="needs root to create files owned by someone else")
def test_ownership_foreign_file(home: Path) -> None:
    make(home / ".config/hexchat/sub/a")
    os.chown(home / ".config/hexchat/sub/a", 65534, 65534)
    blocks = check_ownership(home / ".config/hexchat", uid=0)
    assert [b.code for b in blocks] == ["foreign-owner"]
    assert "uid 65534" in blocks[0].detail


@pytest.mark.skipif(IS_ROOT, reason="root can read everything")
def test_ownership_unreadable_directory(home: Path) -> None:
    locked = home / ".config/hexchat/locked"
    make(locked / "a")
    locked.chmod(0)
    try:
        blocks = check_ownership(home / ".config/hexchat")
    finally:
        locked.chmod(0o700)
    assert [b.code for b in blocks] == ["unreadable"]


def test_mount_point_inside_item(home: Path, tmp_path: Path) -> None:
    item = home / ".config/my app"
    make(item / "data/a")
    mountinfo = tmp_path / "mountinfo"
    escaped = str((item / "data").resolve()).replace(" ", "\\040")
    mountinfo.write_text(f"36 35 98:0 / {escaped} rw,noatime master:1 - ext3 /dev/x rw\n")
    blocks = check_mounts(item, mountinfo)
    assert [b.code for b in blocks] == ["mount-point"]
    assert "my app/data" in blocks[0].detail


def test_no_mount_point(home: Path, tmp_path: Path) -> None:
    make(home / ".config/hexchat/a")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("22 1 0:21 / /proc rw - proc proc rw\n")
    assert check_mounts(home / ".config/hexchat", mountinfo) == []


def test_same_filesystem(home: Path) -> None:
    make(home / ".config/hexchat/a")
    assert check_same_filesystem(home / ".config/hexchat", home / ".local/share/fsu/q") == []


def test_other_filesystem_is_blocked(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make(home / ".config/hexchat/a")
    monkeypatch.setattr(safeguard, "_nearest_existing", lambda _path: Path("/proc"))
    blocks = check_same_filesystem(home / ".config/hexchat", home / "q")
    assert [b.code for b in blocks] == ["cross-filesystem"]


# -- processes ----------------------------------------------------------------------


def test_cwd_inside_item_is_in_use(home: Path) -> None:
    item = home / ".config/hexchat"
    make(item / "a")
    with running(["sleep", "30"], cwd=item):
        blocks = check_in_use(item)
    assert [b.code for b in blocks] == ["in-use"]
    assert "sleep" in blocks[0].detail


def test_open_file_inside_item_is_in_use(home: Path) -> None:
    item = home / ".local/share/hexchat"
    make(item / "db.sqlite")
    script = f"import time; f = open({str(item / 'db.sqlite')!r}); time.sleep(30)"
    with running([sys.executable, "-c", script]):
        blocks = check_in_use(item)
    assert [b.code for b in blocks] == ["in-use"]


def test_running_application_blocks(home: Path, tmp_path: Path) -> None:
    item = home / ".config/hexchat"
    make(item / "a")
    fake_app = renamed_sleep(tmp_path, "hexchat")
    with running([str(fake_app), "30"]):
        blocks = check_in_use(item, matched_name="hexchat")
    assert [b.code for b in blocks] == ["app-running"]


def test_unused_item(home: Path) -> None:
    make(home / ".config/hexchat/a")
    assert check_in_use(home / ".config/hexchat", matched_name="hexchat") == []


# -- policy -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "available", "selected", "allow", "protection", "codes"),
    [
        (0.85, False, False, False, Protection.NONE, []),
        (0.57, True, False, False, Protection.SNAPSHOT, []),
        (0.57, False, False, False, Protection.SNAPSHOT, ["snapshot-required"]),
        (0.57, False, False, True, Protection.NONE, []),
        (0.20, True, False, False, Protection.SNAPSHOT, ["list-only"]),
        (0.20, True, True, False, Protection.SNAPSHOT, []),
        (0.20, False, True, False, Protection.SNAPSHOT, ["snapshot-required"]),
        (0.20, False, True, True, Protection.SNAPSHOT, ["snapshot-required"]),  # no bypass
    ],
)
def test_decide_protection(
    home: Path,
    score: float,
    available: bool,
    selected: bool,
    allow: bool,
    protection: Protection,
    codes: list[str],
) -> None:
    got, blocks = decide_protection(
        candidate(home / ".config/x", home, score),
        snapshot_available=available,
        explicitly_selected=selected,
        allow_without_snapshot=allow,
    )
    assert got is protection
    assert [b.code for b in blocks] == codes


def test_preflight_aggregates(home: Path) -> None:
    item = home / ".config/hexchat"
    make(item / "a")
    with running(["sleep", "30"], cwd=item):
        blocks = preflight(candidate(item, home, 0.85), home, home / ".local/share/fsu/q")
    assert [b.code for b in blocks] == ["in-use"]


# -- Snapper ------------------------------------------------------------------------


def test_mount_point_of(tmp_path: Path) -> None:
    point = mount_point_of(tmp_path)
    assert point == tmp_path.resolve() or point in tmp_path.resolve().parents
    assert mount_point_of(Path("/")) == Path("/")


def _snapper(home: Path, configs: object, list_ok: bool = True) -> FakeRunner:
    responses: dict[tuple[str, ...], str | BaseException] = {
        ("snapper", "--jsonout", "list-configs"): json.dumps(configs),
        ("snapper", "-c", "home", "create"): "42\n",
    }
    responses[("snapper", "-c", "home", "list")] = (
        "ok" if list_ok else CommandError(["snapper"], "No permissions.")
    )
    return FakeRunner(responses)


def test_snapper_not_installed(home: Path) -> None:
    status = SnapshotProvider(home, FakeRunner({})).detect()
    assert not status.available
    assert "not installed" in status.reason


def test_snapper_without_home_config(home: Path) -> None:
    runner = _snapper(home, {"configs": [{"config": "data", "subvolume": "/srv/not-home"}]})
    status = SnapshotProvider(home, runner).detect()
    assert not status.available
    assert "create-config" in status.reason


def test_snapper_available_and_create(home: Path) -> None:
    configs = {"configs": [{"config": "home", "subvolume": str(mount_point_of(home))}]}
    runner = _snapper(home, configs)
    provider = SnapshotProvider(home, runner)
    assert provider.detect().available
    assert provider.create("fsu: test") == ("home", 42)
    create = next(c for c in runner.calls if "create" in c)
    assert "--print-number" in create
    assert create[create.index("--description") :][:2] == ("--description", "fsu: test")


def test_snapper_config_without_permission(home: Path) -> None:
    configs = [{"config": "home", "subvolume": str(mount_point_of(home))}]
    status = SnapshotProvider(home, _snapper(home, configs, list_ok=False)).detect()
    assert not status.available
    assert "ALLOW_USERS" in status.reason


def test_snapper_plain_table_fallback(home: Path) -> None:
    table = f"Config | Subvolume\n-------+----------\nhome   | {mount_point_of(home)}\n"
    runner = FakeRunner(
        {("snapper", "--jsonout", "list-configs"): table, ("snapper", "-c", "home", "list"): ""}
    )
    assert SnapshotProvider(home, runner).detect().config == "home"


def test_create_without_snapper_raises(home: Path) -> None:
    with pytest.raises(CommandError):
        SnapshotProvider(home, FakeRunner({})).create("x")
