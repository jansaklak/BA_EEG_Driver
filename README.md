# 🧠 BrainAccess EEG — Driver for macOS, Windows and Linux

A native, high-performance Python driver for **BrainAccess** EEG headsets from Neurotechnology, including the **BA MINI** and **HALO** models, running on **macOS** (Apple Silicon and Intel), **Windows 10/11** and **Linux** – including where the official `brainaccess` package has no ready-made native libraries (e.g. Apple Silicon Macs).

The driver communicates directly over **Bluetooth Low Energy (GATT)** using the `bleak` library (macOS: CoreBluetooth, Windows: WinRT, Linux: BlueZ), decodes the hardware streams from the 24-bit **ADS1299** converter, filters out mains interference (50 Hz notch, DC drift) and exposes the data in real time.

---

## ⚡ Quick start

Python 3.10+ is required. The first run creates the `.venv` environment and installs the packages automatically.

```bash
./halo.sh web      # macOS / Linux: web panel (EEG preview, tasks with recording, analyses)
halo.bat web       # Windows (cmd or PowerShell: .\halo.bat web)
```

| System | Preparation |
|---|---|
| **macOS** | nothing more; on the first scan, allow Terminal to use Bluetooth. |
| **Windows 10/11** | Python from [python.org](https://www.python.org/downloads/) (check *Add python.exe to PATH*). Bluetooth must be turned on. The firewall may ask about network access for the panel – “private networks” is enough. |
| **Linux** | `sudo apt install python3-venv python3-tk bluez` (Debian/Ubuntu; `python3-tk` only for `./halo.sh plot`), the `bluetooth` service running, user in the `bluetooth` group. For LSL streams also install **liblsl** (a `.deb` package from [github.com/sccn/liblsl/releases](https://github.com/sccn/liblsl/releases) or `conda install -c conda-forge liblsl`) – without it, recording, tasks and analyses work normally; only the LSL streams are disabled. |

1. In the panel click **Connect to headset** (or **Scan BLE** and choose a device).
2. In the **Experimental tasks** card set the parameters and click **▶ Run**.
3. Enter the participant's **ID (pseudonym)** – e.g. `person01`. From this moment everything happens automatically:
   - EEG recording starts,
   - the task opens full screen (SPACE – start, ESC – abort),
   - when the task ends, recording stops and the triggers are written into the EEG file.
4. Click **📊 Analysis** next to the task and enter the same ID – the most recent session
   of that participant is analyzed. PNG plots appear in the panel and in the session directory.
   The **Sessions** button shows all recordings and lets you analyze any of them.

### Session files

Each task run creates a separate directory named `TASK_PSEUDONYM_DATE`:

```text
recordings/FLANKER_person01_20261030-183005/
├── FLANKER_person01_20261030-183005.csv            EEG (µV) + trigger column
├── FLANKER_person01_20261030-183005_events.csv     list of triggers: time, sample number, delay
├── FLANKER_person01_20261030-183005_behavior.csv   responses / task blocks
├── FLANKER_person01_20261030-183005_meta.json      parameters, device, montage, REF/BIAS, synchronization check
├── FLANKER_person01_20261030-183005_task.log       output of the task process
└── ... after analysis: *_erp.png, *_rt.png, *_analysis.json, *_erp.npz
```

The EEG file has the columns `sample_index, lsl_time, Cz, Fp1, …, trigger`. The time of the EEG
samples and the time of the triggers come from the same clock (`pylsl.local_clock`), so nothing
needs to be synchronized – the trigger is written to the first sample whose time is ≥ the event
time, and `_events.csv` gives the exact offset (`offset_ms`). `_meta.json` contains, among other
things, the number of samples, lost BLE packets and warnings (e.g. triggers outside the recording range).

The pseudonym may contain letters, digits and `-` (no spaces or `_`, because `_` separates the name fields).

### Tasks and analyses from the terminal

The panel is recommended, but the same scripts also work manually (without EEG recording – only
triggers and behavior). On Windows use `.venv\Scripts\python` instead of `.venv/bin/python`:

```bash
.venv/bin/python -m tasks.flanker.task --id person01 --trials 40
.venv/bin/python -m tasks.flanker.analysis --id person01          # most recent session of this ID
.venv/bin/python -m tasks.rest.analysis --session REST_person01_20261030-190000
```

Available tasks (`tasks/`):

| Task | What it does | Analysis (PNG) |
|---|---|---|
| `FLANKER` | congruent/incongruent arrows, response ← / → | ERP correct vs incorrect, reaction times and accuracy |
| `REST` | alternating eyes-open / eyes-closed blocks with a sound cue | spectrum and band power, alpha reactivity |
| `MUSIC` | silence / music blocks over headphones, prompts for when to put on and take off the headphones, relaxation ratings | topographic band maps, change heatmap, spectrogram, alpha asymmetry |

A new task = a new directory in `tasks/` (described in [tasks/README.md](tasks/README.md)); the panel
detects it automatically, without a restart.

### Other driver commands

On Windows type `halo.bat` instead of `./halo.sh`. The device address (`--target`) is a UUID on macOS
and a MAC address on Windows and Linux (e.g. `AA:BB:CC:DD:EE:FF`) – you can always give a name instead, e.g. `"BA MINI 002"`.

```bash
./halo.sh scan                   # scan for devices
./halo.sh monitor --auto-connect # wait for the headset to be switched on and connect
./halo.sh stream                 # telemetry in the terminal
./halo.sh check                  # quick signal diagnostics
./halo.sh plot                   # oscilloscope (Matplotlib)
./halo.sh lsl                    # EEG stream over LSL (OpenViBE, LabRecorder)
.venv/bin/python -m pytest tests # tests
```

The tasks also send triggers as an LSL stream (`BrainAccess-Flanker-Markers`,
`BrainAccess-Rest-Markers`), so they can be recorded in parallel in LabRecorder.


---

## 💡 How to switch on and prepare the headset

1. **Switching the device on:**
   - Switch on the headset according to the marking on its casing.
   - Wait until the device starts advertising over Bluetooth Low Energy.
   - The LED should pulse blue (*Breathing Blue*).
2. **Bluetooth pairing:**
   - ⚠️ **DO NOT pair the headset manually in the system Bluetooth settings!**
   - The GATT protocol connects directly from the application/driver. If the headset was added in the system earlier, remove it:
     macOS – *Settings → Bluetooth → “Forget This Device”*; Windows – *Settings → Bluetooth & devices → “Remove device”*;
     Linux – `bluetoothctl remove <address>`.
3. **LED status:**
   - **Pulsing blue:** Ready to connect.
   - **Solid blue:** Connected to the computer.
   - **Fast red flash:** Low battery (<15%).
   - **Pulsing red:** Charging over USB-C.

---

## 📡 Architecture and BLE protocol

The headset transmits data through dedicated GATT services:

- **GATT Control Service:** `ace20bb6-5d39-45de-9f21-b101acf45b9b`
- **Hardware configuration:** `3467e3a2-4511-ae66-7515-b26fce099337`
  - MINI: PGA gain = 8, 8 active channels, 250 Hz
    (`0x00, 8x 0x04, 0xFF, 0x06`)
- **EEG notification stream:** `a12f7c4b-9e8d-6a53-1e0f-c274815b36d7`
- **START/STOP command:** `49beba02-5f6b-4ca2-b3c9-2e6b71aa9392` (`0x01` = start, `0x00` = stop)
- **MINI data format:**
  - The first sample contains 8 channels as signed 24-bit little-endian.
  - The following samples contain signed 12-bit deltas, bit-packed (MSB-first).
  - The footer is 6 or 12 bytes; the counter in the footer indicates the index of the last sample.
  - Voltage scaling: $LSB = \frac{2 \times V_{REF}}{\text{Gain} \times 2^{24}} \approx 0.0671\,\mu\text{V}/\text{count}$.
  - Channels: 8 montage positions: **Cz**, **Fp1**, **Fp2**, **F3**, **F4**,
    **C3**, **C4**, **Oz**.

  In the current physical layout, `REF=P8` and `BIAS=P7`. These are reference
  contacts, not additional EEG channels. Such a montage makes sense for analyzing
  frontal and central activity, but P7/P8 are not a neutral reference point like an
  ear/mastoid; if the headset allows it, A1/A2 or mastoid are usually better as REF/BIAS.

> The position names are only a montage mapping. They can be changed before the
> stream starts, but the decoder always keeps all 8 channels.

### Preview filters and recording

The dashboard lets you turn the preview filter on/off, set the cutoff frequencies of the
band-pass filter and the 50 Hz Notch filter. Changes reset the filter state so that samples
with different settings are not mixed. The filters apply only to the plot, quality and band
power. CSV recordings from the dashboard are saved from raw, decoded samples before DSP.

---

## 🔬 Signal processing pipeline (DSP Pipeline)

The built-in `HaloDSP` module performs the following in real time:
1. **DC drift removal (baseline removal):** IIR high-pass filter ($\tau = 0.5\text{ s}$).
2. **Band-stop filter (50 Hz Notch):** Removing interference from the European mains using second-order sections (SOS).
3. **Band-pass filter (0.5 – 45 Hz):** 4th-order Butterworth filter.
4. **Band power (Welch PSD):**
   - **Delta:** 0.5 – 4.0 Hz (deep sleep)
   - **Theta:** 4.0 – 8.0 Hz (drowsiness, meditation)
   - **Alpha:** 8.0 – 13.0 Hz (calm, closed eyes)
   - **Beta:** 13.0 – 30.0 Hz (active thinking, focus)
   - **Gamma:** 30.0 – 45.0 Hz (high-level cognitive processing)
5. **Eye-blink detection:** Tracking EOG peaks on the frontal channels
   Fp1, Fp2, F3 and F4 and merging them into a single event.
6. **FOOOF spectral parameterization (Fitting Oscillations & One-Over-F):**
   Separating the aperiodic background ($1/f^\chi$) from genuine neural oscillations
   (Gaussian peaks). The model is computed independently for each channel and aggregated
   by the median. The feature provides:
   - the traditional Welch PSD spectrum,
   - the **flattened spectrum** ($\text{PSD} - \text{aperiodic background}$), isolating pure oscillations (e.g. the alpha band),
   - fit quality parameters ($R^2$, error, number of detected peaks).
   Computations are performed in the DSP layer with caching (TTL), and the web panel
   offers a preview mode switch **Regular PSD / FOOOF**.

### Independent online analysis: gaze direction

The [online_analysis.py](./driver/brainaccess_halo/online_analysis.py) module is
separate from the BLE driver. The dashboard passes it only the decoded samples
through a callback. To begin with, it provides a calibrated, experimental
`neutral` / `up` / `down` classifier based on the shared Fp1/Fp2 signal. This is an
approximation of vertical EOG, not an eye tracker; calibration must be performed
after the headset has been put on.

After connecting the device:

```bash
# check the status
curl http://localhost:8420/api/analysis/gaze

# hold each position calmly for 2 seconds
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

The current result is available via `GET /api/analysis/gaze` and in the `gaze` field
of the existing `/ws` WebSocket. The returned fields are `label`, `confidence`, `ready`
and `reason`. Before full calibration the result has `ready=false`; the application
should then ignore the label. The module can later be replaced or extended without
changing [driver.py](./driver/brainaccess_halo/driver.py).

---

## 💻 Usage as a Python library

```python
import asyncio
from driver.brainaccess_halo import HaloDriver

async def main():
    driver = HaloDriver(enable_dsp=True)
    
    # 1. Connect (automatically detects the nearest HALO headset)
    await driver.connect()
    print(f"Connected to {driver.device_name}, battery: {driver.battery_level}%")
    
    # 2. Callback for new samples
    def on_samples(batch):
        for sample in batch:
            print(f"Fp1={sample.fp1:.2f}µV, Fp2={sample.fp2:.2f}µV, O1={sample.o1:.2f}µV, O2={sample.o2:.2f}µV")

    driver.add_callback(on_samples)
    
    # 3. Start the stream
    await driver.start_stream()
    await asyncio.sleep(10)
    
    # 4. Shut down
    await driver.stop_stream()
    await driver.disconnect()

asyncio.run(main())
```

---


## 📂 Directory structure

```text
EEG-project/
├── halo.sh                 # macOS/Linux launcher (creates .venv from requirements.txt on first run)
├── halo.bat                # the same for Windows
├── requirements.txt
├── driver/                 # device and acquisition – no experiment logic
│   ├── cli.py              # ./halo.sh commands
│   ├── web_dashboard.py    # web panel (FastAPI): preview, tasks, analyses
│   ├── task_runner.py      # task discovery, session EEG recording, trigger insertion
│   ├── live_plot.py
│   └── brainaccess_halo/   # BLE, packet decoder, DSP, LSL
├── tasks/
│   ├── common.py           # shared: session file names, triggers, full screen, data loading
│   ├── flanker/            # task.json, task.py, analysis.py
│   ├── music/              # task.json, task.py, analysis.py
│   └── rest/               # task.json, task.py, analysis.py
├── tests/                  # pytest
├── recordings/             # sessions: recordings/TASK_PSEUDONYM_DATE/
├── archiwum/               # old recordings (former format) and a copy of the code from before the cleanup
└── BrainAccess/reference/  # external reference project (not used by the code)
```
