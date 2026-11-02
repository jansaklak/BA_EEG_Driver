# 🧠 BrainAccess EEG — Sterownik dla macOS, Windows i Linux

Natywny, wydajny sterownik w Pythonie dla czepków EEG **BrainAccess** firmy Neurotechnology, w tym modeli **BA MINI** i **HALO**, działający na **macOS** (Apple Silicon i Intel), **Windows 10/11** i **Linuksie** – także tam, gdzie oficjalny pakiet `brainaccess` nie ma gotowych bibliotek natywnych (np. Mac z Apple Silicon).

Sterownik komunikuje się bezpośrednio przez protokół **Bluetooth Low Energy (GATT)** przez bibliotekę `bleak` (macOS: CoreBluetooth, Windows: WinRT, Linux: BlueZ), dekoduje sprzętowe strumienie z 24-bitowego przetwornika **ADS1299**, filtruje zakłócenia sieciowe (Notch 50 Hz, DC drift) i udostępnia dane w czasie rzeczywistym.

---

## ⚡ Szybki start

Wymagany Python 3.10+. Pierwsze uruchomienie samo tworzy środowisko `.venv` i instaluje pakiety.

```bash
./halo.sh web      # macOS / Linux: panel WWW (podgląd EEG, zadania z zapisem, analizy)
halo.bat web       # Windows (cmd lub PowerShell: .\halo.bat web)
```

| System | Przygotowanie |
|---|---|
| **macOS** | nic więcej; przy pierwszym skanowaniu zezwól Terminalowi na Bluetooth. |
| **Windows 10/11** | Python z [python.org](https://www.python.org/downloads/) (zaznacz *Add python.exe to PATH*). Bluetooth musi być włączony. Zapora może zapytać o dostęp do sieci dla panelu – wystarczy „sieci prywatne”. |
| **Linux** | `sudo apt install python3-venv python3-tk bluez` (Debian/Ubuntu; `python3-tk` tylko dla `./halo.sh plot`), usługa `bluetooth` uruchomiona, użytkownik w grupie `bluetooth`. Dla strumieni LSL doinstaluj **liblsl** (pakiet `.deb` z [github.com/sccn/liblsl/releases](https://github.com/sccn/liblsl/releases) albo `conda install -c conda-forge liblsl`) – bez niej zapis, zadania i analizy działają normalnie, wyłączone są tylko strumienie LSL. |

1. W panelu kliknij **Połącz z czepkiem** (albo **Skanuj BLE** i wybierz urządzenie).
2. W karcie **Zadania eksperymentalne** ustaw parametry i kliknij **▶ Uruchom**.
3. Podaj **ID (pseudonim)** badanego – np. `osoba01`. Od tej chwili wszystko dzieje się samo:
   - startuje zapis EEG,
   - otwiera się zadanie na pełnym ekranie (SPACJA – start, ESC – przerwanie),
   - po zakończeniu zadania zapis się zatrzymuje, a triggery są wpisywane do pliku EEG.
4. Kliknij **📊 Analiza** przy zadaniu i podaj to samo ID – analizowana jest najnowsza sesja
   tego badanego. Wykresy PNG pojawiają się w panelu i w katalogu sesji.
   Przycisk **Sesje** pokazuje wszystkie nagrania i pozwala przeanalizować dowolne z nich.

### Pliki sesji

Każde uruchomienie zadania tworzy osobny katalog o nazwie `ZADANIE_PSEUDONIM_DATA`:

```text
recordings/FLANKER_osoba01_20261030-183005/
├── FLANKER_osoba01_20261030-183005.csv            EEG (µV) + kolumna trigger
├── FLANKER_osoba01_20261030-183005_events.csv     lista triggerów: czas, nr próbki, opóźnienie
├── FLANKER_osoba01_20261030-183005_behavior.csv   odpowiedzi / bloki zadania
├── FLANKER_osoba01_20261030-183005_meta.json      parametry, urządzenie, montaż, REF/BIAS, kontrola synchronizacji
├── FLANKER_osoba01_20261030-183005_task.log       wyjście procesu zadania
└── ... po analizie: *_erp.png, *_rt.png, *_analysis.json, *_erp.npz
```

Plik EEG ma kolumny `sample_index, lsl_time, Cz, Fp1, …, trigger`. Czas próbek EEG i czas
triggerów pochodzą z tego samego zegara (`pylsl.local_clock`), więc nie trzeba niczego
synchronizować – trigger jest wpisany do pierwszej próbki o czasie ≥ czas zdarzenia, a
`_events.csv` podaje dokładne przesunięcie (`offset_ms`). `_meta.json` zawiera m.in. liczbę
próbek, utracone pakiety BLE i ostrzeżenia (np. triggery poza zakresem nagrania).

Pseudonim może zawierać litery, cyfry i `-` (bez spacji i `_`, bo `_` rozdziela pola nazwy).

### Zadania i analizy z terminala

Panel jest zalecany, ale te same skrypty działają ręcznie (bez zapisu EEG – tylko triggery
i zachowanie). Na Windows zamiast `.venv/bin/python` użyj `.venv\Scripts\python`:

```bash
.venv/bin/python -m tasks.flanker.task --id osoba01 --trials 40
.venv/bin/python -m tasks.flanker.analysis --id osoba01          # najnowsza sesja tego ID
.venv/bin/python -m tasks.rest.analysis --session REST_osoba01_20261030-190000
```

Dostępne zadania (`tasks/`):

| Zadanie | Co robi | Analiza (PNG) |
|---|---|---|
| `FLANKER` | strzałki zgodne/niezgodne, odpowiedź ← / → | ERP correct vs incorrect, czasy reakcji i poprawność |
| `REST` | naprzemienne bloki oczy otwarte / zamknięte z sygnałem dźwiękowym | widmo i moc pasm, reaktywność alfa |
| `MUSIC` | bloki cisza / muzyka w słuchawkach, polecenia kiedy założyć i zdjąć słuchawki, oceny relaksu | mapy topograficzne pasm, heatmapa zmian, spektrogram, asymetria alfa |

Nowe zadanie = nowy katalog w `tasks/` (opis w [tasks/README.md](tasks/README.md)); panel
wykrywa go automatycznie, bez restartu.

### Pozostałe polecenia sterownika

Na Windows zamiast `./halo.sh` wpisz `halo.bat`. Adres urządzenia (`--target`) na macOS to UUID,
na Windows i Linuksie adres MAC (np. `AA:BB:CC:DD:EE:FF`) – zawsze można też podać nazwę, np. `"BA MINI 002"`.

```bash
./halo.sh scan                   # skanowanie urządzeń
./halo.sh monitor --auto-connect # czekaj na włączenie czepka i połącz
./halo.sh stream                 # telemetria w terminalu
./halo.sh check                  # szybka diagnostyka sygnału
./halo.sh plot                   # oscyloskop (Matplotlib)
./halo.sh lsl                    # strumień EEG przez LSL (OpenViBE, LabRecorder)
.venv/bin/python -m pytest tests # testy
```

Zadania wysyłają też triggery jako strumień LSL (`BrainAccess-Flanker-Markers`,
`BrainAccess-Rest-Markers`), więc można je równolegle nagrywać w LabRecorder.


---

## 💡 Jak włączyć i przygotować czepek BrainAccess

1. **Włączenie urządzenia:**
   - Włącz czepek zgodnie z oznaczeniem na jego obudowie.
   - Poczekaj, aż urządzenie zacznie reklamować się przez Bluetooth Low Energy.
   - Dioda LED powinna pulsować na niebiesko (*Breathing Blue*).
2. **Parowanie Bluetooth:**
   - ⚠️ **NIE paruj opaski ręcznie w ustawieniach Bluetooth systemu!**
   - Protokół GATT łączy się bezpośrednio z poziomu aplikacji/sterownika. Jeśli opaska została wcześniej dodana w systemie, usuń ją:
     macOS – *Ustawienia → Bluetooth → „Zapomnij to urządzenie”*; Windows – *Ustawienia → Bluetooth i urządzenia → „Usuń urządzenie”*;
     Linux – `bluetoothctl remove <adres>`.
3. **Stan diody LED:**
   - **Pulsujący niebieski:** Gotowa do połączenia.
   - **Ciągły niebieski:** Połączono z komputerem.
   - **Szybki czerwony błysk:** Niski stan baterii (<15%).
   - **Pulsujący czerwony:** Ładowanie przez USB-C.

---

## 📡 Architektura i protokół BLE

Opaska przesyła dane za pomocą dedykowanych serwisów GATT:

- **GATT Control Service:** `ace20bb6-5d39-45de-9f21-b101acf45b9b`
- **Konfiguracja sprzętowa:** `3467e3a2-4511-ae66-7515-b26fce099337`
  - MINI: wzmocnienie PGA = 8, 8 aktywnych kanałów, 250 Hz
    (`0x00, 8x 0x04, 0xFF, 0x06`)
- **Strumień powiadomień EEG:** `a12f7c4b-9e8d-6a53-1e0f-c274815b36d7`
- **Komenda START/STOP:** `49beba02-5f6b-4ca2-b3c9-2e6b71aa9392` (`0x01` = start, `0x00` = stop)
- **Format danych MINI:**
  - Pierwsza próbka zawiera 8 kanałów jako signed 24-bit little-endian.
  - Kolejne próbki zawierają signed 12-bit deltas spakowane bitowo (MSB-first).
  - Stopka ma 6 albo 12 bajtów; licznik w stopce wskazuje indeks ostatniej próbki.
  - Skalowanie napięcia: $LSB = \frac{2 \times V_{REF}}{\text{Gain} \times 2^{24}} \approx 0.0671\,\mu\text{V}/\text{count}$.
  - Kanały: 8 pozycji montażu: **Cz**, **Fp1**, **Fp2**, **F3**, **F4**,
    **C3**, **C4**, **Oz**.

  W aktualnym układzie fizycznym `REF=P8`, `BIAS=P7`. Są to styki
  referencyjne, a nie dodatkowe kanały EEG. Taki montaż ma sens do analizy
  aktywności czołowej i centralnej, ale P7/P8 nie są neutralnym punktem
  odniesienia tak jak ucho/mastoid; jeśli czepek to umożliwia, A1/A2 lub
  mastoid są zwykle lepsze jako REF/BIAS.

> Nazwy pozycji są tylko mapowaniem montażu. Można je zmienić przed
> rozpoczęciem strumienia, ale dekoder zawsze zachowuje pełne 8 kanałów.

### Filtry podglądu i zapis

Dashboard pozwala włączać/wyłączać filtr podglądu, ustawiać częstotliwości
graniczne filtra pasmowego oraz filtr Notch 50 Hz. Zmiany resetują stan filtrów
żeby nie mieszać próbek z różnymi ustawieniami. Filtry dotyczą wyłącznie
wykresu, jakości i mocy pasm. Nagrania CSV z dashboardu są zapisywane z
surowych, zdekodowanych próbek przed DSP.

---

## 🔬 Potok przetwarzania sygnału (DSP Pipeline)

Wbudowany moduł `HaloDSP` wykonuje w czasie rzeczywistym:
1. **Usuwanie dryfu DC (Baseline removal):** Filtr górnoprzepustowy IIR ($\tau = 0.5\text{ s}$).
2. **Filtr pasmowo-zaporowy (50 Hz Notch):** Wycinanie zakłóceń z europejskiej sieci elektroenergetycznej z wykorzystaniem sekcji drugiego rzędu (SOS).
3. **Filtr pasmowoprzepustowy (0.5 – 45 Hz):** 4-rzędowy filtr Butterwortha.
4. **Moc pasm (PSD Welch):**
   - **Delta:** 0.5 – 4.0 Hz (głęboki sen)
   - **Theta:** 4.0 – 8.0 Hz (senność, medytacja)
   - **Alpha:** 8.0 – 13.0 Hz (spokój, zamknięte oczy)
   - **Beta:** 13.0 – 30.0 Hz (aktywne myślenie, skupienie)
   - **Gamma:** 30.0 – 45.0 Hz (wysokie przetwarzanie poznawcze)
5. **Detektor mrugnięć (Eye-Blink Detection):** Śledzenie pików EOG na
   kanałach czołowych Fp1, Fp2, F3 i F4 oraz ich łączenie w jedno zdarzenie.
6. **Parametryzacja widma FOOOF (Fitting Oscillations & One-Over-F):**
   Rozdzielenie tła aperiodycznego ($1/f^\chi$) od rzeczywistych oscylacji neuronalnych
   (piki gaussowskie). Model liczony jest niezależnie dla poszczególnych kanałów i agregowany
   medianą. Funkcja udostępnia:
   - tradycyjne widmo Welch PSD,
   - **widmo spłaszczone** ($\text{PSD} - \text{tło aperiodyczne}$), izolujące czyste oscylacje (np. pasmo alfa),
   - parametry jakości dopasowania ($R^2$, błąd, liczba wykrytych pików).
   Obliczenia wykonywane są w warstwie DSP z buforowaniem cache (TTL), a w panelu WWW
   dostępny jest przełącznik trybu podglądu **Zwykłe PSD / FOOOF**.

### Niezależna analiza online: kierunek spojrzenia

Moduł [online_analysis.py](./driver/brainaccess_halo/online_analysis.py) jest
oddzielony od sterownika BLE. Dashboard podaje mu tylko zdekodowane próbki
przez callback. Na początek udostępnia kalibrowany, eksperymentalny klasyfikator
`neutral` / `up` / `down` oparty o wspólny sygnał Fp1/Fp2. To jest przybliżenie
pionowego EOG, a nie eye-tracker; kalibrację trzeba wykonać po założeniu czepka.

Po połączeniu urządzenia:

```bash
# sprawdź stan
curl http://localhost:8420/api/analysis/gaze

# każdą pozycję wykonuj spokojnie przez 2 sekundy
curl -X POST http://localhost:8420/api/analysis/gaze/calibration \
  -H 'Content-Type: application/json' -d '{"label":"neutral","seconds":2}'
curl -X POST http://localhost:8420/api/analysis/gaze/calibration/stop

curl -X POST http://localhost:8420/api/analysis/gaze/calibration \
  -H 'Content-Type: application/json' -d '{"label":"up","seconds":2}'
curl -X POST http://localhost:8420/api/analysis/gaze/calibration/stop

curl -X POST http://localhost:8420/api/analysis/gaze/calibration \
  -H 'Content-Type: application/json' -d '{"label":"down","seconds":2}'
curl -X POST http://localhost:8420/api/analysis/gaze/calibration/stop
```

Aktualny wynik jest dostępny przez `GET /api/analysis/gaze` oraz w polu
`gaze` istniejącego WebSocket `/ws`. Zwracane są `label`, `confidence`, `ready`
i `reason`. Przed pełną kalibracją wynik ma `ready=false`; aplikacja powinna
wtedy ignorować etykietę. Moduł można później wymienić lub rozszerzyć bez
zmiany [driver.py](./driver/brainaccess_halo/driver.py).

---

## 💻 Użycie jako biblioteka Python

```python
import asyncio
from driver.brainaccess_halo import HaloDriver

async def main():
    driver = HaloDriver(enable_dsp=True)
    
    # 1. Połączenie (automatyczne wykrycie najbliższej opaski HALO)
    await driver.connect()
    print(f"Połączono z {driver.device_name}, bateria: {driver.battery_level}%")
    
    # 2. Callback na nowe próbki
    def on_samples(batch):
        for sample in batch:
            print(f"Fp1={sample.fp1:.2f}µV, Fp2={sample.fp2:.2f}µV, O1={sample.o1:.2f}µV, O2={sample.o2:.2f}µV")

    driver.add_callback(on_samples)
    
    # 3. Uruchomienie strumienia
    await driver.start_stream()
    await asyncio.sleep(10)
    
    # 4. Zakończenie
    await driver.stop_stream()
    await driver.disconnect()

asyncio.run(main())
```

---


## 📂 Struktura katalogów

```text
EEG-project/
├── halo.sh                 # launcher macOS/Linux (tworzy .venv z requirements.txt przy pierwszym uruchomieniu)
├── halo.bat                # to samo dla Windows
├── requirements.txt
├── driver/                 # urządzenie i akwizycja – bez logiki eksperymentów
│   ├── cli.py              # polecenia ./halo.sh
│   ├── web_dashboard.py    # panel WWW (FastAPI): podgląd, zadania, analizy
│   ├── task_runner.py      # wykrywanie zadań, zapis EEG sesji, wpisywanie triggerów
│   ├── live_plot.py
│   └── brainaccess_halo/   # BLE, dekoder pakietów, DSP, LSL
├── tasks/
│   ├── common.py           # wspólne: nazwy plików sesji, triggery, pełny ekran, wczytywanie danych
│   ├── flanker/            # task.json, task.py, analysis.py
│   ├── music/              # task.json, task.py, analysis.py
│   └── rest/               # task.json, task.py, analysis.py
├── tests/                  # pytest
├── recordings/             # sesje: recordings/ZADANIE_PSEUDONIM_DATA/
├── archiwum/               # stare nagrania (dawny format) i kopia kodu sprzed porządków
└── BrainAccess/reference/  # zewnętrzny projekt referencyjny (nieużywany przez kod)
```
