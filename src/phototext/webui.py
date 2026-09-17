"""Read-only local web UI over the phototext catalog.

Serves a browse/search interface with `http.server` (stdlib only) and a
strictly read-only SQLite connection (`mode=ro` + `PRAGMA query_only`).
Thumbnails are generated with the same imaging pipeline as the worker and
cached at `<db_dir>/thumbs/<photo id>.jpg` (photo ids are content-hash stable,
so the cache never goes stale).
"""

from __future__ import annotations

import html
import re
import secrets
import shutil
import sqlite3
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import pathname2url

from . import db
from .config import ensure_noindex
from .imaging import ImageReadError, prepare_image
from .memes import find_clusters

PAGE_SIZE = 48
THUMB_EDGE = 480

_CONTENT_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "jfif": "image/jpeg",
    "png": "image/png",
    "heic": "image/heic",
    "heif": "image/heif",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "gif": "image/gif",
    "jp2": "image/jp2",
    "psd": "image/vnd.adobe.photoshop",
    "pict": "image/pict",
    "dng": "image/dng",
}


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _open_ro(db_path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(f"file:{pathname2url(str(db_path))}?mode=ro", uri=True)
    except sqlite3.Error:
        conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        conn.execute("PRAGMA query_only=ON")
    except sqlite3.Error:
        pass
    return conn


_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.5 -apple-system, "Segoe UI", sans-serif;
       background: #14161a; color: #e6e6e6; }
header { padding: 14px 20px; background: #1c2027; border-bottom: 1px solid #2a2f39;
         display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
header h1 { font-size: 16px; margin: 0; color: #8ab4f8; }
header form { display: flex; gap: 6px; flex: 1; min-width: 260px; }
input[type=text] { flex: 1; padding: 6px 10px; border-radius: 6px; border: 1px solid #2a2f39;
                   background: #14161a; color: #e6e6e6; }
button { padding: 6px 14px; border-radius: 6px; border: 0; background: #2a5db0;
         color: white; cursor: pointer; }
nav { padding: 10px 20px; display: flex; gap: 10px; flex-wrap: wrap; font-size: 13px; }
nav a { color: #9aa4b2; text-decoration: none; padding: 3px 10px; border-radius: 12px; }
nav a.on { background: #2a5db0; color: white; }
main { padding: 0 20px 40px; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
         gap: 14px; margin-top: 12px; }
.card { background: #1c2027; border-radius: 8px; overflow: hidden; text-decoration: none;
        color: inherit; display: flex; flex-direction: column; }
.card img { width: 100%; height: 150px; object-fit: cover; background: #0d0e11;
            display: block; }
.card .body { padding: 8px 10px 10px; font-size: 12.5px; }
.card .path { color: #9aa4b2; font-size: 11px; word-break: break-all; margin-top: 4px; }
.snippet { max-height: 5.4em; overflow: hidden; }
.muted { color: #8b949e; }
.badge { display: inline-block; font-size: 10.5px; padding: 1px 7px; margin-right: 5px;
         border-radius: 8px; background: #2a2f39; color: #9aa4b2; }
.badge.err { background: #5c1f1f; color: #ffb4b4; }
.badge.ok { background: #1e3a24; color: #a8e2b8; }
.pager { margin: 18px 0; display: flex; gap: 10px; }
.pager a { color: #8ab4f8; text-decoration: none; }
.detail { display: flex; gap: 24px; margin-top: 16px; flex-wrap: wrap; }
.detail img { max-width: min(680px, 100%); max-height: 78vh; border-radius: 8px;
              background: #0d0e11; }
.meta { flex: 1; min-width: 300px; }
.meta table { border-collapse: collapse; font-size: 13px; margin-bottom: 14px; }
.meta td { padding: 3px 12px 3px 0; vertical-align: top; }
.meta td:first-child { color: #9aa4b2; white-space: nowrap; }
pre { background: #1c2027; padding: 12px; border-radius: 8px; white-space: pre-wrap;
      font-size: 13px; max-width: 100%; overflow-wrap: anywhere; }
details { margin-top: 12px; }
ul.locs { font-size: 12.5px; color: #c7cdd6; padding-left: 18px; }
ul.locs li { margin: 10px 0; }
ul.locs a { color: #8ab4f8; }
ul.locs .muted { font-size: 11.5px; }
.actions { margin: 10px 0 4px; }
form.act { display: inline-block; margin-right: 8px; }
button.mini { padding: 4px 12px; border-radius: 6px; border: 1px solid #2a2f39;
             background: #1c2027; color: #e6e6e6; font-size: 12px; cursor: pointer; }
button.mini:hover { background: #2a2f39; }
.trow { display: flex; gap: 12px; align-items: flex-start; background: #1c2027;
        border-radius: 8px; padding: 8px; margin-bottom: 10px; }
.trow img { width: 110px; height: 82px; object-fit: cover; border-radius: 6px;
            background: #0d0e11; }
.tbody { flex: 1; font-size: 13px; }
.note { color: #9aa4b2; margin: 12px 0; }
a.back { color: #8ab4f8; text-decoration: none; }
"""


def _page(title: str, body: str) -> bytes:
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{esc(title)}</title><style>{_CSS}</style></head>"
        f"<body>{body}</body></html>"
    )
    return doc.encode("utf-8")


def _header(conn: sqlite3.Connection, q: str = "") -> str:
    counts = db.status_counts(conn)
    return (
        "<header><h1>phototext</h1>"
        "<form action='/' method='get'>"
        "<input type='text' name='q' placeholder='Search recovered text and context...'"
        f" value='{esc(q)}'><button>Search</button></form>"
        f"<span class='muted'>{counts.get('total', 0)} photos "
        f"(&#10003; {counts.get('done', 0)} &#8987; {counts.get('queued', 0)} "
        f"&#10007; {counts.get('error', 0)})</span></header>"
    )


def _status_tabs(status: str, memes_active: bool = False) -> str:
    parts = []
    for key, label in (
        ("all", "All"),
        ("done", "Done"),
        ("queued", "Queued"),
        ("error", "Errors"),
        ("deferred", "Deferred"),
    ):
        cls = " class='on'" if status == key else ""
        parts.append(f"<a{cls} href='/?status={key}'>{label}</a>")
    memes_cls = " class='on'" if memes_active else ""
    parts.append(f"<a{memes_cls} href='/memes'>Memes</a>")
    parts.append("<a href='/trash'>Trash</a>")
    return f"<nav>{''.join(parts)}</nav>"


def _action_form(
    action: str, photo_id: int, label: str, token: str, confirm: str | None = None
) -> str:
    onsubmit = f" onsubmit=\"return confirm('{esc(confirm)}')\"" if confirm else ""
    return (
        f"<form class='act' method='post' action='/{action}/{photo_id}'{onsubmit}>"
        f"<input type='hidden' name='token' value='{esc(token)}'>"
        f"<button class='mini'>{esc(label)}</button></form>"
    )


def _snippet(text: str, limit: int = 220) -> str:
    flat = " ".join((text or "").split())
    if len(flat) > limit:
        flat = flat[:limit].rsplit(" ", 1)[0] + " ..."
    return flat


def _card(row, snippet: str) -> str:
    status = row["status"]
    badge = (
        f"<span class='badge err'>{esc(status)}</span>"
        if status == "error"
        else f"<span class='badge ok'>{esc(status)}</span>"
        if status == "done"
        else f"<span class='badge'>{esc(status)}</span>"
    )
    if status == "error":
        snippet_html = f"<span class='muted'>{esc(row['error'] or 'error')}</span>"
    elif snippet:
        snippet_html = f"<div class='snippet'>{esc(snippet)}</div>"
    else:
        snippet_html = "<span class='muted'>(no text)</span>"
    kind = row["text_kind"] or ""
    language = row["language"] or ""
    category = row["category"] or ""
    path = row["path"] or ""
    name = path.rsplit("/", 1)[-1] if path else "(no location)"
    tile_badge = "<span class='badge'>tiled</span>" if row["tiled"] else ""
    gate_badge = "<span class='badge'>gated</span>" if row["gated"] else ""
    deriv_badge = ("<span class='badge'>preview</span>"
                   if row["derivative"] else "")
    return (
        f"<a class='card' href='/photo/{row['id']}'>"
        f"<img src='/thumb/{row['id']}' alt='{esc(name)}' loading='lazy'>"
        "<div class='body'>"
        f"{badge}{tile_badge}{gate_badge}{deriv_badge}"
        f"<span class='badge'>{esc(category or kind)}</span>"
        f"{snippet_html}"
        f"<div class='path'>{esc(name)}</div>"
        "</div></a>"
    )


def _pager(base: str, page: int, total: int) -> str:
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if pages <= 1:
        return ""
    parts = []
    if page > 1:
        parts.append(f"<a href='{base}page={page - 1}'>&#8592; newer</a>")
    parts.append(f"<span class='muted'>page {page} / {pages}</span>")
    if page < pages:
        parts.append(f"<a href='{base}page={page + 1}'>older &#8594;</a>")
    return f"<div class='pager'>{''.join(parts)}</div>"


def render_list(conn: sqlite3.Connection, params: dict, ctx: dict | None = None) -> bytes:
    q = (params.get("q", [""])[0] or "").strip()
    try:
        page = max(1, int(params.get("page", ["1"])[0] or 1))
    except ValueError:
        page = 1
    offset = (page - 1) * PAGE_SIZE
    cards = []
    if q:
        try:
            rows, _effective = db.search_photos(
                conn, q, limit=PAGE_SIZE, offset=offset, highlight=("", "")
            )
            total = db.search_count(conn, q)
        except sqlite3.OperationalError:
            body = (
                _header(conn)
                + f"<main><p class='note'>Search is unavailable: the catalog schema is "
                "older than this build. Run <code>phototext migrate</code> (or any CLI "
                "command) once, then reload.</p></main>"
            )
            return _page("phototext", body)
        note = f"{total} match(es) for '{q}'"
        if total > len(rows) + offset:
            note += f" — showing {offset + 1}&ndash;{offset + len(rows)}"
        cards = [_card(r, r["text_snip"] or "") for r in rows]
        base = "/?" + urlencode({"q": q}) + "&"
        body = _header(conn, q) + f"<main><p class='note'>{esc(note)}</p>"
    else:
        status = params.get("status", ["all"])[0] or "all"
        if status not in ("all", "done", "queued", "error", "processing"):
            status = "all"
        category = (params.get("category", [""])[0] or "").strip() or None
        text_filter = params.get("text", ["all"])[0] or "all"
        if text_filter not in ("all", "yes", "no"):
            text_filter = "all"
        has_text = None if text_filter == "all" else (text_filter == "yes")
        show_hidden = params.get("hidden", [""])[0] == "1"
        rows, total = db.page_photos(
            conn, status=status, has_text=has_text, category=category,
            limit=PAGE_SIZE, offset=offset, show_hidden=show_hidden,
        )
        cards = [_card(r, _snippet(r["text"] or "")) for r in rows]

        def qs() -> str:
            parts = {
                "status": status,
                "category": category,
                "text": text_filter,
                "hidden": "1" if show_hidden else "",
            }
            query = urlencode({k: v for k, v in parts.items() if v and v != "all"})
            return f"/?{query}&" if query else "/?"

        base = qs()
        body = _header(conn) + _status_tabs(status)
        hidden_chip = (
            "<a href='/'>hide hidden</a>" if show_hidden else "<a href='/?hidden=1'>show hidden</a>"
        )
        body += f"<nav>{hidden_chip}</nav>"
        cats = [
            r["category"]
            for r in conn.execute(
                "SELECT DISTINCT category FROM photos WHERE category IS NOT NULL "
                "ORDER BY category"
            )
        ]
        if cats:
            chips = [
                f"<a{cls} href='/?status={status}&text={text_filter}&category={esc(c)}'>{esc(c)}</a>"
                for c in cats
                for cls in (" class='on'" if c == category else "",)
            ]
            chips.append(f"<a href='/?status={status}&text={text_filter}'>all categories</a>")
            body += f"<nav>{''.join(chips)}</nav>"
        text_chips = []
        for value, label in (("all", "any text"), ("yes", "with text"), ("no", "no text")):
            cls = " class='on'" if text_filter == value else ""
            text_chips.append(f"<a{cls} href='/?status={status}&text={value}'>{label}</a>")
        body += f"<nav>{''.join(text_chips)}</nav>"
        body += f"<main><p class='note'>{total} photo(s) with status '{status}'"
        if category:
            body += f" and category '{esc(category)}'"
        if text_filter != "all":
            body += f" ({'with' if text_filter == 'yes' else 'no'} text)"
        body += "</p>"
    body += "<div class='cards'>" + "".join(cards) + "</div>"
    body += _pager(base, page, total)
    body += "</main>"
    return _page("phototext", body)


def render_detail(conn: sqlite3.Connection, photo_id: int, ctx: dict | None = None) -> bytes | None:
    row = db.get_photo(conn, photo_id)
    if row is None:
        return None
    locs = db.photo_locations(conn, photo_id)
    rows_meta = [
        ("status", row["status"]),
        ("kind", row["text_kind"] or "?"),
        ("category", row["category"] or "?"),
        ("language", row["language"] or "?"),
        ("has text", "yes" if row["has_text"] else "no"),
        ("model", row["model"] or "?"),
        (
            "extraction",
            "gate tier (prefilter)"
            if row["gated"]
            else "tiled (4 quadrants)"
            if row["tiled"]
            else "single pass",
        ),
        ("duration", f"{row['duration_ms'] / 1000.0:.1f}s" if row["duration_ms"] else "?"),
        ("first seen", row["first_seen_at"] or "?"),
        ("finished", row["finished_at"] or "?"),
        ("sha256", (row["sha256"] or "")[:16] + " ..."),
        ("size", f"{row['byte_size']:,} bytes" if row["byte_size"] else "?"),
    ]
    if row["error"]:
        rows_meta.append(("error", row["error"]))
    meta = "".join(f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in rows_meta)
    actions = ""
    if ctx and ctx.get("writable") and ctx.get("token"):
        token = ctx["token"]
        if row["deleted_at"]:
            actions = (
                f"<p class='muted'>in trash since {esc(row['deleted_at'])}</p>"
                + _action_form("restore", photo_id, "restore", token)
                + _action_form(
                    "purge", photo_id, "purge (forget forever)", token,
                    confirm="Forget this photo permanently? Files on disk are not touched.",
                )
            )
        else:
            actions = (
                _action_form(
                    "unhide" if row["hidden"] else "hide", photo_id,
                    "unhide" if row["hidden"] else "hide", token,
                )
                + _action_form(
                    "delete", photo_id, "move to trash", token,
                    confirm="Move this photo to the trash? Files on disk are not touched.",
                )
            )
    loc_items = []
    for loc in locs:
        kind = loc["source_kind"] or "source"
        exists = Path(loc["path"]).exists()
        disk = (
            "<span class='badge ok'>on disk</span>"
            if exists
            else "<span class='badge err'>missing</span>"
        )
        reveal = f"/reveal/{photo_id}?loc={loc['id']}"
        loc_items.append(
            f"<li>{disk} <span class='badge'>{esc(kind)}</span> "
            f"<a href='{reveal}'>reveal in Finder</a><br>"
            f"{esc(loc['path'])}<br>"
            f"<span class='muted'>from {esc(loc['source_uri'] or '?')}</span></li>"
        )
    loc_list = "".join(loc_items)
    text = row["text"] or ""
    context = row["context"] or ""
    raw = row["raw_response"] or ""
    body = _header(conn) + "<main><p><a class='back' href='/'>&#8592; back to photos</a></p>"
    if actions:
        body += f"<div class='actions'>{actions}</div>"
    body += f"<div class='detail'>"
    body += f"<img src='/image/{photo_id}' alt='photo {photo_id}'>"
    body += "<div class='meta'>"
    body += f"<table>{meta}</table>"
    if text:
        body += f"<p class='muted'>recovered text</p><pre>{esc(text)}</pre>"
    if context:
        body += f"<p class='muted'>context</p><pre>{esc(context)}</pre>"
    if locs:
        body += f"<p class='muted'>where it is stored ({len(locs)} location(s))</p><ul class='locs'>{loc_list}</ul>"
    warns = db.photo_warnings_list(conn, photo_id)
    if warns:
        body += "<p class='muted'>warnings</p><ul class='locs'>" + "".join(
            f"<li><span class='badge err'>{esc(w['kind'])}</span> {esc(w['message'])}</li>"
            for w in warns
        ) + "</ul>"
    if raw:
        body += "<details><summary>raw model response</summary>" \
                f"<pre>{esc(raw)}</pre></details>"
    body += "</div></div></main>"
    return _page(f"photo {photo_id}", body)


def render_memes(conn: sqlite3.Connection, ctx: dict | None = None) -> bytes:
    try:
        clusters = find_clusters(conn)
    except sqlite3.OperationalError:
        clusters = []
    body = _header(conn) + _status_tabs("", memes_active=True)
    if not clusters:
        note = "no meme-like groups found"
        try:
            unhashed = conn.execute(
                "SELECT COUNT(*) AS n FROM photos WHERE phash IS NULL"
            ).fetchone()["n"]
        except sqlite3.OperationalError:
            unhashed = 0
        if unhashed:
            note += (
                f" — {unhashed} photo(s) have no perceptual hash yet; "
                "run `phototext memes` once to compute them"
            )
        body += f"<main><p class='note'>{esc(note)}</p></main>"
        return _page("memes - phototext", body)
    body += (
        "<main><p class='note'>" + esc(str(len(clusters))) + " meme-like group(s) "
        "— near-identical photos, often with overlaid text</p>"
    )
    for index, group in enumerate(clusters, 1):
        with_text = next(
            (r for r in group if r["has_text"] and (r["text"] or "").strip()), None
        )
        snippet = _snippet(with_text["text"]) if with_text else "(no text)"
        cards = "".join(_card(r, _snippet(r["text"] or "")) for r in group[:12])
        body += (
            f"<p class='muted'>group {index}: {len(group)} photo(s) — {esc(snippet)}</p>"
            f"<div class='cards'>{cards}</div>"
        )
    body += "</main>"
    return _page("memes - phototext", body)


def render_trash(conn: sqlite3.Connection, ctx: dict | None = None) -> bytes:
    rows = db.trash_list(conn)
    body = _header(conn) + _status_tabs("trash")
    if not rows:
        body += "<main><p class='note'>trash is empty</p></main>"
        return _page("trash - phototext", body)
    writable = bool(ctx and ctx.get("writable") and ctx.get("token"))
    body += f"<main><p class='note'>{len(rows)} photo(s) in the trash. "
    if not writable:
        body += "Start the server with --writable to restore or purge from here."
    body += "</p>"
    for row in rows:
        name = (row["path"] or "").rsplit("/", 1)[-1] or "(no location)"
        snippet = _snippet(row["text"] or "") or "<span class='muted'>(no text)</span>"
        buttons = ""
        if writable:
            token = ctx["token"]
            buttons = (
                _action_form("restore", row["id"], "restore", token)
                + _action_form(
                    "purge", row["id"], "purge", token,
                    confirm="Forget this photo permanently?",
                )
            )
        body += (
            f"<div class='trow'><a href='/photo/{row['id']}'>"
            f"<img src='/thumb/{row['id']}' alt='{esc(name)}' loading='lazy'></a>"
            f"<div class='tbody'><div class='path'>{esc(name)} "
            f"<span class='muted'>({esc(row['deleted_at'] or '')})</span></div>"
            f"<div class='snippet'>{snippet}</div>{buttons}</div></div>"
        )
    body += "</main>"
    return _page("trash - phototext", body)


def _first_existing(conn: sqlite3.Connection, photo_id: int) -> Path | None:
    path = db.find_first_existing_location(conn, photo_id)
    return Path(path) if path else None


def thumb_bytes(conn: sqlite3.Connection, photo_id: int, thumbs_dir: Path) -> bytes | None:
    cache = thumbs_dir / f"{photo_id}.jpg"
    if cache.exists():
        try:
            return cache.read_bytes()
        except OSError:
            pass
    path = _first_existing(conn, photo_id)
    if path is None:
        return None
    try:
        data = prepare_image(path, max_edge=THUMB_EDGE, jpeg_quality=72)
    except ImageReadError:
        return None
    try:
        thumbs_dir.mkdir(parents=True, exist_ok=True)
        ensure_noindex(thumbs_dir)
        cache.write_bytes(data)
    except OSError:
        pass
    return data


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address,
        handler,
        db_path: Path,
        thumbs_dir: Path,
        writable: bool = False,
        token: str | None = None,
    ):
        super().__init__(address, handler)
        self.phototext_db = db_path
        self.phototext_thumbs = thumbs_dir
        self.phototext_writable = writable
        self.phototext_token = token


class _Handler(BaseHTTPRequestHandler):
    server_version = "phototext-webui"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if ctype.startswith("text/html") else "max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: bytes) -> None:
        self._send(200, body, "text/html; charset=utf-8")

    def _not_found(self, msg: str = "not found") -> None:
        self._send(404, f"<html><body><p>{esc(msg)}</p></body></html>".encode(), "text/html; charset=utf-8")

    def _redirect(self) -> None:
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        try:
            self._route()
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._send(500, f"server error: {esc(e)}".encode(), "text/plain")
            except Exception:
                pass

    def do_POST(self) -> None:
        try:
            self._route_post()
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._send(500, f"server error: {esc(e)}".encode(), "text/plain")
            except Exception:
                pass

    def _route_post(self) -> None:
        route = urlsplit(self.path).path
        if not self.server.phototext_writable:
            self._not_found("server is read-only (restart with --writable to enable actions)")
            return
        match = re.match(r"^/(hide|unhide|delete|restore|purge)/(\d+)$", route)
        if match is None:
            self._not_found()
            return
        action, photo_id = match.group(1), int(match.group(2))
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        fields = parse_qs(body)
        token = self.headers.get("X-Phototext-Write") or (fields.get("token", [""])[0])
        if not token or token != self.server.phototext_token:
            self._send(403, b"forbidden: missing or invalid write token", "text/plain")
            return
        origin = self.headers.get("Origin")
        if origin:
            origin_host = urlsplit(origin).netloc
            if origin_host and origin_host != self.headers.get("Host"):
                self._send(403, b"forbidden: cross-origin write", "text/plain")
                return
        conn = db.connect(self.server.phototext_db)
        try:
            if action == "hide":
                db.hide_photos(conn, [photo_id], hidden=True)
                dest = f"/photo/{photo_id}"
            elif action == "unhide":
                db.hide_photos(conn, [photo_id], hidden=False)
                dest = f"/photo/{photo_id}"
            elif action == "delete":
                db.trash_photos(conn, [photo_id])
                dest = f"/photo/{photo_id}"
            elif action == "restore":
                db.restore_photos(conn, [photo_id])
                dest = f"/photo/{photo_id}"
            else:
                db.purge_photos(conn, [photo_id])
                dest = "/trash"
        finally:
            conn.close()
        self.send_response(303)
        self.send_header("Location", dest)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _route(self) -> None:
        parsed = urlsplit(self.path)
        route = parsed.path
        params = parse_qs(parsed.query)
        ctx = {
            "writable": self.server.phototext_writable,
            "token": self.server.phototext_token,
        }
        if route in ("", "/"):
            if "q" in params and not params["q"][0].strip():
                self._redirect()
                return
            conn = _open_ro(self.server.phototext_db)
            try:
                self._send_html(render_list(conn, params, ctx))
            finally:
                conn.close()
            return
        if route == "/memes":
            conn = _open_ro(self.server.phototext_db)
            try:
                self._send_html(render_memes(conn, ctx))
            finally:
                conn.close()
            return
        if route == "/trash":
            conn = _open_ro(self.server.phototext_db)
            try:
                self._send_html(render_trash(conn, ctx))
            finally:
                conn.close()
            return
        for prefix, handler in (
            ("/photo/", self._route_photo),
            ("/thumb/", self._route_thumb),
            ("/image/", self._route_image),
            ("/reveal/", self._route_reveal),
        ):
            if route.startswith(prefix):
                tail = route[len(prefix):]
                if not tail.isdigit():
                    self._not_found("bad photo id")
                    return
                handler(int(tail), params, ctx)
                return
        self._not_found()

    def _route_photo(self, photo_id: int, params: dict, ctx: dict) -> None:
        conn = _open_ro(self.server.phototext_db)
        try:
            body = render_detail(conn, photo_id, ctx)
        finally:
            conn.close()
        if body is None:
            self._not_found(f"photo {photo_id} not found")
        else:
            self._send_html(body)

    def _route_thumb(self, photo_id: int, params: dict, ctx: dict) -> None:
        conn = _open_ro(self.server.phototext_db)
        try:
            data = thumb_bytes(conn, photo_id, self.server.phototext_thumbs)
        finally:
            conn.close()
        if data is None:
            self._not_found("no preview")
        else:
            self._send(200, data, "image/jpeg")

    def _route_image(self, photo_id: int, params: dict, ctx: dict) -> None:
        conn = _open_ro(self.server.phototext_db)
        try:
            path = _first_existing(conn, photo_id)
        finally:
            conn.close()
        if path is None:
            self._not_found("original file not on disk")
            return
        try:
            size = path.stat().st_size
            with path.open("rb") as f:
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    _CONTENT_TYPES.get(path.suffix.lower().lstrip("."), "application/octet-stream"),
                )
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                shutil.copyfileobj(f, self.wfile)
        except OSError as e:
            self._not_found(f"cannot read original: {e}")

    def _route_reveal(self, photo_id: int, params: dict, ctx: dict) -> None:
        try:
            loc_id = int(params.get("loc", ["0"])[0])
        except ValueError:
            loc_id = 0
        conn = _open_ro(self.server.phototext_db)
        try:
            loc = db.photo_location_by_id(conn, photo_id, loc_id)
        finally:
            conn.close()
        if loc is None:
            self._not_found("location does not belong to this photo")
            return
        path = Path(loc["path"])
        if not path.exists():
            self._not_found("file no longer exists on disk")
            return
        opener = shutil.which("open")
        if opener is None:
            self._not_found("reveal needs macOS (`open` command)")
            return
        subprocess.run([opener, "-R", str(path)], check=False)
        self.send_response(303)
        self.send_header("Location", f"/photo/{photo_id}")
        self.send_header("Content-Length", "0")
        self.end_headers()


def serve(
    db_path: Path | str,
    host: str = "127.0.0.1",
    port: int = 8765,
    writable: bool = False,
) -> None:
    db_path = Path(db_path).expanduser()
    if not db_path.exists():
        raise FileNotFoundError(
            f"catalog not found: {db_path} — run `phototext scan` first"
        )
    thumbs_dir = db_path.parent / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    ensure_noindex(thumbs_dir)
    token = secrets.token_hex(16) if writable else None
    httpd = _Server((host, port), _Handler, db_path, thumbs_dir, writable, token)
    actual_host, actual_port = httpd.server_address[:2]
    mode = "read-only" if not writable else "read-write (hide/delete/restore/purge enabled)"
    print(f"phototext web UI: http://{actual_host}:{actual_port}  ({mode}; Ctrl+C to stop)")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("warning: serving on a non-loopback address; anyone who can reach this "
              "machine can browse your photos and recovered text")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
