# phototext

Recover text from your photos using a local Ollama vision model. phototext scans an
iPhoto/Photos library (or any folder), sends each photo to a local vision LLM
(e.g. `gemma4:12b`), and stores the extracted text plus a short description of each
photo in a local SQLite catalog.

It is built for long, interruptible runs: stop it anytime, restart later, and it picks
up exactly where it left off. Nothing is ever written into your photo library.

## How it works

1. `scan` walks the library's originals (read-only) and identifies every photo by its
   SHA-256 content hash. Duplicate photos (same bytes in multiple places or albums)
   are processed once.
2. `run` claims queued photos one at a time, converts/downscales the image (HEIC,
   TIFF, PSD, old iPhoto-era formats, with a macOS `sips` fallback for anything
   Pillow cannot read), and sends it to Ollama with a JSON-schema-constrained
   prompt. The verbatim text, a context description, the text kind, and the language
   are stored per photo.
3. Everything lands in `~/.phototext/catalog.db` (SQLite). Inspect it with
   `phototext results`, `phototext status`, or plain SQL.

Progress is crash-safe: each photo is a single transaction, and photos interrupted
mid-run are automatically recovered on the next `run`.

## Requirements

- macOS
- Python 3.11+
- [Ollama](https://ollama.com) running locally with a vision model
  (default: `gemma4:12b`; any multimodal tag works)

## Install

### As an everyday command

```bash
cd phototext
./install.sh            # installs to ~/.local/opt/phototext (or /opt with sudo)
phototext doctor        # wrapper lands in ~/.local/bin
```

The install is a self-contained snapshot with a structured data dir:

```
~/.local/opt/phototext/venv/      python env + code snapshot
~/.local/opt/phototext/var/        config.toml, catalog.db, backups/, log/
~/.local/bin/phototext            runs the installed snapshot
~/.local/bin/phototext-dev         runs the workspace code via the repo's .venv
```

Re-run `./install.sh` to upgrade: it refreshes the snapshot and migrates the
catalog schema (a pre-migration backup is kept in `var/backups`).
`./install.sh --uninstall [--purge]` removes it (`--purge` also deletes the
catalog). The installed command and `phototext-dev` never share config or
catalog.

### For development

```bash
uv venv .venv && uv pip install -e .
.venv/bin/phototext ...            # or: bin/phototext-dev
```

## Quickstart

```bash
.venv/bin/phototext doctor                        # check Ollama, model, vision, database
.venv/bin/phototext scan "~/Pictures/Old iPhoto Library.photolibrary"
.venv/bin/phototext run --limit 5                 # small first test
.venv/bin/phototext results --full                # see what was extracted
.venv/bin/phototext run                           # process everything
```

## Commands

| Command | What it does |
| --- | --- |
| `phototext scan [PATHS...]` | Register and scan sources (folders or photo libraries). Omit paths to rescan all registered sources. |
| `phototext run` | Scan + process queued photos until done, budget, or Ctrl+C. |
| `phototext status` | Counts, throughput, ETA, recent errors. |
| `phototext results` | Show recent extractions (`--status done/error/all`, `--full` for untruncated text). |
| `phototext search QUERY` | Full-text search (FTS5) over recovered text and context; FTS5 syntax, phrases in double quotes. |
| `phototext export` | Export results as JSONL or CSV (`--format jsonl\|csv`, `--status`, `--output`). |
| `phototext retry` | Requeue failed photos. |
| `phototext reprocess` | Requeue selected photos for re-extraction: `--errors`, `--no-text`, `--tiled`, `--gated`, `--all`, or `--ids-file` of ids/paths. |
| `phototext categories` | List the categories the models assigned, with counts. |
| `phototext hide / unhide <ids...>` | Hide photos from default views (catalog only; files untouched). |
| `phototext delete <ids...>` | Move photos to the catalog trash (tombstone; files untouched). |
| `phototext restore <ids...>` | Restore photos from the trash. |
| `phototext purge <ids...>` / `phototext purge --empty-trash` | Forget trashed photos permanently. |
| `phototext trash` | List trashed photos. |
| `phototext unscan <source>` | Forget a registered source (by id or path) and photos only seen there. |
| `phototext memes` | Find near-identical photos (perceptual hash clusters), likely memes. |
| `phototext people name ID NAME` | Seed a person from a photo (`--box x,y,w,h` to crop the face); the model builds a recognition profile. |
| `phototext people run` | Tag people across the library — one model call per photo checks every named person. |
| `phototext people list / photos NAME` | People with tag counts; a person's photos with confidence. |
| `phototext people confirm / remove ID NAME` | Mark a tag correct (ground truth) or remove it. |
| `phototext people rename / reset / delete NAME` | Rename; drop model tags (keeps confirmed); delete (`--yes`). |
| `phototext search --person NAME QUERY` | Full-text search within a person's tagged photos. |
| `phototext migrate` | Apply pending catalog schema migrations (backs up the catalog first). |
| `phototext serve` | Local web UI: browse photos + recovered text, search box (`--host`, `--port`, `--writable` for hide/delete actions). |
| `phototext doctor` | Diagnose config, database, Ollama, model, vision support (prints the version first). |
| `phototext --version` | Print the installed version (`doctor` shows it too). |
| `phototext autocomplete` | Install TAB completion for the `phototext` command into your shell profile (bash or zsh). |
| `phototext profiles` | List named profiles (alternate catalogs) with photo counts. |
| `phototext help` | The built-in usage guide (workflows, iCloud notes, `help <command>`). |

Useful `run` options:

| Option | Effect |
| --- | --- |
| `--stop-after 90m` | Stop after a time budget (`45s`, `2h`, `1h30m`, ...). |
| `--limit 50` | Process at most N photos this run. |
| `--model TAG` | Override the model for this run. |
| `--no-scan` | Skip rescanning sources. |
| `--skip-preflight` | Skip the Ollama startup check. |
| `--no-idle-detection` | Do not pause while other models are loaded in Ollama. |
| `--watch` | Keep running: watch sources for new photos and process them as they appear (`--watch-interval`). |
| `--workers N` | Parallel worker processes (for parallel/remote inference backends; a single local model slot serializes anyway). |
| `--two-tier` | Check each photo with the cheap prefilter model first; textless photos finish there in seconds with a short description and category. |

Global options: `--config PATH` to use a specific config file, `--db PATH` to use a
specific catalog, `--profile NAME` to switch to an alternate catalog.

## iPhoto / Photos libraries

Point `scan` at the library package (`.photolibrary` / `.photoslibrary`). For
iPhoto libraries phototext walks only the originals inside (`Originals/`,
`Masters/`, or `originals/`). For Photos libraries it reads the library database
via `osxphotos` and inventories **every** photo — local originals are processed
as full images, iCloud-only photos with local previews are processed from those
previews (flagged `derivative`), and photos with no local pixels are recorded as
`deferred` (nothing to extract until they are downloaded). The scan summary shows
the split: `new N | previews N | awaiting download N`. Deferred photos are promoted
automatically on the next scan after they download. Plain folders work the same way.

Reading a library requires **Full Disk Access** for your terminal app:
System Settings -> Privacy & Security -> Full Disk Access. Without it, folders
inside the library are silently unreadable (phototext prints a warning if it
detects this).

## Slices (scan-time filters)

`scan` and `run` accept filters that restrict what gets queued; everything
else is left untouched:

```bash
phototext scan --favorites                       # only favorites/flagged
phototext scan --album "Trip 2014"               # one album
phototext scan --date-from 2013 --date-to 2013   # by file date
phototext scan --limit 200                       # first 200 new photos
phototext scan --ids-file ids.txt                # listed paths or UUIDs
phototext run --album "Trip 2014"                # scan + process just the slice
```

Album, favorites, and UUID lookups read the library's own database (Photos
libraries via `osxphotos`, iPhoto via its `Database/apdb`, schema detected at
runtime with clear errors if it cannot be parsed). Date and path filters work
on plain folders too. Photos already queued from earlier scans are still
processed by `run`.

## Web UI

```bash
phototext serve                 # http://127.0.0.1:8765
```

A read-only local web app over the catalog: a card grid of photos (cached
thumbnails generated from the originals), status tabs, full-text search, and
per-photo pages with the recovered text, context, metadata, the raw model
response, and **where the photo is really stored** — every on-disk location
with its source (which folder or which iPhoto/Photos library), an on-disk
status, and a *reveal in Finder* link that opens the real file's location,
even inside a `.photolibrary`/`.photoslibrary` package. It binds to loopback
only, opens the catalog read-only (`mode=ro` + `query_only`), and never
writes to the library. Ctrl+C stops it.

There is also a **People** tab: person cards with face crops, and per-person
pages with a review queue for uncertain model tags. With `serve --writable`
the photo page grows a face-box picker — drag a rectangle around a face,
type a name, and the person is seeded (recognition profile built on the
spot) — plus confirm/remove buttons on every person chip and rename/reset/
delete on the person page. All writes stay behind the session token and
Origin check.

## Long runs and resume

- Ctrl+C (or SIGTERM) stops gracefully after the current photo; a second Ctrl+C
  forces an immediate quit.
- Idle detection: before each photo the run checks `/api/ps` and pauses while a
  *different* model is loaded in Ollama (e.g. you are using a coding model),
  resuming when it unloads. Disable with `idle_detection = false` or
  `--no-idle-detection`.
- Dense photos (receipts, signs) that the single-pass extraction cannot parse
  automatically fall back to **quadrant tiling**: the photo is split into four
  overlapping tiles cut from the original resolution (each tile gets the full
  `max_image_edge` budget — roughly 2x the linear resolution of the whole-image
  pass), extracted separately, and merged. Tiled photos are flagged in
  `results`/export/web UI; redo them later with a better model via
  `phototext reprocess --tiled --model <tag>`.
- **Every photo stays in the catalog** regardless of text — the point is
  filterability: `has_text` (with text / no text), the model-assigned
  `category` (`phototext categories` lists what exists; filter in `results`,
  `search --category`, and the web UI), and full-text search over text +
  context. Categories are chosen by the model from what each photo shows,
  not from a fixed list.
- **Memes**: `phototext memes` clusters near-identical photos by perceptual
  hash and flags groups that carry text — the classic re-shared image. The
  web UI has a Memes tab with the same clusters.
- **People**: name a person once and tag them everywhere. On a photo in the
  web UI (writable mode) drag a box around a face and type a name — or
  `phototext people name <photo-id> "Ryan" --box x,y,w,h`. The model writes a
  recognition profile from the seed crop(s), then `phototext people run`
  checks every photo in one call per photo (all people at once, using the
  fast `person_model`). Tags carry confidence; below `person_min_confidence`
  they land in a review queue. Confirm/remove in the web UI or via
  `people confirm/remove`; `people reset` drops model tags but keeps your
  confirmations; `search --person Ryan "invoice"` searches within a person.
  Seed face crops are stored under `<state>/people/`.
- `--stop-after 2h` bounds a run, e.g. overnight or "while I'm at lunch".
- Re-running `run` only processes what is still queued; already-done photos are
  never re-processed.
- Hard kills (crash, power loss) are safe: interrupted photos are recovered
  automatically on the next `run`.
- Output is line-buffered, so `phototext run | tee overnight.log` shows live
  progress.

## Profiles

A profile is a separate catalog (plus optional settings) under the state
directory — split purposes (memes vs documents), models per purpose, or
experiments without touching the main catalog:

```bash
phototext --profile memes scan ~/Pictures/memes
phototext --profile memes run
phototext profiles          # list profiles with photo counts
```

Profile state lives in `<state>/profiles/<name>/`: the catalog is
`catalog.db`, and a `config.toml` there (optional) *replaces* the base
config for that profile — it can be a one-liner like
`model = "gemma3:4b"` for a cheap meme pass (everything else falls back
to defaults). Thumbnails and migration backups stay inside the profile
dir, so profiles are fully isolated. An explicit `--db` still wins over
the profile's catalog.

## Configuration

`~/.phototext/config.toml` is created with defaults on first use. CLI flags
override it.

| Key | Default | Meaning |
| --- | --- | --- |
| `ollama_url` | `http://localhost:11434` | Ollama server. |
| `model` | `gemma4:12b` | Any vision-capable model tag. |
| `max_image_edge` | `1024` | Longest image edge (px) sent to the model. Smaller is faster. |
| `max_output_tokens` | `2048` | Cap on generated tokens per photo; bounds repetition loops. Dense documents are ~500-1000 tokens. |
| `num_ctx` | `8192` | Model context window; image tokens count against it. |
| `max_attempts` | `3` | Model-call attempts per photo before it is marked failed. |
| `request_timeout_s` | `300` | HTTP timeout for one model call. |
| `transport_retries` / `transport_backoff_s` | `3` / `30` | What to do if Ollama drops mid-run. |
| `structured_output` | `true` | Ollama JSON-schema-constrained output; set `false` if the model struggles. |
| `idle_detection` | `true` | Pause the run while a different model is loaded in Ollama (`/api/ps`). |
| `idle_poll_s` | `15` | How often to re-check while paused. |
| `lease_timeout_s` | `3600` | Multi-worker runs: reclaim photos from dead workers after this long. |
| `two_tier` | `false` | Cheap prefilter model gates the full pass; textless photos finish at the gate. |
| `prefilter_model` | `gemma3:4b` | The gate model for two-tier mode. |
| `person_model` | *(prefilter)* | Model for person matching + recognition profiles. |
| `person_min_confidence` | `0.6` | Model tags below this confidence wait in the review queue. |
| `prefilter_max_edge` | `512` | Image size for gate calls (smaller is faster). |
| `max_image_pixels` | `357913941` | Hard decode budget per image (~357 MP). Suspected decompression bombs are recorded as errors — deliberately without the `sips` fallback. |
| `db_path` | `~/.phototext/catalog.db` | SQLite catalog location. |

## Performance

Measured on an M2 Pro (16 GB), `gemma4:12b` Q4_K_M, `max_image_edge` 1024:
**~20 s per photo** after the model is warm (about 180 photos/hour). The first
photo after an idle period includes a model load (~30-60 s). Larger images and
denser text take longer.

The pipeline sends `think: false` with every request. Gemma4's reasoning mode
burns 1500-2000+ tokens on dense documents (4+ minutes per photo, with no
transcription-quality gain) — OCR needs perception, not reasoning. If Ollama ever
rejects that parameter, the client automatically retries without it.

For a big library, expect multi-day total run time. That is what the resume design
is for: run it in spare cycles (`--stop-after 2h`) and let it accumulate. To go
faster: lower `max_image_edge`, or use a smaller vision model via `--model`.

## Testing

```bash
.venv/bin/python tests/e2e.py
```

Self-contained: spawns a mock Ollama server, generates a fake iPhoto-style
library (with an apdb), and covers scanning/dedup, happy path, failures,
retry, transport loss, crash recovery, budgets, SIGINT, doctor, sips fallback,
full-text search, schema migration with backups, export, and slice scans
(dates, limit, ids-file, album/favorites/UUID via the library database). No
network or real library involved.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `doctor`: server not reachable | Start Ollama (`ollama serve` or the app). |
| `doctor`: model not present | `ollama pull gemma4:12b` or set `model` in the config. |
| `doctor`: failed on test image | The model is not vision-capable; pick a multimodal tag. |
| Scan finds nothing in a library | Grant Full Disk Access to your terminal app, then rescan. |
| Some photos show as `error` | `phototext results --status error`, then `phototext retry`. |
| Unreadable image errors | Usually a format even `sips` cannot read; the path is recorded so you can inspect it. |

## Roadmap

- **M2 (done, 0.2.0)**: slices (album/date/favorites/limit/ids-file), FTS5
  full-text search, JSONL/CSV export, schema migrations with backups, installer.
- **M3**: local web UI to browse photos alongside their recovered text;
  re-processing with a better model; quadrant tiling for dense documents.
- **Backlog**: meme identification (perceptual-hash clustering of repeated images
  with overlaid text), idle detection (pause while Ollama is busy), watch mode.

See `DESIGN.md` for the full design and decision log.
