from __future__ import annotations

import csv
import json
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import List, Optional

import typer

from . import config, db, people as people_mod, scanner, webui, worker
from .config import load_config
from .imaging import test_image_b64
from .library_meta import norm_path
from .memes import ensure_hashes, find_clusters
from .ollama_client import OllamaClient

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

app = typer.Typer(
    no_args_is_help=True,
    help="Recover text from photos using a local Ollama vision model.\n\n"
    "Run `phototext help` for a usage guide with typical workflows.",
)
people_app = typer.Typer(
    no_args_is_help=True,
    help="Identify people in photos and tag them across the library.",
)
app.add_typer(people_app, name="people")

USAGE_GUIDE = """\
phototext — recover text from photos using a local Ollama vision model

GETTING STARTED
  phototext doctor                                        check Ollama, model, vision, db
  phototext --version                                    show the installed version
  phototext scan "~/Pictures/Photos Library.photoslibrary" register a library or folder
  phototext run --limit 5                                  small first test
  phototext run                                            process the whole queue
  phototext autocomplete                                  install TAB completion (bash/zsh)
  phototext search "invoice"                               full-text search over recovered text
  phototext serve                                         browse photos + text at http://127.0.0.1:8765

TYPICAL WORKFLOWS
  Overnight run:         phototext run --stop-after 8h
  While I'm at lunch:    phototext run --stop-after 90m
  Favorites only:        phototext run --favorites
  One album:            phototext run --album "Trip 2014"
  Photos from 2013:     phototext run --date-from 2013 --date-to 2013
  Keep watching:          phototext run --watch               (auto-process new photos)
  Parallel workers:      phototext run --workers 3           (for parallel/remote backends)
  Cheaper first pass:   phototext run --two-tier              (prefilter skips textless photos)
  Redo failures:         phototext retry && phototext run
  Redo dense photos:     phototext reprocess --tiled --model <tag>
  Find memes:            phototext memes
  Tag a person:          phototext people name 512 "Ryan" --box 300,150,400,400
                          (box = face region in pixels; omit for the whole photo)
  Tag people everywhere: phototext people run --stop-after 4h
                          (one pass checks every named person per photo)
  Review uncertain tags: phototext people photos Ryan    (or: serve --writable)
  Confirm / remove:      phototext people confirm 512 Ryan
                          phototext people remove 512 Ryan
  Start over for one:    phototext people reset Ryan     (keeps confirmed tags)
  Search by person:      phototext search --person Ryan "invoice"
  See categories:        phototext categories
  Export results:        phototext export --format csv --output results.csv
  Browse in a browser:   phototext serve
  Edit from browser:     phototext serve --writable           (hide/delete, token-guarded)
  Hide some results:     phototext hide 12 15                 (catalog only, files untouched)
  Delete + restore:      phototext delete 12 && phototext trash
                         phototext restore 12; phototext purge --empty-trash to forget
  Drop a bad scan:       phototext status (find the source id) && phototext unscan <id>
  Separate catalogs:    phototext --profile memes scan ~/Pictures/memes
                         (phototext profiles lists them; per-profile config.toml)

ICLOUD PHOTO LIBRARIES
  Scanning a .photoslibrary inventories every photo via its database:
  local originals are processed in full, iCloud-only photos with local
  previews are processed from those previews (flagged 'preview'), and photos
  with no local pixels are parked as 'deferred' until you download them —
  the next scan picks them up automatically.

WHERE THINGS LIVE
  config:   ~/.phototext/config.toml     (installed copy: <prefix>/var/config.toml)
  catalog:  ~/.phototext/catalog.db      (installed copy: <prefix>/var/catalog.db)
  web UI:   phototext serve -> http://127.0.0.1:8765

Run `phototext <command> --help` for all options of a command.
"""

_state: dict = {}


def _version() -> str:
    # Source tree (editable/dev installs): pyproject.toml is authoritative;
    # installed metadata there can be stale. Snapshot installs have no
    # pyproject next to the package and use the dist metadata.
    try:
        import tomllib

        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        if pyproject.is_file():
            with open(pyproject, "rb") as f:
                return str(tomllib.load(f)["project"]["version"])
    except Exception:  # pragma: no cover
        pass
    try:
        from importlib.metadata import version

        return version("phototext")
    except Exception:  # pragma: no cover - uninstalled source tree
        return "unknown"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"phototext {_version()}")
        raise typer.Exit()


@app.callback()
def main(
    config: Optional[Path] = typer.Option(None, "--config", help="Path to a config.toml file."),
    db_path: Optional[Path] = typer.Option(None, "--db", help="Path to the SQLite catalog."),
    profile: Optional[str] = typer.Option(
        None,
        "--profile",
        help="Use a named profile: separate catalog (+ optional config.toml) "
        "under <state>/profiles/<name>/. --db still wins if passed.",
    ),
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        expose_value=False,
        help="Show the phototext version and exit.",
    ),
) -> None:
    _state["config"] = config
    _state["db"] = db_path
    _state["profile"] = profile


def _base_config_path() -> Path:
    if _state.get("config"):
        return Path(_state["config"]).expanduser()
    return config.DEFAULT_CONFIG_PATH


_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _profile_dir(name: str) -> Path:
    if not _PROFILE_NAME.fullmatch(name) or ".." in name:
        typer.echo(f"error: invalid profile name: {name!r}", err=True)
        raise typer.Exit(2)
    return _base_config_path().parent / "profiles" / name


def _cfg():
    cfg = None
    if _state.get("profile"):
        pdir = _profile_dir(_state["profile"])
        pconf = pdir / "config.toml"
        if pconf.is_file():
            cfg = load_config(pconf)
        if cfg is None:
            try:
                cfg = load_config(_state.get("config"))
            except FileNotFoundError as e:
                typer.echo(f"error: {e}", err=True)
                raise typer.Exit(2)
        cfg.db_path = pdir / "catalog.db"
    else:
        try:
            cfg = load_config(_state.get("config"))
        except FileNotFoundError as e:
            typer.echo(f"error: {e}", err=True)
            raise typer.Exit(2)
    if _state.get("db"):
        cfg.db_path = Path(_state["db"]).expanduser()
    return cfg


def _counts_summary(counts: dict) -> str:
    parts = []
    for key in ("queued", "processing", "done", "error", "deferred"):
        if counts.get(key):
            parts.append(f"{key} {counts[key]}")
    return f"{counts.get('total', 0)} photo(s)" + (f" ({', '.join(parts)})" if parts else "")


def _opt_album():
    return typer.Option(
        None, "--album", help="Only queue photos in this album (photo libraries only)."
    )


def _opt_date_from():
    return typer.Option(
        None,
        "--date-from",
        help="Only queue photos dated on/after this (YYYY-MM-DD, YYYY-MM, or YYYY).",
    )


def _opt_date_to():
    return typer.Option(
        None,
        "--date-to",
        help="Only queue photos dated on/before this (YYYY-MM-DD, YYYY-MM, or YYYY).",
    )


def _opt_favorites():
    return typer.Option(
        False, "--favorites", help="Only queue favorite/flagged photos (libraries only)."
    )


def _opt_ids_file():
    return typer.Option(
        None,
        "--ids-file",
        help="File with one photo path or UUID per line; only those are queued.",
    )


def _build_slice(
    album: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    favorites: bool,
    limit: Optional[int],
    ids_file: Optional[Path],
) -> Optional[scanner.Slice]:
    fields: dict = {}
    if album is not None:
        if not album.strip():
            typer.echo("error: --album requires a name", err=True)
            raise typer.Exit(2)
        fields["album"] = album.strip()
    if date_from is not None:
        try:
            fields["date_from"] = scanner.parse_slice_date(date_from, end=False)
        except ValueError as e:
            typer.echo(f"error: --date-from: {e}", err=True)
            raise typer.Exit(2)
    if date_to is not None:
        try:
            fields["date_to"] = scanner.parse_slice_date(date_to, end=True)
        except ValueError as e:
            typer.echo(f"error: --date-to: {e}", err=True)
            raise typer.Exit(2)
    if (
        fields.get("date_from") is not None
        and fields.get("date_to") is not None
        and fields["date_from"] > fields["date_to"]
    ):
        typer.echo("error: --date-from is after --date-to", err=True)
        raise typer.Exit(2)
    if favorites:
        fields["favorites"] = True
    if limit is not None:
        if limit < 1:
            typer.echo("error: --limit must be at least 1", err=True)
            raise typer.Exit(2)
        fields["limit"] = limit
    if ids_file is not None:
        if not Path(ids_file).expanduser().is_file():
            typer.echo(f"error: ids file not found: {ids_file}", err=True)
            raise typer.Exit(2)
        fields["ids_file"] = Path(ids_file).expanduser()
    if not fields:
        return None
    return scanner.Slice(**fields)


@app.command()
def scan(
    paths: Optional[List[Path]] = typer.Argument(
        None, help="Folder or photo-library path(s) to register and scan."
    ),
    album: Optional[str] = _opt_album(),
    date_from: Optional[str] = _opt_date_from(),
    date_to: Optional[str] = _opt_date_to(),
    favorites: bool = _opt_favorites(),
    limit: Optional[int] = typer.Option(
        None, "--limit", help="Queue at most N new photos from this scan."
    ),
    ids_file: Optional[Path] = _opt_ids_file(),
) -> None:
    """Register sources and scan them. Omit PATHS to rescan all registered sources.

    Slice filters (--album, --date-from/--date-to, --favorites, --limit,
    --ids-file) restrict this scan: only matching photos are registered/queued;
    everything else is left untouched.
    """
    cfg = _cfg()
    slice_spec = _build_slice(album, date_from, date_to, favorites, limit, ids_file)
    conn = db.connect(cfg.db_path)
    if paths:
        refs = []
        for p in paths:
            try:
                refs.append(scanner.register_source(conn, p))
            except (FileNotFoundError, ValueError) as e:
                typer.echo(f"error: {e}", err=True)
                raise typer.Exit(2)
        for ref in refs:
            typer.echo(f"source: {ref.uri} [{ref.kind}] originals at {ref.root}")
        targets = [(ref.uri, ref.id) for ref in refs]
    else:
        rows = db.get_sources(conn)
        if not rows:
            typer.echo("No sources registered yet. Usage: phototext scan /path/to/photo-library")
            raise typer.Exit(2)
        targets = [(row["uri"], row["id"]) for row in rows]
    totals = scanner.ScanStats()
    for uri, source_id in targets:
        typer.echo(f"scanning {uri}")
        try:
            stats = scanner.scan_source(conn, uri, source_id, slice_spec)
        except (FileNotFoundError, ValueError) as e:
            typer.echo(f"  error: {e}", err=True)
            if slice_spec is not None:
                raise typer.Exit(2)
            continue
        line = (
            f"  images {stats.images_found} | new {stats.new_photos} | updated {stats.updated} "
            f"| unchanged {stats.unchanged} | errors {stats.errors}"
        )
        if stats.slice_skipped:
            line += f" | slice-skipped {stats.slice_skipped}"
        if stats.deferred:
            line += f" | awaiting download {stats.deferred}"
        if stats.previews:
            line += f" | previews {stats.previews}"
        typer.echo(line)
        totals.update(stats)
    typer.echo(f"catalog: {_counts_summary(db.status_counts(conn))}")


@app.command()
def run(
    stop_after: Optional[str] = typer.Option(
        None, "--stop-after", help="Stop after a duration, e.g. 45s, 90m, 2h, 1h30m."
    ),
    model: Optional[str] = typer.Option(None, "--model", help="Override the model for this run."),
    no_scan: bool = typer.Option(False, "--no-scan", help="Skip rescanning registered sources."),
    skip_preflight: bool = typer.Option(
        False, "--skip-preflight", help="Skip Ollama preflight checks."
    ),
    limit: Optional[int] = typer.Option(
        None, "--limit", help="Process at most N photos this run (good for a first test)."
    ),
    album: Optional[str] = _opt_album(),
    date_from: Optional[str] = _opt_date_from(),
    date_to: Optional[str] = _opt_date_to(),
    favorites: bool = _opt_favorites(),
    ids_file: Optional[Path] = _opt_ids_file(),
    no_idle_detection: bool = typer.Option(
        False,
        "--no-idle-detection",
        help="Do not pause while other models are loaded in Ollama.",
    ),
    watch: bool = typer.Option(
        False,
        "--watch",
        help="Keep running after the queue drains: watch sources for new photos "
        "and process them as they appear.",
    ),
    watch_interval: int = typer.Option(
        60, "--watch-interval", help="Seconds between watch scans (with --watch)."
    ),
    workers: int = typer.Option(
        1,
        "--workers",
        help="Parallel worker processes. Only useful with parallel-capable or "
        "remote inference backends; a single Ollama model slot serializes anyway.",
    ),
    two_tier: bool = typer.Option(
        False,
        "--two-tier",
        help="Check each photo with the cheap prefilter model first; textless "
        "photos finish there (seconds, with a short description and category).",
    ),
) -> None:
    """Scan sources and process queued photos until done, budget, or Ctrl+C.

    Slice filters (--album, --date-from/--date-to, --favorites, --ids-file)
    restrict the scan phase: only matching photos are newly queued. Photos
    already queued from earlier scans are still processed.
    """
    cfg = _cfg()
    if no_idle_detection:
        cfg = replace(cfg, idle_detection=False)
    if two_tier:
        cfg = replace(cfg, two_tier=True)
    if workers > 1 and limit is not None:
        typer.echo(
            "error: --limit is not supported with --workers > 1; use --stop-after "
            "to bound the run instead",
            err=True,
        )
        raise typer.Exit(2)
    slice_spec = _build_slice(album, date_from, date_to, favorites, None, ids_file)
    try:
        code = worker.run_pipeline(
            cfg,
            stop_after=stop_after,
            model=model,
            no_scan=no_scan,
            skip_preflight=skip_preflight,
            limit=limit,
            slice_spec=slice_spec,
            watch=watch,
            watch_interval_s=watch_interval,
            workers=workers,
            config_path=_state.get("config"),
        )
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)
    raise typer.Exit(code)


@app.command()
def status() -> None:
    """Show catalog counts, sources, throughput, and recent errors."""
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    typer.echo(f"catalog: {cfg.db_path}")
    sources = db.get_sources(conn)
    typer.echo(f"sources: {len(sources)}")
    for row in sources:
        typer.echo(f"  [{row['id']}] [{row['kind']}] {row['uri']}")
    counts = db.status_counts(conn)
    typer.echo(f"photos:  {_counts_summary(counts)}")
    hidden_row = conn.execute(
        "SELECT COUNT(*) AS n FROM photos WHERE hidden = 1 AND deleted_at IS NULL"
    ).fetchone()
    trash_row = conn.execute(
        "SELECT COUNT(*) AS n FROM photos WHERE deleted_at IS NOT NULL"
    ).fetchone()
    if hidden_row["n"] or trash_row["n"]:
        typer.echo(
            f"hidden:  {hidden_row['n']} photo(s)   trash: {trash_row['n']} photo(s)"
        )
    n_warnings = db.warnings_count(conn)
    if n_warnings:
        typer.echo(f"warnings: {n_warnings}")
    people_rows = db.people_list(conn, cfg.person_min_confidence)
    if people_rows:
        typer.echo(
            f"people:   {len(people_rows)} person(s), "
            f"{sum(p['tags'] for p in people_rows)} tag(s) "
            f"({sum(p['uncertain'] for p in people_rows)} to review)"
        )
    avg = db.avg_recent_duration_ms(conn)
    if avg:
        queued = counts.get("queued", 0)
        if queued:
            eta_min = avg / 1000.0 * queued / 60.0
            typer.echo(
                f"throughput: {avg / 1000.0:.1f}s/photo (recent avg), "
                f"~{eta_min:.0f} min left for {queued} queued"
            )
        else:
            typer.echo(f"throughput: {avg / 1000.0:.1f}s/photo (recent avg)")
    last = db.last_finished(conn)
    if last:
        typer.echo(f"last finished: {last}")
    errors = db.recent_results(conn, n=5, status="error")
    if errors:
        typer.echo("recent errors:")
        for row in errors:
            typer.echo(f"  [{row['id']}] {row['path'] or '?'}\n      {row['error']}")


@app.command()
def results(
    n: int = typer.Option(20, help="Number of photos to show."),
    status: str = typer.Option(
        "done", help="Filter: done, error, queued, processing, or all."
    ),
    category: Optional[str] = typer.Option(
        None, "--category", help="Filter by photo category (document, sign, scene, ...)."
    ),
    show_hidden: bool = typer.Option(
        False, "--hidden", help="Include hidden photos."
    ),
    full: bool = typer.Option(False, "--full", help="Show the full text without truncation."),
) -> None:
    """Show recent extraction results (text + context per photo)."""
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    rows = db.recent_results(
        conn, n=n, status=status, category=category, show_hidden=show_hidden
    )
    if not rows:
        typer.echo(f"no results with status '{status}' yet")
        return
    for row in rows:
        typer.echo(f"[{row['id']}] {row['path'] or '(no location)'}")
        if row["status"] == "error":
            typer.echo(f"    error: {row['error']}")
            for warning in db.photo_warnings_list(conn, row["id"]):
                typer.echo(f"    warning: {warning['kind']}: {warning['message']}")
            continue
        typer.echo(
            f"    kind: {row['text_kind'] or '?'}  category: {row['category'] or '?'}  "
            f"language: {row['language'] or '?'}  "
            f"has_text: {'yes' if row['has_text'] else 'no'}  model: {row['model'] or '?'}"
            + ("  tiled: yes" if row["tiled"] else "")
            + ("  gated: yes" if row["gated"] else "")
        )
        for warning in db.photo_warnings_list(conn, row["id"]):
            typer.echo(f"    warning: {warning['kind']}: {warning['message']}")
        text = row["text"] or ""
        if not full and len(text) > 600:
            text = text[:600] + f" ... ({len(row['text'])} chars total, use --full)"
        for i, line in enumerate(text.splitlines()):
            typer.echo(("    text: " if i == 0 else "          ") + line)
        typer.echo(f"    context: {row['context'] or ''}")


@app.command()
def search(
    query: List[str] = typer.Argument(
        ..., help="Terms to find in recovered text/context (FTS5 syntax; quote phrases)."
    ),
    limit: int = typer.Option(
        20, "--limit", help="Maximum number of matches to show."
    ),
    category: Optional[str] = typer.Option(
        None, "--category", help="Only match photos with this category (see `phototext categories`)."
    ),
    person: Optional[str] = typer.Option(
        None, "--person", help="Only match photos tagged with this person (see `phototext people list`)."
    ),
    include_hidden: bool = typer.Option(
        False, "--hidden", help="Include hidden photos in matches."
    ),
) -> None:
    """Full-text search over recovered text and photo context."""
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    joined = " ".join(query)
    highlight = ("\x1b[1m", "\x1b[0m") if sys.stdout.isatty() else ("", "")
    try:
        rows, effective = db.search_photos(
            conn, joined, limit=limit, highlight=highlight, category=category,
            include_hidden=include_hidden, person=person,
        )
        total = db.search_count(conn, joined, include_hidden=include_hidden, person=person)
    except sqlite3.OperationalError as e:
        typer.echo(f"error: search failed: {e}", err=True)
        raise typer.Exit(2)
    if not rows:
        typer.echo(f"no matches for '{joined}'")
        return
    header = f"{total} match(es) for '{joined}'"
    if effective != joined:
        header += f" (matched as {effective})"
    typer.echo(header)
    for row in rows:
        typer.echo(f"[{row['id']}] {row['path'] or '(no location)'}")
        typer.echo(
            f"    kind: {row['text_kind'] or '?'}  language: {row['language'] or '?'}  "
            f"model: {row['model'] or '?'}"
        )
        text = (row["text_snip"] or "").strip()
        if text:
            for i, line in enumerate(text.splitlines()):
                typer.echo(("    text: " if i == 0 else "          ") + line)
        context = (row["context_snip"] or "").strip()
        if context:
            typer.echo(f"    context: {context}")


_EXPORT_FIELDS = [
    "id",
    "sha256",
    "status",
    "has_text",
    "text_kind",
    "category",
    "language",
    "model",
    "tiled",
    "gated",
    "derivative",
    "duration_ms",
    "first_seen_at",
    "finished_at",
    "error",
    "paths",
    "text",
    "context",
]


def _export_record(row) -> dict:
    paths = (row["paths"] or "").split("\x1f") if row["paths"] else []
    has_text = row["has_text"]
    return {
        "id": row["id"],
        "sha256": row["sha256"],
        "status": row["status"],
        "has_text": None if has_text is None else bool(has_text),
        "text_kind": row["text_kind"],
        "category": row["category"],
        "language": row["language"],
        "model": row["model"],
        "tiled": bool(row["tiled"]),
        "gated": bool(row["gated"]),
        "derivative": bool(row["derivative"]),
        "duration_ms": row["duration_ms"],
        "first_seen_at": row["first_seen_at"],
        "finished_at": row["finished_at"],
        "error": row["error"],
        "paths": paths,
        "text": row["text"],
        "context": row["context"],
    }


@app.command()
def export(
    fmt: str = typer.Option("jsonl", "--format", help="Output format: jsonl or csv."),
    output: Optional[Path] = typer.Option(
        None, "--output", help="Write to a file instead of stdout."
    ),
    status: str = typer.Option(
        "done", "--status", help="Export photos with this status (done, error, all, ...)."
    ),
) -> None:
    """Export extraction results as JSONL or CSV."""
    cfg = _cfg()
    if fmt not in ("jsonl", "csv"):
        typer.echo(f"error: --format must be jsonl or csv, not {fmt!r}", err=True)
        raise typer.Exit(2)
    conn = db.connect(cfg.db_path)
    rows = db.export_rows(conn, status=status)
    records = [_export_record(r) for r in rows]
    if output is not None:
        output = output.expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        out = output.open("w", encoding="utf-8", newline="")
    else:
        out = sys.stdout
    try:
        if fmt == "jsonl":
            for record in records:
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
        else:
            fieldnames = list(records[0].keys()) if records else list(_EXPORT_FIELDS)
            writer = csv.DictWriter(out, fieldnames=fieldnames)
            writer.writeheader()
            for record in records:
                writer.writerow(record)
    finally:
        if output is not None:
            out.close()
    if output is not None:
        typer.echo(f"wrote {len(records)} record(s) to {output}")


@app.command()
def retry() -> None:
    """Requeue photos that previously failed."""
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    n = db.requeue_all_errors(conn)
    typer.echo(f"requeued {n} failed photo(s)")


@app.command()
def hide(ids: List[int]) -> None:
    """Hide photos from views and search (data kept; `unhide` reverses it)."""
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    n = db.hide_photos(conn, ids, hidden=True)
    typer.echo(f"hid {n} photo(s)")


@app.command()
def unhide(ids: List[int]) -> None:
    """Make hidden photos visible again."""
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    n = db.hide_photos(conn, ids, hidden=False)
    typer.echo(f"unhid {n} photo(s)")


@app.command()
def delete(ids: List[int]) -> None:
    """Move photos to the trash.

    The catalog remembers the deletion: rescans will not re-add them. The
    photo files on disk are never touched.
    """
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    n = db.trash_photos(conn, ids)
    typer.echo(f"moved {n} photo(s) to the trash (review with: phototext trash)")


@app.command()
def restore(ids: List[int]) -> None:
    """Restore photos from the trash."""
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    n = db.restore_photos(conn, ids)
    typer.echo(f"restored {n} photo(s)")


@app.command()
def purge(ids: List[int]) -> None:
    """Forget photos permanently: rows, locations, and search entries.

    A later rescan of the same files registers them as new photos. Files are
    never touched.
    """
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    n = db.purge_photos(conn, ids)
    typer.echo(f"purged {n} photo(s)")


@app.command()
def unscan(
    source: str = typer.Argument(
        ..., help="Source id (integer, see `phototext status`) or its exact path."
    ),
) -> None:
    """Unregister a source and forget photos that were only seen there.

    Photos whose content also exists in other sources keep their remaining
    locations. Files on disk are never touched.
    """
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    candidates = []
    if source.isdigit():
        candidates = [r for r in db.get_sources(conn) if str(r["id"]) == source]
    else:
        want = norm_path(source)
        candidates = [r for r in db.get_sources(conn) if norm_path(r["uri"]) == want]
    if not candidates:
        typer.echo("no matching source. Registered sources:", err=True)
        for row in db.get_sources(conn):
            typer.echo(f"  [{row['id']}] [{row['kind']}] {row['uri']}", err=True)
        raise typer.Exit(2)
    if len(candidates) > 1:
        typer.echo("error: ambiguous source; use the numeric id", err=True)
        raise typer.Exit(2)
    row = candidates[0]
    typer.echo(f"unscanning [{row['id']}] {row['uri']}")
    result = db.remove_source(conn, row["id"])
    typer.echo(
        f"removed {result['locations']} location(s); "
        f"forgotten {result['forgotten']} photo(s) seen only there"
    )
    typer.echo(f"catalog: {_counts_summary(db.status_counts(conn))}")


@app.command()
def trash() -> None:
    """List photos in the trash."""
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    rows = db.trash_list(conn)
    if not rows:
        typer.echo("trash is empty")
        return
    typer.echo(f"{len(rows)} photo(s) in the trash:")
    for row in rows:
        name = (row["path"] or "").rsplit("/", 1)[-1] or "(no location)"
        snippet = (row["text"] or "").replace("\n", " ").strip()[:40]
        typer.echo(f"  [{row['id']}] {row['deleted_at']}  {name}  {snippet}")
    ids = " ".join(str(r["id"]) for r in rows)
    typer.echo(f"restore with: phototext restore <id>...    purge with: phototext purge <id>...")


@app.command()
def autocomplete(
    shell: Optional[str] = typer.Argument(
        None, help="Shell to install for: bash or zsh (default: your login shell)."
    ),
) -> None:
    """Install TAB completion for phototext into your shell profile."""
    import os

    from typer.completion import install

    target = (shell or os.path.basename(os.environ.get("SHELL") or "")).lower()
    if target not in ("bash", "zsh"):
        if shell or target:
            what = f"unsupported shell: {shell or target}"
        else:
            what = "could not detect your shell"
        typer.echo(f"error: {what} — pass 'zsh' or 'bash' explicitly", err=True)
        raise typer.Exit(2)
    _shell, path = install(shell=target, prog_name="phototext")
    typer.echo(f"{_shell} completion installed: {path}")
    typer.echo("restart your terminal (or open a new shell) to activate it")


@app.command()
def profiles() -> None:
    """List named profiles (alternate catalogs) with their photo counts."""
    base = _base_config_path().parent / "profiles"
    if not base.is_dir():
        typer.echo("no profiles yet — start one with: phototext --profile <name> scan <path>")
        return
    active = _state.get("profile") or ""
    shown = False
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        shown = True
        mark = "*" if d.name == active else " "
        dbp = d / "catalog.db"
        if dbp.exists():
            con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
            try:
                row = con.execute(
                    "SELECT COUNT(*), COALESCE(SUM(status='done'), 0) "
                    "FROM photos WHERE deleted_at IS NULL"
                ).fetchone()
                typer.echo(f"{mark} {d.name}: {row[0]} photo(s), {row[1]} done")
            finally:
                con.close()
        else:
            typer.echo(f"{mark} {d.name}: (no catalog yet)")
    if not shown:
        typer.echo("no profiles yet — start one with: phototext --profile <name> scan <path>")


@app.command()
def help(
    command: Optional[str] = typer.Argument(
        None, metavar="COMMAND", help="Show detailed help for this command."
    ),
) -> None:
    """Show a usage guide with typical workflows."""
    if command:
        app(args=[command, "--help"])
        return
    typer.echo(USAGE_GUIDE)


@app.command()
def categories() -> None:
    """List the photo categories the models assigned, with counts.

    Categories are chosen by the model from what each photo shows; use them
    with `phototext results --category X`, `phototext search --category X`,
    or the web UI's category filter.
    """
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    rows = conn.execute(
        "SELECT category, COUNT(*) AS n FROM photos "
        "WHERE category IS NOT NULL GROUP BY category ORDER BY n DESC, category"
    ).fetchall()
    if not rows:
        typer.echo("no categories yet (process some photos first)")
        return
    total = sum(r["n"] for r in rows)
    typer.echo(f"{len(rows)} category value(s) across {total} photo(s):")
    for row in rows:
        typer.echo(f"  {row['n']:>5}  {row['category']}")


@app.command("_worker", hidden=True)
def _worker(
    model: Optional[str] = typer.Option(None, "--model", help="Model override."),
    persistent: bool = typer.Option(
        False, "--persistent", help="Keep running after the queue drains (watch mode)."
    ),
    no_idle_detection: bool = typer.Option(
        False, "--no-idle-detection", help="Do not pause for foreign models."
    ),
) -> None:
    """Internal: one worker process for --workers mode."""
    cfg = _cfg()
    if no_idle_detection:
        cfg = replace(cfg, idle_detection=False)
    raise typer.Exit(worker.worker_child(cfg, model=model, persistent=persistent))


@app.command()
def memes(
    max_distance: int = typer.Option(
        8, "--max-distance", help="Max hamming distance between 64-bit hashes to group photos."
    ),
    min_size: int = typer.Option(
        2, "--min-size", help="Smallest group to report."
    ),
) -> None:
    """Find likely memes: clusters of near-identical photos that contain text."""
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    computed = ensure_hashes(conn)
    if computed:
        typer.echo(f"computed perceptual hashes for {computed} photo(s)")
    groups = find_clusters(conn, max_distance=max_distance, min_size=min_size)
    if not groups:
        typer.echo("no meme-like groups found (no repeated images within distance)")
        return
    typer.echo(f"{len(groups)} meme-like group(s):")
    for group in groups:
        with_text = next(
            (r for r in group if r["has_text"] and (r["text"] or "").strip()), None
        )
        if with_text:
            snippet = with_text["text"].replace("\n", " ").strip()[:60]
        else:
            snippet = "(no text extracted)"
        typer.echo(f"  group: {len(group)} photo(s) - \"{snippet}\"")
        for row in group[:5]:
            name = (row["path"] or "").rsplit("/", 1)[-1] or "(no location)"
            typer.echo(f"    [{row['id']}] {name}")
        if len(group) > 5:
            typer.echo(f"    ... and {len(group) - 5} more")


def _load_reprocess_ids(path: Path) -> tuple[list[int], set[str]]:
    ids: list[int] = []
    paths: set[str] = set()
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            ids.append(int(line))
        except ValueError:
            paths.add(norm_path(line))
    if not ids and not paths:
        typer.echo(f"error: ids file {path} contains no ids", err=True)
        raise typer.Exit(2)
    return ids, paths


@app.command()
def reprocess(
    model: Optional[str] = typer.Option(
        None, "--model", help="Suggested model for the re-extraction (run with --model)."
    ),
    errors: bool = typer.Option(False, "--errors", help="Requeue failed photos."),
    no_text: bool = typer.Option(False, "--no-text", help="Requeue done photos with no text."),
    tiled: bool = typer.Option(
        False, "--tiled", help="Requeue photos extracted via the tiling fallback (dense)."
    ),
    gated: bool = typer.Option(
        False,
        "--gated",
        help="Requeue photos that finished at the cheap prefilter tier (redo with the full model).",
    ),
    derivative: bool = typer.Option(
        False,
        "--derivative",
        help="Requeue photos extracted from preview thumbnails (redo after originals download).",
    ),
    all_photos: bool = typer.Option(False, "--all", help="Requeue every photo."),
    ids_file: Optional[Path] = typer.Option(
        None, "--ids-file", help="File with one photo id (integer) or path per line."
    ),
) -> None:
    """Requeue selected photos for re-extraction, then run `phototext run`.

    Text is re-extracted and replaced on completion; until then the previous
    results stay in place.
    """
    cfg = _cfg()
    selectors = [errors, no_text, tiled, gated, derivative, all_photos, ids_file is not None]
    if sum(1 for s in selectors if s) != 1:
        typer.echo(
            "error: choose exactly one selector: --errors, --no-text, --tiled, "
            "--gated, --derivative, --all, or --ids-file",
            err=True,
        )
        raise typer.Exit(2)
    if ids_file is not None and not Path(ids_file).expanduser().is_file():
        typer.echo(f"error: ids file not found: {ids_file}", err=True)
        raise typer.Exit(2)
    conn = db.connect(cfg.db_path)
    if ids_file is not None:
        ids, paths = _load_reprocess_ids(Path(ids_file).expanduser())
        photo_ids = ids + db.photo_ids_for_paths(conn, paths)
    else:
        photo_ids = db.select_photo_ids(
            conn,
            errors=errors,
            no_text=no_text,
            tiled=tiled,
            gated=gated,
            derivative=derivative,
            all_photos=all_photos,
        )
    n = db.requeue_photos(conn, photo_ids)
    if n == 0:
        typer.echo("no matching photos to requeue")
        return
    typer.echo(f"requeued {n} photo(s)")
    hint = "phototext run"
    if model:
        hint += f" --model {model}"
    typer.echo(f"process them with: {hint}")


@app.command()
def serve(
    host: str = typer.Option(
        "127.0.0.1", "--host", help="Interface to bind (default: loopback only)."
    ),
    port: int = typer.Option(
        8765, "--port", help="Port to listen on (0 picks a free one)."
    ),
    writable: bool = typer.Option(
        False,
        "--writable",
        help="Enable write actions in the web UI (hide/delete/restore/purge), "
        "protected by a session token. Default: strictly read-only.",
    ),
) -> None:
    """Serve the read-only local web UI: browse photos, recovered text, and search."""
    cfg = _cfg()
    try:
        webui.serve(cfg.db_path, host=host, port=port, writable=writable, person_cfg=cfg)
    except FileNotFoundError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)


def _person_by_name(conn, name: str):
    person = db.get_person_by_name(conn, name)
    if person is None:
        typer.echo(f"error: no person named '{name}' (see `phototext people list`)", err=True)
        raise typer.Exit(2)
    return person


def _name_seed(cfg, photo_id: int, name: str, box_text: str | None) -> None:
    conn = db.connect(cfg.db_path)
    photo = db.get_photo(conn, photo_id)
    if photo is None or photo["deleted_at"]:
        typer.echo(f"error: no photo with id {photo_id}", err=True)
        raise typer.Exit(2)
    if not name.strip() or len(name) > people_mod.PERSON_MAX_NAME:
        typer.echo(
            f"error: name must be 1-{people_mod.PERSON_MAX_NAME} characters", err=True
        )
        raise typer.Exit(2)
    try:
        box = people_mod.parse_box(box_text)
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)
    source = db.find_first_existing_location(conn, photo_id)
    if source is None:
        typer.echo(
            f"error: photo {photo_id} has no readable file on disk "
            "(moved, deleted, or iCloud-only)",
            err=True,
        )
        raise typer.Exit(2)
    person = db.upsert_person(conn, name)
    crop = people_mod.save_seed_crop(cfg.db_path, person["id"], photo_id, Path(source), box)
    if crop is None:
        typer.echo("warning: could not save a face crop (unreadable image)", err=True)
    db.tag_person(conn, photo_id, person["id"], 1.0, "seed", box_text if box else None)
    typer.echo(f"person '{person['name']}' seeded from photo {photo_id}")
    try:
        description = people_mod.build_description(
            conn, cfg.db_path, person["id"], cfg
        )
    except Exception as e:
        typer.echo(
            f"warning: could not build a recognition profile yet ({e}); "
            "retry with `phototext people describe "
            f"{person['name']}` or `phototext people run`",
            err=True,
        )
        return
    typer.echo(f"recognition profile: {description}")


@people_app.command("name")
def people_name(
    photo_id: int = typer.Argument(..., help="Photo id to seed the person from."),
    name: str = typer.Argument(..., help="Person name (existing name adds a seed)."),
    box: Optional[str] = typer.Option(
        None, "--box", help="Face region 'x,y,w,h' in original pixels; omit for the whole photo."
    ),
) -> None:
    """Name a person on a photo. Repeat with more photos to add seeds."""
    _name_seed(_cfg(), photo_id, name, box)


@people_app.command("describe")
def people_describe(
    name: str = typer.Argument(..., help="Person name."),
) -> None:
    """Rebuild a person's recognition profile from their seed photos."""
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    person = _person_by_name(conn, name)
    try:
        description = people_mod.build_description(
            conn, cfg.db_path, person["id"], cfg
        )
    except Exception as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(f"recognition profile: {description}")


@people_app.command("run")
def people_run(
    model: Optional[str] = typer.Option(
        None, "--model", help="Override the person matching model for this run."
    ),
    person: Optional[List[str]] = typer.Option(
        None, "--person", help="Only match this person (repeatable)."
    ),
    stop_after: Optional[str] = typer.Option(
        None, "--stop-after", help="Stop after a duration, e.g. 45s, 90m, 2h, 1h30m."
    ),
    limit: Optional[int] = typer.Option(
        None, "--limit", help="Evaluate at most N photos this run."
    ),
) -> None:
    """Tag people across the library (one model call per photo, all people)."""
    cfg = _cfg()
    if limit is not None and limit < 1:
        typer.echo("error: --limit must be at least 1", err=True)
        raise typer.Exit(2)
    try:
        code = people_mod.run_matching(
            cfg, model=model, person_names=person, stop_after=stop_after, limit=limit
        )
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)
    raise typer.Exit(code)


@people_app.command("list")
def people_list() -> None:
    """List people with their tag counts."""
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    people = db.people_list(conn, cfg.person_min_confidence)
    if not people:
        typer.echo(
            "no people yet — start with: phototext people name <photo-id> <name> --box x,y,w,h"
        )
        return
    for person in people:
        typer.echo(
            f"  [{person['id']}] {person['name']}: {person['tags']} tag(s) — "
            f"{person['confirmed']} confirmed, {person['strong']} confident, "
            f"{person['uncertain']} to review"
        )
        if not person["description"]:
            typer.echo("        no recognition profile yet (run `phototext people run`)")


@people_app.command("photos")
def people_photos(
    name: str = typer.Argument(..., help="Person name."),
    limit: int = typer.Option(50, "--limit", help="Maximum photos to show."),
) -> None:
    """Show a person's tagged photos with confidence."""
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    person = _person_by_name(conn, name)
    rows = db.person_tag_rows(conn, person["id"])
    if not rows:
        typer.echo(f"no visible photos tagged '{person['name']}'")
        return
    typer.echo(
        f"{len(rows)} photo(s) tagged '{person['name']}' "
        f"(threshold {cfg.person_min_confidence:.2f}):"
    )
    for row in rows[:limit]:
        mark = row["origin"]
        if row["origin"] == "model":
            mark = (
                f"model {row['confidence']:.2f}"
                + ("  <-- review" if row["confidence"] < cfg.person_min_confidence else "")
            )
        name_of = (row["path"] or "").rsplit("/", 1)[-1] or "(no location)"
        typer.echo(f"  [{row['id']}] {mark:<14} {name_of}")
    if len(rows) > limit:
        typer.echo(f"  ... and {len(rows) - limit} more")


@people_app.command("confirm")
def people_confirm(
    photo_id: int = typer.Argument(..., help="Photo id."),
    name: str = typer.Argument(..., help="Person name."),
) -> None:
    """Mark a tag as correct (ground truth; model runs will not change it)."""
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    person = _person_by_name(conn, name)
    db.confirm_person_tag(conn, photo_id, person["id"])
    typer.echo(f"confirmed '{person['name']}' on photo {photo_id}")


@people_app.command("remove")
def people_remove(
    photo_id: int = typer.Argument(..., help="Photo id."),
    name: str = typer.Argument(..., help="Person name."),
) -> None:
    """Remove a person tag from a photo."""
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    person = _person_by_name(conn, name)
    db.untag_person(conn, photo_id, person["id"])
    typer.echo(f"removed '{person['name']}' from photo {photo_id}")


@people_app.command("rename")
def people_rename(
    old: str = typer.Argument(..., help="Current name."),
    new: str = typer.Argument(..., help="New name."),
) -> None:
    """Rename a person (tags move with them)."""
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    person = _person_by_name(conn, old)
    if not new.strip() or len(new) > people_mod.PERSON_MAX_NAME:
        typer.echo(
            f"error: name must be 1-{people_mod.PERSON_MAX_NAME} characters", err=True
        )
        raise typer.Exit(2)
    if db.get_person_by_name(conn, new) is not None:
        typer.echo(f"error: a person named '{new}' already exists", err=True)
        raise typer.Exit(2)
    db.rename_person(conn, person["id"], new)
    typer.echo(f"renamed '{person['name']}' -> '{new.strip()}'")


@people_app.command("reset")
def people_reset(
    name: str = typer.Argument(..., help="Person name."),
) -> None:
    """Reset a person's model tags (keeps seeds and confirmed tags)."""
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    person = _person_by_name(conn, name)
    n = db.reset_person_tags(conn, person["id"])
    typer.echo(f"cleared {n} model tag(s) for '{person['name']}'")


@people_app.command("delete")
def people_delete(
    name: str = typer.Argument(..., help="Person name."),
    yes: bool = typer.Option(False, "--yes", help="Actually delete (safety flag)."),
) -> None:
    """Delete a person and all their tags (seed crops are removed too)."""
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    person = _person_by_name(conn, name)
    if not yes:
        n_tags = conn.execute(
            "SELECT COUNT(*) FROM person_tags WHERE person_id = ?", (person["id"],)
        ).fetchone()[0]
        typer.echo(
            f"refusing to delete '{person['name']}' without --yes "
            f"({n_tags} tag(s) would be removed)"
        )
        raise typer.Exit(2)
    db.delete_person(conn, person["id"])
    shutil.rmtree(people_mod.person_dir(cfg.db_path) / str(person["id"]), ignore_errors=True)
    typer.echo(f"deleted person '{person['name']}'")


@app.command()
def migrate(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show pending migrations without applying them."
    ),
) -> None:
    """Apply pending catalog schema migrations, backing up the catalog first."""
    cfg = _cfg()
    db_path = Path(cfg.db_path).expanduser()
    typer.echo(f"catalog: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        current = db.schema_version(conn)
        pending = db.pending_migrations(conn)
        if not pending:
            typer.echo(f"schema: v{current} (up to date)")
            return
        typer.echo(
            f"schema: v{current}, pending migration(s): "
            + ", ".join(f"v{v}" for v in pending)
        )
        if dry_run:
            typer.echo("dry run: nothing applied")
            return
        backup = db.backup_catalog(db_path)
        if backup is not None:
            typer.echo(f"backup: {backup}")
        try:
            applied = db.migrate(conn)
        except Exception as e:
            typer.echo(f"error: migration failed: {e}", err=True)
            if backup is not None:
                typer.echo(f"a pre-migration backup is at {backup}", err=True)
            raise typer.Exit(1)
        typer.echo(f"migrated: v{current} -> v{max(applied)}")
    finally:
        conn.close()


@app.command()
def doctor() -> None:
    """Check database, Ollama server, model availability, and vision support."""
    cfg = _cfg()
    bad = 0

    def report(passed: bool, label: str, detail: str = "") -> None:
        nonlocal bad
        mark = "PASS" if passed else "FAIL"
        typer.echo(f"  [{mark}] {label}" + (f" -- {detail}" if detail else ""))
        if not passed:
            bad += 1

    typer.echo(f"phototext {_version()}")
    typer.echo(f"database: {cfg.db_path}")
    conn = db.connect(cfg.db_path)
    counts = db.status_counts(conn)
    report(True, "database readable", _counts_summary(counts))
    typer.echo(f"ollama: {cfg.ollama_url}  model: {cfg.model}")
    client = OllamaClient(cfg)
    try:
        models = client.check_connection()
        report(True, "server reachable", f"{len(models)} model(s) installed")
    except Exception as e:
        report(False, "server reachable", f"cannot connect ({e}); is `ollama serve` running?")
        raise typer.Exit(1)
    if cfg.model in models:
        report(True, "model present", cfg.model)
    else:
        available = ", ".join(sorted(m for m in models if m)) or "none"
        report(
            False,
            "model present",
            f"'{cfg.model}' not installed (available: {available}); "
            f"run `ollama pull {cfg.model}` or edit the config",
        )
        raise typer.Exit(1)
    t0 = time.monotonic()
    try:
        client.extract(test_image_b64())
        report(True, "vision extraction works", f"{time.monotonic() - t0:.1f}s on a test image")
    except Exception as e:
        report(False, "vision extraction works", f"{e}; is '{cfg.model}' a vision model?")
    report(shutil.which("sips") is not None, "sips fallback available")
    if bad:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
