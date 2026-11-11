"""On-demand, bounded PresentMon frame history for the overlay graph."""
from __future__ import annotations

import csv
import math
import os
import subprocess
import threading
import time
import uuid
from collections import deque


class FrameHistory:
    def __init__(self, pid):
        self.pid = pid
        self.chain = None
        self.values = deque(maxlen=180)
        self.last_sample = 0.0

    def add(self, row, now):
        try:
            if int(row.get("ProcessID", 0)) != self.pid:
                return False
            value = float(row.get("FrameTime") or row.get("MsBetweenPresents") or 0)
            if not math.isfinite(value) or not 0 < value <= 10000:
                return False
        except (ValueError, TypeError):
            return False
        chain = row.get("SwapChainAddress")
        if self.chain is not None and chain and chain != self.chain:
            return False
        if chain:
            self.chain = chain
        self.values.append(value)
        self.last_sample = now
        return True

    def snapshot(self, now):
        values = list(self.values)
        recent = values[-60:]
        fresh = bool(recent and now - self.last_sample < 2)
        mean = sum(recent) / len(recent) if fresh else None
        return {"samples": values, "frame_ms": round(mean, 2) if mean else None,
                "fps": round(1000 / mean, 1) if mean else None,
                "status": "" if fresh else "Waiting for application frames..."}


class LivePerformanceGraph:
    def __init__(self, state, indicator, executable):
        self.state, self.indicator, self.executable = state, indicator, executable
        self._lock = threading.RLock()
        self._session = None
        self._workers = set()

    def toggle(self, enabled=None):
        with self._lock:
            if enabled is False or self._session is not None:
                self.stop()
                return True
            executable = self.executable()
            pid = self.state.scaling_target_pid or (self.state.current_scaled_target or {}).get("pid")
            if not executable or not pid:
                self.indicator.log("Install PresentMon in Performance & Benchmark to use the live graph", kind="error")
                return False
            session = {"exe": str(executable), "name": f"LSCompanionOverlay-{uuid.uuid4().hex}",
                       "history": FrameHistory(pid), "stop": threading.Event(), "process": None}
            self._session = session
            worker = threading.Thread(target=self._capture, args=(session,), daemon=True,
                                      name="OverlayPerformance")
            self._workers.add(worker)
            worker.start()
            self.refresh()
            return True

    def refresh(self):
        with self._lock:
            if self._session:
                self.indicator.set_performance_graph(self._session["history"].snapshot(time.monotonic()))

    def stop(self, wait=False):
        with self._lock:
            session, self._session = self._session, None
            if session:
                session["stop"].set()
                process = session["process"]
                if process and process.poll() is None:
                    try:
                        process.terminate()
                    except OSError:
                        pass
            workers = list(self._workers)
            self.indicator.set_performance_graph(None)
        if wait:
            for worker in workers:
                worker.join(timeout=5)

    def _capture(self, session):
        process = None
        try:
            command = [session["exe"], "--process_id", str(session["history"].pid),
                       "--output_stdout", "--no_console_stats", "--session_name", session["name"],
                       "--terminate_on_proc_exit"]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8-sig", errors="replace", bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            with self._lock:
                session["process"] = process
                if session["stop"].is_set():
                    process.terminate()
            header = None
            for raw in process.stdout:
                if session["stop"].is_set():
                    break
                cells = next(csv.reader([raw]))
                if "ProcessID" in cells and ("FrameTime" in cells or "MsBetweenPresents" in cells):
                    header = cells
                elif header:
                    with self._lock:
                        session["history"].add(dict(zip(header, cells)), time.monotonic())
            if not session["stop"].is_set():
                self.indicator.log("Live graph capture ended; check PresentMon and the application", kind="error")
        except (OSError, ValueError):
            if not session["stop"].is_set():
                self.indicator.log("Could not start the live graph; check PresentMon installation", kind="error")
        finally:
            # Only terminate this graph's uniquely named ETW session. Other
            # captures, diagnostics and benchmark sessions are never stopped.
            if process is not None:
                try:
                    subprocess.run([session["exe"], "--session_name", session["name"],
                                    "--terminate_existing_session"], timeout=2,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
                if process.stdout:
                    process.stdout.close()
            with self._lock:
                if self._session is session:
                    self._session = None
                    self.indicator.set_performance_graph(None)
                self._workers.discard(threading.current_thread())
