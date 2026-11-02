"""Zadanie REST: naprzemienne bloki oczy otwarte / zamknięte, na pełnym ekranie.

Na początku każdego bloku odtwarzany jest krótki dźwięk (przy zamkniętych
oczach badany wie, kiedy je otworzyć) i zapisywany jest trigger.

    .venv/bin/python -m tasks.rest.task --id osoba01 --blocks 6 --block-seconds 30

Klawisze: SPACJA start, ESC przerwanie.
"""

from __future__ import annotations

import argparse
import math
import sys

import numpy as np

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

TASK_CODE = "REST"

MARKERS = {
    "session_start": "REST_SESSION_START",
    "open": "REST_EYES_OPEN",
    "closed": "REST_EYES_CLOSED",
    "session_end": "REST_SESSION_END",
}

BEHAVIOR_FIELDS = ["block", "state", "start_lsl_time", "end_lsl_time", "duration_sec"]

BACKGROUND = (255, 255, 255)
FOREGROUND = (20, 35, 60)
MUTED = (80, 95, 115)


def make_beep(pygame, frequency: float = 880.0, duration: float = 0.16, volume: float = 0.35):
    """Generuje krótki ton sinusoidalny z łagodnym narastaniem/wygaszaniem (bez trzasków)."""
    sample_rate = pygame.mixer.get_init()[0]
    count = int(sample_rate * duration)
    envelope = np.ones(count)
    ramp = min(int(sample_rate * 0.01), count // 2)
    envelope[:ramp] = np.linspace(0.0, 1.0, ramp)
    envelope[-ramp:] = np.linspace(1.0, 0.0, ramp)
    wave = np.sin(2.0 * math.pi * frequency * np.arange(count) / sample_rate)
    samples = (wave * envelope * volume * 32767).astype(np.int16)
    channels = pygame.mixer.get_init()[2]
    if channels > 1:
        samples = np.repeat(samples[:, None], channels, axis=1)
    return pygame.sndarray.make_sound(np.ascontiguousarray(samples))


def run_task(paths, blocks: int, block_seconds: float, beep_volume: float = 0.35) -> int:
    if blocks < 2 or blocks % 2:
        raise ValueError("Liczba bloków musi być parzysta i wynosić co najmniej 2.")
    if block_seconds < 5:
        raise ValueError("Blok musi trwać co najmniej 5 sekund.")

    markers = MarkerLog(paths.events, lsl_name="BrainAccess-Rest-Markers",
                        source_id="brainaccess_rest_markers")
    pygame, screen = open_fullscreen("REST — BrainAccess EEG")
    pygame.mixer.init()
    beep = make_beep(pygame, volume=beep_volume)
    width, height = screen.get_size()
    font = pygame.font.Font(None, max(64, height // 9))
    small_font = pygame.font.Font(None, max(30, height // 26))
    clock = pygame.time.Clock()
    rows: list[dict] = []
    exit_code = EXIT_OK

    def draw(message: str, detail: str) -> None:
        screen.fill(BACKGROUND)
        text = font.render(message, True, FOREGROUND)
        screen.blit(text, text.get_rect(center=(width // 2, height // 2 - 30)))
        subtitle = small_font.render(detail, True, MUTED)
        screen.blit(subtitle, subtitle.get_rect(center=(width // 2, height // 2 + 50)))
        pygame.display.flip()

    try:
        wait_for_space(pygame, screen, [
            "Naciśnij SPACJĘ, aby rozpocząć",
            "Usiądź wygodnie, rozluźnij się i ogranicz ruchy.",
            "Po każdym sygnale dźwiękowym zmień stan oczu zgodnie z poleceniem.",
        ])
        markers.push(MARKERS["session_start"])

        for block_index in range(blocks):
            # Kolejność: otwarte, zamknięte, otwarte, zamknięte...
            state = "open" if block_index % 2 == 0 else "closed"
            label = "OTWARTE OCZY — patrz na ekran" if state == "open" else "ZAMKNIJ OCZY"

            # Dźwięk + trigger początku bloku.
            beep.play()
            start = markers.push(MARKERS[state])
            end = start + block_seconds
            try:
                # Przez cały blok pokazujemy polecenie i pozostały czas.
                while markers.now() < end:
                    for event in pygame.event.get():
                        check_abort(pygame, event)
                    remaining = max(0.0, end - markers.now())
                    draw(label, f"Blok {block_index + 1}/{blocks}  ·  pozostało {remaining:.0f} s")
                    clock.tick(30)
            finally:
                # Blok zapisujemy także wtedy, gdy przerwano go w trakcie (rzeczywisty koniec).
                actual_end = min(end, markers.now())
                rows.append({
                    "block": block_index + 1,
                    "state": state,
                    "start_lsl_time": f"{start:.6f}",
                    "end_lsl_time": f"{actual_end:.6f}",
                    "duration_sec": round(actual_end - start, 3),
                })

        # Końcowy sygnał: badany może otworzyć oczy.
        beep.play()
        draw("KONIEC — dziękujemy!", "Możesz otworzyć oczy.")
        pygame.time.wait(1500)
    except TaskAborted:
        print("Zadanie przerwane (ESC).")
        exit_code = EXIT_ABORTED
    finally:
        markers.push(MARKERS["session_end"])
        markers.close()
        write_rows(paths.behavior, BEHAVIOR_FIELDS, rows)
        pygame.quit()

    print(f"REST zakończony: {len(rows)} bloków. Dane: {paths.behavior}")
    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description="REST: oczy otwarte / zamknięte")
    add_session_arguments(parser)
    parser.add_argument("--blocks", type=int, default=6, help="Parzysta liczba bloków")
    parser.add_argument("--block-seconds", type=float, default=30.0, help="Długość bloku w sekundach")
    parser.add_argument("--beep-volume", type=float, default=0.35)
    args = parser.parse_args()
    params = {"blocks": args.blocks, "block_seconds": args.block_seconds, "beep_volume": args.beep_volume}
    paths = prepare_session(args, TASK_CODE, "rest", params)
    try:
        sys.exit(run_task(paths, args.blocks, args.block_seconds, args.beep_volume))
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
