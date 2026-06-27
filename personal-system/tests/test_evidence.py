import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_evidence():
    spec = importlib.util.spec_from_file_location("evidence", ROOT / "tools" / "evidence.py")
    e = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(e)
    return e


def test_evidence_log_and_surface(tmp_path, monkeypatch):
    monkeypatch.setenv("PMEM_DB", str(tmp_path / "memory.db"))
    ev = load_evidence()
    ev.log_win("shipped the memory engine")
    out = [r["content"] for r in ev.surface_evidence()]
    assert "shipped the memory engine" in out
