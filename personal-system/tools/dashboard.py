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
import os
import json
import html
import pathlib
import subprocess
import plistlib
import importlib.util
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSETS = ROOT / "tools" / "dashboard_assets"

COLOUR_HEX = {
    "green": "#2e7d32", "purple": "#6a1b9a", "red": "#c62828", "black": "#212121",
    "pink": "#d81b60", "gold": "#f9a825", "white": "#fafafa", "blue": "#1565c0",
}

MODELS = ["haiku", "sonnet", "opus"]
PLIST = os.path.expanduser("~/Library/LaunchAgents/com.user.claude-telegram.plist")
LABEL = "com.user.claude-telegram"

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


GOLD = "#f0c25c"

# The belly kanji IS the colour's meaning, brushed onto the doll itself.
COLOUR_KANJI = {
    "red": "福", "gold": "富", "white": "始", "black": "守",
    "green": "健", "purple": "志", "pink": "愛", "blue": "学",
}
_SVG_BODY = {"white": "#efece3"}   # a pure-white body would swallow the mask
_SVG_DECO = {"gold": "#7c4a12"}    # gold brushwork needs contrast on a gold body


def _daruma_svg(colour, state):
    body = _SVG_BODY.get(colour) or COLOUR_HEX.get(colour, "#888888")
    deco = _SVG_DECO.get(colour, GOLD)
    kanji = COLOUR_KANJI.get(colour, "福")
    # eyes: a bold open ring until filled; pupils are the only fill="#111" (test contract)
    left = ('<circle cx="38.5" cy="45" r="11.5" fill="#111"/>' if state in ("left", "both") else
            '<circle cx="38.5" cy="45" r="9.5" fill="none" stroke="#1a1a1a" stroke-width="4.6"/>')
    right = ('<circle cx="81.5" cy="45" r="11.5" fill="#111"/>' if state == "both" else
             '<circle cx="81.5" cy="45" r="9.5" fill="none" stroke="#1a1a1a" stroke-width="4.6"/>')
    return (
        f'<svg class="daruma" width="104" height="90" viewBox="0 0 120 104" data-state="{state}">'
        # wide, round-bottomed body — flat vector, no outline
        f'<path d="M60 4 C92 4 114 25 114 57 C114 86 91 100 60 100 '
        f'C29 100 6 86 6 57 C6 25 28 4 60 4 Z" fill="{body}"/>'
        f'<ellipse cx="45" cy="22" rx="30" ry="12" fill="#fff" opacity=".12"/>'
        f'<ellipse cx="60" cy="93" rx="44" ry="10" fill="#000" opacity=".07"/>'
        # white goggle mask across both eyes
        f'<circle cx="38.5" cy="45" r="25" fill="#fffdf7"/>'
        f'<circle cx="81.5" cy="45" r="25" fill="#fffdf7"/>'
        f'<rect x="38.5" y="22" width="43" height="46" fill="#fffdf7"/>'
        f'{left}{right}'
        # gold brush flourishes flanking the belly
        f'<path d="M26 66 q-9 11 -3 26" stroke="{deco}" stroke-width="5.6" '
        f'stroke-linecap="round" fill="none"/>'
        f'<path d="M36 69 q-7 9 -2 20" stroke="{deco}" stroke-width="4.8" '
        f'stroke-linecap="round" fill="none"/>'
        f'<path d="M94 66 q9 11 3 26" stroke="{deco}" stroke-width="5.6" '
        f'stroke-linecap="round" fill="none"/>'
        f'<path d="M84 69 q7 9 2 20" stroke="{deco}" stroke-width="4.8" '
        f'stroke-linecap="round" fill="none"/>'
        # the meaning, written on the belly
        f'<text x="60" y="92" text-anchor="middle" font-size="27" font-weight="700" '
        f'fill="{deco}" font-family="Hiragino Mincho ProN,Yu Mincho,serif">{kanji}</text>'
        f'</svg>'
    )


# Traditional daruma colour symbolism — each doll's colour carries its wish.
COLOUR_MEANING = {
    "red": "luck & protection", "gold": "wealth & prosperity",
    "white": "balance & beginnings", "black": "wards off misfortune",
    "green": "health & vitality", "purple": "growth & self-mastery",
    "pink": "love & connection", "blue": "work & learning",
}


def _goal_card(g):
    state = g.get("daruma_state", "none")
    svg = _daruma_svg(g["colour"], state)
    set_d = (g.get("committed_at") or "—")[:10]
    done_d = (g.get("achieved_at") or "—")[:10]
    won = state == "both"
    meaning = COLOUR_MEANING.get(g["colour"], g["colour"])
    # one daruma, one next step — never both eyes' actions at once
    if state == "none":
        action = f'<a href="/commit?id={g["id"]}">◑ commit</a>'
    elif state == "left":
        action = f'<a href="/achieve?id={g["id"]}">● achieve</a>'
    else:
        action = ""
    actions = f'<div class="actions">{action}</div>' if action else ""
    return (
        f'<div class="card goal {"won" if won else ""}" data-goal-id="{g["id"]}">{svg}'
        f'<div class="meta"><h3>{_esc(g["title"])}</h3>'
        f'<p class="why">{_esc(g.get("why_it_matters", ""))}</p>'
        f'<p class="track"><span class="pill {g["colour"]}"></span> {meaning}'
        f' · {g["owner"]} · set {set_d} · done {done_d}</p>'
        f'{actions}</div></div>'
    )


def _swatches(checked="red"):
    out = ""
    for c, hx in COLOUR_HEX.items():
        chk = " checked" if c == checked else ""
        meaning = COLOUR_MEANING.get(c, c)
        out += (f'<label class="swatch" title="{c} — {meaning}">'
                f'<input type="radio" name="colour" value="{c}"{chk}>'
                f'<span style="--c:{hx}"></span>'
                # only the checked swatch's meaning is shown (pure CSS)
                f'<em class="meaning">{COLOUR_KANJI.get(c, "")} {meaning}</em></label>')
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
    # a habit is a practice, never "done" — today it is kept, or not yet
    s = h.get("streak", 0)
    flames = ("🔥 " + str(s) + (" day" if s == 1 else " days")) if s else "not yet begun"
    done = h.get("done_today")
    tick = ('<span class="ticked">kept today ✓</span>' if done
            else f'<a href="/habit-done?id={h["id"]}">✓ kept today</a>')
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


THEME_CONTROLS = (
    '<div class="theme-controls" role="group" aria-label="theme">'
    '<button data-theme-btn="washi" title="Washi — paper and ink">和</button>'
    '<button data-theme-btn="sakura" title="Sakura — blossom and koi">桜</button>'
    '<button data-theme-btn="twilight" title="Twilight temple — dark">月</button>'
    '</div>'
)


def _page(body, title="達磨 Daruma Board", celebrate=None):
    cel = f' data-celebrate="{_esc(celebrate)}"' if celebrate else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<script>{BOOT_JS}</script>"
        "<link rel='stylesheet' href='/static/style.css'>"
        # favicon: a tiny daruma, no asset file needed
        "<link rel='icon' href=\"data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' "
        "viewBox='0 0 120 104'><path d='M60 4 C92 4 114 25 114 57 C114 86 91 100 60 100 "
        "C29 100 6 86 6 57 C6 25 28 4 60 4 Z' fill='%23c0392b'/>"
        "<circle cx='38.5' cy='45' r='25' fill='%23fffdf7'/>"
        "<circle cx='81.5' cy='45' r='25' fill='%23fffdf7'/>"
        "<rect x='38.5' y='22' width='43' height='46' fill='%23fffdf7'/>"
        "<circle cx='38.5' cy='45' r='11.5' fill='%23111111'/>"
        "<circle cx='81.5' cy='45' r='9.5' fill='none' stroke='%23111111' stroke-width='4.6'/>"
        "<text x='60' y='92' text-anchor='middle' font-size='27' font-weight='700' "
        "fill='%23d8a94e' font-family='serif'>志</text></svg>\">"
        f"<title>{title}</title></head>"
        f"<body{cel}><canvas id='particles'></canvas>"
        "<div class='koi-layer' aria-hidden='true'></div>"
        f"<main>{body}</main>"
        "<script src='/static/board.js'></script></body></html>"
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
        + THEME_CONTROLS + '</header>' + err +
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
    # The system panels live on the engawa — the quiet veranda off the main room.
    if tokens is not None:
        s += "<h2>Context efficiency</h2>" + _token_panel(tokens)
    if model is not None:
        s += "<h2>Daemon</h2>" + _model_panel(model)
    s += ('<footer class="engawa-door">'
          '<a href="/engawa" title="the engawa — system panels, off the main room">縁側</a>'
          '</footer>')
    return _page(s, celebrate=celebrate)


def render_engawa(tokens, model):
    s = (
        '<header class="masthead"><div>'
        '<h1><span class="kanji">縁側</span> Engawa</h1>'
        '<p class="sub">the veranda behind the house — where the machinery hums</p></div>'
        + THEME_CONTROLS + '</header>'
    )
    s += "<h2>Context efficiency</h2>" + _token_panel(tokens)
    s += "<h2>Daemon</h2>" + _model_panel(model)
    s += '<footer class="engawa-door"><a href="/">← back to the board</a></footer>'
    return _page(s, title="縁側 Engawa — Daruma Board")


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _enriched_habits():
    h = _mod("habits")
    return [{**hb, "streak": h.streak(hb["id"]),
             "done_today": date.today().isoformat() in hb.get("log", [])}
            for hb in h.list_habits()]


def _current_model():
    try:
        with open(PLIST, "rb") as f:
            pl = plistlib.load(f)
        return pl.get("EnvironmentVariables", {}).get("TELEGRAM_ROUTER_MODEL", "sonnet")
    except Exception:
        return "sonnet"


def _restart_daemon():
    uid = str(os.getuid())
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", PLIST], capture_output=True)


def _set_model(m):
    if m not in MODELS:
        return
    with open(PLIST, "rb") as f:
        pl = plistlib.load(f)
    pl.setdefault("EnvironmentVariables", {})["TELEGRAM_ROUTER_MODEL"] = m
    with open(PLIST, "wb") as f:
        plistlib.dump(pl, f)
    _restart_daemon()


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
            if u.path == "/engawa":
                page = render_engawa(tok.summary(recent=50), _current_model())
            else:
                page = render_board(
                    g.list_goals(), _enriched_habits(),
                    wishes=wmod.list_wishes(),
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
