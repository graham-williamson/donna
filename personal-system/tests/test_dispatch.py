import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_dispatch():
    spec = importlib.util.spec_from_file_location("dispatch", ROOT / "tools" / "dispatch.py")
    d = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(d)
    return d


def test_parse_address_glyph():
    d = load_dispatch()
    assert d.parse_address("🌱 I feel like a fraud") == ("esme", "I feel like a fraud")
    assert d.parse_address("💪 leg day?") == ("nike", "leg day?")
    assert d.parse_address("🗻 what is enough") == ("bodhi", "what is enough")


def test_parse_address_name():
    d = load_dispatch()
    assert d.parse_address("Esme, I'm anxious") == ("esme", "I'm anxious")
    assert d.parse_address("bodhi: meaning?") == ("bodhi", "meaning?")
    assert d.parse_address("Donna do the thing") == ("donna", "do the thing")


def test_parse_address_none():
    d = load_dispatch()
    assert d.parse_address("just a normal message") == (None, "just a normal message")
    assert d.parse_address("Hello there") == (None, "Hello there")


def test_route_default_is_donna(tmp_path):
    d = load_dispatch()
    assert d.route("hi", state_path=tmp_path / "s.json")["persona"] == "donna"


def test_route_sticky(tmp_path):
    d = load_dispatch()
    sp = tmp_path / "s.json"
    d.route("🌱 hello", state_path=sp)
    res = d.route("still talking", state_path=sp)
    assert res["persona"] == "esme" and res["switched"] is False


def test_route_reply_to_overrides(tmp_path):
    d = load_dispatch()
    sp = tmp_path / "s.json"
    d.route("🌱 hello", state_path=sp)
    res = d.route("about that workout", reply_to="nike", state_path=sp)
    assert res["persona"] == "nike" and res["switched"] is True


def test_route_strips_address(tmp_path):
    d = load_dispatch()
    sp = tmp_path / "s.json"
    res = d.route("Esme, I'm worried", state_path=sp)
    assert res["persona"] == "esme" and res["text"] == "I'm worried"


def test_state_persists(tmp_path):
    d = load_dispatch()
    sp = tmp_path / "s.json"
    d.save_state("bodhi", sp)
    assert d.load_state(sp) == "bodhi"


def test_assemble_context_fallback_header():
    d = load_dispatch()
    ctx = d.assemble_context("esme")
    assert "Esme" in ctx and "🌱" in ctx


def test_assemble_context_includes_recall():
    d = load_dispatch()
    ctx = d.assemble_context("nike", recall="- [training] deadlift PB")
    assert "Nike" in ctx and "deadlift PB" in ctx
