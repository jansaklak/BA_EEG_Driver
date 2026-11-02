#!/usr/bin/env bash
# BrainAccess EEG — uruchamianie z katalogu projektu (macOS, Linux; Windows: halo.bat)
#   ./halo.sh web    -> panel WWW (podgląd, zadania z zapisem EEG, analizy)
#   ./halo.sh scan   -> skanowanie BLE        (pełna lista: ./halo.sh --help)
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$DIR/.venv/bin/python"
export PYTHONUTF8=1

if [ ! -f "$VENV_PYTHON" ]; then
    echo "⚠️ Środowisko wirtualne nie istnieje. Tworzenie..."
    if ! python3 -m venv "$DIR/.venv"; then
        echo "Nie udało się utworzyć .venv. Linux (Debian/Ubuntu): sudo apt install python3-venv"
        exit 1
    fi
    "$VENV_PYTHON" -m pip install --upgrade pip
    "$VENV_PYTHON" -m pip install -r "$DIR/requirements.txt" || exit 1
fi

# Uruchamiamy z katalogu projektu, żeby działały importy driver.* i tasks.*
cd "$DIR"
exec "$VENV_PYTHON" -m driver.cli "$@"
