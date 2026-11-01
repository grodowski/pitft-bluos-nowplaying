#!/usr/bin/env python3
"""Draws the README previews with made-up covers, using the real render code.

  python3 preview.py    # writes docs/pitft-preview.png and docs/lametric-preview.png

No player, screen or clock needed. Fonts fall back to Arial off the Pi, so the PiTFT text
looks slightly different from DejaVu on the device.
"""
import os

from PIL import Image, ImageDraw

import lametric
import nowplaying

DOCS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
LABEL = (170, 170, 170)


def sunset():
    c = Image.new("RGB", (600, 600))
    d = ImageDraw.Draw(c)
    for y in range(600):
        d.line((0, y, 600, y), fill=(int(250 - y * 0.3), int(120 + y * 0.05), int(80 + y * 0.2)))
    d.ellipse((330, 120, 480, 270), fill=(255, 230, 150))
    d.polygon([(0, 600), (0, 380), (150, 300), (300, 420), (450, 330), (600, 400), (600, 600)], fill=(25, 20, 45))
    return c


def portrait():
    c = Image.new("RGB", (600, 600), (230, 225, 215))
    d = ImageDraw.Draw(c)
    d.ellipse((200, 120, 400, 340), fill=(40, 30, 30))
    d.rectangle((150, 330, 450, 600), fill=(30, 30, 40))
    d.rectangle((40, 40, 560, 90), fill=(200, 30, 40))
    return c


def stripes():
    c = Image.new("RGB", (600, 600), (20, 40, 90))
    d = ImageDraw.Draw(c)
    for i in range(0, 600, 100):
        d.rectangle((i, 0, i + 40, 600), fill=(240, 200, 40))
    return c


def pitft_preview(path):
    size = (480, 320)
    shots = [
        ("Spotify, liked", sunset(), {"state": "stream", "title1": "Low Sun Over the Ridge",
                                       "title2": "The Evening Hours", "title3": "Long Shadows"}, True),
        ("paused", portrait(), {"state": "pause", "title1": "Portrait in Red",
                                "title2": "Marta Ilves", "title3": "Studio Sessions, Vol. 2"}, None),
    ]
    gap = 24
    out = Image.new("RGB", (size[0] * len(shots) + gap * (len(shots) + 1), size[1] + 2 * gap + 20), (8, 8, 8))
    d = ImageDraw.Draw(out)
    for i, (label, cover, status, liked) in enumerate(shots):
        x = gap + i * (size[0] + gap)
        frame = nowplaying.render(size, status, nowplaying.prepare_art(cover, size), liked=liked)
        out.paste(frame, (x, gap))
        d.text((x, gap + size[1] + 8), label, fill=LABEL)
    out.save(path)


def lametric_preview(path):
    size = (37, 8)
    rows = [("sunset, playing", sunset(), None), ("sunset, paused", sunset(), "pause"),
            ("portrait, playing", portrait(), None), ("stripes, resumed", stripes(), "play")]
    S, LW = 16, 150  # LED pitch, label column
    out = Image.new("RGB", (LW + size[0] * S + 20, (size[1] * S + 14) * len(rows) + 10), (8, 8, 8))
    d = ImageDraw.Draw(out)
    for i, (label, cover, icon) in enumerate(rows):
        art = cover.resize((size[1], size[1]), Image.BOX)
        frame = lametric.render(size, art, 0.4, lametric.Look, lametric.make_wash(cover, size), icon)
        oy = 10 + i * (size[1] * S + 14)
        d.text((10, oy + size[1] * S // 2 - 6), label, fill=LABEL)
        for x in range(size[0]):
            for y in range(size[1]):
                c = frame.getpixel((x, y))
                c = c if any(c) else (14, 14, 14)  # unlit LEDs, faintly visible
                d.ellipse((LW + x * S + 2, oy + y * S + 2, LW + x * S + S - 2, oy + y * S + S - 2), fill=c)
    out.save(path)


if __name__ == "__main__":
    os.makedirs(DOCS, exist_ok=True)
    pitft_preview(os.path.join(DOCS, "pitft-preview.png"))
    lametric_preview(os.path.join(DOCS, "lametric-preview.png"))
    print("wrote docs/pitft-preview.png, docs/lametric-preview.png")
