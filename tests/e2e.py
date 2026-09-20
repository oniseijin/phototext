#!/usr/bin/env python3
"""Run: .venv/bin/python tests/e2e.py"""
from __future__ import annotations

import csv
import json
import os
import random
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent.parent
try:
    import phototext  # noqa: F401
except ImportError:
    sys.path.insert(0, str(ROOT / "src"))

MODEL = "gemma4:mock"
FAILURES: list[str] = []


def _try_get(url: str):
    try:
        resp = requests.get(url, timeout=2)
        return resp if resp.status_code == 200 else None
    except Exception:
        return None


def check(cond, msg: str) -> None:
    if cond:
        print(f"  ok    {msg}")
    else:
        FAILURES.append(msg)
        print(f"  FAIL  {msg}")


def font(size: int = 48):
    for attempt in (
        lambda: ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", size),
        lambda: ImageFont.load_default(size=size),
        lambda: ImageFont.load_default(),
    ):
        try:
            return attempt()
        except Exception:
            continue
    raise RuntimeError("no usable font found")


def make_text_image(path: Path, lines: list[str], size=(640, 480)) -> None:
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    y = 40
    for line in lines:
        d.text((40, y), line, fill="black", font=font())
        y += 64
    im.save(path)


def make_plain_image(path: Path) -> None:
    gradient = Image.linear_gradient("L").resize((640, 480))
    ImageOps.colorize(gradient, "navy", "orange").save(path)


def make_person_image(path: Path, color: str) -> None:
    im = Image.new("RGB", (640, 480), color)
    d = ImageDraw.Draw(im)
    d.text(
        (40, 40), "person photo",
        fill="black" if color == "white" else "white", font=font(),
    )
    im.save(path)


def build_library(work: Path) -> Path:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    lib = work / "Old iPhoto Library.photolibrary"
    originals = lib / "Originals" / "2013"
    originals.mkdir(parents=True)
    make_text_image(originals / "sign.jpg", ["OPEN", "24 HOURS"])
    make_text_image(originals / "doc.png", ["Invoice #42", "Acme Corp", "Total: $99.50"])
    make_plain_image(originals / "nature.jpg")
    im = Image.new("RGB", (480, 640), "beige")
    d = ImageDraw.Draw(im)
    d.text((40, 60), "MILK EGGS BREAD", fill="black", font=font())
    im.save(originals / "note.heic")
    (originals / "doc-copy.png").write_bytes((originals / "doc.png").read_bytes())
    (originals / "noextfile").write_bytes((originals / "sign.jpg").read_bytes())
    (originals / "movie.mov").write_bytes(b"not really a movie")
    (originals / "edit.aae").write_bytes(b"sidecar")
    (originals / ".DS_Store").write_bytes(b"junk")
    build_iphoto_apdb(lib)
    return lib


def build_iphoto_apdb(lib: Path) -> None:
    apdb = lib / "Database" / "apdb" / "Database"
    apdb.parent.mkdir(parents=True)
    con = sqlite3.connect(apdb)
    con.executescript(
        """
        CREATE TABLE RKVersion (uuid TEXT, masterId TEXT, name TEXT, flagged INTEGER);
        CREATE TABLE RKMaster (uuid TEXT, imagePath TEXT);
        CREATE TABLE RKAlbum (uuid TEXT, name TEXT);
        CREATE TABLE RKAlbumVersion (albumId INTEGER, versionId INTEGER);
        """
    )
    for uuid, path in [
        ("m1", "Originals/2013/sign.jpg"),
        ("m2", "Originals/2013/doc.png"),
        ("m3", "Originals/2013/nature.jpg"),
        ("m4", "Originals/2013/note.heic"),
        ("m5", "Originals/2013/doc-copy.png"),
    ]:
        con.execute("INSERT INTO RKMaster (uuid, imagePath) VALUES (?, ?)", (uuid, path))
    for uuid, master, name, flagged in [
        ("V1", "m1", "sign", 0),
        ("V2", "m2", "doc", 1),
        ("V3", "m3", "nature", 0),
        ("V4", "m4", "note", 1),
        ("V5", "m5", "doc copy", 0),
    ]:
        con.execute(
            "INSERT INTO RKVersion (uuid, masterId, name, flagged) VALUES (?, ?, ?, ?)",
            (uuid, master, name, flagged),
        )
    for uuid, name in [("a1", "Trip"), ("a2", "Party"), ("a3", "Empty")]:
        con.execute("INSERT INTO RKAlbum (uuid, name) VALUES (?, ?)", (uuid, name))
    for album_rowid, version_rowid in [(1, 1), (1, 4), (2, 2)]:
        con.execute(
            "INSERT INTO RKAlbumVersion (albumId, versionId) VALUES (?, ?)",
            (album_rowid, version_rowid),
        )
    con.commit()
    con.close()


def build_slice_folder(work: Path) -> Path:
    folder = work / "slicefolder"
    folder.mkdir()
    names = ["a2013.jpg", "b2013.jpg", "c2013.jpg", "d2024.jpg", "e2024.jpg", "f2024.jpg"]
    for name in names:
        make_text_image(folder / name, [f"slice {name}"])
    old = time.mktime((2013, 6, 15, 12, 0, 0, 0, 0, -1))
    new = time.mktime((2024, 6, 15, 12, 0, 0, 0, 0, -1))
    for name in names[:3]:
        os.utime(folder / name, (old, old))
    for name in names[3:]:
        os.utime(folder / name, (new, new))
    return folder


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_mock(port: int, mode_file: Path, ps_file: Path | None = None) -> subprocess.Popen:
    cmd = [
        sys.executable,
        str(ROOT / "tests" / "mock_ollama.py"),
        "--model",
        MODEL,
        "--port",
        str(port),
        "--mode-file",
        str(mode_file),
        "--slow-seconds",
        "3.0",
    ]
    if ps_file is not None:
        cmd += ["--ps-file", str(ps_file)]
    proc = subprocess.Popen(cmd)
    for _ in range(100):
        try:
            requests.get(f"http://127.0.0.1:{port}/api/tags", timeout=1)
            return proc
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("mock ollama did not start")


class CLI:
    def __init__(self, config_path: Path):
        self.cmd = [sys.executable, "-u", "-m", "phototext", "--config", str(config_path)]

    def run(self, *args, expect: int = 0, timeout: int = 180) -> str:
        proc = subprocess.run(
            self.cmd + list(args), capture_output=True, text=True, timeout=timeout
        )
        out = proc.stdout + proc.stderr
        check(
            proc.returncode == expect,
            f"exit {proc.returncode} == {expect} for: phototext {' '.join(args)}"
            + ("" if proc.returncode == expect else f"\n      output: {out[-600:]}"),
        )
        return out


def db_open(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(db_path)


def reset_queued(con: sqlite3.Connection) -> None:
    con.execute(
        "UPDATE photos SET status='queued', attempts=0, error=NULL, "
        "started_at=NULL, finished_at=NULL"
    )
    con.commit()


def count(con: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return con.execute(sql, params).fetchone()[0]


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="phototext-e2e-"))
    print(f"workdir: {work}")
    lib = build_library(work)
    db_path = work / "catalog.db"
    mode_file = work / "mode"
    mode_file.write_text("ok")
    ps_file = work / "ps"
    ps_file.write_text("")
    port = free_port()
    mock = start_mock(port, mode_file, ps_file)
    config = work / "config.toml"
    config.write_text(
        f'ollama_url = "http://127.0.0.1:{port}"\n'
        f'model = "{MODEL}"\n'
        f'db_path = "{db_path}"\n'
        "max_attempts = 2\n"
        "transport_retries = 2\n"
        "transport_backoff_s = 1\n"
        "request_timeout_s = 20\n"
    )
    cli = CLI(config)

    def fresh_cli(name: str, extra: str = "") -> CLI:
        cfgp = work / f"config-{name}.toml"
        cfgp.write_text(
            f'ollama_url = "http://127.0.0.1:{port}"\n'
            f'model = "{MODEL}"\n'
            f'db_path = "{work}/db-{name}.db"\n' + extra
        )
        return CLI(cfgp)

    try:
        print("\n[1] scan registers library, dedups by content hash")
        cli.run("scan", str(lib))
        con = db_open(db_path)
        check(count(con, "SELECT COUNT(*) FROM photos") == 4, "4 unique photos")
        check(count(con, "SELECT COUNT(*) FROM locations") == 6, "6 locations")
        check(count(con, "SELECT COUNT(*) FROM photos WHERE status='queued'") == 4, "4 queued")
        check(count(con, "SELECT COUNT(*) FROM sources") == 1, "1 source registered")
        check(
            con.execute("SELECT kind FROM sources LIMIT 1").fetchone()[0] == "library",
            "source kind is library",
        )
        con.close()

        print("\n[2] status reports the queue")
        out = cli.run("status")
        check("queued 4" in out, "status shows queued 4")

        print("\n[3] run processes the queue end to end")
        out = cli.run("run")
        check("Queue drained" in out, "run drains the queue")
        con = db_open(db_path)
        check(count(con, "SELECT COUNT(*) FROM photos WHERE status='done'") == 4, "all done")
        check(
            count(con, "SELECT COUNT(*) FROM photos WHERE text='MOCK EXTRACTED TEXT'") == 4,
            "text stored",
        )
        check(
            count(con, "SELECT COUNT(*) FROM photos WHERE model=?", (MODEL,)) == 4,
            "model recorded",
        )
        check(
            count(con, "SELECT COUNT(*) FROM photos WHERE duration_ms IS NULL OR duration_ms<=0")
            == 0,
            "durations recorded",
        )
        check(count(con, "SELECT COUNT(*) FROM photos WHERE raw_response IS NULL") == 0, "raw responses stored")
        con.close()

        print("\n[4] results command shows extractions")
        out = cli.run("results", "--n", "10")
        check("MOCK EXTRACTED TEXT" in out, "results shows extracted text")
        check("originals" in out.lower(), "results shows source path")

        print("\n[5] rescan is a fast no-op")
        out = cli.run("scan")
        check("new 0" in out and "unchanged 6" in out, "rescan finds nothing new")
        con = db_open(db_path)
        check(count(con, "SELECT COUNT(*) FROM photos") == 4, "still 4 photos")
        con.close()

        print("\n[6] model failures mark errors, then retry recovers")
        mode_file.write_text("fail500")
        con = db_open(db_path)
        reset_queued(con)
        con.close()
        cli.run("run", "--skip-preflight")
        con = db_open(db_path)
        check(count(con, "SELECT COUNT(*) FROM photos WHERE status='error'") == 4, "failures marked as error")
        check(count(con, "SELECT COUNT(*) FROM photos WHERE attempts=1") == 4, "attempts recorded")
        check(count(con, "SELECT COUNT(*) FROM photos WHERE error LIKE '%mock%'") == 4, "error messages stored")
        con.close()
        out = cli.run("retry")
        check("requeued 4" in out, "retry requeues failures")
        mode_file.write_text("ok")
        cli.run("run")
        con = db_open(db_path)
        check(count(con, "SELECT COUNT(*) FROM photos WHERE status='done'") == 4, "recovered after retry")
        con.close()

        print("\n[7] ollama unreachable mid-run requeues and exits nonzero")
        mock.terminate()
        mock.wait()
        con = db_open(db_path)
        reset_queued(con)
        con.close()
        cli.run("run", "--skip-preflight", expect=1)
        con = db_open(db_path)
        check(count(con, "SELECT COUNT(*) FROM photos WHERE status='queued'") == 4, "items requeued, nothing lost")
        con.close()
        mock = start_mock(port, mode_file, ps_file)

        print("\n[8] interrupted processing rows are recovered on next run")
        stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
        fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
        con = db_open(db_path)
        con.execute(
            "UPDATE photos SET status='done' WHERE id IN (SELECT id FROM photos ORDER BY id LIMIT 2)"
        )
        con.execute(
            "UPDATE photos SET status='processing', started_at=? "
            "WHERE id IN (SELECT id FROM photos ORDER BY id LIMIT 1 OFFSET 2)",
            (stale,),
        )
        con.execute(
            "UPDATE photos SET status='processing', started_at=? "
            "WHERE id IN (SELECT id FROM photos ORDER BY id LIMIT 1 OFFSET 3)",
            (fresh,),
        )
        con.commit()
        con.close()
        out = cli.run("run", "--no-scan")
        check("Recovered 2" in out, "both interrupted items recovered")
        con = db_open(db_path)
        check(count(con, "SELECT COUNT(*) FROM photos WHERE status='done'") == 4, "recovered items processed")
        check(count(con, "SELECT COUNT(*) FROM photos WHERE status='processing'") == 0, "no processing rows remain")
        con.close()

        print("\n[9] --stop-after budget stops the run cleanly")
        mode_file.write_text("slow")
        con = db_open(db_path)
        reset_queued(con)
        con.close()
        cli.run("run", "--skip-preflight", "--stop-after", "4s")
        con = db_open(db_path)
        done = count(con, "SELECT COUNT(*) FROM photos WHERE status='done'")
        queued = count(con, "SELECT COUNT(*) FROM photos WHERE status='queued'")
        con.close()
        check(1 <= done <= 3 and queued >= 1, f"budget respected (done={done}, queued={queued})")

        print("\n[10] SIGINT stops gracefully after current photo")
        con = db_open(db_path)
        reset_queued(con)
        con.close()
        proc = subprocess.Popen(
            cli.cmd + ["run", "--skip-preflight"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # Wait until the first photo is mid-model-call (the slow mock holds
        # it there for seconds) instead of a blind sleep: a fixed delay
        # raced startup imports under load, and signaling right after the
        # banner would fire before the first claim.
        ready = {"seen": False}

        def _wait_ready() -> None:
            for line in proc.stdout:
                if "Processing" in line:
                    ready["seen"] = True
                    break

        reader = threading.Thread(target=_wait_ready, daemon=True)
        reader.start()
        reader.join(timeout=60)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                poll = sqlite3.connect(db_path, timeout=1)
                n = poll.execute(
                    "SELECT COUNT(*) FROM photos WHERE status='processing'"
                ).fetchone()[0]
                poll.close()
            except sqlite3.Error:
                n = 0
            if n:
                break
            time.sleep(0.05)
        proc.send_signal(signal.SIGINT)
        out, _ = proc.communicate(timeout=60)
        check(
            proc.returncode == 0,
            f"graceful exit (rc={proc.returncode})\n      output: {out[-400:]}",
        )
        con = db_open(db_path)
        check(count(con, "SELECT COUNT(*) FROM photos WHERE status='done'") == 1, "current photo finished")
        check(count(con, "SELECT COUNT(*) FROM photos WHERE status='queued'") == 3, "remaining stay queued")
        con.close()
        mode_file.write_text("ok")

        print("\n[11] doctor passes against the mock, fails on a dead server")
        cli.run("doctor")
        bad_port = free_port()
        bad_config = work / "bad-config.toml"
        bad_config.write_text(
            f'ollama_url = "http://127.0.0.1:{bad_port}"\nmodel = "{MODEL}"\ndb_path = "{db_path}"\n'
        )
        cli = CLI(bad_config)
        cli.run("doctor", expect=1)
        cli = CLI(config)

        print("\n[12] parse_duration")
        from phototext.worker import parse_duration

        check(parse_duration("45") == 45.0, "plain seconds")
        check(parse_duration("90m") == 5400.0, "minutes")
        check(parse_duration("2h") == 7200.0, "hours")
        check(parse_duration("1h30m") == 5400.0, "compound")
        try:
            parse_duration("nope")
            check(False, "rejects garbage")
        except ValueError:
            check(True, "rejects garbage")

        from phototext.prompt import normalize_result

        collapsed = normalize_result(
            {
                "has_text": True,
                "text": "happy\n\n\n\n\n\n\nend\n\n\n",
                "context": "x",
                "text_kind": "document",
                "language": "en",
            }
        )
        check(collapsed["text"] == "happy\n\nend", "newline runs are collapsed")

        print("\n[13] sips fallback works")
        from phototext.imaging import _sips_to_jpeg

        converted = _sips_to_jpeg(lib / "Originals" / "2013" / "sign.jpg")
        check(converted is not None and converted.stat().st_size > 0, "sips converts to jpeg")
        if converted is not None:
            converted.unlink(missing_ok=True)

        print("\n[14] full-text search over recovered text")
        out = cli.run("run")
        check("Queue drained" in out, "remaining queue drained first")
        out = cli.run("search", "MOCK")
        check("4 match(es)" in out, "search finds all done photos")
        check("MOCK EXTRACTED TEXT" in out, "search shows a text snippet")
        out = cli.run("search", '"EXTRACTED TEXT"')
        check("4 match(es)" in out, "phrase search works")
        out = cli.run("search", "zzznothing")
        check("no matches" in out, "no-match message")
        out = cli.run("search", "zzz:$qq")
        check("no matches" in out, "invalid fts syntax falls back to a phrase")

        print("\n[15] migrate command backs up and applies pending migrations")
        out = cli.run("migrate")
        check("up to date" in out, "migrate is a no-op when current")
        con = db_open(db_path)
        con.executescript(
            "DROP TABLE photos_fts; DROP TRIGGER photos_fts_ai; "
            "DROP TRIGGER photos_fts_ad; DROP TRIGGER photos_fts_au; "
            "ALTER TABLE photos DROP COLUMN tiled; "
            "ALTER TABLE photos DROP COLUMN phash; "
            "ALTER TABLE photos DROP COLUMN gated; "
            "ALTER TABLE photos DROP COLUMN category; "
            "ALTER TABLE photos DROP COLUMN hidden; "
            "ALTER TABLE photos DROP COLUMN deleted_at; "
            "ALTER TABLE photos DROP COLUMN derivative; "
            "ALTER TABLE photos DROP COLUMN date_taken; "
            "ALTER TABLE photos DROP COLUMN offloaded; "
            "ALTER TABLE photos DROP COLUMN hidden_origin; "
            "ALTER TABLE photos DROP COLUMN vision_text; "
            "DROP TABLE IF EXISTS photo_warnings; "
            "DROP TABLE IF EXISTS person_tags; "
            "DROP TABLE IF EXISTS people; "
            "DROP TABLE IF EXISTS photo_assets; "
            "DROP TABLE IF EXISTS photo_embeddings; "
            "DELETE FROM schema_version WHERE version >= 2;"
        )
        con.commit()
        con.close()
        out = cli.run("migrate", "--dry-run")
        check(
            "pending migration(s): v2, v3, v4, v5, v6, v7, v8, v9, v10, v11, v12, v13, v14" in out,
            "dry run reports pending migrations",
        )
        check("dry run: nothing applied" in out, "dry run applies nothing")
        out = cli.run("migrate")
        check("migrated: v1 -> v14" in out, "migrate applies pending migrations")
        check("backup:" in out, "migrate backs up first")
        con = db_open(db_path)
        check(count(con, "SELECT COUNT(*) FROM photos_fts") == 4, "fts rebuilt with 4 rows")
        con.close()
        out = cli.run("search", "MOCK")
        check("4 match(es)" in out, "search works after migrate")

        # v14 repair: a v13-era catalog stored asset uuids lowercased, which
        # breaks AppleScript's case-sensitive `media item id` lookup. The
        # migration recovers the true case from the originals/ file name.
        true_case = "AA11BB22-CC33-44DD-55EE-FF6677889900"
        con = db_open(db_path)
        con.execute(
            "INSERT INTO photo_assets (source_id, uuid, photo_id) VALUES (1, ?, 1)",
            (true_case.lower(),),
        )
        con.execute(
            "INSERT INTO locations (photo_id, source_id, path, mtime, size) "
            "VALUES (1, 1, ?, 0, 0)",
            (f"/fake/originals/A/{true_case}.jpeg",),
        )
        con.execute("DELETE FROM schema_version WHERE version >= 14")
        con.commit()
        con.close()
        out = cli.run("migrate")
        check("migrated: v13 -> v14" in out, "v14 re-applies after a rewind")
        con = db_open(db_path)
        check(
            con.execute(
                "SELECT uuid FROM photo_assets WHERE photo_id = 1"
            ).fetchone()[0]
            == true_case,
            "v14 recovers true-case asset uuids from paths",
        )
        check(
            con.execute(
                "SELECT photo_id FROM photo_assets WHERE uuid = ?",
                (true_case.lower(),),
            ).fetchone()[0]
            == 1,
            "asset lookups are case-insensitive after v14",
        )
        con.close()

        print("\n[16] export jsonl/csv")
        out = cli.run("export")
        lines = [line for line in out.strip().splitlines() if line.strip()]
        check(len(lines) == 4, "jsonl has 4 records")
        records = [json.loads(line) for line in lines]
        check(
            all(r["text"] == "MOCK EXTRACTED TEXT" for r in records), "jsonl text field"
        )
        check(all(r["paths"] for r in records), "jsonl includes paths")
        check(all(r["status"] == "done" for r in records), "jsonl status field")
        out = cli.run("export", "--format", "csv")
        rows = list(csv.reader(out.strip().splitlines()))
        check(
            rows[0][0] == "id" and "text" in rows[0] and "context" in rows[0],
            "csv header",
        )
        check(len(rows) == 5, "csv has 4 records + header")
        check(
            rows[1][rows[0].index("text")] == "MOCK EXTRACTED TEXT", "csv text cell"
        )
        out = cli.run("export", "--status", "error")
        check(out.strip() == "", "export --status error is empty when none")
        outfile = work / "export.jsonl"
        cli.run("export", "--output", str(outfile))
        check(
            len(outfile.read_text().strip().splitlines()) == 4, "export --output writes file"
        )

        print("\n[17] slice scans on a folder (dates, limit, ids-file)")
        folder = build_slice_folder(work)
        c2 = fresh_cli("datedb")
        out = c2.run("scan", str(folder), "--date-from", "2013", "--date-to", "2013")
        check("new 3" in out and "slice-skipped 3" in out, "date slice queues only 2013")
        out = c2.run("scan", "--date-from", "2024")
        check("new 3" in out, "open-ended year slice queues 2024")
        c3 = fresh_cli("limitdb")
        out = c3.run("scan", str(folder), "--limit", "2")
        check("new 2" in out, "limit caps newly queued photos")
        ids = work / "ids.txt"
        ids.write_text(f"{folder / 'a2013.jpg'}\n{folder / 'd2024.jpg'}\n")
        c4 = fresh_cli("idsdb")
        out = c4.run("scan", str(folder), "--ids-file", str(ids))
        check("new 2" in out, "ids-file with paths queues exactly those")
        c5 = fresh_cli("badslice")
        out = c5.run("scan", str(folder), "--album", "Trip", expect=2)
        check("photo libraries" in out, "album on a folder errors")
        out = c5.run("scan", str(folder), "--favorites", expect=2)
        check("photo libraries" in out, "favorites on a folder errors")

        print("\n[18] slice scans on the library (album/favorites/uuid via apdb)")
        c6 = fresh_cli("albumdb")
        out = c6.run("scan", str(lib), "--album", "Trip")
        check("new 2" in out, "album slice queues Trip members")
        con = db_open(work / "db-albumdb.db")
        check(count(con, "SELECT COUNT(*) FROM photos") == 2, "album queued exactly 2 photos")
        con.close()
        out = c6.run("scan", str(lib), "--album", "Nope", expect=2)
        check("not found" in out, "unknown album errors")
        c7 = fresh_cli("favdb")
        out = c7.run("scan", str(lib), "--favorites")
        check("new 2" in out, "favorites slice queues flagged photos")
        uuidfile = work / "uuids.txt"
        uuidfile.write_text("v3\n")
        c8 = fresh_cli("uuiddb")
        out = c8.run("scan", str(lib), "--ids-file", str(uuidfile))
        check("new 1" in out, "ids-file with a uuid resolves via the library db")
        uuidfile2 = work / "uuids2.txt"
        uuidfile2.write_text("v3\nDEADBEEF\n")
        c9 = fresh_cli("unknownuuid")
        out = c9.run("scan", str(lib), "--ids-file", str(uuidfile2), expect=2)
        check("not found in this library" in out, "unknown uuid errors")

        print("\n[19] run applies slice filters to its scan phase")
        c10 = fresh_cli("rundb")
        out = c10.run("scan", str(lib), "--album", "Empty")
        check("new 0" in out, "empty album queues nothing")
        con = db_open(work / "db-rundb.db")
        check(count(con, "SELECT COUNT(*) FROM sources") == 1, "source registered")
        con.close()
        out = c10.run("run", "--album", "Trip", "--skip-preflight")
        check("Queue drained" in out, "run drains the slice queue")
        con = db_open(work / "db-rundb.db")
        check(
            count(con, "SELECT COUNT(*) FROM photos WHERE status='done'") == 2,
            "run processed exactly the album slice",
        )
        con.close()

        print("\n[20] serve: read-only web UI")
        web_port = free_port()
        proc = subprocess.Popen(
            cli.cmd + ["serve", "--port", str(web_port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        base = f"http://127.0.0.1:{web_port}"
        up = False
        for _ in range(100):
            try:
                up = requests.get(base + "/", timeout=1).status_code == 200
                if up:
                    break
            except Exception:
                time.sleep(0.1)
        check(up, "web UI comes up")
        if up:
            r = requests.get(base + "/")
            check("4 photos" in r.text, "list page shows catalog counts")
            check("/thumb/1" in r.text, "cards reference thumbnails")
            r = requests.get(base + "/?q=MOCK")
            check("4 match(es) for" in r.text, "web search finds matches")
            check("MOCK EXTRACTED TEXT" in r.text, "web search shows snippets")
            r = requests.get(base + "/?q=zzznothing")
            check("0 match(es)" in r.text, "web search with no hits")
            r = requests.get(base + "/photo/1")
            check(r.status_code == 200 and "recovered text" in r.text, "detail page renders")
            check(
                "Old iPhoto Library.photolibrary" in r.text and ">library<" in r.text,
                "detail shows the source library for each location",
            )
            check("reveal in Finder" in r.text, "detail offers reveal-in-Finder links")
            r = requests.get(base + "/thumb/1")
            check(
                r.status_code == 200
                and r.headers["Content-Type"] == "image/jpeg"
                and len(r.content) > 100,
                "thumbnail served",
            )
            r = requests.get(base + "/image/1")
            check(
                r.status_code == 200 and int(r.headers["Content-Length"]) > 100,
                "original image served",
            )
            con_heic = db_open(db_path)
            heic_id = con_heic.execute(
                "SELECT p.id FROM photos p JOIN locations l ON l.photo_id=p.id "
                "WHERE l.path LIKE '%.heic' LIMIT 1"
            ).fetchone()[0]
            con_heic.close()
            r = requests.get(f"{base}/image/{heic_id}")
            check(
                r.status_code == 200
                and r.headers["Content-Type"] == "image/jpeg"
                and r.content[:2] == b"\xff\xd8",
                "HEIC original served as a converted JPEG",
            )
            check(
                not (work / "views" / "1.jpg").exists()
                and (work / "views" / f"{heic_id}.jpg").exists(),
                "browser-safe originals bypass the view cache, HEIC fills it",
            )
            check(requests.get(base + "/photo/99999").status_code == 404, "unknown photo 404s")
            check(requests.get(base + "/thumb/abc").status_code == 404, "non-numeric id 404s")
            check(
                requests.get(base + "/photo/../../etc/passwd").status_code == 404,
                "path traversal is rejected",
            )
            check(
                requests.get(base + "/reveal/1?loc=999999").status_code == 404,
                "reveal with unknown location 404s",
            )
            check(
                requests.get(base + "/reveal/99999?loc=1").status_code == 404,
                "reveal for unknown photo 404s",
            )
            check(
                requests.get(base + "/reveal/2?loc=1").status_code == 404,
                "reveal cannot use another photo's location",
            )
        proc.terminate()
        proc.wait(timeout=10)
        c11 = fresh_cli("nosuchdb")
        proc2 = subprocess.Popen(
            c11.cmd + ["serve"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        out2, _ = proc2.communicate(timeout=30)
        check(proc2.returncode == 2, "serve errors on a missing catalog")

        print("\n[21] idle detection pauses while another model is loaded")
        c12 = fresh_cli("idledb", "idle_poll_s = 1\n")
        idle_folder = work / "idlefolder"
        idle_folder.mkdir()
        make_text_image(idle_folder / "one.jpg", ["idle one"])
        make_text_image(idle_folder / "two.jpg", ["idle two"])
        c12.run("scan", str(idle_folder))
        ps_file.write_text("othermodel:7b\n")
        runlog = work / "idle-run.log"
        with open(runlog, "w") as logf:
            idle_proc = subprocess.Popen(
                c12.cmd + ["run", "--skip-preflight"],
                stdout=logf,
                stderr=subprocess.STDOUT,
                text=True,
            )
        paused = False
        for _ in range(100):
            if "paused: other model" in runlog.read_text():
                paused = True
                break
            time.sleep(0.2)
        check(paused, "run pauses while a foreign model is loaded")
        ps_file.write_text("")
        idle_proc.wait(timeout=60)
        check(idle_proc.returncode == 0, "run finishes after the pause clears")
        log = runlog.read_text()
        check("resumed: Ollama is free" in log, "run reports resuming")
        con = db_open(work / "db-idledb.db")
        check(
            count(con, "SELECT COUNT(*) FROM photos WHERE status='done'") == 2,
            "paused run still processes everything",
        )
        reset_queued(con)
        con.close()
        ps_file.write_text("othermodel:7b\n")
        out = c12.run("run", "--no-idle-detection", "--skip-preflight")
        check("paused: other model" not in out, "--no-idle-detection skips the pause")
        check("Queue drained" in out, "run drains without pausing")
        ps_file.write_text("")

        print("\n[22] repetition-loop salvage and bounded timeouts")
        c13 = fresh_cli("loopdb")
        loop_folder = work / "loopfolder"
        loop_folder.mkdir()
        make_text_image(loop_folder / "one.jpg", ["loop one"])
        make_text_image(loop_folder / "two.jpg", ["loop two"])
        c13.run("scan", str(loop_folder))
        mode_file.write_text("junkonce")
        out = c13.run("run", "--skip-preflight")
        con = db_open(work / "db-loopdb.db")
        check(
            count(con, "SELECT COUNT(*) FROM photos WHERE status='done'") == 2,
            "truncated-JSON output is salvaged by the anti-loop retry",
        )
        con.close()
        mode_file.write_text("ok")

        c14 = fresh_cli("timeoutdb", "request_timeout_s = 1\nmax_attempts = 2\n")
        c14.run("scan", str(loop_folder))
        mode_file.write_text("slow")
        out = c14.run("run", "--skip-preflight")
        con = db_open(work / "db-timeoutdb.db")
        check(
            count(con, "SELECT COUNT(*) FROM photos WHERE status='error'") == 2,
            "request timeouts mark photos as errors after bounded attempts",
        )
        check(
            count(con, "SELECT COUNT(*) FROM photos WHERE error LIKE '%timeout%'") == 2,
            "timeout errors are recorded",
        )
        con.close()
        mode_file.write_text("ok")

        print("\n[23] tiling fallback for photos the single pass cannot parse")
        c15 = fresh_cli("tiledb")
        tile_folder = work / "tilefolder"
        tile_folder.mkdir()
        make_text_image(tile_folder / "dense.jpg", ["DENSE RECEIPT LINE ONE", "TOTAL DUE 99"])
        make_text_image(tile_folder / "plain.jpg", ["plain photo"])
        c15.run("scan", str(tile_folder))
        mode_file.write_text("junkfirst2")
        out = c15.run("run", "--skip-preflight")
        check("(tiled)" in out, "dense photo falls back to quadrant tiling")
        con = db_open(work / "db-tiledb.db")
        check(
            count(con, "SELECT COUNT(*) FROM photos WHERE tiled=1 AND status='done'") == 1,
            "tiling recorded and completed",
        )
        check(
            count(con, "SELECT COUNT(*) FROM photos WHERE status='done'") == 2,
            "the plain photo still extracts normally",
        )
        check(
            count(con, "SELECT COUNT(*) FROM photos WHERE raw_response LIKE '%tile boundary%'") == 1,
            "tiled raw responses are kept with boundaries",
        )
        con.close()
        mode_file.write_text("ok")

        print("\n[24] reprocess selections")
        out = c15.run("reprocess", "--tiled")
        check("requeued 1" in out, "reprocess --tiled selects the tiled photo")
        out = c15.run("run", "--skip-preflight", "--limit", "5")
        check("Queue drained" in out, "requeued photo reprocessed")
        con = db_open(work / "db-tiledb.db")
        con.execute("UPDATE photos SET has_text=0 WHERE id=1")
        con.execute("UPDATE photos SET status='error' WHERE id=2")
        con.commit()
        con.close()
        out = c15.run("reprocess", "--no-text")
        check("requeued 1" in out, "reprocess --no-text selects no-text photos")
        out = c15.run("reprocess", "--errors")
        check("requeued 1" in out, "reprocess --errors selects failed photos")
        idsfile = work / "reprocess-ids.txt"
        idsfile.write_text(f"2\n{tile_folder / 'dense.jpg'}\n")
        out = c15.run("reprocess", "--ids-file", str(idsfile))
        check("requeued 2" in out, "reprocess --ids-file accepts ids and paths")
        c15.run("reprocess", expect=2)
        out = c15.run("run", "--skip-preflight")
        check("Queue drained" in out, "reprocess queue drains")

        print("\n[25] watch mode picks up new photos as they appear")
        c16 = fresh_cli("watchdb")
        watch_folder = work / "watchfolder"
        watch_folder.mkdir()
        make_text_image(watch_folder / "a.jpg", ["watch a"])
        make_text_image(watch_folder / "b.jpg", ["watch b"])
        c16.run("scan", str(watch_folder))
        watchlog = work / "watch-run.log"
        with open(watchlog, "w") as logf:
            watch_proc = subprocess.Popen(
                c16.cmd
                + ["run", "--watch", "--watch-interval", "1", "--skip-preflight"],
                stdout=logf,
                stderr=subprocess.STDOUT,
                text=True,
            )

        def wait_for(predicate, tries: int = 100) -> bool:
            for _ in range(tries):
                if predicate():
                    return True
                time.sleep(0.2)
            return False

        con = db_open(work / "db-watchdb.db")
        drained = wait_for(
            lambda: count(con, "SELECT COUNT(*) FROM photos WHERE status='done'") == 2
        )
        check(drained, "watch mode processes the initial queue")
        make_text_image(watch_folder / "c.jpg", ["watch c"])
        picked_up = wait_for(
            lambda: count(con, "SELECT COUNT(*) FROM photos WHERE status='done'") == 3
        )
        check(picked_up, "watch mode picks up and processes a new photo")
        con.close()
        watch_proc.terminate()
        watch_proc.wait(timeout=30)
        log = watchlog.read_text()
        check("Watching 1 source(s)" in log, "watch mode announces watching")
        check("watch: 1 new photo(s) queued" in log, "watch mode reports new arrivals")

        print("\n[26] meme identification via perceptual hash")
        c17 = fresh_cli("memedb")
        meme_folder = work / "memefolder"
        meme_folder.mkdir()
        make_text_image(meme_folder / "m1.jpg", ["WHEN THE CODE WORKS"])
        im = Image.open(meme_folder / "m1.jpg").crop((4, 4, 636, 476))
        ImageEnhance.Brightness(im).enhance(1.05).save(meme_folder / "m2.jpg")
        Image.effect_noise((640, 480), 100).convert("RGB").save(meme_folder / "other.jpg")
        c17.run("scan", str(meme_folder))
        c17.run("run", "--skip-preflight")
        con = db_open(work / "db-memedb.db")
        check(
            count(con, "SELECT COUNT(*) FROM photos WHERE phash IS NOT NULL") == 3,
            "perceptual hashes computed at scan time",
        )
        con.close()
        out = c17.run("memes")
        check("1 meme-like group" in out, "memes finds the near-identical pair")
        check("MOCK EXTRACTED TEXT" in out, "group text snippet is shown")
        check("other.jpg" not in out, "unrelated photos are not grouped")
        meme_port = free_port()
        meme_proc = subprocess.Popen(
            c17.cmd + ["serve", "--port", str(meme_port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        meme_base = f"http://127.0.0.1:{meme_port}"
        if wait_for(lambda: _try_get(meme_base + "/memes") is not None, tries=50):
            r = _try_get(meme_base + "/memes")
            check("1 meme-like group" in r.text, "web UI shows meme groups")
            r = _try_get(meme_base + "/")
            check("Memes" in r.text, "web UI links the Memes tab")
        else:
            check(False, "memes web page comes up")
        meme_proc.terminate()
        meme_proc.wait(timeout=10)

        print("\n[27] multi-process workers with stale-lease recovery")
        c18 = fresh_cli("workdb")
        work_folder = work / "workfolder"
        work_folder.mkdir()
        for name in ("w1.jpg", "w2.jpg", "w3.jpg", "w4.jpg"):
            make_text_image(work_folder / name, [f"worker {name}"])
        c18.run("scan", str(work_folder))
        out = c18.run("run", "--workers", "2", "--skip-preflight")
        check("All workers finished." in out, "multi-worker run completes")
        check("[w1]" in out and "[w2]" in out, "worker output is prefixed")
        con = db_open(work / "db-workdb.db")
        check(
            count(con, "SELECT COUNT(*) FROM photos WHERE status='done'") == 4,
            "both workers processed the queue",
        )
        stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
        con.execute(
            "UPDATE photos SET status='processing', started_at=? "
            "WHERE id IN (SELECT id FROM photos ORDER BY id LIMIT 1)",
            (stale,),
        )
        con.commit()
        con.close()
        out = c18.run("run", "--workers", "2", "--skip-preflight", "--no-scan")
        check("reclaimed 1 stale" in out, "stale worker leases are reclaimed")
        con = db_open(work / "db-workdb.db")
        check(
            count(con, "SELECT COUNT(*) FROM photos WHERE status='done'") == 4,
            "reclaimed photo is reprocessed",
        )
        con.close()
        c18.run("run", "--workers", "2", "--limit", "2", expect=2)

        print("\n[28] two-tier gate: textless photos finish at the cheap tier")
        c19 = fresh_cli("gatedb", 'two_tier = true\nprefilter_model = "gemma3:mock"\n')
        gate_folder = work / "gatefolder"
        gate_folder.mkdir()
        make_text_image(gate_folder / "g1.jpg", ["gate one"])
        make_text_image(gate_folder / "g2.jpg", ["gate two"])
        c19.run("scan", str(gate_folder))
        out = c19.run("run", "--skip-preflight")
        check("(gated)" not in out, "text photos skip the gate tier")
        con = db_open(work / "db-gatedb.db")
        check(
            count(con, "SELECT COUNT(*) FROM photos WHERE gated=1") == 0,
            "gate says has_text -> full pass",
        )
        check(
            count(con, "SELECT COUNT(*) FROM photos WHERE category='document'") == 2,
            "full pass fills category",
        )
        con.close()
        mode_file.write_text("gatenotext")
        # Vision reads PIL-drawn text, so the textless fixtures must carry
        # no text at all — otherwise the gate is skipped (vision_text set)
        # and the gate tier is never exercised. The two plain images are
        # inverted copies so content-hash dedup keeps them as two photos.
        gate_folder2 = work / "gatefolder2"
        gate_folder2.mkdir()
        make_plain_image(gate_folder2 / "p1.jpg")
        ImageOps.invert(Image.open(gate_folder2 / "p1.jpg")).save(
            gate_folder2 / "p2.jpg"
        )
        c20 = fresh_cli("gatedb2", 'two_tier = true\nprefilter_model = "gemma3:mock"\n')
        c20.run("scan", str(gate_folder2))
        out = c20.run("run", "--skip-preflight")
        check("(gated)" in out, "textless photos finish at the gate tier")
        con = db_open(work / "db-gatedb2.db")
        check(
            count(con, "SELECT COUNT(*) FROM photos WHERE gated=1 AND has_text=0") == 2,
            "gate tier recorded and textless",
        )
        check(
            count(con, "SELECT COUNT(*) FROM photos WHERE model='gemma3:mock'") == 2,
            "gate model recorded for gated photos",
        )
        check(
            count(con, "SELECT COUNT(*) FROM photos WHERE category='scene'") == 2,
            "gate fills category",
        )
        con.close()
        out = c20.run("reprocess", "--gated")
        check("requeued 2" in out, "reprocess --gated re-selects gate-tier photos")
        mode_file.write_text("ok")
        out = c20.run("run", "--skip-preflight")
        con = db_open(work / "db-gatedb2.db")
        check(
            count(con, "SELECT COUNT(*) FROM photos WHERE gated=0 AND model='gemma4:mock'") == 2,
            "reprocessed photos upgrade to the full model",
        )
        con.close()
        out = c20.run("categories")
        check("document" in out, "categories lists what the models assigned")
        gate_port = free_port()
        gate_proc = subprocess.Popen(
            c20.cmd + ["serve", "--port", str(gate_port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        gate_base = f"http://127.0.0.1:{gate_port}"
        if wait_for(lambda: _try_get(gate_base + "/") is not None, tries=50):
            r = _try_get(gate_base + "/?category=document")
            check("2 photo(s) with status 'all'" in r.text, "web category filter works")
            r = _try_get(gate_base + "/?text=no")
            check("0 photo(s)" in r.text, "web text filter works")
        else:
            check(False, "gate web UI comes up")
        gate_proc.terminate()
        gate_proc.wait(timeout=10)

        print("\n[29] help command")
        out = cli.run("--version")
        check(
            re.search(r"^phototext \d+\.\d+", out) is not None,
            "--version prints the version",
        )
        out = cli.run("help")
        check("GETTING STARTED" in out, "help prints the usage guide")
        out = cli.run("help", "search")
        check("search [OPTIONS] {query}" in out, "help for a command shows its help")
        cli.run("help", "nosuchcmd", expect=2)
        out = c19.run("search", "MOCK", "--category", "document")
        check("2 match(es)" in out, "search filters by category")
        out = c19.run("search", "MOCK", "--category", "scene")
        check("no matches" in out, "search category filter excludes others")

        print("\n[30] hide, delete, and the trash bin")
        c21 = fresh_cli("hidedb")
        hide_folder = work / "hidefolder"
        hide_folder.mkdir()
        make_text_image(hide_folder / "h1.jpg", ["hide one"])
        make_text_image(hide_folder / "h2.jpg", ["hide two"])
        c21.run("scan", str(hide_folder))
        c21.run("run", "--skip-preflight")
        out = c21.run("hide", "1")
        check("hid 1" in out, "hide command works")
        out = c21.run("search", "MOCK")
        check("1 match(es)" in out, "hidden photos are excluded from search")
        out = c21.run("search", "MOCK", "--hidden")
        check("2 match(es)" in out, "search --hidden includes hidden photos")
        out = c21.run("unhide", "1")
        check("unhid 1" in out, "unhide restores visibility")
        out = c21.run("delete", "2")
        check("moved 1" in out, "delete moves to the trash")
        out = c21.run("trash")
        check("[2]" in out, "trash lists deleted photos")
        out = c21.run("search", "MOCK")
        check("1 match(es)" in out, "deleted photos are excluded")
        out = c21.run("scan", str(hide_folder))
        check("new 0" in out, "rescan does not resurrect deleted photos (tombstone)")
        out = c21.run("restore", "2")
        check("restored 1" in out, "restore brings photos back")
        out = c21.run("search", "MOCK")
        check("2 match(es)" in out, "restored photo is searchable again")
        make_text_image(hide_folder / "h3.jpg", ["hide three"])
        c21.run("scan", str(hide_folder))
        out = c21.run("delete", "3")
        check("moved 1" in out, "a queued photo can be deleted")
        out = c21.run("run", "--skip-preflight")
        check("Nothing to process" in out, "deleted queued photo is not processed")
        con = db_open(work / "db-hidedb.db")
        check(
            count(con, "SELECT COUNT(*) FROM photos WHERE status='done'") == 2,
            "done count unchanged after deleting queued photo",
        )
        con.close()
        out = c21.run("purge", "3")
        check("purged 1" in out, "purge forgets permanently")
        con = db_open(work / "db-hidedb.db")
        check(count(con, "SELECT COUNT(*) FROM photos") == 2, "purged row gone")
        con.close()
        out = c21.run("scan", str(hide_folder))
        check("new 1" in out, "rescan re-adds purged photos")
        out = c21.run("status")
        check("[1] [folder]" in out, "status lists source ids")
        hide_port = free_port()
        hide_proc = subprocess.Popen(
            c21.cmd + ["serve", "--port", str(hide_port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        hide_base = f"http://127.0.0.1:{hide_port}"
        if wait_for(lambda: _try_get(hide_base + "/") is not None, tries=50):
            r = requests.post(f"{hide_base}/delete/1", data={"token": "x"}, timeout=5)
            check(r.status_code == 404, "read-only server rejects write actions")
            detail = _try_get(hide_base + "/photo/1")
            check("name='token'" not in detail.text, "read-only server hides action forms")
        else:
            check(False, "read-only serve comes up")
        hide_proc.terminate()
        hide_proc.wait(timeout=10)
        wr_port = free_port()
        wr_proc = subprocess.Popen(
            c21.cmd + ["serve", "--port", str(wr_port), "--writable"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        wr_base = f"http://127.0.0.1:{wr_port}"
        if wait_for(lambda: _try_get(wr_base + "/photo/1") is not None, tries=50):
            detail = _try_get(wr_base + "/photo/1")
            m = re.search(r"name='token' value='([0-9a-f]+)'", detail.text)
            check(m is not None, "writable server embeds the token")
            if m:
                token = m.group(1)
                r = requests.post(
                    f"{wr_base}/delete/1", data={"token": token}, timeout=5,
                    allow_redirects=False,
                )
                check(r.status_code == 303, "tokened delete redirects")
                con = db_open(work / "db-hidedb.db")
                check(
                    count(con, "SELECT COUNT(*) FROM photos WHERE deleted_at IS NOT NULL") == 1,
                    "web delete lands in the trash",
                )
                con.close()
                r = requests.post(f"{wr_base}/delete/2", data={"token": "bad"}, timeout=5)
                check(r.status_code == 403, "wrong token is rejected")
                r = requests.post(f"{wr_base}/delete/2", timeout=5)
                check(r.status_code == 403, "missing token is rejected")
                # Hidden-only view + one-click card toggle
                list_page = _try_get(wr_base + "/")
                check(
                    list_page is not None and "action='/hide/2'" in list_page.text,
                    "writable cards carry hide toggles",
                )
                r = requests.post(
                    f"{wr_base}/hide/2",
                    data={"token": token, "next": "/?hidden=only"},
                    timeout=5, allow_redirects=False,
                )
                check(
                    r.status_code == 303 and r.headers["Location"] == "/?hidden=only",
                    "card toggle returns to the list it came from",
                )
                list_after = _try_get(wr_base + "/")
                check("hidden (1)" in list_after.text, "hidden chip shows the count")
                check("h2.jpg" not in list_after.text, "hidden photo leaves the default view")
                hidden_page = _try_get(wr_base + "/?hidden=only")
                check(
                    hidden_page is not None
                    and "all photos" in hidden_page.text
                    and "h2.jpg" in hidden_page.text
                    and "action='/unhide/2'" in hidden_page.text,
                    "hidden view lists only hidden photos with unhide toggles",
                )
                r = requests.post(
                    f"{wr_base}/unhide/2", data={"token": token},
                    timeout=5, allow_redirects=False,
                )
                check(
                    r.status_code == 303 and r.headers["Location"] == "/photo/2",
                    "toggle without next falls back to the photo page",
                )
                hidden_after = _try_get(wr_base + "/?hidden=only")
                check(
                    hidden_after is not None and "h2.jpg" not in hidden_after.text,
                    "unhidden photo leaves the hidden view",
                )
        else:
            check(False, "writable serve comes up")
        wr_proc.terminate()
        wr_proc.wait(timeout=10)

        print("\n[31] unscan removes a source and its unique photos")
        c22 = fresh_cli("unscandb")
        hide_src = work / "unscan-shared"
        hide_src.mkdir()
        (hide_src / "dup.jpg").write_bytes((hide_folder / "h1.jpg").read_bytes())
        c22.run("scan", str(hide_src))
        unscan_folder = work / "unscanfolder"
        unscan_folder.mkdir()
        (unscan_folder / "dup.jpg").write_bytes((hide_folder / "h1.jpg").read_bytes())
        make_text_image(unscan_folder / "unique.jpg", ["only here"])
        out = c22.run("scan", str(unscan_folder))
        check("new 1" in out, "duplicate content dedups across sources")
        con = db_open(work / "db-unscandb.db")
        check(count(con, "SELECT COUNT(*) FROM photos") == 2, "two photos registered")
        con.close()
        out = c22.run("unscan", str(unscan_folder))
        check("forgotten 1" in out, "unscan forgets photos only seen there")
        con = db_open(work / "db-unscandb.db")
        check(count(con, "SELECT COUNT(*) FROM photos") == 1, "shared photo survives")
        check(count(con, "SELECT COUNT(*) FROM sources") == 1, "source unregistered")
        con.close()
        c22.run("unscan", "99", expect=2)

        print("\n[32] warnings and the decompression-bomb guard")
        c23 = fresh_cli("bombdb", "max_image_pixels = 1000\n")
        bomb_folder = work / "bombfolder"
        bomb_folder.mkdir()
        make_text_image(bomb_folder / "big.jpg", ["looks innocent"])
        out = c23.run("scan", str(bomb_folder))
        check("new 1" in out, "oversized photo still registers at scan time")
        out = c23.run("run", "--skip-preflight")
        con = db_open(work / "db-bombdb.db")
        check(
            count(con, "SELECT COUNT(*) FROM photos WHERE status='error' "
                       "AND error LIKE '%pixels%'") == 1,
            "pixel budget exceeded is a recorded error (no sips fallback)",
        )
        con.close()
        con = db_open(work / "db-bombdb.db")
        db_ok = con.execute("SELECT id FROM photos LIMIT 1").fetchone()[0]
        con.close()
        from phototext import db as dbmod

        con = db_open(work / "db-bombdb.db")
        con.row_factory = None
        dbmod.record_warning(con, db_ok, "TestWarning", "first")
        dbmod.record_warning(con, db_ok, "TestWarning", "first")
        con.commit()
        rows = con.execute(
            "SELECT COUNT(*) FROM photo_warnings WHERE photo_id=?", (db_ok,)
        ).fetchone()[0]
        check(rows == 1, "warnings are deduped per photo+kind+message")
        con.close()
        out = c23.run("results", "--status", "error")
        check("warning: TestWarning" in out, "results shows recorded warnings")
        out = c23.run("status")
        check("warnings: 1" in out, "status counts warnings")

        print("\n[33] deferred photos (iCloud inventory, promotion)")
        c24 = fresh_cli("deferdb")
        defer_folder = work / "deferfolder"
        defer_folder.mkdir()
        make_text_image(defer_folder / "real.jpg", ["real file"])
        c24.run("scan", str(defer_folder))
        con = db_open(work / "db-deferdb.db")
        con.row_factory = None
        photo_id = con.execute("SELECT id FROM photos LIMIT 1").fetchone()[0]
        digest = con.execute("SELECT sha256 FROM photos WHERE id=?", (photo_id,)).fetchone()[0]
        dbmod.ensure_deferred_photo(con, 1, "CLOUD-1", None, False)
        dbmod.ensure_deferred_photo(con, 1, "CLOUD-2", str(defer_folder / "real.jpg"), True)
        con.commit()
        check(
            con.execute("SELECT COUNT(*) FROM photos WHERE status='deferred'").fetchone()[0] == 1,
            "cloud-only photo without preview is deferred",
        )
        check(
            con.execute("SELECT COUNT(*) FROM photos WHERE status='queued' AND derivative=1").fetchone()[0] == 1,
            "cloud-only photo with preview is queued and flagged",
        )
        con.close()
        out = c24.run("run", "--skip-preflight")
        check("deferred 1" in out or "queued 1" in out, "run reports deferred photos separately")
        con = db_open(work / "db-deferdb.db")
        con.row_factory = None
        check(
            con.execute("SELECT COUNT(*) FROM photos WHERE status='deferred'").fetchone()[0] == 1,
            "deferred photos are never claimed",
        )
        kept = con.execute("SELECT id FROM photos WHERE sha256=?", (digest,)).fetchone()[0]
        promoted = dbmod.promote_deferred(
            con, "CLOUD-1", digest, 123, 1, str(defer_folder / "real.jpg"), 1,
        )
        con.commit()
        check(
            con.execute("SELECT COUNT(*) FROM photos WHERE sha256 LIKE 'deferred:%'").fetchone()[0] == 1,
            "promotion collapses the deferred row into the content row",
        )
        con.close()

        print("\n[36] people: name, match, review, reset, search, web UI")
        ppl_dir = work / "peopledir"
        ppl_dir.mkdir()
        ppl_cfg = work / "config-peopledb.toml"
        ppl_cfg.write_text(
            f'ollama_url = "http://127.0.0.1:{port}"\n'
            f'model = "{MODEL}"\n'
            f'db_path = "{ppl_dir}/catalog.db"\n'
            "person_min_confidence = 0.6\n"
            "face_detection = false\n"  # [39] covers faces; keep this pass whole-photo
        )
        c26 = CLI(ppl_cfg)
        ppl_src = work / "peoplefolder"
        ppl_src.mkdir()
        make_person_image(ppl_src / "red.jpg", "red")
        make_person_image(ppl_src / "green.jpg", "lime")  # bright green: mock keys on g>150
        make_person_image(ppl_src / "plain.jpg", "white")
        c26.run("scan", str(ppl_src))
        c26.run("run", "--skip-preflight")
        con = db_open(ppl_dir / "catalog.db")
        con.row_factory = None

        def pid_of(name: str) -> int:
            return con.execute(
                "SELECT p.id FROM photos p JOIN locations l ON l.photo_id = p.id "
                "WHERE l.path LIKE ?", (f"%{name}%",),
            ).fetchone()[0]

        red_id, green_id, plain_id = pid_of("red.jpg"), pid_of("green.jpg"), pid_of("plain.jpg")
        out = c26.run("people", "name", str(red_id), "Ryan", "--box", "200,100,240,260")
        check("person 'Ryan' seeded" in out, "people name creates a seed")
        check(
            (ppl_dir / "people" / ".metadata_never_index").exists()
            and (ppl_dir / "people" / "1" / ".metadata_never_index").exists(),
            "seed crop dirs carry spotlight no-index markers",
        )
        check("MOCK PERSON PROFILE" in out, "recognition profile built from the seed crop")
        ryan_id = con.execute("SELECT id FROM people WHERE name='Ryan'").fetchone()[0]
        check(
            (ppl_dir / "people" / str(ryan_id) / f"seed-{red_id}.jpg").is_file(),
            "seed face crop saved on disk",
        )
        check(
            con.execute(
                "SELECT COUNT(*) FROM person_tags WHERE person_id=? AND origin='seed' "
                "AND box='200,100,240,260'", (ryan_id,),
            ).fetchone()[0] == 1,
            "seed tag stored with its box",
        )
        c26.run("people", "name", str(red_id), "Ryan", "--box", "junk", expect=2)
        out = c26.run("people", "name", str(red_id), "Sam", "--box", "10,10,300,300")
        check("person 'Sam' seeded" in out, "second person seeded on the same photo")
        sam_id = con.execute("SELECT id FROM people WHERE name='Sam'").fetchone()[0]
        out = c26.run("people", "list")
        check("Ryan" in out and "Sam" in out, "people list shows both people")
        out = c26.run("people", "photos", "Ryan")
        check(f"[{red_id}]" in out and "seed" in out, "people photos shows the seed")
        c26.run("people", "photos", "NoSuchPerson", expect=2)

        out = c26.run("people", "run")
        check("Matching 2 photo(s) against 2 person(s)" in out, "run evaluates the untagged photos")
        check("2 tag(s) added (2 below threshold)" in out, "green tagged below threshold for both")
        check("2 marked absent" in out, "plain recorded absent for both people")
        green_conf = con.execute(
            "SELECT confidence FROM person_tags WHERE photo_id=? AND person_id=?",
            (green_id, ryan_id),
        ).fetchone()[0]
        check(abs(green_conf - 0.45) < 0.01, "green tag carries the model confidence")
        check(
            con.execute(
                "SELECT COUNT(*) FROM person_tags WHERE photo_id=? AND present=0",
                (plain_id,),
            ).fetchone()[0] == 2,
            "absent rows recorded for both people",
        )
        out = c26.run("people", "run")
        check("nothing to do" in out, "re-run skips fully evaluated photos")
        out = c26.run("people", "photos", "Ryan")
        check("review" in out, "uncertain tag flagged for review")
        out = c26.run("people", "confirm", str(green_id), "Ryan")
        check("confirmed 'Ryan'" in out, "confirm promotes the tag to ground truth")
        out = c26.run("people", "reset", "Ryan")
        check("cleared 1 model tag(s)" in out, "reset drops model tags, keeps seeds and confirmed")
        out = c26.run("people", "run", "--person", "Ryan")
        check("Matching 1 photo(s)" in out, "reset photos become candidates again")
        out = c26.run("search", "MOCK", "--person", "Ryan")
        check("2 match(es)" in out, "search --person finds the tagged photos")
        out = c26.run("search", "MOCK", "--person", "Sam")
        check("2 match(es)" in out, "search --person Sam matches seed + model tag")
        out = c26.run("people", "remove", str(green_id), "Ryan")
        check("removed 'Ryan'" in out, "remove deletes a tag")
        out = c26.run("search", "MOCK", "--person", "Ryan")
        check("1 match(es)" in out, "search reflects the removed tag")
        out = c26.run("people", "rename", "Ryan", "Ry")
        check("renamed 'Ryan' -> 'Ry'" in out, "rename works")
        c26.run("people", "rename", "Ry", "Sam", expect=2)

        web_port = free_port()
        proc = subprocess.Popen(
            c26.cmd + ["serve", "--port", str(web_port), "--writable"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        wr_base = f"http://127.0.0.1:{web_port}"
        up = False
        for _ in range(100):
            try:
                up = requests.get(wr_base + "/", timeout=1).status_code == 200
                if up:
                    break
            except Exception:
                time.sleep(0.1)
        check(up, "writable web UI comes up for people")
        if up:
            detail = requests.get(f"{wr_base}/photo/{red_id}")
            m = re.search(r"name='token' value='([0-9a-f]+)'", detail.text)
            token = m.group(1) if m else ""
            check("pickimg" in detail.text, "detail page embeds the face-box picker")
            check(
                "data-w='640' data-h='480'" in detail.text,
                "picker carries the original display size for box scaling",
            )
            check("drag a box around a face" in detail.text, "detail page explains the picker")
            check("Ry &mdash; seed" in detail.text or "Ry — seed" in detail.text,
                  "detail page shows the person chip")
            r = requests.get(f"{wr_base}/people")
            check("Ry" in r.text and "Sam" in r.text, "people page lists people")
            r = requests.get(f"{wr_base}/person/{sam_id}")
            check("review queue" in r.text, "person page shows the review queue")
            check("reset model tags" in r.text, "person page offers reset/rename/delete")
            r = requests.get(f"{wr_base}/face/{ryan_id}/{red_id}")
            check(
                r.status_code == 200 and r.headers["Content-Type"] == "image/jpeg",
                "seed face crop is served",
            )
            check(requests.get(f"{wr_base}/person/999").status_code == 404,
                  "unknown person 404s")
            r = requests.post(
                f"{wr_base}/person/tag",
                data={"photo_id": plain_id, "name": "Web Person",
                      "box": "5,5,120,120", "token": token},
                timeout=30, allow_redirects=False,
            )
            check(r.status_code == 303, "web tag creates a person (redirects)")
            web_id = None
            try:
                web_id = con.execute(
                    "SELECT id FROM people WHERE name='Web Person'"
                ).fetchone()[0]
            except TypeError:
                pass
            check(web_id is not None, "web tag persisted the person")
            if web_id is not None:
                check(
                    con.execute(
                        "SELECT COUNT(*) FROM person_tags WHERE person_id=? "
                        "AND origin='seed' AND box='5,5,120,120'", (web_id,),
                    ).fetchone()[0] == 1,
                    "web tag stored the seed and box",
                )
                check(
                    con.execute(
                        "SELECT description FROM people WHERE id=?", (web_id,),
                    ).fetchone()[0] is not None,
                    "web tag built a recognition profile via Ollama",
                )
            r = requests.post(
                f"{wr_base}/person/confirm",
                data={"photo_id": green_id, "person_id": sam_id, "token": token},
                timeout=5, allow_redirects=False,
            )
            check(r.status_code == 303, "web confirm redirects")
            check(
                con.execute(
                    "SELECT origin FROM person_tags WHERE photo_id=? AND person_id=?",
                    (green_id, sam_id),
                ).fetchone()[0] == "user",
                "web confirm persisted",
            )
            out = c26.run("people", "confirm", str(green_id), "Sam", "--add-seed")
            check("seed anchor added" in out, "confirm --add-seed adds a profile seed")
            check(
                (ppl_dir / "people" / str(sam_id) / f"seed-{green_id}.jpg").is_file(),
                "confirmed seed crop saved on disk",
            )
            check(
                con.execute(
                    "SELECT COUNT(*) FROM person_tags WHERE person_id=? AND seed=1",
                    (sam_id,),
                ).fetchone()[0] == 2,
                "seed pool grows with the confirmed tag",
            )
            r = requests.get(f"{wr_base}/person/{sam_id}")
            check("2 seed anchor(s)" in r.text, "person page shows the seed count")
            r = requests.post(
                f"{wr_base}/person/remove",
                data={"photo_id": green_id, "person_id": sam_id, "token": token},
                timeout=5, allow_redirects=False,
            )
            check(r.status_code == 303 and con.execute(
                "SELECT COUNT(*) FROM person_tags WHERE photo_id=? AND person_id=?",
                (green_id, sam_id),
            ).fetchone()[0] == 0, "web remove persisted")
            r = requests.post(
                f"{wr_base}/person/rename",
                data={"person_id": web_id, "name": "Web Renamed", "token": token},
                timeout=5, allow_redirects=False,
            )
            check(r.status_code == 303 and con.execute(
                "SELECT COUNT(*) FROM people WHERE name='Web Renamed'"
            ).fetchone()[0] == 1, "web rename persisted")
            r = requests.post(
                f"{wr_base}/person/tag",
                data={"photo_id": plain_id, "name": "Nope"},
                timeout=5, allow_redirects=False,
            )
            check(r.status_code == 403, "missing token rejected")
            r = requests.post(
                f"{wr_base}/person/reset",
                data={"person_id": "", "token": token},
                timeout=5, allow_redirects=False,
            )
            check(r.status_code == 400, "bad person id is a 400")
            r = requests.get(f"{wr_base}/?person={requests.utils.quote('Sam')}")
            check("tagged 'Sam'" in r.text, "list page filters by person")
            # filters compose: hidden composes with person and other chips
            # (green's Sam tag was removed above, so red — Sam's seed — is
            # the person's one remaining photo)
            r = requests.post(
                f"{wr_base}/hide/{red_id}", data={"token": token},
                timeout=5, allow_redirects=False,
            )
            check(r.status_code == 303, "hide red for the compose check")
            r = requests.get(f"{wr_base}/", params={"person": "Sam"})
            check("red.jpg" not in r.text, "person filter drops hidden photos by default")
            r = requests.get(f"{wr_base}/", params={"person": "Sam", "hidden": "only"})
            check(
                "red.jpg" in r.text and "plain.jpg" not in r.text,
                "hidden-only composes with the person filter",
            )
            r = requests.get(f"{wr_base}/", params={"hidden": "only"})
            check(
                "person=Sam&hidden=only" in r.text,
                "person chips keep the hidden filter",
            )
            check(
                "status=done&hidden=only" in r.text,
                "status tabs keep the hidden filter",
            )
            r = requests.get(f"{wr_base}/person/{sam_id}", params={"hidden": "only"})
            check(
                "red.jpg" in r.text and "all photos" in r.text,
                "person page hidden view lists that person's hidden photos",
            )
            requests.post(
                f"{wr_base}/unhide/{red_id}", data={"token": token}, timeout=5
            )
        proc.terminate()
        proc.wait(timeout=10)
        con.close()
        c26.run("people", "delete", "Web Renamed", expect=2)
        out = c26.run("people", "delete", "Web Renamed", "--yes")
        check("deleted person 'Web Renamed'" in out, "people delete removes a person")
        out = c26.run("people", "list")
        check("Web Renamed" not in out and "Sam" in out, "deleted person gone, others remain")
    finally:
        mock.terminate()
        mock.wait()

    print("\n[34] shell autocomplete install")
    sandbox = work / "home34"
    sandbox.mkdir()
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(sandbox)
    try:
        out = c24.run("autocomplete", "zsh")
        check(
            (sandbox / ".zfunc" / "_phototext").is_file(),
            "zsh completion script written",
        )
        zshrc = (sandbox / ".zshrc").read_text()
        check("zfunc" in zshrc and "compinit" in zshrc, "zshrc wires fpath + compinit")
        c24.run("autocomplete", "zsh")
        check(
            sandbox.joinpath(".zshrc").read_text().count("fpath+=~/.zfunc") == 1,
            "re-running does not duplicate rc lines",
        )
        out = c24.run("autocomplete", "bash")
        check(
            (sandbox / ".bash_completions" / "phototext.sh").is_file(),
            "bash completion script written",
        )
        check(
            ".bash_completions/phototext.sh" in (sandbox / ".bashrc").read_text(),
            "bashrc sources the completion script",
        )
        c24.run("autocomplete", "nosuchshell", expect=2)
        console = Path(sys.executable).parent / "phototext"
        proc = subprocess.run(
            [str(console)],
            env={**os.environ, "_PHOTOTEXT_COMPLETE": "complete_bash",
                 "COMP_WORDS": "phototext sc", "COMP_CWORD": "1"},
            capture_output=True, text=True, timeout=60,
        )
        check(
            proc.stdout.strip() == "scan",
            f"completion runtime answers with matching commands (got: {proc.stdout.strip()!r})",
        )
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home

    print("\n[35] named profiles")
    c25 = fresh_cli("profdb")
    prof_src = work / "proffolder"
    prof_src.mkdir()
    make_text_image(prof_src / "one.jpg", ["profile text"])
    c25.run("--profile", "memes", "scan", str(prof_src))
    prof_db = work / "profiles" / "memes" / "catalog.db"
    check(prof_db.is_file(), "profile catalog created under profiles/<name>/")
    con = db_open(prof_db)
    check(count(con, "SELECT COUNT(*) FROM photos") == 1, "profile catalog has the photo")
    con.close()
    out = c25.run("status")
    check("0 photo" in out, "default catalog untouched by profile scan")
    out = c25.run("--profile", "memes", "status")
    check("1 photo" in out, "profile status sees its photos")
    out = c25.run("profiles")
    check("memes" in out and "1 photo" in out, "profiles lists profile with counts")
    c25.run("--profile", "../evil", "status", expect=2)
    (work / "profiles" / "memes" / "config.toml").write_text(
        f'ollama_url = "http://127.0.0.1:{port}"\nmodel = "{MODEL}"\n'
    )
    out = c25.run("--profile", "memes", "status")
    check("1 photo" in out, "profile config.toml replaces base config")

    print("\n[37] date taken: EXIF at scan, slices, backfill, search, timeline")
    port37 = free_port()
    mock37 = start_mock(port37, mode_file)
    try:
        def fresh_cli37(name: str, extra: str = "") -> CLI:
            cfgp = work / f"config-{name}.toml"
            cfgp.write_text(
                f'ollama_url = "http://127.0.0.1:{port37}"\n'
                f'model = "{MODEL}"\n'
                f'db_path = "{work}/db-{name}.db"\n' + extra
            )
            return CLI(cfgp)

        def make_dated_image(path: Path, taken: str) -> None:
            exif = Image.Exif()
            exif.get_ifd(0x8769)[36867] = taken  # DateTimeOriginal
            exif[306] = taken
            im = Image.new("RGB", (640, 480), "steelblue")
            d = ImageDraw.Draw(im)
            d.text((40, 40), "dated photo", fill="white", font=font())
            im.save(path, exif=exif)

        date_src = work / "datefolder"
        date_src.mkdir()
        make_dated_image(date_src / "a2011.jpg", "2011:03:15 08:00:00")
        make_dated_image(date_src / "b2023.jpg", "2023:05:01 10:00:00")
        make_text_image(date_src / "c-noexif.jpg", ["no exif here"])
        os.utime(date_src / "c-noexif.jpg", (1262304000, 1262304000))  # 2010-01-01

        c37 = fresh_cli37("dates")
        c37.run("scan", str(date_src))
        con37 = db_open(work / "db-dates.db")

        def date_of(fragment: str):
            return con37.execute(
                "SELECT p.date_taken FROM photos p JOIN locations l ON l.photo_id=p.id "
                "WHERE l.path LIKE ?", (f"%{fragment}%",),
            ).fetchone()[0]

        check(date_of("a2011") == "2011-03-15T08:00:00", "EXIF date stored at scan")
        check(date_of("b2023") == "2023-05-01T10:00:00", "second EXIF date stored")
        check(date_of("c-noexif") is None, "no EXIF leaves date_taken NULL")

        out = c37.run("backfill-dates", "--from-mtime")
        check(
            "dates recorded: 0 from EXIF, 1 from file mtime" in out,
            "backfill fills the EXIF-less photo from its file mtime",
        )
        check(
            date_of("c-noexif") is not None and date_of("c-noexif").startswith("2010-"),
            "backfilled mtime date recorded",
        )
        out = c37.run("backfill-dates")
        check("all photos already have a date taken" in out, "backfill is a no-op when done")

        # Date slices prefer the EXIF date: file mtimes are 'now', so a 2023
        # slice only matches via EXIF.
        c37b = fresh_cli37("dates-slice")
        out = c37b.run("scan", str(date_src), "--date-from", "2023", "--date-to", "2023")
        check("2023-01-01" in out, "slice widens the year to a date range")
        check("new 1 |" in out, "date slice matches the EXIF date, not the mtime")

        c37.run("run", "--skip-preflight")
        out = c37.run("search", "MOCK", "--year", "2023")
        check("1 match(es)" in out, "search --year filters by date taken")
        out = c37.run("search", "MOCK", "--date-from", "2011", "--date-to", "2011")
        check("1 match(es)" in out, "search --date-from/--date-to filter by date taken")
        out = c37.run("search", "MOCK", "--year", "1999")
        check("no matches" in out, "year with no photos finds nothing")
        c37.run("search", "MOCK", "--year", "20x3", expect=2)

        web37_port = free_port()
        proc37 = subprocess.Popen(
            c37.cmd + ["serve", "--port", str(web37_port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        base37 = f"http://127.0.0.1:{web37_port}"
        up37 = False
        for _ in range(100):
            try:
                up37 = requests.get(base37 + "/", timeout=1).status_code == 200
                if up37:
                    break
            except Exception:
                time.sleep(0.1)
        check(up37, "web UI comes up for the timeline")
        if up37:
            r = requests.get(base37 + "/")
            check("2023" in r.text and "2011" in r.text, "year chips render")
            check("/duplicates" in r.text, "duplicates tab appears in the nav")
            r = requests.get(base37 + "/?year=2023")
            check("taken in 2023" in r.text and "b2023" in r.text, "year filter narrows the list")
            check("a2011" not in r.text, "year filter excludes other years")
            b_id = con37.execute(
                "SELECT p.id FROM photos p JOIN locations l ON l.photo_id=p.id "
                "WHERE l.path LIKE '%b2023%'"
            ).fetchone()[0]
            r = requests.get(base37 + f"/photo/{b_id}")
            check("2023-05-01 10:00" in r.text, "detail page shows the taken date")
        proc37.terminate()
        proc37.wait(timeout=10)
        con37.close()
    finally:
        mock37.terminate()
        mock37.wait()

    print("\n[38] near-duplicate finder")
    c38 = fresh_cli("dupes")

    def make_noise_image(path: Path, seed: int, size=(600, 400)) -> Image.Image:
        rng = random.Random(seed)
        im = Image.new("RGB", size)
        im.putdata(
            [(rng.randrange(256), rng.randrange(256), rng.randrange(256))
             for _ in range(size[0] * size[1])]
        )
        return im

    dup_src = work / "dupefolder"
    dup_src.mkdir()
    noise = make_noise_image(dup_src / "one.jpg", 7)
    noise.save(dup_src / "one.jpg", quality=95)
    noise.resize((300, 200)).save(dup_src / "one-small.jpg", quality=75)
    make_noise_image(dup_src / "two.jpg", 99).save(dup_src / "two.jpg", quality=95)
    c38.run("scan", str(dup_src))
    out = c38.run("duplicates")
    check("1 duplicate group(s)" in out, "resized copy clusters with its original")
    check("keep [" in out and "largest file" in out, "keep-largest hint shown")
    check("one-small.jpg" in out, "duplicate paths listed")
    out = c38.run("duplicates", "--json")
    groups = json.loads(out)
    check(
        len(groups) == 1 and len(groups[0]) == 2
        and {g["path"].rsplit("/", 1)[-1] for g in groups[0]} == {"one.jpg", "one-small.jpg"},
        "json output lists the pair",
    )
    web38_port = free_port()
    proc38 = subprocess.Popen(
        c38.cmd + ["serve", "--port", str(web38_port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base38 = f"http://127.0.0.1:{web38_port}"
    up38 = False
    for _ in range(100):
        try:
            up38 = requests.get(base38 + "/duplicates", timeout=1).status_code == 200
            if up38:
                break
        except Exception:
            time.sleep(0.1)
    check(up38, "duplicates tab serves")
    if up38:
        r = requests.get(base38 + "/duplicates")
        check("keep (largest)" in r.text, "web duplicates page marks the keep candidate")
    proc38.terminate()
    proc38.wait(timeout=10)
    out = c38.run("duplicates", "--threshold", "0")
    check("no duplicate groups found" in out, "threshold 0 finds nothing (distance 1)")
    con38 = db_open(work / "db-dupes.db")
    con38.execute(
        "UPDATE photos SET derivative = 1 WHERE id = (SELECT p.id FROM photos p "
        "JOIN locations l ON l.photo_id = p.id WHERE l.path LIKE '%one-small%')"
    )
    con38.commit()
    out = c38.run("duplicates")
    check(
        "no duplicate groups found" in out,
        "iCloud preview proxies are excluded from duplicates",
    )
    con38.close()

    print("\n[39] face detection: auto-box, crops, and the no-faces skip")
    port39 = free_port()
    mock39 = start_mock(port39, mode_file)
    try:
        cfg39 = work / "config-faces.toml"
        cfg39.write_text(
            f'ollama_url = "http://127.0.0.1:{port39}"\n'
            f'model = "{MODEL}"\n'
            f'db_path = "{work}/db-faces.db"\n'
            "person_min_confidence = 0.6\n"
            "face_detection = true\n"
        )
        c39 = CLI(cfg39)
        face_src = work / "facefolder"
        face_src.mkdir()
        make_person_image(face_src / "red.jpg", "red")
        make_person_image(face_src / "green.jpg", "lime")
        make_person_image(face_src / "plain.jpg", "white")
        c39.run("scan", str(face_src))
        c39.run("run", "--skip-preflight")
        con39 = db_open(work / "db-faces.db")

        def fpid_of(name: str) -> int:
            return con39.execute(
                "SELECT p.id FROM photos p JOIN locations l ON l.photo_id=p.id "
                "WHERE l.path LIKE ?", (f"%{name}%",),
            ).fetchone()[0]

        fred, fgreen, fplain = fpid_of("red.jpg"), fpid_of("green.jpg"), fpid_of("plain.jpg")
        # Test seam: one face on red and green; plain gets two faces only to
        # prove the multi-face error, then goes seam-less for the real
        # no-faces skip below.
        os.environ["PHOTOTEXT_TEST_FACES"] = (
            f"{fred}:150,150,250,250;{fgreen}:150,150,250,250;{fplain}:0,0,10,10+50,50,20,20"
        )
        out = c39.run("people", "name", str(fred), "Ryan")
        check(
            "auto-detected a single face at 150,150,250,250" in out,
            "people name auto-adopts a lone detected face",
        )
        check("person 'Ryan' seeded" in out, "auto-boxed seed created")
        out = c39.run("people", "name", str(fplain), "Sam", expect=2)
        check("found 2 faces" in out, "multiple faces require an explicit --box")
        out = c39.run("people", "name", str(fplain), "Sam", "--box", "10,10,300,300")
        check("person 'Sam' seeded" in out, "explicit --box still works")
        os.environ["PHOTOTEXT_TEST_FACES"] = (
            f"{fred}:150,150,250,250;{fgreen}:150,150,250,250"
        )
        out = c39.run("people", "run")
        check(
            "macOS Vision face detection on" in out,
            "run reports face detection is active",
        )
        check(
            "Matching 3 photo(s) against 2 person(s)" in out,
            "every untagged photo is a candidate",
        )
        check(
            f"photo {fplain}: no faces" in out,
            "photos without faces are recorded without a model call",
        )
        check(
            "4 tag(s) added (2 below threshold)" in out,
            "seam face crops drive the mock verdicts",
        )
        check("1 marked absent" in out, "only the unseeded person is marked absent")
        check(
            con39.execute(
                "SELECT COUNT(*) FROM person_tags t JOIN people pe ON pe.id=t.person_id "
                "WHERE t.photo_id=? AND pe.name='Ryan' AND t.present=0",
                (fplain,),
            ).fetchone()[0] == 1,
            "absent row stored for the faceless photo",
        )
        check(
            con39.execute(
                "SELECT t.present FROM person_tags t JOIN people pe ON pe.id=t.person_id "
                "WHERE t.photo_id=? AND pe.name='Sam'",
                (fplain,),
            ).fetchone()[0] == 1,
            "seeded ground truth survives the no-faces skip",
        )
        out = c39.run("doctor")
        check(
            "[PASS] face detection (macOS Vision)" in out,
            "doctor reports Vision availability",
        )
        con39.close()
    finally:
        os.environ.pop("PHOTOTEXT_TEST_FACES", None)
        mock39.terminate()
        mock39.wait()

    print("\n[40] surrogates: unencodable filenames and model output")
    port40 = free_port()
    mock40 = start_mock(port40, mode_file)
    try:
        cfg40 = work / "config-surrogates.toml"
        cfg40.write_text(
            f'ollama_url = "http://127.0.0.1:{port40}"\n'
            f'model = "{MODEL}"\n'
            f'db_path = "{work}/db-surrogates.db"\n'
        )
        c40 = CLI(cfg40)
        sur_src = work / "surrogatefolder"
        sur_src.mkdir()
        make_person_image(sur_src / "good.jpg", "white")
        out = c40.run("scan", str(sur_src))
        check("unencodable-names" not in out, "clean scan reports no bad names")
        # APFS refuses to create invalid-UTF-8 filenames, so exercise the
        # skip guard with a stubbed walker (mangled names can still arrive
        # from old HFS+ libraries or external disks).
        import phototext.scanner as scanner_mod

        bad_name = sur_src / "bad\udced\udca0\udcbe.jpg"
        real_iter = scanner_mod._iter_image_files
        scanner_mod._iter_image_files = lambda root, errs: iter([bad_name])
        try:
            con40 = db_open(work / "db-surrogates.db")
            stats40 = scanner_mod.scan_source(con40, str(sur_src), 1, quiet=True)
        finally:
            scanner_mod._iter_image_files = real_iter
        check(stats40.bad_names == 1, "mangled filename is skipped and counted")
        check(
            count(con40, "SELECT COUNT(*) FROM photos") == 1,
            "bad-name file is not registered",
        )
        mode_file.write_text("surrogate")
        out = c40.run("run")
        check("Queue drained" in out, "run survives unpaired-surrogate model output")
        check(
            count(con40, "SELECT COUNT(*) FROM photos WHERE status='done'") == 1,
            "photo completes despite the bad escape",
        )
        text40 = con40.execute("SELECT text FROM photos LIMIT 1").fetchone()[0]
        check("\ufffd" in text40, "lone surrogate sanitized to U+FFFD in stored text")
        con40.close()
    finally:
        mode_file.write_text("ok")
        mock40.terminate()
        mock40.wait()

    print("\n[41] iCloud offload, library hidden sync, preview caching")
    cloud_dir = work / "clouddir"
    cloud_dir.mkdir()
    cloud_lib = cloud_dir / "Synced.photoslibrary"
    (cloud_lib / "originals" / "A").mkdir(parents=True)
    (cloud_lib / "resources" / "derivatives" / "B").mkdir(parents=True)
    make_text_image(cloud_lib / "originals/A/keep.jpg", ["KEEPER NOTE"])
    make_text_image(cloud_lib / "originals/A/vanish.jpg", ["VANISHING NOTE"])
    make_text_image(cloud_lib / "resources/derivatives/B/UUID-2_2_4096.jpeg", ["PREVIEW"])
    from pillow_heif import register_heif_opener

    register_heif_opener()
    im41 = Image.new("RGB", (640, 480), "beige")
    d41 = ImageDraw.Draw(im41)
    d41.text((40, 60), "GHOST HEIC", fill="black", font=font())
    im41.save(cloud_lib / "originals/A/ghost.heic")
    ghost_path = str(cloud_lib / "originals/A/ghost.heic")
    (cloud_dir / "ghost.heic.bak").write_bytes(
        (cloud_lib / "originals/A/ghost.heic").read_bytes()
    )
    seam = cloud_dir / "assets.json"
    vanish_path = str(cloud_lib / "originals/A/vanish.jpg")

    def seam_entries(uuid2_path, uuid2_hidden, uuid3_path):
        seam.write_text(
            json.dumps(
                [
                    ["UUID-1", str(cloud_lib / "originals/A/keep.jpg"), False],
                    ["UUID-2", uuid2_path, uuid2_hidden],
                    ["UUID-3", uuid3_path, False],
                ]
            )
        )

    seam_entries(vanish_path, True, ghost_path)
    cloud_cfg = work / "config-clouddb.toml"
    cloud_cfg.write_text(
        f'ollama_url = "http://127.0.0.1:{port}"\n'
        f'model = "{MODEL}"\n'
        f'db_path = "{cloud_dir}/catalog.db"\n'
        "face_detection = false\n"
    )
    c41 = CLI(cloud_cfg)
    con41 = db_open(cloud_dir / "catalog.db")
    con41.row_factory = None
    os.environ["PHOTOTEXT_TEST_ASSETS"] = str(seam)
    try:
        out = c41.run("scan", str(cloud_lib))
        check("new 3" in out, "seamed Photos-library scan registers 3 photos")
        check("library-hidden 1" in out, "scan summary reports library hiddens")
        check(
            count(con41, "SELECT COUNT(*) FROM photo_assets") == 3,
            "asset map records every library asset",
        )
        check(
            con41.execute(
                "SELECT COUNT(*) FROM photo_assets "
                "WHERE uuid IN ('uuid-1', 'uuid-2', 'uuid-3')"
            ).fetchone()[0]
            == 3,
            "asset map records every library asset",
        )
        check(
            con41.execute(
                "SELECT uuid FROM photo_assets WHERE uuid = 'uuid-2'"
            ).fetchone()[0]
            == "UUID-2",
            "asset uuids keep their original case",
        )
        check(
            con41.execute(
                "SELECT hidden, hidden_origin FROM photos WHERE id = 2"
            ).fetchone()
            == (1, "library"),
            "library hidden imports with origin 'library'",
        )
        # phototext's own verdicts beat the library's
        c41.run("unhide", "2")
        c41.run("scan", str(cloud_lib))  # the seam still hides UUID-2
        check(
            con41.execute("SELECT hidden, hidden_origin FROM photos WHERE id = 2").fetchone()
            == (0, "user"),
            "user unhide survives the library re-hiding",
        )
        seam_entries(vanish_path, False, ghost_path)
        c41.run("scan", str(cloud_lib))
        check(
            con41.execute("SELECT hidden FROM photos WHERE id = 2").fetchone()[0] == 0,
            "library unhide leaves the user's choice alone",
        )
        c41.run("hide", "2")
        c41.run("scan", str(cloud_lib))
        check(
            con41.execute("SELECT hidden, hidden_origin FROM photos WHERE id = 2").fetchone()
            == (1, "user"),
            "user hide wins over the library",
        )
        # insure the pixels before iCloud takes them away
        out = c41.run("cache-previews")
        check("3 photo(s) cached" in out, "cache-previews fills caches for every photo")
        check((cloud_dir / "thumbs" / "3.jpg").is_file(), "thumb cache filled")
        check((cloud_dir / "views" / "3.jpg").is_file(), "view cache filled for every type")
        # offload: iCloud evicts both originals; only UUID-2 keeps a preview
        os.remove(cloud_lib / "originals/A/vanish.jpg")
        os.remove(cloud_lib / "originals/A/ghost.heic")
        seam_entries(None, False, None)
        out = c41.run("scan", str(cloud_lib))
        check("offloaded 2" in out, "scan reports offloaded originals")
        check(
            count(con41, "SELECT COUNT(*) FROM photos") == 3,
            "offload never duplicates photos",
        )
        check(
            con41.execute("SELECT offloaded FROM photos WHERE id = 2").fetchone()[0] == 1
            and con41.execute("SELECT offloaded FROM photos WHERE id = 3").fetchone()[0] == 1,
            "offloaded flags set",
        )
        check(
            count(
                con41,
                "SELECT COUNT(*) FROM locations WHERE path LIKE '%derivatives%'",
            )
            == 1,
            "the Photos preview derivative is attached as a location",
        )
        check(
            count(con41, "SELECT COUNT(*) FROM locations WHERE photo_id = 3") == 0,
            "offloaded photo without a preview has no locations",
        )
        # the web keeps showing whatever pixels it still has
        port41 = free_port()
        serve41 = subprocess.Popen(
            c41.cmd + ["serve", "--port", str(port41)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base41 = f"http://127.0.0.1:{port41}"
        try:
            for _ in range(60):
                try:
                    requests.get(base41 + "/", timeout=1)
                    break
                except Exception:
                    time.sleep(0.1)
            r = requests.get(f"{base41}/photo/3", timeout=5)
            check(
                r.status_code == 200 and "offloaded by iCloud" in r.text,
                "detail page shows the offloaded badge",
            )
            check("reveal in Finder" not in r.text, "no reveal link for photos without files")
            check("open in Photos" in r.text, "open in Photos link present")
            r = requests.get(f"{base41}/image/3", timeout=5)
            check(
                r.status_code == 200 and r.headers["Content-Type"] == "image/jpeg",
                "image served from the cached view after offload",
            )
            r = requests.get(f"{base41}/thumb/3", timeout=5)
            check(r.status_code == 200, "thumbnail served from the cache after offload")
            r = requests.get(f"{base41}/photo/2", timeout=5)
            check("reveal in Finder" in r.text, "derivative location still reveals")
            r = requests.get(f"{base41}/open-photos/999", timeout=5, allow_redirects=False)
            check(r.status_code == 404, "open-photos 404s without an asset id")
            r = requests.get(f"{base41}/open-photos/abc", timeout=5, allow_redirects=False)
            check(r.status_code == 404, "open-photos rejects non-numeric ids")
        finally:
            serve41.terminate()
            serve41.wait()
        # downloading the original again clears the offloaded state
        (cloud_lib / "originals/A/ghost.heic").write_bytes(
            (cloud_dir / "ghost.heic.bak").read_bytes()
        )
        seam_entries(None, False, ghost_path)
        c41.run("scan", str(cloud_lib))
        check(
            con41.execute("SELECT offloaded FROM photos WHERE id = 3").fetchone()[0] == 0,
            "a downloaded original clears the offloaded flag",
        )
        check(
            count(con41, "SELECT COUNT(*) FROM locations WHERE photo_id = 3") == 1,
            "the original location is restored",
        )
        # unscan forgets the asset map with the source
        c41.run("unscan", str(cloud_lib))
        check(
            count(con41, "SELECT COUNT(*) FROM photo_assets") == 0,
            "unscan clears the asset map",
        )
        check(count(con41, "SELECT COUNT(*) FROM photos") == 0, "unscan forgets source-only photos")
    finally:
        os.environ.pop("PHOTOTEXT_TEST_ASSETS", None)
        con41.close()

    print("\n[42] iPhoto hidden import (apdb) and no-op without a hidden column")
    iph_dir = work / "iphoto-hidden"
    (iph_dir / "library.photolibrary/Originals/2013").mkdir(parents=True)
    make_text_image(iph_dir / "library.photolibrary/Originals/2013/secret.jpg", ["TOP SECRET"])
    make_text_image(iph_dir / "library.photolibrary/Originals/2013/open.jpg", ["NOTHING HERE"])
    apdb41 = iph_dir / "library.photolibrary/Database/apdb/Database"
    apdb41.parent.mkdir(parents=True)
    ah = sqlite3.connect(apdb41)
    ah.executescript(
        "CREATE TABLE RKVersion (uuid TEXT, masterId TEXT, name TEXT, "
        "flagged INTEGER, hidden INTEGER);"
        "CREATE TABLE RKMaster (uuid TEXT, imagePath TEXT);"
    )
    ah.execute("INSERT INTO RKMaster VALUES ('k1', 'Originals/2013/secret.jpg')")
    ah.execute("INSERT INTO RKMaster VALUES ('k2', 'Originals/2013/open.jpg')")
    ah.execute(
        "INSERT INTO RKVersion (uuid, masterId, name, flagged, hidden) "
        "VALUES ('W1', 'k1', 'secret', 0, 1)"
    )
    ah.execute(
        "INSERT INTO RKVersion (uuid, masterId, name, flagged, hidden) "
        "VALUES ('W2', 'k2', 'open', 0, 0)"
    )
    ah.commit()
    ah.close()
    iph_cfg = work / "config-iphoto-hidden.toml"
    iph_cfg.write_text(
        f'ollama_url = "http://127.0.0.1:{port}"\n'
        f'model = "{MODEL}"\n'
        f'db_path = "{iph_dir}/catalog.db"\n'
    )
    c42 = CLI(iph_cfg)
    out = c42.run("scan", str(iph_dir / "library.photolibrary"))
    check(
        "1 photo(s) hidden in the library" in out,
        "iPhoto scan reports the library hidden flag",
    )
    con42 = db_open(iph_dir / "catalog.db")
    con42.row_factory = None
    check(
        con42.execute(
            "SELECT COUNT(*) FROM photos WHERE hidden = 1 AND hidden_origin = 'library'"
        ).fetchone()[0]
        == 1,
        "apdb-hidden photo imports with origin 'library'",
    )
    check(
        con42.execute(
            "SELECT COUNT(*) FROM photos WHERE hidden = 1"
        ).fetchone()[0]
        == 1,
        "only the hidden version is hidden",
    )
    con42.close()
    out = cli.run("scan", str(lib))
    check(
        "library-hidden" not in out,
        "apdb without a hidden column stays a no-op",
    )

    print("\n[43] model-call wall-clock cap (trickling server)")
    port43 = free_port()
    mode43 = work / "mode43"
    mode43.write_text("trickle")
    mock43 = start_mock(port43, mode43)
    try:
        cfg43 = work / "config-trickle.toml"
        cfg43.write_text(
            f'ollama_url = "http://127.0.0.1:{port43}"\n'
            f'model = "{MODEL}"\n'
            f'db_path = "{work}/db-trickle.db"\n'
            "request_timeout_s = 2\n"
        )
        c43 = CLI(cfg43)
        tri_src = work / "tricklefolder"
        tri_src.mkdir()
        make_plain_image(tri_src / "one.jpg")
        c43.run("scan", str(tri_src))
        started43 = time.time()
        out = c43.run("run", "--skip-preflight")
        elapsed43 = time.time() - started43
        con43 = db_open(work / "db-trickle.db")
        con43.row_factory = None
        try:
            check(
                count(con43, "SELECT COUNT(*) FROM photos WHERE status='error'") == 1,
                "trickling model call errors the photo instead of hanging",
            )
            err43 = con43.execute(
                "SELECT error FROM photos WHERE status='error'"
            ).fetchone()[0]
            check(
                "exceeded the 2s timeout" in (err43 or ""),
                "timeout error names the wall-clock budget",
            )
            check(elapsed43 < 15, "the run does not wait out the trickle")
        finally:
            con43.close()
    finally:
        mock43.terminate()
        mock43.wait()

    print("\n[44] recent-first claims, reprocess selectors, derivative deferral")
    port44 = free_port()
    mock44 = start_mock(port44, mode_file)
    try:

        def fresh_cli44(name: str, extra: str = "") -> CLI:
            cfgp = work / f"config-{name}.toml"
            cfgp.write_text(
                f'ollama_url = "http://127.0.0.1:{port44}"\n'
                f'model = "{MODEL}"\n'
                f'db_path = "{work}/db-{name}.db"\n' + extra
            )
            return CLI(cfgp)

        c44 = fresh_cli44("recentfirst", "recent_first = true\n")
        rec_src = work / "recentfolder"
        rec_src.mkdir()

        def make_dated44(path: Path, taken: str, label: str) -> None:
            exif = Image.Exif()
            exif.get_ifd(0x8769)[36867] = taken
            exif[306] = taken
            im = Image.new("RGB", (640, 480), "steelblue")
            d = ImageDraw.Draw(im)
            d.text((40, 40), label, fill="white", font=font())
            im.save(path, exif=exif)

        # aaa is scanned first (lowest id); zzz is the newest — the two claim
        # orders must therefore pick different photos.
        make_dated44(rec_src / "aaa.jpg", "2011:03:15 08:00:00", "OLD ERA")
        make_dated44(rec_src / "zzz.jpg", "2023:05:01 10:00:00", "NEW ERA")
        c44.run("scan", str(rec_src))
        c44.run("run", "--skip-preflight", "--limit", "1")
        con44 = db_open(work / "db-recentfirst.db")
        con44.row_factory = None
        done_path44 = con44.execute(
            "SELECT l.path FROM photos p JOIN locations l ON l.photo_id = p.id "
            "WHERE p.status = 'done'"
        ).fetchone()[0]
        check("zzz.jpg" in done_path44, "recent_first claims the newest photo first")
        c44b = fresh_cli44("fifo44")
        c44b.run("scan", str(rec_src))
        c44b.run("run", "--skip-preflight", "--limit", "1")
        con44b = db_open(work / "db-fifo44.db")
        con44b.row_factory = None
        done_path44b = con44b.execute(
            "SELECT l.path FROM photos p JOIN locations l ON l.photo_id = p.id "
            "WHERE p.status = 'done'"
        ).fetchone()[0]
        check("aaa.jpg" in done_path44b, "fifo claims the lowest id first")
        con44.close()
        con44b.close()

        # reprocess granularity: model / date / category selectors
        c44.run("run", "--skip-preflight", "--limit", "1")  # finish the backlog
        out = c44.run("reprocess", "--done-with", MODEL)
        check("requeued 2 photo(s)" in out, "reprocess --done-with selects by model")
        c44.run("run", "--skip-preflight")
        out = c44.run("reprocess", "--done-before", "2030-01-01")
        check("requeued 2 photo(s)" in out, "reprocess --done-before selects by date")
        c44.run("run", "--skip-preflight")
        out = c44.run("reprocess", "--category", "document")
        check("requeued 2 photo(s)" in out, "reprocess --category selects by category")
        out = c44.run("reprocess", "--done-with", "nope:never")
        check(
            "no matching photos to requeue" in out,
            "reprocess --done-with misses cleanly",
        )

        # process_derivatives = false keeps preview extractions deferred
        deriv44_dir = work / "deriv44"
        deriv44_dir.mkdir()
        deriv44_lib = deriv44_dir / "Synced.photoslibrary"
        (deriv44_lib / "originals" / "A").mkdir(parents=True)
        seam44 = deriv44_dir / "assets.json"
        seam44.write_text(json.dumps([["D-1", None, False]]))
        cfg44d = work / "config-deriv44.toml"
        cfg44d.write_text(
            f'ollama_url = "http://127.0.0.1:{port44}"\n'
            f'model = "{MODEL}"\n'
            f'db_path = "{deriv44_dir}/catalog.db"\n'
            "process_derivatives = false\n"
        )
        c44d = CLI(cfg44d)
        os.environ["PHOTOTEXT_TEST_ASSETS"] = str(seam44)
        try:
            # a preview derivative exists, but the config defers it
            (deriv44_lib / "resources" / "derivatives" / "B").mkdir(parents=True)
            make_text_image(
                deriv44_lib / "resources/derivatives/B/D-1_2_4096.jpeg",
                ["DERIV PREVIEW"],
            )
            out = c44d.run("scan", str(deriv44_lib))
            check("previews 1" in out, "scan still reports the preview derivative")
            con44d = db_open(deriv44_dir / "catalog.db")
            con44d.row_factory = None
            check(
                con44d.execute(
                    "SELECT status FROM photos WHERE sha256 = 'deferred:D-1'"
                ).fetchone()[0]
                == "deferred",
                "process_derivatives=false keeps previews deferred",
            )
            con44d.close()
        finally:
            os.environ.pop("PHOTOTEXT_TEST_ASSETS", None)

        # worker exit codes propagate: a dead backend must fail the run
        dead_port = free_port()
        cfg44e = work / "config-dead44.toml"
        cfg44e.write_text(
            f'ollama_url = "http://127.0.0.1:{dead_port}"\n'
            f'model = "{MODEL}"\n'
            f'db_path = "{work}/db-dead44.db"\n'
            "transport_retries = 1\ntransport_backoff_s = 1\n"
        )
        c44e = CLI(cfg44e)
        dead_src = work / "deadfolder44"
        dead_src.mkdir()
        make_text_image(dead_src / "one.jpg", ["WILL NOT RUN"])
        c44e.run("scan", str(dead_src))
        out = c44e.run("run", "--skip-preflight", "--workers", "2", expect=1)
        check("aborted" in out, "unreachable backend aborts the workers")
        con44e = db_open(work / "db-dead44.db")
        con44e.row_factory = None
        check(
            con44e.execute(
                "SELECT COUNT(*) FROM photos WHERE status = 'queued'"
            ).fetchone()[0]
            == 1,
            "aborted workers leave the photo queued",
        )
        con44e.close()
    finally:
        mock44.terminate()
        mock44.wait()

    print("\n[45] macOS Vision OCR (vision_text, FTS, backfill, gate skip)")
    port45 = free_port()
    mock45 = start_mock(port45, mode_file)
    try:

        def fresh_cli45(name: str, extra: str = "") -> CLI:
            cfgp = work / f"config-{name}.toml"
            cfgp.write_text(
                f'ollama_url = "http://127.0.0.1:{port45}"\n'
                f'model = "{MODEL}"\n'
                f'db_path = "{work}/db-{name}.db"\n' + extra
            )
            return CLI(cfgp)

        # a) OCR seam: scan two photos, check vision-ocr stats and FTS search
        ocr_folder = work / "ocrfolder"
        ocr_folder.mkdir()
        make_text_image(ocr_folder / "alpha.jpg", ["alpha doc"])
        make_text_image(ocr_folder / "beta.jpg", ["beta doc"])
        c45 = fresh_cli45("ocr")
        os.environ["PHOTOTEXT_TEST_OCR"] = "*:RECEIPT SEAM TEXT"
        try:
            out = c45.run("scan", str(ocr_folder))
            check("vision-ocr 2" in out, "scan summary reports vision-ocr count")
            con45 = db_open(work / "db-ocr.db")
            check(
                count(con45, "SELECT COUNT(*) FROM photos WHERE vision_text IS NOT NULL") == 2,
                "both photos get vision_text",
            )
            check(
                count(con45, "SELECT COUNT(*) FROM photos_fts WHERE photos_fts MATCH 'RECEIPT'") == 2,
                "FTS indexes vision_text (searchable before run)",
            )
            con45.close()
            out = c45.run("search", "RECEIPT")
            check("2 match(es)" in out, "search finds OCR text before run")
        finally:
            os.environ.pop("PHOTOTEXT_TEST_OCR", None)

        # b) backfill-ocr on an image Vision cannot read: graceful no-op
        ocr2_folder = work / "ocr2folder"
        ocr2_folder.mkdir()
        make_plain_image(ocr2_folder / "a.jpg")
        c45b = fresh_cli45("ocr2")
        c45b.run("scan", str(ocr2_folder))
        out = c45b.run("backfill-ocr")
        check("0 photo(s)" in out or "without readable files" in out,
              "backfill-ocr on synthetic images handles empty gracefully")
        out2 = c45b.run("backfill-ocr")
        check(
            "1 photo(s) without OCR text" in out2,
            "second backfill-ocr retries photos Vision cannot read",
        )

        # c) gate skip: photo with vision_text skips the gate and goes full pass
        mode_file.write_text("gatenotext")
        ocr3_folder = work / "ocr3folder"
        ocr3_folder.mkdir()
        # plain images: real Vision must find nothing, so only the seam
        # (photo 1) carries vision_text; the inverted copy keeps dedup away
        make_plain_image(ocr3_folder / "one.jpg")
        ImageOps.invert(Image.open(ocr3_folder / "one.jpg")).save(
            ocr3_folder / "two.jpg"
        )
        c45c = fresh_cli45("ocr3", 'two_tier = true\nprefilter_model = "gemma3:mock"\n')
        # Set seam so only photo id 1 gets vision_text (photo 2 gets none)
        os.environ["PHOTOTEXT_TEST_OCR"] = "1:OCR TEXT HERE"
        try:
            c45c.run("scan", str(ocr3_folder))
            con45c = db_open(work / "db-ocr3.db")
            # Verify photo 1 got vision_text and photo 2 didn't
            con45c.row_factory = None
            check(
                con45c.execute(
                    "SELECT vision_text FROM photos WHERE id = 1"
                ).fetchone()[0]
                == "OCR TEXT HERE",
                "photo 1 gets seam vision_text",
            )
            check(
                con45c.execute(
                    "SELECT vision_text FROM photos WHERE id = 2"
                ).fetchone()[0]
                is None,
                "photo 2 has no vision_text",
            )
            con45c.close()
            out = c45c.run("run", "--skip-preflight")
            # photo 1: has vision_text -> skips gate -> full model (gated=0)
            # photo 2: no vision_text -> gate (gatenotext mode: has_text=false) -> gated=1
            check("(gated)" in out, "photo 2 finishes at the gate")
            con45c2 = db_open(work / "db-ocr3.db")
            con45c2.row_factory = None
            check(
                con45c2.execute(
                    "SELECT gated, model FROM photos WHERE id = 1"
                ).fetchone() == (0, MODEL),
                "photo 1: gate skipped (vision_text present), full model used",
            )
            check(
                con45c2.execute(
                    "SELECT gated, model FROM photos WHERE id = 2"
                ).fetchone() == (1, "gemma3:mock"),
                "photo 2: no vision_text, gate finishes it",
            )
            con45c2.close()
        finally:
            os.environ.pop("PHOTOTEXT_TEST_OCR", None)
    finally:
        mock45.terminate()
        mock45.wait()

    print("\n[46] text embeddings + semantic search")
    port46 = free_port()
    mock46 = start_mock(port46, mode_file)
    try:
        def fresh_cli46(name: str, extra: str = "") -> CLI:
            cfgp = work / f"config-{name}.toml"
            cfgp.write_text(
                f'ollama_url = "http://127.0.0.1:{port46}"\n'
                f'model = "{MODEL}"\n'
                f'db_path = "{work}/db-{name}.db"\n'
                f'embed_model = "nomic-mock"\n'
                "face_detection = false\n"
                "request_timeout_s = 20\n"
                "transport_retries = 2\ntransport_backoff_s = 1\n" + extra
            )
            return CLI(cfgp)

        c46 = fresh_cli46("embed")
        embed_src = work / "embedsrc"
        embed_src.mkdir()
        make_text_image(embed_src / "electric.jpg", ["electric utility bill"])
        make_text_image(embed_src / "invoice.jpg", ["invoice statement payment"])
        make_text_image(embed_src / "beach.jpg", ["beach waves sunshine"])
        c46.run("scan", str(embed_src))
        c46.run("run", "--skip-preflight")
        con46 = db_open(work / "db-embed.db")
        con46.execute("UPDATE photos SET text='electric utility bill' WHERE id=1")
        con46.execute("UPDATE photos SET text='invoice statement payment' WHERE id=2")
        con46.execute("UPDATE photos SET text='beach waves sunshine' WHERE id=3")
        con46.commit()
        con46.close()

        out = c46.run("embed")
        check("embedded 3 photo(s) with nomic-mock" in out, "embed processes all done photos")
        con46 = db_open(work / "db-embed.db")
        con46.row_factory = None
        check(
            con46.execute("SELECT COUNT(*) FROM photo_embeddings").fetchone()[0] == 3,
            "photo_embeddings has 3 rows",
        )
        row46 = con46.execute("SELECT * FROM photo_embeddings LIMIT 1").fetchone()
        check(row46[2] == 8 and len(row46[3]) == 32, "dims=8 and vector is 32 bytes (8 x float32)")
        con46.close()

        # semantic: "receipt" maps to "bill" in synonyms — matches bill photos
        out = c46.run("search", "--semantic", "receipt")
        check("match(es)" in out, "semantic search finds results")
        check("score" in out, "semantic results show scores")
        # electric utility bill should be ranked high (utility->bill shares dims)
        check("[1]" in out or "[2]" in out, "at least one bill photo appears")
        # beach should be last or absent
        lines46 = [l for l in out.splitlines() if l.strip().startswith("[")]
        scores46 = []
        for l in lines46:
            if "score" in l:
                scores46.append(float(l.split("score")[-1].strip()))
        if len(scores46) >= 3:
            check(scores46[0] >= scores46[-1], "beach photo ranks lowest")
        elif len(scores46) >= 2:
            check("[3]" not in lines46[0], "beach is not first")

        # FTS search for "receipt" should find NOTHING (zero keyword overlap)
        out = c46.run("search", "receipt")
        check("no matches" in out, "plain FTS receipt finds nothing")

        # similar command
        # find beach photo id
        beach_id = None
        invoice_id = None
        con46 = db_open(work / "db-embed.db")
        for r in con46.execute("SELECT p.id, p.text FROM photos p").fetchall():
            if "beach" in (r[1] or ""):
                beach_id = r[0]
            if "invoice" in (r[1] or ""):
                invoice_id = r[0]
        con46.close()
        if beach_id:
            out = c46.run("similar", str(beach_id))
            check("score" in out, "similar command shows scores")
            lines_sim = [l for l in out.splitlines() if l.strip().startswith("[")]
            check(f"[{beach_id}]" not in " ".join(lines_sim), "beach not first for similar beach")
        if invoice_id:
            out = c46.run("similar", str(invoice_id))
            lines_sim = [l for l in out.splitlines() if l.strip().startswith("[")]
            check(f"[{beach_id}]" in " ".join(lines_sim), "beach appears for similar invoice")

        # embed again -> missing only
        out = c46.run("embed")
        check("0 photo(s)" in out, "embed reports 0 photos (all have embeddings)")

        # --all re-embeds everyone
        out = c46.run("embed", "--all")
        check("embedded 3 photo(s)" in out, "embed --all re-embeds all 3")

        # no embed_model: semantic search errors
        cfg46n = work / "config-noemb46.toml"
        cfg46n.write_text(
            f'ollama_url = "http://127.0.0.1:{port46}"\n'
            f'model = "{MODEL}"\n'
            f'db_path = "{work}/db-noemb46.db"\n'
            "face_detection = false\n"
        )
        c46n = CLI(cfg46n)
        out = c46n.run("search", "--semantic", "x", expect=2)
        check("set embed_model" in out, "semantic search without embed_model errors")

        out = c46.run("similar", "9999", expect=2)
        check("no photo" in out, "similar with bad id errors")
    finally:
        mock46.terminate()
        mock46.wait()

    print("\n[47] sidebar scales to many people and categories")
    side47_dir = work / "side47"
    side47_dir.mkdir()
    cfg47 = work / "config-side47.toml"
    cfg47.write_text(
        f'ollama_url = "http://127.0.0.1:{port}"\n'
        f'model = "{MODEL}"\n'
        f'db_path = "{side47_dir}/catalog.db"\n'
        "face_detection = false\n"
    )
    c47 = CLI(cfg47)
    side47_src = side47_dir / "s"
    side47_src.mkdir()
    make_text_image(side47_src / "one.jpg", ["sidebar scale"])
    make_text_image(side47_src / "two.jpg", ["sidebar scale 2"])
    c47.run("scan", str(side47_src))
    con47 = db_open(side47_dir / "catalog.db")
    con47.row_factory = None
    # ten people and a decade of years: the groups need the scalable chrome
    for i in range(10):
        con47.execute("INSERT INTO people (name) VALUES (?)", (f"Scale Person {i:02d}",))
        con47.execute(
            "INSERT INTO person_tags (photo_id, person_id) VALUES (1, ?)", (i + 1,)
        )
    con47.execute(
        "UPDATE photos SET category = 'scalecat', "
        "date_taken = '2023-01-01T00:00:00' WHERE id = 1"
    )
    con47.execute(
        "UPDATE photos SET date_taken = '2013-01-01T00:00:00' WHERE id = 2"
    )
    con47.commit()
    con47.close()
    port47 = free_port()
    serve47 = subprocess.Popen(
        c47.cmd + ["serve", "--port", str(port47)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base47 = f"http://127.0.0.1:{port47}"
    try:
        for _ in range(60):
            try:
                requests.get(base47 + "/", timeout=1)
                break
            except Exception:
                time.sleep(0.1)
        r = requests.get(base47 + "/", timeout=5)
        check(
            "<details class='fgroup' open>" in r.text,
            "filter groups render as collapsible details",
        )
        check("class='ffilter'" in r.text, "many people get a sidebar filter box")
        check("<script>" in r.text, "the filter script loads with the filter box")
        check(
            "filter people" in r.text, "people filter box carries its placeholder"
        )
        check(
            "Scale Person 09" in r.text and "hidden (0)" in r.text,
            "all links still render inside the group",
        )
        check(
            "side-top" in r.text and "side-scroll" in r.text,
            "global nav is pinned above the scrolling filters",
        )
        # only two categories: no filter box needed there
        check(
            "filter categories" not in r.text,
            "small category lists stay filterless",
        )
    finally:
        serve47.terminate()
        serve47.wait()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
