"""Face-region detection via the macOS Vision framework.

`detect_faces(path, photo_id)` returns pixel boxes (x, y, w, h) in display
(EXIF-oriented) coordinates — the same frame `imaging.crop_jpeg`, the web
picker, and `people name --box` use — largest face first. Photos with no
detected faces let `people run` skip the model call entirely; photos with
faces are matched on face crops instead of the whole image.

macOS only: Vision ships with the OS and runs on-device (a few tens of ms
per photo). If the import fails, every call returns [] and the matching
pass falls back to whole-photo evaluation.

Test seam: `PHOTOTEXT_TEST_FACES="<photo_id>:x,y,w,h[+x,y,w,h;...]"` —
e2e only. When set and the photo id matches (or the key is `*`), those
boxes are returned verbatim without touching Vision, keeping the
faces-found path deterministic without real face fixtures.
"""

from __future__ import annotations

import os
from pathlib import Path

from .imaging import display_size as _display_size

try:  # macOS only; degrade to "no faces" everywhere else
    import Quartz  # noqa: F401
    from Vision import VNDetectFaceRectanglesRequest, VNImageRequestHandler

    _VISION_OK = True
except Exception:  # pragma: no cover - non-macOS or stripped system
    _VISION_OK = False

MAX_FACES = 4


def vision_problem() -> str | None:
    """None when Vision is usable, else a one-line reason (for doctor)."""
    if _VISION_OK:
        return None
    return "macOS Vision framework is not importable (face detection disabled)"


def _test_overrides(photo_id: int | None) -> list[tuple[int, int, int, int]] | None:
    raw = os.environ.get("PHOTOTEXT_TEST_FACES", "").strip()
    if not raw or photo_id is None:
        return None
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        key, _, spec = entry.partition(":")
        if key not in ("*", str(photo_id)) or not spec.strip():
            continue
        boxes = []
        for part in spec.split("+"):
            try:
                x, y, w, h = (int(v.strip()) for v in part.split(","))
                boxes.append((x, y, w, h))
            except ValueError:
                continue
        return boxes
    return None


def _vision_boxes(path: Path) -> list[tuple[int, int, int, int]]:
    size = _display_size(path)
    if size is None:
        return []
    width, height = size
    url = Quartz.CFURLCreateFromFileSystemRepresentation(
        None, str(path).encode("utf-8"), len(str(path).encode("utf-8")), False
    )
    if url is None:
        return []
    handler = VNImageRequestHandler.alloc().initWithURL_options_(url, None)
    request = VNDetectFaceRectanglesRequest.alloc().init()
    try:
        ok, _error = handler.performRequests_error_([request], None)
    except Exception:
        return []
    if not ok:
        return []
    boxes = []
    for obs in request.results() or []:
        bb = obs.boundingBox()
        if bb.size.width <= 0 or bb.size.height <= 0:
            continue
        # Vision: normalized, origin bottom-left; convert to top-left pixels.
        x = int(bb.origin.x * width)
        y = int((1.0 - bb.origin.y - bb.size.height) * height)
        w = int(bb.size.width * width)
        h = int(bb.size.height * height)
        if w > 0 and h > 0:
            boxes.append((x, y, w, h))
    boxes.sort(key=lambda b: b[2] * b[3], reverse=True)
    return boxes


def detect_faces(
    path: Path, photo_id: int | None = None
) -> list[tuple[int, int, int, int]]:
    """Face boxes (x, y, w, h) in display pixels, largest first.

    Returns [] when Vision is unavailable, the file is unreadable, or no
    faces are found. The e2e test seam (see module docstring) short-circuits
    this for deterministic fixtures.
    """
    override = _test_overrides(photo_id)
    if override is not None:
        return override
    if not _VISION_OK or not Path(path).exists():
        return []
    try:
        return _vision_boxes(Path(path))
    except Exception:
        return []
