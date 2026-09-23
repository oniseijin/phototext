#!/usr/bin/env python3
"""Build the demo catalog behind the screenshots in docs/ and README.md.

Generates a synthetic photo library (~54 authored images: receipts, invoices,
letters, postcards, signs, menus, tickets, chat and settings screenshots,
book pages, notes, memes, scenes, people, a duplicate pair), then runs the
real phototext pipeline against docs/demo/demo_mock.py — a mock Ollama whose
extraction responses are authored per photo and matched by a 16x16 luminance
fingerprint. The result is a catalog with realistic, varied content: full-text
search hits, categories, a year timeline, people with a review queue, meme
clusters, a duplicate pair, one error, two queued, one hidden, one trashed.

Usage:
    .venv/bin/python docs/demo/make_demo_catalog.py [--out docs/demo/build]

Output:
    build/demo-library/    the synthetic photo library (originals)
    build/demo-state/      catalog.db, thumbs/, views/, people/
    build/manifest.json    fingerprints + authored responses (mock input)
    build/config.toml      phototext config pointing at the demo state

Then serve it:
    .venv/bin/phototext serve --config docs/demo/build/config.toml
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import zlib
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

MODEL = "gemma4:12b"
VEC_MARGIN = 250  # min luminance distance between manifest fingerprints

# ---------------------------------------------------------------------------
# fonts


def _font(size: int, bold: bool = False):
    name = "Arial Bold" if bold else "Arial"
    for candidate in (
        lambda: ImageFont.truetype(f"/System/Library/Fonts/Supplemental/{name}.ttf", size),
        lambda: ImageFont.load_default(size=size),
        lambda: ImageFont.load_default(),
    ):
        try:
            return candidate()
        except Exception:
            continue
    raise RuntimeError("no usable font found")


def _center(d, cx, y, text, font, fill):
    w = d.textlength(text, font=font)
    d.text((cx - w / 2, y), text, font=font, fill=fill)


def _right(d, rx, y, text, font, fill):
    w = d.textlength(text, font=font)
    d.text((rx - w, y), text, font=font, fill=fill)


# ---------------------------------------------------------------------------
# painters — each returns (image, recovered text)


def paint_receipt(p):
    w, h = 760, 1120
    im = Image.new("RGB", (w, h), (250, 249, 245))
    d = ImageDraw.Draw(im)
    accent = p["accent"]
    d.rectangle([0, 0, w, 118], fill=accent)
    _center(d, w / 2, 22, p["store"], _font(40, bold=True), "white")
    _center(d, w / 2, 74, p["tagline"], _font(22), (235, 240, 248))
    _center(d, w / 2, 140, p["dt"], _font(26), (40, 40, 40))
    y = 200
    f, fb = _font(30), _font(30, bold=True)
    subtotal = sum(float(price) for _, price in p["items"])
    lines = [p["store"], p["tagline"], p["dt"], ""]
    for item, price in p["items"]:
        d.text((60, y), item, font=f, fill=(30, 30, 30))
        _right(d, w - 60, y, price, f, (30, 30, 30))
        dots = d.textlength(".", font=f)
        x0 = 60 + d.textlength(item, font=f) + 14 * dots
        x1 = w - 60 - d.textlength(price, font=f) - 14 * dots
        if x1 > x0:
            n = int((x1 - x0) / dots)
            d.text((x0, y), "." * n, font=f, fill=(150, 150, 150))
        lines.append(f"{item:<24}{price:>8}")
        y += 52
    y += 14
    d.line([60, y, w - 60, y], fill=(200, 200, 200), width=2)
    y += 16
    for label, value, bold in (
        ("SUBTOTAL", f"{subtotal:.2f}", False),
        ("TAX", p["tax"], False),
        ("TOTAL", p["total"], True),
    ):
        font = fb if bold else f
        d.text((60, y), label, font=font, fill=(20, 20, 20) if bold else (60, 60, 60))
        _right(d, w - 60, y, value, font, (20, 20, 20) if bold else (60, 60, 60))
        lines.append(f"{label:<24}{value:>8}")
        y += 56
    y += 10
    _center(d, w / 2, y, p["card"], f, (110, 110, 110))
    _center(d, w / 2, y + 44, p["footer"], _font(24), (130, 130, 130))
    lines += ["", p["card"], p["footer"]]
    return im, "\n".join(lines)


def paint_invoice(p):
    w, h = 1000, 1300
    im = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(im)
    accent = p["accent"]
    d.rectangle([0, 0, w, 14], fill=accent)
    d.text((70, 60), p["company"], font=_font(46, bold=True), fill=(25, 25, 25))
    d.text((70, 116), p["company_sub"], font=_font(22), fill=(110, 110, 110))
    d.text((70, 200), "INVOICE", font=_font(34, bold=True), fill=accent)
    _right(d, w - 70, 200, f"No. {p['no']}", _font(30), (60, 60, 60))
    _right(d, w - 70, 240, p["dt"], _font(26), (110, 110, 110))
    d.text((70, 320), "BILL TO", font=_font(22, bold=True), fill=(130, 130, 130))
    y = 352
    for line in p["bill_to"]:
        d.text((70, y), line, font=_font(26), fill=(50, 50, 50))
        y += 36
    y += 40
    d.line([70, y, w - 70, y], fill=(225, 225, 225), width=2)
    y += 24
    f = _font(28)
    d.text((70, y), "DESCRIPTION", font=_font(22, bold=True), fill=(130, 130, 130))
    _right(d, w - 70, y, "AMOUNT", _font(22, bold=True), fill=(130, 130, 130))
    y += 48
    lines = [
        p["company"], p["company_sub"], "", "INVOICE", f"No. {p['no']}", p["dt"],
        "", "BILL TO", *p["bill_to"], "", "DESCRIPTION" + " " * 40 + "AMOUNT",
    ]
    subtotal = 0.0
    for desc, amount in p["lines"]:
        d.text((70, y), desc, font=f, fill=(40, 40, 40))
        _right(d, w - 70, y, amount, f, (40, 40, 40))
        d.line([70, y + 40, w - 70, y + 40], fill=(240, 240, 240), width=1)
        lines.append(f"{desc:<44}{amount:>10}")
        subtotal += float(amount)
        y += 56
    y += 30
    for label, value, bold in (
        ("Subtotal", f"{subtotal:.2f}", False),
        ("Tax", p["tax"], False),
        ("TOTAL DUE", p["total"], True),
    ):
        font = _font(30, bold=True) if bold else f
        d.text((w - 420, y), label, font=font, fill=(20, 20, 20) if bold else (80, 80, 80))
        _right(d, w - 70, y, value, font, (20, 20, 20) if bold else (80, 80, 80))
        lines.append(f"{label:<44}{value:>10}")
        y += 52
    y += 40
    d.text((70, y), p["note"], font=_font(24), fill=(130, 130, 130))
    lines += ["", p["note"]]
    return im, "\n".join(lines)


def paint_letter(p):
    w, h = 1100, 1400
    im = Image.new("RGB", (w, h), (252, 249, 240))
    d = ImageDraw.Draw(im)
    f = _font(30)
    fb = _font(30, bold=True)
    _right(d, w - 80, 70, p["date"], f, (90, 90, 90))
    d.text((80, 70), p["sender"], font=fb, fill=(60, 60, 60))
    d.text((80, 140), p["addr"], font=f, fill=(120, 120, 120))
    y = 300
    lines = [p["sender"], p["addr"], p["date"], "", p["greeting"]]
    d.text((80, y), p["greeting"], font=fb, fill=(35, 35, 35))
    y += 90
    for para in p["body"]:
        for line in para:
            d.text((80, y), line, font=f, fill=(45, 45, 45))
            lines.append(line)
            y += 48
        y += 30
        lines.append("")
    y += 40
    d.text((80, y), p["closing"], font=f, fill=(45, 45, 45))
    d.text((80, y + 70), p["signature"], font=_font(40), fill=(25, 25, 25))
    lines += [p["closing"], p["signature"]]
    return im, "\n".join(lines)


def paint_postcard(p):
    w, h = 1100, 800
    im = Image.new("RGB", (w, h), "white")
    top = paint_scene({"variant": p["scene"], "w": w, "h": int(h * 0.55)})[0]
    im.paste(top, (0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle([0, int(h * 0.55) - 6, w, int(h * 0.55)], fill=(230, 230, 230))
    y0 = int(h * 0.55) + 40
    f = _font(26)
    lines = [p["greeting"]]
    d.text((60, y0), p["greeting"], font=_font(30), fill=(40, 40, 40))
    y = y0 + 56
    for line in p["message"]:
        d.text((60, y), line, font=f, fill=(60, 60, 60))
        lines.append(line)
        y += 42
    ay = y0
    for line in p["address"]:
        d.text((640, ay), line, font=f, fill=(35, 35, 35))
        lines.append(line)
        ay += 42
    d.rectangle([920, y0 - 20, 1050, y0 + 110], outline=(60, 60, 60), width=3)
    d.text((935, y0 + 20), p["stamp"], font=_font(24), fill=(60, 60, 60))
    lines.append(p["stamp"])
    d.line([600, y0 - 20, 600, h - 40], fill=(200, 200, 200), width=2)
    return im, "\n".join(lines)


def paint_sign(p):
    w, h = 1200, 900
    im = Image.new("RGB", (w, h), p["wall"])
    d = ImageDraw.Draw(im)
    bg, fg = p["bg"], p["fg"]
    mx, my = p.get("margin", (90, 190))
    d.rounded_rectangle([mx, my, w - mx, h - my], 36, fill=bg, outline=p["edge"], width=10)
    y = my + 110
    lines = []
    for i, line in enumerate(p["lines"]):
        font = _font(p["sizes"][i], bold=True)
        _center(d, w / 2, y, line, font, fg)
        lines.append(line)
        y += p["sizes"][i] + 70
    if p.get("sub"):
        _center(d, w / 2, h - 285, p["sub"], _font(30), fg)
        lines.append(p["sub"])
    return im, "\n".join(lines)


def paint_menu(p):
    w, h = 1000, 1300
    im = Image.new("RGB", (w, h), (43, 33, 24))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w, 130], fill=(28, 21, 15))
    _center(d, w / 2, 36, p["title"], _font(44, bold=True), (245, 238, 220))
    _center(d, w / 2, 96, p["sub"], _font(24), (205, 190, 165))
    f, fb = _font(28), _font(28, bold=True)
    cream, gold = (240, 233, 216), (224, 185, 120)
    y = 190
    lines = [p["title"], p["sub"]]
    for header, items in p["sections"]:
        d.text((80, y), header, font=fb, fill=gold)
        lines.append(header)
        y += 50
        d.line([80, y, w - 80, y], fill=(90, 75, 58), width=2)
        y += 22
        for item, price in items:
            d.text((96, y), item, font=f, fill=cream)
            _right(d, w - 96, y, price, f, cream)
            lines.append(f"{item:<30}{price:>8}")
            y += 48
        y += 34
    return im, "\n".join(lines)


def paint_ticket(p):
    w, h = 1400, 560
    im = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(im)
    accent = p["accent"]
    d.rectangle([0, 0, w, h], fill=(248, 247, 244))
    d.rectangle([0, 0, 1000, h], fill=accent)
    d.ellipse([985, -25, 1015, 25], fill=(248, 247, 244))
    d.ellipse([985, h - 25, 1015, h + 25], fill=(248, 247, 244))
    white = "white"
    d.text((70, 80), p["event"], font=_font(54, bold=True), fill=white)
    d.text((70, 170), p["venue"], font=_font(30), fill=(235, 240, 248))
    d.text((70, 250), p["dt"], font=_font(34), fill=white)
    d.text((70, 330), p["info"], font=_font(26), fill=(235, 240, 248))
    d.rounded_rectangle([70, 410, 400, 490], 18, outline=white, width=4)
    d.text((100, 425), p["seat"], font=_font(34, bold=True), fill=white)
    _center(d, 1200, 200, p["code"], _font(44, bold=True), (50, 50, 50))
    _center(d, 1200, 280, "ADMIT ONE", _font(28), (120, 120, 120))
    for i in range(24):
        x = 1030 + i * 14
        d.line([x, 120, x, 440], fill=(210, 205, 195), width=3)
    lines = [
        p["event"], p["venue"], p["dt"], p["info"], p["seat"],
        p["code"], "ADMIT ONE",
    ]
    return im, "\n".join(lines)


def paint_chat(p):
    w, h = 800, 1400
    im = Image.new("RGB", (w, h), (203, 218, 238))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w, 110], fill=(55, 55, 60))
    _center(d, w / 2, 12, p["status_time"], _font(26), "white")
    d.text((40, 12), "LTE", font=_font(22), fill=(220, 220, 225))
    _right(d, w - 40, 12, "87% ▮", _font(22), (220, 220, 225))
    d.text((40, 58), "‹", font=_font(34), fill="white")
    _center(d, w / 2, 62, p["header"], _font(28, bold=True), "white")
    y = 150
    f = _font(28)
    lines = [f"{p['status_time']} — {p['header']}"]
    for side, text, ts in p["msgs"]:
        tw = d.textlength(text, font=f)
        bw = max(tw + 48, 180)
        if side == "l":
            x0, fill, tcol = 40, (225, 225, 230), (110, 110, 120)
        else:
            x0, fill, tcol = w - 40 - bw, (60, 130, 246), "white"
        d.rounded_rectangle([x0, y, x0 + bw, y + 76], 22, fill=fill)
        d.text((x0 + 24, y + 22), text, font=f, fill=(30, 30, 35) if side == "l" else "white")
        d.text((x0 + 8, y + 82), ts, font=_font(20), fill=tcol)
        lines.append(text)
        y += 122
    return im, "\n".join(lines)


def paint_settings(p):
    w, h = 1000, 900
    im = Image.new("RGB", (w, h), (245, 245, 248))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w, 70], fill=(235, 235, 240))
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([24 + i * 34, 24, 42 + i * 34, 42], fill=c)
    _center(d, w / 2, 20, p["title"], _font(24), (80, 80, 85))
    y = 110
    f = _font(28)
    lines = [p["title"]]
    for row in p["rows"]:
        label, value, on = (row + (None,))[:3] if len(row) == 2 else row
        d.rounded_rectangle([40, y, w - 40, y + 92], 14, fill="white",
                            outline=(228, 228, 233), width=2)
        d.text((70, y + 30), label, font=f, fill=(45, 45, 50))
        if isinstance(on, bool):
            bg = (60, 130, 246) if on else (210, 210, 215)
            knob = (225, 225, 230) if on else "white"
            kx = w - 118 if on else w - 158
            d.rounded_rectangle([w - 170, y + 32, w - 66, y + 60], 14, fill=bg)
            d.ellipse([kx, y + 28, kx + 36, y + 64], fill=knob)
            state = "ON" if on else "OFF"
        else:
            _right(d, w - 70, y + 30, str(value), f, (130, 130, 140))
            state = str(value)
        lines.append(f"{label}: {state}")
        y += 104
    return im, "\n".join(lines)


def paint_book(p):
    w, h = 1000, 1400
    im = Image.new("RGB", (w, h), (247, 244, 236))
    d = ImageDraw.Draw(im)
    _center(d, w / 2, 90, p["title"], _font(40, bold=True), (40, 40, 45))
    _center(d, w / 2, 156, p["author"], _font(26), (110, 110, 115))
    d.line([w / 2 - 90, 210, w / 2 + 90, 210], fill=(180, 175, 165), width=2)
    y = 280
    f = _font(28)
    lines = [p["title"], p["author"], ""]
    for line in p["text"]:
        d.text((110, y), line, font=f, fill=(55, 55, 60))
        lines.append(line)
        y += 46
    _center(d, w / 2, h - 90, p["page"], _font(24), (140, 140, 145))
    lines += ["", p["page"]]
    return im, "\n".join(lines)


def paint_note(p):
    w, h = 900, 1150
    im = Image.new("RGB", (w, h), (253, 248, 210))
    d = ImageDraw.Draw(im)
    d.line([110, 0, 110, h], fill=(226, 120, 120), width=4)
    ink = (38, 66, 148)
    f = _font(34)
    y = 80
    lines = []
    for line in p["lines"]:
        d.text((150, y), line, font=f, fill=ink)
        lines.append(line)
        y += 66
    d.line([60, y + 30, w - 60, y + 34], fill=ink, width=3)
    d.text((150, y + 60), p["signed"], font=_font(40), fill=ink)
    lines += ["", p["signed"]]
    return im, "\n".join(lines)


def paint_meme(p):
    w, h = 1200, 1200
    im = Image.new("RGB", (w, h), p["bg"])
    d = ImageDraw.Draw(im)
    # flat "subject" in the middle: a round unimpressed cat
    cx = w // 2 + p.get("cat_dx", 0)
    cy, r = h // 2, int(260 * p.get("cat_scale", 1.0))
    for dx, dy in ((-1, 0), (1, 0)):
        d.polygon([(cx + dx * r * 0.55, cy - r * 0.75),
                   (cx + dx * r * 0.95, cy - r * 1.45),
                   (cx + dx * r * 0.15, cy - r * 1.15)], fill=p["cat"])
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=p["cat"])
    d.polygon([(cx - r * 0.9, cy + r * 0.2), (cx, cy + r * 1.35), (cx + r * 0.9, cy + r * 0.2)],
              fill=p["cat"])
    for ex in (-1, 1):
        d.ellipse([cx + ex * 100 - 34, cy - 90, cx + ex * 100 + 34, cy - 22], fill="white")
        d.ellipse([cx + ex * 100 - 12, cy - 72, cx + ex * 100 + 12, cy - 40], fill=(25, 25, 25))
    d.line([cx - 70, cy + 95, cx + 70, cy + 95], fill=(25, 25, 25), width=10)
    for wx in (-1, 0, 1):
        d.line([cx + wx * 46, cy + 40, cx + wx * 46, cy + 95], fill=(255, 255, 255), width=6)
    fb = _font(64, bold=True)
    lines = []
    y = 60
    for line in p["top"]:
        _center(d, cx, y, line, fb, "white")
        lines.append(line)
        y += 86
    y = h - 80 - 86 * (len(p["bottom"]) - 1)
    for line in p["bottom"]:
        _center(d, cx, y, line, fb, "white")
        lines.append(line)
        y += 86
    return im, "\n".join(lines)


def paint_scene(p):
    variant = p["variant"]
    w = p.get("w", 1400)
    h = p.get("h", 1000)
    pal = p.get("palette", {})
    im = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(im)

    def band(y0, y1, color):
        d.rectangle([0, y0, w, y1], fill=color)

    if variant == "beach":
        band(0, h * 0.55, pal.get("sky", (120, 190, 230)))
        d.ellipse([w * 0.62, h * 0.08, w * 0.78, h * 0.24], fill=(255, 238, 170))
        band(h * 0.55, h * 0.78, pal.get("sea", (40, 160, 175)))
        d.rectangle([0, h * 0.55, w, h * 0.60], fill=(90, 195, 205))
        band(h * 0.78, h, pal.get("sand", (232, 210, 160)))
    elif variant == "mountains":
        band(0, h * 0.62, pal.get("sky", (150, 130, 175)))
        d.ellipse([w * 0.70, h * 0.06, w * 0.86, h * 0.22], fill=(250, 240, 210))
        for x0, x1, peak, color in (
            (int(-w * 0.1), int(w * 0.45), int(h * 0.30), (95, 80, 120)),
            (int(w * 0.25), int(w * 0.85), int(h * 0.22), (120, 100, 145)),
            (int(w * 0.55), int(w * 1.1), int(h * 0.34), (80, 65, 105)),
        ):
            d.polygon([(x0, int(h * 0.62)), (x1, int(h * 0.62)), ((x0 + x1) // 2, peak)], fill=color)
        band(h * 0.62, h, pal.get("ground", (70, 60, 95)))
    elif variant == "forest":
        band(0, h * 0.55, pal.get("sky", (175, 215, 200)))
        band(h * 0.55, h, pal.get("ground", (85, 120, 80)))
        for row, (y, size, color) in enumerate((
            (int(h * 0.58), 130, (55, 95, 60)),
            (int(h * 0.70), 170, (45, 82, 52)),
            (int(h * 0.86), 220, (36, 70, 44)),
        )):
            x = 40
            while x < w + 100:
                d.polygon([(x, y), (x + size // 2, y - size), (x + size, y)], fill=color)
                x += size // 2 + 30
    elif variant == "city":
        band(0, h * 0.68, pal.get("sky", (250, 175, 120)))
        d.ellipse([w * 0.72, h * 0.38, w * 0.88, h * 0.54], fill=(255, 235, 190))
        x = 30
        import random as _r
        rng = _r.Random(7)
        while x < w:
            bw = rng.randint(90, 190)
            bh = rng.randint(int(h * 0.25), int(h * 0.52))
            d.rectangle([x, h * 0.68 - bh, x + bw, h * 0.68], fill=pal.get("bldg", (60, 55, 75)))
            for wy in range(int(h * 0.68 - bh + 20), int(h * 0.68) - 16, 34):
                for wx in range(x + 14, x + bw - 12, 26):
                    if rng.random() < 0.6:
                        d.rectangle([wx, wy, wx + 12, wy + 14], fill=(255, 225, 160))
            x += bw + 18
        band(h * 0.68, h, pal.get("ground", (45, 40, 58)))
    elif variant == "desert":
        band(0, h * 0.6, pal.get("sky", (245, 200, 140)))
        d.ellipse([w * 0.60, h * 0.10, w * 0.76, h * 0.26], fill=(255, 245, 200))
        band(h * 0.6, h, pal.get("sand", (225, 175, 110)))
        d.ellipse([int(-w * 0.2), int(h * 0.55), int(w * 0.6), int(h * 0.95)], fill=(210, 155, 90))
        d.ellipse([int(w * 0.5), int(h * 0.68), int(w * 1.3), int(h * 1.0)], fill=(235, 185, 120))
    elif variant == "lake":
        band(0, h * 0.52, pal.get("sky", (235, 245, 250)))
        for x0, x1, peak, color in (
            (int(-w * 0.05), int(w * 0.5), int(h * 0.18), (110, 125, 140)),
            (int(w * 0.45), int(w * 1.05), int(h * 0.26), (140, 150, 160)),
        ):
            d.polygon([(x0, int(h * 0.52)), (x1, int(h * 0.52)), ((x0 + x1) // 2, peak)], fill=color)
        band(h * 0.52, h, pal.get("water", (110, 160, 185)))
        d.rectangle([0, h * 0.52, w, h * 0.56], fill=(150, 185, 205))
        for i in range(6):
            y = h * 0.60 + i * h * 0.065
            d.line([w * 0.25, y, w * 0.75, y], fill=(190, 215, 228), width=4)
    elif variant == "garden":
        band(0, h * 0.5, pal.get("sky", (210, 232, 220)))
        band(h * 0.5, h, pal.get("grass", (120, 165, 105)))
        import random as _r
        rng = _r.Random(11)
        for _ in range(26):
            fx, fy = rng.randint(30, w - 30), rng.randint(int(h * 0.55), h - 40)
            col = rng.choice([(235, 130, 140), (245, 200, 90), (200, 140, 200), (240, 240, 240)])
            for dx, dy in ((0, -14), (-12, 4), (12, 4)):
                d.ellipse([fx + dx - 9, fy + dy - 9, fx + dx + 9, fy + dy + 9], fill=col)
            d.line([fx, fy + 8, fx, fy + 26], fill=(70, 110, 65), width=4)
    elif variant == "harbor":
        band(0, h * 0.6, pal.get("sky", (190, 205, 215)))
        for x0, x1, peak, color in (
            (int(-w * 0.1), int(w * 0.4), int(h * 0.25), (120, 130, 140)),
            (int(w * 0.5), int(w * 1.1), int(h * 0.32), (95, 105, 115)),
        ):
            d.polygon([(x0, int(h * 0.6)), (x1, int(h * 0.6)), ((x0 + x1) // 2, peak)], fill=color)
        band(h * 0.6, h, pal.get("water", (75, 115, 135)))
        d.polygon([(w * 0.38, h * 0.78), (w * 0.62, h * 0.78), (w * 0.58, h * 0.86), (w * 0.42, h * 0.86)],
                  fill=(55, 50, 60))
        d.rectangle([w * 0.49, h * 0.60, w * 0.51, h * 0.78], fill=(55, 50, 60))
        d.polygon([(w * 0.51, h * 0.62), (w * 0.60, h * 0.70), (w * 0.51, h * 0.70)], fill=(240, 240, 235))
    else:
        raise ValueError(variant)
    return im, ""


def paint_person(p):
    w, h = 1200, 1600
    im = Image.new("RGB", (w, h), p["bg"])
    d = ImageDraw.Draw(im)
    split = p.get("band", (0.62, 18))
    d.rectangle([0, int(h * split[0]), w, h],
                fill=tuple(min(c + split[1], 255) for c in p["bg"]))
    if p.get("window"):
        x0, y0, x1, y1 = p["window"]
        d.rectangle([x0, y0, x1, y1], fill=(216, 226, 232))
        d.rectangle([x0, y0, x1, y1], outline=(120, 100, 80), width=10)
        d.line([(x0 + x1) // 2, y0, (x0 + x1) // 2, y1], fill=(120, 100, 80), width=8)
        d.line([x0, (y0 + y1) // 2, x1, (y0 + y1) // 2], fill=(120, 100, 80), width=8)
    d.ellipse([w * 0.1, h * 0.72, w * 0.9, h * 1.25], fill=tuple(min(c - 12, 255) for c in p["bg"]))
    for fx, fy, fr, skin, hair, shirt, glasses in p["figures"]:
        # shoulders (behind the head)
        d.rounded_rectangle([fx - fr * 2.1, fy + fr * 1.5, fx + fr * 2.1, h + 40], 120, fill=shirt)
        d.rectangle([fx - 34, fy + fr - 12, fx + 34, fy + fr + 90], fill=skin)  # neck
        d.ellipse([fx - fr, fy - fr, fx + fr, fy + fr], fill=skin)  # head
        d.pieslice([fx - fr, fy - fr, fx + fr, fy + fr], 180, 360, fill=hair)  # hair
        d.rectangle([fx - fr, fy - fr, fx - fr + 26, fy], fill=hair)
        d.rectangle([fx + fr - 26, fy - fr, fx + fr, fy], fill=hair)
        for ex in (-1, 1):
            d.ellipse([fx + ex * 52 - 13, fy - 26, fx + ex * 52 + 13, fy], fill=(30, 30, 35))
        d.arc([fx - 48, fy + 18, fx + 48, fy + 86], 20, 160, fill=(120, 70, 70), width=8)
        if glasses:
            for ex in (-1, 1):
                d.ellipse([fx + ex * 55 - 38, fy - 36, fx + ex * 55 + 38, fy + 12],
                          outline=(70, 60, 55), width=7)
                d.line([fx + ex * 17, fy - 12, fx - ex * 17, fy - 12], fill=(70, 60, 55), width=7)
    return im, ""


PAINTERS = {
    "receipt": paint_receipt,
    "invoice": paint_invoice,
    "letter": paint_letter,
    "postcard": paint_postcard,
    "sign": paint_sign,
    "menu": paint_menu,
    "ticket": paint_ticket,
    "chat": paint_chat,
    "settings": paint_settings,
    "book": paint_book,
    "note": paint_note,
    "meme": paint_meme,
    "scene": paint_scene,
    "person": paint_person,
}

# ---------------------------------------------------------------------------
# demo people (shirt colors double as the mock matcher's signal)


def _figure(kind, cx, cy, fr, bg):
    if kind == "maya":
        return (cx, cy, fr, (242, 201, 160), (62, 44, 36), (235, 95, 90), False)
    if kind == "theo":
        return (cx, cy, fr, (224, 170, 120), (180, 120, 60), (85, 165, 100), False)
    return (cx, cy, fr, (221, 188, 152), (210, 210, 212), (110, 90, 215), True)


SOLO_BOX = (330, 270, 540, 750)
DUO_BOXES = ((140, 300, 390, 690), (660, 300, 390, 690))


def _solo(kind, bg, cx=600, cy=500, r=150, band=(0.62, 18), window=None):
    return dict(bg=bg, band=band, window=window,
                figures=[_figure(kind, cx, cy, r, bg)])


def _duo(bg, spread=(330, 870), r=118, band=(0.66, 16)):
    lx, rx = spread
    return dict(
        bg=bg, band=band,
        figures=[_figure("maya", lx, 420, r, bg), _figure("theo", rx, 420, r, bg)],
    )


# ---------------------------------------------------------------------------
# the demo photo set


def S(file, sub, taken, kind, params, ctx, category, language="en",
      text_kind=None, has_text=True, flags=(), cluster=None):
    return dict(file=file, sub=sub, taken=taken, kind=kind, params=params,
                ctx=ctx, category=category, language=language,
                text_kind=text_kind, has_text=has_text, flags=list(flags),
                cluster=cluster)


def photo_set():
    photos = [
        # receipts
        S("cafe-receipt-2013.jpg", "2013", "2013:04:12 09:42:00", "receipt",
          dict(store="BLUEBOTTLE COFFEE", tagline="SAN FRANCISCO, CA",
               dt="04/12/2013  09:42",
               items=[("LATTE", "4.50"), ("CROISSANT", "3.25"), ("TIP", "1.00")],
               tax="0.00", total="8.75", card="VISA ****4821",
               footer="THANK YOU — SEE YOU SOON", accent=(32, 118, 178)),
          "A paper cafe receipt from Bluebottle Coffee: a latte and a croissant, $8.75 on Visa.",
          "receipt"),
        S("grocery-receipt.jpg", "2019", "2019:11:02 17:20:00", "receipt",
          dict(store="HARBOR MARKET", tagline="SEASIDE, OR",
               dt="11/02/2019  17:20",
               items=[("MILK 2%", "3.89"), ("EGGS DOZEN", "4.29"), ("BREAD SOURDOUGH", "5.50"),
                      ("APPLES GALA LB", "2.99"), ("COFFEE 12OZ", "8.99")],
               tax="1.29", total="26.95", card="DEBIT ****3347",
               footer="THANK YOU FOR SHOPPING LOCAL", accent=(22, 132, 92)),
          "A grocery receipt from Harbor Market: milk, eggs, bread, apples and coffee, $26.95.",
          "receipt"),
        S("hardware-receipt.jpg", "2016", "2016:06:18 11:05:00", "receipt",
          dict(store="PARKSIDE HARDWARE", tagline="SPRINGFIELD, OH",
               dt="06/18/2016  11:05",
               items=[("WOOD SCREWS #8", "6.49"), ("SANDBAPER 120", "3.19"), ("PAINT BRUSH 2IN", "7.99")],
               tax="1.18", total="18.85", card="CASH",
               footer="RETURNS WITHIN 30 DAYS W/ RECEIPT", accent=(176, 74, 32)),
          "A hardware store receipt: screws, sandpaper and a paint brush, paid cash.",
          "receipt"),
        S("restaurant-receipt.jpg", "2021", "2021:09:24 20:15:00", "receipt",
          dict(store="TRATTORIA ROMA", tagline="NORTH END, BOSTON",
               dt="09/24/2021  20:15",
               items=[("BRUSCHETTA", "9.50"), ("TAGLIATELLE", "19.00"),
                      ("TIRAMISU", "8.00"), ("SAN PELLEGRINO", "4.50")],
               tax="3.42", total="44.42", card="AMEX ****1009",
               footer="GRAZIE — 18% GRATUITY INCLUDED", accent=(122, 28, 36)),
          "A dinner receipt from Trattoria Roma: bruschetta, tagliatelle, tiramisu and sparkling water.",
          "receipt"),
        # invoices and letters
        S("invoice-42.jpg", "2024", "2024:03:02 10:00:00", "invoice",
          dict(company="ACME SUPPLY LLC", company_sub="INDUSTRIAL GOODS — SINCE 1987",
               no="42", dt="March 2, 2024",
               bill_to=["Northwind Manufacturing", "2100 Foundry Road", "Portland, OR 97209"],
               lines=[("Widget assembly kit", "1240.00"), ("Sprocket set (12)", "480.00"),
                      ("Shipping — freight", "165.00")],
               tax="145.75", total="2030.75",
               note="Payment due net 30. Make checks payable to Acme Supply LLC.",
               accent=(28, 74, 140)),
          "Invoice 42 from Acme Supply to Northwind Manufacturing: parts and freight, $2,030.75 due.",
          "invoice"),
        S("utility-bill.jpg", "2023", "2023:08:01 09:00:00", "invoice",
          dict(company="CITY POWER & LIGHT", company_sub="MUNICIPAL UTILITY",
               no="AUG-2023", dt="August 1, 2023",
               bill_to=["R. Alvarez", "88 Larkspur Lane", "Springfield, OH 45501"],
               lines=[("Electric — 612 kWh", "98.40"), ("Grid maintenance fee", "12.00")],
               tax="8.19", total="118.59",
               note="Budget billing available — call 555-0100.", accent=(60, 110, 60)),
          "An August utility bill from City Power & Light: electricity and fees, $118.59.",
          "invoice"),
        S("letter-aunt-2011.jpg", "2011", "2011:02:14 15:30:00", "letter",
          dict(sender="Rose Delacroix", addr="14 Willow Street, Lafayette, LA",
               date="February 14, 2011",
               greeting="Dear Ellie,",
               body=[["Your cousin's tomatoes came in early this year — the wet"],
                     ["spring agreed with them. I have started six seedlings"],
                     ["for your garden along the south fence, like we said."],
                     [""],
                     ["The church bazaar is the first Saturday of June. Bring"],
                     ["your lemon bars if you can; Father Michael asks about you."]],
               closing="With love,", signature="Aunt Rose"),
          "A handwritten letter from Aunt Rose about her garden, seedlings and the June church bazaar.",
          "letter", text_kind="handwriting"),
        # postcards
        S("postcard-paris.jpg", "2015", "2015:05:09 14:20:00", "postcard",
          dict(scene="city", greeting="Bonjour from Paris!",
               message=["The rain stopped just long enough for this", "view of the river. Wish you were here."],
               address=["Marie Laurent", "12 Rue des Lilas", "69003 Lyon, France"],
               stamp="LA POSTE 0,85 €"),
          "A postcard from Paris with a short message in French to Marie Laurent in Lyon.",
          "postcard", language="fr", text_kind="other"),
        S("postcard-canyon.jpg", "2018", "2018:07:22 08:45:00", "postcard",
          dict(scene="desert", greeting="Greetings from the canyon!",
               message=["Hiked down at dawn — five miles, worth every", "step. The layers glow at sunrise."],
               address=["Ben & Susie Carter", "PO Box 219", "Flagstaff, AZ 86002"],
               stamp="USA 34¢"),
          "A postcard from canyon country to Ben and Susie Carter in Flagstaff.",
          "postcard", text_kind="other"),
        # signs
        S("sign-open-24h.jpg", "2017", "2017:03:11 23:10:00", "sign",
          dict(lines=["OPEN", "24 HOURS"], sizes=[130, 90], sub="SELF SERVICE • NO IDLE PARKING",
               bg=(29, 78, 196), fg="white", edge=(220, 225, 240), wall=(208, 206, 198),
               margin=(70, 180)),
          "A lit blue sign in a window: OPEN 24 HOURS, self service.",
          "sign", text_kind="sign"),
        S("sign-coffee.jpg", "2024", "2024:10:05 07:55:00", "sign",
          dict(lines=["COFFEE", "ROASTED DAILY"], sizes=[120, 74], sub="SINCE 1962 — OPEN 6AM",
               bg=(120, 72, 32), fg=(255, 240, 205), edge=(90, 55, 25), wall=(150, 142, 130),
               margin=(150, 250)),
          "A wooden coffee shop sign: coffee roasted daily since 1962, open 6am.",
          "sign", text_kind="sign"),
        S("sign-parking.jpg", "2013", "2013:08:30 12:00:00", "sign",
          dict(lines=["PARKING", "$5 / DAY"], sizes=[110, 80],
               sub="PAY AT KIOSK — NO OVERNIGHT", bg=(24, 24, 26), fg=(245, 200, 60),
               edge=(230, 230, 235), wall=(132, 138, 146), margin=(100, 210)),
          "A yellow-on-black parking sign: $5 per day, pay at kiosk.",
          "sign", text_kind="sign"),
        S("sign-beach.jpg", "2019", "2019:06:15 16:40:00", "sign",
          dict(lines=["DOG BEACH"], sizes=[120], sub="LEASH OPTIONAL — CLEAN UP AFTER",
               bg=(16, 105, 120), fg="white", edge=(200, 235, 240), wall=(168, 182, 188),
               margin=(150, 260)),
          "A teal sign pointing to the dog beach: leash optional, clean up after pets.",
          "sign", text_kind="sign"),
        # menus
        S("menu-cafe.jpg", "2022", "2022:01:08 08:30:00", "menu",
          dict(title="THE DAILY GRIND", sub="COFFEE & SMALL BITES",
               sections=[("ESPRESSO BAR", [("ESPRESSO", "3.00"), ("MACCHIATO", "3.75"),
                                          ("FLAT WHITE", "4.25"), ("LATTE", "4.50"),
                                          ("CORTADO", "4.00"), ("FILTER OF THE DAY", "3.50")]),
                         ("FROM THE CASE", [("BUTTER CROISSANT", "3.25"), ("ALMOND ROLL", "3.75"),
                                            ("SAVORY SCONE", "4.00")])]),
          "A cafe menu board: espresso drinks from $3.00 and pastries from the case.",
          "menu", text_kind="menu"),
        S("menu-diner.jpg", "2014", "2014:11:27 09:15:00", "menu",
          dict(title="ROSIE'S DINER", sub="BREAKFAST ALL DAY",
               sections=[("CLASSICS", [("PANCAKE STACK", "7.50"), ("EGGS ANY STYLE", "6.25"),
                                       ("BACON OR SAUSAGE", "3.50"), ("HASH BROWNS", "2.75")]),
                         ("SIDES", [("TOAST", "1.50"), ("OATMEAL", "4.00")])]),
          "A diner breakfast menu: pancakes, eggs, bacon, hash browns.",
          "menu", text_kind="menu"),
        # tickets
        S("ticket-concert.jpg", "2018", "2018:10:12 19:30:00", "ticket",
          dict(event="THE MIDNIGHT ECHOES", venue="THE ORPHEUM — MEZZANINE",
               dt="FRI OCT 12 2018 — DOORS 8PM", info="STANDING ROOM / ALL AGES",
               seat="ROW J SEAT 12", code="ME-81247", accent=(88, 36, 135)),
          "A concert ticket for The Midnight Echoes at the Orpheum, row J seat 12.",
          "ticket", text_kind="other"),
        S("ticket-flight.jpg", "2023", "2023:05:18 06:10:00", "ticket",
          dict(event="SFO → JFK", venue="SKYWARD AIR 418 — GATE B22",
               dt="THU MAY 18 2023 — 6:10A", info="BOARDING GROUP 3 / WINDOW",
               seat="SEAT 14A", code="0QK4ND", accent=(26, 96, 180)),
          "A boarding pass: Skyward Air 418 from SFO to JFK, seat 14A, boarding group 3.",
          "ticket", text_kind="other"),
        S("ticket-museum.jpg", "2016", "2016:04:09 13:45:00", "ticket",
          dict(event="MUSEUM OF FLIGHT", venue="SPECIAL EXHIBIT — WINGS OVER TIME",
               dt="SAT APR 9 2016 — 1:45PM", info="ADMIT ONE ADULT",
               seat="TIMED ENTRY", code="MF-2041", accent=(140, 100, 30)),
          "A museum admission ticket for the Museum of Flight special exhibit.",
          "ticket", text_kind="other"),
        # screenshots
        S("screenshot-coffee-order.jpg", "2025", "2025:02:14 09:41:00", "chat",
          dict(status_time="9:41", header="Sam",
               msgs=[("l", "morning! on my way, want anything?", "9:38 AM"),
                     ("r", "yes please — large oat latte, extra shot", "9:39 AM"),
                     ("r", "and a croissant if they have them", "9:39 AM"),
                     ("l", "one large oat latte extra shot + croissant, got it", "9:40 AM")]),
          "A text message conversation with Sam about a coffee order: large oat latte and a croissant.",
          "screenshot", text_kind="screenshot"),
        S("screenshot-reminder.jpg", "2024", "2024:09:30 18:22:00", "chat",
          dict(status_time="6:22", header="Dr. Patel's office",
               msgs=[("l", "Reminder: dental cleaning Thursday 10/03 at 9:00 AM.", "4:02 PM"),
                     ("l", "Reply C to confirm, R to reschedule.", "4:02 PM"),
                     ("r", "C", "5:47 PM")]),
          "A text reminder from the dentist's office about a Thursday cleaning appointment.",
          "screenshot", text_kind="screenshot", flags=("trash",)),
        S("screenshot-settings.jpg", "2025", "2025:03:01 21:05:00", "settings",
          dict(title="Library — Settings", rows=[("Process derivatives", True),
                                                 ("Vision OCR at scan", True),
                                                 ("Idle detection", True),
                                                 ("Theme", "icloud"),
                                                 ("Two-tier gate", False)]),
          "A settings window with toggles for scan-time OCR, idle detection and processing options.",
          "screenshot", text_kind="screenshot"),
        # book and notes
        S("book-page.jpg", "2012", "2012:07:19 21:00:00", "book",
          dict(title="THE LONG WAY HOME", author="E. M. Halloway — 1961",
               text=["The road out of the valley had been gravel once, but the",
                     "winter had taken its share and left the rest to the",
                     "grasses. She counted fence posts to keep her mind off",
                     "the distance, the way her father had taught her, and",
                     "by the ninety-first post the town appeared below,",
                     "small and patient in the evening light.",
                     "",
                     "At the general store she asked after the man with the",
                     "gray truck. The clerk remembered him, and remembered",
                     "the truck, and had opinions about both which he was",
                     "generous with. She bought bread, matches and a pound",
                     "of coffee, and walked out past the ninety-first post."],
               page="— 117 —"),
          "A page from a novel: a woman walking out of the valley, asking after a man with a gray truck.",
          "book", text_kind="book"),
        S("recipe-card.jpg", "2011", "2011:11:20 16:00:00", "note",
          dict(lines=["Mom's Pancakes", "2 C flour", "2 T sugar", "1 t salt",
                      "2 eggs, beaten", "2 C buttermilk", "1/2 stick butter, melted",
                      "fold gently — lumps are good!"],
               signed="— Mom"),
          "Grandma's handwritten recipe card for buttermilk pancakes on yellow paper.",
          "recipe", text_kind="handwriting"),
        S("note-groceries.jpg", "2025", "2025:09:19 18:30:00", "note",
          dict(lines=["MILK", "EGGS", "BREAD", "coffee filters", "batteries AA"],
               signed="— for the weekend"),
          "A handwritten grocery list: milk, eggs, bread, coffee filters, batteries.",
          "note", text_kind="handwriting", flags=("queued",)),
        # memes
        S("meme-monday-1.jpg", "2020", "2020:04:06 12:30:00", "meme",
          dict(bg=(58, 60, 88), cat=(250, 214, 137),
               top=["MONDAYS."], bottom=[]),
          "The grumpy cat MONDAYS meme, reshared.",
          "meme", text_kind="other", cluster="monday"),
        S("meme-monday-2.jpg", "2020", "2020:04:10 09:00:00", "meme",
          dict(bg=(52, 54, 82), cat=(246, 209, 132),
               top=["MONDAYS."], bottom=[]),
          "The grumpy cat MONDAYS meme, reshared.",
          "meme", text_kind="other", cluster="monday"),
        S("meme-monday-3.jpg", "2021", "2021:01:11 08:15:00", "meme",
          dict(bg=(64, 66, 94), cat=(254, 219, 142),
               top=["MONDAYS."], bottom=[]),
          "The grumpy cat MONDAYS meme, reshared.",
          "meme", text_kind="other", cluster="monday"),
        S("meme-diet-1.jpg", "2022", "2022:03:14 15:00:00", "meme",
          dict(bg=(150, 140, 160), cat=(240, 220, 190), cat_scale=0.75, cat_dx=150,
               top=["ME:"], bottom=["ONE MORE COOKIE WON'T HURT"]),
          "A cat meme about diet promises: one more cookie won't hurt.",
          "meme", text_kind="other", cluster="diet"),
        S("meme-diet-2.jpg", "2022", "2022:03:20 11:00:00", "meme",
          dict(bg=(146, 136, 156), cat=(236, 215, 184), cat_scale=0.75, cat_dx=150,
               top=["ME:"], bottom=["ONE MORE COOKIE WON'T HURT"]),
          "A cat meme about diet promises: one more cookie won't hurt.",
          "meme", text_kind="other", cluster="diet"),
        # scenes (no text)
        S("IMG_2841.jpg", "2015", "2015:07:04 17:30:00", "scene",
          dict(variant="beach"), "An empty beach in late afternoon: turquoise sea and warm sand.",
          "beach", has_text=False, text_kind="scene"),
        S("IMG_3105.jpg", "2017", "2017:09:30 18:50:00", "scene",
          dict(variant="mountains"), "Dusk in the mountains, three peaks fading into purple.",
          "mountains", has_text=False, text_kind="scene"),
        S("IMG_0177.jpg", "2013", "2013:10:12 14:05:00", "scene",
          dict(variant="forest"), "Dense evergreen forest in three receding rows.",
          "forest", has_text=False, text_kind="scene"),
        S("IMG_5210.jpg", "2019", "2019:01:20 19:15:00", "scene",
          dict(variant="city"), "A city skyline at dusk with lit windows.",
          "city", has_text=False, text_kind="scene"),
        S("IMG_4402.jpg", "2021", "2021:05:05 10:40:00", "scene",
          dict(variant="desert"), "Rolling desert dunes under a pale hot sky.",
          "desert", has_text=False, text_kind="scene"),
        S("IMG_0043.jpg", "2011", "2011:08:08 07:20:00", "scene",
          dict(variant="lake"), "A calm alpine lake mirroring two ridgelines.",
          "lake", has_text=False, text_kind="scene", flags=("dup-base",), cluster="lake"),
        S("IMG_3388.jpg", "2020", "2020:05:10 12:00:00", "scene",
          dict(variant="garden"), "A flower garden in full bloom.",
          "garden", has_text=False, text_kind="scene"),
        S("IMG_6002.jpg", "2024", "2024:06:30 16:45:00", "scene",
          dict(variant="harbor"), "A small sailboat harbor below gray hills.",
          "harbor", has_text=False, text_kind="scene", flags=("queued",)),
        # people (no text; shirt colors are the matcher signal)
        S("maya-01.jpg", "2019", "2019:05:04 14:10:00", "person",
          _solo("maya", (110, 90, 80), cx=490, cy=540, r=150, band=(0.55, 30)),
          "Maya smiling at the camera in a coral shirt.",
          "person", has_text=False, text_kind="scene", flags=("seed-maya", "box-solo")),
        S("maya-02.jpg", "2022", "2022:08:19 11:30:00", "person",
          _solo("maya", (64, 74, 88), cx=700, cy=430, r=138, band=(0.78, 28)),
          "Maya laughing, eyes closed, same coral shirt.",
          "person", has_text=False, text_kind="scene", flags=("person-maya", "box-solo")),
        S("maya-03.jpg", "2024", "2024:12:01 17:00:00", "person",
          _solo("maya", (92, 84, 104), cx=620, cy=560, r=155, band=(0.66, 24)),
          "Maya at golden hour in her coral shirt.",
          "person", has_text=False, text_kind="scene", flags=("person-maya", "box-solo")),
        S("theo-01.jpg", "2020", "2020:09:12 15:20:00", "person",
          _solo("theo", (86, 96, 76), cx=520, cy=470, r=146, band=(0.62, 32)),
          "Theo mid-laugh in his green jacket.",
          "person", has_text=False, text_kind="scene", flags=("seed-theo", "box-solo")),
        S("theo-02.jpg", "2023", "2023:03:17 10:45:00", "person",
          _solo("theo", (120, 94, 78), cx=740, cy=520, r=120, band=(0.70, 26),
          window=(60, 180, 520, 820)),
          "Theo on a foggy morning, green jacket zipped up.",
          "person", has_text=False, text_kind="scene", flags=("person-theo", "box-solo")),
        S("theo-03.jpg", "2025", "2025:01:05 13:15:00", "person",
          _solo("theo", (62, 64, 86), cx=560, cy=460, r=170, band=(0.52, 34)),
          "Theo leaning on a railing, grinning in green.",
          "person", has_text=False, text_kind="scene", flags=("person-theo", "box-solo")),
        S("grandma-01.jpg", "2011", "2011:12:25 12:00:00", "person",
          _solo("grandma", (96, 70, 100), cx=660, cy=550, r=140, band=(0.58, 30)),
          "Grandma in her violet blouse by the Christmas tree.",
          "person", has_text=False, text_kind="scene", flags=("seed-grandma", "box-solo")),
        S("grandma-02.jpg", "2014", "2014:07:06 16:30:00", "person",
          _solo("grandma", (52, 86, 80), cx=500, cy=530, r=132, band=(0.74, 22)),
          "Grandma in the garden, violet blouse and silver hair.",
          "person", has_text=False, text_kind="scene", flags=("person-grandma", "box-solo")),
        S("grandma-03.jpg", "2018", "2018:04:01 13:00:00", "person",
          _solo("grandma", (118, 84, 72), cx=710, cy=500, r=150, band=(0.50, 36)),
          "Grandma laughing at the kitchen table.",
          "person", has_text=False, text_kind="scene", flags=("person-grandma", "box-solo")),
        S("maya-theo-01.jpg", "2023", "2023:06:10 18:20:00", "person",
          _duo((84, 84, 76), spread=(320, 880), r=112, band=(0.66, 16)),
          "Maya and Theo together at a friend's barbecue.",
          "person", has_text=False, text_kind="scene",
          flags=("person-maya", "person-theo", "box-duo")),
        S("maya-theo-02.jpg", "2024", "2024:10:31 19:45:00", "person",
          _duo((48, 96, 108), spread=(400, 800), r=118, band=(0.72, 30)),
          "Maya and Theo in costume on Halloween.",
          "person", has_text=False, text_kind="scene",
          flags=("person-maya", "person-theo", "box-duo")),
        # one photo the model cannot read (ends up in the Errors tab)
        S("sign-neon-broken.jpg", "2025", "2025:04:02 22:30:00", "sign",
          dict(lines=["O EN", "N 24 H"], sizes=[110, 80],
               bg=(30, 30, 42), fg=(255, 60, 80), edge=(255, 90, 110), wall=(18, 18, 26),
               margin=(140, 230)),
          "A broken neon sign with several letters unlit.",
          "sign", text_kind="sign", flags=("fail",)),
        # the duplicate pair (resized re-encode of the lake photo)
        dict(file="lake-copy-small.jpg", sub="2011", taken="2011:08:08 07:20:00",
             kind="scene", params=dict(variant="lake"),
             ctx="A calm alpine lake mirroring two ridgelines.", category="lake",
             language="unknown", text_kind="scene", has_text=False,
             flags=("dup-of", "IMG_0043.jpg"), cluster="lake"),
    ]
    return photos


# ---------------------------------------------------------------------------
# build helpers


def luminance_vec(im, size=16):
    return list(im.convert("L").resize((size, size)).getdata())


DOC_KINDS = {"receipt", "invoice", "letter", "menu", "book", "note", "ticket"}
SURFACES = [
    (96, 64, 40), (140, 100, 60), (70, 72, 74), (120, 80, 60),
    (86, 90, 96), (110, 86, 70), (78, 62, 52), (150, 110, 80),
]


def on_surface(paper: Image.Image, name: str) -> Image.Image:
    """Photograph a document: paper inset on a table surface.

    Surface color, paper scale and offset derive from the file name hash, so
    same-archetype documents get structurally different photos (and their
    9x8 dhash skeletons stop clustering as memes/duplicates).
    """
    h = zlib.crc32(name.encode())
    surface = SURFACES[h % len(SURFACES)]
    scale = 0.72 + ((h >> 3) % 10) / 100
    pw, ph = int(paper.width * scale), int(paper.height * scale)
    mx, my = paper.width - pw, paper.height - ph
    ox = (h >> 7) % max(mx, 1)
    oy = (h >> 11) % max(my, 1)
    canvas = Image.new("RGB", paper.size, surface)
    d = ImageDraw.Draw(canvas)
    d.rectangle([ox + 14, oy + 18, ox + pw + 8, oy + ph + 12],
                fill=tuple(max(c - 18, 0) for c in surface))
    canvas.paste(paper.resize((pw, ph)), (ox, oy))
    return canvas


def save_with_exif(im, path, taken):
    exif = Image.Exif()
    exif.get_ifd(0x8769)[36867] = taken
    exif[306] = taken
    im.save(path, exif=exif, quality=90)


def build_library(out: Path, lib_dir: Path):
    from pillow_heif import register_heif_opener

    register_heif_opener()
    lib = lib_dir
    if lib.exists():
        import shutil

        shutil.rmtree(lib)
    lib.mkdir(parents=True)
    (lib / ".metadata_never_index").write_bytes(b"")
    print(f"library dir: {lib}")
    manifest = []
    texts = {}
    for spec in photo_set():
        sub = lib / spec["sub"]
        sub.mkdir(exist_ok=True)
        path = sub / spec["file"]
        painter = PAINTERS[spec["kind"]]
        im, text = painter(spec["params"])
        if spec["kind"] == "scene":
            im = im.resize((1400, 1000)) if im.size != (1400, 1000) else im
        if spec["kind"] in DOC_KINDS:
            im = on_surface(im, spec["file"])
        save_with_exif(im, path, spec["taken"])
        if "dup-of" in spec["flags"]:
            base = lib / spec["sub"] / spec["flags"][spec["flags"].index("dup-of") + 1]
            small = Image.open(base).copy()
            small = small.resize((700, 500))
            small.save(path, exif=Image.open(base).info.get("exif"), quality=60)
            text = ""
        text = re.sub(r"\n{3,}", "\n\n", text).strip("\n")
        manifest.append(
            {
                "name": spec["file"],
                "vec": luminance_vec(Image.open(path)),
                "response": {
                    "has_text": spec["has_text"],
                    "text": text,
                    "context": spec["ctx"] or "",
                    "text_kind": spec["text_kind"] or ("document" if spec["has_text"] else "none"),
                    "language": spec["language"],
                    "category": spec["category"],
                },
                "fail": "fail" in spec["flags"],
            }
        )
        texts[spec["file"]] = manifest[-1]["response"]
    # sanity: every fingerprint must be clearly nearest to itself.
    # Same-cluster pairs (meme variants, the duplicate pair) are expected to
    # be near-identical and share one response, so they are exempt.
    clusters = [s.get("cluster") for s in photo_set()]
    worst = None
    for i, a in enumerate(manifest):
        for j, b in enumerate(manifest):
            if i == j or (clusters[i] and clusters[i] == clusters[j]):
                continue
            d = sum(abs(x - y) for x, y in zip(a["vec"], b["vec"]))
            if worst is None or d < worst[0]:
                worst = (d, a["name"], b["name"])
    assert worst and worst[0] >= VEC_MARGIN, (
        f"manifest fingerprints too close: {worst[1]} vs {worst[2]} "
        f"distance {worst[0]} < {VEC_MARGIN} — make the images more distinct"
    )
    print(f"library: {len(manifest)} photos in {lib}")
    print(f"manifest: closest distinct pair distance {worst[0]} (margin {VEC_MARGIN})")
    (out / "manifest.json").write_text(json.dumps(manifest))
    return lib, texts


# ---------------------------------------------------------------------------
# pipeline


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class CLI:
    def __init__(self, config_path: Path):
        self.cmd = [sys.executable, "-u", "-m", "phototext", "--config", str(config_path)]

    def run(self, *args, env=None, expect=0, timeout=600):
        proc = subprocess.run(
            self.cmd + list(args), capture_output=True, text=True,
            timeout=timeout, env=env, cwd=str(ROOT),
        )
        out = proc.stdout + proc.stderr
        if proc.returncode != expect:
            print(out[-2000:])
            raise SystemExit(
                f"exit {proc.returncode} != {expect}: phototext {' '.join(args)}"
            )
        return out


def phototext_id_of(conn, filename):
    """Photo id by basename. The '%/' prefix anchors the match to the whole
    basename so theo-01.jpg never matches maya-theo-01.jpg."""
    row = conn.execute(
        "SELECT p.id FROM photos p JOIN locations l ON l.photo_id = p.id "
        "WHERE l.path LIKE '%/' || ?",
        (filename,),
    ).fetchone()
    assert row is not None, f"no photo for {filename}"
    return row[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "build"))
    ap.add_argument("--lib-dir",
                    default="~/Pictures/phototext-demo-library")
    args = ap.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    lib_dir = Path(args.lib_dir).expanduser().resolve()

    lib, texts = build_library(out, lib_dir)
    state = out / "demo-state"
    if state.exists():
        import shutil

        shutil.rmtree(state)
    db_path = state / "catalog.db"

    port = free_port()
    mock = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve().parent / "demo_mock.py"),
         "--port", str(port), "--manifest", str(out / "manifest.json")],
    )
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tags", timeout=1)
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise SystemExit("demo mock did not start")

        config = out / "config.toml"
        config.write_text(
            f'ollama_url = "http://127.0.0.1:{port}"\n'
            f'model = "{MODEL}"\n'
            f'db_path = "{db_path}"\n'
            "max_attempts = 2\n"
            "transport_retries = 2\n"
            "transport_backoff_s = 1\n"
            "request_timeout_s = 60\n"
            "person_min_confidence = 0.6\n"
        )
        cli = CLI(config)

        print("[1/6] scan")
        cli.run("scan", str(lib))
        print("[2/6] run")
        cli.run("run")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        ok, errored = 0, 0
        for spec in photo_set():
            pid = phototext_id_of(conn, spec["file"])
            row = conn.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
            if "fail" in spec["flags"]:
                errored += 1
                assert row["status"] == "error", f"{spec['file']}: expected error, got {row['status']}"
                continue
            assert row["status"] == "done", (
                f"{spec['file']}: status {row['status']} — {row['error']}"
            )
            expected = texts[spec["file"]]
            assert row["text"] == expected["text"], (
                f"{spec['file']}: stored text mismatch\n  stored: {row['text'][:120]!r}\n"
                f"  expect: {expected['text'][:120]!r}"
            )
            assert row["category"] == expected["category"], (
                f"{spec['file']}: category {row['category']!r} != {expected['category']!r}"
            )
            ok += 1
        print(f"      {ok} done, {errored} error (as designed)")

        print("      staging realistic durations")
        for spec in photo_set():
            if "fail" in spec["flags"]:
                continue
            pid = phototext_id_of(conn, spec["file"])
            seed = zlib.crc32(spec["file"].encode())
            cat = spec["category"]
            if cat in ("scene", "person"):
                lo, hi = 6000, 11000
            elif cat in ("screenshot", "chat") or cat == "screenshot":
                lo, hi = 9000, 14000
            else:
                lo, hi = 12000, 26000
            ms = lo + seed % (hi - lo)
            conn.execute("UPDATE photos SET duration_ms=? WHERE id=?", (ms, pid))
        conn.commit()

        print("[3/6] people")
        env = os.environ.copy()
        faces = []
        for spec in photo_set():
            boxes = (
                DUO_BOXES if "box-duo" in spec["flags"]
                else (SOLO_BOX,) if "box-solo" in spec["flags"]
                else ()
            )
            if not boxes:
                continue
            pid = phototext_id_of(conn, spec["file"])
            faces.append(
                f"{pid}:" + "+".join(f"{x},{y},{w},{h}" for x, y, w, h in boxes)
            )
        env["PHOTOTEXT_TEST_FACES"] = ";".join(faces)

        seeds = [
            ("maya-01.jpg", "Maya"),
            ("theo-01.jpg", "Theo"),
            ("grandma-01.jpg", "Grandma"),
        ]
        for frag, name in seeds:
            pid = phototext_id_of(conn, frag)
            cli.run("people", "name", str(pid), name, "--box",
                    f"{SOLO_BOX[0]},{SOLO_BOX[1]},{SOLO_BOX[2]},{SOLO_BOX[3]}")
        cli.run("people", "run", env=env)
        for person, want in (("Maya", 4), ("Theo", 4), ("Grandma", 2)):
            n = conn.execute(
                "SELECT COUNT(*) FROM person_tags t JOIN people p ON p.id = t.person_id "
                "WHERE p.name = ? AND t.present = 1 AND t.origin = 'model'",
                (person,),
            ).fetchone()[0]
            assert n == want, f"{person}: {n} model tags, expected {want}"
        theo_conf = conn.execute(
            "SELECT t.confidence FROM person_tags t JOIN people p ON p.id = t.person_id "
            "WHERE p.name = 'Theo' AND t.present = 1 AND t.origin = 'model' LIMIT 1"
        ).fetchone()[0]
        assert theo_conf < 0.6, f"Theo confidence {theo_conf} should sit in the review queue"
        print("      3 people tagged (Theo in the review queue, as designed)")

        print("[4/6] cache previews")
        cli.run("cache-previews")

        print("[5/6] hide + trash")
        hide_id = phototext_id_of(conn, "sign-parking.jpg")
        cli.run("hide", str(hide_id))
        trash_id = phototext_id_of(conn, "screenshot-reminder.jpg")
        cli.run("delete", str(trash_id))

        print("[6/6] leave two photos queued")
        for frag in ("IMG_6002.jpg", "note-groceries.jpg"):
            pid = phototext_id_of(conn, frag)
            conn.execute(
                "UPDATE photos SET status='queued', attempts=0, error=NULL, "
                "started_at=NULL, finished_at=NULL WHERE id=?", (pid,),
            )
        conn.commit()
        counts = dict(
            conn.execute(
                "SELECT status, COUNT(*) FROM photos GROUP BY status"
            ).fetchall()
        )
        conn.close()
        print(f"      catalog: {counts}")
    finally:
        mock.send_signal(signal.SIGTERM)
        mock.wait(timeout=10)

    print(f"""
demo catalog ready: {db_path}

Serve it (read-only):
    .venv/bin/phototext serve --config {out / 'config.toml'}

Serve it writable (people picker, confirm chips, bulk delete):
    .venv/bin/phototext serve --config {out / 'config.toml'} --writable
""")


if __name__ == "__main__":
    main()
