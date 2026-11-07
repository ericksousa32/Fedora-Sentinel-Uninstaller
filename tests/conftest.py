"""Utilitários compartilhados pelos testes."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


Response = str | BaseException


class FakeRunner:
    """Substitui :func:`fsu.system.run_command` com respostas por prefixo.

    A chave mais longa que for prefixo do comando vence. Comandos sem
    resposta cadastrada levantam ``CommandNotFoundError``.
    """

    def __init__(self, responses: dict[tuple[str, ...], Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> str:
        from fsu.system import CommandNotFoundError

        command = tuple(args)
        self.calls.append(command)
        matches = [k for k in self.responses if command[: len(k)] == k]
        if not matches:
            raise CommandNotFoundError(command, "executável não encontrado (fake)")
        response = self.responses[max(matches, key=len)]
        if isinstance(response, BaseException):
            raise response
        return response


RPM_INSTALLED = "\n".join(
    [
        "bash",
        "rpm",
        "dnf5",
        "firefox",
        "gimp",  # removido em 396, mas reinstalado depois
        "mozilla-filesystem",
        "kdenlive",
        "linuxtoys",
    ]
)


def default_responses() -> dict[tuple[str, ...], Response]:
    return {
        ("rpm", "-qa"): RPM_INSTALLED,
        ("dnf5", "history", "list"): fixture_text("dnf5_history_list.json"),
        ("dnf5", "history", "info"): fixture_text("dnf5_history_info.json"),
        ("flatpak", "list"): fixture_text("flatpak_list.txt"),
    }


@pytest.fixture
def fake_runner() -> FakeRunner:
    return FakeRunner(default_responses())


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home" / "erick"
    path.mkdir(parents=True)
    return path
