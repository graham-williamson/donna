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
    assert "Morning Calm" in html and "🔥 3 days" in html and "mark done" in html


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
