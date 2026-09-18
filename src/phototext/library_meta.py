"""Resolve album / favorites / UUID membership to file paths inside a photo library.

Photos libraries (``.photoslibrary``) are read via the optional ``osxphotos``
package. iPhoto libraries (``.photolibrary``) are read directly from their
``Database/apdb`` SQLite file; iPhoto's schema was never documented and varies
across versions, so tables/columns are detected at runtime and any structure we
cannot understand raises a ``LibraryMetaError`` telling the user to fall back to
date or path-based slices instead.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path


class LibraryMetaError(ValueError):
    pass


def norm_path(p: str | Path) -> str:
    """Canonical form for path comparisons: resolved (symlinks, /var ->
    /private/var) and lowercased (macOS case-insensitive filesystems)."""
    return os.path.realpath(os.path.expanduser(str(p))).lower()


def resolve_paths(
    library: Path,
    album: str | None = None,
    favorites: bool = False,
    uuids: set[str] | None = None,
) -> set[str]:
    """Normalized paths of library photos matching every given criterion.

    Empty/falsy ``uuids`` means "no UUID constraint". Unknown UUIDs raise.
    """
    library = Path(library).expanduser().resolve()
    if not library.is_dir():
        raise LibraryMetaError(f"photo library not found: {library}")
    uuids = {u.lower() for u in (uuids or set())}
    if library.name.lower().endswith(".photoslibrary"):
        return _photos_paths(library, album, favorites, uuids)
    return _iphoto_paths(library, album, favorites, uuids)


def _get(obj, name: str, default=None):
    try:
        value = getattr(obj, name)
    except Exception:
        return default
    return default if value is None else value


def _photos_paths(
    library: Path, album: str | None, favorites: bool, uuids: set[str]
) -> set[str]:
    try:
        import osxphotos
    except ImportError as e:
        raise LibraryMetaError(
            "reading albums/favorites from a Photos library needs the 'osxphotos' "
            "package (uv pip install osxphotos); or filter with --date-from/--date-to "
            "or --ids-file containing paths"
        ) from e
    try:
        photo_db = osxphotos.PhotosDB(str(library))
        photos = photo_db.photos()
    except LibraryMetaError:
        raise
    except Exception as e:
        raise LibraryMetaError(f"could not read Photos library '{library}': {e}") from e
    wanted_album = None if album is None else album.lower()
    known_uuids: set[str] = set()
    out: set[str] = set()
    for photo in photos:
        if not _get(photo, "isphoto", True) or _get(photo, "ismovie", False):
            continue
        photo_uuid = str(_get(photo, "uuid", "")).lower()
        known_uuids.add(photo_uuid)
        if uuids and photo_uuid not in uuids:
            continue
        if favorites and not _get(photo, "favorite", False):
            continue
        if wanted_album is not None:
            titles = [
                str(_get(a, "title", "") or "").lower()
                for a in (_get(photo, "albums", []) or [])
            ]
            if wanted_album not in titles:
                continue
        path = _get(photo, "path", None)
        if path is None:
            path = _get(photo, "original_path", None)
        if path:
            out.add(norm_path(path))
    if uuids:
        missing = sorted(u for u in uuids if u not in known_uuids)
        if missing:
            raise LibraryMetaError(
                f"UUID(s) not found in this library: {', '.join(missing)}"
            )
    return out


_IPHOTO_DB_CANDIDATES = ("Database/apdb/Database", "Database/Library.apdb")


def iter_photos_assets(library: Path):
    """Yield (uuid, original_path_or_None, hidden) for every photo asset a
    Photos library knows about — including iCloud-only ones with no local
    file.

    Raises LibraryMetaError if osxphotos is unavailable or the library
    cannot be read.

    Test seam: ``PHOTOTEXT_TEST_ASSETS=<json-file>`` replaces osxphotos
    with a JSON list of ``[uuid, original_path_or_null, hidden]`` triples,
    so the Photos-library scan path can be exercised without a real
    library (used by tests/e2e.py; real libraries never see it).
    """
    seam = os.environ.get("PHOTOTEXT_TEST_ASSETS", "").strip()
    if seam:
        with open(seam, encoding="utf-8") as f:
            entries = json.load(f)
        for uuid, path, hidden in entries:
            yield str(uuid), (Path(path) if path else None), bool(hidden)
        return
    try:
        import osxphotos
    except ImportError as e:
        raise LibraryMetaError(
            "reading Photos libraries needs the 'osxphotos' package "
            "(uv pip install osxphotos)"
        ) from e
    try:
        photo_db = osxphotos.PhotosDB(str(library))
    except Exception as e:
        raise LibraryMetaError(f"could not read Photos library '{library}': {e}") from e
    for photo in photo_db.photos(movies=False):
        path = _get(photo, "original_path", None) or _get(photo, "path", None)
        hidden = bool(_get(photo, "hidden", False))
        yield str(photo.uuid), (Path(path) if path else None), hidden


def find_derivative(library: Path, uuid: str) -> Path | None:
    """Best-effort preview thumbnail for a photo without a local original.

    Derivative naming is undocumented; we glob by uuid and take the largest
    match. Returns None when no preview exists.
    """
    derivatives = library / "resources" / "derivatives"
    if not derivatives.is_dir():
        return None
    try:
        matches = [p for p in derivatives.glob(f"*/{uuid}*") if p.is_file()]
    except OSError:
        return None
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_size)


def _open_iphoto_db(library: Path) -> sqlite3.Connection:
    candidates: list[Path] = [library / rel for rel in _IPHOTO_DB_CANDIDATES]
    database_dir = library / "Database"
    if database_dir.is_dir():
        candidates.extend(sorted(database_dir.rglob("*.apdb")))
    tried = 0
    for cand in candidates:
        if not cand.is_file():
            continue
        tried += 1
        try:
            conn = sqlite3.connect(f"file:{cand}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            tables = {
                r[0].lower()
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "rkversion" in tables and "rkmaster" in tables:
                conn.row_factory = sqlite3.Row
                return conn
        except sqlite3.Error:
            pass
        conn.close()
    if tried:
        raise LibraryMetaError(
            f"no readable iPhoto database (RKVersion/RKMaster tables) inside '{library}'; "
            "album/favorites/UUID slices are unavailable for it — use --date-from/--date-to "
            "or --ids-file containing paths instead"
        )
    raise LibraryMetaError(
        f"no iPhoto database found inside '{library}' (looked in Database/); "
        "album/favorites/UUID slices need a .photolibrary or .photoslibrary source"
    )


def _tables_by_lower(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        r[0].lower(): r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _cols(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _pick(cols: list[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {c.lower(): c for c in cols}
    for cand in candidates:
        got = lowered.get(cand.lower())
        if got is not None:
            return got
    return None


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no")
    return bool(value)


def _chunked(items: list, size: int = 400):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _iphoto_paths(
    library: Path, album: str | None, favorites: bool, uuids: set[str]
) -> set[str]:
    conn = _open_iphoto_db(library)
    try:
        return _iphoto_query(conn, library, album, favorites, uuids)
    finally:
        conn.close()


def _iphoto_query(
    conn: sqlite3.Connection,
    library: Path,
    album: str | None,
    favorites: bool,
    uuids: set[str],
) -> set[str]:
    tables = _tables_by_lower(conn)
    version_table = tables["rkversion"]
    master_table = tables["rkmaster"]

    version_cols = _cols(conn, version_table)
    master_cols = _cols(conn, master_table)
    uuid_col = _pick(version_cols, ("uuid",))
    master_ref = _pick(version_cols, ("masterid", "masteruuid", "master_id", "master_uuid"))
    master_uuid_col = _pick(master_cols, ("uuid",))
    master_path_col = _pick(master_cols, ("imagepath", "image_path", "path"))
    if not (uuid_col and master_ref and master_uuid_col and master_path_col):
        raise LibraryMetaError(
            "this iPhoto database does not have the expected RKVersion/RKMaster columns "
            f"(uuid={uuid_col}, master ref={master_ref}, master uuid={master_uuid_col}, "
            f"master path={master_path_col}); album/favorites/UUID slices are unavailable — "
            "use --date-from/--date-to or --ids-file containing paths instead"
        )
    flag_col = _pick(version_cols, ("flagged", "favorite", "isfavorite", "is_favorite"))

    # Versions are matched in Python so all filters intersect simply.
    selects = [
        f'v.rowid AS _rowid',
        f'v."{uuid_col}" AS _uuid',
        f'v."{master_ref}" AS _mref',
    ]
    if favorites:
        if flag_col is None:
            raise LibraryMetaError(
                "no flagged/favorite column found in this iPhoto database; "
                "--favorites is unavailable — use --ids-file or --date-from/--date-to instead"
            )
        selects.append(f'v."{flag_col}" AS _flag')
    rows = conn.execute(
        f'SELECT {", ".join(selects)} FROM "{version_table}" v'
    ).fetchall()

    if uuids:
        known = {str(r["_uuid"]).lower() for r in rows}
        missing = sorted(u for u in uuids if u not in known)
        if missing:
            raise LibraryMetaError(
                f"UUID(s) not found in this library: {', '.join(missing)}"
            )
        rows = [r for r in rows if str(r["_uuid"]).lower() in uuids]
    if favorites:
        rows = [r for r in rows if _truthy(r["_flag"])]
    if album is not None:
        rows = [r for r in rows if r["_rowid"] in _album_version_rowids(conn, tables, album)]

    mrefs = [r["_mref"] for r in rows if r["_mref"] is not None]
    if not mrefs:
        return set()

    return _master_paths_for(
        conn, library, mrefs, master_table, master_uuid_col, master_path_col
    )


def _master_paths_for(
    conn: sqlite3.Connection,
    library: Path,
    mrefs: list,
    master_table: str,
    master_uuid_col: str,
    master_path_col: str,
) -> set[str]:
    """Resolve version master refs (uuid strings or rowids) to library paths."""
    # Master link: either v.<master_ref> = m.<uuid> or m.rowid = v.<master_ref>.
    paths: set[str] = set()
    if isinstance(mrefs[0], str) and not str(mrefs[0]).isdigit():
        sql_mrefs = [str(m) for m in mrefs]
        join = f'"{master_uuid_col}"'
        params = sql_mrefs
        for chunk in _chunked(params):
            marks = ",".join("?" for _ in chunk)
            for (p,) in conn.execute(
                f'SELECT "{master_path_col}" FROM "{master_table}" WHERE {join} IN ({marks})',
                chunk,
            ):
                paths.add(p)
    else:
        int_mrefs = [int(m) for m in mrefs]
        for chunk in _chunked(int_mrefs):
            marks = ",".join("?" for _ in chunk)
            for (p,) in conn.execute(
                f'SELECT "{master_path_col}" FROM "{master_table}" WHERE rowid IN ({marks})',
                chunk,
            ):
                paths.add(p)
    return {_resolve_iphoto_path(library, p) for p in paths if p}


def hidden_iphoto_paths(library: Path) -> set[str]:
    """Normalized paths of photos hidden inside an iPhoto library (best-effort).

    Old iPhoto schemas vary; when no hidden flag column can be found, or
    the database cannot be opened, the result is simply empty — the scan
    never fails because of this. Never raises.
    """
    library = Path(library).expanduser().resolve()
    try:
        conn = _open_iphoto_db(library)
    except LibraryMetaError:
        return set()
    try:
        tables = _tables_by_lower(conn)
        version_table = tables["rkversion"]
        master_table = tables["rkmaster"]
        version_cols = _cols(conn, version_table)
        master_cols = _cols(conn, master_table)
        master_ref = _pick(version_cols, ("masterid", "masteruuid", "master_id", "master_uuid"))
        master_uuid_col = _pick(master_cols, ("uuid",))
        master_path_col = _pick(master_cols, ("imagepath", "image_path", "path"))
        hidden_col = _pick(version_cols, ("hidden", "ishidden", "is_hidden"))
        if not (master_ref and master_uuid_col and master_path_col and hidden_col):
            return set()
        rows = conn.execute(
            f'SELECT "{master_ref}" AS _mref, "{hidden_col}" AS _hidden '
            f'FROM "{version_table}"'
        ).fetchall()
        mrefs = [r["_mref"] for r in rows if _truthy(r["_hidden"])]
        if not mrefs:
            return set()
        return _master_paths_for(
            conn, library, mrefs, master_table, master_uuid_col, master_path_col
        )
    except (sqlite3.Error, ValueError, TypeError):
        return set()
    finally:
        conn.close()


def _album_version_rowids(
    conn: sqlite3.Connection, tables: dict[str, str], album: str
) -> set[int]:
    album_table = tables.get("rkalbum")
    membership_table = next(
        (t for name, t in sorted(tables.items()) if "albumversion" in name), None
    )
    if album_table is None or membership_table is None:
        raise LibraryMetaError(
            "no RKAlbum/RKAlbumVersion tables in this iPhoto database; "
            "--album is unavailable — use --ids-file or --date-from/--date-to instead"
        )
    album_cols = _cols(conn, album_table)
    album_name_col = _pick(album_cols, ("name", "albumname", "title"))
    album_uuid_col = _pick(album_cols, ("uuid",))
    membership_cols = _cols(conn, membership_table)
    album_ref = _pick(membership_cols, ("albumid", "album_id"))
    version_ref = _pick(membership_cols, ("versionid", "version_id"))
    if not (album_name_col and album_ref and version_ref):
        raise LibraryMetaError(
            "unexpected iPhoto album tables/columns; --album is unavailable — "
            "use --ids-file or --date-from/--date-to instead"
        )
    n = conn.execute(
        f'SELECT COUNT(*) FROM "{album_table}" WHERE "{album_name_col}" = ? COLLATE NOCASE',
        (album,),
    ).fetchone()[0]
    if not n:
        raise LibraryMetaError(f"album '{album}' not found in this iPhoto library")
    album_match = f'"{album_name_col}" = ? COLLATE NOCASE'
    album_uuid_select = (
        f'SELECT "{album_uuid_col}" FROM "{album_table}" WHERE {album_match}'
        if album_uuid_col
        else None
    )
    album_rowid_select = f'SELECT rowid FROM "{album_table}" WHERE {album_match}'
    member_refs: list = []
    for album_select in (album_uuid_select, album_rowid_select):
        if album_select is None:
            continue
        member_refs = [
            r[0]
            for r in conn.execute(
                f'SELECT "{version_ref}" FROM "{membership_table}" '
                f'WHERE "{album_ref}" IN ({album_select})',
                (album,),
            )
        ]
        if member_refs:
            break
    if member_refs and isinstance(member_refs[0], str) and not str(member_refs[0]).isdigit():
        # version refs are version uuids; map them back to rowids
        wanted = {str(m).lower() for m in member_refs}
        version_table = tables["rkversion"]
        uuid_col = _pick(_cols(conn, version_table), ("uuid",))
        return {
            r[0]
            for r in conn.execute(f'SELECT rowid, "{uuid_col}" FROM "{version_table}"')
            if str(r[1]).lower() in wanted
        }
    return {int(m) for m in member_refs}


def _resolve_iphoto_path(library: Path, stored: str) -> str:
    p = Path(stored)
    if not p.is_absolute():
        p = library / p
    return norm_path(p)
