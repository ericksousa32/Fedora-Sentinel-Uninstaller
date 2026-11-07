"""Núcleo de coleta (etapa 1): identifica candidatos a resíduo, SOMENTE LEITURA.

Fluxo de :meth:`Collector.collect`:

1. Monta os inventários: RPM instalados, removidos no DNF5, Flatpaks
   instalados e nomes de arquivos ``.desktop``.
2. Lista o primeiro nível de ``~/.config``, ``~/.local/share``, ``~/.cache``
   e ``~/.var/app`` sem seguir symlinks (:class:`HomeScanner`).
3. Descarta itens protegidos e itens que correspondem exatamente a algo
   instalado; pontua o restante com :mod:`fsu.heuristics`.

Falha segura: se a fonte necessária para julgar uma categoria não estiver
disponível (ex.: ``rpm`` ou ``flatpak`` ausentes), essa categoria inteira é
ignorada, em vez de tudo parecer "órfão".

Este módulo nunca cria, move, altera ou apaga arquivos.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

from fsu import heuristics
from fsu.inventory import (
    DEFAULT_DESKTOP_DIRS,
    USER_FLATPAK_DESKTOP_DIR,
    DesktopIndex,
    DnfHistory,
    FlatpakInventory,
    RpmInventory,
    is_noise_package,
)
from fsu.models import Candidate, Category, Evidence, ScanReport
from fsu.paths import SCAN_ROOTS
from fsu.protected import is_protected
from fsu.system import CommandError, CommandNotFoundError, Runner, run_command

# Últimos componentes de IDs Flatpak genéricos demais para servirem de nome
# (com.spotify.Client, org.telegram.desktop, com.obsproject.Studio…).
_GENERIC_ID_PARTS = frozenset(
    {"desktop", "client", "studio", "launcher", "app", "application", "editor", "player"}
)


def measure(path: Path) -> tuple[int, bool]:
    """Tamanho em bytes de ``path`` sem seguir symlinks nem cruzar montagens.

    Devolve ``(bytes, completo)``; ``completo`` é ``False`` se alguma parte
    não pôde ser lida ou estava em outro sistema de arquivos.
    """
    root_stat = path.lstat()
    if not stat.S_ISDIR(root_stat.st_mode):
        return root_stat.st_size, True
    total, complete = 0, True
    stack = [str(path)]
    while stack:
        directory = stack.pop()
        try:
            iterator = os.scandir(directory)
        except OSError:
            complete = False
            continue
        with iterator:
            for entry in iterator:
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError:
                    complete = False
                    continue
                if stat.S_ISDIR(entry_stat.st_mode):
                    if entry_stat.st_dev != root_stat.st_dev:
                        complete = False  # ponto de montagem: não atravessa
                        continue
                    stack.append(entry.path)
                else:
                    total += entry_stat.st_size
    return total, complete


class HomeScanner:
    """Lista os itens de primeiro nível das raízes de varredura."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.warnings: list[str] = []

    def entries(self) -> Iterator[tuple[Category, Path]]:
        for category, relative_root in SCAN_ROOTS.items():
            root = self.home / relative_root
            if root.is_symlink():
                # Raiz apontando para outro lugar (ex.: gerenciador de dotfiles):
                # fora do escopo seguro desta etapa.
                self.warnings.append(f"{root} é um symlink; raiz ignorada por segurança.")
                continue
            if not root.is_dir():
                continue
            try:
                names = sorted(child.name for child in root.iterdir())
            except OSError as exc:
                self.warnings.append(f"não foi possível listar {root}: {exc}")
                continue
            for name in names:
                yield category, root / name


class Collector:
    """Orquestra inventários, varredura do $HOME e pontuação."""

    def __init__(
        self,
        home: Path | None = None,
        runner: Runner = run_command,
        desktop_dirs: Iterable[Path] | None = None,
    ) -> None:
        self.home = (home or Path.home()).absolute()
        self._runner = runner
        if desktop_dirs is None:
            desktop_dirs = (*DEFAULT_DESKTOP_DIRS, self.home / USER_FLATPAK_DESKTOP_DIR)
        self._desktop_dirs = tuple(desktop_dirs)

    # -- inventários ---------------------------------------------------------

    def _rpm_installed(self, warnings: list[str]) -> frozenset[str] | None:
        try:
            return RpmInventory(self._runner).installed_names()
        except CommandError as exc:
            warnings.append(
                f"Inventário RPM indisponível ({exc}); ~/.config, ~/.local/share e "
                "~/.cache não serão avaliados."
            )
            return None

    def _removed(self, installed: frozenset[str] | None, warnings: list[str]) -> set[str]:
        history = DnfHistory(self._runner)
        try:
            removed = history.removed_packages()
        except CommandError as exc:
            warnings.append(f"Histórico do DNF5 indisponível ({exc}).")
            return set()
        finally:
            warnings.extend(history.warnings)
        installed = installed or frozenset()
        return {r.name for r in removed if r.name not in installed and not is_noise_package(r.name)}

    def _flatpak_installed(self, warnings: list[str]) -> frozenset[str] | None:
        try:
            return FlatpakInventory(self._runner).installed_app_ids()
        except CommandNotFoundError:
            warnings.append("Flatpak não encontrado; ~/.var/app não será avaliado.")
        except CommandError as exc:
            warnings.append(f"Inventário Flatpak indisponível ({exc}); ~/.var/app ignorado.")
        return None

    # -- pontuação -----------------------------------------------------------

    def _candidate(
        self, path: Path, category: Category, score: float, evidence: list[Evidence],
        matched: str | None, warnings: list[str],
    ) -> Candidate | None:  # fmt: skip
        try:
            info = path.lstat()
        except OSError as exc:
            warnings.append(f"não foi possível inspecionar {path}: {exc}")
            return None
        is_link = stat.S_ISLNK(info.st_mode)
        if is_link:
            size, complete = 0, True
            evidence.append(Evidence("fsu", "symlink: só o link será considerado, nunca o destino"))
        else:
            size, complete = measure(path)
        if not complete:
            warnings.append(f"tamanho de {path} é parcial (conteúdo ilegível ou outra montagem).")
        if info.st_uid != os.getuid():
            evidence.append(Evidence("fsu", f"dono diferente do usuário atual (uid {info.st_uid})"))
        return Candidate(
            path=path,
            category=category,
            score=score,
            action=heuristics.action_for(score),
            size_bytes=size,
            mtime=info.st_mtime,
            is_symlink=is_link,
            matched_name=matched,
            evidence=tuple(evidence),
        )

    def collect(self) -> ScanReport:
        warnings: list[str] = []
        rpm_installed = self._rpm_installed(warnings)
        removed = self._removed(rpm_installed, warnings)
        flatpak_ids = self._flatpak_installed(warnings)

        desktop = DesktopIndex(self._desktop_dirs)
        installed_names: set[str] = set(desktop.known_names())
        warnings.extend(desktop.warnings)
        if rpm_installed:
            installed_names |= rpm_installed
        if flatpak_ids:
            installed_names |= flatpak_ids
            installed_names |= {
                last
                for last in (app_id.rsplit(".", 1)[1] for app_id in flatpak_ids)
                if len(last) >= 4 and last.lower() not in _GENERIC_ID_PARTS
            }

        scanner = HomeScanner(self.home)
        candidates: list[Candidate] = []
        protected: list[Path] = []
        installed_skipped = 0

        for category, path in scanner.entries():
            if is_protected(path, self.home):
                protected.append(path)
                continue

            evidence: list[Evidence] = []
            matched: str | None = None

            if category is Category.FLATPAK:
                if flatpak_ids is None:
                    continue
                if path.name in flatpak_ids:
                    installed_skipped += 1
                    continue
                score = heuristics.SCORE_FLATPAK_ORPHAN
                matched = path.name
                evidence.append(Evidence("flatpak", f"app {path.name} não está instalado"))
            else:
                if rpm_installed is None:
                    continue
                installed_match = heuristics.best_match(path.name, installed_names)
                if installed_match and installed_match.kind is heuristics.MatchKind.EXACT:
                    installed_skipped += 1
                    continue
                removed_match = heuristics.best_match(path.name, removed)
                if removed_match is None:
                    score = heuristics.SCORE_NO_MATCH
                    evidence.append(Evidence("heuristic", "sem correspondência conhecida"))
                elif removed_match.kind is heuristics.MatchKind.EXACT:
                    score = heuristics.SCORE_EXACT_REMOVED
                    matched = removed_match.name
                    evidence.append(
                        Evidence("dnf5-history", f"pacote removido: {removed_match.name}")
                    )
                else:
                    score = heuristics.partial_removed_score(removed_match.strength)
                    matched = removed_match.name
                    evidence.append(
                        Evidence(
                            "dnf5-history",
                            f"semelhante ao pacote removido {removed_match.name} "
                            f"({removed_match.strength:.2f})",
                        )
                    )
                if installed_match is not None:
                    score = min(score, heuristics.SCORE_CAP_PARTIAL_INSTALLED)
                    evidence.append(
                        Evidence(
                            "installed",
                            f"semelhante ao instalado {installed_match.name} "
                            f"({installed_match.strength:.2f})",
                        )
                    )

            candidate = self._candidate(path, category, score, evidence, matched, warnings)
            if candidate is not None:
                candidates.append(candidate)

        warnings.extend(scanner.warnings)
        candidates.sort(key=lambda c: (-c.score, str(c.path)))
        return ScanReport(
            candidates=tuple(candidates),
            warnings=tuple(warnings),
            protected_skipped=tuple(protected),
            installed_skipped=installed_skipped,
        )


# -- execução manual (provisória, até o cli.py da etapa 4) ---------------------


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"  # pragma: no cover


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fsu.collector",
        description="Varredura SOMENTE LEITURA de resíduos no $HOME.",
    )
    parser.add_argument("--json", action="store_true", help="saída em JSON")
    parser.add_argument("--home", type=Path, default=None, help="home alternativa (testes)")
    args = parser.parse_args(argv)

    report = Collector(home=args.home).collect()
    if args.json:
        json.dump(report.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    for c in report.candidates:
        reasons = "; ".join(e.detail for e in c.evidence)
        print(f"{c.score:.2f}  {c.action.value:<25} {_human_size(c.size_bytes):>10}  {c.path}")
        print(f"      └─ {reasons}")
    total = sum(c.size_bytes for c in report.candidates)
    print(
        f"\n{len(report.candidates)} candidatos ({_human_size(total)}); "
        f"{len(report.protected_skipped)} protegidos; "
        f"{report.installed_skipped} correspondem a algo instalado."
    )
    for warning in report.warnings:
        print(f"aviso: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
