"""
BrainAccess EEG — Universal Real-Time Web Dashboard (FastAPI + WebSockets + HTML5 Canvas).
Fully supports BrainAccess MINI (8 electrodes 0-7 + REF/BIAS with full 10-20 montage mapping)
and BrainAccess HALO (4-channel headband).
Cross-platform: macOS, Windows, Linux (BLE via bleak).
"""

import asyncio
import json
import logging
import os
import sys
import time
import webbrowser
from collections import deque
from typing import Dict, List, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from tasks.common import RECORDINGS_DIR, resolve_session

from .brainaccess_halo import (
    BANDS,
    CHANNELS,
    DEFAULT_MINI_MONTAGE,
    DEFAULT_MINI_REFERENCE,
    DEFAULT_MINI_BIAS,
    DeviceProfile,
    HaloDSP,
    HaloDriver,
    HaloRecorder,
    GazeAnalyzer,
    MONTAGE_PRESETS,
    PROFILE_HALO,
    PROFILE_MINI,
    SAMPLE_RATE,
    STANDARD_10_20_POSITIONS,
)
from .task_runner import TaskSession, discover_tasks, list_sessions, run_analysis

logger = logging.getLogger("HaloWeb")
app = FastAPI(title="BrainAccess EEG Monitor")
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
# Pliki sesji (PNG, CSV, JSON) dostępne w przeglądarce pod /recordings/...
app.mount("/recordings", StaticFiles(directory=str(RECORDINGS_DIR)), name="recordings")

driver: Optional[HaloDriver] = None
recorder: Optional[HaloRecorder] = None
active_websockets: Set[WebSocket] = set()
live_sample_buffer = deque(maxlen=2000)
live_sample_sequence = 0

# Current active montage for MINI (persisted across sessions)
current_montage: List[str] = list(DEFAULT_MINI_MONTAGE)
current_reference: str = DEFAULT_MINI_REFERENCE
current_bias: str = DEFAULT_MINI_BIAS
gaze_analyzer: Optional[GazeAnalyzer] = None
# Aktualna (lub ostatnia) sesja zadania uruchomionego z panelu.
task_session: Optional[TaskSession] = None


def _session_running() -> bool:
    return task_session is not None and task_session.running


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="pl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>BrainAccess EEG Monitor — MINI & HALO</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-base: #f4f7fb;
      --bg-surface: #ffffff;
      --bg-card: #ffffff;
      --bg-card-hover: #f8fafc;
      --border: #dbe3ee;
      --border-accent: #93c5fd;
      --text: #172033;
      --text-muted: #5b6b82;
      --text-dim: #7b8799;
      --cyan: #087ea4;
      --indigo: #4f46a5;
      --emerald: #138a61;
      --rose: #c92f4b;
      --amber: #a16207;
      --purple: #7e22ce;
      --teal: #0f766e;
      --orange: #c2410c;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg-base);
      color: var(--text);
      font-family: 'Plus Jakarta Sans', sans-serif;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      overflow-x: hidden;
    }
    header {
      padding: 0.85rem 2rem;
      background: rgba(255, 255, 255, 0.96);
      backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
      position: sticky;
      top: 0;
      z-index: 100;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 0.85rem;
    }
    .brand-icon {
      width: 38px;
      height: 38px;
      background: linear-gradient(135deg, var(--cyan), var(--indigo));
      border-radius: 10px;
      display: grid;
      place-items: center;
      font-weight: 800;
      font-size: 1rem;
      color: #050811;
      box-shadow: 0 0 20px rgba(56, 189, 248, 0.4);
    }
    .brand-title {
      font-size: 1.22rem;
      font-weight: 800;
      letter-spacing: -0.02em;
      background: linear-gradient(90deg, #172033, #53627a);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }
    .brand-subtitle {
      font-size: 0.72rem;
      color: var(--cyan);
      font-family: 'JetBrains Mono', monospace;
      letter-spacing: 0.04em;
      font-weight: 600;
    }
    .header-controls {
      display: flex;
      gap: 0.7rem;
      align-items: center;
    }
    .model-badge {
      display: inline-flex;
      align-items: center;
      padding: 0.35rem 0.75rem;
      border-radius: 9999px;
      font-size: 0.78rem;
      font-weight: 700;
      font-family: 'JetBrains Mono', monospace;
      background: #faf5ff;
      color: var(--purple);
      border: 1px solid rgba(192, 132, 252, 0.35);
    }
    .status-badge {
      display: flex;
      align-items: center;
      gap: 0.55rem;
      padding: 0.45rem 0.9rem;
      border-radius: 9999px;
      font-size: 0.85rem;
      font-weight: 600;
      background: #f8fafc;
      border: 1px solid var(--border);
    }
    .status-dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--rose);
      box-shadow: 0 0 10px var(--rose);
      transition: all 0.3s;
    }
    .status-dot.connected {
      background: var(--emerald);
      box-shadow: 0 0 12px var(--emerald);
    }
    .status-dot.scanning {
      background: var(--amber);
      box-shadow: 0 0 12px var(--amber);
      animation: pulse 1.2s infinite;
    }
    @keyframes pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.4; transform: scale(1.25); }
    }
    button {
      padding: 0.52rem 1.15rem;
      border-radius: 9px;
      border: none;
      font-weight: 600;
      font-size: 0.85rem;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
    }
    .btn-primary {
      background: linear-gradient(135deg, #38bdf8, #6366f1);
      color: #ffffff;
      font-weight: 700;
      box-shadow: 0 4px 15px rgba(56, 189, 248, 0.25);
    }
    .btn-primary:hover {
      filter: brightness(1.1);
      transform: translateY(-1px);
      box-shadow: 0 6px 20px rgba(56, 189, 248, 0.35);
    }
    .btn-danger {
      background: #fff1f2;
      border: 1px solid var(--rose);
      color: #be123c;
    }
    .btn-danger:hover {
      background: #ffe4e6;
      transform: translateY(-1px);
    }
    .btn-secondary {
      background: #ffffff;
      color: var(--text);
      border: 1px solid var(--border);
    }
    .btn-secondary:hover {
      background: #f1f5f9;
      border-color: #b8c7d9;
      transform: translateY(-1px);
    }
    .btn-montage {
      background: #faf5ff;
      border: 1px solid #d8b4fe;
      color: #7e22ce;
    }
    .btn-montage:hover {
      background: #f3e8ff;
      border-color: var(--purple);
      transform: translateY(-1px);
    }
    .btn-recording {
      background: rgba(244, 63, 94, 0.9);
      color: #ffffff;
      animation: pulse-border 1.5s infinite;
    }
    @keyframes pulse-border {
      0%, 100% { box-shadow: 0 0 0 0 rgba(244, 63, 94, 0.7); }
      50% { box-shadow: 0 0 0 8px rgba(244, 63, 94, 0); }
    }

    main {
      padding: 1.5rem 2rem;
      display: grid;
      grid-template-columns: 1fr 380px;
      gap: 1.5rem;
      flex: 1;
    }
    @media (max-width: 1250px) {
      main { grid-template-columns: 1fr; }
      .sidebar, .psd-panel { grid-column: auto; }
    }
    .psd-panel { grid-column: 1; }
    .sidebar { grid-column: 2; grid-row: 1 / span 2; }
    .card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 1.35rem;
      box-shadow: 0 4px 18px rgba(30, 55, 90, 0.08);
      position: relative;
    }
    .card-title {
      font-size: 0.95rem;
      font-weight: 700;
      letter-spacing: -0.01em;
      margin-bottom: 0.85rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .chart-container {
      display: flex;
      flex-direction: column;
      gap: 0.85rem;
    }
    .chart-toolbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 0.75rem;
    }
    .ch-legend {
      display: flex;
      flex-wrap: wrap;
      gap: 0.55rem;
      align-items: center;
    }
    .ch-badge {
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      font-size: 0.78rem;
      font-weight: 700;
      padding: 0.25rem 0.65rem;
      border-radius: 7px;
      background: #f8fafc;
      border: 1px solid var(--border);
      font-family: 'JetBrains Mono', monospace;
      cursor: pointer;
      transition: all 0.2s;
    }
    .ch-badge:hover {
      border-color: var(--cyan);
      transform: translateY(-1px);
    }
    .ch-indicator {
      width: 9px;
      height: 9px;
      border-radius: 3px;
    }
    .scale-group {
      display: inline-flex;
      gap: 0.35rem;
      align-items: center;
    }
    .btn-scale {
      padding: 0.3rem 0.65rem;
      border-radius: 6px;
      font-size: 0.75rem;
      font-weight: 600;
      background: #f8fafc;
      color: var(--text-muted);
      border: 1px solid var(--border);
    }
    .btn-scale.active {
      background: rgba(56, 189, 248, 0.22);
      color: var(--cyan);
      border-color: var(--cyan);
    }
    .btn-psd-mode {
      padding: 0.25rem 0.65rem;
      border-radius: 6px;
      font-size: 0.72rem;
      font-weight: 600;
      background: #f8fafc;
      color: var(--text-muted);
      border: 1px solid var(--border);
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .btn-psd-mode:hover {
      background: #f1f5f9;
      color: var(--text);
    }
    .btn-psd-mode.active {
      background: rgba(37, 99, 235, 0.12);
      color: #2563eb;
      border-color: #2563eb;
    }
    .btn-psd-mode.active.fooof-active {
      background: rgba(16, 185, 129, 0.15);
      color: #059669;
      border-color: #10b981;
    }
    canvas {
      width: 100%;
      height: 590px;
      background: #fbfdff;
      border-radius: 12px;
      border: 1px solid var(--border);
      display: block;
    }

    .sidebar {
      display: flex;
      flex-direction: column;
      gap: 1.25rem;
    }
    .stat-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 0.75rem;
    }
    .stat-box {
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 0.75rem 0.9rem;
    }
    .stat-label {
      font-size: 0.75rem;
      color: var(--text-muted);
      margin-bottom: 0.25rem;
    }
    .stat-val {
      font-family: 'JetBrains Mono', monospace;
      font-weight: 700;
      font-size: 1.1rem;
      color: var(--cyan);
    }

    /* Electrode Quality List */
    .quality-list {
      display: flex;
      flex-direction: column;
      gap: 0.65rem;
      max-height: 420px;
      overflow-y: auto;
      padding-right: 0.35rem;
    }
    .quality-list::-webkit-scrollbar { width: 5px; }
    .quality-list::-webkit-scrollbar-thumb {
      background: #cbd5e1;
      border-radius: 3px;
    }
    .contact-item {
      background: #fbfdff;
      border: 1px solid var(--border);
      border-radius: 9px;
      padding: 0.65rem 0.85rem;
      transition: border-color 0.2s;
    }
    .contact-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 0.35rem;
      font-size: 0.85rem;
    }
    .contact-title {
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 0.45rem;
      font-family: 'JetBrains Mono', monospace;
    }
    .contact-badge {
      font-size: 0.72rem;
      font-weight: 700;
      padding: 0.15rem 0.5rem;
      border-radius: 5px;
      font-family: 'JetBrains Mono', monospace;
    }
    .badge-excellent { background: rgba(52, 211, 153, 0.18); color: #34d399; border: 1px solid rgba(52, 211, 153, 0.3); }
    .badge-good      { background: rgba(56, 189, 248, 0.18); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3); }
    .badge-moderate  { background: rgba(251, 191, 36, 0.18); color: #fbbf24; border: 1px solid rgba(251, 191, 36, 0.3); }
    .badge-poor      { background: rgba(244, 63, 94, 0.18);  color: #f43f5e; border: 1px solid rgba(244, 63, 94, 0.3); }

    .bar-bg {
      height: 6px;
      background: #e8eef6;
      border-radius: 3px;
      overflow: hidden;
      margin-bottom: 0.4rem;
    }
    .bar-fill {
      height: 100%;
      width: 0%;
      border-radius: 3px;
      transition: width 0.2s ease-out;
    }
    .contact-sub {
      font-size: 0.73rem;
      color: var(--text-muted);
      font-family: 'JetBrains Mono', monospace;
      display: flex;
      justify-content: space-between;
    }

    /* PSD chart */
    .psd-chart {
      display: block;
      width: 100%;
      height: 360px;
      background: #fbfdff;
      border: 1px solid var(--border);
      border-radius: 8px;
    }
    /* Modals */
    .modal-overlay {
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.32);
      backdrop-filter: blur(10px);
      display: none;
      place-items: center;
      z-index: 200;
      padding: 1rem;
    }
    .modal-overlay.open { display: grid; }
    .modal-card {
      background: #ffffff;
      border: 1px solid var(--border-accent);
      border-radius: 20px;
      width: 100%;
      max-width: 680px;
      max-height: 90vh;
      overflow-y: auto;
      padding: 1.85rem;
      box-shadow: 0 20px 55px rgba(30, 55, 90, 0.22);
    }
    .modal-title {
      font-size: 1.25rem;
      font-weight: 800;
      margin-bottom: 0.4rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .modal-desc {
      font-size: 0.82rem;
      color: var(--text-muted);
      line-height: 1.5;
      margin-bottom: 1.25rem;
    }

    /* Montage UI */
    .montage-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 0.75rem;
      margin-bottom: 1.25rem;
    }
    @media (max-width: 600px) {
      .montage-grid { grid-template-columns: 1fr; }
    }
    .montage-channel-card {
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 0.65rem 0.85rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
    }
    .montage-ch-label {
      font-weight: 700;
      font-size: 0.82rem;
      font-family: 'JetBrains Mono', monospace;
      display: flex;
      align-items: center;
      gap: 0.45rem;
      min-width: 90px;
    }
    .montage-select {
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 7px;
      color: var(--text);
      padding: 0.4rem 0.6rem;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.85rem;
      font-weight: 600;
      flex: 1;
      outline: none;
    }
    .montage-select:focus {
      border-color: var(--cyan);
    }
    .ref-bias-banner {
      background: rgba(56, 189, 248, 0.08);
      border: 1px solid rgba(56, 189, 248, 0.25);
      border-radius: 10px;
      padding: 0.85rem 1rem;
      margin-bottom: 1.25rem;
      display: flex;
      justify-content: space-around;
      font-size: 0.82rem;
    }
    .preset-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 0.45rem;
      margin-bottom: 1.25rem;
    }
    .chip {
      padding: 0.35rem 0.75rem;
      border-radius: 9999px;
      background: #f8fafc;
      border: 1px solid var(--border);
      font-size: 0.76rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s;
    }
    .chip:hover {
      background: rgba(192, 132, 252, 0.2);
      border-color: var(--purple);
      color: var(--text);
    }

    /* Head Map View (SVG) */
    .headmap-container {
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 1rem;
      display: flex;
      justify-content: center;
      align-items: center;
      margin-bottom: 1.25rem;
    }

    /* Scanner Device List */
    .device-list {
      display: flex;
      flex-direction: column;
      gap: 0.65rem;
      margin: 1.25rem 0;
      max-height: 280px;
      overflow-y: auto;
    }
    .device-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 0.75rem 0.95rem;
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 10px;
      cursor: pointer;
      transition: all 0.2s;
    }
    .device-row:hover {
      background: rgba(56, 189, 248, 0.1);
      border-color: var(--cyan);
    }
    .device-info-name {
      font-weight: 700;
      font-size: 0.9rem;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }
    .device-info-addr {
      font-size: 0.72rem;
      color: var(--text-dim);
      font-family: 'JetBrains Mono', monospace;
    }
    .tag-mini {
      background: rgba(192, 132, 252, 0.2);
      color: var(--purple);
      font-size: 0.68rem;
      font-weight: 700;
      padding: 0.1rem 0.4rem;
      border-radius: 4px;
    }
    .tag-halo {
      background: rgba(56, 189, 248, 0.2);
      color: var(--cyan);
      font-size: 0.68rem;
      font-weight: 700;
      padding: 0.1rem 0.4rem;
      border-radius: 4px;
    }
    .error-banner {
      background: rgba(244, 63, 94, 0.12);
      border: 1px solid var(--rose);
      border-radius: 8px;
      padding: 0.75rem;
      font-size: 0.8rem;
      color: #fecdd3;
      margin-top: 0.75rem;
      display: none;
    }
    .gaze-result {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 0.75rem;
      margin-bottom: 0.75rem;
    }
    .gaze-label {
      font-size: 1.35rem;
      font-weight: 800;
      color: var(--cyan);
    }
    .gaze-confidence {
      color: var(--text-muted);
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.78rem;
    }
    .gaze-calibration-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 0.4rem;
    }
    .gaze-step {
      min-width: 0;
      padding: 0.45rem 0.3rem;
      font-size: 0.72rem;
    }
    .gaze-help {
      color: var(--text-muted);
      font-size: 0.73rem;
      line-height: 1.4;
      margin-bottom: 0.7rem;
    }

    /* ---- Zadania eksperymentalne ---- */
    .task-item {
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 0.7rem 0.75rem;
      margin-bottom: 0.6rem;
      background: #f8fafc;
    }
    .task-name { font-weight: 800; font-size: 0.9rem; }
    .task-code { font-family: 'JetBrains Mono', monospace; font-size: 0.68rem; color: var(--indigo); margin-left: 0.3rem; }
    .task-desc { font-size: 0.73rem; color: var(--text-muted); margin: 0.25rem 0 0.5rem; line-height: 1.35; }
    .task-params { display: grid; grid-template-columns: 1fr 1fr; gap: 0.4rem; margin-bottom: 0.55rem; }
    .task-params label { font-size: 0.68rem; color: var(--text-muted); }
    .task-params input {
      width: 100%; margin-top: 0.15rem; padding: 0.28rem 0.4rem; border: 1px solid var(--border);
      border-radius: 6px; background: #fff; color: var(--text); font-family: 'JetBrains Mono', monospace; font-size: 0.78rem;
    }
    .task-actions { display: flex; gap: 0.4rem; }
    .task-actions button { flex: 1; justify-content: center; padding: 0.4rem 0.5rem; font-size: 0.78rem; }
    .session-box {
      border-radius: 10px; padding: 0.65rem 0.75rem; margin-bottom: 0.7rem; font-size: 0.78rem;
      border: 1px solid var(--border-accent); background: #eff6ff; line-height: 1.5;
    }
    .session-box.done { border-color: #86efac; background: #f0fdf4; }
    .session-box.warn { border-color: #fcd34d; background: #fffbeb; }
    .session-box.err { border-color: #fda4af; background: #fff1f2; }
    .session-box code { font-family: 'JetBrains Mono', monospace; font-size: 0.7rem; word-break: break-all; }
    .session-row { display: flex; justify-content: space-between; gap: 0.5rem; }
    .results-modal-card { max-width: 1100px; width: 94vw; max-height: 90vh; overflow-y: auto; }
    .results-png { width: 100%; border: 1px solid var(--border); border-radius: 8px; margin-top: 0.6rem; background: #fff; }
    .file-links { display: flex; flex-wrap: wrap; gap: 0.35rem; margin-top: 0.4rem; }
    .file-links a {
      font-family: 'JetBrains Mono', monospace; font-size: 0.7rem; padding: 0.15rem 0.45rem;
      border: 1px solid var(--border); border-radius: 6px; color: var(--cyan); text-decoration: none; background: #fff;
    }
    .sessions-table { width: 100%; border-collapse: collapse; font-size: 0.78rem; }
    .sessions-table td, .sessions-table th { border-bottom: 1px solid var(--border); padding: 0.4rem; text-align: left; }
    .analysis-log { white-space: pre-wrap; font-family: 'JetBrains Mono', monospace; font-size: 0.7rem;
      background: #0f172a; color: #e2e8f0; padding: 0.6rem; border-radius: 8px; max-height: 220px; overflow: auto; margin-top: 0.6rem; }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <div>
        <div class="brand-title">BrainAccess EEG Monitor</div>
      </div>
    </div>
    <div class="header-controls">
      <span id="modelBadge" class="model-badge">MINI (8ch)</span>
      <div class="status-badge">
        <div id="statusDot" class="status-dot"></div>
        <span id="statusText">Rozłączony</span>
      </div>
      <button class="btn-montage" onclick="openMontageModal()">Montaż elektrod</button>
      <button class="btn-secondary" onclick="openScanner()">Skanuj BLE</button>
      <button id="btnConnect" class="btn-primary" onclick="connectBrainAccess()">Połącz z czepkiem</button>
      <button id="btnRecord" class="btn-secondary" onclick="toggleRecord()">⏺ Nagraj CSV</button>
    </div>
  </header>

  <main>
    <div class="card chart-container">
      <div class="chart-toolbar">
        <div id="chLegend" class="ch-legend">
          <!-- Dynamic channel pills inserted by JS -->
        </div>
        <div class="scale-group">
          <span style="font-size:0.78rem; color:var(--text-muted); margin-right:0.25rem;">Skala:</span>
          <button class="btn-scale" onclick="setScale(25, this)">±25 µV</button>
          <button class="btn-scale active" onclick="setScale(50, this)">±50 µV</button>
          <button class="btn-scale" onclick="setScale(100, this)">±100 µV</button>
          <button class="btn-scale" onclick="setScale(200, this)">±200 µV</button>
          <button class="btn-scale" onclick="setScale(500, this)">±500 µV</button>
          <button id="btnPause" class="btn-scale" onclick="togglePause()">⏸ Pauza</button>
        </div>
      </div>
      <canvas id="eegCanvas" width="980" height="600"></canvas>
    </div>

    <div class="card psd-panel">
      <div class="card-title" style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.5rem;">
        <div>
          <span>Moc pasm mózgowych (PSD)</span>
          <span id="psdSubtitle" style="font-size:0.72rem; color:var(--text-dim)">Mediana z kanałów · µV²/Hz · skala log</span>
        </div>
        <div style="display:flex; gap:0.4rem; align-items:center;">
          <span id="fooofBadge" style="font-size:0.72rem; color:var(--text-muted); padding:2px 8px; border-radius:12px; background:rgba(100,116,139,0.08); display:none;"></span>
          <button id="btnPsdRegular" class="btn-psd-mode active" onclick="setPsdMode('regular')">Zwykłe PSD</button>
          <button id="btnPsdFooof" class="btn-psd-mode" onclick="setPsdMode('fooof')">FOOOF</button>
        </div>
      </div>
      <canvas id="psdCanvas" class="psd-chart" width="980" height="360"></canvas>
    </div>

    <div class="sidebar">
      <div class="card">
        <div class="card-title">
          <span>Zadania eksperymentalne</span>
          <button class="btn-secondary" style="padding:0.15rem 0.5rem; font-size:0.72rem;" onclick="openSessions()">Sesje</button>
        </div>
        <div id="sessionBox"></div>
        <div id="taskList" style="font-size:0.8rem; color:var(--text-muted)">Wczytywanie zadań...</div>
      </div>
      <div class="card">
        <div class="card-title">
          <span>Status urządzenia</span>
          <span id="txtDeviceName" style="font-size:0.75rem; color:var(--text-dim); font-family:'JetBrains Mono'">--</span>
        </div>
        <div class="stat-grid">
          <div class="stat-box">
            <div class="stat-label">Bateria</div>
            <div id="valBat" class="stat-val">-- %</div>
          </div>
          <div class="stat-box">
            <div class="stat-label">Częstotliwość</div>
            <div id="valHz" class="stat-val">0.0 Hz</div>
          </div>
          <div class="stat-box">
            <div class="stat-label">Próbek łącznie</div>
            <div id="valSamples" class="stat-val">0</div>
          </div>

        </div>
        <div style="margin-top:0.65rem; font-size:0.75rem; color:var(--text-muted); display:flex; justify-content:space-between;">
          <span>Utracone pakiety: <strong id="valDropped" style="color:var(--text)">0</strong></span>
          <span id="valMontageCount" style="font-family:'JetBrains Mono'; color:var(--purple)">8 kanałów EEG</span>
        </div>
      </div>

      <div class="card">
        <div class="card-title">
          <span>Kontakt elektrod ze skórą</span>
          <button class="btn-secondary" style="padding:0.15rem 0.5rem; font-size:0.72rem;" onclick="openMontageModal()">Zmień pozycje</button>
        </div>
        <div id="qualityList" class="quality-list">
          <!-- Dynamically populated per channel -->
        </div>
      </div>

      <div class="card">
        <div class="card-title">Filtry podglądu</div>
        <p style="font-size:0.74rem;color:var(--text-muted);margin-bottom:0.75rem;">
          Filtry czasu rzeczywistego (DSP). Zapis CSV pobiera surowe dane z przetwornika ADS1299.
        </p>
        <label style="display:flex;align-items:center;gap:0.4rem;font-size:0.83rem;">
          <input id="filterEnabled" type="checkbox" checked onchange="applyFilters()"> Filtruj podgląd (DC remove + IIR)
        </label>
        <div style="display:grid; grid-template-columns: 1fr 1fr; gap:0.5rem; margin-top:0.6rem;">
          <label style="font-size:0.76rem;color:var(--text-muted)">Górnoprzep. (Hz):
            <input id="highpassHz" type="number" min="0" max="40" step="0.1" value="0.5" style="width:100%;margin-top:0.2rem;padding:0.3rem;background:#ffffff;border:1px solid var(--border);border-radius:6px;color:var(--text);" onchange="applyFilters()">
          </label>
          <label style="font-size:0.76rem;color:var(--text-muted)">Dolnoprzep. (Hz):
            <input id="lowpassHz" type="number" min="5" max="120" step="0.5" value="45" style="width:100%;margin-top:0.2rem;padding:0.3rem;background:#ffffff;border:1px solid var(--border);border-radius:6px;color:var(--text);" onchange="applyFilters()">
          </label>
        </div>
        <label style="display:flex;align-items:center;gap:0.4rem;margin-top:0.6rem;font-size:0.83rem;">
          <input id="notchEnabled" type="checkbox" checked onchange="applyFilters()"> Notch 50 Hz (tłumienie sieci)
        </label>
      </div>
    </div>
  </main>

  <!-- Modal for Cap Electrode Montage Selection -->
  <div id="montageModal" class="modal-overlay">
    <div class="modal-card">
      <div class="modal-title">
        <span>Montaż elektrod na czepku (System 10-20)</span>
        <button class="btn-secondary" style="padding:0.2rem 0.5rem;" onclick="closeMontageModal()">Zamknij</button>
      </div>
      <p class="modal-desc">
        Wybierz pozycje, do których podłączone są elektrody <b>0 do 7</b> na Twoim czepku BrainAccess MINI. Nazwy zostaną automatycznie zastosowane na wykresach, analizie impedancji i w plikach CSV.
      </p>

      <div class="ref-bias-banner">
        <div><b>REF (Odniesienie):</b> <span id="currentReference">P8</span></div>
        <div><b>BIAS (Masa/DRL):</b> <span id="currentBias">P7</span></div>
      </div>

      <div style="font-size:0.8rem; font-weight:700; margin-bottom:0.4rem; color:var(--text-muted)">Gotowe szablony montażu:</div>
      <div class="preset-chips">
        <div class="chip" onclick="applyPreset(['Cz', 'Fp1', 'Fp2', 'F3', 'F4', 'C3', 'C4', 'Oz'])">Mój układ (REF P8, BIAS P7)</div>
        <div class="chip" onclick="applyPreset(['F7', 'FC5', 'Fp1', 'POz', 'C3', 'C4', 'O1', 'O2'])">Zbalansowany (F7, FC5, Fp1, POz, C3, C4, O1, O2)</div>
        <div class="chip" onclick="applyPreset(['Fp1', 'Fp2', 'F3', 'F4', 'C3', 'C4', 'O1', 'O2'])">Standardowy 10-20 (Fp1, Fp2, F3, F4, C3, C4, O1, O2)</div>
        <div class="chip" onclick="applyPreset(['Fp1', 'Fp2', 'F7', 'F8', 'FC5', 'FC6', 'C3', 'C4'])">Czołowy / BCI (Fp1, Fp2, F7, F8, FC5, FC6, C3, C4)</div>
        <div class="chip" onclick="applyPreset(['C3', 'C4', 'P3', 'Pz', 'P4', 'POz', 'O1', 'O2'])">Tylny / Alfa & Sen (C3, C4, P3, Pz, P4, POz, O1, O2)</div>
        <div class="chip" onclick="applyPreset(['Ch0', 'Ch1', 'Ch2', 'Ch3', 'Ch4', 'Ch5', 'Ch6', 'Ch7'])">Domyślne numery (Ch0–Ch7)</div>
      </div>

      <div id="montageInputs" class="montage-grid">
        <!-- 8 channel dropdown rows generated by JS -->
      </div>

      <div class="montage-grid">
        <div class="montage-channel-card">
          <div class="montage-ch-label">REF (Odniesienie):</div>
          <select id="sel-reference" class="montage-select"></select>
        </div>
        <div class="montage-channel-card">
          <div class="montage-ch-label">BIAS (Masa/DRL):</div>
          <select id="sel-bias" class="montage-select"></select>
        </div>
      </div>

      <div style="display:flex; justify-content:space-between; align-items:center; margin-top:1rem;">
        <button class="btn-secondary" onclick="closeMontageModal()">Anuluj</button>
        <button class="btn-primary" onclick="saveMontage()">Zastosuj montaż</button>
      </div>
    </div>
  </div>

  <!-- Modal for scanning BLE devices -->
  <div id="scanModal" class="modal-overlay">
    <div class="modal-card">
      <div class="modal-title">
        <span>Wykryte urządzenia Bluetooth LE</span>
        <button class="btn-secondary" style="padding:0.2rem 0.5rem;" onclick="closeScanner()">Zamknij</button>
      </div>
      <p style="font-size:0.8rem; color:var(--text-muted);">
        Wybierz czepek BrainAccess z poniższej listy, aby połączyć się z nim bezpośrednio:
      </p>
      <div id="deviceList" class="device-list">
        <div style="text-align:center; padding:1.5rem; color:var(--text-dim); font-size:0.85rem;">
          Skanowanie w toku...
        </div>
      </div>
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <button class="btn-secondary" onclick="scanDevices()">Odśwież skanowanie</button>
        <button class="btn-primary" onclick="closeScanner()">Zamknij</button>
      </div>
      <div id="connectErrorBanner" class="error-banner"></div>
    </div>
  </div>

  <!-- Modal z wynikami analizy / listą sesji -->
  <div id="resultsModal" class="modal-overlay">
    <div class="modal-card results-modal-card">
      <div class="modal-title">
        <span id="resultsTitle">Wyniki</span>
        <button class="btn-secondary" style="padding:0.2rem 0.5rem;" onclick="closeResults()">Zamknij</button>
      </div>
      <div id="resultsBody"></div>
    </div>
  </div>

  <script>
    // 8-Channel Neon Palette
    const PALETTE = [
      '#38bdf8', // 0: Cyan
      '#818cf8', // 1: Indigo
      '#34d399', // 2: Emerald
      '#fbbf24', // 3: Amber
      '#f43f5e', // 4: Rose
      '#c084fc', // 5: Purple
      '#2dd4bf', // 6: Teal
      '#fb923c'  // 7: Orange
    ];

    // Standard 10-20 electrode positions list
    const ALL_10_20 = [
      "Fp1", "Fpz", "Fp2",
      "AF7", "AF3", "AFz", "AF4", "AF8",
      "F7", "F5", "F3", "F1", "Fz", "F2", "F4", "F6", "F8",
      "FT7", "FC5", "FC3", "FC1", "FCz", "FC2", "FC4", "FC6", "FT8",
      "T7", "C5", "C3", "C1", "Cz", "C2", "C4", "C6", "T8",
      "TP7", "CP5", "CP3", "CP1", "CPz", "CP2", "CP4", "CP6", "TP8",
      "P7", "P5", "P3", "P1", "Pz", "P2", "P4", "P6", "P8",
      "PO7", "PO3", "POz", "PO4", "PO8",
      "O1", "Oz", "O2", "Iz",
      "A1", "A2", "M1", "M2",
      "Ch0", "Ch1", "Ch2", "Ch3", "Ch4", "Ch5", "Ch6", "Ch7"
    ];

    const DEFAULT_MINI_MONTAGE = ['Cz', 'Fp1', 'Fp2', 'F3', 'F4', 'C3', 'C4', 'Oz'];
    // Replace the previous built-in montage once, while preserving future
    // user-selected layouts.
    let savedMontage = localStorage.getItem('ba_montage');
    let currentReference = localStorage.getItem('ba_reference') || 'P8';
    let currentBias = localStorage.getItem('ba_bias') || 'P7';
    let ACTIVE_CHANNELS = savedMontage ? JSON.parse(savedMontage) : DEFAULT_MINI_MONTAGE;
    if (JSON.stringify(ACTIVE_CHANNELS) === JSON.stringify(['F7', 'FC5', 'Fp1', 'POz', 'C3', 'C4', 'O1', 'O2'])) {
      ACTIVE_CHANNELS = DEFAULT_MINI_MONTAGE;
      localStorage.setItem('ba_montage', JSON.stringify(ACTIVE_CHANNELS));
    }

    const BUFFER_LEN = 750; // 3 seconds at 250 Hz
    let eegBuffers = {};

    function initBuffers(channels) {
      if (channels && channels.length > 0) {
        ACTIVE_CHANNELS = channels;
      }
      eegBuffers = {};
      ACTIVE_CHANNELS.forEach(ch => {
        eegBuffers[ch] = [];
      });
      renderLegend();
      renderQualityCards();
      document.getElementById('valMontageCount').innerText = `${ACTIVE_CHANNELS.length} kanałów EEG`;
      document.getElementById('currentReference').innerText = currentReference;
      document.getElementById('currentBias').innerText = currentBias;
    }
    initBuffers(ACTIVE_CHANNELS);

    function renderLegend() {
      const container = document.getElementById('chLegend');
      container.innerHTML = '';
      ACTIVE_CHANNELS.forEach((ch, idx) => {
        const col = PALETTE[idx % PALETTE.length];
        const badge = document.createElement('div');
        badge.className = 'ch-badge';
        badge.onclick = () => openMontageModal();
        badge.innerHTML = `<div class="ch-indicator" style="background:${col}"></div><span><b>${idx}:</b> ${ch}</span>`;
        container.appendChild(badge);
      });
    }

    function renderQualityCards() {
      const container = document.getElementById('qualityList');
      container.innerHTML = '';
      ACTIVE_CHANNELS.forEach((ch, idx) => {
        const col = PALETTE[idx % PALETTE.length];
        const div = document.createElement('div');
        div.className = 'contact-item';
        div.id = 'card-' + ch;
        div.innerHTML = `
          <div class="contact-header">
            <span class="contact-title" style="color:${col}">
              <span style="display:inline-block; width:8px; height:8px; border-radius:2px; background:${col}"></span>
              [${idx}] ${ch}
            </span>
            <span id="badge-${ch}" class="contact-badge badge-good">--</span>
          </div>
          <div class="bar-bg"><div id="bar-${ch}" class="bar-fill" style="width:0%; background:${col}"></div></div>
          <div class="contact-sub">
            <span id="subEEG-${ch}">EEG: 0.0 µV</span>
            <span id="subMains-${ch}">50Hz: 0.0 µV</span>
          </div>
        `;
        container.appendChild(div);
      });
    }

    // Modal Montage Logic
    function openMontageModal() {
      const container = document.getElementById('montageInputs');
      container.innerHTML = '';
      ACTIVE_CHANNELS.forEach((currentPos, idx) => {
        const col = PALETTE[idx % PALETTE.length];
        const row = document.createElement('div');
        row.className = 'montage-channel-card';

        let optionsHtml = '';
        ALL_10_20.forEach(pos => {
          const sel = (pos.toUpperCase() === currentPos.toUpperCase()) ? 'selected' : '';
          optionsHtml += `<option value="${pos}" ${sel}>${pos}</option>`;
        });

        row.innerHTML = `
          <div class="montage-ch-label" style="color:${col}">
            <span style="width:10px; height:10px; border-radius:50%; background:${col}; display:inline-block"></span>
            Elektroda ${idx}:
          </div>
          <select id="sel-ch-${idx}" class="montage-select">
            ${optionsHtml}
          </select>
        `;
        container.appendChild(row);
      });

      const referenceSelect = document.getElementById('sel-reference');
      const biasSelect = document.getElementById('sel-bias');
      const referenceOptions = ALL_10_20.map(pos =>
        `<option value="${pos}" ${pos === currentReference ? 'selected' : ''}>${pos}</option>`
      ).join('');
      const biasOptions = ALL_10_20.map(pos =>
        `<option value="${pos}" ${pos === currentBias ? 'selected' : ''}>${pos}</option>`
      ).join('');
      referenceSelect.innerHTML = referenceOptions;
      biasSelect.innerHTML = biasOptions;

      document.getElementById('montageModal').classList.add('open');
    }

    function closeMontageModal() {
      document.getElementById('montageModal').classList.remove('open');
    }

    function applyPreset(presetArray) {
      presetArray.forEach((pos, idx) => {
        const sel = document.getElementById(`sel-ch-${idx}`);
        if (sel) sel.value = pos;
      });
    }

    async function saveMontage() {
      const newChannels = [];
      for (let i = 0; i < ACTIVE_CHANNELS.length; i++) {
        const sel = document.getElementById(`sel-ch-${i}`);
        newChannels.push(sel ? sel.value : ACTIVE_CHANNELS[i]);
      }

      ACTIVE_CHANNELS = newChannels;
      currentReference = document.getElementById('sel-reference').value;
      currentBias = document.getElementById('sel-bias').value;
      localStorage.setItem('ba_montage', JSON.stringify(newChannels));
      localStorage.setItem('ba_reference', currentReference);
      localStorage.setItem('ba_bias', currentBias);
      document.getElementById('currentReference').innerText = currentReference;
      document.getElementById('currentBias').innerText = currentBias;

      // Send mapping to backend
      try {
        await fetch('/api/montage', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            channels: newChannels,
            reference: currentReference,
            bias: currentBias
          })
        });
      } catch (e) {
        console.error('Failed to save montage to server:', e);
      }

      initBuffers(newChannels);
      closeMontageModal();
    }

    // Canvas Renderer
    const canvas = document.getElementById('eegCanvas');
    const ctx = canvas.getContext('2d');
    const psdCanvas = document.getElementById('psdCanvas');
    const psdCtx = psdCanvas.getContext('2d');
    let currentScaleUv = 50.0;
    let isPaused = false;
    let ws = null;
    let recording = false;

    // ---- PSD chart: HiDPI canvas, log-Y axis, band shading, temporal smoothing ----
    const PSD_BANDS = [
      {name: 'δ', lo: 0.5, hi: 4,  color: 'rgba(100, 116, 139, 0.10)'},
      {name: 'θ', lo: 4,   hi: 8,  color: 'rgba(168, 85, 247, 0.10)'},
      {name: 'α', lo: 8,   hi: 13, color: 'rgba(16, 185, 129, 0.13)'},
      {name: 'β', lo: 13,  hi: 30, color: 'rgba(37, 99, 235, 0.08)'},
      {name: 'γ', lo: 30,  hi: 45, color: 'rgba(245, 158, 11, 0.10)'},
    ];
    const PSD_SMOOTHING = 0.2;   // 0..1; smaller = smoother (EMA between 10 Hz frames)
    let psdSmooth = null;
    let currentPsdMode = 'regular'; // 'regular' or 'fooof'

    function setPsdMode(mode) {
      if (currentPsdMode === mode) return;
      currentPsdMode = mode;
      psdSmooth = null;
      const btnReg = document.getElementById('btnPsdRegular');
      const btnFooof = document.getElementById('btnPsdFooof');
      const sub = document.getElementById('psdSubtitle');
      if (mode === 'fooof') {
        if (btnReg) btnReg.classList.remove('active');
        if (btnFooof) btnFooof.classList.add('active', 'fooof-active');
        if (sub) sub.innerText = 'Widmo spłaszczone FOOOF (PSD - tło aperiodyczne) · µV²/Hz · skala log';
      } else {
        if (btnFooof) btnFooof.classList.remove('active', 'fooof-active');
        if (btnReg) btnReg.classList.add('active');
        if (sub) sub.innerText = 'Mediana z kanałów · µV²/Hz · skala log';
      }
    }

    function drawPsdChart(frequencies, powers, isFooof = false, fooofInfo = null) {
      const dpr = window.devicePixelRatio || 1;
      const w = psdCanvas.clientWidth;
      const h = psdCanvas.clientHeight;
      if (!w || !h) return;
      if (psdCanvas.width !== Math.round(w * dpr) || psdCanvas.height !== Math.round(h * dpr)) {
        psdCanvas.width = Math.round(w * dpr);
        psdCanvas.height = Math.round(h * dpr);
      }
      psdCtx.setTransform(dpr, 0, 0, dpr, 0, 0);

      const left = 56, right = 14, top = 14, bottom = 30;
      const plotWidth = w - left - right;
      const plotHeight = h - top - bottom;
      psdCtx.clearRect(0, 0, w, h);
      psdCtx.font = '11px "JetBrains Mono", monospace';

      const hasData = frequencies && frequencies.length > 1 && powers && powers.length === frequencies.length;

      // Notice when FOOOF is selected but model is not ready
      if (isFooof && (!hasData || (fooofInfo && !fooofInfo.fitted))) {
        psdCtx.fillStyle = '#64748b';
        psdCtx.textAlign = 'center';
        psdCtx.font = '13px "JetBrains Mono", monospace';
        const msg = (fooofInfo && fooofInfo.details) ? fooofInfo.details : 'Oczekiwanie na model FOOOF...';
        psdCtx.fillText(msg, w / 2, h / 2);
        psdCtx.textAlign = 'start';
        return;
      }

      // X range follows the data: ~45 Hz with the preview low-pass on, up to 80 Hz otherwise.
      const maxFrequency = hasData ? Math.max(10, Math.ceil(frequencies[frequencies.length - 1])) : 45;
      const xOf = f => left + plotWidth * f / maxFrequency;

      // Temporal smoothing (exponential moving average across telemetry frames).
      let smooth = null;
      if (hasData) {
        if (!psdSmooth || psdSmooth.length !== powers.length) {
          psdSmooth = powers.map(p => (Number.isFinite(Number(p)) ? Number(p) : 0));
        } else {
          for (let i = 0; i < powers.length; i++) {
            const p = Number.isFinite(Number(powers[i])) ? Number(powers[i]) : 0;
            psdSmooth[i] += PSD_SMOOTHING * (p - psdSmooth[i]);
          }
        }
        smooth = psdSmooth;
      } else {
        psdSmooth = null;
      }

      // Log-Y range snapped to whole decades so the axis does not jitter.
      let logMin = -1, logMax = 2;
      if (smooth) {
        let lo = Infinity, hi = -Infinity;
        for (let i = 0; i < frequencies.length; i++) {
          if (frequencies[i] < 0.5) continue;
          const lg = Math.log10(Math.max(smooth[i], 1e-6));
          if (lg < lo) lo = lg;
          if (lg > hi) hi = lg;
        }
        if (Number.isFinite(lo) && Number.isFinite(hi)) {
          if (isFooof) {
            lo = Math.max(lo, -1.5);
          }
          logMin = Math.floor(lo);
          logMax = Math.ceil(hi);
          if (logMax - logMin < 2) logMax = logMin + 2;
        }
      }
      const yOf = p => {
        const frac = (Math.log10(Math.max(p, 1e-6)) - logMin) / (logMax - logMin);
        return top + plotHeight * (1 - Math.min(1, Math.max(0, frac)));
      };

      // Band shading
      PSD_BANDS.forEach(b => {
        if (b.lo >= maxFrequency) return;
        const x0 = xOf(b.lo);
        const x1 = xOf(Math.min(b.hi, maxFrequency));
        psdCtx.fillStyle = b.color;
        psdCtx.fillRect(x0, top, x1 - x0, plotHeight);
        psdCtx.fillStyle = '#64748b';
        psdCtx.fillText(b.name, (x0 + x1) / 2 - 3, top + 12);
      });

      // Grid + axis labels
      psdCtx.strokeStyle = '#dbe4ef';
      psdCtx.fillStyle = '#64748b';
      psdCtx.lineWidth = 1;
      for (let n = logMin; n <= logMax; n++) {
        const y = top + plotHeight * (1 - (n - logMin) / (logMax - logMin));
        psdCtx.beginPath();
        psdCtx.moveTo(left, y);
        psdCtx.lineTo(w - right, y);
        psdCtx.stroke();
        psdCtx.fillText(String(Math.pow(10, n)), 4, y + 4);
      }
      const xStep = maxFrequency <= 50 ? 5 : 10;
      for (let frequency = 0; frequency <= maxFrequency; frequency += xStep) {
        const x = xOf(frequency);
        psdCtx.beginPath();
        psdCtx.moveTo(x, top);
        psdCtx.lineTo(x, h - bottom);
        psdCtx.stroke();
        psdCtx.fillText(frequency + ' Hz', Math.min(x - 10, w - 40), h - 9);
      }

      if (isFooof && fooofInfo && fooofInfo.fitted) {
        psdCtx.fillStyle = '#059669';
        psdCtx.font = '10px "JetBrains Mono", monospace';
        psdCtx.fillText(`FOOOF: R²=${fooofInfo.r_squared} · piki: ${fooofInfo.n_peaks}`, w - right - 180, top + 12);
        psdCtx.font = '11px "JetBrains Mono", monospace';
      }

      if (!smooth) return;

      // Curve (+ light fill underneath)
      const pts = [];
      for (let i = 0; i < frequencies.length; i++) {
        if (frequencies[i] < 0.5 || frequencies[i] > maxFrequency) continue;
        pts.push([xOf(frequencies[i]), yOf(smooth[i])]);
      }
      if (pts.length < 2) return;
      psdCtx.beginPath();
      pts.forEach((p, i) => (i === 0 ? psdCtx.moveTo(p[0], p[1]) : psdCtx.lineTo(p[0], p[1])));
      psdCtx.lineTo(pts[pts.length - 1][0], top + plotHeight);
      psdCtx.lineTo(pts[0][0], top + plotHeight);
      psdCtx.closePath();
      psdCtx.fillStyle = isFooof ? 'rgba(16, 185, 129, 0.08)' : 'rgba(37, 99, 235, 0.08)';
      psdCtx.fill();

      psdCtx.beginPath();
      pts.forEach((p, i) => (i === 0 ? psdCtx.moveTo(p[0], p[1]) : psdCtx.lineTo(p[0], p[1])));
      psdCtx.strokeStyle = isFooof ? '#059669' : '#2563eb';
      psdCtx.lineWidth = 2;
      psdCtx.lineJoin = 'round';
      psdCtx.stroke();
    }

    function setScale(scaleVal, btn) {
      currentScaleUv = scaleVal;
      document.querySelectorAll('.scale-group .btn-scale').forEach(b => {
        if (b.id !== 'btnPause') b.classList.remove('active');
      });
      if (btn) btn.classList.add('active');
    }

    function togglePause() {
      isPaused = !isPaused;
      const btn = document.getElementById('btnPause');
      btn.innerText = isPaused ? '▶ Wznów' : '⏸ Pauza';
      if (isPaused) btn.classList.add('active');
      else btn.classList.remove('active');
    }

    function initWebSocket() {
      const loc = window.location;
      const wsUrl = (loc.protocol === 'https:' ? 'wss://' : 'ws://') + loc.host + '/ws';
      ws = new WebSocket(wsUrl);

      ws.onopen = () => console.log('WebSocket connected');
      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          handleStreamData(data);
        } catch (e) {
          console.error('WS Parse Error:', e);
        }
      };
      ws.onclose = () => {
        setTimeout(initWebSocket, 2000);
      };
    }

    // Keep calibration controls usable before the first telemetry message.
    window.calibrateGaze = async function(label) {
      const labels = {neutral: 'neutralnie', up: 'w górę', down: 'w dół'};
      const statusEl = document.getElementById('gazeCalibrationStatus');
      statusEl.innerText = `Patrz ${labels[label]} przez 2 sekundy...`;
      statusEl.style.color = 'var(--amber)';
      try {
        const response = await fetch('/api/analysis/gaze/calibration', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({label, seconds: 2})
        });
        const result = await response.json();
        if (result.status !== 'started') throw new Error(result.message || 'Nie udało się rozpocząć kalibracji');
        window.setTimeout(() => fetch('/api/analysis/gaze/calibration/stop', {method: 'POST'}), 2200);
      } catch (error) {
        statusEl.innerText = error.message;
        statusEl.style.color = 'var(--rose)';
      }
    };
    window.resetGazeCalibration = async function() {
      try {
        await fetch('/api/analysis/gaze/reset', {method: 'POST'});
      } catch (error) {
        document.getElementById('gazeCalibrationStatus').innerText = error.message;
      }
    };

    function handleStreamData(data) {
      const statusDot = document.getElementById('statusDot');
      const statusText = document.getElementById('statusText');
      const btnConnect = document.getElementById('btnConnect');
      const modelBadge = document.getElementById('modelBadge');
      const txtDeviceName = document.getElementById('txtDeviceName');

      if (data.connected) {
        statusDot.className = 'status-dot connected';
        const device = data.device || 'BrainAccess';
        statusText.innerText = 'Połączono (' + device + ')';
        btnConnect.innerText = 'Rozłącz';
        btnConnect.className = 'btn-danger';
        txtDeviceName.innerText = device;

        if (data.model) {
          modelBadge.innerText = data.model + (data.channels ? ` (${data.channels.length}ch)` : '');
        }

        // Dynamically adjust channels if changed
        if (data.channels && JSON.stringify(data.channels) !== JSON.stringify(ACTIVE_CHANNELS)) {
          initBuffers(data.channels);
        }
      } else {
        if (!statusDot.classList.contains('scanning')) {
          statusDot.className = 'status-dot';
          statusText.innerText = 'Rozłączony';
          btnConnect.innerText = 'Połącz z czepkiem';
          btnConnect.className = 'btn-primary';
          txtDeviceName.innerText = '--';
        }
      }

      // Update the waveform before optional dashboard widgets. A missing
      // secondary widget must never stop the live signal.
      if (data.chunk && !isPaused) {
        data.chunk.forEach(s => {
          ACTIVE_CHANNELS.forEach(ch => {
            const filteredValue = Number(s['filtered_' + ch]);
            const decodedValue = Number(s[ch]);
            const val = Number.isFinite(filteredValue) ? filteredValue : decodedValue;
            if (eegBuffers[ch]) {
              if (!Number.isFinite(val)) return;
              eegBuffers[ch].push(val);
              if (eegBuffers[ch].length > BUFFER_LEN) {
                eegBuffers[ch].splice(0, eegBuffers[ch].length - BUFFER_LEN);
              }
            }
          });
        });
      }

      if (data.battery !== null && data.battery !== undefined) {
        const val = data.battery;
        const color = val > 50 ? 'var(--emerald)' : (val > 20 ? 'var(--amber)' : 'var(--rose)');
        const batteryEl = document.getElementById('valBat');
        if (batteryEl) batteryEl.innerHTML = `<span style="color:${color}">${val}%</span>`;
      }
      const hzEl = document.getElementById('valHz');
      const samplesEl = document.getElementById('valSamples');
      const droppedEl = document.getElementById('valDropped');
      if (hzEl) hzEl.innerText = (data.hz || 0).toFixed(1) + ' Hz';
      if (samplesEl) samplesEl.innerText = (data.samples_total || 0).toLocaleString();
      if (droppedEl) droppedEl.innerText = data.dropped || 0;
      updateGaze(data.gaze);

      // Update FOOOF badge and chart mode
      const badge = document.getElementById('fooofBadge');
      if (badge && data.fooof && data.fooof.model_info) {
        const mi = data.fooof.model_info;
        if (mi.fitted) {
          badge.style.display = 'inline-block';
          badge.style.color = '#059669';
          badge.style.background = 'rgba(16, 185, 129, 0.12)';
          badge.innerText = `FOOOF: R²=${mi.r_squared} · ${mi.channels_fitted} kan.`;
        } else if (currentPsdMode === 'fooof') {
          badge.style.display = 'inline-block';
          badge.style.color = '#d97706';
          badge.style.background = 'rgba(245, 158, 11, 0.10)';
          badge.innerText = mi.details || 'Brak modelu';
        } else {
          badge.style.display = 'none';
        }
      }

      if (currentPsdMode === 'fooof') {
        const fData = data.fooof;
        if (fData && fData.model_info && fData.model_info.fitted) {
          drawPsdChart(fData.frequencies || [], fData.psd_flat || [], true, fData.model_info);
        } else {
          drawPsdChart([], [], true, fData ? fData.model_info : null);
        }
      } else if (data.psd) {
        drawPsdChart(data.psd.frequencies || [], data.psd.powers || [], false, null);
      }

      function updateGaze(gaze) {
        if (!gaze) return;
        const labels = {neutral: 'Neutralnie', up: 'W górę', down: 'W dół', unknown: '--'};
        const labelEl = document.getElementById('gazeLabel');
        const confidenceEl = document.getElementById('gazeConfidence');
        const readyEl = document.getElementById('gazeReady');
        const statusEl = document.getElementById('gazeCalibrationStatus');
        if (!labelEl || !confidenceEl || !readyEl || !statusEl) return;
        labelEl.innerText = labels[gaze.label] || gaze.label || '--';
        confidenceEl.innerText = gaze.confidence > 0
          ? `pewność: ${Math.round(gaze.confidence * 100)}%`
          : 'pewność: --';
        readyEl.innerText = gaze.ready ? 'skalibrowano' : 'nie skalibrowano';
        readyEl.style.color = gaze.ready ? 'var(--emerald)' : 'var(--text-dim)';
        if (gaze.active) {
          statusEl.innerText = `Kalibracja: ${labels[gaze.active] || gaze.active} (${gaze.samples || 0} próbek)...`;
          statusEl.style.color = 'var(--amber)';
        } else if (gaze.ready) {
          statusEl.innerText = 'Kalibracja gotowa';
          statusEl.style.color = 'var(--emerald)';
        } else if (gaze.reason === 'not_enough_data_or_missing_Fp1_Fp2') {
          statusEl.innerText = 'Podłącz MINI z kanałami Fp1 i Fp2';
        } else {
          statusEl.innerText = `Brakuje: ${(gaze.labels || []).length}/3 pozycji`;
        }
      }

      async function calibrateGaze(label) {
        const labels = {neutral: 'neutralnie', up: 'w górę', down: 'w dół'};
        const statusEl = document.getElementById('gazeCalibrationStatus');
        statusEl.innerText = `Patrz ${labels[label]} przez 2 sekundy...`;
        statusEl.style.color = 'var(--amber)';
        try {
          const response = await fetch('/api/analysis/gaze/calibration', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({label, seconds: 2})
          });
          const result = await response.json();
          if (result.status !== 'started') throw new Error(result.message || 'Nie udało się rozpocząć kalibracji');
        } catch (error) {
          statusEl.innerText = error.message;
          statusEl.style.color = 'var(--rose)';
          return;
        }
        window.setTimeout(async () => {
          try {
            await fetch('/api/analysis/gaze/calibration/stop', {method: 'POST'});
          } catch (error) {
            statusEl.innerText = 'Błąd zakończenia kalibracji: ' + error.message;
            statusEl.style.color = 'var(--rose)';
          }
        }, 2200);
      }

      async function resetGazeCalibration() {
        try {
          await fetch('/api/analysis/gaze/reset', {method: 'POST'});
        } catch (error) {
          document.getElementById('gazeCalibrationStatus').innerText = error.message;
        }
      }
      // Expose handlers for the calibration buttons rendered in the page.
      window.calibrateGaze = calibrateGaze;
      window.resetGazeCalibration = resetGazeCalibration;

      // Update Detailed Electrode Contact Quality
      if (data.quality) {
        ACTIVE_CHANNELS.forEach((ch) => {
          const q = data.quality[ch];
          if (q) {
            const badge = document.getElementById('badge-' + ch);
            const bar = document.getElementById('bar-' + ch);
            const subEEG = document.getElementById('subEEG-' + ch);
            const subMains = document.getElementById('subMains-' + ch);

            if (badge && bar && subEEG && subMains) {
              const score = q.score_pct || 0;
              bar.style.width = score + '%';
              subEEG.innerText = 'EEG: ' + (q.eeg_rms || 0).toFixed(1) + ' µV';
              subMains.innerText = '50Hz: ' + (q.mains_50hz || 0).toFixed(1) + ' µV';

              let badgeClass = 'badge-poor';
              let badgeLabel = 'Słaby';
              if (score >= 75) { badgeClass = 'badge-excellent'; badgeLabel = 'Świetny'; }
              else if (score >= 55) { badgeClass = 'badge-good'; badgeLabel = 'Dobry'; }
              else if (score >= 35) { badgeClass = 'badge-moderate'; badgeLabel = 'Średni'; }

              badge.className = 'contact-badge ' + badgeClass;
              badge.innerText = score + '% ' + badgeLabel;
            }
          }
        });
      }

    }

    function renderCanvas() {
      // Draw in CSS pixels, back the canvas with device pixels (sharp on HiDPI / Retina).
      const dpr = window.devicePixelRatio || 1;
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;
      if (!w || !h) {
        requestAnimationFrame(renderCanvas);
        return;
      }
      if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
        canvas.width = Math.round(w * dpr);
        canvas.height = Math.round(h * dpr);
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      const nCh = ACTIVE_CHANNELS.length;
      if (nCh === 0) {
        requestAnimationFrame(renderCanvas);
        return;
      }

      const chHeight = h / nCh;
      const yScale = (chHeight * 0.40) / currentScaleUv;

      for (let i = 0; i < nCh; i++) {
        const ch = ACTIVE_CHANNELS[i];
        const col = PALETTE[i % PALETTE.length];
        const yTop = chHeight * i;
        const yBase = yTop + chHeight / 2;

        ctx.save();
        ctx.beginPath();
        ctx.rect(0, yTop, w, chHeight);
        ctx.clip();

        // Baseline (0 µV)
        ctx.strokeStyle = 'rgba(71, 85, 105, 0.28)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(0, yBase);
        ctx.lineTo(w, yBase);
        ctx.stroke();

        // Upper & lower scale lines
        const yUpper = yBase - (currentScaleUv * yScale);
        const yLower = yBase + (currentScaleUv * yScale);
        ctx.strokeStyle = 'rgba(71, 85, 105, 0.16)';
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(0, yUpper);
        ctx.lineTo(w, yUpper);
        ctx.moveTo(0, yLower);
        ctx.lineTo(w, yLower);
        ctx.stroke();
        ctx.setLineDash([]);

        // Channel Lane Separator
        ctx.strokeStyle = 'rgba(71, 85, 105, 0.28)';
        ctx.beginPath();
        ctx.moveTo(0, yTop + chHeight);
        ctx.lineTo(w, yTop + chHeight);
        ctx.stroke();

        // Channel Tag & Position
        ctx.fillStyle = col;
        ctx.font = 'bold 12px Plus Jakarta Sans';
        ctx.fillText(`[${i}] ${ch}`, 14, yTop + 16);

        ctx.fillStyle = 'rgba(71, 85, 105, 0.75)';
        ctx.font = '10px JetBrains Mono';
        // Draw Waveform
        const buf = eegBuffers[ch] || [];
        if (buf.length > 0) {
          const sorted = [...buf].filter(Number.isFinite).sort((a, b) => a - b);
          const baseline = sorted[Math.floor(sorted.length / 2)] || 0;
          const displayYScale = (chHeight * 0.40) / currentScaleUv;
          ctx.fillText(`+${currentScaleUv}µV`, w - 70, yBase - chHeight * 0.40 + 3);
          ctx.fillText(`-${currentScaleUv}µV`, w - 70, yBase + chHeight * 0.40 + 3);
          ctx.strokeStyle = col;
          ctx.lineWidth = 1.6;
          ctx.lineJoin = 'round';
          ctx.beginPath();

          const dx = w / Math.max(BUFFER_LEN - 1, 1);
          const startX = Math.max(0, w - (buf.length - 1) * dx);
          for (let x = 0; x < buf.length; x++) {
            const px = startX + x * dx;
            const py = yBase - ((buf[x] - baseline) * displayYScale);
            if (x === 0) ctx.moveTo(px, py);
            else ctx.lineTo(px, py);
          }
          ctx.stroke();
        }

        ctx.restore();
      }

      requestAnimationFrame(renderCanvas);
    }

    async function connectBrainAccess(targetAddress = null) {
      const btn = document.getElementById('btnConnect');
      const dot = document.getElementById('statusDot');
      const statusText = document.getElementById('statusText');

      if (btn.innerText.includes('Rozłącz')) {
        await fetch('/api/disconnect', { method: 'POST' });
        btn.innerText = 'Połącz z czepkiem';
        btn.className = 'btn-primary';
        dot.className = 'status-dot';
        statusText.innerText = 'Rozłączony';
        return;
      }

      btn.innerText = 'Łączenie...';
      dot.className = 'status-dot scanning';
      statusText.innerText = targetAddress ? `Łączenie z ${targetAddress}...` : 'Skanowanie BLE i łączenie...';

      try {
        const response = await fetch('/api/connect', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(targetAddress ? { target: targetAddress, montage: ACTIVE_CHANNELS } : { montage: ACTIVE_CHANNELS })
        });
        const result = await response.json();
        if (result.status === 'connected' || result.status === 'already_connected') {
          btn.innerText = 'Rozłącz';
          btn.className = 'btn-danger';
          dot.className = 'status-dot connected';
          statusText.innerText = 'Połączono (' + result.device + ')';
          if (result.channels) initBuffers(result.channels);
          closeScanner();
        } else {
          btn.innerText = 'Połącz z czepkiem';
          btn.className = 'btn-primary';
          dot.className = 'status-dot';
          statusText.innerText = 'Błąd połączenia';
          showErrorBanner(result.message || 'Nie udało się połączyć z urządzeniem.');
        }
      } catch (err) {
        btn.innerText = 'Połącz z czepkiem';
        btn.className = 'btn-primary';
        dot.className = 'status-dot';
        statusText.innerText = 'Błąd sieci';
        showErrorBanner('Błąd żądania do serwera: ' + err.message);
      }
    }

    function showErrorBanner(msg) {
      const banner = document.getElementById('connectErrorBanner');
      banner.style.display = 'block';
      banner.innerHTML = `<b>Błąd połączenia:</b> ${msg}<br><br>` +
        `<b>Wskazówki:</b><br>` +
        `1. Upewnij się, że czepek jest włączony (dioda LED pulsuje na niebiesko).<br>` +
        `2. Jeśli urządzenie było sparowane w ustawieniach Bluetooth systemu, usuń je stamtąd ("Zapomnij" / "Usuń urządzenie").<br>` +
        `3. Sprawdź, czy żaden inny program (np. terminal) nie blokuje połączenia Bluetooth.`;
    }

    async function applyFilters() {
      const response = await fetch('/api/filters', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          enabled: document.getElementById('filterEnabled').checked,
          highpass_hz: Number(document.getElementById('highpassHz').value),
          lowpass_hz: Number(document.getElementById('lowpassHz').value),
          notch_enabled: document.getElementById('notchEnabled').checked
        })
      });
      const result = await response.json();
      if (result.status !== 'ok') {
        throw new Error(result.message || 'Nie udało się zastosować filtrów.');
      }
      // The backend resets its filter state; discard samples produced by
      // the previous filter configuration so the preview stays continuous.
      ACTIVE_CHANNELS.forEach(ch => {
        eegBuffers[ch] = [];
      });
    }

    async function toggleRecord() {
      recording = !recording;
      const btn = document.getElementById('btnRecord');
      if (recording) {
        const res = await fetch('/api/record/start', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'recording') {
          btn.innerText = 'Zatrzymaj nagrywanie';
          btn.className = 'btn-recording';
        } else {
          recording = false;
          alert('Błąd startu nagrywania: ' + (data.message || 'Niepołączony'));
        }
      } else {
        const res = await fetch('/api/record/stop', { method: 'POST' });
        const data = await res.json();
        alert('Zapisano plik CSV:\\n' + data.csv_path + '\\nPróbek: ' + data.total_samples);
        btn.innerText = 'Nagraj CSV';
        btn.className = 'btn-secondary';
      }
    }

    // BLE Scanner Modal
    function openScanner() {
      document.getElementById('scanModal').classList.add('open');
      document.getElementById('connectErrorBanner').style.display = 'none';
      scanDevices();
    }

    function closeScanner() {
      document.getElementById('scanModal').classList.remove('open');
    }

    async function scanDevices() {
      const listEl = document.getElementById('deviceList');
      listEl.innerHTML = '<div style="text-align:center; padding:1.5rem; color:var(--text-muted); font-size:0.85rem;">Skanowanie urządzeń Bluetooth LE (4 sekundy)...</div>';
      try {
        const res = await fetch('/api/scan');
        const data = await res.json();
        if (data.status === 'ok' && data.devices && data.devices.length > 0) {
          listEl.innerHTML = '';
          data.devices.forEach(d => {
            const row = document.createElement('div');
            row.className = 'device-row';
            const isMini = (d.model === 'MINI') || d.name.toUpperCase().includes('MINI');
            const isHalo = (d.model === 'HALO') || d.name.toUpperCase().includes('HALO');
            const tag = isMini ? '<span class="tag-mini">MINI (8ch)</span>' : (isHalo ? '<span class="tag-halo">HALO (4ch)</span>' : '');

            row.innerHTML = `
              <div>
                <div class="device-info-name">
                  ${d.name} ${tag}
                </div>
                <div class="device-info-addr">${d.address} • RSSI: ${d.rssi || '--'} dBm</div>
              </div>
              <button class="btn-primary" style="padding:0.35rem 0.75rem; font-size:0.78rem;" onclick="connectBrainAccess('${d.address}')">Połącz</button>
            `;
            listEl.appendChild(row);
          });
        } else {
          listEl.innerHTML = '<div style="text-align:center; padding:1.5rem; color:var(--amber); font-size:0.85rem;">Nie znaleziono czepków BrainAccess. Upewnij się, że urządzenie jest włączone i dioda LED oddycha na niebiesko.</div>';
        }
      } catch (err) {
        listEl.innerHTML = `<div style="text-align:center; padding:1.5rem; color:var(--rose); font-size:0.85rem;">Błąd skanowania: ${err.message}</div>`;
      }
    }

    // ================= Zadania eksperymentalne =================
    let TASKS = [];
    let sessionPollTimer = null;
    let lastSessionStatus = null;

    function escapeHtml(text) {
      return String(text ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }

    function rememberedId() {
      try { return localStorage.getItem('eegLastPseudonym') || ''; } catch (e) { return ''; }
    }

    function askPseudonym(question) {
      const value = prompt(question, rememberedId());
      if (value === null) return null;
      const id = value.trim();
      if (!/^[0-9A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż-]{1,40}$/.test(id)) {
        alert('ID może zawierać tylko litery, cyfry i "-" (bez spacji i "_").');
        return null;
      }
      try { localStorage.setItem('eegLastPseudonym', id); } catch (e) {}
      return id;
    }

    async function loadTasks() {
      const res = await fetch('/api/tasks');
      const data = await res.json();
      TASKS = data.tasks;
      const list = document.getElementById('taskList');
      if (!TASKS.length) {
        list.innerHTML = 'Brak zadań w katalogu <code>tasks/</code>.';
      } else {
        list.innerHTML = TASKS.map(task => `
          <div class="task-item">
            <div><span class="task-name">${escapeHtml(task.name)}</span><span class="task-code">${escapeHtml(task.code)}</span></div>
            <div class="task-desc">${escapeHtml(task.description || '')}</div>
            <div class="task-params">
              ${task.params.map(p => `
                <label>${escapeHtml(p.label || p.name)}
                  <input id="param-${task.key}-${p.name}" type="number" value="${p.default ?? ''}"
                    ${p.min !== undefined ? `min="${p.min}"` : ''} ${p.max !== undefined ? `max="${p.max}"` : ''}
                    step="${p.type === 'int' ? 1 : 'any'}">
                </label>`).join('')}
            </div>
            <div class="task-actions">
              <button class="btn-primary task-run-btn" onclick="runTask('${task.key}')">▶ Uruchom</button>
              ${task.has_analysis ? `<button class="btn-secondary" onclick="analyzeTask('${task.key}')">📊 Analiza</button>` : ''}
            </div>
          </div>`).join('');
      }
      renderSession(data.session);
      if (data.session && data.session.running) startSessionPolling();
    }

    async function runTask(key) {
      const task = TASKS.find(t => t.key === key);
      const pseudonym = askPseudonym(`${task.name}\n\nPodaj ID (pseudonim) badanego:`);
      if (!pseudonym) return;
      const params = {};
      task.params.forEach(p => { params[p.name] = document.getElementById(`param-${key}-${p.name}`).value; });
      const res = await fetch(`/api/tasks/${key}/run`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ pseudonym, params })
      });
      const data = await res.json();
      if (data.status !== 'started') { alert(data.message || 'Nie udało się uruchomić zadania.'); return; }
      renderSession(data.session);
      startSessionPolling();
    }

    function startSessionPolling() {
      if (sessionPollTimer) return;
      sessionPollTimer = setInterval(async () => {
        const res = await fetch('/api/session');
        const data = await res.json();
        renderSession(data.session);
        if (!data.session || !data.session.running) { clearInterval(sessionPollTimer); sessionPollTimer = null; }
      }, 1000);
    }

    async function abortSession() {
      if (!confirm('Przerwać zadanie? Dane zebrane do tej pory zostaną zapisane.')) return;
      await fetch('/api/session/abort', { method: 'POST' });
    }

    function fileLinks(session, files) {
      return `<div class="file-links">${files.map(f =>
        `<a href="/recordings/${encodeURIComponent(session)}/${encodeURIComponent(f)}" target="_blank">${escapeHtml(f)}</a>`).join('')}</div>`;
    }

    function renderSession(s) {
      const box = document.getElementById('sessionBox');
      document.querySelectorAll('.task-run-btn').forEach(b => b.disabled = !!(s && s.running));
      if (!s) { box.innerHTML = ''; return; }
      const labels = {starting: 'Uruchamianie', running: '● Zapis EEG + zadanie w toku', finishing: 'Kończenie zapisu...',
                      completed: '✔ Zakończono', aborted: 'Przerwano', error: 'Błąd'};
      let cls = 'session-box';
      if (s.status === 'completed') cls += s.message ? ' warn' : ' done';
      if (s.status === 'aborted') cls += ' warn';
      if (s.status === 'error') cls += ' err';
      const minutes = Math.floor(s.elapsed_sec / 60), seconds = Math.floor(s.elapsed_sec % 60);
      box.innerHTML = `
        <div class="${cls}">
          <div class="session-row"><b>${escapeHtml(labels[s.status] || s.status)}</b><span>${minutes}:${String(seconds).padStart(2, '0')}</span></div>
          <div>${escapeHtml(s.task_name)} · ID: <b>${escapeHtml(s.pseudonym)}</b></div>
          <div>Próbek EEG: <b>${s.eeg_samples}</b> · triggerów: <b>${s.events}</b></div>
          <code>${escapeHtml(s.session)}</code>
          ${s.message ? `<div style="margin-top:0.3rem;color:var(--amber)">${escapeHtml(s.message)}</div>` : ''}
          ${s.running ? `<div style="margin-top:0.45rem"><button class="btn-danger" style="padding:0.3rem 0.6rem;font-size:0.75rem" onclick="abortSession()">■ Przerwij zadanie</button></div>`
                      : fileLinks(s.session, s.files) +
                        `<div style="margin-top:0.45rem"><button class="btn-secondary" style="padding:0.3rem 0.6rem;font-size:0.75rem"
                           onclick="analyzeSession('${s.task}', '${escapeHtml(s.session)}')">📊 Analizuj tę sesję</button></div>`}
        </div>`;
      if (lastSessionStatus === 'finishing' && !s.running) loadTasks();
      lastSessionStatus = s.status;
    }

    async function analyzeTask(key) {
      const task = TASKS.find(t => t.key === key);
      const pseudonym = askPseudonym(`Analiza: ${task.name}\n\nPodaj ID (pseudonim) – zostanie użyta najnowsza sesja:`);
      if (!pseudonym) return;
      await startAnalysis(key, { pseudonym });
    }

    async function analyzeSession(key, session) {
      await startAnalysis(key, { session });
    }

    async function startAnalysis(key, body) {
      openResults('Analiza w toku...', '<div style="padding:1.5rem;text-align:center;color:var(--text-muted)">Trwa analiza – to może potrwać kilkanaście sekund.</div>');
      try {
        const res = await fetch(`/api/tasks/${key}/analyze`, {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
        });
        showAnalysis(await res.json());
      } catch (err) {
        openResults('Błąd analizy', escapeHtml(err.message));
      }
    }

    function showAnalysis(r) {
      if (!r.session) { openResults('Błąd analizy', `<p>${escapeHtml(r.message)}</p>`); return; }
      const stamp = Date.now();
      const images = (r.png || []).map(f =>
        `<a href="/recordings/${encodeURIComponent(r.session)}/${encodeURIComponent(f)}" target="_blank">
           <img class="results-png" src="/recordings/${encodeURIComponent(r.session)}/${encodeURIComponent(f)}?t=${stamp}" alt="${escapeHtml(f)}"></a>`).join('');
      openResults(`${r.status === 'ok' ? 'Wyniki analizy' : 'Błąd analizy'} — ${r.session}`, `
        ${r.status !== 'ok' ? `<p style="color:var(--rose)"><b>${escapeHtml(r.message)}</b></p>` : ''}
        ${fileLinks(r.session, r.files || [])}
        ${images}
        ${r.output ? `<details ${r.status !== 'ok' ? 'open' : ''}><summary style="margin-top:0.6rem;cursor:pointer;font-size:0.8rem">Wyjście skryptu</summary>
          <div class="analysis-log">${escapeHtml(r.output)}</div></details>` : ''}`);
    }

    async function openSessions() {
      const res = await fetch('/api/sessions');
      const data = await res.json();
      const rows = data.sessions.map(s => {
        const task = TASKS.find(t => t.code === s.task_code);
        return `<tr>
          <td><code>${escapeHtml(s.session)}</code><br><span style="color:var(--text-muted)">${escapeHtml(s.created || '')}</span></td>
          <td>${escapeHtml(s.status || '')}${s.eeg_recorded ? '' : ' · <i>bez EEG</i>'}${(s.warnings || []).length ? `<br><span style="color:var(--amber)">${escapeHtml(s.warnings.join(' '))}</span>` : ''}</td>
          <td>${fileLinks(s.session, s.files)}</td>
          <td>${task && task.has_analysis ? `<button class="btn-secondary" style="padding:0.25rem 0.5rem;font-size:0.72rem"
                 onclick="analyzeSession('${task.key}', '${escapeHtml(s.session)}')">Analizuj</button>` : ''}</td>
        </tr>`;
      }).join('');
      openResults('Nagrane sesje (recordings/)', rows
        ? `<table class="sessions-table"><tr><th>Sesja</th><th>Status</th><th>Pliki</th><th></th></tr>${rows}</table>`
        : '<p style="color:var(--text-muted)">Brak sesji.</p>');
    }

    function openResults(title, html) {
      document.getElementById('resultsTitle').innerText = title;
      document.getElementById('resultsBody').innerHTML = html;
      document.getElementById('resultsModal').classList.add('open');
    }

    function closeResults() {
      document.getElementById('resultsModal').classList.remove('open');
    }

    loadTasks();
    initWebSocket();
    renderCanvas();
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    return DASHBOARD_HTML


@app.get("/api/status")
async def api_status():
    global driver, recorder
    if driver and driver.is_connected:
        return {
            "connected": True,
            "device": driver.device_name,
            "address": driver.device_address,
            "model": driver.profile.model_name,
            "channels": driver.channels,
            "battery": driver.battery_level,
            "recording": recorder is not None,
            "samples": driver.sample_count,
        }
    return {"connected": False}


@app.get("/api/montage")
async def api_get_montage():
    global driver, current_montage, current_reference, current_bias
    channels = driver.channels if driver else current_montage
    return {
        "channels": channels,
        "positions": STANDARD_10_20_POSITIONS,
        "presets": MONTAGE_PRESETS,
        "electrode_count": driver.profile.electrode_count if driver else 8,
        "reference": current_reference,
        "bias": current_bias,
    }


@app.post("/api/montage")
async def api_set_montage(payload: dict):
    global driver, current_montage, current_reference, current_bias
    if _session_running():
        return {"status": "error", "message": "Nie można zmienić montażu w trakcie zadania."}
    new_channels = payload.get("channels")
    if not new_channels or not isinstance(new_channels, (list, tuple)):
        return {"status": "error", "message": "Expected list of channel names"}

    reference = str(payload.get("reference", current_reference)).strip()
    bias = str(payload.get("bias", current_bias)).strip()
    valid_positions = set(STANDARD_10_20_POSITIONS)
    if reference not in valid_positions or bias not in valid_positions:
        return {"status": "error", "message": "Invalid REF or BIAS position"}
    current_montage = [str(c).strip() for c in new_channels]
    current_reference = reference
    current_bias = bias
    if driver:
        driver.set_channel_mapping(current_montage)
        logger.info("Applied new montage to driver: %s", current_montage)
    return {
        "status": "ok",
        "channels": current_montage,
        "reference": current_reference,
        "bias": current_bias,
    }


@app.get("/api/scan")
async def api_scan(timeout: float = 4.0):
    try:
        devices = await HaloDriver.scan(timeout=timeout)
        # ``HaloDriver.scan`` keeps the BLEDevice object internally so the
        # caller can connect without scanning again. BLEDevice is not JSON
        # serializable, therefore never expose it through the HTTP API.
        public_devices = [
            {
                "name": item["name"],
                "address": item["address"],
                "rssi": item["rssi"],
                "is_brainaccess": item["is_brainaccess"],
                "model": item["model"],
                "match_reason": item["match_reason"],
            }
            for item in devices
        ]
        return {"status": "ok", "devices": public_devices}
    except Exception as e:
        logger.exception("Scan failed")
        return {"status": "error", "message": str(e)}


@app.post("/api/connect")
async def api_connect(payload: Optional[dict] = None):
    global driver, current_montage, gaze_analyzer
    target = None
    if payload and isinstance(payload, dict):
        target = payload.get("target")
        if payload.get("montage"):
            current_montage = [str(c).strip() for c in payload["montage"]]

    try:
        if not driver:
            driver = HaloDriver(enable_dsp=True)
        elif driver.is_connected:
            return {
                "status": "already_connected",
                "device": driver.device_name,
                "model": driver.profile.model_name,
                "channels": driver.channels,
                "battery": driver.battery_level,
            }
        else:
            try:
                await driver.disconnect()
            except Exception:
                pass
            driver = HaloDriver(enable_dsp=True)

        logger.info("Connecting to target: %s", target or "auto-discovery")
        await driver.connect(target=target, timeout=10.0)

        # Apply user montage if connected to MINI
        if driver.profile.model_name == "MINI" and current_montage:
            driver.set_channel_mapping(current_montage)

        gaze_analyzer = GazeAnalyzer(driver.channels, sample_rate=SAMPLE_RATE)
        driver.add_callback(gaze_analyzer.push_samples)
        await driver.start_stream()
        return {
            "status": "connected",
            "device": driver.device_name,
            "model": driver.profile.model_name,
            "channels": driver.channels,
            "battery": driver.battery_level,
        }
    except Exception as e:
        logger.exception("Connect failed")
        if driver:
            try:
                await driver.disconnect()
            except Exception:
                pass
        return {"status": "error", "message": str(e)}


@app.post("/api/disconnect")
async def api_disconnect():
    global driver, recorder, gaze_analyzer
    if _session_running():
        return {"status": "error", "message": "Trwa zadanie z zapisem EEG – najpierw je zakończ lub przerwij."}
    if recorder and driver:
        try:
            driver.remove_callback(recorder.write_samples)
            recorder.stop()
        except Exception:
            pass
        recorder = None

    if driver and driver.is_connected:
        if gaze_analyzer:
            driver.remove_callback(gaze_analyzer.push_samples)
        gaze_analyzer = None
        await driver.disconnect()
        return {"status": "disconnected"}
    return {"status": "not_connected"}


@app.get("/api/analysis/gaze")
async def api_gaze_status():
    if not gaze_analyzer:
        return {"status": "not_connected", "gaze": {"label": "unknown", "confidence": 0.0, "ready": False}}
    return {"status": "ok", "gaze": gaze_analyzer.classify(), "calibration": gaze_analyzer.calibration_status()}


@app.post("/api/analysis/gaze/calibration")
async def api_gaze_calibration(payload: dict):
    if not gaze_analyzer:
        return {"status": "error", "message": "Najpierw połącz urządzenie."}
    try:
        gaze_analyzer.start_calibration(
            str(payload.get("label", "")),
            float(payload.get("seconds", 2.0)),
        )
        return {"status": "started", "calibration": gaze_analyzer.calibration_status()}
    except (TypeError, ValueError) as exc:
        return {"status": "error", "message": str(exc)}


@app.post("/api/analysis/gaze/calibration/stop")
async def api_gaze_calibration_stop():
    if not gaze_analyzer:
        return {"status": "error", "message": "Najpierw połącz urządzenie."}
    return {"status": "stopped", "calibration": gaze_analyzer.stop_calibration()}


@app.post("/api/analysis/gaze/reset")
async def api_gaze_reset():
    if not gaze_analyzer:
        return {"status": "error", "message": "Najpierw połącz urządzenie."}
    gaze_analyzer.reset()
    return {"status": "ok"}


@app.post("/api/filters")
async def api_filters(settings: dict):
    if not driver or not driver.dsp:
        return {"status": "error", "message": "Najpierw połącz urządzenie."}
    try:
        driver.configure_preview_filters(
            enabled=bool(settings.get("enabled", True)),
            highpass_hz=float(settings.get("highpass_hz", 0.5)),
            lowpass_hz=float(settings.get("lowpass_hz", 45.0)),
            notch_enabled=bool(settings.get("notch_enabled", True)),
        )
        return {"status": "ok"}
    except (TypeError, ValueError) as exc:
        return {"status": "error", "message": str(exc)}


@app.post("/api/record/start")
async def api_record_start():
    global recorder, driver
    if not driver or not driver.is_connected:
        return {"status": "error", "message": "Najpierw połącz urządzenie."}
    recorder = HaloRecorder()
    meta = driver.get_summary()
    recorder.start(meta)
    driver.add_callback(recorder.write_samples)
    return {"status": "recording", "file": str(recorder.csv_path)}


@app.post("/api/record/stop")
async def api_record_stop():
    global recorder, driver
    if recorder:
        if driver:
            driver.remove_callback(recorder.write_samples)
        info = recorder.stop()
        recorder = None
        return info
    return {"status": "not_recording"}


# --------------------------------------------------------------------------
# Zadania eksperymentalne (tasks/*) – uruchamianie, zapis EEG, analiza
# --------------------------------------------------------------------------

@app.get("/api/tasks")
async def api_tasks():
    return {
        "tasks": list(discover_tasks().values()),
        "session": task_session.status_dict() if task_session else None,
    }


@app.post("/api/tasks/{task_key}/run")
async def api_task_run(task_key: str, payload: dict):
    global task_session
    task = discover_tasks().get(task_key)
    if not task:
        return {"status": "error", "message": f"Nieznane zadanie: {task_key}"}
    if _session_running():
        return {"status": "error", "message": "Inne zadanie jest w toku."}
    if not driver or not driver.is_connected:
        return {"status": "error", "message": "Najpierw połącz czepek – zapis EEG uruchamia się automatycznie z zadaniem."}
    try:
        session = TaskSession(task, str(payload.get("pseudonym", "")), payload.get("params") or {},
                              driver, current_reference, current_bias)
        await session.start()
    except (ValueError, FileExistsError) as exc:
        return {"status": "error", "message": str(exc)}
    task_session = session
    return {"status": "started", "session": session.status_dict()}


@app.get("/api/session")
async def api_session():
    return {"session": task_session.status_dict() if task_session else None}


@app.post("/api/session/abort")
async def api_session_abort():
    if not _session_running():
        return {"status": "not_running"}
    await task_session.abort()
    return {"status": "aborting"}


@app.get("/api/sessions")
async def api_sessions(task: Optional[str] = None, pseudonym: Optional[str] = None):
    task_code = discover_tasks().get(task, {}).get("code") if task else None
    return {"sessions": list_sessions(task_code, pseudonym or None)}


@app.post("/api/tasks/{task_key}/analyze")
async def api_task_analyze(task_key: str, payload: dict):
    task = discover_tasks().get(task_key)
    if not task or not task.get("has_analysis"):
        return {"status": "error", "message": f"Zadanie {task_key} nie ma skryptu analysis.py"}
    if _session_running() and task_session.task["key"] == task_key:
        return {"status": "error", "message": "Poczekaj na zakończenie zadania."}
    try:
        # Wystarczy ID – bierzemy najnowszą sesję tego zadania dla tego badanego.
        paths = resolve_session(task["code"], payload.get("pseudonym"), payload.get("session"))
    except (FileNotFoundError, ValueError) as exc:
        return {"status": "error", "message": str(exc)}
    return await run_analysis(task, paths)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    global live_sample_sequence
    await ws.accept()
    active_websockets.add(ws)
    client_sequence = live_sample_sequence
    try:
        while True:
            await asyncio.sleep(0.1)  # 10 Hz telemetry push
            if not driver or not driver.is_connected:
                await ws.send_text(json.dumps({
                    "connected": False,
                    "channels": current_montage,
                }))
                continue

            channels = driver.channels
            dsp = driver.dsp
            quality = dsp.get_signal_quality() if dsp else {}
            powers_all = dsp.get_band_powers() if dsp else {}
            try:
                psd_frequencies, psd_powers = (
                    dsp.get_psd(max_frequency=80.0) if dsp else ([], [])
                )
            except Exception:
                logger.exception("PSD calculation failed")
                psd_frequencies, psd_powers = [], []

            try:
                fooof_data = dsp.get_fooof(max_frequency=45.0) if dsp else {
                    "frequencies": [],
                    "psd": [],
                    "psd_flat": [],
                    "model_info": {"available": False, "fitted": False, "details": "Brak sterownika"},
                }
            except Exception:
                logger.exception("FOOOF calculation failed")
                fooof_data = {
                    "frequencies": [],
                    "psd": [],
                    "psd_flat": [],
                    "model_info": {"available": False, "fitted": False, "details": "Błąd obliczeń FOOOF"},
                }
            gaze = gaze_analyzer.classify() if gaze_analyzer else {
                "label": "unknown", "confidence": 0.0, "ready": False,
            }

            # Safe band powers calculation across active channels
            avg_powers = {}
            for b in BANDS:
                if powers_all and channels:
                    avg_powers[b] = sum(powers_all.get(ch, {}).get(b, 0.0) for ch in channels) / max(1, len(channels))
                else:
                    avg_powers[b] = 0.0

            # Drain recent samples from driver queue
            while not driver._queue.empty():
                s = driver._queue.get_nowait()
                sample = s if isinstance(s, dict) else s.as_dict()
                live_sample_sequence += 1
                live_sample_buffer.append((live_sample_sequence, sample))

            chunk = []
            for sequence, sample in live_sample_buffer:
                if sequence > client_sequence:
                    chunk.append(sample)
            if chunk:
                client_sequence = live_sample_buffer[-1][0]

            # Keep each client responsive even if it was briefly suspended.
            if len(chunk) > 150:
                chunk = chunk[-150:]

            payload = {
                "connected": driver.is_connected,
                "device": driver.device_name,
                "model": driver.profile.model_name,
                "channels": channels,
                "battery": driver.battery_level,
                "hz": SAMPLE_RATE,
                "samples_total": driver.sample_count,
                "dropped": driver.decoder.dropped_packets,
                "gaze": gaze,
                "band_powers": avg_powers,
                "psd": {
                    "frequencies": psd_frequencies,
                    "powers": psd_powers,
                },
                "fooof": fooof_data,
                "quality": quality,
                "chunk": chunk,
            }
            await ws.send_text(json.dumps(payload))
    except WebSocketDisconnect:
        active_websockets.discard(ws)
    except Exception:
        logger.exception("WebSocket telemetry loop failed")
        active_websockets.discard(ws)


def run_web(port: int = 8420):
    print(f"\nBrainAccess EEG Web Dashboard uruchomiony pod adresem: http://localhost:{port}")
    webbrowser.open(f"http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    run_web()