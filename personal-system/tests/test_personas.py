import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_dispatch():
    spec = importlib.util.spec_from_file_location("dispatch", ROOT / "tools" / "dispatch.py")
    d = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(d)
    return d


def test_each_persona_overlay_loads():
    d = load_dispatch()
    for pid, name in [("donna", "Donna"), ("nike", "Nike"),
                      ("esme", "Esme"), ("bodhi", "Bodhi")]:
        ctx = d.assemble_context(pid)
        assert name in ctx and len(ctx) > 200


def test_esme_has_safety_boundary():
    t = (ROOT / "personas" / "esme" / "PERSONA.md").read_text().lower()
    assert "not a clinician" in t and "crisis" in t


def test_bodhi_never_religion():
    t = (ROOT / "personas" / "bodhi" / "PERSONA.md").read_text().lower()
    assert "never" in t and "religion" in t
