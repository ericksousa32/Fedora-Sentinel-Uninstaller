from __future__ import annotations

from pathlib import Path

import pytest

from fsu.models import Category
from fsu.protected import category_of, is_protected

HOME = Path("/home/erick")


@pytest.mark.parametrize(
    ("relative", "category"),
    [
        (".config/hexchat", Category.CONFIG),
        (".local/share/hexchat", Category.DATA),
        (".cache/hexchat", Category.CACHE),
        (".var/app/org.example.App", Category.FLATPAK),
    ],
)
def test_category_of_first_level(relative: str, category: Category) -> None:
    assert category_of(HOME / relative, HOME) is category


@pytest.mark.parametrize(
    "path",
    [
        HOME / ".config",  # the root itself
        HOME / ".config/hexchat/servlist.conf",  # second level
        HOME / ".ssh",  # outside the roots
        HOME / "Documents",
        Path("/etc/hexchat"),  # outside home
    ],
)
def test_anything_outside_first_level_is_protected(path: Path) -> None:
    assert is_protected(path, HOME)


@pytest.mark.parametrize(
    "relative",
    [
        ".local/share/fsu",  # FSU's own quarantine
        ".config/fsu",
        ".cache/fsu",
        ".config/dconf",
        ".config/autostart",
        ".config/kdeglobals",
        ".config/plasma-org.kde.plasma.desktop-appletsrc",
        ".local/share/keyrings",
        ".local/share/flatpak",
        ".local/share/containers",
        ".local/share/Trash",
        ".local/share/man",
        ".config/GTK-3.0",  # case-insensitive
        # KDE/Qt session items reported in the first real scan
        ".config/krunnerrc",
        ".config/QtProject.conf",
        ".config/Trolltech.conf",
        ".config/kde.org",
        ".config/org.kde.kded.smart",
        ".cache/kcrash-metadata",
        ".cache/qtshadercache-x86_64-little_endian-lp64",
        ".cache/event-sound-cache.tdb.962730d5f2bf4d4b959ec9825b133d8f.x86_64-redhat-linux-gnu",
        ".local/share/knewstuff3",
        ".local/share/kpeoplevcard",
    ],
)
def test_protected_names(relative: str) -> None:
    assert is_protected(HOME / relative, HOME)


@pytest.mark.parametrize(
    "relative",
    [
        ".config/hexchat",
        ".local/share/kwebkitpart",
        ".var/app/com.spotify.Client",
        ".cache/mozilla",  # owned by Firefox: judged, not blindly protected
        ".config/kolourpaintrc",
    ],
)
def test_regular_items_are_not_protected(relative: str) -> None:
    assert not is_protected(HOME / relative, HOME)
