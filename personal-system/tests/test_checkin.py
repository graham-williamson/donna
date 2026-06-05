import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_skill_contract_layers():
    t = (ROOT / "personas" / "nike" / "skills" / "morning-checkin" / "SKILL.md").read_text()
    assert "INPUTS" in t and "PROCESS" in t and "OUTPUTS" in t


def test_gather_inputs(tmp_path, monkeypatch):
    monkeypatch.setenv("PMEM_DB", str(tmp_path / "memory.db"))
    gp = str(tmp_path / "g.json")
    goals = load("goals")
    pmem = load("pmem")
    g = goals.add_goal("Run 10k", "green", goals_path=gp)
    goals.commit_goal(g["id"], goals_path=gp)
    pmem.add({"kind": "observation", "owner": "nike", "content": "slept 5h", "topics": ["energy"]})
    checkin = load("checkin")
    res = checkin.gather_inputs(goals_path=gp)
    assert len(res["active_goals"]) == 1
    assert any("slept 5h" in r["content"] for r in res["recent_energy"])


def test_log_response(tmp_path, monkeypatch):
    monkeypatch.setenv("PMEM_DB", str(tmp_path / "memory.db"))
    checkin = load("checkin")
    checkin.log_response("legs are toast", energy="low")
    pmem = load("pmem")
    contents = [r["content"] for r in pmem.recall(topic="energy", persona="nike", limit=10)]
    assert "legs are toast" in contents and any("low" in c for c in contents)
