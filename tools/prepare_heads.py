"""Prepare head photos for the game: strip the baked-in checkerboard
background, crop to the face, apply a circular mask and resize to 512px.

Input:  static/img/groom-head.png, static/img/bride-head.png (raw exports)
Output: same paths, game-ready. Originals are backed up to tools/debug/.
"""
import shutil
from collections import deque

from PIL import Image, ImageDraw, ImageFilter

OUT_SIZE = 512


def is_background(r, g, b, a):
    """True for already-transparent or baked checkerboard pixels
    (desaturated light gray/white squares)."""
    if a < 10:
        return True
    mx, mn = max(r, g, b), min(r, g, b)
    return (mx - mn) <= 20 and mn >= 150


def flood_clear_background(im):
    """BFS from all borders: clear connected background pixels so light
    areas INSIDE the face (skin highlights, eyes) are never touched."""
    w, h = im.size
    px = im.load()
    seen = bytearray(w * h)
    dq = deque()
    for x in range(w):
        dq.append((x, 0))
        dq.append((x, h - 1))
    for y in range(h):
        dq.append((0, y))
        dq.append((w - 1, y))
    while dq:
        x, y = dq.popleft()
        if x < 0 or y < 0 or x >= w or y >= h:
            continue
        i = y * w + x
        if seen[i]:
            continue
        seen[i] = 1
        r, g, b, a = px[x, y]
        if not is_background(r, g, b, a):
            continue
        px[x, y] = (0, 0, 0, 0)
        dq.append((x + 1, y))
        dq.append((x - 1, y))
        dq.append((x, y + 1))
        dq.append((x, y - 1))
    return im


def keep_largest_component(im):
    """Drop small floating opaque islands (watermarks, checker leftovers)."""
    w, h = im.size
    px = im.load()
    labels = [0] * (w * h)
    sizes = {}
    next_label = 0
    for sy in range(h):
        for sx in range(w):
            if labels[sy * w + sx] or px[sx, sy][3] <= 10:
                continue
            next_label += 1
            comp = 0
            dq = deque([(sx, sy)])
            labels[sy * w + sx] = next_label
            while dq:
                x, y = dq.popleft()
                comp += 1
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < w and 0 <= ny < h:
                        j = ny * w + nx
                        if not labels[j] and px[nx, ny][3] > 10:
                            labels[j] = next_label
                            dq.append((nx, ny))
            sizes[next_label] = comp
    if not sizes:
        return im
    keep = max(sizes, key=sizes.get)
    for y in range(h):
        for x in range(w):
            lb = labels[y * w + x]
            if lb and lb != keep:
                px[x, y] = (0, 0, 0, 0)
    return im


def square_around_content(im, margin=0.04):
    """Center the opaque content on a square transparent canvas."""
    bbox = im.getchannel("A").getbbox()
    im = im.crop(bbox)
    side = int(max(im.size) * (1 + margin))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
    return canvas


def apply_circle_mask(im):
    """Antialiased circular alpha mask (matches the in-game circular crop)."""
    side = im.width
    ss = side * 4
    mask = Image.new("L", (ss, ss), 0)
    ImageDraw.Draw(mask).ellipse((8, 8, ss - 8, ss - 8), fill=255)
    mask = mask.resize((side, side), Image.LANCZOS)
    alpha = im.getchannel("A")
    from PIL import ImageChops
    im.putalpha(ImageChops.multiply(alpha, mask))
    return im


def smooth_alpha(im):
    """Soften jagged cut edges with a subtle alpha blur."""
    alpha = im.getchannel("A").filter(ImageFilter.GaussianBlur(1.2))
    im.putalpha(alpha)
    return im


def process(name, face_box=None):
    path = f"static/img/{name}-head.png"
    shutil.copy(path, f"tools/debug/orig_{name}.png")
    im = Image.open(path).convert("RGBA")
    if face_box:
        im = im.crop(face_box)
    im = flood_clear_background(im)
    im = keep_largest_component(im)
    im = smooth_alpha(im)
    im = square_around_content(im)
    im = apply_circle_mask(im)
    im = im.resize((OUT_SIZE, OUT_SIZE), Image.LANCZOS)
    im.save(path, optimize=True)

    # Previews: on a dark background + at in-game head size (~26 px)
    prev = Image.new("RGBA", (OUT_SIZE, OUT_SIZE), (26, 26, 46, 255))
    prev.alpha_composite(im)
    prev.convert("RGB").save(f"tools/debug/{name}_preview.png")
    tiny = im.resize((26, 26), Image.LANCZOS)
    tiny_bg = Image.new("RGBA", (64, 64), (26, 26, 46, 255))
    tiny_bg.alpha_composite(tiny, (19, 19))
    tiny_bg.convert("RGB").resize((128, 128), Image.NEAREST).save(
        f"tools/debug/{name}_ingame.png")
    print(f"{name}: done")


# Groom: the head fills the frame, no pre-crop needed.
process("groom")
# Bride: crop the head area only — long hair below the chin, the black
# clothing and the bottom-right watermark stay outside the box.
# Box picked from the 10%-grid debug image (source is 992x1073).
process("bride", face_box=(152, 60, 840, 773))
