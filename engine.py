"""GoAnywhere engine — wraps pymobiledevice3 for iOS GPS spoofing.

Handles: device discovery, developer-mode/tunnel status, fixed location
set/clear, motion interpolation (walk/bike/drive), GPX routes, history.
All local, no accounts, no limits.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if getattr(sys, "frozen", False):
    # bundled exe: keep user data in %LOCALAPPDATA%, not inside the bundle
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "GoAnywhere"
else:
    DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
HISTORY_FILE = DATA_DIR / "history.json"
PM3 = [sys.executable, "-m", "pymobiledevice3"]

# iAnyGo speed presets (km/h)
SPEED_PRESETS = {"walking": 7.0, "biking": 19.0, "driving": 30.0}


def haversine_m(a, b) -> float:
    r = 6_371_000.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


@dataclass
class Motion:
    """Interpolated move from current point along waypoints at speed km/h."""
    waypoints: list[tuple[float, float]]
    speed_kmh: float
    index: int = 0
    pos: tuple[float, float] = (0.0, 0.0)
    started: float = field(default_factory=time.monotonic)
    state: str = "idle"          # idle | running | paused | done
    paused_at: float = 0.0
    pause_offset: float = 0.0

    @property
    def total_distance_m(self) -> float:
        pts = self.waypoints
        return sum(haversine_m(pts[i], pts[i + 1]) for i in range(len(pts) - 1))

    def position_at(self, elapsed_s: float) -> tuple[float, float]:
        """Walk the polyline; return interpolated point at elapsed time."""
        remaining = self.speed_kmh * 1000 / 3600 * elapsed_s
        pts = self.waypoints
        for i in range(len(pts) - 1):
            seg = haversine_m(pts[i], pts[i + 1])
            if seg < 0.01:
                continue
            if remaining <= seg:
                f = remaining / seg
                return (pts[i][0] + (pts[i + 1][0] - pts[i][0]) * f,
                        pts[i][1] + (pts[i + 1][1] - pts[i][1]) * f)
            remaining -= seg
        return pts[-1]

    def tick(self) -> tuple[tuple[float, float] | None, bool]:
        """Return (point_to_push, finished)."""
        if self.state != "running":
            return None, False
        elapsed = time.monotonic() - self.started - self.pause_offset
        pos = self.position_at(elapsed)
        total = self.total_distance_m
        done = self.speed_kmh > 0 and elapsed >= total / (self.speed_kmh * 1000 / 3600)
        if done:
            self.state = "done"
            return pts_last(self.waypoints), True
        self.pos = pos
        return pos, False


def pts_last(pts):
    return pts[-1]


class Engine:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.current: tuple[float, float] | None = None
        self.motion: Motion | None = None
        self.device: dict | None = None
        self.spoofing = False
        self.last_error: str | None = None
        self.history: list[dict] = self._load_history()
        self._tasks: set[asyncio.Task] = set()

    # ---------- device ----------
    _dev_cache: tuple[float, dict | None] | None = None
    _dev_lock: asyncio.Lock | None = None

    async def refresh_device(self) -> dict | None:
        # the UI polls every 2 s; serve from a 5 s cache to keep /api/status
        # instant and stop hammering usbmux
        import time as _t
        if self._dev_cache and _t.time() - self._dev_cache[0] < 5:
            return self._dev_cache[1]
        if Engine._dev_lock is None:
            Engine._dev_lock = asyncio.Lock()
        async with Engine._dev_lock:
            # re-check: another waiter may have refreshed while we queued
            if self._dev_cache and _t.time() - self._dev_cache[0] < 5:
                return self._dev_cache[1]
            result = await self._refresh_device_now()
            Engine._dev_cache = (_t.time(), result)
            return result

    async def _refresh_device_now(self) -> dict | None:
        if getattr(sys, "frozen", False):
            # in the frozen exe sys.executable is the app itself — use the
            # pymobiledevice3 API in-process instead of its CLI
            try:
                from pymobiledevice3.usbmux import list_devices
                devs = await list_devices()
                if not devs:
                    self.device = None
                    return None
                dev = devs[0]
                info = {"UniqueDeviceID": dev.serial, "DeviceName": "iPhone",
                        "ProductVersion": "", "ProductType": "",
                        "ConnectionType": dev.connection_type}
                try:
                    from pymobiledevice3.lockdown import create_using_usbmux
                    ld = await create_using_usbmux(serial=dev.serial)
                    vals = ld.all_values
                    info["DeviceName"] = vals.get("DeviceName", "iPhone")
                    info["ProductVersion"] = vals.get("ProductVersion", "")
                    info["ProductType"] = vals.get("ProductType", "")
                except Exception:  # noqa: BLE001
                    pass  # basic info is enough
                self.device = info
            except Exception as e:  # noqa: BLE001
                self.device = None
                self.last_error = f"device scan: {type(e).__name__}: {e}"
            return self.device
        out = await self._run(PM3 + ["usbmux", "list"], timeout=20)
        try:
            devs = json.loads(out)
        except (json.JSONDecodeError, TypeError):
            self.device = None
            return None
        self.device = devs[0] if devs else None
        return self.device

    async def developer_mode(self) -> bool | None:
        if getattr(sys, "frozen", False):
            return None  # CLI unavailable; verify-op surfaces the real state
        out = await self._run(PM3 + ["amfi", "developer-mode-status"], timeout=60)
        t = out.strip().splitlines()
        if not t:
            return None
        v = t[-1].strip().lower()
        return v == "true" if v in ("true", "false") else None

    # ---------- spoofing via persistent worker ----------
    worker: asyncio.subprocess.Process | None = None
    worker_ready: bool = False
    conn_reader = None
    conn_writer = None
    _latest: tuple[float, float] | None = None   # coalescing slot for set()
    _worker_lock: asyncio.Lock | None = None

    async def _ensure_worker(self):
        if Engine._worker_lock is None:
            Engine._worker_lock = asyncio.Lock()
        async with Engine._worker_lock:
            if self.worker is not None and self.worker.returncode is None and self.worker_ready:
                return True
            await self._spawn_worker()
            return self.worker_ready

    async def _spawn_worker(self):
        self.worker_ready = False
        self.conn = None
        # stale trust state: turn the cryptic lockdownd failure into advice
        if self.last_error and "SavePairRecordFailed" in self.last_error:
            self.last_error = ("iPhone refused pairing — unlock it and tap "
                               '"Trust", then try again (Settings > General '
                               "> Transfer or Reset > Reset Location & Privacy "
                               "if it still refuses)")
        # never leave an old worker alive: only one process may hold the
        # RSD tunnel, and a zombie keeps grabbing it
        old = self.worker
        if old is not None and old.returncode is None:
            try:
                old.kill()
            except ProcessLookupError:
                pass
        for w in (self.conn_writer, old.stdin if old else None):
            try:
                if w is not None:
                    w.close()
            except Exception:  # noqa: BLE001
                pass
        self.conn_reader = self.conn_writer = None
        srv = None
        if getattr(sys, "frozen", False):
            # frozen exe: re-launch ourselves in worker mode over a socket
            loop = asyncio.get_event_loop()
            conn_fut: asyncio.Future = loop.create_future()

            async def _on_conn(reader, writer):
                if not conn_fut.done():
                    conn_fut.set_result((reader, writer))

            srv = await asyncio.start_server(_on_conn, "127.0.0.1", 0)
            port = srv.sockets[0].getsockname()[1]
            cmd = [sys.executable, "--worker", self.device["UniqueDeviceID"] if self.device else "-",
                   f"tcp:{port}"]
            stdin_pipe = stdout_pipe = None
        else:
            cmd = [sys.executable, str(APP_DIR / "spoof_worker.py")]
            if self.device:
                cmd.append(self.device["UniqueDeviceID"])
            stdin_pipe, stdout_pipe = asyncio.subprocess.PIPE, asyncio.subprocess.PIPE
        try:
            self.worker = await asyncio.create_subprocess_exec(
                *cmd, stdin=stdin_pipe, stdout=stdout_pipe,
                stderr=subprocess.DEVNULL, cwd=str(APP_DIR))
        except OSError as e:
            self.last_error = f"spawn failed: {e}"
            if srv:
                srv.close()
            return
        if srv is not None:
            try:
                res = await self._race_worker(conn_fut, 60)
            except (asyncio.TimeoutError, OSError, asyncio.CancelledError):
                res = None
            srv.close()
            if res is None:
                self.last_error = "worker did not connect back "\
                    "(developer mode off, device unplugged, or not trusted)"
                await self._kill_worker()
                return
            self.conn_reader, self.conn_writer = res
        first = await self._race_worker(self._read_worker_line(timeout=90),
                                        95)
        if first and first.get("ready"):
            self.worker_ready = True
            self.last_error = None
            src = self.conn_reader or (self.worker.stdout if self.worker else None)
            t = asyncio.create_task(self._drain_worker(self.worker, src))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
            # verify the DVT location channel actually opens (developer mode etc.)
            self._send_worker({"op": "verify"})
        else:
            self.last_error = (first or {}).get("fatal", "worker died at startup "
                             "(developer mode off, device unplugged, or not trusted)")
            self.worker_ready = False
            await self._kill_worker()

    async def _race_worker(self, awaitable, timeout):
        """Await awaitable, but return None early if the worker dies."""
        task = asyncio.ensure_future(awaitable)
        death = asyncio.ensure_future(self.worker.wait()) \
            if self.worker is not None else None
        wait_for = {task} | ({death} if death else set())
        try:
            done, pending = await asyncio.wait(wait_for, timeout=timeout,
                                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            for p in pending:
                p.cancel()
        if task in done:
            try:
                return task.result()
            except (Exception, asyncio.CancelledError):
                return None
        return None  # worker died or timed out

    async def _kill_worker(self):
        if self.worker is not None and self.worker.returncode is None:
            try:
                self.worker.kill()
                await self.worker.wait()
            except Exception:  # noqa: BLE001
                pass

    async def stop_worker(self):
        """Kill the tunnel worker (e.g. after a pairing reset)."""
        if Engine._worker_lock is None:
            Engine._worker_lock = asyncio.Lock()
        async with Engine._worker_lock:
            self.stop_motion()
            await self._kill_worker()
            self.worker_ready = False
            self.spoofing = False

    async def _read_worker_line(self, timeout: float = 30) -> dict | None:
        src = self.conn_reader if self.conn_reader is not None else (
            self.worker.stdout if self.worker else None)
        if src is None:
            return None
        try:
            data = await asyncio.wait_for(src.readline(), timeout=timeout)
        except (asyncio.TimeoutError, ConnectionError):
            return None
        if not data:
            return None
        try:
            return json.loads(data.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return None

    def _send_worker(self, obj: dict) -> bool:
        data = (json.dumps(obj) + "\n").encode()
        try:
            if self.conn_writer is not None:
                if self.conn_writer.is_closing():
                    return False
                self.conn_writer.write(data)
            elif self.worker is not None and self.worker.stdin is not None:
                self.worker.stdin.write(data)
            else:
                return False
            return True
        except (ConnectionError, OSError, BrokenPipeError):
            return False

    async def _drain_worker(self, proc, src):
        """Sole reader of one worker's stdout; tracks errors and liveness."""
        while proc.returncode is None:
            if src is None:
                break
            try:
                data = await asyncio.wait_for(src.readline(), timeout=10)
            except asyncio.TimeoutError:
                continue
            except (ConnectionError, OSError):
                break
            if not data:      # EOF — stop, no busy loop
                break
            try:
                line = json.loads(data.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if line.get("fatal"):
                self.last_error = line["fatal"]
                self.worker_ready = False
                self.spoofing = False
            elif line.get("ok") is False:
                self.last_error = line.get("err", "worker error")
        if self.worker is proc:
            self.worker_ready = False
            self.spoofing = False

    gamemode = None   # GameModeManager, wired by server; None = USB/DVT only

    def _bt_feeding(self) -> bool:
        """True when the Bluetooth accessory is streaming to the phone.
        While it feeds, we MUST NOT use the USB developer API: PoGo flags
        simulated fixes (isSimulatedBySoftware) -> Error 12. The iAP2 NMEA
        stream is the phone's GPS source instead."""
        if self.gamemode is None:
            return False
        try:
            return self.gamemode.snapshot().get("phase") in (
                "authenticated", "streaming")
        except Exception:  # noqa: BLE001
            return False

    async def set_location(self, lat: float, lng: float) -> tuple[bool, str]:
        if self._bt_feeding():
            # position travels over Bluetooth to the iPhone as accessory
            # GPS (the game_mode runner polls /api/status); skip DVT
            self.current = (lat, lng)
            self.spoofing = True
            return True, "bluetooth"
        if not await self._ensure_worker():
            return False, self.last_error or "worker not ready"
        if not self._send_worker({"op": "set", "lat": lat, "lng": lng}):
            self.worker_ready = False
            return False, "worker pipe closed"
        self.current = (lat, lng)
        self.spoofing = True
        return True, "ok"

    async def clear_location(self) -> tuple[bool, str]:
        self.stop_motion()      # stop first so a running loop cannot
        # re-spoof right after the clear lands
        if self._bt_feeding():
            self.spoofing = False
            self.current = None
            return True, "bluetooth"
        if not await self._ensure_worker():
            return False, self.last_error or "worker not ready"
        if not self._send_worker({"op": "clear"}):
            self.worker_ready = False
        self.spoofing = False
        return True, "ok"

    async def get_sim_state(self) -> str:
        return "simulating" if self.spoofing else "real"

    # ---------- motion ----------
    def stop_motion(self):
        if self.motion and self.motion.state in ("running", "paused"):
            self.motion.state = "done"

    async def move_to(self, lat: float, lng: float, speed_kmh: float,
                      waypoints: list[tuple[float, float]] | None = None,
                      follow_roads: bool = False):
        async with self.lock:
            self.stop_motion()
            # start from the live position; if unknown, start at the route's
            # own first waypoint — never a phantom default city
            start = self.current or (
                tuple(waypoints[0]) if waypoints else (lat, lng))
            pts = list(waypoints) if waypoints else [start, (lat, lng)]
            if pts[0] != start:
                pts = [start] + pts
            if follow_roads:
                road = await self.osrm_chain(pts)
                if road:
                    pts = road
            self.motion = Motion(waypoints=pts, speed_kmh=speed_kmh, pos=start)
            self.motion.state = "running"
            t = asyncio.create_task(self._motion_loop(self.motion))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        return self.motion

    async def osrm_chain(self, pts) -> list[tuple[float, float]] | None:
        """Road polyline through every waypoint. Tries ONE request with all
        coordinates (demo server accepts up to 500), then falls back to one
        request per leg. Any total failure -> None so the caller keeps the
        straight polyline."""
        pts = [tuple(p) for p in pts]
        legs = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)
                if haversine_m(pts[i], pts[i + 1]) >= 1.0]
        if not legs or len(legs) > 50:
            return None
        if any(haversine_m(a, b) > 100_000 for a, b in legs) or \
                sum(haversine_m(a, b) for a, b in legs) > 400_000:
            return None
        # single-request path: all waypoints in order
        road = await self._osrm_all(pts)
        if road:
            return road
        # leg-by-leg fallback
        out: list[tuple[float, float]] = []
        for a, b in legs:
            leg = await self.osrm_route(a, b)
            if not leg:
                return None
            # OSRM snaps to the nearest road; keep the exact click points
            # at both ends so the route really starts/ends where asked
            for p in [a] + leg + [b]:
                if not out or haversine_m(out[-1], p) >= 0.05:
                    out.append(p)    # also drops the shared joint between legs
        return out if len(out) >= 2 else None

    async def _osrm_all(self, pts) -> list[tuple[float, float]] | None:
        """One OSRM call through ALL points, first/last pinned exactly."""
        coords = ";".join(f"{p[1]:.6f},{p[0]:.6f}" for p in pts)
        url = ("https://router.project-osrm.org/route/v1/driving/"
               f"{coords}?overview=full&geometries=geojson")
        try:
            import aiohttp
            async with aiohttp.ClientSession(
                    headers={"User-Agent": "GoAnywhere/1.0"}) as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=12)) as r:
                    if r.status != 200:
                        return None
                    data = await r.json(content_type=None)
            routes = data.get("routes") or []
            if not routes:
                return None
            coords = routes[0]["geometry"]["coordinates"]
            road = [(c[1], c[0]) for c in coords]
            if len(road) < 2:
                return None
            road[0], road[-1] = tuple(pts[0]), tuple(pts[-1])
            return road
        except Exception:  # noqa: BLE001
            return None

    async def osrm_route(self, start, end) -> list[tuple[float, float]] | None:
        """Road geometry start->end from the public OSRM demo server."""
        url = (f"https://router.project-osrm.org/route/v1/driving/"
               f"{start[1]:.6f},{start[0]:.6f};{end[1]:.6f},{end[0]:.6f}"
               f"?overview=full&geometries=geojson")
        try:
            import aiohttp
            async with aiohttp.ClientSession(
                    headers={"User-Agent": "GoAnywhere/1.0"}) as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        return None
                    data = await r.json(content_type=None)
            routes = data.get("routes") or []
            if not routes:
                return None
            coords = routes[0]["geometry"]["coordinates"]
            pts = [(c[1], c[0]) for c in coords]
            return pts if len(pts) >= 2 else None
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger("goanywhere").warning("osrm failed: %s", e)
            return None

    async def _motion_loop(self, m: Motion):
        consecutive_fails = 0
        while m.state == "running":
            pos, done = m.tick()
            if pos is not None:
                ok, out = await self.set_location(*pos)
                if ok:
                    consecutive_fails = 0
                else:
                    consecutive_fails += 1
                    if consecutive_fails >= 3:
                        m.state = "done"
                        break
            if done:
                self.record("route", m.waypoints)
                break
            await asyncio.sleep(1.0)

    # ---------- joystick ----------
    _joy_task: asyncio.Task | None = None
    _joy_vec: tuple[float, float] = (0.0, 0.0)   # dx, dy in -1..1 (screen space)

    async def joystick(self, dx: float, dy: float, speed_kmh: float):
        """Continuous vector move. Called with (0,0) to stop."""
        self._joy_vec = (dx, dy)
        if (dx or dy) and (self._joy_task is None or self._joy_task.done()):
            self._joy_task = asyncio.create_task(self._joystick_loop(speed_kmh))

    async def _joystick_loop(self, speed_kmh: float):
        step_m = speed_kmh * 1000 / 3600  # per second
        while True:
            dx, dy = self._joy_vec
            if not (dx or dy):
                await asyncio.sleep(0.3)
                if not any(self._joy_vec):
                    break
            lat, lng = self.current or (0, 0)
            if self.current is None:
                await asyncio.sleep(0.5)   # no known position yet
                continue
            # ~111320 m per degree both ways (good enough at this precision)
            nlat = lat + (dy * step_m) / 111_320
            nlng = lng + (dx * step_m) / (111_320 * max(0.01, math.cos(math.radians(lat))))
            await self.set_location(nlat, nlng)
            await asyncio.sleep(1.0)

    # ---------- GPX ----------
    @staticmethod
    def parse_gpx(text: str) -> list[tuple[float, float]]:
        root = ET.fromstring(text)
        ns = "{http://www.topografix.com/GPX/1/1}"
        pts = []
        for trkpt in root.iter(f"{ns}trkpt"):
            pts.append((float(trkpt.get("lat")), float(trkpt.get("lon"))))
        if not pts:  # tolerate waypoints-only gpx
            for wpt in root.iter(f"{ns}wpt"):
                pts.append((float(wpt.get("lat")), float(wpt.get("lon"))))
        return pts

    # ---------- history ----------
    def _load_history(self) -> list[dict]:
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def record(self, kind: str, pts: list[tuple[float, float]]):
        entry = {
            "type": 1 if kind == "teleport" else 2,
            "startCoordinate": {"latitude": pts[0][0], "longitude": pts[0][1]},
            "endCoordinate": {"latitude": pts[-1][0], "longitude": pts[-1][1]},
            "data": [{"latitude": p[0], "longitude": p[1]} for p in pts[1:-1]] if len(pts) > 2 else [],
            "distance": round(haversine_m(pts[0], pts[-1]) if len(pts) == 2 else
                              sum(haversine_m(pts[i], pts[i+1]) for i in range(len(pts)-1))),
            "collectTimestamp": int(time.time() * 1000),
        }
        self.history.insert(0, entry)
        self.history = self.history[:500]
        HISTORY_FILE.write_text(json.dumps(self.history, indent=1), encoding="utf-8")

    # ---------- utils ----------
    async def _run(self, cmd: list[str], timeout: int = 60) -> str:
        rc, out = await self._run_capture(cmd, timeout=timeout)
        return out if rc == 0 else ""

    async def _run_capture(self, cmd: list[str], timeout: int = 60) -> tuple[int, str]:
        env = dict(os.environ)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        try:
            data = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "timeout"
        text = data[0].decode("utf-8", "replace") if data and data[0] else ""
        return proc.returncode or 0, text
