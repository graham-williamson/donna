# Daruma Board — Japanese Revamp Design

**Date:** 2026-06-06
**Status:** Approved pending Graham's spec review
**Builds on:** §6 Daruma board (2026-06-05 personal-multi-agent-system spec), Plan 4 implementation

## Purpose

Massively revamp the Daruma Board (`personal-system/tools/dashboard.py`, localhost:8765) into a beautiful, calming, Japanese-themed dashboard. Three switchable visual themes, ambient animation, authentic daruma artwork, and new life features: on-board add/edit, a wins alcove, a wish wall, celebrations, seasonal awareness, a daily zen line, and a focus timer.

Local-only. No internet exposure in this version (an internet-friendly SaaS variant is deliberately deferred — it is the seed entry on the new wish wall).

## Architecture

`tools/dashboard.py` remains the zero-dependency Python-stdlib server (routes + data access via `goals.py`, `habits.py`, `tokens.py`, `evidence.py`, new `wishes.py`). Frontend assets move out of inline strings into `tools/dashboard_assets/`, served by a new `/static/<name>` route (path-sanitised, whitelisted extensions: css/js/json/svg).

```
personal-system/tools/
  dashboard.py            # server: routes, HTML rendering, daruma SVG generation
  wishes.py               # NEW: ema wish wall engine (wishes.json)
  dashboard_assets/       # NEW
    style.css             # base layout + 3 theme blocks via CSS custom properties
    board.js              # theme switcher, animations, celebration, incense timer, forms UX
    zen.json              # ~60 haiku / zen / stoic lines for the daily zen moment
```

Data: `_shared/_state/wishes.json` (gitignored, Time-Machine-backed, same pattern as `goals.json`).

Existing engines (`goals.py`, `habits.py`, `tokens.py`, `evidence.py`) are untouched. `wishes.py` calls `goals.add_goal` on promotion. Verified signatures: `goals.add_goal(title, colour, why_it_matters)`, `habits.add_habit(name, identity, cue)`, `evidence.surface_evidence(limit)`.

## Theme system

`<html data-theme="washi|sakura|twilight">` drives all colour/animation via CSS custom properties.

- **Washi (和)** — light theme A. Handmade-paper background, sumi-e ink accents, muted earth tones + vermillion (#a63a2b), gold (#caa64b).
- **Sakura (桜)** — light theme B. Blossom pinks over pond greens (#d8616b, #9fc4bc), soft white cards.
- **Twilight Temple (月)** — dark mode. Indigo night (#141b2d→#1f2a44), lantern gold (#d4af37), torii-silhouette horizon strip.

Switching: header control with 和 / 桜 toggle plus 月 dark-mode button. Default: follow `prefers-color-scheme` (dark → twilight; light → last-chosen light theme, initially washi). Choice persisted in `localStorage` (`daruma-theme`, `daruma-light-pref`). No server round-trip; no FOUC (inline boot script sets `data-theme` before paint).

## Signature animations (per theme)

All ambient, GPU-cheap (CSS transforms/opacity + one low-frequency JS particle layer), capped particle counts, paused when tab hidden, fully disabled under `prefers-reduced-motion`.

- **Washi:** slow ink-wash mist drifting horizontally; faint floating dust motes; sections fade-and-slide in on load like brush strokes.
- **Sakura:** falling petals at 2–3 depths; two koi (SVG) gliding along slow bezier paths beneath the content, subtle tail sway.
- **Twilight:** twinkling stars; drifting fireflies (soft glow pulses); gentle lantern-glow breathing behind cards.

## Daruma artwork

`_daruma_svg(colour, state)` redrawn: authentic okiagari silhouette (round-bottomed, near-spherical), cream face patch, ink eyebrow + moustache strokes, gold tassel/body flourish, goal-coloured body, subtle radial shading. Eye states unchanged in contract: `none` (two blank eyes), `left` (left pupil filled, committed), `both` (achieved). Hover: small roly-poly wobble (CSS rotate keyframes around bottom-centre origin) — daruma get back up.

Existing test contract preserved: rendered SVG carries `data-state` and pupils use `fill="#111"` so `test_dashboard.py` eye-count assertions keep passing (updated only if markup must change, with equivalent assertions).

## Page structure (top → bottom)

1. **Header** — 達磨 Daruma Board title, current 72-micro-season subtitle, theme controls.
2. **Daily zen** — one line from `zen.json`, deterministic pick by date (`hash(date) % len`), calligraphy-styled card.
3. **Goals** — daruma cards (redrawn), commit/achieve actions, add-goal form (collapsible): title, colour (8 swatches), why-it-matters.
4. **Ema Wall (wish list)** — wishes as wooden ema plaques hanging on a rack rail. Add-wish form (text only). Each ema: wish text, added date, actions: *promote to daruma* (choose colour → calls `goals.add_goal`, wish marked promoted) and *release* (archived, not deleted). Seeded with: "Internet-friendly SaaS version of the Daruma Board".
5. **Habits** — existing habit cards restyled; mark-done keeps streak flame; add-habit form (name, identity, cue).
6. **Tokonoma (wins alcove)** — recent wins from `evidence.surface_evidence()` (achieved goals already log there) displayed as treasures in an alcove-styled band.
7. **Incense focus timer** — client-side: choose 25/50 min, an incense stick burns down visually, soft WebAudio chime at the end. No persistence.
8. **Context efficiency** — existing token panel, restyled.
9. **Daemon** — existing model switcher + restart, restyled.

## Celebration

On `/achieve` the redirect carries `?celebrate=<goal_id>`; the page then plays: petal/gold particle burst from the achieved daruma, 達成 ("accomplished") kanji rising and fading, temple-bell chime synthesised via WebAudio (no audio assets; respects reduced-motion by skipping visuals and playing nothing unless user-initiated — bell only on this user-initiated flow).

## Seasonal awareness

A static table of Japan's 72 micro-seasons (kō) embedded in `dashboard.py` (or `board.js`): date-range → name (Japanese + English), e.g. "腐草為螢 — Rotten grass becomes fireflies (Jun 11–15)". Shown in the header subtitle. The particle layer takes a seasonal accent: winter adds slow snow, summer adds fireflies to light themes, autumn swaps some sakura petals for momiji leaves. Theme remains the user's choice; season only flavours the particles.

## `wishes.py` contract

```
add_wish(text, wishes_path=None) -> wish dict  {id, text, created_at, status:"open", promoted_goal_id:None}
list_wishes(status="open", wishes_path=None) -> [wish]
promote_wish(id, colour, wishes_path=None, goals_path=None) -> goal dict  (status→"promoted", links goal id)
release_wish(id, wishes_path=None) -> wish  (status→"released"; never deleted)
```

Colour validation delegated to `goals.add_goal` (raises `ValueError` on bad colour).

## New/changed HTTP routes

| Route | Method | Action |
|---|---|---|
| `/static/<name>` | GET | serve asset from `dashboard_assets/` (sanitised, whitelisted) |
| `/add-goal` | POST | form → `goals.add_goal(title, colour, why_it_matters)` |
| `/add-habit` | POST | form → `habits.add_habit(name, identity, cue)` |
| `/add-wish` | POST | form → `wishes.add_wish(text)` |
| `/promote-wish` | POST | form (id, colour) → `wishes.promote_wish` |
| `/release-wish` | GET | `?id=` → `wishes.release_wish` |
| existing `/commit`, `/achieve`, `/habit-done`, `/set-model`, `/restart` | GET | unchanged (achieve redirect gains `?celebrate=`) |

POST bodies are `application/x-www-form-urlencoded`, parsed with stdlib. All mutating routes redirect 303 to `/`. Server stays bound to 127.0.0.1; port override behaviour unchanged.

## Mobile

Responsive layout (single column under 700px, touch-sized tap targets, reduced particle counts on small screens). Internet/Tailscale access: **out of scope** — wish-listed.

## Error handling

- Static route: unknown/traversal path → 404; unknown extension → 404.
- Form posts: missing/invalid fields → 303 redirect with `?error=` query, rendered as a quiet inline notice (no stack traces to the browser).
- `wishes.json` absent → treated as empty wall; created on first write (same as goals).
- All animation JS defensive: missing canvas/feature → static page still fully functional (progressive enhancement; the board works with JS disabled except timer/celebration/theme-switch).

## Testing

Extend `tests/test_dashboard.py`, add `tests/test_wishes.py` (pytest, same loader pattern):

- wishes: add → open; promote → goal created with right colour/title + wish linked; release → archived not deleted; invalid colour raises.
- dashboard render: `data-theme` boot present; daruma SVG eye states (existing assertions); ema wall renders wishes; tokonoma renders evidence wins; forms present; zen line present and deterministic for a fixed date; 72-season subtitle present for a fixed date.
- routes: POST add-goal/add-wish mutate state files (tmp paths); static route serves css and rejects traversal (`/static/../goals.py` → 404).

## Out of scope (wish-listed / later)

- Internet-friendly access (Tailscale or hosted SaaS variant) — **on the ema wall**.
- Habit heatmap grid; morning "today panel" (offered, not selected).
- Real audio assets; sound beyond the synthesised bell/chime.
- Editing goal text after creation (add-only for now, matching current engines).
