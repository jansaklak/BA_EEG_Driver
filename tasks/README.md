# Zadania eksperymentalne

Każde zadanie to katalog w `tasks/`. Panel WWW (`./halo.sh web`) wykrywa je
automatycznie – wystarczy dodać katalog, bez restartu serwera.

```text
tasks/
├── common.py            # wspólne narzędzia (nazwy plików, triggery, pełny ekran, wczytywanie)
└── moje_zadanie/
    ├── __init__.py
    ├── task.json        # opis dla panelu: kod, nazwa, parametry formularza
    ├── task.py          # procedura (pygame, pełny ekran)
    ├── analysis.py      # analiza (opcjonalna) – zapisuje PNG/JSON w katalogu sesji
    └── README.md
```

## task.json

```json
{
  "code": "MOJE",
  "name": "Moje zadanie",
  "description": "Krótki opis widoczny w panelu.",
  "params": [
    {"name": "trials", "label": "Liczba prób", "type": "int", "default": 40, "min": 1, "max": 500}
  ]
}
```

`code` trafia do nazwy plików (`MOJE_osoba01_20261030-183005`). Każdy parametr
jest przekazywany do `task.py` jako `--<name> <wartość>`.

## Kontrakt task.py

Panel uruchamia: `python -m tasks.<katalog>.task --session-dir <katalog> --id <ID> [parametry]`.

- Użyj `add_session_arguments()` i `prepare_session()` z `tasks.common`.
- Triggery wysyłaj przez `MarkerLog(paths.events).push("NAZWA")` – zapisuje czas
  `local_clock`, ten sam co próbki EEG. Zwrócony czas zapisz w `_behavior.csv`.
- Dane zachowania zapisz do `paths.behavior` (także po przerwaniu – blok `finally`).
- Okno: `open_fullscreen()`, w pętli zdarzeń `check_abort()` (ESC kończy zadanie).
- Kod wyjścia: `0` – zakończone, `2` (`EXIT_ABORTED`) – przerwane.

Zapis EEG, wpisanie triggerów do pliku EEG i metadane robi panel
(`driver/task_runner.py`) – zadanie nie łączy się z czepkiem.

## Kontrakt analysis.py

Panel uruchamia: `python -m tasks.<katalog>.analysis --session <katalog sesji>`.
Z terminala wystarczy `--id <ID>` (najnowsza sesja). Użyj `resolve_session()`,
`load_eeg()`, `load_behavior()` i zapisuj wyniki przez `paths.output("_cos.png")` –
panel pokaże wszystkie pliki PNG z katalogu sesji.
