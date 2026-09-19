"""Optical character recognition via the macOS Vision framework.

`vision_ocr(path, photo_id)` returns recognized text joined with newlines,
stripped; "" when nothing is recognized or Vision is unavailable.

macOS only: Vision ships with the OS and runs on-device (a few hundred ms
per photo). If the import fails, every call returns "".

Test seam: `PHOTOTEXT_TEST_OCR="<photo_id>|*:text to use"` — e2e only.
When set and the photo id matches (or the key is `*`), that text is returned
verbatim without touching Vision, keeping the OCR-found path deterministic
without real OCR fixtures.
"""

from __future__ import annotations

import os
from pathlib import Path

try:  # macOS only; degrade to "" everywhere else
    import Quartz  # noqa: F401
    from Vision import VNRecognizeTextRequest, VNImageRequestHandler

    _VISION_OK = True
except Exception:  # pragma: no cover - non-macOS or stripped system
    _VISION_OK = False


def vision_problem() -> str | None:
    """None when Vision is usable, else a one-line reason (for doctor)."""
    if _VISION_OK:
        return None
    return "macOS Vision framework is not importable (OCR disabled)"


def _test_override(photo_id: int | None) -> str | None:
    raw = os.environ.get("PHOTOTEXT_TEST_OCR", "").strip()
    if not raw or photo_id is None:
        return None
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        key, _, text = entry.partition(":")
        if key not in ("*", str(photo_id)):
            continue
        return text
    return None


def _vision_recognize(path: Path) -> str:
    url = Quartz.CFURLCreateFromFileSystemRepresentation(
        None, str(path).encode("utf-8"), len(str(path).encode("utf-8")), False
    )
    if url is None:
        return ""
    handler = VNImageRequestHandler.alloc().initWithURL_options_(url, None)
    request = VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLanguages_(["en-US", "fr-FR", "de-DE", "es-ES", "it-IT",
                                      "pt-BR", "zh-Hans", "zh-Hant", "ja-JP", "ko-KR",
                                      "ru-RU", "ar-SA"])
    request.setRecognitionLevel_(1)  # 1 = accurate
    request.setUsesLanguageCorrection_(True)
    try:
        ok, _error = handler.performRequests_error_([request], None)
    except Exception:
        return ""
    if not ok:
        return ""
    lines: list[str] = []
    for obs in request.results() or []:
        top = obs.topCandidates_(1)
        if top and len(top) > 0:
            lines.append(top[0].string())
    return "\n".join(lines).strip()


def vision_ocr(path: Path, photo_id: int | None = None) -> str:
    """Recognize text in an image using macOS Vision.

    Returns the recognized text joined with newlines and stripped, or ""
    when Vision is unavailable, the file is unreadable, or nothing is found.
    The e2e test seam (see module docstring) short-circuits this for
    deterministic fixtures.
    """
    override = _test_override(photo_id)
    if override is not None:
        return override
    if not _VISION_OK or not Path(path).exists():
        return ""
    try:
        return _vision_recognize(Path(path))
    except Exception:
        return ""