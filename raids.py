"""Live raids + Max Battles near a point, proxied from community scanner sites.

The pogomap network (londonpogomap.com, nycpokemap.com, ...) serves the whole
scan area as JSON from /raids.php with no CORS and no radius query, so we fetch
server-side, cache 60 s per site and filter by distance here.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

import aiohttp

from engine import haversine_m

# approx scan-area bboxes (lat_min, lat_max, lng_min, lng_max), measured from
# live raids.php data 2026-11-09 and padded ~10 km. Manchester/Liverpool sites
# don't exist (DNS fails), so north-west England has no coverage.
CITY_SITES = {
    "londonpogomap.com": ("London", (51.35, 51.67, -0.40, 0.18)),
    "nycpokemap.com": ("New York", (40.40, 40.90, -74.40, -73.50)),
    "sgpokemap.com": ("Singapore", (1.15, 1.56, 103.52, 104.10)),
    "sydneypogomap.com": ("Sydney", (-34.10, -33.65, 150.80, 151.42)),
    "vanpokemap.com": ("Vancouver", (49.00, 49.42, -123.40, -122.75)),
}
CACHE_TTL = 60.0
MAX_WINDOW_S = 7 * 86400          # drop multi-month junk windows (seen: 41 d)
UA = {"User-Agent": "Mozilla/5.0"}
CACHE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "GoAnywhere" / "cache"
NAMES_FILE = CACHE_DIR / "pokemon_names.json"
UICONS_FILE = CACHE_DIR / "uicons_pokemon.json"
WEEK = 7 * 86400
MASTERFILE = "https://raw.githubusercontent.com/WatWowMap/Masterfile-Generator/master/master-latest.json"
POGOAPI_NAMES = "https://pogoapi.net/api/v1/pokemon_names.json"
UICONS = "https://cdn.jsdelivr.net/gh/WatWowMap/wwm-uicons-webp@main/pokemon/"
UICONS_INDEX = "https://cdn.jsdelivr.net/gh/WatWowMap/wwm-uicons-webp@main/index.json"
FALLBACK = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/home/{}.png"

_cache: dict[str, tuple[float, dict]] = {}
_locks: dict[str, asyncio.Lock] = {}
_names: dict | None = None        # {"dex": {"n": name, "f": {form_id: form name}}}
_uicons: set | None = None
_meta_ts = 0.0
_session: aiohttp.ClientSession | None = None


def _sess() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(headers=UA,
                                         timeout=aiohttp.ClientTimeout(total=20))
    return _session


async def close() -> None:
    if _session is not None and not _session.closed:
        await _session.close()


def pick_site(lat: float, lng: float):
    """Site whose bbox holds the point, else None."""
    for site, (_, (a, b, c, d)) in CITY_SITES.items():
        if a <= lat <= b and c <= lng <= d:
            return site
    return None


def nearest_site(lat: float, lng: float):
    def ctr(bb): return ((bb[0] + bb[1]) / 2, (bb[2] + bb[3]) / 2)
    s = min(CITY_SITES, key=lambda k: haversine_m((lat, lng), ctr(CITY_SITES[k][1])))
    return CITY_SITES[s][0], haversine_m((lat, lng), ctr(CITY_SITES[s][1])) / 1000


def _fresh(p: Path) -> bool:
    try:
        return time.time() - p.stat().st_mtime < WEEK
    except OSError:
        return False


def _load(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save(p: Path, obj) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    except OSError:
        pass


async def _get_json(url: str):
    async with _sess().get(url) as r:
        r.raise_for_status()
        return json.loads(await r.read())


async def _ensure_meta() -> None:
    """Names + UICONS file index: disk cache, weekly refresh, failure-tolerant."""
    global _names, _uicons, _meta_ts
    if _names is not None and time.time() - _meta_ts < 3600:
        return
    _meta_ts = time.time()
    names = _load(NAMES_FILE) if _fresh(NAMES_FILE) else None
    if not names:
        try:
            mf = (await _get_json(MASTERFILE))["pokemon"]
            names = {k: {"n": v.get("name", ""), "f": {fk: fv.get("name", "")
                     for fk, fv in (v.get("forms") or {}).items() if fv.get("name")}}
                     for k, v in mf.items()}
        except Exception:  # noqa: BLE001
            try:  # names only (no forms)
                pa = await _get_json(POGOAPI_NAMES)
                names = {k: {"n": v.get("name", ""), "f": {}} for k, v in pa.items()}
            except Exception:  # noqa: BLE001
                names = None
        if names:
            _save(NAMES_FILE, names)
        else:
            names = _load(NAMES_FILE) or {}   # stale beats nothing
    _names = names
    icons = _load(UICONS_FILE) if _fresh(UICONS_FILE) else None
    if not icons:
        try:
            icons = (await _get_json(UICONS_INDEX))["pokemon"]
            _save(UICONS_FILE, icons)
        except Exception:  # noqa: BLE001
            icons = _load(UICONS_FILE) or []
    _uicons = set(icons)


def display_name(dex: int, form: int, level: int) -> str:
    if not dex:
        return f"Egg L{level}"
    e = (_names or {}).get(str(dex)) or {}
    base = e.get("n") or f"Pokemon #{dex}"
    fn = (e.get("f") or {}).get(str(form or 0), "")
    if fn and fn.lower() not in ("normal", "unset", base.lower()):
        return f"{base} ({fn})"
    return base


def _evo_suffix(fname: str, level: int) -> str:
    f = fname.lower()
    if "primal" in f:
        return "_e4"
    if re.search(r"mega\b.*\bx\b", f):
        return "_e2"
    if re.search(r"mega\b.*\by\b", f):
        return "_e3"
    if "mega" in f or level in (6, 7):   # 6/7 = mega / legendary-mega tiers
        return "_e1"
    return ""


def image_urls(dex: int, form: int, level: int, name: str) -> tuple[str, str]:
    """(UICONS url, PokeAPI fallback). _e before _a; _f only if it exists."""
    if not dex:
        return "", ""
    e = _evo_suffix(name, level)
    a = "_a1" if "shadow" in name.lower() else "_a2" if "purified" in name.lower() else ""
    cands = []
    if form:
        cands.append(f"{dex}{e}_f{form}{a}.webp")
    cands += [f"{dex}{e}{a}.webp", f"{dex}{e}.webp", f"{dex}.webp"]
    pick = next((c for c in cands if c in (_uicons or ())), None) \
        or (cands[1] if _uicons else f"{dex}{e}{a}.webp")
    return UICONS + pick, FALLBACK.format(dex)


async def _site_data(site: str) -> tuple[dict, float]:
    lock = _locks.setdefault(site, asyncio.Lock())
    async with lock:
        hit = _cache.get(site)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1], time.time() - hit[0]
        url = f"https://{site}/raids.php?time={int(time.time() * 1000)}"
        async with _sess().get(url, headers={"Referer": f"https://{site}/"}) as r:
            r.raise_for_status()
            data = json.loads(await r.read())
        _cache[site] = (time.time(), data)
        return data, 0.0


def slim(data: dict, lat: float, lng: float, km: float, now: float | None = None) -> list:
    """Pure filter: active windows within km, nearest first,
    max 80 raids + 70 max battles; also derive gym locations."""
    now = now or time.time()
    out = []
    gyms: dict[tuple, float] = {}   # (name,lat,lng) -> nearest dist seen
    for kind, key in (("raid", "raids"), ("max", "battles")):
        for p in data.get(key) or []:
            try:
                s, e = int(p["raid_start"]), int(p["raid_end"])
                plat, plng = float(p["lat"]), float(p["lng"])
            except (KeyError, TypeError, ValueError):
                continue
            d = haversine_m((lat, lng), (plat, plng))
            if d > km * 1000:
                continue
            # the scanner only lists gyms that have raids or Max Battles —
            # those ARE gym locations, so harvest them as a gym layer too
            gkey = ((p.get("gym_name") or "").strip().lower(),
                    round(plat, 5), round(plng, 5))
            if gkey[0] and gyms.get(gkey, 1e18) > d:
                gyms[gkey] = d
            if not (s <= now <= e) or e - s > MAX_WINDOW_S:
                continue
            dex, form, lvl = int(p.get("pokemon_id") or 0), int(p.get("form") or 0), int(p.get("level") or 0)
            name = display_name(dex, form, lvl)
            img, fb = image_urls(dex, form, lvl, name)
            out.append((d, {"kind": kind, "gym": p.get("gym_name") or "", "name": name,
                            "img": img, "img_fb": fb, "level": lvl, "lat": plat,
                            "lng": plng, "start": s, "end": e, "dist_m": round(d)}))
    out.sort(key=lambda t: t[0])
    # cap per kind so a flood of Max Battles cannot crowd out raids
    kept, n_r, n_m = [], 0, 0
    for _, x in out:
        if x["kind"] == "raid":
            if n_r >= 80:
                continue
            n_r += 1
        else:
            if n_m >= 70:
                continue
            n_m += 1
        kept.append(x)
    # gyms (name+coords harvested from raid/Max stations), nearest first, 300 cap
    gl = sorted(((d, k) for k, d in gyms.items()))[:300]
    for d, (gname, plat, plng) in gl:
        kept.append({"kind": "gym", "gym": gname.title(), "name": "Gym",
                     "img": "", "img_fb": "", "level": 0, "lat": plat,
                     "lng": plng, "start": 0, "end": 0, "dist_m": round(d)})
    return kept


async def fetch_raids_ex(lat: float, lng: float, km: float = 10):
    """-> (site, age_s, [slim dicts]). Raises LookupError when no coverage."""
    site = pick_site(lat, lng)
    if site is None:
        city, dist = nearest_site(lat, lng)
        raise LookupError(f"no scanner coverage here (nearest: {city}, {dist:.0f} km away)")
    await _ensure_meta()
    data, age = await _site_data(site)
    return site, age, slim(data, lat, lng, km)


async def fetch_raids(lat: float, lng: float, km: float = 10) -> list:
    return (await fetch_raids_ex(lat, lng, km))[2]
