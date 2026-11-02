"""Zadanie SSVEP — gra: patrzysz na kwadrat, punkt jedzie w jego stronę.

Cztery kwadraty (góra, dół, lewo, prawo) migają z RÓŻNYMI częstotliwościami.
Na środku ekranu jest punkt. Badany patrzy na kwadrat z zieloną ramką (cel);
EEG z potylicy jest analizowane na żywo (CCA, okno przesuwne), a punkt
przesuwa się w stronę kwadratu, na który — według dekodera — patrzy badany.
Próba kończy się, gdy punkt dotknie któregokolwiek kwadratu (trafiony cel =
poprawnie, inny = błędnie) albo po upływie czasu próby.

Źródło EEG (zadanie nie łączy się z opaską — robi to panel):
  1. plik roboczy sesji `*_eeg.part.csv`, który panel zapisuje na żywo,
  2. strumień LSL typu "EEG" (jeśli ktoś go wystawia).
  --input auto  (domyślnie) 1 → 2 → a gdy brak danych: SYMULACJA
  --input eeg   jak auto, ale bez symulacji (brak danych = błąd)
  --input file / lsl   wymusza jedno ze źródeł
  --input sim   symulacja: przytrzymanie strzałki = „patrzenie" na kwadrat,
                sygnał przechodzi przez ten sam dekoder co prawdziwe EEG

Uruchamiane z panelu WWW (zapis EEG automatyczny) albo ręcznie:

    .venv/bin/python -m tasks.ssvep.task --id osoba01 --trials 10

Klawisze: SPACJA start, ESC przerwanie, H — pokaż/ukryj podgląd dekodera.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time

from tasks.common import (
    EXIT_ABORTED,
    EXIT_OK,
    MarkerLog,
    TaskAborted,
    add_session_arguments,
    check_abort,
    open_fullscreen,
    prepare_session,
    wait_for_space,
    write_rows,
)
from tasks.ssvep.decoder import EegStream, SimulatedEeg, SsvepDecoder

TASK_CODE = "SSVEP"
EXIT_ERROR = 1

MARKERS = {
    "session_start": "SSVEP_SESSION_START",
    "trial_start": "SSVEP_TRIAL_START",
    "target_up": "SSVEP_TARGET_UP",
    "target_down": "SSVEP_TARGET_DOWN",
    "target_left": "SSVEP_TARGET_LEFT",
    "target_right": "SSVEP_TARGET_RIGHT",
    "stimulus_onset": "SSVEP_STIMULUS_ONSET",      # start migania + sterowania
    "stimulus_offset": "SSVEP_STIMULUS_OFFSET",    # koniec migania
    "response_up": "SSVEP_RESPONSE_UP",            # punkt dotknął kwadratu ...
    "response_down": "SSVEP_RESPONSE_DOWN",
    "response_left": "SSVEP_RESPONSE_LEFT",
    "response_right": "SSVEP_RESPONSE_RIGHT",
    "response_timeout": "SSVEP_RESPONSE_TIMEOUT",
    "response_correct": "SSVEP_RESPONSE_CORRECT",
    "response_incorrect": "SSVEP_RESPONSE_INCORRECT",
    "session_end": "SSVEP_SESSION_END",
}

BEHAVIOR_FIELDS = [
    "trial", "target", "response", "result", "rt_sec", "stimulus_lsl_time",
    "mode", "decode_acc",
]

DIRECTIONS = ["up", "down", "left", "right"]
DIR_VECTORS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
DIR_LABELS = {"up": "G", "down": "D", "left": "L", "right": "P"}
# Domyślne częstotliwości (Hz). Przy odświeżaniu 60 Hz: 8 / 10 / 12 / 15.
# Żadna nie jest harmoniczną (2f) innej.
DEFAULT_FREQS = {"up": 10.0, "down": 15.0, "left": 8.0, "right": 12.0}

# ── Kolory ──
BACKGROUND = (12, 12, 16)
SQUARE_STATIC = (150, 150, 160)       # kwadraty przed migotaniem
FLICKER_DARK = (12, 12, 16)
FLICKER_LIGHT = (255, 255, 255)
OUTLINE_COLOR = (70, 72, 85)
FIX_DOT_COLOR = (230, 70, 70)         # mała kropka fiksacji w kwadracie
PLAYER_COLOR = (255, 205, 50)         # punkt sterowany
PLAYER_OUTLINE = (20, 20, 20)
TARGET_COLOR = (80, 230, 120)
HIT_OK = (60, 200, 100)
HIT_BAD = (220, 70, 70)
TEXT_COLOR = (200, 200, 210)
MUTED_COLOR = (110, 115, 130)
WARN_COLOR = (255, 170, 60)

# ── Czasy i parametry sterowania ──
CUE_SEC = 1.5            # pokazanie celu przed migotaniem
FEEDBACK_SEC = 0.9
UPDATE_SEC = 0.2         # co ile odświeżamy decyzję dekodera
MIN_WINDOW_SEC = 1.0     # tyle danych potrzeba, by zacząć sterować
SMOOTHING = 0.6          # wygładzanie korelacji (EMA): wyżej = wolniej, spokojniej
DEADZONE = 0.06          # margines r1−r2 poniżej którego punkt stoi
FULL_SPEED_SEC = 5.0     # czas dojścia do kwadratu przy pełnej pewności i speed=1
VELOCITY_TAU = 0.35      # bezwładność punktu (s)
FLICKER_FPS_CAP = 240    # przy włączonym vsync to ograniczenie nie działa
FILE_WAIT_SEC = 5.0      # ile czekamy na dane z pliku sesji
LSL_WAIT_SEC = 3.0       # ile szukamy strumienia LSL


def lerp_color(c0, c1, a: float):
    return tuple(int(c0[i] + (c1[i] - c0[i]) * a) for i in range(3))


def decide(scores: dict[str, float], min_corr: float, full_margin: float):
    """(zwycięski kierunek, siła 0..1) z wygładzonych korelacji CCA."""
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (d1, r1), (_, r2) = ranked[0], ranked[1]
    if r1 < min_corr:
        return d1, 0.0
    strength = (r1 - r2 - DEADZONE) / max(full_margin - DEADZONE, 1e-6)
    return d1, max(0.0, min(1.0, strength))


def keyboard_gaze(pygame) -> str | None:
    """Tryb symulacji: przytrzymana strzałka = patrzenie na dany kwadrat."""
    keys = pygame.key.get_pressed()
    for direction, key in (("up", pygame.K_UP), ("down", pygame.K_DOWN),
                           ("left", pygame.K_LEFT), ("right", pygame.K_RIGHT)):
        if keys[key]:
            return direction
    return None


def run_task(paths, trials: int, trial_seconds: float, window_seconds: float = 2.0,
             speed: float = 1.0, freqs: dict[str, float] | None = None,
             input_mode: str = "auto", eeg_name: str | None = None,
             channels: list[int] | None = None, min_corr: float = 0.28,
             full_margin: float = 0.20, seed: int | None = None,
             gaze_fn=None) -> int:
    """`gaze_fn(target) -> kierunek|None` — tylko testy: autopilot w trybie symulacji."""
    if trials < 1:
        raise ValueError("Liczba prób musi być ≥ 1.")
    freqs = dict(freqs or DEFAULT_FREQS)
    rng = random.Random(seed)

    markers = MarkerLog(paths.events, lsl_name="BrainAccess-SSVEP-Markers",
                        source_id="brainaccess_ssvep_markers")
    pygame, screen = open_fullscreen("SSVEP — BrainAccess EEG")
    width, height = screen.get_size()
    cx, cy = width // 2, height // 2

    # ── Geometria ──
    side = min(width, height)
    size = int(side / 6.5)                 # bok kwadratu
    offset = int(side * 0.30)              # środek kwadratu od środka ekranu
    positions = {
        "up": (cx, cy - offset), "down": (cx, cy + offset),
        "left": (cx - offset, cy), "right": (cx + offset, cy),
    }
    reach = offset - size / 2              # odległość środka ekranu od krawędzi kwadratu
    speed_px = speed * reach / FULL_SPEED_SEC
    player_r = max(10, size // 8)
    dot_r = max(4, size // 24)
    rects = {}
    for d, (x, y) in positions.items():
        r = pygame.Rect(0, 0, size, size)
        r.center = (x, y)
        rects[d] = r

    font = pygame.font.Font(None, max(64, height // 9))
    small_font = pygame.font.Font(None, max(30, height // 28))
    clock = pygame.time.Clock()
    rows: list[dict] = []
    fps_samples: list[float] = []
    exit_code = EXIT_OK
    state = {"hud": True}

    def handle_events() -> None:
        for ev in pygame.event.get():
            check_abort(pygame, ev)
            if ev.type == pygame.KEYDOWN and ev.key == pygame.K_h:
                state["hud"] = not state["hud"]

    def message(lines: list[str], seconds: float = 0.0, color=TEXT_COLOR) -> None:
        pygame.event.pump()
        screen.fill(BACKGROUND)
        for i, line in enumerate(lines):
            surf = small_font.render(line, True, color)
            y = cy + int((i - (len(lines) - 1) / 2) * small_font.get_linesize() * 1.3)
            screen.blit(surf, surf.get_rect(center=(cx, y)))
        pygame.display.flip()
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            handle_events()
            clock.tick(30)

    def render(flicker_t: float | None = None, target: str | None = None,
               hit: str | None = None, hit_ok: bool = False,
               player: tuple[float, float] | None = (0.0, 0.0),
               info: str = "", hud: str = "", hud_color=MUTED_COLOR) -> None:
        screen.fill(BACKGROUND)
        for d, rect in rects.items():
            if hit == d:
                color = HIT_OK if hit_ok else HIT_BAD
            elif flicker_t is not None:
                lum = 0.5 * (1.0 + math.sin(2 * math.pi * freqs[d] * flicker_t))
                color = lerp_color(FLICKER_DARK, FLICKER_LIGHT, lum)
            else:
                color = SQUARE_STATIC
            pygame.draw.rect(screen, color, rect)
            pygame.draw.rect(screen, OUTLINE_COLOR, rect, 2)
            pygame.draw.circle(screen, FIX_DOT_COLOR, rect.center, dot_r)
            if d == target:
                pygame.draw.rect(screen, TARGET_COLOR, rect.inflate(size // 4, size // 4),
                                 max(4, size // 28), border_radius=8)
        if player is not None:
            pos = (int(cx + player[0]), int(cy + player[1]))
            pygame.draw.circle(screen, PLAYER_OUTLINE, pos, player_r + 3)
            pygame.draw.circle(screen, PLAYER_COLOR, pos, player_r)
        if info:
            surf = small_font.render(info, True, MUTED_COLOR)
            screen.blit(surf, surf.get_rect(center=(cx, small_font.get_height())))
        if hud and state["hud"]:
            surf = small_font.render(hud, True, hud_color)
            screen.blit(surf, surf.get_rect(center=(cx, height - small_font.get_height())))
        pygame.display.flip()

    def idle(seconds: float, **draw_kwargs) -> None:
        """Faza bez migania; EEG dalej jest czytane (ciągły stan filtra)."""
        end = markers.now() + seconds
        while markers.now() < end:
            handle_events()
            source.pull()
            render(**draw_kwargs)
            clock.tick(60)

    source: EegStream | None = None
    try:
        # ── Źródło EEG ──
        meta = paths.read_meta()
        # panel zapisuje EEG, chyba że zadanie odpalono z terminala (eeg_recorded=False)
        panel_recording = meta.get("eeg_recorded", True) is not False
        sim: SimulatedEeg | None = None
        if input_mode != "sim":
            message(["Szukam danych EEG…"])
            if input_mode in ("auto", "eeg", "file") and panel_recording:
                source = EegStream.from_live_csv(
                    paths.eeg_partial, fs_hint=meta.get("sample_rate_hz"),
                    timeout=FILE_WAIT_SEC, channels=channels, clock=markers.now)
            if source is None and input_mode in ("auto", "eeg", "lsl"):
                source = EegStream.connect(eeg_name, timeout=LSL_WAIT_SEC, channels=channels)
            if source is None:
                if input_mode != "auto":
                    print("BŁĄD: nie znaleziono danych EEG.")
                    message(["Nie znaleziono danych EEG.",
                             "Sprawdź, czy panel zapisuje EEG (opaska połączona).",
                             "Plik: " + str(paths.eeg_partial.name)], 5.0, WARN_COLOR)
                    return EXIT_ERROR
                print("UWAGA: brak danych EEG — tryb SYMULACJI (strzałki = „patrzenie”).")
                message(["UWAGA: brak danych EEG z opaski.",
                         "Gra przejdzie w tryb SYMULACJI (strzałki = „patrzenie”).",
                         "Dane z tej sesji NIE są prawdziwym EEG."], 3.5, WARN_COLOR)
        if source is None:
            sim = SimulatedEeg(freqs, seed=seed)
            source = sim.as_stream()
        mode = "sim" if sim is not None else "eeg"
        decoder = SsvepDecoder(freqs, source.fs)
        mode_text = (f"{source.description}; kanały: {', '.join(source.used_labels)}"
                     if mode == "eeg"
                     else "SYMULACJA (brak EEG) — przytrzymaj strzałkę = patrzenie na kwadrat")
        print(f"Źródło: {source.description}")
        print(f"Kanały w danych: {source.labels}; do analizy: {source.used_labels}")
        if mode == "eeg" and source.all_channels_used and len(source.labels) > 2:
            print("UWAGA: nie rozpoznałem kanałów potylicznych (O*/PO*/P*) po etykietach — "
                  "analizuję wszystkie. Wskaż właściwe przez --channels (indeksy od 0).")
        if mode == "eeg" and source.timestamped:
            print(f"Opóźnienie danych względem zegara zadania: {source.age():.2f} s")
        print("Częstotliwości (Hz): " + ", ".join(f"{d}={f:g}" for d, f in freqs.items()))

        wait_for_space(pygame, screen, [
            "Naciśnij SPACJĘ, aby rozpocząć",
            "Patrz na kwadrat z zieloną ramką — każdy miga inaczej.",
            "Punkt ze środka pojedzie w stronę kwadratu, na który patrzysz.",
            f"Źródło: {mode_text}",
            "ESC przerywa.",
        ])
        markers.push(MARKERS["session_start"])

        # zrównoważona, losowa kolejność celów
        plan: list[str] = []
        while len(plan) < trials:
            block = DIRECTIONS[:]
            rng.shuffle(block)
            plan += block
        plan = plan[:trials]

        for trial_idx, target in enumerate(plan, start=1):
            markers.push(MARKERS["trial_start"])
            markers.push(MARKERS[f"target_{target}"])
            info = (f"Próba {trial_idx}/{trials}   ·   "
                    f"{'EEG' if mode == 'eeg' else 'SYMULACJA'}")

            # 1. Wskazanie celu (kwadraty jeszcze nie migają)
            if sim is not None:
                sim.gaze = None
            idle(CUE_SEC, target=target, info=info)

            # 2. Miganie + sterowanie na żywo
            smooth: dict[str, float] | None = None
            winner: str | None = None
            strength = 0.0
            decode_total = decode_hits = 0
            px = py = vx = vy = 0.0
            hit: str | None = None
            frames = 0
            onset = markers.push(MARKERS["stimulus_onset"])
            source.mark(onset)
            last = onset
            next_update = onset + MIN_WINDOW_SEC
            rt = None
            hud = "analiza sygnału…"

            while True:
                handle_events()
                source.pull()
                now = markers.now()
                t = now - onset
                if t >= trial_seconds:
                    break
                dt = min(now - last, 0.1)
                last = now
                frames += 1

                if sim is not None:
                    sim.gaze = gaze_fn(target) if gaze_fn else keyboard_gaze(pygame)

                if now >= next_update:
                    next_update = now + UPDATE_SEC
                    x = source.window(window_seconds, MIN_WINDOW_SEC)
                    if x is not None:
                        raw = decoder.scores(x)
                        smooth = raw if smooth is None else {
                            d: SMOOTHING * smooth[d] + (1 - SMOOTHING) * raw[d]
                            for d in DIRECTIONS}
                        winner, strength = decide(smooth, min_corr, full_margin)
                        decode_total += 1
                        decode_hits += winner == target
                        hud = "  ".join(
                            f"{DIR_LABELS[d]} {freqs[d]:g}Hz r={smooth[d]:.2f}"
                            for d in DIRECTIONS)
                        if source.timestamped:
                            hud += f"   (dane opóźnione o {source.age():.1f} s)"

                stale = source.age() > source.stale_after
                if stale:
                    tx = ty = 0.0
                    hud = "BRAK SYGNAŁU EEG"
                elif winner is None:
                    tx = ty = 0.0
                else:
                    dx, dy = DIR_VECTORS[winner]
                    tx, ty = dx * strength * speed_px, dy * strength * speed_px
                a = min(1.0, dt / VELOCITY_TAU)
                vx += (tx - vx) * a
                vy += (ty - vy) * a
                limit = float(offset)
                px = max(-limit, min(limit, px + vx * dt))
                py = max(-limit, min(limit, py + vy * dt))

                for d, rect in rects.items():
                    if rect.collidepoint(cx + px, cy + py):
                        hit = d
                        break
                if hit is not None:
                    rt = markers.push(MARKERS[f"response_{hit}"]) - onset
                    break

                render(flicker_t=t, target=target, player=(px, py), info=info,
                       hud=hud, hud_color=WARN_COLOR if stale else MUTED_COLOR)
                clock.tick(FLICKER_FPS_CAP)

            markers.push(MARKERS["stimulus_offset"])
            if sim is not None:
                sim.gaze = None
            if frames:
                fps_samples.append(frames / max(markers.now() - onset, 1e-6))

            # 3. Ocena
            if hit is None:
                result = "timeout"
                markers.push(MARKERS["response_timeout"])
            elif hit == target:
                result = "correct"
                markers.push(MARKERS["response_correct"])
            else:
                result = "incorrect"
                markers.push(MARKERS["response_incorrect"])

            decode_acc = decode_hits / decode_total if decode_total else None
            rows.append({
                "trial": trial_idx,
                "target": target,
                "response": hit or "",
                "result": result,
                "rt_sec": "" if rt is None else round(rt, 6),
                "stimulus_lsl_time": f"{onset:.6f}",
                "mode": mode,
                "decode_acc": "" if decode_acc is None else round(decode_acc, 3),
            })
            print(f"Próba {trial_idx}/{trials}: cel={target} → {hit or '—'} ({result})"
                  + (f", t={rt:.1f}s" if rt is not None else "")
                  + (f", dekoder {decode_acc:.0%}" if decode_acc is not None else ""))

            # 4. Feedback
            label = {"correct": "TRAFIONE", "incorrect": "POMYŁKA",
                     "timeout": "CZAS MINĄŁ"}[result]
            idle(FEEDBACK_SEC, target=target, hit=hit, hit_ok=(hit == target),
                 player=(px, py), info=label)

            # 5. Przerwa między próbami
            idle(rng.uniform(0.8, 1.2), info=info)

        # Ekran końcowy
        screen.fill(BACKGROUND)
        text = font.render("KONIEC — dziękujemy!", True, TEXT_COLOR)
        screen.blit(text, text.get_rect(center=(cx, cy)))
        correct = sum(1 for r in rows if r["result"] == "correct")
        detail = small_font.render(
            f"{len(rows)} prób  ·  trafionych {correct}  ·  {100 * correct / max(len(rows), 1):.0f}%",
            True, MUTED_COLOR)
        screen.blit(detail, detail.get_rect(center=(cx, cy + 60)))
        pygame.display.flip()
        pygame.time.wait(2000)

    except TaskAborted:
        print("Zadanie przerwane (ESC).")
        exit_code = EXIT_ABORTED
    finally:
        markers.push(MARKERS["session_end"])
        markers.close()
        write_rows(paths.behavior, BEHAVIOR_FIELDS, rows)
        pygame.quit()

    if fps_samples:
        fps = sum(fps_samples) / len(fps_samples)
        print(f"Średnia częstość pętli podczas migania: {fps:.0f}/s")
        if fps < 55:
            print("UWAGA: pętla poniżej 55/s — miganie może być nieregularne. "
                  "Zamknij inne programy / sprawdź odświeżanie monitora.")
    correct = sum(1 for r in rows if r["result"] == "correct")
    print(f"SSVEP zakończony: {len(rows)} prób, trafionych {correct}. Dane: {paths.behavior}")
    return exit_code


def parse_freqs(text: str) -> dict[str, float]:
    values = [float(v) for v in text.replace(";", ",").split(",") if v.strip()]
    if len(values) != 4:
        raise ValueError("--freqs wymaga 4 wartości: góra,dół,lewo,prawo")
    if any(v <= 0 or v > 30 for v in values):
        raise ValueError("częstotliwości muszą być w zakresie (0, 30] Hz")
    freqs = dict(zip(DIRECTIONS, values))
    items = list(freqs.items())
    for i, (da, fa) in enumerate(items):
        for db, fb in items[i + 1:]:
            if abs(fa - fb) < 0.5:
                raise ValueError(f"{da} ({fa:g} Hz) i {db} ({fb:g} Hz) są zbyt blisko")
            if abs(2 * fa - fb) < 0.5 or abs(2 * fb - fa) < 0.5:
                print(f"UWAGA: {da} ({fa:g} Hz) i {db} ({fb:g} Hz) są swoimi harmonicznymi "
                      "— dekoder może je mylić.")
    return freqs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SSVEP — gra: cztery migające kwadraty sterują punktem")
    add_session_arguments(parser)
    parser.add_argument("--trials", type=int, default=10, help="Liczba prób")
    parser.add_argument("--trial-sec", type=float, default=20.0,
                        help="Maksymalny czas jednej próby (s)")
    parser.add_argument("--window-sec", type=float, default=2.0,
                        help="Długość okna analizy SSVEP (s)")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="Mnożnik prędkości punktu")
    parser.add_argument("--freqs", default="10,15,8,12",
                        help="Częstotliwości migania w Hz: góra,dół,lewo,prawo")
    parser.add_argument("--input", choices=["auto", "eeg", "file", "lsl", "sim"],
                        default="auto",
                        help="Źródło sygnału: auto (plik sesji → LSL → symulacja), "
                             "eeg (jak auto, bez symulacji), file, lsl, sim")
    parser.add_argument("--eeg-stream", default=None,
                        help="Nazwa strumienia LSL z EEG (domyślnie: pierwszy typu EEG)")
    parser.add_argument("--channels", default=None,
                        help="Indeksy kanałów do analizy, np. 2,3 (domyślnie: O*/PO*/P*)")
    parser.add_argument("--min-corr", type=float, default=0.28,
                        help="Minimalna korelacja CCA, by punkt ruszył")
    parser.add_argument("--full-margin", type=float, default=0.20,
                        help="Przewaga nad drugim kwadratem dająca pełną prędkość")
    parser.add_argument("--seed", type=int, default=None)
    # zgodność ze starym panelem/zapisanymi ustawieniami — ignorowane
    parser.add_argument("--stimulus-sec", type=float, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--response-window-sec", type=float, default=None,
                        help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.trials < 1:
        parser.error("--trials musi być ≥ 1")
    try:
        freqs = parse_freqs(args.freqs)
        channels = ([int(c) for c in args.channels.split(",") if c.strip()]
                    if args.channels else None)
    except ValueError as exc:
        parser.error(str(exc))
    if args.stimulus_sec is not None or args.response_window_sec is not None:
        print("UWAGA: --stimulus-sec/--response-window-sec są nieaktualne "
              "(użyj --trial-sec); ignoruję.")
    params = {
        "trials": args.trials,
        "trial_sec": args.trial_sec,
        "window_sec": args.window_sec,
        "speed": args.speed,
        "freqs": freqs,
        "input": args.input,
        "min_corr": args.min_corr,
        "full_margin": args.full_margin,
        "seed": args.seed,
    }
    paths = prepare_session(args, TASK_CODE, "ssvep", params)
    sys.exit(run_task(paths, args.trials, args.trial_sec, args.window_sec, args.speed,
                      freqs, args.input, args.eeg_stream, channels, args.min_corr,
                      args.full_margin, args.seed))


if __name__ == "__main__":
    main()
