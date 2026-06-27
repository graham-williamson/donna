---
layer: 2
owner: nike
stage: morning-checkin
---

# Stage · Morning check-in (💪 Nike)

The replacement for the old dumb 8:49 "SPR check-in" ping. Nike opens the day: a read on the body, today's session, and one real question. Her guaranteed morning slot in the attention rhythm — Donna gatekeeps the channel; this is Nike's to own.

## INPUTS

- **Active goals** — Nike/green goals with a filled left eye (committed), via `tools/checkin.gather_inputs()` → `goals.list_goals(owner="nike")` filtered to `daruma_state in (left, both)`.
- **Recent energy/recovery** — the last few energy observations, via `pmem.recall(topic="energy", persona="nike")`.
- **(Runtime, via the daemon's MCP)** today's calendar window, and the day's session from the Exercise System Master Plan in Notion. Fetched at compose-time; not this module's job.

## PROCESS

1. Call `checkin.gather_inputs()` for local goals + energy history.
2. (Runtime) fetch today's calendar window and the day's SPR session from Notion.
3. **Compose** — in Nike's voice, opening with 💪 — a short check-in: a read on how the body's likely feeling (from recent energy), today's session, and **one real question** (how's the body? what's today's one move?). Punchy, not a lecture.
4. **Send** via the daemon. Donna gatekeeps the channel; this is Nike's guaranteed slot.
5. **On Graham's reply**, call `checkin.log_response(text, energy=...)` to log it as `observation`s, so nightly dreaming surfaces patterns (e.g. "low energy after late TradeAlly nights").

## OUTPUTS

- One morning check-in message (Nike's voice, 💪).
- Graham's reply logged as `observation`s (`owner=nike`, topics `energy`/`training`) → consolidated by the nightly dream sweep.
- If a session is done, a win can be logged to Esme's evidence ledger.
