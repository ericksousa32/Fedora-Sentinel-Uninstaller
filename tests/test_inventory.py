from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeRunner, default_responses, fixture_text
from fsu.inventory import (
    AppImageInventory,
    DesktopIndex,
    DnfHistory,
    FlatpakInventory,
    RpmInventory,
    appimage_base_name,
    is_noise_package,
    nevra_name,
    parse_desktop_file,
)
from fsu.system import CommandError


@pytest.mark.parametrize(
    ("nevra", "name"),
    [
        ("hexchat-2.16.2-9.fc45.x86_64", "hexchat"),
        ("gimp-2:3.0.4-1.fc45.x86_64", "gimp"),
        ("aspnetcore-runtime-8.0-8.0.21-1.fc45.x86_64", "aspnetcore-runtime-8.0"),
        ("cowsay-3.8.4-1.fc45.noarch", "cowsay"),
    ],
)
def test_nevra_name(nevra: str, name: str) -> None:
    assert nevra_name(nevra) == name


@pytest.mark.parametrize(
    ("name", "noise"),
    [
        ("kf5-kparts", True),
        ("qt5-qtwebkit", True),
        ("python3-requests", True),
        ("gimp-libs", True),
        ("libreoffice-core", False),
        ("librewolf", False),
        ("hexchat", False),
        ("kwebkitpart", False),
    ],
)
def test_noise_packages(name: str, noise: bool) -> None:
    assert is_noise_package(name) is noise


def test_rpm_installed_names(fake_runner: FakeRunner) -> None:
    names = RpmInventory(fake_runner).installed_names()
    assert {"bash", "gimp", "linuxtoys"} <= names


def test_rpm_empty_inventory_is_an_error() -> None:
    with pytest.raises(CommandError, match="empty"):
        RpmInventory(FakeRunner({("rpm",): "\n"})).installed_names()


def test_history_uses_a_single_range_call(fake_runner: FakeRunner) -> None:
    DnfHistory(fake_runner).removed_packages()
    info_calls = [c for c in fake_runner.calls if c[:3] == ("dnf5", "history", "info")]
    # Real IDs from the fixture: 393..405
    assert info_calls == [("dnf5", "history", "info", "393..405", "--json")]


def test_history_removed_packages(fake_runner: FakeRunner) -> None:
    removed = {r.name: r for r in DnfHistory(fake_runner).removed_packages()}
    assert set(removed) == {
        "kwebkitpart",
        "kf5-kparts",
        "qt5-qtwebkit",
        "aspnetcore-runtime-8.0",
        "dotnet-runtime-8.0",
        "hexchat",
        "gimp",
        "kolourpaint",
        "kunifiedpush",
    }
    # "Replaced" (firefox upgrade) is not a removal.
    assert "firefox" not in removed
    # The most recent removal wins: 396, not 393.
    assert removed["hexchat"].transaction_id == 396
    assert removed["kwebkitpart"].reason == "User"


def test_history_falls_back_to_individual_calls() -> None:
    responses = default_responses()
    one = (
        '[{"id": 398, "end_time": 1, "packages": [{"nevra": "a-1-1.x86_64", "action": "Remove"}]}]'
    )
    responses[("dnf5", "history", "info", "393..405")] = CommandError(["dnf5"], "bad spec")
    responses[("dnf5", "history", "info")] = one
    history = DnfHistory(FakeRunner(responses), fallback_limit=3)
    removed = history.removed_packages()
    assert [r.name for r in removed] == ["a"]
    assert history.warnings
    assert "one by one" in history.warnings[0]


def test_history_invalid_json_is_an_error() -> None:
    runner = FakeRunner({("dnf5", "history", "list"): "this is not json"})
    with pytest.raises(CommandError, match="invalid JSON"):
        DnfHistory(runner).removed_packages()


def test_history_empty() -> None:
    runner = FakeRunner({("dnf5", "history", "list"): "[]"})
    assert DnfHistory(runner).removed_packages() == []


def test_flatpak_filters_localized_header_and_duplicates(fake_runner: FakeRunner) -> None:
    ids = FlatpakInventory(fake_runner).installed_app_ids()
    assert "ID de aplicativo" not in ids
    assert "com.hypixel.HytaleLauncher" in ids
    expected = {
        line.strip() for line in fixture_text("flatpak_list.txt").splitlines()[1:] if line.strip()
    }
    assert ids == expected


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_parse_desktop_file_quoted_exec_with_spaces(tmp_path: Path) -> None:
    entry = parse_desktop_file(
        _write(
            tmp_path / "x.desktop",
            "[Desktop Entry]\nName=My App\n# comment=ignored\n"
            'Exec="/home/u/My Apps/my app.AppImage" %F\n',
        )
    )
    assert entry.name == "My App"
    assert entry.exec_target == "/home/u/My Apps/my app.AppImage"
    assert entry.is_appimage


def test_desktop_index(tmp_path: Path) -> None:
    apps = tmp_path / "applications"
    _write(
        apps / "code.desktop",
        "[Desktop Entry]\nName=VS Code\nExec=/usr/share/code/code --unity %F\n"
        "StartupWMClass=Code\n[Desktop Action new]\nExec=ignored --new\n",
    )
    _write(apps / "org.inkscape.Inkscape.desktop", "[Desktop Entry]\nExec=flatpak run x\n")
    _write(apps / "com.obsproject.Studio.desktop", "[Desktop Entry]\nExec=flatpak run y\n")
    _write(apps / "foo.desktop", "[Desktop Entry]\nExec=env A=1 /usr/bin/foo-bin %U\n")
    names = DesktopIndex([apps, tmp_path / "missing"]).known_names()
    assert {"code", "Code", "org.inkscape.Inkscape", "Inkscape", "foo", "foo-bin"} <= names
    # Generic last components must not become "installed" names (LM Studio bug).
    assert "Studio" not in names
    assert not {"run", "ignored", "env", "flatpak"} & names


@pytest.mark.parametrize(
    ("file_name", "base"),
    [
        ("photocraft-0.18.0-linux-x86_64.appimage", "photocraft"),
        ("LM-Studio-0.3.5-x64.AppImage", "LM-Studio"),
        ("comfy_desktop.appimage", "comfy_desktop"),
        ("upscayl.appimage", "upscayl"),
    ],
)
def test_appimage_base_name(file_name: str, base: str) -> None:
    assert appimage_base_name(file_name) == base


def test_appimage_inventory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    appimages = home / "AppImages"
    applications = home / ".local/share/applications"
    _write(appimages / "lmstudio.appimage", "")
    _write(appimages / "comfy_desktop.appimage", "")
    _write(
        applications / "comfy_desktop.desktop",
        f"[Desktop Entry]\nName=Comfy Desktop\nExec={appimages}/comfy_desktop.appimage\n",
    )
    photocraft = _write(
        applications / "ai.storyteller.photocraft.desktop",
        "[Desktop Entry]\nName=PhotoCraft\n"
        f'Exec="{home}/Downloads/photocraft-0.18.0-linux-x86_64.appimage" %F\n'
        "StartupWMClass=ai.storyteller.photocraft\nX-AppImage-Integrate=false\n",
    )
    _write(  # Icon= with an application ID (a path would carry no information)
        applications / "upscayl.desktop",
        f"[Desktop Entry]\nName=Upscayl\nExec={appimages}/upscayl.appimage\n"
        "Icon=org.upscayl.Upscayl\n",
    )
    _write(appimages / "upscayl.appimage", "")
    _write(
        applications / "ovd.desktop",
        f"[Desktop Entry]\nName=Open Video Downloader\nExec={appimages}/ovd.appimage\n"
        f"Icon={appimages}/.icons/open_video_downloader\nStartupWMClass=open-video-downloader\n",
    )
    _write(appimages / "ovd.appimage", "")
    _write(  # on removable media: a missing file proves nothing
        applications / "usb.desktop",
        "[Desktop Entry]\nName=UsbTool\nExec=/run/media/u/usb/usbtool.AppImage\n",
    )
    _write(  # not an AppImage: ignored
        applications / "native.desktop", "[Desktop Entry]\nName=Native\nExec=/usr/bin/nope\n"
    )

    state = AppImageInventory(home, applications, [appimages]).scan()

    assert {"lmstudio", "comfy_desktop", "Comfy Desktop"} <= state.present_names
    assert {"org.upscayl.Upscayl", "open-video-downloader"} <= state.present_names
    assert not any("/" in name for name in state.present_names)
    assert {"photocraft", "PhotoCraft", "ai.storyteller.photocraft"} <= state.missing_names
    assert "UsbTool" not in state.missing_names
    assert "Native" not in state.missing_names
    assert state.stale_launchers == (photocraft,)
