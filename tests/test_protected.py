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
        HOME / ".config",  # a própria raiz
        HOME / ".config/hexchat/servlist.conf",  # segundo nível
        HOME / ".ssh",  # fora das raízes
        HOME / "Documentos",
        Path("/etc/hexchat"),  # fora da home
    ],
)
def test_anything_outside_first_level_is_protected(path: Path) -> None:
    assert is_protected(path, HOME)


@pytest.mark.parametrize(
    "relative",
    [
        ".local/share/fsu",  # quarentena do próprio FSU
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
        ".config/GTK-3.0",  # sem diferenciar maiúsculas
    ],
)
def test_protected_names(relative: str) -> None:
    assert is_protected(HOME / relative, HOME)


@pytest.mark.parametrize(
    "relative", [".config/hexchat", ".local/share/kwebkitpart", ".var/app/com.spotify.Client"]
)
def test_regular_items_are_not_protected(relative: str) -> None:
    assert not is_protected(HOME / relative, HOME)
