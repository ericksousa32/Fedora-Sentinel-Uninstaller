"""Integração com um Fedora real (rpm, dnf5). Executado no job "fedora" da CI.

No CI, o workflow instala e remove um pacote (``FSU_EXPECT_REMOVED``) antes
dos testes, garantindo uma transação de remoção real no histórico.
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
    pytest.mark.skipif(shutil.which("dnf5") is None, reason="requer Fedora com dnf5"),
]

EXPECTED = os.environ.get("FSU_EXPECT_REMOVED")


def test_rpm_inventory_real() -> None:
    names = RpmInventory().installed_names()
    assert {"rpm", "dnf5"} <= names


def test_history_real() -> None:
    history = DnfHistory()
    assert history.transaction_ids()
    removed = {r.name for r in history.removed_packages()}
    assert history.warnings == [], history.warnings  # a chamada com intervalo funcionou
    if EXPECTED:
        assert EXPECTED in removed


@pytest.mark.skipif(not EXPECTED, reason="defina FSU_EXPECT_REMOVED")
def test_end_to_end_real(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".config" / str(EXPECTED)).mkdir(parents=True)
    (home / ".config" / "dnf5").mkdir(parents=True)  # instalado: não é candidato
    report = Collector(home=home).collect()
    found = {c.path.name: c for c in report.candidates}
    assert found[str(EXPECTED)].action is Action.QUARANTINE
    assert "dnf5" not in found
