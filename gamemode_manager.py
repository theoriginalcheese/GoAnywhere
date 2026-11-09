"""Game Mode supervisor: runs game_mode.py as a subprocess and tracks its
JSON-line events so the UI can show pairing/auth/stream status live."""
from __future__ import annotations

import asyncio
import json
import subprocess  # noqa: F401  (kept for parity with engine patterns)
import sys
import time
from pathlib import Path

APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


class GameModeManager:
    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.state = {
            "running": False, "phase": "off",   # off|starting|paired|authenticated|streaming|reconnecting|error
            "fixes": 0, "detail": "", "updated": 0.0,
        }
        self._lock = asyncio.Lock()

    def _set(self, phase: str, detail: str = ""):
        self.state.update(phase=phase, detail=detail,
                          updated=time.time())

    async def start(self):
        async with self._lock:
            if self.proc is not None and self.proc.returncode is None:
                return {"ok": True, "detail": "already running"}
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "--gamemode"]
            else:
                cmd = [sys.executable, str(APP_DIR / "game_mode.py"),
                       "send", "--duration", "0"]
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE,
                    stderr=subprocess.DEVNULL, cwd=str(APP_DIR))
            except OSError as e:
                self._set("error", f"spawn failed: {e}")
                return {"ok": False, "detail": str(e)}
            self._set("starting", "launching Bluetooth accessory session")
            asyncio.create_task(self._pump(self.proc))
            return {"ok": True}

    async def stop(self):
        async with self._lock:
            if self.proc is not None and self.proc.returncode is None:
                try:
                    self.proc.kill()
                    await self.proc.wait()
                except Exception:  # noqa: BLE001
                    pass
            self._set("off")
            return {"ok": True}

    async def _pump(self, proc):
        """Read one runner's stdout; update state. Ignore stale pumps."""
        assert proc.stdout is not None
        while True:
            try:
                data = await asyncio.wait_for(proc.stdout.readline(), 30)
            except (asyncio.TimeoutError, ConnectionError, OSError):
                if proc.returncode is not None:
                    break
                continue
            if not data:
                break
            try:
                ev = json.loads(data.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if self.proc is not proc:      # superseded by a restart
                break
            self._apply(ev)
        if self.proc is proc and self.state["phase"] not in ("off", "error"):
            code = proc.returncode
            self._set("error" if code not in (0, None) else "off",
                      f"runner exited ({code})" if code else "")

    def _apply(self, ev: dict):
        e = ev.get("event")
        if e == "phone":
            self._set("paired", f"paired with {ev.get('name', 'phone')}")
        elif e == "authentication_succeeded":
            self._set("authenticated", "MFi authentication accepted")
        elif e == "location_requested":
            self._set("streaming", "iPhone is requesting GPS fixes")
        elif e == "fix_sent":
            self.state["fixes"] = ev.get("count", self.state["fixes"])
            self.state["updated"] = time.time()
            if self.state["phase"] != "streaming":
                self._set("streaming")
        elif e == "disconnected":
            self._set("authenticated", "link dropped — reconnecting")
        elif e == "error":
            self._set("error", f"{ev.get('type', 'error')}: "
                               f"{ev.get('detail', '')}"[:160])
        elif e in ("session_end", "finished", "interrupted"):
            self._set("off", e)

    def snapshot(self) -> dict:
        running = self.proc is not None and self.proc.returncode is None
        return {**self.state, "running": running}
