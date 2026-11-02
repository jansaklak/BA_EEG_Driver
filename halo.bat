@echo off
rem BrainAccess EEG - uruchamianie z katalogu projektu (Windows)
rem   halo.bat web    -> panel WWW (podglad, zadania z zapisem EEG, analizy)
rem   halo.bat scan   -> skanowanie BLE        (pelna lista: halo.bat --help)
setlocal
set "DIR=%~dp0"
set "VENV_PYTHON=%DIR%.venv\Scripts\python.exe"
rem UTF-8 dla polskich znakow w konsoli, logach i plikach.
set PYTHONUTF8=1
chcp 65001 >nul

if not exist "%VENV_PYTHON%" (
    echo Srodowisko wirtualne nie istnieje. Tworzenie...
    py -3 -m venv "%DIR%.venv" 2>nul || python -m venv "%DIR%.venv"
    if not exist "%VENV_PYTHON%" (
        echo Nie znaleziono Pythona 3. Zainstaluj go z https://www.python.org/downloads/ ^(zaznacz "Add python.exe to PATH"^).
        exit /b 1
    )
    "%VENV_PYTHON%" -m pip install --upgrade pip
    "%VENV_PYTHON%" -m pip install -r "%DIR%requirements.txt" || exit /b 1
)

rem Uruchamiamy z katalogu projektu, zeby dzialaly importy driver.* i tasks.*
cd /d "%DIR%"
"%VENV_PYTHON%" -m driver.cli %*
