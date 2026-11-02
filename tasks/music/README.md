# MUSIC — cisza vs muzyka w słuchawkach

Naprzemienne bloki spoczynkowe: cisza, muzyka, cisza, muzyka… Muzykę odtwarza badany
(np. z telefonu). **Zalecane słuchawki douszne** – pałąk nauszników dociska elektrody
Cz/C3/C4 i zmienia sygnał niezależnie od muzyki.

```bash
.venv/bin/python -m tasks.music.task --id osoba01 --blocks 6 --block-seconds 60
.venv/bin/python -m tasks.music.analysis --id osoba01
```

Przebieg każdego bloku:

1. Polecenie na ekranie: **ZAŁÓŻ SŁUCHAWKI I WŁĄCZ MUZYKĘ** albo **WYŁĄCZ MUZYKĘ I ZDEJMIJ
   SŁUCHAWKI** – badany potwierdza SPACJĄ, gdy jest gotowy.
2. 5 s odliczania na uspokojenie po ruchu rąk (poza analizą).
3. Blok: krzyżyk fiksacji, oczy otwarte (domyślnie 60 s).
4. Ocena relaksu 1–5, a po muzyce także ocena muzyki 1–5.

Triggery: `MUSIC_SESSION_START`, `MUSIC_SILENCE_START`, `MUSIC_MUSIC_START`,
`MUSIC_BLOCK_END`, `MUSIC_SESSION_END`. Granice bloków i oceny są w `*_behavior.csv`.

Analiza (`analysis.py`): filtr 1–40 Hz + notch 50 Hz, okna 2 s z krokiem 1 s (bez pierwszych
2 s bloku), kanał z artefaktem > 200 µV p-p jest pomijany w danym oknie (`--artifact-ptp`).
Warunki porównywane są medianą mocy z czystych okien.

- `*_topomap.png` – mapy głowy (heatmapy) mocy δ θ α β γ: cisza, muzyka, zmiana %,
- `*_heatmap.png` – kanał × pasmo: zmiana % mocy bezwzględnej (z wielkością efektu d)
  i względnej (pasmo / 1–40 Hz, mniej wrażliwa na kontakt elektrod),
- `*_timecourse.png` – spektrogram całej sesji i przebieg mocy względnej θ/α/β z blokami,
- `*_indices.png` – asymetria alfa czołowa ln(F4) − ln(F3), indeks zaangażowania
  β/(α+θ) w blokach, relaks deklarowany vs alfa,
- `*_music.npz`, `*_analysis.json` – dane liczbowe (także p Manna-Whitneya, orientacyjnie:
  okna nie są niezależne).
