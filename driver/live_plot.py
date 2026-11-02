"""
BrainAccess HALO — Real-time Multi-channel Matplotlib Visualizer.
"""

import asyncio
import sys
import threading
import time
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

from .brainaccess_halo import HaloDriver, CHANNELS, SAMPLE_RATE

class HaloLivePlotter:
    def __init__(self, driver: HaloDriver, window_sec: float = 4.0):
        self.driver = driver
        self.window_sec = window_sec
        self.channels = list(driver.channels)
        self.n_points = int(window_sec * SAMPLE_RATE)
        self.time_axis = np.linspace(-window_sec, 0, self.n_points)

        # Plot setup
        plt.style.use("dark_background")
        n_ch = len(self.channels)
        self.fig, axes = plt.subplots(n_ch, 1, figsize=(10, 2.2 * n_ch), sharex=True)
        self.axes = [axes] if n_ch == 1 else list(axes)
        
        model_name = driver.profile.model_name
        self.fig.suptitle(f"BrainAccess {model_name} — Real-time {n_ch}-Channel EEG (µV)", fontsize=14, fontweight="bold", color="#38bdf8")

        palette = ["#38bdf8", "#818cf8", "#34d399", "#fbbf24", "#f43f5e", "#a855f7", "#06b6d4", "#f97316"]
        self.lines = {}
        for idx, ch in enumerate(self.channels):
            ax = self.axes[idx]
            col = palette[idx % len(palette)]
            line, = ax.plot(self.time_axis, np.zeros(self.n_points), color=col, lw=1.2, label=ch)
            ax.set_ylabel(f"{ch} (µV)", fontsize=10, fontweight="bold", color=col)
            ax.set_ylim(-80, 80)
            ax.grid(True, linestyle="--", alpha=0.3, color="#64748b")
            ax.legend(loc="upper right", framealpha=0.4)
            self.lines[ch] = line

        self.axes[-1].set_xlabel("Czas (s)", fontsize=11)
        self.fig.tight_layout()

    def update(self, frame):
        if not self.driver.dsp:
            return list(self.lines.values())

        for ch in self.channels:
            buf = self.driver.dsp.filtered_buffers.get(ch, np.array([]))
            if len(buf) >= self.n_points:
                data = buf[-self.n_points:]
            else:
                data = np.pad(buf, (self.n_points - len(buf), 0), mode="constant")
            if ch in self.lines:
                self.lines[ch].set_ydata(data)

        # Dynamic auto-scale every 2 seconds
        if frame % 40 == 0:
            for idx, ch in enumerate(self.channels):
                buf = self.driver.dsp.filtered_buffers.get(ch, np.array([]))
                if len(buf) > SAMPLE_RATE:
                    p2p = np.ptp(buf[-SAMPLE_RATE:])
                    limit = max(40.0, min(250.0, p2p * 0.8))
                    self.axes[idx].set_ylim(-limit, limit)

        return list(self.lines.values())

    def start(self):
        ani = FuncAnimation(self.fig, self.update, interval=50, blit=True)
        plt.show()


async def run_live_plot(target: str = None):
    driver = HaloDriver(enable_dsp=True)
    print("Łączenie z BrainAccess HALO...")
    await driver.connect(target=target)
    await driver.start_stream()
    print("Strumieniowanie rozpoczęte! Uruchamianie okna wykresu...")

    # Run matplotlib on the main thread
    plotter = HaloLivePlotter(driver)
    
    # Run async loop in background thread while matplotlib runs GUI main loop
    loop = asyncio.get_running_loop()
    
    def cleanup():
        print("Zamykanie...")
        asyncio.run_coroutine_threadsafe(driver.stop_stream(), loop)
        asyncio.run_coroutine_threadsafe(driver.disconnect(), loop)

    import atexit
    atexit.register(cleanup)
    plotter.start()

if __name__ == "__main__":
    target_dev = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(run_live_plot(target_dev))
