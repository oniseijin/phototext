# phototext — Design

A local, resumable pipeline that scans photo sources (iPhoto/Photos libraries or
folders), sends each photo to a local Ollama vision model, and stores extracted
text plus photo context in a queryable SQLite catalog. Run it in spare cycles,
stop anytime, restart later — it picks up exactly where it left off.

## Use cases

1. **Text recovery (primary, M1)** — extract all visible text from every photo,
   verbatim, with a short description of the photo for context.
2. **Search photos by their text (M2)** — "find the photo with the invoice number",
   "which screenshot mentioned the API key". Served by an FTS5 index over
   `text` + `context`.
3. **Meme identification (backlog)** — identify reusable meme images: photos with
   overlaid caption text that recur across time/sources. Design sketch in
   _Backlog_ below.
4. **General photo context search (M2/M3)** — the `context` field makes photos
   findable by what they depict, not just their text.

## Core principles

1. **Content-addressed identity** — every photo is identified by the SHA-256 of
   its bytes, not its path. Duplicates across albums/folders are processed once;
   resume is stable across library reorganizations; rescans are cheap.
2. **Durable state machine** — SQLite is the single source of truth. Each photo
   moves `queued -> processing -> done | error` in committed transactions.
3. **Read-only sources** — the library is never modified; originals are read in
   place (temp files only for format conversion).
4. **Cheap and interruptible** — time budgets (`--stop-after`), graceful Ctrl+C,
   crash recovery, single-photo transactions, low resource use.

## Architecture

```
 folder walker        iPhoto / Photos library package
 (recursive)          (.photolibrary / .photoslibrary -> Originals/Masters/originals)
       └────────────────────┬────────────────────────────┘
                              ▼  scan: hash, dedup, upsert
                       ┌───────────────┐
                       │ SQLite catalog│  sources / photos / locations
                       └──────┬────────┘
                              ▼  queue (photos.status = 'queued')
        worker loop (single process):
          claim -> prepare image (decode, EXIF-orient, downscale <= 1024px JPEG;
                    Pillow + pillow-heif, with macOS `sips` fallback for formats
                    Pillow cannot decode: old iPhoto formats, PSD, some RAW)
               -> Ollama /api/chat (structured JSON output, temperature 0)
               -> normalize + store (text, context, text_kind, language,
                  model, raw_response, duration)
                              ▼
                   results / status / retry / doctor
```

## Data model (SQLite, `~/.phototext/catalog.db`)

- `sources` — registered scan roots: `id`, `kind` (`folder` | `library`), `uri`
  (unique).
- `photos` — one row per unique image:
  - identity: `sha256` (unique), `byte_size`, `first_seen_at`
  - state: `status` (`queued|processing|done|error`), `attempts`, `error`,
    `started_at`, `finished_at`, `duration_ms`
  - results: `has_text`, `text` (verbatim transcription), `context` (1-2 sentence
    description), `text_kind`, `language`, `model`, `raw_response` (full model
    output, kept so parsing can be changed later without re-running inference)
- `locations` — many per photo: `photo_id`, `source_id`, `path`, `mtime`, `size`;
  unique per `(source_id, path)`.

Schema changes go through versioned migrations in `src/phototext/db.py`
(`MIGRATIONS` dict + `schema_version` table). Never edit an applied migration;
add a new version.

## Scan stage

- Library packages are detected by suffix; the walker descends only the originals
  directory (`originals`, `Originals`, `Masters`, `masters`).
- Image candidates: known image extensions (including HEIC, PSD, PICT, common
  RAW), plus extensionless files sniffed via Pillow. Dotfiles, sidecars (`.aae`),
  and videos are skipped.
- Fast path: `(path, mtime, size)` match against `locations` skips re-hashing.
- New/changed files are hashed; hash collision with an existing row dedups; new
  locations on an existing hash update `locations` only.
- Walk errors (usually Full Disk Access) are collected and reported with a hint.

## Resume semantics

- `run` owns the catalog at startup: **all** `processing` rows are requeued
  (single-process design; a hard kill cannot leave permanent stuck rows). If
  multi-process is ever added, replace this with real leases
  (`started_at` + expiry check on claim).
- The worker claims one photo at a time (`SELECT ... LIMIT 1` then
  `UPDATE ... WHERE status='queued'` guard).
- Ctrl+C / SIGTERM: finish the current photo, commit, exit. Second signal force
  quits. `--stop-after` and `--limit` bound a run the same way.
- Transport loss (Ollama down mid-run): pause-and-retry with backoff; if it stays
  down, the current photo is requeued and the run exits nonzero — nothing is lost.
- Model failures (HTTP 5xx, unparseable output): retried up to `max_attempts`
  (tracked in `attempts`), then `status='error'` with the message and any raw
  output. `phototext retry` requeues errors and resets attempts.

## Ollama integration

- `POST /api/chat` with base64 JPEG, `stream: false`, `format` = JSON schema
  (Ollama structured outputs; `temperature: 0`, `num_ctx: 8192`), and
  `think: false` (with an automatic retry without the parameter for Ollama
  versions that reject it). Thinking is disabled because gemma4's reasoning mode
  generates 1500-2000+ tokens on dense documents — 4+ minutes per photo with no
  transcription-quality gain. Measured on the same image: thinking 1974+ tokens
  (incomplete after 4 min) vs `think: false` 152 tokens / 20.7 s with a perfect
  verbatim transcription.
- Preflight on `run` and in `doctor`: server reachable, model installed, and a
  16x16 test image returns usable JSON (catches non-vision models).
- Prompt asks for **verbatim transcription** (preserve line breaks and reading
  order), a brief context description, a `text_kind` classification, and language.
  Response schema (kept small for small-model compliance):

```json
{
  "has_text": false,
  "text": "",
  "context": "",
  "text_kind": "document|sign|screenshot|handwriting|scene|menu|label|book|other|none",
  "language": ""
}
```

- `raw_response` stores the full model output for every photo, so output parsing
  or classification can be revised later without re-running inference.

## Slices (M2)

A slice is a scan-time filter over registered sources: `--album`, `--date-from`,
`--date-to`, `--favorites`, `--limit N`, or a file of UUIDs/paths. Sources emit
candidates; filters narrow; only the slice is queued. (Decision: "slice" =
filtered subset, not a separate export.)

## CLI surface

```
phototext scan [PATHS...] [--album A] [--date-from D] [--date-to D]
                     [--favorites] [--limit N] [--ids-file F]
                     # slice filters restrict what is registered/queued
phototext run [--stop-after D] [--model TAG] [--limit N] [--no-scan] [--skip-preflight]
              [--album A] [--date-from D] [--date-to D] [--favorites] [--ids-file F]
phototext status                    # counts, throughput, ETA, recent errors
phototext results [--status S] [--full] [--n N]
phototext search QUERY [--limit N]  # FTS5 over text + context
phototext export [--format jsonl|csv] [--status S] [--output F]
phototext migrate [--dry-run]       # apply pending schema migrations (backup first)
phototext serve [--host H] [--port P] # read-only web UI (browse/search/reveal)
phototext retry                     # requeue failed photos
phototext reprocess [--errors|--no-text|--tiled|--all|--ids-file F] [--model M]
                                     # requeue selected photos for re-extraction
phototext doctor                    # config, db, ollama, model, vision checks
```

Global: `--config PATH`, `--db PATH`. Config file `~/.phototext/config.toml`
(see README for keys). The installer's `phototext` wrapper passes
`--config <prefix>/var/config.toml` so the installed copy uses its own
structured `var/` state; `phototext-dev` runs the workspace code with the
default `~/.phototext` state.

## Failure modes

| Case | Behavior |
| --- | --- |
| Corrupt/undecodable image | `error` status with message; `sips` fallback tried first |
| No-text photos | `has_text=false`, context still stored |
| Photo file moved/deleted | `error` with "no readable file"; rescan updates locations |
| Ollama down at start | preflight fails, nothing started, exit 1 |
| Ollama drops mid-run | backoff retries; then requeue + exit 1 |
| Model output unparseable | retries, then `error` with raw output saved |
| Duplicate photos | single result via content hash |
| Library unreadable | warning + Full Disk Access hint |

## Performance (measured)

M2 Pro, 16 GB, `gemma4:12b` Q4_K_M (GPU/Metal), `max_image_edge` 1024:
- ~19 s/photo warm (319 prompt tokens, ~230 output tokens)
- ~30-60 s model load on first photo after idle
- Scan/hashing: I/O-bound, ~thousands of photos/minute

At ~180 photos/hour, large libraries take days of accumulated run time — which is
the point of the resume design. Speed knobs: `max_image_edge`, a smaller vision
model, `num_ctx`.

## Milestones

### M1 — core pipeline (implemented)

- Folder + iPhoto/Photos library scanning via originals walk; HEIC/PSD/RAW/PICT
  support with `sips` fallback
- Content-hash dedup, locations, incremental rescan fast path
- Resumable single-process worker; budgets; graceful stop; crash recovery
- Ollama structured extraction, preflight, retries, transport-loss safety
- `scan / run / status / results / retry / doctor`
- Self-contained e2e suite (mock Ollama): `tests/e2e.py`

### M2 — slices, search, export (implemented, 0.2.0)

- Slice filters (album/date/favorites/limit; `osxphotos` for Photos libraries,
  iPhoto via its `Database/apdb` SQLite or mtime fallback)
- FTS5 virtual table over `text` + `context` (sync via triggers), `phototext search`
- `phototext export --format jsonl|csv`
- Schema migrations with pre-migration backups (`phototext migrate`,
  `<db_dir>/backups/`), applied on connect and by the installer
- `install.sh`: snapshot install to `/opt/phototext` or `~/.local/opt/phototext`
  with `var/` data layout, bin wrappers, `--uninstall/--purge/--test`,
  and a `phototext-dev` wrapper for workspace code

### M3 — UI and quality (in progress)

- Local web UI (read-only): browse photos + recovered text, search box
  — **shipped**: `phototext serve` (stdlib http.server, read-only catalog
  connection, cached thumbnails, FTS search, per-photo source info +
  reveal-in-Finder via `open -R`)
- `phototext reprocess --model <better>` for upgrades (raw_response already kept)
  — **shipped**: `reprocess` with `--errors/--no-text/--tiled/--all/--ids-file`
  selectors; `--model` prints the matching run hint
- Quadrant tiling for dense documents (receipts): split into 4 tiles, merge text
  — **shipped** as the automatic fallback in `worker._extract_photo`: whole
  image -> anti-loop retry -> 4 overlapping tiles from original resolution,
  merged; `photos.tiled` flag (migration 3), surfaced in results/export/web UI

### Backlog

- **Meme identification**: add a perceptual hash (`phash`, e.g. 64-bit dHash/pHash)
  column in a migration; cluster photos by hamming distance to find repeated
  images; combine with `text_kind`/caption text to flag likely memes
  (recurring image + overlaid text). Surface as `phototext memes` or a UI view.
- ~~Idle detection~~ — **shipped**: before each photo the run checks `/api/ps`
  and pauses while a foreign model is loaded (default on, `--no-idle-detection`
  or `idle_detection = false` to disable, `idle_poll_s` to tune).
- Hard mode: targeted reprocess of a flagged subset (e.g. suspected
  handwriting, empty extractions) with thinking enabled — thinking is too slow
  as a default but might help on genuinely hard photos
- Text-only mode: a cheap fast vision model gates the full pass ("any visible
  text?"), so textless photos finish in seconds — only worth it for very
  large libraries where context descriptions are not wanted
- ~~Watch mode~~ — **shipped** (`--watch`, `--watch-interval`)
- ~~Multi-process workers~~ — **shipped** (`--workers N` with claim leases,
  stale-lease recovery, parent-liveness guards; most useful with
  parallel-capable or remote inference backends)
- ~~Meme identification~~ — **shipped** (63-bit dhash at scan time +
  `phototext memes` + web Memes tab)
- ~~Two-tier gate / categories~~ — **shipped** (`--two-tier`, free-form
  model-chosen categories, `phototext categories`, `reprocess --gated`)
- ~~Hide/delete/trash~~ — **shipped** (catalog tombstones, `hidden` filter,
  `--writable` web actions, `unscan` for source removal; migrations 6)
- ~~Photo warnings~~ — **shipped** (`photo_warnings` table, scan/process capture,
  `max_image_pixels` decompression-bomb guard without sips fallback; migration 7)
- ~~iCloud inventory~~ — **shipped** (osxphotos-driven `.photoslibrary` scan:
  full originals + derivative previews + `deferred` cloud-only rows,
  auto-promotion on download; migration 8)

## Decision log

- **Stack**: Python 3.11+, stdlib SQLite (WAL), requests, Pillow + pillow-heif,
  typer. `osxphotos` deliberately deferred to M2 (iPhoto libraries are accessed
  via the filesystem, which works for both iPhoto and Photos).
- **Slice** = scan-time filters over registered sources (user decision).
- **Web UI** deferred to M3; SQLite + FTS5 keeps that a thin read-only layer.
- **`sips` fallback** (user suggestion): macOS CLI tooling is fair game for
  library/format handling; Pillow remains primary.
- **Thinking disabled** (`think: false`): reasoning mode gave no quality gain on
  OCR but unbounded latency. If a future "hard mode" is wanted (e.g. difficult
  handwriting), it should be a targeted reprocess of a subset, not the default
  (see backlog).
- **Startup ownership recovery** over time-based leases: v1 is single-process, and
  a hard kill must never leave stuck rows for a fast-restart user.
- **iPhoto first, folders too**: the same walker covers both; library packages get
  originals-directory detection.
- **`osxphotos` promoted to a hard dependency in M2** (user's libraries are
  Photos): needed for album/favorites/UUID slices on `.photoslibrary` packages.
  iPhoto keeps the stdlib adaptive apdb reader — do not silently switch it to
  osxphotos.
- **Installer = snapshot + dev wrappers** (user decision): the installed
  `phototext` runs the code snapshot taken at install time, never the
  workspace; `phototext-dev` (and repo `bin/phototext-dev`) runs workspace code
  via the repo `.venv` with `~/.phototext` state. Prefix auto-selects `/opt`
  (sudo/root) else `~/.local/opt`; data is contained in `<prefix>/var/` by a
  wrapper injecting `--config <prefix>/var/config.toml` (no code-side layout
  branching). Wrappers are generated for every `pyproject.toml` entry point so
  an M3 server installs automatically; every entry point must therefore accept
  the standard global `--config/--db` flags.
- **Migrations with backups**: `db.connect()` backs up any pre-existing catalog
  (SQLite backup API, `<db_dir>/backups/`) before applying pending migrations;
  `phototext migrate [--dry-run]` does it explicitly and the installer runs it
  on every upgrade — a failed upgrade must leave the catalog recoverable.
- **FTS5 as an external-content table** (`photos_fts`) synced by triggers that
  fire only when `text`/`context` actually change; migration 2 rebuilds
  existing rows. `search` falls back to quoting the whole query as a single
  phrase when FTS5 rejects it (syntax errors, unknown column filters).
- **iPhoto apdb schema is detected at runtime**: iPhoto's database was never
  documented and varies by version, so tables/columns and uuid-vs-rowid join
  kinds are discovered by introspection and sampling; anything unexpected is a
  hard error pointing at date/path slices (the "mtime fallback").
- **Web UI is stdlib and strictly read-only** (user decision): `http.server`
  with no client-side frameworks, catalog opened `mode=ro` + `query_only`,
  thumbnails cached under `<db_dir>/thumbs` (ids are content-stable so the
  cache never goes stale). "Reveal in Finder" (`open -R`) is the only action
  endpoint — browsers block `file://` links from `http://` pages, and reveal
  works for photos inside library packages too.
- **Idle detection is default-on and checked per claim**: before each photo
  the worker reads `/api/ps` and pauses (polling `idle_poll_s`) while any
  *foreign* model is loaded; our own model or an idle server proceeds.
  Model names are compared with `:latest` stripped (Ollama reports
  `llama3:latest` for a configured `llama3`). Unreachable during the check
  reuses the transport-loss retry path. Opt-out: `--no-idle-detection` or
  `idle_detection = false` in the config.
- **Output is token-capped (`num_predict`, 2048 default), newline runs are
  collapsed, and unparseable output triggers one anti-loop retry**: measured on
  a real library, temperature-0 greedy decoding hit newline repetition loops
  on text-dense photos (e.g. 7775 tokens / 479 s of `\n`, blowing the request
  timeout). The cap bounds worst-case latency; `normalize_result` collapses
  3+ consecutive newlines; and when the JSON is truncated by the cap, the
  client retries once with `temperature 0.7` + `repeat_penalty 1.2` — the
  penalty alone did NOT break the loop, the temperature bump does. Request
  timeouts are bounded attempts (not infinite transport retries).
- **Tiling is a fallback, folded into one extraction ladder** (user decision):
  whole image -> anti-loop retry -> quadrant tiling. Tiling multiplies model
  calls by ~5, so it only triggers when the single pass cannot produce
  parseable output; tiles are cut from the ORIGINAL resolution (each tile gets
  the full `max_image_edge` budget, ~2x the linear resolution of the whole
  image pass) with 8% overlap. `photos.tiled` (migration 3) marks the
  photos that needed it, and `reprocess --tiled [--model M]` re-selects them
  for a better model or a future improved strategy.
- **Textless photos: full pass by default, two-tier gate as an option** (user
  decision, revisited): the `context` description is a first-class feature, so
  single-tier (default) keeps the rich full-model description for every
  photo. `two_tier = true` (or `--two-tier`) adds a cheap fast gate model
  (default `gemma3:4b`, 512px input) that answers "any visible text?" plus
  a one-line description and a **category**; textless photos finish at
  the gate (~seconds, still searchable and described), text-bearing photos
  proceed to the full pass. Tracking: `photos.gated` (migration 5) + the
  `model` column record the tier; `reprocess --gated` redoes gate-tier
  photos with the full model (misclassification recovery). Category is
  filled by both tiers from a shared enum and is filterable in the web
  UI — its usefulness grows with catalog size. Gate failures
  (parse/timeout) fall through to the full pass; the gate must never make
  things worse. Above all, **every photo stays in the catalog regardless of
  text** (user decision): the point of tiers and categories is filterability
  (has_text, category, full-text content) — never exclusion.

- **Delete means tombstone** (user decision): `delete`/`hide` only edit the
  catalog; files are never touched. Trash + restore + purge compose; every
  query (results, search, export, run claims, web) filters tombstones, and
  `status_counts` excludes them so deleted photos stop appearing as queued.
  `unscan` forgets a source and its unique photos; content seen elsewhere
  survives via its other locations.
- **Web UI stays read-only by default** (user decision): writes require
  `serve --writable`, a session token embedded in action forms, and same-origin
  POSTs (Origin header check). Read-only servers 404 write routes entirely.
- **Pixel policy, no fallback** (user decision): `max_image_pixels`
  (default ~357M, Pillow's own limit) is a hard budget — a suspected
  decompression bomb is a recorded error, deliberately *without* the `sips`
  fallback, because shelling out to decode a bomb defeats the guard.
  Softer warnings (PIL etc.) are captured per photo in `photo_warnings`
  (UserWarning scope) and surfaced in `results`, `status`, and the web UI.
- **iCloud libraries are inventories, not just files** (user decision: both
  options): `.photoslibrary` sources are enumerated via `osxphotos` — a
  superset of the originals walk. Local originals are normal photos; cloud-only
  photos get a `deferred:<uuid>` identity with status `deferred` (no local
  pixels) or `queued` + `derivative = 1` (processed best-effort from a local
  preview under `resources/derivatives/`). When the original downloads, the
  next scan promotes the deferred row into the real content hash row and
  requeues it. The scan summary reports the three-way split (`previews`,
  `awaiting download`).
