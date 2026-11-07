"""Caminhos fixos usados pelo FSU, relativos ao $HOME."""

from __future__ import annotations

from pathlib import Path

from fsu.models import Category

# Únicas pastas que o FSU varre (apenas o primeiro nível de cada uma).
SCAN_ROOTS: dict[Category, Path] = {
    Category.CONFIG: Path(".config"),
    Category.DATA: Path(".local/share"),
    Category.CACHE: Path(".cache"),
    Category.FLATPAK: Path(".var/app"),
}

# Diretório de trabalho do próprio FSU (quarentena, estado). Fica dentro de
# ~/.local/share e por isso é protegido explicitamente.
FSU_DATA_DIR = Path(".local/share/fsu")
QUARANTINE_DIR = FSU_DATA_DIR / "quarantine"
