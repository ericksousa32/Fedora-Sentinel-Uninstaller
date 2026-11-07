"""Integration with a real Fedora system (rpm, dnf5). Run by the "fedora" CI job.

In CI the workflow installs and removes a package (``FSU_EXPECT_REMOVED``)
before the tests, guaranteeing a real removal transaction in the history.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from fsu.collector import Collector
from fsu.inventory import DnfHistory, RpmInventory
from fsu.models import Action
from fsu.quarantine import QuarantineStore
from fsu.safeguard import SnapshotProvider, check_in_use

pytestmark = [
    pytest.mark.fedora,
    pytest.mark.skipif(shutil.which("dnf5") is None, reason="requires Fedora with dnf5"),
]

EXPECTED = os.environ.get("FSU_EXPECT_REMOVED")


def test_rpm_inventory_real() -> None:
    names = RpmInventory().installed_names()
    assert {"rpm", "dnf5"} <= names


def test_history_real() -> None:
    history = DnfHistory()
    assert history.transaction_ids()
    removed = {r.name for r in history.removed_packages()}
    assert history.warnings == [], history.warnings  # the range call worked
    if EXPECTED:
        assert EXPECTED in removed


@pytest.mark.skipif(not EXPECTED, reason="set FSU_EXPECT_REMOVED")
def test_end_to_end_real(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".config" / str(EXPECTED)).mkdir(parents=True)
    (home / ".config" / "dnf5").mkdir(parents=True)  # installed: not a candidate
    report = Collector(home=home).collect()
    found = {c.path.name: c for c in report.candidates}
    assert found[str(EXPECTED)].action is Action.QUARANTINE
    assert "dnf5" not in found


def test_quarantine_roundtrip_real(tmp_path: Path) -> None:
    """Real rename-based move and restore on the CI Fedora file system."""
    from fsu.heuristics import action_for
    from fsu.models import Candidate, Category

    home = tmp_path / "home"
    item = home / ".config" / "fsu-integration"
    (item / "sub").mkdir(parents=True)
    (item / "sub" / "file").write_text("data")
    store = QuarantineStore(home)
    cand = Candidate(item, Category.CONFIG, 0.85, action_for(0.85), 4, 0.0, False)
    assert check_in_use(item) == []
    manifest = store.quarantine(cand)
    assert not item.exists()
    assert store.restore(manifest.id) == item
    assert (item / "sub" / "file").read_text() == "data"


def test_snapper_detection_does_not_crash(tmp_path: Path) -> None:
    status = SnapshotProvider(tmp_path).detect()
    # No Snapper config covers a CI temp dir: snapshots must be reported unavailable.
    assert not status.available
    assert status.reason
