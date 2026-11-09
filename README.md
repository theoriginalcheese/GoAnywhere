# GoAnywhere

A free, unlimited, home-made replacement for Tenorshare iAnyGo. It spoofs
your iPhone's GPS from a Windows desktop app that looks and works like
iAnyGo — big map, click to jump, routes, joystick, GPX, speed presets —
with no account, no subscription, no device limits.

Includes **Game Mode**: the Bluetooth trick that defeats Pokémon GO
*Error 12*, by pretending to be an MFi GPS accessory instead of a
developer tool.

## Features

- **One-Step teleport** — click anywhere on the map, jump there instantly.
- **Multi-Point routes** — drop numbered waypoints; the last one becomes
  the destination pin. Walk/drive the route at your chosen speed,
  optionally following real roads (OSRM) instead of straight lines.
- **Joystick** — steer in real time.
- **GPX playback** — load a `.gpx` track and follow it.
- **Live position dot** (Google-Maps style) + draggable destination pin,
  distance / ETA estimates, place search, movement history.
- **Game Mode (Bluetooth)** — iAP2 MFi accessory emulation over RFCOMM.
  While it streams, your position is delivered to the iPhone as
  *accessory GPS*, which Pokémon GO treats as real hardware — no Error 12.
- Local web UI (also opens as a native window), map tiles proxied and
  cached locally.

## How it works

Two independent ways to move the iPhone's location:

1. **USB feed (default)** — [pymobiledevice3](https://github.com/doronz88/pymobiledevice3)
   opens a userspace Remote-Server-Discovery tunnel (no admin rights) and
   uses the developer `LocationSimulation` service. Great for most apps.
2. **Bluetooth Game Mode** — `game_mode.py` speaks iAP2 over a Bluetooth
   RFCOMM socket, authenticates with an MFi certificate, and streams NMEA
   `GGA`/`RMC` sentences at ~1 Hz once the iPhone asks for them
   (`0xFFFA`). iOS then treats the accessory as its GPS source. Since
   Pokémon GO 0.315.2 flags developer-simulated fixes
   (`isSimulatedBySoftware`), this is the path that clears Error 12.

While Game Mode is authenticated the app **automatically stops using the
USB feed**, so there is exactly one location source — the status line
reads *"Simulating GPS · Bluetooth ✓"*. Stop Game Mode and the USB feed
comes back. The runner follows the app's live position, so teleport,
routes and joystick all keep working over Bluetooth.

## Requirements

- Windows 10/11 with **Bluetooth** (for Game Mode).
- iPhone with **Developer Mode ON** (Settings → Privacy & Security →
  Developer Mode) for the USB feed, and **trusted this PC** (tap *Trust*
  when first plugged in — keep the phone unlocked while pairing).
- A proper data cable (not charge-only).
- Python 3.12 — the launcher creates `.venv` itself (via `uv`, or `pip`
  as a fallback).

## Quick start

1. Plug the phone in and unlock it.
2. Double-click **`GoAnywhere.bat`** (first run builds the venv).
3. The app window opens on a map. Pick a mode and go.

Re-running the launcher is safe — it kills any old instance first.

## Speed presets

| Preset | Feels like |
| --- | --- |
| 0.1 km/h | standing about / tiny nudges |
| 7 km/h | normal walking, hatches eggs |
| 19 km/h | cycling |
| 30 km/h | slow drive |

Pokémon GO stops counting distance above roughly 30 km/h, so there's no
point going faster.

## Game Mode setup (Error 12)

In the app, **Game Mode → Setup guide** walks you through it with live
ticks per step. Summary:

1. Keep the iPhone on USB and unlocked.
2. Pair Bluetooth: iPhone → Settings → Bluetooth → *your PC's name* →
   Pair. On iOS 18+, also connect it from the Control Centre Bluetooth
   panel.
3. Import the MFi identity once (below) — the guide shows *MFi
   authentication accepted* when the iPhone buys it.
4. Turn the iPhone's **Location Services, Wi-Fi and Cellular off**,
   count to 10, turn them back on.
5. Open Pokémon GO. When the wizard's fix counter ticks, the iPhone is
   pulling GPS from the app over Bluetooth.

Run it standalone any time with **`GameMode.bat`** (auto-retries and
follows the app's position).

### MFi identity

iAP2 authentication needs an MFi certificate/key pair, looked up under
`%LOCALAPPDATA%\GoAnywhere\mfi\` (`certificate.p7b`, `identity.pk8`). One
easy source is the shared experimental identity bundled in the
[DiPlay](https://github.com/ncToolbox/DiPlay) Android APK:

```bash
git clone https://github.com/aur3l14no/accessory-location
python accessory-location/tools/import_diplay_identity.py \
    --apk DiPlay.apk --output-dir "%LOCALAPPDATA%\GoAnywhere\mfi"
```

Or set `ACCESSORY_MFI_CERT_FILE` / `ACCESSORY_MFI_KEY_FILE`.

## Raids & Max Battles overlay

Tick **Raids & Max Battles** under the Game Mode card to draw live raids
(ringed by tier: grey L1-2, purple L3, orange L4, red L5, pink mega) and
Max Battles (blue "M" diamonds) within 10 km of your spoofed position (or the
map centre). Click one for the Pokémon, gym and a live "ends in" countdown.
The toggle is remembered between launches.

- **Data:** community scanner sites on the pogomap network, fetched via
  `GET /api/raids?lat=&lng=&km=` (`raids.py`). The server proxies them (no
  CORS on those sites), caches each city for 60 s and the UI polls once a
  minute — please don't lower that.
- **Coverage is scanner cities only:** London (`londonpogomap.com`, roughly
  zones 1-3), New York, Singapore, Sydney and Vancouver. There's no
  Manchester or Liverpool site, so from Chester the nearest coverage is
  London (~250 km); outside a covered city the UI shows the
  "no scanner coverage here" message instead of markers.
- **Names/images:** names and form names from the WatWowMap Masterfile
  (pogoapi.net names as fallback), cached weekly in
  `%LOCALAPPDATA%\GoAnywhere\cache\`. Sprites are hotlinked (never bundled)
  from [WatWowMap UICONS](https://github.com/WatWowMap/wwm-uicons-webp), falling
  back to PokeAPI HOME sprites, then a plain Poké Ball. Pokémon artwork is
  © Nintendo / Game Freak / The Pokémon Company / Niantic, used as fan
  content, and is **not** covered by this repo's MIT licence.

## Getting your real location back

Hit **Restore real GPS** in the UI. Unplugging and restarting the phone
does the same if all else fails.

## Building the exe

```bash
pip install pyinstaller
pyinstaller GoAnywhere.spec
```

`dist/GoAnywhere/GoAnywhere.exe` is a self-contained onedir build. Note
the `--collect-all` packages and `--paths ref_accessory_location/src` +
hidden imports `accessory_location.{link,wire,profile}` baked into the
spec — they are needed for the frozen Bluetooth runner.

## Repo layout

| File | Role |
|---|---|
| `desktop_app.py` | native window (pywebview) + embedded server |
| `server.py` | aiohttp REST API + tile proxy (`/api/...`, `/tiles/...`) |
| `raids.py` | raids / Max Battles proxy for the map overlay (pogomap network) |
| `engine.py` | device discovery, USB worker mgmt, motion, road routing |
| `spoof_worker.py` | persistent USB tunnel worker (JSON-line IPC) |
| `game_mode.py` | Bluetooth iAP2 accessory runner (Game Mode) |
| `gamemode_manager.py` | supervises the runner, tracks its state |
| `static/index.html` | the whole UI (vendored Leaflet, offline-safe) |
| `ref_accessory_location/` | GPL-3 reference implementation (submodule) |

## Troubleshooting

- **Phone not found** — unlock it, replug, make sure you tapped *Trust*.
  Try another cable/port.
- **SavePairRecordFailed / pairing refused** — iOS refuses to save
  pairing records while the screen is locked. Unlock the phone and use
  the app's **Fix pairing** button.
- **Developer mode error** — Settings → Privacy & Security → Developer
  Mode, let the phone reboot and confirm.
- **Drops out mid-session** — replug; the server respawns the tunnel
  worker automatically.
- **Error 12 with Game Mode running** — do the step-4 location ritual;
  if Wi-Fi positioning drags you "home", try airplane mode + Wi-Fi only.
- **Bans / warnings** — spoofing is against Niantic's ToS. Move like a
  human: sensible speeds, no planet-hopping.

## Privacy

Everything runs locally. History, logs, tile cache and pairing state
live under `%LOCALAPPDATA%\GoAnywhere\` and `%APPDATA%\pymobiledevice3\`.
The only outbound requests are map tiles (OpenStreetMap), place search
(Nominatim) and road geometry (public OSRM demo router, rate-limited 1
req/s — straight-line fallback if it's grumpy). No telemetry, no account.

## Licence

MIT for GoAnywhere's own code. `ref_accessory_location/` (submodule) is
GPL-3.0 by its author and used as a reference dependency; Leaflet is MIT.
Map data © OpenStreetMap contributors. Not affiliated with Tenorshare,
Niantic or Apple.
