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
                   search, export, migrate, serve, reprocess, help,
                   hide/unhide/delete/restore/purge/trash, unscan,
                   categories, memes, autocomplete, profiles, people
                   (name/run/list/photos/confirm/remove/rename/reset/
                   delete/describe)
  config.py        dataclass Config, ~/.phototext/config.toml (TOML) loading
  db.py            SQLite schema, migrations (+ backups), FTS5 sync, queries
  imaging.py       hashing, decode/downscale/encode, quadrant tiles, sips
                   fallback, test image
  library_meta.py  album/favorites/UUID -> paths: osxphotos (Photos) or
                   adaptive iPhoto apdb reader
  people.py        person seeds (face crops), recognition profiles, the
                   `people run` matching pass (one call per photo)
  memes.py         63-bit dhash, backfill, hamming clustering (union-find)
                   (warnings capture, bomb guard, deferred/derivative iCloud
                   inventory live in scanner/db/worker — see invariants)
  ollama_client.py /api/chat + /api/tags + /api/ps, structured output,
                   preflight, two-tier gate call, person describe/match
                   calls, anti-loop retry, parsing
  prompt.py        prompts, response schema, tile merge, normalization
  scanner.py       source resolution, walk, dedup, fast path, Slice filters
  webui.py         read-only local web UI (http.server): browse/search/detail,
                   thumbnails, reveal-in-Finder
  worker.py        run loop: claim/process, retries, budgets, signals
bin/phototext-dev  dev wrapper: workspace code via repo .venv
install.sh         installer: snapshot venv, var/ layout, bin wrappers, migrate
CHANGELOG.md       release notes; update when bumping the version
tests/
  mock_ollama.py   stand-in Ollama server (modes: ok, fail500, slow)
  e2e.py           full pipeline test suite
```

## Architecture invariants

- Photos are identified by **SHA-256 content hash**, never by path. `locations`
  maps one photo to many paths/sources; duplicate content is processed once.
- `photos.status` state machine: `queued -> processing -> done | error`. Every
  transition is a committed transaction.
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
  the full model). Gate failures fall through to the full pass. Categories
  are model-chosen free-form short labels (normalize via
  `prompt.normalize_category`); discover them with `phototext categories`.
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
  with locations elsewhere survives. `purge` is the only row deletion.
- **Web writes are opt-in**: `serve --writable` turns on POST-only hide/
  unhide/delete/restore/purge routes guarded by a session token + Origin
  check; read-only servers 404 them. Keep the token checks intact.
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
  deferred iCloud inventory/promotion, named profiles, and people
  (name/box/seed crop, matching with uncertain review queue, confirm/remove,
  reset keeping user tags, search filter, web picker + person pages). Run it
  after any change to scanner/worker/db/ollama_client/cli/library_meta/
  webui/memes/people/prompt.
- macOS `realpath` resolves `/var` -> `/private/var` and can normalize path case
  (`Originals` -> `originals`); never assert exact path strings.

## Environment notes

- cli.py sets stdout to line-buffered so piped/`tee`'d overnight runs show live
  progress. Do not remove.
- State dirs (and `~/.ollama`, via install.sh) carry `.metadata_never_index`
  markers so Spotlight/mds stays out of the catalog, thumbnails, and model
  blobs. Keep dropping them wherever new state dirs are created.
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
all shipped; 0.5.0: tombstones + writable web, warnings + pixel policy,
iCloud deferred inventory).
