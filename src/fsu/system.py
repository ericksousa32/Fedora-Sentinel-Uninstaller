"""Execução controlada de comandos externos.

Todo subprocesso do FSU passa por :func:`run_command`: sem shell, com
timeout, com locale fixo (a saída do Fedora do usuário pode estar em
português) e com erros tipados. Os módulos recebem um ``Runner`` por
injeção, o que permite testá-los sem um Fedora real.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence

Runner = Callable[[Sequence[str]], str]

DEFAULT_TIMEOUT = 60.0


class CommandError(RuntimeError):
    """O comando falhou (código de saída diferente de zero ou timeout)."""

    def __init__(self, args: Sequence[str], message: str) -> None:
        self.command = tuple(args)
        super().__init__(f"{' '.join(self.command)}: {message}")


class CommandNotFoundError(CommandError):
    """O executável não existe no sistema."""


def _environment() -> dict[str, str]:
    env = dict(os.environ)
    # Saída previsível: mensagens e cabeçalhos em inglês, UTF-8.
    env["LC_ALL"] = "C.UTF-8"
    env["LANG"] = "C.UTF-8"
    return env


def run_command(args: Sequence[str], timeout: float = DEFAULT_TIMEOUT) -> str:
    """Executa ``args`` (sem shell) e devolve o stdout.

    Levanta :class:`CommandNotFoundError` se o executável não existir e
    :class:`CommandError` para falhas ou timeout.
    """
    if not args:
        raise ValueError("comando vazio")
    try:
        completed = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_environment(),
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise CommandNotFoundError(args, "executável não encontrado") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(args, f"tempo esgotado após {timeout:.0f}s") from exc
    except OSError as exc:
        raise CommandError(args, str(exc)) from exc

    if completed.returncode != 0:
        stderr = completed.stderr.strip().splitlines()
        detail = stderr[-1] if stderr else "sem mensagem de erro"
        raise CommandError(args, f"código de saída {completed.returncode}: {detail}")
    return completed.stdout
