# donna-recon

Browser-recon recorder. Launches an isolated Chromium, follows Graham
through a site, and writes a structured recording directory that the next
Claude Code session consumes directly to spec a capability executor.

Domain-general — same tool for Everyone Active, Tesco, OpenTable, Amazon,
whatever comes next. Secrets are redacted **at write time** in both the
network log and the HTML snapshots. The tool opens zero outbound sockets
itself; only Chromium hits the network, driven by Graham's clicks.

## First-time setup

From this directory:

- `python3 -m venv .venv` — create the isolated Python 3.12+ venv.
- `.venv/bin/pip install -r requirements.txt` — install Playwright + bs4.
- `.venv/bin/playwright install chromium` — download the pinned Chromium.
- `chmod +x donna-recon` — make the launcher executable.

Optional: symlink `donna-recon` onto your `PATH`, e.g.
`ln -s "$PWD/donna-recon" ~/bin/donna-recon`.

## Day-to-day use

All commands are invoked via the launcher:

- `./donna-recon start [--url URL]` — foreground. Prints the output dir
  and CDP port, then blocks. Open the Chromium window that pops up, log
  in, click through the states you care about. Press **F9** to tag a
  state (a browser prompt asks for a label). Ctrl-C here when done.
- `./donna-recon stop` — from a second terminal. Sends SIGTERM to the
  recorder, waits for clean shutdown.
- `./donna-recon mark "bookable class row"` — belt-and-braces fallback
  to F9. Works even if the page blocks `window.prompt`.
- `./donna-recon list` — list past recordings.
- `./donna-recon show <id>` — summarise one recording.

Only one recording can be active at a time (enforced by `fcntl.flock`).

## What lands on disk

Per recording, under `~/.donna-recon/<iso-ts>/` (mode 0700, files 0600):

```
meta.json        — start/stop timestamps, Chromium version, CDP port
trace.jsonl      — ordered events: navigations, markers, subframe navs, page closes
network.jsonl    — every request + response, redacted metadata only
snapshots/
  0001-<slug>.html     — sanitised outerHTML (passwords, tokens, csrf stripped)
  0001-<slug>.png      — viewport screenshot
  ...
```

## What gets redacted

**Network log (`network.jsonl`):**

- URL query params whose name looks secret (`token`, `password`, `csrf`,
  `auth`, `session`, …) → value replaced with `[REDACTED]`, name kept.
- `Cookie`, `Set-Cookie`, `Authorization`, `Proxy-Authorization` headers
  dropped wholesale.
- Request body form fields / JSON values under secret-looking keys →
  `[REDACTED]`.
- Malformed or opaque bodies → metadata shape only, no raw bytes.
- Response bodies are **never** captured in v1 — only status,
  content-length, content-type.

**HTML snapshots (`snapshots/*.html`):**

- `<input type="password">` values stripped.
- `<input type="hidden">` with secret-looking name/id → value stripped.
- `<input autocomplete="current-password|new-password|one-time-code">` →
  value stripped.
- `<meta name="csrf-token">` (and similar) → content stripped.
- Inline `<script>` blocks — JSON-ish `"key":"value"` pairs with
  secret-looking keys redacted; `Bearer <token>` literals redacted.
- Inline `<script type="application/json">` — parsed, redacted
  recursively, re-serialised.
- `data-*` attributes with secret-looking names → value `[REDACTED]`.

**Screenshots are NOT automatically sanitised.** A viewport PNG is a
bitmap — there's no reliable programmatic redaction. Don't press F9 or
run `./donna-recon mark` while a password or OTP is visible on-screen.

## Handoff to the next session

Tell the next Claude Code session:

> read `~/.donna-recon/<id>/meta.json` and `trace.jsonl`, then follow
> references into `snapshots/`

That's all the context it needs to write an executor spec from live data
rather than guessed markup.

## Running tests

```
.venv/bin/pytest
.venv/bin/mypy donna_recon tests
```

## Troubleshooting

- **"another recording is already active"** — a recorder still holds the
  flock. Check `./donna-recon list` and `cat ~/.donna-recon/.current`.
  If the PID in `recorder.pid` is dead, the next `start` will clean up
  automatically.
- **Stale ephemeral profile dirs** — if the recorder crashed, the wipe
  step was skipped. Remove by hand: `rm -rf /tmp/donna-recon-*`.
- **F9 does nothing on a weird site** — some pages swallow keydown or
  block `window.prompt`. Use `./donna-recon mark <label>` from another
  terminal instead; the file-based marker path is independent of any
  page JS.
- **The indicator badge doesn't show on the first page** — the init
  script is injected via CDP *after* the initial page loads, so the
  badge only appears on the next navigation. Not a functional issue.
