# Experimental tasks

Each task is a directory in `tasks/`. The web panel (`./halo.sh web`) detects them
automatically – just add a directory, no server restart needed.

```text
tasks/
├── common.py            # shared utilities (file names, triggers, full screen, loading)
└── my_task/
    ├── __init__.py
    ├── task.json        # description for the panel: code, name, form parameters
    ├── task.py          # the procedure (pygame, full screen)
    ├── analysis.py      # analysis (optional) – saves PNG/JSON in the session directory
    └── README.md
```

## task.json

```json
{
  "code": "MYTASK",
  "name": "My task",
  "description": "A short description shown in the panel.",
  "params": [
    {"name": "trials", "label": "Number of trials", "type": "int", "default": 40, "min": 1, "max": 500}
  ]
}
```

`code` goes into the file names (`MYTASK_person01_20261030-183005`). Each parameter
is passed to `task.py` as `--<name> <value>`.

## The task.py contract

The panel runs: `python -m tasks.<task_dir>.task --session-dir <session_dir> --id <ID> [parameters]`.

- Use `add_session_arguments()` and `prepare_session()` from `tasks.common`.
- Send triggers via `MarkerLog(paths.events).push("NAME")` – it records the
  `local_clock` time, the same one as the EEG samples. Save the returned time in `_behavior.csv`.
- Save behavior data to `paths.behavior` (also after an abort – use a `finally` block).
- Window: `open_fullscreen()`, and in the event loop `check_abort()` (ESC ends the task).
- Exit code: `0` – completed, `2` (`EXIT_ABORTED`) – aborted.

EEG recording, writing triggers into the EEG file and the metadata are handled by the panel
(`driver/task_runner.py`) – the task does not connect to the headset.

## The analysis.py contract

The panel runs: `python -m tasks.<task_dir>.analysis --session <session_dir>`.
From the terminal `--id <ID>` is enough (most recent session). Use `resolve_session()`,
`load_eeg()` and `load_behavior()`, and save results via `paths.output("_something.png")` –
the panel will show all PNG files from the session directory.
