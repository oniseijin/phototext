# Changelog

All notable changes to phototext are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
semantic versioning.

## [Unreleased]

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
