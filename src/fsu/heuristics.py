"""Deterministic (non-AI) scoring of residue candidates.

The score is the confidence that an item is left over by something that is
*no longer installed*. The action bands are fixed and conservative:

* ``>= 0.70`` - direct quarantine;
* ``0.40 to 0.70`` - quarantine only with a preventive snapshot;
* ``< 0.40`` - listed only.

This module will also be the AI engine's fallback (stage 3).
"""

from __future__ import annotations

import bisect
import enum
import fnmatch
import re
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache

from fsu.models import Action

THRESHOLD_QUARANTINE = 0.70
THRESHOLD_SNAPSHOT = 0.40

SCORE_FLATPAK_ORPHAN = 0.95
SCORE_EXACT_REMOVED = 0.85
SCORE_VARIANT_REMOVED = 0.80  # exact match through a name variant (e.g. "<app>rc")
SCORE_OWNER_REMOVED = 0.80  # folder owned (per DIRECTORY_OWNERS) by a removed package
SCORE_EXACT_STALE_APPIMAGE = 0.75  # launcher points to an AppImage that is gone
SCORE_SEEN_THEN_GONE = 0.75  # FSU's own history saw the program, now it is gone
SCORE_LAUNCHER_TARGET_MISSING = 0.85  # launcher/service runs a path or Flatpak that is gone
SCORE_LAUNCHER_PROGRAM_NOT_FOUND = 0.75  # bare program name not found in the search path
SCORE_PARTIAL_REMOVED_MIN = 0.50
SCORE_PARTIAL_REMOVED_MAX = 0.65
SCORE_NO_MATCH = 0.20
SCORE_CAP_PARTIAL_INSTALLED = 0.30

# Labels for partial-match strength in user- and model-facing evidence.
SIMILARITY_STRONG = 0.85
SIMILARITY_MODERATE = 0.60

# Minimum normalized length to accept a prefix match.
MIN_PREFIX_LEN = 4
# Minimum similarity (difflib ratio) to accept an approximate match.
MIN_SIMILARITY = 0.85

# KConfig naming convention: <component>rc, <component>staterc, ...
# Longest first, so "staterc" wins over "rc".
_KCONFIG_SUFFIXES = ("staterc", "partrc", "uirc", "rc")
_FILE_SUFFIXES = (".conf", ".json", ".ini", ".cfg")
_PREFIX_SEPARATORS = re.compile(r"[-_.]")

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def _letter_counts(normalized: str) -> tuple[int, ...]:
    """Character histogram of a normalized name (for a cheap similarity bound)."""
    return tuple(normalized.count(c) for c in _ALPHABET)


# Last components of reverse-DNS IDs that are too generic to identify an app
# (com.spotify.Client, org.telegram.desktop, com.obsproject.Studio, ...).
GENERIC_ID_PARTS = frozenset(
    {"desktop", "client", "studio", "launcher", "app", "application", "editor", "player", "gui"}
)


# Folders whose name is not the package name. Keys are normalized item names;
# values are fnmatch patterns of the packages (or Flatpak IDs) that own the
# folder. Any owner installed -> the folder is in use; otherwise, an owner
# found among removed packages is evidence of removal.
DIRECTORY_OWNERS: dict[str, tuple[str, ...]] = {
    # Browsers and mail
    "mozilla": ("firefox", "firefox-*", "thunderbird", "seamonkey", "org.mozilla.firefox"),
    "thunderbird": ("thunderbird", "org.mozilla.Thunderbird"),
    "googlechrome": ("google-chrome-*",),
    "chromium": ("chromium", "chromium-*", "org.chromium.Chromium"),
    "bravesoftware": ("brave-browser*", "com.brave.Browser"),
    "microsoftedge": ("microsoft-edge-*", "com.microsoft.Edge"),
    # Gaming
    "steam": ("steam", "com.valvesoftware.Steam"),
    # Development tools
    "nuget": ("dotnet*", "mono-core", "mono-devel"),  # .NET package cache (~/.nuget too)
    "dotnet": ("dotnet*",),  # ~/.dotnet
    "vscode": ("code", "code-insiders", "com.visualstudio.code", "codium", "com.vscodium.codium"),
    "vscodeshared": ("code", "code-insiders", "com.visualstudio.code"),
    "wget": ("wget", "wget1*", "wget2*"),  # HSTS database
    # KDE components whose config file is not named after the package
    "katevirc": ("kate", "kwrite", "kf6-ktexteditor", "kf5-ktexteditor"),
    "konsolesshconfig": ("konsole", "konsole5"),
    "filetypesrc": ("kde-cli-tools*", "plasma-systemsettings", "systemsettings"),
}


def owners_matching(item_name: str, names: Iterable[str]) -> list[str]:
    """Names among ``names`` that own ``item_name`` per :data:`DIRECTORY_OWNERS`."""
    patterns = DIRECTORY_OWNERS.get(normalize(item_name))
    if not patterns:
        return []
    return sorted(
        name for name in names if any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)
    )


class MatchKind(enum.Enum):
    EXACT = "exact"
    PARTIAL = "partial"


@dataclass(frozen=True)
class Match:
    name: str  # original package/app name that matched
    kind: MatchKind
    strength: float  # 1.0 for EXACT; (0, 1) for PARTIAL
    variant: str | None = None  # item-name variant used; None = original name


@lru_cache(maxsize=65536)
def normalize(name: str) -> str:
    """Canonical form for comparison: lowercase letters and digits only.

    ``"obs-studio"`` -> ``"obsstudio"``; ``".Code"`` -> ``"code"``.
    """
    return _NON_ALNUM.sub("", name.lower())


def useful_id_part(app_id: str) -> str | None:
    """Last component of a reverse-DNS ID, if it is specific enough.

    ``org.inkscape.Inkscape`` -> ``Inkscape``; ``com.spotify.Client`` -> ``None``.
    """
    if "." not in app_id:
        return None
    last = app_id.rsplit(".", 1)[1]
    if len(last) < MIN_PREFIX_LEN or last.lower() in GENERIC_ID_PARTS:
        return None
    return last


def name_variants(item_name: str) -> list[str]:
    """Alternative names under which an item may belong to an application.

    * KConfig suffixes: ``kolourpaintrc`` -> ``kolourpaint``,
      ``kfontinstuirc`` -> ``kfontinst``;
    * file extensions: ``sealert.conf`` -> ``sealert``;
    * the part before the first ``-``, ``_`` or ``.``:
      ``kunifiedpush-org.kde.neochat`` -> ``kunifiedpush``.

    Variants shorter than ``MIN_PREFIX_LEN`` (after normalization) are dropped,
    except for KConfig/extension stripping of at least 3 characters.
    """
    variants: list[str] = []
    lowered = item_name.lower()
    for suffix in _FILE_SUFFIXES:
        if lowered.endswith(suffix) and len(item_name) - len(suffix) >= 3:
            variants.append(item_name[: -len(suffix)])
            break
    for suffix in _KCONFIG_SUFFIXES:
        if lowered.endswith(suffix) and len(normalize(item_name[: -len(suffix)])) >= 3:
            variants.append(item_name[: -len(suffix)])
            break
    head = _PREFIX_SEPARATORS.split(item_name, maxsplit=1)[0]
    if head != item_name and len(normalize(head)) >= MIN_PREFIX_LEN:
        variants.append(head)
    seen = {normalize(item_name)}
    unique: list[str] = []
    for variant in variants:
        key = normalize(variant)
        if key and key not in seen:
            seen.add(key)
            unique.append(variant)
    return unique


def compare(item_name: str, candidate_name: str) -> Match | None:
    """Compare one $HOME item name with one package/app name (no variants)."""
    a, b = normalize(item_name), normalize(candidate_name)
    if not a or not b:
        return None
    if a == b:
        return Match(candidate_name, MatchKind.EXACT, 1.0)
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= MIN_PREFIX_LEN and longer.startswith(shorter):
        return Match(candidate_name, MatchKind.PARTIAL, len(shorter) / len(longer))
    if 2 * len(shorter) / (len(a) + len(b)) < MIN_SIMILARITY:
        return None
    matcher = SequenceMatcher(None, a, b)
    if matcher.quick_ratio() < MIN_SIMILARITY:
        return None
    ratio = matcher.ratio()
    return Match(candidate_name, MatchKind.PARTIAL, ratio) if ratio >= MIN_SIMILARITY else None


def _rank(match: Match) -> tuple[bool, bool, float, str]:
    """Sort key for ``min()``: EXACT before PARTIAL, original name before a
    variant, stronger before weaker, then alphabetical (deterministic)."""
    return (
        match.kind is not MatchKind.EXACT,
        match.variant is not None,
        -match.strength,
        match.name,
    )


class NameIndex:
    """Fast lookup of the best match for an item name among many names.

    Equivalent to calling :func:`compare` against every name, but uses a hash
    for exact matches, a sorted list for prefixes and length buckets for
    similarity, which matters with thousands of installed packages.
    """

    def __init__(self, names: Iterable[str]) -> None:
        by_norm: dict[str, str] = {}
        for name in names:
            key = normalize(name)
            if key and (key not in by_norm or name < by_norm[key]):
                by_norm[key] = name
        self._by_norm = by_norm
        self._sorted = sorted(by_norm)
        self._by_len: dict[int, list[tuple[str, tuple[int, ...]]]] = {}
        for key in self._sorted:
            self._by_len.setdefault(len(key), []).append((key, _letter_counts(key)))

    def __len__(self) -> int:
        return len(self._by_norm)

    def _match_one(self, item_name: str, variant: str | None) -> Match | None:
        a = normalize(item_name)
        if not a:
            return None
        if a in self._by_norm:
            return Match(self._by_norm[a], MatchKind.EXACT, 1.0, variant)

        found: dict[str, float] = {}
        # Names that start with the item (item is the shorter string).
        if len(a) >= MIN_PREFIX_LEN:
            i = bisect.bisect_left(self._sorted, a)
            while i < len(self._sorted) and self._sorted[i].startswith(a):
                key = self._sorted[i]
                found[key] = len(a) / len(key)
                i += 1
        # Names that are a prefix of the item (name is the shorter string).
        for length in range(MIN_PREFIX_LEN, len(a)):
            key = a[:length]
            if key in self._by_norm:
                found[key] = length / len(a)
        # Approximate similarity, only among names of compatible length.
        low = int(len(a) * MIN_SIMILARITY / (2 - MIN_SIMILARITY))
        high = int(len(a) * (2 - MIN_SIMILARITY) / MIN_SIMILARITY) + 1
        counts = _letter_counts(a)
        for length in range(max(low, 1), high + 1):
            # Upper bound shared by every key of this length (lengths only).
            if 2 * min(len(a), length) / (len(a) + length) < MIN_SIMILARITY:
                continue
            needed = MIN_SIMILARITY * (len(a) + length) / 2
            for key, key_counts in self._by_len.get(length, ()):
                if key in found:  # already matched as a prefix
                    continue
                # Same bound as difflib's quick_ratio(), computed in C via map().
                if sum(map(min, counts, key_counts)) < needed:
                    continue
                ratio = SequenceMatcher(None, a, key).ratio()
                if ratio >= MIN_SIMILARITY:
                    found[key] = ratio
        if not found:
            return None
        matches = [
            Match(self._by_norm[key], MatchKind.PARTIAL, strength, variant)
            for key, strength in found.items()
        ]
        return min(matches, key=_rank)

    def match(self, item_name: str, use_variants: bool = True) -> Match | None:
        """Best match for ``item_name`` (and, optionally, its variants)."""
        matches = []
        original = self._match_one(item_name, None)
        if original is not None:
            if original.kind is MatchKind.EXACT:
                return original
            matches.append(original)
        if use_variants:
            for variant in name_variants(item_name):
                found = self._match_one(variant, variant)
                if found is not None:
                    matches.append(found)
        return min(matches, key=_rank) if matches else None


def best_match(item_name: str, names: Iterable[str], use_variants: bool = True) -> Match | None:
    """Convenience wrapper around :class:`NameIndex` for one-off lookups."""
    return NameIndex(names).match(item_name, use_variants)


def partial_removed_score(strength: float) -> float:
    """Map the strength of a partial match to a score (0.50-0.65)."""
    clamped = min(max(strength, 0.0), 1.0)
    span = SCORE_PARTIAL_REMOVED_MAX - SCORE_PARTIAL_REMOVED_MIN
    return round(SCORE_PARTIAL_REMOVED_MIN + span * clamped, 3)


def action_for(score: float) -> Action:
    """Allowed action band for a score."""
    if score >= THRESHOLD_QUARANTINE:
        return Action.QUARANTINE
    if score >= THRESHOLD_SNAPSHOT:
        return Action.QUARANTINE_WITH_SNAPSHOT
    return Action.LIST_ONLY
