import importlib.util
import pathlib
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_add_derives_owner_from_colour(tmp_path):
    goals = load("goals")
    g = goals.add_goal("Deadlift 140", "green", goals_path=str(tmp_path / "g.json"))
    assert g["owner"] == "nike" and g["daruma_state"] == "none" and g["committed_at"] is None


def test_commit_fills_left_eye(tmp_path):
    goals = load("goals")
    gp = str(tmp_path / "g.json")
    g = goals.add_goal("x", "purple", goals_path=gp)
    c = goals.commit_goal(g["id"], goals_path=gp)
    assert c["daruma_state"] == "left" and c["committed_at"]


def test_achieve_fills_both_and_logs_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("PMEM_DB", str(tmp_path / "memory.db"))
    goals = load("goals")
    gp = str(tmp_path / "g.json")
    g = goals.add_goal("Run 10k", "green", goals_path=gp)
    goals.commit_goal(g["id"], goals_path=gp)
    a = goals.achieve_goal(g["id"], goals_path=gp)
    assert a["daruma_state"] == "both" and a["achieved_at"] and a["evidence"]
    ev = load("evidence")
    assert any("Run 10k" in r["content"] for r in ev.surface_evidence())


def test_invalid_colour(tmp_path):
    goals = load("goals")
    with pytest.raises(ValueError):
        goals.add_goal("x", "teal", goals_path=str(tmp_path / "g.json"))


def test_list_filters(tmp_path):
    goals = load("goals")
    gp = str(tmp_path / "g.json")
    goals.add_goal("a", "green", goals_path=gp)
    goals.add_goal("b", "purple", goals_path=gp)
    assert len(goals.list_goals(colour="green", goals_path=gp)) == 1


def test_burn_requires_achievement_then_archives(tmp_path, monkeypatch):
    monkeypatch.setenv("PMEM_DB", str(tmp_path / "memory.db"))
    goals = load("goals")
    gp = str(tmp_path / "g.json")
    g = goals.add_goal("x", "red", goals_path=gp)
    with pytest.raises(ValueError):
        goals.burn_goal(g["id"], goals_path=gp)      # not yet achieved — no kuyo
    goals.achieve_goal(g["id"], goals_path=gp)
    b = goals.burn_goal(g["id"], goals_path=gp)
    assert b["burned_at"]
    assert goals.list_goals(goals_path=gp)[0]["burned_at"]  # archived, never deleted
