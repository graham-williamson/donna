import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_dashboard():
    spec = importlib.util.spec_from_file_location("dashboard", ROOT / "tools" / "dashboard.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_render_shows_colour_and_title():
    dash = load_dashboard()
    html = dash.render_board([{
        "id": 1, "title": "Deadlift 140kg", "colour": "green", "owner": "nike",
        "why_it_matters": "strength", "daruma_state": "left",
        "committed_at": "2026-06-05", "achieved_at": None}])
    assert "Deadlift 140kg" in html and "#2e7d32" in html and 'data-state="left"' in html


def test_render_eye_fills():
    dash = load_dashboard()
    none = dash.render_board([{"id": 1, "title": "x", "colour": "red",
                               "owner": "shared", "daruma_state": "none"}])
    both = dash.render_board([{"id": 2, "title": "y", "colour": "red",
                               "owner": "shared", "daruma_state": "both"}])
    assert none.count('fill="#111"') == 0 and both.count('fill="#111"') == 2


def test_render_habits_panel():
    dash = load_dashboard()
    html = dash.render_board([], [{
        "id": 1, "name": "Morning Calm", "identity": "I am someone who begins in stillness",
        "cue": "when I wake", "owner": "bodhi", "streak": 3, "done_today": False}])
    assert "Morning Calm" in html and "🔥 3 days" in html and "kept today" in html


def test_render_token_panel():
    dash = load_dashboard()
    html = dash.render_board([], None, {
        "telegram": {
            "turns": 12, "avg_context_per_turn": 90000, "avg_fresh_input_per_turn": 6000,
            "avg_output_per_turn": 300, "cache_hit_rate": 40,
            "by_model": {"claude-haiku-4-5": 12},
            "by_agent": {"nike": {
                "turns": 5, "avg_context_per_turn": 90000, "avg_fresh_input_per_turn": 6000,
                "avg_output_per_turn": 300, "cache_hit_rate": 40, "by_model": {}}},
            "last": []},
        "cli": {
            "turns": 3, "avg_context_per_turn": 200000, "avg_fresh_input_per_turn": 1000,
            "avg_output_per_turn": 500, "cache_hit_rate": 90, "by_model": {}, "last": []},
    })
    # both channels labelled, the agent named, the heavy verdict shown
    assert "Telegram" in html and "CLI" in html
    assert "nike" in html and "90,000" in html and "heavy" in html


def test_render_model_panel():
    dash = load_dashboard()
    html = dash.render_board([], None, None, "haiku")
    assert "Daemon model" in html and "restart daemon" in html and "/set-model?m=sonnet" in html


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


# ---- live-feedback round (2026-06-06): meanings, habit wording, next-action, engawa ----

def test_goal_actions_show_only_next_step():
    dash = load_dashboard()
    fresh = dash.render_board([{"id": 1, "title": "x", "colour": "red",
                                "owner": "shared", "daruma_state": "none"}])
    committed = dash.render_board([{"id": 2, "title": "y", "colour": "red",
                                    "owner": "shared", "daruma_state": "left"}])
    won = dash.render_board([{"id": 3, "title": "z", "colour": "red",
                              "owner": "shared", "daruma_state": "both"}])
    assert "/commit?id=1" in fresh and "/achieve?id=1" not in fresh
    assert "/achieve?id=2" in committed and "/commit?id=2" not in committed
    assert "/commit?id=3" not in won and "/achieve?id=3" not in won


def test_colour_meanings_surface():
    dash = load_dashboard()
    page = dash.render_board([{"id": 1, "title": "x", "colour": "green",
                               "owner": "nike", "daruma_state": "none"}])
    assert "health" in page                       # green's meaning on the card
    assert "wealth" in page                       # gold's meaning in swatch titles


def test_habits_are_kept_not_done():
    dash = load_dashboard()
    page = dash.render_board([], [{
        "id": 1, "name": "Morning Calm", "identity": "i", "cue": "c",
        "owner": "bodhi", "streak": 0, "done_today": False}])
    assert "mark done" not in page and "not yet started" not in page
    assert "kept today" in page and "not yet begun" in page


def test_main_board_is_zen_panels_live_on_engawa(server):
    main_page = urllib.request.urlopen(server + "/").read().decode()
    assert "Daemon model" not in main_page and "Context efficiency" not in main_page
    assert 'href="/engawa"' in main_page          # quiet doorway in the footer
    engawa = urllib.request.urlopen(server + "/engawa").read().decode()
    assert "Daemon model" in engawa and "Context efficiency" in engawa


# ---- polish round: reference-grade daruma + selection meanings ----

def test_daruma_belly_kanji_carries_meaning():
    dash = load_dashboard()
    green = dash.render_board([{"id": 1, "title": "x", "colour": "green",
                                "owner": "nike", "daruma_state": "none"}])
    purple = dash.render_board([{"id": 1, "title": "x", "colour": "purple",
                                 "owner": "esme", "daruma_state": "none"}])
    assert "健" in green and "志" in purple


def test_swatch_shows_selected_meaning():
    dash = load_dashboard()
    page = dash.render_board([])
    assert '<em class="meaning">' in page and "love & connection" in page


def test_burned_daruma_leave_the_board_and_won_offer_kuyo():
    dash = load_dashboard()
    won = dash.render_board([{"id": 5, "title": "won", "colour": "red",
                              "owner": "shared", "daruma_state": "both"}])
    assert "/burn?id=5" in won and "temple" in won
    gone = dash.render_board([{"id": 6, "title": "ashes", "colour": "red",
                               "owner": "shared", "daruma_state": "both",
                               "burned_at": "2026-06-06T00:00:00Z"}])
    assert "ashes" not in gone
