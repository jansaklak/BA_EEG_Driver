# Sterownik BrainAccess

Kod urządzenia i akwizycji – bez logiki konkretnych eksperymentów:

- `brainaccess_halo/` – BLE, dekoder MINI/HALO, DSP, LSL,
- `cli.py` – polecenia `./halo.sh` (scan, stream, check, plot, lsl, web),
- `web_dashboard.py` – panel WWW: podgląd sygnału, uruchamianie zadań, analizy,
- `task_runner.py` – wykrywanie zadań z `tasks/`, zapis EEG sesji z czasem
  `local_clock`, wpisywanie triggerów do pliku EEG, metadane sesji,
- `live_plot.py` – wykres sygnału na żywo.

Zadania znajdują się w `tasks/` i nie łączą się z czepkiem – zapis EEG prowadzi panel.
