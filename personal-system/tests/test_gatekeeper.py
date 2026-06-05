import importlib.util
import pathlib
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_interrupt_sends(tmp_path):
    gk = load("gatekeeper")
    r = gk.propose("donna", "urgent", "interrupt", queue_path=str(tmp_path / "q.json"))
    assert r["action"] == "send"


def test_digest_queues_and_drains(tmp_path):
    gk = load("gatekeeper")
    q = str(tmp_path / "q.json")
    gk.propose("esme", "gentle nudge", "digest", queue_path=q)
    gk.propose("bodhi", "a reflection", "digest", queue_path=q)
    assert len(gk.drain_digest(queue_path=q)) == 2
    assert gk.drain_digest(queue_path=q) == []


def test_silent(tmp_path):
    gk = load("gatekeeper")
    r = gk.propose("nike", "fyi", "silent", queue_path=str(tmp_path / "q.json"))
    assert r["action"] == "silent"


def test_bad_tier(tmp_path):
    gk = load("gatekeeper")
    with pytest.raises(ValueError):
        gk.propose("donna", "x", "shout", queue_path=str(tmp_path / "q.json"))
