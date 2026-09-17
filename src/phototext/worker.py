from __future__ import annotations

import base64
import os
import warnings
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from . import db, scanner
from .config import Config, with_model
from .imaging import ImageReadError, prepare_image, prepare_tiles
from .ollama_client import (
    ModelOutputError,
    OllamaClient,
    OllamaServerError,
    OllamaTimeout,
    OllamaUnreachable,
)
from .prompt import merge_tile_results, normalize_gate_result, normalize_result

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(d|h|m|s)?", re.IGNORECASE)
_UNIT_SECONDS = {"d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0}


class RunAborted(Exception):
    pass


def parse_duration(text: str) -> float:
    s = text.strip().replace(" ", "")
    if not s:
        raise ValueError(f"invalid duration: {text!r} (examples: 45s, 90m, 2h, 1h30m)")
    total = 0.0
    pos = 0
    matched = False
    while pos < len(s):
        m = _DURATION_RE.match(s, pos)
        if m is None:
            raise ValueError(f"invalid duration: {text!r} (examples: 45s, 90m, 2h, 1h30m)")
        total += float(m.group(1)) * _UNIT_SECONDS[(m.group(2) or "s").lower()]
        pos = m.end()
        matched = True
    if not matched or total <= 0:
        raise ValueError(f"invalid duration: {text!r} (examples: 45s, 90m, 2h, 1h30m)")
    return total


class RunController:
    def __init__(self) -> None:
        self.stop = False
        signal.signal(signal.SIGINT, self._handle)
        signal.signal(signal.SIGTERM, self._handle)

    def _handle(self, signum, frame) -> None:
        if not self.stop:
            self.stop = True
            print(
                "\nStopping after the current photo... (press Ctrl+C again to force quit)",
                file=sys.stderr,
            )
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)


def interruptible_sleep(seconds: float, controller: RunController) -> bool:
    end = time.monotonic() + seconds
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return True
        if controller.stop:
            return False
        time.sleep(min(1.0, remaining))


def run_pipeline(
    cfg: Config,
    stop_after: str | None = None,
    model: str | None = None,
    no_scan: bool = False,
    skip_preflight: bool = False,
    limit: int | None = None,
    slice_spec: scanner.Slice | None = None,
    watch: bool = False,
    watch_interval_s: int = 60,
    workers: int = 1,
    config_path: Path | None = None,
) -> int:
    if stop_after is not None:
        parse_duration(stop_after)
    if workers < 1:
        raise ValueError("--workers must be at least 1")
    controller = RunController()
    conn = db.connect(cfg.db_path)
    if workers <= 1:
        # A single-worker run owns the catalog at startup; multi-worker runs
        # use leases (reclaim_stale) so workers never stomp on each other.
        recovered = db.reset_processing(conn)
        if recovered:
            print(f"Recovered {recovered} photo(s) that were interrupted mid-processing.")
    if not no_scan:
        sources = db.get_sources(conn)
        if not sources:
            print("No sources registered. Add one first: phototext scan /path/to/photo-library")
            return 1
        for src in sources:
            print(f"Scanning: {src['uri']}")
            try:
                scanner.scan_source(conn, src["uri"], src["id"], slice_spec)
            except (FileNotFoundError, ValueError) as e:
                print(f"  scan error: {e}")
                if slice_spec is not None:
                    print("Not processing: fix the slice filters and run again.")
                    return 1
    counts = db.status_counts(conn)
    queued = counts.get("queued", 0)
    if queued == 0 and not watch and (workers <= 1 or counts.get("processing", 0) == 0):
        print(f"Nothing to process. Catalog: {_summary(counts)}")
        return 0
    effective = with_model(cfg, model)
    client = OllamaClient(effective)
    if not skip_preflight:
        problems = client.preflight()
        if problems:
            for problem in problems:
                print(f"preflight: {problem}")
            print("Not starting. Run `phototext doctor` for details.")
            return 1
    deadline = time.monotonic() + parse_duration(stop_after) if stop_after else None
    print(f"Processing {queued} photo(s) with model '{effective.model}'...")
    exit_code = 0
    if workers > 1:
        exit_code = _run_multi(
            cfg, model, workers, watch, watch_interval_s, deadline, controller,
            slice_spec, config_path,
        )
        counts = db.status_counts(conn)
        print(f"Catalog now: {_summary(counts)}")
        print("Inspect results: phototext results    |    Progress: phototext status")
        return exit_code
    processed = 0
    failed = 0
    watching_announced = False
    while True:
        if controller.stop:
            print("Stopped by user. Progress is saved; run again to continue.")
            break
        if deadline is not None and time.monotonic() >= deadline:
            print("Stopped: time budget reached. Progress is saved; run again to continue.")
            break
        if limit is not None and processed >= limit:
            print(f"Stopped: --limit {limit} reached. Run again to continue.")
            break
        if effective.idle_detection:
            try:
                if not _wait_while_busy(client, effective, controller):
                    print("Stopped by user while paused. Progress is saved; run again to continue.")
                    break
            except RunAborted as e:
                print(f"aborted: {e}")
                print("Nothing was lost: unfinished items stay queued. Start Ollama and run again.")
                exit_code = 1
                break
        row = db.claim_next(conn)
        if row is None:
            if not watch:
                print(f"Queue drained: {processed} processed, {failed} failed this run.")
                break
            if not watching_announced:
                watching_announced = True
                n_sources = len(db.get_sources(conn))
                print(
                    f"Watching {n_sources} source(s) for new photos "
                    f"(checking every {watch_interval_s}s; Ctrl+C to stop)"
                )
            newly = _watch_scan(conn, slice_spec)
            if newly:
                print(f"watch: {newly} new photo(s) queued")
                continue
            if not _watch_wait(watch_interval_s, deadline, controller):
                break
            continue
        try:
            outcome = _process_item(conn, client, effective, row, controller, processed + 1, queued)
        except RunAborted as e:
            print(f"aborted: {e}")
            print("Nothing was lost: unfinished items stay queued. Start Ollama and run again.")
            exit_code = 1
            break
        if outcome == "stopped":
            print("Stopped by user. Progress is saved; run again to continue.")
            break
        processed += 1
        if outcome == "error":
            failed += 1
    counts = db.status_counts(conn)
    print(f"Catalog now: {_summary(counts)}")
    print("Inspect results: phototext results    |    Progress: phototext status")
    return exit_code


def _watch_scan(conn, slice_spec: scanner.Slice | None) -> int:
    """Fast rescan of registered sources; returns newly queued photos."""
    newly = 0
    for src in db.get_sources(conn):
        try:
            stats = scanner.scan_source(conn, src["uri"], src["id"], slice_spec, quiet=True)
            newly += stats.new_photos
        except (FileNotFoundError, ValueError) as e:
            print(f"  watch scan error: {e}")
    return newly


def _watch_wait(interval: float, deadline: float | None, controller: RunController) -> bool:
    """Sleep between watch scans, clamped to the time budget.

    Returns False when the run should stop (user stop or budget reached)."""
    end = time.monotonic() + interval
    if deadline is not None:
        end = min(end, deadline)
    if not interruptible_sleep(max(0.1, end - time.monotonic()), controller):
        print("Stopped by user. Progress is saved; run again to continue.")
        return False
    if deadline is not None and time.monotonic() >= deadline:
        print("Stopped: time budget reached. Progress is saved; run again to continue.")
        return False
    return True


def worker_child(
    cfg: Config,
    model: str | None = None,
    persistent: bool = False,
    no_idle_detection: bool = False,
) -> int:
    """One claim/process worker for `--workers` mode (spawned by run_pipeline).

    Claims are atomic, so multiple children (or a rerun) share the queue
    safely; a crashed worker's photos are reclaimed once their lease
    (lease_timeout_s) expires instead of at the next startup.
    """
    controller = RunController()
    if no_idle_detection:
        cfg = replace(cfg, idle_detection=False)
    effective = with_model(cfg, model)
    client = OllamaClient(effective)
    conn = db.connect(cfg.db_path)
    reclaimed = db.reclaim_stale(conn, effective.lease_timeout_s)
    if reclaimed:
        print(f"reclaimed {reclaimed} stale photo(s) from dead workers")
    total = db.status_counts(conn).get("queued", 0)
    processed = 0
    failed = 0
    last_reclaim = time.monotonic()
    while True:
        if controller.stop:
            print("worker stopping: progress is saved")
            break
        if os.getppid() == 1:
            print("worker exiting: parent process is gone")
            break
        if time.monotonic() - last_reclaim > 300:
            last_reclaim = time.monotonic()
            db.reclaim_stale(conn, effective.lease_timeout_s)
        if effective.idle_detection:
            try:
                if not _wait_while_busy(client, effective, controller):
                    break
            except RunAborted as e:
                print(f"aborted: {e}")
                return 1
        row = db.claim_next(conn)
        if row is None:
            if not persistent:
                break
            if not interruptible_sleep(1.0, controller):
                break
            continue
        try:
            outcome = _process_item(
                conn, client, effective, row, controller, processed + 1, total
            )
        except RunAborted as e:
            print(f"aborted: {e}")
            print("Nothing was lost: unfinished items stay queued. Start Ollama and run again.")
            return 1
        if outcome == "stopped":
            break
        processed += 1
        if outcome == "error":
            failed += 1
    print(f"worker finished: {processed} processed, {failed} failed")
    return 0


def _run_multi(
    cfg: Config,
    model: str | None,
    workers: int,
    watch: bool,
    watch_interval_s: int,
    deadline: float | None,
    controller: RunController,
    slice_spec: scanner.Slice | None,
    config_path: Path | None,
) -> int:
    """Spawn worker children, relay their output, watch sources, stop on budget."""
    conn = db.connect(cfg.db_path)
    cmd = [sys.executable, "-u", "-m", "phototext"]
    if config_path is not None:
        cmd += ["--config", str(config_path)]
    cmd += ["--db", str(cfg.db_path), "_worker"]
    if model:
        cmd += ["--model", model]
    if not cfg.idle_detection:
        cmd += ["--no-idle-detection"]
    if watch:
        cmd += ["--persistent"]

    def relay(proc: subprocess.Popen, index: int) -> None:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            print(f"[w{index}] {line.rstrip()}")
        proc.stdout.close()

    children: list[subprocess.Popen] = []
    threads: list[threading.Thread] = []
    for index in range(1, workers + 1):
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        children.append(proc)
        thread = threading.Thread(target=relay, args=(proc, index), daemon=True)
        thread.start()
        threads.append(thread)
    print(f"Started {workers} worker(s)...")

    def stop_children() -> None:
        for proc in children:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
        for proc in children:
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.terminate()
        for thread in threads:
            thread.join(timeout=5)

    stop_reason = None
    last_scan = time.monotonic()
    while any(proc.poll() is None for proc in children):
        if controller.stop:
            stop_reason = "Stopped by user. Progress is saved; run again to continue."
            break
        if deadline is not None and time.monotonic() >= deadline:
            stop_reason = "Stopped: time budget reached. Progress is saved; run again to continue."
            break
        if watch and time.monotonic() - last_scan >= watch_interval_s:
            last_scan = time.monotonic()
            newly = _watch_scan(conn, slice_spec)
            if newly:
                print(f"watch: {newly} new photo(s) queued")
        time.sleep(1)
    stop_children()
    if stop_reason is None:
        stop_reason = "All workers finished."
    print(stop_reason)
    return 0


def _extract_photo(
    client: OllamaClient, cfg: Config, path: Path, full_b64: str
) -> tuple[dict, str, bool]:
    """Extraction escalation: whole image (with the client's internal anti-loop
    retry) -> quadrant tiling for photos the single pass cannot parse.

    Returns (raw_result, raw_content, tiled). Raises ModelOutputError when
    even the tiles produce nothing usable; transport/server errors propagate.
    """
    try:
        raw, raw_content = client.extract(full_b64)
        return raw, raw_content, False
    except ModelOutputError:
        pass
    tiles = prepare_tiles(
        path, cfg.max_image_edge, max_pixels=cfg.max_image_pixels
    )
    results: list[dict] = []
    contents: list[str] = []
    last_error: ModelOutputError | None = None
    for tile in tiles:
        try:
            tile_raw, tile_content = client.extract(
                base64.b64encode(tile).decode("ascii")
            )
        except ModelOutputError as e:
            last_error = e
            continue
        results.append(tile_raw)
        contents.append(tile_content)
    if not results:
        raise last_error or ModelOutputError("tiling produced no usable output")
    raw_content = "\n\n-- phototext tile boundary --\n\n".join(contents)
    return merge_tile_results(results), raw_content, True


def _process_item(
    conn,
    client: OllamaClient,
    cfg: Config,
    row,
    controller: RunController,
    index: int,
    total: int,
) -> str:
    photo_id = row["id"]
    if row["attempts"] >= cfg.max_attempts:
        db.mark_error(conn, photo_id, "max attempts exceeded; run `phototext retry` to try again")
        _report_error(index, total, 0.0, "max attempts exceeded", None, photo_id)
        return "error"
    path = db.find_first_existing_location(conn, photo_id)
    if path is None:
        db.mark_error(conn, photo_id, "no readable file (moved, deleted, or no permission?)")
        _report_error(index, total, 0.0, "file missing", None, photo_id)
        return "error"
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", UserWarning)
            image_bytes = prepare_image(
                Path(path), cfg.max_image_edge, max_pixels=cfg.max_image_pixels
            )
        for warning in caught:
            db.record_warning(
                conn, photo_id,
                type(warning.message).__name__, str(warning.message),
            )
    except ImageReadError as e:
        db.mark_error(conn, photo_id, f"unreadable image: {e}")
        _report_error(index, total, 0.0, str(e), path, photo_id)
        return "error"
    b64 = base64.b64encode(image_bytes).decode("ascii")
    started = time.monotonic()
    if cfg.two_tier:
        try:
            gate_raw, gate_content = client.gate(b64)
            if not gate_raw.get("has_text"):
                result = normalize_gate_result(gate_raw)
                duration_ms = max(1, int((time.monotonic() - started) * 1000))
                db.mark_done(
                    conn, photo_id, result, client.gate_model, gate_content,
                    duration_ms, gated=True,
                )
                _report_done(
                    index, total, time.monotonic() - started, result, path,
                    gated=True,
                )
                return "done"
        except (ModelOutputError, OllamaServerError, OllamaTimeout):
            print("  gate failed; using the full pass for this photo")
        except OllamaUnreachable:
            pass  # the full pass below handles unreachable properly
    tries_left = cfg.max_attempts - row["attempts"]
    raw: dict | None = None
    raw_content: str | None = None
    tiled = False
    gated = False
    last_error: Exception | None = None
    while tries_left > 0:
        if controller.stop:
            db.requeue(conn, photo_id)
            return "stopped"
        try:
            raw, raw_content, tiled = _extract_photo(client, cfg, Path(path), b64)
            break
        except OllamaUnreachable as e:
            print(f"  ollama unreachable ({e}); retrying...")
            if not _wait_for_ollama(client, cfg, controller):
                db.requeue(conn, photo_id)
                raise RunAborted(f"Ollama at {client.base_url} stayed unreachable.") from e
            continue
        except (OllamaServerError, ModelOutputError, OllamaTimeout) as e:
            last_error = e
            tries_left -= 1
            if tries_left > 0:
                interruptible_sleep(min(30.0, 2.0 ** (cfg.max_attempts - tries_left)), controller)
    if raw is None:
        content = getattr(last_error, "content", None)
        db.mark_error(conn, photo_id, f"{type(last_error).__name__}: {last_error}", content)
        _report_error(
            index, total, time.monotonic() - started, str(last_error), path, photo_id
        )
        return "error"
    result = normalize_result(raw)
    duration_ms = max(1, int((time.monotonic() - started) * 1000))
    db.mark_done(conn, photo_id, result, client.model, raw_content, duration_ms, tiled)
    _report_done(index, total, time.monotonic() - started, result, path, tiled)
    return "done"


def _wait_for_ollama(client: OllamaClient, cfg: Config, controller: RunController) -> bool:
    for _ in range(cfg.transport_retries):
        if controller.stop:
            return False
        interruptible_sleep(cfg.transport_backoff_s, controller)
        if controller.stop:
            return False
        try:
            client.check_connection()
            return True
        except OllamaUnreachable:
            continue
    return False


def _normalize_model_name(name: str) -> str:
    return name[: -len(":latest")] if name.endswith(":latest") else name


def _foreign_models(client: OllamaClient) -> list[str]:
    ours = _normalize_model_name(client.model)
    return sorted(
        {
            name
            for name in (m for m in client.loaded_models() if m)
            if _normalize_model_name(name) != ours
        }
    )


def _wait_while_busy(client: OllamaClient, cfg: Config, controller: RunController) -> bool:
    """Pause while a foreign model is loaded in Ollama.

    Returns True to proceed, False when stopped by the user. Raises
    RunAborted if Ollama stays unreachable.
    """
    announced = False
    while True:
        if controller.stop:
            return False
        try:
            foreign = _foreign_models(client)
        except OllamaUnreachable as e:
            print(f"  ollama unreachable ({e}); retrying...")
            if not _wait_for_ollama(client, cfg, controller):
                if controller.stop:
                    return False
                raise RunAborted(f"Ollama at {client.base_url} stayed unreachable.") from e
            continue
        if not foreign:
            if announced:
                print("resumed: Ollama is free again")
            return True
        if not announced:
            print(
                f"paused: other model(s) loaded in Ollama ({', '.join(foreign)}); "
                f"waiting for them to unload (checking every {cfg.idle_poll_s}s)"
            )
            announced = True
        if not interruptible_sleep(cfg.idle_poll_s, controller):
            return False


def _summary(counts: dict) -> str:
    parts = []
    for key in ("queued", "processing", "done", "error", "deferred"):
        if counts.get(key):
            parts.append(f"{key} {counts[key]}")
    total = counts.get("total", 0)
    return f"{total} photo(s)" + (f" ({', '.join(parts)})" if parts else "")


def _report_done(
    index: int,
    total: int,
    seconds: float,
    result: dict,
    path: str | None,
    tiled: bool = False,
    gated: bool = False,
) -> None:
    name = Path(path).name if path else ""
    snippet = result["text"].replace("\n", " ").strip()[:50]
    flag = "text" if result["has_text"] else "no-text"
    marks = " ".join(m for m, on in (("(tiled)", tiled), ("(gated)", gated)) if on)
    print(
        f"  [{index}/{total}] {seconds:5.1f}s {result['text_kind']:<11} {flag:<7} "
        f"{marks:<8} {name}  {snippet}"
    )


def _report_error(
    index: int, total: int, seconds: float, reason: str, path: str | None, photo_id: int
) -> None:
    name = Path(path).name if path else f"photo {photo_id}"
    print(f"  [{index}/{total}] {seconds:5.1f}s ERROR       {'':7} {name}  {reason}")
