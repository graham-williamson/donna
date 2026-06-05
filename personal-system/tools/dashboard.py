#!/usr/bin/env python3
"""Daruma board dashboard — coloured dolls with fillable eyes + a set/complete tracker.

Launch on demand (NOT a daemon):  python3 tools/dashboard.py
Serves an interactive board on http://localhost:8765 — click to fill the left eye
(commit) or the right eye (achieve). Reads/writes goals.json via tools/goals.py.
"""
import pathlib
import importlib.util

ROOT = pathlib.Path(__file__).resolve().parents[1]

COLOUR_HEX = {
    "green": "#2e7d32", "purple": "#6a1b9a", "red": "#c62828", "black": "#212121",
    "pink": "#d81b60", "gold": "#f9a825", "white": "#fafafa", "blue": "#1565c0",
}


def _daruma_svg(colour, state):
    hexc = COLOUR_HEX.get(colour, "#888888")
    left = '<circle cx="28" cy="42" r="3.5" fill="#111"/>' if state in ("left", "both") else ""
    right = '<circle cx="52" cy="42" r="3.5" fill="#111"/>' if state == "both" else ""
    return (
        f'<svg width="80" height="90" viewBox="0 0 80 90" data-state="{state}">'
        f'<ellipse cx="40" cy="52" rx="34" ry="36" fill="{hexc}" stroke="#333" stroke-width="2"/>'
        f'<circle cx="28" cy="42" r="7" fill="#fff" stroke="#333"/>'
        f'<circle cx="52" cy="42" r="7" fill="#fff" stroke="#333"/>'
        f'{left}{right}</svg>'
    )


def render_board(goals):
    cards = []
    for g in goals:
        svg = _daruma_svg(g["colour"], g.get("daruma_state", "none"))
        set_d = (g.get("committed_at") or "—")[:10]
        done_d = (g.get("achieved_at") or "—")[:10]
        cards.append(
            f'<div class="goal" data-id="{g["id"]}">{svg}'
            f'<div class="meta"><h3>{g["title"]}</h3>'
            f'<p class="why">{g.get("why_it_matters", "")}</p>'
            f'<p class="track">set: {set_d} · done: {done_d} · owner: {g["owner"]} · {g["colour"]}</p>'
            f'<a href="/commit?id={g["id"]}">commit (left eye)</a> &nbsp; '
            f'<a href="/achieve?id={g["id"]}">achieve (right eye)</a>'
            f'</div></div>'
        )
    body = "\n".join(cards) or "<p>No goals yet — add one via the crew or the CLI.</p>"
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Daruma Board</title>"
        "<style>body{font-family:-apple-system,system-ui,sans-serif;background:#f4f1ea;"
        "color:#222;padding:24px;max-width:760px;margin:auto}"
        ".goal{display:flex;gap:18px;align-items:center;background:#fff;border-radius:14px;"
        "padding:18px;margin:14px 0;box-shadow:0 1px 6px rgba(0,0,0,.08)}"
        ".meta h3{margin:0 0 4px}.why{color:#555;margin:0 0 6px}"
        ".track{color:#888;font-size:13px;margin:0 0 8px}"
        "a{color:#6a1b9a;font-size:13px;text-decoration:none}a:hover{text-decoration:underline}"
        "</style></head><body><h1>\U0001F3AF Daruma Board</h1>" + body + "</body></html>"
    )


def _goals_mod():
    spec = importlib.util.spec_from_file_location("goals", ROOT / "tools" / "goals.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def serve(port=8765):
    import http.server
    import urllib.parse
    g = _goals_mod()

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
            body = render_board(g.list_goals()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

    print(f"Daruma board → http://localhost:{port}  (Ctrl-C to stop)")
    http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    serve()
