"""Theme system for phototext web UI: tokens, CSS, and JS snippets.

Provides a 3-theme design system (icloud / machine / samaritan) and the
full compiled CSS string for the web UI.  Tokens carry every color,
radius, font, and effect used by the CSS so each theme is a single
variable substitution.  The compiled CSS is importable with zero side
effects (no file writes, no network, no subprocess — just a string).

The original hardcoded _CSS from webui.py is reproduced exactly, with
all color/radius/font values replaced by `var(--pt-<token>)` references.
New Person-of-Interest design elements (scanlines, corner brackets,
reticle, etc.) are added on top.  Machine and Samaritan share mono
captions, uppercase chrome, and theme-specific glow/frame rules through
a block of selectors scoped under `html[data-theme='machine']` and
`html[data-theme='samaritan']`.

Public surface:
  TOKENS          – dict[str, dict[str, str]]  (icloud, machine, samaritan)
  THEMES          – tuple[str, ...]            (\"icloud\", \"machine\", \"samaritan\")
  DEFAULT_THEME   – str                        \"icloud\"
  THEME_CSS       – str                        compiled CSS for <style> injection
  restore_js      – callable: restore_js(default_theme: str) -> str
  TOGGLE_JS       – str                        theme-switch click handler
"""

from __future__ import annotations


TOKENS: dict[str, dict[str, str]] = {
    "icloud": {
        "bg": "#14161a",
        "surface-1": "#1c2027",
        "surface-2": "#242a33",
        "line": "#2a2f39",
        "line-faint": "#2a2f39",
        "ink": "#e6e6e6",
        "ink-dim": "#9aa4b2",
        "ink-faint": "#6b7380",
        "accent": "#2a5db0",
        "accent-ink": "#ffffff",
        "link": "#8ab4f8",
        "brand": "#8ab4f8",
        "ok-bg": "#1e3a24",
        "ok-ink": "#a8e2b8",
        "err-bg": "#5c1f1f",
        "err-ink": "#ffb4b4",
        "review-bg": "#4a3a14",
        "review-ink": "#ffd27d",
        "selection-bg": "rgba(42,93,176,.5)",
        "selection-ink": "#ffffff",
        "image-bg": "#0d0e11",
        "nav-ink": "#c7cdd6",
        "radius-sm": "6px",
        "radius-pill": "12px",
        "chip-bg": "rgba(13,14,17,.85)",
        "selbox-tint": "rgba(138,180,248,.18)",
        "frame-edge": "transparent",
        "frame-glow": "none",
        "face-glow": "none",
        "rec-glow": "none",
        "scanline-opacity": "0",
        "reticle": "none",
        "radius": "8px",
        "font-ui": '-apple-system, "Segoe UI", sans-serif',
        "font-mono": 'ui-monospace, "SF Mono", Menlo, monospace',
    },
    "machine": {
        "bg": "#000000",
        "surface-1": "rgba(255,255,255,.04)",
        "surface-2": "rgba(255,255,255,.07)",
        "line": "#aaa3a3",
        "line-faint": "rgba(170,163,163,.35)",
        "ink": "#ffffff",
        "ink-dim": "rgba(255,255,255,.6)",
        "ink-faint": "rgba(255,255,255,.35)",
        "accent": "#ff0000",
        "accent-ink": "#ffffff",
        "link": "#ffffff",
        "brand": "#ffffff",
        "ok-bg": "rgba(71,255,86,.15)",
        "ok-ink": "rgba(71,255,86,.85)",
        "err-bg": "rgba(255,0,0,.15)",
        "err-ink": "#ff0000",
        "review-bg": "rgba(255,196,0,.12)",
        "review-ink": "#ffc400",
        "selection-bg": "rgba(134,0,0,.6)",
        "selection-ink": "rgba(255,0,0,.95)",
        "image-bg": "#000000",
        "nav-ink": "rgba(255,255,255,.78)",
        "radius-sm": "0px",
        "radius-pill": "0px",
        "chip-bg": "rgba(0,0,0,.85)",
        "selbox-tint": "rgba(255,255,255,.15)",
        "frame-edge": "transparent",
        "frame-glow": "drop-shadow(0 0 6px var(--pt-ink))",
        "face-glow": "0 0 6px rgba(255,255,255,.7)",
        "rec-glow": "0 0 12px var(--pt-accent)",
        "scanline-opacity": "1",
        "reticle": "none",
        "radius": "0px",
        "font-ui": "'Barlow Semi Condensed', -apple-system, \"Segoe UI\", sans-serif",
        "font-mono": "'JetBrains Mono', ui-monospace, \"SF Mono\", Menlo, monospace",
    },
    "samaritan": {
        "bg": "#ffffff",
        "surface-1": "rgba(0,0,0,.04)",
        "surface-2": "rgba(0,0,0,.06)",
        "line": "rgba(0,0,0,.45)",
        "line-faint": "rgba(0,0,0,.18)",
        "ink": "#000000",
        "ink-dim": "rgba(0,0,0,.55)",
        "ink-faint": "rgba(0,0,0,.35)",
        "accent": "#e8000d",
        "accent-ink": "#ffffff",
        "link": "#000000",
        "brand": "#000000",
        "ok-bg": "transparent",
        "ok-ink": "#000000",
        "err-bg": "transparent",
        "err-ink": "#e8000d",
        "review-bg": "transparent",
        "review-ink": "#e8000d",
        "selection-bg": "#e8000d",
        "selection-ink": "#ffffff",
        "image-bg": "#f4f4f4",
        "nav-ink": "rgba(0,0,0,.78)",
        "radius-sm": "0px",
        "radius-pill": "0px",
        "chip-bg": "rgba(255,255,255,.85)",
        "selbox-tint": "rgba(232,0,13,.12)",
        "frame-edge": "var(--pt-line)",
        "frame-glow": "none",
        "face-glow": "none",
        "rec-glow": "none",
        "scanline-opacity": "0",
        "reticle": "block",
        "radius": "0px",
        "font-ui": "'Barlow Semi Condensed', -apple-system, \"Segoe UI\", sans-serif",
        "font-mono": "'JetBrains Mono', ui-monospace, \"SF Mono\", Menlo, monospace",
    },
}

THEMES = ("icloud", "machine", "samaritan")
DEFAULT_THEME = "icloud"


def _theme_block(selector: str, color_scheme: str, tokens: dict[str, str]) -> str:
    """Return a CSS rule block that sets color-scheme and custom properties."""
    lines: list[str] = [selector + " {", f"  color-scheme: {color_scheme};"]
    lines.extend(f"  --pt-{name}: {value};" for name, value in tokens.items())
    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Build THEME_CSS — assembled once at import time so the result is a plain str.
# ---------------------------------------------------------------------------

_FONTS = """\
@font-face {
  font-family: 'Barlow Semi Condensed';
  src: url('/fonts/barlow-semi-condensed-400.woff2') format('woff2');
  font-weight: 400;
  font-style: normal;
  font-display: swap;
}
@font-face {
  font-family: 'Barlow Semi Condensed';
  src: url('/fonts/barlow-semi-condensed-600.woff2') format('woff2');
  font-weight: 600;
  font-style: normal;
  font-display: swap;
}
@font-face {
  font-family: 'Barlow Semi Condensed';
  src: url('/fonts/barlow-semi-condensed-700.woff2') format('woff2');
  font-weight: 700;
  font-style: normal;
  font-display: swap;
}
@font-face {
  font-family: 'JetBrains Mono';
  src: url('/fonts/jetbrains-mono-var.woff2') format('woff2-variations');
  font-weight: 100 800;
  font-style: normal;
  font-display: swap;
}"""

_SHARED_CSS = """\
* { box-sizing: border-box; }

body {
  margin: 0;
  font: 15px/1.5 var(--pt-font-ui);
  background: var(--pt-bg);
  color: var(--pt-ink);
}

body::after {
  content: '';
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 9999;
  opacity: var(--pt-scanline-opacity);
  background: repeating-linear-gradient(
    0deg,
    rgba(255,255,255,.025) 0 1px,
    transparent 1px 3px
  );
}

::selection {
  color: var(--pt-selection-ink);
  background: var(--pt-selection-bg);
}

.shell { display: flex; min-height: 100vh; }

.side {
  width: 236px;
  flex-shrink: 0;
  background: var(--pt-surface-1);
  border-right: 1px solid var(--pt-line);
  padding: 16px 12px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  position: sticky;
  top: 0;
  height: 100vh;
  overflow-y: auto;
}

.side .brand {
  font-size: 17px;
  font-weight: 600;
  color: var(--pt-brand);
  padding: 0 4px;
}

.side form.search { display: flex; gap: 6px; }

.side form.search input[type=text] { flex: 1; min-width: 0; }

.navgroup {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: .06em;
  color: var(--pt-ink-faint);
  padding: 0 6px;
  margin-top: 8px;
}

.navlist { display: flex; flex-direction: column; gap: 1px; }

.navlist a {
  color: var(--pt-nav-ink);
  text-decoration: none;
  padding: 5px 8px;
  border-radius: var(--pt-radius-sm);
  font-size: 13.5px;
}

.navlist a:hover { background: var(--pt-surface-2); }

.navlist a.on { background: var(--pt-accent); color: var(--pt-accent-ink); }

.sidefoot {
  margin-top: auto;
  font-size: 11.5px;
  color: var(--pt-ink-faint);
  padding: 0 6px;
}

.content { flex: 1; min-width: 0; padding: 14px 22px 40px; }

@media (max-width: 720px) {
  .shell { flex-direction: column; }
  .side { width: auto; height: auto; position: static; overflow: visible; }
  .side-scroll { overflow: visible; }
}

input[type=text] {
  flex: 1;
  padding: 6px 10px;
  border-radius: var(--pt-radius-sm);
  border: 1px solid var(--pt-line);
  background: var(--pt-bg);
  color: var(--pt-ink);
}

button {
  padding: 6px 14px;
  border-radius: var(--pt-radius-sm);
  border: 0;
  background: var(--pt-accent);
  color: var(--pt-accent-ink);
  cursor: pointer;
}

nav {
  padding: 6px 0;
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 13px;
}

nav a {
  color: var(--pt-ink-dim);
  text-decoration: none;
  padding: 3px 10px;
  border-radius: var(--pt-radius-pill);
}

nav a.on { background: var(--pt-accent); color: var(--pt-accent-ink); }

.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 14px;
  margin-top: 12px;
}

.card {
  background: var(--pt-surface-1);
  border-radius: var(--pt-radius);
  overflow: hidden;
  text-decoration: none;
  color: inherit;
  display: flex;
  flex-direction: column;
  position: relative;
  border: 1px solid var(--pt-frame-edge);
}

.card img {
  width: 100%;
  height: 150px;
  object-fit: cover;
  background: var(--pt-image-bg);
  display: block;
}

.card .body {
  padding: 8px 10px 10px;
  font-size: 12.5px;
}

.card .path {
  color: var(--pt-ink-dim);
  font-size: 11px;
  word-break: break-all;
  margin-top: 4px;
}

.cardbox { position: relative; }

.cardbox > a.card { display: flex; }

.cardbox form.act {
  position: absolute;
  top: 6px;
  right: 6px;
  margin: 0;
  z-index: 2;
}

.cardbox form.act button.mini {
  background: var(--pt-chip-bg);
}

.snippet {
  max-height: 5.4em;
  overflow: hidden;
}

.muted { color: var(--pt-ink-dim); }

.badge {
  display: inline-block;
  font-size: 10.5px;
  padding: 1px 7px;
  margin-right: 5px;
  border-radius: var(--pt-radius);
  background: var(--pt-surface-2);
  color: var(--pt-ink-dim);
}

.badge.err { background: var(--pt-err-bg); color: var(--pt-err-ink); }

.badge.ok { background: var(--pt-ok-bg); color: var(--pt-ok-ink); }

.badge.review { background: var(--pt-review-bg); color: var(--pt-review-ink); }

.pager { margin: 18px 0; display: flex; gap: 10px; }

.pager a { color: var(--pt-link); text-decoration: none; }

.detail { display: flex; gap: 24px; margin-top: 16px; flex-wrap: wrap; }

.detail img {
  max-width: min(680px, 100%);
  max-height: 78vh;
  border-radius: var(--pt-radius);
  background: var(--pt-image-bg);
}

.meta { flex: 1; min-width: 300px; }

.meta table { border-collapse: collapse; font-size: 13px; margin-bottom: 14px; }

.meta td { padding: 3px 12px 3px 0; vertical-align: top; }

.meta td:first-child { color: var(--pt-ink-dim); white-space: nowrap; }

pre {
  background: var(--pt-surface-1);
  padding: 12px;
  border-radius: var(--pt-radius);
  white-space: pre-wrap;
  font-size: 13px;
  max-width: 100%;
  overflow-wrap: anywhere;
}

details { margin-top: 12px; }

ul.locs {
  font-size: 12.5px;
  color: var(--pt-nav-ink);
  padding-left: 18px;
}

ul.locs li { margin: 10px 0; }

ul.locs a { color: var(--pt-link); }

ul.locs .muted { font-size: 11.5px; }

.actions { margin: 10px 0 4px; }

form.act { display: inline-block; margin-right: 8px; }

button.mini {
  padding: 4px 12px;
  border-radius: var(--pt-radius-sm);
  border: 1px solid var(--pt-line);
  background: var(--pt-surface-1);
  color: var(--pt-ink);
  font-size: 12px;
  cursor: pointer;
}

button.mini:hover { background: var(--pt-surface-2); }

.trow {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  background: var(--pt-surface-1);
  border-radius: var(--pt-radius);
  padding: 8px;
  margin-bottom: 10px;
}

.trow img {
  width: 110px;
  height: 82px;
  object-fit: cover;
  border-radius: var(--pt-radius-sm);
  background: var(--pt-image-bg);
}

.tbody { flex: 1; font-size: 13px; }

.note { color: var(--pt-ink-dim); margin: 12px 0; }

a.back { color: var(--pt-link); text-decoration: none; }

.pickwrap { position: relative; display: inline-block; }

.pickwrap img { cursor: crosshair; }

.selbox {
  position: absolute;
  border: 2px solid var(--pt-link);
  background: var(--pt-selbox-tint);
  display: none;
  pointer-events: none;
}

.tagform { display: flex; gap: 6px; margin-top: 10px; max-width: 420px; }

.tagform input[type=text] { flex: 1; }

.people { margin: 6px 0 10px; }

.chipline { margin: 4px 0; }

.chipline .badge { font-size: 12px; padding: 2px 8px; }

.face {
  width: 72px;
  height: 72px;
  object-fit: cover;
  border-radius: var(--pt-radius);
  background: var(--pt-image-bg);
  display: block;
}

.pcard {
  display: flex;
  gap: 14px;
  align-items: center;
  background: var(--pt-surface-1);
  border-radius: var(--pt-radius);
  padding: 12px;
  margin-bottom: 10px;
  text-decoration: none;
  color: inherit;
}

.pcard .pbody { flex: 1; }

.wideform { display: flex; gap: 6px; margin: 8px 0; max-width: 420px; }

.wideform input[type=text] {
  flex: 1;
  padding: 6px 10px;
  border-radius: var(--pt-radius-sm);
  border: 1px solid var(--pt-line);
  background: var(--pt-bg);
  color: var(--pt-ink);
}

.side { overflow: hidden; }

.side-top {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.side-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

details.fgroup > summary {
  list-style: none;
  cursor: pointer;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: .06em;
  color: var(--pt-ink-faint);
  padding: 0 6px;
  display: flex;
  justify-content: space-between;
}

details.fgroup > summary::-webkit-details-marker { display: none; }

summary .count { color: var(--pt-ink-dim); text-transform: none; }

.fitems { max-height: 34vh; overflow-y: auto; }

.ffilter { width: calc(100% - 12px); margin: 4px 6px; font-size: 12px; padding: 4px 8px; }"""

_REDUCED_MOTION = """\
@media (prefers-reduced-motion: reduce) {
  .rec-dot { animation: none; }
  .card::before { transition: none; }
}"""

_PRINT = """\
@media print {
  .lightbox { display: none; }
  body { background: #fff; color: #000; }
  body::after { display: none; }
  .theme-toggle { display: none; }
}"""

_MACHINE_SAMARITAN = """\
html[data-theme='machine'] pre,
html[data-theme='samaritan'] pre {
  border: 1px solid var(--pt-line);
  background: var(--pt-surface-1);
  font-family: var(--pt-font-mono);
}

html[data-theme='machine'] .selbox {
  box-shadow: var(--pt-face-glow);
}

html[data-theme='samaritan'] .selbox::after,
html[data-theme='machine'] .selbox::after {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  width: 6px;
  height: 6px;
  background: var(--pt-accent);
}

html[data-theme='machine'] .card::before {
  content: '';
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 1;
  opacity: 0;
  transition: opacity .12s;
  --c: 16px;
  --b: 2px solid currentColor;
  background:
    linear-gradient(currentColor, currentColor) 0 0 / var(--c) var(--b) no-repeat,
    linear-gradient(currentColor, currentColor) 0 0 / var(--b) var(--c) no-repeat,
    linear-gradient(currentColor, currentColor) 100% 0 / var(--c) var(--b) no-repeat,
    linear-gradient(currentColor, currentColor) 100% 0 / var(--b) var(--c) no-repeat,
    linear-gradient(currentColor, currentColor) 0 100% / var(--c) var(--b) no-repeat,
    linear-gradient(currentColor, currentColor) 0 100% / var(--b) var(--c) no-repeat,
    linear-gradient(currentColor, currentColor) 100% 100% / var(--c) var(--b) no-repeat,
    linear-gradient(currentColor, currentColor) 100% 100% / var(--b) var(--c) no-repeat;
  filter: var(--pt-frame-glow);
}

html[data-theme='machine'] .card:hover::before { opacity: 1; }

html[data-theme='machine'] .card .snippet,
html[data-theme='samaritan'] .card .snippet,
html[data-theme='machine'] .card .path,
html[data-theme='samaritan'] .card .path {
  font-family: var(--pt-font-mono);
  font-size: 11px;
}

html[data-theme='machine'] .side .brand,
html[data-theme='samaritan'] .side .brand,
html[data-theme='machine'] .navgroup,
html[data-theme='samaritan'] .navgroup,
html[data-theme='machine'] .badge,
html[data-theme='samaritan'] .badge,
html[data-theme='machine'] button,
html[data-theme='samaritan'] button,
html[data-theme='machine'] button.mini,
html[data-theme='samaritan'] button.mini,
html[data-theme='machine'] .sidefoot,
html[data-theme='samaritan'] .sidefoot {
  text-transform: uppercase;
  letter-spacing: .08em;
  font-family: var(--pt-font-ui);
}"""

_REC_DOT = """\
.rec-dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--pt-accent);
  box-shadow: var(--pt-rec-glow);
  animation: pt-pulse 1.2s infinite ease-in-out;
  flex: none;
}

.rec-dot.off { animation: none; box-shadow: none; }

@keyframes pt-pulse {
  0%, 100% { opacity: .35; }
  50% { opacity: 1; }
}"""

_SUBJECT = """\
.subject {
  position: relative;
  margin: 0;
  padding: 0;
  border: none;
}
html[data-theme='machine'] .subject,
html[data-theme='samaritan'] .subject {
  margin: .5rem 0 .75rem;
  padding: .75rem;
  border: 1px solid var(--pt-frame-edge);
}

html[data-theme='machine'] .subject::before,
html[data-theme='samaritan'] .subject::before {
  content: '';
  position: absolute;
  inset: 0;
  pointer-events: none;
  --c: 16px;
  --b: 2px solid currentColor;
  background:
    linear-gradient(currentColor, currentColor) 0 0 / var(--c) var(--b) no-repeat,
    linear-gradient(currentColor, currentColor) 0 0 / var(--b) var(--c) no-repeat,
    linear-gradient(currentColor, currentColor) 100% 0 / var(--c) var(--b) no-repeat,
    linear-gradient(currentColor, currentColor) 100% 0 / var(--b) var(--c) no-repeat,
    linear-gradient(currentColor, currentColor) 0 100% / var(--c) var(--b) no-repeat,
    linear-gradient(currentColor, currentColor) 0 100% / var(--b) var(--c) no-repeat,
    linear-gradient(currentColor, currentColor) 100% 100% / var(--c) var(--b) no-repeat,
    linear-gradient(currentColor, currentColor) 100% 100% / var(--b) var(--c) no-repeat;
  filter: var(--pt-frame-glow);
}

.subject::after {
  content: '';
  display: var(--pt-reticle);
  position: absolute;
  top: 50%;
  left: 50%;
  width: 24px;
  height: 24px;
  transform: translate(-50%, -50%);
  pointer-events: none;
  background:
    linear-gradient(currentColor, currentColor) 50% 0 / 1px 100% no-repeat,
    linear-gradient(currentColor, currentColor) 0 50% / 100% 1px no-repeat;
  opacity: .6;
}

.subject.tone-err { color: var(--pt-err-ink); }
.subject.tone-warn { color: var(--pt-review-ink); }

.designation {
  display: none;
  position: absolute;
  top: -.7em;
  left: .75rem;
  font-family: var(--pt-font-mono);
  font-size: .6875rem;
  text-transform: uppercase;
  letter-spacing: .08em;
  background: var(--pt-bg);
  color: var(--pt-tone, var(--pt-ink));
  border: 1px solid var(--pt-tone, var(--pt-ink));
  padding: .1rem .5rem;
}
html[data-theme='machine'] .designation,
html[data-theme='samaritan'] .designation { display: inline-block; }"""

_THEME_TOGGLE = """\
.theme-toggle {
  font-family: var(--pt-font-mono);
  font-size: .6875rem;
  text-transform: uppercase;
  letter-spacing: .08em;
  color: var(--pt-ink-dim);
  background: transparent;
  border: 1px solid var(--pt-line);
  padding: .2rem .6rem;
  cursor: pointer;
}

.theme-toggle:hover {
  color: var(--pt-accent);
  border-color: var(--pt-accent);
}"""

# ---------------------------------------------------------------------------
# THEME_CSS  — the complete compiled stylesheet
# ---------------------------------------------------------------------------

_LIGHTBOX_CSS = """\
.lightbox {
  display: none;
  position: fixed;
  inset: 0;
  z-index: 1000;
  background: rgba(0, 0, 0, 0.85);
  padding: 2rem;
  cursor: zoom-out;
  overflow: hidden;
}
.lightbox.open { display: flex; align-items: center; justify-content: center; }
.lightbox-frame {
  position: relative;
  padding: 1rem;
  color: var(--pt-ink);
  border: 1px solid var(--pt-frame-edge);
}
.lightbox-frame::before {
  content: '';
  position: absolute;
  inset: 0;
  pointer-events: none;
  --c: 24px;
  --b: 2px solid currentColor;
  background:
    linear-gradient(currentColor, currentColor) 0 0 / var(--c) var(--b) no-repeat,
    linear-gradient(currentColor, currentColor) 0 0 / var(--b) var(--c) no-repeat,
    linear-gradient(currentColor, currentColor) 100% 0 / var(--c) var(--b) no-repeat,
    linear-gradient(currentColor, currentColor) 100% 0 / var(--b) var(--c) no-repeat,
    linear-gradient(currentColor, currentColor) 0 100% / var(--c) var(--b) no-repeat,
    linear-gradient(currentColor, currentColor) 0 100% / var(--b) var(--c) no-repeat,
    linear-gradient(currentColor, currentColor) 100% 100% / var(--c) var(--b) no-repeat,
    linear-gradient(currentColor, currentColor) 100% 100% / var(--b) var(--c) no-repeat;
  filter: var(--pt-frame-glow);
}
.lightbox-zoom {
  position: relative;
  width: fit-content;
  margin: 0 auto;
  transform-origin: 50% 50%;
}
.lightbox-frame img {
  display: block;
  max-width: 92vw;
  max-height: 80vh;
  width: auto;
  height: auto;
  cursor: default;
}
.lightbox-zoom img.zoomed { cursor: grab; }
.lightbox-zoom img.dragging { cursor: grabbing; }
.lightbox-controls {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 0.5rem;
  margin-top: 0.6rem;
}
.lightbox-controls button {
  font-family: var(--pt-font-mono);
  font-size: 0.6875rem;
  text-transform: uppercase;
  letter-spacing: .08em;
  color: var(--pt-ink-dim);
  background: transparent;
  border: 1px solid var(--pt-line);
  padding: 0.2rem 0.6rem;
  cursor: pointer;
}
.lightbox-controls button:hover {
  color: var(--pt-accent);
  border-color: var(--pt-accent);
}
.lb-level {
  font-family: var(--pt-font-mono);
  font-size: 0.6875rem;
  color: var(--pt-ink-faint);
  min-width: 3.5rem;
  text-align: center;
}
.lightbox-caption {
  font-family: var(--pt-font-mono);
  font-size: 0.75rem;
  color: var(--pt-ink-dim);
  margin: 0.6rem 0 0;
  text-align: center;
}
"""

LIGHTBOX_JS = """\
(function () {
  var box = document.getElementById('lightbox');
  if (!box) return;
  var frame = box.querySelector('.lightbox-frame');
  var zoomEl = box.querySelector('.lightbox-zoom');
  var img = zoomEl.querySelector('img');
  var cap = box.querySelector('.lightbox-caption');
  var level = box.querySelector('.lb-level');
  var imgs = Array.prototype.slice.call(
    document.querySelectorAll('.detail img:not(#pickimg)')
  ).filter(function (el) { return !el.closest('#lightbox'); });
  var current = -1;
  var scale = 1;
  var tx = 0;
  var ty = 0;
  var dragging = false;
  var lx = 0;
  var ly = 0;

  function apply() {
    if (scale < 1) { scale = 1; }
    if (scale <= 1) { tx = 0; ty = 0; }
    zoomEl.style.transform =
      'translate(' + tx + 'px, ' + ty + 'px) scale(' + scale + ')';
    if (level) { level.textContent = Math.round(scale * 100) + '%'; }
    img.classList.toggle('zoomed', scale > 1);
  }

  function reset() {
    scale = 1;
    tx = 0;
    ty = 0;
    apply();
  }

  function open(i) {
    var src = imgs[i];
    if (!src) return;
    img.src = src.src;
    var fig = src.closest('figure');
    var capText = '';
    if (fig) {
      var tag = fig.querySelector('.designation');
      capText = tag ? tag.textContent : '';
    }
    cap.textContent = capText || (src.alt || '');
    box.classList.add('open');
    box.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    current = i;
    reset();
  }

  function close() {
    box.classList.remove('open');
    box.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
    current = -1;
  }

  function zoomBy(factor, clientX, clientY) {
    var s0 = scale;
    var s1 = Math.min(8, Math.max(1, s0 * factor));
    if (s1 === s0) { return; }
    var cx = 0;
    var cy = 0;
    if (clientX !== undefined) {
      var rect = zoomEl.getBoundingClientRect();
      cx = clientX - (rect.left + rect.width / 2);
      cy = clientY - (rect.top + rect.height / 2);
    }
    tx -= (cx / s0) * (s1 - s0);
    ty -= (cy / s0) * (s1 - s0);
    scale = s1;
    apply();
  }

  imgs.forEach(function (el, i) {
    el.addEventListener('click', function (e) {
      e.preventDefault();
      open(i);
    });
  });

  box.addEventListener('click', function (e) {
    if (e.target === box || e.target === frame || e.target === zoomEl) {
      close();
    }
  });
  img.addEventListener('click', function (e) {
    e.stopPropagation();
  });
  box.addEventListener('wheel', function (e) {
    if (current < 0) { return; }
    e.preventDefault();
    zoomBy(e.deltaY < 0 ? 1.25 : 0.8, e.clientX, e.clientY);
  }, { passive: false });

  Array.prototype.slice.call(box.querySelectorAll('[data-zoom]')).forEach(
    function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        var mode = btn.getAttribute('data-zoom');
        if (mode === 'in') { zoomBy(1.25); }
        else if (mode === 'out') { zoomBy(0.8); }
        else { reset(); }
      });
    }
  );

  img.addEventListener('dblclick', function () {
    scale = scale > 1 ? 1 : 2.5;
    tx = 0;
    ty = 0;
    apply();
  });
  img.addEventListener('mousedown', function (e) {
    if (scale <= 1) { return; }
    e.preventDefault();
    dragging = true;
    lx = e.clientX;
    ly = e.clientY;
    img.classList.add('dragging');
  });
  document.addEventListener('mousemove', function (e) {
    if (!dragging) { return; }
    tx += e.clientX - lx;
    ty += e.clientY - ly;
    lx = e.clientX;
    ly = e.clientY;
    apply();
  });
  document.addEventListener('mouseup', function () {
    dragging = false;
    img.classList.remove('dragging');
  });

  document.addEventListener('keydown', function (e) {
    if (current < 0) return;
    if (e.key === 'Escape') close();
    if (e.key === 'ArrowRight') open((current + 1) % imgs.length);
    if (e.key === 'ArrowLeft') open((current - 1 + imgs.length) % imgs.length);
  });
})();
"""

THEME_CSS: str = (
    _FONTS
    + "\n\n"
    + _theme_block(":root,\nhtml[data-theme='icloud']", "dark", TOKENS["icloud"])
    + "\n\n"
    + _theme_block("html[data-theme='machine']", "dark", TOKENS["machine"])
    + "\n\n"
    + _theme_block("html[data-theme='samaritan']", "light", TOKENS["samaritan"])
    + "\n\n"
    + _SHARED_CSS
    + "\n"
    + _REC_DOT
    + "\n\n"
    + _SUBJECT
    + "\n\n"
    + _THEME_TOGGLE
    + "\n"
    + _MACHINE_SAMARITAN
    + "\n"
    + _REDUCED_MOTION
    + "\n"
    + _PRINT
    + "\n"
    + _LIGHTBOX_CSS
)


def restore_js(default_theme: str = DEFAULT_THEME) -> str:
    """Return a JS snippet that restores the theme on page load.

    The snippet reads `localStorage['pt-theme']` and falls back to
    `default_theme`.  The parameter lets callers decide what the fallback
    is (typically `DEFAULT_THEME` but a profile may override it).
    """
    if default_theme not in THEMES:
        raise ValueError(f"unknown theme: {default_theme!r}")
    themes_list = list(THEMES)
    return (
        "(function(){"
        f"var t='{default_theme}';"
        "try{var s=localStorage.getItem('pt-theme');"
        f"if(s&&{themes_list!r}.indexOf(s)!==-1)t=s;}}"
        "catch(e){}"
        "document.documentElement.setAttribute('data-theme',t);"
        "})();"
    )


TOGGLE_JS: str = (
    "(function(){"
    "var btn=document.getElementById('pt-theme-toggle');"
    "if(!btn)return;"
    "var order=['icloud','machine','samaritan'];"
    "var names={icloud:'iCloud',machine:'Machine',samaritan:'Samaritan'};"
    "function label(){"
    "var t=document.documentElement.getAttribute('data-theme')||'icloud';"
    "btn.textContent=names[t]||t;}"
    "btn.addEventListener('click',function(){"
    "var cur=document.documentElement.getAttribute('data-theme')||'icloud';"
    "var idx=order.indexOf(cur);"
    "var next=order[(idx+1)%order.length];"
    "document.documentElement.setAttribute('data-theme',next);"
    "try{localStorage.setItem('pt-theme',next);}catch(e){}"
    "label();});"
    "label();})();"
)