# Changelog

All notable changes to phototext are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
semantic versioning.

## [Unreleased]

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
