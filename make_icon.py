"""Draw the GoAnywhere app icon: rounded blue tile + white map-pin, PNG->ICO."""
import struct
from io import BytesIO
from PIL import Image, ImageDraw

S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
# rounded tile, app accent #4C69FF on slightly deeper base
d.rounded_rectangle([16, 16, S - 16, S - 16], radius=110, fill=(76, 105, 255, 255))
d.rounded_rectangle([16, 16, S - 16, S - 16], radius=110,
                    outline=(58, 84, 224, 255), width=8)
# pin: teardrop = circle + triangle, white
cx, cy, r = S // 2, 210, 118
d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 255))
d.polygon([(cx - r * 0.72, cy + r * 0.55), (cx + r * 0.72, cy + r * 0.55),
           (cx, 452)], fill=(255, 255, 255, 255))
# pin hole, accent colour
hr = 46
d.ellipse([cx - hr, cy - hr, cx + hr, cy + hr], fill=(76, 105, 255, 255))
# soft location halo under the pin tip (translucent white rings)
for rad in (86, 56):
    d.ellipse([cx - rad, 452 - rad // 2, cx + rad, 452 + rad // 2],
              outline=(255, 255, 255, 90), width=12)

pngs = {}
for size in (256, 128, 64, 48, 32, 16):
    pngs[size] = img.resize((size, size), Image.LANCZOS)
# build ICO container with PNG-compressed entries (supported since Vista)
entries, data = [], b""
for size in (16, 32, 48, 64, 128, 256):
    buf = BytesIO()
    pngs[size].save(buf, "PNG")
    entries.append((size, buf.getvalue()))
out = struct.pack("<HHH", 0, 1, len(entries))
offset = 6 + 16 * len(entries)
for size, blob in entries:
    w = 0 if size == 256 else size
    out += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(blob), offset)
    offset += len(blob)
out += b"".join(b for _, b in entries)
open("goanywhere.ico", "wb").write(out)
img.save("goanywhere.png")
print("wrote goanywhere.ico", len(out), "bytes")
