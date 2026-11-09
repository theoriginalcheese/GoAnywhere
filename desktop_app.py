"""GoAnywhere desktop app — native Windows window (WebView2) around the
local GoAnywhere server. No browser tab needed.

Frozen-exe note: the location tunnel worker runs by re-launching this same
exe with `--worker` (see engine._spawn_worker), so PyInstaller bundles stay
self-contained.
"""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

PORT = 8642
URL = f"http://127.0.0.1:{PORT}"


def _log(msg: str) -> None:
    try:
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "GoAnywhere"
        base.mkdir(parents=True, exist_ok=True)
        with open(base / "app.log", "a", encoding="utf-8") as f:
            import datetime
            f.write(f"{datetime.datetime.now():%F %T} {msg}\n")
    except OSError:
        pass


def _run_server() -> None:
    try:
        from aiohttp import web
        from server import make_app

        async def _serve():
            runner = web.AppRunner(make_app())
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", PORT)
            await site.start()
            _log(f"server bound on {PORT}")
            while True:
                await asyncio.sleep(3600)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_serve())
    except BaseException:  # noqa: BLE001
        import traceback
        _log("server thread crashed:\n" + traceback.format_exc())
        print(traceback.format_exc(), flush=True)


def _wait_port(timeout: float = 15.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.5)
            try:
                s.connect(("127.0.0.1", PORT))
                return True
            except OSError:
                time.sleep(0.3)
    return False


def main() -> int:
    if "--worker" in sys.argv:
        from spoof_worker import main as worker_main
        return asyncio.run(worker_main())
    if "--gamemode" in sys.argv:
        import game_mode
        sys.argv = [sys.argv[0], "send", "--duration", "0"]
        game_mode.main()
        return 0

    # another instance already running? just open its window target
    already = _wait_port(timeout=0.5)
    if not already:
        threading.Thread(target=_run_server, daemon=True).start()
        if not _wait_port():
            print("server failed to start — see server.log")
            return 1

    import webview
    webview.create_window(
        "GoAnywhere",
        URL,
        width=1100,
        height=700,
        min_size=(860, 560),
        text_select=True,
    )
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
