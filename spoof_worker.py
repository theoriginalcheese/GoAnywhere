"""spoof_worker.py — holds ONE userspace RSD tunnel + one DVT instruments
channel open and applies location updates as line-delimited JSON.

IPC: dev mode = stdin/stdout; frozen app = argv[2] "tcp:PORT" (connect back
to the parent's socket). argv[1] = device serial ("-" for none).
  in : {"op":"set","lat":..,"lng":..} | {"op":"clear"} | {"op":"verify"} | {"op":"ping"}
  out: {"ok":true,"op":..} | {"ok":false,"op":..,"err":..} | {"ready":..} | {"fatal":..}

Uses the DVT LocationSimulation instruments channel — the path that works
on iOS 17+ (com.apple.dt.simulatelocation is gone there).
"""
import asyncio
import json
import sys


async def main() -> int:
    from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

    rest = [a for a in sys.argv[1:] if a != "--worker"]
    ipc = next((a for a in rest if a.startswith("tcp:")), None)
    serial = next((a for a in rest if not a.startswith("tcp:")), None)
    if serial in ("-", ""):
        serial = None

    loop = asyncio.get_event_loop()

    if ipc and ipc.startswith("tcp:"):
        reader, writer = await asyncio.open_connection("127.0.0.1", int(ipc[4:]))

        async def read_line():
            data = await reader.readline()
            return data.decode("utf-8", "replace") if data else ""

        def emit(obj):
            writer.write((json.dumps(obj) + "\n").encode())
    else:
        async def read_line():
            return await loop.run_in_executor(None, sys.stdin.readline)

        def emit(obj):
            sys.stdout.write(json.dumps(obj) + "\n")
            sys.stdout.flush()

    tunnel = UserspaceRsdTunnel(serial=serial, autopair=True)

    try:
        rsd = await tunnel.aopen()
    except Exception as e:  # noqa: BLE001
        emit({"fatal": f"tunnel: {type(e).__name__}: {e}"})
        return 2
    emit({"ready": True, "udid": getattr(rsd, "udid", serial)})

    dvt = None
    loc = None

    async def open_channel():
        nonlocal dvt, loc
        dvt = DvtProvider(rsd)
        await dvt.__aenter__()
        loc = LocationSimulation(dvt)
        await loc.__aenter__()

    async def close_channel():
        nonlocal dvt, loc
        for obj in (loc, dvt):
            try:
                if obj is not None:
                    await obj.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        loc = None
        dvt = None
    last = None
    fails = 0
    try:
        while True:
            line = await read_line()
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                emit({"ok": False, "err": "bad json"})
                continue
            op = msg.get("op")
            try:
                if op in ("set", "clear"):
                    if loc is None:
                        await open_channel()
                    if op == "set":
                        await loc.set(float(msg["lat"]), float(msg["lng"]))
                        last = (msg["lat"], msg["lng"])
                    else:
                        await loc.clear()
                        last = None
                    emit({"ok": True, "op": op})
                elif op == "ping":
                    emit({"ok": True, "op": "ping"})
                elif op == "verify":
                    if loc is None:
                        await open_channel()
                    emit({"ok": True, "op": "verify"})
                else:
                    emit({"ok": False, "err": f"unknown op {op}"})
            except Exception as e:  # noqa: BLE001
                await close_channel()  # force reopen on next op
                fails += 1
                emit({"ok": False, "op": op, "err": f"{type(e).__name__}: {e}"})
                if fails >= 3:
                    # tunnel is dead (phone unplugged); the engine's drain
                    # sees fatal and respawns us with a fresh tunnel
                    emit({"fatal": "location channel failing repeatedly "
                          "(device unplugged or tunnel dead)"})
                    return 3
            else:
                fails = 0
    finally:
        await close_channel()
        try:
            await tunnel.aclose()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
