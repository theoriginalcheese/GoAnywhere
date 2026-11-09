"""GoAnywhere Game Mode: iAP2 accessory-location over Bluetooth RFCOMM on Windows.

Speaks the accessory protocol to the paired iPhone (service UUID
00000000-deca-fade-deca-deafdecacafe), authenticates with a shared experimental
MFi identity (DiPlay), then streams NMEA GGA/RMC fixes so iOS sources location
"from accessory" — the trick iAnyGo uses to dodge Pokemon GO Error 12.

Protocol/framing reused from the GPL-3 accessory-location reference (local
private build). Usage:
    python game_mode.py probe
    python game_mode.py send --lat 51.5007 --lng -0.1246 [--interval 1] [--duration 0]
Env: ACCESSORY_MFI_CERT_FILE, ACCESSORY_MFI_KEY_FILE
"""
import argparse
import asyncio
import json
import math
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "ref_accessory_location", "src"))

from accessory_location.link import Link                      # noqa: E402
from accessory_location.profile import identity, location_message, location_sentences  # noqa: E402
from accessory_location.wire import DETECT, Message, parameter  # noqa: E402

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, utils   # noqa: E402
from cryptography.hazmat.primitives.serialization import pkcs7    # noqa: E402

from winrt.windows.devices.bluetooth import (                     # noqa: E402
    BluetoothAdapter, BluetoothDevice)
from winrt.windows.devices.bluetooth.rfcomm import (              # noqa: E402
    RfcommDeviceService, RfcommServiceId)
from winrt.windows.devices.enumeration import DeviceInformation   # noqa: E402

IAP2_SERVICE = "00000000-deca-fade-deca-deafdecacafe"


def emit(**event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


# ---------------- WinRT discovery ----------------

def _find_all(selector, related=None):
    try:
        return DeviceInformation.find_all_async(selector).get()
    except TypeError:
        return DeviceInformation.find_all_async(selector, related or []).get()


def find_phone():
    """Return (name, mac bytes) for the paired iPhone."""
    paired = _find_all(
        BluetoothDevice.get_device_selector_from_pairing_state(True))
    for d in paired:
        if "iphone" in (d.name or "").lower():
            # id format: Bluetooth#Bluetooth<adapter-mac>-<device-mac>
            mac = d.id.rsplit("-", 1)[-1]
            return d.name, bytes.fromhex(mac.replace(":", ""))
    names = [d.name for d in paired]
    raise RuntimeError(f"no paired iPhone found (paired devices: {names})")


def find_channel(phone_mac: bytes) -> int:
    """Probe RFCOMM channels 1-6; the iAP2 service echoes the detection seq."""
    mac = phone_mac.hex(":")
    for ch in range(1, 7):
        s = None
        try:
            s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM,
                              socket.BTPROTO_RFCOMM)
            s.settimeout(12)
            s.connect((mac, ch))
            s.sendall(DETECT)
            if s.recv(6) == DETECT:
                return ch
        except OSError:
            continue
        finally:
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        time.sleep(0.4)
    raise RuntimeError(f"no iAP2 RFCOMM service found on {mac} (channels 1-6)")


def controller_address() -> bytes:
    a = BluetoothAdapter.get_default_async().get()
    addr = getattr(a, "bluetooth_address", None)
    if addr is None:
        raise RuntimeError("WinRT BluetoothAddress unavailable on this build")
    return int(addr).to_bytes(6, "big")


# ---------------- asyncio stream over the RFCOMM socket ----------------

class RfcommStream:
    def __init__(self, sock):
        self.s = sock
        self.buf = bytearray()

    @classmethod
    async def open(cls, mac: bytes, channel: int):
        loop = asyncio.get_running_loop()
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM,
                          socket.BTPROTO_RFCOMM)
        s.settimeout(20)
        await loop.run_in_executor(None, lambda: s.connect(
            (mac.hex(":"), channel)))
        return cls(s)

    async def recvall(self, size: int) -> bytes:
        loop = asyncio.get_running_loop()
        while len(self.buf) < size:
            try:
                chunk = await loop.run_in_executor(None, self.s.recv, 4096)
            except (TimeoutError, OSError):
                return bytes(self.buf)          # let the link surface it
            if not chunk:
                return bytes(self.buf)
            self.buf += chunk
        out = bytes(self.buf[:size])
        del self.buf[:size]
        return out

    async def sendall(self, data: bytes):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.s.sendall, data)

    async def close(self):
        try:
            self.s.close()
        except OSError:
            pass


# ---------------- MFi auth (in-process, DiPlay identity) ----------------

class MfiAuth:
    def __init__(self):
        base = os.path.join(os.environ.get("LOCALAPPDATA", "."),
                            "GoAnywhere", "mfi")
        cert_path = os.environ.get(
            "ACCESSORY_MFI_CERT_FILE", os.path.join(base, "certificate.p7b"))
        key_path = os.environ.get(
            "ACCESSORY_MFI_KEY_FILE", os.path.join(base, "identity.pk8"))
        if not cert_path or not key_path:
            raise RuntimeError("set ACCESSORY_MFI_CERT_FILE / ACCESSORY_MFI_KEY_FILE")
        self.cert = open(cert_path, "rb").read()
        self.key = serialization.load_der_private_key(
            open(key_path, "rb").read(), password=None)
        leafs = pkcs7.load_der_pkcs7_certificates(self.cert)
        if not leafs:
            raise RuntimeError("cert container empty")

    def certificate_msg(self) -> Message:
        return Message(0xAA01, parameter(0, self.cert))

    def sign_msg(self, challenge: bytes) -> Message:
        der = self.key.sign(challenge, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        r, s = utils.decode_dss_signature(der)
        return Message(0xAA03, parameter(0, r.to_bytes(32, "big") + s.to_bytes(32, "big")))


# ---------------- session ----------------

async def run(args):
    name, mac = find_phone()
    emit(event="phone", name=name, mac=mac.hex(":"))
    channel = find_channel(mac)
    emit(event="rfcomm", channel=channel)
    end = asyncio.get_running_loop().time() + (args.duration or 30 * 60)
    while True:  # the phone drops the link when it does not need fixes
        try:
            done = await _session(args, mac, end, channel)
        except (EOFError, ConnectionError, TimeoutError, OSError) as e:
            emit(event="disconnected", reason=str(e)[:80])
            done = False
        if done:
            break
        if asyncio.get_running_loop().time() >= end:
            emit(event="finished")
            break
        await asyncio.sleep(1.5)


async def app_position():
    """Current spoofed position from the running GoAnywhere app, or None."""
    import urllib.request
    def _get():
        with urllib.request.urlopen("http://127.0.0.1:8642/api/status",
                                    timeout=2) as r:
            return json.loads(r.read()).get("current")
    try:
        return await asyncio.get_running_loop().run_in_executor(None, _get)
    except Exception:  # noqa: BLE001
        return None


async def _session(args, mac, end, channel=1):
    stream = await RfcommStream.open(mac, channel)
    auth = MfiAuth()
    identified = authenticated = False
    active, last_sent, intervals = set(), {}, {}
    interval = args.interval
    sent = 0
    link = None
    try:
        await stream.sendall(DETECT)
        reply = await stream.recvall(6)
        if reply != DETECT:
            emit(event="detect", ok=False, reply=reply.hex())
        else:
            emit(event="detect", ok=True)
        link = Link(stream, emit)
        await link.handshake()
        while asyncio.get_running_loop().time() < end:
            try:
                message = await link.receive(0.5)
            except TimeoutError:
                message = None
            if message is not None:
                mid = message.identifier
                emit(event="message", identifier=f"0x{mid:04x}")
                if mid == 0x1D00:
                    await link.send(identity("bluetooth", controller_address()))
                elif mid == 0x1D02:
                    identified = True
                    emit(event="identification_accepted")
                elif mid == 0x1D03:
                    raise RuntimeError("identification rejected")
                elif mid == 0xAA00:
                    await link.send(auth.certificate_msg())
                elif mid == 0xAA02:
                    from accessory_location.wire import parameters
                    challenges = [b for i, b in parameters(message.body) if i == 0]
                    await link.send(auth.sign_msg(challenges[0]))
                elif mid == 0xAA05:
                    authenticated = True
                    emit(event="authentication_succeeded")
                elif mid == 0xAA04:
                    raise RuntimeError("authentication failed — device does not trust this identity")
                elif mid == 0xFFFA:
                    from accessory_location.wire import parameters
                    fields = parameters(message.body)
                    active = {i for i, b in fields if i in (1, 2) and not b}
                    intervals = {i - 0x8000: max(1.0, int.from_bytes(b, "big") / 1000)
                                 for i, b in fields
                                 if i in (0x8001, 0x8002) and len(b) == 4}
                    last_sent.clear()
                    emit(event="location_requested", sentences=sorted(active))
                elif mid == 0xFFFC:
                    active.clear()
                    emit(event="location_stopped")
            if args.command == "send" and identified and authenticated and active:
                now = asyncio.get_running_loop().time()
                due = sorted(i for i in active
                             if now - last_sent.get(i, -math.inf) >=
                             max(interval, intervals.get(i, 1)))
                if due:
                    if args.lat is None:      # follow the app's live position
                        pos = await app_position()
                        if pos is None:
                            continue
                        lat, lng = float(pos[0]), float(pos[1])
                    else:
                        lat, lng = args.lat, args.lng
                    sentences = location_sentences(lat, lng)
                    await link.send(location_message([sentences[i] for i in due]))
                    last_sent.update({i: now for i in due})
                    sent += 1
                    if sent == 1 or sent % 30 == 0:
                        emit(event="fix_sent", count=sent, lat=lat, lng=lng)
        emit(event="session_end", sent=sent, authenticated=authenticated)
        return True
    finally:
        if link is not None:
            try:
                await link.close()
            except Exception:  # noqa: BLE001
                pass
        await stream.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("probe", "send"):
        c = sub.add_parser(name)
        c.add_argument("--interval", type=float, default=1.0)
        c.add_argument("--duration", type=float, default=0)
        if name == "send":
            c.add_argument("--lat", type=float, default=None)
            c.add_argument("--lng", type=float, default=None)
    args = ap.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        emit(event="interrupted")
    except Exception as e:  # noqa: BLE001
        emit(event="error", type=type(e).__name__, detail=str(e))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
