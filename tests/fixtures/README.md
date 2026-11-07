# Fixtures

| Arquivo | Origem |
|---|---|
| `dnf5_history_list.json` | **Real** — `dnf5 history list --json` (Fedora 45, dnf5 5.4.6.0), transações 393–405. |
| `flatpak_list.txt` | **Real** — `flatpak list --app --columns=application` no terminal (cabeçalho traduzido e ID duplicado, como veio). |
| `dnf5_history_info.json` | **Sintética** — montada a partir do esquema documentado de `dnf5 history info --json`. Deve ser substituída por uma saída real (ver issue/etapa 1). |
