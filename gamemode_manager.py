"""Game Mode supervisor: runs game_mode.py as a subprocess, tracks its
JSON-line events for the UI, and keeps it alive — auto-starts on app launch
(remembered preference) and restarts the runner whenever it dies (phone out
of range, link drop, RFCOMM hiccup)."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess  # noqa: F401  (kept for parity with engine patterns)
import sys
import time
from pathlib import Path

APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
CONFIG_PATH = (Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
               / "GoAnywhere" / "config.json")
RESTART_DELAY = 15.0     # seconds after a runner death before respawning


def _load_want() -> bool:
    try:
        return bool(json.loads(CONFIG_PATH.read_text()).get("gamemode", True))
    except Exception:  # noqa: BLE001
        return True      # default ON: the USB feed triggers Pokemon Error 12


def _save_want(v: bool):
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps({"gamemode": bool(v)}))
    except OSError:
        pass



class GameModeManager:
    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.state = {
            "running": False, "phase": "off",   # off|starting|paired|authenticated|streaming|reconnecting|error
            "fixes": 0, "detail": "", "updated": 0.0,
        }
        self._lock = asyncio.Lock()
        self._want = False          # user wants Game Mode on (persisted)
        self._watchdog: asyncio.Task | None = None

    def _set(self, phase: str, detail: str = ""):
        self.state.update(phase=phase, detail=detail,
                          updated=time.time())

    async def autostart(self):
        """Called on server startup: start Game Mode unless the user last
        turned it off."""
        self._want = _load_want()
        if self._want:
            await self.start()

    async def start(self):
        self._want = True
        _save_want(True)
        async with self._lock:
            await self._spawn_inner()
        self._ensure_watchdog()
        return {"ok": True} if (self.proc and self.proc.returncode is None) \
            else {"ok": False, "detail": self.state.get("detail", "spawn failed")}

    async def stop(self):
        self._want = False
        _save_want(False)
        async with self._lock:
            if self.proc is not None and self.proc.returncode is None:
                try:
                    self.proc.kill()
                    await self.proc.wait()
                except Exception:  # noqa: BLE001
                    pass
            self._set("off")
        return {"ok": True}

    async def _spawn_inner(self):
        """Start the runner if not already up. Caller holds the lock."""
        if self.proc is not None and self.proc.returncode is None:
            return
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
            return
        if self.state["phase"] in ("off", "error", "reconnecting"):
            self._set("starting", "launching Bluetooth accessory session")
        asyncio.create_task(self._pump(self.proc))

    def _ensure_watchdog(self):
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._watch())

    async def _watch(self):
        """While the user wants Game Mode, respawn the runner a few seconds
        after it dies — phone out of range or BT hiccups must not silently
        drop us back onto the flagged USB feed."""
        next_ok = 0.0
        was_alive = False
        while self._want:
            alive = self.proc is not None and self.proc.returncode is None
            now = time.time()
            if alive:
                was_alive = True
                next_ok = 0.0
            elif now >= next_ok:
                if was_alive:
                    self._set("reconnecting",
                              "Bluetooth runner died — restarting")
                    was_alive = False
                async with self._lock:
                    await self._spawn_inner()
                next_ok = now + RESTART_DELAY
            await asyncio.sleep(2)

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
        # session_end / finished: leave phase alone; the watchdog respawns
        # while the user wants Game Mode, so a transient end is recoverable.

    def snapshot(self) -> dict:
        running = self.proc is not None and self.proc.returncode is None
        return {**self.state, "running": running}
