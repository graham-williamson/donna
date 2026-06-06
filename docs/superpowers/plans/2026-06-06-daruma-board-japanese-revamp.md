# Daruma Board Japanese Revamp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the Daruma Board frontend as a beautiful, calming Japanese dashboard — three switchable themes (washi 和 / sakura 桜 light, twilight temple 月 dark), per-theme ambient animation, authentic daruma artwork, an ema wish wall, a tokonoma wins alcove, on-board add forms, achieve celebrations, 72 micro-seasons, a daily zen line, and an incense focus timer.

**Architecture:** `tools/dashboard.py` stays the zero-dependency stdlib server; frontend assets move to `tools/dashboard_assets/` (style.css, board.js, zen.json) served by a new sanitised `/static/<name>` route. A new `wishes.py` engine owns `wishes.json` (ema wall) and promotes wishes into goals via `goals.add_goal`. Existing engines are untouched.

**Tech Stack:** Python 3 stdlib (`json`, `http.server`, `urllib`), vanilla CSS/JS (CSS custom properties per theme, canvas particles, WebAudio bell, `offset-path` koi), pytest.

**Spec:** `docs/superpowers/specs/2026-06-06-daruma-board-japanese-revamp-design.md`

## File structure

- Create: `personal-system/tools/wishes.py` — ema wish engine (wishes.json: add / promote→goal / release, never delete)
- Create: `personal-system/tools/dashboard_assets/zen.json` — daily zen/haiku lines
- Create: `personal-system/tools/dashboard_assets/style.css` — base layout + 3 theme variable blocks
- Create: `personal-system/tools/dashboard_assets/board.js` — theme switch, particles, koi, celebration, incense, auto-refresh
- Modify: `personal-system/tools/dashboard.py` — render revamp (top half) + routes/server (bottom half)
- Test: `personal-system/tests/test_wishes.py`, extend `personal-system/tests/test_dashboard.py`
- Data (runtime, gitignored): `personal-system/_shared/_state/wishes.json`

All commands below run from `/Users/grahamwilliamson/donna/personal-system` unless stated. Commit from repo root `/Users/grahamwilliamson/donna`.

---

### Task 1: `wishes.py` — the ema wall engine

**Files:**
- Create: `personal-system/tools/wishes.py`
- Test: `personal-system/tests/test_wishes.py`

- [ ] **Step 1: Write the failing tests**

Create `personal-system/tests/test_wishes.py`:

```python
import importlib.util
import pathlib
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_add_wish_is_open(tmp_path):
    w = load("wishes").add_wish("see the cherry blossoms in Kyoto",
                                wishes_path=str(tmp_path / "w.json"))
    assert w["status"] == "open" and w["promoted_goal_id"] is None and w["created_at"]


def test_promote_creates_goal_and_links(tmp_path):
    wishes = load("wishes")
    wp, gp = str(tmp_path / "w.json"), str(tmp_path / "g.json")
    w = wishes.add_wish("learn the tea ceremony", wishes_path=wp)
    goal = wishes.promote_wish(w["id"], "purple", wishes_path=wp, goals_path=gp)
    assert goal["title"] == "learn the tea ceremony" and goal["colour"] == "purple"
    promoted = wishes.list_wishes(status="promoted", wishes_path=wp)
    assert promoted[0]["promoted_goal_id"] == goal["id"]


def test_promote_bad_colour_leaves_wish_open(tmp_path):
    wishes = load("wishes")
    wp = str(tmp_path / "w.json")
    w = wishes.add_wish("x", wishes_path=wp)
    with pytest.raises(ValueError):
        wishes.promote_wish(w["id"], "teal", wishes_path=wp,
                            goals_path=str(tmp_path / "g.json"))
    assert wishes.list_wishes(wishes_path=wp)[0]["status"] == "open"


def test_release_archives_not_deletes(tmp_path):
    wishes = load("wishes")
    wp = str(tmp_path / "w.json")
    w = wishes.add_wish("x", wishes_path=wp)
    wishes.release_wish(w["id"], wishes_path=wp)
    assert wishes.list_wishes(wishes_path=wp) == []
    assert wishes.list_wishes(status=None, wishes_path=wp)[0]["status"] == "released"


def test_seed_daru_once(tmp_path):
    wishes = load("wishes")
    wp = str(tmp_path / "w.json")
    assert len(wishes.seed_defaults(wishes_path=wp)) == 1
    assert wishes.seed_defaults(wishes_path=wp) == []
    assert "daru.life" in wishes.list_wishes(wishes_path=wp)[0]["text"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_wishes.py -v`
Expected: FAIL — `FileNotFoundError` loading `tools/wishes.py`

- [ ] **Step 3: Implement `wishes.py`**

Create `personal-system/tools/wishes.py`:

```python
#!/usr/bin/env python3
"""Ema wish wall — deferred dreams (wishes.json).

At a shrine you write a wish on a wooden ema plaque and hang it up. Here a
wish hangs on the wall until you either promote it to a daruma (it becomes a
goal — you commit) or release it. Nothing is deleted.
"""
import os
import json
import pathlib
import argparse
import importlib.util
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_WISHES_PATH = os.environ.get("WISHES_PATH", str(ROOT / "_shared" / "_state" / "wishes.json"))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"wishes": []}


def _save(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def add_wish(text, wishes_path=None):
    path = wishes_path or DEFAULT_WISHES_PATH
    data = _load(path)
    wid = max([w["id"] for w in data["wishes"]], default=0) + 1
    wish = {"id": wid, "text": text, "created_at": now_iso(),
            "status": "open", "promoted_goal_id": None}
    data["wishes"].append(wish)
    _save(path, data)
    return wish


def _find(data, wid):
    for w in data["wishes"]:
        if w["id"] == wid:
            return w
    raise KeyError(wid)


def _goals():
    spec = importlib.util.spec_from_file_location("goals", ROOT / "tools" / "goals.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def promote_wish(wid, colour, wishes_path=None, goals_path=None):
    path = wishes_path or DEFAULT_WISHES_PATH
    data = _load(path)
    w = _find(data, wid)
    goal = _goals().add_goal(w["text"], colour, why_it_matters="from the ema wall",
                             goals_path=goals_path)
    w["status"] = "promoted"
    w["promoted_goal_id"] = goal["id"]
    _save(path, data)
    return goal


def release_wish(wid, wishes_path=None):
    path = wishes_path or DEFAULT_WISHES_PATH
    data = _load(path)
    w = _find(data, wid)
    w["status"] = "released"
    _save(path, data)
    return w


def list_wishes(status="open", wishes_path=None):
    ws = _load(wishes_path or DEFAULT_WISHES_PATH)["wishes"]
    return [w for w in ws if status is None or w["status"] == status]


DARU = "Daru (daru.life) — internet-friendly SaaS version of the Daruma Board"


def seed_defaults(wishes_path=None):
    path = wishes_path or DEFAULT_WISHES_PATH
    if any(w["text"] == DARU for w in list_wishes(status=None, wishes_path=path)):
        return []
    return [add_wish(DARU, wishes_path=path)]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="wishes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("text")
    p = sub.add_parser("promote")
    p.add_argument("id", type=int)
    p.add_argument("colour")
    r = sub.add_parser("release")
    r.add_argument("id", type=int)
    ls = sub.add_parser("list")
    ls.add_argument("--all", action="store_true")
    sub.add_parser("seed")
    args = ap.parse_args(argv)
    if args.cmd == "add":
        print(json.dumps(add_wish(args.text)))
    elif args.cmd == "promote":
        print(json.dumps(promote_wish(args.id, args.colour)))
    elif args.cmd == "release":
        print(json.dumps(release_wish(args.id)))
    elif args.cmd == "list":
        print(json.dumps(list_wishes(status=None if args.all else "open"), indent=2))
    elif args.cmd == "seed":
        print(json.dumps(seed_defaults()))


if __name__ == "__main__":
    main()
```

Note: `promote_wish` calls `goals.add_goal` *before* mutating the wish, so an invalid colour raises `ValueError` and leaves the wish open (test 3 depends on this ordering).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_wishes.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/grahamwilliamson/donna
git add personal-system/tools/wishes.py personal-system/tests/test_wishes.py
git commit -m "feat(wishes): ema wish wall engine — add/promote/release, Daru seed"
```

---

### Task 2: Seasons + daily zen data

**Files:**
- Create: `personal-system/tools/dashboard_assets/zen.json`
- Modify: `personal-system/tools/dashboard.py` (imports + insert after the `LABEL = ...` line)
- Test: `personal-system/tests/test_dashboard.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `personal-system/tests/test_dashboard.py`:

```python
from datetime import date


def test_season_for_known_dates():
    dash = load_dashboard()
    assert dash.season_for(date(2026, 6, 12))["kanji"] == "腐草為螢"
    assert dash.season_for(date(2026, 1, 2))["kanji"] == "雪下出麦"  # wraps to year end
    assert dash.season_for(date(2026, 3, 27))["english"] == "First cherry blossoms"


def test_zen_deterministic_per_day():
    dash = load_dashboard()
    a = dash.zen_for(date(2026, 6, 6))
    assert a == dash.zen_for(date(2026, 6, 6))
    assert a["text"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_dashboard.py -v -k "season or zen"`
Expected: FAIL — `AttributeError: module 'dashboard' has no attribute 'season_for'`

- [ ] **Step 3: Create `zen.json`**

Create `personal-system/tools/dashboard_assets/zen.json`:

```json
[
  {"text": "七転び八起き — fall down seven times, get up eight.", "by": "Japanese proverb"},
  {"text": "An old pond — a frog leaps in, the sound of water.", "by": "Matsuo Bashō"},
  {"text": "O snail, climb Mount Fuji, but slowly, slowly!", "by": "Kobayashi Issa"},
  {"text": "Sitting quietly, doing nothing, spring comes, and the grass grows by itself.", "by": "Zenrin-kushū"},
  {"text": "The temple bell stops — but the sound keeps coming out of the flowers.", "by": "Matsuo Bashō"},
  {"text": "Barn's burnt down — now I can see the moon.", "by": "Mizuta Masahide"},
  {"text": "In the beginner's mind there are many possibilities, but in the expert's there are few.", "by": "Shunryū Suzuki"},
  {"text": "If you cannot find the truth right where you are, where else do you expect to find it?", "by": "Dōgen"},
  {"text": "The bamboo that bends is stronger than the oak that resists.", "by": "Japanese proverb"},
  {"text": "This dewdrop world is a dewdrop world — and yet, and yet.", "by": "Kobayashi Issa"},
  {"text": "The thief left it behind: the moon at my window.", "by": "Ryōkan"},
  {"text": "Confine yourself to the present.", "by": "Marcus Aurelius"},
  {"text": "No great thing is created suddenly.", "by": "Epictetus"},
  {"text": "It is not that we have a short time to live, but that we waste a lot of it.", "by": "Seneca"},
  {"text": "When you have completed 95 percent of your journey, you are only halfway there.", "by": "Japanese proverb"},
  {"text": "Before enlightenment: chop wood, carry water. After enlightenment: chop wood, carry water.", "by": "Zen saying"},
  {"text": "Even monkeys fall from trees.", "by": "Japanese proverb"},
  {"text": "The day you decide to do it is your lucky day.", "by": "Japanese proverb"},
  {"text": "A journey of a thousand miles begins with a single step.", "by": "Laozi"},
  {"text": "Each of you is perfect the way you are — and you can use a little improvement.", "by": "Shunryū Suzuki"},
  {"text": "When walking, walk. When eating, eat.", "by": "Zen saying"},
  {"text": "Winter solitude — in a world of one colour, the sound of wind.", "by": "Matsuo Bashō"},
  {"text": "In this world we walk on the roof of hell, gazing at flowers.", "by": "Kobayashi Issa"},
  {"text": "Nothing lasts, nothing is finished, nothing is perfect.", "by": "Wabi-sabi"},
  {"text": "The impediment to action advances action. What stands in the way becomes the way.", "by": "Marcus Aurelius"},
  {"text": "One kind word can warm three winter months.", "by": "Japanese proverb"},
  {"text": "Morning glory! The well bucket entangled, I ask for water.", "by": "Chiyo-ni"},
  {"text": "Let go or be dragged.", "by": "Zen saying"},
  {"text": "Every day is a journey, and the journey itself is home.", "by": "Matsuo Bashō"},
  {"text": "Vision without action is a daydream. Action without vision is a nightmare.", "by": "Japanese proverb"}
]
```

- [ ] **Step 4: Add seasons table + helpers to `dashboard.py`**

In `personal-system/tools/dashboard.py`, replace the import block at the top:

```python
import os
import pathlib
import subprocess
import plistlib
import importlib.util
```

with:

```python
import os
import json
import html
import pathlib
import subprocess
import plistlib
import importlib.util
from datetime import date
```

After the `ROOT = pathlib.Path(__file__).resolve().parents[1]` line add:

```python
ASSETS = ROOT / "tools" / "dashboard_assets"
```

Then directly after the `LABEL = "com.user.claude-telegram"` line insert:

```python
# ---- Japan's 72 micro-seasons (kō): (month, day, kanji, english), start dates ----
SEASONS_72 = [
    (1, 5, "芹乃栄", "Parsley flourishes"),
    (1, 10, "水泉動", "Springs thaw"),
    (1, 15, "雉始雊", "Pheasants start to call"),
    (1, 20, "款冬華", "Butterburs bud"),
    (1, 25, "水沢腹堅", "Ice thickens on streams"),
    (1, 30, "鶏始乳", "Hens start laying eggs"),
    (2, 4, "東風解凍", "East wind melts the ice"),
    (2, 9, "黄鶯睍睆", "Bush warblers start singing"),
    (2, 14, "魚上氷", "Fish emerge from the ice"),
    (2, 19, "土脉潤起", "Rain moistens the soil"),
    (2, 24, "霞始靆", "Mist starts to linger"),
    (3, 1, "草木萌動", "Grass sprouts, trees bud"),
    (3, 6, "蟄虫啓戸", "Hibernating insects surface"),
    (3, 11, "桃始笑", "First peach blossoms"),
    (3, 16, "菜虫化蝶", "Caterpillars become butterflies"),
    (3, 21, "雀始巣", "Sparrows start to nest"),
    (3, 26, "桜始開", "First cherry blossoms"),
    (3, 31, "雷乃発声", "Distant thunder"),
    (4, 5, "玄鳥至", "Swallows return"),
    (4, 10, "鴻雁北", "Wild geese fly north"),
    (4, 15, "虹始見", "First rainbows"),
    (4, 20, "葭始生", "First reeds sprout"),
    (4, 25, "霜止出苗", "Last frost, rice seedlings grow"),
    (4, 30, "牡丹華", "Peonies bloom"),
    (5, 5, "蛙始鳴", "Frogs start singing"),
    (5, 10, "蚯蚓出", "Worms surface"),
    (5, 15, "竹笋生", "Bamboo shoots sprout"),
    (5, 21, "蚕起食桑", "Silkworms feast on mulberry"),
    (5, 26, "紅花栄", "Safflowers bloom"),
    (5, 31, "麦秋至", "Wheat ripens"),
    (6, 6, "螳螂生", "Praying mantises hatch"),
    (6, 11, "腐草為螢", "Rotten grass becomes fireflies"),
    (6, 16, "梅子黄", "Plums turn yellow"),
    (6, 21, "乃東枯", "Self-heal withers"),
    (6, 26, "菖蒲華", "Irises bloom"),
    (7, 2, "半夏生", "Crow-dipper sprouts"),
    (7, 7, "温風至", "Warm winds blow"),
    (7, 12, "蓮始開", "First lotus blossoms"),
    (7, 17, "鷹乃学習", "Hawks learn to fly"),
    (7, 23, "桐始結花", "Paulownia produce seeds"),
    (7, 28, "土潤溽暑", "Earth is damp, air is humid"),
    (8, 2, "大雨時行", "Great rains sometimes fall"),
    (8, 7, "涼風至", "Cool winds blow"),
    (8, 12, "寒蝉鳴", "Evening cicadas sing"),
    (8, 17, "蒙霧升降", "Thick fog descends"),
    (8, 23, "綿柎開", "Cotton flowers bloom"),
    (8, 28, "天地始粛", "Heat starts to die down"),
    (9, 2, "禾乃登", "Rice ripens"),
    (9, 7, "草露白", "Dew glistens white on grass"),
    (9, 12, "鶺鴒鳴", "Wagtails sing"),
    (9, 17, "玄鳥去", "Swallows leave"),
    (9, 23, "雷乃収声", "Thunder ceases"),
    (9, 28, "蟄虫坏戸", "Insects hole up underground"),
    (10, 3, "水始涸", "Farmers drain fields"),
    (10, 8, "鴻雁来", "Wild geese return"),
    (10, 13, "菊花開", "Chrysanthemums bloom"),
    (10, 18, "蟋蟀在戸", "Crickets chirp at the door"),
    (10, 23, "霜始降", "First frost"),
    (10, 28, "霎時施", "Light rains sometimes fall"),
    (11, 2, "楓蔦黄", "Maple and ivy turn yellow"),
    (11, 7, "山茶始開", "Camellias bloom"),
    (11, 12, "地始凍", "Land starts to freeze"),
    (11, 17, "金盞香", "Daffodils bloom"),
    (11, 22, "虹蔵不見", "Rainbows hide"),
    (11, 27, "朔風払葉", "North wind blows the leaves away"),
    (12, 2, "橘始黄", "Tachibana citrus turns yellow"),
    (12, 7, "閉塞成冬", "Cold sets in, winter begins"),
    (12, 12, "熊蟄穴", "Bears head into their dens"),
    (12, 16, "鱖魚群", "Salmon gather and swim upstream"),
    (12, 21, "乃東生", "Self-heal sprouts"),
    (12, 26, "麋角解", "Deer shed their antlers"),
    (12, 31, "雪下出麦", "Wheat sprouts under snow"),
]


def season_for(d=None):
    d = d or date.today()
    cur = SEASONS_72[-1]  # before Jan 5 we're still in the year-end kō
    for m, dd, jp, en in SEASONS_72:
        if (m, dd) <= (d.month, d.day):
            cur = (m, dd, jp, en)
    return {"kanji": cur[2], "english": cur[3]}


def zen_for(d=None):
    d = d or date.today()
    lines = json.loads((ASSETS / "zen.json").read_text())
    return lines[d.toordinal() % len(lines)]


def _esc(s):
    return html.escape(str(s or ""), quote=True)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_dashboard.py -v`
Expected: all pass (new + the 5 existing render tests)

- [ ] **Step 6: Commit**

```bash
cd /Users/grahamwilliamson/donna
git add personal-system/tools/dashboard.py personal-system/tools/dashboard_assets/zen.json personal-system/tests/test_dashboard.py
git commit -m "feat(dashboard): 72 micro-seasons + daily zen line"
```

---

### Task 3: Render revamp — daruma art, themes, ema wall, tokonoma, forms, incense

**Files:**
- Modify: `personal-system/tools/dashboard.py` (replace from `def _daruma_svg` through `def render_board` inclusive; the `STYLE` constant is deleted)
- Test: `personal-system/tests/test_dashboard.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `personal-system/tests/test_dashboard.py`:

```python
def test_render_new_sections_and_assets():
    dash = load_dashboard()
    page = dash.render_board(
        [{"id": 1, "title": "x", "colour": "green", "owner": "nike", "daruma_state": "left"}],
        wishes=[{"id": 1, "text": "Daru SaaS", "created_at": "2026-06-06T00:00:00Z", "status": "open"}],
        wins=[{"content": "Achieved goal: Run 10k", "created_at": "2026-06-01T00:00:00Z"}],
        today=date(2026, 6, 12))
    assert 'data-theme-btn="twilight"' in page                      # theme switcher
    assert "絵馬" in page and "Daru SaaS" in page                    # ema wall
    assert "床の間" in page and "Run 10k" in page                    # tokonoma
    assert 'action="/add-goal"' in page and 'action="/add-wish"' in page
    assert 'action="/add-habit"' in page and 'action="/promote-wish"' in page
    assert "/static/style.css" in page and "/static/board.js" in page
    assert "腐草為螢" in page                                        # season subtitle
    assert "incense" in page                                         # focus timer block


def test_render_celebrate_flag_and_escaping():
    dash = load_dashboard()
    page = dash.render_board(
        [{"id": 7, "title": "<script>alert(1)</script>", "colour": "red",
          "owner": "shared", "daruma_state": "none"}],
        celebrate=7, today=date(2026, 6, 12))
    assert 'data-celebrate="7"' in page
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_dashboard.py -v -k "new_sections or celebrate"`
Expected: FAIL — `TypeError: render_board() got an unexpected keyword argument 'wishes'`

- [ ] **Step 3: Replace the render half of `dashboard.py`**

Update the module docstring (first lines of file) to:

```python
#!/usr/bin/env python3
"""Daruma board dashboard — a calm, Japanese-themed local board.

Three themes: washi 和 / sakura 桜 (light) and twilight temple 月 (dark mode).
Goals are daruma with fillable eyes; wishes hang on an ema wall; wins rest in
the tokonoma. Plus habits with streaks, a context-efficiency (token) panel,
an incense focus timer, and a daemon model switcher + restart button.

Launch on demand (NOT a daemon):  python3 tools/dashboard.py
Serves on http://localhost:8765. Reads/writes via goals.py, habits.py,
wishes.py; reads tokens.py and evidence.py; switches the daemon model via the
launchd plist + launchctl. Static assets live in tools/dashboard_assets/.
"""
```

Then delete everything from `def _daruma_svg(colour, state):` through the end of `def render_board(...)` (inclusive — this removes the old `STYLE` constant too) and put this in its place. The `AGENT_GLYPH`/`_models_str`/`_agent_lines`/`_chan_card`/`_token_panel`/`_model_panel` definitions are carried over unchanged inside this block — do not keep a second copy:

```python
def _daruma_svg(colour, state):
    hexc = COLOUR_HEX.get(colour, "#888888")
    left = '<circle cx="38" cy="50" r="4.6" fill="#111"/>' if state in ("left", "both") else ""
    right = '<circle cx="58" cy="50" r="4.6" fill="#111"/>' if state == "both" else ""
    return (
        f'<svg class="daruma" width="96" height="106" viewBox="0 0 96 106" data-state="{state}">'
        # round-bottomed okiagari body
        f'<path d="M48 5 C71 5 89 27 89 59 C89 87 72 99 48 99 C24 99 7 87 7 59 C7 27 25 5 48 5 Z" '
        f'fill="{hexc}" stroke="#241a16" stroke-width="2"/>'
        # soft top light + ground shadow (no gradient ids, safe to repeat per card)
        f'<ellipse cx="38" cy="30" rx="24" ry="16" fill="#fff" opacity=".14"/>'
        f'<ellipse cx="48" cy="88" rx="30" ry="9" fill="#000" opacity=".10"/>'
        # cream face patch
        f'<path d="M48 22 C61 22 69 33 69 47 C69 63 60 72 48 72 C36 72 27 63 27 47 C27 33 35 22 48 22 Z" '
        f'fill="#f6efdd"/>'
        # gold flourishes framing the face
        f'<path d="M28 40 q-5 6 -3 13 M68 40 q5 6 3 13" stroke="#caa64b" stroke-width="2" '
        f'fill="none" opacity=".9"/>'
        # ink brows (crane strokes)
        f'<path d="M30 37 q7 -6 15 -2" stroke="#241a16" stroke-width="3" fill="none" stroke-linecap="round"/>'
        f'<path d="M51 35 q8 -4 15 2" stroke="#241a16" stroke-width="3" fill="none" stroke-linecap="round"/>'
        # eye whites + pupils (the contract: pupils are the only fill="#111")
        f'<ellipse cx="38" cy="50" rx="8" ry="8.6" fill="#fff" stroke="#241a16" stroke-width="1.6"/>'
        f'<ellipse cx="58" cy="50" rx="8" ry="8.6" fill="#fff" stroke="#241a16" stroke-width="1.6"/>'
        f'{left}{right}'
        # nose, moustache (tortoise whiskers), mouth
        f'<path d="M46 56 q2 2 4 0" stroke="#241a16" stroke-width="1.4" fill="none"/>'
        f'<path d="M40 62 q-7 5 -12 3 M56 62 q7 5 12 3" stroke="#241a16" stroke-width="1.8" '
        f'fill="none" stroke-linecap="round"/>'
        f'<path d="M43 67 q5 4 10 0" stroke="#241a16" stroke-width="1.8" fill="none" stroke-linecap="round"/>'
        # gold belly kanji 福 (fortune)
        f'<text x="48" y="92" text-anchor="middle" font-size="11" fill="#caa64b" '
        f'font-family="Hiragino Mincho ProN,serif">福</text>'
        f'</svg>'
    )


def _goal_card(g):
    svg = _daruma_svg(g["colour"], g.get("daruma_state", "none"))
    set_d = (g.get("committed_at") or "—")[:10]
    done_d = (g.get("achieved_at") or "—")[:10]
    won = g.get("daruma_state") == "both"
    return (
        f'<div class="card goal {"won" if won else ""}" data-goal-id="{g["id"]}">{svg}'
        f'<div class="meta"><h3>{_esc(g["title"])}</h3>'
        f'<p class="why">{_esc(g.get("why_it_matters", ""))}</p>'
        f'<p class="track"><span class="pill {g["colour"]}"></span> {g["colour"]}'
        f' · {g["owner"]} · set {set_d} · done {done_d}</p>'
        f'<div class="actions"><a href="/commit?id={g["id"]}">◑ commit</a>'
        f'<a href="/achieve?id={g["id"]}">● achieve</a></div></div></div>'
    )


def _swatches(checked="red"):
    out = ""
    for c, hx in COLOUR_HEX.items():
        chk = " checked" if c == checked else ""
        out += (f'<label class="swatch" title="{c}">'
                f'<input type="radio" name="colour" value="{c}"{chk}>'
                f'<span style="--c:{hx}"></span></label>')
    return f'<div class="swatches">{out}</div>'


def _goal_form():
    return (
        '<details class="adder"><summary>＋ new daruma</summary>'
        '<form method="post" action="/add-goal">'
        '<input name="title" placeholder="the goal" required maxlength="120">'
        '<input name="why" placeholder="why it matters" maxlength="200">'
        + _swatches() +
        '<button type="submit">place on the board</button></form></details>'
    )


EMA_TILTS = ["-2.2deg", "1.6deg", "-1.1deg", "2.4deg", "-1.8deg"]


def _ema(w, i=0):
    tilt = EMA_TILTS[i % len(EMA_TILTS)]
    return (
        f'<div class="ema" style="--tilt:{tilt}">'
        f'<p class="ema-text">{_esc(w["text"])}</p>'
        f'<p class="ema-date">{(w.get("created_at") or "")[:10]}</p>'
        f'<details class="ema-promote"><summary>→ daruma</summary>'
        f'<form method="post" action="/promote-wish">'
        f'<input type="hidden" name="id" value="{w["id"]}">'
        + _swatches() +
        f'<button type="submit">commit to it</button></form></details>'
        f'<a class="ema-release" href="/release-wish?id={w["id"]}" '
        f'title="let this wish go">release</a></div>'
    )


def _ema_wall(wishes):
    plaques = "".join(_ema(w, i) for i, w in enumerate(wishes)) \
        or '<p class="sub">No wishes hung yet.</p>'
    form = ('<details class="adder"><summary>＋ hang a wish</summary>'
            '<form method="post" action="/add-wish">'
            '<input name="text" placeholder="one day…" required maxlength="160">'
            '<button type="submit">hang it</button></form></details>')
    return f'<div class="ema-rail"></div><div class="ema-wall">{plaques}</div>{form}'


def _habit_card(h):
    s = h.get("streak", 0)
    flames = ("🔥 " + str(s) + (" day" if s == 1 else " days")) if s else "not yet started"
    done = h.get("done_today")
    tick = ('<span class="ticked">✓ done today</span>' if done
            else f'<a href="/habit-done?id={h["id"]}">✓ mark done</a>')
    return (
        f'<div class="card habit{" done-today" if done else ""}">'
        f'<div class="streak">{flames}</div>'
        f'<div class="meta"><h3>{_esc(h["name"])}</h3>'
        f'<p class="identity">{_esc(h.get("identity", ""))}</p>'
        f'<p class="track">cue: {_esc(h.get("cue", ""))} · {_esc(h.get("owner", ""))}</p>'
        f'<div class="actions">{tick}</div></div></div>'
    )


def _habit_form():
    return (
        '<details class="adder"><summary>＋ new habit</summary>'
        '<form method="post" action="/add-habit">'
        '<input name="name" placeholder="the habit" required maxlength="80">'
        '<input name="identity" placeholder="I am someone who…" required maxlength="160">'
        '<input name="cue" placeholder="cue (when I…)" maxlength="120">'
        '<button type="submit">begin the chain</button></form></details>'
    )


def _tokonoma(wins):
    if not wins:
        return '<p class="sub">The alcove awaits its first treasure.</p>'
    items = "".join(
        f'<div class="treasure"><span class="treasure-mark">◆</span>'
        f'<p>{_esc(w.get("content", ""))}</p>'
        f'<span class="treasure-date">{(w.get("created_at") or "")[:10]}</span></div>'
        for w in wins)
    return f'<div class="tokonoma">{items}</div>'


def _incense():
    return (
        '<div class="card incense" id="incense">'
        '<div class="incense-holder"><div class="incense-stick">'
        '<div class="incense-burn"></div><div class="incense-tip"></div></div>'
        '<div class="incense-smoke"><span></span><span></span><span></span></div></div>'
        '<div class="meta"><h3>Incense focus</h3>'
        '<p class="why">Light a stick. Work until it burns down.</p>'
        '<div class="actions incense-controls">'
        '<button data-incense="25">25 min</button>'
        '<button data-incense="50">50 min</button>'
        '<button data-incense-stop hidden>extinguish</button>'
        '<b class="incense-left" hidden></b></div></div></div>'
    )


AGENT_GLYPH = {"donna": "💁‍♀️", "nike": "💪", "esme": "🌱", "bodhi": "🗻",
               "unknown": "·", "cli": "🖥️"}


def _models_str(by_model):
    return ", ".join(
        f"{(k.split('-')[1] if '-' in k else k)}×{v}" for k, v in (by_model or {}).items())


def _agent_lines(by_agent):
    rows = ""
    for ag, d in (by_agent or {}).items():
        if not d.get("turns"):
            continue
        g = AGENT_GLYPH.get(ag, "·")
        rows += (f"<li>{g} <b>{ag}</b> · {d['turns']} turns · "
                 f"ctx {d['avg_context_per_turn']:,} · fresh {d['avg_fresh_input_per_turn']:,} · "
                 f"out {d['avg_output_per_turn']:,}</li>")
    return f"<ul class='recent'>{rows}</ul>" if rows else ""


def _chan_card(title, d):
    if not d or not d.get("turns"):
        return (f"<div class='card'><div class='meta'><h3>{title}</h3>"
                f"<p class='sub'>No turns recorded yet.</p></div></div>")
    heavy = d["avg_context_per_turn"] >= 60000 or d["avg_fresh_input_per_turn"] >= 5000
    verdict = ("⚠ heavy — turns carry a lot of context; trim CLAUDE.md / history"
               if heavy else "✓ lean — per-turn context is reasonable")
    return (
        f"<div class='card'><div class='meta'>"
        f"<h3>{title} · last {d['turns']} turns</h3>"
        f"<p class='track'>avg context/turn <b>{d['avg_context_per_turn']:,}</b> · "
        f"fresh input/turn <b class='{'hot' if heavy else ''}'>{d['avg_fresh_input_per_turn']:,}</b> · "
        f"output/turn {d['avg_output_per_turn']:,} · cache hit {d['cache_hit_rate']}%</p>"
        f"<p class='why'>{verdict} · models: {_models_str(d.get('by_model'))}</p>"
        f"{_agent_lines(d.get('by_agent'))}</div></div>"
    )


def _token_panel(summary):
    if not summary:
        return "<p class='sub'>No token data yet.</p>"
    return (_chan_card("📱 Telegram — the live bot", summary.get("telegram"))
            + _chan_card("🖥️ CLI — dev terminal", summary.get("cli")))


def _model_panel(current):
    btns = " ".join(
        (f"<b class='cur'>{m}</b>" if m == current else f"<a href='/set-model?m={m}'>{m}</a>")
        for m in MODELS)
    return (
        f"<div class='card model'><div class='meta'><h3>Daemon model</h3>"
        f"<p class='track'>current: <b>{current}</b> — switching auto-restarts the bot:</p>"
        f"<div class='actions big'>{btns}</div>"
        f"<div class='actions'><a class='restart' href='/restart'>⟳ restart daemon</a></div>"
        f"</div></div>"
    )


# Inline theme boot — runs before first paint so there is no flash of wrong theme.
BOOT_JS = (
    "(function(){try{var t=localStorage.getItem('daruma-theme')||"
    "(matchMedia('(prefers-color-scheme: dark)').matches?'twilight':"
    "(localStorage.getItem('daruma-light-pref')||'washi'));"
    "document.documentElement.dataset.theme=t;}catch(e){"
    "document.documentElement.setAttribute('data-theme','washi');}})();"
)


def render_board(goals, habits=None, tokens=None, model=None, wishes=None,
                 wins=None, today=None, celebrate=None, error=None):
    today = today or date.today()
    season = season_for(today)
    zen = zen_for(today)
    err = f'<p class="notice">{_esc(error)}</p>' if error else ""
    s = (
        '<header class="masthead"><div>'
        '<h1><span class="kanji">達磨</span> Daruma Board</h1>'
        f'<p class="sub season">{season["kanji"]} — {season["english"]}'
        ' · one eye to commit, one eye to arrive</p></div>'
        '<div class="theme-controls" role="group" aria-label="theme">'
        '<button data-theme-btn="washi" title="Washi — paper and ink">和</button>'
        '<button data-theme-btn="sakura" title="Sakura — blossom and koi">桜</button>'
        '<button data-theme-btn="twilight" title="Twilight temple — dark">月</button>'
        '</div></header>' + err +
        f'<section class="zen"><p class="zen-text">{_esc(zen["text"])}</p>'
        f'<p class="zen-by">{_esc(zen.get("by", ""))}</p></section>'
    )
    s += "<h2>Goals</h2>" + ("\n".join(_goal_card(g) for g in goals)
                             or "<p class='sub'>No goals yet.</p>")
    s += _goal_form()
    if wishes is not None:
        s += '<h2>Ema Wall <span class="jp">絵馬</span></h2>' + _ema_wall(wishes)
    if habits:
        s += "<h2>Habits</h2>" + "\n".join(_habit_card(h) for h in habits)
    s += _habit_form()
    if wins is not None:
        s += '<h2>Tokonoma <span class="jp">床の間</span></h2>' + _tokonoma(wins)
    s += '<h2>Focus <span class="jp">線香</span></h2>' + _incense()
    if tokens is not None:
        s += "<h2>Context efficiency</h2>" + _token_panel(tokens)
    if model is not None:
        s += "<h2>Daemon</h2>" + _model_panel(model)
    cel = f' data-celebrate="{_esc(celebrate)}"' if celebrate else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<script>{BOOT_JS}</script>"
        "<link rel='stylesheet' href='/static/style.css'>"
        "<title>達磨 Daruma Board</title></head>"
        f"<body{cel}><canvas id='particles'></canvas>"
        "<div class='koi-layer' aria-hidden='true'></div>"
        f"<main>{s}</main>"
        "<script src='/static/board.js'></script></body></html>"
    )
```

Also update `_enriched_habits` (later in the file) to drop its local import — `date` is now imported at the top:

```python
def _enriched_habits():
    h = _mod("habits")
    return [{**hb, "streak": h.streak(hb["id"]),
             "done_today": date.today().isoformat() in hb.get("log", [])}
            for hb in h.list_habits()]
```

Note the old `<meta http-equiv='refresh' content='30'>` is intentionally gone — a page refresh would reset the incense timer; `board.js` (Task 5) handles gentle reloads instead.

- [ ] **Step 4: Run the full dashboard test file**

Run: `python3 -m pytest tests/test_dashboard.py tests/test_goals.py -v`
Expected: all pass — including the pre-existing eye-fill and panel tests

- [ ] **Step 5: Commit**

```bash
cd /Users/grahamwilliamson/donna
git add personal-system/tools/dashboard.py personal-system/tests/test_dashboard.py
git commit -m "feat(dashboard): Japanese render revamp — real daruma, ema wall, tokonoma, zen, incense, theme switcher"
```

---

### Task 4: `style.css` — three themes, responsive, reduced-motion

**Files:**
- Create: `personal-system/tools/dashboard_assets/style.css`

- [ ] **Step 1: Write the stylesheet**

Create `personal-system/tools/dashboard_assets/style.css` with exactly this content:

```css
/* Daruma Board — washi 和 / sakura 桜 (light) + twilight temple 月 (dark) */
* { box-sizing: border-box; }

:root, html[data-theme="washi"] {
  --bg0:#f3ecdb; --bg1:#ece2cc; --bg2:#e4d8bd;
  --paper:#fbf7ec; --paper-edge:#ddd0b4; --ink:#3b3128; --muted:#8a7d6a; --soft:#6b5d49;
  --accent:#a63a2b; --gold:#b8923f; --link:#7a3326;
  --shadow:0 2px 12px rgba(80,60,30,.08); --glow:none;
  --rail0:#8a6a4a; --rail1:#6e5238; --ema0:#e8d3ae; --ema1:#d9bf92; --ema-ink:#4a3a28;
  --texture:repeating-linear-gradient(2deg, rgba(120,90,40,.018) 0 3px, transparent 3px 7px);
}
html[data-theme="sakura"] {
  --bg0:#fdf3f3; --bg1:#f6e7e7; --bg2:#dfe9e3;
  --paper:#fffcfb; --paper-edge:#eed7d7; --ink:#4a3a3a; --muted:#a98c8c; --soft:#7a5a5a;
  --accent:#d8616b; --gold:#d9a04a; --link:#b04a55;
  --shadow:0 3px 14px rgba(180,110,110,.13); --glow:none;
  --rail0:#a4795c; --rail1:#8a6248; --ema0:#f0ddbd; --ema1:#e2c79c; --ema-ink:#5a4632;
  --texture:none;
}
html[data-theme="twilight"] {
  --bg0:#10172a; --bg1:#1a2440; --bg2:#241f33;
  --paper:rgba(30,40,64,.92); --paper-edge:rgba(212,175,55,.28); --ink:#e7e3d6;
  --muted:#8d93b8; --soft:#bcb7a4; --accent:#d4af37; --gold:#d4af37; --link:#e0c468;
  --shadow:0 4px 18px rgba(0,0,0,.45); --glow:0 0 22px rgba(212,175,55,.07);
  --rail0:#3a2f24; --rail1:#2b2118; --ema0:#5a4732; --ema1:#483823; --ema-ink:#e8dcc0;
  --texture:none;
}

html { scroll-behavior: smooth; }
body {
  margin: 0; min-height: 100vh; color: var(--ink);
  font-family: -apple-system, "Hiragino Sans", system-ui, sans-serif;
  background: linear-gradient(180deg, var(--bg0), var(--bg1) 60%, var(--bg2));
  background-attachment: fixed;
  transition: background .6s ease, color .6s ease;
}
body::before { content:""; position:fixed; inset:0; pointer-events:none; background:var(--texture); }
/* torii + hills silhouette — twilight only */
html[data-theme="twilight"] body::after {
  content:""; position:fixed; left:0; right:0; bottom:0; height:140px; pointer-events:none; opacity:.55;
  background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 600 140'%3E%3Cg fill='%23070b16'%3E%3Cpath d='M0 140 L0 118 Q150 88 300 118 Q450 148 600 108 L600 140 Z'/%3E%3Crect x='430' y='42' width='8' height='98'/%3E%3Crect x='502' y='42' width='8' height='98'/%3E%3Cpath d='M414 30 Q470 18 526 30 L522 40 L418 40 Z'/%3E%3Crect x='424' y='50' width='92' height='7'/%3E%3Crect x='466' y='30' width='8' height='27'/%3E%3C/g%3E%3C/svg%3E") bottom center / 600px 140px repeat-x;
}
#particles { position:fixed; inset:0; z-index:0; pointer-events:none; }
.koi-layer { position:fixed; inset:0; z-index:0; pointer-events:none; overflow:hidden; }
main { position:relative; z-index:1; max-width:840px; margin:0 auto; padding:26px 18px 96px; }

/* ---------- masthead ---------- */
.masthead { display:flex; justify-content:space-between; align-items:flex-start; gap:14px; }
h1 { margin:0; font-family:"Hiragino Mincho ProN", Georgia, serif; font-weight:500;
     font-size:30px; letter-spacing:1px; }
h1 .kanji { color:var(--accent); margin-right:6px; }
.sub { color:var(--muted); margin:4px 0 18px; }
.season { font-size:13.5px; letter-spacing:.4px; }
h2 { font-size:13px; text-transform:uppercase; letter-spacing:3px; color:var(--muted);
     border-bottom:1px solid var(--paper-edge); padding-bottom:7px; margin:34px 0 14px;
     font-weight:600; }
h2 .jp { float:right; letter-spacing:1px; color:var(--gold); font-weight:400; }
.theme-controls { display:flex; gap:8px; }
.theme-controls button {
  width:40px; height:40px; border-radius:50%; border:1px solid var(--paper-edge);
  background:var(--paper); color:var(--ink); font-size:17px; cursor:pointer;
  font-family:"Hiragino Mincho ProN", serif; box-shadow:var(--shadow);
  transition:transform .25s ease, border-color .25s ease;
}
.theme-controls button:hover { transform:translateY(-2px); }
.theme-controls button.active { border-color:var(--accent); color:var(--accent); }
.notice { background:var(--paper); border:1px solid var(--accent); border-radius:10px;
          padding:10px 14px; color:var(--accent); }

/* ---------- zen card ---------- */
.zen { background:var(--paper); border:1px solid var(--paper-edge); border-radius:4px;
       border-left:4px solid var(--gold); box-shadow:var(--shadow), var(--glow);
       padding:18px 22px; margin-top:6px; }
.zen-text { margin:0; font-family:"Hiragino Mincho ProN", Georgia, serif;
            font-size:17px; line-height:1.65; }
.zen-by { margin:8px 0 0; color:var(--muted); font-size:12.5px; text-align:right;
          letter-spacing:1px; }

/* ---------- cards ---------- */
.card { display:flex; gap:18px; align-items:center; background:var(--paper);
        border:1px solid var(--paper-edge); border-radius:14px; padding:18px;
        margin:12px 0; box-shadow:var(--shadow), var(--glow);
        transition:background .6s ease, border-color .6s ease; }
.card.won { border-color:var(--gold); }
.card.won .meta h3::after { content:" 達成"; color:var(--gold); font-size:13px; }
.meta { flex:1; min-width:0; }
.meta h3 { margin:0 0 4px; font-size:17px; }
.why, .identity { color:var(--soft); margin:0 0 7px; }
.identity { font-style:italic; }
.track { color:var(--muted); font-size:12.5px; margin:0 0 9px; }
.track b { color:var(--soft); } .track b.hot { color:#c0392b; }
html[data-theme="twilight"] .track b.hot { color:#ff8a7a; }
.pill { display:inline-block; width:11px; height:11px; border-radius:50%; vertical-align:-1px; }
.pill.green{background:#2e7d32}.pill.purple{background:#6a1b9a}.pill.red{background:#c62828}
.pill.black{background:#212121}.pill.pink{background:#d81b60}.pill.gold{background:#f9a825}
.pill.white{background:#fafafa;border:1px solid #ccc}.pill.blue{background:#1565c0}
.actions a, .ticked, .cur { font-size:13px; text-decoration:none; color:var(--link); margin-right:14px; }
.actions.big a, .actions.big .cur { font-size:15px; font-weight:600; }
.cur { color:var(--gold); } .actions a:hover { text-decoration:underline; }
.ticked { color:var(--gold); } .restart { color:#c0392b; }
html[data-theme="twilight"] .restart { color:#ff8a7a; }
.streak { min-width:74px; text-align:center; font-size:15px; font-weight:600; color:#c0532a; }
html[data-theme="twilight"] .streak { color:#e8a06a; }
.recent { margin:6px 0 0; padding-left:18px; color:var(--muted); font-size:12px; }
.recent li { margin:2px 0; }

/* lantern breathing — twilight only */
html[data-theme="twilight"] .card { animation:lantern 7s ease-in-out infinite; }
@keyframes lantern {
  0%,100% { box-shadow:var(--shadow), 0 0 18px rgba(212,175,55,.05); }
  50%     { box-shadow:var(--shadow), 0 0 30px rgba(212,175,55,.11); }
}

/* daruma roly-poly wobble on hover */
.daruma { flex-shrink:0; transform-origin:50% 92%; }
.goal:hover .daruma { animation:wobble 1.5s ease-out; }
@keyframes wobble {
  0%{rotate:0deg} 18%{rotate:7deg} 38%{rotate:-5deg}
  58%{rotate:3deg} 78%{rotate:-1.5deg} 100%{rotate:0deg}
}

/* section entrance — brush-stroke fade */
main > * { animation:brush .7s ease both; }
main > *:nth-child(-n+4) { animation-delay:.05s; }
@keyframes brush { from { opacity:0; transform:translateY(10px); } to { opacity:1; transform:none; } }

/* ---------- forms ---------- */
.adder { margin:10px 0 4px; }
.adder summary { cursor:pointer; color:var(--muted); font-size:13.5px; letter-spacing:1px;
                 list-style:none; }
.adder summary::-webkit-details-marker { display:none; }
.adder summary:hover { color:var(--accent); }
.adder form { display:flex; flex-wrap:wrap; gap:10px; align-items:center;
              background:var(--paper); border:1px dashed var(--paper-edge);
              border-radius:12px; padding:14px; margin-top:10px; }
.adder input[type="text"], .adder input:not([type]) {
  flex:1 1 200px; background:transparent; border:none;
  border-bottom:1px solid var(--paper-edge); color:var(--ink); padding:7px 2px;
  font-size:14px; outline:none; }
.adder input:focus { border-bottom-color:var(--accent); }
.adder button, .incense-controls button {
  border:1px solid var(--accent); color:var(--accent); background:transparent;
  border-radius:18px; padding:7px 16px; font-size:13px; cursor:pointer;
  transition:background .2s ease, color .2s ease; }
.adder button:hover, .incense-controls button:hover { background:var(--accent); color:var(--paper); }
.swatches { display:flex; gap:7px; }
.swatch input { display:none; }
.swatch span { display:block; width:20px; height:20px; border-radius:50%;
               background:var(--c); border:2px solid transparent; cursor:pointer;
               box-shadow:inset 0 0 0 1px rgba(0,0,0,.15); }
.swatch input:checked + span { border-color:var(--ink); transform:scale(1.15); }

/* ---------- ema wall ---------- */
.ema-rail { height:10px; border-radius:5px; margin:4px 2px 0;
            background:linear-gradient(180deg, var(--rail0), var(--rail1));
            box-shadow:0 2px 5px rgba(0,0,0,.25); }
.ema-wall { display:flex; flex-wrap:wrap; gap:18px; padding:16px 8px 6px; }
.ema { position:relative; width:170px; padding:30px 14px 12px; rotate:var(--tilt, 0deg);
       background:linear-gradient(170deg, var(--ema0), var(--ema1));
       border:1px solid rgba(0,0,0,.14); border-radius:8px 8px 12px 12px;
       box-shadow:0 4px 9px rgba(0,0,0,.18); color:var(--ema-ink);
       clip-path:polygon(0 18px, 50% 0, 100% 18px, 100% 100%, 0 100%);
       transition:rotate .3s ease; }
.ema:hover { rotate:0deg; }
.ema::before { content:""; position:absolute; top:10px; left:50%; translate:-50% 0;
               width:7px; height:7px; border-radius:50%;
               background:rgba(0,0,0,.4); box-shadow:0 -9px 0 -2px rgba(0,0,0,.28); }
.ema-text { margin:0 0 6px; font-size:13.5px; line-height:1.45;
            font-family:"Hiragino Mincho ProN", Georgia, serif; }
.ema-date { margin:0 0 8px; font-size:10.5px; opacity:.6; }
.ema-promote summary { font-size:12px; cursor:pointer; color:inherit; opacity:.8; list-style:none; }
.ema-promote summary::-webkit-details-marker { display:none; }
.ema-promote form { display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; }
.ema-promote button { border:1px solid var(--ema-ink); color:var(--ema-ink);
                      background:transparent; border-radius:14px; padding:4px 10px;
                      font-size:11.5px; cursor:pointer; }
.ema-release { position:absolute; right:10px; bottom:8px; font-size:10.5px;
               color:inherit; opacity:.45; text-decoration:none; }
.ema-release:hover { opacity:.9; }

/* ---------- tokonoma ---------- */
.tokonoma { display:flex; gap:14px; overflow-x:auto; padding:16px;
            background:linear-gradient(180deg, var(--rail1), var(--rail0));
            border-radius:14px; box-shadow:inset 0 2px 10px rgba(0,0,0,.35); }
.treasure { flex:0 0 220px; background:var(--paper); border-radius:10px; padding:12px 14px;
            border:1px solid var(--paper-edge); box-shadow:var(--shadow); }
.treasure p { margin:6px 0; font-size:13.5px; line-height:1.5; }
.treasure-mark { color:var(--gold); font-size:12px; }
.treasure-date { color:var(--muted); font-size:11px; }

/* ---------- incense ---------- */
.incense-holder { position:relative; width:60px; height:130px; flex-shrink:0; }
.incense-stick { position:absolute; left:50%; translate:-50% 0; bottom:14px;
                 width:5px; height:100px; border-radius:3px;
                 background:linear-gradient(180deg, #6d5a3f, #4d3f2c); overflow:visible; }
.incense-burn { position:absolute; top:0; left:0; right:0; height:var(--burnt, 0%);
                background:var(--bg1); transition:height 1s linear; }
.incense-tip { position:absolute; top:var(--burnt, 0%); left:50%; translate:-50% -50%;
               width:9px; height:9px; border-radius:50%; opacity:0;
               background:radial-gradient(circle, #ffd27a, #e2654f 60%, transparent 70%); }
.incense.lit .incense-tip { opacity:1; animation:ember 1.6s ease-in-out infinite; }
@keyframes ember { 0%,100%{filter:brightness(1)} 50%{filter:brightness(1.6)} }
.incense-holder::after { content:""; position:absolute; bottom:0; left:50%; translate:-50% 0;
                width:44px; height:16px; border-radius:50%;
                background:linear-gradient(180deg, var(--rail0), var(--rail1)); }
.incense-smoke span { position:absolute; left:50%; bottom:118px; width:3px; height:3px;
                border-radius:50%; background:var(--muted); opacity:0; }
.incense.lit .incense-smoke span { animation:smoke 4s ease-out infinite; }
.incense.lit .incense-smoke span:nth-child(2) { animation-delay:1.3s; }
.incense.lit .incense-smoke span:nth-child(3) { animation-delay:2.6s; }
@keyframes smoke {
  0%   { opacity:0; transform:translate(0,0) scale(1); }
  15%  { opacity:.5; }
  100% { opacity:0; transform:translate(9px,-46px) scale(3.2); }
}
.incense-left { font-variant-numeric:tabular-nums; color:var(--gold); }

/* ---------- koi (sakura only) ---------- */
.koi { position:fixed; width:110px; opacity:0; z-index:0; offset-rotate:auto; }
html[data-theme="sakura"] .koi { opacity:.17; }
.koi-a { offset-path:path("M -150 620 C 300 520, 700 700, 1300 560 C 1700 470, 2100 620, 2400 560");
         animation:swim 85s linear infinite; }
.koi-b { offset-path:path("M 2400 760 C 1900 840, 1400 660, 800 780 C 400 860, 100 740, -150 780");
         animation:swim 110s linear infinite; width:80px; }
@keyframes swim { from { offset-distance:0%; } to { offset-distance:100%; } }
.koi svg { animation:koisway 2.6s ease-in-out infinite alternate; display:block; }
@keyframes koisway { from { transform:rotate(-4deg); } to { transform:rotate(4deg); } }

/* ---------- celebration ---------- */
.tassei { position:absolute; left:50%; top:30%; translate:-50% 0; z-index:5;
          font-family:"Hiragino Mincho ProN", serif; font-size:46px; color:var(--gold);
          text-shadow:0 2px 14px rgba(0,0,0,.25); pointer-events:none;
          animation:tassei 2.4s ease-out forwards; }
@keyframes tassei {
  0% { opacity:0; transform:translateY(18px) scale(.8); }
  25% { opacity:1; transform:translateY(0) scale(1.05); }
  70% { opacity:1; transform:translateY(-8px) scale(1); }
  100% { opacity:0; transform:translateY(-30px) scale(1); }
}
.burst-p { position:absolute; width:9px; height:9px; border-radius:60% 40% 55% 45%;
           pointer-events:none; z-index:4; }
.goal { position:relative; }

/* ---------- responsive + reduced motion ---------- */
@media (max-width: 700px) {
  main { padding:18px 12px 70px; }
  h1 { font-size:24px; }
  .card { flex-direction:row; gap:12px; padding:14px; }
  .daruma { width:72px; height:80px; }
  .ema { width:46%; min-width:150px; }
  .actions a { padding:6px 0; display:inline-block; }
  .theme-controls button { width:36px; height:36px; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation:none !important; transition:none !important; }
}
```

- [ ] **Step 2: Sanity-check the CSS parses**

Run: `python3 - <<'EOF'
import pathlib
css = pathlib.Path("tools/dashboard_assets/style.css").read_text()
assert css.count("{") == css.count("}"), "unbalanced braces"
assert all(t in css for t in ['data-theme="washi"', 'data-theme="sakura"', 'data-theme="twilight"'])
print("css ok,", css.count("{"), "blocks")
EOF`
Expected: `css ok, <n> blocks`

- [ ] **Step 3: Commit**

```bash
cd /Users/grahamwilliamson/donna
git add personal-system/tools/dashboard_assets/style.css
git commit -m "feat(dashboard): three-theme stylesheet — washi/sakura/twilight, ema wall, tokonoma, incense"
```

---

### Task 5: `board.js` — ambience, koi, celebration, bell, incense, auto-refresh

**Files:**
- Create: `personal-system/tools/dashboard_assets/board.js`

- [ ] **Step 1: Write the script**

Create `personal-system/tools/dashboard_assets/board.js` with exactly this content:

```javascript
/* Daruma Board ambience + interactions. Progressive enhancement — the board
   works without this file; this adds theme switching, ambient particles,
   koi, the achieve celebration, and the incense focus timer. */
(function () {
  'use strict';
  var reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  var doc = document.documentElement;

  /* ---------- theme switching ---------- */
  function applyTheme(t) {
    doc.dataset.theme = t;
    try {
      localStorage.setItem('daruma-theme', t);
      if (t !== 'twilight') localStorage.setItem('daruma-light-pref', t);
    } catch (e) {}
    markButtons();
    restartAmbience();
  }
  function markButtons() {
    document.querySelectorAll('[data-theme-btn]').forEach(function (b) {
      b.classList.toggle('active', b.dataset.themeBtn === doc.dataset.theme);
    });
  }
  document.querySelectorAll('[data-theme-btn]').forEach(function (b) {
    b.addEventListener('click', function () { applyTheme(b.dataset.themeBtn); });
  });
  markButtons();

  /* ---------- temple bell (WebAudio — no audio assets) ---------- */
  function bell(times) {
    try {
      var ctx = new (window.AudioContext || window.webkitAudioContext)();
      for (var i = 0; i < (times || 1); i++) {
        var t0 = ctx.currentTime + 0.05 + i * 1.4;
        [523.25, 1046.5, 1567.98].forEach(function (f, j) {
          var o = ctx.createOscillator(), g = ctx.createGain();
          o.type = 'sine'; o.frequency.value = f;
          g.gain.setValueAtTime(0.16 / (j + 1), t0);
          g.gain.exponentialRampToValueAtTime(0.0001, t0 + 1.3);
          o.connect(g); g.connect(ctx.destination);
          o.start(t0); o.stop(t0 + 1.3);
        });
      }
    } catch (e) {}
  }

  /* ---------- ambient particle layer ---------- */
  var canvas = document.getElementById('particles');
  var ctx2d = canvas ? canvas.getContext('2d') : null;
  var parts = [], raf = 0;
  function resize() {
    if (!canvas) return;
    canvas.width = innerWidth; canvas.height = innerHeight;
  }
  addEventListener('resize', resize);

  function rnd(a, b) { return a + Math.random() * (b - a); }

  function seasonNow() {
    var m = new Date().getMonth() + 1;
    return (m === 12 || m <= 2) ? 'winter' : m <= 5 ? 'spring' : m <= 8 ? 'summer' : 'autumn';
  }

  function mixFor(theme) {
    var s = seasonNow(), m = [];
    function add(kind, n) { while (n-- > 0) m.push(kind); }
    if (theme === 'washi') {
      add('mist', 3); add('mote', s === 'winter' ? 6 : 14);
      if (s === 'winter') add('snow', 16);
      if (s === 'summer') add('firefly', 5);
      if (s === 'autumn') add('momiji', 6);
    } else if (theme === 'sakura') {
      add('petal', s === 'autumn' ? 8 : 16);
      if (s === 'autumn') add('momiji', 10);
      if (s === 'winter') add('snow', 18);
      if (s === 'summer') add('firefly', 5);
    } else {
      add('star', 42); add('firefly', 8);
      if (s === 'winter') add('snow', 14);
    }
    if (innerWidth < 700) m = m.filter(function (_, i) { return i % 2 === 0; });
    return m;
  }

  function makeParticle(kind) {
    var p = { kind: kind, x: rnd(0, innerWidth), y: rnd(0, innerHeight), ph: rnd(0, 6.28) };
    if (kind === 'mote')    { p.r = rnd(1, 2.4); p.vx = rnd(-.08, .08); p.vy = rnd(-.05, -.16); p.a = rnd(.06, .14); }
    if (kind === 'mist')    { p.r = rnd(160, 320); p.vx = rnd(.03, .1); p.vy = 0; p.a = rnd(.025, .05); }
    if (kind === 'petal')   { p.r = rnd(3, 5.5); p.vx = rnd(.1, .4); p.vy = rnd(.3, .7); p.a = rnd(.5, .85); p.hue = Math.random() < .5 ? '#f4c6cf' : '#eaa6b4'; }
    if (kind === 'momiji')  { p.r = rnd(3.5, 6); p.vx = rnd(.1, .4); p.vy = rnd(.3, .65); p.a = rnd(.5, .8); p.hue = Math.random() < .5 ? '#c8552f' : '#d9763a'; }
    if (kind === 'snow')    { p.r = rnd(1, 2.6); p.vx = rnd(-.06, .06); p.vy = rnd(.18, .45); p.a = rnd(.3, .6); }
    if (kind === 'star')    { p.y = rnd(0, innerHeight * .65); p.r = rnd(.6, 1.6); p.vx = 0; p.vy = 0; p.a = rnd(.25, .7); p.tw = rnd(.4, 1.4); }
    if (kind === 'firefly') { p.r = rnd(1.4, 2.4); p.vx = rnd(-.25, .25); p.vy = rnd(-.18, .18); p.a = rnd(.4, .8); p.tw = rnd(.6, 1.6); }
    return p;
  }

  function step(p, t) {
    p.x += p.vx; p.y += p.vy;
    if (p.kind === 'petal' || p.kind === 'momiji') p.x += Math.sin(t / 900 + p.ph) * .3;
    if (p.kind === 'firefly') {
      p.vx += rnd(-.02, .02); p.vy += rnd(-.02, .02);
      p.vx = Math.max(-.3, Math.min(.3, p.vx)); p.vy = Math.max(-.3, Math.min(.3, p.vy));
    }
    if (p.x > innerWidth + 40) p.x = -30;
    if (p.x < -40) p.x = innerWidth + 30;
    if (p.y > innerHeight + 20) { p.y = -15; p.x = rnd(0, innerWidth); }
    if (p.y < -360) p.y = innerHeight + 10;
  }

  function draw(p, t) {
    var c = ctx2d;
    if (p.kind === 'mote') {
      c.globalAlpha = p.a; c.fillStyle = '#8c7850';
      c.beginPath(); c.arc(p.x, p.y, p.r, 0, 7); c.fill();
    } else if (p.kind === 'mist') {
      var g = c.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.r);
      g.addColorStop(0, 'rgba(200,185,150,' + p.a + ')');
      g.addColorStop(1, 'rgba(200,185,150,0)');
      c.globalAlpha = 1; c.fillStyle = g;
      c.beginPath(); c.arc(p.x, p.y, p.r, 0, 7); c.fill();
    } else if (p.kind === 'petal' || p.kind === 'momiji') {
      c.save(); c.translate(p.x, p.y); c.rotate(t / 1300 + p.ph);
      c.globalAlpha = p.a; c.fillStyle = p.hue;
      c.beginPath(); c.ellipse(0, 0, p.r, p.r * .62, 0, 0, 7); c.fill();
      c.restore();
    } else if (p.kind === 'snow') {
      c.globalAlpha = p.a; c.fillStyle = '#fff';
      c.beginPath(); c.arc(p.x, p.y, p.r, 0, 7); c.fill();
    } else if (p.kind === 'star') {
      c.globalAlpha = p.a * (0.55 + 0.45 * Math.sin(t / 1000 * p.tw + p.ph));
      c.fillStyle = '#f2e6c0';
      c.beginPath(); c.arc(p.x, p.y, p.r, 0, 7); c.fill();
    } else if (p.kind === 'firefly') {
      var a = p.a * (0.4 + 0.6 * Math.sin(t / 700 * p.tw + p.ph));
      var g2 = c.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.r * 5);
      g2.addColorStop(0, 'rgba(220,235,140,' + a + ')');
      g2.addColorStop(1, 'rgba(220,235,140,0)');
      c.globalAlpha = 1; c.fillStyle = g2;
      c.beginPath(); c.arc(p.x, p.y, p.r * 5, 0, 7); c.fill();
    }
  }

  function loop(t) {
    ctx2d.clearRect(0, 0, canvas.width, canvas.height);
    for (var i = 0; i < parts.length; i++) { step(parts[i], t); draw(parts[i], t); }
    raf = requestAnimationFrame(loop);
  }

  function restartAmbience() {
    if (!canvas || !ctx2d || reduced) return;
    cancelAnimationFrame(raf);
    resize();
    parts = mixFor(doc.dataset.theme).map(makeParticle);
    raf = requestAnimationFrame(loop);
    ensureKoi();
  }

  /* ---------- koi (sakura theme — CSS offset-path does the swimming) ---------- */
  var KOI =
    '<svg viewBox="0 0 120 60">' +
    '<path d="M14 30 C30 8 78 8 100 26 C106 30 106 30 100 34 C78 52 30 52 14 30 Z" fill="#f6f3ee"/>' +
    '<path d="M14 30 C8 18 2 16 4 28 C2 44 8 42 14 30 Z" fill="#f0958a"/>' +
    '<circle cx="88" cy="27" r="2.4" fill="#222"/>' +
    '<path d="M44 16 C54 14 64 18 70 24 C62 28 50 26 44 16 Z" fill="#e2654f"/>' +
    '<path d="M36 42 C44 40 54 42 60 38 C52 34 40 35 36 42 Z" fill="#e2654f" opacity=".85"/>' +
    '</svg>';
  function ensureKoi() {
    var layer = document.querySelector('.koi-layer');
    if (!layer) return;
    if (reduced || doc.dataset.theme !== 'sakura') { layer.innerHTML = ''; return; }
    if (!layer.children.length) {
      layer.innerHTML = '<div class="koi koi-a">' + KOI + '</div>' +
                        '<div class="koi koi-b">' + KOI + '</div>';
    }
  }

  /* ---------- achieve celebration ---------- */
  var celebrateId = document.body.dataset.celebrate;
  function celebrate(card) {
    bell(2);
    if (reduced || !card) return;
    var k = document.createElement('div');
    k.className = 'tassei'; k.textContent = '達成';
    card.appendChild(k);
    setTimeout(function () { k.remove(); }, 2600);
    var colours = ['#f4c6cf', '#eaa6b4', '#d4af37', '#f2e6c0', '#e2654f'];
    for (var i = 0; i < 26; i++) {
      var s = document.createElement('span');
      s.className = 'burst-p';
      s.style.background = colours[i % colours.length];
      s.style.left = '90px'; s.style.top = '60px';
      card.appendChild(s);
      var ang = rnd(0, 6.28), dist = rnd(60, 170);
      s.animate([
        { transform: 'translate(0,0) rotate(0deg)', opacity: 1 },
        { transform: 'translate(' + Math.cos(ang) * dist + 'px,' +
          (Math.sin(ang) * dist - 40) + 'px) rotate(' + rnd(-220, 220) + 'deg)', opacity: 0 }
      ], { duration: rnd(900, 1600), easing: 'cubic-bezier(.2,.6,.4,1)', fill: 'forwards' });
      setTimeout(function (el) { el.remove(); }.bind(null, s), 1700);
    }
  }
  if (celebrateId) {
    celebrate(document.querySelector('[data-goal-id="' + celebrateId + '"]'));
    history.replaceState(null, '', '/');
  }

  /* ---------- incense focus timer ---------- */
  var incense = document.getElementById('incense');
  if (incense) {
    var stick = incense.querySelector('.incense-stick');
    var leftEl = incense.querySelector('.incense-left');
    var stopBtn = incense.querySelector('[data-incense-stop]');
    var startBtns = incense.querySelectorAll('[data-incense]');
    var tick = null, endAt = 0, total = 0;
    function fmt(s) { return Math.floor(s / 60) + ':' + ('0' + Math.floor(s % 60)).slice(-2); }
    function update() {
      var s = (endAt - Date.now()) / 1000;
      if (s <= 0) { finish(); return; }
      leftEl.textContent = fmt(s);
      stick.style.setProperty('--burnt', (100 * (1 - s / total)) + '%');
    }
    function start(mins) {
      total = mins * 60; endAt = Date.now() + total * 1000;
      window.__incense = true;
      startBtns.forEach(function (b) { b.hidden = true; });
      stopBtn.hidden = false; leftEl.hidden = false;
      incense.classList.add('lit');
      tick = setInterval(update, 1000); update();
    }
    function reset() {
      clearInterval(tick); window.__incense = false;
      incense.classList.remove('lit');
      startBtns.forEach(function (b) { b.hidden = false; });
      stopBtn.hidden = true;
      stick.style.setProperty('--burnt', '0%');
    }
    function finish() { reset(); bell(2); leftEl.hidden = false; leftEl.textContent = '— done'; }
    startBtns.forEach(function (b) {
      b.addEventListener('click', function () { start(parseInt(b.dataset.incense, 10)); });
    });
    stopBtn.addEventListener('click', function () { reset(); leftEl.hidden = true; });
  }

  /* ---------- gentle auto-refresh (replaces the old meta refresh) ---------- */
  setInterval(function () {
    if (!window.__incense && !document.hidden && !document.querySelector('details[open]')) {
      location.reload();
    }
  }, 120000);

  restartAmbience();
})();
```

- [ ] **Step 2: Syntax-check**

Run: `node --check tools/dashboard_assets/board.js && echo OK`
Expected: `OK` (if node is unavailable: `python3 -c "print(open('tools/dashboard_assets/board.js').read().count('{') == open('tools/dashboard_assets/board.js').read().count('}'))"` → `True`)

- [ ] **Step 3: Commit**

```bash
cd /Users/grahamwilliamson/donna
git add personal-system/tools/dashboard_assets/board.js
git commit -m "feat(dashboard): ambience engine — particles, koi, temple bell, celebration, incense timer"
```

---

### Task 6: Routes — static assets, POST forms, celebrate, testable server

**Files:**
- Modify: `personal-system/tools/dashboard.py` (replace `def serve(...)` through end of file)
- Test: `personal-system/tests/test_dashboard.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `personal-system/tests/test_dashboard.py`:

```python
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("GOALS_PATH", str(tmp_path / "g.json"))
    monkeypatch.setenv("HABITS_PATH", str(tmp_path / "h.json"))
    monkeypatch.setenv("WISHES_PATH", str(tmp_path / "w.json"))
    monkeypatch.setenv("PMEM_DB", str(tmp_path / "memory.db"))
    dash = load_dashboard()
    srv = dash.make_server(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _post(url, **fields):
    data = urllib.parse.urlencode(fields).encode()
    return urllib.request.urlopen(urllib.request.Request(url, data=data)).read().decode()


def test_index_serves_with_daru_seed(server):
    page = urllib.request.urlopen(server + "/").read().decode()
    assert "Daruma Board" in page and "daru.life" in page


def test_static_served_and_traversal_blocked(server):
    res = urllib.request.urlopen(server + "/static/style.css")
    assert res.headers["Content-Type"].startswith("text/css")
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(server + "/static/%2e%2e%2fgoals.py")
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(server + "/static/dashboard.py")


def test_add_goal_and_wish_via_post(server):
    page = _post(server + "/add-goal", title="Climb Fuji", colour="green",
                 why="because it is there")
    assert "Climb Fuji" in page
    page = _post(server + "/add-wish", text="open a dojo")
    assert "open a dojo" in page


def test_promote_wish_via_post(server):
    _post(server + "/add-wish", text="run a marathon")
    page = _post(server + "/promote-wish", id="2", colour="green")  # id 1 = Daru seed
    assert "run a marathon" in page and 'data-state="none"' in page


def test_bad_colour_redirects_with_error(server):
    page = _post(server + "/add-goal", title="x", colour="teal", why="")
    assert "could not save" in page


def test_achieve_redirect_carries_celebrate(server):
    _post(server + "/add-goal", title="zz", colour="red", why="")
    page = urllib.request.urlopen(server + "/achieve?id=1").read().decode()
    assert 'data-celebrate="1"' in page
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_dashboard.py -v -k "server or static or via_post or celebrate_flag or error"`
Expected: FAIL — `AttributeError: module 'dashboard' has no attribute 'make_server'`

- [ ] **Step 3: Replace the server half of `dashboard.py`**

Delete from `def serve(port=8765):` through the end of the file and replace with:

```python
ASSET_TYPES = {".css": "text/css; charset=utf-8",
               ".js": "application/javascript; charset=utf-8",
               ".json": "application/json", ".svg": "image/svg+xml"}


def make_handler(g, hmod, tok, wmod, ev):
    import http.server
    import urllib.parse

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _redirect(self, loc="/"):
            self.send_response(303)
            self.send_header("Location", loc)
            self.end_headers()

        def _serve_static(self, name):
            target = (ASSETS / name).resolve()
            if (target.parent != ASSETS.resolve()
                    or target.suffix not in ASSET_TYPES or not target.is_file()):
                return self.send_error(404)
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ASSET_TYPES[target.suffix])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path.startswith("/static/"):
                return self._serve_static(urllib.parse.unquote(u.path[len("/static/"):]))
            if u.path == "/commit" and "id" in q:
                g.commit_goal(int(q["id"][0]))
                return self._redirect()
            if u.path == "/achieve" and "id" in q:
                gid = int(q["id"][0])
                g.achieve_goal(gid)
                return self._redirect(f"/?celebrate={gid}")
            if u.path == "/habit-done" and "id" in q:
                hmod.log_done(int(q["id"][0]))
                return self._redirect()
            if u.path == "/release-wish" and "id" in q:
                wmod.release_wish(int(q["id"][0]))
                return self._redirect()
            if u.path == "/set-model" and "m" in q:
                _set_model(q["m"][0])
                return self._redirect()
            if u.path == "/restart":
                _restart_daemon()
                return self._redirect()
            page = render_board(
                g.list_goals(), _enriched_habits(), tok.summary(recent=50),
                _current_model(), wishes=wmod.list_wishes(),
                wins=ev.surface_evidence(limit=8),
                celebrate=q.get("celebrate", [None])[0],
                error=q.get("error", [None])[0])
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode()
            fields = {k: v[0].strip() for k, v in urllib.parse.parse_qs(raw).items()}

            def need(*names):
                for n in names:
                    if not fields.get(n):
                        raise ValueError(f"{n} required")
            try:
                if self.path == "/add-goal":
                    need("title", "colour")
                    g.add_goal(fields["title"], fields["colour"], fields.get("why", ""))
                elif self.path == "/add-habit":
                    need("name", "identity")
                    hmod.add_habit(fields["name"], fields["identity"], fields.get("cue", ""))
                elif self.path == "/add-wish":
                    need("text")
                    wmod.add_wish(fields["text"])
                elif self.path == "/promote-wish":
                    need("id", "colour")
                    wmod.promote_wish(int(fields["id"]), fields["colour"])
                else:
                    return self.send_error(404)
            except (ValueError, KeyError) as e:
                return self._redirect("/?error=" + urllib.parse.quote(f"could not save: {e}"))
            self._redirect()

    return Handler


def make_server(port=8765):
    import http.server
    g = _mod("goals")
    hmod = _mod("habits")
    tok = _mod("tokens")
    wmod = _mod("wishes")
    ev = _mod("evidence")
    wmod.seed_defaults()
    return http.server.HTTPServer(("127.0.0.1", port),
                                  make_handler(g, hmod, tok, wmod, ev))


def serve(port=8765):
    srv = make_server(port)
    print(f"Daruma board → http://localhost:{srv.server_port}  (Ctrl-C to stop)")
    srv.serve_forever()


if __name__ == "__main__":
    import sys
    # Optional port override (env or argv) so a second instance — e.g. a
    # separate TradeAlly board — can run alongside this one on another port.
    # Bound to 127.0.0.1, which is shared across all local macOS user
    # accounts, so either account can view either board at localhost:<port>.
    port = int(os.environ.get("DARUMA_PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8765))
    serve(port)
```

- [ ] **Step 4: Run the entire suite**

Run: `python3 -m pytest tests/ -v`
Expected: all pass (wishes, dashboard render, HTTP routes, goals, habits, everything pre-existing)

- [ ] **Step 5: Commit**

```bash
cd /Users/grahamwilliamson/donna
git add personal-system/tools/dashboard.py personal-system/tests/test_dashboard.py
git commit -m "feat(dashboard): static assets route, add/promote/release endpoints, celebrate redirect, testable server"
```

---

### Task 7: Live verification — all three themes

**Files:** none (manual verification)

- [ ] **Step 1: Start the board on a scratch port**

Run (background): `cd /Users/grahamwilliamson/donna/personal-system && python3 tools/dashboard.py 8766`

- [ ] **Step 2: Verify in a real browser (Playwright MCP)**

1. Navigate to `http://localhost:8766` — expect the washi board (or twilight if the OS is in dark mode), the season subtitle, the zen line, the seeded Daru ema.
2. Screenshot. Click `桜` — petals should fall, koi should glide. Screenshot.
3. Click `月` — stars/fireflies, lantern glow, torii silhouette. Screenshot.
4. Open `＋ new daruma`, add a test goal, confirm the daruma renders blank-eyed; `◑ commit` fills the left eye.
5. `● achieve` it — expect the 達成 kanji, the petal burst, the bell, and the goal appearing in the Tokonoma.
6. Start a 25-min incense, confirm the ember lights and the countdown runs; extinguish it.
7. Check the browser console for errors — expect none.
8. Resize to ~390px width — single column, everything tappable.

- [ ] **Step 3: Clean up the test goal/wish state**

The verification goal/achievement wrote to the real `goals.json`/evidence ledger. Remove the test goal entry from `_shared/_state/goals.json` by hand (it's JSON — delete the test object) and note the evidence row id if Graham wants it pruned.

- [ ] **Step 4: Stop the scratch server, run the full suite one last time**

Run: `python3 -m pytest tests/ -q`
Expected: all pass

- [ ] **Step 5: Final commit (if any fixes were needed)**

```bash
cd /Users/grahamwilliamson/donna
git add -A personal-system
git commit -m "fix(dashboard): polish from live verification"
```

## Done-when

- All three themes switch instantly with no flash, persist across reloads, and dark mode follows the OS by default.
- Each theme has its signature ambient animation; `prefers-reduced-motion` disables all of it.
- Daruma look like daruma; eyes fill on commit/achieve; achieving triggers 達成 + bell + tokonoma entry.
- Wishes hang on the ema wall, promote into daruma, release without deletion; Daru (daru.life) is seeded.
- Goals/habits/wishes can be added from the board; all 12 pre-existing + new tests pass.

## Out of scope (later)

- Internet/Tailscale access and the Daru SaaS (wish-listed on the ema wall).
- Habit heatmap; morning "today" panel.
- Editing existing goal/habit text.
