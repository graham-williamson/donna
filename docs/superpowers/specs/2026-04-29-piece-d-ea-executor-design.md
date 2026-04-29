# Piece D — Everyone Active class-booking executor

**Date:** 2026-04-29
**Phase:** 2
**Status:** Design approved, pending implementation plan
**Spec refs:** `donna-security-v1.md` v1.1 §8 (capability manifest), §9.2 (subprocess isolation), §17 (Phase 2 — browser executors + age vault)
**Depends on:** Piece C (creds injection into executor, shipped)
**Unblocks:** Piece E (Phase 2 rule gate in CLAUDE.md)

---

## 1. Purpose

Ship the first real capability executor: a Playwright-based browser automation subprocess that books gym classes at Everyone Active. The broker spawns it, passes credentials via inherited pipe fd, and receives a structured JSON outcome. This is the template for all future browser-automation executors.

## 2. Non-goals

- Monitoring GUI or executor dashboard. Later phase.
- Payment-required classes. Executor fails cleanly if a payment step is encountered.
- Revalidation handler. Staleness is handled by the executor's own error codes.
- Persistent browser sessions or cookie caching. Launch and kill every time.
- Multi-centre discovery. Only Graham's two centres (Chesham, Chilterns) are mapped.

## 3. Executor identity and subprocess contract

**Binary:** `/Users/donna-broker/broker/executors/everyone_active_book` — Python script, `#!/usr/bin/env python3`, mode `0755`. Runs as `donna-broker` user.

**Input contract:**

- **stdin:** JSON `{"capability": "everyone_active.book_class", "params": {...}}`
- **DONNA_CREDS_FD:** fd number → read to EOF → JSON bytes `{"email": "...", "password": "..."}`

**Output contract:**

- **stdout:** JSON result object (success or error shape, see §7)
- **exit code:** `0` = success, non-zero = failure
- **stderr:** diagnostic only, never parsed by broker (hashed for audit)

**Dependencies:** `playwright` Python package + Chromium, installed under `donna-broker`'s home directory.

## 4. Parameter schema

File: `broker/manifests/schemas/everyone_active_book.json`

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "$id": "donna://schemas/everyone_active_book.json",
  "title": "everyone_active.book_class params",
  "type": "object",
  "additionalProperties": false,
  "required": ["activity_name", "centre", "date"],
  "properties": {
    "activity_name": {
      "type": "string",
      "description": "Activity name as shown on EA timetable (e.g. 'Gym Session', 'Lane Swimming')"
    },
    "centre": {
      "type": "string",
      "enum": ["chesham", "chilterns"]
    },
    "date": {
      "type": "string",
      "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
      "description": "ISO date in Europe/London calendar day terms"
    },
    "start_time": {
      "type": "string",
      "pattern": "^[0-2][0-9]:[0-5][0-9]$",
      "description": "If omitted, books the first matching activity on that date"
    },
    "allow_waitlist": {
      "type": "boolean",
      "default": true,
      "description": "If true, adds to waitlist when class is full. If false, fails with class_full."
    }
  }
}
```

Changes from the original schema: `class_id` → `activity_name` (free text), `centre_id` → `centre` (enum), added `allow_waitlist`.

## 5. Credential format

Vault entry: `/Users/donna-broker/.config/donna/creds/everyone_active.age`

Plaintext shape (age-encrypted at rest):

```json
{"email": "...", "password": "..."}
```

Created via `age -r` with the broker's identity public key. Permissions `0440`, owner `donna-broker`.

## 6. Booking flow

Single-file Python script, linear Playwright flow, headless Chromium. Launch and kill browser on every invocation — no session reuse.

### Step 1 — Read inputs

- Parse stdin JSON for params
- Read `DONNA_CREDS_FD` to EOF, parse as JSON for email + password
- Close the creds fd immediately

### Step 2 — Login

- Navigate to `https://account.everyoneactive.com/login`
- Fill `#emailAddress` with email, `#password` with password
- Click `button[type="submit"]`
- Wait for redirect to `book.everyoneactive.com/Connect/memberHomePage.aspx`
- If login fails (stays on login page or error element appears): exit with `login_failed`

### Step 3 — Set search criteria

Centre mapping (hardcoded):

| `centre` param | Dropdown value | Display name |
|---|---|---|
| `chesham` | `0243` | Chesham Leisure Centre |
| `chilterns` | `0255` | Chilterns Lifestyle Centre |

Sequence:

1. Select centre from `#ctl00_MainContent__advanceSearchUserControl_SitesSimple` — wait for `__doPostBack` page reload to complete
2. Set date on `#ctl00_MainContent__advanceSearchUserControl_specificDate` — the HTML `type="Date"` input accepts `YYYY-MM-DD` natively, so pass the ISO date string directly
3. Select activity from `#ctl00_MainContent__advanceSearchUserControl_ActivityGroups` — case-insensitive substring match of `activity_name` against option text. If multiple options match, exit with `class_not_found` and list the ambiguous matches in the detail so Donna can retry with a more specific name
4. If no matching activity in dropdown: exit with `class_not_found`, detail "activity not found in dropdown for this centre"

### Step 4 — Search

- Click the search/postback trigger to submit the timetable query
- Wait for navigation to `mrmClassStatus.aspx`

### Step 5 — Select class

- Parse `data-qa-id` attributes on book buttons to find matching row
- If `start_time` provided: match against the time in `data-qa-id` (`Date=DD/MM/YYYY HH:MM:SS`)
- If `start_time` omitted: take the first available result
- If no matching row: exit with `class_not_found`
- Check button class: `btn-success` = available, `btn-wait` = waitlist only
- If waitlist and `allow_waitlist` is `false`: exit with `class_full`
- Click the book/waitlist button

### Step 6 — Confirm

- Wait for `mrmConfirmBooking.aspx`
- If a payment step appears instead: exit with `booking_rejected`, detail "payment step encountered — not yet supported"
- Click `#ctl00_MainContent_btnBasket` (the "Book" confirmation button)
- Wait for `mrmBookingConfirmed.aspx`
- If confirmation page shows an error: exit with `booking_rejected`

### Step 7 — Scrape result and exit

- Scrape confirmation page for all available fields
- Write JSON result to stdout (see §7)
- Exit 0

## 7. Output shapes

### Success (exit 0)

```json
{
  "status": "booked",
  "waitlisted": false,
  "activity_name": "Gym Session",
  "centre": "Chesham Leisure Centre",
  "date": "2026-05-01",
  "start_time": "07:30",
  "duration_minutes": 60,
  "spaces_remaining": 8,
  "booking_reference": "...",
  "confirmation_text": "Thank you for your booking. You will receive a booking confirmation email shortly."
}
```

When waitlisted: `"status": "waitlisted"`, `"waitlisted": true`, `spaces_remaining` omitted or `0`.

### Failure (exit 1)

```json
{
  "error_code": "class_full",
  "detail": "Gym Session - 0 spaces remaining, waitlist available but allow_waitlist=false"
}
```

### Error taxonomy

| Code | Meaning |
|---|---|
| `login_failed` | Credentials rejected at account.everyoneactive.com |
| `class_not_found` | Search completed but no matching activity/time on results page |
| `class_full` | Class found but full, and `allow_waitlist` was `false` |
| `booking_rejected` | Confirmation page showed an error, or unexpected payment step |
| `session_expired` | ASP.NET VIEWSTATE expired mid-flow |
| `site_unavailable` | EA site didn't load or returned 5xx |
| `unexpected_dom` | Page structure doesn't match expected selectors (site redesign) |

All error results include a `detail` string — either scraped from the page or generated by the executor to explain what happened.

## 8. Manifest changes

### `capabilities.yaml` update

```yaml
- name: everyone_active.book_class
  executor:
    type: subprocess
    binary: /Users/donna-broker/broker/executors/everyone_active_book
    timeout_seconds: 120
  creds:
    delivery: fd3
    entry: everyone_active
  param_schema:
    $ref: ./schemas/everyone_active_book.json
  params_exact_match_required: true
  derived_fields_allowed: []
  risk_level: medium
  revalidate:
    not_applicable: no_external_state
  idempotency_date_from: params.date
  approval_window_minutes: 120
  execution_window_minutes: 60
```

Changes: added `creds:` block, replaced `revalidate.handler` with `not_applicable`.

## 9. Post-execution behaviour (Piece E)

After a successful `everyone_active.book_class` execution, Donna checks Graham's Gmail for the Everyone Active booking confirmation email within a couple of minutes and confirms to Graham that it came through. This rule lands with the Phase 2 rule gate in Piece E.

## 10. Future extensions (not in scope)

- **Payment-required classes:** Handle the payment confirmation step for classes that cost extra. Currently fails cleanly with `booking_rejected`.
- **Cancellation executor:** `everyone_active.cancel_class` — separate capability, separate binary.
- **Class search executor:** `everyone_active.list_classes` — returns available slots without booking.
- **Monitoring GUI:** Dashboard for executor success rates, recon gaps, and execution history.
- **Additional centres:** Extend the centre mapping when Graham's gym membership changes.
- **Shared utilities extraction:** When the second browser executor arrives, extract common patterns (creds reading, browser launch, result formatting) into a shared module.
