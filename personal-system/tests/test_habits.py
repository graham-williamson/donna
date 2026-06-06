import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("habits", ROOT / "tools" / "habits.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_add_habit(tmp_path):
    h = load()
    hp = str(tmp_path / "h.json")
    out = h.add_habit("Morning Calm", "I am someone who begins in stillness",
                      cue="when I wake", owner="bodhi", habits_path=hp)
    assert out["owner"] == "bodhi" and out["identity"].startswith("I am") and out["log"] == []


def test_log_done_idempotent(tmp_path):
    h = load()
    hp = str(tmp_path / "h.json")
    g = h.add_habit("x", "id", habits_path=hp)
    h.log_done(g["id"], day="2026-06-05", habits_path=hp)
    h.log_done(g["id"], day="2026-06-05", habits_path=hp)
    assert h.list_habits(hp)[0]["log"] == ["2026-06-05"]


def test_streak(tmp_path):
    h = load()
    hp = str(tmp_path / "h.json")
    g = h.add_habit("x", "id", habits_path=hp)
    for d in ("2026-06-03", "2026-06-04", "2026-06-05"):
        h.log_done(g["id"], day=d, habits_path=hp)
    assert h.streak(g["id"], today="2026-06-05", habits_path=hp) == 3


def test_streak_breaks_on_gap(tmp_path):
    h = load()
    hp = str(tmp_path / "h.json")
    g = h.add_habit("x", "id", habits_path=hp)
    h.log_done(g["id"], day="2026-06-03", habits_path=hp)
    h.log_done(g["id"], day="2026-06-05", habits_path=hp)
    assert h.streak(g["id"], today="2026-06-05", habits_path=hp) == 1


def test_due_today(tmp_path):
    h = load()
    hp = str(tmp_path / "h.json")
    g = h.add_habit("x", "id", habits_path=hp)
    assert any(x["id"] == g["id"] for x in h.due_today(today="2026-06-05", habits_path=hp))
    h.log_done(g["id"], day="2026-06-05", habits_path=hp)
    assert not any(x["id"] == g["id"] for x in h.due_today(today="2026-06-05", habits_path=hp))


def test_seed_morning_calm(tmp_path):
    h = load()
    hp = str(tmp_path / "h.json")
    assert len(h.seed_defaults(habits_path=hp)) == 1
    assert h.seed_defaults(habits_path=hp) == []  # idempotent
    assert any(x["name"] == "Morning Calm" for x in h.list_habits(hp))
