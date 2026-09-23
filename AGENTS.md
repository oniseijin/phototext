# AGENTS.md

Guidance for AI coding agents working in this repo.

## Commands

- Setup: `uv venv .venv && uv pip install -e .`
- Run CLI (dev, workspace code): `.venv/bin/phototext ...` or `bin/phototext-dev`
- Install as an everyday command: `./install.sh` — snapshot venv at
  `~/.local/opt/phototext` (or `/opt/phototext` with sudo), wrappers
  `~/.local/bin/phototext` (installed snapshot, `var/` state) and
  `~/.local/bin/phototext-dev` (workspace code, `~/.phototext` state).
  Re-run to upgrade; `--uninstall [--purge]`; `--test` runs e2e against the
  installed venv.
- Tests: `.venv/bin/python tests/e2e.py` — self-contained, spawns a mock Ollama
  server and a generated fake iPhoto-style library (with an apdb) in a temp
  dir. No network, no real library, no real Ollama.
- No linter/typechecker is configured. Keep code stdlib-first and compatible with
  Python 3.11+.

## Project layout

```
src/phototext/
  cli.py           typer commands; global --config/--db; scan/run slices,
                   search (--person/--year/--date-from/--date-to,
                   --semantic), export, migrate, serve, reprocess, help,
                   hide/unhide/delete/restore/purge [--empty-trash]/trash,
                   unscan, backfill-dates, backfill-ocr, embed, similar,
                   cache-previews, clean-caches, categories, memes,
                   duplicates, autocomplete, profiles, people
                   (name/run/list/photos/confirm [--add-seed]/remove/rename/
                   reset/delete/describe)
  config.py        dataclass Config, ~/.phototext/config.toml (TOML) loading
  db.py            SQLite schema, migrations (+ backups), FTS5 sync, queries
  faces.py         macOS Vision face detection (display-pixel boxes,
                   PHOTOTEXT_TEST_FACES seam for e2e, graceful degradation)
  imaging.py       hashing, decode/downscale/encode, EXIF date read,
                   quadrant tiles, sips fallback, crop_jpeg, test image
  library_meta.py  album/favorites/UUID -> paths: osxphotos (Photos) or
                   adaptive iPhoto apdb reader; iter_photos_assets (uuid,
                   path, hidden) + PHOTOTEXT_TEST_ASSETS seam; apdb hidden
                   flag; find_derivative
  ocr.py           macOS Vision OCR (VNRecognizeTextRequest) tier-0:
                   vision_text at scan time, PHOTOTEXT_TEST_OCR seam,
                   graceful degradation
  people.py        person seeds (face crops), recognition profiles, the
                   `people run` matching pass (face prefilter + crops)
  memes.py         63-bit dhash, backfill, hamming clustering via BK-tree
                   (union-find, exact); shared by memes view and the
                   duplicates finder (tight threshold + derivative exclusion)
  ollama_client.py /api/chat + /api/tags + /api/ps, structured output,
                   preflight, two-tier gate call, person describe/match
                   calls, anti-loop retry, parsing
  prompt.py        prompts, response schema, tile merge, normalization
  scanner.py       source resolution, walk, dedup, fast path, EXIF
                   date_taken at registration, Slice filters; Photos
                   library scan (asset map, offload demote, hidden sync)
  webtheme.py     PoI theme system: TOKENS ×3 (icloud default, machine,
                   samaritan), `--pt-*` CSS variables, no-FOUC restore JS,
                   3-state toggle; self-hosted OFL fonts under fonts/
  webui.py         read-only local web UI (http.server): iCloud-style sidebar
                   chrome (Library/Discover/Utilities + filter groups),
                   browse/search/detail, year timeline, thumbnails, people
                   pages, memes + duplicates tabs, reveal-in-Finder,
                   open-in-Photos (all known asset ids, newest first),
                   views/thumbs caches, writable bulk-delete/bulk-purge
                   routes + cache GC helpers
  worker.py        run loop: claim/process, retries, budgets, signals
bin/phototext-dev  dev wrapper: workspace code via repo .venv
install.sh         installer: snapshot venv, var/ layout, bin wrappers, migrate
CHANGELOG.md       release notes; update when bumping the version
tests/
  mock_ollama.py   stand-in Ollama server (modes: ok, fail500, slow, trickle)
  e2e.py           full pipeline test suite
```

## Architecture invariants

- Photos are identified by **SHA-256 content hash**, never by path. `locations`
  maps one photo to many paths/sources; duplicate content is processed once.
- `photos.status` state machine: `queued -> processing -> done | error`. Every
  transition is a committed transaction.
- **Claim order** (`db.claim_next`): FIFO by id by default; `recent_first = true`
  orders by `COALESCE(date_taken, first_seen_at) DESC` so nightly runs surface
  recent photos first. `run --workers N` propagates child exit codes — a fully
  aborted night must exit non-zero for cron.
- `run` **owns the catalog at startup**: it requeues all `processing` rows
  (single-process design). If multi-process is added, replace this with real
  claim leases — do not silently weaken it.
- `attempts` counts failed model-call cycles; `max_attempts` caps them;
  `retry` requeues errors and resets attempts.
- `raw_response` stores the full model output so parsing/normalization can be
  revised without re-running inference. Preserve it.
- Schema changes: add a new entry to `db.MIGRATIONS` with an incremented version.
  Never edit or reorder existing migrations. Pending migrations are applied on
  `db.connect()` and via `phototext migrate`; a pre-existing catalog is backed up
  to `<db_dir>/backups/` first (`backup_catalog`, SQLite backup API). The
  installer runs `migrate` against `<prefix>/var/catalog.db` after upgrading the
  snapshot.
- A **slice** is a scan-time filter (`scanner.Slice`): only matching files are
  registered/queued; everything else (including existing statuses) is untouched.
  `run` applies slice filters only to its scan phase — photos already queued
  from earlier scans are still processed. Album/favorites/UUID membership is
  resolved to paths by `library_meta` (osxphotos for Photos, adaptive apdb read
  for iPhoto); unknown UUIDs and unparsable schemas are hard errors.
- Extraction is an **escalation ladder** in `worker._extract_photo`: whole
  image -> the client's internal anti-loop retry (temp 0.7 + repeat_penalty on
  unparseable output) -> quadrant tiling (`imaging.prepare_tiles`, cut from
  original resolution; merged by `prompt.merge_tile_results`). Tiled photos
  carry `photos.tiled = 1` (migration 3); `reprocess --tiled` re-selects them.
  Do not make tiling the default — it multiplies model calls by ~5.
- The **two-tier gate** (`two_tier`, off by default) runs a cheap prefilter
  model first; textless photos finish there with a short description +
  category (`photos.gated`, migration 5; `reprocess --gated` redoes them with
  the full model). Gate failures fall through to the full pass. The gate
  image is downscaled to `prefilter_max_edge` (512px) via
  `imaging.downscale_jpeg_bytes` — keep it cheap. Categories
  are model-chosen free-form short labels (normalize via
  `prompt.normalize_category`); discover them with `phototext categories`.
  `reprocess` also selects by `--done-with MODEL` / `--done-before DATE` /
  `--category NAME` — the "All/Missing" granularity for model swaps.
- **Workers**: `--workers 1` (default) keeps single-process startup ownership
  (`reset_processing`). `--workers N` spawns `phototext _worker` children with
  atomic claims and **stale-lease recovery** (`db.reclaim_stale`,
  `lease_timeout_s`) instead of startup reset — never add a blanket
  reset_processing to the multi-worker path. Children exit on their own if the
  parent dies (`os.getppid() == 1` check) — do not remove it.
- **Delete means tombstone**: `hide`/`delete` only edit the catalog (`hidden`,
  `deleted_at`, migration 6); files are never touched. All queries (claims,
  results, search, export, web, `status_counts`) exclude tombstones.
  `unscan <source>` forgets a source and photos only seen there — content
  with locations elsewhere survives. `purge` is the only row deletion and
  also removes the photo's cached `thumbs/<id>.jpg` / `views/<id>.jpg`
  (tombstoned photos keep them — the trash view and restore need them);
  `phototext clean-caches` garbage-collects cache files orphaned by purges
  that predate the cleanup.
- **Web writes are opt-in**: `serve --writable` turns on POST-only hide/
  unhide/delete/restore/purge routes guarded by a session token + Origin
  check; read-only servers 404 them. `POST /bulk-delete` tombstones every
  photo matching the posted view filters (hidden-only, person, category,
  status, text, year, dates, or search query) — it refuses unfiltered
  requests, recomputes the selection server-side from the same filters,
  and stays manual by design: photos are never trashed automatically when
  their asset disappears from the Photos library. Keep the token checks
  intact. `POST /bulk-purge` empties the trash from the web (same gate;
  the trash view carries a "purge all" button).
  `/reveal` and `/open-photos` are read-only GET routes on purpose (they
  only shell out to `open -R` / osascript `spotlight` locally); keep them
  out of the writable gate.
- **Pixel policy**: `max_image_pixels` (default ~357M) is a hard budget;
  `_bomb_check` raises and the error deliberately bypasses the sips
  fallback. Softer PIL warnings are captured per photo
  (`photo_warnings`, migration 7; UserWarning scope) and surfaced in
  results/status/web detail.
- **iCloud inventory** (migration 8): `.photoslibrary` sources are scanned
  via `osxphotos` (`_scan_photos_library`), not the walk. Cloud-only photos
  get a `deferred:<uuid>` identity — status `deferred` (no local pixels) or
  `queued` with `derivative = 1` (best-effort local preview under
  `resources/derivatives/`). The next scan promotes downloaded originals
  (`promote_deferred` collapses into the content-hash row). Scan summaries
  report the split (`previews`, `awaiting download`).
- **Offload demote** (migration 11): every asset seen is mapped in
  `photo_assets(source_id, uuid, photo_id)` (uuid raw-case, NOCASE
  collation as of migration 14 — AppleScript's `media item id` is
  case-sensitive, so "open in Photos" needs the true case; v14 repairs
  old lowercased rows from originals/ paths; the open-photos route tries
  every known asset id, newest first — deleting a duplicate in Photos
  leaves a stale row behind) — the link that
  survives iCloud Optimize Storage. When an asset's original disappears but
  the map knows the photo, `db.demote_offloaded` keeps the processed row
  (never requeues), prunes its dead locations, attaches the Photos preview
  derivative, and sets `photos.offloaded`; a returning original clears it in
  `promote_deferred`. Never let a demoted photo fall back into the
  `ensure_deferred_photo` path — that duplicates already-processed photos.
  `unscan`/`purge` clean the map (FK on photos.id). The web falls back to
  the `views/` cache for any photo without on-disk pixels; serve photos
  before enabling Optimize Storage with `phototext cache-previews`.
- **Vision OCR tier-0** (migration 12): `ocr.py` runs macOS Vision
  `VNRecognizeTextRequest` at scan time into `photos.vision_text`
  (FTS-indexed; `vision_ocr` config, default on, silent no-op without
  Vision; `PHOTOTEXT_TEST_OCR` seam; `backfill-ocr` for old rows;
  never overwrites non-NULL). Real Vision DOES read PIL-drawn text —
  tests that need "no text found" must use `make_plain_image`. In the
  two-tier gate, photos with vision_text skip the gate call entirely.
- **Embeddings** (migration 13): `photo_embeddings(photo_id, model, dims,
  vector BLOB)`; `phototext embed` (missing-by-default, `--all`) embeds
  `text + context` via Ollama `/api/embed` (`embed_model` config, empty =
  off); `search --semantic` cosine-ranks (stdlib array, precomputed norms)
  merged with the person/year/date facets; `similar <id>` nearest
  neighbors. The mock's /api/embed is deterministic (synonym-canonicalized
  word-hash vectors).
- **Library hidden sync** (migration 11): `photos.hidden_origin` is
  NULL | 'library' | 'user'. Scans apply the library's hidden flag via
  `db.apply_library_hidden` — 'user' rows (phototext's own hide/unhide,
  backfilled onto pre-existing hiddens by the migration) are never touched;
  a library unhide only clears rows the library hid. iPhoto walk-scans
  import hidden best-effort from the apdb (`hidden_iphoto_paths`, no hidden
  column = silent no-op). Hidden photos are still claimed by the worker —
  invisible in views/results/search/export, visible in the web hidden view.
- **Profiles** (`--profile <name>`) swap the state dir to
  `<config parent>/profiles/<name>/`: catalog `catalog.db`, optional
  `config.toml` that *replaces* the base config for that profile.
  Precedence: `--db` flag > profile catalog > config `db_path`. Name is
  validated (no path traversal). Worker children get an explicit `--db`,
  so profiles survive multi-worker runs.
- **Watch mode** (`--watch`) re-scans registered sources on an interval using
  the fast path (mtime/size) and queues new photos; the time budget still
  bounds the run.
- **People** (migration 9): `people` + `person_tags` (present, confidence,
  origin 'seed'|'user'|'model', box). Seed/user rows are ground truth —
  `db.tag_person`'s upsert never lets a model row overwrite them. Model
  matches below `person_min_confidence` stay as a review queue (they are
  stored, just flagged); absent verdicts are recorded with present=0 so
  re-runs skip them. Seed face crops live in `<db_dir>/people/<id>/`. The
  matching pass is one call per photo for all people (person model defaults
  to the prefilter); it is standalone like memes, not part of the extraction
  worker. Web person routes go through the same --writable + token + Origin
  gate as the other write actions.
- **People seeds** (migration 10): `person_tags.seed = 1` marks a tag as a
  profile anchor; `person_seed_photo_ids` selects by the seed flag, not
  origin. `people confirm --add-seed` (web: `confirm+seed`) grows the pool
  and writes the crop; `people describe` rebuilds from it. Migration 10
  backfills seed=1 onto existing origin='seed' rows — never remove that
  UPDATE.
- **Face detection** (`faces.py`, macOS Vision via pyobjc, `face_detection`
  config, default on): `people run` records absent for *everyone* on
  faceless photos without a model call (ground-truth rows skipped and not
  counted), and matches on up to `faces.MAX_FACES` crops instead of whole
  photos. If Vision is not importable, `run_matching` falls back to
  whole-photo matching with a warning rather than marking the library
  absent. `people name` without `--box` auto-adopts a lone face, errors on
  multiple. Boxes are display-pixel (EXIF-oriented) — same frame as
  `imaging.crop_jpeg` and the web picker. E2e drives the faces-found path
  via the `PHOTOTEST_FACES`-style seam `PHOTOTEXT_TEST_FACES` (documented in
  `faces.py`); real Vision on synthetic images returns no faces, which the
  suite uses for the skip path.
- **Date taken** (migration 10): `photos.date_taken` (ISO) is recorded at
  registration (`imaging.read_date_taken`, EXIF 36867 then 306, header
  read only). Rescans opportunistically fill NULLs for touched files;
  `backfill-dates` walks the rest (`--from-mtime` fallback). Date slices
  prefer date_taken and fall back to mtime. Search/browse filter on it
  (`--year`, `--date-from/--date-to`, web year chips) — photos without a
  date fall out of date-filtered results.
- **Duplicates** reuse `memes.find_clusters` with a tighter default
  (hamming <= 4) and `exclude_derivatives=True` so iCloud previews never
  pair with their originals; the memes view keeps derivatives. Read-only
  like everything else.
- **Web themes** (`webtheme.py`): three looks — icloud (default, visually
  the pre-0.9.0 UI), machine/samaritan (PoI) — as `--pt-*` CSS custom
  properties switched by the `data-theme` attribute on `<html>`, restored
  before first paint by a no-FOUC script (`pt-theme` localStorage,
  `web_theme` config sets the server default, browser choice wins).
  PoI-only treatments (subject brackets, designations, scanlines,
  hover-acquisition cards, mono captions) are gated to
  `html[data-theme='machine'/'samaritan']` selectors so icloud stays
  pixel-identical; keep it that way — new chrome must be theme-tokenized,
  never hardcoded (e2e [48] gates color literals out of webui.py). Fonts
  are self-hosted OFL woff2 under `src/phototext/fonts/` served at
  `/fonts/` (traversal-guarded, immutable cache).

## Testing rules

- Never point tests or dev runs at the user's real photo library or the real
  Ollama server. Use generated fixtures in a temp dir and `tests/mock_ollama.py`.
- The e2e suite covers: scan/dedup/library detection, fast rescan, happy path,
  model-failure errors, retry, transport-loss requeue, interrupted-run recovery,
  stop-after budgets, SIGINT graceful stop, doctor, sips fallback, duration
  parsing, FTS search (incl. phrase fallback), migrate + backups, jsonl/csv
  export, slice scans (dates, limit, ids-file paths/UUIDs, album/favorites
  via the generated apdb, run-with-slice), and the web UI (list/search/detail,
  thumbnails, image serving, source info + reveal validation, 404/traversal),
  idle detection (pause while a foreign model is loaded, resume,
  `--no-idle-detection`), tiling fallback, reprocess selections, watch mode,
  meme clustering + web view, multi-process workers with stale-lease
  recovery, the two-tier gate (categories, gated reprocess), the help
  command, hide/delete/trash + unscan, warnings + the bomb guard,
  deferred iCloud inventory/promotion, named profiles, people
  (name/box/seed crop, matching with uncertain review queue, confirm/remove,
  reset keeping user tags, search filter, web picker + person pages,
  confirm --add-seed + web confirm+seed), date taken (EXIF at scan,
  slice preference, backfill, search date filters, web timeline), the
  near-duplicate finder (CLI + json + web tab + derivative exclusion),
  and face detection (auto-box, multi-face refusal, face-crop matching,
  the no-faces skip, ground-truth survival, doctor), surrogate hardening
  (unpaired-surrogate model output sanitized at parse time, unencodable
  filenames skipped and counted), and iCloud offload + library hidden
  (seamed Photos-library scan via `PHOTOTEXT_TEST_ASSETS`: asset map,
  demote without duplicates, derivative relink, re-promote, cached-view
  fallback, hidden sync both directions with user-override survival,
  iPhoto apdb hidden import + no-op without the column, cache-previews,
  unscan cleaning the asset map), the queue-order/ops batch ([44]:
  recent_first vs FIFO claim order, reprocess --done-with/--done-before/
  --category, process_derivatives=false deferral, worker exit-code
  propagation on a dead backend), Vision OCR ([45]: seam scan stats,
  FTS-before-run, backfill graceful-empty, gate skip — with plain-image
  fixtures since real Vision reads PIL text), embeddings ([46]: embed
  missing-only and --all, semantic hit with zero FTS overlap, similar,
  error paths), and sidebar scaling ([47]: pinned nav, collapsible fgroups,
  filter boxes past 8 people/categories); the theme system ([48]: token
  parity, toggle + no-FOUC ordering, fonts route + traversal guard,
  web_theme config default, and the no-color-literals gate on webui.py);
  the ops batch ([21] extensions
  and [15]): v14 migration rewind + true-case repair from paths, raw-case
  asset storage, web bulk-delete (hidden-only and search scopes, token/
  400/404 gates, multiple-asset-row link survival), purge cache cleanup,
  clean-caches orphan GC with live-row retention, and bulk-purge from the
  trash view (button, route, empty-on-empty). APFS refuses to create
  invalid-UTF-8 filenames, so the scanner skip guard is exercised via the
  stubbed-walker check in e2e section [40] while the model-output path runs
  end-to-end
  against the mock's `surrogate` mode. The open-in-Photos route is never
  executed in e2e (it would launch Photos.app); only its links/404s are
  checked — osascript/AppleScript `spotlight` needs a manual smoke test.
  Run it
  after any change to scanner/worker/db/ollama_client/cli/library_meta/
  webui/memes/people/prompt/faces.
- Model output is sanitized for unpaired surrogates at the single parse
  choke point (`ollama_client.parse_model_json`); never parse model JSON
  outside it.
- macOS `realpath` resolves `/var` -> `/private/var` and can normalize path case
  (`Originals` -> `originals`); never assert exact path strings.

## Environment notes

- cli.py sets stdout to line-buffered so piped/`tee`'d overnight runs show live
  progress. Do not remove.
- State dirs (and `~/.ollama`, via install.sh) carry `.metadata_never_index`
  markers so Spotlight/mds stays out of the catalog, thumbnails, and model
  blobs. Keep dropping them wherever new state dirs are created. The repo
  root carries one too (committed) so the workspace — including `.venv` —
  stays out of the index.
- Default catalog: `~/.phototext/catalog.db`; default config:
  `~/.phototext/config.toml` (created on first use). Use `--config`/`--db` to
  isolate test/dev state. The installed wrapper overrides this: `phototext`
  (from install.sh) uses `<prefix>/var/config.toml` + `<prefix>/var/catalog.db`;
  only `phototext-dev` and bare dev invocations use the `~/.phototext` state.
- Measured on this machine (M2 Pro, 16 GB): `gemma4:12b` ~20 s/photo warm,
  ~30-60 s model load after idle. The client sends `think: false` (gemma4's
  reasoning mode runs 4+ min/photo on dense documents with no quality gain) —
  do not remove it. Keep this in mind for any manual smoke runs — bound them
  with `--stop-after`.

## Roadmap

See `DESIGN.md` for the full design, decision log, and milestones (M2 shipped:
slices, FTS search, export, migrations + installer; M3: web UI, idle
detection, reprocess, tiling, memes, watch mode, workers, two-tier gate —
all shipped; 0.3.x: tombstones + writable web, warnings + pixel policy,
iCloud deferred inventory, hidden view + composing filters; 0.4.0: iCloud
offload resilience + library hidden sync).
