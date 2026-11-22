"""Protection list: $HOME items that FSU never treats as residue.

The rule is conservative: when in doubt, protect. A false "protected" only
means something is not cleaned; a false "residue" can break the user session.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from fsu.models import Category
from fsu.paths import scan_roots

# fnmatch patterns (case-insensitive) applied to the name of the first-level
# item inside each scanned root.
# fmt: off
PROTECTED_PATTERNS: dict[Category, tuple[str, ...]] = {
    Category.CONFIG: (
        "fsu",
        # Session, desktop and user services
        "dconf", "autostart", "systemd", "environment.d", "user-dirs.*",
        "mimeapps.list", "*-mimeapps.list", "monitors.xml", "menus",
        "gtk-*", "gnome-*", "gtkrc*", "xsettingsd", "ibus", "fcitx*",
        "fontconfig", "pulse", "pipewire", "wireplumber", "session",
        "user-tmpfiles.d", "glib-2.0", "QtProject.conf", "Trolltech.conf",
        "Unknown Organization", "xdg-desktop-portal*",
        # KDE Plasma / Frameworks (no package of the same name)
        "kdeglobals", "kdedefaults", "kde.org", "KDE", "kwin*", "plasma*", "kscreen*",
        "ksmserverrc", "kded*rc", "kglobalshortcutsrc", "khotkeysrc", "kcminputrc",
        "kxkbrc", "kactivitymanagerd*", "baloo*", "akonadi*", "kwalletrc",
        "kwalletmanager*", "powermanagementprofilesrc", "powerdevilrc", "breezerc",
        "krunnerrc", "ksplashrc", "kiorc", "ktrashrc", "trashrc", "kservicemenurc",
        "kconf_updaterc", "ktimezonedrc", "kprivacyrc", "kalendaracrc",
        "emaildefaults", "emailidentities", "specialmailcollectionsrc",
        "kcmfonts", "kcmkeyboardrc", "knfsshare", "systemmonitorrc",
        "org.kde.*", "kaccessrc", "bluedevilglobalrc", "kwritemetainfos",
        "signond", "qtvirtualkeyboard",
        # Security and identity
        "keyrings", "gnupg", "ssh", "pki", "Yubico",
        # System tools without a package of the same name
        "flatpak", "containers", "environment",
    ),
    Category.DATA: (
        "fsu",
        "flatpak", "containers", "keyrings", "kwalletd", "Trash",
        "applications", "desktop-directories", "icons", "fonts", "mime",
        "themes", "sounds", "backgrounds", "wallpapers", "color-schemes",
        "recently-used.xbel", "user-places.xbel*", "gvfs-metadata",
        "gnome-*", "systemd", "dbus-1", "xorg", "sddm", "man",
        "plasma*", "kactivitymanagerd", "baloo", "akonadi*", "kxmlgui5", "kwin*",
        "tracker*", "localsearch*", "evolution", "pki", "nautilus",
        # KDE Frameworks data without a package of the same name
        "knewstuff3", "kpeople*", "krunnerstaterc", "libkunitconversion",
        "kded*", "kservices*", "kservicetypes*", "apps", "local-mail",
        "qtposition-geoclue2", "remoteview", "org.kde.*", "kscreen",
    ),
    Category.CACHE: (
        "fsu",
        "fontconfig", "thumbnails", "mesa_shader_cache*", "nvidia", "radv_builtin_shaders*",
        "flatpak", "dconf", "ibus", "gnome-*", "plasma*", "ksycoca*", "kwin*",
        "tracker*", "localsearch*", "gstreamer-*", "systemd",
        # KDE / Qt session caches
        "krunner", "bookmarksrunner", "kcrash-metadata", "ksvg-elements",
        "kscreenlocker_greet", "ksmserver-logout-greeter", "org.kde.*",
        "qtshadercache-*", "QtLocation", "kioexec", "drkonqi",
        # Other shared caches
        "event-sound-cache.tdb.*", "obexd", "wayland-errors", "gtk-*", "glycin",
        "JNA",
    ),
    Category.FLATPAK: (),
    Category.STATE: (
        "fsu", "wireplumber", "pipewire", "systemd", "plasma*", "kwin*", "gnome-*",
    ),
    # Hidden folders directly in $HOME. Besides identity and the XDG roots,
    # folders that usually hold *user data* are protected even when their
    # application was removed: removing an app does not make its data disposable.
    Category.HOME: (
        # XDG roots, FSU itself and the desktop session
        ".config", ".local", ".cache", ".var", ".fsu", ".dbus", ".kde", ".kde4",
        ".gnome", ".gnome2", ".themes", ".icons", ".fonts", ".pulse", ".Trash*",
        # Identity, keys and credentials
        ".ssh", ".gnupg", ".pki", ".password-store", ".android", ".docker", ".kube",
        ".aws", ".azure", ".terraform.d", ".vnc", ".cert", ".ollama",
        # User data: browsers/mail profiles, games, Windows prefixes, models, ...
        ".mozilla", ".thunderbird", ".librewolf", ".waterfox", ".floorp",
        ".minecraft", ".steam", ".steampath", ".steampid", ".local/share/Steam",
        ".wine", ".wine*", ".PlayOnLinux", ".lutris", ".lmstudio", ".java",
        ".electrum", ".bitcoin", ".ethereum", ".monero",
    ),
    Category.LAUNCHER: (
        "mimeapps.list", "defaults.list", "mimeinfo.cache", "*.list",
        "default.target.wants", "*.wants", "*.requires",
    ),
}
# fmt: on


def matching_root(path: Path, home: Path) -> tuple[Category, Path] | None:
    """Category and scan root of which ``path`` is *exactly* a direct child."""
    if path.name in ("", ".", ".."):
        return None
    for category, roots in scan_roots(home).items():
        for root in roots:
            if path.parent == root:
                return category, root
    return None


def category_of(path: Path, home: Path) -> Category | None:
    """Category of a first-level item, or ``None``."""
    found = matching_root(path, home)
    return found[0] if found else None


def is_protected(path: Path, home: Path) -> bool:
    """Whether FSU must ignore ``path``.

    Anything that is not a direct child of a scan root is protected
    (fail-safe). In $HOME itself, only hidden folders can ever be candidates.
    """
    category = category_of(path, home)
    if category is None:
        return True
    if category is Category.HOME and not path.name.startswith("."):
        return True
    name = path.name.lower()
    patterns = PROTECTED_PATTERNS[category]
    return any(fnmatch.fnmatchcase(name, pattern.lower()) for pattern in patterns)
