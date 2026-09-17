#!/usr/bin/env python3
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


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
