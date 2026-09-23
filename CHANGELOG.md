# Changelog

All notable changes to phototext are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
semantic versioning.

## [Unreleased]

### Added

- Web UI theme system (`src/phototext/webtheme.py`): three themes — iCloud
  (default), **Machine** and **Samaritan** (Person of Interest
  surveillance aesthetic) — via CSS custom properties + `data-theme`
  attribute, a no-FOUC restore script, and a sidebar toggle; per-browser
  choice persists in `localStorage`, and a `web_theme` config value sets
  the server default.
- Self-hosted theme fonts (Barlow Semi Condensed + JetBrains Mono, SIL
  OFL; licenses bundled) served from `/fonts/` — the PoI themes work fully
  offline.
- PoI element treatments: bracketed subject frames with designation tags
  on photo detail pages and person page headers, terminal-styled recovered
  text, hover-acquisition brackets on grid cards (machine), hairline card
  frames (samaritan), mono snippet captions, and a semantic REC dot that
  pulses while the queue is active.
- Duplicates view: keep suggestion carries a PoI `KEEP` designation;
  derivatives get a `derivative` badge.
- E2E section [48] (theme tokens parity, toggle, no-FOUC ordering, fonts
  route + traversal guard, config default, color-literal gate).

## [0.8.0] - 2026-09-22

### Added

- **mlx-serve backend**: `provider = "ollama" | "mlx-serve"` selects the
  LLM backend; a `[mlx-serve]` config table overlays per-provider model
  names (`model`, `prefilter_model`, `person_model`, `embed_model`) at load,
  so switching back is a one-line change with the ollama names kept as the
  base. New `mlx_client.py` mirrors `OllamaClient`'s method surface and
  exception types over the OpenAI-compatible API (`/v1/chat/completions`
  with image content parts + `json_schema` structured output,
  `/v1/embeddings`, `/v1/models`); `make_client(cfg)` (clients.py) is the
  single construction point for worker/cli/people. Idle-pause coordination
  is a no-op on mlx-serve (models coexist under LRU/budget rules).
  Existing text/description results are model-agnostic (no reprocessing
  needed); embeddings re-populate incrementally per model
  (`photo_embeddings` is keyed by model, search is model-scoped).
  e2e: `PHOTOTEXT_E2E_PROVIDER=mlx-serve tests/e2e.py` runs the full suite
  against `tests/mock_mlx.py`.

## [0.7.0] - 2026-09-20

### Added

- **Bulk delete in the web UI**: any filtered view (hidden-only, person,
  category, status, text, year, date range, or a search) now offers a
  confirmed "delete all N in view" button on writable servers. The
  selection is recomputed server-side from the same filters, unfiltered
  requests are refused, and nothing is ever deleted automatically —
  removing a photo from the Photos app leaves it in phototext until you
  choose otherwise.
- **Cache cleanup**: `purge` now removes the photo's cached
  thumbnails/views along with its rows, and the trash view gained a
  "purge all" button (web `POST /bulk-purge`, CLI `purge --empty-trash`).
  New `phototext clean-caches` command garbage-collects cache files
  orphaned by purges that predate the cleanup.

## [0.6.2] - 2026-09-20

### Fixed

- **"Open in Photos" failed for photos that existed as several library
  assets** (e.g. a duplicate you since deleted in Photos): the route
  referenced only the first-known asset id, which may no longer resolve.
  It now tries every known asset id — newest registration first — until
  Photos resolves one.

## [0.6.1] - 2026-09-20

### Fixed

- **"Open in Photos" failed with** `execution error: Photos got an error:
  Can't get media item id "…" (-1728)`. AppleScript's `media item id`
  lookup is case-sensitive, but asset UUIDs were stored lowercased.
  `photo_assets.uuid` is now kept in its original case with NOCASE
  collation (migration 14), and the migration repairs existing rows by
  recovering the true case from the originals/ file name Photos derives
  from the UUID.

## [0.6.0] - 2026-09-20

### Added

- **Free local OCR at scan time (tier-0)**: macOS Vision
  (`VNRecognizeTextRequest`, ~150ms/photo, no model) records what it can
  read into `photos.vision_text` (migration 12, also indexed by FTS5) —
  photos become searchable before the LLM pass ever reaches them, and
  `phototext backfill-ocr` catches up existing catalogs. When the two-tier
  gate is on, photos with Vision text skip the gate call entirely (they
  clearly have text). `vision_ocr` config (default on; silently a no-op
  without Vision).
- **Semantic search**: `phototext embed` builds text embeddings via
  Ollama `/api/embed` (set `embed_model`, e.g. `nomic-embed-text`) into
  `photo_embeddings` (migration 13); `phototext search --semantic QUERY`
  ranks by cosine similarity — merged with the existing person/year/date
  filters — and `phototext similar <photo-id>` finds nearest neighbors.
  Answers "the red receipt photo" without keyword overlap with the stored
  text.
- **Sidebar that scales**: the web sidebar's global nav (Library /
  Discover / Utilities / search) is now pinned while filter groups
  (Views / People / Categories / Text / Years) scroll in their own
  region as collapsible groups with link counts. People and Categories
  groups with more than 8 links grow a client-side filter box (type
  "rya" to find "Ryan" among 150); years cap their scroll height.

## [0.5.0] - 2026-09-19

### Changed

- The web UI is laid out like iCloud Photos: a left sidebar carries the
  search field, the Library / Discover / Utilities navigation, and every
  filter (hidden, people, categories, text, years) instead of stacked
  chip rows across the top. All features, URLs, and actions are
  unchanged — same routes, same writable gate, same composing filters,
  now with room to grow. Narrow windows collapse the sidebar on top.

### Added

- `recent_first` config: claim the newest photos first (capture date,
  falling back to first-seen) while a long backlog runs, so yesterday's
  photos surface during nightly runs instead of 2013's. Default stays
  FIFO (oldest first).
- `process_derivatives` config: when false, iCloud-only photos with a
  local preview wait as `deferred` until the real original downloads,
  instead of spending a full model call on a low-resolution preview.
- `reprocess --done-with MODEL`, `--done-before DATE`, and `--category
  NAME` — re-run exactly the photos extracted by an older model, before
  a date, or in a category, instead of `--all`.

### Fixed

- Meme/duplicate clustering is exact but no longer quadratic: a BK-tree
  replaces the O(n^2) scan, keeping the web Memes/Duplicates tabs usable
  at 15k+ photos (minutes → seconds). Verified against the naive scan
  on randomized data.
- The two-tier gate now downscales to `prefilter_max_edge` (512px,
  previously defined but never used) before the prefilter model call.
- `run --workers N` propagates worker exit codes: a night where every
  worker aborted (dead backend) now exits non-zero instead of reporting
  success to cron.

## [0.4.1] - 2026-09-19

### Fixed

- Model calls now enforce `request_timeout_s` as a hard wall-clock budget
  on the whole call. Previously it was only a per-read socket timeout, so
  a backend that kept the connection open (or trickled bytes) could hold
  a "timed out" call indefinitely — a nightly `run --stop-after` could
  overshoot its budget and Ctrl+C could not take effect between photos.
  A trickling server is now cut off after the budget and the photo
  records the usual timeout error.

## [0.4.0] - 2026-09-18

### Added

- iCloud Optimize Storage resilience: Photos-library assets are tracked by
  UUID (`photo_assets`), so when iCloud evicts an original *after* the photo
  was processed, the scan keeps the processed row (flagged `offloaded`),
  attaches the local Photos preview as its location instead of registering
  a duplicate, and restores the original location when it downloads again.
  `unscan` forgets the asset map with the source.
- The library's hidden flag imports into phototext's hidden: photos hidden
  in Photos (or in an iPhoto apdb, best-effort) drop out of views, search,
  export, and the web UI's default grid, and appear in the hidden view.
  Phototext's own hide/unhide choices always win — rescans never override
  them, and un-hiding in the library unhides only what the library hid.
- `phototext cache-previews` backfills the web thumbnail and detail-view
  caches for every photo with local pixels (resumable; `--thumbs-only` for
  the grid cache only). Run it before enabling Optimize Storage so photos
  stay viewable no matter what iCloud evicts.
- "open in Photos" link on the photo detail page — shows the photo inside
  the Photos app via AppleScript `spotlight` on the asset UUID. Allowed on
  read-only servers like reveal; the first use triggers macOS' Automation
  permission prompt.
- Scan summaries report `offloaded N` and `library-hidden N`.

### Fixed

- Rescanning a library whose originals iCloud has evicted no longer
  duplicates already-processed photos as new `deferred:` rows.
- The web `/image/` route falls back to the cached detail view for any
  photo whose original is gone; previously browser-safe photos (JPEG) had
  no cached view and simply 404'd.

## [0.3.4] - 2026-09-18

### Fixed

- The web UI hidden filter now composes with every other filter instead of
  being dropped when one is changed: person, category, text, status, and
  year chips all keep the current hidden view (and each other) in their
  links. Person pages gained a matching `hidden (N)` view, so a person's
  hidden photos are reachable there too.

## [0.3.3] - 2026-09-18

### Added

- Web UI hidden view: a `hidden (N)` chip on the browse page lists only
  hidden photos (hidden cards carry an "unhide" toggle); toggling a photo
  returns to the list it came from instead of the photo page.
- One-click hide/unhide toggles on grid cards in writable mode
  (`serve --writable`), with a "hidden" badge on hidden photos. The detail
  page keeps its toggle; read-only servers show neither.

## [0.3.2] - 2026-09-18

### Fixed

- The web UI detail page did not display photos stored as HEIC (or TIFF and
  other formats most browsers cannot render): the page served the original
  file bytes, so the image appeared only in thumbnails and card views.
  Non-browser-safe originals are now converted to JPEG on first view and
  cached under `<state>/views/`; JPEG/PNG/GIF/WebP/BMP still stream raw.
- The face-box picker now scales drawn boxes by the photo's true display
  size (embedded as `data-w`/`data-h`), so tagging stays pixel-accurate
  even when the served image is a downscaled conversion.

## [0.3.1] - 2026-09-18

### Fixed

- A run could exit with `'utf-8' codec can't encode character ... surrogates
  not allowed` when the model emitted an unpaired surrogate escape (half an
  emoji) in its JSON: the escape survived parsing but cannot be stored in
  SQLite. Model output is now sanitized at parse time (lone surrogates
  become U+FFFD) and the same bug can no longer kill an overnight run.
- Files with unencodable (mangled) filenames are skipped with a warning and
  counted in the scan summary (`unencodable-names`) instead of aborting the
  scan.

### Changed

- The progress console now replaces unencodable characters instead of
  crashing on them, so a stray filename or model character cannot end a
  piped run.

## [0.3.0] - 2026-09-17

### Added

- Photo capture dates (migration 10): new photos record their EXIF
  `DateTimeOriginal` at scan time; `phototext backfill-dates [--from-mtime]`
  catches up existing rows. Date slices (`--date-from/--date-to`) prefer the
  EXIF date and fall back to the file date.
- Timeline browsing: `phototext search --year/--date-from/--date-to`, web
  year chips, date filters, and the taken date on photo pages.
- Near-duplicate finder: `phototext duplicates [--threshold 4] [--json]`
  clusters resized/re-encoded copies by perceptual hash (iCloud preview
  proxies excluded) with a keep-the-largest-file hint, plus a web
  Duplicates tab. Read-only — files are never touched.
- People feedback loop: `phototext people confirm <photo> <name> --add-seed`
  (web: `confirm+seed`) grows a person's seed pool from verified tags; the
  recognition profile rebuilds from those anchors with
  `phototext people describe`. Person pages show seed counts.
- Face detection via the macOS Vision framework (`pyobjc` is now a
  dependency): `people run` skips the model call entirely for photos
  without faces and matches on close-up face crops otherwise;
  `phototext people name` without `--box` auto-adopts a lone detected
  face (and refuses an ambiguous multi-face photo). `doctor` reports
  Vision availability; `face_detection` in the config turns it off.

### Changed

- `phototext people confirm` now creates the tag when none exists instead
  of silently doing nothing.

## [0.2.0] - 2026-09-17

### Added

- People identification and tagging (migration 9). Name a person from one
  photo — in the web UI by dragging a box around a face, or
  `phototext people name <photo-id> <name> --box x,y,w,h` — and the model
  builds a recognition profile from the seed crop(s).
- `phototext people run`: a matching pass that evaluates every photo
  against all registered people in a single model call per photo
  (defaults to the fast `person_model`, i.e. the prefilter). Resumable;
  absent verdicts are remembered so re-runs only cover new ground.
- Confidence + review queue: model tags below `person_min_confidence`
  (0.6) are flagged for review; confirm or remove them in the web UI or
  via `phototext people confirm/remove`. Confirmed and seed tags are
  ground truth the model never overwrites.
- `phototext people list/photos/rename/reset/delete/describe` management
  commands; `reset` drops model tags but keeps seeds and confirmations.
- Web UI People tab: person cards with face crops, per-person pages with
  the review queue, and (with `--writable`) the face-box picker, tag
  chips with confirm/remove, and rename/reset/delete — all behind the
  existing session-token + Origin gate. `?person=` filter and
  `phototext search --person NAME` narrow results to a person.

## [0.1.0] - 2026-09-17

Initial release.

### Added

- Content-hash catalog: photos identified by SHA-256, never by path;
  duplicate content is processed once. Fast rescan path (mtime/size).
- Text extraction via a local Ollama vision model with an escalation ladder:
  whole image, then the client's anti-loop retry, then quadrant tiling cut
  from original resolution (`run`, `reprocess --tiled`).
- Optional two-tier gate: a cheap prefilter model lets textless photos finish
  with a short description + category (`two_tier`, `reprocess --gated`).
- Model-chosen free-form categories (`phototext categories`) and FTS5
  full-text search over recovered text + context, with phrase fallback.
- Export results as JSONL or CSV (`export`).
- Slices: scan/run filters by album, favorites, dates, limit, or an ids-file
  of paths/UUIDs (iPhoto libraries via the adaptive apdb reader, Photos
  libraries via osxphotos).
- Local web UI (`serve`): browse/search/detail pages, cached thumbnails,
  reveal-in-Finder, source locations. Opt-in write mode (`--writable`)
  with session-token and Origin checks for hide/delete/restore/purge.
- Idle detection: a run pauses while a different model is loaded in Ollama
  and resumes when it unloads (`--no-idle-detection` to disable).
- Memes: perceptual-hash clustering of near-identical photos that carry text
  (`phototext memes`, with a web view).
- Watch mode (`--watch`) re-scans on an interval; multi-process workers with
  atomic claims and stale-lease recovery (`--workers N`).
- Catalog lifecycle: `hide`/`unhide`, `delete`/`restore`, `trash`, `purge`
  are tombstones in the catalog — image files are never touched. `unscan`
  forgets a source and photos only seen there.
- Per-photo decode warnings and a hard `max_image_pixels` budget against
  decompression bombs (deliberately no `sips` fallback for those).
- iCloud-aware `.photoslibrary` scans: full local originals, best-effort
  local previews (`derivative`), or `deferred` rows for cloud-only photos
  that are promoted automatically once downloaded. Scan summaries report
  the split (full images, previews, awaiting download).
- Named profiles (`--profile <name>`, `phototext profiles`): alternate
  catalogs with optional per-profile `config.toml`.
- Schema migrations with automatic pre-migration backups (`phototext
  migrate`; applied automatically on connect).
- Installer (`install.sh`): snapshot venv under `~/.local/opt/phototext`,
  `phototext` / `phototext-dev` wrappers, self-upgrade, `--uninstall
  [--purge]`, e2e self-test.
- Shell completion for bash and zsh (`phototext autocomplete`,
  `--install-completion`).
- `--version`, `doctor` diagnostics, and a built-in usage guide
  (`phototext help [COMMAND]`).
- Spotlight (`mds`) stays out of catalog/thumbnail/model directories via
  `.metadata_never_index` markers.
