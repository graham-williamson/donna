#!/usr/bin/env python3
"""Daruma board dashboard — coloured goal dolls with fillable eyes, an
Atomic-Habits panel with streaks, a context-efficiency (token) panel, and a
daemon model switcher + restart button (so you never touch bash for it).

Launch on demand (NOT a daemon):  python3 tools/dashboard.py
Serves on http://localhost:8765. Reads/writes via goals.py, habits.py; reads
tokens.py; switches the daemon model via the launchd plist + launchctl.
"""
import os
import pathlib
import subprocess
import plistlib
import importlib.util

ROOT = pathlib.Path(__file__).resolve().parents[1]

COLOUR_HEX = {
    "green": "#2e7d32", "purple": "#6a1b9a", "red": "#c62828", "black": "#212121",
    "pink": "#d81b60", "gold": "#f9a825", "white": "#fafafa", "blue": "#1565c0",
}

MODELS = ["haiku", "sonnet", "opus"]
PLIST = os.path.expanduser("~/Library/LaunchAgents/com.user.claude-telegram.plist")
LABEL = "com.user.claude-telegram"


def _daruma_svg(colour, state):
    hexc = COLOUR_HEX.get(colour, "#888888")
    left = '<circle cx="34" cy="48" r="4.5" fill="#111"/>' if state in ("left", "both") else ""
    right = '<circle cx="58" cy="48" r="4.5" fill="#111"/>' if state == "both" else ""
    return (
        f'<svg width="96" height="108" viewBox="0 0 96 108" data-state="{state}">'
        f'<ellipse cx="48" cy="60" rx="42" ry="46" fill="{hexc}" stroke="#2a1f1f" stroke-width="2.5"/>'
        f'<ellipse cx="48" cy="54" rx="26" ry="28" fill="#f6efe1"/>'
        f'<circle cx="34" cy="48" r="9" fill="#fff" stroke="#2a1f1f" stroke-width="1.6"/>'
        f'<circle cx="58" cy="48" r="9" fill="#fff" stroke="#2a1f1f" stroke-width="1.6"/>'
        f'{left}{right}'
        f'<path d="M40 70 q8 7 16 0" stroke="#2a1f1f" stroke-width="1.6" fill="none"/>'
        f'</svg>'
    )


def _goal_card(g):
    svg = _daruma_svg(g["colour"], g.get("daruma_state", "none"))
    set_d = (g.get("committed_at") or "—")[:10]
    done_d = (g.get("achieved_at") or "—")[:10]
    won = g.get("daruma_state") == "both"
    return (
        f'<div class="card {"won" if won else ""}">{svg}'
        f'<div class="meta"><h3>{g["title"]}</h3>'
        f'<p class="why">{g.get("why_it_matters", "")}</p>'
        f'<p class="track"><span class="pill {g["colour"]}"></span> {g["colour"]}'
        f' · {g["owner"]} · set {set_d} · done {done_d}</p>'
        f'<div class="actions"><a href="/commit?id={g["id"]}">◑ commit</a>'
        f'<a href="/achieve?id={g["id"]}">● achieve</a></div></div></div>'
    )


def _habit_card(h):
    s = h.get("streak", 0)
    flames = ("🔥 " + str(s) + (" day" if s == 1 else " days")) if s else "not yet started"
    done = h.get("done_today")
    tick = ('<span class="ticked">✓ done today</span>' if done
            else f'<a href="/habit-done?id={h["id"]}">✓ mark done</a>')
    return (
        f'<div class="card habit{" done-today" if done else ""}">'
        f'<div class="streak">{flames}</div>'
        f'<div class="meta"><h3>{h["name"]}</h3>'
        f'<p class="identity">{h.get("identity", "")}</p>'
        f'<p class="track">cue: {h.get("cue", "")} · {h.get("owner", "")}</p>'
        f'<div class="actions">{tick}</div></div></div>'
    )


def _token_panel(t):
    if not t or not t.get("turns"):
        return "<p class='sub'>No token data yet.</p>"
    heavy = t["avg_fresh_input_per_turn"] >= 4000
    verdict = ("⚠ heavy — context may be bloating; check the per-turn injection"
               if heavy else "✓ lean — context cost is reasonable")
    recent = "".join(
        f"<li>{x['ctx']:,} ctx · {x['out']:,} out · "
        f"{x['model'].split('-')[1] if '-' in x['model'] else x['model']}</li>"
        for x in t.get("last", []))
    models = ", ".join(
        f"{(k.split('-')[1] if '-' in k else k)}×{v}" for k, v in t.get("by_model", {}).items())
    return (
        f"<div class='card'><div class='meta'>"
        f"<h3>Context efficiency · last {t['turns']} turns</h3>"
        f"<p class='track'>avg context/turn <b>{t['avg_context_per_turn']:,}</b> · "
        f"fresh input/turn <b class='{'hot' if heavy else ''}'>{t['avg_fresh_input_per_turn']:,}</b> · "
        f"output/turn {t['avg_output_per_turn']:,} · cache hit {t['cache_hit_rate']}%</p>"
        f"<p class='why'>{verdict} · models: {models}</p>"
        f"<ul class='recent'>{recent}</ul></div></div>"
    )


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


STYLE = """
:root{--ink:#2a1f1f;--paper:#efe7d6}*{box-sizing:border-box}
body{font-family:-apple-system,system-ui,"Hiragino Sans",sans-serif;color:var(--ink);
 background:radial-gradient(circle at 20% 0%,#f6efe1,var(--paper));margin:0;padding:32px 18px;max-width:780px;margin-inline:auto}
h1{margin:0 0 4px}.sub{color:#8a7d6a;margin:0 0 22px}
h2{font-size:14px;text-transform:uppercase;letter-spacing:2px;color:#8a7d6a;border-bottom:1px solid #d8ccb6;padding-bottom:6px;margin:30px 0 14px}
.card{display:flex;gap:18px;align-items:center;background:#fffdf8;border:1px solid #e6dcc6;border-radius:16px;padding:18px;margin:12px 0;box-shadow:0 2px 10px rgba(60,40,20,.06)}
.card.won{background:linear-gradient(#fffdf8,#f3f8ee);border-color:#cfe6c4}
.card.done-today{background:linear-gradient(#fffdf8,#f0f4fb)}
.meta{flex:1}.meta h3{margin:0 0 4px;font-size:17px}
.why,.identity{color:#6b5d49;margin:0 0 7px}.identity{font-style:italic}
.track{color:#9a8c76;font-size:12.5px;margin:0 0 9px}.track b{color:#5b4d39}.track b.hot{color:#c0392b}
.pill{display:inline-block;width:11px;height:11px;border-radius:50%;vertical-align:-1px}
.pill.green{background:#2e7d32}.pill.purple{background:#6a1b9a}.pill.red{background:#c62828}
.pill.black{background:#212121}.pill.pink{background:#d81b60}.pill.gold{background:#f9a825}
.pill.white{background:#fafafa;border:1px solid #ccc}.pill.blue{background:#1565c0}
.actions a,.ticked,.cur{font-size:13px;text-decoration:none;color:#6a1b9a;margin-right:14px}
.actions.big a,.actions.big .cur{font-size:15px;font-weight:600}
.cur{color:#2e7d32}.actions a:hover{text-decoration:underline}.ticked{color:#2e7d32}
.restart{color:#c0392b}.streak{min-width:74px;text-align:center;font-size:15px;font-weight:600;color:#c0532a}
.recent{margin:6px 0 0;padding-left:18px;color:#9a8c76;font-size:12px}.recent li{margin:2px 0}
"""


def render_board(goals, habits=None, tokens=None, model=None):
    s = "<h1>🎯 Daruma Board</h1><p class='sub'>One eye to commit. One eye to arrive.</p>"
    s += "<h2>Goals</h2>" + ("\n".join(_goal_card(g) for g in goals) or "<p class='sub'>No goals yet.</p>")
    if habits:
        s += "<h2>Habits</h2>" + "\n".join(_habit_card(h) for h in habits)
    if tokens is not None:
        s += "<h2>Context efficiency</h2>" + _token_panel(tokens)
    if model is not None:
        s += "<h2>Daemon</h2>" + _model_panel(model)
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Daruma Board</title><style>{STYLE}</style></head><body>{s}</body></html>")


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _enriched_habits():
    from datetime import date
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
            html = render_board(g.list_goals(), _enriched_habits(),
                                tok.summary(recent=50), _current_model())
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

    print(f"Daruma board → http://localhost:{port}  (Ctrl-C to stop)")
    http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    serve()
