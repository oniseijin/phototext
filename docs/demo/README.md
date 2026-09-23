# Demo catalog

The screenshots in [../web-ui.md](../web-ui.md) and [../themes.md](../themes.md)
come from a synthetic catalog: ~50 generated photos (receipts, invoices,
letters, postcards, signs, menus, tickets, chat and settings screenshots,
book pages, notes, memes, scenes, people, a duplicate pair) pushed through the
real pipeline against `demo_mock.py` — a mock Ollama whose extraction
responses are authored per photo and matched by a 16x16 luminance
fingerprint. No real photos, no real model, fully reproducible.

Rebuild it (also refreshes `~/Pictures/phototext-demo-library`):

```bash
.venv/bin/python docs/demo/make_demo_catalog.py
```

Then serve it:

```bash
.venv/bin/phototext --config docs/demo/build/config.toml serve            # read-only
.venv/bin/phototext --config docs/demo/build/config.toml serve --writable # picker + bulk actions
```

The catalog intentionally contains: a `coffee` search theme, EXIF dates
spread over 2011-2025, categories, three people (one with tags sitting in
the review queue), two meme clusters, a duplicate pair, one model-error
photo, one hidden, one trashed, and two still queued so the REC dot pulses.
