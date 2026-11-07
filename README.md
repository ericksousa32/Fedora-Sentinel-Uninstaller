# Fedora Sentinel Uninstaller (FSU)

Ferramenta inteligente, segura e cirúrgica para o saneamento de dados residuais
no `$HOME` do Fedora Linux: pastas deixadas em `~/.config`, `~/.local/share`,
`~/.cache` e `~/.var/app` por aplicativos RPM e Flatpak já desinstalados.

> **Estado:** em desenvolvimento. A etapa atual (coletor) é **somente leitura**:
> nada é movido nem apagado.

## Requisitos

- Fedora com **DNF5** (Fedora 41+); testado no Fedora 45
- Python 3.10+, sem dependências externas
- Opcional: `flatpak`

## Uso atual (etapa 1 — varredura somente leitura)

```bash
git clone -b dev https://github.com/ericksousa32/Fedora-Sentinel-Uninstaller.git
cd Fedora-Sentinel-Uninstaller
PYTHONPATH=src python3 -m fsu.collector          # tabela
PYTHONPATH=src python3 -m fsu.collector --json   # JSON
```

Execute como **usuário comum** (nunca com `sudo`).

### Como ler o resultado

| Score | Ação permitida (etapas futuras) |
|---|---|
| ≥ 0,70 | Quarentena direta |
| 0,40 – 0,70 | Quarentena somente com snapshot preventivo |
| < 0,40 | Apenas listado |

## Princípios de segurança

- O FSU roda como usuário comum; nunca opera arquivos como root.
- Symlinks nunca são seguidos; montagens não são atravessadas.
- Se uma fonte de verdade (rpm, dnf5, flatpak) falhar, a categoria que depende
  dela é ignorada — nunca tudo vira "órfão".
- Lista de proteção para itens críticos da sessão (dconf, keyrings, autostart,
  Plasma/GNOME, Flatpak, containers…) e para a própria pasta do FSU.

## Desenvolvimento

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -m "not fedora" && ruff check . && ruff format --check . && mypy
```

Os testes marcados com `fedora` rodam contra `rpm`/`dnf5` reais (job de CI em
container `fedora:latest`).

## Licença

GPL-3.0-or-later.
