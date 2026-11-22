"""FSU's inventory history and the objective facts shown for list-only items."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FakeRunner, default_responses
from fsu import inventory
from fsu.collector import Collector
from fsu.history import InventoryHistory, Observation, history_path, observe
from fsu.inventory import running_process_names as real_running_process_names
from fsu.models import Action, Candidate
from helpers import make


def _write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _obs(path: str, *names: str, kind: str = "appimage") -> dict[str, Observation]:
    return {f"{kind}:{path}": Observation(kind, path, frozenset(names))}


# -- observe ----------------------------------------------------------------------------


def test_observe(tmp_path: Path) -> None:
    appimages = tmp_path / "AppImages"
    _write(appimages / "nuvio-1.2.0.AppImage")
    _write(appimages / "notes.txt")
    apps = tmp_path / "applications"
    tool = _write(tmp_path / "opt/tool/tool")
    _write(apps / "tool.desktop", f"[Desktop Entry]\nName=My Tool\nExec={tool} %U\n")
    _write(apps / "gone.desktop", "[Desktop Entry]\nExec=/nonexistent/gone\n")
    _write(apps / "system.desktop", "[Desktop Entry]\nExec=/usr/bin/true\n")
    found = observe(
        [appimages, tmp_path / "missing"],
        apps,
        {"konsave": "/usr/local/bin/konsave", "true": "/usr/bin/true"},
    )
    by_kind = {(o.kind, Path(o.path).name): o.names for o in found.values()}
    assert by_kind[("appimage", "nuvio-1.2.0.AppImage")] == {"nuvio"}
    assert "My Tool" in by_kind[("launcher-target", "tool")]
    assert by_kind[("program", "konsave")] == {"konsave"}
    assert ("program", "true") not in by_kind  # system programs belong to RPM
    assert not any(name == "gone" for _kind, name in by_kind)


# -- update ------------------------------------------------------------------------------


def test_first_run_reports_nothing_and_records(tmp_path: Path) -> None:
    history = InventoryHistory(tmp_path / "state/inventory.json")
    result = history.update(_obs(str(tmp_path / "a.AppImage"), "a"), now=100)
    assert result.gone == []
    assert result.recorded
    saved = json.loads((tmp_path / "state/inventory.json").read_text())
    assert saved["version"] == 1


def test_seen_then_gone(tmp_path: Path) -> None:
    app = _write(tmp_path / "nuvio.AppImage")
    history = InventoryHistory(tmp_path / "inventory.json")
    history.update(_obs(str(app), "nuvio"), now=100)
    app.unlink()
    gone = history.update({}, now=200).gone
    assert [(g.path, set(g.names), g.last_seen) for g in gone] == [(str(app), {"nuvio"}, 100)]
    # Evidence persists on later scans.
    assert history.update({}, now=300).gone[0].path == str(app)


def test_updated_appimage_is_not_gone(tmp_path: Path) -> None:
    """Gear Lever-style update: new file name, same application name."""
    old = _write(tmp_path / "nuvio-1.0.AppImage")
    history = InventoryHistory(tmp_path / "inventory.json")
    history.update(_obs(str(old), "nuvio"), now=100)
    old.unlink()
    new = _write(tmp_path / "nuvio-2.0.AppImage")
    assert history.update(_obs(str(new), "nuvio"), now=200).gone == []


def test_removable_media_is_never_gone(tmp_path: Path) -> None:
    history = InventoryHistory(tmp_path / "inventory.json")
    history.update(_obs("/run/media/u/usb/tool.AppImage", "tool"), now=100)
    assert history.update({}, now=200).gone == []


def test_no_record_does_not_write(tmp_path: Path) -> None:
    history = InventoryHistory(tmp_path / "inventory.json")
    result = history.update(_obs(str(tmp_path / "a.AppImage"), "a"), record=False)
    assert not result.recorded
    assert not (tmp_path / "inventory.json").exists()


@pytest.mark.parametrize("content", ["{not json", '{"version": 99}', "[]"])
def test_unreadable_history_starts_over(tmp_path: Path, content: str) -> None:
    path = _write(tmp_path / "inventory.json", content)
    result = InventoryHistory(path).update({}, now=1)
    assert result.warnings
    assert json.loads(path.read_text())["version"] == 1


# -- collector integration ---------------------------------------------------------------


def _collect(home: Path, record: bool = True) -> dict[str, Candidate]:
    report = Collector(
        home, FakeRunner(default_responses()), [], executable_dirs=[], record_history=record
    ).collect()
    return {str(c.path.relative_to(home)): c for c in report.candidates}


def test_appimage_removed_between_scans(home: Path) -> None:
    app = _write(home / "AppImages/nuvio-1.0.AppImage")
    make(home / ".cache/nuvio/cache.bin")
    make(home / ".config/nuvio/settings")
    make(home / ".nuvio/data")  # hidden folder in $HOME: strict area
    first = _collect(home)
    assert ".cache/nuvio" not in first  # the AppImage exists: installed
    assert history_path(home).exists()

    app.unlink()
    second = _collect(home)
    for item in (".cache/nuvio", ".config/nuvio", ".nuvio"):
        assert second[item].score == 0.75, item
        assert second[item].action is Action.QUARANTINE
        assert "was present on" in second[item].evidence[0].detail


def test_history_is_read_even_without_recording(home: Path) -> None:
    app = _write(home / "AppImages/nuvio.AppImage")
    make(home / ".config/nuvio/settings")
    _collect(home)  # records
    app.unlink()
    before = history_path(home).read_text()
    found = _collect(home, record=False)
    assert found[".config/nuvio"].score == 0.75
    assert history_path(home).read_text() == before


def test_without_history_no_evidence(home: Path) -> None:
    make(home / ".config/nuvio/settings")
    found = _collect(home, record=False)
    assert found[".config/nuvio"].score == 0.20
    assert not history_path(home).exists()


# -- facts for list-only items ---------------------------------------------------------


def test_list_only_items_get_facts(home: Path) -> None:
    make(home / ".cache/mystery/blob")
    facts = _collect(home)[".cache/mystery"].evidence[-1]
    assert facts.source == "facts"
    assert "last write 0 day(s) ago" in facts.detail
    assert "no program, launcher or running process with exactly this name ('mystery')" in (
        facts.detail
    )


def test_running_process_is_reported(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inventory, "running_process_names", lambda *a, **k: {"mystery"})
    make(home / ".cache/mystery/blob")
    item = _collect(home)[".cache/mystery"]
    assert item.score == 0.20  # facts never change the score
    assert "process 'mystery' exists" in item.evidence[-1].detail


def test_facts_only_for_list_only_items(home: Path) -> None:
    make(home / ".config/hexchat/x")  # removed package: 0.85
    assert all(e.source != "facts" for e in _collect(home)[".config/hexchat"].evidence)


def test_real_running_process_names() -> None:
    names = real_running_process_names()
    assert any("python" in n or "pytest" in n for n in names)
