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
