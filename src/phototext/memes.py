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
DUPLICATE_HAMMING_DEFAULT = 4


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


def _hamming_pairs(values: list[int], max_distance: int):
    """Yield (i, j) index pairs whose hamming distance <= max_distance.

    Exact like a naive O(n^2) scan, but via a BK-tree so large catalogs
    cluster in seconds instead of minutes. Duplicate values share one node
    (they are distance 0 from each other)."""
    if len(values) < 2:
        return
    tree: list[tuple[int, dict[int, int]]] = []
    node_rows: list[list[int]] = []
    by_value: dict[int, int] = {}
    for i, v in enumerate(values):
        node = by_value.get(v)
        if node is not None:
            node_rows[node].append(i)
            continue
        if not tree:
            tree.append((v, {}))
            node_rows.append([i])
            by_value[v] = 0
            continue
        node = 0
        while True:
            value, children = tree[node]
            d = (value ^ v).bit_count()
            child = children.get(d)
            if child is None:
                tree.append((v, {}))
                node_rows.append([i])
                children[d] = len(tree) - 1
                by_value[v] = len(tree) - 1
                break
            node = child
    for a in range(len(tree)):
        value_a, _ = tree[a]
        stack = [0]
        while stack:
            cur = stack.pop()
            value_c, children_c = tree[cur]
            d = (value_a ^ value_c).bit_count()
            if d <= max_distance:
                if a == cur:
                    # Duplicates share this node: pair them with each other.
                    rows_a = node_rows[a]
                    for x in range(len(rows_a)):
                        for y in range(x + 1, len(rows_a)):
                            yield rows_a[x], rows_a[y]
                else:
                    for i in node_rows[a]:
                        for j in node_rows[cur]:
                            yield i, j
            # Triangle inequality: only children at edge distance k with
            # |k - d| <= max_distance can hold a match for value_a.
            for k, child in children_c.items():
                if d - max_distance <= k <= d + max_distance:
                    stack.append(child)


def find_clusters(
    conn: sqlite3.Connection,
    max_distance: int = HAMMING_DEFAULT,
    min_size: int = 2,
    exclude_derivatives: bool = False,
) -> list[list[sqlite3.Row]]:
    """Groups of near-identical photos (hamming distance <= max_distance).

    Rows carry id, phash, has_text, text_kind, status, and the first
    location path. Groups are returned largest first. With
    exclude_derivatives, iCloud preview proxies are skipped so a local
    original never pairs with its own cloud preview (used by the
    duplicates view, not the meme view)."""
    where = "p.phash IS NOT NULL AND p.deleted_at IS NULL"
    if exclude_derivatives:
        where += " AND p.derivative = 0"
    rows = conn.execute(
        "SELECT p.id, p.phash, p.has_text, p.text_kind, p.category, p.tiled, "
        "p.gated, p.derivative, p.status, p.error, p.text, p.language, p.model, "
        "p.byte_size, p.date_taken, p.hidden, "
        "(SELECT path FROM locations WHERE photo_id = p.id ORDER BY id LIMIT 1) AS path "
        f"FROM photos p WHERE {where}"
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

    for i, j in _hamming_pairs([r["phash"] for r in rows], max_distance):
        union(i, j)
    groups: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for i in range(len(rows)):
        groups[find(i)].append(rows[i])
    return sorted(
        (g for g in groups.values() if len(g) >= min_size),
        key=len,
        reverse=True,
    )
