"""Inspect the raw head photos: alpha histogram, semi-transparent halo,
watermark and dark-clothing regions. Debug output goes to tools/debug/."""
import os
from PIL import Image

SRC = {
    "groom": "static/img/groom-head.png",
    "bride": "static/img/bride-head.png",
}
os.makedirs("tools/debug", exist_ok=True)

for name, path in SRC.items():
    im = Image.open(path).convert("RGBA")
    w, h = im.size
    px = im.load()
    buckets = {"a0": 0, "a_low": 0, "a_mid": 0, "a_high": 0, "a255": 0}
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            a = px[x, y][3]
            if a == 0:
                buckets["a0"] += 1
            elif a < 64:
                buckets["a_low"] += 1
            elif a < 192:
                buckets["a_mid"] += 1
            elif a < 255:
                buckets["a_high"] += 1
            else:
                buckets["a255"] += 1
    total = sum(buckets.values())
    print(f"--- {name} {w}x{h} ---")
    for k, v in buckets.items():
        print(f"  {k}: {v * 100 // total}%")

    # Visualize semi-transparent zones (0 < alpha < 250) in red
    dbg = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    dpx = dbg.load()
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            if a < 250:
                dpx[x, y] = (255, 0, 0, 255)
            else:
                dpx[x, y] = (r, g, b, 255)
    dbg.save(f"tools/debug/{name}_semitransparent.png")

    # Sample colors in the bride's lower-center area (suspected clothing)
    if name == "bride":
        for yy in (int(h * 0.85), int(h * 0.92), int(h * 0.98)):
            row = [px[int(w * fx), yy] for fx in (0.35, 0.5, 0.65)]
            print(f"  y={yy}: {row}")
