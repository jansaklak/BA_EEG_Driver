# Sterownik BrainAccess

Kod urządzenia i akwizycji – bez logiki konkretnych eksperymentów:

- `brainaccess_halo/` – BLE, dekoder MINI/HALO, DSP, LSL,
- `cli.py` – polecenia `./halo.sh` (scan, stream, check, plot, lsl, web),
- `web_dashboard.py` – panel WWW: podgląd sygnału, uruchamianie zadań, analizy,
- `task_runner.py` – wykrywanie zadań z `tasks/`, zapis EEG sesji z czasem
  `local_clock`, wpisywanie triggerów do pliku EEG, metadane sesji,
- `live_plot.py` – wykres sygnału na żywo.

Zadania znajdują się w `tasks/` i nie łączą się z czepkiem – zapis EEG prowadzi panel.

Panel pozwala wybrać **250 Hz lub 500 Hz** w polu „Próbkowanie” przed
połączeniem z czepkiem. Aby zmienić częstotliwość, rozłącz urządzenie i połącz
je ponownie. Wybór jest zapamiętywany w przeglądarce. Domyślne ustawienie
serwera można podać jako `./halo.sh web --sample-rate 500`.

Test sprzętu: `./halo.sh check --sample-rate 500 --seconds 20`.
Opcja `--sample-rate` działa też w poleceniach `stream`, `monitor`, `plot`
i `lsl`. Filtry, okna analizy, LSL, metadane nagrań i znaczniki czasu zadań
korzystają z wybranej częstotliwości.

Kody konfiguracji: 250 Hz = `0x06`, 500 Hz = `0x05`
([StreamRate producenta](https://www.brainaccess.ai/documentation/python-api/4.0.0/content/api/brainaccess/core/stream_rate/index.html)).

Panel pokazuje zmierzone tempo odbioru próbek w Hz. Po 3 sekundach bez nowych
próbek wyświetla komunikat „Brak nowych próbek EEG”; zerwanie połączenia BLE
zmienia status na „Rozłączony”. Jeśli ostrzeżenie nie znika, sprawdź zasilanie
i zasięg czepka, następnie rozłącz go i połącz ponownie w panelu.

Dopasowanie FOOOF działa w tle na kopii danych, a panel wyświetla ostatni
gotowy wynik. Bufor podglądu przechowuje do 5 sekund najnowszych próbek,
także gdy karta przeglądarki jest zamknięta. Limit dotyczy podglądu; zapis
sesji otrzymuje wszystkie odebrane paczki próbek.
