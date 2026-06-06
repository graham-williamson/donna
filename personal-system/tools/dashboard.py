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


def serve(port=8765):
    import http.server
    import urllib.parse
    g = _mod("goals")
    hmod = _mod("habits")
    tok = _mod("tokens")

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _redirect(self):
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path == "/commit" and "id" in q:
                g.commit_goal(int(q["id"][0]))
                return self._redirect()
            if u.path == "/achieve" and "id" in q:
                g.achieve_goal(int(q["id"][0]))
                return self._redirect()
            if u.path == "/habit-done" and "id" in q:
                hmod.log_done(int(q["id"][0]))
                return self._redirect()
            if u.path == "/set-model" and "m" in q:
                _set_model(q["m"][0])
                return self._redirect()
            if u.path == "/restart":
                _restart_daemon()
                return self._redirect()
            model = _current_model()
            html = render_board(g.list_goals(), _enriched_habits(),
                                tok.summary(recent=50), model)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

    print(f"Daruma board → http://localhost:{port}  (Ctrl-C to stop)")
    http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    import sys
    # Optional port override (env or argv) so a second instance — e.g. a
    # separate TradeAlly board — can run alongside this one on another port.
    # Bound to 127.0.0.1, which is shared across all local macOS user
    # accounts, so either account can view either board at localhost:<port>.
    port = int(os.environ.get("DARUMA_PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8765))
    serve(port)
