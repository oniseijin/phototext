#!/usr/bin/env python3
"""mlx-serve (OpenAI-compatible) twin of mock_ollama.py: same modes,
mode-file/ps-file conventions, and response content — different endpoints
and wire shapes. `ps-file` is accepted for CLI compatibility and ignored
(the mlx client's loaded_models() is always empty)."""
import argparse
import base64
import io
import json
import re
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_EMBED_SYNONYMS = {
    "invoice": "bill", "receipt": "bill", "statement": "bill",
    "utility": "bill", "grocery": "food", "recipe": "food",
    "beach": "nature", "waves": "nature", "sunset": "nature",
}


def _sample_image_color(img_b64: str) -> tuple[int, int, int] | None:
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(base64.b64decode(img_b64))).convert("RGB")
        return im.getpixel((im.width // 2, int(im.height * 0.75)))
    except Exception:
        return None


def _split_content(payload: dict) -> tuple[str, list[str]]:
    """OpenAI multimodal content -> (prompt text, [image b64])."""
    content = payload["messages"][-1].get("content")
    if isinstance(content, str):
        return content, []
    prompt = ""
    images: list[str] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            prompt = str(part.get("text") or "")
        elif part.get("type") == "image_url":
            url = str((part.get("image_url") or {}).get("url") or "")
            if url.startswith("data:") and "," in url:
                images.append(url.split(",", 1)[1])
    return prompt, images


def _schema_properties(payload: dict) -> dict:
    rf = payload.get("response_format") or {}
    schema = (rf.get("json_schema") or {}).get("schema") or {}
    return schema.get("properties") or {}


def _person_match_response(payload: dict) -> dict:
    prompt, images = _split_content(payload)
    ids = [int(m) for m in re.findall(r'"person_id"\s*:\s*(\d+)', prompt)]
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

        def _chat(self, content: str) -> None:
            self._json(
                200,
                {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion",
                    "created": 0,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 10,
                        "total_tokens": 20,
                    },
                },
            )

        def _junk(self) -> None:
            self._chat('{"has_text": true, "text": "x' + ("\n" * 4000))

        def do_GET(self):
            if self.path == "/v1/models":
                self._json(
                    200,
                    {
                        "object": "list",
                        "data": [{"id": model, "object": "model"}],
                    },
                )
            else:
                self._json(404, {"error": {"message": "not found"}})

        def do_POST(self):
            if self.path == "/v1/embeddings":
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                inputs = payload.get("input") or []
                embeddings = []
                for text in inputs:
                    vec = [0.0] * 8
                    words = re.findall(r"[a-z]+", str(text).lower())
                    for w in words:
                        c = _EMBED_SYNONYMS.get(w, w)
                        idx = zlib.crc32(c.encode()) % 8
                        vec[idx] += 1.0
                    norm = (sum(v * v for v in vec)) ** 0.5
                    if norm > 0:
                        vec = [v / norm for v in vec]
                    embeddings.append(vec)
                self._json(
                    200,
                    {
                        "object": "list",
                        "data": [
                            {"object": "embedding", "index": i, "embedding": vec}
                            for i, vec in enumerate(embeddings)
                        ],
                    },
                )
                return
            if self.path != "/v1/chat/completions":
                self._json(404, {"error": {"message": "not found"}})
                return
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            mode = mode_file.read_text().strip() if mode_file.exists() else "ok"
            if mode != last_mode["mode"]:
                last_mode["mode"] = mode
                chat_calls["n"] = 0
            props = _schema_properties(payload)
            if "matches" in props:
                self._chat(json.dumps(_person_match_response(payload)))
                return
            if "description" in props and "context" not in props:
                self._chat(
                    json.dumps(
                        {
                            "description": (
                                "MOCK PERSON PROFILE: short dark hair, "
                                "red jacket, sturdy build"
                            )
                        }
                    )
                )
                return
            if payload.get("model") != model:
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
                self._chat(content)
                return
            if mode == "fail500":
                self._json(500, {"error": {"message": "mock: simulated server error"}})
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
                body = (b" " * 400) + json.dumps(
                    {
                        "id": "chatcmpl-mock",
                        "object": "chat.completion",
                        "created": 0,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": '{"has_text": true, "text": "trickle", '
                                    '"context": "slow drip", "text_kind": "document", '
                                    '"language": "en", "category": "document"}',
                                },
                                "finish_reason": "stop",
                            }
                        ],
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
                content = (
                    '{"has_text": true, "text": "half an emoji \\ud83e lands here '
                    'and stays in the stored text", "context": "A mock photo used '
                    'by the surrogate test.", "text_kind": "document", '
                    '"language": "en", "category": "document"}'
                )
                self._chat(content)
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
            self._chat(content)

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
