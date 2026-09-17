#!/usr/bin/env bash
#
# phototext installer.
#
# Creates a self-contained install of this repo:
#
#   <prefix>/venv/              python env with a snapshot of the current code
#   <prefix>/var/config.toml    main config (db_path -> <prefix>/var/catalog.db)
#   <prefix>/var/catalog.db     the catalog
#   <prefix>/var/log/           log directory (reserved)
#   <bin-dir>/phototext         wrapper that runs the installed CLI with the var config
#   <bin-dir>/phototext-dev     wrapper that runs the WORKSPACE copy via the repo's
#                               dev venv (.venv) and ~/.phototext state
#
# The installed command runs the code snapshot taken at install time; re-run
# install.sh to upgrade it. Workspace/dev usage goes through phototext-dev,
# which runs the editable install in the repo's .venv — the two never share
# config or catalog.
#
# The prefix defaults to /opt/phototext when possible (running as root, or
# /opt is writable), otherwise ~/.local/opt/phototext. The bin dir defaults to
# ~/.local/bin (or /usr/local/bin when running as root); any --bin-dir works,
# using sudo if the directory is not writable.
#
# Wrappers are created for every entry point declared in pyproject.toml, so a
# future phototext-server is picked up automatically. All phototext entry
# points must accept the standard global --config/--db flags; the wrapper for
# each one passes --config <prefix>/var/config.toml.
#
# Usage:
#   ./install.sh                                   install (auto prefix)
#   ./install.sh --prefix /opt/phototext           install to a specific prefix
#   ./install.sh --bin-dir /usr/local/bin          put wrappers elsewhere
#   ./install.sh --uninstall [--purge]            remove venv + wrappers (--purge also
#                                                 removes <prefix>/var, i.e. the catalog)
#   ./install.sh --test                           also run the e2e suite with the
#                                                 installed venv
#   Re-running install.sh upgrades the code and migrates the catalog schema
#   (a pre-migration backup is kept in <prefix>/var/backups); var/ is kept.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PREFIX=""
BIN_DIR=""
UNINSTALL=0
PURGE=0
RUN_TEST=0
ASSUME_YES=0

usage() {
  sed -n '3,39p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)
      [ $# -ge 2 ] || { echo "--prefix requires a value" >&2; exit 2; }
      PREFIX="$2"; shift 2 ;;
    --bin-dir)
      [ $# -ge 2 ] || { echo "--bin-dir requires a value" >&2; exit 2; }
      BIN_DIR="$2"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    --purge) PURGE=1; shift ;;
    --test) RUN_TEST=1; shift ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }
info() { echo "==> $*"; }

[ -f "$SCRIPT_DIR/pyproject.toml" ] || die "pyproject.toml not found in $SCRIPT_DIR; run from the phototext repo"
[ -d "$SCRIPT_DIR/src/phototext" ] || die "src/phototext not found in $SCRIPT_DIR"

if [ -z "$PREFIX" ]; then
  if [ "$(id -u)" -eq 0 ]; then
    PREFIX="/opt/phototext"
  elif [ -d /opt/phototext ] && [ -w /opt/phototext ]; then
    PREFIX="/opt/phototext"
  elif [ -e /opt/phototext ]; then
    die "/opt/phototext exists but is not writable; re-run with sudo or pass --prefix"
  else
    PREFIX="$HOME/.local/opt/phototext"
    echo "note: /opt/phototext needs sudo; installing user-wide at $PREFIX"
    echo "note: for a system-wide install: sudo $0 --prefix /opt/phototext"
  fi
fi
if [ -z "$BIN_DIR" ]; then
  if [ "$(id -u)" -eq 0 ]; then BIN_DIR="/usr/local/bin"; else BIN_DIR="$HOME/.local/bin"; fi
fi

VENV="$PREFIX/venv"
VAR="$PREFIX/var"
MARKER="phototext-installer:prefix=$PREFIX"

remove_file() {
  local f="$1"
  if [ -w "$(dirname "$f")" ]; then
    rm -f "$f"
  elif command -v sudo >/dev/null 2>&1; then
    sudo rm -f "$f"
  else
    die "cannot remove $f (no write permission and no sudo)"
  fi
}

installed_wrappers() {
  [ -d "$BIN_DIR" ] || return 0
  for f in "$BIN_DIR"/*; do
    [ -f "$f" ] || continue
    grep -Fq "$MARKER" "$f" 2>/dev/null && printf '%s\n' "$f"
  done
}

if [ "$UNINSTALL" -eq 1 ]; then
  for f in $(installed_wrappers); do
    info "removing wrapper $f"
    remove_file "$f"
  done
  if [ -d "$VENV" ]; then
    info "removing $VENV"
    rm -rf "$VENV"
  fi
  if [ -d "$VAR" ]; then
    if [ "$PURGE" -eq 1 ]; then
      info "purging $VAR (catalog deleted)"
      rm -rf "$VAR"
    else
      echo "kept $VAR (catalog and config); pass --purge to delete it too"
    fi
  fi
  rmdir "$PREFIX" 2>/dev/null || true
  rmdir "$BIN_DIR" 2>/dev/null || true
  info "uninstalled"
  exit 0
fi

info "installing phototext"
info "prefix:   $PREFIX (snapshot from $SCRIPT_DIR)"
info "bin dir:  $BIN_DIR"
mkdir -p "$PREFIX"

if [ ! -x "$VENV/bin/python" ]; then
  info "creating venv"
  if command -v uv >/dev/null 2>&1; then
    uv venv "$VENV"
  else
    python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
      || die "python3 >= 3.11 required (or install uv: https://docs.astral.sh/uv/)"
    python3 -m venv "$VENV"
  fi
fi

info "installing package (snapshot of current code)"
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$VENV/bin/python" "$SCRIPT_DIR"
else
  "$VENV/bin/pip" install "$SCRIPT_DIR"
fi

mkdir -p "$VAR/log"
touch "$VAR/.metadata_never_index" "$VAR/log/.metadata_never_index"
info "spotlight: $VAR excluded from indexing"
if [ -d "$HOME/.ollama" ]; then
  touch "$HOME/.ollama/.metadata_never_index"
  info "spotlight: ~/.ollama excluded from indexing (model blobs)"
fi

if [ ! -f "$VAR/config.toml" ]; then
  info "writing $VAR/config.toml"
  if "$VENV/bin/python" - "$VAR" <<'PY'
import re
import sys
from pathlib import Path

var = Path(sys.argv[1])
legacy = Path.home() / ".phototext" / "config.toml"
if legacy.exists():
    text = legacy.read_text()
    print("==> migrating config from ~/.phototext/config.toml (original kept)")
else:
    from phototext.config import EXAMPLE_CONFIG
    text = EXAMPLE_CONFIG
text = re.sub(r"(?m)^db_path\s*=\s*.*$", f'db_path = "{var}/catalog.db"', text)
(var / "config.toml").write_text(text)
PY
  then :; else
    printf 'db_path = "%s"\n' "$VAR/catalog.db" > "$VAR/config.toml"
  fi
  grep -Fq "$VAR/catalog.db" "$VAR/config.toml" \
    || printf 'db_path = "%s"\n' "$VAR/catalog.db" > "$VAR/config.toml"
fi

if [ ! -f "$VAR/catalog.db" ] && [ -f "$HOME/.phototext/catalog.db" ]; then
  info "copying existing catalog from ~/.phototext (original kept)"
  cp "$HOME/.phototext/catalog.db" "$VAR/catalog.db"
  [ -f "$HOME/.phototext/catalog.db-wal" ] \
    && cp "$HOME/.phototext/catalog.db-wal" "$VAR/catalog.db-wal" || true
fi

if [ -f "$VAR/catalog.db" ]; then
  info "checking catalog schema"
  if ! "$VENV/bin/phototext" --config "$VAR/config.toml" migrate; then
    die "catalog migration failed; see $VAR/backups for the pre-migration backup"
  fi
fi

ENTRY_POINTS="$("$VENV/bin/python" - "$SCRIPT_DIR/pyproject.toml" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as f:
    scripts = tomllib.load(f)["project"].get("scripts", {})
print(" ".join(scripts))
PY
)" || ENTRY_POINTS="phototext"
[ -n "$ENTRY_POINTS" ] || ENTRY_POINTS="phototext"

write_wrapper() {
  local name="$1" wrapper="$BIN_DIR/$1" target="$VENV/bin/$1"
  [ -x "$target" ] || die "entry point $name not found in $VENV/bin"
  if [ -e "$wrapper" ] && ! grep -Fq "$MARKER" "$wrapper" 2>/dev/null; then
    die "refusing to overwrite $wrapper (not installed by phototext); remove it or use --bin-dir"
  fi
  local tmp
  tmp="$(mktemp)"
  {
    printf '#!/bin/bash\n'
    printf '# %s\n' "$MARKER"
    printf '# installed by the phototext installer; re-run install.sh to refresh\n'
    printf 'exec %q --config %q "$@"\n' "$target" "$VAR/config.toml"
  } > "$tmp"
  chmod 0755 "$tmp"
  if [ -w "$BIN_DIR" ]; then
    mv "$tmp" "$wrapper"
  elif command -v sudo >/dev/null 2>&1; then
    sudo install -m 0755 "$tmp" "$wrapper"
    rm -f "$tmp"
  else
    rm -f "$tmp"
    die "cannot write to $BIN_DIR (no permission and no sudo); try --bin-dir"
  fi
  info "wrapper: $wrapper -> $target"
}

mkdir -p "$BIN_DIR" 2>/dev/null || true
for name in $ENTRY_POINTS; do
  write_wrapper "$name"
done

dev_tmp="$(mktemp)"
{
  printf '#!/bin/bash\n'
  printf '# %s\n' "$MARKER"
  printf '# dev wrapper: runs the WORKSPACE copy via the repo dev venv, not the install\n'
  printf 'ROOT=%q\n' "$SCRIPT_DIR"
  printf 'if [ ! -x "$ROOT/.venv/bin/phototext" ]; then\n'
  printf '  echo "phototext-dev: $ROOT/.venv/bin/phototext is missing." >&2\n'
  printf '  echo "create it with:  cd \\"$ROOT\\" && uv venv .venv && uv pip install -e ." >&2\n'
  printf '  exit 1\n'
  printf 'fi\n'
  printf 'exec "$ROOT/.venv/bin/phototext" "$@"\n'
} > "$dev_tmp"
chmod 0755 "$dev_tmp"
if [ -w "$BIN_DIR" ]; then
  mv "$dev_tmp" "$BIN_DIR/phototext-dev"
elif command -v sudo >/dev/null 2>&1; then
  sudo install -m 0755 "$dev_tmp" "$BIN_DIR/phototext-dev"
  rm -f "$dev_tmp"
else
  rm -f "$dev_tmp"
  die "cannot write to $BIN_DIR (no permission and no sudo); try --bin-dir"
fi
info "wrapper: $BIN_DIR/phototext-dev -> $SCRIPT_DIR/.venv (workspace code)"

if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ]; then
  chown -R "$SUDO_USER:$(id -gn "$SUDO_USER")" "$PREFIX"
  info "chowned $PREFIX to $SUDO_USER"
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo "note: $BIN_DIR is not on your PATH"
    add_line="export PATH=\"$BIN_DIR:\$PATH\""
    if [ "${ZSH_VERSION:-}" != "" ] || [ "${SHELL:-}" = */zsh ]; then
      rc="$HOME/.zshrc"
    else
      rc="$HOME/.profile"
    fi
    reply=""
    if [ "$ASSUME_YES" -eq 1 ]; then
      reply="y"
    elif [ -t 0 ]; then
      printf 'add "%s" to %s? [y/N] ' "$add_line" "$rc"
      read -r reply || reply=""
    fi
    if [ "$reply" = "y" ] || [ "$reply" = "Y" ]; then
      printf '\n# added by the phototext installer\n%s\n' "$add_line" >> "$rc"
      info "updated $rc (start a new shell or run: $add_line)"
    else
      echo "to use phototext now, run: $add_line"
    fi
    ;;
esac

info "smoke test"
"$BIN_DIR/phototext" --help > /dev/null
"$BIN_DIR/phototext" status > /dev/null
echo "ok: $BIN_DIR/phototext runs and uses $VAR/catalog.db"

if [ "$RUN_TEST" -eq 1 ]; then
  info "running e2e suite with the installed venv"
  "$VENV/bin/python" "$SCRIPT_DIR/tests/e2e.py"
fi

info "done"
echo
echo "  command:   $BIN_DIR/phototext        (installed snapshot, $VAR/catalog.db)"
echo "  dev cmd:   $BIN_DIR/phototext-dev    (workspace code, ~/.phototext state)"
echo "  config:    $VAR/config.toml"
echo "  next:      $BIN_DIR/phototext doctor"
echo "  upgrade:   $0    (re-run to refresh the snapshot; var/ is kept)"
echo "  uninstall: $0 --uninstall [--purge]"
