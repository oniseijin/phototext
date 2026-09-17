from __future__ import annotations

import re

SYSTEM_PROMPT = (
    "You are a precise OCR and image analysis assistant. You transcribe visible text "
    "exactly, preserving line breaks and reading order, and you describe photos "
    "factually and briefly."
)

USER_PROMPT = (
    "Examine this photo and respond with JSON only.\n"
    "1. Transcribe ALL visible text exactly as written, preserving line breaks and "
    'reading order. If the photo contains no text, set "text" to an empty string and '
    '"has_text" to false.\n'
    '2. In "context", briefly describe what the photo shows and where the text '
    "appears, if any.\n"
    '3. In "text_kind", classify the text; in "language", give the main language of '
    "the text.\n"
    '4. In "category", classify what the photo shows in one or two lowercase words '
    "of your own choosing (e.g. document, sign, receipt, beach, concert, golf, "
    "birthday, food, screenshot, meme, scenery). Use a plain, generic noun."
)

VALID_TEXT_KINDS = [
    "document",
    "sign",
    "screenshot",
    "handwriting",
    "scene",
    "menu",
    "label",
    "book",
    "other",
    "none",
]

CATEGORY_EXAMPLES = [
    "document",
    "sign",
    "screenshot",
    "receipt",
    "label",
    "card",
    "meme",
    "scene",
    "food",
    "person",
    "other",
]

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "has_text": {"type": "boolean"},
        "text": {"type": "string"},
        "context": {"type": "string"},
        "text_kind": {"type": "string", "enum": VALID_TEXT_KINDS},
        "language": {"type": "string"},
        "category": {
            "type": "string",
            "description": "one or two lowercase generic words for what the photo shows",
            "maxLength": 40,
        },
    },
    "required": ["has_text", "text", "context", "text_kind", "language", "category"],
}

GATE_PROMPT = (
    "Look at this photo and respond with JSON only.\n"
    '1. "has_text": does the photo contain any readable text (words, numbers, '
    "signs)?\n"
    '2. "category": classify what the photo shows in one or two lowercase words '
    "of your own choosing (e.g. document, sign, receipt, beach, concert, food, "
    "screenshot, meme, scenery). Use a plain, generic noun.\n"
    '3. "context": one short sentence describing the photo.'
)

GATE_SCHEMA = {
    "type": "object",
    "properties": {
        "has_text": {"type": "boolean"},
        "category": {
            "type": "string",
            "description": "one or two lowercase generic words for what the photo shows",
            "maxLength": 40,
        },
        "context": {"type": "string"},
    },
    "required": ["has_text", "category", "context"],
}


PERSON_DESCRIBE_PROMPT = (
    "These image(s) all show the same person. Study how they look and respond "
    "with JSON only.\n"
    'In "description", write a concise but specific visual recognition profile '
    "of the person — face shape, hair, skin, build, and any distinguishing "
    "features or typical style — detailed enough to pick them out of other "
    "casual photos. Do not mention any name, do not guess exact ages, and do "
    "not describe clothing unless it is clearly their habitual style."
)

PERSON_DESCRIBE_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string",
            "description": "visual recognition profile, 2-6 sentences",
            "maxLength": 1500,
        },
    },
    "required": ["description"],
}


PERSON_MATCH_PROMPT_HEAD = (
    "Decide which of the following people appear in this photo. Respond with "
    "JSON only.\n"
    "People to look for (person_id with a recognition profile):\n"
)

PERSON_MATCH_PROMPT_TAIL = (
    "\nExamine every person in the photo. For each person listed above, add one "
    'entry to "matches": "person_id" (the integer id), "present" (true only if '
    'you are reasonably sure they appear), and "confidence" (0.0 to 1.0). Judge '
    "only what is actually visible; people not in the photo get present=false. "
    "Include every listed person_id exactly once."
)

PERSON_MATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "description": "one entry per listed person",
            "items": {
                "type": "object",
                "properties": {
                    "person_id": {"type": "integer"},
                    "present": {"type": "boolean"},
                    "confidence": {"type": "number"},
                },
                "required": ["person_id", "present", "confidence"],
            },
        }
    },
    "required": ["matches"],
}


def normalize_person_matches(raw: dict, valid_ids: set[int]) -> list[dict]:
    """Coerce a match response into known person ids with clamped confidence."""
    out: list[dict] = []
    seen: set[int] = set()
    items = raw.get("matches")
    if not isinstance(items, list):
        items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            person_id = int(item.get("person_id"))
        except (TypeError, ValueError):
            continue
        if person_id not in valid_ids or person_id in seen:
            continue
        present = item.get("present")
        if not isinstance(present, bool):
            present = bool(present)
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            confidence = 1.0 if present else 0.0
        out.append(
            {
                "person_id": person_id,
                "present": present,
                "confidence": min(1.0, max(0.0, confidence)),
            }
        )
        seen.add(person_id)
    return out


def merge_tile_results(results: list[dict]) -> dict:
    """Merge quadrant extractions (TL, TR, BL, BR order) into one result."""
    with_text = [
        r
        for r in results
        if r.get("has_text") and isinstance(r.get("text"), str) and r["text"].strip()
    ]
    context = " | ".join(
        c for c in (r.get("context") for r in results) if isinstance(c, str) and c.strip()
    )
    if not with_text:
        return {
            "has_text": False,
            "text": "",
            "context": context,
            "text_kind": "none",
            "language": with_text[0]["language"] if with_text else "unknown",
        }
    return {
        "has_text": True,
        "text": "\n\n".join(r["text"].strip() for r in with_text),
        "context": context,
        "text_kind": with_text[0]["text_kind"],
        "language": with_text[0]["language"],
        "category": with_text[0].get("category") or "other",
    }


def normalize_category(value) -> str:
    """Free-form category: lowercase, collapsed, capped; 'other' if empty."""
    category = " ".join(str(value or "").lower().split())[:40].strip()
    return category or "other"


def normalize_gate_result(raw: dict) -> dict:
    """Normalize a gate-tier result into the full result shape (no text)."""
    context = raw.get("context")
    if not isinstance(context, str):
        context = "" if context is None else str(context)
    context = re.sub(r"\n{3,}", "\n\n", context).strip("\n")
    return {
        "has_text": False,
        "text": "",
        "context": context,
        "text_kind": "none",
        "language": "unknown",
        "category": normalize_category(raw.get("category")),
    }


def normalize_result(raw: dict) -> dict:
    has_text = raw.get("has_text")
    if not isinstance(has_text, bool):
        has_text = bool(raw.get("text"))
    text = raw.get("text")
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    # Collapse pathological newline runs (repetition loops at temperature 0
    # can emit thousands of blank lines before the token cap stops them).
    text = re.sub(r"\n{3,}", "\n\n", text).strip("\n")
    context = raw.get("context")
    if not isinstance(context, str):
        context = "" if context is None else str(context)
    kind = str(raw.get("text_kind") or "").strip().lower()
    if kind not in VALID_TEXT_KINDS:
        kind = "other" if kind else "none"
    language = str(raw.get("language") or "").strip() or "unknown"
    return {
        "has_text": has_text,
        "text": text,
        "context": context,
        "text_kind": kind,
        "language": language,
        "category": normalize_category(raw.get("category")),
    }
