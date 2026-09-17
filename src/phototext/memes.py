"""Meme identification via perceptual hashing.

Every photo gets a 64-bit dHash (difference hash) at scan time: the image is
downscaled to 9x8 grayscale and each bit records whether a pixel is brighter
than its right neighbor. Visually near-identical photos (re-uploads, resized
memes) land within a small hamming distance; `find_clusters` groups them with
union-find and combines that with text presence to flag likely memes
(recurring image + overlaid text).
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path

from . import imaging  # noqa: F401  (registers the HEIF opener)
from PIL import Image

HAMMING_DEFAULT = 8


def dhash(path: Path) -> int | None:
    """63-bit dHash of an image, or None if it cannot be decoded.

    Kept within the signed 64-bit INTEGER range SQLite accepts.
    """
    try:
        with Image.open(path) as im:
            gray = im.convert("L").resize((9, 8))
        px = list(gray.getdata())
        bits = 0
        for row in range(8):
            base = row * 9
            for col in range(8):
                bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
        return bits & 0x7FFFFFFFFFFFFFFF
    except Exception:
        return None


def ensure_hashes(conn: sqlite3.Connection) -> int:
    """Compute phashes for photos missing one (readable file required).

    Returns how many were computed. Photos whose files are gone stay NULL.
    """
    rows = conn.execute(
        "SELECT p.id, (SELECT path FROM locations WHERE photo_id = p.id "
        "ORDER BY id LIMIT 1) AS path FROM photos p WHERE p.phash IS NULL"
    ).fetchall()
    computed = 0
    for row in rows:
        if not row["path"] or not Path(row["path"]).exists():
            continue
        value = dhash(Path(row["path"]))
        if value is None:
            continue
        conn.execute("UPDATE photos SET phash = ? WHERE id = ?", (value, row["id"]))
        computed += 1
    if computed:
        conn.commit()
    return computed


def find_clusters(
    conn: sqlite3.Connection,
    max_distance: int = HAMMING_DEFAULT,
    min_size: int = 2,
) -> list[list[sqlite3.Row]]:
    """Groups of near-identical photos (hamming distance <= max_distance).

    Rows carry id, phash, has_text, text_kind, text, status, and the first
    location path. Groups are returned largest first.
    """
    rows = conn.execute(
        "SELECT p.id, p.phash, p.has_text, p.text_kind, p.category, p.tiled, "
        "p.gated, p.derivative, p.status, p.error, p.text, p.language, p.model, "
        "(SELECT path FROM locations WHERE photo_id = p.id ORDER BY id LIMIT 1) AS path "
        "FROM photos p WHERE p.phash IS NOT NULL AND p.deleted_at IS NULL"
    ).fetchall()
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i in range(len(rows)):
        phash_i = rows[i]["phash"]
        for j in range(i + 1, len(rows)):
            if (phash_i ^ rows[j]["phash"]).bit_count() <= max_distance:
                union(i, j)
    groups: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for i in range(len(rows)):
        groups[find(i)].append(rows[i])
    return sorted(
        (g for g in groups.values() if len(g) >= min_size),
        key=len,
        reverse=True,
    )
