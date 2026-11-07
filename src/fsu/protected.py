"""Lista de proteção: itens do $HOME que o FSU nunca considera resíduo.

A regra é conservadora: na dúvida, proteger. Um falso "protegido" só
deixa de limpar algo; um falso "resíduo" pode quebrar a sessão do usuário.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from fsu.models import Category
from fsu.paths import SCAN_ROOTS

# Padrões (fnmatch, sem diferenciar maiúsculas) aplicados ao nome do item de
# primeiro nível dentro de cada raiz varrida.
# fmt: off
PROTECTED_PATTERNS: dict[Category, tuple[str, ...]] = {
    Category.CONFIG: (
        "fsu",
        # Sessão, desktop e serviços do usuário
        "dconf", "autostart", "systemd", "environment.d", "user-dirs.*",
        "mimeapps.list", "*-mimeapps.list", "monitors.xml", "menus",
        "gtk-*", "gnome-*", "gtkrc*", "xsettingsd", "ibus", "fcitx*",
        "fontconfig", "pulse", "pipewire", "wireplumber", "session",
        # KDE Plasma
        "kdeglobals", "kdedefaults", "kwin*", "plasma*", "kscreen*", "ksmserverrc",
        "kded*rc", "kglobalshortcutsrc", "khotkeysrc", "kcminputrc", "kxkbrc",
        "kactivitymanagerd*", "baloo*", "akonadi*", "kwalletrc", "kwalletmanager*",
        "powermanagementprofilesrc", "powerdevilrc", "breezerc",
        # Segurança e identidade
        "keyrings", "gnupg", "ssh", "pki", "Yubico",
        # Ferramentas do sistema que não têm pacote homônimo
        "flatpak", "containers", "environment",
    ),
    Category.DATA: (
        "fsu",
        "flatpak", "containers", "keyrings", "kwalletd", "Trash",
        "applications", "desktop-directories", "icons", "fonts", "mime",
        "themes", "sounds", "backgrounds", "wallpapers", "color-schemes",
        "recently-used.xbel", "user-places.xbel*", "gvfs-metadata",
        "gnome-*", "systemd", "dbus-1", "xorg", "sddm",
        "plasma*", "kactivitymanagerd", "baloo", "akonadi*", "kxmlgui5", "kwin*",
        "tracker*", "localsearch*", "evolution", "pki", "nautilus",
    ),
    Category.CACHE: (
        "fsu",
        "fontconfig", "thumbnails", "mesa_shader_cache*", "nvidia", "radv_builtin_shaders*",
        "flatpak", "dconf", "ibus", "gnome-*", "plasma*", "ksycoca*", "kwin*",
        "tracker*", "localsearch*", "gstreamer-*", "systemd",
    ),
    Category.FLATPAK: (),
}
# fmt: on


def category_of(path: Path, home: Path) -> Category | None:
    """Devolve a categoria de um item de primeiro nível, ou ``None``.

    Só retorna uma categoria quando ``path`` é *exatamente* um filho direto
    de uma das raízes de varredura.
    """
    try:
        relative = path.relative_to(home)
    except ValueError:
        return None
    for category, root in SCAN_ROOTS.items():
        if relative.parent == root and relative.name not in ("", ".", ".."):
            return category
    return None


def is_protected(path: Path, home: Path) -> bool:
    """Indica se ``path`` deve ser ignorado pelo FSU.

    Qualquer caminho que não seja um filho direto de uma raiz de varredura é
    considerado protegido (falha segura).
    """
    category = category_of(path, home)
    if category is None:
        return True
    name = path.name.lower()
    return any(
        fnmatch.fnmatchcase(name, pattern.lower()) for pattern in PROTECTED_PATTERNS[category]
    )
