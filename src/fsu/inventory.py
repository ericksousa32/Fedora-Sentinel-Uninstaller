"""Fontes de verdade sobre o que está (ou esteve) instalado no sistema.

* :class:`RpmInventory` — pacotes RPM instalados agora (``rpm -qa``).
* :class:`DnfHistory` — pacotes removidos segundo o histórico do DNF5
  (``dnf5 history list/info --json``). Não lê arquivos de log nem o
  ``/var/lib/dnf`` do DNF4.
* :class:`FlatpakInventory` — aplicativos Flatpak instalados (usuário e sistema).
* :class:`DesktopIndex` — nomes conhecidos a partir dos arquivos ``.desktop``
  de aplicativos instalados (evita falsos positivos como ``~/.config/Code``).

Todas as classes são somente leitura.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from fsu.models import RemovedPackage
from fsu.system import CommandError, Runner, run_command

# Ações do DNF5 que significam que o pacote saiu do sistema. "Replaced" e
# "Obsoleted" não entram: o nome normalmente continua instalado em outra versão
# ou sob outro pacote que o substitui.
_REMOVE_ACTIONS = frozenset({"remove", "removed"})

# Pacotes que praticamente nunca criam pastas próprias no $HOME. Ignorá-los
# reduz ruído (ex.: remover um app leva junto várias bibliotecas).
_NOISE_SUFFIXES = (
    "-devel", "-libs", "-doc", "-docs", "-debuginfo", "-debugsource", "-static",
    "-headers", "-common", "-data", "-filesystem", "-fonts", "-langpacks",
)  # fmt: skip
_NOISE_PREFIXES = (
    "python3-", "perl-", "golang-", "rust-", "texlive-", "ghc-", "ocaml-",
    "lib", "kf5-", "kf6-", "qt5-", "qt6-", "glibc-", "langpacks-",
)  # fmt: skip
# Exceções ao prefixo "lib" que são aplicativos de verdade.
_NOISE_PREFIX_EXCEPTIONS = ("libre",)  # libreoffice, librewolf

_FLATPAK_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z_][A-Za-z0-9_-]*)+$")


def nevra_name(nevra: str) -> str:
    """Extrai o nome de um NEVRA: ``nome-[época:]versão-release.arch``."""
    without_arch = nevra.rsplit(".", 1)[0] if "." in nevra else nevra
    parts = without_arch.rsplit("-", 2)
    return parts[0] if len(parts) == 3 else without_arch


def is_noise_package(name: str) -> bool:
    """Pacotes de biblioteca/suporte que não geram resíduos próprios no $HOME."""
    lowered = name.lower()
    if lowered.endswith(_NOISE_SUFFIXES):
        return True
    if lowered.startswith(_NOISE_PREFIX_EXCEPTIONS):
        return False
    return lowered.startswith(_NOISE_PREFIXES)


class RpmInventory:
    """Pacotes RPM atualmente instalados."""

    def __init__(self, runner: Runner = run_command) -> None:
        self._runner = runner

    def installed_names(self) -> frozenset[str]:
        output = self._runner(["rpm", "-qa", "--queryformat", "%{NAME}\\n"])
        names = frozenset(line.strip() for line in output.splitlines() if line.strip())
        if not names:
            # Um Fedora sem nenhum RPM instalado é impossível: trate como falha
            # para que nada seja julgado com base em um inventário vazio.
            raise CommandError(["rpm", "-qa"], "inventário vazio")
        return names


class DnfHistory:
    """Leitura do histórico de transações do DNF5 (formato JSON)."""

    def __init__(self, runner: Runner = run_command, fallback_limit: int = 150) -> None:
        self._runner = runner
        self._fallback_limit = fallback_limit
        self.warnings: list[str] = []

    def _json(self, args: Sequence[str]) -> list[dict[str, Any]]:
        output = self._runner(args)
        try:
            data = json.loads(output) if output.strip() else []
        except json.JSONDecodeError as exc:
            raise CommandError(args, f"JSON inválido: {exc.msg}") from exc
        if not isinstance(data, list):
            raise CommandError(args, "JSON inesperado: esperado um array")
        return [item for item in data if isinstance(item, dict)]

    def transaction_ids(self) -> list[int]:
        """IDs de todas as transações registradas (ordem decrescente)."""
        transactions = self._json(["dnf5", "history", "list", "--json"])
        ids = {t["id"] for t in transactions if isinstance(t.get("id"), int)}
        return sorted(ids, reverse=True)

    def _details(self, ids: list[int]) -> list[dict[str, Any]]:
        """Detalhes (com pacotes) das transações.

        Tenta uma única chamada com intervalo (``min..max``). Se a versão do
        DNF5 não aceitar, cai para chamadas individuais nas transações mais
        recentes, limitadas por ``fallback_limit``.
        """
        if not ids:
            return []
        spec = f"{min(ids)}..{max(ids)}"
        try:
            return self._json(["dnf5", "history", "info", spec, "--json"])
        except CommandError as exc:
            self.warnings.append(
                f"dnf5 history info com intervalo falhou ({exc}); consultando as "
                f"{min(len(ids), self._fallback_limit)} transações mais recentes uma a uma."
            )
        details: list[dict[str, Any]] = []
        for tid in ids[: self._fallback_limit]:
            details.extend(self._json(["dnf5", "history", "info", str(tid), "--json"]))
        return details

    def removed_packages(self) -> list[RemovedPackage]:
        """Pacotes removidos, com a remoção mais recente de cada nome.

        Não desconta pacotes reinstalados depois: isso é feito pelo coletor
        cruzando com :class:`RpmInventory`, que é a fonte do estado atual.
        """
        latest: dict[str, RemovedPackage] = {}
        for transaction in self._details(self.transaction_ids()):
            tid = transaction.get("id")
            end_time = transaction.get("end_time") or transaction.get("start_time") or 0
            packages = transaction.get("packages")
            if not isinstance(tid, int) or not isinstance(packages, list):
                continue
            for package in packages:
                if not isinstance(package, dict):
                    continue
                action = str(package.get("action", "")).strip().lower()
                nevra = package.get("nevra")
                if action not in _REMOVE_ACTIONS or not isinstance(nevra, str):
                    continue
                name = nevra_name(nevra)
                record = RemovedPackage(
                    name=name,
                    transaction_id=tid,
                    removed_at=int(end_time),
                    reason=str(package.get("reason", "")),
                )
                current = latest.get(name)
                if current is None or record.removed_at >= current.removed_at:
                    latest[name] = record
        return sorted(latest.values(), key=lambda r: (-r.removed_at, r.name))


class FlatpakInventory:
    """Aplicativos Flatpak instalados (instalações de usuário e de sistema)."""

    def __init__(self, runner: Runner = run_command) -> None:
        self._runner = runner

    def installed_app_ids(self) -> frozenset[str]:
        output = self._runner(["flatpak", "list", "--app", "--columns=application"])
        # Filtra cabeçalhos (que podem vir traduzidos, ex.: "ID de aplicativo")
        # aceitando apenas linhas com formato de ID Flatpak.
        return frozenset(
            line.strip() for line in output.splitlines() if _FLATPAK_ID.match(line.strip())
        )


# Locais de .desktop de aplicativos instalados. ~/.local/share/applications
# fica de fora de propósito: pode conter atalhos órfãos de apps removidos.
DEFAULT_DESKTOP_DIRS: tuple[Path, ...] = (
    Path("/usr/share/applications"),
    Path("/usr/local/share/applications"),
    Path("/var/lib/flatpak/exports/share/applications"),
)
USER_FLATPAK_DESKTOP_DIR = Path(".local/share/flatpak/exports/share/applications")

_EXEC_WRAPPERS = frozenset({"env", "sh", "bash", "flatpak", "pkexec", "sudo", "gio", "xdg-open"})


class DesktopIndex:
    """Nomes associados a aplicativos instalados, extraídos de arquivos ``.desktop``."""

    def __init__(self, directories: Iterable[Path]) -> None:
        self._directories = tuple(directories)
        self.warnings: list[str] = []

    @staticmethod
    def _names_from_file(path: Path) -> set[str]:
        names = {path.stem}
        # Último componente de IDs reverse-DNS: org.inkscape.Inkscape -> Inkscape
        if "." in path.stem:
            names.add(path.stem.rsplit(".", 1)[1])
        in_entry = False
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if line.startswith("["):
                in_entry = line == "[Desktop Entry]"
                continue
            if not in_entry or "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if key == "StartupWMClass" and value:
                names.add(value)
            elif key == "Exec" and value:
                for token in value.split():
                    if "=" in token and not token.startswith("/"):
                        continue  # variáveis de ambiente (FOO=bar)
                    binary = Path(token.strip("\"'")).name
                    if binary == "flatpak":
                        break  # "flatpak run <id>": o ID já vem do nome do arquivo
                    if binary in _EXEC_WRAPPERS:
                        continue
                    if binary and not binary.startswith("%"):
                        names.add(binary)
                    break
        return {n for n in names if n}

    def known_names(self) -> frozenset[str]:
        names: set[str] = set()
        for directory in self._directories:
            if not directory.is_dir():
                continue
            try:
                files = sorted(directory.glob("*.desktop"))
            except OSError as exc:
                self.warnings.append(f"não foi possível listar {directory}: {exc}")
                continue
            for desktop_file in files:
                try:
                    names |= self._names_from_file(desktop_file)
                except OSError as exc:
                    self.warnings.append(f"não foi possível ler {desktop_file}: {exc}")
        return frozenset(names)
