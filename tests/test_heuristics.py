from __future__ import annotations

import pytest

from fsu.heuristics import (
    MatchKind,
    action_for,
    best_match,
    compare,
    normalize,
    partial_removed_score,
)
from fsu.models import Action


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("obs-studio", "obsstudio"), (".Code", "code"), ("qBittorrent", "qbittorrent"), ("---", "")],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


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
    # "kde" é curto demais para casar por prefixo com "kdenlive".
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
    assert match.name == "gimp"
    assert match.kind is MatchKind.EXACT


def test_best_match_is_deterministic() -> None:
    names = ["hexchat-a", "hexchat-b"]
    first = best_match("hexchat", names)
    assert first == best_match("hexchat", list(reversed(names)))


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
