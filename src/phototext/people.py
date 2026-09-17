"""Person identification: seed crops, recognition profiles, matching pass.

A person is anchored by one or more *seed* photos: the user draws a box
around a face (web UI) or passes `--box` (CLI) and names the person. The
model then writes a recognition profile from the seed crops, and
`people run` evaluates every candidate photo against all registered people
in a single call per photo. Tags carry a confidence and an origin
('seed', 'user', or 'model'); model tags below `person_min_confidence`
stay in a review queue, and user/seed tags are ground truth the model
never overwrites.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

from . import db
from .config import Config, ensure_noindex
from .faces import MAX_FACES as PERSON_MAX_FACES
from .faces import detect_faces, vision_problem
from .imaging import ImageReadError, crop_jpeg, prepare_image
from .ollama_client import (
    ModelOutputError,
    OllamaClient,
    OllamaServerError,
    OllamaTimeout,
    OllamaUnreachable,
)
from .prompt import normalize_person_matches
from .worker import RunController, _wait_while_busy, parse_duration

PERSON_SEED_EDGE = 512
PERSON_MAX_SEEDS = 3
PERSON_MAX_NAME = 60


def person_dir(db_path: Path) -> Path:
    return Path(db_path).expanduser().parent / "people"


def seed_crop_path(db_path: Path, person_id: int, photo_id: int) -> Path:
    return person_dir(db_path) / str(person_id) / f"seed-{photo_id}.jpg"


def parse_box(text: str | None) -> tuple[int, int, int, int] | None:
    """'x,y,w,h' (original pixels) -> tuple, or None for the whole photo."""
    if text is None or not text.strip():
        return None
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 4:
        raise ValueError("box must be 'x,y,w,h' in pixels, e.g. 120,80,300,300")
    try:
        x, y, w, h = (int(p) for p in parts)
    except ValueError:
        raise ValueError("box must be 'x,y,w,h' with integer pixels") from None
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        raise ValueError("box x,y must be >= 0 and w,h > 0")
    return (x, y, w, h)


def save_seed_crop(
    db_path: Path, person_id: int, photo_id: int, source: Path, box=None
) -> Path | None:
    """Write the face crop for a seed. box=None stores a whole-photo crop."""
    try:
        if box is not None:
            data = crop_jpeg(source, box, max_edge=PERSON_SEED_EDGE)
        else:
            data = prepare_image(source, max_edge=PERSON_SEED_EDGE)
    except ImageReadError:
        return None
    target = seed_crop_path(db_path, person_id, photo_id)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        ensure_noindex(target.parent)
        target.write_bytes(data)
    except OSError:
        return None
    return target


def load_seed_crops(db_path: Path, conn: sqlite3.Connection, person_id: int) -> list[str]:
    """Base64 seed crops for a person, up to PERSON_MAX_SEEDS. Crops whose
    file is gone are re-cut from the original via the stored box."""
    crops: list[str] = []
    for photo_id in db.person_seed_photo_ids(conn, person_id, PERSON_MAX_SEEDS):
        target = seed_crop_path(db_path, person_id, photo_id)
        data: bytes | None = None
        if target.exists():
            try:
                data = target.read_bytes()
            except OSError:
                data = None
        if data is None:
            source = db.find_first_existing_location(conn, photo_id)
            if source:
                row = conn.execute(
                    "SELECT box FROM person_tags WHERE photo_id = ? AND person_id = ?",
                    (photo_id, person_id),
                ).fetchone()
                box = parse_box(row["box"]) if row and row["box"] else None
                try:
                    if box is not None:
                        data = crop_jpeg(Path(source), box, max_edge=PERSON_SEED_EDGE)
                    else:
                        data = prepare_image(Path(source), max_edge=PERSON_SEED_EDGE)
                except ImageReadError:
                    data = None
        if data:
            crops.append(base64.b64encode(data).decode("ascii"))
    return crops


def build_description(
    conn: sqlite3.Connection, db_path: Path, person_id: int, cfg: Config,
    client: OllamaClient | None = None,
) -> str:
    """(Re)build a person's recognition profile from their seed crops."""
    crops = load_seed_crops(db_path, conn, person_id)
    if not crops:
        raise ValueError(
            "no readable seed photos for this person — add one with "
            "`phototext people name <photo-id> <name>`"
        )
    if client is None:
        client = OllamaClient(cfg)
    raw, _content = client.describe_person(crops)
    description = " ".join(str(raw.get("description") or "").split())[:1500].strip()
    if not description:
        raise ValueError("model returned an empty description")
    db.set_person_description(conn, person_id, description)
    return description


def people_json_for_prompt(conn: sqlite3.Connection, person_ids: list[int]) -> str:
    rows = conn.execute(
        "SELECT id, name, description FROM people WHERE id IN "
        f"({','.join('?' for _ in person_ids)}) ORDER BY id",
        person_ids,
    ).fetchall()
    return json.dumps(
        [
            {
                "person_id": r["id"],
                "name": r["name"],
                "recognition_profile": r["description"] or "",
            }
            for r in rows
        ],
        indent=2,
    )


def run_matching(
    cfg: Config,
    model: str | None = None,
    person_names: list[str] | None = None,
    stop_after: str | None = None,
    limit: int | None = None,
) -> int:
    """Tag people across the library. One model call per candidate photo
    evaluates every registered person. Returns an exit code."""
    if stop_after is not None:
        parse_duration(stop_after)
    conn = db.connect(cfg.db_path)
    people = db.people_list(conn, cfg.person_min_confidence)
    if not people:
        print("no people yet — name one first: phototext people name <photo-id> <name>")
        return 1
    if person_names:
        by_name = {p["name"].lower(): p for p in people}
        selected = []
        for name in person_names:
            person = by_name.get(name.strip().lower())
            if person is None:
                known = ", ".join(p["name"] for p in people)
                print(f"error: no person named '{name}' (known: {known})", flush=True)
                return 2
            selected.append(person)
    else:
        selected = people
    person_cfg = cfg
    if model:
        person_cfg = replace(person_cfg, person_model=model)
    client = OllamaClient(person_cfg)
    # People without a recognition profile get one before the pass; a
    # person with no profile at all cannot be matched.
    for person in selected:
        if person["description"]:
            continue
        try:
            build_description(conn, cfg.db_path, person["id"], cfg, client)
        except (ValueError, ModelOutputError) as e:
            print(f"error: cannot build a profile for '{person['name']}': {e}")
            return 1
        print(f"profile built for '{person['name']}'", flush=True)
    person_ids = [p["id"] for p in selected]
    candidates = db.people_candidates(conn, person_ids)
    if not candidates:
        print("nothing to do: every visible photo already has tags for these people")
        return 0
    profiles = people_json_for_prompt(conn, person_ids)
    threshold = cfg.person_min_confidence
    if cfg.face_detection:
        face_problem = vision_problem()
        if face_problem is not None:
            # Without Vision every photo would look faceless and the whole
            # pass would mark everyone absent — fall back to whole-photo
            # matching instead of silently degrading.
            print(f"warning: {face_problem}; matching whole photos instead")
            cfg = replace(cfg, face_detection=False)
    face_note = "macOS Vision face detection on" if cfg.face_detection else ""
    print(
        f"Matching {len(candidates)} photo(s) against {len(selected)} person(s) "
        f"with model '{client.person_model}' (confidence threshold {threshold:.2f})"
        + (f", {face_note}" if face_note else "")
        + "...",
        flush=True,
    )
    controller = RunController()
    deadline = time.monotonic() + parse_duration(stop_after) if stop_after else None
    evaluated = 0
    tagged = 0
    uncertain = 0
    absent = 0
    errors = 0
    aborted = False
    for row in candidates:
        if controller.stop:
            print("Stopped by user. Progress is saved; run again to continue.")
            break
        if deadline is not None and time.monotonic() >= deadline:
            print("Stopped: time budget reached. Progress is saved; run again to continue.")
            break
        if limit is not None and evaluated >= limit:
            print(f"Stopped: --limit {limit} reached. Run again to continue.")
            break
        if cfg.idle_detection:
            try:
                if not _wait_while_busy(client, person_cfg, controller):
                    print("Stopped by user while paused. Progress is saved.")
                    break
            except Exception as e:
                print(f"aborted: {e}")
                aborted = True
                break
        photo_id = row["id"]
        path = db.find_first_existing_location(conn, photo_id)
        if path is None:
            errors += 1
            print(f"  [{evaluated + 1}/{len(candidates)}] photo {photo_id}: no readable file",
                  flush=True)
            continue
        face_boxes: list[tuple[int, int, int, int]] = []
        if cfg.face_detection:
            face_boxes = detect_faces(Path(path), photo_id)
            if not face_boxes:
                # No faces -> nobody in the selected set can appear. Record
                # absent for everyone without spending a model call; seed and
                # user rows are ground truth and stay untouched.
                for person in selected:
                    existing = db.person_tag_row(conn, photo_id, person["id"])
                    if existing is not None and existing["present"] and existing["origin"] != "model":
                        continue
                    db.tag_person(
                        conn, photo_id, person["id"], 0.0, "model", present=False
                    )
                    absent += 1
                evaluated += 1
                print(f"  [{evaluated}/{len(candidates)}] photo {photo_id}: no faces", flush=True)
                continue
        images_b64: list[str] = []
        try:
            if face_boxes:
                # Match on close-up face crops (largest first, bounded).
                for box in face_boxes[:PERSON_MAX_FACES]:
                    crop = crop_jpeg(
                        Path(path), box, max_edge=PERSON_SEED_EDGE,
                        max_pixels=cfg.max_image_pixels,
                    )
                    images_b64.append(base64.b64encode(crop).decode("ascii"))
            if not images_b64:
                image_bytes = prepare_image(
                    Path(path), cfg.max_image_edge, max_pixels=cfg.max_image_pixels
                )
                images_b64 = [base64.b64encode(image_bytes).decode("ascii")]
        except ImageReadError as e:
            errors += 1
            print(f"  [{evaluated + 1}/{len(candidates)}] photo {photo_id}: unreadable ({e})",
                  flush=True)
            continue
        try:
            raw, _content = client.match_people(images_b64, profiles)
        except (ModelOutputError, OllamaServerError, OllamaTimeout) as e:
            errors += 1
            print(f"  [{evaluated + 1}/{len(candidates)}] photo {photo_id}: {e}", flush=True)
            continue
        except OllamaUnreachable as e:
            print(f"aborted: Ollama at {client.base_url} unreachable ({e})")
            aborted = True
            break
        evaluated += 1
        matches = normalize_person_matches(raw, set(person_ids))
        hits: list[str] = []
        for match in matches:
            person = next(p for p in selected if p["id"] == match["person_id"])
            if match["present"]:
                db.tag_person(
                    conn, photo_id, match["person_id"], match["confidence"], "model"
                )
                tagged += 1
                hits.append(
                    f"{person['name']} {match['confidence']:.2f}"
                    + ("?" if match["confidence"] < threshold else "")
                )
                if match["confidence"] < threshold:
                    uncertain += 1
            else:
                # Absent is recorded (present=0) so later runs skip the photo;
                # it also retracts an earlier model tag (user/seed rows are
                # ground truth and stay).
                db.tag_person(
                    conn, photo_id, match["person_id"], match["confidence"], "model",
                    present=False,
                )
                absent += 1
        note = "; ".join(hits) if hits else "no matches"
        print(f"  [{evaluated}/{len(candidates)}] photo {photo_id}: {note}", flush=True)
    conn.commit()
    print(
        f"Done: {evaluated} photo(s) evaluated, {tagged} tag(s) added "
        f"({uncertain} below threshold), {absent} marked absent, {errors} error(s)."
    )
    people_now = db.people_list(conn, cfg.person_min_confidence)
    for person in people_now:
        if person["id"] in person_ids:
            print(
                f"  {person['name']}: {person['tags']} tag(s) — "
                f"{person['confirmed']} confirmed, {person['strong']} confident, "
                f"{person['uncertain']} to review"
            )
    print("Review uncertain tags: phototext serve --writable (or `phototext people photos <name>`)")
    return 1 if aborted else 0
