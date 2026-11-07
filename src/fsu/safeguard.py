"""Safety vault (stage 2): READ-ONLY checks that run before anything is moved.

Every check returns a list of :class:`Block` objects; an empty list means the
item may be quarantined. Nothing in this module changes files - the move
itself lives in :mod:`fsu.quarantine`. The only side effect is
:meth:`SnapshotProvider.create`, which asks Snapper for a snapshot.

Checks:

* :func:`ensure_not_root` - FSU never runs as root (or through sudo/pkexec).
* :func:`revalidate` - the item is still what the scan saw, in the same place.
* :func:`check_ownership` - every file inside belongs to the current user.
* :func:`check_mounts` - no mount point inside the item.
* :func:`check_same_filesystem` - item and quarantine share a file system
  (a plain atomic ``rename`` is the only supported move).
* :func:`check_in_use` - no process of the user holds files in the item and
  the owning application is not running.
* :func:`decide_protection` - the score-band policy (snapshot required, ...).
"""

from __future__ import annotations

import contextlib
import enum
import json
import os
import stat
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from fsu.heuristics import normalize
from fsu.models import Action, Candidate
from fsu.protected import is_protected, matching_root
from fsu.system import CommandError, CommandNotFoundError, Runner, run_command

PROC_ROOT = Path("/proc")
MOUNTINFO = PROC_ROOT / "self" / "mountinfo"
_MAX_REPORTED = 3  # how many offending paths/processes to name in a Block


class RootRefusedError(RuntimeError):
    """FSU was started as root or through a privilege-escalation tool."""


@dataclass(frozen=True)
class Block:
    """A reason why an item must not be moved now."""

    code: str
    detail: str


def ensure_not_root(euid: int | None = None, environ: Mapping[str, str] | None = None) -> None:
    """Refuse to run as root, via sudo or via pkexec.

    Operating on $HOME as root would create root-owned files and could follow
    paths the user does not control.
    """
    euid = os.geteuid() if euid is None else euid
    environ = os.environ if environ is None else environ
    if euid == 0:
        raise RootRefusedError("FSU must run as a regular user, never as root.")
    for variable in ("SUDO_UID", "PKEXEC_UID"):
        if variable in environ:
            raise RootRefusedError(f"FSU must not be started through sudo/pkexec ({variable}).")


def _walk(path: Path) -> Iterator[tuple[Path, os.stat_result | None]]:
    """Yield every entry under ``path`` (included) with its lstat, never following links.

    Entries that cannot be read are yielded with ``None``.
    """
    try:
        root_stat = path.lstat()
    except OSError:
        yield path, None
        return
    yield path, root_stat
    if not stat.S_ISDIR(root_stat.st_mode):
        return
    stack = [path]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except OSError:
            yield directory, None
            continue
        for entry in entries:
            child = Path(entry.path)
            try:
                child_stat = entry.stat(follow_symlinks=False)
            except OSError:
                yield child, None
                continue
            yield child, child_stat
            if stat.S_ISDIR(child_stat.st_mode):
                stack.append(child)


def _summarize(paths: list[str]) -> str:
    shown = ", ".join(paths[:_MAX_REPORTED])
    extra = len(paths) - _MAX_REPORTED
    return f"{shown} (+{extra} more)" if extra > 0 else shown


# -- individual checks ------------------------------------------------------------


def symlink_in_chain(root: Path, home: Path) -> bool:
    """Whether ``root``, or any folder between ``home`` and it, is a symlink.

    ``home`` itself may be reached through a symlink (/home -> /var/home on
    Fedora Atomic); that is fine. For roots outside $HOME (custom XDG
    variables) only the root itself is checked.
    """
    if root == home:
        return False
    if home not in root.parents:
        return root.is_symlink()
    current = home
    for part in root.relative_to(home).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def revalidate(candidate: Candidate, home: Path) -> list[Block]:
    """The item still exists, is still in scope and its location has no symlink."""
    path = candidate.path
    if not os.path.lexists(path):
        return [Block("missing", f"{path} no longer exists")]
    found = matching_root(path, home)
    if found is None or found[0] is not candidate.category:
        return [Block("out-of-scope", f"{path} is not a first-level item of a scan root")]
    if is_protected(path, home):
        return [Block("protected", f"{path} is protected")]
    # No symlinked component between $HOME and the item (e.g. ~/.config
    # pointing elsewhere), and the parent must resolve exactly to its root.
    root = found[1]
    if symlink_in_chain(root, home):
        return [Block("symlinked-root", f"{root} (or a folder above it) is a symlink")]
    try:
        if root == home or home in root.parents:
            expected_parent = home.resolve(strict=True) / root.relative_to(home)
        else:
            expected_parent = root.parent.resolve(strict=True) / root.name
    except OSError as exc:
        return [Block("unreachable", f"cannot resolve {root}: {exc}")]
    try:
        actual_parent = path.parent.resolve(strict=True)
    except OSError as exc:
        return [Block("unreachable", f"cannot resolve {path.parent}: {exc}")]
    if actual_parent != expected_parent:
        return [Block("symlinked-root", f"{path.parent} resolves to {actual_parent}")]
    if path.is_symlink() is not candidate.is_symlink:
        return [Block("changed", f"{path} changed type since the scan")]
    return []


def check_ownership(path: Path, uid: int | None = None) -> list[Block]:
    """Every entry (without following links) belongs to ``uid`` and is readable."""
    uid = os.getuid() if uid is None else uid
    foreign: list[str] = []
    unreadable: list[str] = []
    for entry, entry_stat in _walk(path):
        if entry_stat is None:
            unreadable.append(str(entry))
        elif entry_stat.st_uid != uid:
            foreign.append(f"{entry} (uid {entry_stat.st_uid})")
    blocks = []
    if foreign:
        blocks.append(Block("foreign-owner", "owned by another user: " + _summarize(foreign)))
    if unreadable:
        blocks.append(Block("unreadable", "cannot inspect: " + _summarize(unreadable)))
    return blocks


def _unescape_mountinfo(field: str) -> str:
    """mountinfo escapes space, tab, newline and backslash as octal (\\040...)."""
    out, i = [], 0
    while i < len(field):
        if field[i] == "\\" and i + 4 <= len(field) and field[i + 1 : i + 4].isdigit():
            out.append(chr(int(field[i + 1 : i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def mount_points(mountinfo: Path = MOUNTINFO) -> list[Path]:
    """Mount points of the current mount namespace."""
    try:
        lines = mountinfo.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    points = []
    for line in lines:
        fields = line.split()
        if len(fields) > 4:
            points.append(Path(_unescape_mountinfo(fields[4])))
    return points


def check_mounts(path: Path, mountinfo: Path = MOUNTINFO) -> list[Block]:
    """No mount point (including bind mounts and Btrfs subvolumes) inside the item."""
    try:
        root_stat = path.lstat()
    except OSError as exc:
        return [Block("unreadable", f"cannot inspect {path}: {exc}")]
    if stat.S_ISLNK(root_stat.st_mode):
        return []
    real = path.resolve()
    inside = [str(p) for p in mount_points(mountinfo) if p == real or real in p.parents]
    for entry, entry_stat in _walk(path):
        if entry_stat is not None and entry_stat.st_dev != root_stat.st_dev:
            inside.append(str(entry))
    if inside:
        return [Block("mount-point", "contains a mount point or subvolume: " + _summarize(inside))]
    return []


def _nearest_existing(path: Path) -> Path:
    current = path
    while not os.path.lexists(current) and current != current.parent:
        current = current.parent
    return current


def check_same_filesystem(path: Path, quarantine_root: Path) -> list[Block]:
    """Item and quarantine must share a file system (and Btrfs subvolume)."""
    try:
        item_dev = path.lstat().st_dev
        target_dev = _nearest_existing(quarantine_root).stat().st_dev
    except OSError as exc:
        return [Block("unreadable", f"cannot compare file systems: {exc}")]
    if item_dev != target_dev:
        return [
            Block(
                "cross-filesystem",
                f"{path} is on another file system or subvolume than the quarantine; "
                "moving it would require a copy, which is not supported",
            )
        ]
    return []


def _readlink(path: Path) -> str | None:
    try:
        return str(path.readlink())
    except OSError:
        return None


def _proc_links(pid_dir: Path) -> Iterator[str]:
    links = [_readlink(pid_dir / name) for name in ("cwd", "root", "exe")]
    with contextlib.suppress(OSError):
        links += [_readlink(fd) for fd in (pid_dir / "fd").iterdir()]
    yield from (link for link in links if link)
    try:
        maps = (pid_dir / "maps").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in maps.splitlines():
        parts = line.split(maxsplit=5)
        if len(parts) == 6 and parts[5].startswith("/"):
            yield parts[5]


def _is_inside(link: str, target: str) -> bool:
    link = link.removesuffix(" (deleted)")
    return link == target or link.startswith(target + "/")


def check_in_use(
    path: Path,
    matched_name: str | None = None,
    proc_root: Path = PROC_ROOT,
    uid: int | None = None,
) -> list[Block]:
    """No process of the user uses the item, and the owning app is not running.

    Only the user's own processes can be inspected: Linux hides other users'
    open files from unprivileged users. This is a documented limitation.
    """
    uid = os.getuid() if uid is None else uid
    own_pid = os.getpid()
    targets = {str(path.parent.resolve() / path.name)}
    if not path.is_symlink():
        targets.add(str(path.resolve()))
    app = normalize(matched_name) if matched_name else ""
    users: list[str] = []
    running: list[str] = []
    try:
        pid_dirs = [p for p in proc_root.iterdir() if p.name.isdigit()]
    except OSError as exc:
        return [Block("proc-unavailable", f"cannot inspect processes: {exc}")]
    for pid_dir in pid_dirs:
        if int(pid_dir.name) == own_pid:
            continue
        try:
            if pid_dir.stat().st_uid != uid:
                continue
            comm = (pid_dir / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue  # process exited or is not readable
        label = f"{comm} (pid {pid_dir.name})"
        if any(_is_inside(link, t) for link in _proc_links(pid_dir) for t in targets):
            users.append(label)
            continue
        if app and _is_app_process(pid_dir, comm, app, matched_name or ""):
            running.append(label)
    blocks = []
    if users:
        blocks.append(Block("in-use", "files in use by: " + _summarize(users)))
    if running:
        blocks.append(Block("app-running", "application is running: " + _summarize(running)))
    return blocks


def _is_app_process(pid_dir: Path, comm: str, app: str, matched_name: str) -> bool:
    # comm is truncated to 15 characters by the kernel.
    norm_comm = normalize(comm)
    if norm_comm and (norm_comm == app or (len(comm) >= 15 and app.startswith(norm_comm))):
        return True
    exe = Path(_readlink(pid_dir / "exe") or "").name
    if exe and normalize(exe) == app:
        return True
    if "." in matched_name:  # Flatpak ID: "flatpak run <id>" / bwrap arguments
        try:
            cmdline = (pid_dir / "cmdline").read_bytes().split(b"\0")
        except OSError:
            return False
        return matched_name.encode() in cmdline
    return False


# -- policy -----------------------------------------------------------------------


class Protection(enum.Enum):
    NONE = "none"
    SNAPSHOT = "snapshot"


def decide_protection(
    candidate: Candidate,
    *,
    snapshot_available: bool,
    explicitly_selected: bool = False,
    allow_without_snapshot: bool = False,
) -> tuple[Protection, list[Block]]:
    """Score-band policy agreed for FSU.

    * ``>= 0.70``: quarantine directly.
    * ``0.40-0.70``: snapshot required; without Snapper, only with explicit
      ``allow_without_snapshot``.
    * ``< 0.40``: only if the user selected the item explicitly, and always
      with a snapshot (no bypass).
    """
    if candidate.action is Action.QUARANTINE:
        return Protection.NONE, []
    if candidate.action is Action.QUARANTINE_WITH_SNAPSHOT:
        if snapshot_available:
            return Protection.SNAPSHOT, []
        if allow_without_snapshot:
            return Protection.NONE, []
        return Protection.SNAPSHOT, [
            Block(
                "snapshot-required",
                "medium-confidence item: needs a Snapper snapshot or explicit confirmation",
            )
        ]
    if not explicitly_selected:
        return Protection.SNAPSHOT, [
            Block("list-only", "low-confidence item: only moved if you select it explicitly")
        ]
    if not snapshot_available:
        return Protection.SNAPSHOT, [
            Block("snapshot-required", "low-confidence item: always needs a Snapper snapshot")
        ]
    return Protection.SNAPSHOT, []


def preflight(
    candidate: Candidate,
    home: Path,
    quarantine_root: Path,
    proc_root: Path = PROC_ROOT,
    mountinfo: Path = MOUNTINFO,
) -> list[Block]:
    """All file-system and process checks for one item (policy not included)."""
    blocks = revalidate(candidate, home)
    if blocks:
        return blocks  # the rest makes no sense on a changed/missing item
    path = candidate.path
    blocks += check_ownership(path)
    blocks += check_mounts(path, mountinfo)
    blocks += check_same_filesystem(path, quarantine_root)
    blocks += check_in_use(path, candidate.matched_name, proc_root)
    return blocks


# -- Snapper ----------------------------------------------------------------------


def mount_point_of(path: Path) -> Path:
    """Deepest directory containing ``path`` on the same device (mount or subvolume)."""
    current = path.resolve()
    device = current.stat().st_dev
    while current != current.parent and current.parent.stat().st_dev == device:
        current = current.parent
    return current


@dataclass(frozen=True)
class SnapshotStatus:
    available: bool
    config: str | None
    reason: str


SNAPPER_SETUP_HINT = (
    "to enable snapshots of your home, an administrator can run once: "
    "'sudo snapper -c home create-config /home' and "
    "'sudo snapper -c home set-config ALLOW_USERS=$USER SYNC_ACL=yes' "
    "(consider TIMELINE_CREATE=no if you do not want hourly snapshots)"
)


def _parse_list_configs(output: str) -> dict[str, str]:
    """Parse ``snapper --jsonout list-configs`` (or the plain table) into {config: subvolume}."""
    configs: dict[str, str] = {}
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        data = None
    if data is not None:
        items = data.get("configs", []) if isinstance(data, dict) else data
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and "config" in item and "subvolume" in item:
                    configs[str(item["config"])] = str(item["subvolume"])
        return configs
    for line in output.splitlines():
        for separator in ("│", "|"):
            if separator in line:
                name, _, subvolume = (part.strip() for part in line.partition(separator))
                if name and subvolume and not set(name) <= set("─-┼+"):
                    configs[name] = subvolume
                break
    configs.pop("Config", None)
    return configs


class SnapshotProvider:
    """Finds the Snapper config covering $HOME and creates snapshots as the user.

    FSU never configures Snapper (that needs root). A config is usable only
    if its subvolume is the mount point of $HOME and the user may access it
    (``ALLOW_USERS``/``ALLOW_GROUPS``).
    """

    def __init__(self, home: Path, runner: Runner = run_command) -> None:
        self.home = home
        self._runner = runner
        self._status: SnapshotStatus | None = None

    def detect(self) -> SnapshotStatus:
        if self._status is None:
            self._status = self._detect()
        return self._status

    def _detect(self) -> SnapshotStatus:
        try:
            output = self._runner(["snapper", "--jsonout", "list-configs"])
        except CommandNotFoundError:
            return SnapshotStatus(False, None, "snapper is not installed")
        except CommandError as exc:
            return SnapshotStatus(False, None, f"cannot list Snapper configs ({exc})")
        home_mount = mount_point_of(self.home)
        configs = _parse_list_configs(output)
        matching = sorted(name for name, sub in configs.items() if Path(sub) == home_mount)
        if not matching:
            return SnapshotStatus(
                False, None, f"no Snapper config for {home_mount}; {SNAPPER_SETUP_HINT}"
            )
        config = matching[0]
        try:
            self._runner(["snapper", "-c", config, "list"])
        except CommandError as exc:
            return SnapshotStatus(
                False,
                config,
                f"Snapper config '{config}' is not accessible to this user ({exc}); "
                "it needs ALLOW_USERS or ALLOW_GROUPS",
            )
        return SnapshotStatus(True, config, f"Snapper config '{config}' covers {home_mount}")

    def create(self, description: str) -> tuple[str, int]:
        """Create a single snapshot; returns ``(config, number)``."""
        status = self.detect()
        if not status.available or status.config is None:
            raise CommandError(["snapper"], status.reason)
        output = self._runner(
            [
                "snapper", "-c", status.config, "create",
                "--type", "single",
                "--cleanup-algorithm", "number",
                "--description", description,
                "--print-number",
            ]
        )  # fmt: skip
        try:
            return status.config, int(output.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise CommandError(["snapper", "create"], f"unexpected output: {output!r}") from exc
