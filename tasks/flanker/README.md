# Flanker (Eriksen)

Strzałki `<<<<<` / `<<><<` itd.; badany wskazuje kierunek środkowej strzałki (← / →).
Uruchamiaj z panelu WWW (zapis EEG włącza się automatycznie) albo z terminala:

```bash
.venv/bin/python -m tasks.flanker.task --id osoba01 --trials 40 --stimulus-ms 200 --response-window-ms 1200
.venv/bin/python -m tasks.flanker.analysis --id osoba01
```

Przebieg próby: fiksacja 500 ms → strzałki (domyślnie 200 ms) → okno odpowiedzi
(domyślnie 1200 ms od bodźca) → przerwa 0,8–1,2 s.

Triggery: `SESSION_START`, `TRIAL_CONGRUENT` / `TRIAL_INCONGRUENT`, `STIMULUS_ONSET`
(zapisywany zaraz po wyświetleniu klatki), `RESPONSE_LEFT` / `RESPONSE_RIGHT`,
`RESPONSE_CORRECT` / `RESPONSE_INCORRECT` / `RESPONSE_TIMEOUT`, `SESSION_END`.

Analiza (`analysis.py`):

- `*_rt.png` – czasy reakcji (poprawne odpowiedzi) i poprawność dla prób zgodnych/niezgodnych,
  efekt flankera w `*_analysis.json`,
- `*_erp.png`, `*_erp.npz` – ERP `correct` vs `incorrect`, epoki −200…800 ms względem
  `STIMULUS_ONSET`; filtr 0,5–30 Hz, notch 49–51 Hz, liniowa korekcja baseline,
  odrzucanie epok z artefaktami (> 300 µV p-p lub > 250 µV). `--raw` wyłącza czyszczenie.
