from __future__ import annotations

import calendar
import os
import sys
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image

from . import db, memes as memes_mod
from .imaging import sha256_file
from .library_meta import LibraryMetaError, norm_path, resolve_paths

IMAGE_EXTS = {
    "jpg",
    "jpeg",
    "jfif",
    "png",
    "heic",
    "heif",
    "tif",
    "tiff",
    "webp",
    "bmp",
    "gif",
    "jp2",
    "jpx",
    "j2k",
    "pict",
    "pct",
    "psd",
    "tga",
    "ico",
    "dng",
    "cr2",
    "crw",
    "nef",
    "arw",
    "orf",
    "raf",
    "rw2",
    "sr2",
    "pef",
}

SKIP_EXTS = {
    "mov",
    "mp4",
    "m4v",
    "avi",
    "mkv",
    "3gp",
    "wmv",
    "db",
    "db-wal",
    "db-shm",
    "plist",
    "aae",
    "json",
    "xml",
    "txt",
    "rtf",
    "pdf",
    "mp3",
    "m4a",
    "wav",
    "aiff",
    "zip",
    "gz",
}

LIBRARY_SUFFIXES = (".photoslibrary", ".photolibrary")
ORIGINALS_SUBDIRS = ("originals", "Originals", "Masters", "masters")


@dataclass(frozen=True)
class SourceRef:
    id: int
    uri: str
    kind: str
    root: Path


@dataclass
class ScanStats:
    images_found: int = 0
    new_photos: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: int = 0
    slice_skipped: int = 0
    deferred: int = 0
    previews: int = 0

    def update(self, other: "ScanStats") -> None:
        for field in (
            "images_found",
            "new_photos",
            "updated",
            "unchanged",
            "errors",
            "slice_skipped",
            "deferred",
            "previews",
        ):
            setattr(self, field, getattr(self, field) + getattr(other, field))


@dataclass(frozen=True)
class Slice:
    """A scan-time filter: only matching files are registered/queued."""

    date_from: datetime | None = None
    date_to: datetime | None = None
    album: str | None = None
    favorites: bool = False
    limit: int | None = None
    ids_file: Path | None = None

    def is_active(self) -> bool:
        return bool(
            self.date_from
            or self.date_to
            or self.album
            or self.favorites
            or self.limit
            or self.ids_file
        )

    def describe(self) -> str:
        parts = []
        if self.date_from or self.date_to:
            parts.append(
                "dates "
                + (self.date_from.date().isoformat() if self.date_from else "...")
                + ".."
                + (self.date_to.date().isoformat() if self.date_to else "...")
            )
        if self.album:
            parts.append(f"album '{self.album}'")
        if self.favorites:
            parts.append("favorites")
        if self.limit is not None:
            parts.append(f"limit {self.limit}")
        if self.ids_file:
            parts.append(f"ids from {self.ids_file}")
        return ", ".join(parts)


def parse_slice_date(text: str, *, end: bool) -> datetime:
    """Parse YYYY, YYYY-MM, YYYY-MM-DD, or a full ISO datetime.

    A bare date/month/year is widened to cover the whole period: `end=False`
    gives the first instant, `end=True` the last.
    """
    s = text.strip()
    if not s:
        raise ValueError("empty date")
    if s.count("-") == 0 and len(s) == 4:
        year = int(s)
        return (
            datetime(year, 12, 31, 23, 59, 59, 999999)
            if end
            else datetime(year, 1, 1)
        )
    if s.count("-") == 1:
        year, month = (int(part) for part in s.split("-"))
        if end:
            last = calendar.monthrange(year, month)[1]
            return datetime(year, month, last, 23, 59, 59, 999999)
        return datetime(year, month, 1)
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(
            f"invalid date: {text!r} (expected YYYY-MM-DD, YYYY-MM, or YYYY)"
        ) from None
    if end and len(s) <= 10:
        return d.replace(hour=23, minute=59, second=59, microsecond=999999)
    return d


def load_ids_file(path: Path) -> tuple[set[str], set[str]]:
    """Read an ids file: one path or UUID per line, `#` comments allowed.

    Returns (paths, uuids) with paths already normalized. A line counts as a
    path if it contains a separator or exists on disk; anything else is
    treated as a UUID.
    """
    try:
        content = Path(path).expanduser().read_text()
    except OSError as e:
        raise ValueError(f"could not read ids file {path}: {e}") from e
    paths: set[str] = set()
    uuids: set[str] = set()
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        candidate = Path(line).expanduser()
        if os.sep in line or (candidate.exists() and candidate.is_file()):
            paths.add(norm_path(candidate))
        else:
            uuids.add(line.lower())
    if not paths and not uuids:
        raise ValueError(f"ids file {path} contains no ids")
    return paths, uuids


def _slice_checker(uri: str, kind: str, spec: Slice):
    """Build a (path, stat) -> bool predicate for a slice. Raises ValueError
    (via library_meta) if album/favorites/UUID resolution is impossible."""
    constraints: list[set[str]] = []
    uuids: set[str] = set()
    if spec.ids_file is not None:
        id_paths, uuids = load_ids_file(spec.ids_file)
        if id_paths:
            constraints.append(id_paths)
    if spec.album or spec.favorites or uuids:
        if kind != "library":
            raise ValueError(
                "--album/--favorites and UUID ids only apply to photo libraries, not "
                "plain folders; use --date-from/--date-to or path ids instead"
            )
        try:
            resolved = resolve_paths(
                Path(uri), album=spec.album, favorites=spec.favorites, uuids=uuids
            )
        except LibraryMetaError as e:
            raise ValueError(str(e)) from e
        constraints.append(resolved)
    allowed: set[str] | None = None
    if constraints:
        allowed = constraints[0]
        for extra in constraints[1:]:
            allowed = allowed & extra

    date_from, date_to = spec.date_from, spec.date_to

    def checker(path: Path, st: os.stat_result) -> bool:
        if allowed is not None and norm_path(path) not in allowed:
            return False
        if date_from is not None or date_to is not None:
            mtime = datetime.fromtimestamp(st.st_mtime)
            if date_from is not None and mtime < date_from:
                return False
            if date_to is not None and mtime > date_to:
                return False
        return True

    return checker


def resolve_source(path: Path) -> tuple[Path, str]:
    path = Path(path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"path not found: {path}")
    path = path.resolve()
    if path.name.endswith(LIBRARY_SUFFIXES):
        for sub in ORIGINALS_SUBDIRS:
            candidate = path / sub
            if candidate.is_dir():
                return candidate, "library"
        raise ValueError(
            f"'{path}' looks like a photo library but no originals folder was found inside "
            f"(looked for: {', '.join(ORIGINALS_SUBDIRS)}). Pass the folder that directly "
            "contains the image files instead."
        )
    return path, "folder"


def register_source(conn, path: Path) -> SourceRef:
    root, kind = resolve_source(path)
    uri = str(Path(path).expanduser().resolve())
    source_id = db.upsert_source(conn, uri, kind)
    conn.commit()
    return SourceRef(source_id, uri, kind, root)


def scan_source(
    conn, uri: str, source_id: int, slice_spec: Slice | None = None, quiet: bool = False
) -> ScanStats:
    root, kind = resolve_source(Path(uri))
    stats = ScanStats()
    pending = 0
    walk_errors: list[OSError] = []
    checker = None
    if slice_spec is not None and slice_spec.is_active():
        if not quiet:
            print(f"  slice: {slice_spec.describe()}")
        checker = _slice_checker(uri, kind, slice_spec)
    if kind == "library" and Path(uri).name.lower().endswith(".photoslibrary"):
        return _scan_photos_library(
            conn, Path(uri), source_id, stats, checker, slice_spec, quiet
        )
    queued_new = 0
    for image_path in _iter_image_files(root, walk_errors):
        stats.images_found += 1
        try:
            st = image_path.stat()
        except OSError:
            stats.errors += 1
            continue
        if st.st_size == 0:
            continue
        if checker is not None and not checker(image_path, st):
            stats.slice_skipped += 1
            continue
        existing = db.get_location(conn, source_id, str(image_path))
        if (
            existing is not None
            and existing["mtime"] == int(st.st_mtime)
            and existing["size"] == st.st_size
        ):
            stats.unchanged += 1
        else:
            try:
                digest = sha256_file(image_path)
            except OSError:
                stats.errors += 1
                continue
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", UserWarning)
                phash = memes_mod.dhash(image_path)
            photo_id, created = db.upsert_photo(conn, digest, st.st_size, phash)
            for warning in caught:
                db.record_warning(
                    conn, photo_id,
                    type(warning.message).__name__, str(warning.message),
                )
            db.upsert_location(
                conn, photo_id, source_id, str(image_path), int(st.st_mtime), st.st_size
            )
            if created:
                stats.new_photos += 1
                if slice_spec is not None and slice_spec.limit is not None:
                    queued_new += 1
            else:
                stats.updated += 1
        pending += 1
        if pending >= 500:
            conn.commit()
            pending = 0
            if not quiet:
                print(f"  ... {stats.images_found} images", end="\r", flush=True)
        if (
            slice_spec is not None
            and slice_spec.limit is not None
            and queued_new >= slice_spec.limit
        ):
            if not quiet:
                print(f"  slice: queued {queued_new} new photo(s); stopping this scan")
            break
    conn.commit()
    if stats.images_found and not quiet:
        print()
    if walk_errors:
        print(
            f"warning: {len(walk_errors)} folder(s) could not be read during the walk "
            f"(first: {walk_errors[0]}). If scanning a Photos/iPhoto library, grant Full "
            "Disk Access to your terminal app and rescan.",
            file=sys.stderr,
        )
    return stats


def _scan_photos_library(
    conn, library: Path, source_id: int, stats: "ScanStats", checker, slice_spec, quiet: bool
) -> "ScanStats":
    """Scan a Photos library via its database (osxphotos), not the filesystem.

    This is a superset of walking originals/: it also registers iCloud-only
    photos (deferred until their file appears) and processes their preview
    derivatives as best-effort extractions (flagged `derivative`).
    """
    from .library_meta import find_derivative, iter_photos_assets

    pending = 0
    for uuid, original in iter_photos_assets(library):
        stats.images_found += 1
        if original is not None and original.exists():
            try:
                st = original.stat()
            except OSError:
                stats.errors += 1
                continue
            if checker is not None and not checker(original, st):
                stats.slice_skipped += 1
                continue
            existing = db.get_location(conn, source_id, str(original))
            deferred = conn.execute(
                "SELECT id FROM photos WHERE sha256 = ?", (db.deferred_key(uuid),)
            ).fetchone()
            if (
                existing is not None
                and existing["mtime"] == int(st.st_mtime)
                and existing["size"] == st.st_size
                and deferred is None
            ):
                stats.unchanged += 1
                continue
            try:
                digest = sha256_file(original)
            except OSError:
                stats.errors += 1
                continue
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", UserWarning)
                phash = memes_mod.dhash(original)
            photo_id = db.promote_deferred(
                conn, uuid, digest, st.st_size, source_id, str(original),
                int(st.st_mtime),
            )
            if phash is not None:
                conn.execute(
                    "UPDATE photos SET phash = ? WHERE id = ?", (phash, photo_id)
                )
            for warning in caught:
                db.record_warning(
                    conn, photo_id,
                    type(warning.message).__name__, str(warning.message),
                )
            if deferred is not None:
                stats.updated += 1
            else:
                stats.new_photos += 1
            pending += 1
        else:
            if slice_spec is not None and slice_spec.is_active():
                stats.slice_skipped += 1
                continue
            derivative_path = find_derivative(library, uuid)
            existing = conn.execute(
                "SELECT p.id, p.derivative FROM photos p WHERE p.sha256 = ?",
                (db.deferred_key(uuid),),
            ).fetchone()
            if existing is None:
                db.ensure_deferred_photo(
                    conn, source_id, uuid,
                    str(derivative_path) if derivative_path else None,
                    bool(derivative_path),
                )
                if derivative_path:
                    stats.previews += 1
                else:
                    stats.deferred += 1
            elif derivative_path is not None and not existing["derivative"]:
                db.ensure_deferred_photo(
                    conn, source_id, uuid, str(derivative_path), True
                )
                conn.execute(
                    "UPDATE photos SET status = 'queued' WHERE id = ? AND "
                    "status = 'deferred'",
                    (existing["id"],),
                )
                stats.previews += 1
            pending += 1
        if pending >= 500:
            conn.commit()
            pending = 0
            if not quiet:
                print(f"  ... {stats.images_found} images", end="\r", flush=True)
    conn.commit()
    if stats.images_found and not quiet:
        print()
    return stats


def _iter_image_files(root: Path, walk_errors: list[OSError]):
    def onerror(err: OSError) -> None:
        walk_errors.append(err)

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            ext = path.suffix.lower().lstrip(".")
            if ext in IMAGE_EXTS:
                yield path
            elif ext in SKIP_EXTS:
                continue
            elif _looks_like_image(path):
                yield path


def _looks_like_image(path: Path) -> bool:
    try:
        with Image.open(path) as im:
            return im.format is not None
    except Exception:
        return False
