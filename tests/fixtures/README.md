# Fixtures

| File | Origin |
|---|---|
| `dnf5_history_list.json` | **Real** - `dnf5 history list --json` (Fedora 45, dnf5 5.4.6.0), transactions 393-405. |
| `flatpak_list.txt` | **Real** - `flatpak list --app --columns=application` on a terminal (localized header and a duplicated ID, as captured). |
| `dnf5_history_info.json` | **Synthetic** - built from the documented `dnf5 history info --json` schema. The real format is exercised by the `fedora` CI job. |
