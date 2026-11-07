from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import FakeRunner, default_responses
from fsu.collector import Collector, main, measure
from fsu.models import Action, Candidate, Category
from fsu.system import CommandError


def _make(path: Path, size: int = 10) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


@pytest.fixture
def populated_home(home: Path) -> Path:
    # Resíduos de pacotes removidos (fixture do histórico)
    _make(home / ".config/hexchat/servlist.conf", 100)
    _make(home / ".local/share/kwebkitpart/data.db", 200)
    _make(home / ".config/aspnetcore/settings.json", 5)
    # Pacote removido mas reinstalado depois (gimp está no rpm -qa)
    _make(home / ".config/GIMP/gimprc")
    # Instalado agora
    _make(home / ".config/kdenlive/kdenliverc")
    # Correspondência via arquivo .desktop instalado
    _make(home / ".config/Code/settings.json")
    # Desconhecido
    _make(home / ".cache/misterio/blob")
    # Protegidos
    _make(home / ".config/dconf/user")
    _make(home / ".local/share/fsu/quarantine/item/arquivo")
    # Flatpak: órfão e instalado
    _make(home / ".var/app/org.gimp.GIMP/config/x")
    _make(home / ".var/app/com.spotify.Client/cache/y")
    # Fora do escopo
    _make(home / ".ssh/id_ed25519")
    return home


@pytest.fixture
def desktop_dir(tmp_path: Path) -> Path:
    apps = tmp_path / "usr-share-applications"
    apps.mkdir()
    (apps / "code.desktop").write_text(
        "[Desktop Entry]\nExec=/usr/share/code/code %F\nStartupWMClass=Code\n", encoding="utf-8"
    )
    return apps


def _collect(home: Path, runner: FakeRunner, desktop_dir: Path) -> dict[str, Candidate]:
    report = Collector(home=home, runner=runner, desktop_dirs=[desktop_dir]).collect()
    return {str(c.path.relative_to(home)): c for c in report.candidates}


def _snapshot(root: Path) -> dict[str, tuple[int, int, float, bytes | str]]:
    """Retrato completo da árvore: modo, tamanho, mtime e conteúdo/destino."""
    state: dict[str, tuple[int, int, float, bytes | str]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            info = path.lstat()
            if path.is_symlink():
                content: bytes | str = str(path.readlink())
            elif path.is_file():
                content = path.read_bytes()
            else:
                content = b""
            state[str(path)] = (info.st_mode, info.st_size, info.st_mtime, content)
    return state


def test_scoring(populated_home: Path, fake_runner: FakeRunner, desktop_dir: Path) -> None:
    found = _collect(populated_home, fake_runner, desktop_dir)

    flatpak = found[".var/app/org.gimp.GIMP"]
    assert (flatpak.score, flatpak.action) == (0.95, Action.QUARANTINE)
    assert flatpak.category is Category.FLATPAK

    hexchat = found[".config/hexchat"]
    assert (hexchat.score, hexchat.action) == (0.85, Action.QUARANTINE)
    assert hexchat.matched_name == "hexchat"
    assert hexchat.size_bytes == 100

    assert found[".local/share/kwebkitpart"].score == 0.85

    aspnet = found[".config/aspnetcore"]
    assert aspnet.action is Action.QUARANTINE_WITH_SNAPSHOT
    assert aspnet.matched_name == "aspnetcore-runtime-8.0"

    mystery = found[".cache/misterio"]
    assert (mystery.score, mystery.action) == (0.20, Action.LIST_ONLY)

    # Nunca candidatos:
    for relative in (
        ".config/GIMP",  # gimp reinstalado
        ".config/kdenlive",  # instalado
        ".config/Code",  # via .desktop
        ".var/app/com.spotify.Client",  # Flatpak instalado
        ".config/dconf",  # protegido
        ".local/share/fsu",  # quarentena do FSU
        ".ssh",  # fora do escopo
    ):
        assert relative not in found, relative

    # Ordenado por score decrescente
    scores = [c.score for c in found.values()]
    assert scores == sorted(scores, reverse=True)


def test_report_counters(populated_home: Path, fake_runner: FakeRunner, desktop_dir: Path) -> None:
    report = Collector(populated_home, fake_runner, [desktop_dir]).collect()
    protected = {str(p.relative_to(populated_home)) for p in report.protected_skipped}
    assert protected == {".config/dconf", ".local/share/fsu"}
    assert report.installed_skipped == 4  # GIMP, kdenlive, Code, Spotify
    assert report.warnings == ()


def test_collector_is_read_only(
    populated_home: Path, fake_runner: FakeRunner, desktop_dir: Path
) -> None:
    (populated_home / ".config/link-externo").symlink_to(desktop_dir)
    before = _snapshot(populated_home.parent)
    before_outside = _snapshot(desktop_dir)
    Collector(populated_home, fake_runner, [desktop_dir]).collect()
    assert _snapshot(populated_home.parent) == before
    assert _snapshot(desktop_dir) == before_outside
    # Nenhum comando externo que altere o sistema foi chamado.
    allowed = {("rpm",), ("dnf5", "history"), ("flatpak", "list")}
    for call in fake_runner.calls:
        assert any(call[: len(prefix)] == prefix for prefix in allowed), call


def test_symlinks_are_never_followed(
    home: Path, fake_runner: FakeRunner, desktop_dir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "fora-da-home"
    _make(outside / "grande.bin", 10_000)
    (home / ".config").mkdir(parents=True)
    (home / ".config/hexchat").symlink_to(outside)
    (home / ".config/quebrado").symlink_to(tmp_path / "nao-existe")

    found = _collect(home, fake_runner, desktop_dir)
    link = found[".config/hexchat"]
    assert link.is_symlink
    assert link.size_bytes == 0  # não somou o destino
    assert any("symlink" in e.detail for e in link.evidence)
    assert found[".config/quebrado"].is_symlink
    assert (outside / "grande.bin").stat().st_size == 10_000


def test_symlinked_root_is_skipped(
    home: Path, fake_runner: FakeRunner, desktop_dir: Path, tmp_path: Path
) -> None:
    real = tmp_path / "dotfiles-config"
    _make(real / "hexchat/servlist.conf")
    (home / ".config").symlink_to(real)
    report = Collector(home, fake_runner, [desktop_dir]).collect()
    assert report.candidates == ()
    assert any("symlink" in w for w in report.warnings)


def test_without_flatpak_var_app_is_ignored(populated_home: Path, desktop_dir: Path) -> None:
    responses = default_responses()
    del responses[("flatpak", "list")]
    report = Collector(populated_home, FakeRunner(responses), [desktop_dir]).collect()
    assert not any(c.category is Category.FLATPAK for c in report.candidates)
    assert any("Flatpak não encontrado" in w for w in report.warnings)
    assert any(c.path.name == "hexchat" for c in report.candidates)


def test_without_rpm_only_flatpak_is_evaluated(populated_home: Path, desktop_dir: Path) -> None:
    responses = default_responses()
    responses[("rpm", "-qa")] = CommandError(["rpm"], "rpmdb corrompido")
    report = Collector(populated_home, FakeRunner(responses), [desktop_dir]).collect()
    assert {c.category for c in report.candidates} == {Category.FLATPAK}
    assert any("Inventário RPM indisponível" in w for w in report.warnings)


def test_without_history_nothing_scores_high(populated_home: Path, desktop_dir: Path) -> None:
    responses = default_responses()
    responses[("dnf5", "history", "list")] = CommandError(["dnf5"], "falhou")
    report = Collector(populated_home, FakeRunner(responses), [desktop_dir]).collect()
    non_flatpak = [c for c in report.candidates if c.category is not Category.FLATPAK]
    assert non_flatpak
    assert all(c.action is Action.LIST_ONLY for c in non_flatpak)


def test_partial_match_to_installed_caps_score(home: Path, desktop_dir: Path) -> None:
    responses = default_responses()
    responses[("rpm", "-qa")] = "bash\nhexchat-plugins\n"
    _make(home / ".config/hexchat/x")
    found = _collect(home, FakeRunner(responses), desktop_dir)
    assert found[".config/hexchat"].score == 0.30
    assert found[".config/hexchat"].action is Action.LIST_ONLY


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignora permissões de leitura")
def test_unreadable_content_is_reported(
    home: Path, fake_runner: FakeRunner, desktop_dir: Path
) -> None:
    secret = home / ".config/hexchat/privado"
    _make(secret / "x")
    secret.chmod(0)
    try:
        report = Collector(home, fake_runner, [desktop_dir]).collect()
    finally:
        secret.chmod(0o700)
    assert any("parcial" in w for w in report.warnings)


def test_measure_file_and_directory(tmp_path: Path) -> None:
    _make(tmp_path / "d/a", 3)
    _make(tmp_path / "d/sub/b", 4)
    (tmp_path / "d/link").symlink_to(tmp_path / "d/a")
    assert measure(tmp_path / "d/a") == (3, True)
    size, complete = measure(tmp_path / "d")
    assert complete
    assert size == 3 + 4 + len(str((tmp_path / "d/link").readlink()))


def test_main_json(
    populated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import fsu.collector as module

    runner = FakeRunner(default_responses())
    original = module.Collector

    def factory(home: Path | None = None) -> Collector:
        return original(home=home, runner=runner, desktop_dirs=[])

    monkeypatch.setattr(module, "Collector", factory)
    assert main(["--json", "--home", str(populated_home)]) == 0
    data = json.loads(capsys.readouterr().out)
    paths = {Path(c["path"]).name for c in data["candidates"]}
    assert {"hexchat", "org.gimp.GIMP"} <= paths
