"""Pontuação determinística (sem IA) de candidatos a resíduo.

O score é a confiança de que o item é resíduo de algo que *não está mais
instalado*. As faixas de ação são fixas e conservadoras:

* ``>= 0.70`` — quarentena direta;
* ``0.40 a 0.70`` — quarentena só com snapshot preventivo;
* ``< 0.40`` — apenas listado.

Este módulo também será o *fallback* do motor de IA (etapa 3).
"""

from __future__ import annotations

import enum
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
SCORE_PARTIAL_REMOVED_MIN = 0.50
SCORE_PARTIAL_REMOVED_MAX = 0.65
SCORE_NO_MATCH = 0.20
SCORE_CAP_PARTIAL_INSTALLED = 0.30

# Tamanho mínimo (após normalização) para aceitar correspondência por prefixo.
MIN_PREFIX_LEN = 4
# Similaridade mínima (difflib) para aceitar correspondência aproximada.
MIN_SIMILARITY = 0.85

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


class MatchKind(enum.Enum):
    EXACT = "exact"
    PARTIAL = "partial"


@dataclass(frozen=True)
class Match:
    name: str  # nome original (pacote/app) que correspondeu
    kind: MatchKind
    strength: float  # 1.0 para EXACT; (0, 1) para PARTIAL


@lru_cache(maxsize=65536)
def normalize(name: str) -> str:
    """Forma canônica para comparação: minúsculas, só letras e dígitos.

    ``"obs-studio"`` → ``"obsstudio"``; ``".Code"`` → ``"code"``;
    ``"settings.json"`` → ``"settingsjson"``.
    """
    return _NON_ALNUM.sub("", name.lower())


def compare(item_name: str, candidate_name: str) -> Match | None:
    """Compara o nome de um item do $HOME com um nome de pacote/app."""
    a, b = normalize(item_name), normalize(candidate_name)
    if not a or not b:
        return None
    if a == b:
        return Match(candidate_name, MatchKind.EXACT, 1.0)
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= MIN_PREFIX_LEN and longer.startswith(shorter):
        return Match(candidate_name, MatchKind.PARTIAL, len(shorter) / len(longer))
    # Limite superior barato da similaridade antes do cálculo completo.
    if 2 * len(shorter) / (len(a) + len(b)) < MIN_SIMILARITY:
        return None
    matcher = SequenceMatcher(None, a, b)
    if matcher.quick_ratio() < MIN_SIMILARITY:
        return None
    ratio = matcher.ratio()
    if ratio >= MIN_SIMILARITY:
        return Match(candidate_name, MatchKind.PARTIAL, ratio)
    return None


def best_match(item_name: str, names: Iterable[str]) -> Match | None:
    """Melhor correspondência de ``item_name`` dentro de ``names``.

    EXACT sempre vence PARTIAL; entre PARTIAL vence a maior ``strength``;
    empates são resolvidos pelo nome, para resultados determinísticos.
    """
    best: Match | None = None
    for name in names:
        match = compare(item_name, name)
        if match is None:
            continue
        if match.kind is MatchKind.EXACT:
            if best is None or best.kind is not MatchKind.EXACT or name < best.name:
                best = match
            continue
        if best is not None and best.kind is MatchKind.EXACT:
            continue
        if best is None or (match.strength, best.name) > (best.strength, match.name):
            best = match
    return best


def partial_removed_score(strength: float) -> float:
    """Converte a força de uma correspondência parcial em score (0.50–0.65)."""
    clamped = min(max(strength, 0.0), 1.0)
    span = SCORE_PARTIAL_REMOVED_MAX - SCORE_PARTIAL_REMOVED_MIN
    return round(SCORE_PARTIAL_REMOVED_MIN + span * clamped, 3)


def action_for(score: float) -> Action:
    """Faixa de ação permitida para um score."""
    if score >= THRESHOLD_QUARANTINE:
        return Action.QUARANTINE
    if score >= THRESHOLD_SNAPSHOT:
        return Action.QUARANTINE_WITH_SNAPSHOT
    return Action.LIST_ONLY
