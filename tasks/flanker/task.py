"""Zadanie Flanker (Eriksen) na pełnym ekranie z triggerami.

Uruchamiane z panelu WWW (zapis EEG włącza się automatycznie) albo ręcznie:

    .venv/bin/python -m tasks.flanker.task --id osoba01 --trials 40

Klawisze: ← / → odpowiedź, SPACJA start, ESC przerwanie.
"""

from __future__ import annotations

import argparse
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

TASK_CODE = "FLANKER"

# Nazwy triggerów zapisywanych w pliku _events.csv i w kolumnie "trigger" pliku EEG.
MARKERS = {
    "session_start": "SESSION_START",
    "trial_congruent": "TRIAL_CONGRUENT",
    "trial_incongruent": "TRIAL_INCONGRUENT",
    "stimulus_onset": "STIMULUS_ONSET",
    "response_left": "RESPONSE_LEFT",
    "response_right": "RESPONSE_RIGHT",
    "response_correct": "RESPONSE_CORRECT",
    "response_incorrect": "RESPONSE_INCORRECT",
    "response_timeout": "RESPONSE_TIMEOUT",
    "session_end": "SESSION_END",
}

BEHAVIOR_FIELDS = [
    "trial", "condition", "direction", "response", "result",
    "reaction_time_sec", "stimulus_lsl_time",
]

BACKGROUND = (255, 255, 255)
FOREGROUND = (20, 35, 60)


def arrows_text(direction: int, congruent: bool) -> str:
    """Środkowa strzałka wskazuje kierunek; boczne są zgodne albo przeciwne."""
    target = ">" if direction > 0 else "<"
    flanker = target if congruent else ("<" if direction > 0 else ">")
    return flanker * 2 + target + flanker * 2


def run_task(paths, trials: int, stimulus_seconds: float, response_window_seconds: float,
             seed: int | None = None) -> int:
    if not 0.05 <= stimulus_seconds <= response_window_seconds:
        raise ValueError("Czas bodźca musi być ≥ 50 ms i nie dłuższy niż okno odpowiedzi.")
    rng = random.Random(seed)

    # Triggery idą do pliku _events.csv sesji oraz do strumienia LSL (dla LabRecorder).
    markers = MarkerLog(paths.events, lsl_name="BrainAccess-Flanker-Markers",
                        source_id="brainaccess_flanker_markers")
    pygame, screen = open_fullscreen("Flanker — BrainAccess EEG")
    width, height = screen.get_size()
    center = (width // 2, height // 2)
    arrow_font = pygame.font.Font(None, max(120, height // 5))
    fixation_font = pygame.font.Font(None, max(80, height // 8))
    clock = pygame.time.Clock()
    rows: list[dict] = []
    exit_code = EXIT_OK

    def show(draw=None) -> None:
        screen.fill(BACKGROUND)
        if draw:
            draw()
        pygame.display.flip()

    def wait(seconds: float, draw=None) -> None:
        """Wyświetla ekran przez zadany czas, reagując tylko na ESC."""
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            for event in pygame.event.get():
                check_abort(pygame, event)
            show(draw)
            clock.tick(120)

    def draw_fixation() -> None:
        image = fixation_font.render("+", True, FOREGROUND)
        screen.blit(image, image.get_rect(center=center))

    try:
        wait_for_space(pygame, screen, [
            "Naciśnij SPACJĘ, aby rozpocząć",
            "Wskaż kierunek ŚRODKOWEJ strzałki: klawisz LEWO lub PRAWO",
            "Odpowiadaj szybko i dokładnie. ESC przerywa.",
        ])
        markers.push(MARKERS["session_start"])

        for trial_index in range(1, trials + 1):
            # 1. Losujemy kierunek i zgodność bocznych strzałek.
            direction = rng.choice((-1, 1))
            congruent = rng.choice((True, False))
            condition = "congruent" if congruent else "incongruent"
            expected = "left" if direction < 0 else "right"
            markers.push(MARKERS[f"trial_{condition}"])

            # 2. Punkt fiksacji 500 ms.
            wait(0.5, draw_fixation)
            pygame.event.clear()

            # 3. Bodziec: najpierw rysujemy i pokazujemy (flip), dopiero potem
            #    zapisujemy trigger – tak czas triggera jest najbliżej pojawienia się obrazu.
            text = arrow_font.render(arrows_text(direction, congruent), True, FOREGROUND)
            show(lambda: screen.blit(text, text.get_rect(center=center)))
            onset = markers.push(MARKERS["stimulus_onset"])

            # 4. Czekamy na odpowiedź; bodziec znika po stimulus_seconds.
            response = None
            response_time = None
            stimulus_hidden = False
            while markers.now() - onset < response_window_seconds and response is None:
                for event in pygame.event.get():
                    check_abort(pygame, event)
                    if event.type == pygame.KEYDOWN and event.key in (pygame.K_LEFT, pygame.K_RIGHT):
                        response = "left" if event.key == pygame.K_LEFT else "right"
                        response_time = markers.push(MARKERS[f"response_{response}"]) - onset
                        break
                if not stimulus_hidden and markers.now() - onset >= stimulus_seconds:
                    show()
                    stimulus_hidden = True
                clock.tick(240)

            # 5. Ocena odpowiedzi i trigger wyniku.
            if response is None:
                result = "timeout"
            else:
                result = "correct" if response == expected else "incorrect"
            markers.push(MARKERS[f"response_{result}"])
            rows.append({
                "trial": trial_index,
                "condition": condition,
                "direction": expected,
                "response": response or "",
                "result": result,
                "reaction_time_sec": "" if response_time is None else round(response_time, 6),
                "stimulus_lsl_time": f"{onset:.6f}",
            })

            # 6. Przerwa między próbami 0,8–1,2 s (pusty ekran).
            wait(rng.uniform(0.8, 1.2))
    except TaskAborted:
        print("Zadanie przerwane (ESC).")
        exit_code = EXIT_ABORTED
    finally:
        # Zawsze zapisujemy to, co zebraliśmy – także po przerwaniu.
        markers.push(MARKERS["session_end"])
        markers.close()
        write_rows(paths.behavior, BEHAVIOR_FIELDS, rows)
        pygame.quit()

    correct = sum(row["result"] == "correct" for row in rows)
    print(f"Flanker zakończony: {len(rows)} prób, poprawnych {correct}. Dane: {paths.behavior}")
    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description="Zadanie Flanker z triggerami")
    add_session_arguments(parser)
    parser.add_argument("--trials", type=int, default=40)
    parser.add_argument("--stimulus-ms", type=float, default=200.0, help="Czas widoczności strzałek")
    parser.add_argument("--response-window-ms", type=float, default=1200.0, help="Okno odpowiedzi")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()
    if args.trials < 1:
        parser.error("--trials musi być dodatnie")
    params = {"trials": args.trials, "stimulus_ms": args.stimulus_ms,
              "response_window_ms": args.response_window_ms, "seed": args.seed}
    paths = prepare_session(args, TASK_CODE, "flanker", params)
    sys.exit(run_task(paths, args.trials, args.stimulus_ms / 1000.0,
                      args.response_window_ms / 1000.0, args.seed))


if __name__ == "__main__":
    main()
