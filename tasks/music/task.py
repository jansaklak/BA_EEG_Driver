"""Zadanie MUSIC: naprzemienne bloki cisza / muzyka w słuchawkach, na pełnym ekranie.

Muzykę odtwarza badany (np. z telefonu) w słuchawkach – najlepiej dousznych, bo pałąk
nauszników dociska elektrody Cz/C3/C4. Przed każdym blokiem ekran mówi, czy założyć
słuchawki i włączyć muzykę, czy je zdjąć. Badany potwierdza SPACJĄ, dopiero potem po
kilku sekundach uspokojenia startuje blok (trigger) – ruch rąk przy słuchawkach nie
trafia do analizowanego fragmentu. Po każdym bloku badany ocenia relaks (1–5),
a po bloku z muzyką także to, jak podobała mu się muzyka (1–5).

    .venv/bin/python -m tasks.music.task --id osoba01 --blocks 6 --block-seconds 60

Klawisze: SPACJA potwierdzenie, 1–5 ocena, ESC przerwanie.
"""

from __future__ import annotations

import argparse
import sys

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

TASK_CODE = "MUSIC"

MARKERS = {
    "session_start": "MUSIC_SESSION_START",
    "silence": "MUSIC_SILENCE_START",
    "music": "MUSIC_MUSIC_START",
    "block_end": "MUSIC_BLOCK_END",
    "session_end": "MUSIC_SESSION_END",
}

BEHAVIOR_FIELDS = [
    "block", "condition", "start_lsl_time", "end_lsl_time", "duration_sec",
    "relax_rating", "liking_rating",
]

SETTLE_SECONDS = 5.0

BACKGROUND = (255, 255, 255)
FOREGROUND = (20, 35, 60)
MUTED = (80, 95, 115)
MUSIC_COLOR = (124, 58, 237)
SILENCE_COLOR = (37, 99, 235)


def run_task(paths, blocks: int, block_seconds: float) -> int:
    if blocks < 2 or blocks % 2:
        raise ValueError("Liczba bloków musi być parzysta i wynosić co najmniej 2.")
    if block_seconds < 20:
        raise ValueError("Blok musi trwać co najmniej 20 sekund.")

    markers = MarkerLog(paths.events, lsl_name="BrainAccess-Music-Markers",
                        source_id="brainaccess_music_markers")
    pygame, screen = open_fullscreen("MUSIC — BrainAccess EEG")
    # Klawisze 1–5 z rzędu cyfr i z klawiatury numerycznej.
    rating_keys = {getattr(pygame, f"K_{prefix}{value}"): value for value in range(1, 6) for prefix in ("", "KP")}
    width, height = screen.get_size()
    center = (width // 2, height // 2)
    cross_font = pygame.font.Font(None, max(80, height // 8))
    font = pygame.font.Font(None, max(56, height // 12))
    small_font = pygame.font.Font(None, max(30, height // 26))
    clock = pygame.time.Clock()
    rows: list[dict] = []
    exit_code = EXIT_OK

    def draw_lines(lines: list[tuple[str, object, tuple]]) -> None:
        """lines: (tekst, font, kolor) – wyśrodkowane w pionie."""
        screen.fill(BACKGROUND)
        total = sum(f.get_height() + 20 for _, f, _ in lines)
        y = height // 2 - total // 2
        for text, line_font, color in lines:
            image = line_font.render(text, True, color)
            screen.blit(image, image.get_rect(midtop=(width // 2, y)))
            y += line_font.get_height() + 20
        pygame.display.flip()

    def countdown(seconds: float, title: str, color) -> None:
        end = markers.now() + seconds
        while markers.now() < end:
            for event in pygame.event.get():
                check_abort(pygame, event)
            draw_lines([
                (title, font, color),
                ("Usiądź wygodnie, rozluźnij się i patrz na krzyżyk.", small_font, MUTED),
                (f"Start za {max(0.0, end - markers.now()):.0f} s", small_font, MUTED),
            ])
            clock.tick(30)

    def ask_rating(question: str, low: str, high: str) -> int:
        pygame.event.clear()
        while True:
            for event in pygame.event.get():
                check_abort(pygame, event)
                if event.type == pygame.KEYDOWN and event.key in rating_keys:
                    return rating_keys[event.key]
            draw_lines([
                (question, font, FOREGROUND),
                (f"1 – {low}      2      3      4      5 – {high}", small_font, MUTED),
                ("Naciśnij klawisz 1–5.", small_font, MUTED),
            ])
            clock.tick(30)

    try:
        wait_for_space(pygame, screen, [
            "Naciśnij SPACJĘ, aby rozpocząć",
            "Bloki ciszy i muzyki na zmianę. Przed każdym blokiem zobaczysz,",
            "czy założyć słuchawki i włączyć muzykę, czy je zdjąć.",
            "W trakcie bloku patrz na krzyżyk i ograniczaj ruchy. ESC przerywa.",
        ])
        markers.push(MARKERS["session_start"])

        for block_index in range(blocks):
            # Kolejność: cisza, muzyka, cisza, muzyka... (cisza na początek = punkt odniesienia).
            condition = "silence" if block_index % 2 == 0 else "music"
            color = MUSIC_COLOR if condition == "music" else SILENCE_COLOR
            label = "MUZYKA" if condition == "music" else "CISZA"

            # 1. Polecenie dotyczące słuchawek – badany potwierdza SPACJĄ.
            if condition == "music":
                instruction = [
                    "ZAŁÓŻ SŁUCHAWKI I WŁĄCZ MUZYKĘ",
                    "Ustaw wygodną głośność. Muzyka gra przez cały blok.",
                    "Gdy muzyka już gra, naciśnij SPACJĘ.",
                ]
            elif block_index == 0:
                instruction = [
                    "CISZA — BEZ SŁUCHAWEK",
                    "Upewnij się, że słuchawki są zdjęte, a muzyka wyłączona.",
                    "Gdy jest cisza, naciśnij SPACJĘ.",
                ]
            else:
                instruction = [
                    "WYŁĄCZ MUZYKĘ I ZDEJMIJ SŁUCHAWKI",
                    "Odłóż słuchawki tak, żeby nic nie było słychać.",
                    "Gdy jest cisza, naciśnij SPACJĘ.",
                ]
            wait_for_space(pygame, screen, instruction, color=color)

            # 2. Kilka sekund na uspokojenie po ruchu rąk (poza analizowanym blokiem).
            countdown(SETTLE_SECONDS, label, color)

            # 3. Blok: krzyżyk fiksacji, trigger początku i końca.
            start = markers.push(MARKERS[condition])
            end = start + block_seconds
            row = {"block": block_index + 1, "condition": condition, "start_lsl_time": f"{start:.6f}"}
            try:
                while markers.now() < end:
                    for event in pygame.event.get():
                        check_abort(pygame, event)
                    screen.fill(BACKGROUND)
                    cross = cross_font.render("+", True, FOREGROUND)
                    screen.blit(cross, cross.get_rect(center=center))
                    info = small_font.render(
                        f"{label}  ·  blok {block_index + 1}/{blocks}  ·  pozostało {max(0.0, end - markers.now()):.0f} s",
                        True, color)
                    screen.blit(info, info.get_rect(midtop=(width // 2, height // 12)))
                    pygame.display.flip()
                    clock.tick(30)
            finally:
                # Blok zapisujemy także wtedy, gdy przerwano go w trakcie (rzeczywisty koniec).
                actual_end = min(end, markers.now())
                row.update(end_lsl_time=f"{actual_end:.6f}", duration_sec=round(actual_end - start, 3))
                rows.append(row)
            markers.push(MARKERS["block_end"])

            # 4. Oceny subiektywne (poza blokiem – nie wpływają na analizowany EEG).
            row["relax_rating"] = ask_rating("Jak bardzo jesteś teraz zrelaksowany/a?", "wcale", "bardzo")
            if condition == "music":
                row["liking_rating"] = ask_rating("Jak bardzo podobała Ci się muzyka?", "wcale", "bardzo")

        draw_lines([
            ("KONIEC — dziękujemy!", font, FOREGROUND),
            ("Możesz wyłączyć muzykę i zdjąć słuchawki.", small_font, MUTED),
        ])
        pygame.time.wait(2500)
    except TaskAborted:
        print("Zadanie przerwane (ESC).")
        exit_code = EXIT_ABORTED
    finally:
        markers.push(MARKERS["session_end"])
        markers.close()
        write_rows(paths.behavior, BEHAVIOR_FIELDS, rows)
        pygame.quit()

    print(f"MUSIC zakończony: {len(rows)} bloków. Dane: {paths.behavior}")
    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description="MUSIC: cisza vs muzyka w słuchawkach")
    add_session_arguments(parser)
    parser.add_argument("--blocks", type=int, default=6, help="Parzysta liczba bloków")
    parser.add_argument("--block-seconds", type=float, default=60.0, help="Długość bloku w sekundach")
    args = parser.parse_args()
    params = {"blocks": args.blocks, "block_seconds": args.block_seconds}
    paths = prepare_session(args, TASK_CODE, "music", params)
    try:
        sys.exit(run_task(paths, args.blocks, args.block_seconds))
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
