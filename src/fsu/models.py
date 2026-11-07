"""Modelos de dados imutáveis compartilhados pelos módulos do FSU."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class Category(enum.Enum):
    """Origem do item residual dentro do $HOME."""

    CONFIG = "config"  # ~/.config
    DATA = "data"  # ~/.local/share
    CACHE = "cache"  # ~/.cache
    FLATPAK = "flatpak"  # ~/.var/app


class Action(enum.Enum):
    """O que o FSU tem permissão de fazer com um candidato, segundo o score.

    As faixas são aplicadas por :func:`fsu.heuristics.action_for`.
    """

    QUARANTINE = "quarantine"  # score >= 0.70: quarentena direta
    QUARANTINE_WITH_SNAPSHOT = "quarantine_with_snapshot"  # 0.40 <= score < 0.70
    LIST_ONLY = "list_only"  # score < 0.40: nunca movido automaticamente


@dataclass(frozen=True)
class Evidence:
    """Uma justificativa legível para o score de um candidato."""

    source: str  # ex.: "flatpak", "dnf5-history", "rpm", "desktop"
    detail: str


@dataclass(frozen=True)
class RemovedPackage:
    """Pacote RPM removido segundo o histórico do DNF5."""

    name: str
    transaction_id: int
    removed_at: int  # UNIX time do fim da transação
    reason: str = ""  # motivo de instalação registrado (User, Dependency…)


@dataclass(frozen=True)
class Candidate:
    """Item do $HOME que pode ser resíduo de um aplicativo removido."""

    path: Path
    category: Category
    score: float
    action: Action
    size_bytes: int
    mtime: float
    is_symlink: bool
    matched_name: str | None = None
    evidence: tuple[Evidence, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "category": self.category.value,
            "score": round(self.score, 3),
            "action": self.action.value,
            "size_bytes": self.size_bytes,
            "mtime": self.mtime,
            "is_symlink": self.is_symlink,
            "matched_name": self.matched_name,
            "evidence": [{"source": e.source, "detail": e.detail} for e in self.evidence],
        }


@dataclass(frozen=True)
class ScanReport:
    """Resultado de uma varredura somente leitura."""

    candidates: tuple[Candidate, ...]
    warnings: tuple[str, ...] = ()
    protected_skipped: tuple[Path, ...] = field(default=())
    installed_skipped: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": [c.to_dict() for c in self.candidates],
            "warnings": list(self.warnings),
            "protected_skipped": [str(p) for p in self.protected_skipped],
            "installed_skipped": self.installed_skipped,
        }
