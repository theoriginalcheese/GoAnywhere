"""GoAnywhere server — local web UI for iOS GPS spoofing (iAnyGo-style).

Run:  .venv/Scripts/python.exe server.py   (then open http://127.0.0.1:8642)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from aiohttp import web

from engine import DATA_DIR, SPEED_PRESETS, Engine, haversine_m
from gamemode_manager import GameModeManager

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys._MEIPASS)  # bundled resources live here
else:
    APP_DIR = Path(__file__).resolve().parent
engine = Engine()
gmgr = GameModeManager()
engine.gamemode = gmgr   # while BT accessory streams, engine skips the
# USB developer feed so PoGo sees real accessory GPS, not simulated fixes


def _alog(msg: str) -> None:
    try:
        with open(DATA_DIR / "app.log", "a", encoding="utf-8") as f:
            import datetime
            f.write(f"{datetime.datetime.now():%F %T} {msg}\n")
    except OSError:
        pass


@web.middleware
async def _err_log(request, handler):
    try:
        return await handler(request)
    except Exception:  # noqa: BLE001
        import traceback
        _alog(f"handler error {request.path}\n{traceback.format_exc()}")
        raise


async def index(_) -> web.Response:
    return web.FileResponse(APP_DIR / "static" / "index.html")


async def status(_) -> web.json_response:
    dev = await engine.refresh_device()
    m = engine.motion
    mpath = None
    if m and m.state in ("running", "paused") and len(m.waypoints) > 1:
        pts = m.waypoints
        step = max(1, len(pts) // 400)  # decimate long road geometry
        mpath = [[round(a, 6), round(b, 6)] for a, b in pts[::step]]
        last = [round(pts[-1][0], 6), round(pts[-1][1], 6)]
        if mpath[-1] != last:
            mpath.append(last)
    return web.json_response({
        "device": dev,
        "spoofing": engine.spoofing,
        "feed": "bluetooth" if engine._bt_feeding() else "usb",
        "current": engine.current,
        "motion": {
            "state": m.state if m else "idle",
            "speed_kmh": m.speed_kmh if m else None,
            "path": mpath,
        },
        "error": engine.last_error,
    })


async def teleport(request: web.Request) -> web.Response:
    data = await request.json()
    lat, lng = float(data["lat"]), float(data["lng"])
    engine.stop_motion()   # a running route must not fight the jump
    prev = engine.current  # capture before set_location overwrites it
    ok, msg = await engine.set_location(lat, lng)
    if ok:
        engine.record("teleport", [prev or (lat, lng), (lat, lng)])
    return web.json_response({"ok": ok, "detail": msg})


async def move(request: web.Request) -> web.Response:
    data = await request.json()
    lat, lng = float(data["lat"]), float(data["lng"])
    speed = float(data.get("speed_kmh", SPEED_PRESETS["walking"]))
    if speed <= 0:
        return web.json_response({"ok": False, "detail": "speed must be > 0"},
                                 status=400)
    waypoints = [(float(p["lat"]), float(p["lng"])) for p in data["waypoints"]] \
        if "waypoints" in data else None
    m = await engine.move_to(lat, lng, speed, waypoints,
                             follow_roads=bool(data.get("roads", False)))
    return web.json_response({"ok": True, "distance_m": round(m.total_distance_m),
                              "eta_s": round(m.total_distance_m / (speed * 1000 / 3600))})


async def stop(_) -> web.Response:
    engine.stop_motion()
    return web.json_response({"ok": True})


async def clear(request: web.Request) -> web.Response:
    ok, msg = await engine.clear_location()
    return web.json_response({"ok": ok, "detail": msg})


async def joystick(request: web.Request) -> web.Response:
    data = await request.json()
    await engine.joystick(float(data.get("dx", 0)), float(data.get("dy", 0)),
                          float(data.get("speed_kmh", SPEED_PRESETS["walking"])))
    return web.json_response({"ok": True})


async def gpx_play(request: web.Request) -> web.Response:
    data = await request.json()
    pts = Engine.parse_gpx(data["gpx"])
    if len(pts) < 2:
        return web.json_response({"ok": False, "detail": "no trackpoints in GPX"})
    speed = float(data.get("speed_kmh", SPEED_PRESETS["driving"]))
    await engine.move_to(pts[-1][0], pts[-1][1], speed, waypoints=pts)
    return web.json_response({"ok": True, "points": len(pts)})


async def history(_) -> web.Response:
    return web.json_response(engine.history)


# ---- map tile proxy with cache (survives flaky Wi-Fi; OSM usage policy ok) ----
_TILE_CACHE_DIR = (DATA_DIR / "tilecache")
_tile_mem: "dict[tuple[int,int,int], bytes]" = {}
_TILE_MEM_MAX = 4000
_tile_session = None


async def _get_tile_session():
    global _tile_session
    if _tile_session is None or _tile_session.closed:
        import aiohttp
        _tile_session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ttl_dns_cache=600, limit=24),
            headers={"User-Agent": "GoAnywhere/1.0 (personal GPS tool)"})
    return _tile_session


async def tiles(request: web.Request) -> web.Response:
    z, x, y = (int(request.match_info[k]) for k in ("z", "x", "y"))
    key = (z, x, y)
    if key in _tile_mem:
        return web.Response(body=_tile_mem[key], content_type="image/png",
                            headers={"Cache-Control": "public, max-age=86400"})
    path = _TILE_CACHE_DIR / str(z) / f"{x}-{y}.png"
    try:
        body = path.read_bytes()
    except OSError:
        body = None
    if body is None:
        host = "abc"[(x + y) % 3]
        url = f"https://{host}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        try:
            sess = await _get_tile_session()
            async with sess.get(url, timeout=__import__("aiohttp").ClientTimeout(total=8)) as r:
                if r.status != 200:
                    return web.Response(status=502, body=b"")
                body = await r.read()
        except Exception as e:  # noqa: BLE001
            _alog(f"tile fetch failed {url}: {e}")
            return web.Response(status=502, body=b"")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(body)
            os.replace(tmp, path)          # atomic; no half-written tiles
        except OSError:
            pass
    if len(_tile_mem) >= _TILE_MEM_MAX:    # drop oldest quarter, keep hot tiles
        for k in list(_tile_mem)[: _TILE_MEM_MAX // 4]:
            _tile_mem.pop(k, None)
    _tile_mem[key] = body
    return web.Response(body=body, content_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


async def gamemode_status(_):
    return web.json_response(gmgr.snapshot())


async def gamemode_start(_):
    # if a USB developer spoof is active, drop it first: the accessory
    # feed replaces it, and a lingering simulated fix keeps Error 12 alive
    if engine.spoofing and not engine._bt_feeding():
        await engine.clear_location()
    return web.json_response(await gmgr.start())


async def gamemode_stop(_):
    return web.json_response(await gmgr.stop())


async def bluetooth_status(_):
    loop = asyncio.get_running_loop()

    def _check():
        try:
            import game_mode
            name, mac = game_mode.find_phone()
            return {"paired": True, "name": name, "mac": mac.hex(":")}
        except Exception as e:  # noqa: BLE001
            return {"paired": False, "detail": str(e)[:140]}
    return web.json_response(await loop.run_in_executor(None, _check))


async def open_bt_settings(_):
    import subprocess
    subprocess.Popen(["cmd", "/c", "start", "", "ms-settings:bluetooth"])
    return web.json_response({"ok": True})


async def pairing_reset(request: web.Request):
    """Drop the saved pairing record so the next attempt re-pairs cleanly."""
    import os
    loop = asyncio.get_running_loop()

    def _do():
        removed, failed = [], []
        dev = Engine._dev_cache[1] if Engine._dev_cache else None
        udid = (dev or {}).get("UniqueDeviceID", "")
        if not udid:
            return removed, failed  # never wipe SystemConfiguration etc.
        # pmd3 lookup order: usbmuxd store, iTunes store, local store.
        # All three must be cleared or the old record wins on next start.
        bases = [
            Path(os.environ.get("ALLUSERSPROFILE", r"C:\ProgramData"))
            / "Apple" / "Lockdown",
            Path.home() / ".pymobiledevice3",
            Path(os.environ.get("APPDATA", ".")) / "pymobiledevice3",
        ]
        for base in bases:
            if not base.exists():
                continue
            for f in base.glob("*.plist"):
                if not udid or f.stem == udid:
                    try:
                        f.unlink()
                        removed.append(str(f))
                    except OSError:
                        failed.append(str(f))
        return removed, failed
    removed, failed = await loop.run_in_executor(None, _do)
    if hasattr(engine, "stop_worker"):
        await engine.stop_worker()
    return web.json_response({"ok": not failed, "removed": removed,
                              "failed": failed})


async def search(request: web.Request) -> web.Response:
    """Nominatim geocoding proxy (set a honest User-Agent)."""
    q = request.query.get("q", "").strip()
    if not q:
        return web.json_response([])
    url = ("https://nominatim.openstreetmap.org/search?format=json&limit=8&q="
           + urllib.parse.quote(q))
    req = urllib.request.Request(url, headers={"User-Agent":
        "GoAnywhere/0.1 (personal GPS tool)"})

    def fetch():
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    loop = asyncio.get_event_loop()
    try:
        hits = await loop.run_in_executor(None, fetch)
    except Exception as e:  # noqa: BLE001
        return web.json_response({"ok": False, "detail": str(e)})
    out = [{"name": h.get("display_name", ""), "lat": float(h["lat"]),
            "lng": float(h["lon"])} for h in hits]
    return web.json_response(out)


def make_app() -> web.Application:
    async def _cleanup(_app):
        global _tile_session
        if _tile_session is not None and not _tile_session.closed:
            await _tile_session.close()
    app = web.Application(middlewares=[_err_log])
    app.on_cleanup.append(_cleanup)
    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/teleport", teleport)
    app.router.add_post("/api/move", move)
    app.router.add_post("/api/stop", stop)
    app.router.add_post("/api/clear", clear)
    app.router.add_post("/api/joystick", joystick)
    app.router.add_post("/api/gpx", gpx_play)
    app.router.add_get("/api/history", history)
    app.router.add_get("/tiles/{z}/{x}/{y}.png", tiles)
    app.router.add_get("/api/search", search)
    app.router.add_get("/api/gamemode", gamemode_status)
    app.router.add_post("/api/gamemode/start", gamemode_start)
    app.router.add_post("/api/gamemode/stop", gamemode_stop)
    app.router.add_get("/api/bluetooth", bluetooth_status)
    app.router.add_post("/api/pairing/reset", pairing_reset)
    app.router.add_post("/api/open-bt-settings", open_bt_settings)
    app.router.add_static("/static", APP_DIR / "static")
    return app


if __name__ == "__main__":
    print("GoAnywhere → http://127.0.0.1:8642")
    web.run_app(make_app(), host="127.0.0.1", port=8642, print=None)
