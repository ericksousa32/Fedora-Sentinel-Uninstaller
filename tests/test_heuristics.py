from __future__ import annotations

import random
import string

import pytest

from fsu.heuristics import (
    MatchKind,
    NameIndex,
    action_for,
    best_match,
    compare,
    name_variants,
    normalize,
    partial_removed_score,
    useful_id_part,
)
from fsu.models import Action


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("obs-studio", "obsstudio"), (".Code", "code"), ("qBittorrent", "qbittorrent"), ("---", "")],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


@pytest.mark.parametrize(
    ("app_id", "part"),
    [
        ("org.inkscape.Inkscape", "Inkscape"),
        ("com.obsproject.Studio", None),  # generic
        ("com.spotify.Client", None),
        ("org.telegram.desktop", None),
        ("com.heroicgameslauncher.hgl", None),  # too short
        ("bash", None),
    ],
)
def test_useful_id_part(app_id: str, part: str | None) -> None:
    assert useful_id_part(app_id) == part


@pytest.mark.parametrize(
    ("item", "variants"),
    [
        ("kolourpaintrc", ["kolourpaint"]),
        ("kfontinstuirc", ["kfontinst"]),
        ("krunnerstaterc", ["krunner"]),
        ("okularpartrc", ["okular"]),
        ("sealert.conf", ["sealert"]),
        ("kunifiedpush-org.kde.neochat", ["kunifiedpush"]),
        ("kunifiedpush-:1.330", ["kunifiedpush"]),
        ("xdg-desktop-portal-kderc", ["xdg-desktop-portal-kde"]),
        ("hexchat", []),
        ("arc", []),  # "a" is too short to be a KConfig stem
        ("kde.org", []),  # "kde" is too short as a prefix
    ],
)
def test_name_variants(item: str, variants: list[str]) -> None:
    assert name_variants(item) == variants


def test_exact_ignores_case_and_punctuation() -> None:
    match = compare("OBS_Studio", "obs-studio")
    assert match is not None
    assert match.kind is MatchKind.EXACT


def test_prefix_partial() -> None:
    match = compare("google-chrome", "google-chrome-stable")
    assert match is not None
    assert match.kind is MatchKind.PARTIAL
    assert 0 < match.strength < 1


def test_short_prefix_is_rejected() -> None:
    # "kde" is too short to match "kdenlive" by prefix.
    assert compare("kde", "kdenlive") is None


def test_similarity_partial() -> None:
    match = compare("hexchatt", "hexchat")
    assert match is not None
    assert match.kind is MatchKind.PARTIAL


def test_unrelated() -> None:
    assert compare("discord", "inkscape") is None


def test_best_match_prefers_exact() -> None:
    match = best_match("gimp", ["gimp-help", "gimp", "gimpfu"])
    assert match is not None
    assert (match.name, match.kind) == ("gimp", MatchKind.EXACT)


def test_variant_exact_beats_original_partial() -> None:
    match = best_match("kolourpaintrc", ["kolourpaint"])
    assert match is not None
    assert match.kind is MatchKind.EXACT
    assert match.variant == "kolourpaint"


def test_original_exact_has_no_variant() -> None:
    match = best_match("dolphin", ["dolphin"])
    assert match is not None
    assert match.variant is None


def test_variants_can_be_disabled() -> None:
    match = best_match("kolourpaintrc", ["kolourpaint"], use_variants=False)
    assert match is not None
    assert match.kind is MatchKind.PARTIAL


def test_best_match_is_deterministic() -> None:
    names = ["hexchat-a", "hexchat-b"]
    first = best_match("hexchat", names)
    assert first == best_match("hexchat", list(reversed(names)))
    assert first is not None
    assert first.name == "hexchat-a"


def test_name_index_matches_naive_compare() -> None:
    """NameIndex must give the same answer as comparing against every name."""
    rng = random.Random(42)
    alphabet = string.ascii_lowercase[:6] + "-"
    names = {"".join(rng.choices(alphabet, k=rng.randint(3, 12))) for _ in range(400)}
    index = NameIndex(names)
    for _ in range(300):
        item = "".join(rng.choices(alphabet, k=rng.randint(3, 12)))
        naive = [m for m in (compare(item, n) for n in names) if m is not None]
        expected = None
        if naive:
            expected = min(
                naive, key=lambda m: (m.kind is not MatchKind.EXACT, -m.strength, m.name)
            )
        got = index.match(item, use_variants=False)
        if expected is None:
            assert got is None, item
        else:
            assert got is not None, item
            assert (got.kind, round(got.strength, 9)) == (
                expected.kind,
                round(expected.strength, 9),
            ), item


def test_partial_score_range() -> None:
    assert partial_removed_score(0.0) == 0.50
    assert partial_removed_score(1.0) == 0.65
    assert partial_removed_score(7.0) == 0.65


@pytest.mark.parametrize(
    ("score", "action"),
    [
        (0.95, Action.QUARANTINE),
        (0.70, Action.QUARANTINE),
        (0.6999, Action.QUARANTINE_WITH_SNAPSHOT),
        (0.40, Action.QUARANTINE_WITH_SNAPSHOT),
        (0.3999, Action.LIST_ONLY),
        (0.0, Action.LIST_ONLY),
    ],
)
def test_action_bands(score: float, action: Action) -> None:
    assert action_for(score) is action
