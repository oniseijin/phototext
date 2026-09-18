from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import ensure_noindex
from .library_meta import norm_path

SCHEMA_VERSION = 8

MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE sources (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        uri TEXT NOT NULL UNIQUE,
        added_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE photos (
        id INTEGER PRIMARY KEY,
        sha256 TEXT NOT NULL UNIQUE,
        byte_size INTEGER NOT NULL,
        first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
        status TEXT NOT NULL DEFAULT 'queued',
        attempts INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        started_at TEXT,
        finished_at TEXT,
        duration_ms INTEGER,
        has_text INTEGER,
        text TEXT,
        context TEXT,
        text_kind TEXT,
        language TEXT,
        model TEXT,
        raw_response TEXT
    );

    CREATE TABLE locations (
        id INTEGER PRIMARY KEY,
        photo_id INTEGER NOT NULL REFERENCES photos(id),
        source_id INTEGER NOT NULL REFERENCES sources(id),
        path TEXT NOT NULL,
        mtime INTEGER,
        size INTEGER,
        UNIQUE (source_id, path)
    );

    CREATE INDEX idx_photos_status ON photos(status);
    CREATE INDEX idx_locations_photo ON locations(photo_id);
    """,
    2: """
    CREATE VIRTUAL TABLE photos_fts USING fts5(
        text, context,
        content='photos', content_rowid='id'
    );

    CREATE TRIGGER photos_fts_ai AFTER INSERT ON photos BEGIN
        INSERT INTO photos_fts(rowid, text, context) VALUES (new.id, new.text, new.context);
    END;

    CREATE TRIGGER photos_fts_ad AFTER DELETE ON photos BEGIN
        INSERT INTO photos_fts(photos_fts, rowid, text, context)
        VALUES ('delete', old.id, old.text, old.context);
    END;

    CREATE TRIGGER photos_fts_au AFTER UPDATE ON photos
    WHEN new.text IS NOT old.text OR new.context IS NOT old.context
    BEGIN
        INSERT INTO photos_fts(photos_fts, rowid, text, context)
        VALUES ('delete', old.id, old.text, old.context);
        INSERT INTO photos_fts(rowid, text, context) VALUES (new.id, new.text, new.context);
    END;

    INSERT INTO photos_fts(photos_fts) VALUES ('rebuild');
    """,
    3: """
    ALTER TABLE photos ADD COLUMN tiled INTEGER NOT NULL DEFAULT 0;
    """,
    4: """
    ALTER TABLE photos ADD COLUMN phash INTEGER;
    """,
    5: """
    ALTER TABLE photos ADD COLUMN gated INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE photos ADD COLUMN category TEXT;
    """,
    6: """
    ALTER TABLE photos ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE photos ADD COLUMN deleted_at TEXT;
    """,
    7: """
    CREATE TABLE photo_warnings (
        id INTEGER PRIMARY KEY,
        photo_id INTEGER NOT NULL REFERENCES photos(id),
        kind TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (photo_id, kind, message)
    );
    """,
    8: """
    ALTER TABLE photos ADD COLUMN derivative INTEGER NOT NULL DEFAULT 0;
    """,
    9: """
    CREATE TABLE people (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        description TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT
    );

    CREATE TABLE person_tags (
        photo_id INTEGER NOT NULL REFERENCES photos(id),
        person_id INTEGER NOT NULL REFERENCES people(id),
        present INTEGER NOT NULL DEFAULT 1,
        confidence REAL NOT NULL DEFAULT 1.0,
        origin TEXT NOT NULL DEFAULT 'model',
        box TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT,
        PRIMARY KEY (photo_id, person_id)
    );

    CREATE INDEX idx_person_tags_person ON person_tags(person_id);
    """,
    10: """
    ALTER TABLE photos ADD COLUMN date_taken TEXT;
    ALTER TABLE person_tags ADD COLUMN seed INTEGER NOT NULL DEFAULT 0;
    UPDATE person_tags SET seed = 1 WHERE origin = 'seed';
    """,
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str) -> sqlite3.Connection:
    db_path = Path(db_path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ensure_noindex(db_path.parent)
    preexisting = db_path.exists() and db_path.stat().st_size > 0
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    _migrate(conn, db_path, preexisting)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version").fetchone()
    return row["v"]


def pending_migrations(conn: sqlite3.Connection) -> list[int]:
    current = schema_version(conn)
    return [v for v in sorted(MIGRATIONS) if v > current]


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations; returns the versions applied."""
    applied: list[int] = []
    for version in pending_migrations(conn):
        conn.executescript(MIGRATIONS[version])
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        applied.append(version)
    if applied:
        conn.commit()
    return applied


def backup_catalog(db_path: Path | str) -> Path | None:
    """Consistent backup copy of the catalog into <db_dir>/backups/.

    Returns the backup path, or None if there is nothing to back up.
    Raises on failure (better to stop than to migrate unprotected).
    """
    db_path = Path(db_path).expanduser()
    try:
        if not db_path.exists() or db_path.stat().st_size == 0:
            return None
    except OSError:
        return None
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ensure_noindex(backup_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"{db_path.name}.{stamp}.bak"
    try:
        src = sqlite3.connect(str(db_path))
        try:
            dst = sqlite3.connect(str(target))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except Exception:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target


def _migrate(
    conn: sqlite3.Connection, db_path: Path, preexisting: bool
) -> None:
    if pending_migrations(conn):
        if preexisting:
            backup_catalog(db_path)
        migrate(conn)


def upsert_source(conn: sqlite3.Connection, uri: str, kind: str) -> int:
    conn.execute(
        "INSERT INTO sources (kind, uri) VALUES (?, ?) ON CONFLICT(uri) DO NOTHING",
        (kind, uri),
    )
    row = conn.execute("SELECT id FROM sources WHERE uri = ?", (uri,)).fetchone()
    return row["id"]


def get_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM sources ORDER BY id").fetchall()


def remove_source(conn: sqlite3.Connection, source_id: int) -> dict:
    """Unregister a source and forget photos that were only seen there.

    Photos whose content also exists in other sources keep their remaining
    locations. Returns counts for reporting.
    """
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise ValueError(f"no source with id {source_id} (see `phototext status`)")
    locations = conn.execute(
        "SELECT COUNT(*) AS n FROM locations WHERE source_id = ?", (source_id,)
    ).fetchone()["n"]
    conn.execute("DELETE FROM locations WHERE source_id = ?", (source_id,))
    if _table_exists(conn, "photo_warnings"):
        conn.execute(
            "DELETE FROM photo_warnings WHERE photo_id IN "
            "(SELECT p.id FROM photos p WHERE NOT EXISTS "
            "(SELECT 1 FROM locations l WHERE l.photo_id = p.id))"
        )
    if _table_exists(conn, "person_tags"):
        conn.execute(
            "DELETE FROM person_tags WHERE photo_id IN "
            "(SELECT p.id FROM photos p WHERE NOT EXISTS "
            "(SELECT 1 FROM locations l WHERE l.photo_id = p.id))"
        )
    forgotten = conn.execute(
        "DELETE FROM photos WHERE NOT EXISTS "
        "(SELECT 1 FROM locations l WHERE l.photo_id = photos.id)"
    ).rowcount
    conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    conn.commit()
    return {
        "uri": row["uri"],
        "kind": row["kind"],
        "locations": locations,
        "forgotten": max(0, forgotten),
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def upsert_photo(
    conn: sqlite3.Connection,
    sha256: str,
    byte_size: int,
    phash: int | None = None,
    date_taken: str | None = None,
) -> tuple[int, bool]:
    cur = conn.execute(
        "INSERT INTO photos (sha256, byte_size, phash, date_taken) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(sha256) DO NOTHING",
        (sha256, byte_size, phash, date_taken),
    )
    created = cur.rowcount == 1
    row = conn.execute("SELECT id FROM photos WHERE sha256 = ?", (sha256,)).fetchone()
    return row["id"], created


def upsert_location(
    conn: sqlite3.Connection,
    photo_id: int,
    source_id: int,
    path: str,
    mtime: int,
    size: int,
) -> None:
    conn.execute(
        """
        INSERT INTO locations (photo_id, source_id, path, mtime, size)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_id, path) DO UPDATE SET
            photo_id = excluded.photo_id,
            mtime = excluded.mtime,
            size = excluded.size
        """,
        (photo_id, source_id, path, mtime, size),
    )


def get_location(conn: sqlite3.Connection, source_id: int, path: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM locations WHERE source_id = ? AND path = ?", (source_id, path)
    ).fetchone()


def claim_next(conn: sqlite3.Connection) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM photos WHERE status = 'queued' AND deleted_at IS NULL "
        "ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    cur = conn.execute(
        "UPDATE photos SET status = 'processing', started_at = ? "
        "WHERE id = ? AND status = 'queued'",
        (now_utc(), row["id"]),
    )
    if cur.rowcount != 1:
        return None
    conn.commit()
    return conn.execute("SELECT * FROM photos WHERE id = ?", (row["id"],)).fetchone()


def mark_done(
    conn: sqlite3.Connection,
    photo_id: int,
    result: dict,
    model: str,
    raw_response: str | None,
    duration_ms: int,
    tiled: bool = False,
    gated: bool = False,
) -> None:
    conn.execute(
        """
        UPDATE photos SET
            status = 'done', has_text = ?, text = ?, context = ?, text_kind = ?, language = ?,
            model = ?, raw_response = ?, duration_ms = ?, finished_at = ?, error = NULL,
            tiled = ?, gated = ?, category = ?
        WHERE id = ?
        """,
        (
            int(result["has_text"]),
            result["text"],
            result["context"],
            result["text_kind"],
            result["language"],
            model,
            raw_response,
            duration_ms,
            now_utc(),
            int(tiled),
            int(gated),
            result.get("category"),
            photo_id,
        ),
    )
    conn.commit()


def mark_error(
    conn: sqlite3.Connection,
    photo_id: int,
    message: str,
    raw_response: str | None = None,
) -> None:
    conn.execute(
        "UPDATE photos SET status = 'error', error = ?, attempts = attempts + 1, "
        "finished_at = ?, raw_response = COALESCE(?, raw_response) WHERE id = ?",
        (message, now_utc(), raw_response, photo_id),
    )
    conn.commit()


def requeue(conn: sqlite3.Connection, photo_id: int) -> None:
    conn.execute(
        "UPDATE photos SET status = 'queued', started_at = NULL, finished_at = NULL WHERE id = ?",
        (photo_id,),
    )
    conn.commit()


def requeue_all_errors(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "UPDATE photos SET status = 'queued', attempts = 0, error = NULL, "
        "started_at = NULL, finished_at = NULL "
        "WHERE status = 'error' AND deleted_at IS NULL"
    )
    conn.commit()
    return cur.rowcount


def hide_photos(
    conn: sqlite3.Connection, photo_ids: list[int], hidden: bool = True
) -> int:
    if not photo_ids:
        return 0
    marks = ",".join("?" for _ in photo_ids)
    cur = conn.execute(
        f"UPDATE photos SET hidden = ? WHERE id IN ({marks}) AND deleted_at IS NULL",
        [int(hidden)] + photo_ids,
    )
    conn.commit()
    return cur.rowcount


def trash_photos(conn: sqlite3.Connection, photo_ids: list[int]) -> int:
    """Move photos to the trash (catalog tombstone; files are never touched).

    The row is kept so rescans remember the deletion via the content hash.
    """
    if not photo_ids:
        return 0
    marks = ",".join("?" for _ in photo_ids)
    cur = conn.execute(
        f"UPDATE photos SET deleted_at = ?, hidden = 0 "
        f"WHERE id IN ({marks}) AND deleted_at IS NULL",
        [now_utc()] + photo_ids,
    )
    conn.commit()
    return cur.rowcount


def restore_photos(conn: sqlite3.Connection, photo_ids: list[int]) -> int:
    if not photo_ids:
        return 0
    marks = ",".join("?" for _ in photo_ids)
    cur = conn.execute(
        f"UPDATE photos SET deleted_at = NULL WHERE id IN ({marks})",
        photo_ids,
    )
    conn.commit()
    return cur.rowcount


def purge_photos(conn: sqlite3.Connection, photo_ids: list[int]) -> int:
    """Forget photos permanently: rows, locations, and search entries.

    A later rescan of the same files will register them as new photos.
    """
    if not photo_ids:
        return 0
    marks = ",".join("?" for _ in photo_ids)
    if _table_exists(conn, "photo_warnings"):
        conn.execute(
            f"DELETE FROM photo_warnings WHERE photo_id IN ({marks})", photo_ids
        )
    if _table_exists(conn, "person_tags"):
        conn.execute(f"DELETE FROM person_tags WHERE photo_id IN ({marks})", photo_ids)
    conn.execute(f"DELETE FROM locations WHERE photo_id IN ({marks})", photo_ids)
    cur = conn.execute(f"DELETE FROM photos WHERE id IN ({marks})", photo_ids)
    conn.commit()
    return cur.rowcount


def trash_list(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.*, (SELECT path FROM locations WHERE photo_id = p.id "
        "ORDER BY id LIMIT 1) AS path "
        "FROM photos p WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC"
    ).fetchall()


def record_warning(
    conn: sqlite3.Connection, photo_id: int, kind: str, message: str
) -> None:
    """Persist a processing warning for a photo (deduped per photo+kind+message)."""
    conn.execute(
        "INSERT OR IGNORE INTO photo_warnings (photo_id, kind, message) "
        "VALUES (?, ?, ?)",
        (photo_id, kind, message[:300]),
    )


def photo_warnings_list(
    conn: sqlite3.Connection, photo_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT kind, message, created_at FROM photo_warnings WHERE photo_id = ? "
        "ORDER BY id",
        (photo_id,),
    ).fetchall()


def warnings_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM photo_warnings").fetchone()["n"]


DEFERRED_PREFIX = "deferred:"


def deferred_key(uuid: str) -> str:
    return DEFERRED_PREFIX + uuid


def ensure_deferred_photo(
    conn: sqlite3.Connection,
    source_id: int,
    uuid: str,
    path: str | None,
    derivative: bool,
) -> None:
    """Register an iCloud-only photo so the inventory is complete.

    Identity is 'deferred:<uuid>' until real content exists locally. Photos
    with a preview derivative are queued for best-effort extraction
    (flagged `derivative`); the rest wait as status='deferred'.
    """
    key = deferred_key(uuid)
    row = conn.execute("SELECT id FROM photos WHERE sha256 = ?", (key,)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO photos (sha256, byte_size, status, derivative) "
            "VALUES (?, 0, ?, ?)",
            (key, "queued" if derivative else "deferred", int(derivative)),
        )
        photo_id = cur.lastrowid
    else:
        photo_id = row["id"]
    if path:
        conn.execute(
            "INSERT INTO locations (photo_id, source_id, path, mtime, size) "
            "VALUES (?, ?, ?, 0, 0) "
            "ON CONFLICT(source_id, path) DO UPDATE SET photo_id = excluded.photo_id",
            (photo_id, source_id, path),
        )


def promote_deferred(
    conn: sqlite3.Connection,
    uuid: str,
    sha256: str,
    byte_size: int,
    source_id: int,
    path: str,
    mtime: int,
    requeue: bool = True,
) -> int:
    """A deferred photo's original appeared locally: give it a real content
    identity and (re)queue it. Returns the surviving photo id."""
    key = deferred_key(uuid)
    deferred = conn.execute("SELECT id FROM photos WHERE sha256 = ?", (key,)).fetchone()
    existing = conn.execute("SELECT id FROM photos WHERE sha256 = ?", (sha256,)).fetchone()
    if existing is not None:
        if deferred is not None:
            if _table_exists(conn, "photo_warnings"):
                conn.execute(
                    "DELETE FROM photo_warnings WHERE photo_id = ?", (deferred[0],)
                )
            conn.execute(
                "DELETE FROM locations WHERE photo_id = ?", (deferred[0],)
            )
            conn.execute("DELETE FROM photos WHERE id = ?", (deferred[0],))
        photo_id = existing[0]
    elif deferred is not None:
        photo_id = deferred[0]
        conn.execute(
            "UPDATE photos SET sha256 = ?, byte_size = ?, derivative = 0, "
            "attempts = 0, error = NULL, started_at = NULL, finished_at = NULL, "
            "status = ? WHERE id = ?",
            (sha256, byte_size, "queued" if requeue else "done", photo_id),
        )
    else:
        cur = conn.execute(
            "INSERT INTO photos (sha256, byte_size) VALUES (?, ?) "
            "ON CONFLICT(sha256) DO NOTHING",
            (sha256, byte_size),
        )
        row = conn.execute(
            "SELECT id FROM photos WHERE sha256 = ?", (sha256,)
        ).fetchone()
        photo_id = row["id"]
    conn.execute(
        "INSERT INTO locations (photo_id, source_id, path, mtime, size) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(source_id, path) DO UPDATE SET "
        "photo_id = excluded.photo_id, mtime = excluded.mtime, size = excluded.size",
        (photo_id, source_id, path, mtime, byte_size),
    )
    return photo_id


def select_photo_ids(
    conn: sqlite3.Connection,
    *,
    errors: bool = False,
    no_text: bool = False,
    tiled: bool = False,
    gated: bool = False,
    derivative: bool = False,
    all_photos: bool = False,
) -> list[int]:
    """Photo-id selections for `reprocess`. Exactly one selector should be set."""
    visible = "deleted_at IS NULL AND hidden = 0"
    if errors:
        sql, params = (
            f"SELECT id FROM photos WHERE status = 'error' AND {visible} ORDER BY id",
            [],
        )
    elif no_text:
        sql, params = (
            f"SELECT id FROM photos WHERE status = 'done' AND has_text = 0 "
            f"AND {visible} ORDER BY id",
            [],
        )
    elif tiled:
        sql, params = (
            f"SELECT id FROM photos WHERE status = 'done' AND tiled = 1 "
            f"AND {visible} ORDER BY id",
            [],
        )
    elif gated:
        sql, params = (
            f"SELECT id FROM photos WHERE status = 'done' AND gated = 1 "
            f"AND {visible} ORDER BY id",
            [],
        )
    elif derivative:
        sql, params = (
            f"SELECT id FROM photos WHERE status = 'done' AND derivative = 1 "
            f"AND {visible} ORDER BY id",
            [],
        )
    elif all_photos:
        sql, params = (
            f"SELECT id FROM photos WHERE {visible} AND status != 'deferred' ORDER BY id",
            [],
        )
    else:
        return []
    return [r[0] for r in conn.execute(sql, params)]


def photo_ids_for_paths(conn: sqlite3.Connection, paths: set[str]) -> list[int]:
    """Photo ids whose locations match any of the given normalized paths."""
    out: set[int] = set()
    for row in conn.execute("SELECT DISTINCT photo_id, path FROM locations"):
        if row[1] and norm_path(row[1]) in paths:
            out.add(row[0])
    return sorted(out)


def requeue_photos(conn: sqlite3.Connection, photo_ids: list[int]) -> int:
    if not photo_ids:
        return 0
    marks = ",".join("?" for _ in photo_ids)
    cur = conn.execute(
        f"UPDATE photos SET status = 'queued', attempts = 0, error = NULL, "
        f"started_at = NULL, finished_at = NULL WHERE id IN ({marks})",
        photo_ids,
    )
    conn.commit()
    return cur.rowcount


def reset_processing(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "UPDATE photos SET status = 'queued', started_at = NULL "
        "WHERE status = 'processing' AND deleted_at IS NULL"
    )
    conn.commit()
    return cur.rowcount


def reclaim_stale(conn: sqlite3.Connection, lease_timeout_s: int = 3600) -> int:
    """Requeue 'processing' rows whose worker went silent (stale lease).

    The multi-worker equivalent of startup recovery: a crashed worker's photos
    become claimable again once the lease expires, without stomping on rows
    belonging to healthy workers.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=lease_timeout_s)
    ).isoformat(timespec="seconds")
    cur = conn.execute(
        "UPDATE photos SET status = 'queued', attempts = 0, error = NULL, "
        "started_at = NULL, finished_at = NULL "
        "WHERE status = 'processing' AND started_at IS NOT NULL AND started_at <= ? "
        "AND deleted_at IS NULL",
        (cutoff,),
    )
    conn.commit()
    return cur.rowcount


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {
        r["status"]: r["n"]
        for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM photos "
            "WHERE deleted_at IS NULL GROUP BY status"
        )
    }
    counts["total"] = sum(v for k, v in counts.items() if k != "total")
    return counts


def find_first_existing_location(conn: sqlite3.Connection, photo_id: int) -> str | None:
    for row in conn.execute(
        "SELECT path FROM locations WHERE photo_id = ? ORDER BY id", (photo_id,)
    ):
        if os.path.exists(row["path"]):
            return row["path"]
    return None


def photos_missing_date_taken(conn: sqlite3.Connection) -> list[int]:
    """Photo ids (not tombstoned) without a date_taken, oldest rows first."""
    return [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM photos WHERE date_taken IS NULL AND deleted_at IS NULL "
            "ORDER BY id"
        )
    ]


def set_date_taken(
    conn: sqlite3.Connection, photo_id: int, value: str
) -> bool:
    """Record date_taken if the photo does not have one yet."""
    cur = conn.execute(
        "UPDATE photos SET date_taken = ? WHERE id = ? AND date_taken IS NULL",
        (value, photo_id),
    )
    return cur.rowcount == 1


def date_taken_histogram(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """(year, n) rows for visible photos with a known date, newest year first."""
    return conn.execute(
        "SELECT strftime('%Y', date_taken) AS year, COUNT(*) AS n "
        "FROM photos WHERE date_taken IS NOT NULL AND deleted_at IS NULL AND hidden = 0 "
        "GROUP BY year ORDER BY year DESC"
    ).fetchall()


def recent_results(
    conn: sqlite3.Connection,
    n: int = 20,
    status: str | None = "done",
    category: str | None = None,
    show_hidden: bool = False,
) -> list[sqlite3.Row]:
    sql = (
        "SELECT p.*, (SELECT path FROM locations WHERE photo_id = p.id ORDER BY id LIMIT 1) AS path "
        "FROM photos p"
    )
    params: list = []
    where = ["p.deleted_at IS NULL"]
    if status and status != "all":
        where.append("p.status = ?")
        params.append(status)
    if category:
        where.append("p.category = ?")
        params.append(category)
    if not show_hidden:
        where.append("p.hidden = 0")
    sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.finished_at IS NOT NULL, p.finished_at DESC LIMIT ?"
    params.append(n)
    return conn.execute(sql, params).fetchall()


def avg_recent_duration_ms(conn: sqlite3.Connection, n: int = 50) -> float | None:
    row = conn.execute(
        "SELECT AVG(duration_ms) AS a FROM (SELECT duration_ms FROM photos "
        "WHERE status = 'done' AND duration_ms IS NOT NULL "
        "ORDER BY finished_at DESC LIMIT ?)",
        (n,),
    ).fetchone()
    return row["a"]


def last_finished(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT MAX(finished_at) AS m FROM photos WHERE finished_at IS NOT NULL"
    ).fetchone()
    return row["m"]


def _date_filter_parts(
    date_from: str | None, date_to: str | None, year: str | None
) -> tuple[list[str], list]:
    """SQL fragments + params filtering photos.date_taken, in a fixed order.

    Date filters only match photos that have a date_taken; NULLs fall out.
    """
    frags: list[str] = []
    params: list = []
    if date_from:
        frags.append("p.date_taken >= ?")
        params.append(date_from)
    if date_to:
        frags.append("p.date_taken <= ?")
        params.append(date_to)
    if year:
        frags.append("strftime('%Y', p.date_taken) = ?")
        params.append(year)
    return frags, params


def search_photos(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 50,
    offset: int = 0,
    highlight: tuple[str, str] = ("", ""),
    category: str | None = None,
    include_hidden: bool = False,
    person: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    year: str | None = None,
) -> tuple[list[sqlite3.Row], str]:
    """Full-text search over text + context, optionally within a category.

    Returns (rows, effective_query). If `query` is not usable as FTS5 syntax
    (syntax errors, column filters like `foo:bar` on unknown columns), retries
    once with the whole input quoted as a single phrase. Raises the original
    sqlite3.OperationalError if even the phrase form cannot be matched.
    """
    sql = (
        "SELECT p.id, p.status, p.has_text, p.text_kind, p.language, p.model, p.tiled, "
        "p.gated, p.category, p.derivative, p.hidden, "
        "length(p.text) AS text_len, "
        "snippet(photos_fts, 0, ?, ?, ' ... ', 10) AS text_snip, "
        "snippet(photos_fts, 1, ?, ?, ' ... ', 6) AS context_snip, "
        "bm25(photos_fts) AS rank, "
        "(SELECT path FROM locations WHERE photo_id = p.id ORDER BY id LIMIT 1) AS path "
        "FROM photos_fts JOIN photos p ON p.id = photos_fts.rowid "
        "WHERE photos_fts MATCH ? AND p.deleted_at IS NULL"
    )
    if not include_hidden:
        sql += " AND p.hidden = 0"
    if category:
        sql += " AND p.category = ?"
    if person:
        sql += (
            " AND p.id IN (SELECT t.photo_id FROM person_tags t "
            "JOIN people pe ON pe.id = t.person_id "
            "WHERE pe.name = ? COLLATE NOCASE AND t.present = 1)"
        )
    date_frags, date_params = _date_filter_parts(date_from, date_to, year)
    if date_frags:
        sql += " AND " + " AND ".join(date_frags)
    sql += " ORDER BY rank LIMIT ? OFFSET ?"
    open_m, close_m = highlight

    def run_match(q: str) -> list[sqlite3.Row]:
        params = [open_m, close_m, open_m, close_m, q]
        if category:
            params.append(category)
        if person:
            params.append(person)
        params += date_params
        params += [limit, offset]
        return conn.execute(sql, params).fetchall()

    try:
        return run_match(query), query
    except sqlite3.OperationalError as first_error:
        phrase = '"' + query.replace('"', '""') + '"'
        try:
            return run_match(phrase), phrase
        except sqlite3.OperationalError:
            raise first_error from None


def search_count(
    conn: sqlite3.Connection, query: str, include_hidden: bool = False,
    person: str | None = None,
    date_from: str | None = None, date_to: str | None = None, year: str | None = None,
) -> int:
    """Number of photos matching an FTS5 query (visible ones only)."""
    sql = (
        "SELECT COUNT(*) AS n FROM photos_fts JOIN photos p ON p.id = photos_fts.rowid "
        "WHERE photos_fts MATCH ? AND p.deleted_at IS NULL"
    )
    if not include_hidden:
        sql += " AND p.hidden = 0"
    if person:
        sql += (
            " AND p.id IN (SELECT t.photo_id FROM person_tags t "
            "JOIN people pe ON pe.id = t.person_id "
            "WHERE pe.name = ? COLLATE NOCASE AND t.present = 1)"
        )
    date_frags, date_params = _date_filter_parts(date_from, date_to, year)
    if date_frags:
        sql += " AND " + " AND ".join(date_frags)

    def run(q: str) -> int:
        params = [q]
        if person:
            params.append(person)
        params += date_params
        return conn.execute(sql, params).fetchone()["n"]

    try:
        return run(query)
    except sqlite3.OperationalError as first_error:
        phrase = '"' + query.replace('"', '""') + '"'
        try:
            return run(phrase)
        except sqlite3.OperationalError:
            raise first_error from None


def page_photos(
    conn: sqlite3.Connection,
    status: str | None = "done",
    has_text: bool | None = None,
    category: str | None = None,
    limit: int = 50,
    offset: int = 0,
    show_hidden: bool = False,
    hidden_only: bool = False,
    person: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    year: str | None = None,
) -> tuple[list[sqlite3.Row], int]:
    """Browse photos (no query), most recently finished first. Returns
    (rows, total matching the filter)."""
    where = ["p.deleted_at IS NULL"]
    params: list = []
    if hidden_only:
        where.append("p.hidden = 1")
    elif not show_hidden:
        where.append("p.hidden = 0")
    if status and status != "all":
        where.append("p.status = ?")
        params.append(status)
    if has_text is not None:
        where.append("p.has_text = ?")
        params.append(int(has_text))
    if category:
        where.append("p.category = ?")
        params.append(category)
    if person:
        where.append(
            "p.id IN (SELECT t.photo_id FROM person_tags t "
            "JOIN people pe ON pe.id = t.person_id "
            "WHERE pe.name = ? COLLATE NOCASE AND t.present = 1)"
        )
        params.append(person)
    date_frags, date_params = _date_filter_parts(date_from, date_to, year)
    for frag in date_frags:
        where.append(frag)
    params += date_params
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM photos p {where_sql}", params
    ).fetchone()["n"]
    rows = conn.execute(
        "SELECT p.*, (SELECT path FROM locations WHERE photo_id = p.id "
        "ORDER BY id LIMIT 1) AS path "
        f"FROM photos p {where_sql} "
        "ORDER BY (p.finished_at IS NULL), p.finished_at DESC, p.id DESC "
        "LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return rows, total


def get_photo(conn: sqlite3.Connection, photo_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()


def photo_locations(conn: sqlite3.Connection, photo_id: int) -> list[sqlite3.Row]:
    """Every on-disk location of a photo, with the source it was found in."""
    return conn.execute(
        "SELECT l.*, s.kind AS source_kind, s.uri AS source_uri "
        "FROM locations l JOIN sources s ON s.id = l.source_id "
        "WHERE l.photo_id = ? ORDER BY l.id",
        (photo_id,),
    ).fetchall()


def photo_location_by_id(
    conn: sqlite3.Connection, photo_id: int, location_id: int
) -> sqlite3.Row | None:
    """One location row, only if it belongs to the given photo."""
    return conn.execute(
        "SELECT l.*, s.kind AS source_kind, s.uri AS source_uri "
        "FROM locations l JOIN sources s ON s.id = l.source_id "
        "WHERE l.photo_id = ? AND l.id = ?",
        (photo_id, location_id),
    ).fetchone()


# --- people -----------------------------------------------------------------


def upsert_person(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    """Create a person by name, or return the existing row."""
    name = " ".join(name.split())
    conn.execute(
        "INSERT INTO people (name) VALUES (?) ON CONFLICT(name) DO NOTHING", (name,)
    )
    row = conn.execute("SELECT * FROM people WHERE name = ?", (name,)).fetchone()
    conn.commit()
    return row


def get_person_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM people WHERE name = ? COLLATE NOCASE", (name.strip(),)
    ).fetchone()


def get_person(conn: sqlite3.Connection, person_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()


def set_person_description(
    conn: sqlite3.Connection, person_id: int, description: str
) -> None:
    conn.execute(
        "UPDATE people SET description = ?, updated_at = ? WHERE id = ?",
        (description, now_utc(), person_id),
    )
    conn.commit()


def rename_person(conn: sqlite3.Connection, person_id: int, new_name: str) -> None:
    new_name = " ".join(new_name.split())
    conn.execute(
        "UPDATE people SET name = ?, updated_at = ? WHERE id = ?",
        (new_name, now_utc(), person_id),
    )
    conn.commit()


def tag_person(
    conn: sqlite3.Connection,
    photo_id: int,
    person_id: int,
    confidence: float,
    origin: str,
    box: str | None = None,
    present: bool = True,
    seed: bool = False,
) -> None:
    """Upsert a person tag. User and seed rows are ground truth: a later
    model tag never overwrites them. present=False records that the model
    judged the person absent (re-runs skip the photo). seed=1 marks the tag
    as an anchor for the recognition profile."""
    conn.execute(
        """
        INSERT INTO person_tags (photo_id, person_id, present, confidence, origin, box, seed)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(photo_id, person_id) DO UPDATE SET
            confidence = excluded.confidence,
            origin = excluded.origin,
            present = excluded.present,
            box = COALESCE(excluded.box, person_tags.box),
            seed = MAX(person_tags.seed, excluded.seed),
            updated_at = excluded.created_at
        WHERE excluded.origin != 'model' OR person_tags.origin NOT IN ('user', 'seed')
        """,
        (photo_id, person_id, int(present), confidence, origin, box, int(seed)),
    )
    conn.commit()


def confirm_person_tag(
    conn: sqlite3.Connection,
    photo_id: int,
    person_id: int,
    add_seed: bool = False,
) -> None:
    """Promote a tag to user-verified ground truth, optionally marking it
    as a profile seed."""
    conn.execute(
        "UPDATE person_tags SET confidence = 1.0, origin = 'user', present = 1, "
        "seed = MAX(seed, ?), "
        "updated_at = ? WHERE photo_id = ? AND person_id = ?",
        (int(add_seed), now_utc(), photo_id, person_id),
    )
    conn.commit()


def person_tag_row(
    conn: sqlite3.Connection, photo_id: int, person_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM person_tags WHERE photo_id = ? AND person_id = ?",
        (photo_id, person_id),
    ).fetchone()


def untag_person(conn: sqlite3.Connection, photo_id: int, person_id: int) -> None:
    conn.execute(
        "DELETE FROM person_tags WHERE photo_id = ? AND person_id = ?",
        (photo_id, person_id),
    )
    conn.commit()


def reset_person_tags(conn: sqlite3.Connection, person_id: int) -> int:
    """Drop model-origin tags for a person; user and seed tags survive."""
    cur = conn.execute(
        "DELETE FROM person_tags WHERE person_id = ? AND origin = 'model'",
        (person_id,),
    )
    conn.commit()
    return cur.rowcount


def delete_person(conn: sqlite3.Connection, person_id: int) -> int:
    # person_tags rows first: foreign keys are ON, the parent cannot go while
    # children reference it.
    conn.execute("DELETE FROM person_tags WHERE person_id = ?", (person_id,))
    cur = conn.execute("DELETE FROM people WHERE id = ?", (person_id,))
    conn.commit()
    return cur.rowcount


def people_list(
    conn: sqlite3.Connection, min_confidence: float = 0.6
) -> list[sqlite3.Row]:
    """People with tag counts (visible photos only)."""
    return conn.execute(
        """
        SELECT pe.id, pe.name, pe.description, pe.created_at, pe.updated_at,
            (SELECT COUNT(*) FROM person_tags t JOIN photos p ON p.id = t.photo_id
             WHERE t.person_id = pe.id AND t.present = 1 AND p.deleted_at IS NULL) AS tags,
            (SELECT COUNT(*) FROM person_tags t JOIN photos p ON p.id = t.photo_id
             WHERE t.person_id = pe.id AND t.present = 1 AND p.deleted_at IS NULL
             AND t.origin IN ('seed', 'user')) AS confirmed,
            (SELECT COUNT(*) FROM person_tags t JOIN photos p ON p.id = t.photo_id
             WHERE t.person_id = pe.id AND t.present = 1 AND p.deleted_at IS NULL
             AND t.origin = 'model' AND t.confidence >= ?) AS strong,
            (SELECT COUNT(*) FROM person_tags t JOIN photos p ON p.id = t.photo_id
             WHERE t.person_id = pe.id AND t.present = 1 AND p.deleted_at IS NULL
             AND t.origin = 'model' AND t.confidence < ?) AS uncertain,
            (SELECT COUNT(*) FROM person_tags t WHERE t.person_id = pe.id
             AND t.seed = 1) AS seeds
        FROM people pe
        ORDER BY pe.name COLLATE NOCASE
        """,
        (min_confidence, min_confidence),
    ).fetchall()


def person_tag_rows(
    conn: sqlite3.Connection,
    person_id: int,
    hidden_only: bool = False,
    show_hidden: bool = False,
) -> list[sqlite3.Row]:
    """Tagged photos of a person (visible only by default; pass
    show_hidden to include hidden photos or hidden_only for just those),
    seeds and high confidence first — the tail of the list doubles as the
    review queue."""
    if hidden_only:
        hidden_clause = "AND p.hidden = 1"
    elif show_hidden:
        hidden_clause = ""
    else:
        hidden_clause = "AND p.hidden = 0"
    return conn.execute(
        f"""
        SELECT p.id, p.status, p.has_text, p.text_kind, p.language, p.category,
            p.tiled, p.gated, p.derivative, p.text, p.error, p.hidden,
            (SELECT path FROM locations WHERE photo_id = p.id ORDER BY id LIMIT 1) AS path,
            t.confidence, t.origin, t.box
        FROM person_tags t JOIN photos p ON p.id = t.photo_id
        WHERE t.person_id = ? AND t.present = 1 AND p.deleted_at IS NULL
            {hidden_clause}
        ORDER BY t.origin = 'seed' DESC, t.confidence DESC, t.created_at DESC
        """,
        (person_id,),
    ).fetchall()


def people_for_photo(conn: sqlite3.Connection, photo_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT t.person_id, t.confidence, t.origin, t.box, pe.name "
        "FROM person_tags t JOIN people pe ON pe.id = t.person_id "
        "WHERE t.photo_id = ? AND t.present = 1 "
        "ORDER BY t.origin = 'seed' DESC, t.confidence DESC",
        (photo_id,),
    ).fetchall()


def person_seed_photo_ids(
    conn: sqlite3.Connection, person_id: int, limit: int = 3
) -> list[int]:
    """Most recent seed anchors for a person (input for the description)."""
    return [
        r["photo_id"]
        for r in conn.execute(
            "SELECT photo_id FROM person_tags WHERE person_id = ? AND seed = 1 "
            "ORDER BY COALESCE(updated_at, created_at) DESC, photo_id DESC LIMIT ?",
            (person_id, limit),
        )
    ]


def people_candidates(
    conn: sqlite3.Connection, person_ids: list[int]
) -> list[sqlite3.Row]:
    """Visible photos with local-pixel potential that are still missing a tag
    for at least one of the given people (the resume rule for the
    matching pass: fully tagged photos are skipped)."""
    if not person_ids:
        return []
    marks = ",".join("?" for _ in person_ids)
    return conn.execute(
        f"""
        SELECT p.id,
            (SELECT path FROM locations WHERE photo_id = p.id ORDER BY id LIMIT 1) AS path
        FROM photos p
        WHERE p.deleted_at IS NULL AND p.hidden = 0 AND p.status != 'deferred'
        AND EXISTS (
            SELECT 1 FROM people pe WHERE pe.id IN ({marks})
            AND NOT EXISTS (
                SELECT 1 FROM person_tags t
                WHERE t.photo_id = p.id AND t.person_id = pe.id
            )
        )
        ORDER BY p.id
        """,
        person_ids,
    ).fetchall()


def export_rows(
    conn: sqlite3.Connection, status: str | None = "done"
) -> list[sqlite3.Row]:
    """All photos with their location paths, for export. Paths are joined
    with the \\x1f unit separator in the `paths` column."""
    sql = (
        "SELECT p.*, (SELECT GROUP_CONCAT(path, char(31)) FROM locations "
        "WHERE photo_id = p.id) AS paths FROM photos p WHERE p.deleted_at IS NULL"
    )
    params: list = []
    if status and status != "all":
        sql += " AND p.status = ?"
        params.append(status)
    sql += " ORDER BY p.id"
    return conn.execute(sql, params).fetchall()
