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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import pathname2url

from . import db
from . import people as people_mod
from . import webtheme
from .config import Config, ensure_noindex
from .imaging import ImageReadError, crop_jpeg, display_size, prepare_image
from .memes import DUPLICATE_HAMMING_DEFAULT, find_clusters

PAGE_SIZE = 48
THUMB_EDGE = 480
VIEW_EDGE = 2048

# Theme the restore script falls back to when the browser has no saved
# choice; `serve(theme=...)` reassigns this per server process.
_default_theme = webtheme.DEFAULT_THEME

# Formats every mainstream browser can render natively; anything else
# (HEIC, TIFF, PSD, ...) gets a converted JPEG on the detail page.
_BROWSER_SAFE = {"jpg", "jpeg", "jfif", "png", "gif", "webp", "bmp"}

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


def _page(title: str, body: str, sidebar: str = "") -> bytes:
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<script>{webtheme.restore_js(_default_theme)}</script>"
        f"<title>{esc(title)}</title><style>{webtheme.THEME_CSS}</style></head>"
        f"<body><div class='shell'><aside class='side'>{sidebar}</aside>"
        f"<div class='content'>{body}</div></div>"
        f"<script>{webtheme.TOGGLE_JS}</script></body></html>"
    )
    return doc.encode("utf-8")


_FILTER_JS = """
(function(){
  document.querySelectorAll('.ffilter').forEach(function(i){
    i.addEventListener('input',function(){
      var q=i.value.toLowerCase();
      i.parentElement.querySelectorAll('.fitems a').forEach(function(a){
        a.style.display=(a.classList.contains('futil')||!q||
          a.textContent.toLowerCase().indexOf(q)>=0)?'':'none';});
    });
  });
})();
"""


def _fgroup(title: str, links_html: str, count: int, filter_placeholder: str | None = None) -> str:
    """A collapsible sidebar filter group with a link count; groups with
    many links can carry a client-side filter box."""
    box = ""
    if filter_placeholder is not None:
        box = (
            f"<input class='ffilter' type='text' placeholder='{esc(filter_placeholder)}'>"
        )
    return (
        f"<details class='fgroup' open>"
        f"<summary>{esc(title)}<span class='count'>{count}</span></summary>"
        f"{box}"
        f"<div class='fitems navlist'>{links_html}</div>"
        f"</details>"
    )


def _sidebar(
    conn: sqlite3.Connection,
    q: str = "",
    status: str = "",
    active: str = "",
    url_for=None,
    extra: str = "",
) -> str:
    """The left rail: brand, search, and the global nav (status filters,
    Discover, Utilities) stay pinned; page-specific filter sections and
    the counts footer scroll below them."""
    counts = db.status_counts(conn)
    link = url_for or (lambda key: f"/?status={key}")
    rec = counts.get("queued", 0) + counts.get("processing", 0) > 0
    rec_dot = (
        f"<span class='rec-dot{'on' if rec else ' off'}'"
        f" title='{'processing queue active' if rec else 'queue idle'}'></span>"
    )

    def item(href: str, label: str, on: bool = False) -> str:
        cls = " class='on'" if on else ""
        return f"<a{cls} href='{href}'>{label}</a>"

    library = "".join(
        item(link(key), label, on=(status == key and not active))
        for key, label in (
            ("all", "All"),
            ("done", "Done"),
            ("queued", "Queued"),
            ("error", "Errors"),
            ("deferred", "Deferred"),
        )
    )
    discover = (
        item("/people", "People", active == "people")
        + item("/memes", "Memes", active == "memes")
        + item("/duplicates", "Duplicates", active == "duplicates")
    )
    utilities = item("/trash", "Trash", active == "trash")
    script = f"<script>{_FILTER_JS}</script>" if "class='ffilter'" in extra else ""
    return (
        "<div class='side-top'>"
        f"<div class='brand'>phototext {rec_dot}</div>"
        "<form class='search' action='/' method='get'>"
        "<input type='text' name='q' placeholder='Search recovered text and context...'"
        f" value='{esc(q)}'><button>Search</button></form>"
        "<button type='button' class='theme-toggle' id='pt-theme-toggle'>iCloud</button>"
        "<div class='navgroup'>Library</div>"
        f"<div class='navlist'>{library}</div>"
        "<div class='navgroup'>Discover</div>"
        f"<div class='navlist'>{discover}</div>"
        "<div class='navgroup'>Utilities</div>"
        f"<div class='navlist'>{utilities}</div>"
        "</div>"
        "<div class='side-scroll'>"
        f"{extra}"
        f"<div class='sidefoot'>{counts.get('total', 0)} photos "
        f"(&#10003; {counts.get('done', 0)} &#8987; {counts.get('queued', 0)} "
        f"&#10007; {counts.get('error', 0)})</div>"
        f"{script}"
        "</div>"
    )


def _action_form(
    action: str,
    photo_id: int,
    label: str,
    token: str,
    confirm: str | None = None,
    next_url: str | None = None,
    form_class: str = "act",
) -> str:
    onsubmit = f" onsubmit=\"return confirm('{esc(confirm)}')\"" if confirm else ""
    next_input = (
        f"<input type='hidden' name='next' value='{esc(next_url)}'>" if next_url else ""
    )
    return (
        f"<form class='{form_class}' method='post' action='/{action}/{photo_id}'{onsubmit}>"
        f"<input type='hidden' name='token' value='{esc(token)}'>"
        f"{next_input}"
        f"<button class='mini'>{esc(label)}</button></form>"
    )


def _bulk_delete_form(
    token: str, next_url: str, fields: dict[str, str], total: int
) -> str:
    """One-click 'delete everything in this view' (writable servers only).

    Manual and confirmed by design: nothing is ever deleted automatically
    because a photo left Photos. The filter fields are echoed so the route
    recomputes the same selection server-side.
    """
    inputs = "".join(
        f"<input type='hidden' name='{esc(k)}' value='{esc(v)}'>"
        for k, v in fields.items()
        if v
    )
    return (
        "<form class='act bulkdel' method='post' action='/bulk-delete' "
        f"onsubmit=\"return confirm('Move all {total} photo(s) in this view "
        "to the trash? Files are never touched.')\">"
        f"<input type='hidden' name='token' value='{esc(token)}'>"
        f"<input type='hidden' name='next' value='{esc(next_url)}'>"
        f"{inputs}"
        f"<button class='mini'>delete all {total} in view</button></form>"
    )


def _snippet(text: str, limit: int = 220) -> str:
    flat = " ".join((text or "").split())
    if len(flat) > limit:
        flat = flat[:limit].rsplit(" ", 1)[0] + " ..."
    return flat


def _card(row, snippet: str, action_html: str = "") -> str:
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
    hidden_badge = ("<span class='badge'>hidden</span>" if row["hidden"] else "")
    inner = (
        f"<img src='/thumb/{row['id']}' alt='{esc(name)}' loading='lazy'>"
        "<div class='body'>"
        f"{badge}{tile_badge}{gate_badge}{deriv_badge}{hidden_badge}"
        f"<span class='badge'>{esc(category or kind)}</span>"
        f"{snippet_html}"
        f"<div class='path'>{esc(name)}</div>"
        "</div>"
    )
    if action_html:
        # Writable mode: wrap the card so the hide/unhide toggle can sit on
        # top of the link without nesting a form inside the anchor.
        return (
            f"<div class='cardbox'>"
            f"<a class='card' href='/photo/{row['id']}'>{inner}</a>"
            f"{action_html}</div>"
        )
    return f"<a class='card' href='/photo/{row['id']}'>{inner}</a>"


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


def _parse_date_params(params: dict) -> tuple[str | None, str | None, str | None]:
    """(date_from, date_to, year) from query params, ignoring invalid values.

    date_to is widened to the end of its day so same-day photos match.
    """
    year = (params.get("year", [""])[0] or "").strip() or None
    if year is not None and not (year.isdigit() and len(year) == 4):
        year = None
    date_from = (params.get("date-from", [""])[0] or "").strip() or None
    if date_from is not None:
        try:
            datetime.strptime(date_from, "%Y-%m-%d")
        except ValueError:
            date_from = None
    date_to = (params.get("date-to", [""])[0] or "").strip() or None
    if date_to is not None:
        try:
            datetime.strptime(date_to, "%Y-%m-%d")
            date_to += "T23:59:59"
        except ValueError:
            date_to = None
    return date_from, date_to, year


def render_list(conn: sqlite3.Connection, params: dict, ctx: dict | None = None) -> bytes:
    q = (params.get("q", [""])[0] or "").strip()
    try:
        page = max(1, int(params.get("page", ["1"])[0] or 1))
    except ValueError:
        page = 1
    offset = (page - 1) * PAGE_SIZE
    date_from, date_to, year = _parse_date_params(params)
    cards = []
    if q:
        try:
            rows, _effective = db.search_photos(
                conn, q, limit=PAGE_SIZE, offset=offset, highlight=("", ""),
                date_from=date_from, date_to=date_to, year=year,
            )
            total = db.search_count(
                conn, q, date_from=date_from, date_to=date_to, year=year
            )
        except sqlite3.OperationalError:
            body = (
                f"<p class='note'>Search is unavailable: the catalog schema is "
                "older than this build. Run <code>phototext migrate</code> (or any CLI "
                "command) once, then reload.</p>"
            )
            return _page("phototext", body, _sidebar(conn, q=q))
        note = f"{total} match(es) for '{q}'"
        if year:
            note += f" taken in {year}"
        if date_from or date_to:
            span = date_from[:10] if date_from else "..."
            if date_to:
                span += f" to {date_to[:10]}"
            note += f" taken {span}"
        if total > len(rows) + offset:
            note += f" — showing {offset + 1}&ndash;{offset + len(rows)}"
        cards = [_card(r, r["text_snip"] or "") for r in rows]
        base_parts = {"q": q}
        if year:
            base_parts["year"] = year
        if date_from:
            base_parts["date-from"] = date_from[:10]
        if date_to:
            base_parts["date-to"] = date_to[:10]
        base = "/?" + urlencode(base_parts) + "&"
        year_section = ""
        years = db.date_taken_histogram(conn)
        if years:
            q_enc = urlencode({"q": q})
            chips = []
            for y in years:
                cls = " class='on'" if year == y["year"] else ""
                chips.append(
                    f"<a{cls} href='/?{q_enc}&year={y['year']}'>{y['year']} ({y['n']})</a>"
                )
            if year:
                chips.append(f"<a class='futil' href='/?{q_enc}'>all years</a>")
            year_section = _fgroup("Years", "".join(chips), len(years))
        sidebar = _sidebar(conn, q=q, extra=year_section)
        body = f"<p class='note'>{esc(note)}</p>"
        token = (ctx or {}).get("token") if (ctx or {}).get("writable") else None
        if token and total:
            body += _bulk_delete_form(
                token,
                "/?" + urlencode(base_parts),
                {
                    "q": q,
                    "year": year or "",
                    "date-from": date_from[:10] if date_from else "",
                    "date-to": date_to[:10] if date_to else "",
                },
                total,
            )
    else:
        status = params.get("status", ["all"])[0] or "all"
        if status not in ("all", "done", "queued", "error", "processing"):
            status = "all"
        category = (params.get("category", [""])[0] or "").strip() or None
        person = (params.get("person", [""])[0] or "").strip() or None
        text_filter = params.get("text", ["all"])[0] or "all"
        if text_filter not in ("all", "yes", "no"):
            text_filter = "all"
        has_text = None if text_filter == "all" else (text_filter == "yes")
        hidden_param = params.get("hidden", [""])[0]
        show_hidden = hidden_param == "1"
        hidden_only = hidden_param == "only"
        rows, total = db.page_photos(
            conn, status=status, has_text=has_text, category=category,
            limit=PAGE_SIZE, offset=offset, show_hidden=show_hidden,
            hidden_only=hidden_only, person=person,
            date_from=date_from, date_to=date_to, year=year,
        )

        def qs(over: dict | None = None) -> str:
            parts = {
                "status": status,
                "category": category,
                "text": text_filter,
                "person": person,
                "hidden": ("only" if hidden_only else "1" if show_hidden else ""),
                "year": year or "",
                "date-from": (params.get("date-from", [""])[0] or "").strip()
                if (date_from is not None)
                else "",
                "date-to": (params.get("date-to", [""])[0] or "").strip()
                if (date_to is not None)
                else "",
            }
            for key, value in (over or {}).items():
                if value:
                    parts[key] = value
                else:
                    parts.pop(key, None)
            query = urlencode({k: v for k, v in parts.items() if v and v != "all"})
            return f"/?{query}&" if query else "/?"

        base = qs()
        hidden_n = conn.execute(
            "SELECT COUNT(*) AS n FROM photos WHERE hidden = 1 AND deleted_at IS NULL"
        ).fetchone()["n"]
        if hidden_only:
            hidden_chip = f"<a class='on' href='{qs({'hidden': ''})}'>all photos</a>"
        elif show_hidden:  # legacy mixed view (?hidden=1): hidden shown inline
            hidden_chip = f"<a href='{qs({'hidden': ''})}'>hide hidden</a>"
        else:
            hidden_chip = f"<a href='{qs({'hidden': 'only'})}'>hidden ({hidden_n})</a>"
        token = (ctx or {}).get("token") if (ctx or {}).get("writable") else None
        if token:
            next_url = f"{base}page={page}"

            def _toggle(r) -> str:
                hidden_now = bool(r["hidden"])
                return _action_form(
                    "unhide" if hidden_now else "hide", r["id"],
                    "unhide" if hidden_now else "hide", token, next_url=next_url,
                )
        else:
            def _toggle(r) -> str:
                return ""
        cards = [_card(r, _snippet(r["text"] or ""), _toggle(r)) for r in rows]
        sections = [_fgroup("Views", hidden_chip, 1)]
        people_rows = db.people_list(
            conn, float((ctx or {}).get("min_confidence") or 0.6)
        )
        if people_rows:
            chips = [
                f"<a{cls} href='{qs({'person': p['name']})}'>{esc(p['name'])}</a>"
                for p in people_rows
                for cls in (" class='on'" if person == p["name"] else "",)
            ]
            if person:
                chips.append(f"<a class='futil' href='{qs({'person': ''})}'>all people</a>")
            sections.append(
                _fgroup(
                    "People", "".join(chips), len(people_rows),
                    filter_placeholder="filter people" if len(people_rows) > 8 else None,
                )
            )
        cats = [
            r["category"]
            for r in conn.execute(
                "SELECT DISTINCT category FROM photos WHERE category IS NOT NULL "
                "ORDER BY category"
            )
        ]
        if cats:
            chips = [
                f"<a{cls} href='{qs({'category': c})}'>{esc(c)}</a>"
                for c in cats
                for cls in (" class='on'" if c == category else "",)
            ]
            chips.append(f"<a class='futil' href='{qs({'category': ''})}'>all categories</a>")
            sections.append(
                _fgroup(
                    "Categories", "".join(chips), len(cats),
                    filter_placeholder="filter categories" if len(cats) > 8 else None,
                )
            )
        text_chips = []
        for value, label in (("all", "any text"), ("yes", "with text"), ("no", "no text")):
            cls = " class='on'" if text_filter == value else ""
            text_chips.append(f"<a{cls} href='{qs({'text': value})}'>{label}</a>")
        sections.append(_fgroup("Text", "".join(text_chips), 3))
        years = db.date_taken_histogram(conn)
        if years:
            chips = []
            for y in years:
                cls = " class='on'" if year == y["year"] else ""
                chips.append(
                    f"<a{cls} href='{qs({'year': y['year']})}'>{y['year']} ({y['n']})</a>"
                )
            if year:
                chips.append(f"<a class='futil' href='{qs({'year': ''})}'>all years</a>")
            sections.append(_fgroup("Years", "".join(chips), len(years)))
        sidebar = _sidebar(
            conn, status=status, url_for=lambda key: qs({"status": key}),
            extra="".join(sections),
        )
        body = f"<p class='note'>{total} photo(s) with status '{status}'"
        if category:
            body += f" and category '{esc(category)}'"
        if person:
            body += f" tagged '{esc(person)}'"
        if year:
            body += f" taken in {esc(year)}"
        if date_from or date_to:
            span = date_from[:10] if date_from else "..."
            if date_to:
                span += f" to {date_to[:10]}"
            body += f" taken {esc(span)}"
        if text_filter != "all":
            body += f" ({'with' if text_filter == 'yes' else 'no'} text)"
        body += "</p>"
        if token and total and (
            hidden_only or person or category or status != "all"
            or text_filter != "all" or year or date_from or date_to
        ):
            body += _bulk_delete_form(
                token,
                f"{base}page=1",
                {
                    "hidden": "only" if hidden_only else "",
                    "person": person or "",
                    "category": category or "",
                    "status": "" if status == "all" else status,
                    "text": "" if text_filter == "all" else text_filter,
                    "year": year or "",
                    "date-from": date_from[:10] if date_from else "",
                    "date-to": date_to[:10] if date_to else "",
                },
                total,
            )
    body += "<div class='cards'>" + "".join(cards) + "</div>"
    body += _pager(base, page, total)
    return _page("phototext", body, sidebar)


def render_detail(conn: sqlite3.Connection, photo_id: int, ctx: dict | None = None) -> bytes | None:
    row = db.get_photo(conn, photo_id)
    if row is None:
        return None
    locs = db.photo_locations(conn, photo_id)
    rows_meta = [
        ("status", row["status"]),
        ("taken", row["date_taken"][:16].replace("T", " ") if row["date_taken"] else "?"),
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
    if row["offloaded"]:
        rows_meta.insert(1, ("original", "offloaded by iCloud"))
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
        if exists:
            reveal = (
                f"<a href='/reveal/{photo_id}?loc={loc['id']}'>reveal in Finder</a><br>"
            )
        else:
            reveal = ""
        loc_items.append(
            f"<li>{disk} <span class='badge'>{esc(kind)}</span> "
            f"{reveal}"
            f"{esc(loc['path'])}<br>"
            f"<span class='muted'>from {esc(loc['source_uri'] or '?')}</span></li>"
        )
    loc_list = "".join(loc_items)
    photos_link = (
        f"<p class='muted'><a href='/open-photos/{photo_id}'>open in Photos</a>"
        " &#8594; shows the photo inside the Photos app</p>"
        if db.asset_uuids(conn, photo_id)
        else ""
    )
    text = row["text"] or ""
    context = row["context"] or ""
    raw = row["raw_response"] or ""
    threshold = float((ctx or {}).get("min_confidence") or 0.6)
    tags = db.people_for_photo(conn, photo_id)
    writable = bool(ctx and ctx.get("writable") and ctx.get("token"))
    people_section = ""
    if tags:
        chips = []
        for t in tags:
            if t["origin"] == "seed":
                label = f"{t['name']} — seed"
            elif t["origin"] == "user":
                label = f"{t['name']} — confirmed"
            else:
                pct = f"{t['confidence'] * 100:.0f}%"
                label = f"{t['name']} — {pct}" + (
                    " (review)" if t["confidence"] < threshold else ""
                )
            chip = f"<a class='badge' href='/person/{t['person_id']}'>{esc(label)}</a>"
            if writable:
                chip += (
                    _person_action("confirm", photo_id, t["person_id"], "✓", ctx["token"])
                    + _person_action("remove", photo_id, t["person_id"], "✕", ctx["token"])
                )
            chips.append(f"<div class='chipline'>{chip}</div>")
        people_section = (
            "<p class='muted'>people</p><div class='people'>" + "".join(chips) + "</div>"
        )
    body = "<p><a class='back' href='/'>&#8592; back to photos</a></p>"
    if actions:
        body += f"<div class='actions'>{actions}</div>"
    tone = " tone-err" if row["status"] == "error" else ""
    taken = row["date_taken"][:16].replace("T", " ") if row["date_taken"] else ""
    designation = (
        f"<span class='designation'>photo {photo_id}"
        + (f" // {taken}" if taken else "")
        + "</span>"
    )
    body += f"<div class='detail'>"
    if writable:
        # The picker submits boxes in original-image pixels (crop_jpeg's
        # frame); /image/ may serve a downscaled conversion of HEIC and
        # friends, so embed the true display size for the JS to scale by.
        pick_attrs = ""
        pick_path = _first_existing(conn, photo_id)
        if pick_path is not None:
            dims = display_size(pick_path)
            if dims:
                pick_attrs = f" data-w='{dims[0]}' data-h='{dims[1]}'"
        body += (
            f"<figure class='subject{tone}'>{designation}"
            "<div class='pickwrap'>"
            f"<img id='pickimg' src='/image/{photo_id}'{pick_attrs} alt='photo {photo_id}'>"
            "<div id='selbox' class='selbox'></div></div></figure>"
            "<form class='tagform' method='post' action='/person/tag'>"
            f"<input type='hidden' name='photo_id' value='{photo_id}'>"
            f"<input type='hidden' name='token' value='{esc(ctx['token'])}'>"
            "<input type='hidden' name='box' id='boxfield' value=''>"
            "<input type='text' name='name' placeholder=\"person's name\" maxlength='60'>"
            "<button>tag person</button></form>"
            "<p class='muted' id='boxhint'>drag a box around a face on the photo, "
            "then enter a name (no box = whole photo)</p>"
            f"<script>{_PICKER_JS}</script>"
        )
    else:
        body += (
            f"<figure class='subject{tone}'>{designation}"
            f"<img src='/image/{photo_id}' alt='photo {photo_id}'></figure>"
        )
    body += photos_link
    body += "<div class='meta'>"
    body += f"<table>{meta}</table>"
    body += people_section
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
    body += "</div></div>"
    return _page(f"photo {photo_id}", body, _sidebar(conn))


def render_memes(conn: sqlite3.Connection, ctx: dict | None = None) -> bytes:
    try:
        clusters = find_clusters(conn)
    except sqlite3.OperationalError:
        clusters = []
    sidebar = _sidebar(conn, active="memes")
    body = ""
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
        body += f"<p class='note'>{esc(note)}</p>"
        return _page("memes - phototext", body, sidebar)
    body += (
        "<p class='note'>" + esc(str(len(clusters))) + " meme-like group(s) "
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
    return _page("memes - phototext", body, sidebar)


def render_duplicates(conn: sqlite3.Connection, ctx: dict | None = None) -> bytes:
    try:
        clusters = find_clusters(
            conn,
            max_distance=DUPLICATE_HAMMING_DEFAULT,
            exclude_derivatives=True,
        )
    except sqlite3.OperationalError:
        clusters = []
    sidebar = _sidebar(conn, active="duplicates")
    body = ""
    if not clusters:
        note = "no duplicate groups found"
        try:
            unhashed = conn.execute(
                "SELECT COUNT(*) AS n FROM photos WHERE phash IS NULL"
            ).fetchone()["n"]
        except sqlite3.OperationalError:
            unhashed = 0
        if unhashed:
            note += (
                f" — {unhashed} photo(s) have no perceptual hash yet; "
                "run `phototext duplicates` once to compute them"
            )
        body += f"<p class='note'>{esc(note)}</p>"
        return _page("duplicates - phototext", body, sidebar)
    body += (
        "<p class='note'>" + esc(str(len(clusters))) + " duplicate group(s) "
        "— resized/re-encoded copies of the same image (perceptual hash within "
        f"{DUPLICATE_HAMMING_DEFAULT} bits). iCloud preview proxies are excluded. "
        "The largest file is highlighted; cleaning up is yours to do — files are "
        "never touched.</p>"
    )
    for index, group in enumerate(clusters, 1):
        biggest = max(group, key=lambda r: r["byte_size"] or 0)
        cards = ""
        keep_figure = ""
        for r in group[:12]:
            if r["id"] == biggest["id"]:
                mark = " <span class='badge ok'>keep</span>"
            else:
                mark = " <span class='badge'>derivative</span>"
            taken = f" <span class='badge'>{esc(r['date_taken'][:10])}</span>" if r["date_taken"] else ""
            card = (
                f"<a class='card' href='/photo/{r['id']}'>"
                f"<img src='/thumb/{r['id']}' alt='' loading='lazy'>"
                f"<div class='body'>{esc(str(r['byte_size'] or 0))} bytes{mark}{taken}</div></a>"
            )
            if r["id"] == biggest["id"]:
                keep_figure = (
                    "<figure class='subject'><span class='designation'>KEEP</span>"
                    + card + "</figure>"
                )
            else:
                cards += card
        body += (
            f"<p class='muted'>group {index}: {len(group)} photo(s)</p>"
            f"<div class='cards'>{keep_figure}{cards}</div>"
        )
    return _page("duplicates - phototext", body, sidebar)


def render_trash(conn: sqlite3.Connection, ctx: dict | None = None) -> bytes:
    rows = db.trash_list(conn)
    sidebar = _sidebar(conn, active="trash")
    body = ""
    if not rows:
        body += "<p class='note'>trash is empty</p>"
        return _page("trash - phototext", body, sidebar)
    writable = bool(ctx and ctx.get("writable") and ctx.get("token"))
    body += f"<p class='note'>{len(rows)} photo(s) in the trash. "
    if not writable:
        body += "Start the server with --writable to restore or purge from here."
    body += "</p>"
    if writable:
        body += (
            "<form class='act bulkdel' method='post' action='/bulk-purge' "
            f"onsubmit=\"return confirm('Permanently forget all {len(rows)} "
            "photo(s) in the trash? This cannot be undone.')\">"
            f"<input type='hidden' name='token' value='{esc(ctx['token'])}'>"
            f"<button class='mini'>purge all {len(rows)}</button></form>"
        )
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
    return _page("trash - phototext", body, sidebar)


def _first_existing(conn: sqlite3.Connection, photo_id: int) -> Path | None:
    path = db.find_first_existing_location(conn, photo_id)
    return Path(path) if path else None


def cache_dirs(db_path: Path) -> tuple[Path, Path]:
    """(thumbs, views) cache dirs for a catalog at db_path."""
    return db_path.parent / "thumbs", db_path.parent / "views"


def remove_cached_images(db_path: Path, photo_ids: list[int]) -> int:
    """Drop thumbs/views cache files for photos whose rows are gone (purge).

    Tombstoned photos keep their caches — the trash view still shows
    thumbnails and restore must work.
    """
    removed = 0
    for cache_dir in cache_dirs(db_path):
        for photo_id in photo_ids:
            cache = cache_dir / f"{photo_id}.jpg"
            try:
                cache.unlink()
                removed += 1
            except FileNotFoundError:
                pass
            except OSError:
                pass
    return removed


def clean_cached_images(db_path: Path, dry_run: bool = False) -> tuple[int, int]:
    """Garbage-collect cache files whose photo row no longer exists.

    Covers files orphaned by purges older than the cleanup-on-purge logic.
    Returns (files removed, bytes freed).
    """
    conn = _open_ro(db_path)
    try:
        live = {
            row["id"]
            for row in conn.execute("SELECT id FROM photos")
        }
    finally:
        conn.close()
    removed = freed = 0
    for cache_dir in cache_dirs(db_path):
        if not cache_dir.is_dir():
            continue
        for cache in cache_dir.iterdir():
            if not cache.name.endswith(".jpg") or not cache.stem.isdigit():
                continue
            if int(cache.stem) in live:
                continue
            try:
                size = cache.stat().st_size
                if not dry_run:
                    cache.unlink()
                removed += 1
                freed += size
            except OSError:
                pass
    return removed, freed


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


def view_image_bytes(conn: sqlite3.Connection, photo_id: int, views_dir: Path) -> bytes | None:
    """Display-ready JPEG for the detail page, cached under <db_dir>/views/.

    Browser-safe originals never reach here while the file exists (the
    route serves them raw); this converts HEIC/TIFF/PSD/... so every
    browser can render them. When the original is gone entirely (iCloud
    offload), the cache alone serves the route. Returns None when there
    is no cache and no readable file."""
    cache = views_dir / f"{photo_id}.jpg"
    if cache.exists():
        try:
            return cache.read_bytes()
        except OSError:
            pass
    path = _first_existing(conn, photo_id)
    if path is None:
        return None
    try:
        data = prepare_image(path, max_edge=VIEW_EDGE, jpeg_quality=85)
    except ImageReadError:
        return None
    try:
        views_dir.mkdir(parents=True, exist_ok=True)
        ensure_noindex(views_dir)
        cache.write_bytes(data)
    except OSError:
        pass
    return data


def _person_action(
    action: str,
    photo_id: int,
    person_id: int,
    label: str,
    token: str,
    extra_fields: str = "",
) -> str:
    return (
        f"<form class='act' method='post' action='/person/{action}'>"
        f"<input type='hidden' name='photo_id' value='{photo_id}'>"
        f"<input type='hidden' name='person_id' value='{person_id}'>"
        f"<input type='hidden' name='token' value='{esc(token)}'>"
        f"{extra_fields}"
        f"<button class='mini'>{esc(label)}</button></form>"
    )


def _origin_badge(row, threshold: float) -> str:
    origin = row["origin"]
    if origin == "seed":
        return "<span class='badge ok'>seed</span>"
    if origin == "user":
        return "<span class='badge ok'>confirmed</span>"
    confidence = row["confidence"]
    pct = f"{confidence * 100:.0f}%"
    if confidence < threshold:
        return f"<span class='badge review'>{pct} review</span>"
    return f"<span class='badge'>{pct}</span>"


def _person_grid(
    rows, ctx: dict | None, threshold: float, person_id: int, with_actions: bool
) -> str:
    writable = bool(ctx and ctx.get("writable") and ctx.get("token")) and with_actions
    cards = []
    for row in rows:
        name = (row["path"] or "").rsplit("/", 1)[-1] or "(no location)"
        snippet = _snippet(row["text"] or "")
        badges = (
            f"<span class='badge'>{esc(row['status'])}</span>"
            f"{_origin_badge(row, threshold)}"
        )
        if snippet:
            badges += f"<div class='snippet'>{esc(snippet)}</div>"
        else:
            badges += "<div class='snippet'><span class='muted'>(no text)</span></div>"
        badges += f"<div class='path'>{esc(name)}</div>"
        if writable:
            badges += (
                "<div class='actions'>"
                + _person_action("confirm", row["id"], person_id, "confirm", ctx["token"])
                + _person_action(
                    "confirm", row["id"], person_id, "confirm+seed", ctx["token"],
                    extra_fields="<input type='hidden' name='seed' value='1'>",
                )
                + _person_action("remove", row["id"], person_id, "remove", ctx["token"])
                + "</div>"
            )
        cards.append(
            f"<div class='card'>"
            f"<a href='/photo/{row['id']}'><img src='/thumb/{row['id']}' "
            f"alt='{esc(name)}' loading='lazy'></a>"
            f"<div class='body'>{badges}</div></div>"
        )
    if not cards:
        return "<p class='note'>none</p>"
    return "<div class='cards'>" + "".join(cards) + "</div>"


_PICKER_JS = """
(function () {
  var img = document.getElementById('pickimg');
  if (!img) return;
  var box = document.getElementById('selbox');
  var field = document.getElementById('boxfield');
  var hint = document.getElementById('boxhint');
  var start = null;
  function pos(e) {
    var r = img.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top, r];
  }
  img.addEventListener('mousedown', function (e) {
    e.preventDefault();
    start = pos(e);
    box.style.display = 'block';
    box.style.left = start[0] + 'px';
    box.style.top = start[1] + 'px';
    box.style.width = '0px';
    box.style.height = '0px';
  });
  window.addEventListener('mousemove', function (e) {
    if (!start) return;
    var p = pos(e);
    var x = Math.min(start[0], p[0]), y = Math.min(start[1], p[1]);
    var w = Math.abs(p[0] - start[0]), h = Math.abs(p[1] - start[1]);
    box.style.left = x + 'px'; box.style.top = y + 'px';
    box.style.width = w + 'px'; box.style.height = h + 'px';
  });
  window.addEventListener('mouseup', function (e) {
    if (!start) return;
    var p = pos(e);
    var x = Math.min(start[0], p[0]), y = Math.min(start[1], p[1]);
    var w = Math.abs(p[0] - start[0]), h = Math.abs(p[1] - start[1]);
    start = null;
    if (w < 8 || h < 8) { box.style.display = 'none'; field.value = ''; return; }
    var r = img.getBoundingClientRect();
    var ow = img.dataset.w ? +img.dataset.w : img.naturalWidth;
    var oh = img.dataset.h ? +img.dataset.h : img.naturalHeight;
    var sx = ow / r.width, sy = oh / r.height;
    field.value = [Math.round(x * sx), Math.round(y * sy),
                   Math.round(w * sx), Math.round(h * sy)].join(',');
    if (hint) hint.textContent = 'box set (drag again to change it) — now enter a name';
  });
})();
"""


def render_people(conn: sqlite3.Connection, ctx: dict | None = None) -> bytes:
    threshold = float((ctx or {}).get("min_confidence") or 0.6)
    people = db.people_list(conn, threshold)
    sidebar = _sidebar(conn, active="people")
    body = ""
    if not people:
        body += (
            "<p class='note'>no people yet. Open a photo, drag a box around "
            "a face, and give the person a name (needs a writable server: "
            "<code>phototext serve --writable</code>), or use "
            "<code>phototext people name <photo-id> <name> --box x,y,w,h</code>.</p>"
        )
        return _page("people - phototext", body, sidebar)
    body += f"<p class='note'>{len(people)} person(s)</p>"
    for person in people:
        seeds = db.person_seed_photo_ids(conn, person["id"], 1)
        face = f"/face/{person['id']}/{seeds[0]}" if seeds else ""
        if not face and person["tags"]:
            top = db.person_tag_rows(conn, person["id"])
            if top:
                face = f"/thumb/{top[0]['id']}"
        img = (
            f"<img class='face' src='{face}' alt='' loading='lazy'>"
            if face
            else "<div class='face'></div>"
        )
        body += (
            f"<a class='pcard' href='/person/{person['id']}'>{img}<div class='pbody'>"
            f"<strong>{esc(person['name'])}</strong> "
            f"<span class='badge'>{person['tags']} tag(s)</span> "
            f"<span class='badge ok'>{person['confirmed']} confirmed</span> "
            f"<span class='badge'>{person['seeds']} seed(s)</span> "
            f"<span class='badge'>{person['strong']} confident</span> "
            f"<span class='badge review'>{person['uncertain']} to review</span>"
            f"<div class='muted'>{_snippet(person['description'] or '', 160) or 'no recognition profile yet'}</div>"
            f"</div></a>"
        )
    return _page("people - phototext", body, sidebar)


def render_person(
    conn: sqlite3.Connection,
    person_id: int,
    ctx: dict | None = None,
    params: dict | None = None,
) -> bytes | None:
    person = db.get_person(conn, person_id)
    if person is None:
        return None
    threshold = float((ctx or {}).get("min_confidence") or 0.6)
    hidden_param = (params or {}).get("hidden", [""])[0]
    hidden_only = hidden_param == "only"
    show_hidden = hidden_param == "1"
    rows = db.person_tag_rows(
        conn, person_id, hidden_only=hidden_only, show_hidden=show_hidden
    )
    seed_count = conn.execute(
        "SELECT COUNT(*) AS n FROM person_tags WHERE person_id = ? AND seed = 1",
        (person_id,),
    ).fetchone()["n"]
    hidden_n = conn.execute(
        "SELECT COUNT(*) AS n FROM person_tags t JOIN photos p ON p.id = t.photo_id "
        "WHERE t.person_id = ? AND t.present = 1 AND p.hidden = 1 "
        "AND p.deleted_at IS NULL",
        (person_id,),
    ).fetchone()["n"]
    review = [r for r in rows if r["origin"] == "model" and r["confidence"] < threshold]
    settled = [r for r in rows if r not in review]
    sidebar = _sidebar(conn, active="people")
    body = "<p><a class='back' href='/people'>&#8592; all people</a></p>"
    writable = bool(ctx and ctx.get("writable") and ctx.get("token"))
    body += f"<h2 style='margin:6px 0'>{esc(person['name'])}</h2>"
    if hidden_only:
        chip = f"<a class='on' href='/person/{person_id}'>all photos</a>"
    elif show_hidden:  # legacy mixed view (?hidden=1)
        chip = f"<a href='/person/{person_id}'>hide hidden</a>"
    else:
        chip = f"<a href='/person/{person_id}?hidden=only'>hidden ({hidden_n})</a>"
    body += f"<nav>{chip}</nav>"
    body += (
        f"<p class='note'>{seed_count} seed anchor(s) for the recognition profile — "
        "grow it with <code>confirm+seed</code> on tagged photos, then refresh with "
        f"<code>phototext people describe {esc(person['name'])}</code></p>"
    )
    if person["description"]:
        body += f"<p class='muted'>recognition profile</p><pre>{esc(person['description'])}</pre>"
    else:
        body += (
            "<p class='note'>no recognition profile yet — it is built automatically by "
            f"<code>phototext people run</code> or <code>phototext people describe "
            f"{esc(person['name'])}</code></p>"
        )
    if writable:
        token = ctx["token"]
        body += (
            "<div class='actions'>"
            "<form class='act wideform' method='post' action='/person/rename'>"
            f"<input type='hidden' name='person_id' value='{person_id}'>"
            f"<input type='hidden' name='token' value='{esc(token)}'>"
            "<input type='text' name='name' placeholder='new name' maxlength='60'>"
            "<button class='mini'>rename</button></form>"
            "<form class='act' method='post' action='/person/reset'>"
            f"<input type='hidden' name='person_id' value='{person_id}'>"
            f"<input type='hidden' name='token' value='{esc(token)}'>"
            "<button class='mini'>reset model tags</button></form>"
            "<form class='act' method='post' action='/person/delete'"
            " onsubmit=\"return confirm('Delete this person and all their tags?')\">"
            f"<input type='hidden' name='person_id' value='{person_id}'>"
            f"<input type='hidden' name='token' value='{esc(token)}'>"
            "<button class='mini'>delete person</button></form>"
            "</div>"
        )
    body += (
        f"<p class='note'>{len(rows)} tagged photo(s): {len(settled)} settled, "
        f"{len(review)} to review (model confidence below {threshold:.2f})</p>"
    )
    if review:
        body += "<p class='muted'>review queue — confirm or remove each</p>"
        body += _person_grid(review, ctx, threshold, person_id, with_actions=True)
    if settled:
        body += "<p class='muted'>tagged photos</p>"
        body += _person_grid(settled, ctx, threshold, person_id, with_actions=True)
    return _page(f"{person['name']} - phototext", body, sidebar)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address,
        handler,
        db_path: Path,
        thumbs_dir: Path,
        views_dir: Path,
        writable: bool = False,
        token: str | None = None,
        person_cfg: Config | None = None,
    ):
        super().__init__(address, handler)
        self.phototext_db = db_path
        self.phototext_thumbs = thumbs_dir
        self.phototext_views = views_dir
        self.phototext_writable = writable
        self.phototext_token = token
        self.phototext_person_cfg = person_cfg
        self.phototext_min_confidence = (
            person_cfg.person_min_confidence if person_cfg else 0.6
        )


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
        person_match = re.match(r"^/person/(tag|confirm|remove|rename|reset|delete)$", route)
        bulk = route == "/bulk-delete"
        bulk_purge = route == "/bulk-purge"
        if (
            match is None
            and person_match is None
            and not bulk
            and not bulk_purge
        ):
            self._not_found()
            return
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
            if match is not None:
                action, photo_id = match.group(1), int(match.group(2))
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
                    remove_cached_images(self.server.phototext_db, [photo_id])
                    dest = "/trash"
                # Card toggles pass the list URL they came from so the
                # toggle does not yank the user onto the detail page.
                next_url = (fields.get("next", [""])[0] or "").strip()
                if next_url.startswith("/") and not next_url.startswith("//"):
                    dest = next_url
            elif bulk:
                dest = self._handle_bulk_delete(conn, fields)
                if dest is None:
                    self._send(
                        400,
                        b"refusing to bulk-delete without a filter",
                        "text/plain",
                    )
                    return
                next_url = (fields.get("next", [""])[0] or "").strip()
                if next_url.startswith("/") and not next_url.startswith("//"):
                    dest = next_url
            elif bulk_purge:
                ids = [row["id"] for row in db.trash_list(conn)]
                db.purge_photos(conn, ids)
                remove_cached_images(self.server.phototext_db, ids)
                dest = "/trash"
            else:
                try:
                    dest = self._handle_person_post(conn, person_match.group(1), fields)
                except ValueError as e:
                    self._send(400, f"bad request: {esc(e)}".encode(), "text/plain")
                    return
        finally:
            conn.close()
        self.send_response(303)
        self.send_header("Location", dest)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_bulk_delete(
        self, conn: sqlite3.Connection, fields: dict
    ) -> str:
        """Tombstone every photo matching the posted view filters.

        Manual by design: photos are never trashed automatically when
        their asset disappears from the Photos library — this only runs
        when the user confirms the button. Refuses unfiltered requests so
        an accidental click cannot trash the whole catalog.
        """

        def field(name: str) -> str:
            return (fields.get(name, [""])[0] or "").strip()

        q = field("q")
        hidden_only = field("hidden") == "only"
        status = field("status") or "all"
        if status not in ("all", "done", "queued", "error", "processing"):
            status = "all"
        text_filter = field("text") or "all"
        if text_filter not in ("all", "yes", "no"):
            text_filter = "all"
        has_text = None if text_filter == "all" else (text_filter == "yes")
        category = field("category") or None
        person = field("person") or None
        date_from, date_to, year = _parse_date_params(
            {
                "year": [field("year")],
                "date-from": [field("date-from")],
                "date-to": [field("date-to")],
            }
        )
        scoped = bool(
            q or hidden_only or person or category or status != "all"
            or text_filter != "all" or year or date_from or date_to
        )
        if not scoped:
            return None
        if q:
            try:
                rows, _eff = db.search_photos(
                    conn, q, limit=10**9,
                    date_from=date_from, date_to=date_to, year=year,
                )
                ids = [r["id"] for r in rows]
            except sqlite3.OperationalError:
                ids = []
        else:
            ids = db.photo_ids_matching(
                conn, status=status, has_text=has_text, category=category,
                hidden_only=hidden_only, person=person,
                date_from=date_from, date_to=date_to, year=year,
            )
        db.trash_photos(conn, ids)
        return "/"

    def _handle_person_post(
        self, conn: sqlite3.Connection, action: str, fields: dict
    ) -> str:
        def field(name: str) -> str:
            return (fields.get(name, [""])[0] or "").strip()

        def int_field(name: str) -> int:
            try:
                return int(field(name))
            except ValueError:
                raise ValueError(f"{name} must be an integer") from None

        if action in ("tag", "confirm", "remove"):
            photo_id = int_field("photo_id")
            if action == "tag":
                name = field("name")
                if not name or len(name) > people_mod.PERSON_MAX_NAME:
                    raise ValueError(
                        f"name must be 1-{people_mod.PERSON_MAX_NAME} characters"
                    )
                photo = db.get_photo(conn, photo_id)
                if photo is None or photo["deleted_at"]:
                    raise ValueError(f"no photo with id {photo_id}")
                box = people_mod.parse_box(field("box"))
                source = db.find_first_existing_location(conn, photo_id)
                if source is None:
                    raise ValueError(
                        f"photo {photo_id} has no readable file on disk "
                        "(moved, deleted, or iCloud-only)"
                    )
                person = db.upsert_person(conn, name)
                people_mod.save_seed_crop(
                    self.server.phototext_db, person["id"], photo_id,
                    Path(source), box,
                )
                db.tag_person(
                    conn, photo_id, person["id"], 1.0, "seed",
                    field("box") if box else None, seed=True,
                )
                if self.server.phototext_person_cfg is not None:
                    try:
                        people_mod.build_description(
                            conn, self.server.phototext_db, person["id"],
                            self.server.phototext_person_cfg,
                        )
                    except Exception:
                        pass  # person exists; the profile is built by people run
                return f"/photo/{photo_id}"
            person_id = int_field("person_id")
            if action == "confirm":
                add_seed = field("seed") == "1"
                db.confirm_person_tag(conn, photo_id, person_id, add_seed=add_seed)
                if add_seed:
                    row = db.person_tag_row(conn, photo_id, person_id)
                    source = db.find_first_existing_location(conn, photo_id)
                    if row is not None and source is not None:
                        box = people_mod.parse_box(row["box"]) if row["box"] else None
                        people_mod.save_seed_crop(
                            self.server.phototext_db, person_id, photo_id,
                            Path(source), box,
                        )
            else:
                db.untag_person(conn, photo_id, person_id)
            return f"/photo/{photo_id}"
        person_id = int_field("person_id")
        if action == "rename":
            name = field("name")
            if not name or len(name) > people_mod.PERSON_MAX_NAME:
                raise ValueError(
                    f"name must be 1-{people_mod.PERSON_MAX_NAME} characters"
                )
            if db.get_person_by_name(conn, name) is not None:
                raise ValueError(f"a person named '{name}' already exists")
            db.rename_person(conn, person_id, name)
            return f"/person/{person_id}"
        if action == "reset":
            db.reset_person_tags(conn, person_id)
            return f"/person/{person_id}"
        person = db.get_person(conn, person_id)
        if person is None:
            raise ValueError(f"no person with id {person_id}")
        db.delete_person(conn, person_id)
        shutil.rmtree(
            people_mod.person_dir(self.server.phototext_db) / str(person_id),
            ignore_errors=True,
        )
        return "/people"

    def _route(self) -> None:
        parsed = urlsplit(self.path)
        route = parsed.path
        params = parse_qs(parsed.query)
        ctx = {
            "writable": self.server.phototext_writable,
            "token": self.server.phototext_token,
            "min_confidence": self.server.phototext_min_confidence,
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
        if route == "/duplicates":
            conn = _open_ro(self.server.phototext_db)
            try:
                self._send_html(render_duplicates(conn, ctx))
            finally:
                conn.close()
            return
        if route == "/people":
            conn = _open_ro(self.server.phototext_db)
            try:
                self._send_html(render_people(conn, ctx))
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
        person_match = re.match(r"^/person/(\d+)$", route)
        face_match = re.match(r"^/face/(\d+)/(\d+)$", route)
        font_match = re.match(r"^/fonts/([A-Za-z0-9._-]+\.woff2)$", route)
        if font_match is not None:
            self._route_fonts(font_match.group(1))
            return
        if person_match is not None:
            self._route_person(int(person_match.group(1)), params, ctx)
            return
        if face_match is not None:
            self._route_face(int(face_match.group(1)), int(face_match.group(2)))
            return
        for prefix, handler in (
            ("/photo/", self._route_photo),
            ("/thumb/", self._route_thumb),
            ("/image/", self._route_image),
            ("/reveal/", self._route_reveal),
            ("/open-photos/", self._route_open_photos),
        ):
            if route.startswith(prefix):
                tail = route[len(prefix):]
                if not tail.isdigit():
                    self._not_found("bad photo id")
                    return
                handler(int(tail), params, ctx)
                return
        self._not_found()

    def _route_person(self, person_id: int, params: dict, ctx: dict) -> None:
        conn = _open_ro(self.server.phototext_db)
        try:
            body = render_person(conn, person_id, ctx, params)
        finally:
            conn.close()
        if body is None:
            self._not_found(f"person {person_id} not found")
        else:
            self._send_html(body)

    def _route_face(self, person_id: int, photo_id: int) -> None:
        db_path = self.server.phototext_db
        conn = _open_ro(db_path)
        try:
            person = db.get_person(conn, person_id)
            tagged = conn.execute(
                "SELECT 1 FROM person_tags WHERE photo_id = ? AND person_id = ?",
                (photo_id, person_id),
            ).fetchone()
        finally:
            conn.close()
        if person is None or tagged is None:
            self._not_found("no face crop for this person and photo")
            return
        crop = people_mod.seed_crop_path(db_path, person_id, photo_id)
        if crop.exists():
            try:
                self._send(200, crop.read_bytes(), "image/jpeg")
                return
            except OSError:
                pass
        conn = _open_ro(db_path)
        try:
            row = conn.execute(
                "SELECT box FROM person_tags WHERE photo_id = ? AND person_id = ?",
                (photo_id, person_id),
            ).fetchone()
            box = people_mod.parse_box(row["box"]) if row and row["box"] else None
            path = _first_existing(conn, photo_id)
        finally:
            conn.close()
        if path is None:
            self._not_found("no face crop")
            return
        try:
            if box is not None:
                data = crop_jpeg(path, box, max_edge=people_mod.PERSON_SEED_EDGE)
            else:
                data = prepare_image(path, max_edge=people_mod.PERSON_SEED_EDGE)
        except ImageReadError:
            self._not_found("no face crop")
            return
        try:
            crop.parent.mkdir(parents=True, exist_ok=True)
            ensure_noindex(crop.parent.parent)
            ensure_noindex(crop.parent)
            crop.write_bytes(data)
        except OSError:
            pass
        self._send(200, data, "image/jpeg")

    def _route_fonts(self, name: str) -> None:
        fonts_dir = (Path(__file__).resolve().parent / "fonts")
        target = (fonts_dir / name).resolve()
        if not target.is_relative_to(fonts_dir) or not target.is_file():
            self._not_found("no such font")
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "font/woff2")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(data)

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
            converted = None
            if path is not None and path.suffix.lower().lstrip(".") not in _BROWSER_SAFE:
                # Most browsers cannot render HEIC/TIFF/PSD originals;
                # serve a converted, cached JPEG instead.
                converted = view_image_bytes(conn, photo_id, self.server.phototext_views)
            elif path is None:
                # Original offloaded by iCloud (or the file vanished):
                # fall back to the cached view so the detail page keeps
                # rendering whatever pixels we still have.
                converted = view_image_bytes(conn, photo_id, self.server.phototext_views)
        finally:
            conn.close()
        if converted is not None:
            self._send(200, converted, "image/jpeg")
            return
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

    def _route_open_photos(self, photo_id: int, params: dict, ctx: dict) -> None:
        """Show the photo in Photos.app (AppleScript 'spotlight').

        Works on read-only servers like /reveal — it only tells the local
        Photos app to reveal an asset by its library UUID. The first use
        triggers macOS' Automation permission prompt for the terminal
        running phototext.
        """
        conn = _open_ro(self.server.phototext_db)
        try:
            uuids = db.asset_uuids(conn, photo_id)
        finally:
            conn.close()
        if not uuids:
            self._not_found("photo has no Photos-library asset id")
            return
        osascript = shutil.which("osascript")
        if osascript is None:
            self._not_found("opening Photos needs macOS (`osascript`)")
            return
        # The same photo can exist as several library assets; rows for
        # copies since deleted from Photos are stale but harmless — try
        # each id (newest first) until Photos resolves one.
        for uuid in uuids:
            try:
                proc = subprocess.run(
                    [
                        osascript, "-e",
                        'tell application "Photos" to spotlight '
                        f'(media item id "{uuid}")',
                    ],
                    check=False,
                    timeout=15,
                )
            except subprocess.TimeoutExpired:
                break
            if proc.returncode == 0:
                break
        self.send_response(303)
        self.send_header("Location", f"/photo/{photo_id}")
        self.send_header("Content-Length", "0")
        self.end_headers()


def serve(
    db_path: Path | str,
    host: str = "127.0.0.1",
    port: int = 8765,
    writable: bool = False,
    person_cfg: Config | None = None,
    theme: str | None = None,
) -> None:
    global _default_theme
    db_path = Path(db_path).expanduser()
    if not db_path.exists():
        raise FileNotFoundError(
            f"catalog not found: {db_path} — run `phototext scan` first"
        )
    thumbs_dir = db_path.parent / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    ensure_noindex(thumbs_dir)
    views_dir = db_path.parent / "views"
    views_dir.mkdir(parents=True, exist_ok=True)
    ensure_noindex(views_dir)
    token = secrets.token_hex(16) if writable else None
    if theme is not None and theme in webtheme.THEMES:
        _default_theme = theme
    httpd = _Server(
        (host, port), _Handler, db_path, thumbs_dir, views_dir, writable, token, person_cfg
    )
    actual_host, actual_port = httpd.server_address[:2]
    mode = "read-only" if not writable else "read-write (hide/delete/restore/purge + people enabled)"
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
