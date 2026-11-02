# REST — oczy otwarte i zamknięte

Naprzemienne bloki: otwarte, zamknięte, otwarte… Na początku każdego bloku i na końcu
sesji odtwarzany jest krótki dźwięk (przy zamkniętych oczach badany wie, kiedy je otworzyć).

```bash
.venv/bin/python -m tasks.rest.task --id osoba01 --blocks 6 --block-seconds 30
.venv/bin/python -m tasks.rest.analysis --id osoba01
```

Triggery: `REST_SESSION_START`, `REST_EYES_OPEN`, `REST_EYES_CLOSED`, `REST_SESSION_END`.
Granice bloków (czas LSL) są w `*_behavior.csv`.

Analiza (`analysis.py`) pomija pierwsze 2 s każdego bloku i liczy widmo Welcha (okno 2 s):

- `*_spectrum.png` – średnie widmo open vs closed i moc pasm δ θ α β γ wg kanału,
- `*_alpha.png` – stosunek mocy alfa zamknięte/otwarte (reaktywność alfa, oczekiwane > 1,
  zwłaszcza na Oz),
- `*_rest.npz`, `*_analysis.json` – dane liczbowe.
