# Piece D — Everyone Active Executor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the `everyone_active_book` executor binary — a standalone Python/Playwright script that the broker spawns to book gym classes at Everyone Active.

**Architecture:** Single-file Python script at `broker/executors/everyone_active_book`. Reads JSON params from stdin, credentials from `DONNA_CREDS_FD` pipe, launches headless Chromium, navigates the EA booking flow (login → advanced search → select class → confirm), writes structured JSON result to stdout. Deployed to `/Users/donna-broker/broker/executors/everyone_active_book`.

**Tech Stack:** Python 3, Playwright (sync API), headless Chromium. Runs as `donna-broker` user under the broker's subprocess executor framework.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `broker/executors/everyone_active_book` | Create | The executor script — standalone Python, `#!/usr/bin/env python3`, mode 0755 |
| `broker/manifests/schemas/everyone_active_book.json` | Modify | Updated schema: `activity_name`, `centre` (enum), `allow_waitlist` |
| `broker/manifests/capabilities.yaml` | Modify | Add `creds:` block, change `revalidate` to `not_applicable` |

---

### Task 1: Update parameter schema

**Files:**
- Modify: `broker/manifests/schemas/everyone_active_book.json`

- [ ] **Step 1: Replace schema with updated fields**

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
      "description": "Activity type as shown on EA timetable (e.g. 'Swimming Sessions', 'Adult Activities')"
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

- [ ] **Step 2: Verify schema parses as valid JSON Schema Draft-07**

Run: `python3 -c "import json; from jsonschema import Draft7Validator; Draft7Validator.check_schema(json.load(open('broker/manifests/schemas/everyone_active_book.json')))"`
Expected: no output (success)

- [ ] **Step 3: Commit**

```bash
git add broker/manifests/schemas/everyone_active_book.json
git commit -m "schema: update everyone_active_book — activity_name, centre enum, allow_waitlist"
```

---

### Task 2: Update capabilities manifest

**Files:**
- Modify: `broker/manifests/capabilities.yaml` (lines 111-134)

- [ ] **Step 1: Replace the everyone_active.book_class entry**

Replace lines 111-134 with:

```yaml
  # ---- Everyone Active (Phase 2 — browser executor) ----------------------------

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

- [ ] **Step 2: Verify manifest loads cleanly**

Run from broker venv:
```bash
cd broker && python3 -c "from broker.validator import load_capabilities; caps = load_capabilities('manifests/capabilities.yaml'); print(f'{len(caps)} capabilities loaded'); ea = [c for c in caps if c.name == 'everyone_active.book_class'][0]; print(f'creds: {ea.creds}'); print(f'revalidate: {ea.revalidate}')"
```
Expected: capabilities count, creds block with `delivery=fd3, entry=everyone_active`, revalidate with `not_applicable=no_external_state`.

- [ ] **Step 3: Commit**

```bash
git add broker/manifests/capabilities.yaml
git commit -m "manifest: everyone_active.book_class — add creds block, revalidate not_applicable"
```

---

### Task 3: Write executor script — input handling and error framework

**Files:**
- Create: `broker/executors/everyone_active_book`

- [ ] **Step 1: Create the executors directory**

```bash
mkdir -p broker/executors
```

- [ ] **Step 2: Write the full executor script**

Create `broker/executors/everyone_active_book` with the complete implementation:

```python
#!/usr/bin/env python3
"""Everyone Active class-booking executor.

Subprocess contract: reads JSON from stdin, credentials from DONNA_CREDS_FD
pipe, writes JSON result to stdout. Exit 0 = success, exit 1 = failure.

Spec: security-v1.1 §8 (capability executor), §17 (Phase 2 browser executor).
Design: docs/superpowers/specs/2026-04-29-piece-d-ea-executor-design.md
"""
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

CENTRES = {
    "chesham": ("0243", "Chesham Leisure Centre"),
    "chilterns": ("0255", "Chilterns Lifestyle Centre"),
}

# Selectors — derived from donna-recon sessions 2026-04-22 and 2026-04-29.
# Advanced search section on memberHomePage.aspx:
SEL_CENTRE = "#ctl00_MainContent__advanceSearchUserControl_SitesAdvanced"
SEL_ACTIVITY_TYPE = "#ctl00_MainContent__advanceSearchUserControl_ActivityGroups"
SEL_DATE_FROM = "#ctl00_MainContent__advanceSearchUserControl_startDate"
SEL_DATE_TO = "#ctl00_MainContent__advanceSearchUserControl_endDate"
SEL_SEARCH_BTN = "#ctl00_MainContent__advanceSearchUserControl__searchBtn"
# Confirm/confirmed pages:
SEL_CONFIRM_BOOK = "#ctl00_MainContent_btnBasket"


def fail(error_code: str, detail: str) -> None:
    json.dump({"error_code": error_code, "detail": detail}, sys.stdout)
    sys.stdout.flush()
    sys.exit(1)


def read_creds() -> dict:
    fd_str = os.environ.get("DONNA_CREDS_FD")
    if fd_str is None:
        fail("login_failed", "DONNA_CREDS_FD not set")
    fd = int(fd_str)
    data = b""
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        data += chunk
    os.close(fd)
    return json.loads(data)


def read_params() -> dict:
    request = json.load(sys.stdin)
    return request["params"]


def login(page, creds: dict) -> None:
    try:
        page.goto("https://account.everyoneactive.com/login", timeout=30000)
    except PwTimeout:
        fail("site_unavailable", "login page did not load within 30s")

    page.fill("#emailAddress", creds["email"])
    page.fill("#password", creds["password"])
    page.click("button[type='submit']")

    try:
        page.wait_for_url("**/memberHomePage.aspx", timeout=30000)
    except PwTimeout:
        error_el = page.query_selector(".validation-summary-errors, .field-validation-error, .error-message")
        detail = error_el.text_content().strip() if error_el else "login page did not redirect"
        fail("login_failed", detail)


def expand_advanced_search(page) -> None:
    panel_heading = page.query_selector("[data-target='#collapseAdv']")
    if panel_heading is None:
        fail("unexpected_dom", "advanced search panel heading not found")
    panel = page.query_selector("#collapseAdv")
    if panel and "in" not in (panel.get_attribute("class") or ""):
        panel_heading.click()
        page.wait_for_timeout(500)


def search_classes(page, centre_code: str, activity_name: str, date: str) -> None:
    expand_advanced_search(page)

    # Select centre — triggers __doPostBack, reloads activity dropdowns.
    page.select_option(SEL_CENTRE, value=centre_code)
    page.wait_for_load_state("networkidle")

    # Match activity type from dropdown options (case-insensitive substring).
    options = page.query_selector_all(f"{SEL_ACTIVITY_TYPE} option")
    matches = []
    for opt in options:
        text = (opt.text_content() or "").strip()
        val = opt.get_attribute("value") or ""
        if val == "":
            continue
        if activity_name.lower() in text.lower():
            matches.append((val, text))

    if not matches:
        available = [
            (opt.text_content() or "").strip()
            for opt in options
            if (opt.get_attribute("value") or "") != ""
        ]
        fail("class_not_found",
             f"activity '{activity_name}' not found in dropdown. "
             f"Available: {', '.join(available)}")
    if len(matches) > 1:
        names = [m[1] for m in matches]
        fail("class_not_found",
             f"ambiguous activity name '{activity_name}', "
             f"matches: {', '.join(names)}")

    page.select_option(SEL_ACTIVITY_TYPE, value=matches[0][0])
    page.wait_for_load_state("networkidle")

    # Set from and to date to the same day.
    page.fill(SEL_DATE_FROM, date)
    page.fill(SEL_DATE_TO, date)

    # Click search.
    page.click(SEL_SEARCH_BTN)
    try:
        page.wait_for_url("**/mrmClassStatus.aspx", timeout=30000)
    except PwTimeout:
        fail("class_not_found", "search did not navigate to results page")


def select_and_book(page, start_time: str | None, allow_waitlist: bool) -> dict:
    buttons = page.query_selector_all("input[data-qa-id^='button-ActivityID=']")
    if not buttons:
        fail("class_not_found", "no bookable classes found in search results")

    target = None
    target_qa_id = ""
    for btn in buttons:
        qa_id = btn.get_attribute("data-qa-id") or ""
        if start_time:
            time_match = re.search(r"Date=\d{2}/\d{2}/\d{4}\s+(\d{2}:\d{2})", qa_id)
            if time_match and time_match.group(1) == start_time:
                target = btn
                target_qa_id = qa_id
                break
        else:
            target = btn
            target_qa_id = qa_id
            break

    if not target:
        fail("class_not_found", f"no class found at {start_time}")

    # Parse metadata from data-qa-id.
    btn_class = target.get_attribute("class") or ""
    is_waitlist = "btn-wait" in btn_class
    btn_value = target.get_attribute("value") or ""

    if is_waitlist and not allow_waitlist:
        fail("class_full", f"{btn_value} — waitlist available but allow_waitlist=false")

    # Extract fields from data-qa-id for the result.
    duration_match = re.search(r"Duration=(\d+)", target_qa_id)
    date_match = re.search(r"Date=(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2})", target_qa_id)

    # Try to read spaces remaining from the sibling availability button.
    spaces = None
    row = target.evaluate_handle("el => el.closest('tr') || el.parentElement")
    if row:
        avail_btn = row.as_element().query_selector("input[id$='_btnAvaliable']")
        if avail_btn:
            avail_text = avail_btn.get_attribute("value") or ""
            spaces_match = re.search(r"(\d+)\s+space", avail_text)
            if spaces_match:
                spaces = int(spaces_match.group(1))

    # Click book.
    target.click()

    # Wait for confirmation page.
    try:
        page.wait_for_url("**/mrmConfirmBooking.aspx", timeout=30000)
    except PwTimeout:
        fail("session_expired", "did not reach confirmation page")

    # Check for unexpected payment page.
    if page.query_selector("input[id*='payment'], input[id*='Payment'], .payment-form"):
        fail("booking_rejected", "payment step encountered — not yet supported")

    # Confirm the booking.
    confirm_btn = page.query_selector(SEL_CONFIRM_BOOK)
    if not confirm_btn:
        fail("unexpected_dom", "confirm booking button not found")
    confirm_btn.click()

    # Wait for confirmed page.
    try:
        page.wait_for_url("**/mrmBookingConfirmed.aspx", timeout=30000)
    except PwTimeout:
        fail("booking_rejected", "did not reach booking confirmed page")

    # Scrape confirmation.
    confirmation_text = ""
    conf_el = page.query_selector("#ctl00_MainContent_lblConfirmationMessage, .confirmation-message, h1 + p strong")
    if conf_el:
        confirmation_text = conf_el.text_content().strip()

    # Build ISO date from DD/MM/YYYY if we parsed it.
    iso_date = ""
    matched_time = ""
    if date_match:
        d, m, y = date_match.group(1).split("/")
        iso_date = f"{y}-{m}-{d}"
        matched_time = date_match.group(2)

    result = {
        "status": "waitlisted" if is_waitlist else "booked",
        "waitlisted": is_waitlist,
        "date": iso_date,
        "start_time": matched_time,
        "duration_minutes": int(duration_match.group(1)) if duration_match else None,
        "spaces_remaining": spaces if not is_waitlist else None,
        "confirmation_text": confirmation_text,
    }
    return result


def main() -> None:
    params = read_params()
    creds = read_creds()

    activity_name = params["activity_name"]
    centre = params["centre"]
    date = params["date"]
    start_time = params.get("start_time")
    allow_waitlist = params.get("allow_waitlist", True)

    centre_info = CENTRES.get(centre)
    if not centre_info:
        fail("class_not_found", f"unknown centre: {centre}")
    centre_code, centre_display = centre_info

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        try:
            login(page, creds)
            search_classes(page, centre_code, activity_name, date)
            result = select_and_book(page, start_time, allow_waitlist)
            result["activity_name"] = activity_name
            result["centre"] = centre_display
            json.dump(result, sys.stdout)
            sys.stdout.flush()
        except SystemExit:
            raise
        except Exception as exc:
            fail("unexpected_dom", f"{type(exc).__name__}: {exc}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Make the script executable**

```bash
chmod +x broker/executors/everyone_active_book
```

- [ ] **Step 4: Verify syntax**

```bash
python3 -c "import py_compile; py_compile.compile('broker/executors/everyone_active_book', doraise=True)"
```
Expected: no output (success)

- [ ] **Step 5: Commit**

```bash
git add broker/executors/everyone_active_book
git commit -m "feat: everyone_active_book executor — Playwright booking automation"
```

---

### Task 4: Set up donna-broker Python environment

This task requires Graham to run commands as himself or via sudo. The executor needs Playwright and Chromium installed for the `donna-broker` user.

**Files:** None (system configuration)

- [ ] **Step 1: Create the executors directory on the target path**

```bash
sudo -u donna-broker mkdir -p /Users/donna-broker/broker/executors
```

- [ ] **Step 2: Install Playwright for donna-broker**

Check if donna-broker has a Python venv or uses system Python:

```bash
sudo -u donna-broker python3 --version
sudo -u donna-broker python3 -c "import playwright" 2>&1
```

If playwright is not installed:

```bash
sudo -u donna-broker python3 -m pip install playwright
sudo -u donna-broker python3 -m playwright install chromium
```

- [ ] **Step 3: Verify Playwright and Chromium work**

```bash
sudo -u donna-broker python3 -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); b = p.chromium.launch(headless=True); b.close(); p.stop(); print('OK')"
```
Expected: `OK`

---

### Task 5: Create credential vault entry

**Files:** None (vault configuration)

- [ ] **Step 1: Encrypt the Everyone Active credentials**

Graham provides his EA email and password. Encrypt them:

```bash
echo '{"email":"<EA_EMAIL>","password":"<EA_PASSWORD>"}' | sudo -u donna-broker age -r "$(sudo -u donna-broker cat /Users/donna-broker/.config/donna/creds/identity.age.pub 2>/dev/null || echo 'NEED_PUBLIC_KEY')" -o /Users/donna-broker/.config/donna/creds/everyone_active.age
```

If the identity doesn't have a separate `.pub` file, extract the public key:

```bash
sudo -u donna-broker age-keygen -y /Users/donna-broker/.config/donna/creds/identity.age
```

Then use that public key with `-r`.

- [ ] **Step 2: Set permissions**

```bash
sudo chown donna-broker:donna-broker /Users/donna-broker/.config/donna/creds/everyone_active.age
sudo chmod 0440 /Users/donna-broker/.config/donna/creds/everyone_active.age
```

- [ ] **Step 3: Verify vault entry passes health check**

```bash
sudo -u donna-broker /usr/local/bin/donna-broker verify-vault
```

Expected: `everyone_active.age` shows `OK` on all checks.

- [ ] **Step 4: Test decryption round-trip**

```bash
sudo -u donna-broker age --decrypt -i /Users/donna-broker/.config/donna/creds/identity.age /Users/donna-broker/.config/donna/creds/everyone_active.age | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'email: {d[\"email\"][:3]}...')"
```

Expected: first 3 characters of the email address (proves decrypt works without showing full creds).

---

### Task 6: Deploy executor and manifest

**Files:** None (deployment)

- [ ] **Step 1: Copy executor to donna-broker path**

```bash
sudo cp broker/executors/everyone_active_book /Users/donna-broker/broker/executors/everyone_active_book
sudo chown donna-broker:donna-broker /Users/donna-broker/broker/executors/everyone_active_book
sudo chmod 0755 /Users/donna-broker/broker/executors/everyone_active_book
```

- [ ] **Step 2: Deploy updated manifest and schema**

Use the existing deploy script:

```bash
sudo broker/deploy-manifests.sh
```

Or manually:

```bash
sudo cp broker/manifests/capabilities.yaml /Users/donna-broker/.config/donna/capabilities.yaml
sudo cp broker/manifests/schemas/everyone_active_book.json /Users/donna-broker/.config/donna/schemas/everyone_active_book.json
sudo chown donna-broker:donna-broker /Users/donna-broker/.config/donna/capabilities.yaml /Users/donna-broker/.config/donna/schemas/everyone_active_book.json
```

- [ ] **Step 3: Verify manifest loads from deployed path**

```bash
sudo -u donna-broker /usr/local/bin/donna-broker verify-manifests
```

Expected: clean output, no errors.

---

### Task 7: Manual end-to-end test

**Files:** None (testing)

- [ ] **Step 1: Dry-run the executor directly**

Create a test input file and run the executor outside the broker framework to verify it can log in and navigate:

```bash
# Write test input (don't do this with real creds — pipe them)
echo '{"capability":"everyone_active.book_class","params":{"activity_name":"Swimming Sessions","centre":"chesham","date":"2026-05-01"}}' > /tmp/ea-test-input.json

# Decrypt creds and pipe to executor via fd 3
sudo -u donna-broker bash -c '
  exec 3< <(age --decrypt -i /Users/donna-broker/.config/donna/creds/identity.age /Users/donna-broker/.config/donna/creds/everyone_active.age)
  DONNA_CREDS_FD=3 python3 /Users/donna-broker/broker/executors/everyone_active_book < /tmp/ea-test-input.json
'
```

Expected: JSON output with either a booking result or a structured error (e.g., `class_not_found` if no swimming sessions on that date).

- [ ] **Step 2: Test via broker approval flow**

From a Donna session, ask to book a class. Donna should:
1. Call `donna-broker request` with `capability: everyone_active.book_class`
2. Graham approves via Telegram
3. Donna calls `donna-broker execute` with the approval code
4. Broker spawns the executor, passes creds via pipe
5. Executor books the class
6. Donna reports the result

This exercises the full end-to-end path including credential injection.

- [ ] **Step 3: Verify error paths**

Test each error code manually:
- `login_failed`: temporarily set wrong password in vault (or use a test-only creds entry)
- `class_not_found`: search for an activity name that doesn't exist (e.g., `"Underwater Basket Weaving"`)
- `class_full` with `allow_waitlist: false`: find a full class and set `allow_waitlist` to false
- `site_unavailable`: disconnect network mid-run (or test with a bad URL)

- [ ] **Step 4: Commit any fixes from testing**

```bash
git add -A
git commit -m "fix: executor adjustments from manual testing"
```
