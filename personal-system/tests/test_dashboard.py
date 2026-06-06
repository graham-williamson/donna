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
