#!/usr/bin/env python3
"""Mock Ollama server with authored, per-photo responses for the demo catalog.

Reads the manifest written by make_demo_catalog.py: a JSON list of
{name, vec, response, fail} entries. Extraction calls are matched by nearest
16x16 luminance vector (downscales do not move it), so every generated demo
photo gets its authored text/context/category. Person calls are answered from
face-crop colors (the demo people each wear a distinctive shirt).

Usage:
    .venv/bin/python docs/demo/demo_mock.py --port 11434 \
        --manifest docs/demo/build/manifest.json
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

MODEL = "gemma4:12b"

# Demo people: shirt colors double as the matcher's signal. Theo's 0.52
# confidence lands in the web review queue (person_min_confidence = 0.6).
PEOPLE = [
    {"name": "Maya", "confidence": 0.9,
     "test": lambda r, g, b: r > 190 and g < 140 and b < 140},
    {"name": "Theo", "confidence": 0.52,
     "test": lambda r, g, b: g > 140 and r < 150 and b < 150},
    {"name": "Grandma", "confidence": 0.85,
     "test": lambda r, g, b: b > 170 and r < 180},
]

PROFILES = {
    "Maya": "Woman in her thirties, shoulder-length dark hair, warm smile, "
    "usually wearing a coral shirt.",
    "Theo": "Young man with short sandy hair and a green jacket, slight grin, "
    "often photographed mid-laugh.",
    "Grandma": "Elderly woman with silver hair, round glasses, violet blouse, "
    "gentle expression.",
}
GENERIC_PROFILE = "A person with distinctive features, seen clearly and well lit."

_manifest: list[dict] = []


def _vec(img_b64: str, size: int = 16) -> list[int] | None:
    try:
        im = Image.open(io.BytesIO(base64.b64decode(img_b64))).convert("L")
        return list(im.resize((size, size)).getdata())
    except Exception:
        return None


def _nearest(vec: list[int]) -> dict | None:
    best, best_d = None, None
    for entry in _manifest:
        d = sum(abs(a - b) for a, b in zip(vec, entry["vec"]))
        if best_d is None or d < best_d:
            best, best_d = entry, d
    return best


def _crop_color(img_b64: str) -> tuple[int, int, int] | None:
    """Sample the crop at center/72% height — inside the shirt in every demo
    face crop (heads sit at 20-45%, shoulders below 45%). Flat colors survive
    JPEG re-encoding, so one pixel is a stable classifier."""
    try:
        im = Image.open(io.BytesIO(base64.b64decode(img_b64))).convert("RGB")
        return im.getpixel((im.width // 2, int(im.height * 0.72)))
    except Exception:
        return None


def _person_match_response(payload: dict) -> dict:
    prompt = payload["messages"][-1].get("content") or ""
    ids = [
        (int(m.group(1)), m.group(2))
        for m in re.finditer(
            r'"person_id"\s*:\s*(\d+),\s*\n?\s*"name"\s*:\s*"([^"]+)"', prompt
        )
    ]
    if not ids:
        ids = [(int(m), str(m))
               for m in re.findall(r'"person_id"\s*:\s*(\d+)', prompt)]
    seen = set()
    ordered = [p for p in ids if not (p in seen or seen.add(p))]
    people = {p["name"]: p for p in PEOPLE}
    matches = []
    for pid, name in ordered:
        rule = people.get(name)
        present = False
        if rule is not None:
            for img in payload["messages"][-1].get("images") or []:
                rgb = _crop_color(img)
                if rgb is not None and rule["test"](*rgb):
                    present = True
                    break
        matches.append(
            {
                "person_id": pid,
                "present": present,
                "confidence": rule["confidence"] if present else 0.05,
            }
        )
    return {"matches": matches}


def _describe_response(payload: dict) -> dict:
    """Pick the canned profile by matching the seed crop to a demo person."""
    text = GENERIC_PROFILE
    for img in payload["messages"][-1].get("images") or []:
        rgb = _crop_color(img)
        if rgb is None:
            continue
        for person in PEOPLE:
            if person["test"](*rgb):
                text = PROFILES[person["name"]]
                break
        break
    return {"description": text}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            self._json(200, {"models": [{"name": MODEL}]})
        elif self.path == "/api/ps":
            self._json(200, {"models": []})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/chat":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        fmt = payload.get("format")
        props = (fmt or {}).get("properties") if isinstance(fmt, dict) else None
        if isinstance(props, dict):
            if "matches" in props:
                self._json(200, {
                    "model": MODEL,
                    "created_at": "2026-01-01T00:00:00Z",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(_person_match_response(payload)),
                    },
                    "done": True,
                })
                return
            if "description" in props and "context" not in props:
                self._json(200, {
                    "model": MODEL,
                    "created_at": "2026-01-01T00:00:00Z",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(_describe_response(payload)),
                    },
                    "done": True,
                })
                return
        images = payload["messages"][-1].get("images") or []
        vec = _vec(images[0]) if images else None
        entry = _nearest(vec) if vec is not None else None
        if entry is None or entry.get("fail"):
            self._json(500, {"error": "model server experienced an internal error"})
            return
        self._json(200, {
            "model": MODEL,
            "created_at": "2026-01-01T00:00:00Z",
            "message": {
                "role": "assistant",
                "content": json.dumps(entry["response"]),
            },
            "done": True,
        })


def main() -> None:
    global _manifest
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    _manifest = json.loads(Path(args.manifest).read_text())
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"demo mock ollama on http://127.0.0.1:{args.port} "
          f"({len(_manifest)} manifest entries)")
    server.serve_forever()


if __name__ == "__main__":
    main()
