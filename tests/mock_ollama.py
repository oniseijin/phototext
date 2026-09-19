#!/usr/bin/env python3
import argparse
import base64
import io
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _sample_image_color(img_b64: str) -> tuple[int, int, int] | None:
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(base64.b64decode(img_b64))).convert("RGB")
        return im.getpixel((im.width // 2, int(im.height * 0.75)))
    except Exception:
        return None


def _person_match_response(payload: dict) -> dict:
    """Deterministic by image content: reddish -> confident match, greenish ->
    uncertain match (0.45), anything else -> absent."""
    prompt = payload["messages"][-1].get("content") or ""
    ids = [int(m) for m in re.findall(r'"person_id"\s*:\s*(\d+)', prompt)]
    images = payload["messages"][-1].get("images") or []
    present, confidence = False, 0.05
    if images:
        rgb = _sample_image_color(images[0])
        if rgb is not None:
            r, g, b = rgb
            if r > 150 and g < 110 and b < 110:
                present, confidence = True, 0.9
            elif g > 150 and r < 110 and b < 110:
                present, confidence = True, 0.45
    return {
        "matches": [
            {"person_id": pid, "present": present, "confidence": confidence}
            for pid in ids
        ]
    }


def build_handler(model: str, mode_file: Path, slow_seconds: float, ps_file: Path | None):
    chat_calls = {"n": 0}
    last_mode = {"mode": None}

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

        def _junk(self) -> None:
            self._json(
                200,
                {
                    "model": model,
                    "created_at": "2026-01-01T00:00:00Z",
                    "message": {
                        "role": "assistant",
                        "content": '{"has_text": true, "text": "x' + ("\n" * 4000),
                    },
                    "done": True,
                },
            )

        def do_GET(self):
            if self.path == "/api/tags":
                self._json(200, {"models": [{"name": model}]})
            elif self.path == "/api/ps":
                names = []
                if ps_file is not None and ps_file.exists():
                    names = [
                        line.strip()
                        for line in ps_file.read_text().splitlines()
                        if line.strip()
                    ]
                self._json(200, {"models": [{"name": n} for n in names]})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/api/chat":
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            mode = mode_file.read_text().strip() if mode_file.exists() else "ok"
            if mode != last_mode["mode"]:
                last_mode["mode"] = mode
                chat_calls["n"] = 0
            # Person calls are dispatched by their response schema before the
            # model-tag check (people use the prefilter/person model).
            fmt = payload.get("format")
            if isinstance(fmt, dict):
                props = fmt.get("properties") or {}
                if "matches" in props:
                    self._json(
                        200,
                        {
                            "model": payload.get("model"),
                            "created_at": "2026-01-01T00:00:00Z",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(_person_match_response(payload)),
                            },
                            "done": True,
                        },
                    )
                    return
                if "description" in props and "context" not in props:
                    self._json(
                        200,
                        {
                            "model": payload.get("model"),
                            "created_at": "2026-01-01T00:00:00Z",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {
                                        "description": (
                                            "MOCK PERSON PROFILE: short dark hair, "
                                            "red jacket, sturdy build"
                                        )
                                    }
                                ),
                            },
                            "done": True,
                        },
                    )
                    return
            if payload.get("model") != model:
                # gate call (two-tier prefilter uses a different model tag)
                gate_has_text = mode != "gatenotext"
                content = json.dumps(
                    {
                        "has_text": gate_has_text,
                        "category": "document" if gate_has_text else "scene",
                        "context": (
                            "a photo containing a document"
                            if gate_has_text
                            else "a calm landscape with no writing"
                        ),
                    }
                )
                self._json(
                    200,
                    {
                        "model": payload.get("model"),
                        "created_at": "2026-01-01T00:00:00Z",
                        "message": {"role": "assistant", "content": content},
                        "done": True,
                    },
                )
                return
            if mode == "fail500":
                self._json(500, {"error": "mock: simulated server error"})
                return
            if mode == "junkonce":
                chat_calls["n"] += 1
                if chat_calls["n"] == 1:
                    self._junk()
                    return
            if mode == "junkfirst2":
                chat_calls["n"] += 1
                if chat_calls["n"] <= 2:
                    self._junk()
                    return
            if mode == "slow":
                time.sleep(slow_seconds)
            if mode == "trickle":
                # Dribble a valid response one byte at a time: data keeps
                # arriving, so per-read socket timeouts never fire and only
                # the client's wall-clock cap can end the call.
                body = (b" " * 400) + json.dumps(
                    {
                        "model": model,
                        "created_at": "2026-01-01T00:00:00Z",
                        "message": {
                            "role": "assistant",
                            "content": '{"has_text": true, "text": "trickle", '
                            '"context": "slow drip", "text_kind": "document", '
                            '"language": "en", "category": "document"}',
                        },
                        "done": True,
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    for b in body:
                        self.wfile.write(bytes([b]))
                        self.wfile.flush()
                        time.sleep(0.05)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if mode == "surrogate":
                # Raw JSON text carrying an unpaired surrogate escape (half an
                # emoji): json.loads materializes a lone \ud83e that would
                # crash strict-UTF-8 storage if not sanitized.
                content = (
                    '{"has_text": true, "text": "half an emoji \\ud83e lands here '
                    'and stays in the stored text", "context": "A mock photo used '
                    'by the surrogate test.", "text_kind": "document", '
                    '"language": "en", "category": "document"}'
                )
                self._json(
                    200,
                    {
                        "model": model,
                        "created_at": "2026-01-01T00:00:00Z",
                        "message": {"role": "assistant", "content": content},
                        "done": True,
                    },
                )
                return
            content = json.dumps(
                {
                    "has_text": True,
                    "text": "MOCK EXTRACTED TEXT",
                    "context": "A mock photo used by the end-to-end test.",
                    "text_kind": "document",
                    "language": "en",
                    "category": "document",
                }
            )
            self._json(
                200,
                {
                    "model": model,
                    "created_at": "2026-01-01T00:00:00Z",
                    "message": {"role": "assistant", "content": content},
                    "done": True,
                },
            )

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemma4:mock")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--mode-file", required=True)
    ap.add_argument("--ps-file", default=None)
    ap.add_argument("--slow-seconds", type=float, default=2.5)
    args = ap.parse_args()
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port),
        build_handler(
            args.model,
            Path(args.mode_file),
            args.slow_seconds,
            Path(args.ps_file) if args.ps_file else None,
        ),
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
