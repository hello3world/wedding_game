"""Debug: draw a 10%-step grid over both photos to pick face coordinates."""
from PIL import Image, ImageDraw

for name in ("groom", "bride"):
    im = Image.open(f"static/img/{name}-head.png").convert("RGB")
    w, h = im.size
    d = ImageDraw.Draw(im)
    for i in range(1, 10):
        x = w * i // 10
        y = h * i // 10
        d.line([(x, 0), (x, h)], fill=(0, 255, 0), width=2)
        d.line([(0, y), (w, y)], fill=(0, 255, 0), width=2)
        d.text((x + 4, 4), str(i), fill=(255, 0, 0))
        d.text((4, y + 4), str(i), fill=(255, 0, 0))
    im.thumbnail((500, 500))
    im.save(f"tools/debug/{name}_grid.png")
print("done")
