"""
BrainAccess EEG — Command Line Interface (macOS, Windows, Linux).
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from .brainaccess_halo import (
    BANDS,
    CHANNELS,
    HaloDSP,
    HaloDriver,
    HaloPacketDecoder,
    HaloRecorder,
    SAMPLE_RATE,
)

# Konsola Windows bywa w cp1250/cp852 – bez tego polskie znaki i emoji kończą się błędem.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("HaloCLI")

LAUNCHER = "halo.bat" if sys.platform == "win32" else "./halo.sh"
PAIRING_HINT = {
    "darwin": "macOS: Ustawienia → Bluetooth → (i) przy urządzeniu → 'Zapomnij to urządzenie'.",
    "win32": "Windows: Ustawienia → Bluetooth i urządzenia → '...' przy urządzeniu → 'Usuń urządzenie'.",
}.get(sys.platform, "Linux: bluetoothctl remove <adres>; usługa bluetooth musi działać (systemctl status bluetooth).")


def make_bar(val: float, max_val: float = 20.0, width: int = 15) -> str:
    """Helper to render a simple ASCII progress bar."""
    ratio = min(1.0, max(0.0, val / max_val))
    filled = int(ratio * width)
    return "█" * filled + "░" * (width - filled)


async def cmd_scan(args):
    """Scan for BrainAccess EEG devices."""
    print(f"\n🔍 Skanowanie urządzeń Bluetooth Low Energy w zasięgu ({args.timeout:g} sek)...")
    devices = await HaloDriver.scan(timeout=args.timeout)

    halo_list = [d for d in devices if d["is_halo"]]
    other_list = [d for d in devices if not d["is_halo"]]

    if halo_list:
        print("\n" + "=" * 60)
        print("🎉 ZNALEZIONO CZEPEK EEG BRAINACCESS:")
        print("=" * 60)
        for h in halo_list:
            print(f"  👉 Nazwa : {h['name']}")
            print(f"     Adres : {h['address']}")
            print(f"     Sygnał: {h['rssi']} dBm")
            print(f"     Powód : {h['match_reason']}")
            print("-" * 60)
        print("\nAby się połączyć, uruchom:")
        print(f"  {LAUNCHER} stream --target \"{halo_list[0]['address']}\"")
    else:
        print("\n⚠️  Nie wykryto czepka EEG BrainAccess.")
        print("\nPorada:")
        print("  1. Upewnij się, że opaska jest włączona:")
        print("     Włącz czepek BrainAccess i odczekaj kilka sekund na reklamowanie BLE.")
        print("     Opaska zawibruje, a dioda LED zacznie 'oddychać' na niebiesko (Breathing Blue).")
        print("  2. Nie paruj opaski ręcznie w ustawieniach Bluetooth systemu!")
        print(f"     {PAIRING_HINT}")
        print("  3. Uruchom monitorowanie ciągłe komendą:")
        print(f"     {LAUNCHER} monitor\n")

    if args.all and other_list:
        print("\nInne wykryte urządzenia BLE:")
        for d in other_list[:10]:
            print(f"  • {d['name']:<25} | {d['address']} | RSSI: {d['rssi']}")


async def cmd_monitor(args):
    """Continuously scan until a BrainAccess EEG device appears."""
    print("\n📡 Ciągłe monitorowanie BLE w toku...")
    print("👉 Włącz teraz czepek BrainAccess (np. BA MINI) i odczekaj na reklamowanie BLE.")
    print("Naciśnij Ctrl+C, aby przerwać.\n")

    iteration = 1
    while True:
        sys.stdout.write(f"\rPróba {iteration}: skanowanie... ")
        sys.stdout.flush()
        devices = await HaloDriver.scan(timeout=3.0)
        halo_list = [d for d in devices if d["is_halo"]]

        if halo_list:
            h = halo_list[0]
            print(f"\n\n🎯 WYKRYTO CZĘPEK EEG BRAINACCESS!")
            print(f"  Nazwa : {h['name']}")
            print(f"  Adres : {h['address']}")
            print(f"  Sygnał: {h['rssi']} dBm")

            if args.auto_connect:
                print("\nAutomatyczne łączenie...")
                args.device = h["device"]
                args.target = h["address"]
                await cmd_stream(args)
                return
            else:
                print("\nAby rozpocząć odbiór danych, uruchom:")
                print(f"  {LAUNCHER} stream --target \"{h['address']}\"\n")
                return

        iteration += 1
        await asyncio.sleep(1.0)


async def cmd_info(args):
    """Connect and print BrainAccess EEG hardware information."""
    driver = HaloDriver(enable_dsp=False)
    target = getattr(args, "device", None) or getattr(args, "target", None)
    print(f"Łączenie z urządzeniem (target={target or 'auto'})...")
    try:
        await driver.connect(target=target)
    except Exception as e:
        print(f"❌ Błąd połączenia: {e}")
        return

    summary = driver.get_summary()
    print("\n" + "=" * 50)
    print("INFORMACJE O CZEPKU EEG BRAINACCESS:")
    print("=" * 50)
    print(f"  Model         : {summary['model']}")
    print(f"  Adres BLE     : {summary['address']}")
    print(f"  Nazwa         : {summary['device_name']}")
    print(f"  Poziom baterii: {summary['battery_level']}%" if summary['battery_level'] else "  Poziom baterii: N/A")
    print(f"  Firmware      : {summary['firmware']}")
    print(f"  Hardware      : {summary['hardware']}")
    print(f"  Nr seryjny    : {summary['serial']}")
    print(f"  Częstotliwość : {summary['sample_rate']} Hz")
    print(f"  Kanały EEG    : {', '.join(summary['channels'])}")
    print("=" * 50 + "\n")

    await driver.disconnect()


async def cmd_stream(args):
    """Connect and stream live EEG to terminal with stats."""
    driver = HaloDriver(enable_dsp=True)
    recorder = None

    is_record = getattr(args, "record", False)
    session_name = getattr(args, "session", None)
    seconds_limit = getattr(args, "seconds", None)
    target = getattr(args, "device", None) or getattr(args, "target", None)

    if is_record:
        recorder = HaloRecorder(session_name=session_name)
        print(f"📁 Rejestrowanie sesji do: {recorder.csv_path}")
        driver.add_callback(recorder.write_samples)

    print(f"Łączenie z czepkiem BrainAccess (cel: {target or 'auto-discovery'})...")
    try:
        await driver.connect(target=target)
    except Exception as e:
        print(f"❌ Błąd połączenia: {e}")
        print("Upewnij się, że czepek jest włączony i widoczny w Bluetooth.")
        return

    if recorder:
        recorder.start(driver.get_summary())

    await driver.start_stream()
    print(
        f"🟢 Strumień EEG aktywny (250 Hz, {driver.profile.channel_count} kanałów: "
        f"{', '.join(driver.channels)})."
    )
    print("Naciśnij Ctrl+C, aby zatrzymać.\n")

    start_time = time.time()
    last_print = 0.0
    last_sample_count = 0

    try:
        while True:
            await asyncio.sleep(0.1)
            now = time.time()

            # Record incoming samples
            if recorder:
                # We can drain or process through callback
                pass

            if now - last_print >= 0.5:
                dt = now - last_print
                samples_in_interval = driver.sample_count - last_sample_count
                hz = samples_in_interval / dt if dt > 0 else 0
                last_sample_count = driver.sample_count
                last_print = now

                # Metrics from DSP
                quality = driver.dsp.get_signal_quality() if driver.dsp else {}
                powers = driver.dsp.get_band_powers() if driver.dsp else {}
                blinks = driver.dsp.detect_eye_blinks() if driver.dsp else 0
                bat = driver.battery_level
                active_channels = driver.channels

                # Clear screen or print block
                sys.stdout.write("\033[2J\033[H")
                print("=" * 65)
                print(f" 🧠 BRAINACCESS EEG — TELEMETRIA NA ŻYWO")
                print("=" * 65)
                print(f" Status: Połączono | Model: {driver.profile.model_name} | Bateria: {bat if bat is not None else 'N/A'}% | Próbkowanie: {hz:.1f} Hz (cel: 250 Hz)")
                print(f" Łącznie próbek: {driver.sample_count} | Utracone pakiety: {driver.decoder.dropped_packets} | Mrugnięcia: {blinks}")
                print("-" * 65)
                print(" Jakość kontaktu elektrod i poziom szumów:")
                advice_list = []
                for ch in active_channels:
                    q = quality.get(ch, {})
                    score = q.get("score_pct", 0)
                    eeg_rms = q.get("eeg_rms", q.get("rms", 0.0))
                    mains = q.get("mains_50hz", 0.0)
                    status = q.get("status", "")
                    icon = "🟢" if score >= 80 else ("🟡" if score >= 60 else ("🟠" if score >= 35 else "🔴"))
                    bar = make_bar(score, max_val=100.0, width=10)
                    print(f"  [{ch:>4}] {score:>3}% [{bar}] {icon} EEG:{eeg_rms:>4.1f}µV | 50Hz:{mains:>4.1f}µV | {status}")
                    if q.get("advice") and score < 75:
                        advice_list.append(f"{ch}: {q['advice']}")
                if advice_list:
                    print("-" * 65)
                    print(" 💡 Wskazówki: " + " | ".join(advice_list))

                print("-" * 65)
                print(" Średnia moc pasm fal mózgowych (µV²):")
                # Average power across all channels for Delta, Theta, Alpha, Beta, Gamma
                for band in BANDS:
                    avg_p = sum(powers.get(ch, {}).get(band, 0.0) for ch in active_channels) / max(1, len(active_channels)) if powers else 0.0
                    bar = make_bar(avg_p, max_val=15.0, width=16)
                    f_lo, f_hi = BANDS[band]
                    print(f"  {band.capitalize():<6} ({f_lo:>4.1f}-{f_hi:<4.1f} Hz): {avg_p:>6.2f}  [{bar}]")

                print("=" * 65)
                print(" Naciśnij Ctrl+C, aby zakończyć.")

            if seconds_limit and (now - start_time) >= seconds_limit:
                print(f"\nOsiągnięto limit czasu ({seconds_limit} s).")
                break

    except KeyboardInterrupt:
        print("\nPrzerwano przez użytkownika.")
    finally:
        await driver.stop_stream()
        if recorder:
            rec_info = recorder.stop()
            print(f"\n💾 Zapisano nagranie:")
            print(f"   CSV: {rec_info['csv_path']}")
            print(f"   Próbek: {rec_info['total_samples']} ({rec_info['duration_sec']} sek)")
        await driver.disconnect()


async def cmd_check(args):
    """Run an automated diagnostic check on the live signal."""
    driver = HaloDriver(enable_dsp=True)
    target = getattr(args, "target", None)
    duration = getattr(args, "seconds", 6.0)

    print(f"\n🩺 Rozpoczynam automatyczną diagnostykę sygnału czepka BrainAccess ({duration} sek)...")
    print("Łączenie z urządzeniem...")
    try:
        await driver.connect(target=target)
    except Exception as e:
        print(f"❌ Błąd połączenia: {e}")
        return

    summary = driver.get_summary()
    print(f"✅ Połączono: {summary['device_name']} (Bateria: {summary['battery_level']}%)")
    print("Zbieranie próbek EEG...")

    active_channels = driver.channels
    raw_data = {ch: [] for ch in active_channels}

    def collect(samples):
        for s in samples:
            for ch in active_channels:
                raw_data[ch].append(s.channels.get(ch, 0.0))

    driver.add_callback(collect)
    await driver.start_stream()

    for i in range(int(duration)):
        await asyncio.sleep(1.0)
        sys.stdout.write(f"\rPostęp: {i + 1}/{int(duration)} s [odebrano próbek: {driver.sample_count}]")
        sys.stdout.flush()

    await driver.stop_stream()
    await driver.disconnect()

    import numpy as np
    print("\n\n" + "=" * 65)
    print(" 📋 RAPORT DIAGNOSTYCZNY SYGNAŁU EEG — BRAINACCESS")
    print("=" * 65)

    first_ch = active_channels[0] if active_channels else None
    n_samples = len(raw_data[first_ch]) if first_ch else 0
    real_hz = n_samples / duration if duration > 0 else 0
    hz_ok = "✅ OK" if 230 <= real_hz <= 265 else "⚠️ Odchyłka"
    drop_ok = "✅ 0%" if driver.decoder.dropped_packets == 0 else "⚠️ Wystąpiły straty"

    print(f" Urządzenie                : {summary['device_name']} ({summary['model']})")
    print(f" Kanały                    : {', '.join(active_channels)} ({len(active_channels)})")
    print(f" Częstotliwość próbkowania : {real_hz:.1f} Hz (wzorcowa: 250.0 Hz) {hz_ok}")
    print(f" Utracone pakiety BLE      : {driver.decoder.dropped_packets} {drop_ok}")
    print("-" * 65)
    print(" OCENA KONTAKTU ELEKTROD (Amplituda i szum):")

    all_ok = True
    for ch in active_channels:
        arr = np.array(raw_data[ch])
        if len(arr) == 0:
            print(f"  [{ch:>4}] Brak danych")
            continue
        arr_centered = arr - np.median(arr)
        rms = float(np.sqrt(np.mean(arr_centered ** 2)))
        p2p = float(np.ptp(arr_centered))

        if rms < 1.5:
            verdict = "❌ BRAK SYGNAŁU (Zwarcie lub brak kontaktu)"
            all_ok = False
        elif rms > 110.0:
            verdict = "⚠️ DUŻY SZUM (Popraw styk ze skórą, odsuń włosy)"
            all_ok = False
        elif 4.0 <= rms <= 45.0:
            verdict = "✅ WZOROWY (Prawidłowy spoczynkowy sygnał EEG)"
        else:
            verdict = "🟡 AKCEPTOWALNY (Możliwe napięcie mięśni / ruch)"

        print(f"  [{ch:>4}] RMS: {rms:>5.1f} µV | Pk-Pk: {p2p:>6.1f} µV  --> {verdict}")

    print("-" * 65)
    powers = driver.dsp.get_band_powers() if driver.dsp else {}
    print(" WIDMO CZĘSTOTLIWOŚCIOWE (Średnia moc pasm):")
    for band in BANDS:
        avg_p = sum(powers.get(ch, {}).get(band, 0.0) for ch in active_channels) / max(1, len(active_channels)) if powers else 0.0
        f_lo, f_hi = BANDS[band]
        print(f"  {band.capitalize():<6} ({f_lo:>4.1f}-{f_hi:<4.1f} Hz): {avg_p:>6.2f} µV²")

    print("=" * 65)
    if all_ok:
        print("🎉 WYNIK: Wszystkie kanały odbierają prawidłowy sygnał biologiczny!")
    else:
        print("💡 WSKAZÓWKA: W przypadku kanałów potylicznych O1/O2 rozchyl włosy, by elektrody dotykały skóry głowy.")
    print("=" * 65 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Sterownik czepka EEG BrainAccess (macOS, Windows, Linux).")
    subparsers = parser.add_subparsers(dest="command", help="Dostępne polecenia")

    # check
    p_chk = subparsers.add_parser("check", help="Przeprowadź automatyczny test jakości sygnału (diagnostyka)")
    p_chk.add_argument("--target", type=str, default=None, help="Adres lub nazwa urządzenia")
    p_chk.add_argument("--seconds", type=float, default=6.0, help="Czas testu (domyślnie 6 sekund)")

    # scan
    p_scan = subparsers.add_parser("scan", help="Skanuj urządzenia BLE w zasięgu")
    p_scan.add_argument("--timeout", type=float, default=6.0, help="Czas skanowania (sekundy)")
    p_scan.add_argument("--all", action="store_true", help="Pokaż wszystkie urządzenia BLE")

    # monitor
    p_mon = subparsers.add_parser("monitor", help="Ciągłe skanowanie w oczekiwaniu na włączenie opaski")
    p_mon.add_argument("--auto-connect", action="store_true", help="Połącz automatycznie po wykryciu")
    p_mon.add_argument("--record", action="store_true", help="Zapisuj do pliku CSV po połączeniu")
    p_mon.add_argument("--session", type=str, default=None, help="Nazwa sesji do pliku")
    p_mon.add_argument("--seconds", type=float, default=None, help="Czas trwania (domyślnie bez limitu)")

    # info
    p_info = subparsers.add_parser("info", help="Wyświetl informacje o sprzęcie i baterii")
    p_info.add_argument("--target", type=str, default=None, help="Adres lub nazwa urządzenia")

    # stream
    p_stream = subparsers.add_parser("stream", help="Strumieniowanie EEG na żywo w terminalu")
    p_stream.add_argument("--target", type=str, default=None, help="Adres lub nazwa urządzenia")
    p_stream.add_argument("--seconds", type=float, default=None, help="Czas trwania (domyślnie bez limitu)")
    p_stream.add_argument("--record", action="store_true", help="Zapisuj do pliku CSV")
    p_stream.add_argument("--session", type=str, default=None, help="Nazwa sesji do pliku")

    # plot
    p_plot = subparsers.add_parser("plot", help="Uruchom wykres na żywo (Matplotlib GUI)")
    p_plot.add_argument("--target", type=str, default=None, help="Adres lub nazwa urządzenia")

    # lsl
    p_lsl = subparsers.add_parser("lsl", help="Udostępnij strumień EEG przez Lab Streaming Layer (LSL)")
    p_lsl.add_argument("--target", type=str, default=None, help="Adres lub nazwa urządzenia")
    p_lsl.add_argument("--name", type=str, default="BrainAccess-EEG", help="Nazwa strumienia LSL")

    # web
    p_web = subparsers.add_parser("web", help="Panel WWW: podgląd EEG, uruchamianie zadań z zapisem i analizy")
    p_web.add_argument("--port", type=int, default=8420, help="Port serwera (domyślnie 8420)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "check":
        asyncio.run(cmd_check(args))
    elif args.command == "scan":
        asyncio.run(cmd_scan(args))
    elif args.command == "monitor":
        asyncio.run(cmd_monitor(args))
    elif args.command == "info":
        asyncio.run(cmd_info(args))
    elif args.command == "stream":
        asyncio.run(cmd_stream(args))
    elif args.command == "plot":
        from .live_plot import run_live_plot
        asyncio.run(run_live_plot(args.target))
    elif args.command == "lsl":
        async def run_lsl():
            from .brainaccess_halo import HaloDriver, HaloLSLStreamer
            driver = HaloDriver(enable_dsp=False)
            print(f"Łączenie z czepkiem BrainAccess (cel={args.target or 'auto'})...")
            await driver.connect(target=args.target)
            lsl = HaloLSLStreamer(driver, stream_name=args.name)
            lsl.start_lsl()
            await driver.start_stream()
            print(f"🟢 Nadawanie LSL '{args.name}' aktywne na porcie sieciowym! Naciśnij Ctrl+C aby zatrzymać.")
            try:
                while True:
                    await asyncio.sleep(1.0)
            except KeyboardInterrupt:
                print("\nZatrzymywanie...")
            finally:
                lsl.stop_lsl()
                await driver.stop_stream()
                await driver.disconnect()
        asyncio.run(run_lsl())
    elif args.command == "web":
        from .web_dashboard import run_web
        run_web(args.port)



if __name__ == "__main__":
    main()
