# phototext — Design

A local, resumable pipeline that scans photo sources (iPhoto/Photos libraries or
folders), sends each photo to a local LLM server (Ollama or mlx-serve), and
stores extracted text plus photo context in a queryable SQLite catalog. Run it
in spare cycles, stop anytime, restart later — it picks up exactly where it left
off.

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
                -> LLM backend (Ollama /api/chat or mlx-serve
                   /v1/chat/completions; structured JSON output, temperature 0)
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
- Transport loss (LLM server down mid-run): pause-and-retry with backoff; if it stays
  down, the current photo is requeued and the run exits nonzero — nothing is lost.
- Model failures (HTTP 5xx, unparseable output): retried up to `max_attempts`
  (tracked in `attempts`), then `status='error'` with the message and any raw
  output. `phototext retry` requeues errors and resets attempts.

## LLM backends

Two interchangeable server backends behind `make_client(cfg)` in
`clients.py`; `provider = "ollama" | "mlx-serve"` picks one. Both run the
same prompt/schema at temperature 0 with the same retries and failure
semantics.

### Ollama (`provider = "ollama"`, default)

- `POST /api/chat` with base64 JPEG, `stream: false`, `format` = JSON schema
  (Ollama structured outputs; `temperature: 0`, `num_ctx: 8192`), and
  `think: false` (with an automatic retry without the parameter for Ollama
  versions that reject it). Thinking is disabled because gemma4's reasoning mode
  generates 1500-2000+ tokens on dense documents — 4+ minutes per photo with no
  transcription-quality gain. Measured on the same image: thinking 1974+ tokens
  (incomplete after 4 min) vs `think: false` 152 tokens / 20.7 s with a perfect
  verbatim transcription.
- Idle detection: before each photo the run checks `/api/ps` and pauses while
  a *foreign* model is loaded (see decision log).

### mlx-serve (`provider = "mlx-serve"`)

- OpenAI-compatible API: `POST /v1/chat/completions` with base64 image
  content parts + `response_format: json_schema`, `stream: false`,
  `temperature: 0`; `POST /v1/embeddings` for semantic search; `/v1/models`
  for residency checks. `think` is not sent (the server has no reasoning
  toggle to disable).
- `mlx_client.py` mirrors `OllamaClient`'s method surface (extract, gate,
  describe_person, match_people, embed, preflight, loaded_models) and
  *reuses its exception types and JSON parsing* — worker/retry/normalize
  logic is untouched by the backend switch.
- Idle-pause is a no-op here: mlx-serve keeps multiple models resident under
  its own LRU/memory budget rules, so there is no single model slot to
  yield.
- Model names live in a `[mlx-serve]` config overlay on top of the base
  (ollama) names — see the 0.8.0 decision-log entry.

### Shared (both providers)

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
phototext doctor                    # config, db, LLM server, model, vision checks
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
| LLM server down at start | preflight fails, nothing started, exit 1 |
| LLM server drops mid-run | backoff retries; then requeue + exit 1 |
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

mlx-serve (`gemma-4-e4b-it-4bit`, same machine, same 2h45m nightly window):
- 604 photos (~16.4 s/photo) vs 406 (~24 s/photo) on Ollama `gemma4:12b` —
  ≈1.5× the throughput on identical work
- Models load on demand server-side; loads add ~30-60 s only when the server
  cold-starts a model

## Milestones

### M1 — core pipeline (implemented)

- Folder + iPhoto/Photos library scanning via originals walk; HEIC/PSD/RAW/PICT
  support with `sips` fallback
- Content-hash dedup, locations, incremental rescan fast path
- Resumable single-process worker; budgets; graceful stop; crash recovery
- Ollama structured extraction, preflight, retries, transport-loss safety
- `scan / run / status / results / retry / doctor`
- Self-contained e2e suite (mock Ollama; provider-parameterized since 0.8.0):
  `tests/e2e.py`

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

### M3 — UI and quality (implemented, 0.2.0–0.3.0)

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
- **People identification** — **shipped** (0.2.0): seed crops + recognition
  profiles + a one-call-per-photo matching pass with a review queue
  (migration 9), web picker + person pages.
- **Capture dates** — **shipped** (0.3.0): EXIF `DateTimeOriginal` recorded at
  scan (migration 10), `backfill-dates`, date-preferring slices, search/web
  timeline filtering.
- **Near-duplicate finder** — **shipped** (0.3.0): tight-threshold dhash
  clustering reusing the meme machinery, derivative-excluded; CLI + web tab.
- **People feedback loop** — **shipped** (0.3.0): `person_tags.seed` anchors,
  `confirm --add-seed` / web `confirm+seed`, `people describe` rebuild.
- **Face detection** — **shipped** (0.3.0): macOS Vision (pyobjc) prefilter
  and face-crop matching in `people run`, auto-box on `people name`,
  doctor check.
- **Hidden view + card toggles** — **shipped** (0.3.3–0.3.4): web hidden-only
  view, per-card hide/unhide, filters that compose instead of replacing
  each other.
- **Offload demote + asset map** — **shipped** (0.4.0): `photo_assets`
  (source, uuid -> photo) survives iCloud Optimize Storage; `demote_offloaded`
  keeps processed rows when originals are evicted (never requeues, no
  duplicate `deferred:` rows), relinks the Photos preview derivative,
  `photos.offloaded` + web badge, `promote_deferred` clears it;
  `cache-previews` backfills the web caches as eviction insurance.
- **Library hidden sync** — **shipped** (0.4.0): `photos.hidden_origin`
  (NULL | 'library' | 'user'); the library's hidden flag imports and unimports
  in both directions, phototext's own hide/unhide verdicts always win
  (migration backfills existing hiddens as 'user'); iPhoto apdb hidden
  import best-effort via the adaptive reader.

### 0.8.0 — mlx-serve backend (implemented)

- **Provider selection**: `provider = "ollama" | "mlx-serve"` in the
  config; `make_client(cfg)` (clients.py) is the single construction point
  for worker/cli/people. Base model names stay the ollama tags; a
  `[mlx-serve]` table overlays per-provider names (`model`,
  `prefilter_model`, `person_model`, `embed_model`) at load — switching
  back is a one-line flip with the ollama names intact.
- **`mlx_client.py`** mirrors `OllamaClient`'s method surface and exception
  types over the OpenAI-compatible API (`/v1/chat/completions` with image
  content parts + `json_schema`, `/v1/embeddings`, `/v1/models`) — no
  generic "LLMBackend" interface, each provider file stays independently
  readable.
- **No reprocessing on switch**: text/description results are model-agnostic;
  `photos.model` records provenance. Embeddings are per-model
  (`photo_embeddings` keyed by `(photo_id, model)`), so an `embed_model`
  switch starts a fresh incremental set; search is model-scoped.
- **Idle-pause is a no-op on mlx-serve** (models coexist under LRU/memory
  rules — no `/api/ps` equivalent to poll).
- **e2e parameterized**: `PHOTOTEXT_E2E_PROVIDER=mlx-serve` runs the suite
  against `tests/mock_mlx.py` (a mock OpenAI-compatible server); suite
  green on both providers.

### 0.9.0 — PoI themes, lightbox, run reliability (implemented, branch `poi-themes`)

Theme system, lightbox, and the runtime items all implemented; both e2e
provider suites green. Port of the Person of Interest design system from the sibling
`video-security` app (its single source of truth: `report_theme.py`),
plus the runtime niceties worth taking from the same codebase. Decisions
below are locked (see decision log entries).

- **Theme system** — new `src/phototext/webtheme.py` mirroring
  video-security's `report_theme.py`: ~24 `--pt-*` tokens × 3 themes —
  `icloud` (the current look, baked default), `machine` (black +
  `#ff0000`, scanlines, glow, square corners), `samaritan` (white +
  `#e8000d`, reticle, hairline frames, no glow). Mechanism: `data-theme`
  attribute on `<html>` + `pt-theme` localStorage + a no-FOUC restore
  script emitted before the stylesheet; a 3-state toggle
  (`icloud -> machine -> samaritan`) in the pinned sidebar top. Purely
  client-side — read-only `serve` stays read-only. All ~40 color literals
  in `webui._CSS` become token references; ink/accent-ink pairs keep
  samaritan legible.
- **Element mapping**: detail-view images + person header crops get
  corner-bracket `.subject` frames with `.designation` tags (tone =
  status); recovered-text `pre` blocks get `.terminal` styling; the
  face-picker `.selbox` gets the face-box glow + red corner accent; a
  semantic REC dot in the sidebar (pulses while `queued > 0 or
  processing > 0`); scanlines, `::selection`, print + reduced-motion
  rules carried over from the source design system.
- **Grid cards**: hover-acquisition — brackets + tone glow appear only on
  the pointed card in machine (one at a time reads as "tracking"; on
  every card it reads as wallpaper); samaritan cards get a 1px hairline
  frame; mono/dim snippet + path in PoI themes; icloud untouched.
  Duplicates view: the keep-suggestion becomes the bracketed subject with
  a `KEEP` designation, derivatives get a dim `DERIVATIVE` tag.
- **Fonts**: self-hosted Barlow Semi Condensed + JetBrains Mono variable
  woff2 (~120-150 KB package data) served at `/fonts/` (same pattern as
  `/thumb/`), fallback stacks preserved, `LICENSES/OFL.txt` + README
  note. No Google Fonts `@import` — the UI is local-first; an online
  import stalls first paint and guts the theme offline.
- **Config**: `web_theme = "icloud"` sets the server default theme;
  per-browser localStorage still wins.
- **Lightbox** (follow-up commit): the framework-free zoom-to-cursor /
  pan / keyboard-nav lightbox from video-security's report module, wired
  to detail images (users zoom to read recovered text).
- **Run reliability** (from video-security's engine): CaffeinateGuard +
  AC-power warning (overnight runs must survive lid-close and warn on
  battery); `keep_alive: "30m"` + explicit model unload at clean run end
  (warm through watch-mode gaps, free ~8 GB after the nightly cron) —
  never port `_evict_other` single-model eviction (phototext's per-photo
  gate alternation would thrash loads); start-of-run disk preflight
  against the catalog volume.
- **Verification**: e2e additions — token-key parity across themes,
  toggle presence, theme CSS blocks, `/fonts/` 200 + content-type,
  duplicates-view designations — plus a grep gate that no color literal
  survives in `webui.py` outside token dicts; full suite green.

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
- ~~Text-only mode~~ — **shipped** as the two-tier gate (`two_tier`, prefilter
  short-circuits textless photos)
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
- **PoI themes via a token layer, icloud stays default** (user decision,
  0.9.0): the Machine/Samaritan design system ports from video-security's
  `report_theme.py` as a new `webtheme.py` (TOKENS dict + `data-theme`
  attribute + localStorage + no-FOUC restore) rather than restyling
  `webui.py` in place — webui keeps the server, the theme module is the
  single source of the design system and is importable by e2e for token-key
  parity. icloud (the current look) remains the baked default and becomes
  the third theme; machine/samaritan are opt-in per browser, and a
  `web_theme` config value sets the server default (localStorage wins).
  Fonts are self-hosted variable woff2 under OFL — an online `@import`
  would stall first paint and degrade offline, which defeats a
  local-first UI. Grid cards use hover-acquisition brackets (one framed
  card reads as "tracking"; forty read as wallpaper), mono snippets carry
  the text-is-the-product identity into the grid, and the duplicates view
  gets semantic KEEP/DERIVATIVE designations — the one place grid-level
  brackets carry meaning. (Implementation deviations, accepted: Barlow
  Semi Condensed has no published variable woff2 on any reachable mirror,
  so it ships as three static latin weights — JetBrains Mono stays
  variable; four near-identical icloud grays were consolidated into
  ink/nav tokens with icloud keeping its exact old values, and the 6px/12px
  radii became `radius-sm`/`radius-pill` tokens; all PoI treatments are
  gated behind `html[data-theme='machine'/'samaritan']` selectors so icloud
  renders unchanged, and e2e [48] gates color literals out of webui.py.)
- **Provider overlay, not parallel configs** (user decision, 0.8.0):
  `provider` selects the backend, but model names stay single-keyed — the
  base values are the ollama tags and a `[mlx-serve]` table overlays them
  only while that provider is active. Rollback is flipping one line; the
  ollama setup is never destructively edited. The mlx client deliberately
  re-implements the ollama client's surface and exception types rather
  than adapting both to a shared interface, so worker/retry/normalize logic
  is untouched and each provider file stays independently readable.
  Results are model-agnostic by construction (verbatim text + context +
  `raw_response`), so provider switches never require reprocessing; only
  embeddings are model-keyed and re-embed incrementally per model.
- **keep_alive + unload, never evict** (user decision, 0.9.0): port
  video-security's warm-window (`keep_alive: "30m"`) and clean-shutdown
  model unload, but NOT its single-model-residency eviction —
  phototext's two-tier gate alternates gate/main models per photo, so
  evict-on-switch would thrash model loads.
