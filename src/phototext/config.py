from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

DEFAULT_DIR = Path.home() / ".phototext"
DEFAULT_DB_PATH = DEFAULT_DIR / "catalog.db"
DEFAULT_CONFIG_PATH = DEFAULT_DIR / "config.toml"

EXAMPLE_CONFIG = """\
# phototext configuration (CLI flags override these values)

# Ollama server
ollama_url = "http://localhost:11434"

# Any vision-capable model tag installed in Ollama
model = "gemma4:12b"

# Longest image edge (pixels) sent to the model; smaller is faster
max_image_edge = 1024

# Cap on generated tokens per photo; bounds repetition loops and slow photos
# (dense documents are ~500-1000 tokens)
max_output_tokens = 2048

# Context window for the model (image tokens count against it)
num_ctx = 8192

# Attempts per photo before marking it failed
max_attempts = 3

# HTTP timeout for one model call, in seconds
request_timeout_s = 300

# If Ollama drops mid-run: retry this often, this many seconds apart
transport_retries = 3
transport_backoff_s = 30

# Use Ollama structured outputs (JSON schema); set false if the model
# struggles with schema-constrained responses
structured_output = true

# Pause the run while a different model is loaded in Ollama (checked via
# /api/ps before each photo); our own model or an idle server proceed
idle_detection = true

    # How often to re-check while paused, in seconds
    idle_poll_s = 15

    # Multi-worker runs: photos stuck in 'processing' (worker died) are
    # reclaimed after this many seconds without progress
    lease_timeout_s = 3600

    # Refuse images with more pixels than this (decompression-bomb guard);
    # Pillow's own warning threshold is about half of this
    max_image_pixels = 357913941

    # Two-tier extraction: a cheap fast vision model checks each photo for
    # visible text first; textless photos finish there (seconds, with a short
    # description and category), text-bearing photos go to the full model.
    # Off by default: the full model's richer descriptions are worth it unless
    # the library is large.
    two_tier = false
    prefilter_model = "gemma3:4b"
    prefilter_max_edge = 512

    # People: name a person on one photo, then `phototext people run` tags
    # them across the library. Matching uses this model (empty = the
    # prefilter model above); model tags below person_min_confidence are
    # kept in a review queue instead of being trusted.
    person_model = ""
    person_min_confidence = 0.6

    # Face detection with the macOS Vision framework, used by the people
    # matching pass: photos without faces skip the model call entirely and
    # photos with faces are matched on close-up face crops.
    face_detection = true

    # Claim order for the extraction queue. false = oldest first (classic
    # FIFO); true = newest photos first (by capture date, falling back to
    # first-seen), so recent photos surface while a long backlog runs.
    recent_first = false

    # Best-effort extraction from iCloud preview derivatives: when false,
    # cloud-only photos with a local preview wait as 'deferred' until the
    # original downloads, instead of spending a full model call on the
    # low-resolution preview.
    process_derivatives = true

    # SQLite catalog location
    db_path = "~/.phototext/catalog.db"
"""


@dataclass
class Config:
    ollama_url: str = "http://localhost:11434"
    model: str = "gemma4:12b"
    max_image_edge: int = 1024
    max_output_tokens: int = 2048
    num_ctx: int = 8192
    temperature: float = 0.0
    max_attempts: int = 3
    request_timeout_s: int = 300
    transport_retries: int = 3
    transport_backoff_s: int = 30
    structured_output: bool = True
    idle_detection: bool = True
    idle_poll_s: int = 15
    lease_timeout_s: int = 3600
    two_tier: bool = False
    prefilter_model: str = "gemma3:4b"
    prefilter_max_edge: int = 512
    person_model: str = ""
    person_min_confidence: float = 0.6
    face_detection: bool = True
    recent_first: bool = False
    process_derivatives: bool = True
    max_image_pixels: int = 357_913_941
    db_path: Path = DEFAULT_DB_PATH


def ensure_noindex(directory: Path) -> None:
    """Drop a Spotlight marker so mds/mdworker skips this directory."""
    try:
        (directory / ".metadata_never_index").touch(exist_ok=True)
    except OSError:
        pass


def ensure_default_config() -> Path | None:
    try:
        DEFAULT_DIR.mkdir(parents=True, exist_ok=True)
        ensure_noindex(DEFAULT_DIR)
        if not DEFAULT_CONFIG_PATH.exists():
            DEFAULT_CONFIG_PATH.write_text(EXAMPLE_CONFIG)
        return DEFAULT_CONFIG_PATH
    except OSError:
        return None


def load_config(path: Path | None = None) -> Config:
    if path is not None:
        path = Path(path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"config file not found: {path}")
        source: Path | None = path
    else:
        source = ensure_default_config()
    cfg = Config()
    if source is not None and source.exists():
        with open(source, "rb") as f:
            data = tomllib.load(f)
        for key, value in data.items():
            if hasattr(cfg, key):
                if key == "db_path":
                    value = Path(str(value)).expanduser()
                setattr(cfg, key, value)
    return cfg


def with_model(cfg: Config, model: str | None) -> Config:
    return replace(cfg, model=model) if model else cfg
