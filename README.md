# phototext

Recover text from your photos using a local vision LLM. phototext scans an
iPhoto/Photos library (or any folder), sends each photo to a local LLM server —
Ollama (e.g. `gemma4:12b`) or mlx-serve (e.g. `gemma-4-e4b-it-4bit`) — and
stores the extracted text plus a short description of each photo in a local
SQLite catalog.

It is built for long, interruptible runs: stop it anytime, restart later, and it picks
up exactly where it left off. Nothing is ever written into your photo library.

## How it works

1. `scan` walks the library's originals (read-only) and identifies every photo by its
   SHA-256 content hash. Duplicate photos (same bytes in multiple places or albums)
   are processed once.
2. `run` claims queued photos one at a time, converts/downscales the image (HEIC,
   TIFF, PSD, old iPhoto-era formats, with a macOS `sips` fallback for anything
   Pillow cannot read), and sends it to the configured LLM backend (Ollama or
   mlx-serve) with a JSON-schema-constrained prompt. The verbatim text, a
   context description, the text kind, and the language are stored per photo.
3. Everything lands in `~/.phototext/catalog.db` (SQLite). Inspect it with
   `phototext results`, `phototext status`, or plain SQL.

Progress is crash-safe: each photo is a single transaction, and photos interrupted
mid-run are automatically recovered on the next `run`.

## Requirements

- macOS
- Python 3.11+
- One local LLM server with a vision model:
  - [Ollama](https://ollama.com) — the default provider; any multimodal tag
    works (e.g. `gemma4:12b`), or
  - mlx-serve (Apple silicon, OpenAI-compatible) — e.g.
    `mlx-community/gemma-4-e4b-it-4bit`

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
.venv/bin/phototext doctor                        # check the LLM server, model, vision, database
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
| `phototext search QUERY` | Full-text search (FTS5) over recovered text, context, and scan-time Vision OCR text; FTS5 syntax, phrases in double quotes. `--semantic` ranks by embedding similarity instead (`--person`, `--year`, `--date-from/--date-to` filters still apply). |
| `phototext embed` | Build text embeddings for semantic search via the configured provider (needs `embed_model` in the config; `--all` re-embeds). |
| `phototext similar <photo-id>` | Nearest photos by embedding similarity. |
| `phototext backfill-ocr` | Record macOS Vision OCR text for photos scanned before it existed (makes them searchable without the LLM pass). |
| `phototext export` | Export results as JSONL or CSV (`--format jsonl\|csv`, `--status`, `--output`). |
| `phototext retry` | Requeue failed photos. |
| `phototext reprocess` | Requeue selected photos for re-extraction: `--errors`, `--no-text`, `--tiled`, `--gated`, `--done-with MODEL` (after a model upgrade), `--done-before DATE`, `--category NAME`, `--all`, or `--ids-file` of ids/paths. |
| `phototext categories` | List the categories the models assigned, with counts. |
| `phototext hide / unhide <ids...>` | Hide photos from default views (catalog only; files untouched). |
| `phototext delete <ids...>` | Move photos to the catalog trash (tombstone; files untouched). |
| `phototext restore <ids...>` | Restore photos from the trash. |
| `phototext purge <ids...>` / `phototext purge --empty-trash` | Forget trashed photos permanently (also removes their cached thumbnails/views). |
| `phototext cache-previews` | Pre-generate the web thumbnail and detail-view caches for every photo with local pixels (`--thumbs-only` for grid tiles only). Run before enabling iCloud Optimize Storage. |
| `phototext trash` | List trashed photos. |
| `phototext clean-caches` | Remove cached thumbnail/view files whose photos no longer exist (`--dry-run` to preview). |
| `phototext unscan <source>` | Forget a registered source (by id or path) and photos only seen there. |
| `phototext memes` | Find near-identical photos (perceptual hash clusters), likely memes. |
| `phototext duplicates` | Find near-duplicate photos (resized/re-encoded copies), largest file highlighted; `--json`, `--threshold`. |
| `phototext backfill-dates` | Fill capture dates for photos scanned before they were recorded (`--from-mtime` for files without EXIF). |
| `phototext people name ID NAME` | Seed a person from a photo (`--box x,y,w,h` to crop the face; without it a lone detected face is auto-adopted); the model builds a recognition profile. |
| `phototext people run` | Tag people across the library — one model call per photo checks every named person (faceless photos skip the call). |
| `phototext people list / photos NAME` | People with tag counts; a person's photos with confidence. |
| `phototext people confirm / remove ID NAME` | Mark a tag correct (ground truth) or remove it (`--add-seed` also grows the profile). |
| `phototext people rename / reset / delete NAME` | Rename; drop model tags (keeps confirmed); delete (`--yes`). |
| `phototext people describe NAME` | Rebuild a person's recognition profile from their seed photos. |
| `phototext search --person NAME QUERY` | Full-text search within a person's tagged photos (`--year`, `--date-from/--date-to` narrow by capture date). |
| `phototext migrate` | Apply pending catalog schema migrations (backs up the catalog first). |
| `phototext serve` | Local web UI: browse photos + recovered text, search box (`--host`, `--port`, `--writable` for hide/delete actions). |
| `phototext doctor` | Diagnose config, database, LLM server, model vision, and face detection support (prints the version first). |
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
| `--skip-preflight` | Skip the provider startup check. |
| `--no-idle-detection` | Do not pause while other models are loaded (Ollama only; mlx-serve coexists via LRU). |
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

**iCloud Optimize Storage is survivable.** Photos-library assets are tracked by
UUID, so when iCloud evicts an original *after* the photo was processed, the next
scan keeps the processed row (flagged `offloaded`), serves the photo from the
Photos preview it keeps on disk, and restores the original when it downloads
again — text, search, people, and duplicates are never touched. Photos whose
previews iCloud also dropped fall back to phototext's own thumbnail/view caches;
run `phototext cache-previews` once before turning Optimize Storage on and
every photo stays viewable no matter what is evicted.

**The library's hidden flag is honored.** Photos hidden in the library (Photos
via `osxphotos`, iPhoto best-effort from its `apdb`) are hidden in phototext too
— out of the default views and searches, in the web UI's *hidden* view, and
still extracted so their text is ready if you unhide them. Your own hide/unhide
choices in phototext always win: rescans never override them, and un-hiding in
the library unhides only what the library hid.

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
phototext scan --date-from 2013 --date-to 2013   # by capture date (EXIF, else file date)
phototext scan --limit 200                       # first 200 new photos
phototext scan --ids-file ids.txt                # listed paths or UUIDs
phototext run --album "Trip 2014"                # scan + process just the slice
```

Album, favorites, and UUID lookups read the library's own database (Photos
libraries via `osxphotos`, iPhoto via its `Database/apdb`, schema detected at
runtime with clear errors if it cannot be parsed). Date and path filters work
on plain folders too; date slices prefer the EXIF capture date and fall back
to the file date. Photos already queued from earlier scans are still
processed by `run`.

## Web UI

```bash
phototext serve                 # http://127.0.0.1:8765
```

A read-only local web app over the catalog, laid out like iCloud Photos: a
left **sidebar** with the search box, the Library / Discover / Utilities
navigation, and every filter — hidden photos, people, categories, text,
years — while the main area holds a card grid of photos (cached thumbnails
from the originals), status tabs, full-text search, and per-photo pages with
the recovered text, context, metadata, the raw model response, and **where
the photo is really stored** — every on-disk location with its source (which
folder or which iPhoto/Photos library), an on-disk status, a *reveal in
Finder* link that opens the real file's location, even inside a
`.photolibrary`/`.photoslibrary` package, and — for photos that live in a
Photos library — an *open in Photos* link that shows the photo inside the
Photos app. Photos whose originals iCloud has offloaded carry a badge and
keep rendering from their preview or cached view. It binds to loopback only,
opens the catalog read-only (`mode=ro` + `query_only`), and never writes to
the library. Ctrl+C stops it.

There is also a **People** tab: person cards with face crops, and per-person
pages with a review queue for uncertain model tags. With `serve --writable`
the photo page grows a face-box picker — drag a rectangle around a face,
type a name, and the person is seeded (recognition profile built on the
spot) — plus confirm/remove buttons on every person chip and rename/reset/
delete on the person page. All writes stay behind the session token and
Origin check.

Writable mode also carries **bulk actions**: any filtered view (the hidden
view, a person, a category, a status, a year, a date range, or a search)
offers a confirmed *delete all N in view* button that tombstones every
photo in that view — the selection is recomputed server-side from the same
filters, unfiltered requests are refused, and nothing is ever deleted
automatically (deleting a photo in the Photos app leaves it here until
you choose). The trash view carries a matching *purge all* button, and
purging removes the cached thumbnails/views with the rows.

The list page doubles as a **timeline**: year chips (from EXIF capture
dates) narrow the grid, and the search bar accepts date narrowing via the
chips too. A **Duplicates** tab groups resized/re-encoded copies of the
same photo (iCloud preview proxies excluded) with the largest file marked
"keep".

### Themes

The sidebar carries a theme switch with three looks: **iCloud** (the
default — the layout above), **Machine** and **Samaritan** — the
surveillance-console aesthetic from *Person of Interest* (black + neon red
scanlines and glowing corner brackets; white + red with hairline frames
respectively). The choice is saved per browser; a `web_theme` value in the
config sets the server default. PoI themes restyle the whole chrome
(uppercase condensed type, mono captions, terminal-styled recovered text)
and add hover-acquisition brackets on grid cards; photo detail pages get
bracketed subject frames with designation tags, and the duplicates view
marks its keep suggestion accordingly. The REC dot next to the brand
pulses while the processing queue is active.

Theme fonts (Barlow Semi Condensed, JetBrains Mono) are self-hosted under
SIL OFL — license texts ship in `src/phototext/fonts/` — so the UI stays
fully offline.

## Long runs and resume

- Ctrl+C (or SIGTERM) stops gracefully after the current photo; a second Ctrl+C
  forces an immediate quit.
- Idle detection (Ollama only): before each photo the run checks `/api/ps` and
  pauses while a *different* model is loaded (e.g. you are using a coding
  model), resuming when it unloads. Disable with `idle_detection = false` or
  `--no-idle-detection`. On mlx-serve this is a no-op — resident models
  coexist under the server's LRU/memory rules.
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
- **Duplicates**: `phototext duplicates` clusters photos whose perceptual
  hashes are nearly identical (tight threshold) — resized or re-encoded
  copies of the same image — with the largest file highlighted as the one
  to keep. Read-only: cleaning up the files stays yours to do.
- **Capture dates**: photos record their EXIF `DateTimeOriginal` at scan
  time (`phototext backfill-dates` catches up older catalogs); the web UI
  browses by year and `search --year/--date-from/--date-to` narrows
  results.
- **People**: name a person once and tag them everywhere. On a photo in the
  web UI (writable mode) drag a box around a face and type a name — or
  `phototext people name <photo-id> "Ryan" --box x,y,w,h`. The model writes a
  recognition profile from the seed crop(s), then `phototext people run`
  checks every photo in one call per photo (all people at once, using the
  fast `person_model`). Face detection (macOS Vision) skips photos without
  faces entirely and matches on close-up face crops otherwise;
  `people name` without `--box` auto-adopts a lone detected face. Tags carry
  confidence; below `person_min_confidence` they land in a review queue.
  Confirm/remove in the web UI or via `people confirm/remove` — `--add-seed`
  also feeds the confirmed crop back into the recognition profile, refreshed
  by `people describe`; `people reset` drops model tags but keeps your
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

### LLM backend (`provider`)

`provider = "ollama"` (default) or `"mlx-serve"` picks the LLM backend. The
base model names (`model`, `prefilter_model`, `person_model`, `embed_model`)
are the ollama ones; a `[mlx-serve]` table overlays them when that provider is
active, so switching is a one-line change and switching back restores the
ollama names untouched:

```toml
provider  = "mlx-serve"
mlx_url   = "http://127.0.0.1:11234"   # mlx-serve server (OpenAI-compatible)

[mlx-serve]
model           = "mlx-community/gemma-4-e4b-it-4bit"
prefilter_model = "mlx-community/gemma-4-e4b-it-4bit"
embed_model     = "mlx-community/Qwen3-Embedding-0.6B-4bit-DWQ"
```

The mlx-serve client speaks the OpenAI-compatible API (`/v1/chat/completions`
with image content parts and `json_schema` structured output,
`/v1/embeddings`) and mirrors the Ollama client's behavior, retries, and
exception types. Extraction results are model-agnostic — nothing needs
reprocessing when you switch providers. Embeddings are stored per model
(`photo_embeddings` is keyed by `photo_id, model`), so switching `embed_model`
starts a fresh incremental set while search stays model-scoped.

| Key | Default | Meaning |
| --- | --- | --- |
| `provider` | `ollama` | LLM backend: `ollama` or `mlx-serve`. |
| `ollama_url` | `http://localhost:11434` | Ollama server (provider `ollama`). |
| `mlx_url` | `http://127.0.0.1:11234` | mlx-serve server, OpenAI-compatible (provider `mlx-serve`). |
| `model` | `gemma4:12b` | Any vision-capable model tag (per-provider overlays apply). |
| `max_image_edge` | `1024` | Longest image edge (px) sent to the model. Smaller is faster. |
| `max_output_tokens` | `2048` | Cap on generated tokens per photo; bounds repetition loops. Dense documents are ~500-1000 tokens. |
| `num_ctx` | `8192` | Model context window; image tokens count against it. |
| `max_attempts` | `3` | Model-call attempts per photo before it is marked failed. |
| `request_timeout_s` | `300` | HTTP timeout for one model call. |
| `transport_retries` / `transport_backoff_s` | `3` / `30` | What to do if the LLM server drops mid-run. |
| `structured_output` | `true` | JSON-schema-constrained output on both providers; set `false` if the model struggles. |
| `idle_detection` | `true` | Pause the run while a different model is loaded (Ollama `/api/ps`; no-op on mlx-serve). |
| `idle_poll_s` | `15` | How often to re-check while paused. |
| `lease_timeout_s` | `3600` | Multi-worker runs: reclaim photos from dead workers after this long. |
| `two_tier` | `false` | Cheap prefilter model gates the full pass; textless photos finish at the gate. |
| `prefilter_model` | `gemma3:4b` | The gate model for two-tier mode. |
| `recent_first` | `false` | Claim the newest photos first (capture date) while the backlog runs — yesterday's photos surface during nightly runs instead of 2013's. Default is FIFO. |
| `process_derivatives` | `true` | Best-effort extraction from iCloud preview thumbnails for cloud-only photos; `false` keeps them `deferred` until the real original downloads. |
| `person_model` | *(prefilter)* | Model for person matching + recognition profiles. |
| `person_min_confidence` | `0.6` | Model tags below this confidence wait in the review queue. |
| `face_detection` | `true` | macOS Vision face detection for the people pass (skip faceless photos, match on face crops). |
| `vision_ocr` | `true` | Free macOS Vision OCR at scan time into `vision_text` (FTS-indexed) so photos are searchable before the LLM pass; silent no-op without Vision. |
| `embed_model` | *(empty)* | Embedding model for semantic search (e.g. `nomic-embed-text` on ollama, `mlx-community/Qwen3-Embedding-0.6B-4bit-DWQ` on mlx-serve); empty disables `embed`/`--semantic`/`similar`. |
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

On mlx-serve with `mlx-community/gemma-4-e4b-it-4bit` the same machine runs at
**~16 s per photo** — a measured 604 photos in a 2h45m nightly window vs 406
(~24 s/photo) on Ollama `gemma4:12b`, ≈1.5× the throughput.

For a big library, expect multi-day total run time. That is what the resume design
is for: run it in spare cycles (`--stop-after 2h`) and let it accumulate. To go
faster: lower `max_image_edge`, or use a smaller vision model via `--model`.

## Testing

```bash
.venv/bin/python tests/e2e.py
```

Self-contained: spawns a mock server, generates a fake iPhoto-style
library (with an apdb), and covers scanning/dedup, happy path, failures,
retry, transport loss, crash recovery, budgets, SIGINT, doctor, sips fallback,
full-text search, schema migration with backups, export, and slice scans
(dates, limit, ids-file, album/favorites/UUID via the library database). No
network or real library involved.

The suite is provider-parameterized — it runs against `tests/mock_ollama.py`
by default and against `tests/mock_mlx.py` (a mock OpenAI-compatible
mlx-serve) with:

```bash
PHOTOTEXT_E2E_PROVIDER=mlx-serve .venv/bin/python tests/e2e.py
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `doctor`: server not reachable | Start the LLM server: `ollama serve` (or the app), or your mlx-serve service. |
| `doctor`: model not present | `ollama pull gemma4:12b` (or `mlx-serve pull <model-id>` and restart the server) or set `model` in the config. |
| `doctor`: failed on test image | The model is not vision-capable; pick a multimodal tag. |
| Scan finds nothing in a library | Grant Full Disk Access to your terminal app, then rescan. |
| Some photos show as `error` | `phototext results --status error`, then `phototext retry`. |
| Unreadable image errors | Usually a format even `sips` cannot read; the path is recorded so you can inspect it. |

## Roadmap

- **M2 (done, 0.2.0)**: slices (album/date/favorites/limit/ids-file), FTS5
  full-text search, JSONL/CSV export, schema migrations with backups, installer.
- **M3 (done, 0.3.x–0.6.0)**: local web UI (iCloud-style sidebar, people,
  memes, duplicates, trash), reprocessing, quadrant tiling, idle detection,
  watch mode, multi-worker runs, two-tier gate, tombstones + writable web,
  iCloud offload resilience, Vision OCR tier-0, semantic search.
- **0.7.0**: bulk delete in the web UI, cache cleanup on purge.
- **0.8.0**: mlx-serve LLM backend — `provider` config selects ollama vs
  mlx-serve (e4b + Qwen3 embeddings), one-line rollback, model-scoped
  embeddings, e2e-verified on both providers.
- **0.9.0 (planned)**: Person of Interest themes (machine/samaritan) for the
  web UI, image lightbox, run reliability (caffeinate, keep-alive, disk
  preflight).
- **Backlog**: saved searches / find-similar web strip, timeline scrubber,
  YAML sidecar export.

See `DESIGN.md` for the full design and decision log.
